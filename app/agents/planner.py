from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.config import ConfigBundle, RuntimeSettings
from app.models import OutfitItemSpec, SearchBudgets, SearchPlan, SearchPlanItem, SearchPlanStep, SessionState
from app.services.validation import invoke_with_schema_retry

BRAND_HOMEPAGES: dict[str, str] = {
    "UNIQLO": "https://www.uniqlo.com/tw/zh_TW/",
    "GU": "https://www.gu-global.com/tw/zh_TW/",
}

SEARCH_TARGET_CATEGORY_ALIASES: dict[str, str] = {
    "外套": "外套",
    "jacket": "外套",
    "coat": "外套",
    "outerwear": "外套",
    "上身": "上身",
    "上衣": "上身",
    "top": "上身",
    "shirt": "上身",
    "tee": "上身",
    "t-shirt": "上身",
    "tshirt": "上身",
    "下身": "下身",
    "褲": "下身",
    "bottom": "下身",
    "pants": "下身",
    "trousers": "下身",
}

FIXED_WORKFLOW_ACTIONS: list[str] = [
    "hover_menu",
    "click_category",
    "click_subcategory",
    "scroll_to_filters",
    "select_color_filter",
    "select_best_product",
    "select_product_color",
    "capture_left_image_screenshot",
]


class PlannerOutput(BaseModel):
    global_steps: list = Field(default_factory=list)
    per_item_steps: list = Field(default_factory=list)
    planner_notes: list = Field(default_factory=list)


class PlannerAgent:
    def __init__(self, config: ConfigBundle, settings: RuntimeSettings) -> None:
        self._config = config
        self._settings = settings

    async def run(self, state: SessionState) -> SearchPlan:
        selected_outfit_id = state.selected_outfit_id or ""
        profile = state.memory_snapshot.user_profile
        brand_homepage_url = self._brand_homepage_url(profile.brand)
        prompt_payload = {
            "selected_outfit_id": selected_outfit_id,
            "user_profile": profile.model_dump(mode="json"),
            "brand_homepage_url": brand_homepage_url,
            "outfits": [item.model_dump(mode="json") for item in state.current_design_context.outfits],
            "review_report": (
                state.current_design_context.review_report.model_dump(mode="json")
                if state.current_design_context.review_report
                else {}
            ),
        }
        output = await invoke_with_schema_retry(
            system_prompt=self._config.prompts.get("planner_system", ""),
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            output_model=PlannerOutput,
            model_name=self._config.models.get("planner", self._settings.groq_planner_model),
            groq_api_key=self._settings.groq_api_key,
            fallback_factory=lambda: self._fallback(state),
            max_retries=2,
        )

        budgets = SearchBudgets(
            max_steps_per_item=0,
            max_retries_per_item=0,
            max_screenshots_per_item=0,
            max_eval_images_per_item=0,
            top_k_eval_images=0,
        )

        per_item_steps = self._coerce_plan_items(output.per_item_steps, selected_outfit_id)
        if not per_item_steps:
            fallback = self._fallback(state)
            per_item_steps = self._coerce_plan_items(fallback.per_item_steps, selected_outfit_id)
            global_steps = fallback.global_steps
            planner_notes = fallback.planner_notes
        else:
            global_steps = [str(step).strip() for step in output.global_steps if str(step).strip()]
            planner_notes = [str(note).strip() for note in output.planner_notes if str(note).strip()]
        global_steps = self._ensure_brand_homepage_step(global_steps, brand_homepage_url)
        global_steps = self._ensure_men_global_step(global_steps)
        per_item_steps = self._enforce_fixed_workflow(per_item_steps, state)

        return SearchPlan(
            global_steps=global_steps,
            per_item_steps=per_item_steps,
            budgets=budgets,
            planner_notes=planner_notes,
        )

    def _fallback(self, state: SessionState) -> PlannerOutput:
        brand_homepage_url = self._brand_homepage_url(state.memory_snapshot.user_profile.brand)
        global_steps = [
            "Keep cursor route deterministic from MEN menu to category and subcategory.",
            "Wait until subcategory heading and filter panel are visible before applying filters.",
            "After selecting a product, set color on product page, capture left image area, and return normalized crop_box.",
            "If an exact category/subcategory option is unavailable, choose the nearest available option by intent and visual similarity.",
            "After obtaining left-image screenshot, product title, and product URL for an item, persist result and move to next item immediately.",
        ]
        global_steps = self._ensure_brand_homepage_step(global_steps, brand_homepage_url)
        global_steps = self._ensure_men_global_step(global_steps)

        per_item_steps: list[dict] = []
        for outfit in self._target_outfits(state):
            for item in self._searchable_items(outfit.items):
                subcategory = self._subcategory_hint(item.category, item.visual_effect)
                steps = [
                    {
                        "action": "hover_menu",
                        "instruction": self._default_instruction(
                            action="hover_menu",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "click_category",
                        "instruction": self._default_instruction(
                            action="click_category",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "click_subcategory",
                        "instruction": self._default_instruction(
                            action="click_subcategory",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "scroll_to_filters",
                        "instruction": self._default_instruction(
                            action="scroll_to_filters",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "select_color_filter",
                        "instruction": self._default_instruction(
                            action="select_color_filter",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "select_best_product",
                        "instruction": self._default_instruction(
                            action="select_best_product",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "select_product_color",
                        "instruction": self._default_instruction(
                            action="select_product_color",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                    {
                        "action": "capture_left_image_screenshot",
                        "instruction": self._default_instruction(
                            action="capture_left_image_screenshot",
                            category=item.category,
                            color=item.color,
                            subcategory=subcategory,
                            visual_effect=item.visual_effect,
                        ),
                    },
                ]

                per_item_steps.append(
                    {
                        "outfit_id": outfit.outfit_id,
                        "item_id": item.item_id,
                        "item_category": item.category,
                        "filters": [f"color={item.color}", f"visual_effect_hint={item.visual_effect}"],
                        "steps": steps,
                    }
                )

        return PlannerOutput(
            global_steps=global_steps,
            per_item_steps=per_item_steps,
            planner_notes=["Plan generated for selected outfit only."],
        )

    def _brand_homepage_url(self, brand: str | None) -> str:
        token = str(brand or "").strip().upper()
        return BRAND_HOMEPAGES.get(token, "")

    def _ensure_brand_homepage_step(self, global_steps: list[str], homepage_url: str) -> list[str]:
        steps = [str(step).strip() for step in global_steps if str(step).strip()]
        if not homepage_url:
            return steps
        if any(homepage_url in step for step in steps):
            return steps
        return [f"Open brand homepage first: {homepage_url}.", *steps]

    def _ensure_men_global_step(self, global_steps: list[str]) -> list[str]:
        steps = [str(step).strip() for step in global_steps if str(step).strip()]
        men_step = "Use MEN menu only for navigation. Do not switch to WOMEN, KIDS, or other tabs."
        if any("MEN menu only" in step for step in steps):
            return steps
        if steps and steps[0].startswith("Open brand homepage first:"):
            return [steps[0], men_step, *steps[1:]]
        return [men_step, *steps]

    def _enforce_fixed_workflow(self, plan_items: list[SearchPlanItem], state: SessionState) -> list[SearchPlanItem]:
        item_specs_by_id = self._search_item_specs_by_id(state)
        item_specs_by_category = self._search_item_specs_by_category(state)

        normalized: list[SearchPlanItem] = []
        for plan_item in plan_items:
            spec = item_specs_by_id.get(plan_item.item_id)
            normalized_category = self._normalize_search_category(plan_item.item_category)
            if spec is None and normalized_category:
                spec = item_specs_by_category.get(normalized_category)
            if spec is None:
                continue

            category = spec.category
            color = (spec.color if spec else self._extract_color_from_filters(plan_item.filters)) or "目標顏色"
            visual_effect = (spec.visual_effect if spec else "").strip()
            subcategory = self._subcategory_hint(category, visual_effect)
            existing = {
                str(step.action).strip(): str(step.instruction).strip()
                for step in plan_item.steps
                if str(step.action).strip() and str(step.instruction).strip()
            }

            force_default_actions = {
                "hover_menu",
                "click_category",
                "click_subcategory",
                "select_best_product",
                "capture_left_image_screenshot",
            }
            fixed_steps: list[SearchPlanStep] = []
            for action in FIXED_WORKFLOW_ACTIONS:
                fallback_instruction = self._default_instruction(
                    action=action,
                    category=category,
                    color=color,
                    subcategory=subcategory,
                    visual_effect=visual_effect,
                )
                instruction = existing.get(action, fallback_instruction)
                if action in force_default_actions:
                    instruction = fallback_instruction
                fixed_steps.append(SearchPlanStep(action=action, instruction=instruction))

            filters = [str(item).strip() for item in plan_item.filters if str(item).strip()]
            if color and not any(str(value).lower().startswith("color=") for value in filters):
                filters.append(f"color={color}")

            normalized.append(
                SearchPlanItem(
                    outfit_id=plan_item.outfit_id,
                    item_id=spec.item_id,
                    item_category=category,
                    query="",
                    filters=filters,
                    steps=fixed_steps,
                )
            )

        return normalized

    def _extract_color_from_filters(self, filters: list[str]) -> str:
        for raw in filters:
            token = str(raw).strip()
            if not token:
                continue
            if token.lower().startswith("color="):
                return token.split("=", 1)[1].strip()
        return ""

    def _default_instruction(
        self,
        *,
        action: str,
        category: str,
        color: str,
        subcategory: str,
        visual_effect: str,
    ) -> str:
        if action == "hover_menu":
            return "Click MEN at the top directly to open the menu. Do not use hover interactions."
        if action == "click_category":
            return (
                f"Click the closest matching category for target item: {category}. "
                "If exact match is unavailable, choose the nearest category by function and silhouette."
            )
        if action == "click_subcategory":
            return (
                f"Click the closest matching subcategory, for example: {subcategory}. "
                "If exact match is unavailable, choose nearest by visual similarity and proceed without extra tool probing."
            )
        if action == "scroll_to_filters":
            return "Scroll directly to make both subcategory title and filter section visible. Avoid exploratory long scrolling."
        if action == "select_color_filter":
            return f"Apply color filter matching: {color}."
        if action == "select_best_product":
            return (
                f"From filtered products, choose the best match for category and visual effect ({visual_effect}). "
                "Prefer visible products first; if needed, scroll at most 2 pages, then choose nearest match. "
                "Do not select the first item by default."
            )
        if action == "select_product_color":
            return f"On product page right-side options, select color: {color}."
        if action == "capture_left_image_screenshot":
            return (
                "Capture left image gallery screenshot, record product title/product URL, and include "
                "normalized crop_box(x,y,width,height) for the main product image area. "
                "Persist the item result, then stop this item search immediately."
            )
        return action

    def _normalize_search_category(self, category: str) -> str:
        token = str(category or "").strip().lower()
        if not token:
            return ""
        return SEARCH_TARGET_CATEGORY_ALIASES.get(token, "")

    def _is_search_target_category(self, category: str) -> bool:
        return bool(self._normalize_search_category(category))

    def _searchable_items(self, items: list) -> list:
        return [item for item in items if self._is_search_target_category(getattr(item, "category", ""))]

    def _search_item_specs_by_id(self, state: SessionState) -> dict[str, OutfitItemSpec]:
        specs: dict[str, OutfitItemSpec] = {}
        for outfit in self._target_outfits(state):
            for item in self._searchable_items(outfit.items):
                specs[item.item_id] = item
        return specs

    def _search_item_specs_by_category(self, state: SessionState) -> dict[str, OutfitItemSpec]:
        specs: dict[str, OutfitItemSpec] = {}
        for outfit in self._target_outfits(state):
            for item in self._searchable_items(outfit.items):
                normalized = self._normalize_search_category(item.category)
                if normalized and normalized not in specs:
                    specs[normalized] = item
        return specs

    def _coerce_plan_items(self, raw_items, selected_outfit_id: str) -> list[SearchPlanItem]:
        if not isinstance(raw_items, list):
            return []

        plan_items: list[SearchPlanItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            steps_raw = raw.get("steps")
            if not isinstance(steps_raw, list):
                continue

            outfit_id = str(raw.get("outfit_id") or "").strip()
            if selected_outfit_id and outfit_id != selected_outfit_id:
                continue

            steps: list[SearchPlanStep] = []
            for step in steps_raw:
                if not isinstance(step, dict):
                    continue
                action = str(step.get("action") or "").strip()
                instruction = str(step.get("instruction") or "").strip()
                if not action or not instruction:
                    continue
                try:
                    steps.append(SearchPlanStep(action=action, instruction=instruction))
                except Exception:
                    continue

            if not steps:
                continue

            filters = [str(item).strip() for item in raw.get("filters", []) if str(item).strip()]
            try:
                plan_items.append(
                    SearchPlanItem(
                        outfit_id=outfit_id,
                        item_id=str(raw.get("item_id") or "").strip(),
                        item_category=str(raw.get("item_category") or "").strip(),
                        query="",
                        filters=filters,
                        steps=steps,
                    )
                )
            except Exception:
                continue

        return [item for item in plan_items if item.item_id and item.item_category]

    def _target_outfits(self, state: SessionState):
        if state.selected_outfit_id:
            selected = next(
                (
                    outfit
                    for outfit in state.current_design_context.outfits
                    if outfit.outfit_id == state.selected_outfit_id
                ),
                None,
            )
            if selected is not None:
                return [selected]
        return []

    def _subcategory_hint(self, category: str, visual_effect: str = "") -> str:
        effect = str(visual_effect or "").lower()
        if category == "外套":
            if any(token in effect for token in ("防風", "機能", "光澤")):
                return "布勞森/連帽外套"
            return "西裝外套"
        if category == "上身":
            if any(token in effect for token in ("乾淨", "俐落", "剪裁")):
                return "T恤"
            return "長袖上衣"
        if category == "下身":
            if any(token in effect for token in ("簡約", "俐落", "剪裁", "西裝")):
                return "Miracle Air 西裝褲／Smart九分褲"
            return "長褲"

        mapping = {
            "鞋子": "休閒鞋",
            "配件": "皮帶",
        }
        return mapping.get(category, category)
