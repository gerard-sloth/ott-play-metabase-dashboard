"""
Language complexity metrics and G-Eval based language complexity assessment.

Merges:
- Deterministic language complexity metrics (formerly language_complexity.py)
- LanguageComplexityGEval (formerly evaluators_minimatics.py)
"""
from __future__ import annotations

import re
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from ..deepeval_judge import get_geval_judge_model, get_geval_verbose_mode

_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_RE = re.compile(r"[^.!?]+")


def _tokenize_words(text: str) -> List[str]:
    return _WORD_RE.findall(text)


def _split_sentences(text: str) -> List[str]:
    # Keep simple: split by punctuation boundaries, ignore empty fragments.
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def _count_syllables(word: str) -> int:
    # Simple heuristic syllable counter.
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    # Remove trailing silent 'e'
    if w.endswith("e") and len(w) > 2 and not w.endswith("le"):
        w = w[:-1]
    # Count vowel groups
    groups = re.findall(r"[aeiouy]+", w)
    count = len(groups)
    return max(1, count)


def _safe_div(n: float, d: float) -> Optional[float]:
    if d == 0:
        return None
    return n / d


def compute_language_metrics(texts: Iterable[str]) -> Dict[str, Optional[float]]:
    """Compute deterministic language complexity metrics for a list of texts."""
    all_text = "\n".join([t for t in texts if isinstance(t, str)])
    words = _tokenize_words(all_text)
    sentences = _split_sentences(all_text)

    total_words = len(words)
    total_sentences = len(sentences)
    total_chars = sum(len(w) for w in words)
    total_syllables = sum(_count_syllables(w) for w in words)
    unique_words = len({w.lower() for w in words})

    avg_sentence_len = _safe_div(float(total_words), float(total_sentences))
    avg_word_len = _safe_div(float(total_chars), float(total_words))
    type_token_ratio = _safe_div(float(unique_words), float(total_words))

    # Flesch Reading Ease and Flesch-Kincaid Grade
    words_per_sentence = _safe_div(float(total_words), float(total_sentences))
    syllables_per_word = _safe_div(float(total_syllables), float(total_words))

    flesch_reading_ease = None
    flesch_kincaid_grade = None
    if words_per_sentence is not None and syllables_per_word is not None:
        flesch_reading_ease = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
        flesch_kincaid_grade = 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59

    return {
        "total_words": float(total_words),
        "total_sentences": float(total_sentences),
        "total_unique_words": float(unique_words),
        "avg_sentence_length": avg_sentence_len,
        "avg_word_length": avg_word_len,
        "type_token_ratio": type_token_ratio,
        "flesch_reading_ease": flesch_reading_ease,
        "flesch_kincaid_grade": flesch_kincaid_grade,
    }


def compute_question_lengths_prompt_text(text: str) -> List[int]:
    """
    Return word counts for sentences that contain a question mark in prompt text.
    Only sentences with '?' are counted.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    question_spans = re.findall(r"[^?]*\?", text)
    lengths: List[int] = []
    for span in question_spans:
        words = _tokenize_words(span)
        if words:
            lengths.append(len(words))
    return lengths


def compute_option_lengths(options: List[str]) -> List[int]:
    """Return word counts for each multiple-choice option."""
    if not isinstance(options, list):
        return []
    lengths: List[int] = []
    for opt in options:
        if not isinstance(opt, str):
            continue
        words = _tokenize_words(opt)
        lengths.append(len(words))
    return lengths


def aggregate_lengths(lengths: Iterable[int]) -> Dict[str, Optional[float]]:
    """Aggregate lengths into avg/min/max/count."""
    vals = [int(v) for v in lengths if isinstance(v, (int, float))]
    if not vals:
        return {"avg": None, "min": None, "max": None, "count": 0}
    return {
        "avg": sum(vals) / len(vals),
        "min": min(vals),
        "max": max(vals),
        "count": len(vals),
    }


def extract_text_response_and_options(raw: str) -> Tuple[str, List[str]]:
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

    # Legacy toolName/inputParameters shape
    input_params = data.get("inputParameters") if isinstance(data.get("inputParameters"), dict) else None
    if input_params and isinstance(input_params.get("options"), list):
        options = [str(o) for o in input_params.get("options") if isinstance(o, (str, int, float))]

    # Agentic toolCalls shape
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
    """G-Eval based language complexity assessment (0-1 scale). 0.0 = very simple language and 1.0 = very complex language"""

    def __init__(self):
        pass

    def evaluate_turn(self, guru_response: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        text_content, options = extract_text_response_and_options(guru_response)
        options_text = " ".join(options) if options else ""
        combined_text = " ".join([text_content, options_text]).strip()

        result = {
            "type": "language_complexity_geval",
            "evaluation_method": "deepeval",
            "scale": "0-1",
            "prompt_text": self._evaluate_text(text_content) if text_content else None,
            "multiple_options": self._evaluate_text(options_text) if options_text else None,
            "combined": self._evaluate_text(combined_text) if combined_text else None,
        }

        # Convenience passthrough for aggregate logic
        combined_score = (result.get("combined") or {}).get("g_eval_score")
        combined_reason = (result.get("combined") or {}).get("g_eval_reason")
        if combined_score is not None:
            result["g_eval_score"] = combined_score
        if combined_reason:
            result["g_eval_reason"] = combined_reason
        return result

    def _evaluate_text(self, text: str) -> Dict[str, Any]:
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()

        g_eval_metric = GEval(
            name="Language Complexity",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.5,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input="Assess the language complexity of the assistant response.",
            actual_output=text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
        }

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
