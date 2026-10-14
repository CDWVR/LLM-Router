# Connect models and applications

## Bring a model into a workspace

Open **Workspace > Models** as an administrator:

1. Choose a provider template or an approved custom compatible endpoint. Add a connection name and API key. Saved keys are encrypted on the server and are never returned to the browser.
2. Check the connection and discover model IDs, or enter an upstream ID manually. Discovery lists IDs; it does not verify pricing, context limits, vision/tool capability or data residency. The check does not generate a paid answer.
3. Register a unique Router alias with the upstream model ID, context/output limits, prices per million tokens, estimated latency, quality and capabilities. Enable it when the metadata is correct.

Requests select only configured, enabled models meeting the request's constraints. Template model metadata is illustrative: operators must verify it against their provider contracts. Leaving a key blank preserves the saved key. Changing the destination requires a replacement credential to prevent accidental credential forwarding.

SaaS supports HTTPS destinations on built-in provider hosts or operator-approved `ROUTER_PROVIDER_HOSTS`. Loopback services are supported only in local mode. Domain allowlisting is not a substitute for network egress restrictions. To support another wire protocol, add a server adapter and contract tests; compatible endpoints need no code change.

## Call Router

Create a project API key in **Workspace > Account**. The service exposes `GET /v1/models` and `POST /v1/chat/completions`; authenticate with `Authorization: Bearer YOUR_PROJECT_KEY`.

```python
# Install the OpenAI client separately in your consuming application.
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8765/v1",  # Use your HTTPS domain in production.
    api_key=os.environ["ROUTER_PROJECT_KEY"],
)
response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Explain database indexes."}],
    max_completion_tokens=500,
    extra_headers={"Idempotency-Key": "unique-logical-request-id"},
    extra_body={"router": {"max_cost_usd": 0.05, "max_latency_ms": 20000}},
)
print(response.choices[0].message.content)
```

An explicit Router model alias selects that model subject to the same policy checks. Supply a new idempotency key for each logical request. Reusing it with the same body replays a stored completed result; a different body conflicts. Running or failed requests return a conflict rather than silently charging again. Retention is 90 days, so this is not permanent exactly-once delivery. After an ambiguous provider failure, reconcile before retrying with a new key.

## Supported subset

| Input | Behavior |
| --- | --- |
| `messages` | System, user and assistant text; up to 100 messages and bounded total text |
| `stream` | Boolean; SSE Chat Completions chunks, finish chunk and `[DONE]` on success |
| `max_tokens` / `max_completion_tokens` | Positive output cap, constrained by catalog and remaining budget |
| `response_format` | `json_schema`, validated locally; unsupported remote schema references rejected |
| `router` | `max_cost_usd`, `max_latency_ms`, `verify`; may narrow server limits |
| User image blocks | Inline base64 PNG/JPEG/WebP/GIF; remote image URLs rejected; bounded size and pixel count |
| `tools` | Function definitions; filters to tool-capable models; OpenAI and Anthropic adapters |
| Tool history | Assistant `tool_calls` and matching `tool` results; arguments must encode a JSON object |

Tool results are returned to the calling application for validation and execution. Router never executes functions. Tool responses are buffered even when `stream=true`; text-only responses can stream incrementally. Tool requests cannot be combined with answer verification or output schemas. Returned tool arguments are untrusted model output; validate the selected function, schema and permissions before executing it.

Unsupported request fields are rejected, including parameters outside this documented subset. This does not implement the full OpenAI API, Responses API, embeddings, audio, batch jobs or an Anthropic-compatible public endpoint. The Anthropic adapter translates internally to native Messages requests.

SDK streams cannot retract content. If a provider fails after publishing text, the stream ends with an error and no successful `[DONE]`, without switching to a second model. Clients must treat incomplete streams as failures. The web UI can clear partial output before retrying. Required verification and schema checks buffer output until they succeed.

## Budgets and usage

Project admission uses shared database transactions: at most three active generations and 30 starts per minute per project, with a rolling 24-hour reservation ceiling. Conversations also reject simultaneous generations in the same thread. Requests reserve their allowed budget before provider execution; retries consume the same request allowance. Provider timeouts can leave charges uncertain, so reservations are conservative.

`router.reserved_usd` is reserved budget, not a provider invoice. Token counts are returned when available; missing usage is null. Accurate billing still needs provider reconciliation. Catalog price and capability errors can invalidate estimates. The dashboard displays the latest 500 request records; its 24-hour reserved total covers all retained requests in that window.

Errors before streaming use a JSON error object with a safe code. Errors after headers are sent use SSE error frames. Provider credentials and raw provider error bodies are excluded from client error responses.
