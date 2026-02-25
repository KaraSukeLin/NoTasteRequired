# Operational Limits

## Session Behavior

- session storage is in-memory
- service restart clears session and memory data

## Browser Budgets

- `max_steps_per_item`
- `max_retries_per_item`
- `max_screenshots_per_item`
- `max_eval_images_per_item`
- `top_k_eval_images`

## Production Constraints

- production mode must not emit mock products
- if browser-use runtime is unavailable in production, browse phase fails fast with explicit error

## Validation and Fallback

- all agent outputs must pass Pydantic schema validation
- validation failures trigger retries
- exhausted retries degrade to deterministic fallback
