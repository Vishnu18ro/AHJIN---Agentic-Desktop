# Phase 6 — Primary Model + Capability-Aware Fallback Routing

## 1. Executive Summary

Based on empirical benchmarks conducted in **Phase 5** (Model Capability Characterization across 9 task categories) and **Phase 5.1** (Deep Reasoning Reliability under temperature variation), **MiniMax M3** (`minimax/minimax-m3` via OpenRouter) demonstrated superior overall speed (TTFT ~0.8s vs >2.5s for Nemotron Lightning), high code generation proficiency, and consistent instruction-following across general tasks.

Under **Phase 6**, the AHJIN 2.0 routing architecture has been updated with the following architectural decision:

> **MiniMax M3 is the PRIMARY model for ALL requests.**
> MiniMax M3 is attempted first regardless of task complexity (`LIGHT`, `HEAVY`, `CODING`, `REASONING`, `RAG`, `TOOL PLANNING`, `GENERAL CHAT`).
> Only after MiniMax M3 fails, times out, becomes unavailable, or is tripped by circuit breaker/health state does AHJIN branch according to task complexity into specialized fallback chains.

---

## 2. Architecture & Routing Matrix

### 2.1 Routing Flowchart

```text
                     INCOMING USER REQUEST
                               │
                               ▼
                   BERU Analysis & Intent
                               │
                               ▼
                      MiniMax M3 (Primary)
                      Provider: OpenRouter
                      Model: minimax/minimax-m3
                      Priority: 300 | Tier: ALL
                               │
                ┌──────────────┴──────────────┐
                │                             │
             SUCCESS                       FAILURE / TIMEOUT /
                │                          CIRCUIT BREAKER TRIP
                ▼                             │
          Deliver Response                    ▼
                                     Classify Complexity
                                     (LIGHT vs HEAVY)
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      │                                               │
                      ▼                                               ▼
                [LIGHT FALLBACK]                                [HEAVY FALLBACK]
                      │                                               │
             1. Nemotron Lightning                           1. Nemotron Ultra
                Provider: OpenRouter                            Provider: OpenRouter
                Model: ...lightning:free                        Model: ...ultra-550b-a55b:free
                Priority: 220                                   Priority: 230
                      │ (on failure)                                  │ (on failure)
             2. Nemotron Lightning                           2. Nemotron Ultra
                Provider: NVIDIA Direct                         Provider: NVIDIA Direct
                Model: ...lightning-30b-a3b                     Model: ...ultra-550b-a55b
                Priority: 200                                   Priority: 200
                      │ (on failure)                                  │ (on failure)
             3. Gemma 3 4B                                   3. Moonshot Kimi K3
                Provider: Ollama                                Provider: NVIDIA Direct
                Model: gemma3:4b                                Model: moonshotai/kimi-k3
                Priority: 100                                   Priority: 170
                                                                      │ (on failure)
                                                             4. DeepSeek V4 Pro
                                                                Provider: NVIDIA Direct
                                                                Model: ...deepseek-v4-pro-0813
                                                                Priority: 150
                                                                      │ (on failure)
                                                             5. DeepSeek V4 Flash
                                                                Provider: NVIDIA Direct
                                                                Model: ...deepseek-v4-flash-0731
                                                                Priority: 130
                                                                      │ (on failure)
                                                             6. Qwen 3 8B
                                                                Provider: Ollama
                                                                Model: qwen3:8b
                                                                Priority: 120
```

---

## 3. Registered Model Catalog Specifications

The production catalog `ModelCatalog` comprises 10 distinct, independently routable, and independently health-tracked models:

| # | Model ID | Provider | Tier | Role | Priority | Max Output | Verified |
|---|---|---|---|---|---|---|---|
| 1 | `minimax/minimax-m3` | `openrouter` | `ALL` | `PRIMARY` | 300 | 8,192 | Yes |
| 2 | `nvidia/nemotron-3.5-lightning:free` | `openrouter` | `FAST` | `LIGHT_FALLBACK` | 220 | 4,096 | Yes |
| 3 | `nvidia/nemotron-3.5-lightning-30b-a3b` | `nvidia` | `FAST` | `LIGHT_FALLBACK` | 200 | 4,096 | Yes |
| 4 | `nvidia/nemotron-3-ultra-550b-a55b:free` | `openrouter` | `HEAVY` | `HEAVY_FALLBACK` | 230 | 8,192 | Yes |
| 5 | `nvidia/nemotron-3-ultra-550b-a55b` | `nvidia` | `HEAVY` | `HEAVY_FALLBACK` | 200 | 8,192 | Yes |
| 6 | `moonshotai/kimi-k3` | `nvidia` | `HEAVY` | `HEAVY_FALLBACK` | 170 | 8,192 | Yes |
| 7 | `deepseek-ai/deepseek-v4-pro-0813` | `nvidia` | `HEAVY` | `HEAVY_FALLBACK` | 150 | 8,192 | Yes |
| 8 | `deepseek-ai/deepseek-v4-flash-0731` | `nvidia` | `HEAVY` | `HEAVY_FALLBACK` | 130 | 8,192 | Yes |
| 9 | `gemma3:4b` | `ollama` | `FAST` | `OFFLINE_FALLBACK` | 100 | 2,048 | Yes |
| 10 | `qwen3:8b` | `ollama` | `HEAVY` | `OFFLINE_FALLBACK` | 120 | 4,096 | Yes |

---

## 4. Key Implementation Nuances

### 4.1 Tier ALL Representation
- Added `ModelTier.ALL = "ALL"` in `src/ahjin/models/types.py`.
- In `ModelRouter.select_model`:
  - Eligible candidates match if `candidate.tier == target_tier or candidate.tier == ModelTier.ALL`.
  - When returning `ModelSelection`, reported tier mirrors the request's `target_tier` if the selected model has tier `ALL`.

### 4.2 Model Role Abstraction
- Added `ModelRole` enum (`PRIMARY`, `LIGHT_FALLBACK`, `HEAVY_FALLBACK`, `OFFLINE_FALLBACK`).
- Documented in `ModelDescriptor.role` for architectural clarity and inspection.

### 4.3 Distinct OpenRouter vs NVIDIA Direct Health Tracking
- `nvidia/nemotron-3.5-lightning:free` (`openrouter`) and `nvidia/nemotron-3.5-lightning-30b-a3b` (`nvidia`) are distinct catalog entries.
- Each maintains independent latency EMA, consecutive failure counters, and circuit breaker states via `ModelHealthTracker`.

### 4.4 No OmniRoute Fallacy
- Confirmed that no OmniRoute provider, adapter, or credentials exist or were invented.
- The standard provider is `openrouter` using existing `OpenRouterProvider` and `OPENROUTER_API_KEY`.

### 4.5 Clean Production Separation
- MiniMax model ID updated to production endpoint: `minimax/minimax-m3`. The non-functional `:free` model ID has been eliminated from active production routing.

---

## 5. Verification & Test Evidence

1. **Unit Test Suite**:
   - `pytest tests/unit`: **274 passed in 51.73s** (100% pass rate).
   - Dedicated test suite `tests/unit/test_phase6_routing.py` verifies all 14 Phase 6 routing assertions.
2. **Static Analysis & Type Completeness**:
   - `ruff check src tests`: **All checks passed!**
   - `pyright src`: **0 errors, 0 warnings.**
   - `pyright tests/unit/test_phase6_routing.py`: **0 errors, 0 warnings.**
3. **Live Non-Destructive Validation Probe**:
   - Executed `scratch/validate_phase6_live.py`:
     - Catalog entries and roles: Verified 10 models.
     - Primary routing: MiniMax M3 selected for Greeting, Fact, Reasoning, Coding, RAG.
     - Fallback chains: 3-stage LIGHT and 6-stage HEAVY verified with proper fallback steps.
     - Live API probe: Both `minimax/minimax-m3` and `nvidia/nemotron-3.5-lightning:free` responded successfully with live credentials.
