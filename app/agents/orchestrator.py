from __future__ import annotations

import json
from typing import Iterable

from pydantic import BaseModel, Field

from app.config import ConfigBundle, RuntimeSettings
from app.models import (
    RedesignRequest,
    RetryCounters,
    SessionState,
    TurnRequest,
    UserFeedbackMemory,
    UserProfileMemory,
)
from app.services.turn_parser import extract_feedback_hint, parse_profile_updates
from app.services.validation import invoke_with_schema_retry


class OrchestratorValidationOutput(BaseModel):
    is_ready: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    invalid_fields: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    normalized_profile: dict[str, object] = Field(default_factory=dict)


class OrchestratorAgent:
    def __init__(self, config: ConfigBundle, settings: RuntimeSettings) -> None:
        self._config = config
        self._settings = settings

    async def apply_turn(self, state: SessionState, turn: TurnRequest) -> SessionState:
        state.current_phase = "collect"
        state.next_agent = "orchestrator"
        state.pending_question = None
        state.warnings = []
        state.errors = []
        state.retry_counters = RetryCounters()
        state.search_plan = None
        state.found_products = []
        state.outfit_cards = []

        profile = UserProfileMemory.model_validate(state.memory_snapshot.user_profile.model_dump(mode="json"))
        profile = self._merge_profile(profile, turn)
        state.memory_snapshot.user_profile = profile

        feedback = self._build_feedback(state, turn)
        if feedback is not None:
            state.memory_snapshot.user_profile.latest_feedback = feedback

        intent, selected_outfit_id = self._resolve_intent(turn, feedback)
        state.turn_intent = intent
        state.selected_outfit_id = selected_outfit_id

        if intent in {"search", "modify"}:
            if not selected_outfit_id or not self._is_valid_outfit_selection(state, selected_outfit_id):
                state.status = "await_user_choice"
                state.next_agent = "orchestrator"
                state.assistant_message = "請先從目前三套搭配中選擇一套，再決定要搜尋或修改。"
                return state

        if intent == "design":
            is_ready, message = await self._validate_profile_for_design(state)
            if not is_ready:
                state.status = "await_user_choice"
                state.next_agent = "orchestrator"
                state.assistant_message = ""
                state.pending_question = message
                return state

        state.current_design_context.pending_redesign_requests = []
        if intent == "modify" and selected_outfit_id:
            reason = feedback.reason if feedback and feedback.reason else "使用者希望調整這套搭配。"
            suggestions = self._build_redesign_suggestions(feedback)
            state.current_design_context.pending_redesign_requests = [
                RedesignRequest(outfit_id=selected_outfit_id, reason=reason, suggestions=suggestions)
            ]

        state.status = "run_started"
        if intent == "design":
            state.next_agent = "designer"
            state.assistant_message = "收到完整需求，我去叫設計團隊起床工作。"
        elif intent == "modify":
            state.next_agent = "reviewer"
            state.assistant_message = "收到修改需求，發回去給設計團隊重做。"
        else:
            state.next_agent = "planner"
            state.assistant_message = "收到搜尋需求，看看有誰有空幫忙找一下。"

        return state

    async def _validate_profile_for_design(self, state: SessionState) -> tuple[bool, str]:
        required_fields = self._required_profile_fields()
        profile_payload = state.memory_snapshot.user_profile.model_dump(mode="json")

        missing_fields = [
            field
            for field in required_fields
            if not self._has_value(profile_payload.get(field))
        ]
        if missing_fields:
            return False, self._build_profile_error_message(missing_fields, [], [], required_fields)

        brand = str(profile_payload.get("brand") or "").strip()
        if brand and not self._is_supported_brand(brand):
            return False, "目前僅支援 UNIQLO 與 GU，其他品牌暫不支援使用。"

        validation = await self._semantic_validate_profile(
            state=state,
            required_fields=required_fields,
            profile_payload=profile_payload,
        )

        normalized = validation.normalized_profile if isinstance(validation.normalized_profile, dict) else {}
        self._apply_normalized_profile(state, normalized)

        semantic_missing = [field for field in validation.missing_fields if field in required_fields]
        invalid_fields = [field for field in validation.invalid_fields if field in required_fields]
        all_missing = list(dict.fromkeys([*missing_fields, *semantic_missing]))

        if all_missing or invalid_fields or not validation.is_ready:
            return False, self._build_profile_error_message(
                all_missing,
                invalid_fields,
                validation.questions,
                required_fields,
            )

        return True, ""

    def _is_supported_brand(self, brand: str) -> bool:
        token = (brand or "").strip().upper()
        supported = {str(item).strip().upper() for item in self._config.brands if str(item).strip()}
        if not supported:
            supported = {"UNIQLO", "GU"}
        return token in supported

    async def _semantic_validate_profile(
        self,
        *,
        state: SessionState,
        required_fields: list[str],
        profile_payload: dict,
    ) -> OrchestratorValidationOutput:
        fallback = self._fallback_validation(required_fields, profile_payload)
        output = await invoke_with_schema_retry(
            system_prompt=self._config.prompts.get("orchestrator_system", ""),
            user_prompt=json.dumps(
                {
                    "required_fields": required_fields,
                    "profile": profile_payload,
                },
                ensure_ascii=False,
            ),
            output_model=OrchestratorValidationOutput,
            model_name=self._config.models.get("orchestrator", self._settings.groq_orchestrator_model),
            groq_api_key=self._settings.groq_api_key,
            fallback_factory=lambda: fallback,
            max_retries=2,
        )
        return self._coerce_validation_output(output, fallback)

    def _coerce_validation_output(
        self,
        output: OrchestratorValidationOutput,
        fallback: OrchestratorValidationOutput,
    ) -> OrchestratorValidationOutput:
        missing_fields = [
            str(field).strip()
            for field in (output.missing_fields or [])
            if str(field).strip()
        ]
        invalid_fields = [
            str(field).strip()
            for field in (output.invalid_fields or [])
            if str(field).strip()
        ]
        questions = [
            str(item).strip()
            for item in (output.questions or [])
            if str(item).strip()
        ]
        notes = [
            str(item).strip()
            for item in (output.notes or [])
            if str(item).strip()
        ]

        normalized = output.normalized_profile if isinstance(output.normalized_profile, dict) else {}

        is_ready = bool(output.is_ready)
        if missing_fields or invalid_fields:
            is_ready = False

        if not missing_fields and not invalid_fields and not is_ready and fallback.is_ready:
            # Protect against malformed model output that flips readiness without reasons.
            return fallback

        return OrchestratorValidationOutput(
            is_ready=is_ready,
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
            questions=questions,
            notes=notes,
            normalized_profile=normalized,
        )

    def _fallback_validation(
        self,
        required_fields: list[str],
        profile_payload: dict,
    ) -> OrchestratorValidationOutput:
        missing_fields = [
            field
            for field in required_fields
            if not self._has_value(profile_payload.get(field))
        ]
        return OrchestratorValidationOutput(
            is_ready=not missing_fields,
            missing_fields=missing_fields,
            invalid_fields=[],
            questions=[],
            notes=["fallback_validation"],
            normalized_profile={},
        )

    def _apply_normalized_profile(self, state: SessionState, normalized: dict[str, object]) -> None:
        profile = state.memory_snapshot.user_profile
        for field in ("scenario", "primary_scene", "brand"):
            value = normalized.get(field)
            if isinstance(value, str) and value.strip():
                setattr(profile, field, value.strip())

        for field in ("preferences", "exclusions"):
            value = normalized.get(field)
            if not isinstance(value, list):
                continue
            merged = [str(item).strip() for item in value if str(item).strip()]
            if merged:
                setattr(profile, field, list(dict.fromkeys(merged)))

        state.memory_snapshot.user_profile = profile

    def _required_profile_fields(self) -> list[str]:
        required = [str(field).strip() for field in self._config.app.clarification_required_fields if str(field).strip()]
        if required:
            return required
        return ["scenario", "primary_scene", "brand"]

    def _build_profile_error_message(
        self,
        missing_fields: list[str],
        invalid_fields: list[str],
        questions: list[str],
        required_fields: list[str],
    ) -> str:
        label_map = {
            "scenario": "場合",
            "primary_scene": "主要場景",
            "brand": "品牌",
            "preferences": "偏好",
            "exclusions": "避免項目",
        }

        def labels(fields: list[str]) -> str:
            tokens = [label_map.get(field, field) for field in fields]
            return "、".join(tokens)

        _ = (missing_fields, invalid_fields, questions, required_fields, labels)
        return "欄位內容不夠明確：場合、主要場景。請改成可直接理解的描述。"

    def _has_value(self, value: object) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(str(item).strip() for item in value)
        return value is not None

    def _merge_profile(self, profile: UserProfileMemory, turn: TurnRequest) -> UserProfileMemory:
        structured = turn.structured_updates.model_dump(mode="json")
        parsed = parse_profile_updates(turn.message)

        for field in ("scenario", "primary_scene", "brand"):
            value = structured.get(field) or parsed.get(field)
            if isinstance(value, str) and value.strip():
                setattr(profile, field, value.strip())

        ui_brand = (turn.ui_brand_selection or "").strip().upper()
        if ui_brand and ui_brand != "OTHER":
            profile.brand = ui_brand

        preferences = self._merge_list(structured.get("preferences") or parsed.get("preferences"))
        if preferences:
            profile.preferences = preferences

        exclusions = self._merge_list(structured.get("exclusions") or parsed.get("exclusions"))
        if exclusions:
            profile.exclusions = exclusions

        return profile

    def _merge_list(self, values: Iterable[str] | None) -> list[str]:
        merged: list[str] = []
        for item in values or []:
            token = str(item).strip()
            if token and token not in merged:
                merged.append(token)
        return merged

    def _build_feedback(self, state: SessionState, turn: TurnRequest) -> UserFeedbackMemory | None:
        payload = turn.feedback.model_dump(mode="json")
        hinted_outfit_id, hinted_categories = extract_feedback_hint(
            turn.message,
            state.current_design_context.outfits,
        )
        if hinted_outfit_id and not payload.get("selected_outfit_id"):
            payload["selected_outfit_id"] = hinted_outfit_id
        if hinted_outfit_id and not payload.get("preserve_outfit_id"):
            payload["preserve_outfit_id"] = hinted_outfit_id

        merged_categories = [*list(payload.get("replace_categories") or []), *hinted_categories]
        payload["replace_categories"] = list(
            dict.fromkeys([str(item).strip() for item in merged_categories if str(item).strip()])
        )

        selected_outfit_id = payload.get("selected_outfit_id") or payload.get("preserve_outfit_id")
        meaningful = any(
            [
                payload.get("action"),
                selected_outfit_id,
                payload.get("reason"),
                payload.get("replace_categories"),
            ]
        )
        if not meaningful:
            return None

        snapshot = None
        if selected_outfit_id:
            match = next(
                (
                    outfit
                    for outfit in state.current_design_context.outfits
                    if outfit.outfit_id == selected_outfit_id
                ),
                None,
            )
            if match is not None:
                snapshot = match.model_dump(mode="json")

        return UserFeedbackMemory(
            selected_outfit_id=selected_outfit_id,
            reason=payload.get("reason"),
            preserve_outfit_id=payload.get("preserve_outfit_id"),
            replace_categories=list(payload.get("replace_categories") or []),
            outfit_snapshot=snapshot,
        )

    def _resolve_intent(self, turn: TurnRequest, feedback: UserFeedbackMemory | None) -> tuple[str, str | None]:
        action = (turn.feedback.action or "").strip().lower()
        selected = None
        if feedback is not None:
            selected = feedback.selected_outfit_id or feedback.preserve_outfit_id

        if action == "modify":
            return "modify", selected
        if action == "search":
            return "search", selected
        if feedback is None:
            return "design", None
        if feedback.preserve_outfit_id or feedback.replace_categories:
            return "modify", selected
        if selected:
            return "search", selected
        return "design", None

    def _is_valid_outfit_selection(self, state: SessionState, outfit_id: str) -> bool:
        return any(outfit.outfit_id == outfit_id for outfit in state.current_design_context.outfits)

    def _build_redesign_suggestions(self, feedback: UserFeedbackMemory | None) -> list[str]:
        if feedback is None:
            return ["請依照使用者意見重新調整此套搭配。"]
        suggestions = [f"替換 {category}" for category in feedback.replace_categories if str(category).strip()]
        if feedback.reason:
            suggestions.append(f"使用者回饋：{feedback.reason}")
        return suggestions or ["請依照使用者意見重新調整此套搭配。"]
