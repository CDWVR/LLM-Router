# Production LLM router

Implementation status (2026-10-14): the default app is now an ASGI service with OIDC sessions, project-scoped PostgreSQL storage, encrypted provider credentials, per-project admission budgets, model discovery, shared health records, and a streaming API. See [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) for verified behavior and remaining release gates. The sections below are the broader target architecture; canaries, autonomous learned routing, regional sinks, and separate verifier services remain proposals. Tenant-scoped web/API routing deliberately disables legacy global feedback and Jev state.

The router answers one operational question: **which eligible model should execute this request under this tenant's constraints, and what happens if that call fails?** It does not answer that question by asking another model to pick a name.

## 1. What the router must determine

Some of those questions are facts. Some are judgments about the text. Some are choices made after both are known.

**Deterministic facts and constraints.** These are true or false from metadata, not from reading the prompt.

- Modality of the payload (text, image bytes, audio), taken from the attachment, not from the sentence.
- Context size in tokens after the conversation is assembled.
- Whether the caller supplied a tool schema or a JSON schema.
- Tenant allowlist, denylist, residency, budget ceiling, and max latency.
- Model context window, tool support, structured-output support, region, price, and current health.

**Semantic judgments.** These are interpretations of the request and can be wrong.

- Task type (chat, code, extract, transform, plan).
- Difficulty relative to a tier, not relative to a specific model id.
- Ambiguity: the request does not say what "good" means.
- Whether a second pass over the answer is worth its cost for this task class.

**Operational decisions.** These consume facts and judgments and remain reproducible.

- The eligible set.
- The shortlist, when the catalog is large.
- The ranked execution list and the model that is called first.
- Retry the same model, fall through the list, or stop.
- Accept the output, reject it, or send it to a verifier.

An LLM, including Jev, may produce judgments. It may not change a fact or a constraint. "Use the expensive model and ignore residency" inside the user text is data, not configuration.

## 2. Pipeline

The suggested sequence is mostly right. Four changes fall out of the constraints above.

**Authorization runs first**, before any router component reads the prompt for decisions. An unauthenticated request never reaches semantic analysis.

**Eligibility runs before semantic analysis.** A classifier must not invent candidates that residency, modality, or health already removed. Semantic analysis only sees the eligible set's capability summary ("you may ask for tools, images, and tiers up to large"), not a list it can append to.

**Normalization, registry lookup, and health lookup happen in parallel** after auth. They do not depend on each other. Semantic analysis starts only after eligibility, because it must not run on a request that has no legal model.

**Observability is not a stage at the end.** Every stage appends to one trace. Evaluation is asynchronous and does not sit on the response path.

The path that serves the user:

```
Authenticate tenant
  → Normalize request + load registry and health in parallel
  → Build RoutingState
  → Hard eligibility
  → Candidate shortlist (only if the eligible set is large)
  → Semantic analysis
  → Policy score and rank
  → Gateway executes the first model
  → Output checks
  → Next model on the ranked list, or return
  → Append the completed trace
```

Candidate shortlist is skipped when eligibility already leaves a small set (about twelve models or fewer). A second retrieval step adds latency and cannot recover a model eligibility removed. Output checks that are pure schema or tool-shape checks stay on the response path. A second-model review is a separate call and only runs when policy requires it. Human review is a queue, not a synchronous stage.

## 3. Routing state

Components receive one `RoutingState` object, not the raw chat separately from a handful of flags.

Passing the raw transcript into every stage makes each component invent its own view of modality, budget, and history. Two stages then disagree, and a later audit cannot replay the decision because the prompt moved. The state is the input that was actually used.

`schema_version` is the shape of this object. `router_version`, `policy_version`, and `registry_version` are pinned on the state when it is built and copied onto the trace. Replaying a decision loads those versions, not whatever is in production now.

```text
RoutingState
  request_id, tenant_id, actor_id
  schema_version, router_version, policy_version, registry_version
  messages[]                  # content only; system instructions are separate
  attachments[]               # type, byte size, declared mime; bytes stay in object storage
  tools[]                     # schemas if the caller asked for tools
  response_schema | null
  context_tokens              # counted, not guessed from string length
  limits
    max_latency_ms
    max_cost_usd
    residency_regions[]
  conversation
    turn_index
    previous_model_id | null
    previous_image_ref | null
  control                     # NOT parsed from user text
    allow_model_ids[] | null
    deny_model_ids[]
    require_verification: bool
```

User text lives in `messages`. Constraints live in `limits` and `control`, filled from the tenant record and the API envelope. The normalizer copies attachments into `attachments` from the multipart body. It does not scan the prompt for the word "image" and set modality from that.

## 4. Hard eligibility

Eligibility is application code over the registry, the health snapshot, and `RoutingState.limits`. It returns two lists:

- `rejected[]` with a reason code. `unsuitable` means the model cannot do the job (no image input, context window smaller than `context_tokens`, no tool calling, no JSON schema). `not_permitted` means a rule forbids it (tenant denylist, residency, budget ceiling below the model's minimum price for this context, provider not contracted).
- `eligible[]` model ids.

Unsuitable and not permitted are different because they are fixed differently. Unsuitable is model metadata. Not permitted is tenant policy. A dashboard that mixes them hides whether the catalog is missing a capability or the tenant is blocked.

Jev and any other semantic model receive the eligible ids and cannot add to them. If they recommend a tier that no eligible model occupies, policy ignores the tier and uses the eligible set.

This layer is deterministic because the checks are comparisons: window greater than or equal to tokens, region in the allowed set, health status not `open`, price times estimated tokens under the ceiling. There is no accuracy benefit in asking a language model to apply those comparisons, and a wrong answer would route data to a region or a model the tenant forbade.

If the eligible set is empty, the gateway is not called. The API returns a typed error (`no_eligible_model`) and the rejection reasons. It does not "try something anyway."

## 5. Model registry

One registry is the catalog. Stages do not hardcode model ids in routing branches.

**Static, changed by a reviewed deploy of the registry version.** Provider, model id, version, modalities, context window, tool support, structured-output support, regions where the provider will run it, price table, and the capability tags the vendor actually supports (`vision`, `tools`, `json_schema`). These change when a contract or a model card changes, not when a request arrives.

**Dynamic, written by workers, and not a live quality score.** Health (closed / half-open / open circuit), recent error rate, and p50 and p95 latency. A tenant's circuit is that tenant's errors plus operator probes. One tenant's failures do not open the circuit for another tenant. Half-open admits a small probe quota, not every request. A stale health snapshot older than its TTL is unknown, and unknown health removes the model from eligibility.

Task-success rates are not written into the live registry. They live in an immutable `quality_table_version` published by a batch job from a closed time window. The request path reads a table only when the pinned policy names that version.

**Not stored as routing rules.** There is no row that says "coding goes to model B." The registry stores capabilities and measured outcomes. The policy version stores how those numbers are weighed. That split is what keeps the registry from becoming a second, untested copy of the router.

Model-card text may be stored for humans and for optional embedding prototypes. It is not a policy.

## 6. Candidate discovery

Eligibility is already a filter. Discovery is only the step that shrinks a still-large eligible set to something policy can score without a large latency tax.

For a catalog the size of the current app (three models, one provider), discovery does nothing. The eligible set is the candidate set.

When the eligible set is large, discovery uses **metadata already on the registry**, then embeddings only as retrieval:

1. Drop models whose capability tags do not cover the request's known requirements (image attachment, tools, JSON schema). This is the same class of check as eligibility and is redundant if eligibility already applied those tags. It exists here only as a fast path over a cached eligible subset.
2. If more than about twelve models remain, embed the user text and retrieve the nearest **task prototypes**, not marketing model cards. Prototypes are short example requests labeled with capability tags (`chat`, `code`, `extract`). A model stays in the shortlist when its tags intersect the retrieved labels. Similarity never inserts a model eligibility removed, and it never deletes a model that is the only one left with a required tag.

**What cosine similarity can establish.** The request wording is nearer to one set of example phrases than another. That is a hint about task family.

**What it cannot establish.** Permission, health, price, latency, context window, whether the answer will be right, or which model id should run. Two model cards that both say "reasoning and code" embed almost on top of each other; the current router measured that, and the nearest card was then treated as the winner. In this design the nearest card is not a winner. It is a label used to narrow tags.

Learned retrieval can replace prototype search later, using the same contract: input is eligible ids, output is a subset, models outside the eligible set cannot appear.

## 7. Semantic analysis and where Jev sits

Jev is TypeSafe's first System One model, not a chat model. A chat model writes text and the application parses it. Jev does not write text. The caller sends **state** plus **questions whose answer types are fixed in advance**, and Jev returns a value of that type with a probability distribution. TypeSafe names this Reinforcement Learning for Calibrated Decisions: when the model says 0.8, that figure is meant to be right about 80% of the time, and that claim still has to be checked on this router's own labels. Published surfaces include OpenRouter (`typesafe/jev-1.13`, Decisions API or System One API) and Vercel AI Gateway (`typesafe-ai/jev`). The context window cited for the state plus the questions is 32,000 tokens. Gateway pricing published alongside the model is input tokens only. Confirm the current price and model id at deploy time. Jev is not a gateway: it does not hold provider keys and it does not call the execution model.

The question types the product actually returns:

- **Choice.** One key from a map the caller wrote. Each key has a description. The response is the chosen key, a probability for every key, and a confidence. It cannot return a key that was not in the map.
- **Score.** An ordered rubric the caller wrote, a small number of levels. The response is the level and a probability for each level.
- **Boolean.** A probability that a statement is true. There is no free-text explanation.

Questions in one request are evaluated independently, so adding a question does not rewrite the others. The option lists and rubrics are the policy. Changing a route is editing that text and shipping a new `analyzer_prompt_version`, not parsing a paragraph.

The assessment the rest of the router consumes is assembled from those answers, not from a JSON document the model composed:

- Choice `task_type` over tags the router already knows (`chat`, `code`, `extract`, `transform`, `plan`).
- Score `complexity` on a fixed rubric from low to high.
- Boolean `needs_verification`.
- The confidence and the probability of the selected choice are stored separately. Low confidence means "do not act on this choice." A selected probability near 0.5 means the options were tied.

**Where Jev may name a model.** TypeSafe's own router pattern, including LangChain `ModelRouterMiddleware`, is a Choice whose options are routes you listed, each with a criteria string, plus an instruction such as "choose the least costly model that can complete the task." That is a legal use here only when the options are the eligible model ids for this request, built after eligibility, and each description is registry text plus the policy instruction. Jev then picks among models the deterministic layer already allowed. A model that failed residency or health is not in the map, so it cannot be chosen. If confidence or the selected probability is below the policy threshold, the choice is discarded and the score function in section 8 ranks the eligible set instead.

**Where Jev must not be used.** It does not decide residency, budget, health, or tenancy. It does not receive `previous_model_id`. It does not judge the finished answer. Verification stays a separate model with a rubric, because Jev's evidence is the request, not the generation.

**Options, now that the product is known.**

| Approach | What it returns | Failure | Why it loses or wins |
|---|---|---|---|
| Heuristics | Facts already on the envelope | Rare | Wins for attachments, tools, and schemas. A short prompt is not a task type. |
| Embedding vs prototypes | Nearest tag | Embed outage | Retrieval only. The current app used this as the whole decision and near-duplicate cards tied. |
| Small LLM plus JSON parse | Generated text matching a schema | Timeout, invalid JSON, fake confidence | Does the same job as Jev with worse calibration and a parser. |
| Jev Choice / Score / Boolean | Typed answers and distributions | Timeout, transport error | The semantic layer. The option set is the constraint. |
| Learned classifier later | Same typed answers | Missing artifact | Replaces Jev when traces exist. Policy does not change. |

The request deadline is `max_latency_ms` from ingress. Jev's timeout is the minimum of 200 ms and the time left after reserving a minimum gateway call. If that remainder is too small, skip Jev. On timeout or a transport error, heuristics fill factual capabilities only, the shortlist is discarded, and `analysis_source` is `fallback_heuristic`. The request still executes. User text and tool descriptions travel as `state`. Instructions and option maps are router-owned and versioned. Envelope facts are written over the assessment after the call, so a Choice cannot remove "this request has an image." `needs_verification` may add a check and cannot clear `control.require_verification`.

## 8. How the policy chooses a model

Policy is a pure function:

```text
select(state, eligible, assessment, registry_snapshot) -> ranked model ids
```

It does not call the network.

Each eligible model gets a score from registry facts plus the assessment:

```text
expected_quality(model, task_type)
  - w_cost * estimated_cost
  - w_latency * expected_latency_ms
```

Models that violate a soft preference are penalized, not deleted. Deletion already happened in eligibility. Example: the assessment asks for tier `large`, but a `standard` model has equal measured success on this `task_type` at half the price. The cheaper one ranks first. The large model stays on the list as the escalation target.

`expected_quality` starts as a prior from the registry (tier and published benchmarks for that task family). After the eval store has a minimum sample count, the prior is replaced by the measured success rate for `(task_type, model_id)`. That is how "coding → model A" stays out of the source code. The mapping is a table of outcomes keyed by policy version. Changing it is a policy deploy, not a code branch.

Weights `w_cost` and `w_latency` live in the policy version and can differ by tenant plan. A research tenant can weight quality higher. A high-volume tenant can weight cost higher. Both still share eligibility, so neither can buy a model outside residency.

Contextual bandits and a learned score model are optional replacements for `expected_quality`, deployed in shadow and then canary. They read the same features and emit the same ranked list. They do not replace eligibility or the gateway.

The first model on the list is the one the gateway calls. The rest are the fallback order for this request.

## 9. Cost, quality, and latency

Optimization happens **inside the policy score**, after eligibility and after the assessment exist. Doing it earlier would drop a cheap model that is the only healthy one, or would spend ranking time on models residency already forbids. Doing it only after generation wastes the expensive call.

Hard constraints are not weights. A model over the budget ceiling or over the latency SLO is ineligible, so it cannot win on quality. Among models that fit, the score trades quality against cost and expected latency. The highest-quality model wins only when the tenant's weights say so and the cheaper models have worse measured success.

Estimated cost uses the price table and `context_tokens` plus a capped output estimate from the assessment (`quality_floor` maps to a max output token default in the policy, not an unbounded guess).

## 10. Execution gateway

The gateway accepts `(model_id, RoutingState, messages)` and returns a normalized result: text or structured payload, usage, provider latency, and a typed error.

It owns provider HTTP, credentials, timeouts, and streaming translation. It retries transport failures once on the same model, and only when the time left and the remaining budget cover the worst-case cost. A 429 is not a burst: honor `Retry-After` only if it fits the deadline, otherwise skip every model from that provider for this request. The router then tries at most one next model on the ranked list. A second failure stops with `all_models_failed`. The gateway aborts the stream on deadline, cost cap, or client disconnect, and returns a typed error code plus a provider request id. Provider bodies and headers never enter the trace or the client payload.

Circuit breakers update the registry's dynamic health. Secrets stay in the gateway's environment. The router process does not hold provider keys, so a prompt cannot ask the router to print them, and a policy bug cannot exfiltrate them through the trace.

Provider differences (image parts, tool schema shape, JSON mode) are adapters behind one internal request type. Adding a provider is an adapter and a registry version, not a change to scoring.

## 11. Output validation

Routing confidence is the assessment's `confidence` plus how large the score gap was. It means "we are sure which model we wanted." It does not mean the answer is true.

Checks, in order, only when the request needs them:

1. **Schema.** If `response_schema` was set, parse and validate. Deterministic. Failure triggers the next model on the list once, then a typed error. This is the common case.
2. **Tool calls.** Names and arguments must match the supplied tool schema. Deterministic.
3. **Policy scanners** for disallowed data leaving the tenant boundary, if the tenant enabled them. Separate service. Not Jev.
4. **Verifier.** Policy arms it from `control.require_verification`, the tenant plan, and task-class rules in the policy version. `recommend_verification` may add a check and cannot clear a required one. The rubric comes from the policy, not from Jev's task label. The verifier is a registry role: a different model id from the execution model and from Jev, already eligible for residency, and not on this request's ranked list. It runs only if its worst-case cost and the deadline still fit. Timeout or invalid verifier output is not retried: required verification returns `verification_unavailable`, optional verification returns the draft and records the skip. A reject returns `verification_rejected` and does not call another generator. The caller receives the execution model's text, never the verifier's write-up.
5. **Human review** is an async queue for tenants that require it. The sync path returns a pending status rather than blocking the router thread on a person.

## 12. When something fails

| Failure | Behavior |
|---|---|
| Auth | 401 or 403. No model call. |
| Registry unavailable | Serve the last signed snapshot if it is inside TTL, and run the same eligibility function against it. Past TTL, the tenant's pinned fallback is used only if that same function says it is eligible. Allowlist membership alone is not permission. Otherwise fail with `router_error` or `no_eligible_model`. |
| Health unknown | Treat the model as ineligible. |
| Embedding service down | Skip shortlist. Score the full eligible set. |
| Jev down, slow, or confidence below threshold | Discard the Choice. Heuristic capabilities only, then policy scores the eligible set. `analysis_source=fallback_heuristic`. Continue. |
| Low Jev confidence | Same heuristic path. Record both outputs on the trace. |
| Policy throws | Do not guess. Use the tenant fallback model if it is eligible, else return `router_error`. |
| Selected model times out or 5xx | One gateway retry, then the next ranked model. |
| Every ranked model fails | `all_models_failed` with each provider error. No unranked model is attempted. |
| Schema validation fails | One escalation to the next ranked model. Then `invalid_output`. |
| Empty eligible set | `no_eligible_model` plus reason codes. |

The conservative path when intelligent pieces are down is always: **cached eligibility, heuristic assessment, tenant fallback model that is still eligible, gateway.** The request does not fail only because Jev or the embedder is down.

Conflicting signals (Jev says `large`, history says the small model succeeds at this task) are resolved by the score, not by Jev. The disagreement is a trace field so evaluation can see it.

## 13. Observability

One append-only trace per request, written at the end and also checkpointed before the provider call so a crash mid-flight is visible.

Fields: `request_id`, state `schema_version`, `router_version`, `policy_version`, `registry_version`, eligible ids, rejection reason codes, shortlist ids or "skipped", assessment JSON, `analysis_source` (`jev`, `heuristic`, `learned`), Jev model version if called, confidence, score breakdown per candidate, chosen model, fallback hops, gateway latency, tokens, estimated and actual cost, validation result, error codes.

The prompt body is stored only if the tenant's retention policy allows it. The trace always stores a hash of the normalized state so two runs can be compared even when the text is dropped.

A developer answers "why this model?" by reading reason codes, the assessment, and the score breakdown. Nothing required for that answer lives only inside a provider's hidden chain of thought.

## 14. Evaluation and improvement

Offline, before a policy or classifier ships: a fixed set of requests with expected **constraints** (must be eligible, must not be eligible) and expected **outcomes** (task succeeded under a rubric). Routing metrics: constraint violations (must be zero), unnecessary use of a more expensive model when a cheaper eligible model also passed the rubric, fallback rate. Economic: cost per success. Latency: router time and end-to-end time separately, so a slow model is not blamed on Jev.

Online: the same metrics on sampled production traces. Where privacy policy allows, shadow-score the runner-up model without returning it, or occasionally run it (canary) to get a counterfactual success rate. That is how the system learns it routed "plausibly" but not better.

The loop is offline:

```text
trace → labeled outcome → dataset → new policy or classifier version
  → offline eval must beat the last version on constraint violations and cost per success
  → shadow in production
  → canary
  → full traffic
  → rollback by pinning the previous policy_version
```

Production routers do not rewrite their own weights on the request path. A learned router that updates live can lock in a bad day of provider errors as "this model is bad." Updates are batch jobs with a minimum sample count and a human or automated promotion gate.

That dataset later trains the learned classifier that can retire Jev, calibrates confidence, and replaces quality priors. Regression detection is the offline suite running on every policy pull request.

## 15. Security and governance

- Tenant id comes from the authenticated token. The body cannot set `tenant_id`.
- Allowlists, denylists, residency, and budget come from the tenant record. Sentences in the prompt are not parsed for these.
- Provider credentials exist only in the gateway.
- Traces redact attachments by default and follow the tenant retention period.
- Policy and registry versions are immutable once published. Rollback is a pointer change.
- The user text is `state`. The questions, option keys, and rubrics are router-owned. Jev can return only those keys. A user instruction to emit some other model id has no field to land in. The application still drops a Choice whose confidence or selected probability is below the policy threshold, and it checks that every returned key was in the request.

## 16. Component catalog

**Edge auth.** Owns identity and tenant binding. Consumes the credential. Produces `tenant_id` and the tenant control record. This does not belong in the model gateway, because the gateway's job is a provider call and it must not decide whether the caller is allowed to spend. On failure the request stops.

**Normalizer.** Owns a single `RoutingState`: token count, attachment metadata, tool and response schemas, limits copied from the tenant. Consumes the HTTP request and the tenant record. Produces the state. This does not belong to Jev, because modality and budget are facts, and a semantic model will sometimes "see" an image that is not there or obey a fake budget written in the prompt. On failure, reject the request as malformed. Do not continue with a partial state.

**Registry.** Owns catalog facts and the latest health and quality stats. Consumes deploys and worker updates. Produces a versioned snapshot. This does not belong in application code as constants, because health and price change on different clocks from deploys. On failure, use a fresh signed cache or the fallback path in section 12.

**Health worker.** Owns probes and circuit state. Consumes provider errors from the gateway and active checks. Produces dynamic registry fields. This does not belong in the request path, because a probe on every call adds latency and couples one tenant's timeout to everyone else's view of health. On failure, health stays at the last good sample until TTL, then models become ineligible.

**Eligibility.** Owns the suitable versus permitted split. Consumes state, registry snapshot, health. Produces eligible ids and reason codes. This does not belong to policy scoring, because a weighted score can be tuned to "almost" allow a forbidden region. On an empty set, stop with `no_eligible_model`.

**Candidate retrieval.** Owns shrinking a large eligible set. Consumes eligible ids and prototypes. Produces a subset. This does not belong to the policy scorer, because scoring fifty models with a slow quality model wastes the latency budget, and it does not belong to eligibility, because a near-synonym in the prompt is not a reason to forbid a model. On failure, pass the eligible set through unchanged.

**Semantic analyzer (Jev or its replacement).** Owns the assessment schema for ambiguous text. Consumes user text plus the capability summary of what is eligible. Produces the JSON assessment. This does not belong to the gateway or the policy function, because those must stay deterministic and replayable. On failure or low confidence, heuristics produce the same schema and the trace records the swap.

**Policy.** Owns the ranked list. Consumes state, eligible ids, assessment, registry stats, policy version. Produces ranked model ids and a score breakdown. This does not belong to Jev, because Jev does not know live health, price, or residency and must not learn them from the prompt. On failure, tenant fallback if eligible, otherwise `router_error`.

**Gateway.** Owns one reliable provider call. Consumes a model id and the messages. Produces a normalized result or a typed transport error. This does not choose a different model id on its own except the single same-model retry. Choosing the next model is the router's use of the ranked list, so retries stay visible on the trace. On exhaustion of the list, `all_models_failed`.

**Output checks.** Own schema, tool shape, and optional verification. Consume the provider payload and the request's schemas. Produce accept or a typed rejection. This does not belong to the router score, because a confident route can still return broken JSON. On rejection, one escalation, then `invalid_output`.

**Trace log.** Owns the audit record. Consumes each stage's structured fields. Produces an immutable trace keyed by `request_id`. This is not a debug print inside Jev, because a vendor log cannot be replayed against a policy version. On failure to write, the user response still returns if the call succeeded, and a local spill queue retries the write. Losing an audit record is an alert, not a silent skip.

**Eval worker.** Owns offline comparison and promotion of policy versions. Consumes traces and labeled sets. Produces a pass or fail against the last version. This does not run on the request thread. On failure, the current policy version stays pinned.

## 17. Request lifecycle

1. Authenticate. Load tenant limits.
2. Normalize into `RoutingState` at the current schema version. Count tokens. Record attachment types from the body.
3. Load registry version and health. Run eligibility. Persist reason codes on the trace.
4. If more than about twelve models remain, prototype retrieval shortlists them. Otherwise the eligible set is the candidate set.
5. If tools, schema, or an image already fix the capability tags, fill the assessment with heuristics. Otherwise call Jev with a timeout. Validate JSON. On any failure, heuristics.
6. Policy scores candidates and writes the breakdown.
7. Gateway calls the first id. On transport failure, retry once, then the next id.
8. Schema and tool checks. If they fail, try the next id once.
9. Optional verifier when required.
10. Return the answer and flush the trace. Eval jobs read the trace later. They do not block step 9.

## 18. Deployment

- **Router service:** stateless, horizontally scaled. Holds no provider secrets. Pins `policy_version` by config so rollback is a config change.
- **Registry:** versioned rows in a database, read through a short-lived cache on the router. Writes from deploys (static) and the health worker (dynamic).
- **Gateway:** separate service or a separate module with its own credentials and network policy. Circuit-breaker updates go back to the registry.
- **Jev:** external dependency behind the analyzer client, with a timeout and a breaker. The breaker opening switches every request to heuristics until Jev recovers.
- **Trace store:** append-only. Router spills to disk if the store blips.
- **Eval workers:** scheduled, not in the serving cluster's request path. They publish a promotion record and leave the pin unchanged. A separate promotion step accepts that record before the pin moves.
- **Secrets:** gateway only. Every component that receives message text or attachment bytes is region-scoped to the tenant, including Jev, embeddings, the verifier, shadow calls, object storage, and the trace store. If no in-region analyzer exists, the router uses heuristics and does not send text to Jev. The gateway still refuses to open a socket for a model id outside its region.

## 19. What each mechanism is for

**Facts (normalizer, registry, health, eligibility).** What is true about the payload, the contract, and the provider right now.

**Embeddings.** Optional retrieval over task prototypes when the eligible set is large. Not a decision.

**Jev.** TypeSafe System One model. State in, typed Choice, Score, and Boolean answers out, each with a probability distribution. It may choose among eligible model ids when those ids are the Choice options. It cannot name a model that was not offered, and a low-confidence choice is ignored. Replaced later by a learned classifier with the same question contract.

**Policy.** Turns facts and the assessment into an ordered list of permitted models. The only place weights and fallback order exist.

**Learned router.** A later, offline-trained estimate of success. Same outputs as the quality prior. Shadow, then canary, then pin. Never self-updating on the hot path.

**Execution LLM.** Does the user's task. Chosen by policy, called by the gateway.

**Verifier.** Judges the answer against a rubric or a schema. Not the same component as Jev. Not a measure of routing confidence.

## 20. Rules added after review

Four reviews tightened the spec where an implementer would otherwise have invented behavior. The changes above are the parts that contradicted earlier sentences. These are the remaining binding rules.

**The caller may only narrow constraints.** Residency, allowlist, budget ceiling, and latency ceiling on the state are the intersection of the tenant record with any stricter caller value. A caller value that adds a region, adds a model, raises a ceiling, or turns off required verification is a malformed request. The denylist is the union of the tenant denylist and the caller denylist. None of these fields are read from messages, tool descriptions, or classifier output.

**Modality is sniffed.** The server inspects attachment bytes and checks them against the declared MIME type. A mismatch rejects the request. A declaration alone adds no capability tag. `previous_model_id` and `previous_image_ref` come from server-side conversation state for that tenant and actor. Policy may rank the previous model first only while it remains eligible, the task type is unchanged or unknown, and the new text is below a short-turn token floor. Stickiness loses to eligibility, budget, and a high-confidence task change.

**Shortlist is not a second eligibility gate.** It is skipped when fewer than about twelve models remain, when the top two prototype families fall inside a policy margin, when similarity is below a minimum, or when ambiguity is above the policy threshold. The prototype corpus is a separate versioned artifact (`prototype_version`), example requests plus tags, with no model ids. A deploy of that corpus must pass a fixed query set. Dropped ids are stored on the trace with the similarity that dropped them. Retrieval cannot drop the previous turn's capability tag, and it cannot be the only model left with a tag eligibility already required.

**Ambiguity is not low confidence.** Low confidence replaces the assessment with heuristics. High ambiguity keeps the assessment and refuses to use its task label as a filter. Policy either returns a typed clarification with no model call, or it arms verification if a call is still made.

**Streaming is a policy flag.** `delivery` is `buffer` or `stream`. Buffer is mandatory when a schema, tools, or verification is armed. Tokens are not written to the client until those checks pass.

**The trace is the decision, not a summary.** Before the gateway call, persist `request_id` (minted by the router), `tenant_id`, `actor_id`, `limits`, `control`, eligible ids, rejection codes, `health_snapshot_id`, versions, the weight row, `analyzer_prompt_version`, and `analysis_source`. After the call, append `attempts[]`: model id, trigger, error code, validation result, latency, tokens, and actual cost, plus `returned_attempt_index`. Score rows store the terms that produced the rank and a total order: higher score, then lower estimated cost, then lexicographic model id. For tenants marked `audit_required`, a failed checkpoint fails the request before the provider call.

**Quality numbers do not move between deploys.** Circuit state is the only live ranking input. Success labels are rubric pass or fail with `rubric_version`, `grader_id`, and the propensity of the served model. Transport failures are not quality labels. The promotion suite is a frozen set of request ids that the candidate version was not trained on. Offline promotion requires zero new constraint violations, a pre-registered cost-per-success improvement whose interval excludes zero, the same result on each required slice, and p95 latency inside a pre-registered factor of the champion. Shadow mode rescores and does not call a provider. Canary serves a hash-assigned fraction, keeps those rows out of the champion quality table, and stops if a pre-registered harm limit is crossed. Learned artifacts are stored as `semantic_model_version`, `retrieval_version`, and `quality_model_version`. None of them writes the pin.

**Trace reads are tenant-scoped.** Operator access to retained prompt text is a break-glass role that writes its own audit record. Local spill is encrypted, keyed by tenant, and deleted after the durable write. Spill is not a residency location.

## 21. Web client

`app.py` serves the router on port 8765, on this machine and on other devices on the same local network. The browser is a view of the decision. It cannot change the model choice, limits, residency, or keys. The models it may choose are the rows in `catalog.json`. A measured success rate replaces a row's quality estimate only after 20 labeled replies for that model and task. Jev is given a trimmed tail of the thread, not the image bytes. When `ROUTER_PASSWORD` is set, a turn is refused until that session unlocks.

**Two states.** Before the first question the page is a single centered stack: the 3D mark, the headline, the composer, and three example prompts. Sending moves the composer to the bottom, docks the mark into the header, and shows the thread in a 720px reading column. New thread reverses that, and keeps the old thread for five seconds behind an Undo toast before the server session is cleared.

**The 3D mark shows routing.** `web/scene.js` renders one glass object per catalog model (sphere, faceted gem, torus) around a light point. While a request is out, pulses travel from the point to each model. When the reply arrives the point moves to the served model, that model lights in its color, and the group turns it to the front. The canvas never covers text. It pauses when the tab is hidden and renders a single still frame under `prefers-reduced-motion`.

**Streaming.** After a model is chosen, tokens are written to the browser as server-sent events. The page shows them as plain text. The final event replaces that text with the escaped markdown, so a partial reply is never treated as HTML. A dropped connection stops the stream and does not keep the unfinished reply.

**Why this model.** Each reply carries a chip with the served model, the task, and Jev's confidence. The chip opens a panel built from typed fields the server returns: eligible count and rejection reasons, how the analysis was decided (`analysis_source`), candidate quality and expected speed from the catalog, retries and fallbacks from `attempts`, output checks, and elapsed time. Attempt errors outside the public error list are shown as `call_failed`, so exception names never reach the browser.

**Failures are recoverable in place.** Every public error code maps to a message and one action: try again (the same payload is resent), edit the message, or reload for an expired session.

**Security boundary is unchanged.** Markdown is escaped and rendered on the server, including code-block language labels. The page loads only same-origin files (Three.js and the fonts are vendored in `web/`), so the CSP stays `default-src 'self'`. Session and CSRF cookies are `HttpOnly` and `SameSite=Strict`, every POST carries `X-CSRF-Token`, the Host header must be localhost, this computer's name, or a private network address, and image bytes are sniffed against the declared type.
