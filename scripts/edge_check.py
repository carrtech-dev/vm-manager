#!/usr/bin/env python3
"""Opt-in acceptance: disposable workloads on an explicitly selected Ready cluster.

Requires root on the manager host. Creating the test Service may automatically
start the shared edge. Does not call Launch or Connect by default; retains the
edge and removes only this run's uniquely named resources/account. Never resets an existing user's password.
"""
import argparse
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import time

import httpx

from app.security import Store, hash_password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--cluster-id', required=True)
    parser.add_argument('--launch', action='store_true', help='Optionally force launch for diagnostics; not required for automatic integration')
    parser.add_argument('--port', type=int, default=18088)
    args = parser.parse_args()
    suffix = secrets.token_hex(4)
    name = 'vmm-edge-check-' + suffix
    hostname = name + '.invalid'
    store = Store(args.state)
    password = secrets.token_urlsafe(32)
    context = ssl.create_default_context(cafile=str(args.certificate))
    with store.db() as db:
        user_id = db.execute('INSERT INTO users(username,password,role,created) VALUES(?,?,?,?)',
                            (name, hash_password(password), 'administrator', time.time())).lastrowid
    client = httpx.Client(base_url=args.url.rstrip('/') + '/api', verify=context, trust_env=False, timeout=110)
    created, route_id = [], ''
    def api(path, method='GET', body=None):
        response = client.request(method, path, json=body)
        if response.is_error:
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:300]}')
        return response.json()
    def wait(fn, message, timeout=180):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = fn()
            if value: return value
            time.sleep(2)
        raise RuntimeError(message)
    def queued(path, method='POST', body=None):
        result = api(path, method, body or {})
        def finished():
            task = api('/tasks/' + result['task_id'])
            if task['status'] in ('failed','interrupted'): raise RuntimeError(task['message'])
            return task['status'] == 'completed'
        wait(finished, 'Edge configuration task timed out')
    endpoint = '/devops/clusters/' + args.cluster_id
    def resources(): return api(endpoint + '/resources')
    def manage(action, kind, **values): return api(endpoint + '/manage', 'POST', dict(action=action,resource=kind,name=name,namespace='default') | values)
    try:
        login = api('/login', 'POST', {'username':name, 'password':password})
        client.headers['X-CSRF-Token'] = login['csrf']
        clusters = api('/devops/clusters')['clusters']
        cluster = next(c for c in clusters if c['id'] == args.cluster_id)
        if cluster['status'] != 'ready': raise RuntimeError('Select a Ready cluster')
        edge = api('/devops/load-balancer')
        if args.cluster_id in (edge['settings'].get('excluded_clusters') or []):
            raise RuntimeError('This cluster has an explicit integration opt-out; select another cluster or resume it first')
        if args.launch:
            queued('/devops/load-balancer/launch')
        first = cluster['nodes'][0]
        if first['host_id']:
            raise RuntimeError('The automatic acceptance check requires a cluster with its first master on this host')
        manage('create', 'deployment', workload={'image':'nginx:stable-alpine', 'replicas':1})
        created.append('deployment')
        wait(lambda: next((r for r in resources()['deployments'] if r['name'] == name and r['ready'] == 1), None), 'Test Deployment did not become ready')
        # Use an ordinary external kubectl request with no class, Launch or Connect.
        command = ['incus','--force-local','--project','default','exec',first['name'],'--',
                   '/usr/local/bin/k3s','kubectl','expose','deployment',name,'--namespace','default',
                   '--type=LoadBalancer','--port='+str(args.port),'--target-port=80',
                   '--labels=app.kubernetes.io/managed-by=vm-manager','-o','json']
        exposed = json.loads(subprocess.check_output(command,text=True))
        created.append('service')
        assert exposed['spec'].get('loadBalancerClass') == 'vmm.carrtech.dev/edge', 'Admission must supply the class at creation'
        def assigned():
            resource = next((r for r in resources()['services'] if r['name'] == name), None)
            current = api('/devops/load-balancer')
            address = current['settings'].get('address')
            return resource if resource and resource.get('lb_class') == 'vmm.carrtech.dev/edge' and any(entry.get('ip') == address for entry in resource['external']) else None
        wait(assigned, 'Automatic Service integration did not assign the edge address')
        address = api('/devops/load-balancer')['settings']['address']
        # Use an ordinary HTTP request through HAProxy's TCP listener.
        with httpx.Client(trust_env=False, timeout=10) as probe:
            response = probe.get('http://' + address + ':' + str(args.port))
            assert response.status_code == 200 and 'Welcome to nginx' in response.text
        print('PASS: plain kubectl LoadBalancer Service automatically connected, assigned an IP and served TCP traffic without Launch or Connect')
        manage('create', 'ingress', workload={'hostname':hostname,'selector':name,'port':args.port})
        created.append('ingress')
        with tempfile.TemporaryDirectory(prefix=name) as tmp:
            cert, key = Path(tmp)/'cert.pem', Path(tmp)/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-keyout',str(key),'-out',str(cert),
                            '-subj','/CN='+hostname,'-addext','subjectAltName=DNS:'+hostname],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            queued('/devops/load-balancer/routes', body={'hostname':hostname,'mode':'manual','certificate':cert.read_text(),'private_key':key.read_text(),
                'cluster_id':args.cluster_id,'namespace':'kube-system','service':'traefik','service_port':80})
            route_id = next(r['id'] for r in api('/devops/load-balancer')['settings']['routes'] if r['hostname'] == hostname)
            tls = ssl.create_default_context(cafile=str(cert))
            def ingress_ready(manager=False):
                with socket.create_connection((address,443),timeout=10) as connection:
                    with tls.wrap_socket(connection,server_hostname=hostname) as secure:
                        secure.sendall(('GET ' + ('/health' if manager else '/') + ' HTTP/1.1\r\nHost: '+hostname+'\r\nConnection: close\r\n\r\n').encode())
                        result=http.client.HTTPResponse(secure); result.begin()
                        body = result.read()
                        return result.status == 200 and (json.loads(body).get('service') == 'vm-manager' if manager else b'Welcome to nginx' in body)
            wait(ingress_ready, 'HTTPS Ingress route did not serve the test container')
            print('PASS: manually supplied certificate, SNI on shared 443, Traefik Ingress and container response verified')
            queued('/devops/load-balancer/routes', body={'id':route_id,'hostname':hostname,'mode':'manual','backend':'vm-manager'})
            wait(lambda: ingress_ready(manager=True), 'Direct HTTPS VM Manager route failed')
            print('PASS: direct HTTPS VM Manager route uses the verified existing app origin')
        config = api('/devops/load-balancer')
        assert 'private_key' not in json.dumps(config)
        print('PASS: load balancer inventory omits private keys and credentials')
        queued('/devops/load-balancer/routes/' + route_id, 'DELETE', {'confirm_name':hostname}); route_id=''
        for kind in reversed(created.copy()):
            key = 'ingresses' if kind == 'ingress' else kind+'s'
            resource = next(r for r in resources()[key] if r['name'] == name)
            manage('remove',kind,uid=resource['uid'],confirm_name=name)
            created.remove(kind)
        wait(lambda: not any(s['name'] == name for s in api('/devops/load-balancer')['status']['services']), 'Deleted Service listener was retained')
        print('PASS: deleting Kubernetes resources withdraws the managed listener')
    finally:
        errors=[]
        if route_id:
            try: queued('/devops/load-balancer/routes/'+route_id,'DELETE',{'confirm_name':hostname})
            except Exception as exc: errors.append(str(exc))
        for kind in reversed(created):
            try:
                key='ingresses' if kind=='ingress' else kind+'s'
                resource=next((r for r in resources()[key] if r['name']==name),None)
                if resource: manage('remove',kind,uid=resource['uid'],confirm_name=name)
            except Exception as exc: errors.append(str(exc))
        client.close()
        with store.db() as db:
            db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
            db.execute('DELETE FROM users WHERE id=?',(user_id,))
        if errors: raise RuntimeError('Acceptance cleanup requires attention: '+'; '.join(errors))


if __name__ == '__main__':
    main()
