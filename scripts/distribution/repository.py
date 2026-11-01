#!/usr/bin/env python3
"""Create signed APT/DNF metadata. Run in the distribution build container.

The repository directory is a persistent mirror: retain old packages/by-hash files.
Only one release publisher may run at a time. GNUPGHOME must be outside the source.
"""
import argparse
import datetime
import gzip
import hashlib
import html
import os
import re
from pathlib import Path
import shutil
import subprocess


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def sign(key, path, output, clearsign=False):
    output.unlink(missing_ok=True)
    run('gpg', '--batch', '--yes', '--local-user', key, '--armor',
        '--output', str(output), '--clearsign' if clearsign else '--detach-sign', str(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--signing-key', required=True, help='Full GPG fingerprint')
    args = parser.parse_args()
    repo = args.repository.resolve()
    apt = repo / 'apt'
    pool = apt / 'pool/main/v/vm-manager'
    rpm = repo / 'rpm/el10/x86_64'
    pool.mkdir(parents=True, exist_ok=True)
    rpm.mkdir(parents=True, exist_ok=True)
    public = Path(__file__).resolve().parents[2] / 'packaging/signing-key.asc'
    fingerprints = subprocess.check_output(['gpg', '--batch', '--with-colons', '--show-keys', str(public)], text=True)
    if args.signing_key.upper() not in [line.split(':')[9] for line in fingerprints.splitlines() if line.startswith('fpr:')]:
        raise SystemExit('Signing fingerprint does not match the key embedded in the packages')
    key = subprocess.check_output(['gpg', '--batch', '--armor', '--export', args.signing_key])
    if not key:
        raise SystemExit('Signing key not found')
    (repo / 'vm-manager.asc').write_bytes(key)
    for package in sorted(args.artifacts.iterdir()):
        if package.suffix not in ('.deb', '.rpm'):
            continue
        target = (pool if package.suffix == '.deb' else rpm) / package.name
        if target.exists():
            raise SystemExit('Package version already published: ' + target.name)
        shutil.copyfile(package, target)
        if package.suffix == '.rpm':
            run('rpmsign', '--define', '_gpg_name ' + args.signing_key,
                '--define', '__gpg /usr/bin/gpg', '--addsign', str(target))
    if not list(pool.glob('*.deb')) or not list(rpm.glob('*.rpm')):
        raise SystemExit('Both DEB and RPM artifacts are required')
    binary = apt / 'dists/stable/main/binary-amd64'
    binary.mkdir(parents=True, exist_ok=True)
    packages = subprocess.check_output(['apt-ftparchive', 'packages', 'pool'], cwd=apt)
    (binary / 'Packages').write_bytes(packages)
    (binary / 'Packages.gz').write_bytes(gzip.compress(packages, mtime=0))
    for filename in ('Packages', 'Packages.gz'):
        content = (binary / filename).read_bytes()
        by_hash = binary / 'by-hash/SHA256'
        by_hash.mkdir(parents=True, exist_ok=True)
        (by_hash / hashlib.sha256(content).hexdigest()).write_bytes(content)
    now = datetime.datetime.now(datetime.timezone.utc)
    valid = (now + datetime.timedelta(days=90)).strftime('%a, %d %b %Y %H:%M:%S +0000')
    release = subprocess.check_output([
        'apt-ftparchive', '-o', 'APT::FTPArchive::Release::Origin=CarrTech',
        '-o', 'APT::FTPArchive::Release::Label=VM Manager',
        '-o', 'APT::FTPArchive::Release::Suite=stable',
        '-o', 'APT::FTPArchive::Release::Codename=stable',
        '-o', 'APT::FTPArchive::Release::Architectures=amd64',
        '-o', 'APT::FTPArchive::Release::Components=main',
        '-o', 'APT::FTPArchive::Release::Acquire-By-Hash=yes',
        '-o', 'APT::FTPArchive::Release::Valid-Until=' + valid,
        'release', 'dists/stable'], cwd=apt)
    release_path = apt / 'dists/stable/Release'
    # apt-ftparchive versions do not all emit Valid-Until from configuration.
    release_path.write_bytes(("Valid-Until: " + valid + "\n").encode() + release)
    sign(args.signing_key, release_path, release_path.with_name('InRelease'), clearsign=True)
    sign(args.signing_key, release_path, release_path.with_name('Release.gpg'))
    run('createrepo_c', '--update', '--retain-old-md', '10', str(rpm))
    metadata = rpm / 'repodata/repomd.xml'
    sign(args.signing_key, metadata, metadata.with_name('repomd.xml.asc'))
    # This is for direct-download users; APT and DNF verify their own signed hashes.
    sums = repo / 'SHA256SUMS'
    entries = []
    for path in sorted(repo.rglob('*')):
        if path.suffix in ('.deb', '.rpm'):
            entries.append(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + str(path.relative_to(repo)))
    sums.write_text('\n'.join(entries) + '\n')
    sign(args.signing_key, sums, sums.with_name('SHA256SUMS.asc'))
    def version(path):
        match = re.search(r'(\d+)\.(\d+)\.(\d+)', path.name)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)
    package_paths = sorted((p for p in repo.rglob('*') if p.suffix in ('.deb', '.rpm')), key=version, reverse=True)
    latest = '.'.join(map(str, version(package_paths[0])))
    links = ''.join('<li><a href="' + html.escape(p.relative_to(repo).as_posix(), quote=True)
                    + '">' + html.escape(p.name) + '</a></li>' for p in package_paths)
    (repo / 'index.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>VM Manager downloads</title><style>body{font:17px system-ui;max-width:850px;'
        'margin:60px auto;padding:0 24px;line-height:1.6}code{background:#eee;padding:3px}</style>'
        '<h1>VM Manager downloads</h1><p>Latest release: <strong>' + latest + '</strong>.</p><p>Debian 13 amd64 and Rocky/AlmaLinux 10 x86_64.</p>'
        '<ul>' + links + '</ul><p><a href="vm-manager.asc">Signing public key</a> · '
        '<a href="SHA256SUMS">SHA-256 checksums</a> · <a href="SHA256SUMS.asc">Checksum signature</a></p>'
        '<p>Signing fingerprint: <code>' + html.escape(args.signing_key) + '</code></p>'
        '<p>Verify this fingerprint independently in the GitHub installation guide before trusting it.</p>'
        '<p><a href="https://github.com/carrtech-dev/vm-manager/blob/main/docs/distribution.md">'
        'Installation, credentials and update instructions</a></p>'
        '<p>The installer prints browser URLs and opens HTTPS in active firewalld/UFW. '
        'Show access details with <code>sudo vm-manager status</code>.</p>'
        '<p>After installation, use <code>apt update &amp;&amp; apt upgrade</code> or '
        '<code>dnf update</code>. Read initial credentials with '
        '<code>sudo cat /var/lib/vm-manager/initial-admin.txt</code>.</p></html>')
    print('Signed repository:', repo)


if __name__ == '__main__':
    main()
