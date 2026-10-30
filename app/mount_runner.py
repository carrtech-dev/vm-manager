"""Small dependency-free mount service, installed inside supported Linux guests."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    source, target = config['source'], config['path']
    directory = config_path.parent
    if source['kind'] == 's3':
        os.environ['RCLONE_RC_USER'] = 'vmm'
        os.environ['RCLONE_RC_PASS'] = config['rc_password']
        remote = 'remote:' + source['bucket'] + ('/' + source['prefix'] if source['prefix'] else '')
        args = ['rclone', 'mount', remote, target, '--config', str(directory / 'rclone.conf'),
                '--allow-other', '--umask', '000', '--vfs-cache-mode', 'full',
                '--cache-dir', str(directory / 'cache'), '--vfs-cache-max-size', '5G',
                '--rc', '--rc-addr', '127.0.0.1:' + str(config['rc_port'])]
        if config['readonly']:
            args.append('--read-only')
        # Do not log rclone errors, which may contain endpoint query data. Root
        # configuration and cache persist across failures, reboot and retry.
        with open(os.devnull, 'w') as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        os.execvp(args[0], args)
    else:
        options = ['credentials=' + str(directory / 'credentials'), 'vers=3.0',
                   'port=' + str(source['port']), 'nosuid', 'nodev',
                   'file_mode=0666', 'dir_mode=0777', 'ro' if config['readonly'] else 'rw']
        if source['prefix']:
            options.append('prefixpath=' + source['prefix'])
        args = ['mount.cifs', '//' + source['server'] + '/' + source['share'], target, '-o', ','.join(options)]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        if result.returncode:
            raise SystemExit(1)
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping and os.path.ismount(target):
            time.sleep(.5)
        if os.path.ismount(target):
            subprocess.run(['umount', target], check=True, timeout=60)


if __name__ == '__main__':
    main()
