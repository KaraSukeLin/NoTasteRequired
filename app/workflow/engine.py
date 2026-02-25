from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.designer import DesignerAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewerAgent
from app.config import ConfigBundle, RuntimeSettings
from app.models import EventPayload, FoundProduct, OutfitCard, OutfitItemSpec, PhaseName, SessionState
from app.services.browser_exec import BrowserExecutionError, BrowserExecutor
from app.services.memory import InMemoryMemoryStore
from app.services.tracing import TraceRecorder

SEARCH_RESULT_CATEGORY_ORDER = {
    "外套": 0,
    "jacket": 0,
    "coat": 0,
    "outerwear": 0,
    "上身": 1,
    "上衣": 1,
    "top": 1,
    "shirt": 1,
    "tee": 1,
    "t-shirt": 1,
    "tshirt": 1,
    "下身": 2,
    "褲": 2,
    "bottom": 2,
    "pants": 2,
    "trousers": 2,
}


class GraphState(TypedDict):
    session: SessionState


EmitFn = Callable[[str, dict], Awaitable[None]]


class WorkflowEngine:
    def __init__(
        self,
        *,
        config: ConfigBundle,
        settings: RuntimeSettings,
        designer: DesignerAgent,
        reviewer: ReviewerAgent,
        planner: PlannerAgent,
        browser_executor: BrowserExecutor,
        memory_store: InMemoryMemoryStore,
        tracer: TraceRecorder,
    ) -> None:
        self._config = config
        self._settings = settings
        self._designer = designer
        self._reviewer = reviewer
        self._planner = planner
        self._browser_executor = browser_executor
        self._memory_store = memory_store
        self._tracer = tracer

    async def run(self, session: SessionState, emit: EmitFn) -> SessionState:
        graph = self._build_graph(emit)
        result = await graph.ainvoke({"session": session})
        return result["session"]

    def _build_graph(self, emit: EmitFn):
        builder: StateGraph = StateGraph(GraphState)

        builder.add_node("collect", self._collect_node(emit))
        builder.add_node("design", self._design_node(emit))
        builder.add_node("review", self._review_node(emit))
        builder.add_node("plan", self._plan_node(emit))
        builder.add_node("browse", self._browse_node(emit))
        builder.add_node("present", self._present_node(emit))

        builder.add_edge(START, "collect")
        builder.add_conditional_edges(
            "collect",
            self._route_after_collect,
            {
                "design": "design",
                "review": "review",
                "plan": "plan",
            },
        )

        builder.add_edge("design", "review")
        builder.add_conditional_edges(
            "review",
            self._route_after_review,
            {
                "design": "design",
                "present": "present",
            },
        )

        builder.add_edge("plan", "browse")
        builder.add_edge("browse", "present")
        builder.add_edge("present", END)
        return builder.compile()

    def _route_after_collect(self, state: GraphState) -> str:
        session = state["session"]
        if session.turn_intent == "search":
            return "plan"
        if session.turn_intent == "modify":
            return "review"
        return "design"

    def _route_after_review(self, state: GraphState) -> str:
        session = state["session"]
        report = session.current_design_context.review_report

        if report and report.redesign_requests:
            if session.retry_counters.redesign < session.constraints.max_design_retries:
                session.retry_counters.redesign += 1
                session.current_design_context.pending_redesign_requests = list(report.redesign_requests)
                return "design"
            session.warnings.append("Redesign retries exhausted; returning current results.")
            return "present"

        if report and report.approved:
            return "present"

        if session.turn_intent == "modify" and session.retry_counters.redesign == 0:
            session.retry_counters.redesign = 1
            return "present"

        if session.retry_counters.redesign < session.constraints.max_design_retries:
            session.retry_counters.redesign += 1
            if report:
                session.current_design_context.pending_redesign_requests = list(report.redesign_requests)
            return "design"

        session.warnings.append("Redesign retries exhausted; returning current results.")
        return "present"

    def _collect_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "collect"

            async def action() -> str:
                refreshed = self._memory_store.refresh_snapshot(session.session_id)
                refreshed.user_profile = session.memory_snapshot.user_profile
                session.memory_snapshot = refreshed
                self._memory_store.upsert_snapshot(session.session_id, refreshed)
                return "memory snapshot loaded"

            session = await self._run_phase(session, "collect", "load memory", action, emit)
            return {"session": session}

        return node

    def _design_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "design"

            async def action() -> str:
                outfits = await self._designer.run(session)
                session.current_design_context.outfits = outfits
                return f"generated {len(outfits)} outfits"

            session = await self._run_phase(session, "design", "generate outfits", action, emit)
            return {"session": session}

        return node

    def _review_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "review"

            async def action() -> str:
                report = await self._reviewer.run(session)
                session.current_design_context.review_report = report
                return f"approved={report.approved}"

            session = await self._run_phase(session, "review", "review outfits", action, emit)
            return {"session": session}

        return node

    def _plan_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "plan"

            async def action() -> str:
                session.search_plan = await self._planner.run(session)
                return f"planned {len(session.search_plan.per_item_steps)} item searches"

            session = await self._run_phase(session, "plan", "create search plan", action, emit)
            return {"session": session}

        return node

    def _browse_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "browse"

            async def action() -> str:
                if not session.search_plan:
                    raise RuntimeError("search_plan is required before browse phase")

                async def emit_browser_live(payload: dict) -> None:
                    await emit("browser_live", payload)

                result = await self._browser_executor.execute(
                    session.search_plan,
                    session.current_design_context.outfits,
                    progress_emitter=emit_browser_live,
                )
                session.found_products = result.products
                if result.issues:
                    session.warnings.extend(result.issues)
                return f"browser results: {len(result.item_results)} items"

            session = await self._run_phase(session, "browse", "execute browser flow", action, emit)
            return {"session": session}

        return node

    def _present_node(self, emit: EmitFn):
        async def node(state: GraphState) -> GraphState:
            session = state["session"]
            session.current_phase = "present"

            async def action() -> str:
                session.outfit_cards = self._build_cards(session)
                session.current_phase = "done"

                if session.turn_intent in {"design", "modify"}:
                    session.status = "await_user_choice"
                    session.next_agent = "orchestrator"
                    session.assistant_message = "已完成三套搭配設計。請選擇其中一套進行搜尋，或選擇一套繼續修改。"
                else:
                    session.status = "completed"
                    session.next_agent = None
                    session.assistant_message = "NTR Agent 已在網站找到接近這套穿搭的結果，以下依序提供商品圖片、名稱與連結。"

                return f"prepared {len(session.outfit_cards)} outfit cards"

            session = await self._run_phase(session, "present", "compose final cards", action, emit)
            session.memory_snapshot = self._memory_store.update_from_session(session)
            return {"session": session}

        return node

    async def _run_phase(
        self,
        session: SessionState,
        phase: PhaseName,
        input_summary: str,
        action: Callable[[], Awaitable[str]],
        emit: EmitFn,
    ) -> SessionState:
        token = self._tracer.start_phase(phase, input_summary)
        await emit(
            "phase_started",
            {
                "phase": phase,
                "status": "started",
                "input_summary": input_summary,
                "output_summary": "",
                "latency_ms": 0,
                "error_type": None,
                "artifact_refs": [],
            },
        )

        try:
            await action()
            return session
        except BrowserExecutionError as exc:
            session.errors.append(str(exc))
            record = self._tracer.complete_phase(
                token,
                output_summary="browse execution failed",
                status="failed",
                error_type=type(exc).__name__,
                artifact_refs=[],
            )
            await emit("error", record.model_dump(mode="json"))
            session.status = "error"
            session.assistant_message = f"瀏覽流程發生錯誤：{exc}"
            return session
        except Exception as exc:
            session.errors.append(str(exc))
            record = self._tracer.complete_phase(
                token,
                output_summary="phase failed",
                status="failed",
                error_type=type(exc).__name__,
                artifact_refs=[],
            )
            await emit("error", record.model_dump(mode="json"))
            raise

    def _build_cards(self, session: SessionState) -> list[OutfitCard]:
        product_by_item = {product.item_id: product for product in session.found_products}
        cards: list[OutfitCard] = []
        target_outfits = list(session.current_design_context.outfits)
        if session.turn_intent == "search" and session.selected_outfit_id:
            selected = next(
                (outfit for outfit in target_outfits if outfit.outfit_id == session.selected_outfit_id),
                None,
            )
            target_outfits = [selected] if selected is not None else []

        for outfit in target_outfits:
            products: list[FoundProduct] = []
            alternatives: list[str] = []

            outfit_items = list(outfit.items)
            if session.turn_intent == "search":
                outfit_items = [item for item in outfit_items if self._is_search_result_item(item.category)]

            localized_items = [self._localize_item(item) for item in outfit_items]
            localized_outfit_style = self._sanitize_card_style(self._to_zh_tw(outfit.style))
            localized_outfit_title = localized_outfit_style
            localized_outfit_rationale = self._sanitize_card_rationale(self._to_zh_tw(outfit.rationale))

            for item in localized_items:
                found = product_by_item.get(item.item_id)
                if found:
                    products.append(self._localize_product(found))
                    continue

                alternatives.append(
                    f"可改用 category={item.category}, color={item.color}, visual_effect={item.visual_effect} 再搜尋。"
                )

            products.sort(key=lambda product: self._search_result_order(str(product.category)))

            cards.append(
                OutfitCard(
                    outfit_id=outfit.outfit_id,
                    title=localized_outfit_title,
                    style=localized_outfit_style,
                    rationale=localized_outfit_rationale,
                    items=localized_items,
                    products=products,
                    alternatives=alternatives,
                )
            )

        return cards

    def _sanitize_card_style(self, text: str) -> str:
        normalized = re.sub(r"回饋變化\s*[A-Za-z0-9]*", "", str(text or ""), flags=re.IGNORECASE).strip()
        return normalized or "風格方案"

    def _sanitize_card_rationale(self, text: str) -> str:
        normalized = str(text or "")
        normalized = re.sub(r"回饋變化\s*[A-Za-z0-9]*", "", normalized, flags=re.IGNORECASE)
        normalized = normalized.replace("根據你的回饋", "")
        normalized = normalized.replace("已依修改要求調整：", "")
        normalized = re.sub(r"\s{2,}", " ", normalized).strip()
        return normalized or "此風格已依照你的場合與偏好完成搭配。"

    def _localize_item(self, item: OutfitItemSpec) -> OutfitItemSpec:
        return OutfitItemSpec(
            item_id=item.item_id,
            category=self._to_zh_category(item.category),
            color=self._to_zh_tw(item.color),
            visual_effect=self._to_zh_tw(item.visual_effect),
            size_reco=self._to_zh_tw(item.size_reco or "") or None,
        )

    def _localize_product(self, product: FoundProduct) -> FoundProduct:
        payload = product.model_dump(mode="json")
        payload["title"] = self._to_zh_tw(str(payload.get("title") or ""))
        payload["category"] = self._to_zh_category(str(payload.get("category") or ""))
        payload["color"] = self._to_zh_tw(str(payload.get("color") or ""))
        payload["material"] = self._to_zh_tw(str(payload.get("material") or ""))
        return FoundProduct.model_validate(payload)

    def _to_zh_category(self, text: str) -> str:
        token = (text or "").strip().lower()
        mapping = {
            "jacket": "外套",
            "coat": "外套",
            "outerwear": "外套",
            "top": "上身",
            "shirt": "上身",
            "tee": "上身",
            "t-shirt": "上身",
            "tshirt": "上身",
            "pants": "下身",
            "trousers": "下身",
            "bottom": "下身",
            "shoe": "鞋子",
            "shoes": "鞋子",
            "sneakers": "鞋子",
            "accessory": "配件",
            "accessories": "配件",
        }
        if token in mapping:
            return mapping[token]
        return text

    def _to_zh_tw(self, text: str) -> str:
        raw = str(text or "")
        if not raw:
            return raw
        raw = (
            raw.replace("\u2010", "-")
            .replace("\u2011", "-")
            .replace("\u2012", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u2015", "-")
        )

        replacements = {
            "smart-casual": "都會休閒",
            "smart casual": "都會休閒",
            "urban-relaxed": "城市鬆弛",
            "urban relaxed": "城市鬆弛",
            "minimal-japanese": "日系簡約",
            "minimal japanese": "日系簡約",
            "minimal": "極簡",
            "casual": "休閒",
            "formal": "正式",
            "jacket": "外套",
            "coat": "外套",
            "outerwear": "外套",
            "top": "上身",
            "shirt": "上身",
            "pants": "下身",
            "trousers": "下身",
            "shoes": "鞋子",
            "shoe": "鞋子",
            "sneakers": "球鞋",
            "accessory": "配件",
            "black": "黑色",
            "white": "白色",
            "gray": "灰色",
            "grey": "灰色",
            "navy": "海軍藍",
            "beige": "米色",
            "brown": "棕色",
            "blue": "藍色",
            "green": "綠色",
            "red": "紅色",
            "lightweight": "輕盈",
            "structured": "挺版",
            "drapey": "垂墜",
            "matte": "霧面",
            "smooth": "平滑",
        }

        out = raw
        for source, target in replacements.items():
            use_word_boundary = bool(re.fullmatch(r"[a-z]+", source))
            if use_word_boundary:
                pattern = re.compile(rf"\\b{re.escape(source)}\\b", flags=re.IGNORECASE)
            else:
                pattern = re.compile(rf"{re.escape(source)}", flags=re.IGNORECASE)
            out = pattern.sub(target, out)
        return out

    def _is_search_result_item(self, category: str) -> bool:
        return self._search_result_order(category) < 99

    def _search_result_order(self, category: str) -> int:
        token = str(category or "").strip().lower()
        if not token:
            return 99
        return SEARCH_RESULT_CATEGORY_ORDER.get(token, 99)

def payload_to_event(event_name: str, payload: dict) -> EventPayload:
    return EventPayload(event=event_name, data=payload)
