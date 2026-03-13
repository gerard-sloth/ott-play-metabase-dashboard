"""
G-Eval based conversation quality evaluators.
"""
from typing import Any, Dict, List, Optional

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams
from deepeval.metrics.g_eval.utils import Rubric

from ..deepeval_judge import get_geval_judge_model, get_geval_verbose_mode
from .utils import extract_text_content


class OffTopicUserMessageGEval:
    """G-Eval based off-topic detection for user messages (0–1 with high-confidence threshold)."""

    OFFTOPIC_SCORE_THRESHOLD = 0.9

    def __init__(self):
        pass

    def evaluate_text(self, user_text: str) -> Dict[str, Any]:
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()
        rubric = self._rubric()

        g_eval_metric = GEval(
            name="User Off-Topic",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            rubric=rubric,
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=self.OFFTOPIC_SCORE_THRESHOLD,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input="Determine if the user's message is off-topic for a mini-story conversation.",
            actual_output=user_text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "is_offtopic": False,
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "offtopic_score_threshold": self.OFFTOPIC_SCORE_THRESHOLD,
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
            Rubric(
                score_range=(0, 0),
                expected_outcome="Clearly on-topic for the story or conversation.",
            ),
            Rubric(
                score_range=(2, 2),
                expected_outcome="Mostly on-topic; may be brief or ambiguous but still related to story work.",
            ),
            Rubric(
                score_range=(5, 5),
                expected_outcome="Mixed or unclear; could be related but feels partially off-topic.",
            ),
            Rubric(
                score_range=(8, 8),
                expected_outcome="Off-topic and unrelated to story work, but not extreme.",
            ),
            Rubric(
                score_range=(10, 10),
                expected_outcome="Clearly and unmistakably off-topic (e.g., recipes, programming, weather, personal requests).",
            ),
        ]


class UserFrustrationGEval:
    """G-Eval based frustration assessment for user messages (0–1 scale)."""

    def __init__(self):
        pass

    def evaluate_text(self, user_text: str) -> Dict[str, Any]:
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()
        rubric = self._rubric()

        g_eval_metric = GEval(
            name="User Frustration",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            rubric=rubric,
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input="Assess the frustration level in the user's message.",
            actual_output=user_text,
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

    def evaluate_conversation(self, user_texts: List[str]) -> Dict[str, Any]:
        """Assess overall frustration from the full conversation (user-only)."""
        combined = "\n".join([t for t in user_texts if isinstance(t, str) and t.strip()])
        if not combined:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No user text available for conversation-level evaluation.",
            }
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
            Rubric(
                score_range=(0, 1),
                expected_outcome="Neutral or factual message with no frustration signals.",
            ),
            Rubric(
                score_range=(2, 3),
                expected_outcome="Mild tension or shortness but no explicit frustration.",
            ),
            Rubric(
                score_range=(4, 6),
                expected_outcome="Clear frustration signals such as repeated 'why', exclamation marks, or impatience.",
            ),
            Rubric(
                score_range=(7, 8),
                expected_outcome="Strong frustration with blame, complaints, sarcasm, or meta-comments about the interaction.",
            ),
            Rubric(
                score_range=(9, 10),
                expected_outcome="Extreme frustration or hostility: insults, intense blame, rage-level punctuation/caps.",
            ),
        ]


class DetailFixationGEval:
    """G-Eval based detection of micro-detail fixation by the assistant (0–1 scale)."""

    def evaluate_conversation(self, conversation_text: str, assistant_text: str) -> Dict[str, Any]:
        if not assistant_text or not assistant_text.strip():
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No assistant text available for evaluation.",
                "scale": "0-1",
            }
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()
        rubric = self._rubric()

        g_eval_metric = GEval(
            name="Detail Fixation",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            rubric=rubric,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input=conversation_text,
            actual_output=assistant_text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "scale": "0-1",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "scale": "0-1",
        }

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


class OptionQualityGEval:
    """
    G-Eval: are the assistant's multiple-choice options concrete dramatic scene snapshots
    (the Guru #1 RULE), or do they fall back to forbidden patterns (mood labels, archetypes,
    abstract outcomes)?

    Score 0 = all options are concrete and pitch-specific (good).
    Score 1 = options are generic mood labels or archetypes (bad).
    Direction: ↓ lower is better.

    Input: all MC options from the conversation concatenated.
    """

    def evaluate_conversation(self, all_options_text: str) -> Dict[str, Any]:
        if not all_options_text or not all_options_text.strip():
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No MC options found in this conversation.",
                "scale": "0-1",
            }

        g_eval_metric = GEval(
            name="Option Quality",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input="Evaluate the quality of multiple-choice options in a story-building conversation.",
            actual_output=all_options_text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "scale": "0-1",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "scale": "0-1",
        }

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating the quality of multiple-choice options produced by a story-building AI assistant (Guru).\n\n"
            "GOOD options (score towards 0):\n"
            "- Concrete scene snapshots or dramatic beats (10+ words)\n"
            "- Reference specific characters, locations, or objects from the story\n"
            "- Describe a visible action, a scene detail, or a plot consequence\n"
            "- Example: 'Ramón finds the bruise hidden under a long-sleeve shirt and says nothing'\n\n"
            "BAD options (score towards 10):\n"
            "- Mood labels: 'Cozy', 'Dark', 'Hopeful', 'Tense'\n"
            "- Archetypes: 'The Helper', 'The Rebel', 'A Mentor Figure'\n"
            "- Abstract outcomes: 'Trust', 'Betrayal', 'Growth', 'Redemption'\n"
            "- Generic descriptions with no story-specific detail\n"
            "- Options under 7 words with no concrete anchor\n\n"
            "Evaluate ALL options provided and score the OVERALL quality on a 0–10 scale.\n"
            "0 = all options are vivid, specific, dramatic scene beats.\n"
            "10 = most options are mood labels, archetypes, or abstract outcomes."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read all the multiple-choice options provided.",
            "For each option, judge: is it a concrete scene snapshot (10+ words with specific detail) or a mood label / archetype / abstract outcome?",
            "Count how many options fall into each category.",
            "If most options are concrete and specific, score low (0–3).",
            "If most options are abstract or generic, score high (7–10).",
            "Provide a brief justification with 1–2 example options that drove the score.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(
                score_range=(0, 1),
                expected_outcome="All or almost all options are vivid scene beats with specific story detail (10+ words, character/place names, concrete actions).",
            ),
            Rubric(
                score_range=(2, 3),
                expected_outcome="Most options are concrete but a few are short or slightly generic.",
            ),
            Rubric(
                score_range=(4, 6),
                expected_outcome="Mix: some options are concrete scene beats, others are mood labels or abstractions.",
            ),
            Rubric(
                score_range=(7, 8),
                expected_outcome="Most options are mood labels, archetypes, or abstract outcomes with little story-specific detail.",
            ),
            Rubric(
                score_range=(9, 10),
                expected_outcome="Nearly all options are forbidden patterns: single-word moods, archetypes, or abstract nouns with no story grounding.",
            ),
        ]


class QuestionTypeComplianceGEval:
    """
    G-Eval: for character and location turns, does the Guru ask the RIGHT TYPE of question?

    Character turns MUST ask about appearance/vibe/personality (what you'd see in a photo).
    Location turns MUST ask about sensory description (what it looks/sounds/feels/smells like).
    Both MUST NOT ask about actions, plot events, or what characters DO.

    Score 0 = all questions correctly target description (good).
    Score 1 = questions ask about plot/actions instead of description (bad).
    Direction: ↓ lower is better.

    Input: all character and location questions formatted with their declared category.
    """

    def evaluate_conversation(self, categorized_questions: str) -> Dict[str, Any]:
        if not categorized_questions or not categorized_questions.strip():
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No character or location turns found in this conversation.",
                "scale": "0-1",
            }

        g_eval_metric = GEval(
            name="Question Type Compliance",
            criteria=self._criteria(),
            evaluation_steps=self._evaluation_steps(),
            rubric=self._rubric(),
            evaluation_params=[LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input="Evaluate whether character and location questions target the correct type of information.",
            actual_output=categorized_questions,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "scale": "0-1",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "scale": "0-1",
        }

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating whether a story-building AI assistant asks the CORRECT TYPE of question "
            "for each category.\n\n"
            "RULES:\n"
            "CHARACTER questions MUST be about DESCRIBING the character:\n"
            "  ✓ GOOD: appearance, vibe, clothing, size, expression, energy, personality traits — "
            "things you'd notice in a photo or a 5-second silent clip.\n"
            "  ✗ BAD: 'What does X do when…', 'What happens after X…', 'How does X react when…' — "
            "these are story/action questions, not description questions.\n\n"
            "LOCATION questions MUST be about DESCRIBING the place:\n"
            "  ✓ GOOD: what it looks like, sounds like, smells like, feels like — "
            "layout, colors, light, textures, temperature, first impression.\n"
            "  ✗ BAD: 'What object makes the escape harder…', 'What happens here when…', "
            "'Where does X go next…' — these are plot/event questions, not description questions.\n\n"
            "Each question below is labeled with its declared category (character/location).\n"
            "Score the OVERALL compliance on a 0–10 scale.\n"
            "0 = all questions correctly ask about description for their category.\n"
            "10 = most questions ask about plot/actions instead of description."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read each question and its declared category (character or location).",
            "For CHARACTER questions: does it ask about appearance, vibe, or personality (good) or about actions/plot (bad)?",
            "For LOCATION questions: does it ask about sensory description (good) or about plot events/obstacles (bad)?",
            "Count violations (questions that ask the wrong type).",
            "If no or very few violations, score low (0–2). Many violations: score high (7–10).",
            "Provide a brief justification naming 1–2 specific violations if any.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(
                score_range=(0, 1),
                expected_outcome="All questions correctly target description: character questions about appearance/vibe, location questions about sensory details.",
            ),
            Rubric(
                score_range=(2, 3),
                expected_outcome="Mostly correct; 1 question slightly drifts toward action but stays mostly descriptive.",
            ),
            Rubric(
                score_range=(4, 6),
                expected_outcome="Several questions mix description with action or plot elements — borderline compliance.",
            ),
            Rubric(
                score_range=(7, 8),
                expected_outcome="Several clear violations: character questions ask what characters DO; location questions ask about plot events.",
            ),
            Rubric(
                score_range=(9, 10),
                expected_outcome="Most character and location questions are actually story/action questions, completely ignoring the description requirement.",
            ),
        ]


class StoryHallucinationGEval:
    """G-Eval based detection of unsupported or contradictory story events (0–1 scale)."""

    def evaluate_conversation(self, conversation_text: str, assistant_text: str) -> Dict[str, Any]:
        if not assistant_text or not assistant_text.strip():
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No assistant text available for evaluation.",
                "scale": "0-1",
            }
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()
        rubric = self._rubric()

        g_eval_metric = GEval(
            name="Story Hallucination",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            rubric=rubric,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input=conversation_text,
            actual_output=assistant_text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "scale": "0-1",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "scale": "0-1",
        }

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


class TopicJumpGEval:
    """G-Eval based detection of abrupt topic/timeline jumps (0–1 scale)."""

    def evaluate_conversation(self, conversation_text: str, assistant_text: str) -> Dict[str, Any]:
        if not assistant_text or not assistant_text.strip():
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": "No assistant text available for evaluation.",
                "scale": "0-1",
            }
        criteria = self._criteria()
        evaluation_steps = self._evaluation_steps()
        rubric = self._rubric()

        g_eval_metric = GEval(
            name="Topic Jump",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            rubric=rubric,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.0,
            top_logprobs=1,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input=conversation_text,
            actual_output=assistant_text,
        )

        try:
            score = g_eval_metric.measure(test_case)
        except Exception as e:
            return {
                "g_eval_score": 0.0,
                "g_eval_reason": f"Evaluation error: {str(e)}",
                "scale": "0-1",
            }

        reason = getattr(g_eval_metric, "reason", "") or ""
        return {
            "g_eval_score": score,
            "g_eval_reason": reason,
            "scale": "0-1",
        }

    @staticmethod
    def _criteria() -> str:
        return (
            "You are evaluating whether the assistant abruptly changes topics or jumps the story timeline "
            "in a jarring or incoherent way.\n\n"
            "Minor shifts are OK if still relevant. Penalize repeated or extreme jumps that break coherence.\n\n"
            "Score on a 0–10 scale (decimals allowed) where 0 means smooth progression and 10 means consistent incoherence."
        )

    @staticmethod
    def _evaluation_steps() -> List[str]:
        return [
            "Read the conversation context and assistant replies.",
            "Identify abrupt topic or timeline shifts.",
            "Assess how jarring or incoherent the shifts are.",
            "Assign a 0–10 score and provide a brief justification.",
        ]

    @staticmethod
    def _rubric() -> List[Rubric]:
        return [
            Rubric(score_range=(0, 1), expected_outcome="Smooth progression; coherent transitions."),
            Rubric(score_range=(2, 3), expected_outcome="Small shifts but still relevant."),
            Rubric(score_range=(4, 6), expected_outcome="Occasional jarring jumps; some timeline confusion."),
            Rubric(score_range=(7, 8), expected_outcome="Repeated jumps or orthogonal pivots."),
            Rubric(score_range=(9, 10), expected_outcome="Consistent incoherence or contradictory timeline jumps."),
        ]


class StallTurnDetector:
    """
    DeepEval G-Eval metric to detect "stall" turns where the assistant asks an
    extra question even though it could have advanced the workflow.

    This is intentionally distinct from PhaseDetector:
    - PhaseDetector: what phase is happening?
    - StallTurnDetector: given what's already known, did the assistant unnecessarily stall?
    """

    def __init__(self, window_messages: int = 6):
        self.window_messages = window_messages

    def evaluate_turn(
        self,
        conversation_history: List[Dict[str, Any]],
        guru_response: str,
        context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        ctx = context or {}
        assistant_text = extract_text_content(guru_response)

        # Provide a small slice of the conversation so the judge has the relevant evidence.
        recent = conversation_history[-self.window_messages:] if conversation_history else []
        convo = "\n".join(f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in recent)

        criteria = """Evaluate whether the assistant is stalling in the Minimatics workflow.

Score guidance (0.0–1.0):
- 1.0 = NOT a stall: the assistant advances the workflow appropriately, OR asks exactly one necessary clarifying question when required info is genuinely missing/ambiguous.
- 0.0 = Stall: the user has already provided enough information to advance, but the assistant asks another broad/wrap-up question instead of progressing (e.g. it could have locked the next phase / triggered the next tool call, but didn't).

Be strict about redundant clarification loops and "wrap-up" questions when progress is possible."""

        evaluation_steps = [
            "Read the recent conversation context and the assistant response.",
            "Determine whether the user has already provided enough information to proceed to the next workflow action.",
            "If yes, check whether the assistant actually advances vs asks an extra broad/wrap-up question.",
            "Assign a score: 1.0 for non-stall behavior, 0.0 for stall; use intermediate scores only when truly borderline.",
        ]

        g_eval_metric = GEval(
            name="Stall Turn Detection",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.5,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input=(
                "Recent conversation:\n"
                f"{convo}\n\n"
                "Additional context:\n"
                f"{ctx}\n"
            ),
            actual_output=assistant_text,
        )

        try:
            score = float(g_eval_metric.measure(test_case))
            passed = score >= float(getattr(g_eval_metric, "threshold", 0.5))
            is_stall = not passed
            reason = getattr(g_eval_metric, "reason", "No reason provided")
        except Exception as e:
            score = 0.0
            passed = True
            is_stall = False
            reason = f"Evaluation error: {e}"

        return {
            "is_stall": is_stall,
            "passed": passed,
            "g_eval_score": score,
            "g_eval_reason": reason,
            "type": "stall_turn_detection",
            "evaluation_method": "deepeval",
        }


class ToolChoiceNecessarySufficientDetector:
    """
    DeepEval G-Eval metric to judge whether the chosen tool call (or lack of tool call)
    was necessary/sufficient given the recent conversation context.

    This is intentionally scoped to tool usage only (distinct from phase detection and stall detection).
    """

    def __init__(self, window_messages: int = 6):
        self.window_messages = window_messages

    def evaluate_turn(
        self,
        conversation_history: List[Dict[str, Any]],
        guru_response: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ctx = context or {}
        assistant_text = guru_response.strip()
        recent = conversation_history[-self.window_messages:] if conversation_history else []
        convo = "\n".join(f"{m.get('role', 'unknown')}: {m.get('content', '')}" for m in recent)

        # Provide known tool names (minimatics core tools).
        tool_list = ["get_art_style", "get_character", "shot_thinker"]

        criteria = f"""Evaluate whether the assistant's tool choice is necessary and sufficient.

You are given:
- The recent conversation context
- The assistant response (which may be a JSON tool call with toolName/inputParameters/textResponse)

Rules:
- If the assistant calls a tool, it must be the correct tool for the workflow step and not premature.
- If the assistant does NOT call a tool, it must be because asking the user a question is necessary (missing required info) or because tool usage is not needed yet.

Available tool names: {tool_list}.

Score guidance (0.0–1.0):
- 1.0 = tool choice is appropriate (including choosing no tool when a user question is truly needed)
- 0.0 = inappropriate tool choice (wrong tool, premature tool, missing tool when required, or asking for info that already exists)"""

        evaluation_steps = [
            "Inspect the conversation context and infer what the next required action is.",
            "Inspect the assistant response: identify toolName (if any) and whether missing info truly exists.",
            "Judge whether the chosen tool (or no tool) is necessary and sufficient at this point.",
            "Assign a score between 0.0 and 1.0 (1.0 = appropriate, 0.0 = inappropriate).",
        ]

        g_eval_metric = GEval(
            name="Tool Choice Necessary/Sufficient",
            criteria=criteria,
            evaluation_steps=evaluation_steps,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=get_geval_judge_model(),
            strict_mode=False,
            threshold=0.5,
            verbose_mode=get_geval_verbose_mode(),
        )

        test_case = LLMTestCase(
            input=(
                "Recent conversation:\n"
                f"{convo}\n\n"
                "Additional context:\n"
                f"{ctx}\n"
            ),
            actual_output=assistant_text,
        )

        try:
            score = float(g_eval_metric.measure(test_case))
            ok = score >= float(getattr(g_eval_metric, "threshold", 0.5))
            reason = getattr(g_eval_metric, "reason", "No reason provided")
        except Exception as e:
            score = 0.0
            ok = True
            reason = f"Evaluation error: {e}"

        return {
            "passed": ok,
            "g_eval_score": score,
            "g_eval_reason": reason,
            "type": "tool_choice_necessary_sufficient",
            "evaluation_method": "deepeval",
        }
