"""
Create MongoDB views for PostHog Data Warehouse.

Views are read-only — they compute on the fly from source collections.
No ETL sync needed; data is always fresh.

Usage:
    uv run python scripts/create_views.py
"""

import argparse

from src.db import get_db


def create_topic_events_view(db) -> None:
    """
    analytics_topic_events_view — one row per pointsEvent per story.
    Source: miniStories.improvementState.pointsEvents (nested array)
    """
    view_name = "analytics_topic_events_view"

    # Drop if exists so re-runs are idempotent
    if view_name in db.list_collection_names():
        db.drop_collection(view_name)
        print(f"  Dropped existing {view_name}.")

    pipeline = [
        {
            "$unwind": {
                "path": "$improvementState.pointsEvents",
                "includeArrayIndex": "eventIndex",
                "preserveNullAndEmptyArrays": False,
            }
        },
        {
            "$project": {
                "_id": 0,
                "storyId": "$id",
                "userId": "$userId",
                "eventIndex": "$eventIndex",
                "eventType": "$improvementState.pointsEvents.type",
                "topic": "$improvementState.pointsEvents.topic",
                "quality": "$improvementState.pointsEvents.score",
                "gained": "$improvementState.pointsEvents.gained",
                "totalAfter": "$improvementState.pointsEvents.totalAfter",
                "createdAt": "$improvementState.pointsEvents.createdAt",
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$improvementState.pointsEvents.createdAt",
                    }
                },
                "isTest": "$test",
            }
        },
    ]

    db.create_collection(view_name, viewOn="miniStories", pipeline=pipeline)
    count = db[view_name].count_documents({})
    print(f"  Created {view_name} — {count} rows visible.")


def create_user_snapshots_view(db) -> None:
    """
    analytics_user_snapshots_view — one row per story with computed fields.
    Source: miniStories (viewOn)

    Notes:
    - mcqResponses: detected via regex on message content (MCQ options share a
      common prefix pattern); not as precise as ETL classify_user_messages() but
      works without cross-message lookups.
    - regenLoops: counts user messages starting with "Other" (matches ETL heuristic).
    - storyStatus: derived from storySubmission.status (submitted) or user message
      count (wip / not_started). improvementState.story.status is always "empty".
    - firstActivity / lastActivity: story createdAt / updatedAt (not per-message).
    - topicEntropy / levelUpTimes: omitted — require sequential Python logic.
    """
    view_name = "analytics_user_snapshots_view"

    if view_name in db.list_collection_names():
        db.drop_collection(view_name)
        print(f"  Dropped existing {view_name}.")

    # Regex that matches the common prefixes of MCQ option texts.
    # User messages starting with these are counted as MCQ responses;
    # all other user messages are counted as open-ended responses.
    MCQ_REGEX = "^(Elias|For extra points|Other)"

    pipeline = [
        {
            "$addFields": {
                "storyId": "$id",
                "mongoId": "$_id",
                "score": "$improvementState.score",
                "level": "$improvementState.level",
                "storyStatus": {
                    "$cond": {
                        "if": {"$eq": ["$storySubmission.status", "submitted"]},
                        "then": "submitted",
                        "else": {
                            "$cond": {
                                "if": {
                                    "$gt": [
                                        {
                                            "$size": {
                                                "$filter": {
                                                    "input": {"$ifNull": ["$chatSession.messages", []]},
                                                    "as": "m",
                                                    "cond": {"$eq": ["$$m.role", "user"]},
                                                }
                                            }
                                        },
                                        0,
                                    ]
                                },
                                "then": "wip",
                                "else": "not_started",
                            }
                        },
                    }
                },
                "characterCount": {"$size": {"$ifNull": ["$characters", []]}},
                "locationCount": {"$size": {"$ifNull": ["$locations", []]}},
                "topicHistory": {"$ifNull": ["$topicHistory", []]},
                "messages": {"$ifNull": ["$chatSession.messages", []]},
            }
        },
        {
            "$addFields": {
                "totalMessages": {"$size": "$messages"},
                "userMessages": {
                    "$size": {
                        "$filter": {
                            "input": "$messages",
                            "as": "m",
                            "cond": {"$eq": ["$$m.role", "user"]},
                        }
                    }
                },
                "mcqResponses": {
                    "$size": {
                        "$filter": {
                            "input": "$messages",
                            "as": "m",
                            "cond": {
                                "$and": [
                                    {"$eq": ["$$m.role", "user"]},
                                    {
                                        "$regexMatch": {
                                            "input": {"$ifNull": ["$$m.content", ""]},
                                            "regex": MCQ_REGEX,
                                            "options": "i",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                "openEndedResponses": {
                    "$size": {
                        "$filter": {
                            "input": "$messages",
                            "as": "m",
                            "cond": {
                                "$and": [
                                    {"$eq": ["$$m.role", "user"]},
                                    {
                                        "$not": {
                                            "$regexMatch": {
                                                "input": {"$ifNull": ["$$m.content", ""]},
                                                "regex": MCQ_REGEX,
                                                "options": "i",
                                            }
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                # regenLoops: user messages starting with "Other" = user rejected
                # all preset MCQ options (dissatisfaction signal).
                # Matches the ETL's detect_regeneration_loops() heuristic.
                "regenLoops": {
                    "$size": {
                        "$filter": {
                            "input": "$messages",
                            "as": "m",
                            "cond": {
                                "$and": [
                                    {"$eq": ["$$m.role", "user"]},
                                    {
                                        "$regexMatch": {
                                            "input": {"$ifNull": ["$$m.content", ""]},
                                            "regex": "^Other",
                                            "options": "i",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                "totalInputTokens": {
                    "$sum": {
                        "$map": {
                            "input": "$messages",
                            "as": "m",
                            "in": {"$ifNull": ["$$m.usage.input_tokens", 0]},
                        }
                    }
                },
                "totalOutputTokens": {
                    "$sum": {
                        "$map": {
                            "input": "$messages",
                            "as": "m",
                            "in": {"$ifNull": ["$$m.usage.output_tokens", 0]},
                        }
                    }
                },
                "totalCachedTokens": {
                    "$sum": {
                        "$map": {
                            "input": "$messages",
                            "as": "m",
                            "in": {"$ifNull": ["$$m.usage.cached_tokens", 0]},
                        }
                    }
                },
                "firstActivity": "$createdAt",
                "lastActivity": "$updatedAt",
                "modelId": "$chatSession.guruConfig.modelId",
                "systemPromptVersion": "$chatSession.guruConfig.systemPromptVersion",
                "isTest": "$test",
                "snapshotAt": "$$NOW",
            }
        },
        {
            "$project": {
                "_id": 0,
                "userId": 1,
                "mongoId": 1,
                "storyId": 1,
                "title": 1,
                "level": 1,
                "score": 1,
                "storyStatus": 1,
                "totalMessages": 1,
                "userMessages": 1,
                "mcqResponses": 1,
                "openEndedResponses": 1,
                "regenLoops": 1,
                "characterCount": 1,
                "locationCount": 1,
                "topicHistory": 1,
                "totalInputTokens": 1,
                "totalOutputTokens": 1,
                "totalCachedTokens": 1,
                "firstActivity": 1,
                "lastActivity": 1,
                "modelId": 1,
                "systemPromptVersion": 1,
                "isTest": 1,
                "snapshotAt": 1,
            }
        },
    ]

    db.create_collection(view_name, viewOn="miniStories", pipeline=pipeline)
    count = db[view_name].count_documents({})
    print(f"  Created {view_name} — {count} rows visible.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create MongoDB views.")
    parser.add_argument(
        "--view",
        choices=["topic_events", "user_snapshots"],
        help="Which view to create. Omit to create all.",
    )
    args = parser.parse_args()

    db = get_db()
    print("Creating MongoDB views...")

    if args.view == "topic_events":
        create_topic_events_view(db)
    elif args.view == "user_snapshots":
        create_user_snapshots_view(db)
    else:
        create_user_snapshots_view(db)
        create_topic_events_view(db)

    print("Done.")


if __name__ == "__main__":
    main()
