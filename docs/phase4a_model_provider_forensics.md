# Phase 4A — Model / Provider Forensics Report

**Date:** 2026-09-09
**System:** AHJIN 2.0 (post-Phase 3A + Phase 3B)
**Scope:** Read-only forensic inspection. No source code was modified.

---

## 1. Executive Summary

This report documents the complete, verified model/provider configuration of AHJIN 2.0 as determined by full source inspection. It answers all 16 forensic questions from the Phase 4A specification.

**Key findings at a glance:**

| Question | Answer |
|---|---|
| Models registered | 9 (7 cloud, 2 local Ollama) |
| FAST tier model | `nvidia/nemotron-3.5-lightning-30b-a3b` via NVIDIA Direct only |
| HEAVY tier top | `minimax/minimax-m3:free` via OpenRouter |
| Alternate Nemotron Lightning path | **NO** — only one path exists |
| Request payload (streaming) | `model`, `messages`, `temperature=0.7`, `max_tokens=4096`, `stream=True` |
| HTTP timeout | 90.0 seconds (configurable) |
| Connection pooling | `httpx.AsyncClient` with keepalive=20 (NVIDIA only) |
| Health tracking | EMA latency + circuit breaker; affects routing |
| Reasoning content in output | **Never** — intercepted and suppressed at provider level |
| Provider count registered at runtime | 1 (NVIDIA) mandatory; OpenRouter conditional on key; Ollama conditional on flag |

---

## 2. Current Model Catalog

All 9 models from `src/ahjin/models/catalog.py` → `create_default_catalog()`:

| # | model_id | provider_id | Tier | Priority | Quality | endpoint_verified | capabilities |
|---|---|---|---|---|---|---|---|
| 1 | `nvidia/nemotron-3.5-lightning-30b-a3b` | nvidia | FAST | 200 | 85 | ✅ | coding, tool_calling |
| 2 | `minimax/minimax-m3:free` | openrouter | HEAVY | 250 | 95 | ✅ | reasoning, coding, tool_calling, long_ctx |
| 3 | `nvidia/nemotron-3-ultra-550b-a55b:free` | openrouter | HEAVY | 230 | 95 | ✅ | reasoning, coding, tool_calling, long_ctx |
| 4 | `nvidia/nemotron-3-ultra-550b-a55b` | nvidia | HEAVY | 200 | 95 | ✅ | reasoning, coding, tool_calling, long_ctx |
| 5 | `moonshotai/kimi-k3` | nvidia | HEAVY | 170 | 87 | ❌ | reasoning, coding, tool_calling, long_ctx |
| 6 | `deepseek-ai/deepseek-v4-pro-0813` | nvidia | HEAVY | 150 | 92 | ❌ | reasoning, coding, tool_calling, long_ctx |
| 7 | `deepseek-ai/deepseek-v4-flash-0731` | nvidia | HEAVY | 130 | 90 | ❌ | reasoning, coding, tool_calling, long_ctx |
| 8 | `gemma3:4b` | ollama | FAST | 100 | 80 | ✅ | coding |
| 9 | `qwen3:8b` | ollama | HEAVY | 120 | 85 | ✅ | reasoning, coding, tool_calling, long_ctx |

**Key observations:**
- `nemotron-3.5-lightning-30b-a3b` has `capabilities.reasoning = False` — it is explicitly NOT marked as a reasoning model.
- Despite `reasoning=False`, NVIDIA's inference API returns `reasoning_content` deltas for this model. AHJIN intercepts and suppresses them.
- All HEAVY models except `gemma3:4b` and `qwen3:8b` are cloud-hosted.
- `endpoint_verified=False` on Kimi K3 and both DeepSeek models — they are catalog-registered but not confirmed reachable.

---

## 3. Current Provider Map

### Runtime-registered providers (from `src/ahjin/main.py`)

| Provider | ID | Registered | Condition |
|---|---|---|---|
| `NvidiaProvider` | `nvidia` | Always (startup fails if key missing) | `NVIDIA_API_KEY` must be set |
| `OpenRouterProvider` | `openrouter` | Conditional | Only if `OPENROUTER_API_KEY` is set in `.env` |
| `OllamaProvider` | `ollama` | Conditional | Only if `ollama_enabled = True` (default: True) |

### Provider-to-model binding (ModelRouter tier selection)

```
FAST tier request
  → Eligible: nemotron-3.5-lightning-30b-a3b (nvidia, priority=200)
              gemma3:4b (ollama, priority=100) [if ollama registered]
  → Selected: nemotron-3.5-lightning-30b-a3b [higher priority wins]

HEAVY tier request
  → Eligible: minimax/minimax-m3:free (openrouter, priority=250) [if openrouter registered]
              nvidia/nemotron-3-ultra-550b-a55b:free (openrouter, priority=230) [if openrouter registered]
              nvidia/nemotron-3-ultra-550b-a55b (nvidia, priority=200)
              moonshotai/kimi-k3 (nvidia, priority=170, endpoint_verified=False)
              deepseek-ai/deepseek-v4-pro-0813 (nvidia, priority=150, endpoint_verified=False)
              deepseek-ai/deepseek-v4-flash-0731 (nvidia, priority=130, endpoint_verified=False)
              qwen3:8b (ollama, priority=120) [if ollama registered]
  → Selected: minimax/minimax-m3:free [if openrouter registered]
              else: nvidia/nemotron-3-ultra-550b-a55b [nvidia fallback]
```

**Critical note:** If `OPENROUTER_API_KEY` is NOT set, `OpenRouterProvider` is not registered. The ModelRouter would then select models #3 and #2 from the HEAVY catalog — but since the `openrouter` provider_id is not in the registry, `ProviderGateway.get_provider("openrouter")` would raise `KeyError`. This would trigger the REROUTE recovery loop, falling through to `nvidia/nemotron-3-ultra-550b-a55b` (priority=200, provider=nvidia).

---

## 4. Exact Nemotron Lightning Path

### Complete invocation path for a FAST-tier request

```
User message (Telegram)
  ↓
TelegramAdapter.handle_message()
  ↓
TaskDispatcher.dispatch_stream()
  ↓
BeruOrchestrator.plan()
  → preferred_tier = "FAST"  [tool_intent path: hardcoded]
  → preferred_tier = FAST or HEAVY [keyword analysis path]
  ↓
HarnessRunner.run_stream()
  → ContextAssembler.assemble()
  ↓
ProviderGateway.invoke_stream()
  → ModelRouter.select_model(preferred_tier=FAST)
  → returns: provider_id="nvidia", model_id="nvidia/nemotron-3.5-lightning-30b-a3b"
  → ProviderRegistry.get_provider("nvidia") → NvidiaProvider instance
  ↓
NvidiaProvider.invoke_stream()
```

### Exact HTTP request constructed by NvidiaProvider.invoke_stream()

```
POST https://integrate.api.nvidia.com/v1/chat/completions
Authorization: Bearer [NVIDIA_API_KEY — REDACTED]
Content-Type: application/json

{
  "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
  "messages": [
    {"role": "system", "content": "<system_instruction>"},
    // ... conversation_history turns ...
    {"role": "user", "content": "<user_instruction>"}
  ],
  "temperature": 0.7,
  "max_tokens": 4096,
  "stream": true
}
```

### Streaming response handling

1. `client.stream("POST", url, ...)` opens an SSE stream
2. Per-line parsing: lines prefixed with `data: ` are parsed as JSON
3. `choices[0].delta.reasoning_content` → **intercepted, counted, never yielded**
4. `choices[0].delta.content` → yielded immediately to caller
5. `[DONE]` → stream ends
6. Telemetry: all granular stage timestamps recorded to `last_telemetry`

---

## 5. ModelRouter Behavior

### Routing algorithm (from `src/ahjin/models/router.py`)

**Pipeline (in order):**
1. **Hard Capability Gate** — eliminates models missing required capabilities (`requires_reasoning`, `requires_code`, `requires_vision`)
2. **Health & Excluded Filter** — removes UNHEALTHY models (unless cooldown expired for probe) and request-locally excluded models
3. **Hard Latency Constraint** — if `max_latency_ms` is specified, filters by EMA; relaxed to prevent starvation
4. **Tier Match** — prefers `preferred_tier`; falls back to all eligible if no tier match
5. **Ranking** — `max(candidates, key=lambda m: (m.priority, _blended_score(m)))`

**Ranking formula:**
```python
# Primary key: priority (DESC) — operator catalog preference
# Secondary key: blended_score (DESC) — quality weighted by preference

if quality_preference == "speed":
    quality_weight = 1.0; latency_penalty = 0.1
elif quality_preference == "quality":
    quality_weight = 3.0; latency_penalty = 0.001
else:  # "balanced"
    quality_weight = 2.0; latency_penalty = 0.01

score = quality_score * quality_weight
if ema_latency > 0:
    score -= latency_penalty * ema_latency
if endpoint_verified:
    score += 0.001  # micro tie-breaker
```

**BERU tier assignment rules** (from `src/ahjin/beru/orchestrator.py`):

| Condition | preferred_tier | quality_preference |
|---|---|---|
| Tool-resolved request (any tool) | **FAST** (hardcoded L240) | "balanced" (default) |
| Keywords: explain, analyze, summarize, code, etc. | **HEAVY** | "quality" |
| Plain conversation (HI, greetings, poems) | **FAST** | "speed" |

**Critical finding:** The word `"summarize"` is in both `_REASONING_KEYWORDS` AND in `read_kw` (tool chaining). For "find my resume and summarize it":
- `detect_tool_intent()` matches `file_search` deterministically
- BERU hardcodes `preferred_tier="FAST"` for all tool-resolved paths
- ModelRouter selects Nemotron Lightning regardless of content complexity
- This means resume summarization goes to a FAST/non-reasoning-capable model

---

## 6. ProviderGateway Behavior

### Non-streaming (`invoke`)
- `ModelRouter.select_model()` → selection
- `ProviderRegistry.get_provider(provider_id)` → provider instance
- `provider.invoke(request)` → response
- On success: `health_tracker.record_success(model_id, latency_ms)`
- On exception: `health_tracker.record_failure(model_id)` + `exc.model_id = model_id` + re-raise

### Streaming (`invoke_stream`)
- Same routing, then `provider.invoke_stream(request)` yields chunks
- Health recorded as success at stream end; failure on exception

### Retry/reroute behavior (from HarnessRunner)
- `max_recovery_attempts = 2` (default, set by BERU)
- `recovery_policy = REROUTE`
- On failure: failed model_id added to `excluded_models` set
- Next attempt: `gateway.invoke(excluded_model_ids=excluded_models)` — ModelRouter picks next-best model
- Handled exception types: `httpx.HTTPStatusError`, `httpx.RequestError`, `VerificationError`, `CapabilityUnavailableError`
- **Not** caught: `asyncio.CancelledError` (re-raised immediately), generic `Exception` (falls through to outer handler)

---

## 7. Configuration / Environment Map

From `src/ahjin/core/config.py` (secrets REDACTED):

| Setting | Default | Description |
|---|---|---|
| `ahjin_env` | `"development"` | Environment name |
| `offline_mode` | `False` | Force offline routing |
| `telegram_bot_token` | `""` | Telegram API token (REDACTED) |
| `nvidia_api_key` | `""` | NVIDIA API key (REDACTED) |
| `nvidia_base_url` | `"https://integrate.api.nvidia.com/v1"` | NVIDIA endpoint |
| `nvidia_max_tokens` | `4096` | Default max output tokens |
| `nvidia_timeout_seconds` | `90.0` | HTTP client timeout (seconds) |
| `openrouter_api_key` | `""` | OpenRouter API key (REDACTED) |
| `openrouter_base_url` | `"https://openrouter.ai/api/v1"` | OpenRouter endpoint |
| `openrouter_timeout_seconds` | `90.0` | OpenRouter HTTP timeout (seconds) |
| `ollama_base_url` | `"http://localhost:11434/v1"` | Local Ollama endpoint |
| `ollama_enabled` | `True` | Register OllamaProvider |
| `ollama_timeout_seconds` | `60.0` | Ollama HTTP timeout (seconds) |
| `ollama_embedding_model` | `"bge-m3:latest"` | Embedding model for RAG |

**Temperature:** Hard-coded to `0.7` in all three providers. Not configurable via `.env`.
**max_tokens:** Sent as `request.max_tokens or self.max_tokens` → default 4096 from config.
**No reasoning-specific parameters** (e.g., `thinking`, `enable_thinking`, `reasoning_effort`) are sent to any provider.

---

## 8. Existing Alternate Provider Paths for Nemotron Lightning

**Finding: NO existing alternate Nemotron Lightning provider path was found.**

The model `nvidia/nemotron-3.5-lightning-30b-a3b` is registered in the catalog with exactly one provider binding:

```python
provider_id = "nvidia"
```

The OpenRouter catalog entries for Nemotron-family models reference only `nvidia/nemotron-3-ultra-550b-a55b:free` (Nemotron Ultra, not Lightning). There is no OpenRouter catalog entry for `nvidia/nemotron-3.5-lightning-30b-a3b`.

OpenRouter does host this model (it can serve any NVIDIA NIM model), but it is **not registered** in `create_default_catalog()` with `provider_id="openrouter"`. To route Nemotron Lightning through OpenRouter, a new catalog entry with `provider_id="openrouter"` and `model_id="nvidia/nemotron-3.5-lightning-30b-a3b"` would need to be added.

---

## 9. Latency-Relevant Findings

### 9.1 Connection Setup Variability

**CONFIRMED (AHJIN-controlled, partially)**

`NvidiaProvider` uses a persistent `httpx.AsyncClient` with:
- `max_keepalive_connections=20`
- `max_connections=50`
- `keepalive_expiry=60.0`

The client is lazily created on first use and reused across requests. However:
- If the client is closed (e.g., connection dropped by server), `_get_client()` creates a new one, incurring a new TLS handshake.
- NVIDIA's `integrate.api.nvidia.com` endpoint may route requests to different backend clusters depending on load, causing variable TLS setup times.
- Phase 3B confirmed HTTP connect range: 558–4,604 ms (TEST_A had mean 2,487 ms vs TEST_B–D mean ~1,000 ms), suggesting the pool primes after the first request.

`OpenRouterProvider` and `OllamaProvider` create a **new `httpx.AsyncClient` per request** (no pooling). This is an inefficiency, but these are used for HEAVY tier only.

### 9.2 TTFT Variability

**CONFIRMED (provider/model-controlled, partially AHJIN-influenced)**

TTFT = Time from request send → first visible content token.
In Nemotron Lightning, this equals: HTTP_connect + first_SSE + reasoning_duration.

Reasoning duration is the primary driver and is fully provider/model-controlled. AHJIN has no mechanism to shorten reasoning. Context size (which AHJIN does control via ContextAssembler) proportionally increases reasoning duration.

### 9.3 Hidden Reasoning Duration Variability

**CONFIRMED (provider/model-controlled)**

The `reasoning_content` delta stream from Nemotron begins before `content` deltas. AHJIN intercepts this at `delta.get("reasoning_content")` and measures it. The duration varies 3,984–37,269 ms (Phase 3B TEST_A). This is entirely non-deterministic from AHJIN's perspective — it reflects Nemotron's internal chain-of-thought length.

### 9.4 Visible Generation Variability

**CONFIRMED (provider/model-controlled, partially output-length-dependent)**

Visible generation time is proportional to the length of the generated response. AHJIN does not control output length beyond `max_tokens=4096`. Phase 3B visible generation range: 89–23,309 ms.

### 9.5 Context-Dependent Reasoning Expansion

**CONFIRMED (AHJIN-controlled trigger; model-controlled scale)**

Each tool result injected into context expands model reasoning by ~4,000–11,000 ms (Phase 3B delta analysis). AHJIN's ContextAssembler controls what gets injected. The Phase 3A search-result pruning optimization is active and reduces this. No further context reduction is currently applied.

### 9.6 Retry/Fallback Behavior

**CONFIRMED (AHJIN-controlled)**

HarnessRunner implements a same-request REROUTE loop with `max_recovery_attempts=2`. On model failure:
1. Failed model is added to `excluded_models`
2. `gateway.invoke(excluded_model_ids=excluded_models)` is called
3. ModelRouter selects the next-best eligible model
This adds per-retry latency equal to the additional model invocation time.

### 9.7 Streaming Behavior

**CONFIRMED (AHJIN-controlled framing; provider-controlled emission rate)**

`NvidiaProvider.invoke_stream()` uses `client.stream()` with `aiter_lines()`. Chunks are yielded immediately as they arrive — no client-side buffering. The `TelegramAdapter` accumulates chunks and calls `edit_text()` progressively. Emission rate is determined by the provider token generation speed (~tokens/second) which AHJIN cannot control.

### 9.8 Connection Pooling Behavior

**CONFIRMED (AHJIN-controlled, NVIDIA only)**

Only `NvidiaProvider` implements connection pooling. `OpenRouterProvider` and `OllamaProvider` create fresh `httpx.AsyncClient` instances per-call, incurring a new TCP+TLS handshake each time. This is an existing inefficiency for heavy-tier calls.

### 9.9 HTTP Client Lifecycle

**CONFIRMED (AHJIN-controlled)**

- `NvidiaProvider._client`: lazily created on first `invoke_stream()`, reused across requests, explicitly closed via `aclose()` in `main.py` shutdown. If `_client.is_closed`, a new client is created.
- `OpenRouterProvider`: uses `async with httpx.AsyncClient(...)` context manager — client created and destroyed per invocation.
- `OllamaProvider`: same pattern as OpenRouter — per-invocation client, no pooling.

### 9.10 Timeout Behavior

**CONFIRMED (AHJIN-configurable)**

- NVIDIA: `nvidia_timeout_seconds = 90.0` — passed as `timeout=self.timeout_seconds` to both `client.post()` and `client.stream()`. This is a **total** timeout, not a per-chunk read timeout. If the full stream takes >90s, the request times out. Phase 3B observed max total times of ~82s (TEST_D) — within the 90s limit but close.
- OpenRouter: `openrouter_timeout_seconds = 90.0` — same per-client-context timeout.
- Ollama: `ollama_timeout_seconds = 60.0` — shorter; appropriate for local inference.

**Risk:** TEST_D P90 = 60.6s with max observed at 82.8s. The 90s timeout provides only ~7s headroom above max observed. Exceptionally slow Nemotron responses could silently time out.

---

## 10. AHJIN-Controlled vs Provider/Model-Controlled Factors

### AHJIN-Controlled Factors

| Factor | Where controlled | Current state |
|---|---|---|
| Model selection | ModelRouter priority/score | Deterministic; Nemotron Lightning for FAST |
| Context size | ContextAssembler | Pruned after Phase 3A; further reduction possible |
| max_tokens | NvidiaProvider payload | Fixed at 4096; not request-adaptive |
| temperature | NvidiaProvider payload | Fixed at 0.7; not configurable per request |
| Reasoning suppression | NvidiaProvider.invoke_stream | Active; 0/40 exposure in Phase 3B |
| Connection pooling | NvidiaProvider._client | Active for NVIDIA; absent for OpenRouter/Ollama |
| HTTP timeout | settings.nvidia_timeout_seconds | 90s; close to max observed latency |
| Streaming | invoke_stream() | Active; chunks yielded immediately |
| Reroute on failure | HarnessRunner | 2 attempts; active |
| Health tracking | ModelHealthTracker | EMA + circuit breaker; affects future routing |
| Tier assignment | BERU orchestrator | Tool paths always FAST; keywords drive FAST/HEAVY |

### Provider/Model-Controlled Factors

| Factor | Source | AHJIN visibility |
|---|---|---|
| Reasoning chain length | Nemotron model internals | Measured via reasoning_duration_ms |
| Token generation speed | NVIDIA GPU throughput | Measured via visible_generation_ms |
| NVIDIA cluster queue depth | NVIDIA infrastructure | NOT observable |
| TLS connection reuse by server | NVIDIA server policy | Partially — HTTP connect time measured |
| First SSE chunk timing | NVIDIA server scheduling | Measured via first_sse_ms |
| Model variant / routing | NVIDIA internal | NOT observable |

---

## 11. Confirmed vs Plausible vs Not Supported

### CONFIRMED (direct code evidence)

- AHJIN internal overhead is ~0–2ms (telemetry code, benchmark data)
- `reasoning_content` is emitted by Nemotron before `content` (nvidia.py line 294)
- Reasoning content is never yielded to stream or user (nvidia.py line 306: only `content` is yielded)
- HTTP connection pooling is active for NVIDIA only
- Temperature is hardcoded to 0.7 (all three providers)
- max_tokens defaults to 4096 and is not request-adaptive
- Reroute loop executes up to 2 attempts on httpx errors
- Tool-resolved requests are always routed FAST tier (orchestrator.py line 240)
- EMA latency is used as a latency-aware ranking penalty (router.py line 193)
- `endpoint_verified=False` on Kimi K3, DeepSeek Pro, DeepSeek Flash (catalog.py)

### PLAUSIBLE (consistent with code and data; not directly proven)

- NVIDIA server-side cluster load variation explains reasoning duration variance
- Persistent keepalive connections improve HTTP connect time (Phase 3B TEST_A vs B–D pattern is consistent)
- Richer context causes proportionally longer reasoning chains in Nemotron
- Temperature 0.7 contributes to reasoning chain non-determinism

### NOT SUPPORTED BY CURRENT CODE

- NVIDIA GPU load or queue depth directly observable from AHJIN code — no such mechanism exists
- Any per-request reasoning control (no `thinking` or `enable_thinking` parameters sent)
- Any dynamic temperature adjustment based on task type
- Any client-side buffering/throttling of SSE chunks

---

## 12. Risks / Unknowns

| Risk | Severity | Evidence |
|---|---|---|
| **90s timeout close to worst-case latency** | HIGH | Phase 3B TEST_D max=82.8s; P90=60.6s |
| **OpenRouter not pooled** | MEDIUM | Per-call client creation; HEAVY requests pay TLS cost every time |
| **endpoint_verified=False on 3 HEAVY models** | MEDIUM | Kimi K3, DeepSeek Pro, DeepSeek Flash may not be reachable |
| **Reroute loop exposes user to 2× model latency** | MEDIUM | If first HEAVY model fails, fallback adds 30–60s |
| **Tool-resolved paths always FAST even for complex tasks** | MEDIUM | Resume summarization uses Nemotron Lightning (non-reasoning-declared) instead of a reasoning model |
| **Nemotron reasoning non-determinism** | HIGH | CV=56–74% across Phase 3B; no AHJIN lever to reduce this |
| **Reasoning content intercept is delta-field only** | LOW | If NVIDIA changes the field name, reasoning would leak to output |
| **No explicit reconnect logic** | LOW | If keepalive connection drops mid-stream, httpx raises; reroute loop catches it |

---

## 13. Recommended Controlled Experiment for Phase 4B

### Hypothesis
Routing Nemotron Lightning (`nvidia/nemotron-3.5-lightning-30b-a3b`) through **OpenRouter** instead of (or in comparison to) NVIDIA Direct will produce measurably different latency characteristics, because OpenRouter fronts a different backend routing layer and may avoid NVIDIA inference cluster queueing.

### Proposed Phase 4B Experiment

**Objective:** Measure wall-clock latency and TTFT for identical prompts sent through two provider paths for the same Nemotron Lightning model:
- Path A: NVIDIA Direct (`https://integrate.api.nvidia.com/v1`)
- Path B: OpenRouter (`https://openrouter.ai/api/v1` with model `nvidia/nemotron-3.5-lightning-30b-a3b`)

**Method:**
1. Add a second catalog entry for Nemotron Lightning with `provider_id="openrouter"` and a distinct `priority` (lower than existing NVIDIA entry, so default routing is unchanged)
2. Write a benchmark that explicitly forces the two paths using `excluded_model_ids` to isolate each
3. Run 10 identical prompts through each path, interleaved (not batched) to avoid time-of-day bias
4. Compare: total_wall_ms, http_connect_ms, first_sse_ms, ttft_ms, reasoning_dur_ms, visible_gen_ms

**Control variables:**
- Same model ID string sent to both APIs
- Same prompt text for all runs
- Same temperature (0.7), same max_tokens (4096)
- Same AHJIN orchestration stack

**Null hypothesis:** No statistically significant difference in TTFT or reasoning_dur_ms between NVIDIA Direct and OpenRouter for the same model.

**This experiment does NOT change production routing.** The additional catalog entry would be marked with a lower priority, ensuring the production path (NVIDIA Direct, priority=200) is never preempted.

---

## 14. Changes Required for Phase 4B (NOT implemented)

The following changes would be required to execute the Phase 4B experiment. **None are implemented.**

### Change 1 — Add OpenRouter catalog entry for Nemotron Lightning

**File:** `src/ahjin/models/catalog.py`

Add after the existing Nemotron Lightning entry:

```python
catalog.register(
    ModelDescriptor(
        model_id="nvidia/nemotron-3.5-lightning-30b-a3b",
        provider_id="openrouter",          # OpenRouter as alternate provider
        tier=ModelTier.FAST,
        capabilities=ModelCapabilities(
            reasoning=False,
            coding=True,
            vision=False,
            tool_calling=True,
            long_context=False,
        ),
        limits=ModelLimits(max_context_tokens=128000, max_output_tokens=4096),
        priority=190,                       # Lower than NVIDIA Direct (200) — does NOT affect production
        quality_score=85,
        endpoint_verified=False,            # To be confirmed in Phase 4B
    )
)
```

**Routing impact:** The new entry has `priority=190 < 200` (existing NVIDIA entry). ModelRouter's primary ranking key is `priority DESC`, so the NVIDIA Direct path (200) always wins in production. The OpenRouter path is only reachable when the NVIDIA entry is in `excluded_model_ids`.

**Risk:** None to production routing — lower priority guarantees it is never selected over NVIDIA Direct.

### Change 2 — Add streaming telemetry to OpenRouterProvider (for comparable measurements)

**File:** `src/ahjin/providers/openrouter.py`

`OpenRouterProvider.invoke_stream()` currently has no `last_telemetry` dict. To get comparable granular timing data for the Phase 4B comparison, the same telemetry instrumentation as in `NvidiaProvider` would need to be added.

**Note:** OpenRouter may or may not emit `reasoning_content` deltas for this model — this would need to be discovered during Phase 4B.

### Change 3 — Write benchmark_phase4b.py

A new benchmark script that:
- Explicitly routes 10 runs each through provider_id="nvidia" and provider_id="openrouter" using excluded_model_ids
- Records identical granular telemetry per run
- Computes and compares statistics between the two paths

**No changes to production main.py, routing logic, or user-facing behavior.**

---

## 15. Files Inspected

| File | Purpose |
|---|---|
| `src/ahjin/models/catalog.py` | All 9 model registrations |
| `src/ahjin/models/router.py` | Full routing algorithm |
| `src/ahjin/models/health.py` | EMA + circuit breaker |
| `src/ahjin/models/types.py` | ModelDescriptor, ModelTier, Capabilities |
| `src/ahjin/providers/nvidia.py` | NVIDIA provider, payload, pooling, telemetry |
| `src/ahjin/providers/openrouter.py` | OpenRouter provider, per-call client |
| `src/ahjin/providers/ollama.py` | Ollama provider, per-call client |
| `src/ahjin/providers/registry.py` | Provider registry mechanics |
| `src/ahjin/providers/base.py` | BaseModelProvider interface |
| `src/ahjin/harness/gateway.py` | ProviderGateway routing + error attachment |
| `src/ahjin/harness/runner.py` | Reroute loop, telemetry recording |
| `src/ahjin/harness/connectivity.py` | Online/offline detection |
| `src/ahjin/beru/orchestrator.py` | Tier assignment, tool-path FAST hardcode |
| `src/ahjin/beru/types.py` | ExecutionStrategy, RecoveryPolicy |
| `src/ahjin/core/config.py` | All settings, endpoints, timeouts |
| `src/ahjin/main.py` | Bootstrap, provider registration order |
| `src/ahjin/telemetry/timing.py` | Stage constants |

---

## 16. Phase 4A Closure

| Item | Status |
|---|---|
| Source code modified | ❌ NO |
| Routing changed | ❌ NO |
| Providers added/removed | ❌ NO |
| Model priorities changed | ❌ NO |
| `.env` modified | ❌ NO |
| Alternate Nemotron Lightning path found | ❌ NO — does not exist |
| Report generated | ✅ YES |
| Phase 4B defined | ✅ YES — OpenRouter vs NVIDIA Direct comparison |

---

*Phase 4A: Read-only forensic inspection completed 2026-09-09. No source code changes.*
