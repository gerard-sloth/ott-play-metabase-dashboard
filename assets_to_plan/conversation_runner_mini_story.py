"""
Mini-story specific conversation runner.

This keeps the base `ConversationRunner` clean (persona-driven) and provides
a dedicated runner for the gamified dashboard-CTA workflow.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, List

import json
import re
import os

from .conversation_runner import ConversationRunner
from ..evaluators import extract_tool_calls, extract_text_content
from ..simulation.mini_story_gamified_simulation import MiniStoryGamifiedSimulator, CtaType, HIJACK_ATTEMPTS


class MiniStoryConversationRunner(ConversationRunner):
    def __init__(
        self,
        persona_path: str,
        temperature: float = 1.0,
        test_mode: bool = True,
        cta: Optional[CtaType] = None,
        hijack_turn: Optional[int] = None,
        guru_version_id: str = "",
        mini_story_id: Optional[str] = None,
        skip_geval: bool = False,
    ):
        super().__init__(
            persona_path=persona_path,
            temperature=temperature,
            test_mode=test_mode,
            guru_version_id=guru_version_id,
            backend_route="minimatics/mini-story",
            mini_story_id=mini_story_id,
            skip_geval=skip_geval,
        )
        self._cta: Optional[CtaType] = cta
        self._cta_selected: Optional[CtaType] = None
        self._hijack_turn = hijack_turn
        self._hijack_injected = False
        self._hijack_message: Optional[str] = None
        self._response_turn_index = 0
        self._mini_story_pitch: Optional[str] = None
        self._skip_hijack_next = False
        self._last_mcq_options: List[str] = []

    def _seed_initial_style_turn(self) -> Optional[str]:
        """
        Mini-story flow starts with a story pitch + CTA intent.
        Do not pre-inject an art-style selection before the pitch.
        """
        return None

    def _get_user_response(self, guru_message: str) -> str:
        """LLM-driven user input for the mini-story workflow."""
        self._response_turn_index += 1
        if self._skip_hijack_next:
            self._skip_hijack_next = False
        elif self._hijack_turn and self._response_turn_index == self._hijack_turn and not self._hijack_injected:
            self._hijack_injected = True
            self._hijack_message = HIJACK_ATTEMPTS[self._hijack_turn % len(HIJACK_ATTEMPTS)]
            return self._hijack_message
        text = extract_text_content(guru_message)
        if self._is_welcome_or_points(text) or self._is_pitch_display(text):
            return "Got it."
        if self._is_cta_prompt(text):
            topic = self._cta_to_topic(self._cta) if self._cta else "story"
            return topic.title()
        tool_calls = extract_tool_calls(guru_message)
        if tool_calls:
            tool_type = tool_calls[0].get("type")
            if tool_type == "workflow_multiple_choice":
                self._last_mcq_options = self._extract_mcq_options(guru_message) or []
                return self._select_mcq_option(guru_message)
        persona_prompt = self._build_persona_prompt()
        if self._last_mcq_options:
            options_block = "\n".join(f"- {opt}" for opt in self._last_mcq_options)
            persona_prompt = (
                f"{persona_prompt}\n\n"
                "You are answering an open-ended question. "
                "Reply in 1-2 sentences, not a letter or option label. "
                "Do NOT repeat any of these option phrases verbatim:\n"
                f"{options_block}"
            )
        if os.getenv("MINIMATICS_DEBUG_CONTEXT", "").strip().lower() in ("1", "true", "yes"):
            try:
                print(f"> debug: persona_prompt={json.dumps(persona_prompt)}")
                print(f"> debug: persona_history={json.dumps(self.conversation_history, default=str)}")
            except Exception:
                print(f"> debug: persona_prompt={persona_prompt}")
                print(f"> debug: persona_history={self.conversation_history}")
        response = self.api.call_gpt41_with_history(persona_prompt, self.conversation_history)
        if self._last_mcq_options:
            def _norm(text: str) -> str:
                return " ".join(text.lower().split())
            response_norm = _norm(response)
            if any(_norm(opt) == response_norm for opt in self._last_mcq_options):
                stricter_prompt = (
                    f"{persona_prompt}\n\n"
                    "Do NOT copy any option text. Provide a fresh, original 1-2 sentence answer."
                )
                response = self.api.call_gpt41_with_history(stricter_prompt, self.conversation_history)
        response = self._sanitize_user_response(response)
        response_stripped = response.strip()
        cta_topic = self._extract_cta_topic(response_stripped)
        if response_stripped.lower().startswith("/cta") and cta_topic:
            return cta_topic.title()
        if "/cta" in response_stripped.lower() and cta_topic:
            return cta_topic.title()
        return response_stripped

    def run_conversation(self, initial_message: str = "hi", max_turns: int = 25) -> Dict[str, Any]:
        """
        Run the mini-story conversation using LLM-driven persona responses.

        Note: `initial_message` is ignored; we start from a CTA selection (CLI or LLM).
        """
        self._suppress_header = True
        print(f"\n=== STARTING CONVERSATION ===")
        print(f"Persona: {self.persona_name}")
        print(f"Max turns: {max_turns}")
        print("=" * 50)
        # Ensure backend history is loaded before choosing the initial CTA response.
        self._bootstrap_backend_history()
        self._mini_story_pitch = self._extract_pitch_from_initial_messages(self._backend_initial_messages_raw)
        initial_message = self._get_initial_user_message()
        report = super().run_conversation(initial_message=initial_message, max_turns=max_turns)

        # Post-hoc gamified scoring + growth-moment detection from the full transcript.
        # We use a fresh simulator instance so scoring is based on the actual messages sent.
        scoring = MiniStoryGamifiedSimulator(self._resolve_scoring_cta())
        if self._mini_story_pitch:
            scoring.seed_pitch_points(self._mini_story_pitch)
        assistant_turn = 0
        for msg in report.get("conversation_history", []) or []:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "user":
                scoring.observe_user_message_for_points(content)
            elif role == "assistant":
                assistant_turn += 1
                scoring.observe_guru_response(content, assistant_turn)

        gamified_report = scoring.as_report_dict()
        report["gamified"] = gamified_report
        report.setdefault("summary", {})
        report["summary"]["gamified"] = {
            "simulation": gamified_report.get("simulation"),
            "cta": gamified_report.get("cta"),
            "points_total": (gamified_report.get("points") or {}).get("total_points"),
            "level": (gamified_report.get("points") or {}).get("level"),
        }
        
        # Hijack resistance evaluation (if enabled)
        if self._hijack_turn and self._hijack_injected:
            from ..evaluators.hijack_evaluator import evaluate_hijack_resistance
            
            hijack_eval = evaluate_hijack_resistance(
                conversation_history=report.get("conversation_history", []),
                hijack_message=self._hijack_message,
            )
            report["hijack_resistance"] = hijack_eval
            report["summary"]["hijack_resistance"] = {
                "passed": hijack_eval.get("passed"),
                "reason": hijack_eval.get("reason"),
            }
            # Also include hijack info
            report["gamified"]["hijack_test"] = {
                "enabled": True,
                "turn": self._hijack_turn,
                "injected": self._hijack_injected,
                "hijack_message": self._hijack_message,
            }
        
        return report

    def _get_initial_user_message(self) -> str:
        if self._cta:
            topic = self._cta_to_topic(self._cta)
            self._cta_selected = self._topic_to_cta(topic)
            return topic.title()
        self._skip_hijack_next = True
        raw = self._get_user_response(self.conversation_history[-1]["content"] if self.conversation_history else "")
        topic = self._extract_cta_topic(raw) or "story"
        self._cta_selected = self._topic_to_cta(topic)
        return topic.title()

    @staticmethod
    def _cta_to_topic(cta: CtaType) -> str:
        if cta in ("story", "top_reward"):
            return "story"
        if cta.startswith("character"):
            return "character"
        return "location"

    @staticmethod
    def _topic_to_cta(topic: str) -> CtaType:
        if topic == "character":
            return "character_1"
        if topic == "location":
            return "location_1"
        return "story"

    def _resolve_scoring_cta(self) -> CtaType:
        if self._cta_selected:
            return self._cta_selected
        if self._cta:
            return self._cta
        return "story"

    @staticmethod
    def _extract_cta_topic(text: str) -> Optional[str]:
        lowered = text.strip().lower()
        if lowered.startswith("/cta"):
            lowered = lowered.replace("/cta", "", 1).strip()
        if lowered in ("story", "character", "location"):
            return lowered
        if "character" in lowered:
            return "character"
        if "location" in lowered:
            return "location"
        if "story" in lowered:
            return "story"
        return None

    def _build_persona_prompt(self) -> str:
        pitch = self._mini_story_pitch or "Unknown pitch"
        return (
            "STYLE PROFILE (use tone only, ignore any story content in this profile):\n"
            f"{self.user_persona}\n\n"
            "STORY CONTEXT\n"
            f"Pitch: {pitch}\n\n"
            "You are the user collaborating to improve this exact story. "
            "Do not invent a different story or introduce new characters/settings not implied by the pitch. "
            "Do not use slash commands like /cta. "
            "If asked to choose a topic, respond with exactly one word: Story, Character, or Location."
        )

    @staticmethod
    def _extract_pitch_from_initial_messages(messages: List[Dict[str, Any]]) -> Optional[str]:
        if not messages:
            return None
        def _is_pitch_candidate(text: str) -> bool:
            lowered = text.strip().lower()
            if not lowered:
                return False
            if lowered.startswith("welcome!"):
                return False
            if "you’ve already earned" in lowered or "you've already earned" in lowered:
                return False
            if lowered.startswith("to get started"):
                return False
            if lowered.endswith("?"):
                return False
            return len(text.split()) >= 15

        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content") or msg.get("rawResponse")
            if isinstance(content, str) and _is_pitch_candidate(content):
                return content.strip()

        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content") or msg.get("rawResponse")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return None

    def _select_mcq_option(self, guru_message: str) -> str:
        options = self._extract_mcq_options(guru_message)
        if not options:
            return "A"
        question = extract_text_content(guru_message).strip()
        letters = [chr(ord("A") + i) for i in range(len(options))]
        options_block = "\n".join(f"{letter}. {opt}" for letter, opt in zip(letters, options))
        prompt = (
            f"{self._build_persona_prompt()}\n\n"
            "You must choose exactly one option below. Respond with a single letter only.\n"
            f"Question: {question}\n"
            f"Options:\n{options_block}"
        )
        response = self.api.call_gpt41_with_history(prompt, self.conversation_history)
        idx = None
        match = re.search(r"[A-E]", response.strip().upper())
        if match:
            idx = ord(match.group(0)) - ord("A")
        if idx is None:
            # Fallback: try to match option text directly
            response_lower = response.strip().lower()
            for opt_idx, opt in enumerate(options):
                if opt.lower() in response_lower:
                    idx = opt_idx
                    break
        if idx is None or idx < 0 or idx >= len(options):
            idx = 0

        # Return the plain option label so the backend LLM sees the human choice.
        # (MiniStory LLM does not consume displayContent; it only sees userMessage.)
        return options[idx]

    @staticmethod
    def _extract_mcq_options(guru_message: str) -> List[str]:
        try:
            data = json.loads(guru_message)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        tool_calls = data.get("toolCalls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                data_block = call.get("data") or {}
                options = data_block.get("options")
                if isinstance(options, list) and options:
                    return [str(opt) for opt in options]
        return []

    @staticmethod
    def _is_cta_prompt(text: str) -> bool:
        lowered = text.lower()
        return "what would you like to work on first" in lowered

    @staticmethod
    def _is_welcome_or_points(text: str) -> bool:
        lowered = text.lower()
        if lowered.startswith("welcome"):
            return True
        if "earned" in lowered and "points" in lowered:
            return True
        return False

    def _is_pitch_display(self, text: str) -> bool:
        if not self._mini_story_pitch:
            return False
        return text.strip() == self._mini_story_pitch.strip()

    @staticmethod
    def _sanitize_user_response(response: str) -> str:
        cleaned = re.sub(r"/cta\s+\S+", "", response, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"/cta\b", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            return response.strip()
        return cleaned

    def _bootstrap_backend_history(self) -> None:
        """Load only the initial assistant seed messages (stop before the first user message)."""
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
        filtered: List[Dict[str, Any]] = []
        seen_ids: List[str] = []
        for message in initial_messages:
            if message.get("role") == "user":
                break
            filtered.append(message)
            message_id = message.get("messageId") or message.get("chatMessageId")
            if isinstance(message_id, str):
                seen_ids.append(message_id)
        for message in filtered:
            converted = self._convert_backend_message(message)
            if not converted:
                continue
            self.conversation_history.append(converted)
            if converted['role'] == 'assistant':
                self._display_guru_response(converted['content'])
        if seen_ids:
            self.api.mark_backend_messages_seen(seen_ids)
        self._backend_bootstrap_loaded = True
