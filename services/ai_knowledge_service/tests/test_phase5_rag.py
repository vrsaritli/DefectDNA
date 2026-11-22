from mongomock_motor import AsyncMongoMockClient

from services.ai_knowledge_service.app.repository import retrieve_knowledge


async def test_project_filtered_retrieval_is_sorted_and_thresholded() -> None:
    client = AsyncMongoMockClient()
    database = client["defectdna"]
    await database["knowledge_entries"].insert_many([
        {"project_id": "project-a", "status": "ACTIVE", "defect_id": 101, "title": "Close match", "embedding": [1.0, 0.0]},
        {"project_id": "project-a", "status": "ACTIVE", "defect_id": 102, "title": "Wrong match", "embedding": [0.0, 1.0]},
        {"project_id": "project-b", "status": "ACTIVE", "defect_id": 103, "title": "Other project", "embedding": [1.0, 0.0]},
    ])

    cases = await retrieve_knowledge(database, project_id="project-a", query_embedding=[1.0, 0.0], top_k=5, threshold=0.65)

    assert [case["defect_id"] for case in cases] == [101]
