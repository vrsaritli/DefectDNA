import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from services.api_gateway.app.auth.passwords import verify_password


class AccountLocked(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Account is temporarily locked")


class AuthProvider(Protocol):
    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None: ...


class LocalAuthProvider:
    def __init__(
        self,
        database: AsyncIOMotorDatabase[dict[str, Any]],
        max_failures: int,
        lockout_minutes: int,
    ) -> None:
        self.users = database["users"]
        self.max_failures = max_failures
        self.lockout_minutes = lockout_minutes

    async def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = await self.users.find_one({"username": username})
        if user is None:
            return None

        now = datetime.now(UTC)
        locked_until = user.get("locked_until")
        if locked_until is not None and locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until is not None and locked_until > now:
            raise AccountLocked(max(1, int((locked_until - now).total_seconds())))
        if locked_until is not None:
            await self.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"failed_login_count": 0, "locked_until": None}},
            )
            user["failed_login_count"] = 0

        if not user.get("is_active", False) or not await asyncio.to_thread(
            verify_password, password, user.get("password_hash", "")
        ):
            updated = await self.users.find_one_and_update(
                {"_id": user["_id"]},
                {
                    "$inc": {"failed_login_count": 1},
                    "$set": {"updated_at": now},
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated is not None and updated["failed_login_count"] >= self.max_failures:
                locked_until = now + timedelta(minutes=self.lockout_minutes)
                await self.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"locked_until": locked_until, "updated_at": now}},
                )
                raise AccountLocked(self.lockout_minutes * 60)
            return None

        return await self.users.find_one_and_update(
            {"_id": user["_id"], "is_active": True},
            {
                "$set": {
                    "failed_login_count": 0,
                    "locked_until": None,
                    "last_login_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
