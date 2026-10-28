"""Manager-local resource grouping; independent of hypervisor tenancy and hardware."""

import sqlite3
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

Collection = Literal["tags", "projects"]
ResourceKind = Literal["vm", "network"]


class CollectionName(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Enter a name without control characters")
        return value


class Assignment(BaseModel):
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    tag_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("tag_ids")
    @classmethod
    def unique_tags(cls, values):
        if any(not value or len(value) > 64 for value in values):
            raise ValueError("Invalid tag reference")
        return list(dict.fromkeys(values))


def collections(store):
    with store.db() as db:
        return {
            kind: [
                dict(r)
                for r in db.execute(f"SELECT id,name FROM {kind} ORDER BY name_key")
            ]
            for kind in ("tags", "projects")
        }


def create_collection(store, kind: Collection, body):
    # Table names come only from the validated route literal.
    collection_id = uuid.uuid4().hex
    try:
        with store.db() as db:
            db.execute(
                f"INSERT INTO {kind}(id,name,name_key) VALUES(?,?,?)",
                (collection_id, body.name, body.name.casefold()),
            )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A name like this already exists") from exc
    return {"id": collection_id, "name": body.name}


def assign(store, kind: ResourceKind, resource_id, body):
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        if (
            body.project_id
            and not db.execute(
                "SELECT 1 FROM projects WHERE id=?", (body.project_id,)
            ).fetchone()
        ):
            raise HTTPException(404, "Project no longer exists; refresh and try again")
        for tag_id in body.tag_ids:
            if not db.execute("SELECT 1 FROM tags WHERE id=?", (tag_id,)).fetchone():
                raise HTTPException(404, "Tag no longer exists; refresh and try again")
        clear(store, kind, resource_id, db)
        if body.project_id:
            db.execute(
                "INSERT INTO resource_projects VALUES(?,?,?)",
                (kind, resource_id, body.project_id),
            )
        db.executemany(
            "INSERT INTO resource_tags VALUES(?,?,?)",
            [(kind, resource_id, tag_id) for tag_id in body.tag_ids],
        )


def clear(store, kind, resource_id, db=None):
    if db is None:
        with store.db() as connection:
            clear(store, kind, resource_id, connection)
        return
    db.execute(
        "DELETE FROM resource_projects WHERE kind=? AND resource_id=?",
        (kind, resource_id),
    )
    db.execute(
        "DELETE FROM resource_tags WHERE kind=? AND resource_id=?", (kind, resource_id)
    )


def decorate(store, inventory):
    with store.db() as db:
        db.execute("BEGIN")
        projects = {
            (r["kind"], r["resource_id"]): r["project_id"]
            for r in db.execute("SELECT * FROM resource_projects")
        }
        tags = {}
        for row in db.execute("SELECT * FROM resource_tags ORDER BY tag_id"):
            tags.setdefault((row["kind"], row["resource_id"]), []).append(row["tag_id"])
        labels = {
            kind: [
                dict(r)
                for r in db.execute(f"SELECT id,name FROM {kind} ORDER BY name_key")
            ]
            for kind in ("tags", "projects")
        }
    return {
        **inventory,
        **labels,
        **{
            key: [
                dict(
                    r,
                    project_id=projects.get((kind, r["id"])),
                    tag_ids=tags.get((kind, r["id"]), []),
                )
                for r in inventory[key]
            ]
            for kind, key in (("vm", "vms"), ("network", "networks"))
        },
    }
