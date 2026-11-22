from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase


async def ensure_auth_indexes(
    database: AsyncIOMotorDatabase[dict[str, Any]],
) -> None:
    await database["users"].create_index("username", unique=True)
    await database["user_project_roles"].create_index(
        [("user_id", 1), ("project_id", 1)], unique=True
    )
    await database["user_project_roles"].create_index([("project_id", 1), ("role", 1)])
    await database["sessions"].create_index("user_id")
    await database["sessions"].create_index("expires_at", expireAfterSeconds=0)
    await database["rate_limits"].create_index("expires_at", expireAfterSeconds=0)
    await database["audit_log"].create_index([("ts", -1)])
    await database["audit_log"].create_index([("actor_user_id", 1), ("ts", -1)])
