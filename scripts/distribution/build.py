#!/usr/bin/env python3
"""Build DEB and RPM artifacts on Linux using packaging/Containerfile."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASE = '/usr/lib/vm-manager'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def put(root, name, content, mode=0o644):
    path = root / name.lstrip('/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def stage(root, version, revision, public_key, url, kind):
    app = root / BASE.lstrip('/')
    shutil.copytree(ROOT / 'app', app / 'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(ROOT / 'requirements.lock', app / 'requirements.lock')
    put(root, BASE + '/REVISION', revision + '\n')
    spice = app / 'app/static/vendor/spice'
    shutil.copytree('/usr/share/spice-html5', spice, symlinks=False)
    shutil.copyfile('/usr/share/doc/spice-html5/copyright', spice / 'COPYRIGHT')
    wheels = app / 'wheels'
    command = ['python3', '-m', 'pip', 'download', '--only-binary=:all:', '--dest', str(wheels),
               '-r', str(ROOT / 'requirements.lock')]
    if kind == 'rpm':
        command += ['--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312',
                    '--platform', 'manylinux_2_28_x86_64', '--platform', 'manylinux2014_x86_64',
                    '--platform', 'manylinux_2_17_x86_64']
    run(*command)
    for source, target in [('maintain.py', '/usr/libexec/vm-manager-maintain.py'),
                           ('serve.py', '/usr/libexec/vm-manager-serve.py'),
                           ('vm_manager_access.py', '/usr/libexec/vm_manager_access.py'),
                           ('vm-manager', '/usr/bin/vm-manager'),
                           ('ufw-profile', '/etc/ufw/applications.d/vm-manager'),
                           ('vm-manager.service', '/usr/lib/systemd/system/vm-manager.service'),
                           ('vm-manager.conf', '/etc/vm-manager/vm-manager.conf')]:
        put(root, target, (ROOT / 'packaging' / source).read_text(), 0o755 if target.endswith('.py') or target == '/usr/bin/vm-manager' else 0o644)
    broker = (ROOT / 'scripts/vm-manager-storage.service').read_text().replace(
        '/opt/carrtech/vm-manager/current/.venv', '/var/lib/vm-manager-runtime/venv').replace(
        '/opt/carrtech/vm-manager/current', BASE)
    put(root, '/usr/lib/systemd/system/vm-manager-storage.service', broker)
    put(root, '/usr/share/keyrings/vm-manager.asc', public_key.read_text())
    put(root, '/usr/share/doc/vm-manager/README.md', (ROOT / 'docs/distribution.md').read_text())
    if kind == 'deb':
        put(root, '/etc/apt/sources.list.d/vm-manager.sources', f'''Types: deb
URIs: {url}/apt
Suites: stable
Components: main
Architectures: amd64
Signed-By: /usr/share/keyrings/vm-manager.asc
''')
    else:
        put(root, '/etc/yum.repos.d/vm-manager.repo', f'''[vm-manager]
name=CarrTech VM Manager
baseurl={url}/rpm/el10/x86_64
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file:///usr/share/keyrings/vm-manager.asc
''')


PREFLIGHT = '''if test -e /opt/carrtech/vm-manager/current; then
    echo "Source deployment detected; explicit migration required" >&2
    exit 1
fi
if test -f /usr/libexec/vm-manager-maintain.py; then
    /usr/bin/python3 /usr/libexec/vm-manager-maintain.py pre-upgrade
fi
'''
STOP = '''if test -d /run/systemd/system; then
    systemctl stop vm-manager.service vm-manager-storage.service
    systemctl disable vm-manager.service vm-manager-storage.service
fi
/usr/bin/python3 /usr/libexec/vm-manager-maintain.py remove
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--repository-url', default='https://packages.carrtech.dev')
    parser.add_argument('--output', type=Path, default=Path('/out'))
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', args.version):
        parser.error('Use a numeric major.minor.patch version, incremented for every release')
    if not re.fullmatch(r'[a-f0-9]{40}', args.revision):
        parser.error('Revision must be the full Git commit')
    if not re.fullmatch(r'https://[a-z0-9.-]+', args.repository_url):
        parser.error('Repository URL must be an HTTPS origin')
    if os.uname().machine != 'x86_64':
        parser.error('Build on x86_64 Linux (Docker --platform linux/amd64)')
    args.output.mkdir(parents=True, exist_ok=True)
    os.umask(0o022)
    with tempfile.TemporaryDirectory(prefix='vmm-packages-') as temporary:
        temp = Path(temporary)
        deb = temp / 'deb'
        stage(deb, args.version, args.revision, args.public_key, args.repository_url, 'deb')
        put(deb, 'DEBIAN/control', f'''Package: vm-manager
Version: {args.version}-1
Section: admin
Priority: optional
Architecture: amd64
Maintainer: CarrTech <support@carrtech.dev>
Homepage: https://github.com/carrtech-dev/vm-manager
Depends: iproute2, python3 (>= 3.13), python3 (<< 3.14), python3-venv, python3-libvirt, openssl, ca-certificates, systemd, passwd, util-linux, acl, cifs-utils, smbclient, fuse3, rclone, libvirt-clients
Recommends: incus
Description: CarrTech self-hosted virtual machine manager
 HTTPS console for existing Incus and libvirt hosts.
''')
        put(deb, 'DEBIAN/preinst', '#!/bin/sh\nset -eu\n' + PREFLIGHT, 0o755)
        put(deb, 'DEBIAN/postinst', '#!/bin/sh\nset -eu\nif [ "$1" = configure ]; then\n/usr/bin/python3 /usr/libexec/vm-manager-maintain.py configure\nfi\n', 0o755)
        put(deb, 'DEBIAN/prerm', '#!/bin/sh\nset -eu\nif [ "$1" = remove ]; then\n' + STOP + 'fi\n', 0o755)
        put(deb, 'DEBIAN/postrm', '#!/bin/sh\nset -eu\nif test -d /run/systemd/system; then systemctl daemon-reload; fi\n# Persistent data, credentials and certificates are deliberately retained even on purge.\n', 0o755)
        put(deb, 'DEBIAN/conffiles', '/etc/vm-manager/vm-manager.conf\n/etc/apt/sources.list.d/vm-manager.sources\n/etc/ufw/applications.d/vm-manager\n')
        run('dpkg-deb', '--root-owner-group', '--build', str(deb), str(args.output / f'vm-manager_{args.version}-1_amd64.deb'))
        rpm = temp / 'rpm'
        stage(rpm, args.version, args.revision, args.public_key, args.repository_url, 'rpm')
        spec = temp / 'vm-manager.spec'
        files = ['/usr/lib/vm-manager', '/usr/libexec/vm-manager-maintain.py', '/usr/libexec/vm-manager-serve.py',
                 '/usr/lib/systemd/system/vm-manager.service', '/usr/lib/systemd/system/vm-manager-storage.service',
                 '/usr/libexec/vm_manager_access.py', '/usr/bin/vm-manager',
                 '%config(noreplace) /etc/ufw/applications.d/vm-manager',
                 '/usr/share/keyrings/vm-manager.asc', '/usr/share/doc/vm-manager',
                 '%config(noreplace) /etc/vm-manager/vm-manager.conf', '%config(noreplace) /etc/yum.repos.d/vm-manager.repo']
        spec.write_text(f'''Name: vm-manager
Version: {args.version}
Release: 1.el10
Summary: CarrTech self-hosted virtual machine manager
License: LicenseRef-Proprietary
URL: https://github.com/carrtech-dev/vm-manager
BuildArch: x86_64
AutoReqProv: no
Requires: iproute, python3 >= 3.12, python3 < 3.13, python3-pip, python3-libvirt, openssl, ca-certificates, systemd, shadow-utils, util-linux, acl, cifs-utils, samba-client, fuse3, libvirt-client
%description
HTTPS console for existing Incus and libvirt hosts. Optional rclone requires EPEL.
%prep
%build
%install
mkdir -p %{{buildroot}}
cp -a {rpm}/. %{{buildroot}}/
%pre
{PREFLIGHT}
%post
/usr/bin/python3 /usr/libexec/vm-manager-maintain.py configure
%preun
if [ "$1" -eq 0 ]; then
{STOP}
fi
%postun
if test -d /run/systemd/system; then systemctl daemon-reload; fi
%files
%defattr(-,root,root,-)
''' + '\n'.join(files) + '\n')
        run('rpmbuild', '-bb', '--define', '_topdir ' + str(temp / 'rpmbuild'),
            '--define', '_build_id_links none', '--define', '__os_install_post %{nil}', str(spec))
        for path in (temp / 'rpmbuild/RPMS').rglob('*.rpm'):
            shutil.copyfile(path, args.output / path.name)
    print('Built packages in', args.output)


if __name__ == '__main__':
    main()
