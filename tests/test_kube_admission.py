import base64
import json

import pytest

from app import kube_admission, kubernetes


def response(**changes):
    namespace = {'__name__':'test'}
    exec(kube_admission.SERVER, namespace)
    request = {'uid':'request-uid','operation':'CREATE','resource':{'group':'','resource':'services'},
               'namespace':'apps','object':{'metadata':{},'spec':{'type':'LoadBalancer'}}} | changes
    return namespace['admission_response']({'request':request})['response']


def test_plain_service_receives_its_class_during_admission():
    result = response()
    assert result['allowed'] and result['uid'] == 'request-uid' and result['patchType'] == 'JSONPatch'
    assert json.loads(base64.b64decode(result['patch'])) == [{'op':'add','path':'/spec/loadBalancerClass','value':'vmm.carrtech.dev/edge'}]


@pytest.mark.parametrize('changes', [
    {'operation':'UPDATE'}, {'resource':{'group':'','resource':'pods'}}, {'namespace':'kube-system'},
    {'namespace':'kube-public'}, {'namespace':'kube-node-lease'},
    {'object':{'spec':{'type':'ClusterIP'}}}, {'object':{'spec':{'type':'NodePort'}}},
    {'object':{'spec':{'type':'LoadBalancer','loadBalancerClass':'another/provider'}}},
    {'object':{'metadata':{'annotations':{'vmm.carrtech.dev/load-balancer':'disabled'}},'spec':{'type':'LoadBalancer'}}},
])
def test_admission_excludes_ordinary_services_explicit_providers_and_opt_outs(changes):
    result = response(**changes)
    assert result['allowed'] and 'patch' not in result


def test_guest_dispatcher_contains_only_the_fixed_server_and_certificates_stay_in_guest():
    compile(kubernetes.GUEST,'guest','exec')
    assert kube_admission.VERSION in kubernetes.GUEST
    assert 'service.kubernetes.io/load-balancer-cleanup' in kubernetes.GUEST
    assert 'Group=vmm-edge-admission' in kube_admission.BOOTSTRAP
    assert "return {'ready':True, 'version':admission_version}" in kube_admission.BOOTSTRAP


def test_webhook_accepts_the_apiserver_timeout_query_string():
    import http.client
    import threading
    namespace = {'__name__':'test'}
    exec(kube_admission.SERVER,namespace)
    server = namespace['ThreadingHTTPServer'](('127.0.0.1',0),namespace['Handler'])
    worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
    try:
        body = json.dumps({'request':{'uid':'wire-request','operation':'CREATE','resource':{'group':'','resource':'services'},
            'namespace':'apps','object':{'spec':{'type':'LoadBalancer'}}}})
        connection = http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        connection.request('POST','/mutate?timeout=5s',body,{'Content-Type':'application/json'})
        result = connection.getresponse()
        assert result.status == 200
        payload = json.loads(result.read())
        assert payload['response']['uid'] == 'wire-request' and payload['response']['patchType'] == 'JSONPatch'
        connection.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)
