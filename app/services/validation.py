from __future__ import annotations

import json
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

try:
    from langchain_groq import ChatGroq
except Exception:  # pragma: no cover
    ChatGroq = None  # type: ignore[assignment]


def _extract_json(content: str) -> Any:
    content = content.strip()
    if not content:
        raise ValueError("Empty model response")

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in model response")
    return json.loads(content[start : end + 1])


def _is_json_complete(payload: Any, required_keys: list[str]) -> tuple[bool, list[str]]:
    if not isinstance(payload, dict):
        return False, required_keys

    missing = [key for key in required_keys if key not in payload]
    return len(missing) == 0, missing


async def invoke_with_json_retry(
    *,
    system_prompt: str,
    user_prompt: str,
    required_keys: list[str],
    model_name: str,
    groq_api_key: str | None,
    fallback_factory: Callable[[], dict[str, Any]],
    max_retries: int = 2,
) -> dict[str, Any]:
    if ChatGroq is not None and groq_api_key:
        llm = ChatGroq(model=model_name, api_key=groq_api_key, temperature=0)
        for _attempt in range(1, max_retries + 2):
            try:
                response = await llm.ainvoke(
                    [
                        SystemMessage(content=system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                )
                parsed = _extract_json(str(response.content))
                is_complete, _missing_keys = _is_json_complete(parsed, required_keys)
                if not is_complete:
                    raise ValueError("missing required keys")
                if not isinstance(parsed, dict):
                    raise ValueError("model output is not a JSON object")
                return parsed
            except Exception:
                continue

    payload = fallback_factory() or {}
    if not isinstance(payload, dict):
        payload = {}
    for key in required_keys:
        payload.setdefault(key, None)
    return payload


async def invoke_with_schema_retry(
    *,
    system_prompt: str,
    user_prompt: str,
    output_model: type[BaseModel],
    model_name: str,
    groq_api_key: str | None,
    fallback_factory: Callable[[], BaseModel | dict[str, Any]],
    max_retries: int = 2,
) -> BaseModel:
    required_keys = list(output_model.model_fields.keys())

    def fallback_payload() -> dict[str, Any]:
        value = fallback_factory()
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        return {}

    payload = await invoke_with_json_retry(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        required_keys=required_keys,
        model_name=model_name,
        groq_api_key=groq_api_key,
        fallback_factory=fallback_payload,
        max_retries=max_retries,
    )

    normalized = {key: payload.get(key) for key in required_keys}
    return output_model.model_construct(**normalized)
