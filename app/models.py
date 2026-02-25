from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl, field_validator


PhaseName = Literal[
    "collect",
    "design",
    "review",
    "plan",
    "browse",
    "present",
    "done",
]

TurnStatus = Literal[
    "idle",
    "run_started",
    "await_user_choice",
    "completed",
    "error",
]

NextAgentName = Literal[
    "orchestrator",
    "designer",
    "reviewer",
    "planner",
]


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_design_retries: int = 2


class UserFeedbackMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_outfit_id: str | None = None
    reason: str | None = None
    preserve_outfit_id: str | None = None
    replace_categories: list[str] = Field(default_factory=list)
    outfit_snapshot: dict[str, Any] | None = None


class UserProfileMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str | None = None
    primary_scene: str | None = None
    brand: str | None = None
    preferences: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    latest_feedback: UserFeedbackMemory = Field(default_factory=UserFeedbackMemory)


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_profile: UserProfileMemory = Field(default_factory=UserProfileMemory)


class OutfitItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(default_factory=lambda: str(uuid4()))
    category: str
    color: str
    visual_effect: str = Field(validation_alias=AliasChoices("visual_effect", "material"))
    size_reco: str | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, value: Any) -> Any:
        raw = str(value or "").strip()
        if not raw:
            return value

        normalized = raw.lower()
        if normalized in {"外套", "jacket", "coat", "outerwear"}:
            return "外套"
        if normalized in {"上身", "上衣", "shirt", "top", "tee", "t-shirt", "tshirt"}:
            return "上身"
        if normalized in {"下身", "褲", "bottom", "pants", "trousers"}:
            return "下身"
        if normalized in {"鞋子", "鞋", "shoe", "shoes", "sneakers"}:
            return "鞋子"
        if normalized in {"配件", "accessory", "accessories"}:
            return "配件"
        return value


class OutfitCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    style: str
    rationale: str
    items: list[OutfitItemSpec]


class RedesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str
    reason: str
    suggestions: list[str] = Field(default_factory=list)


class OutfitScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str
    style_coherence: int
    color_harmony: int
    scenario_fit: int
    scene_fit: int
    preference_fit: int
    overall: int
    notes: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    per_outfit_scores: list[OutfitScore] = Field(default_factory=list)
    similarity_notes: list[str] = Field(default_factory=list)
    redesign_requests: list[RedesignRequest] = Field(default_factory=list)
    summary: str = ""


class CurrentDesignContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfits: list[OutfitCandidate] = Field(default_factory=list)
    review_report: ReviewReport | None = None
    pending_redesign_requests: list[RedesignRequest] = Field(default_factory=list)


class SearchPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "hover_menu",
        "click_category",
        "click_subcategory",
        "scroll_to_filters",
        "select_color_filter",
        "select_best_product",
        "select_product_color",
        "capture_left_image_screenshot",
        "search",
        "filter",
        "open_product_page",
        "capture_product_screenshot",
    ]
    instruction: str


class SearchPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str = ""
    item_id: str
    item_category: str
    query: str = ""
    filters: list[str] = Field(default_factory=list)
    steps: list[SearchPlanStep] = Field(default_factory=list)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_steps(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        if not value:
            return value

        if all(isinstance(item, str) for item in value):
            actions = ["search", "filter", "open_product_page", "capture_product_screenshot"]
            coerced: list[dict[str, str]] = []
            for idx, item in enumerate(value):
                action = actions[idx] if idx < len(actions) else "capture_product_screenshot"
                coerced.append({"action": action, "instruction": item})
            return coerced

        return value


class SearchBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps_per_item: int
    max_retries_per_item: int
    max_screenshots_per_item: int
    max_eval_images_per_item: int
    top_k_eval_images: int


class SearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_steps: list[str] = Field(default_factory=list)
    per_item_steps: list[SearchPlanItem] = Field(default_factory=list)
    budgets: SearchBudgets
    planner_notes: list[str] = Field(default_factory=list)


class ExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: Literal["PLAN", "ACT", "EVALUATE", "REPLAN", "DONE"]
    detail: str
    status: Literal["ok", "warning", "error"]
    artifact_refs: list[str] = Field(default_factory=list)
    budget_snapshot: dict[str, int] = Field(default_factory=dict)


class ExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    steps: list[ExecutionStep]
    issues: list[str] = Field(default_factory=list)


class ProductCropBox(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float
    height: float


class FoundProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    title: str
    url: HttpUrl | str
    currency: str = "TWD"
    category: str
    color: str
    material: str
    screenshot_base64: str | None = None
    crop_box: ProductCropBox | None = None


class BrowserItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    success: bool
    failed_step: str | None = None
    screenshot_ref: str | None = None
    product: FoundProduct | None = None
    message: str = ""


class RetryCounters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redesign: int = 0


class PhaseTraceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: PhaseName
    status: Literal["started", "completed", "failed"]
    input_summary: str
    output_summary: str
    latency_ms: int
    error_type: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OutfitCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outfit_id: str
    title: str
    style: str
    rationale: str
    items: list[OutfitItemSpec] = Field(default_factory=list)
    products: list[FoundProduct] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    current_phase: PhaseName = "collect"
    constraints: Constraints = Field(default_factory=Constraints)
    memory_snapshot: MemorySnapshot = Field(default_factory=MemorySnapshot)
    current_design_context: CurrentDesignContext = Field(default_factory=CurrentDesignContext)
    turn_intent: Literal["design", "modify", "search"] = "design"
    selected_outfit_id: str | None = None

    search_plan: SearchPlan | None = None
    found_products: list[FoundProduct] = Field(default_factory=list)
    retry_counters: RetryCounters = Field(default_factory=RetryCounters)

    assistant_message: str = ""
    pending_question: str | None = None

    status: TurnStatus = "idle"
    next_agent: NextAgentName | None = "orchestrator"
    active_run_id: str | None = None

    outfit_cards: list[OutfitCard] = Field(default_factory=list)

    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TurnStructuredUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str | None = None
    primary_scene: str | None = None
    brand: str | None = None
    preferences: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)


class TurnFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "modify"] | None = None
    selected_outfit_id: str | None = None
    reason: str | None = None
    preserve_outfit_id: str | None = None
    replace_categories: list[str] = Field(default_factory=list)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = ""
    ui_brand_selection: Literal["UNIQLO", "GU", "OTHER"] | None = None
    structured_updates: TurnStructuredUpdates = Field(default_factory=TurnStructuredUpdates)
    feedback: TurnFeedback = Field(default_factory=TurnFeedback)


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: TurnStatus
    assistant_message: str
    user_profile: dict[str, Any]
    pending_question: str | None = None
    next_agent: NextAgentName | None = None
    run_id: str | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: str
    phase: PhaseName
    status: TurnStatus
    assistant_message: str = ""
    outfit_cards: list[OutfitCard] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["phase_started", "browser_live", "error", "done"]
    data: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    events: list[EventPayload] = Field(default_factory=list)
    done: bool = False
    result: RunResult | None = None
