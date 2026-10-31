"""Named, reusable VM images backed by the verified portable archive engine."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .hypervisor import HypervisorError


class Capture(BaseModel):
    vm_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    block_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    mode: Literal['live', 'shutdown', 'stopped'] = 'stopped'

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Enter a template name without control characters')
        return value


def catalog(store):
    with store.db() as db:
        return [dict(r) for r in db.execute('''
            SELECT b.*, t.name, t.description FROM vm_templates t
            JOIN vm_backups b ON b.id=t.id ORDER BY b.created DESC
        ''')]


def record(store, template_id):
    with store.db() as db:
        row = db.execute('''SELECT b.*, t.name, t.description FROM vm_templates t
            JOIN vm_backups b ON b.id=t.id WHERE t.id=?''', (template_id,)).fetchone()
    if not row:
        raise HypervisorError('Template not found')
    return dict(row)
