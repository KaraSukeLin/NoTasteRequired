from __future__ import annotations

from functools import lru_cache

from app.agents.designer import DesignerAgent
from app.agents.orchestrator import OrchestratorAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewerAgent
from app.config import ConfigBundle, RuntimeSettings, get_config_bundle, get_runtime_settings
from app.services.browser_exec import BrowserExecutor
from app.services.memory import InMemoryMemoryStore
from app.services.session_store import InMemorySessionStore
from app.services.tracing import TraceRecorder
from app.workflow.engine import WorkflowEngine


class AppContainer:
    def __init__(self, config: ConfigBundle, settings: RuntimeSettings) -> None:
        self.config = config
        self.settings = settings

        self.session_store = InMemorySessionStore()
        self.memory_store = InMemoryMemoryStore()
        self.tracer = TraceRecorder()

        self.orchestrator = OrchestratorAgent(config, settings)
        self.designer = DesignerAgent(config, settings)
        self.reviewer = ReviewerAgent(config, settings)
        self.planner = PlannerAgent(config, settings)
        self.browser_executor = BrowserExecutor(settings)

        self.workflow = WorkflowEngine(
            config=config,
            settings=settings,
            designer=self.designer,
            reviewer=self.reviewer,
            planner=self.planner,
            browser_executor=self.browser_executor,
            memory_store=self.memory_store,
            tracer=self.tracer,
        )


@lru_cache(maxsize=1)
def get_container() -> AppContainer:
    settings = get_runtime_settings()
    config = get_config_bundle()
    return AppContainer(config=config, settings=settings)
