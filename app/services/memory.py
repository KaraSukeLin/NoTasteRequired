from __future__ import annotations

from dataclasses import dataclass, field

from app.models import MemorySnapshot, SessionState, UserProfileMemory


@dataclass
class SessionMemory:
    user_profile: UserProfileMemory = field(default_factory=UserProfileMemory)


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, SessionMemory] = {}

    def get_snapshot(self, session_id: str) -> MemorySnapshot:
        record = self._data.setdefault(session_id, SessionMemory())
        return MemorySnapshot(
            user_profile=UserProfileMemory.model_validate(record.user_profile.model_dump(mode="json")),
        )

    def upsert_snapshot(self, session_id: str, snapshot: MemorySnapshot) -> None:
        record = self._data.setdefault(session_id, SessionMemory())
        record.user_profile = UserProfileMemory.model_validate(snapshot.user_profile.model_dump(mode="json"))

    def refresh_snapshot(self, session_id: str) -> MemorySnapshot:
        snapshot = self.get_snapshot(session_id)
        self.upsert_snapshot(session_id, snapshot)
        return snapshot

    def update_from_session(self, state: SessionState) -> MemorySnapshot:
        snapshot = MemorySnapshot(
            user_profile=UserProfileMemory.model_validate(
                state.memory_snapshot.user_profile.model_dump(mode="json")
            )
        )
        self.upsert_snapshot(state.session_id, snapshot)
        return self.get_snapshot(state.session_id)
