from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.models import EventPayload, RunResult, RunState, SessionState


class SessionNotFoundError(KeyError):
    pass


class RunNotFoundError(KeyError):
    pass


class _RunChannel:
    def __init__(self, run_state: RunState) -> None:
        self.state = run_state
        self.condition = asyncio.Condition()


class InMemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._runs: dict[tuple[str, str], _RunChannel] = {}
        self._lock = asyncio.Lock()

    async def create_session(self) -> SessionState:
        async with self._lock:
            session_id = str(uuid4())
            state = SessionState(session_id=session_id)
            self._sessions[session_id] = state
            return state

    async def get_session(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            raise SessionNotFoundError(session_id)
        return state

    async def save_session(self, state: SessionState) -> None:
        state.updated_at = datetime.now(timezone.utc)
        self._sessions[state.session_id] = state

    async def create_run(self, session_id: str) -> RunState:
        run_id = str(uuid4())
        run_state = RunState(run_id=run_id, session_id=session_id)
        self._runs[(session_id, run_id)] = _RunChannel(run_state)
        return run_state

    async def get_run(self, session_id: str, run_id: str) -> RunState:
        channel = self._runs.get((session_id, run_id))
        if channel is None:
            raise RunNotFoundError(run_id)
        return channel.state

    async def append_event(self, session_id: str, run_id: str, payload: EventPayload) -> None:
        channel = self._runs.get((session_id, run_id))
        if channel is None:
            raise RunNotFoundError(run_id)

        async with channel.condition:
            channel.state.events.append(payload)
            channel.condition.notify_all()

    async def complete_run(self, session_id: str, run_id: str, result: RunResult) -> None:
        channel = self._runs.get((session_id, run_id))
        if channel is None:
            raise RunNotFoundError(run_id)

        async with channel.condition:
            channel.state.result = result
            channel.state.done = True
            channel.condition.notify_all()

    async def iter_events(self, session_id: str, run_id: str):
        channel = self._runs.get((session_id, run_id))
        if channel is None:
            raise RunNotFoundError(run_id)

        index = 0
        while True:
            async with channel.condition:
                await channel.condition.wait_for(
                    lambda: index < len(channel.state.events) or channel.state.done
                )
                while index < len(channel.state.events):
                    event = channel.state.events[index]
                    index += 1
                    yield event
                if channel.state.done and index >= len(channel.state.events):
                    break

