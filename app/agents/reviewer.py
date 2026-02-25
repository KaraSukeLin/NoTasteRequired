from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.config import ConfigBundle, RuntimeSettings
from app.models import OutfitScore, RedesignRequest, ReviewReport, SessionState
from app.services.validation import invoke_with_schema_retry


class ReviewerOutput(BaseModel):
    approved: bool | None = None
    per_outfit_scores: list = Field(default_factory=list)
    similarity_notes: list = Field(default_factory=list)
    redesign_requests: list = Field(default_factory=list)
    summary: str = ""


class ReviewerAgent:
    def __init__(self, config: ConfigBundle, settings: RuntimeSettings) -> None:
        self._config = config
        self._settings = settings

    async def run(self, state: SessionState) -> ReviewReport:
        feedback_mode = self._is_feedback_review_mode(state)
        prompt_payload = {
            "review_mode": "feedback_to_suggestions" if feedback_mode else "quality_gate",
            "turn_intent": state.turn_intent,
            "user_profile": state.memory_snapshot.user_profile.model_dump(mode="json"),
            "outfits": [item.model_dump(mode="json", exclude_none=True) for item in state.current_design_context.outfits],
            "pending_redesign_requests": [
                item.model_dump(mode="json") for item in state.current_design_context.pending_redesign_requests
            ],
        }
        output = await invoke_with_schema_retry(
            system_prompt=self._config.prompts.get("reviewer_system", ""),
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            output_model=ReviewerOutput,
            model_name=self._config.models.get("reviewer", self._settings.groq_reviewer_model),
            groq_api_key=self._settings.groq_api_key,
            fallback_factory=lambda: self._fallback(state),
            max_retries=2,
        )

        report = self._coerce_report(state, output, feedback_mode=feedback_mode)
        state.current_design_context.review_report = report
        return report

    def _coerce_report(self, state: SessionState, output: ReviewerOutput, *, feedback_mode: bool) -> ReviewReport:
        scores: list[OutfitScore] = []
        for idx, raw in enumerate(output.per_outfit_scores if isinstance(output.per_outfit_scores, list) else []):
            if not isinstance(raw, dict):
                continue
            try:
                scores.append(
                    OutfitScore(
                        outfit_id=str(raw.get("outfit_id") or self._fallback_outfit_id(state, idx)),
                        style_coherence=self._to_score(raw.get("style_coherence"), default=7),
                        color_harmony=self._to_score(raw.get("color_harmony"), default=7),
                        scenario_fit=self._to_score(raw.get("scenario_fit"), default=7),
                        scene_fit=self._to_score(raw.get("scene_fit"), default=7),
                        preference_fit=self._to_score(raw.get("preference_fit"), default=7),
                        overall=self._to_score(raw.get("overall"), default=7),
                        notes=[str(item) for item in raw.get("notes", []) if str(item).strip()],
                    )
                )
            except Exception:
                continue

        redesign_requests = self._coerce_redesign_requests(output.redesign_requests)
        if feedback_mode and not redesign_requests:
            redesign_requests = self._fallback_feedback_requests(state)
        if not feedback_mode and not redesign_requests:
            redesign_requests = self._fallback_quality_requests_from_scores(scores)

        approved = bool(output.approved)
        if redesign_requests:
            approved = False
        if feedback_mode:
            approved = False

        summary = str(output.summary or "").strip() or "Review completed."
        return ReviewReport(
            approved=approved,
            per_outfit_scores=scores,
            similarity_notes=[str(item) for item in output.similarity_notes if str(item).strip()]
            if isinstance(output.similarity_notes, list)
            else [],
            redesign_requests=redesign_requests,
            summary=summary,
        )

    def _fallback_quality_requests_from_scores(self, scores: list[OutfitScore]) -> list[RedesignRequest]:
        requests: list[RedesignRequest] = []
        for score in scores:
            if score.overall >= 7:
                continue
            requests.append(
                RedesignRequest(
                    outfit_id=score.outfit_id,
                    reason=f"Overall score below 7 ({score.overall}).",
                    suggestions=[
                        "Increase scenario and scene fit",
                        "Improve color harmony and silhouette contrast",
                        "Strengthen preference alignment while keeping diversity",
                    ],
                )
            )
        return requests

    def _fallback(self, state: SessionState) -> ReviewerOutput:
        if self._is_feedback_review_mode(state):
            return ReviewerOutput(
                approved=False,
                per_outfit_scores=[],
                similarity_notes=[],
                redesign_requests=self._fallback_feedback_requests(state),
                summary="Converted user feedback into redesign requests for Designer.",
            )

        profile = state.memory_snapshot.user_profile
        outfits = state.current_design_context.outfits

        scores: list[OutfitScore] = []
        redesign: list[RedesignRequest] = []
        similarity_notes: list[str] = []

        for outfit in outfits:
            notes: list[str] = []
            style_score = 8
            color_score = self._color_score(outfit)
            scenario_score = 8 if profile.scenario else 6
            scene_score = 8 if profile.primary_scene else 6
            preference_score = self._preference_score(outfit, profile.preferences)

            excluded_hits = self._match_exclusions(outfit, profile.exclusions)
            if excluded_hits:
                preference_score = max(3, preference_score - 4)
                notes.append(f"Excluded terms matched: {', '.join(excluded_hits)}")

            overall = round((style_score + color_score + scenario_score + scene_score + preference_score) / 5)
            if overall < 7:
                redesign.append(
                    RedesignRequest(
                        outfit_id=outfit.outfit_id,
                        reason="Overall score below 7.",
                        suggestions=[
                            "Improve color harmony",
                            "Increase scenario fit",
                            "Reduce conflicts with preferences",
                        ],
                    )
                )

            scores.append(
                OutfitScore(
                    outfit_id=outfit.outfit_id,
                    style_coherence=style_score,
                    color_harmony=color_score,
                    scenario_fit=scenario_score,
                    scene_fit=scene_score,
                    preference_fit=preference_score,
                    overall=overall,
                    notes=notes,
                )
            )

        if self._too_similar(outfits):
            similarity_notes.append("Three outfits are too similar; increase visual distance.")
            if outfits:
                redesign.append(
                    RedesignRequest(
                        outfit_id=outfits[-1].outfit_id,
                        reason="Similarity too high across options.",
                        suggestions=["Change outer silhouette", "Adjust color direction", "Swap shoes or accessory"],
                    )
                )

        approved = len(redesign) == 0
        summary = "Review approved." if approved else "Redesign is required."
        return ReviewerOutput(
            approved=approved,
            per_outfit_scores=[item.model_dump(mode="json") for item in scores],
            similarity_notes=similarity_notes,
            redesign_requests=[item.model_dump(mode="json") for item in redesign],
            summary=summary,
        )

    def _coerce_redesign_requests(self, raw_requests) -> list[RedesignRequest]:
        if not isinstance(raw_requests, list):
            return []

        requests: list[RedesignRequest] = []
        seen: set[str] = set()
        for raw in raw_requests:
            if not isinstance(raw, dict):
                continue
            outfit_id = str(raw.get("outfit_id") or "").strip()
            if not outfit_id or outfit_id in seen:
                continue
            seen.add(outfit_id)
            reason = str(raw.get("reason") or "Need adjustment").strip() or "Need adjustment"
            suggestions = [str(item).strip() for item in raw.get("suggestions", []) if str(item).strip()]
            if not suggestions:
                suggestions = ["Adjust color and silhouette based on user intent"]
            requests.append(RedesignRequest(outfit_id=outfit_id, reason=reason, suggestions=suggestions))
        return requests

    def _fallback_feedback_requests(self, state: SessionState) -> list[RedesignRequest]:
        base_requests = state.current_design_context.pending_redesign_requests
        if not base_requests and state.selected_outfit_id:
            return [
                RedesignRequest(
                    outfit_id=state.selected_outfit_id,
                    reason="User requested modifications for selected outfit.",
                    suggestions=["Update target categories", "Preserve overall direction", "Increase fit with feedback"],
                )
            ]

        refined: list[RedesignRequest] = []
        for req in base_requests:
            suggestions = list(req.suggestions) if req.suggestions else []
            if not suggestions:
                suggestions = ["Update target categories", "Preserve overall direction", "Increase fit with feedback"]
            refined.append(RedesignRequest(outfit_id=req.outfit_id, reason=req.reason, suggestions=suggestions))
        return refined

    def _is_feedback_review_mode(self, state: SessionState) -> bool:
        return (
            state.turn_intent == "modify"
            and bool(state.current_design_context.pending_redesign_requests)
            and state.retry_counters.redesign == 0
        )

    def _fallback_outfit_id(self, state: SessionState, idx: int) -> str:
        if idx < len(state.current_design_context.outfits):
            return state.current_design_context.outfits[idx].outfit_id
        return ""

    def _to_score(self, raw, *, default: int) -> int:
        try:
            value = int(float(raw))
        except Exception:
            value = default
        return max(0, min(10, value))

    def _color_score(self, outfit) -> int:
        colors = [item.color for item in outfit.items]
        unique_count = len(set(colors))
        if unique_count >= 4:
            return 7
        if unique_count == 3:
            return 8
        if unique_count == 2:
            return 7
        return 6

    def _preference_score(self, outfit, preferences: list[str]) -> int:
        if not preferences:
            return 7

        text = " ".join(
            [
                outfit.title,
                outfit.style,
                outfit.rationale,
                *[f"{item.category} {item.color} {item.visual_effect}" for item in outfit.items],
            ]
        ).lower()
        hit_count = sum(1 for pref in preferences if pref.lower() in text)
        if hit_count >= 2:
            return 9
        if hit_count == 1:
            return 8
        return 6

    def _match_exclusions(self, outfit, exclusions: list[str]) -> list[str]:
        if not exclusions:
            return []

        text = " ".join(
            [
                outfit.title,
                outfit.rationale,
                *[f"{item.category} {item.color} {item.visual_effect}" for item in outfit.items],
            ]
        ).lower()
        hits = [token for token in exclusions if token.lower() in text]
        return list(dict.fromkeys(hits))

    def _too_similar(self, outfits) -> bool:
        if len(outfits) < 3:
            return False
        signatures = []
        for outfit in outfits:
            colors = ",".join(sorted(item.color for item in outfit.items))
            effects = ",".join(sorted(item.visual_effect for item in outfit.items))
            signatures.append(f"{colors}|{effects}")
        return len(set(signatures)) <= 1
