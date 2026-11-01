"""Guest-side K3s installer. Input and credentials never enter task output."""
from textwrap import indent

from .kubernetes import GUEST

# Executed through the Incus agent as root only on DevOps-owned Ubuntu VMs.
SCRIPT = r'''
import hashlib, ipaddress, json, os, pathlib, pwd, secrets, shutil, socket, subprocess, sys, urllib.request
root = pathlib.Path('/var/lib/vmm-k3s')
root.mkdir(mode=0o700, exist_ok=True)
b = json.load(sys.stdin)
action = b['action']
def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(text)
def command(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
if action == 'probe':
    print(json.dumps({'ready': pathlib.Path('/run/cloud-init/result.json').exists() or not shutil.which('cloud-init')}))
elif action == 'token':
    p = pathlib.Path('/etc/rancher/k3s/config.yaml')
    secure = pathlib.Path('/var/lib/rancher/k3s/server/token')
    token = secure.read_text().strip() if secure.exists() else json.loads(p.read_text())['token'] if p.exists() else secrets.token_hex(32)
    print(json.dumps({'token': token}))
elif action == 'launch':
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'vmm-k3s-install']).returncode == 0:
        print('{}'); sys.exit()
    private(root / 'request.json', json.dumps(b))
    private(root / 'install.py', b['installer'])
    private(root / 'result.json', json.dumps({'status': 'installing'}))
    command(['systemd-run', '--unit=vmm-k3s-install', '--collect', '/usr/bin/python3', str(root / 'install.py')])
    print('{}')
elif action == 'status':
    result = json.loads((root / 'result.json').read_text()) if (root / 'result.json').exists() else {'status': 'pending'}
    if result['status'] == 'installed':
        p = subprocess.run(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=5s', 'get', 'nodes', '-o', 'json'], capture_output=True, text=True, timeout=10)
        if p.returncode == 0: result['nodes'] = json.loads(p.stdout)['items']
    print(json.dumps(result))
elif action == 'kubeconfig':
    print(json.dumps({'kubeconfig': pathlib.Path('/etc/rancher/k3s/k3s.yaml').read_text()}))
elif action == 'connect':
    try:
        with socket.create_connection((str(ipaddress.ip_address(b['address'])), 6443), timeout=5): pass
        print('{"ready":true}')
    except OSError: print('{"ready":false}')
'''

SCRIPT += "\nelif action == 'manage':\n" + indent(GUEST, '    ')

INSTALLER = r'''
import hashlib, json, os, pathlib, platform, pwd, shutil, subprocess, urllib.request
root = pathlib.Path('/var/lib/vmm-k3s')
b = json.loads((root / 'request.json').read_text())
def private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f: f.write(text)
def command(args):
    subprocess.run(args, check=True, stdout=log, stderr=log, timeout=600, env=os.environ | {'DEBIAN_FRONTEND':'noninteractive'})
try:
    with (root / 'install.log').open('w') as log:
        os.chmod(root / 'install.log', 0o600)
        command(['apt-get', 'update', '-q'])
        command(['apt-get', 'install', '-y', '--no-install-recommends', 'ca-certificates', 'iptables', 'curl'])
        arch = platform.machine()
        asset, digest = b['assets'][arch]
        url = 'https://github.com/k3s-io/k3s/releases/download/' + b['version'] + '/' + asset
        target = pathlib.Path('/usr/local/bin/k3s')
        with urllib.request.urlopen(url, timeout=120) as response, (root / 'k3s').open('wb') as out:
            shutil.copyfileobj(response, out)
        if hashlib.sha256((root / 'k3s').read_bytes()).hexdigest() != digest: raise ValueError('Checksum mismatch')
        os.chmod(root / 'k3s', 0o755)
        shutil.copyfile(root / 'k3s', '/usr/local/bin/.k3s-vmm')
        os.chmod('/usr/local/bin/.k3s-vmm', 0o755)
        os.replace('/usr/local/bin/.k3s-vmm', target)
        link = pathlib.Path('/usr/local/bin/kubectl')
        if not link.exists(): link.symlink_to('k3s')
        command(['swapoff', '-a'])
        fstab = pathlib.Path('/etc/fstab')
        fstab.write_text(''.join('# vmm-k3s ' + line if len(line.split()) > 2 and line.split()[2] == 'swap' else line for line in fstab.read_text().splitlines(True)))
        command(['modprobe', 'br_netfilter'])
        command(['modprobe', 'overlay'])
        pathlib.Path('/etc/modules-load.d/k3s.conf').write_text('br_netfilter\noverlay\n')
        pathlib.Path('/etc/sysctl.d/90-k3s.conf').write_text('net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\n')
        command(['sysctl', '--system'])
        config = {'token': b['token'], 'node-name': b['name'], 'node-ip': b['address']}
        if b['role'] == 'master':
            config.update({'cluster-cidr': b['pod_cidr'], 'service-cidr': b['service_cidr'], 'write-kubeconfig-mode': '0600', 'tls-san': [b['address']]})
            if b.get('server'): config['server'] = 'https://' + b['server'] + ':6443'
            else: config['cluster-init'] = True
        else: config['server'] = 'https://' + b['server'] + ':6443'
        private(pathlib.Path('/etc/rancher/k3s/config.yaml'), json.dumps(config))
        role = 'server' if b['role'] == 'master' else 'agent'
        unit = '[Unit]\nDescription=K3s Kubernetes\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=notify\nExecStart=/usr/local/bin/k3s ' + role + '\nKillMode=process\nDelegate=yes\nLimitNOFILE=1048576\nLimitNPROC=infinity\nLimitCORE=infinity\nTasksMax=infinity\nTimeoutStartSec=0\nRestart=always\nRestartSec=5s\n[Install]\nWantedBy=multi-user.target\n'
        pathlib.Path('/etc/systemd/system/k3s.service').write_text(unit)
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'enable', '--now', 'k3s'])
        if b['role'] == 'master':
            try: user = pwd.getpwnam('vmm-kube')
            except KeyError:
                command(['useradd', '--system', '--create-home', '--home-dir', '/home/vmm-kube', '--shell', '/bin/bash', 'vmm-kube'])
                user = pwd.getpwnam('vmm-kube')
            directory = pathlib.Path(user.pw_dir) / '.kube'
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chown(directory, user.pw_uid, user.pw_gid)
            private(directory / 'config', pathlib.Path('/etc/rancher/k3s/k3s.yaml').read_text())
            os.chown(directory / 'config', user.pw_uid, user.pw_gid)
        private(root / 'result.json', json.dumps({'status':'installed'}))
except Exception:
    private(root / 'result.json', json.dumps({'status':'failed', 'message':'K3s installation failed; inspect /var/lib/vmm-k3s/install.log in this VM.'}))
finally:
    (root / 'request.json').unlink(missing_ok=True)
'''
