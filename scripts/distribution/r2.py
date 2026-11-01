#!/usr/bin/env python3
"""Provision the downloads bucket/custom domain, or publish a signed repository.

Cloudflare REST uses CLOUDFLARE_API_TOKEN. Publishing uses bucket-scoped S3
credentials via AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. Never prints secrets.
"""
import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import urllib.error
import urllib.request


def api(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request('https://api.cloudflare.com/client/v4/' + path,
        data=data, method=method, headers={'Authorization': 'Bearer ' + os.environ['CLOUDFLARE_API_TOKEN'],
                                         'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Cloudflare errors contain no bearer credential, but report only code/message.
        result = json.loads(exc.read())
    if not result.get('success'):
        raise SystemExit('Cloudflare API: ' + json.dumps(result.get('errors', [])))
    return result['result']


def provision(account, bucket, domain, zone):
    base = f'accounts/{account}/r2/buckets'
    buckets = api('GET', base)['buckets']
    if bucket not in {item['name'] for item in buckets}:
        api('POST', base, {'name': bucket})
    path = base + '/' + bucket + '/domains/custom'
    domains = api('GET', path)['domains']
    if domain not in {item['domain'] for item in domains}:
        api('POST', path, {'domain': domain, 'zoneId': zone, 'enabled': True,
                           'minTLS': '1.2'})
    print(json.dumps(api('GET', path), indent=2))


def publish(account, bucket, repository):
    import boto3  # Optional publishing dependency; not part of the installed app.
    s3 = boto3.client('s3', endpoint_url=f'https://{account}.r2.cloudflarestorage.com', region_name='auto')
    # Payload and content-addressed metadata first. APT's InRelease goes last.
    # DNF clients may need a retry in the brief repomd.xml/signature rollover.
    paths = [p for p in repository.rglob('*') if p.is_file()]
    if not any(p.name == 'InRelease' for p in paths) or not any(p.name == 'repomd.xml.asc' for p in paths):
        raise SystemExit('Refusing to publish an unsigned/incomplete repository')
    priority = lambda p: 3 if p.name == 'InRelease' else 2 if p.name == 'repomd.xml' else 1 if p.name in ('Release', 'Release.gpg', 'repomd.xml.asc') else 0
    for path in sorted(paths, key=priority):
        key = path.relative_to(repository).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix in ('.deb', '.rpm'):
            try:
                existing = s3.head_object(Bucket=bucket, Key=key)
            except s3.exceptions.ClientError as exc:
                if exc.response['Error']['Code'] not in ('404', 'NoSuchKey', 'NotFound'):
                    raise
            else:
                if existing.get('Metadata', {}).get('sha256') != digest:
                    raise SystemExit('Immutable package already exists with different content: ' + key)
                continue
        immutable = path.suffix in ('.deb', '.rpm') or '/by-hash/' in key
        s3.upload_file(str(path), bucket, key, ExtraArgs={
            'ContentType': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
            'CacheControl': 'public, max-age=31536000, immutable' if immutable else 'no-cache, max-age=0, must-revalidate',
            'Metadata': {'sha256': digest}})
    print('Published', len(paths), 'public repository objects to', bucket)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('provision', 'publish'))
    parser.add_argument('--account', required=True)
    parser.add_argument('--bucket', default='vm-manager-packages')
    parser.add_argument('--domain', default='packages.carrtech.dev')
    parser.add_argument('--zone')
    parser.add_argument('--repository', type=Path)
    args = parser.parse_args()
    if args.action == 'provision':
        if not args.zone:
            parser.error('--zone is required for provision')
        provision(args.account, args.bucket, args.domain, args.zone)
    else:
        if not args.repository or not args.repository.is_dir():
            parser.error('--repository must be a signed repository directory')
        publish(args.account, args.bucket, args.repository)


if __name__ == '__main__':
    main()
