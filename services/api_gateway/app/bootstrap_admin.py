"""Create the first project and Admin account for an empty Phase 2 installation."""

import asyncio
import re
from datetime import UTC, datetime
from getpass import getpass
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from libs.common.config import settings
from libs.common.database import create_mongo_client
from services.api_gateway.app.auth.audit import write_audit
from services.api_gateway.app.auth.passwords import PasswordPolicyError, hash_password
from services.api_gateway.app.auth.repository import ensure_auth_indexes


async def bootstrap() -> None:
    if len(settings.session_signing_key.encode("utf-8")) < 32:
        raise RuntimeError("Set SESSION_SIGNING_KEY to a random value of at least 32 bytes")
    if not settings.mongodb_uri:
        raise RuntimeError("MONGODB_URI is required")

    username = input("Initial Admin username: ").strip().casefold()
    display_name = input("Initial Admin display name: ").strip()
    project_id = input("Initial project ID (lowercase slug): ").strip().casefold()
    project_name = input("Initial project name: ").strip()
    password = getpass("Initial Admin password: ")
    if not re.fullmatch(r"[a-z0-9._@+-]{3,254}", username):
        raise RuntimeError("Username must be an email-style name or handle")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", project_id):
        raise RuntimeError("Project ID must be a lowercase slug")
    if not display_name or not project_name:
        raise RuntimeError("Display name and project name are required")
    try:
        password_hash = hash_password(password, username)
    except PasswordPolicyError as exc:
        raise RuntimeError(str(exc)) from exc

    client: AsyncIOMotorClient[dict[str, Any]] = create_mongo_client(settings.mongodb_uri)
    try:
        database = client[settings.mongodb_db]
        await ensure_auth_indexes(database)
        if await database["users"].find_one({"username": username}) is not None:
            raise RuntimeError("That username already exists")
        if await database["projects"].find_one({"_id": project_id}) is not None:
            raise RuntimeError("That project ID already exists")
        now = datetime.now(UTC)
        result = await database["users"].insert_one(
            {
                "username": username,
                "display_name": display_name,
                "password_hash": password_hash,
                "must_change_password": False,
                "is_active": True,
                "failed_login_count": 0,
                "locked_until": None,
                "last_login_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        await database["projects"].insert_one(
            {
                "_id": project_id,
                "name": project_name,
                "created_at": now,
                "updated_at": now,
            }
        )
        await database["user_project_roles"].insert_one(
            {
                "user_id": result.inserted_id,
                "project_id": project_id,
                "role": "ADMIN",
                "created_at": now,
                "updated_at": now,
            }
        )
        await write_audit(
            database,
            action="INITIAL_ADMIN_CREATED",
            actor_user_id=result.inserted_id,
            project_id=project_id,
            entity="users",
            entity_id=result.inserted_id,
        )
    finally:
        client.close()

    print("Initial Admin and project created.")


if __name__ == "__main__":
    asyncio.run(bootstrap())
