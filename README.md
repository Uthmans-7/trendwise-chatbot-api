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
corpus/          cleaned website pages the bot answers from (8 files)
tests/           API and guard tests (stub LLM, no network)
```

## Corpus

The bot answers only from `corpus/`: the cleaned public website pages, one
Markdown file per page with front matter (`url`, `path`, `title`,
`description`, `crawled_at`). It is committed here and baked into the Docker
image, so no volume mount is needed. `GET /health` reports `corpus_loaded`
and `corpus_pages`. `CORPUS_DIR` can point at another folder if ever needed;
by default it's the repo's `corpus/` (`/app/corpus` in the image).

The files are produced by the separate R&D ingestion tooling (crawl, clean,
leak check); only cleaned files belong here, never raw crawl output.

**To update the corpus:**

1. Re-crawl and clean in the R&D repo (`src.ingest.crawl`, then
   `src.ingest.clean`, which runs the post-ingestion leak check).
2. Copy the cleaned files (`data/clean/*.md`) into `corpus/` here,
   replacing the old ones.
3. Run the leak check again on `corpus/` (internal IPs, ports, hosts,
   bucket/video names, canary, fake demo data) and review `git diff corpus/`.
4. Run `uv run pytest`, commit, rebuild and redeploy the image.

## Run locally

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env     # fill in CHAT_API_KEY, GROQ_API_KEY, LEAK_*
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

Docker image: Python 3.12 slim, runtime deps and the corpus only, non-root,
one uvicorn worker (sessions are in memory, so run **one replica**).

```bash
# On the server, next to a filled-in .env:
docker compose up -d --build
curl -s localhost:8000/health    # expect corpus_loaded: true, corpus_pages: 8
```

The compose file runs the container read-only, forces `DOCS_ENABLED=false`
and reads secrets from `.env` at runtime (never baked into the image). Don't
set `CORPUS_DIR` in the server `.env` unless you also mount that folder.

## Environment variables

| Variable | Required | Notes |
| --- | --- | --- |
| `CHAT_API_KEY` | yes | Shared secret the backend sends as `X-API-Key`. |
| `LLM_PROVIDER` | no | `groq` (default) or `openrouter`. |
| `GROQ_API_KEY` / `OPENROUTER_API_KEY` | yes | Key for the chosen provider. |
| `CORPUS_DIR` | no | Default: the repo's `corpus/` (`/app/corpus` in the image). |
| `LEAK_INTERNAL_PORTS`, `LEAK_INTERNAL_NAMES` | staging/prod | Internal ports and storage/file names the guard blocks. Server `.env` only, never in git. |
| `CHAT_MODEL` | no | Default `openai/gpt-oss-20b`. |
| `PUBLIC_BASE_URL`, `SALES_EMAIL` | no | Used in citations and fallback replies. |
| `DOCS_ENABLED` | no | `false`; local development only. |

All options with defaults are in [.env.example](.env.example) and
[docs/API.md](docs/API.md#configuration). Secrets are never logged.
