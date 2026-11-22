from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument


class JobQueue(ABC):
    @abstractmethod
    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    @abstractmethod
    async def claim(self, job_type: str, owner: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def complete(self, job_id: str, owner: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def fail(self, job_id: str, owner: str, error: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def get(self, job_id: str) -> dict[str, Any] | None:
        raise NotImplementedError


class MongoJobQueue(JobQueue):
    def __init__(
        self,
        database: AsyncIOMotorDatabase[dict[str, Any]],
        collection_name: str = "jobs",
    ) -> None:
        self.collection = database[collection_name]

    async def ensure_indexes(self) -> None:
        await self.collection.create_index(
            [("type", 1), ("status", 1), ("lease_expires_at", 1)]
        )

    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> str:
        now = datetime.now(UTC)
        result = await self.collection.insert_one(
            {
                "type": job_type,
                "payload": payload,
                "status": "PENDING",
                "attempts": 0,
                "owner": None,
                "lease_expires_at": None,
                "created_at": now,
                "updated_at": now,
            }
        )
        return str(result.inserted_id)

    async def claim(self, job_type: str, owner: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=lease_seconds)
        claimed = await self.collection.find_one_and_update(
            {
                "type": job_type,
                "$or": [
                    {"status": "PENDING"},
                    {
                        "status": {"$in": ["CLAIMED", "RUNNING"]},
                        "lease_expires_at": {"$lte": now},
                    },
                ],
            },
            {
                "$set": {
                    "status": "CLAIMED",
                    "owner": owner,
                    "lease_expires_at": lease_expires,
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return claimed

    async def complete(self, job_id: str, owner: str) -> bool:
        from bson import ObjectId

        now = datetime.now(UTC)
        result = await self.collection.update_one(
            {"_id": ObjectId(job_id), "owner": owner, "status": {"$in": ["CLAIMED", "RUNNING"]}},
            {"$set": {"status": "SUCCEEDED", "lease_expires_at": None, "updated_at": now}},
        )
        return result.modified_count == 1

    async def fail(self, job_id: str, owner: str, error: str) -> bool:
        from bson import ObjectId

        now = datetime.now(UTC)
        result = await self.collection.update_one(
            {"_id": ObjectId(job_id), "owner": owner},
            {
                "$set": {
                    "status": "FAILED",
                    "error": error,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            },
        )
        return result.modified_count == 1

    async def get(self, job_id: str) -> dict[str, Any] | None:
        from bson import ObjectId

        return await self.collection.find_one({"_id": ObjectId(job_id)})
