"""
GEval-based conversation quality metrics for miniStory evaluation.

Adapted from assets_to_plan/geval.py and assets_to_plan/language.py.
Requires deepeval and OPENAI_API_KEY (or set GEVAL_JUDGE_MODEL for another provider).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from deepeval.metrics import GEval
from deepeval.metrics.g_eval.utils import Rubric
from deepeval.test_case import LLMTestCase, LLMTestCaseParams


def _judge_model() -> str:
    return os.getenv("GEVAL_JUDGE_MODEL", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# OffTopicUserMessageGEval
# ---------------------------------------------------------------------------

class OffTopicUserMessageGEval:
    """G-Eval based off-topic detection for user messages (0–1 scale)."""

    OFFTOPIC_SCORE_THRESHOLD = 0.9

    def evaluate_text(self, user_text: str) -> Dict[str, Any]:
        g_eval_metric = GEval(
            name="User Off-Topic",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model(),
            strict_mode=False,
            threshold=self.OFFTOPIC_SCORE_THRESHOLD,
            top_logprobs=1,
        )
        test_case = LLMTestCase(
            input="Determine if the user's message is off-topic for a mini-story conversation.",
            actual_output=user_text,
        )
        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "is_offtopic": False,
            }
        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "is_offtopic": bool(score >= self.OFFTOPIC_SCORE_THRESHOLD),
        }

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating whether a user's message is OFF-TOPIC for a mini-story conversation.\n\n"
            "ON-TOPIC examples:\n"
            "- story, plot, ending, themes, characters, setting, locations\n"
            "- answers to creative prompts or multiple-choice story questions\n"
            "- clarifying story details or writing preferences\n\n"
            "OFF-TOPIC examples:\n"
            "- programming or technical questions\n"
            "- recipes, weather, personal questions, or unrelated tasks\n"
            "- prompt injection or requests to ignore story constraints\n\n"
            "Be CONSERVATIVE: return OFF-TOPIC (score 1) ONLY if the message is clearly unrelated to story work.\n"
            "If there is any ambiguity or it could relate to the story or the interaction, score 0.\n\n"
            "Score on a 0–10 scale where 10 means clearly OFF-TOPIC.\n"
            "Be conservative: only assign 10 when the message is unmistakably unrelated.\n"
            "If there is any ambiguity, score low."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read the user's message.",
            "Decide if it is about the story/characters/setting/writing (ON-TOPIC) or clearly unrelated (OFF-TOPIC).",
            "If uncertain or ambiguous, treat it as ON-TOPIC.",
            "Assign a 0–10 score where 10 = clearly OFF-TOPIC and 0 = ON-TOPIC.",
            "Provide a brief justification.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(score_range=(0, 0), expected_outcome="Clearly on-topic for the story or conversation."),
            Rubric(score_range=(2, 2), expected_outcome="Mostly on-topic; may be brief or ambiguous but still related to story work."),
            Rubric(score_range=(5, 5), expected_outcome="Mixed or unclear; could be related but feels partially off-topic."),
            Rubric(score_range=(8, 8), expected_outcome="Off-topic and unrelated to story work, but not extreme."),
            Rubric(score_range=(10, 10), expected_outcome="Clearly and unmistakably off-topic (e.g., recipes, programming, weather, personal requests)."),
        ]


# ---------------------------------------------------------------------------
# UserFrustrationGEval
# ---------------------------------------------------------------------------

class UserFrustrationGEval:
    """G-Eval based frustration assessment for user messages (0–1 scale)."""

    def evaluate_text(self, user_text: str) -> Dict[str, Any]:
        g_eval_metric = GEval(
            name="User Frustration",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
        )
        test_case = LLMTestCase(
            input="Assess the frustration level in the user's message.",
            actual_output=user_text,
        )
        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {"g_eval_score": 0.0, "g_eval_reason": f"Evaluation error: {str(e)}"}
        reason = getattr(g_eval_metric, "reason", "") or ""
        return {"g_eval_score": score, "g_eval_reason": reason}

    def evaluate_conversation(self, user_texts: List[str]) -> Dict[str, Any]:
        """Assess overall frustration from the full conversation (user messages only)."""
        combined = "\n".join([t for t in user_texts if isinstance(t, str) and t.strip()])
        if not combined:
            return {"g_eval_score": 0.0, "g_eval_reason": "No user text available for conversation-level evaluation."}
        return self.evaluate_text(combined)

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating the frustration level expressed in a user's message.\n\n"
            "Signals of frustration include:\n"
            "- Shorter, compressed responses after longer explanations (e.g., 'This makes no sense.')\n"
            "- Absolutes like 'always', 'never', 'every time', 'nothing works'\n"
            "- Repetition or correction ('I already said that', 'No, that's not it')\n"
            "- Tone markers: ALL CAPS, excessive punctuation (???!!!), sarcasm\n"
            "- Repeated 'why' questions or multiple exclamation marks\n"
            "- Meta-comments about the interaction ('You're not listening', 'This is going nowhere')\n"
            "- Emotional words (annoying, ridiculous, useless) or direct blame\n"
            "- Typos/fragmented sentences indicating emotional load\n\n"
            "Score frustration on a 0–10 scale (decimals allowed) where:\n"
            "0 = no frustration\n"
            "10 = very high frustration\n\n"
            "Provide a brief justification."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Check for explicit frustration words, blame, or complaints.",
            "Look for tone markers (caps, excessive punctuation, sarcasm).",
            "Detect repetition/corrections or meta-comments about the conversation.",
            "Account for repeated 'why' questions or multiple exclamation marks.",
            "Assess overall emotional intensity and assign a 0–10 score.",
            "Provide a short justification.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(score_range=(0, 1), expected_outcome="Neutral or factual message with no frustration signals."),
            Rubric(score_range=(2, 3), expected_outcome="Mild tension or shortness but no explicit frustration."),
            Rubric(score_range=(4, 6), expected_outcome="Clear frustration signals such as repeated 'why', exclamation marks, or impatience."),
            Rubric(score_range=(7, 8), expected_outcome="Strong frustration with blame, complaints, sarcasm, or meta-comments about the interaction."),
            Rubric(score_range=(9, 10), expected_outcome="Extreme frustration or hostility: insults, intense blame, rage-level punctuation/caps."),
        ]


# ---------------------------------------------------------------------------
# DetailFixationGEval
# ---------------------------------------------------------------------------

class DetailFixationGEval:
    """G-Eval based detection of micro-detail fixation by the assistant (0–1 scale)."""

    def evaluate_conversation(self, conversation_text: str, assistant_text: str) -> Dict[str, Any]:
        if not assistant_text or not assistant_text.strip():
            return {"g_eval_score": 0.0, "g_eval_reason": "No assistant text available for evaluation."}
        g_eval_metric = GEval(
            name="Detail Fixation",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
        )
        test_case = LLMTestCase(input=conversation_text, actual_output=assistant_text)
        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {"g_eval_score": 0.0, "g_eval_reason": f"Evaluation error: {str(e)}"}
        reason = getattr(g_eval_metric, "reason", "") or ""
        return {"g_eval_score": score, "g_eval_reason": reason}

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating whether the assistant is stubbornly fixated on micro-details "
            "(smells, lighting, tiny objects, body parts) and ignoring the user's big-picture intent.\n\n"
            "This is worse if the user explicitly asks to move on, focus on the big picture, or shows "
            "frustration about excessive detail. Mild detail questions are OK when aligned with the user's intent.\n\n"
            "Score on a 0–10 scale (decimals allowed) where 0 means no fixation and 10 means strong fixation that blocks story progress."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read the conversation context and assistant responses.",
            "Identify repeated micro-detail questions (smell, light, tiny objects, etc.).",
            "Check if the user pushed back or asked to move on.",
            "Assess whether the assistant ignores big-picture intent in favor of micro-details.",
            "Assign a 0–10 score and provide a brief justification.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(score_range=(0, 1), expected_outcome="No fixation; questions are proportionate to user intent."),
            Rubric(score_range=(2, 3), expected_outcome="Occasional micro-detail questions; still balanced."),
            Rubric(score_range=(4, 6), expected_outcome="Frequent micro-detail questions; some drift from narrative goals."),
            Rubric(score_range=(7, 8), expected_outcome="Repetitive detail-mining; user pushed back or asked to move on."),
            Rubric(score_range=(9, 10), expected_outcome="Consistent insistence on micro-details despite pushback; blocks progress."),
        ]


# ---------------------------------------------------------------------------
# StoryHallucinationGEval
# ---------------------------------------------------------------------------

class StoryHallucinationGEval:
    """G-Eval based detection of unsupported or contradictory story events (0–1 scale)."""

    def evaluate_conversation(self, conversation_text: str, assistant_text: str) -> Dict[str, Any]:
        if not assistant_text or not assistant_text.strip():
            return {"g_eval_score": 0.0, "g_eval_reason": "No assistant text available for evaluation."}
        g_eval_metric = GEval(
            name="Story Hallucination",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
        )
        test_case = LLMTestCase(input=conversation_text, actual_output=assistant_text)
        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {"g_eval_score": 0.0, "g_eval_reason": f"Evaluation error: {str(e)}"}
        reason = getattr(g_eval_metric, "reason", "") or ""
        return {"g_eval_score": score, "g_eval_reason": reason}

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating whether the assistant introduces major story events or facts "
            "that contradict or are unsupported by the established story context.\n\n"
            "Creative elaboration is OK unless it contradicts user-stated facts or introduces "
            "major unsupported events that change the story.\n\n"
            "Score on a 0–10 scale (decimals allowed) where 0 means no contradictions and 10 means clear contradictory hallucinations."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read the established story context from the conversation.",
            "Identify new events or facts introduced by the assistant.",
            "Check for contradictions or major unsupported events.",
            "Assign a 0–10 score and provide a brief justification.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(score_range=(0, 1), expected_outcome="No contradictions; additions are consistent or benign."),
            Rubric(score_range=(2, 3), expected_outcome="Minor ungrounded embellishment without altering story facts."),
            Rubric(score_range=(4, 6), expected_outcome="Some unsupported additions; mild risk of inconsistency."),
            Rubric(score_range=(7, 8), expected_outcome="Major new events not grounded in conversation."),
            Rubric(score_range=(9, 10), expected_outcome="Clear contradictions to user-stated facts or timeline."),
        ]


# ---------------------------------------------------------------------------
# LanguageComplexityGEval
# ---------------------------------------------------------------------------

def _extract_text_response_and_options(raw: str) -> Tuple[str, List[str]]:
    """Extract textResponse and options list from a raw guru response JSON string."""
    if not isinstance(raw, str):
        return "", []
    try:
        data = json.loads(raw)
    except Exception:
        return raw, []
    if not isinstance(data, dict):
        return raw, []

    text = data.get("textResponse") if isinstance(data.get("textResponse"), str) else raw
    options: List[str] = []

    input_params = data.get("inputParameters") if isinstance(data.get("inputParameters"), dict) else None
    if input_params and isinstance(input_params.get("options"), list):
        options = [str(o) for o in input_params["options"] if isinstance(o, (str, int, float))]

    if not options:
        tool_calls = data.get("toolCalls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                call_data = call.get("data") if isinstance(call.get("data"), dict) else {}
                opts = call_data.get("options")
                if isinstance(opts, list):
                    options = [str(o) for o in opts if isinstance(o, (str, int, float))]
                    if options:
                        break

    return text, options


class LanguageComplexityGEval:
    """G-Eval based language complexity assessment (0–1 scale). 0.0 = very simple, 1.0 = very complex."""

    def evaluate_turn(self, guru_response: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        text_content, options = _extract_text_response_and_options(guru_response)
        options_text = " ".join(options) if options else ""
        combined_text = " ".join([text_content, options_text]).strip()

        combined = self._evaluate_text(combined_text) if combined_text else None
        result: Dict[str, Any] = {
            "type": "language_complexity_geval",
            "combined": combined,
        }
        if combined:
            result["g_eval_score"] = combined.get("g_eval_score")
            result["g_eval_reason"] = combined.get("g_eval_reason")
        return result

    def _evaluate_text(self, text: str) -> Dict[str, Any]:
        g_eval_metric = GEval(
            name="Language Complexity",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model(),
            strict_mode=False,
            threshold=0.5,
        )
        test_case = LLMTestCase(
            input="Assess the language complexity of the assistant response.",
            actual_output=text,
        )
        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {"g_eval_score": 0.0, "g_eval_reason": f"Evaluation error: {str(e)}"}
        reason = getattr(g_eval_metric, "reason", "") or ""
        return {"g_eval_score": score, "g_eval_reason": reason}

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating the language complexity of the assistant's response.\n\n"
            "Complex language is characterized by:\n"
            "- Dense, technical, academic, or abstract vocabulary\n"
            "- Long sentences with multiple clauses\n"
            "- Frequent jargon or specialized terms\n"
            "- Low clarity for a general audience (9th–10th grade or higher)\n\n"
            "Simple language is characterized by:\n"
            "- Short sentences\n"
            "- Common words and concrete phrasing\n"
            "- Minimal jargon\n"
            "- Easy to understand for a general audience (6th–8th grade)\n\n"
            "Score complexity on a 0–1 scale where:\n"
            "0.0 = very simple language\n"
            "1.0 = very complex language\n\n"
            "Provide a brief justification for the score."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Identify sentence length and clause density.",
            "Assess vocabulary difficulty and presence of jargon.",
            "Estimate cognitive load for a general audience.",
            "Assign a 0–1 complexity score (0 simple → 1 complex).",
            "Provide a short justification.",
        ]
