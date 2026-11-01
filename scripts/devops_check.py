#!/usr/bin/env python3
"""Opt-in disposable cross-host K3s acceptance; never resets existing accounts.

Run on the coordinating host with its release Python environment. --state is
that manager's data directory. The peer must run the same DevOps release.
The parent interfaces must share a LAN with DHCP and outbound internet. Creates
three masters and a worker, then deletes only its generated cluster/network VMs.
Networks are removed separately after successful VM cleanup. Failures retain
cluster VMs for inspection unless --cleanup-on-failure is explicitly supplied.
"""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from app.security import hash_password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--peer-id', required=True)
    parser.add_argument('--local-parent', default='')
    parser.add_argument('--peer-parent', default='')
    parser.add_argument('--local-network', help='Use an existing routed/shared network instead of creating LAN networks')
    parser.add_argument('--peer-network', help='Existing routed/shared network on the peer')
    parser.add_argument('--local-pool', default='hdd-pool')
    parser.add_argument('--peer-pool', default='local')
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--cleanup-on-failure', action='store_true')
    parser.add_argument('--resume-id', help='Retry only a retained vmm-k3s-check-* acceptance deployment')
    args = parser.parse_args()
    suffix = secrets.token_hex(4)
    name = 'vmm-k3s-check-' + suffix
    network = 'vmm-k3s-net-' + suffix
    username, password = 'k3s-check-' + suffix, secrets.token_urlsafe(32)
    db_path = args.state / 'manager.sqlite3'
    if not db_path.is_file(): raise SystemExit('Existing manager database required')
    with sqlite3.connect(db_path) as db:
        user_id = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
                             (username, hash_password(password), 'operator', time.time())).lastrowid
    cluster_id, succeeded = None, False
    tls = ssl.create_default_context(cafile=str(args.certificate))
    client = httpx.Client(base_url=args.url, verify=tls, trust_env=False, timeout=60)
    def api(path, method='GET', body=None):
        response = client.request(method, '/api' + path, json=body)
        if response.is_error: raise RuntimeError(f'{method} {path}: {response.status_code} {response.text[:1000]}')
        return response.json()
    def wait_cluster(status, timeout=2400):
        end, previous = time.monotonic() + timeout, None
        while time.monotonic() < end:
            record = next(c for c in api('/devops/clusters')['clusters'] if c['id'] == cluster_id)
            message = record['status'] + ': ' + record['message']
            if message != previous:
                print(message, flush=True); previous = message
            if record['status'] == status: return record
            if record['status'] in ('failed', 'interrupted'): raise RuntimeError(message)
            time.sleep(5)
        raise RuntimeError('Cluster acceptance timed out')
    try:
        csrf = api('/login', 'POST', {'username':username, 'password':password})['csrf']
        client.headers['X-CSRF-Token'] = csrf
        options = api('/devops/options')
        assert all(next(h for h in options['hosts'] if h['id'] == host).get('options') for host in ('', args.peer_id))
        local = dict(pool=args.local_pool, cpu=2, memory_gib=2, disk_gib=20)
        remote = dict(local, host_id=args.peer_id, pool=args.peer_pool)
        spec = dict(name=name, masters=[remote, local, local], workers=[dict(local, cpu=1, memory_gib=1)],
            new_network={'name':network, 'kind':'lan', 'parents':{'':args.local_parent, args.peer_id:args.peer_parent}})
        if args.local_network or args.peer_network:
            if not (args.local_network and args.peer_network): raise RuntimeError('Both existing network names are required')
            spec.pop('new_network')
            for node in spec['masters'] + spec['workers']:
                node['network'] = args.peer_network if node.get('host_id') else args.local_network
        if args.resume_id:
            existing = next(c for c in api('/devops/clusters')['clusters'] if c['id'] == args.resume_id)
            if not existing['name'].startswith('vmm-k3s-check-'):
                raise RuntimeError('Only disposable acceptance clusters can be resumed')
            name, network = existing['name'], existing['spec']['new_network']['name']
            result = api('/devops/clusters/' + args.resume_id + '/retry', 'POST', {})
        else:
            result = api('/devops/clusters', 'POST', spec)
        cluster_id = result['cluster_id']
        print('Disposable cluster: ' + name + ' (' + cluster_id + ')', flush=True)
        record = wait_cluster('ready')
        assert len(record['nodes']) == 4 and all(n['status'] == 'ready' for n in record['nodes'])
        config = api('/devops/clusters/' + cluster_id + '/kubeconfig', 'POST', {})['kubeconfig']
        assert 'https://' + record['nodes'][0]['address'] + ':6443' in config
        with tempfile.TemporaryDirectory() as temp:
            def material(key):
                value = re.search(r'^\s*' + key + r':\s*(\S+)', config, re.M)[1]
                path = Path(temp) / key
                path.write_bytes(base64.b64decode(value)); path.chmod(0o600)
                return str(path)
            context = ssl.create_default_context(cafile=material('certificate-authority-data'))
            context.load_cert_chain(material('client-certificate-data'), material('client-key-data'))
            endpoint = 'https://' + record['nodes'][0]['address'] + ':6443'
            with httpx.Client(base_url=endpoint, verify=context, trust_env=False, timeout=30) as kube:
                response = kube.get('/api/v1/nodes'); response.raise_for_status()
                assert len(response.json()['items']) == 4
                print('Downloaded kubeconfig authenticated to Kubernetes API; all four nodes present', flush=True)
                for pod_name, node_name, command in (
                    ('vmm-network-server', record['nodes'][0]['name'], ['/bin/sh','-c','mkdir -p /www; echo CROSS_HOST_POD_OK >/www/index.html; httpd -f -p 8080 -h /www']),
                    ('vmm-network-client', record['nodes'][-1]['name'], ['/bin/sh','-c','sleep 900'])):
                    response = kube.post('/api/v1/namespaces/default/pods', json={'apiVersion':'v1','kind':'Pod',
                        'metadata':{'name':pod_name}, 'spec':{'nodeName':node_name, 'containers':[
                            {'name':'check','image':'busybox:1.37.0','command':command}]}})
                    response.raise_for_status()
                end = time.monotonic() + 300
                pod_ip = None
                while time.monotonic() < end:
                    response = kube.get('/api/v1/namespaces/default/pods'); response.raise_for_status()
                    pods = {p['metadata']['name']:p for p in response.json()['items']}
                    if all(any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pods[name]['status'].get('conditions', []))
                           for name in ('vmm-network-server','vmm-network-client')):
                        pod_ip = pods['vmm-network-server']['status']['podIP']; break
                    time.sleep(3)
                if not pod_ip: raise RuntimeError('Disposable cross-host network pods did not become Ready')
        first = record['nodes'][0]
        path = '/api/cluster/hosts/' + first['host_id'] + '/proxy/devops/agent/' + first['id'] + '/terminal'
        async def terminal():
            uri = 'wss://' + urlsplit(args.url).netloc + path + '?csrf=' + csrf
            async with connect(uri, ssl=tls, origin=args.url, additional_headers={
                    'Cookie':'vmm_session=' + client.cookies.get('vmm_session')}, proxy=None) as ws:
                ready = json.loads(await asyncio.wait_for(ws.recv(), 20))
                assert ready['type'] == 'ready' and ready['mode'] == 'kubernetes', ready
                await ws.send(json.dumps({'type':'input', 'data':f"test \"$(id -u)\" != 0 && kubectl get nodes -o name && kubectl exec vmm-network-client -- wget -qO- http://{pod_ip}:8080 && kubectl exec vmm-network-client -- nslookup kubernetes.default.svc.cluster.local && printf 'REMOTE_KUBE_%s\\n' TERMINAL_OK\r"}))
                output = ''
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    data = await asyncio.wait_for(ws.recv(), 20)
                    if isinstance(data, bytes):
                        output += data.decode(errors='replace')
                        await ws.send(json.dumps({'type':'ack'}))
                        if 'REMOTE_KUBE_TERMINAL_OK' in output and 'CROSS_HOST_POD_OK' in output and 'node/' + first['name'] in output: return
                    else:
                        event = json.loads(data)
                        if event.get('type') == 'error': raise RuntimeError(event['message'])
                raise RuntimeError('kubectl terminal did not produce expected node output')
        asyncio.run(terminal())
        print('Remote embedded kubectl terminal, cross-host pod HTTP and service DNS passed using a non-root guest account', flush=True)
        succeeded = True
    finally:
        try:
            if cluster_id and (succeeded or args.cleanup_on_failure):
                api('/devops/clusters/' + cluster_id, 'DELETE', {'confirm_name':name})
                wait_cluster('deleted')
                print('Disposable cluster VMs deleted. Retained network: ' + network, flush=True)
            elif cluster_id:
                print('Retained failed disposable cluster for inspection: ' + name, flush=True)
        finally:
            client.close()
            with sqlite3.connect(db_path) as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM users WHERE id=? AND username=?', (user_id, username))


if __name__ == '__main__':
    main()
