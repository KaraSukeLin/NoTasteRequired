# NoTasteRequired

NoTasteRequired is a FastAPI + LangGraph workflow for menswear outfit generation and product search.

## Demo

![NoTasteRequired demo](docs/demo.gif)

## Workflow

- First design run: `collect -> design -> review -> present`
- Search run after outfit selection: `collect -> plan -> browse -> present`

## Requirements

- Python `3.11+`
- Playwright Chromium runtime

## Setup

1. Install dependencies:

```bash
python -m pip install -e .
```

2. Install Chromium for Playwright:

```bash
python -m playwright install chromium
```

3. Apply for API keys:

- `GROQ_API_KEY`
  - Sign up / log in: `https://console.groq.com/landing/try-groq`
  - Create key: `https://console.groq.com/keys`
- `BROWSER_USE_API_KEY`
  - Sign up / log in: `https://cloud.browser-use.com`
  - Browser Use Cloud docs point to the cloud dashboard for key creation.

4. Create `.env` from `.env.example`:

```powershell
Copy-Item .env.example .env
```

Unix/macOS alternative:

```bash
cp .env.example .env
```

## Environment Variables

Current minimal `.env.example`:

```bash
GROQ_API_KEY=
BROWSER_USE_API_KEY=
```

Behavior:

- `GROQ_API_KEY` empty: agents use deterministic fallback instead of Groq.
- `BROWSER_USE_API_KEY` empty: `browse` phase fails by default.

## Run

```bash
python -m uvicorn app.main:app --reload
```

Open:

- UI: `http://127.0.0.1:8000/`
- Health: `http://127.0.0.1:8000/api/healthz`

## API

- `POST /api/sessions`
- `POST /api/sessions/{session_id}/turn`
- `GET /api/sessions/{session_id}/runs/{run_id}/events` (SSE)
- `GET /api/sessions/{session_id}/runs/{run_id}/result`
- `GET /api/healthz`

## Runtime Notes

- Session and memory data are in-memory only.
- Restarting the service clears all sessions.
- Static app/model/brand/prompt defaults are now in code:
  - `app/config_defaults.py`
