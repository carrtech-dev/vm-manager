"""Interactive guest PTY transport; no shell input or output is persisted."""

import asyncio
import json
import uuid
from urllib.parse import quote, urlsplit

from fastapi import WebSocketDisconnect
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosedOK

from .hypervisor import HypervisorError


# SELinux services cannot transition straight into a logged-in user domain.
# PID 1 -> getty -> login provides the distribution's normal policy transitions.
# Label only this disposable PTY; PAM labels it again for the authenticated user.
LOGIN_SCRIPT = r'''set -eu
if [ -x /bin/login ]; then login=/bin/login
elif [ -x /usr/bin/login ]; then login=/usr/bin/login
else printf 'Guest login program unavailable. Use the graphical console.\n'; exit 1
fi
if [ -e /sys/fs/selinux/enforce ]; then
    for tool in systemd-run systemctl chcon tty; do
        command -v "$tool" >/dev/null || {
            printf 'SELinux guest login requires systemd-run, systemctl, chcon and tty. Use the graphical console.\n'
            exit 1
        }
    done
    if [ -x /sbin/agetty ]; then getty=/sbin/agetty
    elif [ -x /usr/sbin/agetty ]; then getty=/usr/sbin/agetty
    else printf 'SELinux guest login requires agetty. Use the graphical console.\n'; exit 1
    fi
    chcon -t user_devpts_t "$(tty)"
    cleanup() { systemctl stop "$1" >/dev/null 2>&1 || :; }
    trap 'cleanup "$1"' EXIT
    trap 'exit 0' HUP INT TERM
    systemd-run --quiet --wait --collect --pipe --service-type=exec --unit="$1" \
        -- "$getty" --noreset --noclear --noissue --login-program "$login" - xterm-256color <&0 &
    wait $!
else
    exec "$login"
fi
'''


def terminal_message(raw):
    if len(raw.encode('utf-8')) > 131072:
        raise ValueError('Terminal message too large')
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError('Invalid terminal message')
    if message.get('type') == 'ack':
        return 'ack', None
    if message.get('type') == 'input':
        data = message.get('data')
        if not isinstance(data, str) or len(data.encode('utf-8')) > 65536:
            raise ValueError('Invalid terminal input')
        return 'input', data.encode('utf-8')
    if message.get('type') == 'resize':
        cols, rows = message.get('cols'), message.get('rows')
        if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 1 <= rows <= 200):
            raise ValueError('Invalid terminal size')
        return 'resize', json.dumps({'command': 'window-resize', 'args': {
            'width': str(cols), 'height': str(rows)}})
    raise ValueError('Unknown terminal message')


def kubernetes_command(pod=None):
    command = ['/usr/bin/setpriv', '--reuid=vmm-kube', '--regid=vmm-kube', '--init-groups',
               '/usr/bin/env', 'HOME=/home/vmm-kube', 'KUBECONFIG=/home/vmm-kube/.kube/config',
               'K3S_CONFIG_FILE=/dev/null']
    if pod:
        return command + ['/usr/local/bin/k3s', 'kubectl', 'exec', '--stdin', '--tty', '--namespace', pod.namespace,
                          pod.name, '--container', pod.container, '--', pod.shell]
    return command + ['PS1=kubectl> ', '/bin/bash', '--noprofile', '--norc', '-i']


async def bridge(ws, incus, vm_name, authorize, audit, auth_interval=20, mode="login", pod=None):
    operation = None
    data_socket = control = None
    pending = []
    await ws.accept()
    try:
        vm = await asyncio.to_thread(incus.vm, vm_name)
        if vm.get('type') != 'virtual-machine' or vm.get('status', '').lower() != 'running':
            raise HypervisorError('A running virtual machine is required')
        authorize()
        # Login owns guest authentication (including PAM and password echo).
        # Never fall back to a shell when login is unavailable or fails.
        command = (['/bin/sh', '-c', LOGIN_SCRIPT, 'vmm-login',
                    'vmm-terminal-' + uuid.uuid4().hex + '.service']
                   if mode == 'login' else
                   ['/bin/sh', '-c', 'if [ -x /bin/bash ]; then exec /bin/bash -l; else exec /bin/sh -l; fi'])
        if mode == 'kubernetes':
            command = kubernetes_command(pod)
        operation = await asyncio.to_thread(incus.request, 'POST',
            '/1.0/instances/' + quote(vm_name) + '/exec', {
                'command': command,
                'interactive': True, 'wait-for-websocket': True,
                'record-output': False, 'width': 100, 'height': 30,
                'environment': ({'TERM': 'xterm-256color', 'HOME': '/root', 'USER': 'root'}
                                if mode in ('root', 'kubernetes') else {'TERM': 'xterm-256color'}),
                'user': 0, 'group': 0, 'cwd': '/root' if mode == 'root' else '/',
            }, False)
        url = urlsplit(operation['operation'])
        base = 'ws://incus' + url.path + '/websocket?'
        suffix = '&' + url.query if url.query else ''
        fds = operation['metadata']['metadata']['fds']
        control = await unix_connect(incus.socket, uri=base + 'secret=' + fds['control'] + suffix,
                                     max_size=131072, ping_interval=20, ping_timeout=20)
        data_socket = await unix_connect(incus.socket, uri=base + 'secret=' + fds['0'] + suffix,
                                         max_size=1024 * 1024, ping_interval=20, ping_timeout=20)
        authorize()
        audit()
        await ws.send_json({'type': 'ready', 'mode': mode})
        output_credit = asyncio.Event()
        output_credit.set()
        had_input = False
        guest_ended = False

        async def browser_to_guest():
            nonlocal had_input
            while True:
                raw = await ws.receive_text()
                authorize()
                kind, payload = terminal_message(raw)
                if guest_ended:
                    continue
                if kind == 'ack':
                    output_credit.set()
                else:
                    if kind == 'input' and payload:
                        had_input = True
                    await (data_socket if kind == 'input' else control).send(payload)

        async def guest_to_browser():
            nonlocal guest_ended
            while True:
                # One output frame in flight bounds the browser's pending
                # parser queue as well as its retained scrollback buffer.
                await output_credit.wait()
                try:
                    chunk = await data_socket.recv()
                except ConnectionClosedOK:
                    break
                authorize()
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode('utf-8')
                output_credit.clear()
                await ws.send_bytes(chunk)
            # A guest logout can hang up the PTY itself (notably on SELinux).
            # Confirm exec completion before opening a fresh login PTY. login
            # can return the user's nonzero shell status. A failed launcher
            # without interaction must stop, so setup failures cannot retry-loop.
            if mode == 'login':
                guest_ended = True
                # Incus completes the exec operation after its channels close.
                await control.close()
                await data_socket.close()
                path = url.path + '/wait?timeout=5' + ('&' + url.query if url.query else '')
                result = await asyncio.to_thread(incus.request, 'GET', path, None, False)
                metadata = result.get('metadata') or {}
                result_code = (metadata.get('metadata') or {}).get('return')
                if (metadata.get('status_code') != 200 or type(result_code) is not int or
                        (result_code != 0 and not had_input)):
                    raise HypervisorError('Guest login did not complete successfully')
                authorize()
                await ws.send_json({'type': 'exit'})

        async def validate_session():
            while True:
                await asyncio.sleep(auth_interval)
                authorize()

        pending = [asyncio.create_task(fn()) for fn in
                   (browser_to_guest, guest_to_browser, validate_session)]
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            completed.result()
    except (WebSocketDisconnect, ConnectionClosedOK):
        pass
    except ValueError:
        try:
            await ws.close(code=1008, reason='Invalid terminal message')
        except Exception:
            pass
    except Exception:
        try:
            await ws.send_json({'type': 'error', 'message':
                'Terminal unavailable. Check that the VM is running, its Incus agent is connected, and your session is valid.'})
        except Exception:
            pass
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if control:
            try:
                await control.send(json.dumps({'command': 'signal', 'signal': 15}))
            except Exception:
                pass
            try:
                await control.close()
            except Exception:
                pass
        if data_socket:
            try:
                await data_socket.close()
            except Exception:
                pass
        if operation:
            try:
                await asyncio.to_thread(incus.request, 'DELETE', operation['operation'], None, False)
            except Exception:
                pass  # A completed exec operation is no longer cancellable.
        try:
            await ws.close(code=1000)
        except Exception:
            pass
