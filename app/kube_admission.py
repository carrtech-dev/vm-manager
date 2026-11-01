"""Cluster-local admission: default application LoadBalancer Services at creation."""
import hashlib

SERVER = r'''
import base64
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLASS = 'vmm.carrtech.dev/edge'
SYSTEM = ('kube-system', 'kube-public', 'kube-node-lease')

def admission_response(review):
    request = review['request']
    response = {'uid': request['uid'], 'allowed': True}
    obj = request.get('object') or {}
    spec = obj.get('spec') or {}
    metadata = obj.get('metadata') or {}
    resource = request.get('resource') or {}
    if (request.get('operation') == 'CREATE' and resource.get('group', '') == ''
            and resource.get('resource') == 'services' and spec.get('type') == 'LoadBalancer'
            and not spec.get('loadBalancerClass') and request.get('namespace') not in SYSTEM
            and (metadata.get('annotations') or {}).get('vmm.carrtech.dev/load-balancer') != 'disabled'):
        patch = [{'op':'add', 'path':'/spec/loadBalancerClass', 'value':CLASS}]
        response.update(patchType='JSONPatch', patch=base64.b64encode(json.dumps(patch).encode()).decode())
    return {'apiVersion':'admission.k8s.io/v1', 'kind':'AdmissionReview', 'response':response}

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)
    def log_message(self, *args):
        pass  # Never record admission request bodies or client headers.
    def do_GET(self):
        self.send_response(200 if self.path == '/healthz' else 404)
        self.end_headers()
        self.wfile.write(b'ok')
    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/mutate' or not 0 < length <= 1048576:
                self.send_error(400); return
            result = admission_response(json.loads(self.rfile.read(length)))
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
        except (KeyError, TypeError, ValueError):
            self.send_error(400)

if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 19443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/var/lib/vmm-edge-admission/tls.crt', '/var/lib/vmm-edge-admission/tls.key')
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
'''

BOOTSTRAP = r'''
def edge_bootstrap():
    import base64, fcntl, grp, hashlib, os, pathlib, pwd, ssl, tempfile, time, urllib.request
    directory = pathlib.Path('/var/lib/vmm-edge-admission')
    directory.mkdir(mode=0o750, exist_ok=True)
    with (directory / 'bootstrap.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        name = 'vmm-edge-admission'
        labels = {'app.kubernetes.io/managed-by':'vm-manager', 'vmm.carrtech.dev/component':'admission'}
        def metadata(kind):
            result = {'name':name, 'labels':labels, 'annotations':{'vmm.carrtech.dev/admission-version':admission_version}}
            if kind != 'MutatingWebhookConfiguration': result['namespace'] = 'kube-system'
            return result
        def existing(kind):
            text = kube(['get', kind, name] + ([] if kind == 'mutatingwebhookconfiguration' else ['-n','kube-system']) + ['--ignore-not-found','-o','json'])
            obj = json.loads(text) if text.strip() else None
            if obj and any(obj['metadata'].get('labels', {}).get(k) != v for k,v in labels.items()):
                raise ValueError('Admission resource name is occupied by an unmanaged object')
            return obj
        def save(obj):
            # Server-side apply updates only this integration's fields, never forces conflicts.
            kube(['apply','--server-side','--field-manager=vm-manager-admission','-f','-'], json.dumps(obj))
        if not r.get('admission_enabled', True):
            webhook = existing('mutatingwebhookconfiguration')
            if webhook:
                kube(['delete','--raw','/apis/admissionregistration.k8s.io/v1/mutatingwebhookconfigurations/' + name,'-f','-'],
                    json.dumps({'apiVersion':'v1','kind':'DeleteOptions','preconditions':{'uid':webhook['metadata']['uid']}}))
            return {'ready':True, 'version':admission_version}
        secret = existing('secret')
        if not secret:
            with tempfile.TemporaryDirectory(prefix='certificate-', dir=directory) as temp:
                crt, key = pathlib.Path(temp)/'tls.crt', pathlib.Path(temp)/'tls.key'
                p = subprocess.run(['openssl','req','-x509','-newkey','rsa:3072','-nodes','-days','3650',
                    '-keyout',str(key),'-out',str(crt),'-subj','/CN=vmm-edge-admission.kube-system.svc',
                    '-addext','subjectAltName=DNS:vmm-edge-admission.kube-system.svc,DNS:vmm-edge-admission.kube-system.svc.cluster.local'],
                    capture_output=True, timeout=15)
                if p.returncode: raise ValueError('Could not generate the private admission serving certificate')
                secret = {'apiVersion':'v1','kind':'Secret','metadata':metadata('Secret'),'type':'kubernetes.io/tls',
                          'data':{'tls.crt':base64.b64encode(crt.read_bytes()).decode(),'tls.key':base64.b64encode(key.read_bytes()).decode()}}
                kube(['create','-f','-'], json.dumps(secret))
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            p = subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin',name], capture_output=True, timeout=10)
            if p.returncode: raise ValueError('Could not create the admission service account')
            account = pwd.getpwnam(name)
        os.chown(directory, 0, account.pw_gid); os.chmod(directory, 0o750)
        content = {'server.py':admission_server, 'tls.crt':base64.b64decode(secret['data']['tls.crt']).decode(),
                   'tls.key':base64.b64decode(secret['data']['tls.key']).decode()}
        signature = hashlib.sha256((admission_version + secret['data']['tls.crt']).encode()).hexdigest()
        marker = directory / 'version'
        changed = not marker.exists() or marker.read_text() != signature
        unit = '[Unit]\nDescription=VM Manager Kubernetes load-balancer admission\nAfter=network-online.target k3s.service\nWants=network-online.target\n[Service]\nUser=vmm-edge-admission\nGroup=vmm-edge-admission\nExecStart=/usr/bin/python3 /var/lib/vmm-edge-admission/server.py\nRestart=always\nRestartSec=2\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nRestrictSUIDSGID=yes\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n'
        if changed:
            for filename, text in content.items():
                private(directory / filename, text)
                os.chown(directory / filename, 0, account.pw_gid); os.chmod(directory / filename, 0o640)
            pathlib.Path('/etc/systemd/system/vmm-edge-admission.service').write_text(unit)
            subprocess.run(['systemctl','daemon-reload'], check=True, capture_output=True, timeout=10)
        subprocess.run(['systemctl','enable','--now',name], check=True, capture_output=True, timeout=15)
        if changed:
            subprocess.run(['systemctl','restart',name], check=True, capture_output=True, timeout=15)
        context = ssl.create_default_context(cafile=str(directory / 'tls.crt'))
        context.check_hostname = False  # The trusted certificate is for the Service DNS name.
        for attempt in range(12):
            try:
                with urllib.request.urlopen('https://127.0.0.1:19443/healthz', context=context, timeout=2) as response:
                    if response.read() == b'ok': break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise ValueError('The cluster admission service did not become healthy')
        private(marker, signature)
        if r.get('admission_publish', True):
            nodes = get('nodes')['items']
            addresses = [a['address'] for node in nodes
                         if 'node-role.kubernetes.io/control-plane' in node['metadata'].get('labels', {})
                         and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in node.get('status', {}).get('conditions', []))
                         for a in node.get('status', {}).get('addresses', []) if a['type'] == 'InternalIP' and ':' not in a['address']]
            if not addresses: raise ValueError('No Ready control-plane endpoint is available for admission')
            for kind in ('service','endpointslice','mutatingwebhookconfiguration'): existing(kind)
            save({'apiVersion':'v1','kind':'Service','metadata':metadata('Service'),
                  'spec':{'ports':[{'name':'https','port':443,'targetPort':19443}]}})
            endpoints_metadata = metadata('EndpointSlice')
            endpoints_metadata['labels'] = dict(labels, **{'kubernetes.io/service-name':name,'endpointslice.kubernetes.io/managed-by':'vm-manager'})
            save({'apiVersion':'discovery.k8s.io/v1','kind':'EndpointSlice','metadata':endpoints_metadata,'addressType':'IPv4',
                  'ports':[{'name':'https','protocol':'TCP','port':19443}],
                  'endpoints':[{'addresses':[address],'conditions':{'ready':True}} for address in sorted(set(addresses))]})
            save({'apiVersion':'admissionregistration.k8s.io/v1','kind':'MutatingWebhookConfiguration',
                  'metadata':metadata('MutatingWebhookConfiguration'), 'webhooks':[{
                      'name':'loadbalancer.vmm.carrtech.dev','admissionReviewVersions':['v1'],'sideEffects':'None',
                      'failurePolicy':'Fail','timeoutSeconds':5,'reinvocationPolicy':'Never',
                      'clientConfig':{'service':{'namespace':'kube-system','name':name,'path':'/mutate','port':443},'caBundle':secret['data']['tls.crt']},
                      'namespaceSelector':{'matchExpressions':[{'key':'kubernetes.io/metadata.name','operator':'NotIn',
                                             'values':['kube-system','kube-public','kube-node-lease']}]},
                      'matchConditions':[{'name':'default-loadbalancer','expression':"object.spec.type == 'LoadBalancer' && !has(object.spec.loadBalancerClass)"},
                                         {'name':'not-opted-out','expression':"!has(object.metadata.annotations) || !('vmm.carrtech.dev/load-balancer' in object.metadata.annotations) || object.metadata.annotations['vmm.carrtech.dev/load-balancer'] != 'disabled'"}],
                      'rules':[{'apiGroups':[''],'apiVersions':['v1'],'operations':['CREATE'],'resources':['services'],'scope':'Namespaced'}]}]})
        return {'ready':True, 'version':admission_version}
'''

VERSION = hashlib.sha256((SERVER + BOOTSTRAP).encode()).hexdigest()
GUEST = 'admission_server = ' + repr(SERVER) + '\nadmission_version = ' + repr(VERSION) + '\n' + BOOTSTRAP
