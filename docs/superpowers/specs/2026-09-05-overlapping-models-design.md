# Overlapping-Model Support — Design (2026-09-05)

## Problem

Several providers serve overlapping model IDs. The current `ProviderRouter`
picks a single provider per model via first-match-wins (`senary → quinary →
quaternary → tertiary → secondary → primary`), `get_all_models()` concatenates
lists (duplicated IDs), and there is no cross-provider failover. Clients have
no syntax to pin a provider, and operators have no visibility into overlaps.

## Goals / Non-goals

- Goals: deterministic routing for overlapping IDs; client-side provider pin;
  failover on provider-side failures; deduped `/v1/models`; overlap
  observability. Fully backward compatible for bare model names.
- Non-goals: cost/latency-based routing, load balancing across overlap sets,
  semantic caching, per-key budgets (all standard gateway features, deferred).

## 1. Naming syntax + resolution order

Syntax: `<provider>/<model>` pins a provider; bare `<model>` keeps today's
behavior.

```
openlux/gpt-5.6-sol        → force tertiary (OpenLux), upstream sees "gpt-5.6-sol"
dashscope/qwen3.6-plus     → force primary, upstream sees "qwen3.6-plus"
mimo/mimo-v2.5-pro         → force secondary
ark/glm-5.2                → force quaternary
metaspark/muse-spark-1.3   → force quinary
deepseek/deepseek-v4-flash → force senary
gpt-5.6-sol                → no pin → overlap-set resolution (§1.3)
```

Provider slugs (new meaningful names; positional aliases stay valid):

| slug (new) | alias (old) | provider |
|---|---|---|
| `dashscope` | `primary` | DashScope |
| `mimo` | `secondary` | MIMO Coding Plan |
| `openlux` | `tertiary` | OpenLux |
| `ark` | `quaternary` | ARK / BytePlus |
| `metaspark` | `quinary` | Meta AI / Muse Spark |
| `deepseek` | `senary` | DeepSeek |

Resolution order for a request model (after existing `mimo-v2-5` → `mimo-v2.5`
alias normalization, which also applies under a prefix):

1. **Prefix pin** — if `model` contains `/`, split off the head as provider
   slug. Strip it before forwarding upstream (LiteLLM/OpenRouter convention:
   the routing prefix never reaches the upstream API). Unknown slug or pinned
   provider not configured → `400` with an `available_providers` hint; never
   silently reroute.
2. **`MODEL_PROVIDER_MAP`** — existing server-side override (bare name →
   provider), unchanged, evaluated after pin.
3. **Overlap set** — all configured providers whose model list contains the
   bare ID (§2). Tried in priority order with failover (§3).
4. **Default primary** — unknown models go to primary, as today.

## 2. Overlap discovery + `/v1/models` dedup

- **Overlap registry:** at router init (and on config change), build
  `model_id → [providers…]` from all six model lists post-normalization. This
  is the single source of truth for "which configured providers serve X" and
  replaces the chained `if model in _x_ids` checks. Unconfigured providers are
  absent from all sets.
- **`/v1/models` dedup:** one entry per bare model ID. Each entry keeps
  today's `{id, object}` shape plus two additive fields (old clients unaffected):

```json
{ "id": "gpt-5.6-sol", "object": "model",
  "providers": ["openlux", "ark"],
  "provider_models": ["openlux/gpt-5.6-sol", "ark/gpt-5.6-sol"] }
```

Consumers may advertise `provider_models` as selectable options (how
OpenRouter-style `provider/model` IDs surface in pickers); the bare `id`
remains the default route.

## 3. Failover across overlapping providers

Industry best practice (LiteLLM `order` + `fallbacks`, gateway literature):
fail over only on **provider-side problems** — retryable `429`, `5xx`,
timeouts/connection errors. Never on `4xx` (a malformed request fails
identically everywhere; retrying wastes money). This matches the handler's
existing `should_retry_429` / `MAX_5XX_RETRIES` split.

- Bare model on an overlap set → try providers in priority order:
  `MODEL_PROVIDER_MAP` winner first (if any), else
  `senary → quinary → quaternary → tertiary → secondary → primary` (today's
  order, preserved; overridable via `MODEL_FALLBACK_ORDER`, §4). Pinned
  `provider/model` → that provider only, no failover.
- A request that already waited in the rate-limit queue or reserved TPM reuses
  its slot/reservation across failover hops (no double-charge); token
  reconcile/refund happens once at the end, as today.
- Per-provider circuit-breaker state stays per-provider (existing per-limiter
  tracking) — an open circuit skips that provider in the overlap order instead
  of failing the request.
- Session log records `provider` = the one that ultimately served, plus
  `attempted_providers: [...]` for failover visibility in `session_logs/`.
- Retry budgets unchanged: failover hops consume the existing `max_retries` /
  `MAX_5XX_RETRIES` budgets; upstream `400` is terminal. Quota-`429`
  (non-retryable per `should_retry_429`) keeps the existing same-provider
  cooldown path and does not trigger cross-provider failover.

## 4. Config, observability, testing

**Config (all additive, all optional):**

- `MODEL_PROVIDER_MAP` keeps working; values may use new slugs (`openlux`,
  `ark`, …) or old positional values (`tertiary`, …).
- New optional `MODEL_FALLBACK_ORDER` env (e.g. `"openlux,ark,dashscope"`)
  overrides the default overlap try-order per deployment. Unset → today's
  fixed order.
- No new required env vars; unconfigured providers are absent from overlap
  sets, exactly as today.

**Observability:**

- `GET /v1/proxy/status` gains a `model_overlaps` section
  (`model_id → providers`) so the TUI/dashboard can show multi-sourced models.
- Structured logs on each failover hop (`request_id`, `from_provider`,
  `to_provider`, `reason`); TUI consumes via existing `TUILogHandler`, no TUI
  changes required.

**Testing:**

- Unit: prefix parse/strip (incl. `mimo-v2-5` alias under prefix),
  unknown-slug `400`, overlap registry build, deduped `/v1/models`,
  `MODEL_FALLBACK_ORDER` parsing.
- Integration: overlapping model fails over tertiary→quaternary on `502`;
  pinned request does *not* fail over; upstream `400` is terminal (no
  cross-provider retry); quota-`429` stays on the same provider.
- Backward compat: all existing tests pass unchanged — bare names route
  exactly as today when no overlap exists.

## References

- LiteLLM provider-prefix routing (`openrouter/provider/model`, prefix
  stripped before upstream): docs + PRs #24603, #24282, #23539, #24275.
- LiteLLM `order` (priority failover) + `routing_strategy` (latency/cost),
  assignable per routing group.
- DevOpsNess multi-provider gateway patterns: fail over on 5xx/429/timeout,
  not on 400; per-provider circuit breakers; normalized requests with
  `provider_options` escape hatches.
