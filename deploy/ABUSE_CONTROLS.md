# Request abuse controls

SaaS enables shared database-backed fixed-window throttles before dynamic requests reach authentication or application handlers. Local development skips HTTP throttling. These controls complement the per-project generation admission limits; they are not a substitute for an edge WAF or volumetric denial-of-service protection.

| Group | Limit per 60-second window |
| --- | --- |
| Login and OIDC callback combined | 20 per client IP |
| Dynamic non-GET requests | 60 per client IP |
| Other dynamic GET requests | 180 per client IP |
| Provider discovery | 5 per workspace |

Static assets and liveness/readiness probes are excluded. Rejected HTTP requests return 429 with `Retry-After: 60`; if database admission is unavailable they return 503 with a short retry delay. Callers should back off and add jitter. Fixed-window boundaries can allow up to two windows' quota in a short burst; stricter burst control belongs at the edge.

Counters use atomic database upserts, so concurrent requests and application replicas share admission state. Client identities use an HMAC with the deployment secret; raw IPs are not saved in the counter table. Each request group has at most 65,536 hash buckets. A hash collision conservatively shares a quota between clients. This intentionally bounds unauthenticated storage growth, but may create false-positive throttles at scale. Monitor rejection rates and migrate to a dedicated limiter when traffic justifies it.

## Reverse proxy trust

By default, the limiter uses the direct connection's peer IP and ignores all forwarded headers. Behind a proxy this means clients share the proxy's quota until trust is configured.

Set `ROUTER_TRUSTED_PROXY_CIDRS` to the specific IP addresses or controlled network ranges of your reverse proxies. Only connections from these addresses may supply `X-Router-Client-IP`. The included Caddy configuration overwrites that header using the connection's remote address. Other forwarded headers are ignored by the limiter. Invalid or duplicate identity headers fall back to the direct peer address.

Do not trust all addresses (`0.0.0.0/0` or `::/0`) or a subnet containing untrusted workloads. Keep application ports private, and ensure the trusted proxy overwrites incoming client identity headers. For compose, reserve an internal proxy address/network appropriate to your infrastructure and set the matching CIDR; the sample deliberately does not assume your Docker subnet. If a CDN precedes Caddy, configure and test that trust chain explicitly; the default sees the CDN peer address.

## Pause generation

Set `ROUTER_GENERATION_ENABLED=0` and recreate/restart every app worker/container. New web, API and comparison generations receive 503 without starting a model call. Existing in-flight work is not automatically cancelled. Conversations, exports and administration remain available. Set it back to `1` and restart to resume. Any value other than `1` pauses generation, so a typo fails closed.

## Deployment validation

Run migrations before rolling out the limiter (schema revision 3). Verify that two replicas share quotas, spoofed headers from untrusted clients do not change identity, the intended proxy passes the correct address, health checks remain available and limiter database failures deny requests. Configure edge request/connection ceilings, signup restrictions and alerting before opening public traffic. Authenticated per-user limits and risk-based abuse detection remain future improvements.
