"""MongoDB connection and all aggregation pipelines for the OTT Play dashboard."""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


@lru_cache(maxsize=1)
def get_db():
    client = MongoClient(os.environ["MONGODB_URI"])
    return client["renderboard"]


def get_collection():
    return get_db()["miniStories"]


def base_match(include_test: bool = False) -> dict:
    """Base match stage that optionally excludes test documents."""
    if include_test:
        return {}
    return {"test": {"$ne": True}}


# ---------------------------------------------------------------------------
# Tab 1: Progression & Funnel pipelines
# ---------------------------------------------------------------------------


def pipeline_level_distribution(include_test: bool = False) -> list:
    return [
        {"$match": base_match(include_test)},
        {"$group": {"_id": "$improvementState.level", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]


def pipeline_story_status(include_test: bool = False) -> list:
    return [
        {"$match": base_match(include_test)},
        {
            "$addFields": {
                "messageCount": {
                    "$size": {"$ifNull": ["$chatSession.messages", []]}
                }
            }
        },
        {
            "$addFields": {
                "storyStatus": {
                    "$switch": {
                        "branches": [
                            {
                                "case": {"$gte": ["$improvementState.level", 4]},
                                "then": "submitted",
                            },
                            {
                                "case": {"$gt": ["$messageCount", 0]},
                                "then": "wip",
                            },
                        ],
                        "default": "not_started",
                    }
                }
            }
        },
        {"$group": {"_id": "$storyStatus", "count": {"$sum": 1}}},
    ]


def pipeline_points_distribution(include_test: bool = False) -> list:
    return [
        {"$match": base_match(include_test)},
        {
            "$project": {
                "score": "$improvementState.score",
                "level": "$improvementState.level",
                "userId": 1,
                "title": 1,
            }
        },
    ]


def pipeline_topic_balance(include_test: bool = False) -> list:
    return [
        {"$match": base_match(include_test)},
        {"$unwind": "$improvementState.pointsEvents"},
        {"$match": {"improvementState.pointsEvents.type": "interaction"}},
        {
            "$group": {
                "_id": "$improvementState.pointsEvents.topic",
                "count": {"$sum": 1},
                "totalGained": {"$sum": "$improvementState.pointsEvents.gained"},
                "strongCount": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$improvementState.pointsEvents.score",
                                    "strong",
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "basicCount": {
                    "$sum": {
                        "$cond": [
                            {
                                "$eq": [
                                    "$improvementState.pointsEvents.score",
                                    "basic",
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]


def pipeline_pitch_quality(include_test: bool = False) -> list:
    """Points gained from the initial pitch event per story."""
    return [
        {"$match": base_match(include_test)},
        {"$unwind": "$improvementState.pointsEvents"},
        {"$match": {"improvementState.pointsEvents.type": "pitch"}},
        {
            "$project": {
                "pitchPoints": "$improvementState.pointsEvents.gained",
                "userId": 1,
                "title": 1,
            }
        },
    ]


def pipeline_dau(include_test: bool = False) -> list:
    return [
        {"$match": base_match(include_test)},
        {"$unwind": "$chatSession.messages"},
        {"$match": {"chatSession.messages.role": "user"}},
        {
            "$addFields": {
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$chatSession.messages.timestamp",
                    }
                }
            }
        },
        {"$group": {"_id": {"day": "$day", "userId": "$userId"}}},
        {"$group": {"_id": "$_id.day", "dau": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]


def pipeline_user_activity_dates(include_test: bool = False) -> list:
    """Per-user set of active days (for retention calculation)."""
    return [
        {"$match": base_match(include_test)},
        {"$unwind": "$chatSession.messages"},
        {"$match": {"chatSession.messages.role": "user"}},
        {
            "$addFields": {
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$chatSession.messages.timestamp",
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$userId",
                "activeDays": {"$addToSet": "$day"},
                "firstActivity": {"$min": "$chatSession.messages.timestamp"},
            }
        },
    ]


def pipeline_entity_counts(include_test: bool = False) -> list:
    """Characters and locations created per story."""
    return [
        {"$match": base_match(include_test)},
        {
            "$project": {
                "userId": 1,
                "title": 1,
                "level": "$improvementState.level",
                "characterCount": {
                    "$size": {
                        "$objectToArray": {
                            "$ifNull": ["$improvementState.characters", {}]
                        }
                    }
                },
                "locationCount": {
                    "$size": {
                        "$objectToArray": {
                            "$ifNull": ["$improvementState.locations", {}]
                        }
                    }
                },
            }
        },
    ]


def pipeline_wall_detection(include_test: bool = False) -> list:
    """Level distribution enriched with avg score and event count for wall detection."""
    return [
        {"$match": base_match(include_test)},
        {
            "$project": {
                "level": "$improvementState.level",
                "score": "$improvementState.score",
                "pointsEventCount": {
                    "$size": {
                        "$ifNull": ["$improvementState.pointsEvents", []]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$level",
                "count": {"$sum": 1},
                "avgScore": {"$avg": "$score"},
                "avgEventCount": {"$avg": "$pointsEventCount"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def pipeline_points_events_all(include_test: bool = False) -> list:
    """All points events with story context — used for level-up time computation."""
    return [
        {"$match": base_match(include_test)},
        {
            "$project": {
                "storyId": "$id",
                "userId": 1,
                "title": 1,
                "currentLevel": "$improvementState.level",
                "pointsEvents": "$improvementState.pointsEvents",
            }
        },
    ]


# ---------------------------------------------------------------------------
# Tab 2: Guru Chat Performance pipelines
# ---------------------------------------------------------------------------


def pipeline_chat_stats(include_test: bool = False) -> list:
    """Total/user/assistant message counts per story."""
    return [
        {"$match": base_match(include_test)},
        {
            "$addFields": {
                "totalMessages": {
                    "$size": {"$ifNull": ["$chatSession.messages", []]}
                },
                "userMessages": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$chatSession.messages", []]},
                            "as": "m",
                            "cond": {"$eq": ["$$m.role", "user"]},
                        }
                    }
                },
                "assistantMessages": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$chatSession.messages", []]},
                            "as": "m",
                            "cond": {"$eq": ["$$m.role", "assistant"]},
                        }
                    }
                },
                "mcqMessages": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$chatSession.messages", []]},
                            "as": "m",
                            "cond": {
                                "$gt": [
                                    {"$size": {"$ifNull": ["$$m.toolCalls", []]}},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "userId": 1,
                "title": 1,
                "level": "$improvementState.level",
                "score": "$improvementState.score",
                "totalMessages": 1,
                "userMessages": 1,
                "assistantMessages": 1,
                "mcqMessages": 1,
                "modelId": "$chatSession.guruConfig.modelId",
                "promptVersion": "$chatSession.guruConfig.systemPromptVersion",
            }
        },
    ]


def pipeline_token_usage(include_test: bool = False) -> list:
    """Token usage aggregated by day."""
    return [
        {"$match": base_match(include_test)},
        {"$unwind": "$chatSession.messages"},
        {
            "$match": {
                "chatSession.messages.role": "assistant",
                "chatSession.messages.usage": {"$exists": True},
            }
        },
        {
            "$addFields": {
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$chatSession.messages.timestamp",
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$day",
                "totalInputTokens": {
                    "$sum": "$chatSession.messages.usage.input_tokens"
                },
                "totalOutputTokens": {
                    "$sum": "$chatSession.messages.usage.output_tokens"
                },
                "totalCachedTokens": {
                    "$sum": "$chatSession.messages.usage.cached_tokens"
                },
                "messageCount": {"$sum": 1},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def pipeline_messages_for_analysis(include_test: bool = False) -> list:
    """Full messages list per story for MCQ/lag/regen analysis in Python."""
    return [
        {"$match": base_match(include_test)},
        {
            "$project": {
                "storyId": "$id",
                "userId": 1,
                "title": 1,
                "level": "$improvementState.level",
                "topicHistory": 1,
                "messages": {
                    "$map": {
                        "input": {"$ifNull": ["$chatSession.messages", []]},
                        "as": "m",
                        "in": {
                            "role": "$$m.role",
                            "content": "$$m.content",
                            "messageId": "$$m.messageId",
                            "timestamp": "$$m.timestamp",
                            "messageType": "$$m.messageType",
                            "toolCalls": {"$ifNull": ["$$m.toolCalls", []]},
                            "usage": {"$ifNull": ["$$m.usage", None]},
                        },
                    }
                },
            }
        },
    ]


def pipeline_stagnation(include_test: bool = False) -> list:
    """Flag stories with >10 user messages and still at level 1 (stagnant)."""
    return [
        {"$match": base_match(include_test)},
        {
            "$addFields": {
                "userMessageCount": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$chatSession.messages", []]},
                            "as": "m",
                            "cond": {"$eq": ["$$m.role", "user"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "isStagnant": {
                    "$and": [
                        {"$gt": ["$userMessageCount", 10]},
                        {"$lt": ["$improvementState.level", 2]},
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "total": {"$sum": 1},
                "stagnant": {"$sum": {"$cond": ["$isStagnant", 1, 0]}},
            }
        },
    ]
