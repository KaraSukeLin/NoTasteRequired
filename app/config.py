from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config_defaults import DEFAULT_APP, DEFAULT_BRANDS, DEFAULT_MODELS, DEFAULT_PROMPTS


def _resolve_env_placeholders(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_placeholders(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        token = obj[2:-1]
        if ":" in token:
            env_name, default = token.split(":", 1)
            return os.getenv(env_name, default)
        return os.getenv(token, "")
    return obj


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_mode: str = "production"
    allow_mock_products: bool = False
    default_currency: str = "TWD"

    groq_api_key: str | None = None
    groq_orchestrator_model: str = "llama-3.3-70b-versatile"
    groq_designer_model: str = "llama-3.3-70b-versatile"
    groq_reviewer_model: str = "llama-3.3-70b-versatile"
    groq_planner_model: str = "llama-3.3-70b-versatile"

    browser_headless: bool = True
    browser_use_api_key: str | None = None
    browser_use_model: str = "bu-latest"
    browser_flash_mode: bool = True
    browser_cross_origin_iframes: bool = False
    browser_paint_order_filtering: bool = False
    browser_highlight_elements: bool = False
    browser_dom_highlight_elements: bool = False
    browser_max_iframes: int = 24
    browser_max_iframe_depth: int = 2
    browser_wait_for_network_idle_page_load_time: float = 0.2
    browser_wait_between_actions: float = 0.08
    browser_auto_resume_rounds_when_unbounded: int = 20
    browse_interrupt_screenshot_max_chars: int = 60000


class AppConfig(BaseModel):
    clarification_required_fields: list[str] = Field(default_factory=list)
    restart_notice: str
    currency: str


class ConfigBundle(BaseModel):
    app: AppConfig
    models: dict[str, str]
    brands: list[str]
    prompts: dict[str, str]


@lru_cache(maxsize=1)
def get_runtime_settings() -> RuntimeSettings:
    return RuntimeSettings()


@lru_cache(maxsize=1)
def get_config_bundle() -> ConfigBundle:
    app_data = dict(DEFAULT_APP)
    models_data = _resolve_env_placeholders(DEFAULT_MODELS)
    brands_data = list(DEFAULT_BRANDS)
    prompts_data = dict(DEFAULT_PROMPTS)

    settings = get_runtime_settings()

    if "currency" not in app_data:
        app_data["currency"] = settings.default_currency

    return ConfigBundle(
        app=AppConfig(**app_data),
        models=models_data,
        brands=brands_data,
        prompts=prompts_data,
    )
