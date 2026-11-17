# Chat API (v1)

A buffered (non-streaming) HTTP API in front of the Trendwise pre-sales chat
pipeline. Each request runs:

> load history → system prompt + corpus → LLM → output guard → save turn
> → return the guarded answer.

## Authentication

Every `/v1/*` request needs the header `X-API-Key: <CHAT_API_KEY>`. The key
is shared with our backend only; never expose it to browsers. A missing or
wrong key gets `401`. `GET /health` needs no key.

## Endpoints

### `POST /v1/chat`

Request:

```json
{"session_id": "visitor-7f3c", "message": "How much is the Professional plan?"}
```

| Field | Type | Rules |
| --- | --- | --- |
| `session_id` | string | 1–128 characters, not blank. Chosen by the backend; one per visitor conversation. |
| `message` | string | 1–2000 characters, not blank. |

Response `200`:

```json
{
  "session_id": "visitor-7f3c",
  "answer": "The Professional plan is $399 per month ... Sources: https://trendwise.io/pricing",
  "sources": ["https://trendwise.io/pricing"],
  "lead_intent": false,
  "request_id": "5f0c2b..."
}
```

| Field | Meaning |
| --- | --- |
| `answer` | The guarded answer: exactly what the visitor should see. If the output guard blocked the model's answer, this is a safe "contact sales / book a demo" reply. |
| `sources` | Public page URLs the answer cites, all from the corpus. Empty if the guard replaced the answer. |
| `lead_intent` | `true` only for explicit intent in this message: asking for a demo, a quote, to be contacted or to talk to sales/someone, or sharing an email, name or company. Pricing or feature questions alone stay `false`. A v1 keyword heuristic; use it as a hint to start the lead flow. |
| `request_id` | Also in the `X-Request-ID` header; quote it when reporting issues. |

### `DELETE /v1/chat/{session_id}`

Clears the session history (for example on "start over"). Returns `204`
whether or not the session existed. `session_id` is 1–128 characters.

### `GET /health`

No authentication.

```json
{"status": "ok", "corpus_loaded": true, "corpus_pages": 8}
```

If `corpus_loaded` is `false`, the service is up but `/v1/chat` returns
`503` until a corpus is available (it ships in the image, so this means a
misconfigured `CORPUS_DIR`). No model or provider information is
exposed.

## Errors

Every error body has the same shape and never contains stack traces, model
or provider names, provider error text, or internal hosts:

```json
{"error": "<code>", "message": "<safe text>", "request_id": "..."}
```

`/v1/chat` errors `429` and `503` also include `session_id` and a safe
`answer` you can show the visitor: "Sorry, I can't answer right now. Please
contact sales@trendwise.io or book a demo at https://trendwise.io/book-demo."

| Status | `error` | When | Client action |
| --- | --- | --- | --- |
| 401 | `unauthorized` | Missing or wrong `X-API-Key` | Fix the key. |
| 422 | `validation_error` | Field missing, empty, blank or too long; `details` lists the fields | Fix the request. |
| 429 | `rate_limited` | The model provider rate-limited us | Show `answer`; retry after the `Retry-After` header (seconds). |
| 503 | `unavailable` | Model error or timeout (45 s), or no corpus loaded | Show `answer`; retry later. |
| 500 | `internal_error` | Unexpected server error | Retry once; report the `request_id`. |

A failed turn is **not** saved to history.

## Limits and behaviour

| Limit | Value | Setting |
| --- | --- | --- |
| Message length | 1–2000 characters | fixed |
| Session id length | 1–128 characters | fixed |
| History kept per session | last 10 turns | `SESSION_MAX_TURNS` |
| Idle session expiry | 30 minutes | `SESSION_TTL_S` |
| Max time per `/v1/chat` | 45 s | `CHAT_TIMEOUT_S` |
| Upstream retries | none (fail fast to 429 or 503) | `LLM_MAX_RETRIES=1` |

- **Sessions are in memory (v1):** they're lost on restart, and the service
  must run as **one process / one replica**. A Redis store can replace it
  behind the same `SessionStore` interface.
- **Requests for the same session should be sequential.** Concurrent
  requests on one session can interleave history.
- **Throughput depends on the model provider tier.** On Groq's free tier,
  each turn uses about 5.9k of 8k tokens per minute, which allows about
  1 chat per minute. Plan for a paid tier before real traffic. The
  client-side throttle (`RATE_LIMIT_RPM`) can wait longer than
  `CHAT_TIMEOUT_S` under load, which shows up as a `503`.
- **Replies are English only.** Messages in other languages get an English
  "I can only help in English" reply (a `200`, not an error).

## Logging and tracing

Each request logs one JSON line:

- `request_id`, `session_id`, message length, latency;
- prompt, cached and completion tokens;
- the guard result (blocked or not, and violation *types*), `lead_intent`;
- the error class, on failure.

Message text and API keys are never logged. A `Tracer` hook receives the
same event for Langfuse later. The default is a no-op.

## Configuration

Set these via environment or `.env` (see `.env.example`):

| Variable | Required | Default |
| --- | --- | --- |
| `CHAT_API_KEY` | yes | n/a |
| `LLM_PROVIDER` | no | `groq` (or `openrouter`) |
| `GROQ_API_KEY` or `OPENROUTER_API_KEY` | yes, for the chosen provider | n/a |
| `CORPUS_DIR` | no | the repo's `corpus/`; `/app/corpus` in the image (baked in) |
| `LEAK_INTERNAL_PORTS`, `LEAK_INTERNAL_NAMES` | yes on staging/prod | n/a. Internal ports and storage / file names the output guard must block, comma-separated. They live only in the server's `.env`, never in git. Without them, only the generic checks (private IPs, loopback, canary) apply. |
| `CHAT_MODEL` | no | `openai/gpt-oss-20b` |
| `PUBLIC_BASE_URL` | no | `https://trendwise.io` (used in citations; unconfirmed) |
| `SALES_EMAIL` | no | `sales@trendwise.io` |
| `DOCS_ENABLED` | no | `false`. `true` serves `/docs` and `/openapi.json`; local development only. The Dockerfile and docker-compose force it to `false`. |

## curl examples

```bash
BASE=http://localhost:8000
KEY=local-dev-key

curl -s $BASE/health

curl -s -X POST $BASE/v1/chat \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"demo-1","message":"How much is the Professional plan?"}'

# Follow-up in the same session (history is used)
curl -s -X POST $BASE/v1/chat \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"session_id":"demo-1","message":"And if I pay yearly?"}'

# Start over
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE $BASE/v1/chat/demo-1 \
  -H "X-API-Key: $KEY"

# Errors: 401 (no key), 422 (empty message)
curl -s -X POST $BASE/v1/chat -H "Content-Type: application/json" \
  -d '{"session_id":"demo-1","message":"hi"}'
curl -s -X POST $BASE/v1/chat -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" -d '{"session_id":"demo-1","message":""}'
```
