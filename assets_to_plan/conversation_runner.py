"""
DeepEval ConversationalTestCase-based conversation runner for minimatics evaluation
"""
import json
import os
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Any, Optional, Literal

# DeepEval is required for this implementation
from deepeval.test_case import ConversationalTestCase
from deepeval.test_case.conversational_test_case import Turn
from deepeval.metrics import GEval, ConversationalGEval
from deepeval.test_case import LLMTestCaseParams
from deepeval.test_case.conversational_test_case import TurnParams

from ..api.api_client_minimatics import MinamaticsAPI
from ..deepeval_judge import get_geval_judge_metadata
from ..deepeval_judge import get_geval_judge_model, get_geval_verbose_mode
from ..evaluators import (
    PhaseDetector,
    ConfirmationRequestDetector,
    StallTurnDetector,
    ToolChoiceNecessarySufficientDetector,
    StoryCompletenessEvaluator,
    LinearityEvaluator,
)
from ..evaluators import extract_text_content, extract_tool_calls
from ..evaluators import (
    ToolJsonOnlyMetric,
    ToolSameEntityMetric,
    PerTurnConcisionMetric,
    NoReaskStyleMetric,
    TurnBudgetMetric,
    ShotThinkerGateMetric,
    FocusGroupReadiness,
)
from ..evaluators import LanguageComplexityGEval
from ..evaluators.language import compute_language_metrics, extract_text_response_and_options
from ..api.tool_response_parser import ToolResponseParser
from ..metric_toggles import MetricToggles, get_metric_toggles

class ConversationRunner:
    """Run a conversation between simulated user and guru-minimatics"""
    
    def __init__(
        self,
        persona_path: str,
        temperature: float = 1.0,
        test_mode: bool = True,
        mock_asset_generation: bool = True,
        guru_version_id: str = "",
        backend_route: str = "minimatics",
        mini_story_id: Optional[str] = None,
        skip_geval: bool = False,
    ):
        self._skip_geval = skip_geval
        self.metric_toggles: MetricToggles = get_metric_toggles()
        if skip_geval:
            # Generation mode: only run cheap deterministic metrics, skip all G-Eval LLM calls.
            self.metric_toggles = MetricToggles(
                enabled={"per_turn_concision", "tool_json_only", "tool_same_entity", "no_reask_style"},
                disabled=set(),
            )
        self.api = MinamaticsAPI(
            temperature=temperature,
            test_mode=test_mode,
            mock_asset_generation=mock_asset_generation,
            guru_version_id=guru_version_id,
            backend_route=backend_route,
            mini_story_id=mini_story_id,
        )
        self.test_mode = test_mode
        self.mock_asset_generation = mock_asset_generation
        
        self.guru_system_prompt = ""
        self.system_prompt_name = "backend_guru_version_id"

        with open(persona_path, 'r') as f:
            self.user_persona = f.read()
        
        # Initialize evaluators
        self.phase_detector = PhaseDetector()
        self.confirmation_detector = ConfirmationRequestDetector()
        self.stall_detector = StallTurnDetector()
        self.tool_choice_detector = ToolChoiceNecessarySufficientDetector()
        self.completeness_evaluator = StoryCompletenessEvaluator()
        self.linearity_evaluator = LinearityEvaluator()
        # New focus-group metrics
        self.tool_json_only = ToolJsonOnlyMetric()
        self.tool_same_entity = ToolSameEntityMetric()
        self.turn_concision = PerTurnConcisionMetric()
        self.no_reask_style = NoReaskStyleMetric()
        self.turn_budget = TurnBudgetMetric(max_turns=15, max_style_turns=2)
        self.shot_gate = ShotThinkerGateMetric(self.completeness_evaluator)
        self.readiness = FocusGroupReadiness()
        self.language_complexity_geval = LanguageComplexityGEval()
        
        # Conversation state (proper role/content format)
        self.conversation_history: List[Dict[str, str]] = []  # role/content format for GPT calls
        self.turn_evaluations: List[Dict[str, Any]] = []
        self.turn_telemetry: List[Dict[str, Any]] = []
        self.persona_name = persona_path.split('/')[-1].replace('.txt', '')  # Extract from path
        self.json_parsing_failures: List[Dict[str, Any]] = []  # Track JSON parsing issues
        self._backend_bootstrap_loaded = False
        self._last_backend_response: Optional[Dict[str, Any]] = None
        self._prev_assistant_questions: List[str] = []
        self._backend_error: Optional[str] = None
        self._backend_initial_messages_raw: List[Dict[str, Any]] = []
        self._suppress_header = False
    
    def run_conversation(self, initial_message: str = "hi", max_turns: int = 25) -> Dict[str, Any]:
        """Run a complete conversation and return evaluation results"""
        
        if not self._suppress_header:
            print(f"\n=== STARTING CONVERSATION ===")
            print(f"Persona: {self.persona_name}")
            print(f"Max turns: {max_turns}")
            print("=" * 50)

        # Mirror production: pull the initial assistant greeting/tool call from the backend.
        self._bootstrap_backend_history()
        
        # Start conversation
        user_message = initial_message
        turn_count = 0
        
        while turn_count < max_turns:
            turn_count += 1
            
            # Add user message to history (proper format)
            display_message = self._format_user_message_for_history(user_message)
            self.conversation_history.append({
                'role': 'user',
                'content': display_message
            })
            
            # Display user message
            print(f"\n<{self.persona_name}>: {display_message}")
            # Observe style provided
            self.no_reask_style.observe_user(display_message)
            
            # Get guru response
            assistant_start = time.perf_counter()
            guru_response = self._get_guru_response(user_message)
            assistant_elapsed_ms = (time.perf_counter() - assistant_start) * 1000.0
            backend_event = self._pop_latest_event("backend_guru_response")

            if self._is_backend_error_response(guru_response):
                self._backend_error = str(guru_response).strip()
                turn_eval = self._build_backend_error_eval(turn_count, guru_response)
                guru_message = {
                    'role': 'assistant',
                    'content': guru_response,
                    'type': 'backend_error',
                }
                self.conversation_history.append(guru_message)
                self._display_guru_response(guru_response)
                self.turn_evaluations.append(turn_eval)
                self._display_evaluator_feedback(turn_eval)
                print("> evaluator: backend error detected, terminating conversation")
                # Capture telemetry even though we are stopping early.
                turn_telemetry: Dict[str, Any] = {
                    "turn": turn_count,
                    "assistant_latency_ms": assistant_elapsed_ms,
                    "persona_latency_ms": None,
                    "backend": backend_event,
                    "persona": None,
                    "waste": None,
                }
                self.turn_telemetry.append(turn_telemetry)
                break
            
            # Evaluate guru response first
            turn_eval = self._evaluate_turn(guru_response, turn_count)
            
            # Add guru response to history with confirmation flag if detected
            guru_message = {
                'role': 'assistant',
                'content': guru_response
            }
            
            # Add confirmation flag if detected
            if turn_eval['confirmation_evaluation']['is_confirmation_request']:
                guru_message['type'] = 'confirmation'
            
            self.conversation_history.append(guru_message)
            
            # Parse and display guru response
            self._display_guru_response(guru_response)
            self.turn_evaluations.append(turn_eval)
            
            # Display evaluator feedback
            self._display_evaluator_feedback(turn_eval)

            # Collect efficiency + waste telemetry for this assistant turn immediately (even if we terminate)
            assistant_text = extract_text_content(guru_response)
            tool_name_from_response = self._extract_tool_name_from_response(guru_response)
            waste = self._compute_waste_metrics(assistant_text, tool_name_from_response)
            turn_telemetry: Dict[str, Any] = {
                "turn": turn_count,
                "assistant_latency_ms": assistant_elapsed_ms,
                "persona_latency_ms": None,
                "backend": backend_event,
                "persona": None,
                "waste": waste,
            }
            self.turn_telemetry.append(turn_telemetry)
            
            # Check for termination (shot_thinker tool called)
            tool_calls = extract_tool_calls(guru_response)
            if any(call.get("type") == "shot_thinker" for call in tool_calls) or 'shot_thinker' in guru_response.lower():
                print(f"\n> evaluator: CONVERSATION TERMINATED - shot_thinker called")
                # Validate pre-call gate
                shot_gate_eval = self.shot_gate.evaluate_on_call(self.conversation_history + [{'role': 'assistant', 'content': guru_response}])
                turn_eval['shot_thinker_gate'] = shot_gate_eval
                break
            
            # Get next user response
            persona_start = time.perf_counter()
            user_message = self._get_user_response(guru_response)
            persona_elapsed_ms = (time.perf_counter() - persona_start) * 1000.0
            persona_event = self._pop_latest_event("persona_simulation")
            # Enrich last turn telemetry with persona stats
            self.turn_telemetry[-1]["persona_latency_ms"] = persona_elapsed_ms
            self.turn_telemetry[-1]["persona"] = persona_event
            response_lower = guru_response.lower()
            tool_name = None
            tool_calls = extract_tool_calls(guru_response)
            if tool_calls:
                tool_name = tool_calls[0].get("type")
            
            # Handle special case: get_character response
            if tool_name == 'get_character':
                # Inject standard JSON response and parse it
                persona_response = '{"candidates": ["url1", "url2", "url3", "url4"], "selected": 1, "comments": ""}'
                try:
                    tool_params = ToolResponseParser.parse_character_selection(persona_response)
                    user_message = json.dumps(tool_params)
                    print(f"\n> evaluator: JSON injection for get_character response")
                    print(f"  Tool params: {json.dumps(tool_params)}")
                except ValueError as e:
                    print(f"\n> evaluator: Failed to parse character response: {e}")
                    user_message = persona_response
            
            # Handle special case: get_art_style / runGetArtStyle response
            elif tool_name == 'get_art_style':
                # Inject standard JSON response and parse it to tool parameters
                default_candidates = [
                    "url1","url2","url3","url4","url5","url6","url7","url8","url9"
                ]
                persona_response = json.dumps({
                    "candidates": default_candidates,
                    "selected": 1,
                    "comments": ""
                })
                try:
                    tool_params = ToolResponseParser.parse_art_style_selection(persona_response)
                    user_message = json.dumps(tool_params)
                    print(f"\n> evaluator: JSON injection for get_art_style response")
                    print(f"  Tool params: {json.dumps(tool_params)}")
                except ValueError as e:
                    print(f"\n> evaluator: Failed to parse art style response: {e}")
                    user_message = persona_response

            # (telemetry already appended above)
        
        print(f"\n=== CONVERSATION COMPLETED ===")
        
        # Evaluate story completeness
        if self._skip_geval:
            completeness_eval = {
                "completeness_percentage": 0.0,
                "completed_components": 0,
                "total_components": 4,
                "ready_for_shot_thinker": False,
                "missing_components": [],
                "component_details": {},
                "skipped": True,
            }
        else:
            completeness_eval = self.completeness_evaluator.evaluate_conversation(self.conversation_history)
            # Display story completeness summary
            print(f"\n--- STORY COMPLETENESS EVALUATION ---")
            print(f"Overall: {completeness_eval['completeness_percentage']:.1f}% ({completeness_eval['completed_components']}/4 components)")
            for component_name, component_result in completeness_eval['component_details'].items():
                status = "✅" if component_result['passed'] else "❌"
                readable_name = component_name.replace('_', ' ').title()
                print(f"{status} {readable_name}")
            if completeness_eval['ready_for_shot_thinker']:
                print("🎬 Story is ready for shot_thinker!")
            else:
                missing = ', '.join([name.replace('_', ' ').title() for name in completeness_eval['missing_components']])
                print(f"⚠️  Missing components: {missing}")
            print("=" * 40)
        
        # Generate final report 
        report = self._generate_report()
        report['story_completeness'] = completeness_eval

        # Compute FocusGroupReadiness aggregate
        phases = [t['phase_evaluation']['detected_phase'] for t in self.turn_evaluations if 'phase_evaluation' in t]
        tb = self.turn_budget.evaluate_conversation(self.conversation_history, phases)
        # Aggregate pass/fail across tool checks
        tool_json_all_pass = len(self.tool_json_only.violations) == 0
        tool_same_all_pass = len(self.tool_same_entity.violations) == 0
        concision_fails = len(self.turn_concision.violations)
        no_reask_all_pass = len(self.no_reask_style.violations) == 0
        metrics_summary = {
            'turn_budget': tb,
            'tool_json_only': {'all_pass': tool_json_all_pass},
            'tool_same_entity': {'all_pass': tool_same_all_pass},
            'per_turn_concision': {'fails': concision_fails},
            'no_reask_style': {'all_pass': no_reask_all_pass},
        }
        # Include shot_thinker gate if we computed it on termination
        if self.turn_evaluations and 'shot_thinker_gate' in self.turn_evaluations[-1]:
            metrics_summary['shot_thinker_gate'] = self.turn_evaluations[-1]['shot_thinker_gate']
        enabled_components = {
            "turn_budget",
            "shot_thinker_gate",
        }
        for key in ["tool_json_only", "tool_same_entity", "linearity", "no_reask_style"]:
            if self.metric_toggles.is_enabled(key):
                enabled_components.add(key)

        # different naming, mapping it by itself    
        if self.metric_toggles.is_enabled("per_turn_concision"):
            enabled_components.add("concision")

        readiness = self.readiness.score(report['summary'], metrics_summary, enabled_components=enabled_components)
        report['focus_group_readiness'] = readiness

        # Display Focus Group Readiness summary
        print("\n--- FOCUS GROUP READINESS ---")
        print(f"Score: {readiness['score']:.2f}")
        comps = readiness['components']
        active = readiness.get("weights", {})
        active_keys = list(active.keys()) if isinstance(active, dict) else []
        print("Components:")
        for comp in ["turn_budget", "tool_json_only", "tool_same_entity", "linearity", "concision", "no_reask_style", "shot_thinker_gate"]:
            if comp in active_keys:
                print(f"  {comp}: {comps[comp]:.2f}")

        print("=" * 40)
        
        # Conversation-level G-Eval suite (non-redundant vs per-turn metrics)
        if not self._skip_geval:
            conversation_geval = self._evaluate_conversation_geval_suite()
            report["conversation_geval"] = conversation_geval
            self._merge_conversation_geval_into_taxonomy(report, conversation_geval)
        
        return report

    def _evaluate_conversation_geval_suite(self) -> Dict[str, Any]:
        """
        Add the remaining qualitative G-Eval metrics non-redundantly.

        These are conversation-level (ConversationalGEval) metrics:
        - phase progression quality (overall)
        - clarification hygiene (semantic)
        - constraint retention (drift)
        - tool usage appropriateness (semantic)
        - UX clarity (semantic)
        """

        test_case = self.create_conversational_test_case()
        if not test_case:
            return {"error": "Failed to create ConversationalTestCase"}

        judge = get_geval_judge_model()
        verbose = get_geval_verbose_mode()
        evaluation_params = [TurnParams.ROLE, TurnParams.CONTENT, TurnParams.SCENARIO]

        metric_specs = [
            (
                "PhaseProgression_NoStall",
                "conversation_geval:PhaseProgression_NoStall",
                (
                    "Determine whether the assistant progresses through the minimatics workflow without stalling, "
                    "without looping back unnecessarily, and without premature completion. "
                    "Penalize redundant wrap-up questions after sufficient user info was provided. "
                    "In your reason, cite evidence using turn indices like [turn 4], [turn 7]."
                ),
            ),
            (
                "ClarificationHygiene_MinFriction",
                "conversation_geval:ClarificationHygiene_MinFriction",
                (
                    "Determine whether the assistant handles ambiguity with minimal friction: "
                    "asks at most one clarifying question when truly necessary, avoids redundant questions, "
                    "and does not contradict already-provided information. "
                    "In your reason, cite evidence using turn indices like [turn 3]."
                ),
            ),
            (
                "ConstraintRetention_NoDrift",
                "conversation_geval:ConstraintRetention_NoDrift",
                (
                    "Determine whether the assistant retains and respects constraints that the user provided "
                    "(characters, setting, plot constraints, tone) without drifting or contradicting them across turns. "
                    "In your reason, cite evidence using turn indices like [turn 5]."
                ),
            ),
            (
                "UserExperience_UXClarity",
                "conversation_geval:UserExperience_UXClarity",
                (
                    "Determine whether the assistant provides clear next steps, avoids opaque 'done' criteria, "
                    "keeps tone appropriate, and maintains overall clarity for the user. "
                    "In your reason, cite evidence using turn indices like [turn 2]."
                ),
            ),
        ]

        metrics: List[ConversationalGEval] = []
        for name, toggle_key, criteria in metric_specs:
            if not self.metric_toggles.is_enabled(toggle_key):
                continue
            metrics.append(
                ConversationalGEval(
                    name=name,
                    criteria=criteria,
                    evaluation_params=evaluation_params,
                    model=judge,
                    threshold=0.7,
                    strict_mode=False,
                    verbose_mode=verbose,
                )
            )

        results: Dict[str, Any] = {"metrics": {}, "judge": get_geval_judge_metadata()}
        # Mark disabled metrics explicitly (so the report is self-describing).
        for name, toggle_key, _criteria in metric_specs:
            if not self.metric_toggles.is_enabled(toggle_key):
                results["metrics"][name] = {"skipped": True, "passed": True, "reason": "disabled_by_toggle"}
        for metric in metrics:
            try:
                score = metric.measure(test_case)
                results["metrics"][metric.name] = {
                    "score": score,
                    "passed": score >= metric.threshold,
                    "threshold": metric.threshold,
                    "reason": getattr(metric, "reason", None),
                }
            except Exception as exc:
                results["metrics"][metric.name] = {
                    "error": str(exc),
                    "score": 0.0,
                    "passed": False,
                    "threshold": metric.threshold,
                }
        return results

    def _merge_conversation_geval_into_taxonomy(self, report: Dict[str, Any], conversation_geval: Dict[str, Any]) -> None:
        """
        Map conversation-level G-Eval failures into the structured failure taxonomy.
        """
        taxonomy = report.get("failure_taxonomy")
        if not isinstance(taxonomy, dict):
            return
        failures = taxonomy.get("failures")
        if not isinstance(failures, list):
            return

        metrics = conversation_geval.get("metrics")
        if not isinstance(metrics, dict):
            return

        def add(primary: str, metric_name: str, reason: Optional[str]) -> None:
            failures.append(
                {
                    "primary_tag": primary,
                    "secondary_tags": [],
                    "metric_type": f"conversation_geval:{metric_name}",
                    "turn": None,
                    "description": f"Conversation-level G-Eval metric failed: {metric_name}",
                    "evidence": reason,
                }
            )

        mapping = {
            "PhaseProgression_NoStall": "phase_progression",
            "ClarificationHygiene_MinFriction": "clarification_hygiene",
            "ConstraintRetention_NoDrift": "constraint_adherence",
            "UserExperience_UXClarity": "user_experience",
        }

        for metric_name, payload in metrics.items():
            if metric_name not in mapping or not isinstance(payload, dict):
                continue
            passed = payload.get("passed")
            if passed is False:
                add(mapping[metric_name], metric_name, payload.get("reason"))

        # Recompute counts in summary
        summary = taxonomy.get("summary")
        if not isinstance(summary, dict):
            return
        counts = summary.get("failure_counts_by_primary_tag")
        if not isinstance(counts, dict):
            counts = {}
        counts_new: Dict[str, int] = {}
        for f in failures:
            tag = f.get("primary_tag")
            if isinstance(tag, str):
                counts_new[tag] = counts_new.get(tag, 0) + 1
        summary["failure_count_total"] = len(failures)
        summary["failure_counts_by_primary_tag"] = counts_new

    def _pop_latest_event(self, event_type: str) -> Optional[Dict[str, Any]]:
        """Pop the most recent telemetry event of a given type from the API buffer."""
        events = self.api.pop_telemetry_events()
        if not events:
            return None
        # Preserve other events (we only use persona_simulation + backend_guru_response for now).
        matched: Optional[Dict[str, Any]] = None
        remaining: List[Dict[str, Any]] = []
        for ev in events:
            if matched is None and ev.get("type") == event_type:
                matched = ev
            else:
                remaining.append(ev)
        # Put back unconsumed events.
        for ev in remaining:
            self.api._record_telemetry(ev)  # type: ignore[attr-defined]
        return matched

    @staticmethod
    def _extract_tool_name_from_response(guru_response: str) -> Optional[str]:
        calls = extract_tool_calls(guru_response)
        if calls:
            val = calls[0].get("type")
            if isinstance(val, str):
                return val
        return None

    def _compute_waste_metrics(self, assistant_text: str, tool_name: Optional[str]) -> Dict[str, Any]:
        text = assistant_text or ""
        words = [w for w in re.split(r"\s+", text.strip()) if w]
        word_count = len(words)
        question_mark_count = text.count("?")
        questions = self._extract_questions(text)
        interrogative_count = sum(1 for q in questions if self._is_interrogative(q))
        repeated_ask = any(self._is_similar_question(q, prev) for q in questions for prev in self._prev_assistant_questions)
        stall_turn_proxy = (len(questions) > 0 or question_mark_count > 0) and (not tool_name or tool_name.strip() == "")

        # Update rolling history
        if questions:
            self._prev_assistant_questions.extend(questions[:5])
            self._prev_assistant_questions = self._prev_assistant_questions[-20:]

        question_density = (question_mark_count / word_count) if word_count else 0.0
        return {
            "word_count": word_count,
            "question_mark_count": question_mark_count,
            "question_count": len(questions),
            "interrogative_count": interrogative_count,
            "question_density": question_density,
            "repeated_ask": repeated_ask,
            "stall_turn_proxy": stall_turn_proxy,
            "tool_name": tool_name,
        }

    @staticmethod
    def _extract_questions(text: str) -> List[str]:
        # Keep it simple: treat segments ending with '?' as questions.
        if "?" not in text:
            return []
        parts = text.split("?")
        questions: List[str] = []
        for part in parts[:-1]:
            candidate = part.strip()
            if not candidate:
                continue
            # Take the last sentence-like span for cleaner matching.
            candidate = re.split(r"[.!]\s+", candidate)[-1].strip()
            if candidate:
                questions.append(candidate + "?")
        return questions

    @staticmethod
    def _is_interrogative(question: str) -> bool:
        q = question.strip().lower()
        return bool(
            re.match(
                r"^(what|how|why|when|where|who|which|can|could|would|do|did|are|is|should|will)\b",
                q,
            )
        )

    @staticmethod
    def _normalize_question(question: str) -> str:
        q = question.lower()
        q = re.sub(r"[^a-z0-9\s]", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q

    def _is_similar_question(self, a: str, b: str) -> bool:
        na = self._normalize_question(a)
        nb = self._normalize_question(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        return SequenceMatcher(a=na, b=nb).ratio() >= 0.9
    
    def _get_guru_response(self, user_message: str) -> str:
        """Route user turns through the studio-backend minimatics router."""
        backend_response = self.api.send_backend_user_message(user_message)
        self._last_backend_response = backend_response
        raw = backend_response.get('rawResponse') or backend_response.get('content')
        debug_backend = os.getenv("MINIMATICS_DEBUG_BACKEND_RESPONSE", "").strip().lower() in ("1", "true", "yes")
        if debug_backend or not raw:
            try:
                dump = json.dumps(backend_response, default=str)
            except Exception:
                dump = str(backend_response)
            print(f"> backend: response_dump={dump}")
        if isinstance(raw, str) and raw:
            return raw

        # Agentic responses may return text/toolCalls without rawResponse/content.
        text_response = backend_response.get("textResponse")
        tool_calls = backend_response.get("toolCalls") or []
        if isinstance(text_response, str) or tool_calls:
            return json.dumps(
                {
                    "textResponse": text_response or "",
                    "toolCalls": tool_calls if isinstance(tool_calls, list) else [],
                }
            )

        # Fallback to nested message shapes if present.
        message = backend_response.get("message")
        if isinstance(message, dict):
            nested = message.get("rawResponse") or message.get("content") or message.get("textResponse")
            if isinstance(nested, str):
                return nested
            if nested is not None:
                try:
                    return json.dumps(nested)
                except Exception:
                    return str(nested)

        return ""

    @staticmethod
    def _is_backend_error_response(guru_response: str) -> bool:
        if not isinstance(guru_response, str):
            return False
        text = guru_response.strip()
        if text.startswith("❌ Error:"):
            return True
        if "messages' must contain the word 'json'" in text.lower():
            return True
        if "badrequesterror" in text.lower():
            return True
        return False

    @staticmethod
    def _build_backend_error_eval(turn_number: int, guru_response: str) -> Dict[str, Any]:
        return {
            'turn': turn_number,
            'guru_response': guru_response,
            'per_turn_concision': {"skipped": True, "type": "per_turn_concision"},
            'phase_evaluation': {"skipped": True, "detected_phase": None, "type": "phase_detection"},
            'confirmation_evaluation': {
                "skipped": True,
                "is_confirmation_request": False,
                "type": "confirmation_detection",
            },
            'stall_evaluation': {"skipped": True, "is_stall": False, "type": "stall_turn_detection"},
            'tool_choice_evaluation': {"skipped": True, "passed": True, "type": "tool_choice_necessary_sufficient"},
            'linearity_evaluation': {"skipped": True, "type": "linearity"},
            'tool_json_only': {"skipped": True, "passed": True, "type": "tool_json_only"},
            'tool_same_entity': {"skipped": True, "passed": True, "type": "tool_same_entity"},
            'no_reask_style': {"skipped": True, "passed": True, "type": "no_reask_style"},
            'timestamp': turn_number,
            'backend_error': True,
        }
    
    def _get_user_response(self, guru_message: str) -> str:
        """Get simulated user response using conversation context"""

        return self.api.call_gpt41_with_history(self.user_persona, self.conversation_history)

    @staticmethod
    def _format_user_message_for_history(user_message: str) -> str:
        """Prefer human-readable content for history/logs when userMessage is selection JSON."""
        if not isinstance(user_message, str):
            return str(user_message)
        try:
            payload = json.loads(user_message)
        except Exception:
            return user_message
        if not isinstance(payload, dict):
            return user_message
        candidates = payload.get("candidates")
        selected = payload.get("selected")
        if isinstance(candidates, list) and isinstance(selected, int):
            if 0 <= selected < len(candidates):
                try:
                    return str(candidates[selected])
                except Exception:
                    return user_message
        return user_message
    
    def _bootstrap_backend_history(self) -> None:
        """Load initial assistant message(s) from the backend chat session."""
        if self._backend_bootstrap_loaded:
            return
        
        initial_messages = self.api.get_backend_initial_messages()
        self._backend_initial_messages_raw = list(initial_messages)
        project_id = self.api.get_backend_project_id()
        if project_id:
            print(f"> backend: Using minimatics project {project_id}")
        
        if not initial_messages:
            self._backend_bootstrap_loaded = True
            return
        
        for message in initial_messages:
            converted = self._convert_backend_message(message)
            if not converted:
                continue
            self.conversation_history.append(converted)
            if converted['role'] == 'assistant':
                self._display_guru_response(converted['content'])
        
        self._backend_bootstrap_loaded = True
    
    @staticmethod
    def _convert_backend_message(message: Dict[str, Any]) -> Optional[Dict[str, str]]:
        """Convert backend MinimaticsChatMessage to the local role/content format."""
        role = message.get('role')
        if not role:
            return None
        
        content = message.get('rawResponse')
        if not content:
            tool_calls = message.get('toolCalls') or []
            if tool_calls:
                tool_call = tool_calls[0]
                content = json.dumps({
                    "toolName": tool_call.get('type'),
                    "inputParameters": tool_call.get('params', {}),
                    "textResponse": message.get('content')
                })
            else:
                content = message.get('content', '')
        
        return {
            'role': role,
            'content': content or ''
        }
    
    def _evaluate_turn(self, guru_response: str, turn_number: int) -> Dict[str, Any]:
        """Evaluate a single turn using G-Eval evaluators"""
        context = {'turn_number': turn_number}

        def _run_if_enabled(toggle: str, fn, default):
            if self.metric_toggles.is_enabled(toggle):
                return fn()
            return default

        concision_eval = _run_if_enabled(
            "per_turn_concision",
            lambda: self.turn_concision.evaluate_turn(guru_response, turn_number),
            {"skipped": True, "type": "per_turn_concision"},
        )

        # Phase detection (G-Eval) - pass current response for analysis
        phase_eval = _run_if_enabled(
            "phase_evaluation",
            lambda: self.phase_detector.detect_phase(self.conversation_history, context, current_response=guru_response),
            {
                "skipped": True,
                "detected_phase": "story_locking",
                "type": "phase_detection",
                "evaluation_method": "skipped",
            },
        )

        confirmation_eval = _run_if_enabled(
            "confirmation_evaluation",
            lambda: self.confirmation_detector.evaluate_turn(guru_response, context),
            {
                "skipped": True,
                "is_confirmation_request": False,
                "type": "confirmation_detection",
                "evaluation_method": "skipped",
            },
        )

        language_complexity_eval = _run_if_enabled(
            "language_complexity_geval",
            lambda: self.language_complexity_geval.evaluate_turn(guru_response, context),
            {
                "skipped": True,
                "type": "language_complexity_geval",
                "evaluation_method": "skipped",
            },
        )

        dependent_evals = {}
        for toggle, key, fn, default in [
            (
                "stall_evaluation",
                "stall_evaluation",
                lambda: self.stall_detector.evaluate_turn(
                    self.conversation_history,
                    guru_response,
                    {"detected_phase": phase_eval.get("detected_phase"), "turn_number": turn_number},
                ),
                {"skipped": True, "type": "stall_turn_detection", "evaluation_method": "skipped"},
            ),
            (
                "tool_choice_evaluation",
                "tool_choice_evaluation",
                lambda: self.tool_choice_detector.evaluate_turn(
                    self.conversation_history,
                    guru_response,
                    {"detected_phase": phase_eval.get("detected_phase"), "turn_number": turn_number},
                ),
                {
                    "skipped": True,
                    "passed": True,
                    "type": "tool_choice_necessary_sufficient",
                    "evaluation_method": "skipped",
                },
            ),
            (
                "linearity",
                "linearity_evaluation",
                lambda: self.linearity_evaluator.evaluate_turn(
                    guru_response,
                    str(phase_eval.get("detected_phase", "story_locking")),
                    context,
                ),
                {"skipped": True, "type": "linearity"},
            ),
        ]:
            dependent_evals[key] = _run_if_enabled(toggle, fn, default)

        tool_eval_by_name = {}
        for toggle, key, evaluator in [
            ("tool_json_only", "tool_json_only", self.tool_json_only),
            ("tool_same_entity", "tool_same_entity", self.tool_same_entity),
            ("no_reask_style", "no_reask_style", self.no_reask_style),
        ]:
            tool_eval_by_name[key] = _run_if_enabled(
                toggle,
                lambda evaluator=evaluator: evaluator.evaluate_turn(guru_response, turn_number),
                {"skipped": True, "passed": True, "type": key},
            )
        
        return {
            'turn': turn_number,
            'guru_response': guru_response,
            # no Socratic length evaluation
            'per_turn_concision': concision_eval,
            'phase_evaluation': phase_eval,
            'confirmation_evaluation': confirmation_eval,
            'language_complexity_geval': language_complexity_eval,
            'stall_evaluation': dependent_evals["stall_evaluation"],
            'tool_choice_evaluation': dependent_evals["tool_choice_evaluation"],
            'linearity_evaluation': dependent_evals["linearity_evaluation"],
            'tool_json_only': tool_eval_by_name["tool_json_only"],
            'tool_same_entity': tool_eval_by_name["tool_same_entity"],
            'no_reask_style': tool_eval_by_name["no_reask_style"],
            'timestamp': turn_number
        }
    
    def _display_evaluator_feedback(self, turn_eval: Dict[str, Any]) -> None:
        """Display real-time evaluator feedback"""
        feedback_parts: List[str] = []

        # Socratic feedback removed (no longer applicable)

        def _extend_feedback(items) -> None:
            if not items:
                return
            if isinstance(items, str):
                feedback_parts.append(items)
            elif isinstance(items, list):
                feedback_parts.extend([x for x in items if isinstance(x, str) and x])

        def _concision_feedback() -> List[str]:
            concision = turn_eval.get("per_turn_concision", {})
            if not isinstance(concision, dict):
                return []
            wc = concision.get("word_count")
            status = concision.get("status")
            if status == "fail":
                return [f"❌ Too long ({wc} words)"]
            if status == "warn":
                return [f"⚠️ Slightly long ({wc} words)"]
            if wc is not None:
                return [f"✅ Concise ({wc} words)"]
            return []

        def _confirmation_feedback() -> List[str]:
            confirmation = turn_eval.get("confirmation_evaluation", {})
            if isinstance(confirmation, dict) and confirmation.get("is_confirmation_request"):
                return ["🔒 Confirmation request detected"]
            return []

        def _stall_feedback() -> List[str]:
            stall = turn_eval.get("stall_evaluation", {})
            if isinstance(stall, dict) and stall.get("is_stall"):
                return ["⛔ Stall turn detected"]
            return []

        def _phase_feedback() -> List[str]:
            phase = turn_eval.get("phase_evaluation", {})
            if isinstance(phase, dict) and phase.get("detected_phase"):
                return [f"📍 Phase: {phase['detected_phase']}"]
            return []

        def _linearity_feedback() -> List[str]:
            linearity = turn_eval.get("linearity_evaluation", {})
            if isinstance(linearity, dict) and linearity.get("backwards_movement"):
                return [f"⚠️ Backwards: {linearity['regression_details']}"]
            return []

        for toggle, fn in [
            ("per_turn_concision", _concision_feedback),
            ("confirmation_evaluation", _confirmation_feedback),
            ("stall_evaluation", _stall_feedback),
            ("phase_evaluation", _phase_feedback),
            ("linearity", _linearity_feedback),
        ]:
            if self.metric_toggles.is_enabled(toggle):
                _extend_feedback(fn())

        if feedback_parts:
            print(f"> evaluator: {' | '.join(feedback_parts)}")
    
    def create_conversational_test_case(self) -> Optional[ConversationalTestCase]:
        """Create a DeepEval ConversationalTestCase from the conversation history"""
        
        if not self.conversation_history:
            return None
        
        # Convert conversation history to Turn objects for ConversationalTestCase
        turns = []
        for msg in self.conversation_history:
            # Map roles appropriately
            role: Literal["user", "assistant"] = "user" if msg["role"] == "user" else "assistant" 
            turn = Turn(
                role=role,
                content=msg["content"]
            )
            turns.append(turn)
        
        # Create ConversationalTestCase
        conversational_test_case = ConversationalTestCase(
            turns=turns,
            scenario="Minimatics story creation conversation",
            user_description="Simulated user persona helping create a story",
            additional_metadata={
                "system_prompt": self.guru_system_prompt,
                "persona": self.user_persona,
                "conversation_metadata": {
                    "total_turns": len(self.turn_evaluations),
                    "json_parsing_failures": len(self.json_parsing_failures),
                    "completion_reason": "max_turns_reached" if len(self.turn_evaluations) >= 25 else "ongoing"
                }
            }
        )
        
        return conversational_test_case
    
    def evaluate_with_deepeval(self, metrics: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Evaluate the conversation using DeepEval metrics"""
        
        conversational_test_case = self.create_conversational_test_case()
        if not conversational_test_case:
            return {"error": "Failed to create ConversationalTestCase"}
        
        # Default metrics if none provided
        if metrics is None:
            metrics = []
        
        results = {}
        for metric in metrics:
            try:
                score = metric.measure(conversational_test_case)
                results[metric.name] = {
                    "score": score,
                    "passed": score >= metric.threshold,
                    "reason": getattr(metric, 'reason', 'No reason provided')
                }
            except Exception as e:
                results[metric.name] = {
                    "error": str(e),
                    "score": 0,
                    "passed": False
                }
        
        return results
    
    def _display_guru_response(self, guru_response: str) -> None:
        """Parse and display guru response (JSON format expected)"""
        stripped = guru_response.strip()
        if not stripped or stripped[0] not in ('{', '['):
            print(f"<guru>: {guru_response}")
            return

        try:
            import json
            response_data = json.loads(guru_response)

            if 'textResponse' in response_data:
                print(f"<guru>: {response_data['textResponse']}")

            tool_calls = extract_tool_calls(guru_response)
            if tool_calls:
                tool_name = tool_calls[0].get("type", "unknown")
                print(f"\t🔧 toolCall: {tool_name}")
                if tool_name == "workflow_multiple_choice":
                    options = None
                    data = response_data.get("toolCalls")
                    if isinstance(data, list):
                        for call in data:
                            if not isinstance(call, dict):
                                continue
                            call_data = call.get("data") or {}
                            if isinstance(call_data, dict) and isinstance(call_data.get("options"), list):
                                options = call_data.get("options")
                                break
                    if isinstance(options, list) and options:
                        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        for idx, opt in enumerate(options):
                            label = letters[idx] if idx < len(letters) else str(idx + 1)
                            print(f"\t  {label}. {opt}")

        except json.JSONDecodeError as e:
            print(f"<guru>: {guru_response}")
            print(f"\t⚠️  JSON parsing failed: {str(e)}")

            self.json_parsing_failures.append({
                'turn': len(self.turn_evaluations) + 1,
                'error': str(e),
                'raw_response': guru_response[:200] + "..." if len(guru_response) > 200 else guru_response
            })
    
    def _generate_report(self) -> Dict[str, Any]:
        """Generate final conversation report"""
        total_turns = len(self.turn_evaluations)
        violations = []
        
        # Collect all violations
        concision_failures = 0
        confirmation_requests = 0
        linearity_violations = 0
        
        for turn_eval in self.turn_evaluations:
            turn_num = turn_eval['turn']
            
            # Socratic violations removed
            
            # Check concision violations
            concision = turn_eval.get('per_turn_concision', {})
            if concision.get('status') == 'fail':
                concision_failures += 1
                violations.append({
                    'turn': turn_num,
                    'type': 'per_turn_concision',
                    'description': f"Too long: {concision.get('word_count')} words"
                })
            
            # Count confirmation requests
            if turn_eval['confirmation_evaluation']['is_confirmation_request']:
                confirmation_requests += 1
                
            # Check linearity violations
            linearity = turn_eval.get('linearity_evaluation', {})
            if linearity.get('backwards_movement'):
                linearity_violations += 1
                violations.append({
                    'turn': turn_num,
                    'type': 'linearity',
                    'description': f"Linearity violation: {linearity.get('regression_details', 'backwards movement')}",
                    'details': linearity
                })
        
        # Calculate metrics
        word_counts = [len(extract_text_content(turn['guru_response']).split()) for turn in self.turn_evaluations]
        avg_length = sum(word_counts) / len(word_counts) if word_counts else 0

        # Deterministic language complexity metrics (per role + combined)
        assistant_texts: List[str] = []
        assistant_options_texts: List[str] = []
        assistant_combined_texts: List[str] = []
        for msg in self.conversation_history:
            role = msg.get("role")
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            if role == "assistant":
                text_resp, options = extract_text_response_and_options(content)
                if text_resp:
                    assistant_texts.append(text_resp)
                if options:
                    joined = " ".join(options)
                    assistant_options_texts.append(joined)
                combined = " ".join([text_resp, " ".join(options)]).strip()
                if combined:
                    assistant_combined_texts.append(combined)
        language_complexity = {
            "assistant": {
                "prompt_text": compute_language_metrics(assistant_texts),
                "multiple_options": compute_language_metrics(assistant_options_texts),
                "combined": compute_language_metrics(assistant_combined_texts),
            }
        }

        # G-Eval language complexity summary (aggregate from per-turn scores)
        def _aggregate_component(component: str) -> Dict[str, Any]:
            scores: List[float] = []
            reason_sample: Optional[str] = None
            for turn_eval in self.turn_evaluations:
                lc = turn_eval.get("language_complexity_geval") or {}
                if not isinstance(lc, dict):
                    continue
                comp = lc.get(component) if component != "combined" else lc.get("combined")
                if isinstance(comp, dict):
                    raw = comp.get("g_eval_score")
                    if isinstance(raw, (int, float)):
                        scores.append(float(raw))
                    if not reason_sample:
                        reason = comp.get("g_eval_reason")
                        if isinstance(reason, str) and reason.strip():
                            reason_sample = reason.strip()
            return {
                "turns_scored": len(scores),
                "average_score": (sum(scores) / len(scores)) if scores else None,
                "min_score": min(scores) if scores else None,
                "max_score": max(scores) if scores else None,
                "reason_sample": reason_sample,
            }

        lc_summary = {
            "scale": "0-1",
            "prompt_text": _aggregate_component("prompt_text"),
            "multiple_options": _aggregate_component("multiple_options"),
            "combined": _aggregate_component("combined"),
        }
        
        # Check completion reason
        completion_reason = 'max_turns_reached'
        if self._backend_error:
            completion_reason = 'backend_error'
        else:
            for msg in self.conversation_history:
                if 'shot_thinker' in msg.get('content', '').lower():
                    completion_reason = 'shot_thinker_called'
                    break
        
        # Get final story completeness evaluation
        if self._skip_geval:
            final_completeness = {
                "completeness_percentage": 0.0,
                "completed_components": 0,
                "total_components": 4,
                "ready_for_shot_thinker": False,
                "missing_components": [],
                "component_details": {},
                "skipped": True,
            }
        else:
            final_completeness = self.completeness_evaluator.evaluate_conversation(self.conversation_history)

        efficiency_metrics = self._generate_efficiency_metrics()
        failure_taxonomy = self._build_failure_taxonomy()
        
        return {
            'summary': {
                'total_turns': total_turns,
                'violations_count': len(violations),
                'length_adherence_percent': None,
                'average_answer_length': avg_length,
                'completion_reason': completion_reason,
                'backend_error': self._backend_error,
                'persona_used': self.persona_name,
                'system_prompt_used': self.system_prompt_name,
                'metric_toggles': self.metric_toggles.as_dict(),
                'json_parsing_failures': len(self.json_parsing_failures),
                'confirmation_requests': confirmation_requests,
                'confirmation_requests_per_turn': confirmation_requests / total_turns if total_turns > 0 else 0,
                'linearity_violations': linearity_violations,
                'linearity_adherence_percent': ((total_turns - linearity_violations) / total_turns * 100) if total_turns > 0 else 0,
                'story_completeness_percent': final_completeness['completeness_percentage'],
                'story_ready_for_shot_thinker': final_completeness['ready_for_shot_thinker'],
                'story_components_completed': final_completeness['completed_components'],
                'story_components_missing': final_completeness['missing_components'],
                'language_complexity': language_complexity,
                'language_complexity_geval': lc_summary,
                **self.api.get_model_metadata(),
                **get_geval_judge_metadata(),
                **efficiency_metrics.get("summary", {}),
                **failure_taxonomy.get("summary", {}),
            },
            'violations': violations,
            'json_parsing_failures': self.json_parsing_failures,
            'turn_evaluations': self.turn_evaluations,
            'conversation_history': self.conversation_history,
            'efficiency_metrics': efficiency_metrics,
            'failure_taxonomy': failure_taxonomy,
        }

    def _build_failure_taxonomy(self) -> Dict[str, Any]:
        """
        Step 3.1/3.2: Tag failures so we can slice runs by primary cause (not just count them).

        Each failure has:
        - primary_tag: instruction_following | phase_progression | clarification_hygiene |
                       constraint_adherence | tool_correctness | user_experience
        - secondary_tags: optional list
        - evidence: turn index + short excerpt
        """

        failures: List[Dict[str, Any]] = []

        def add_failure(
            *,
            primary_tag: str,
            metric_type: str,
            turn: Optional[int],
            description: str,
            evidence: Optional[str] = None,
            secondary_tags: Optional[List[str]] = None,
        ) -> None:
            failures.append(
                {
                    "primary_tag": primary_tag,
                    "secondary_tags": secondary_tags or [],
                    "metric_type": metric_type,
                    "turn": turn,
                    "description": description,
                    "evidence": evidence,
                }
            )

        # JSON parsing failures (assistant output wasn't valid JSON when expected)
        for failure in self.json_parsing_failures:
            turn = failure.get("turn")
            add_failure(
                primary_tag="instruction_following",
                metric_type="json_parsing_failure",
                turn=turn if isinstance(turn, int) else None,
                description="Assistant response failed JSON parsing (format compliance).",
                evidence=failure.get("raw_response"),
                secondary_tags=["tool_correctness"],
            )

        # Violations currently tracked in summary (linearity + concision)
        for v in self.turn_evaluations:
            turn = v.get("turn")
            if not isinstance(turn, int):
                continue

            if self.metric_toggles.is_enabled("linearity"):
                linearity = v.get("linearity_evaluation") or {}
                if isinstance(linearity, dict) and linearity.get("backwards_movement"):
                    add_failure(
                        primary_tag="phase_progression",
                        metric_type="linearity",
                        turn=turn,
                        description=str(linearity.get("regression_details") or "Linearity regression"),
                        evidence=extract_text_content(v.get("guru_response", ""))[:220],
                    )

            if self.metric_toggles.is_enabled("per_turn_concision"):
                concision = v.get("per_turn_concision") or {}
                if isinstance(concision, dict) and concision.get("status") == "fail":
                    add_failure(
                        primary_tag="user_experience",
                        metric_type="per_turn_concision",
                        turn=turn,
                        description=f"Too long ({concision.get('word_count')} words).",
                        evidence=extract_text_content(v.get("guru_response", ""))[:220],
                        secondary_tags=["clarification_hygiene"],
                    )

            # Tool hygiene violations (focus group metrics)
            for key, primary_tag in [
                ("tool_json_only", "tool_correctness"),
                ("tool_same_entity", "constraint_adherence"),
                ("no_reask_style", "clarification_hygiene"),
            ]:
                if not self.metric_toggles.is_enabled(key):
                    continue
                res = v.get(key)
                if isinstance(res, dict) and res.get("passed") is False:
                    add_failure(
                        primary_tag=primary_tag,
                        metric_type=key,
                        turn=turn,
                        description=f"{key} failed.",
                        evidence=extract_text_content(v.get("guru_response", ""))[:220],
                    )

            if self.metric_toggles.is_enabled("stall_evaluation"):
                stall_eval = v.get("stall_evaluation") or {}
                if isinstance(stall_eval, dict) and stall_eval.get("is_stall") is True:
                    add_failure(
                        primary_tag="phase_progression",
                        metric_type="stall_geval",
                        turn=turn,
                        description="Stall turn detected by G-Eval (assistant could have advanced).",
                        evidence=extract_text_content(v.get("guru_response", ""))[:220],
                        secondary_tags=["clarification_hygiene"],
                    )

            if self.metric_toggles.is_enabled("tool_choice_evaluation"):
                tool_choice = v.get("tool_choice_evaluation") or {}
                if isinstance(tool_choice, dict) and tool_choice.get("passed") is False:
                    add_failure(
                        primary_tag="tool_correctness",
                        metric_type="tool_choice_necessary_sufficient",
                        turn=turn,
                        description="Tool choice judged not necessary/sufficient (G-Eval).",
                        evidence=tool_choice.get("g_eval_reason"),
                    )

        # Shot thinker gate failures (if evaluated on termination)
        if self.turn_evaluations and isinstance(self.turn_evaluations[-1], dict):
            shot_gate = self.turn_evaluations[-1].get("shot_thinker_gate")
            if isinstance(shot_gate, dict) and shot_gate.get("passed") is False:
                add_failure(
                    primary_tag="phase_progression",
                    metric_type="shot_thinker_gate",
                    turn=self.turn_evaluations[-1].get("turn") if isinstance(self.turn_evaluations[-1].get("turn"), int) else None,
                    description="shot_thinker called before required gate conditions were met.",
                    evidence=None,
                    secondary_tags=["instruction_following"],
                )

        # Waste proxies from efficiency telemetry
        for t in self.turn_telemetry:
            turn = t.get("turn")
            if not isinstance(turn, int):
                continue
            waste = t.get("waste") or {}
            if not isinstance(waste, dict):
                continue
            for toggle, key, primary_tag, description, secondary_tags in [
                ("repeated_ask", "repeated_ask", "clarification_hygiene", "Repeated/near-duplicate question (proxy).", None),
                (
                    "stall_turn_proxy",
                    "stall_turn_proxy",
                    "phase_progression",
                    "Assistant asked a question without tool advance (proxy).",
                    ["clarification_hygiene"],
                ),
            ]:
                if self.metric_toggles.is_enabled(toggle) and waste.get(key) is True:
                    add_failure(
                        primary_tag=primary_tag,
                        metric_type=key,
                        turn=turn,
                        description=description,
                        evidence=None,
                        secondary_tags=secondary_tags,
                    )

        counts: Dict[str, int] = {}
        for f in failures:
            tag = f.get("primary_tag")
            if isinstance(tag, str):
                counts[tag] = counts.get(tag, 0) + 1

        summary = {
            "failure_count_total": len(failures),
            "failure_counts_by_primary_tag": counts,
        }

        return {"summary": summary, "failures": failures}

    def _generate_efficiency_metrics(self) -> Dict[str, Any]:
        """Aggregate latency/token/tool/waste proxies across the run."""
        assistant_latencies: List[float] = [
            float(t["assistant_latency_ms"])
            for t in self.turn_telemetry
            if isinstance(t.get("assistant_latency_ms"), (int, float))
        ]
        persona_latencies: List[float] = [
            float(t["persona_latency_ms"])
            for t in self.turn_telemetry
            if isinstance(t.get("persona_latency_ms"), (int, float))
        ]
        backend_totals: List[float] = []
        backend_requests: List[float] = []
        backend_asset_polls: List[float] = []
        tool_calls_counts: List[int] = []
        asset_job_counts: List[int] = []

        persona_input_tokens = 0
        persona_output_tokens = 0
        persona_total_tokens = 0

        question_marks_total = 0
        question_count_total = 0
        repeated_ask_turns = 0
        stall_turns = 0

        for t in self.turn_telemetry:
            backend = t.get("backend") or {}
            if isinstance(backend, dict):
                for key, dest in [
                    ("backend_total_ms", backend_totals),
                    ("backend_request_ms", backend_requests),
                    ("asset_poll_ms", backend_asset_polls),
                ]:
                    val = backend.get(key)
                    if isinstance(val, (int, float)):
                        dest.append(float(val))
                tc = backend.get("tool_calls_count")
                if isinstance(tc, int):
                    tool_calls_counts.append(tc)
                aj = backend.get("asset_job_count")
                if isinstance(aj, int):
                    asset_job_counts.append(aj)

            persona = t.get("persona") or {}
            if isinstance(persona, dict):
                usage = persona.get("usage") or {}
                if isinstance(usage, dict):
                    # Common OpenAI-ish keys; keep best-effort.
                    persona_input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                    persona_output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
                    persona_total_tokens += int(usage.get("total_tokens") or 0)

            waste = t.get("waste") or {}
            if isinstance(waste, dict):
                qm = waste.get("question_mark_count")
                qc = waste.get("question_count")
                if isinstance(qm, int):
                    question_marks_total += qm
                if isinstance(qc, int):
                    question_count_total += qc
                for toggle, key, counter in [
                    ("repeated_ask", "repeated_ask", "repeated_ask_turns"),
                    ("stall_turn_proxy", "stall_turn_proxy", "stall_turns"),
                ]:
                    if self.metric_toggles.is_enabled(toggle) and waste.get(key) is True:
                        if counter == "repeated_ask_turns":
                            repeated_ask_turns += 1
                        elif counter == "stall_turns":
                            stall_turns += 1

        summary: Dict[str, Any] = {
            # Latency aggregates
            "assistant_latency_p50_ms": self._percentile(assistant_latencies, 50),
            "assistant_latency_p95_ms": self._percentile(assistant_latencies, 95),
            "persona_latency_p50_ms": self._percentile(persona_latencies, 50),
            "persona_latency_p95_ms": self._percentile(persona_latencies, 95),
            "backend_total_p50_ms": self._percentile(backend_totals, 50),
            "backend_total_p95_ms": self._percentile(backend_totals, 95),
            "backend_request_p50_ms": self._percentile(backend_requests, 50),
            "backend_request_p95_ms": self._percentile(backend_requests, 95),
            "backend_asset_poll_p50_ms": self._percentile(backend_asset_polls, 50),
            "backend_asset_poll_p95_ms": self._percentile(backend_asset_polls, 95),
            # Tool cost proxies
            "tool_calls_total": int(sum(tool_calls_counts)) if tool_calls_counts else 0,
            "asset_jobs_total": int(sum(asset_job_counts)) if asset_job_counts else 0,
            # Token usage (persona simulation only for now)
            "persona_input_tokens_total": persona_input_tokens,
            "persona_output_tokens_total": persona_output_tokens,
            "persona_total_tokens_total": persona_total_tokens,
            # Waste proxies
            "question_marks_total": question_marks_total,
            "question_count_total": question_count_total,
        }
        for toggle, key, value in [
            ("repeated_ask", "repeated_ask_turns", repeated_ask_turns),
            ("stall_turn_proxy", "stall_turns_proxy", stall_turns),
        ]:
            if self.metric_toggles.is_enabled(toggle):
                summary[key] = value
        return {
            "summary": summary,
            "turns": self.turn_telemetry,
        }

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> Optional[float]:
        if not values:
            return None
        if percentile <= 0:
            return float(min(values))
        if percentile >= 100:
            return float(max(values))
        sorted_vals = sorted(values)
        k = (len(sorted_vals) - 1) * (percentile / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_vals) - 1)
        if f == c:
            return float(sorted_vals[f])
        d0 = sorted_vals[f] * (c - k)
        d1 = sorted_vals[c] * (k - f)
        return float(d0 + d1)
