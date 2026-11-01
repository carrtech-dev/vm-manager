#!/usr/bin/env python3
"""Build and sign one version from a clean Git checkout on a Linux Docker host.

Publication is explicit via --publish, using standard AWS credentials. Preserve
--state outside Git between releases. GNUPGHOME contains the persistent signing
key and must also be outside Git. Never run concurrent publishers on other hosts.
"""
import argparse
import fcntl
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, cwd=ROOT, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--signing-home', type=Path, required=True)
    parser.add_argument('--signing-key', required=True)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--account', default='8b44df6ea391145f6d197d297c8eaca0')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', args.version):
        parser.error('Use numeric major.minor.patch')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT):
        parser.error('Build only a clean, committed, tested Git checkout')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    state, signing = args.state.resolve(), args.signing_home.resolve()
    if state.is_relative_to(ROOT) or signing.is_relative_to(ROOT):
        parser.error('State and private signing directory must be outside the source checkout')
    if not signing.is_dir() or signing.stat().st_mode & 0o077:
        parser.error('Signing directory must exist with mode 0700')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts = state / 'artifacts' / args.version
    repository = state / 'repository'
    with (state / 'release.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if artifacts.exists():
            parser.error('Artifact directory already exists; use a new version or inspect the failed build')
        artifacts.mkdir(parents=True)
        repository.mkdir(exist_ok=True)
        image = 'vm-manager-packager'
        run('docker', 'build', '-f', 'packaging/Containerfile', '-t', image, '.')
        run('docker', 'run', '--rm', '--platform', 'linux/amd64',
            '-v', str(ROOT) + ':/src:ro', '-v', str(artifacts) + ':/out', image,
            '--version', args.version, '--revision', revision,
            '--public-key', '/src/packaging/signing-key.asc')
        run('docker', 'run', '--rm', '--entrypoint', 'python3', '-e', 'GNUPGHOME=/keys',
            '-v', str(ROOT) + ':/src:ro', '-v', str(signing) + ':/keys',
            '-v', str(artifacts) + ':/out', '-v', str(repository) + ':/repository', image,
            'scripts/distribution/repository.py', '--artifacts', '/out', '--repository', '/repository',
            '--signing-key', args.signing_key)
        if args.publish:
            run('python3', 'scripts/distribution/r2.py', 'publish', '--account', args.account,
                '--repository', str(repository))
        print('Release', args.version, 'from', revision, 'ready in', repository)


if __name__ == '__main__':
    main()
