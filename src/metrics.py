"""Pure metric computation functions for analytics_sync.

No I/O — all functions take plain Python data structures and return values.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _parse_ts(ts: Any) -> datetime | None:
    """Parse a MongoDB timestamp value to a timezone-aware datetime.

    Handles:
    - dict with {"$date": "2026-03-02T18:00:16.293Z"}  (Extended JSON)
    - dict with {"$date": <milliseconds int>}
    - ISO 8601 string
    - datetime object (returned as-is, made UTC-aware if naive)
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    if isinstance(ts, dict) and "$date" in ts:
        val = ts["$date"]
        if isinstance(val, str):
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val / 1000, tz=timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def classify_user_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of messages with a `msgClass` field added to each.

    Classification rules:
    - assistant messages       → msgClass = "assistant_response"
    - user messages after an assistant message that had toolCalls → "mcq_response"
    - user messages after an assistant message with no toolCalls  → "open_ended"
    """
    classified: list[dict] = []
    prev_had_tool_calls = False
    for msg in messages:
        m = dict(msg)
        role = m.get("role", "")
        if role == "assistant":
            prev_had_tool_calls = bool(m.get("toolCalls"))
            m["msgClass"] = "assistant_response"
        elif role == "user":
            m["msgClass"] = "mcq_response" if prev_had_tool_calls else "open_ended"
            prev_had_tool_calls = False
        else:
            m["msgClass"] = "unknown"
        classified.append(m)
    return classified


def count_mcq_vs_open(classified: list[dict]) -> dict:
    """Count total user messages, MCQ responses, and open-ended responses."""
    mcq = open_ended = total_user = 0
    for m in classified:
        if m.get("role") == "user":
            total_user += 1
            cls = m.get("msgClass")
            if cls == "mcq_response":
                mcq += 1
            elif cls == "open_ended":
                open_ended += 1
    return {"total_user": total_user, "mcq": mcq, "open_ended": open_ended}


def detect_regeneration_loops(classified: list[dict]) -> int:
    """Count likely regeneration/dissatisfaction signals.

    Heuristic: a user MCQ response whose content starts with or contains
    "Other" (case-insensitive) signals the user rejected all preset options.
    """
    loops = 0
    for m in classified:
        if m.get("role") == "user" and m.get("msgClass") == "mcq_response":
            content = (m.get("content") or "").strip().lower()
            if content.startswith("other") or " other" in content:
                loops += 1
    return loops


def compute_instruction_lags(messages: list[dict]) -> list[float]:
    """Return lag seconds between each assistant prompt and the user's reply.

    Only counts lags < 24 hours (filters out multi-day session gaps).
    """
    lags: list[float] = []
    prev_assistant_ts: datetime | None = None
    for m in messages:
        ts = _parse_ts(m.get("timestamp"))
        role = m.get("role", "")
        if role == "assistant":
            prev_assistant_ts = ts
        elif role == "user":
            if prev_assistant_ts is not None and ts is not None:
                delta = (ts - prev_assistant_ts).total_seconds()
                if 0 < delta < 86_400:
                    lags.append(delta)
            prev_assistant_ts = None
    return lags


def compute_level_up_times(points_events: list[dict]) -> list[float]:
    """Return seconds elapsed between consecutive points events.

    Approximates the time cost of each level-up step.
    Returns an empty list if fewer than 2 events.
    """
    timestamps: list[datetime] = []
    for e in points_events:
        ts = _parse_ts(e.get("createdAt"))
        if ts is not None:
            timestamps.append(ts)
    if len(timestamps) < 2:
        return []
    timestamps.sort()
    return [
        (timestamps[i] - timestamps[i - 1]).total_seconds()
        for i in range(1, len(timestamps))
    ]


def topic_diversity_entropy(topic_history: list[str]) -> float:
    """Shannon entropy (bits) of the topic distribution.

    Higher value = more balanced spread across story / character / location.
    Returns 0.0 for an empty or single-topic history.
    """
    if not topic_history:
        return 0.0
    counts: dict[str, int] = {}
    for t in topic_history:
        counts[t] = counts.get(t, 0) + 1
    total = len(topic_history)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())
