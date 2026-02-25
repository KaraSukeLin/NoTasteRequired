from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sse_starlette.sse import EventSourceResponse

from app.dependencies import AppContainer, get_container
from app.models import EventPayload, RunResult, TurnRequest, TurnResponse
from app.services.session_store import RunNotFoundError, SessionNotFoundError
from app.workflow.engine import payload_to_event


router = APIRouter(prefix="/api", tags=["api"])


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(container: AppContainer = Depends(get_container)):
    session = await container.session_store.create_session()
    session.memory_snapshot = container.memory_store.get_snapshot(session.session_id)
    await container.session_store.save_session(session)
    return {
        "session_id": session.session_id,
    }


@router.post("/sessions/{session_id}/turn", response_model=TurnResponse)
async def post_turn(
    session_id: str,
    body: TurnRequest,
    container: AppContainer = Depends(get_container),
):
    try:
        session = await container.session_store.get_session(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")

    session = await container.orchestrator.apply_turn(session, body)
    container.memory_store.upsert_snapshot(session.session_id, session.memory_snapshot)
    await container.session_store.save_session(session)

    if session.status != "run_started":
        return TurnResponse(
            session_id=session.session_id,
            status=session.status,
            assistant_message=session.assistant_message,
            user_profile=session.memory_snapshot.user_profile.model_dump(mode="json"),
            pending_question=session.pending_question,
            next_agent=session.next_agent,
            run_id=None,
        )

    run = await container.session_store.create_run(session.session_id)
    session.active_run_id = run.run_id
    session.status = "run_started"
    await container.session_store.save_session(session)
    asyncio.create_task(_run_flow(container, session.session_id, run.run_id))
    return TurnResponse(
        session_id=session.session_id,
        status="run_started",
        assistant_message=session.assistant_message,
        user_profile=session.memory_snapshot.user_profile.model_dump(mode="json"),
        pending_question=None,
        next_agent=session.next_agent,
        run_id=run.run_id,
    )


@router.get("/sessions/{session_id}/runs/{run_id}/events")
async def stream_events(
    session_id: str,
    run_id: str,
    container: AppContainer = Depends(get_container),
):
    async def gen():
        try:
            async for event in container.session_store.iter_events(session_id, run_id):
                yield {
                    "event": event.event,
                    "data": json.dumps(event.data, ensure_ascii=False),
                }
        except RunNotFoundError:
            yield {
                "event": "error",
                "data": json.dumps({"message": "run not found"}, ensure_ascii=False),
            }

    return EventSourceResponse(gen())


@router.get("/sessions/{session_id}/runs/{run_id}/result")
async def get_result(
    session_id: str,
    run_id: str,
    container: AppContainer = Depends(get_container),
):
    try:
        run = await container.session_store.get_run(session_id, run_id)
    except RunNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")

    if not run.done or not run.result:
        return Response(status_code=status.HTTP_202_ACCEPTED)
    return run.result.model_dump(mode="json")


@router.get("/healthz")
async def health(container: AppContainer = Depends(get_container)):
    return {
        "status": "ok",
        "mode": container.settings.app_mode,
        "conversation_restore": "disabled_after_restart",
    }


async def _run_flow(container: AppContainer, session_id: str, run_id: str) -> None:
    session = await container.session_store.get_session(session_id)

    async def emit(event: str, data: dict) -> None:
        payload = payload_to_event(event, data)
        await container.session_store.append_event(session_id, run_id, payload)

    try:
        final_state = await container.workflow.run(session, emit)
        await container.session_store.save_session(final_state)

        result = RunResult(
            session_id=session_id,
            run_id=run_id,
            phase=final_state.current_phase,
            status=final_state.status,
            assistant_message=final_state.assistant_message,
            outfit_cards=final_state.outfit_cards,
            issues=final_state.errors + final_state.warnings,
        )

        await emit(
            "done",
            {
                "phase": final_state.current_phase,
                "status": final_state.status,
                "issues": result.issues,
            },
        )
        await container.session_store.complete_run(session_id, run_id, result)
    except Exception as exc:
        payload = EventPayload(event="error", data={"message": str(exc)})
        await container.session_store.append_event(session_id, run_id, payload)
        fallback_result = RunResult(
            session_id=session_id,
            run_id=run_id,
            phase="done",
            status="error",
            assistant_message=f"流程發生錯誤：{exc}",
            outfit_cards=[],
            issues=[str(exc)],
        )
        await container.session_store.complete_run(session_id, run_id, fallback_result)
