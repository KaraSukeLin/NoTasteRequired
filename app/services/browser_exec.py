from __future__ import annotations

import ast
import base64
import io
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import quote, quote_plus

from app.config import RuntimeSettings
from app.models import (
    BrowserItemResult,
    ExecutionStep,
    ExecutionTrace,
    FoundProduct,
    OutfitCandidate,
    OutfitItemSpec,
    SearchPlan,
    SearchPlanItem,
)


class BrowserExecutionError(RuntimeError):
    pass


@dataclass
class BrowseInterruptionPacket:
    item_id: str
    interrupt_reason: str
    resume_round: int
    recent_actions: list[str]
    recent_errors: list[str]
    latest_url: str | None
    screenshot_base64: str | None
    screenshot_truncated: bool


@dataclass
class BrowseInterruptionResolution:
    action: Literal["resume", "abort_item", "abort_browse"]
    reason: str
    resume_steps: list[str] = field(default_factory=list)


InterruptionHandler = Callable[[BrowseInterruptionPacket], Awaitable[BrowseInterruptionResolution]]
ProgressEmitter = Callable[[dict[str, Any]], Awaitable[None]]
LIVE_SCREENSHOT_MAX_CHARS = 18000
LIVE_EMIT_MIN_INTERVAL_SECONDS = 0.15
UNBOUNDED_MAX_STEPS = 100000
ALLOWED_FOUND_PRODUCT_KEYS = {
    "item_id",
    "title",
    "url",
    "currency",
    "category",
    "color",
    "material",
    "screenshot_base64",
    "crop_box",
}


def _build_browser_session_kwargs(settings: RuntimeSettings, *, use_cloud_browser: bool) -> dict[str, Any]:
    return {
        "headless": settings.browser_headless,
        "use_cloud": use_cloud_browser,
        "is_local": not use_cloud_browser,
        # Reduce DOM extraction pressure on heavy commerce pages.
        "cross_origin_iframes": settings.browser_cross_origin_iframes,
        "paint_order_filtering": settings.browser_paint_order_filtering,
        "highlight_elements": settings.browser_highlight_elements,
        "dom_highlight_elements": settings.browser_dom_highlight_elements,
        "max_iframes": max(1, int(settings.browser_max_iframes)),
        "max_iframe_depth": max(0, int(settings.browser_max_iframe_depth)),
        "wait_for_network_idle_page_load_time": max(0.0, float(settings.browser_wait_for_network_idle_page_load_time)),
        "wait_between_actions": max(0.0, float(settings.browser_wait_between_actions)),
    }


@dataclass
class BrowserExecutionResult:
    products: list[FoundProduct]
    traces: list[ExecutionTrace]
    item_results: list[BrowserItemResult]
    issues: list[str]


@dataclass
class _ItemRoundResult:
    product: FoundProduct | None
    interrupt_reason: str | None
    recent_actions: list[str]
    recent_errors: list[str]
    latest_url: str | None
    artifact_refs: list[str]
    screenshot_base64: str | None
    message: str = ""


class _ItemWorker(Protocol):
    async def run_round(self, instructions: list[str], *, max_steps: int | None) -> _ItemRoundResult: ...

    def add_followup_steps(self, steps: list[str]) -> None: ...

    def get_live_url(self) -> str | None: ...

    async def close(self) -> None: ...


class _MockItemWorker:
    def __init__(
        self,
        settings: RuntimeSettings,
        spec: OutfitItemSpec,
        item_plan: SearchPlanItem,
    ) -> None:
        self._settings = settings
        self._spec = spec
        self._item_plan = item_plan

    async def run_round(self, instructions: list[str], *, max_steps: int | None) -> _ItemRoundResult:
        query = (self._item_plan.query or "").lower()
        if "fail" in query or "憭望?" in query:
            return _ItemRoundResult(
                product=None,
                interrupt_reason="run_failed",
                recent_actions=["Open category page", "Apply filters", "Try product detail"],
                recent_errors=["Mock worker intentionally failed for testing query."],
                latest_url=f"https://example.com/search?q={quote_plus(self._item_plan.query)}",
                artifact_refs=[f"artifact://{self._spec.item_id}/search_results.jpg"],
                screenshot_base64=None,
                message="mock failure",
            )

        product = FoundProduct(
            item_id=self._spec.item_id,
            title=f"{self._spec.category} - {self._spec.color}",
            url=(
                "https://example.com/product?"
                f"category={quote_plus(self._spec.category)}&color={quote_plus(self._spec.color)}"
            ),
            currency=self._settings.default_currency,
            category=self._spec.category,
            color=self._spec.color,
            material=self._spec.visual_effect,
        )
        return _ItemRoundResult(
            product=product,
            interrupt_reason=None,
            recent_actions=["Open category page", "Open product page", "Capture screenshot"],
            recent_errors=[],
            latest_url=str(product.url),
            artifact_refs=[f"artifact://{self._spec.item_id}/final_verification.jpg"],
            screenshot_base64=None,
            message="ok",
        )

    def add_followup_steps(self, steps: list[str]) -> None:
        return None

    def get_live_url(self) -> str | None:
        return None

    async def close(self) -> None:
        return None


class _BrowserUseItemWorker:
    def __init__(
        self,
        settings: RuntimeSettings,
        spec: OutfitItemSpec,
        item_plan: SearchPlanItem,
        llm=None,
        browser_session=None,
        owns_browser_session: bool | None = None,
    ) -> None:
        self._settings = settings
        self._spec = spec
        self._item_plan = item_plan
        self._llm = llm
        self._browser_session = browser_session
        self._owns_browser_session = True if owns_browser_session is None else bool(owns_browser_session)
        self._agent = None
        self._pending_followups: list[str] = []
        self._progress_emitter: ProgressEmitter | None = None
        self._current_round: int = 0
        self._last_live_emit_at: float = 0.0
        self._cloud_live_url: str | None = None
        self._cloud_live_url_emitted: bool = False

    async def run_round(self, instructions: list[str], *, max_steps: int | None) -> _ItemRoundResult:
        try:
            self._current_round += 1
            await self._ensure_agent(initial_steps=instructions)
            assert self._agent is not None

            if self._pending_followups:
                self._agent.add_new_task(self._format_followup(self._pending_followups))
                self._pending_followups = []

            if max_steps is None or max_steps <= 0:
                history = await self._agent.run(
                    max_steps=UNBOUNDED_MAX_STEPS,
                    on_step_end=self._emit_step_end_progress,
                )
            else:
                history = await self._agent.run(
                    max_steps=max_steps,
                    on_step_end=self._emit_step_end_progress,
                )
            recent_actions = history.agent_steps()[-8:]
            recent_errors = [item for item in history.errors() if item][-5:]
            latest_url = next((url for url in reversed(history.urls()) if url), None)

            screenshot_base64 = next(
                (item for item in reversed(history.screenshots(n_last=1)) if item),
                None,
            )
            screenshot_path = next(
                (item for item in reversed(history.screenshot_paths(n_last=1)) if item),
                None,
            )
            artifact_refs = [screenshot_path] if screenshot_path else []

            if not history.is_done():
                return _ItemRoundResult(
                    product=None,
                    interrupt_reason="max_steps_reached",
                    recent_actions=recent_actions,
                    recent_errors=recent_errors,
                    latest_url=latest_url,
                    artifact_refs=artifact_refs,
                    screenshot_base64=screenshot_base64,
                    message="Agent run reached step limit without completion.",
                )

            final_result_raw = history.final_result()
            product = self._parse_final_product(
                final_result=final_result_raw,
                latest_url=latest_url,
                screenshot_base64=screenshot_base64,
            )
            if product is None:
                preview = str(final_result_raw or "").strip()
                if len(preview) > 280:
                    preview = f"{preview[:280]}..."
                return _ItemRoundResult(
                    product=None,
                    interrupt_reason="invalid_final_result",
                    recent_actions=recent_actions,
                    recent_errors=[
                        *recent_errors,
                        "Final result is missing or not parseable into FoundProduct.",
                        f"final_result_preview={preview}" if preview else "final_result_preview=<empty>",
                    ],
                    latest_url=latest_url,
                    artifact_refs=artifact_refs,
                    screenshot_base64=screenshot_base64,
                    message="invalid final result",
                )

            return _ItemRoundResult(
                product=product,
                interrupt_reason=None,
                recent_actions=recent_actions,
                recent_errors=recent_errors,
                latest_url=latest_url,
                artifact_refs=artifact_refs,
                screenshot_base64=screenshot_base64,
                message="ok",
            )
        except Exception as exc:
            return _ItemRoundResult(
                product=None,
                interrupt_reason="run_failed",
                recent_actions=[],
                recent_errors=[str(exc)],
                latest_url=None,
                artifact_refs=[],
                screenshot_base64=None,
                message=str(exc),
            )

    def set_progress_emitter(self, emitter: ProgressEmitter | None) -> None:
        self._progress_emitter = emitter

    def add_followup_steps(self, steps: list[str]) -> None:
        cleaned = [step.strip() for step in steps if step and step.strip()]
        if cleaned:
            self._pending_followups = cleaned

    def get_live_url(self) -> str | None:
        return self._cloud_live_url

    async def close(self) -> None:
        if self._browser_session is None:
            self._agent = None
            return

        if not self._owns_browser_session:
            self._agent = None
            return

        try:
            await self._browser_session.stop()
        except Exception as exc:
            if not _is_cdp_not_initialized_error(exc):
                raise
        finally:
            self._browser_session = None
            self._agent = None

    async def _ensure_agent(self, initial_steps: list[str]) -> None:
        if self._agent is not None:
            return

        from browser_use import Agent, BrowserSession, ChatBrowserUse

        if self._browser_session is None:
            use_cloud_browser = self._should_force_cloud_browser()
            session_kwargs = _build_browser_session_kwargs(self._settings, use_cloud_browser=use_cloud_browser)
            self._browser_session = BrowserSession(**session_kwargs)
            self._owns_browser_session = True

        await self._browser_session.start()
        self._cloud_live_url = self._build_cloud_live_url()
        await self._emit_cloud_live_url_if_ready()

        if self._llm is None:
            self._llm = ChatBrowserUse(
                model=self._settings.browser_use_model,
                api_key=self._settings.browser_use_api_key,
            )

        task = self._build_initial_task(initial_steps)
        self._agent = Agent(
            task=task,
            llm=self._llm,
            browser_session=self._browser_session,
            use_vision=True,
            max_actions_per_step=4,
            flash_mode=bool(self._settings.browser_flash_mode),
            use_judge=False,
        )

    async def _emit_step_end_progress(self, agent) -> None:
        if self._progress_emitter is None:
            return

        if not self._cloud_live_url:
            self._cloud_live_url = self._build_cloud_live_url()
        await self._emit_cloud_live_url_if_ready()

        now = time.monotonic()
        if now - self._last_live_emit_at < LIVE_EMIT_MIN_INTERVAL_SECONDS:
            return
        self._last_live_emit_at = now

        history = getattr(agent, "history", None)
        state = getattr(agent, "state", None)
        if history is None:
            return

        try:
            screenshots = history.screenshots(n_last=1)
        except Exception:
            screenshots = []
        screenshot_raw = next((item for item in reversed(screenshots) if item), None)
        screenshot_payload, screenshot_truncated = self._trim_live_screenshot(screenshot_raw)

        try:
            urls = history.urls()
        except Exception:
            urls = []
        latest_url = next((url for url in reversed(urls) if url), "")

        try:
            errors = [item for item in history.errors() if item][-3:]
        except Exception:
            errors = []

        step_number = int(getattr(state, "n_steps", 1) or 1)
        agent_output = getattr(state, "last_model_output", None)
        await self._progress_emitter(
            {
                "item_id": self._spec.item_id,
                "item_category": self._spec.category,
                "round": self._current_round,
                "step": max(1, step_number),
                "status": "in_progress",
                "message": f"step {max(1, step_number)}",
                "latest_url": str(latest_url or "").strip(),
                "recent_actions": self._summarize_actions(agent_output),
                "recent_errors": [str(err) for err in errors if str(err).strip()],
                "screenshot_base64": screenshot_payload,
                "screenshot_truncated": screenshot_truncated,
                "live_url": self._cloud_live_url or "",
            }
        )

    def _summarize_actions(self, agent_output) -> list[str]:
        raw_actions = getattr(agent_output, "action", None)
        if not isinstance(raw_actions, list):
            return []

        rendered: list[str] = []
        for action in raw_actions:
            if hasattr(action, "model_dump"):
                action_data = action.model_dump(exclude_none=True, exclude_unset=True)  # type: ignore[attr-defined]
            elif isinstance(action, dict):
                action_data = action
            else:
                continue

            if not isinstance(action_data, dict) or not action_data:
                continue
            action_name = str(next(iter(action_data.keys()))).strip()
            params = action_data.get(action_name)
            if isinstance(params, dict) and params:
                if "index" in params:
                    rendered.append(f"{action_name}(index={params['index']})")
                else:
                    key = str(next(iter(params.keys()))).strip()
                    value = str(params.get(key, "")).strip()
                    if len(value) > 28:
                        value = f"{value[:28]}..."
                    rendered.append(f"{action_name}({key}={value})")
            else:
                rendered.append(action_name)

        return rendered[-3:]

    def _trim_live_screenshot(self, raw: str | None) -> tuple[str | None, bool]:
        if raw is None:
            return None, False

        limit = min(
            max(1, int(self._settings.browse_interrupt_screenshot_max_chars)),
            LIVE_SCREENSHOT_MAX_CHARS,
        )
        if len(raw) <= limit:
            return raw, False
        return raw[:limit], True

    def _should_force_cloud_browser(self) -> bool:
        return bool((self._settings.browser_use_api_key or "").strip())

    def _build_cloud_live_url(self) -> str | None:
        if self._browser_session is None:
            return None
        use_cloud = bool(getattr(self._browser_session, "cloud_browser", False))
        if not use_cloud:
            profile = getattr(self._browser_session, "browser_profile", None)
            use_cloud = bool(getattr(profile, "use_cloud", False))
        cdp_url = str(getattr(self._browser_session, "cdp_url", "") or "").strip()
        if not use_cloud and "browser-use.com" not in cdp_url.lower():
            return None
        if not cdp_url:
            return None
        return f"https://live.browser-use.com?wss={quote(cdp_url, safe='')}"

    async def _emit_cloud_live_url_if_ready(self) -> None:
        if self._cloud_live_url_emitted:
            return
        if self._progress_emitter is None:
            return
        if not self._cloud_live_url:
            return

        await self._progress_emitter(
            {
                "item_id": self._spec.item_id,
                "item_category": self._spec.category,
                "round": self._current_round,
                "step": 0,
                "status": "cloud_live_ready",
                "message": "cloud live viewer ready",
                "latest_url": "",
                "recent_actions": [],
                "recent_errors": [],
                "screenshot_base64": None,
                "screenshot_truncated": False,
                "live_url": self._cloud_live_url,
            }
        )
        self._cloud_live_url_emitted = True

    def _build_initial_task(self, steps: list[str]) -> str:
        step_lines = [f"{idx + 1}. {step}" for idx, step in enumerate(steps) if step.strip()]
        if not step_lines:
            step_lines = [
                f"1. Search using query: {self._item_plan.query}",
                "2. Open one matching product detail page.",
                "3. Validate category/color/visual effect.",
                "4. Capture a screenshot.",
            ]

        return (
            "You are shopping assistant for a single outfit item.\n"
            f"Target item_id: {self._spec.item_id}\n"
            f"Target category: {self._spec.category}\n"
            f"Target color: {self._spec.color}\n"
            f"Target visual effect: {self._spec.visual_effect}\n"
            "Only search menswear and match nearest available options when exact option does not exist.\n"
            "Start by clicking MEN directly. Do not rely on hover to open menu.\n"
            "Prefer direct click/scroll actions. Avoid custom evaluate/find_elements unless normal actions fail repeatedly.\n"
            "Avoid long exploratory scrolling: decide within current viewport and at most 2 additional page scrolls.\n"
            "Follow these steps:\n"
            f"{chr(10).join(step_lines)}\n"
            "Stop immediately after you have all three outputs for this item: "
            "left-image screenshot, product title, product URL.\n"
            "Estimate where the main product image appears in the final screenshot. "
            "For any brand/site, return normalized crop_box with x,y,width,height in [0,1]. "
            "If uncertain, use null.\n"
            "When done, output JSON only with keys: "
            "title,url,crop_box."
        )

    def _format_followup(self, steps: list[str]) -> str:
        lines = [f"{idx + 1}. {step}" for idx, step in enumerate(steps)]
        return (
            "Continue from the current browser state and follow these additional instructions:\n"
            f"{chr(10).join(lines)}\n"
            "Do not restart from scratch unless absolutely required."
        )

    def _parse_final_product(
        self,
        *,
        final_result: str | None,
        latest_url: str | None,
        screenshot_base64: str | None,
    ) -> FoundProduct | None:
        payload = _extract_json_payload(final_result)
        if payload is None:
            payload = {}

        if "product" in payload and isinstance(payload["product"], dict):
            payload = payload["product"]

        normalized = _normalize_found_product_payload(payload, final_result=final_result)
        normalized["title"] = str(normalized.get("title") or "").strip()
        if not normalized["title"]:
            return None
        if not str(normalized.get("url") or "").strip() and str(latest_url or "").strip():
            normalized["url"] = str(latest_url).strip()
        normalized.setdefault("item_id", self._spec.item_id)
        normalized.setdefault("category", self._spec.category)
        normalized.setdefault("color", self._spec.color)
        normalized.setdefault("material", self._spec.visual_effect)
        normalized.setdefault("currency", self._settings.default_currency)
        if screenshot_base64 and not normalized.get("screenshot_base64"):
            normalized["screenshot_base64"] = screenshot_base64
        crop_candidate = _extract_crop_box_candidate(payload)
        if crop_candidate is None:
            crop_candidate = _extract_crop_box_from_text(final_result)
        crop_box = _coerce_crop_box(crop_candidate)
        if crop_box is None:
            normalized.pop("crop_box", None)
        else:
            normalized["crop_box"] = crop_box
            screenshot_payload = str(normalized.get("screenshot_base64") or "").strip()
            if screenshot_payload:
                cropped_screenshot = _crop_screenshot_base64(screenshot_payload, crop_box)
                if cropped_screenshot and cropped_screenshot != screenshot_payload:
                    normalized["screenshot_base64"] = cropped_screenshot
                    # Screenshot is already cropped to product area; avoid double crop in UI.
                    normalized["crop_box"] = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}

        try:
            product = FoundProduct.model_validate(normalized)
        except Exception:
            return None

        if not str(product.url):
            return None
        return product


WorkerFactory = Callable[[OutfitItemSpec, SearchPlanItem, object | None, object | None], _ItemWorker]


class BrowserExecutor:
    def __init__(
        self,
        settings: RuntimeSettings,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        self._settings = settings
        self._worker_factory = worker_factory or self._default_worker_factory

    async def execute(
        self,
        search_plan: SearchPlan,
        outfits: list[OutfitCandidate],
        interruption_handler: InterruptionHandler | None = None,
        progress_emitter: ProgressEmitter | None = None,
    ) -> BrowserExecutionResult:
        use_mock = self._should_use_mock()
        if not use_mock:
            self._validate_runtime()

        item_to_spec = {item.item_id: item for outfit in outfits for item in outfit.items}

        products: list[FoundProduct] = []
        traces: list[ExecutionTrace] = []
        item_results: list[BrowserItemResult] = []
        issues: list[str] = []

        for item_idx, item_plan in enumerate(search_plan.per_item_steps):
            spec = item_to_spec.get(item_plan.item_id)
            if spec is None:
                continue

            trace_steps: list[ExecutionStep] = []
            round_idx = 0
            worker: _ItemWorker
            if use_mock:
                worker = _MockItemWorker(self._settings, spec, item_plan)
            else:
                # Each item uses an independent browser cloud session.
                worker = self._worker_factory(spec, item_plan, None, None)
            set_progress = getattr(worker, "set_progress_emitter", None)
            if callable(set_progress):
                set_progress(progress_emitter)

            try:
                item_global_steps = self._global_steps_for_item(search_plan.global_steps, item_idx)
                current_instructions = _render_item_steps(item_global_steps, item_plan)
                item_started_at = time.monotonic()
                while True:
                    round_idx += 1
                    trace_steps.append(
                        ExecutionStep(
                            stage="PLAN",
                            detail=f"round={round_idx} item={spec.item_id}",
                            status="ok",
                            budget_snapshot={
                                "round": round_idx,
                                "max_steps_per_item": search_plan.budgets.max_steps_per_item,
                            },
                        )
                    )
                    if progress_emitter is not None:
                        live_url = ""
                        get_live_url = getattr(worker, "get_live_url", None)
                        if callable(get_live_url):
                            live_url = str(get_live_url() or "").strip()
                        await progress_emitter(
                            {
                                "item_id": spec.item_id,
                                "item_category": spec.category,
                                "round": round_idx,
                                "step": 0,
                                "status": "starting",
                                "message": "browser run started",
                                "latest_url": "",
                                "recent_actions": [],
                                "recent_errors": [],
                                "screenshot_base64": None,
                                "screenshot_truncated": False,
                                "live_url": live_url,
                                "elapsed_seconds": 0,
                            }
                        )

                    outcome = await worker.run_round(
                        current_instructions,
                        max_steps=(
                            search_plan.budgets.max_steps_per_item
                            if search_plan.budgets.max_steps_per_item > 0
                            else None
                        ),
                    )
                    elapsed_seconds = round(max(0.0, time.monotonic() - item_started_at), 2)
                    if progress_emitter is not None and outcome.product is not None:
                        live_url = ""
                        get_live_url = getattr(worker, "get_live_url", None)
                        if callable(get_live_url):
                            live_url = str(get_live_url() or "").strip()
                        await progress_emitter(
                            {
                                "item_id": spec.item_id,
                                "item_category": spec.category,
                                "round": round_idx,
                                "step": 0,
                                "status": "item_found",
                                "message": "item found",
                                "latest_url": outcome.latest_url or str(outcome.product.url),
                                "recent_actions": [],
                                "recent_errors": [],
                                "screenshot_base64": None,
                                "screenshot_truncated": False,
                                "live_url": live_url,
                                "elapsed_seconds": elapsed_seconds,
                            }
                        )
                    screenshot_payload, screenshot_truncated = self._trim_screenshot(outcome.screenshot_base64)
                    if progress_emitter is not None:
                        live_url = ""
                        get_live_url = getattr(worker, "get_live_url", None)
                        if callable(get_live_url):
                            live_url = str(get_live_url() or "").strip()
                        await progress_emitter(
                            {
                                "item_id": spec.item_id,
                                "item_category": spec.category,
                                "round": round_idx,
                                "status": "success" if outcome.product is not None else "in_progress",
                                "message": outcome.message,
                                "latest_url": outcome.latest_url,
                                "recent_actions": outcome.recent_actions[-3:],
                                "recent_errors": outcome.recent_errors[-3:],
                                "screenshot_base64": screenshot_payload,
                                "screenshot_truncated": screenshot_truncated,
                                "live_url": live_url,
                                "elapsed_seconds": elapsed_seconds,
                            }
                        )
                    for action in outcome.recent_actions:
                        trace_steps.append(
                            ExecutionStep(
                                stage="ACT",
                                detail=action,
                                status="ok",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )
                    for err in outcome.recent_errors:
                        trace_steps.append(
                            ExecutionStep(
                                stage="EVALUATE",
                                detail=err,
                                status="warning",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )

                    if outcome.product is not None:
                        products.append(outcome.product)
                        screenshot_ref = _latest_artifact_ref(outcome.artifact_refs)
                        trace_steps.append(
                            ExecutionStep(
                                stage="DONE",
                                detail="Item resolved successfully.",
                                status="ok",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )
                        item_results.append(
                            BrowserItemResult(
                                item_id=spec.item_id,
                                success=True,
                                failed_step=None,
                                screenshot_ref=screenshot_ref,
                                product=outcome.product,
                                message="ok",
                            )
                        )
                        break

                    interrupt_reason = outcome.interrupt_reason or "run_failed"
                    trace_steps.append(
                        ExecutionStep(
                            stage="REPLAN",
                            detail=f"interrupted: {interrupt_reason}",
                            status="warning",
                            artifact_refs=outcome.artifact_refs,
                            budget_snapshot={"round": round_idx},
                        )
                    )

                    interruption = BrowseInterruptionPacket(
                        item_id=spec.item_id,
                        interrupt_reason=interrupt_reason,
                        resume_round=round_idx,
                        recent_actions=outcome.recent_actions,
                        recent_errors=outcome.recent_errors,
                        latest_url=outcome.latest_url,
                        screenshot_base64=screenshot_payload,
                        screenshot_truncated=screenshot_truncated,
                    )

                    if interruption_handler is None:
                        if self._should_auto_resume_without_handler(
                            interrupt_reason=interrupt_reason,
                            round_idx=round_idx,
                            max_steps_per_item=search_plan.budgets.max_steps_per_item,
                        ):
                            resume_steps = self._auto_resume_steps(spec)
                            add_followup = getattr(worker, "add_followup_steps", None)
                            if callable(add_followup):
                                add_followup(resume_steps)
                            trace_steps.append(
                                ExecutionStep(
                                    stage="REPLAN",
                                    detail=(
                                        "auto resume after max_steps_reached "
                                        f"(round={round_idx}, item={spec.item_id})"
                                    ),
                                    status="warning",
                                    artifact_refs=outcome.artifact_refs,
                                    budget_snapshot={"round": round_idx},
                                )
                            )
                            continue

                        issue = f"item={spec.item_id} interrupted: {interrupt_reason}"
                        issues.append(issue)
                        item_results.append(
                            BrowserItemResult(
                                item_id=spec.item_id,
                                success=False,
                                failed_step=interrupt_reason,
                                screenshot_ref=_latest_artifact_ref(outcome.artifact_refs),
                                product=None,
                                message=issue,
                            )
                        )
                        trace_steps.append(
                            ExecutionStep(
                                stage="DONE",
                                detail=issue,
                                status="error",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )
                        break

                    resolution = await interruption_handler(interruption)
                    if resolution.action == "resume":
                        current_instructions = resolution.resume_steps or current_instructions
                        add_followup = getattr(worker, "add_followup_steps", None)
                        if callable(add_followup):
                            add_followup(resolution.resume_steps)
                        trace_steps.append(
                            ExecutionStep(
                                stage="REPLAN",
                                detail=f"resume accepted: {resolution.reason}",
                                status="warning",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )
                        continue

                    issue = resolution.reason or f"item={spec.item_id} interrupted: {interrupt_reason}"
                    if resolution.action == "abort_browse":
                        item_results.append(
                            BrowserItemResult(
                                item_id=spec.item_id,
                                success=False,
                                failed_step=interrupt_reason,
                                screenshot_ref=_latest_artifact_ref(outcome.artifact_refs),
                                product=None,
                                message=issue,
                            )
                        )
                        trace_steps.append(
                            ExecutionStep(
                                stage="DONE",
                                detail=issue,
                                status="error",
                                artifact_refs=outcome.artifact_refs,
                                budget_snapshot={"round": round_idx},
                            )
                        )
                        issues.append(f"item={spec.item_id} {issue}")
                        traces.append(ExecutionTrace(item_id=spec.item_id, steps=trace_steps, issues=[issue]))
                        raise BrowserExecutionError(issue)

                    issues.append(f"item={spec.item_id} {issue}")
                    item_results.append(
                        BrowserItemResult(
                            item_id=spec.item_id,
                            success=False,
                            failed_step=interrupt_reason,
                            screenshot_ref=_latest_artifact_ref(outcome.artifact_refs),
                            product=None,
                            message=issue,
                        )
                    )
                    trace_steps.append(
                        ExecutionStep(
                            stage="DONE",
                            detail=issue,
                            status="error",
                            artifact_refs=outcome.artifact_refs,
                            budget_snapshot={"round": round_idx},
                        )
                    )
                    break
            finally:
                close_worker = getattr(worker, "close", None)
                if callable(close_worker):
                    try:
                        maybe_awaitable = close_worker()
                        if hasattr(maybe_awaitable, "__await__"):
                            await maybe_awaitable
                    except Exception as exc:
                        issue = f"cleanup_failed item={spec.item_id}: {exc}"
                        issues.append(issue)
                        trace_steps.append(
                            ExecutionStep(
                                stage="EVALUATE",
                                detail=issue,
                                status="warning",
                                artifact_refs=[],
                                budget_snapshot={"round": round_idx},
                            )
                        )

            trace_issues = [
                result.message
                for result in item_results
                if result.item_id == spec.item_id and not result.success
            ]
            traces.append(ExecutionTrace(item_id=spec.item_id, steps=trace_steps, issues=trace_issues))

        return BrowserExecutionResult(
            products=products,
            traces=traces,
            item_results=item_results,
            issues=issues,
        )

    def _validate_runtime(self) -> None:
        api_key = (self._settings.browser_use_api_key or "").strip()
        if api_key:
            return

        raise BrowserExecutionError(
            "browser-use API key is required when ALLOW_MOCK_PRODUCTS=false. "
            "Set BROWSER_USE_API_KEY in .env."
        )

    def _should_use_mock(self) -> bool:
        return bool(self._settings.allow_mock_products)

    def _trim_screenshot(self, raw: str | None) -> tuple[str | None, bool]:
        if raw is None:
            return None, False

        limit = max(1, int(self._settings.browse_interrupt_screenshot_max_chars))
        if len(raw) <= limit:
            return raw, False
        return raw[:limit], True

    def _default_worker_factory(
        self,
        spec: OutfitItemSpec,
        item_plan: SearchPlanItem,
        browser: object | None,
        llm: object | None,
    ) -> _ItemWorker:
        return _BrowserUseItemWorker(
            settings=self._settings,
            spec=spec,
            item_plan=item_plan,
            llm=llm,
            browser_session=browser,
            owns_browser_session=browser is None,
        )

    def _global_steps_for_item(self, global_steps: list[str], item_idx: int) -> list[str]:
        _ = item_idx
        return list(global_steps)

    def _should_auto_resume_without_handler(
        self,
        *,
        interrupt_reason: str,
        round_idx: int,
        max_steps_per_item: int,
    ) -> bool:
        if interrupt_reason != "max_steps_reached":
            return False
        if max_steps_per_item > 0:
            return False
        max_rounds = max(1, int(self._settings.browser_auto_resume_rounds_when_unbounded))
        return round_idx < max_rounds

    def _auto_resume_steps(self, spec: OutfitItemSpec) -> list[str]:
        return [
            "Continue from current page state. Do not restart homepage/menu unless page is broken.",
            (
                f"Prioritize completion for item {spec.item_id}: select best product, set color, "
                "capture left image screenshot, and output JSON."
            ),
            "Return JSON keys only: title,url,crop_box.",
        ]


def _render_item_steps(global_steps: list[str], item_plan: SearchPlanItem) -> list[str]:
    rendered: list[str] = []
    for idx, step in enumerate(global_steps):
        if str(step).strip():
            rendered.append(f"[GLOBAL {idx + 1}] {str(step).strip()}")
    for idx, step in enumerate(item_plan.steps):
        rendered.append(f"[{idx + 1}] {step.action}: {step.instruction}")
    if item_plan.filters:
        rendered.append(f"Apply filters: {', '.join(item_plan.filters)}")
    return rendered


def _latest_artifact_ref(artifact_refs: list[str]) -> str | None:
    if not artifact_refs:
        return None
    last = artifact_refs[-1]
    if not last:
        return None
    path = Path(str(last))
    if path.suffix:
        return f"artifact://{path.name}"
    return str(last)


def _extract_crop_box_candidate(payload: dict[str, Any]) -> Any:
    if "crop_box" in payload:
        return payload.get("crop_box")
    for key in ("screenshot_crop_box", "image_crop_box", "crop"):
        if key in payload:
            return payload.get(key)
    return None


def _extract_crop_box_from_text(raw: str | None) -> list[float] | None:
    text = str(raw or "")
    if not text:
        return None
    match = re.search(
        r'crop_box"\s*:\s*\[\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*,\s*([-+]?\d*\.?\d+)\s*\]',
        text,
    )
    if not match:
        return None
    try:
        return [float(match.group(idx)) for idx in range(1, 5)]
    except Exception:
        return None


def _coerce_crop_box(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None

    values: dict[str, float]
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        parsed = [_coerce_float(item) for item in raw]
        if any(value is None for value in parsed):
            return None
        values = {
            "x": parsed[0],  # type: ignore[index]
            "y": parsed[1],  # type: ignore[index]
            "width": parsed[2],  # type: ignore[index]
            "height": parsed[3],  # type: ignore[index]
        }
    elif isinstance(raw, dict):
        x = _coerce_float(raw.get("x"))
        y = _coerce_float(raw.get("y"))
        width = _coerce_float(raw.get("width"))
        height = _coerce_float(raw.get("height"))
        if None not in (x, y, width, height):
            values = {
                "x": x,  # type: ignore[arg-type]
                "y": y,  # type: ignore[arg-type]
                "width": width,  # type: ignore[arg-type]
                "height": height,  # type: ignore[arg-type]
            }
        else:
            left = _coerce_float(raw.get("left"))
            top = _coerce_float(raw.get("top"))
            right = _coerce_float(raw.get("right"))
            bottom = _coerce_float(raw.get("bottom"))
            if None in (left, top, right, bottom):
                return None
            values = {
                "x": left,  # type: ignore[arg-type]
                "y": top,  # type: ignore[arg-type]
                "width": (right - left),  # type: ignore[operator]
                "height": (bottom - top),  # type: ignore[operator]
            }
    else:
        return None

    if not all(math.isfinite(value) for value in values.values()):
        return None

    if any(value > 1.0 for value in values.values()) and all(0.0 <= value <= 100.0 for value in values.values()):
        values = {key: (value / 100.0) for key, value in values.items()}

    x = min(max(values["x"], 0.0), 1.0)
    y = min(max(values["y"], 0.0), 1.0)
    width = min(max(values["width"], 0.0), 1.0)
    height = min(max(values["height"], 0.0), 1.0)
    if width <= 0.0 or height <= 0.0:
        return None
    if x >= 1.0 or y >= 1.0:
        return None

    if x + width > 1.0:
        width = 1.0 - x
    if y + height > 1.0:
        height = 1.0 - y
    if width <= 0.0 or height <= 0.0:
        return None

    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _crop_screenshot_base64(raw: str, crop_box: dict[str, float]) -> str:
    payload = str(raw or "").strip()
    if not payload:
        return payload

    try:
        from PIL import Image
    except Exception:
        return payload

    try:
        padded = payload + ("=" * (-len(payload) % 4))
        image_bytes = base64.b64decode(padded)
        with Image.open(io.BytesIO(image_bytes)) as image:
            width = int(image.width or 0)
            height = int(image.height or 0)
            if width <= 1 or height <= 1:
                return payload

            x = int(round(float(crop_box.get("x", 0.0)) * width))
            y = int(round(float(crop_box.get("y", 0.0)) * height))
            w = int(round(float(crop_box.get("width", 0.0)) * width))
            h = int(round(float(crop_box.get("height", 0.0)) * height))

            x = max(0, min(width - 1, x))
            y = max(0, min(height - 1, y))
            w = max(1, min(width - x, w))
            h = max(1, min(height - y, h))
            if w <= 0 or h <= 0:
                return payload

            cropped = image.crop((x, y, x + w, y + h))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            return base64.b64encode(output.getvalue()).decode("ascii")
    except Exception:
        return payload


def _extract_json_payload(raw: str | None) -> dict | None:
    if not raw:
        return None

    text = raw.strip()
    if not text:
        return None

    candidates = [text]

    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(fenced)

    braces = re.findall(r"(\{.*\})", text, flags=re.DOTALL)
    candidates.extend(braces)

    for candidate in candidates:
        loaded = _load_structured_candidate(candidate)
        if isinstance(loaded, dict):
            return loaded
        if isinstance(loaded, list):
            first_dict = next((item for item in loaded if isinstance(item, dict)), None)
            if first_dict is not None:
                return first_dict
    return None


def _load_structured_candidate(candidate: str) -> Any:
    text = str(candidate or "").strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        unescaped = _decode_jsonish_text(text)
        if unescaped and unescaped != text:
            try:
                return json.loads(unescaped)
            except Exception:
                pass

    try:
        return ast.literal_eval(text)
    except Exception:
        unescaped = _decode_jsonish_text(text)
        if unescaped and unescaped != text:
            try:
                return ast.literal_eval(unescaped)
            except Exception:
                return None
        return None


def _normalize_found_product_payload(payload: dict[str, Any], *, final_result: str | None) -> dict[str, Any]:
    source = dict(payload or {})
    normalized: dict[str, Any] = {}

    for key in ALLOWED_FOUND_PRODUCT_KEYS:
        if key in source:
            normalized[key] = source[key]

    title = _pick_text_value(source, "title", "product_title", "product_name", "name")
    if not title:
        title = _extract_title_from_text(final_result)
    if title:
        normalized["title"] = title

    url = _pick_text_value(source, "url", "product_url", "product_link", "link", "href")
    if not url:
        url = _extract_url_from_text(final_result)
    if url:
        normalized["url"] = url

    if "material" not in normalized and "visual_effect" in source:
        normalized["material"] = source.get("visual_effect")

    return normalized


def _pick_text_value(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _extract_title_from_text(raw: str | None) -> str:
    text = _decode_jsonish_text(str(raw or "").strip())
    if not text:
        return ""

    patterns = [
        r'(?im)"title"\s*:\s*"([^"]+)"',
        r"(?im)'title'\s*:\s*'([^']+)'",
        r"(?im)^\s*(?:title|product[_\s-]?title|name)\s*[:：]\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        value = str(match.group(1) or "").strip().strip(",").strip('"').strip("'")
        if value:
            return value
    return ""


def _extract_url_from_text(raw: str | None) -> str:
    text = _decode_jsonish_text(str(raw or ""))
    if not text:
        return ""
    match = re.search(r"https?://[^\s\"'>]+", text)
    if not match:
        return ""
    url = str(match.group(0) or "").strip()
    # Final result often contains escaped JSON fragments like https://...\\"
    url = url.replace("\\/", "/")
    url = url.rstrip("\\,;})]>\"'")
    return url


def _decode_jsonish_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    if "\\\"" not in raw and "\\n" not in raw and "\\/" not in raw and "\\'" not in raw:
        return raw
    return (
        raw.replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\/", "/")
        .replace('\\"', '"')
        .replace("\\'", "'")
    )


def _is_cdp_not_initialized_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return "root cdp client not initialized" in text or "cdp client not initialized" in text
