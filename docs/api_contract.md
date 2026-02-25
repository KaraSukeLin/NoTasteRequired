# API Contract

## Endpoints

- `POST /api/sessions`: create session
- `POST /api/sessions/{session_id}/turn`: submit one user turn
- `GET /api/sessions/{session_id}/runs/{run_id}/events`: SSE event stream
- `GET /api/sessions/{session_id}/runs/{run_id}/result`: async run result
- `GET /api/healthz`: health check

## Turn Request

```json
{
  "message": "string",
  "ui_brand_selection": "UNIQLO|GU|OTHER|null",
  "structured_updates": {
    "scenario": "string|null",
    "primary_scene": "string|null",
    "brand": "string|null",
    "preferences": ["string"],
    "exclusions": ["string"]
  },
  "feedback": {
    "action": "search|modify|null",
    "selected_outfit_id": "string|null",
    "reason": "string|null",
    "preserve_outfit_id": "string|null",
    "replace_categories": ["string"]
  }
}
```

## Turn Response

```json
{
  "session_id": "string",
  "status": "idle|run_started|await_user_choice|completed|error",
  "assistant_message": "string",
  "user_profile": {"...": "..."},
  "pending_question": "string|null",
  "next_agent": "orchestrator|designer|reviewer|planner|null",
  "run_id": "string|null"
}
```

## SSE Events

- `phase_started`
- `phase_completed`
- `warning`
- `error`
- `done`

Payload fields include:

- `phase`
- `status`
- `input_summary`
- `output_summary`
- `latency_ms`
- `error_type`
- `artifact_refs`

## Async Run Result

```json
{
  "session_id": "string",
  "run_id": "string",
  "phase": "done|...",
  "status": "await_user_choice|completed|error",
  "assistant_message": "string",
  "outfit_cards": [],
  "issues": []
}
```
