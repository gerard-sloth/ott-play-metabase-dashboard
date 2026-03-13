"""
Analytics collections sync script for Metabase.

Populates three denormalized collections in the renderboard database:
  - analytics_user_snapshots  (upsert by userId)
  - analytics_chat_events     (insert-once by messageId)
  - analytics_daily_stats     (replace by day)

Usage:
    uv run python -m src.analytics_sync
    uv run python -m src.analytics_sync --include-test
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from pymongo import ASCENDING, errors as mongo_errors

from src.db import get_db, pipeline_messages_for_analysis, pipeline_points_events_all
from src.metrics import (
    classify_user_messages,
    compute_instruction_lags,
    compute_level_up_times,
    count_mcq_vs_open,
    detect_regeneration_loops,
    topic_diversity_entropy,
    _parse_ts,
)


# ---------------------------------------------------------------------------
# Index setup
# ---------------------------------------------------------------------------

def ensure_indexes(db) -> None:
    db["analytics_chat_events"].create_index(
        [("messageId", ASCENDING)], unique=True, background=True
    )
    db["analytics_user_snapshots"].create_index(
        [("userId", ASCENDING)], unique=True, background=True
    )
    db["analytics_daily_stats"].create_index(
        [("day", ASCENDING)], unique=True, background=True
    )
    print("  Indexes verified.")


# ---------------------------------------------------------------------------
# Process a single story document into snapshot + events
# ---------------------------------------------------------------------------

def process_story(story: dict) -> tuple[dict, list[dict]]:
    """
    Returns (user_snapshot, chat_events_list).
    """
    now = datetime.utcnow()
    story_oid = str(story.get("_id", ""))
    story_id = story.get("id", story_oid)
    user_id = story.get("userId", "")
    title = story.get("title", "")
    is_test = bool(story.get("test"))
    level = (story.get("improvementState") or {}).get("level", 0)
    score = (story.get("improvementState") or {}).get("score", 0)

    messages = (story.get("chatSession") or {}).get("messages") or []
    classified = classify_user_messages(messages)
    mcq_counts = count_mcq_vs_open(classified)
    regen_loops = detect_regeneration_loops(classified)
    lags = compute_instruction_lags(messages)
    entropy = topic_diversity_entropy(story.get("topicHistory") or [])

    points_events = (story.get("improvementState") or {}).get("pointsEvents") or []
    level_up_times = compute_level_up_times(points_events)

    # Token totals from assistant messages
    total_input = total_output = total_cached = 0
    for msg in messages:
        usage = msg.get("usage") or {}
        total_input += usage.get("input_tokens", 0) or 0
        total_output += usage.get("output_tokens", 0) or 0
        total_cached += usage.get("cached_tokens", 0) or 0

    # Activity timestamps
    user_ts = [
        _parse_ts(m.get("timestamp"))
        for m in messages
        if m.get("role") == "user"
    ]
    user_ts = [t for t in user_ts if t is not None]
    first_activity = min(user_ts) if user_ts else None
    last_activity = max(user_ts) if user_ts else None

    # Characters & locations
    char_map = (story.get("improvementState") or {}).get("characters") or {}
    loc_map = (story.get("improvementState") or {}).get("locations") or {}

    # Story status
    message_count = len(messages)
    if level >= 4:
        story_status = "submitted"
    elif message_count > 0:
        story_status = "wip"
    else:
        story_status = "not_started"

    guru_cfg = (story.get("chatSession") or {}).get("guruConfig") or {}
    model_id = guru_cfg.get("modelId")
    prompt_version = guru_cfg.get("systemPromptVersion")

    # ---- User snapshot ----
    snapshot = {
        "userId": user_id,
        "storyId": story_id,
        "title": title,
        "level": level,
        "score": score,
        "storyStatus": story_status,
        "totalMessages": len(messages),
        "userMessages": mcq_counts["total_user"],
        "mcqResponses": mcq_counts["mcq"],
        "openEndedResponses": mcq_counts["open_ended"],
        "regenLoops": regen_loops,
        "characterCount": len(char_map),
        "locationCount": len(loc_map),
        "topicHistory": story.get("topicHistory") or [],
        "topicEntropy": entropy,
        "levelUpTimes": level_up_times,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalCachedTokens": total_cached,
        "firstActivity": first_activity,
        "lastActivity": last_activity,
        "modelId": model_id,
        "systemPromptVersion": prompt_version,
        "isTest": is_test,
        "snapshotAt": now,
    }

    # ---- Chat events ----
    chat_events: list[dict] = []
    prev_assistant_ts: datetime | None = None

    for i, msg in enumerate(classified):
        ts = _parse_ts(msg.get("timestamp"))
        day_str = ts.strftime("%Y-%m-%d") if ts else None
        msg_id = msg.get("messageId", f"{story_id}__msg_{i}")
        role = msg.get("role", "unknown")
        msg_class = msg.get("msgClass", "unknown")

        lag = None
        if role == "user" and prev_assistant_ts is not None and ts is not None:
            delta = (ts - prev_assistant_ts).total_seconds()
            lag = delta if 0 < delta < 86_400 else None

        usage = msg.get("usage") or {}
        content_raw = msg.get("content") or ""
        has_mcq = bool((msg.get("toolCalls") or []))

        event = {
            "messageId": msg_id,
            "storyId": story_id,
            "userId": user_id,
            "role": role,
            "messageType": msg.get("messageType"),
            "msgClass": msg_class,
            "hasMCQ": has_mcq,
            "content": content_raw[:500],
            "timestamp": ts,
            "day": day_str,
            "level": level,
            "inputTokens": usage.get("input_tokens", 0) or 0,
            "outputTokens": usage.get("output_tokens", 0) or 0,
            "cachedTokens": usage.get("cached_tokens", 0) or 0,
            "instructionLagSeconds": lag,
            "isTest": is_test,
        }
        chat_events.append(event)

        if role == "assistant":
            prev_assistant_ts = ts
        elif role == "user":
            prev_assistant_ts = None

    return snapshot, chat_events


# ---------------------------------------------------------------------------
# Compute daily stats from a list of chat events
# ---------------------------------------------------------------------------

def compute_daily_stats(day: str, events: list[dict], now: datetime) -> dict:
    dau_users: set[str] = set()
    new_stories: set[str] = set()
    total_msgs = total_user_msgs = mcq_r = open_r = 0
    total_input = total_output = total_cached = 0
    level_ups = completions = 0
    lags: list[float] = []

    for e in events:
        dau_users.add(e["userId"])
        new_stories.add(e["storyId"])
        total_msgs += 1
        if e["role"] == "user":
            total_user_msgs += 1
            if e["msgClass"] == "mcq_response":
                mcq_r += 1
            elif e["msgClass"] == "open_ended":
                open_r += 1
        total_input += e.get("inputTokens", 0) or 0
        total_output += e.get("outputTokens", 0) or 0
        total_cached += e.get("cachedTokens", 0) or 0
        if e.get("instructionLagSeconds") is not None:
            lags.append(e["instructionLagSeconds"])

    return {
        "day": day,
        "dau": len(dau_users),
        "newStories": len(new_stories),
        "totalMessages": total_msgs,
        "totalUserMessages": total_user_msgs,
        "mcqResponses": mcq_r,
        "openEndedResponses": open_r,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalCachedTokens": total_cached,
        "levelUps": level_ups,
        "completions": completions,
        "avgInstructionLagSeconds": sum(lags) / len(lags) if lags else None,
        "computedAt": now,
    }


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync_all(include_test: bool = False) -> None:
    print(f"Starting analytics sync (include_test={include_test})…")
    db = get_db()
    col = db["miniStories"]
    now = datetime.utcnow()

    print("  Ensuring indexes…")
    ensure_indexes(db)

    query = {} if include_test else {"test": {"$ne": True}}
    stories = list(col.find(query))
    print(f"  Found {len(stories)} stories to process.")

    all_snapshots: list[dict] = []
    all_events: list[dict] = []
    daily_buckets: dict[str, list[dict]] = {}

    for story in stories:
        try:
            snapshot, events = process_story(story)
            all_snapshots.append(snapshot)
            all_events.extend(events)
            for event in events:
                day = event.get("day")
                if day:
                    daily_buckets.setdefault(day, []).append(event)
        except Exception as exc:
            print(f"  WARNING: Failed to process story {story.get('id')}: {exc}")

    # Upsert user snapshots
    snap_upserted = 0
    for snap in all_snapshots:
        db["analytics_user_snapshots"].replace_one(
            {"userId": snap["userId"]}, snap, upsert=True
        )
        snap_upserted += 1
    print(f"  Upserted {snap_upserted} user snapshots.")

    # Insert chat events (skip duplicates)
    events_inserted = events_skipped = 0
    for event in all_events:
        try:
            db["analytics_chat_events"].insert_one(event)
            events_inserted += 1
        except mongo_errors.DuplicateKeyError:
            events_skipped += 1
    print(
        f"  Inserted {events_inserted} chat events, "
        f"skipped {events_skipped} duplicates."
    )

    # Replace daily stats
    days_written = 0
    for day, day_events in daily_buckets.items():
        stats = compute_daily_stats(day, day_events, now)
        db["analytics_daily_stats"].replace_one({"day": day}, stats, upsert=True)
        days_written += 1
    print(f"  Wrote {days_written} daily stat records.")

    print("Sync complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync analytics collections for Metabase.")
    parser.add_argument(
        "--include-test",
        action="store_true",
        help="Include documents flagged as test=true",
    )
    args = parser.parse_args()
    sync_all(include_test=args.include_test)


if __name__ == "__main__":
    main()
