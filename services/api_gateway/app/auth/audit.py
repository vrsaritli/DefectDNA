from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase


async def write_audit(
    database: AsyncIOMotorDatabase[dict[str, Any]],
    *,
    action: str,
    actor_user_id: Any = None,
    project_id: str | None = None,
    entity: str | None = None,
    entity_id: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await database["audit_log"].insert_one(
        {
            "ts": datetime.now(UTC),
            "actor_user_id": actor_user_id,
            "project_id": project_id,
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "detail": detail or {},
        }
    )
