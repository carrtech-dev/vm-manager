"""Systemd entry point; subprocess arguments do not pass through a shell."""
import os
import uvicorn

os.chdir('/usr/lib/vm-manager')
import sys
sys.path.insert(0, os.getcwd())
uvicorn.run('app.main:app', host=os.environ.get('VM_MANAGER_HOST', '0.0.0.0'), port=8443,
            ssl_keyfile='/etc/vm-manager/tls/server.key',
            ssl_certfile='/etc/vm-manager/tls/server.crt', proxy_headers=False,
            access_log=False, timeout_keep_alive=10, ws_ping_interval=20, ws_ping_timeout=20)
