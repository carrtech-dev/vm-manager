"""Server-owned feature entitlements; DevOps is bundled until licensing launches.

Keep access decisions here so a future installed licence provider can replace
the bundled grant without changing individual DevOps tools or deleting state.
"""
import os

from fastapi import HTTPException


def catalog():
    enabled = os.environ.get('VM_MANAGER_DEVOPS_ENABLED', 'true').lower() in ('true', '1', 'yes', 'on')
    return {'devops': {'enabled': enabled, 'source': 'included' if enabled else 'disabled'}}


def require_devops():
    if not catalog()['devops']['enabled']:
        raise HTTPException(403, 'DevOps is not enabled on this manager')


def devops_path(path):
    parts = path.strip('/').split('/')
    return parts[:2] == ['api', 'devops'] or (
        len(parts) >= 6 and parts[:3] == ['api', 'cluster', 'hosts'] and parts[4:6] == ['proxy', 'devops'])
