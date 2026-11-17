# Trendwise chatbot API

HTTP service behind the Trendwise website's pre-sales chatbot. It answers
visitor questions **only** from the public website content (the corpus),
says "not stated, contact sales" when something isn't on the site, and flags
explicit lead intent for the backend. Every model answer passes a
deterministic output guard (leak patterns, URL whitelist, number checks)
before it is returned.

Endpoints: `POST /v1/chat`, `DELETE /v1/chat/{session_id}`, `GET /health`.
Full contract: [docs/API.md](docs/API.md).

```
app/
  main.py        create_app factory, routes
  settings.py    all runtime settings (pydantic-settings)
  api/           schemas, errors, session store, pipeline wrapper
  chat/          prompt, LLM client, output guard, leak patterns, corpus reader
tests/           API and guard tests (stub LLM, no network)
```

## Corpus

The service reads the cleaned website pages (one Markdown file per page,
with front matter) from `CORPUS_DIR`. The corpus is **not** in this repo:
it is produced by the separate ingestion tooling (crawl + clean +
leak check) and copied or mounted onto the server, read-only. Updating the
corpus needs a restart, not a rebuild. `GET /health` reports
`corpus_loaded` and `corpus_pages`.

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # fill in CHAT_API_KEY, GROQ_API_KEY, CORPUS_DIR, LEAK_*
uv run uvicorn --factory app.main:create_app --port 8000 --no-server-header
curl -s localhost:8000/health
```

Set `DOCS_ENABLED=true` in your local `.env` to get `/docs`.

Checks:

```bash
uv run ruff check .
uv run pytest
```

## Deploy

Docker image: Python 3.12 slim, runtime deps only, non-root, one uvicorn
worker (sessions are in memory, so run **one replica**).

```bash
# On the server, next to a filled-in .env:
CORPUS_DIR=/srv/trendwise/corpus/current docker compose up -d --build
curl -s localhost:8000/health    # expect corpus_loaded: true
```

`CORPUS_DIR` in that command is the host folder; it's mounted read-only at
`/app/data/clean`. The compose file runs the container read-only, forces
`DOCS_ENABLED=false` and reads secrets from `.env` at runtime (never baked
into the image).

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `CHAT_API_KEY` | yes | Shared secret the backend sends as `X-API-Key`. |
| `LLM_PROVIDER` | no | `groq` (default) or `openrouter`. |
| `GROQ_API_KEY` / `OPENROUTER_API_KEY` | yes | Key for the chosen provider. |
| `CORPUS_DIR` | yes | Cleaned corpus folder (see above). |
| `LEAK_INTERNAL_PORTS`, `LEAK_INTERNAL_NAMES` | staging/prod | Internal ports and storage/file names the guard blocks. Server `.env` only, never in git. |
| `CHAT_MODEL` | no | Default `openai/gpt-oss-20b`. |
| `PUBLIC_BASE_URL`, `SALES_EMAIL` | no | Used in citations and fallback replies. |
| `DOCS_ENABLED` | no | `false`; local development only. |

All options with defaults are in [.env.example](.env.example) and
[docs/API.md](docs/API.md#configuration). Secrets are never logged.
