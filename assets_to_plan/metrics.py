"""Pure Python metric computations on top of MongoDB pipeline results."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# MCQ / Open-Ended Classification
# ---------------------------------------------------------------------------

OTHER_SENTINEL = "Other — type your own answer"


def classify_user_messages(messages: list[dict]) -> list[dict]:
    """
    Enrich each message with a `msgClass` field:
      - "assistant"      : all assistant messages
      - "mcq_response"   : user picked one of the provided MCQ options
      - "open_ended"     : user typed their own answer (no preceding MCQ, or chose Other)
    """
    classified = []
    pending_options: list[str] | None = None

    for msg in messages:
        if msg["role"] == "assistant":
            tool_calls = msg.get("toolCalls") or []
            mcq_calls = [
                tc
                for tc in tool_calls
                if tc.get("type") == "workflow_multiple_choice"
            ]
            if mcq_calls:
                opts = mcq_calls[0].get("data", {}).get("options", [])
                pending_options = [o for o in opts if o != OTHER_SENTINEL]
            else:
                pending_options = None
            classified.append({**msg, "msgClass": "assistant"})
        elif msg["role"] == "user":
            user_text = (msg.get("content") or "").strip()
            if pending_options is not None and user_text in pending_options:
                msg_class = "mcq_response"
            else:
                msg_class = "open_ended"
            classified.append({**msg, "msgClass": msg_class})
            pending_options = None  # reset after user responds
        else:
            classified.append({**msg, "msgClass": "unknown"})

    return classified


def count_mcq_vs_open(classified_messages: list[dict]) -> dict:
    mcq = sum(1 for m in classified_messages if m.get("msgClass") == "mcq_response")
    open_ended = sum(
        1 for m in classified_messages if m.get("msgClass") == "open_ended"
    )
    return {"mcq": mcq, "open_ended": open_ended, "total_user": mcq + open_ended}


# ---------------------------------------------------------------------------
# Regeneration Loop Detection
# ---------------------------------------------------------------------------


def detect_regeneration_loops(classified_messages: list[dict]) -> int:
    """
    Count instances where a user message is immediately followed by another user
    message (no assistant reply between them). This signals the user rejected
    the MCQ options and typed freely, then sent another message.
    """
    loops = 0
    prev_role: str | None = None
    for msg in classified_messages:
        if msg["role"] == "user" and prev_role == "user":
            loops += 1
        prev_role = msg["role"]
    return loops


# ---------------------------------------------------------------------------
# Instruction Lag (time between Guru prompt and user response)
# ---------------------------------------------------------------------------


def compute_instruction_lags(messages: list[dict]) -> list[float]:
    """
    Returns list of lag times in seconds for each (assistant → user) pair.
    Timestamps may be datetime objects (from pymongo) or ISO strings.
    """
    lags: list[float] = []
    prev_assistant_ts: datetime | None = None

    for msg in messages:
        ts = _parse_ts(msg.get("timestamp"))
        if ts is None:
            continue
        if msg["role"] == "assistant":
            prev_assistant_ts = ts
        elif msg["role"] == "user" and prev_assistant_ts is not None:
            delta = (ts - prev_assistant_ts).total_seconds()
            if 0 < delta < 86_400:  # ignore gaps > 24h (likely multi-session)
                lags.append(delta)
            prev_assistant_ts = None

    return lags


def _parse_ts(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    if isinstance(ts, dict) and "$date" in ts:
        raw = ts["$date"]
        if isinstance(raw, str):
            raw = raw.rstrip("Z")
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None
        if isinstance(raw, (int, float)):
            return datetime.utcfromtimestamp(raw / 1000)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.rstrip("Z"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Level-Up Time Computation
# ---------------------------------------------------------------------------


def compute_level_up_times(points_events: list[dict]) -> list[dict]:
    """
    Returns list of level transitions with duration in seconds.
    Uses cumulative totalAfter to detect when level × 100 threshold is crossed.
    """
    THRESHOLD = 100
    results: list[dict] = []

    if not points_events:
        return results

    sorted_events = sorted(
        points_events,
        key=lambda e: _parse_ts(e.get("createdAt")) or datetime.min,
    )

    level = 1
    level_start_time: datetime | None = None

    for event in sorted_events:
        ts = _parse_ts(event.get("createdAt"))
        if ts is None:
            continue
        if level_start_time is None:
            level_start_time = ts

        total_after = event.get("totalAfter", 0) or 0
        level_threshold = THRESHOLD * level

        if total_after >= level_threshold:
            duration = (ts - level_start_time).total_seconds()
            results.append(
                {
                    "from_level": level,
                    "to_level": level + 1,
                    "duration_seconds": max(duration, 0),
                    "level_up_at": ts,
                }
            )
            level += 1
            level_start_time = None  # next event starts new level timer

    return results


# ---------------------------------------------------------------------------
# Retention (D1 / D3 / D7)
# ---------------------------------------------------------------------------


def compute_retention(user_activity_rows: list[dict]) -> dict:
    """
    user_activity_rows: list of {"_id": userId, "activeDays": [...], "firstActivity": datetime}
    Returns {"D1": pct|None, "D3": pct|None, "D7": pct|None}
    """
    now = datetime.utcnow()
    cohort: dict[str, dict] = {}

    for row in user_activity_rows:
        days_raw = row.get("activeDays") or []
        days = sorted(days_raw)
        if not days:
            continue
        first = datetime.strptime(days[0], "%Y-%m-%d")
        cohort[row["_id"]] = {"first": first, "days": set(days)}

    results: dict[str, dict | None] = {}
    for label, delta in [("D1", 1), ("D3", 3), ("D7", 7)]:
        eligible = [u for u in cohort.values() if (now - u["first"]).days >= delta]
        if not eligible:
            results[label] = None
            continue
        retained = sum(
            1
            for u in eligible
            if (u["first"] + timedelta(days=delta)).strftime("%Y-%m-%d") in u["days"]
        )
        results[label] = {
            "pct": retained / len(eligible) * 100,
            "retained": retained,
            "eligible": len(eligible),
        }

    return results


# ---------------------------------------------------------------------------
# Topic Diversity (Shannon Entropy)
# ---------------------------------------------------------------------------


def topic_diversity_entropy(topic_history: list[str]) -> float:
    """Shannon entropy of topic distribution. Higher = more balanced."""
    if not topic_history:
        return 0.0
    counts: dict[str, int] = {}
    for t in topic_history:
        counts[t] = counts.get(t, 0) + 1
    total = len(topic_history)
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values() if c > 0)
    return round(entropy, 3)


# ---------------------------------------------------------------------------
# Stagnation Check Per Story
# ---------------------------------------------------------------------------


def is_stagnant(messages: list[dict], current_level: int, threshold: int = 10) -> bool:
    """True if user sent > threshold messages without progressing past level 1."""
    user_msgs = sum(1 for m in messages if m.get("role") == "user")
    return user_msgs > threshold and current_level < 2


# ---------------------------------------------------------------------------
# Wall Detection (level with most drop-off)
# ---------------------------------------------------------------------------


def detect_wall(level_distribution: list[dict]) -> int | None:
    """
    Given sorted level distribution rows [{"_id": level, "count": n}],
    returns the level where the steepest user drop-off occurs.
    """
    valid_rows = [r for r in level_distribution if r.get("_id") is not None]
    if len(valid_rows) < 2:
        return None
    sorted_rows = sorted(valid_rows, key=lambda r: r["_id"])
    max_drop = 0
    wall_level = None
    for i in range(1, len(sorted_rows)):
        prev_count = sorted_rows[i - 1]["count"]
        curr_count = sorted_rows[i]["count"]
        drop = prev_count - curr_count
        if drop > max_drop:
            max_drop = drop
            wall_level = sorted_rows[i - 1]["_id"]
    return wall_level


# ---------------------------------------------------------------------------
# Aggregate story-level stats from message analysis pipeline
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Session Computation
# ---------------------------------------------------------------------------

SESSION_GAP_MINUTES = 30


def compute_sessions(messages: list[dict], gap_minutes: int = SESSION_GAP_MINUTES) -> list[dict]:
    """
    Split a story's message list into sessions.
    A new session starts whenever the gap between consecutive messages > gap_minutes.
    Returns list of sessions, each: {"start": dt, "end": dt, "message_count": int,
                                      "duration_seconds": float}
    """
    if not messages:
        return []

    gap_seconds = gap_minutes * 60
    sorted_msgs = sorted(
        [m for m in messages if _parse_ts(m.get("timestamp")) is not None],
        key=lambda m: _parse_ts(m["timestamp"]),
    )
    if not sorted_msgs:
        return []

    sessions: list[dict] = []
    session_start = _parse_ts(sorted_msgs[0]["timestamp"])
    session_last = session_start
    count = 1

    for msg in sorted_msgs[1:]:
        ts = _parse_ts(msg["timestamp"])
        if (ts - session_last).total_seconds() > gap_seconds:
            sessions.append({
                "start": session_start,
                "end": session_last,
                "duration_seconds": (session_last - session_start).total_seconds(),
                "message_count": count,
            })
            session_start = ts
            count = 1
        else:
            count += 1
        session_last = ts

    sessions.append({
        "start": session_start,
        "end": session_last,
        "duration_seconds": (session_last - session_start).total_seconds(),
        "message_count": count,
    })
    return sessions


def compute_engagement_stats(story_rows: list[dict]) -> dict:
    """
    Aggregate session-level engagement stats across all stories.
    Returns summary dict with per-user and aggregate stats.
    """
    per_user: dict[str, dict] = {}

    for row in story_rows:
        user_id = row.get("userId", "unknown")
        messages = row.get("messages") or []
        sessions = compute_sessions(messages)

        if user_id not in per_user:
            per_user[user_id] = {"sessions": [], "active_days": set()}

        per_user[user_id]["sessions"].extend(sessions)

        for msg in messages:
            ts = _parse_ts(msg.get("timestamp"))
            if ts:
                per_user[user_id]["active_days"].add(ts.strftime("%Y-%m-%d"))

    user_stats = []
    all_session_durations: list[float] = []
    all_session_counts: list[int] = []
    all_active_days: list[int] = []

    for uid, data in per_user.items():
        s_count = len(data["sessions"])
        durations = [s["duration_seconds"] for s in data["sessions"]]
        active_days = len(data["active_days"])
        avg_dur = sum(durations) / len(durations) if durations else 0

        user_stats.append({
            "userId": uid,
            "sessionCount": s_count,
            "avgSessionDurationSeconds": avg_dur,
            "activeDays": active_days,
            "sessions": data["sessions"],
        })
        all_session_counts.append(s_count)
        all_session_durations.extend(durations)
        all_active_days.append(active_days)

    return {
        "per_user": user_stats,
        "avg_sessions_per_user": (
            sum(all_session_counts) / len(all_session_counts) if all_session_counts else 0
        ),
        "avg_session_duration_seconds": (
            sum(all_session_durations) / len(all_session_durations)
            if all_session_durations
            else 0
        ),
        "avg_active_days_per_user": (
            sum(all_active_days) / len(all_active_days) if all_active_days else 0
        ),
        "all_session_durations": all_session_durations,
        "all_session_counts": all_session_counts,
        "all_active_days": all_active_days,
    }


def enrich_story_stats(story_rows: list[dict]) -> list[dict]:
    """
    Takes output of pipeline_messages_for_analysis and returns enriched dicts
    with MCQ rate, regen loops, avg instruction lag, topic entropy.
    """
    enriched = []
    for row in story_rows:
        messages = row.get("messages") or []
        classified = classify_user_messages(messages)
        mcq_counts = count_mcq_vs_open(classified)
        regen = detect_regeneration_loops(classified)
        lags = compute_instruction_lags(messages)
        entropy = topic_diversity_entropy(row.get("topicHistory") or [])

        total_user = mcq_counts["total_user"]
        mcq_rate = (
            mcq_counts["mcq"] / total_user * 100 if total_user > 0 else None
        )

        enriched.append(
            {
                "storyId": row.get("storyId"),
                "userId": row.get("userId"),
                "title": row.get("title"),
                "level": row.get("level"),
                "totalMessages": len(messages),
                "userMessages": total_user,
                "mcqResponses": mcq_counts["mcq"],
                "openEndedResponses": mcq_counts["open_ended"],
                "mcqRate": mcq_rate,
                "regenLoops": regen,
                "avgInstructionLag": sum(lags) / len(lags) if lags else None,
                "instructionLags": lags,
                "topicEntropy": entropy,
                "topicHistory": row.get("topicHistory") or [],
                "classified_messages": classified,
            }
        )
    return enriched
