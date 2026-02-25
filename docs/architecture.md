# Architecture

## Stack

- Backend: FastAPI
- Workflow engine: LangGraph state machine
- UI: FastAPI template + lightweight JS adapter
- Session memory: in-process (`user_profile`)

## Agent Graph

Initial design flow:

`collect -> design -> review -> present`

- `present` returns 3 outfits and waits for user decision.

Modify flow (user chooses one outfit to modify):

`collect -> review (feedback_to_suggestions) -> design -> review -> present`

Search flow (user chooses one outfit to search):

`collect -> plan -> browse -> present`

## Search Planning

Planner emits deterministic per-item steps:

1. `hover_menu`
2. `click_category`
3. `click_subcategory`
4. `scroll_to_filters`
5. `select_color_filter`
6. `select_best_product`
7. `select_product_color`
8. `capture_left_image_screenshot`

## Observability

Each phase appends trace records with:

- status
- input summary
- output summary
- latency
- error type
- artifact refs

SSE exposes phase transitions and final `done` signal.
