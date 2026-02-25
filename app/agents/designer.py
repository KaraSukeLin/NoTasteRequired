from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.config import ConfigBundle, RuntimeSettings
from app.models import OutfitCandidate, OutfitItemSpec, RedesignRequest, SessionState
from app.services.validation import invoke_with_schema_retry


class DesignerOutput(BaseModel):
    outfits: list = Field(default_factory=list)


class DesignerAgent:
    def __init__(self, config: ConfigBundle, settings: RuntimeSettings) -> None:
        self._config = config
        self._settings = settings

    async def run(self, state: SessionState) -> list[OutfitCandidate]:
        pending = state.current_design_context.pending_redesign_requests
        previous = [item.model_copy(deep=True) for item in state.current_design_context.outfits]
        prompt_payload = {
            "turn_intent": state.turn_intent,
            "selected_outfit_id": state.selected_outfit_id,
            "user_profile": state.memory_snapshot.user_profile.model_dump(mode="json"),
            "current_outfits": [item.model_dump(mode="json", exclude_none=True) for item in previous],
            "redesign_requests": [item.model_dump(mode="json") for item in pending],
        }

        fallback_outfits = self._fallback_for_state(state, previous, pending)
        output = await invoke_with_schema_retry(
            system_prompt=self._config.prompts.get("designer_system", ""),
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            output_model=DesignerOutput,
            model_name=self._config.models.get("designer", self._settings.groq_designer_model),
            groq_api_key=self._settings.groq_api_key,
            fallback_factory=lambda: DesignerOutput(outfits=[item.model_dump(mode="json") for item in fallback_outfits]),
            max_retries=2,
        )

        outfits = self._coerce_outfits(output.outfits)
        if not outfits:
            outfits = [item.model_copy(deep=True) for item in fallback_outfits]

        while len(outfits) < 3:
            outfits.append(fallback_outfits[len(outfits) % len(fallback_outfits)].model_copy(deep=True))
        outfits = outfits[:3]

        if state.turn_intent == "modify" and pending and previous:
            outfits = self._enforce_targeted_redesign(previous, pending, outfits)

        state.current_design_context.pending_redesign_requests = []
        state.current_design_context.outfits = outfits
        return outfits

    def _coerce_outfits(self, raw_outfits) -> list[OutfitCandidate]:
        if not isinstance(raw_outfits, list):
            return []

        generated: list[OutfitCandidate] = []
        for idx, raw in enumerate(raw_outfits):
            if not isinstance(raw, dict):
                continue

            items = self._coerce_items(raw.get("items"))
            if not items:
                continue

            try:
                payload = {
                    "title": str(raw.get("title") or f"方案 {idx + 1}"),
                    "style": str(raw.get("style") or "都會休閒"),
                    "rationale": str(raw.get("rationale") or "已依需求調整整體輪廓與配色。"),
                    "items": items,
                }
                outfit_id = str(raw.get("outfit_id") or "").strip()
                if outfit_id:
                    payload["outfit_id"] = outfit_id
                outfit = OutfitCandidate(**payload)
            except Exception:
                continue

            generated.append(self._ensure_outfit_structure(outfit))

        return generated

    def _coerce_items(self, raw_items) -> list[OutfitItemSpec]:
        if not isinstance(raw_items, list):
            return []

        items: list[OutfitItemSpec] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue

            category = str(raw.get("category") or "").strip()
            color = str(raw.get("color") or "").strip()
            visual_effect = str(raw.get("visual_effect") or raw.get("material") or "").strip()
            if not category or not color:
                continue
            if not visual_effect:
                visual_effect = "輪廓感"

            payload = {
                "category": category,
                "color": color,
                "visual_effect": visual_effect,
            }
            item_id = str(raw.get("item_id") or "").strip()
            if item_id:
                payload["item_id"] = item_id

            try:
                items.append(OutfitItemSpec(**payload))
            except Exception:
                continue
        return items

    def _ensure_outfit_structure(self, outfit: OutfitCandidate) -> OutfitCandidate:
        by_category = {item.category: item for item in outfit.items}
        required = {
            "外套": OutfitItemSpec(category="外套", color="深灰", visual_effect="挺版感"),
            "上身": OutfitItemSpec(category="上身", color="白色", visual_effect="清爽感"),
            "下身": OutfitItemSpec(category="下身", color="黑色", visual_effect="垂墜感"),
        }

        items: list[OutfitItemSpec] = []
        for category, default_item in required.items():
            items.append(by_category.get(category) or default_item)

        accessory = by_category.get("鞋子") or by_category.get("配件")
        if accessory is None:
            accessory = OutfitItemSpec(category="鞋子", color="黑色", visual_effect="俐落感")
        items.append(accessory)

        return OutfitCandidate(
            outfit_id=outfit.outfit_id,
            title=outfit.title,
            style=outfit.style,
            rationale=outfit.rationale,
            items=items,
        )

    def _fallback_outfits(self, state: SessionState) -> list[OutfitCandidate]:
        profile = state.memory_snapshot.user_profile
        scene = profile.primary_scene or "日常室內外移動"

        return [
            OutfitCandidate(
                title="都會俐落款",
                style="都會休閒",
                rationale=f"針對 {scene} 設計，安全好搭、輪廓清楚且上鏡。",
                items=[
                    OutfitItemSpec(category="外套", color="深灰", visual_effect="挺版感"),
                    OutfitItemSpec(category="上身", color="白色", visual_effect="平整感"),
                    OutfitItemSpec(category="下身", color="黑色", visual_effect="垂墜感"),
                    OutfitItemSpec(category="鞋子", color="黑色", visual_effect="霧面感"),
                ],
            ),
            OutfitCandidate(
                title="柔和通勤款",
                style="日系簡約",
                rationale=f"符合 {scene} 的通勤節奏，配色保守穩定且拍照不突兀。",
                items=[
                    OutfitItemSpec(category="外套", color="米色", visual_effect="厚重感"),
                    OutfitItemSpec(category="上身", color="淺藍", visual_effect="清爽感"),
                    OutfitItemSpec(category="下身", color="炭灰", visual_effect="俐落感"),
                    OutfitItemSpec(category="鞋子", color="白色", visual_effect="平滑感"),
                ],
            ),
            OutfitCandidate(
                title="休閒層次款",
                style="城市鬆弛",
                rationale=f"在 {scene} 保留輕鬆感，維持安全色系並強化照片層次。",
                items=[
                    OutfitItemSpec(category="外套", color="卡其", visual_effect="紋理感"),
                    OutfitItemSpec(category="上身", color="淺灰", visual_effect="柔順感"),
                    OutfitItemSpec(category="下身", color="黑色", visual_effect="俐落感"),
                    OutfitItemSpec(category="鞋子", color="深棕", visual_effect="低光澤"),
                ],
            ),
        ]

    def _fallback_for_state(
        self,
        state: SessionState,
        previous: list[OutfitCandidate],
        pending: list[RedesignRequest],
    ) -> list[OutfitCandidate]:
        base = self._fallback_outfits(state)
        if state.turn_intent == "modify" and pending and previous:
            return self._enforce_targeted_redesign(previous, pending, base)
        return base

    def _enforce_targeted_redesign(
        self,
        previous: list[OutfitCandidate],
        requests: list[RedesignRequest],
        generated: list[OutfitCandidate],
    ) -> list[OutfitCandidate]:
        base_outfit, request = self._resolve_redesign_target(previous, requests)
        target_categories = self._infer_replace_categories(base_outfit, request)
        if not target_categories:
            target_categories = [item.category for item in base_outfit.items]

        candidates = [item.model_copy(deep=True) for item in generated]
        fallback_pool = self._fallback_outfits(SessionState(session_id="fallback"))
        while len(candidates) < 3:
            candidates.append(fallback_pool[len(candidates) % len(fallback_pool)].model_copy(deep=True))
        candidates = candidates[:3]

        by_category_base = {item.category: item for item in base_outfit.items}
        result: list[OutfitCandidate] = []

        for idx, source in enumerate(candidates):
            source_by_category = {item.category: item for item in source.items}
            rebuilt_items: list[OutfitItemSpec] = []

            for category, base_item in by_category_base.items():
                if category in target_categories:
                    candidate_item = source_by_category.get(category) or base_item
                    rebuilt_items.append(
                        OutfitItemSpec(
                            category=category,
                            color=candidate_item.color,
                            visual_effect=candidate_item.visual_effect or base_item.visual_effect,
                        )
                    )
                    continue

                rebuilt_items.append(self._clone_item(base_item))

            title = str(source.title or "").strip() or f"{base_outfit.title} 調整方案 {idx + 1}"
            style = str(source.style or "").strip() or base_outfit.style
            rationale = (
                str(source.rationale or "").strip()
                or f"僅針對 {'、'.join(target_categories)} 調整，其餘部件維持原搭配。"
            )

            result.append(
                self._ensure_outfit_structure(
                    OutfitCandidate(
                        title=title,
                        style=style,
                        rationale=rationale,
                        items=rebuilt_items,
                    )
                )
            )

        return result

    def _resolve_redesign_target(
        self,
        previous: list[OutfitCandidate],
        requests: list[RedesignRequest],
    ) -> tuple[OutfitCandidate, RedesignRequest]:
        request_map = {request.outfit_id: request for request in requests}
        for outfit in previous:
            request = request_map.get(outfit.outfit_id)
            if request is not None:
                return outfit, request

        if previous:
            fallback_request = requests[0] if requests else RedesignRequest(
                outfit_id=previous[0].outfit_id,
                reason="使用者希望根據目前搭配調整。",
                suggestions=["更新整體搭配方向"],
            )
            return previous[0], fallback_request

        fallback = self._fallback_outfits(SessionState(session_id="fallback"))[0]
        fallback_request = RedesignRequest(
            outfit_id=fallback.outfit_id,
            reason="使用者希望根據目前搭配調整。",
            suggestions=["更新整體搭配方向"],
        )
        return fallback, fallback_request

    def _infer_replace_categories(self, outfit: OutfitCandidate, request: RedesignRequest) -> list[str]:
        tokens = " ".join([request.reason, *request.suggestions]).lower()
        category_tokens = {
            "外套": ["外套", "jacket", "coat", "outerwear"],
            "上身": ["上身", "上衣", "shirt", "top", "tee", "t-shirt"],
            "下身": ["下身", "褲", "pants", "trousers", "bottom"],
            "鞋子": ["鞋", "鞋子", "shoe", "shoes", "sneaker"],
            "配件": ["配件", "accessory", "accessories"],
        }

        existing_categories = {item.category for item in outfit.items}
        detected: list[str] = []
        for category, aliases in category_tokens.items():
            if category not in existing_categories:
                continue
            if self._contains_any(tokens, aliases):
                detected.append(category)

        if detected:
            return list(dict.fromkeys(detected))
        return []

    def _clone_item(self, item: OutfitItemSpec) -> OutfitItemSpec:
        return OutfitItemSpec(
            category=item.category,
            color=item.color,
            visual_effect=item.visual_effect,
        )

    def _contains_any(self, text: str, tokens: list[str]) -> bool:
        lowered = (text or "").lower()
        return any(token in lowered for token in tokens)
