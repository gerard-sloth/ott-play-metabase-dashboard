"""
Run GEval metrics on miniStory conversations and store results in analytics_geval_scores.

One document per story. Upserts on re-runs so it is idempotent.

Requires OPENAI_API_KEY in environment (or set GEVAL_JUDGE_MODEL for another provider).

Usage:
    uv run python scripts/run_geval.py                       # all non-test stories
    uv run python scripts/run_geval.py --include-test        # include test stories
    uv run python scripts/run_geval.py --story-id <id>       # single story by id field
    uv run python scripts/run_geval.py --limit 5             # first N stories (for testing)
"""

from __future__ import annotations

import argparse
from datetime import datetime
from statistics import mean
from typing import Optional

from pymongo import ASCENDING

from src.db import get_db
from src.geval_metrics import (
    DetailFixationGEval,
    LanguageComplexityGEval,
    OffTopicUserMessageGEval,
    StoryHallucinationGEval,
    UserFrustrationGEval,
)

MIN_USER_MESSAGES = 2  # skip stories with fewer user messages — not enough signal


def evaluate_story(story: dict) -> dict:
    """Run all 5 GEval metrics on a single story and return the score document."""
    messages = (story.get("chatSession") or {}).get("messages") or []

    user_texts = [
        m["content"] for m in messages
        if m.get("role") == "user" and m.get("content") and isinstance(m["content"], str)
    ]
    assistant_texts = [
        m["content"] for m in messages
        if m.get("role") == "assistant" and m.get("content") and isinstance(m["content"], str)
    ]
    conversation_text = "\n".join(
        f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in messages
    )
    all_assistant_text = "\n\n".join(assistant_texts)

    # --- OffTopic: one call per user message ---
    offtopic_scores: list[float] = []
    try:
        offtopic_evaluator = OffTopicUserMessageGEval()
        for text in user_texts:
            result = offtopic_evaluator.evaluate_text(text)
            score = result.get("g_eval_score")
            if score is not None:
                offtopic_scores.append(float(score))
    except Exception as e:
        print(f"    WARNING: OffTopic evaluation failed: {e}")

    offtopic_result = {
        "avg_score": mean(offtopic_scores) if offtopic_scores else None,
        "max_score": max(offtopic_scores) if offtopic_scores else None,
        "offtopic_count": sum(1 for s in offtopic_scores if s >= 0.9),
        "message_count": len(offtopic_scores),
    }

    # --- Frustration: one call for the full conversation ---
    frustration_raw = {"g_eval_score": None, "g_eval_reason": None}
    try:
        frustration_raw = UserFrustrationGEval().evaluate_conversation(user_texts)
    except Exception as e:
        print(f"    WARNING: Frustration evaluation failed: {e}")

    # --- DetailFixation: one call ---
    fixation_raw = {"g_eval_score": None, "g_eval_reason": None}
    try:
        fixation_raw = DetailFixationGEval().evaluate_conversation(conversation_text, all_assistant_text)
    except Exception as e:
        print(f"    WARNING: DetailFixation evaluation failed: {e}")

    # --- StoryHallucination: one call ---
    hallucination_raw = {"g_eval_score": None, "g_eval_reason": None}
    try:
        hallucination_raw = StoryHallucinationGEval().evaluate_conversation(conversation_text, all_assistant_text)
    except Exception as e:
        print(f"    WARNING: StoryHallucination evaluation failed: {e}")

    # --- LanguageComplexity: one call per assistant message ---
    lang_scores: list[float] = []
    try:
        lang_evaluator = LanguageComplexityGEval()
        for text in assistant_texts:
            result = lang_evaluator.evaluate_turn(text)
            score = result.get("g_eval_score")
            if score is not None:
                lang_scores.append(float(score))
    except Exception as e:
        print(f"    WARNING: LanguageComplexity evaluation failed: {e}")

    lang_result = {
        "avg_score": mean(lang_scores) if lang_scores else None,
        "max_score": max(lang_scores) if lang_scores else None,
        "message_count": len(lang_scores),
    }

    return {
        "storyId": story.get("id"),
        "mongoId": str(story["_id"]),
        "userId": story.get("userId"),
        "isTest": bool(story.get("test")),
        "evaluatedAt": datetime.utcnow(),
        "messageCount": len(messages),
        "userMessageCount": len(user_texts),
        "assistantMessageCount": len(assistant_texts),
        "offTopic": offtopic_result,
        "frustration": {
            "score": frustration_raw.get("g_eval_score"),
            "reason": frustration_raw.get("g_eval_reason"),
        },
        "detailFixation": {
            "score": fixation_raw.get("g_eval_score"),
            "reason": fixation_raw.get("g_eval_reason"),
        },
        "storyHallucination": {
            "score": hallucination_raw.get("g_eval_score"),
            "reason": hallucination_raw.get("g_eval_reason"),
        },
        "languageComplexity": lang_result,
    }


def sync_all(
    include_test: bool = False,
    story_id: Optional[str] = None,
    user_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> None:
    print(f"Starting GEval sync (include_test={include_test}, story_id={story_id}, user_id={user_id}, limit={limit})…")
    db = get_db()

    db["analytics_geval_scores"].create_index(
        [("mongoId", ASCENDING)], unique=True, background=True
    )

    query: dict = {}
    if story_id:
        query["id"] = story_id
    elif not include_test:
        query["test"] = {"$ne": True}

    if user_id:
        query["userId"] = user_id

    cursor = db["miniStories"].find(query)
    if limit:
        cursor = cursor.limit(limit)
    stories = list(cursor)
    print(f"  Found {len(stories)} stories to evaluate.")

    evaluated = skipped = errors = 0
    for i, story in enumerate(stories, 1):
        sid = story.get("id", str(story["_id"]))
        messages = (story.get("chatSession") or {}).get("messages") or []
        user_count = sum(1 for m in messages if m.get("role") == "user")

        if user_count < MIN_USER_MESSAGES:
            print(f"  [{i}/{len(stories)}] Skipping {sid} — only {user_count} user message(s).")
            skipped += 1
            continue

        print(f"  [{i}/{len(stories)}] Evaluating {sid} ({user_count} user msgs)…")
        try:
            doc = evaluate_story(story)
            db["analytics_geval_scores"].replace_one(
                {"mongoId": doc["mongoId"]}, doc, upsert=True
            )
            evaluated += 1
        except Exception as exc:
            print(f"    ERROR: Failed for story {sid}: {exc}")
            errors += 1

    print(f"\nDone. Evaluated={evaluated}, Skipped={skipped}, Errors={errors}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GEval metrics on miniStories.")
    parser.add_argument("--include-test", action="store_true", help="Include test=true stories.")
    parser.add_argument("--story-id", help="Evaluate a single story by its id field.")
    parser.add_argument("--user-id", help="Only evaluate stories belonging to this userId.")
    parser.add_argument("--limit", type=int, help="Limit the number of stories processed.")
    args = parser.parse_args()
    sync_all(
        include_test=args.include_test,
        story_id=args.story_id,
        user_id=args.user_id,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
