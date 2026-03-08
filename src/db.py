"""MongoDB connection and shared aggregation pipeline fragments."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
from pymongo.database import Database

_client: MongoClient | None = None


def get_db() -> Database:
    """Return the renderboard Database. Reuses a module-level client."""
    global _client
    uri = os.environ["MONGODB_URI"]
    if _client is None:
        _client = MongoClient(uri)
    return _client.get_database()


# Aggregation pipeline: unwind chatSession.messages for per-message analysis.
# Yields documents with storyId, userId, level, score, and a nested `message` field.
pipeline_messages_for_analysis: list[dict] = [
    {"$match": {"test": {"$ne": True}}},
    {"$unwind": "$chatSession.messages"},
    {
        "$project": {
            "storyId": "$id",
            "userId": 1,
            "level": "$improvementState.level",
            "score": "$improvementState.score",
            "message": "$chatSession.messages",
        }
    },
]

# Aggregation pipeline: unwind improvementState.pointsEvents for per-event analysis.
# Yields documents with storyId, userId, level, and a nested `event` field.
pipeline_points_events_all: list[dict] = [
    {"$match": {"test": {"$ne": True}}},
    {"$unwind": "$improvementState.pointsEvents"},
    {
        "$project": {
            "storyId": "$id",
            "userId": 1,
            "level": "$improvementState.level",
            "event": "$improvementState.pointsEvents",
        }
    },
]
