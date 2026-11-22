from typing import Any

import certifi
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError


async def ping_database(database: AsyncIOMotorDatabase[dict[str, Any]]) -> bool:
    try:
        await database.command("ping")
        return True
    except PyMongoError:
        return False


def create_mongo_client(uri: str) -> AsyncIOMotorClient[dict[str, Any]]:
    if not uri:
        raise RuntimeError("MONGODB_URI is required")
    return AsyncIOMotorClient(uri, tlsCAFile=certifi.where())
