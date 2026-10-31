"""Exercise the guest launcher's shell control flow without touching host policy."""
import os
import re
import shlex
import subprocess
import time

import pytest

from app.guest_terminal import LOGIN_SCRIPT


def launcher(tmp_path, *, selinux=True, label_ok=True, block=False):
    def executable(name, body):
        path = tmp_path / name
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o700)
        return shlex.quote(str(path))

    login = executable('login', 'echo login > "$CHECK_DIR/login-called"\n')
    getty = executable('agetty', 'exit 0\n')
    executable('tty', 'echo /dev/pts/123\n')
    executable('chcon', 'printf "%s\\n" "$@" > "$CHECK_DIR/label"\nexit ' + ('0' if label_ok else '1') + '\n')
    executable('systemctl', '''printf '%s\\n' "$@" > "$CHECK_DIR/stop"
if [ -f "$CHECK_DIR/runner-pid" ]; then
    kill -TERM "$(cat "$CHECK_DIR/runner-pid")" 2>/dev/null || :
fi
''')
    executable('systemd-run', '''printf '%s\\n' "$@" > "$CHECK_DIR/run"
echo $$ > "$CHECK_DIR/runner-pid"
if [ "$CHECK_BLOCK" = 1 ]; then
    trap 'exit 0' TERM HUP
    while :; do sleep 0.02; done
fi
exit 0
''')
    probe = tmp_path / 'enforce'
    if selinux:
        probe.touch()
    script = LOGIN_SCRIPT.replace('/sys/fs/selinux/enforce', shlex.quote(str(probe)))
    replacements = {'/bin/login': login, '/usr/bin/login': login,
                    '/sbin/agetty': getty, '/usr/sbin/agetty': getty}
    script = re.sub(r'/usr/bin/login|/bin/login|/usr/sbin/agetty|/sbin/agetty',
                    lambda match: replacements[match[0]], script)
    env = os.environ | {'PATH': str(tmp_path) + ':' + os.environ['PATH'],
                        'CHECK_DIR': str(tmp_path), 'CHECK_BLOCK': str(int(block))}
    return subprocess.Popen(['/bin/sh', '-c', script, 'vmm-login', 'vmm-terminal-test.service'],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_plain_guest_executes_login_without_service(tmp_path):
    process = launcher(tmp_path, selinux=False)
    assert process.wait(timeout=3) == 0
    assert (tmp_path / 'login-called').exists()
    assert not (tmp_path / 'run').exists()
    assert not (tmp_path / 'label').exists()


@pytest.mark.parametrize('disconnect', [False, True])
def test_selinux_login_stops_its_service_on_exit_and_disconnect(tmp_path, disconnect):
    process = launcher(tmp_path, block=disconnect)
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / 'runner-pid').exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(.01)
        if disconnect:
            process.terminate()
        assert process.wait(timeout=3) == 0
        args = (tmp_path / 'run').read_text().splitlines()
        assert '--pipe' in args and '--collect' in args
        assert '--login-program' in args
        assert not any(arg in ('--autologin', '-a', '-f') for arg in args)
        assert (tmp_path / 'label').read_text().splitlines() == ['-t', 'user_devpts_t', '/dev/pts/123']
        assert (tmp_path / 'stop').read_text().splitlines() == ['stop', 'vmm-terminal-test.service']
        assert not (tmp_path / 'login-called').exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_selinux_label_failure_does_not_fall_back_to_direct_login(tmp_path):
    process = launcher(tmp_path, label_ok=False)
    assert process.wait(timeout=3) != 0
    assert not (tmp_path / 'run').exists()
    assert not (tmp_path / 'login-called').exists()
