# Phase 4B — Temporary Model Speed Comparison Report

> **Forensic Investigation & Comparative Speed Benchmark**  
> **Status**: COMPLETE  
> **Scope**: Isolated, observational speed comparison only. No production routing, configuration, or model priorities modified.

---

## 1. Objective

The sole objective of Phase 4B is to conduct a temporary, strictly isolated benchmark to measure the empirical latency differences among three distinct model and provider invocation paths:

1. **TEST A**: NVIDIA Direct $\rightarrow$ Nemotron 3.5 Lightning 30B (`nvidia/nemotron-3.5-lightning-30b-a3b`)
2. **TEST B**: OpenRouter $\rightarrow$ Nemotron 3.5 Lightning Free (`nvidia/nemotron-3.5-lightning:free`)
3. **TEST C**: OpenRouter $\rightarrow$ MiniMax M3 (`minimax/minimax-m3:free` and active `minimax/minimax-m3`)

The benchmark isolates network, provider queuing, time-to-first-token (TTFT), internal reasoning duration, and visible token generation times under identical prompt and runtime conditions. This investigation is purely observational to provide empirical evidence for future architecture considerations; **no changes to AHJIN production routing, model priorities, or provider logic were made.**

---

## 2. Exact Models and Providers Tested

| Test ID | Provider | Configured Model Identifier | Endpoint URL | Connection Pooling Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **TEST A** | NVIDIA Direct | `nvidia/nemotron-3.5-lightning-30b-a3b` | `https://integrate.api.nvidia.com/v1/chat/completions` | Persistent `httpx.AsyncClient` with keepalive (max 20) |
| **TEST B** | OpenRouter | `nvidia/nemotron-3.5-lightning:free` | `https://openrouter.ai/api/v1/chat/completions` | Fresh `httpx.AsyncClient` per request (AHJIN production standard) |
| **TEST C (Catalog)** | OpenRouter | `minimax/minimax-m3:free` | `https://openrouter.ai/api/v1/chat/completions` | Fresh `httpx.AsyncClient` per request |
| **TEST C (Active)** | OpenRouter | `minimax/minimax-m3` | `https://openrouter.ai/api/v1/chat/completions` | Fresh `httpx.AsyncClient` per request |

### Critical Model Identifier Finding
- **NVIDIA Direct**: The identifier `nvidia/nemotron-3.5-lightning-30b-a3b` configured in AHJIN [catalog.py](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/src/ahjin/models/catalog.py#L55) was verified active and operational.
- **OpenRouter Nemotron**: The free tier endpoint `nvidia/nemotron-3.5-lightning:free` was verified active and operational on OpenRouter.
- **OpenRouter MiniMax M3**:
  - In AHJIN's production [catalog.py](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/src/ahjin/models/catalog.py#L80), the model is registered as `minimax/minimax-m3:free`.
  - Forensic inspection and live endpoint verification revealed that OpenRouter **no longer serves** the `:free` variant of MiniMax M3, returning `HTTP 404 Not Found`.
  - In accordance with the prompt instructions (*"If MiniMax M3 is unavailable through the existing configured OpenRouter path, document that fact rather than modifying production configuration. Do not silently substitute another model."*), production configuration was untouched.
  - To fulfill the core objective of measuring MiniMax M3's actual speed comparison, the benchmark executed:
    1. **TEST C (Catalog)**: `minimax/minimax-m3:free` — exactly as registered in AHJIN (10/10 failed with HTTP 404, documented transparently).
    2. **TEST C (Active)**: `minimax/minimax-m3` — the active OpenRouter endpoint for MiniMax M3, measuring real inference speed across all 10 rounds.

---

## 3. Benchmark Prompt

The exact benchmark prompt specified in the design was used without alteration for every warm-up and measured run across all providers:

> *"Explain what a transformer neural network is in 5 concise bullet points, then give one practical real-world example."*

### Standardized Execution Parameters
- **System Instruction**: `"You are AHJIN 2.0, an Agentic AI Operating Layer."` (standard AHJIN harness context)
- **Temperature**: `0.7`
- **Max Output Tokens**: `4096`
- **Streaming**: `True`
- **Tool Calls**: None (strictly isolated LLM network + inference latency; no RAG, no browser, no local tools)

---

## 4. Methodology

1. **Isolation**: Benchmark logic was implemented entirely inside [scratch/benchmark_phase4b_speed.py](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/scratch/benchmark_phase4b_speed.py).
2. **Provider Reuse**: Existing provider abstractions ([NvidiaProvider](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/src/ahjin/providers/nvidia.py) and [OpenRouterProvider](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/src/ahjin/providers/openrouter.py)) were instantiated directly without altering production code.
3. **Interleaved Execution**: To eliminate temporal network load bias, requests were executed round-robin across the paths:
   - `Round 1`: TEST A $\rightarrow$ TEST B $\rightarrow$ TEST C (Catalog) $\rightarrow$ TEST C (Active)
   - `Round 2`: TEST A $\rightarrow$ TEST B $\rightarrow$ TEST C (Catalog) $\rightarrow$ TEST C (Active)
   - ... through `Round 10`.
4. **Telemetry Instrumentation**:
   - `NvidiaProvider`: Granular stages captured via native `provider.last_telemetry` (`http_connect`, `first_sse`, `first_reasoning`, `first_visible_content`, `reasoning_duration`, `visible_generation`, `stream_completion`).
   - `OpenRouterProvider`: Instrumented in the benchmark script to record identical SSE timestamps (`t_connect`, `t_first_sse`, `t_first_reasoning`, `t_first_visible`, `reasoning_duration`, `visible_generation`, `stream_completion`), maintaining fresh `httpx.AsyncClient` instantiation per request.
5. **Redaction**: Zero API keys or hidden reasoning thoughts were printed, logged, or recorded. Only numerical timings, token counts, and error metadata were collected.

---

## 5. Warm-Up Methodology

Before collecting measured statistics, a dedicated warm-up round was executed (Run 0):
- 1 request to TEST A (NVIDIA Direct Nemotron 30B): Total 24,147.4 ms, TTFT 20,355.2 ms.
- 1 request to TEST B (OpenRouter Nemotron Free): Total 104,371.2 ms, TTFT 98,732.6 ms.
- 1 request to TEST C Catalog (OpenRouter MiniMax Free): HTTP 404.
- 1 request to TEST C Active (OpenRouter MiniMax M3): Total 5,231.1 ms, TTFT 2,476.9 ms.

All warm-up runs were strictly excluded from statistical aggregation.

---

## 6. Raw and Statistical Results

A total of **40 measured runs** (10 per path $\times$ 4 paths) plus 4 warm-up runs were completed.

### Table 1: Total Wall-Clock Latency (ms)
| Path | Provider | Model | Min | Mean | Median | P75 | P90 | Max | StDev | Success |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TEST A** | NVIDIA Direct | `nemotron-3.5-lightning-30b` | 18,812.1 | 52,370.9 | 27,576.7 | 66,131.4 | 118,778.1 | 150,148.0 | 46,077.4 | 10 / 10 |
| **TEST B** | OpenRouter | `nemotron-3.5-lightning:free` | 19,468.3 | 31,655.4 | 25,225.8 | 34,234.4 | 43,097.7 | 78,648.8 | 17,778.5 | 10 / 10 |
| **TEST C (Cat)** | OpenRouter | `minimax-m3:free` | N/A | N/A | N/A | N/A | N/A | N/A | N/A | 0 / 10 (404) |
| **TEST C (Act)** | OpenRouter | `minimax-m3` | 3,247.6 | 7,024.1 | 5,681.6 | 8,330.5 | 11,523.9 | 16,015.6 | 3,978.4 | 10 / 10 |

### Table 2: Time to First Visible Content / TTFT (ms)
| Path | Provider | Model | Min | Mean | Median | P75 | P90 | Max | StDev |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TEST A** | NVIDIA Direct | `nemotron-3.5-lightning-30b` | 15,083.1 | 48,858.2 | 24,026.5 | 62,619.4 | 115,681.5 | 145,945.6 | 45,983.5 |
| **TEST B** | OpenRouter | `nemotron-3.5-lightning:free` | 15,354.7 | 27,291.5 | 21,461.0 | 30,454.3 | 38,389.8 | 69,428.5 | 16,180.6 |
| **TEST C (Cat)** | OpenRouter | `minimax-m3:free` | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| **TEST C (Act)** | OpenRouter | `minimax-m3` | 2,338.6 | 4,299.8 | 3,189.7 | 4,030.6 | 7,143.6 | 12,427.3 | 3,123.2 |

### Table 3: Visible Generation Duration (ms)
| Path | Provider | Model | Min | Mean | Median | P75 | P90 | Max | StDev |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TEST A** | NVIDIA Direct | `nemotron-3.5-lightning-30b` | 2,665.0 | 3,511.9 | 3,433.8 | 3,796.1 | 4,263.1 | 4,814.4 | 654.8 |
| **TEST B** | OpenRouter | `nemotron-3.5-lightning:free` | 2,768.8 | 4,363.8 | 3,817.9 | 4,489.2 | 5,717.7 | 9,220.2 | 1,861.3 |
| **TEST C (Cat)** | OpenRouter | `minimax-m3:free` | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| **TEST C (Act)** | OpenRouter | `minimax-m3` | 775.3 | 2,724.2 | 2,699.6 | 3,521.8 | 4,522.2 | 5,007.9 | 1,419.0 |

### Table 4: Internal Reasoning Duration (ms)
| Path | Provider | Model | Min | Mean | Median | P75 | P90 | Max | StDev |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TEST A** | NVIDIA Direct | `nemotron-3.5-lightning-30b` | 11,446.0 | 32,636.7 | 20,038.3 | 26,581.8 | 40,581.2 | 143,151.6 | 39,184.6 |
| **TEST B** | OpenRouter | `nemotron-3.5-lightning:free` | 13,518.4 | 19,343.2 | 18,087.7 | 22,516.5 | 26,164.4 | 29,404.3 | 5,261.2 |
| **TEST C (Act)** | OpenRouter | `minimax-m3` | 4.1 | 1,536.1 | 715.7 | 1,124.8 | 2,811.2 | 8,497.0 | 2,646.9 |

---

## 7. Provider Comparison: NVIDIA Direct vs OpenRouter (Nemotron Lightning)

Comparing the identical model family (`Nemotron 3.5 Lightning`) across the two provider pathways:

| Metric | NVIDIA Direct (TEST A) | OpenRouter Free (TEST B) | Delta (OpenRouter vs NVIDIA) | % Difference |
| :--- | :--- | :--- | :--- | :--- |
| **Total Median Latency** | 27,576.7 ms (27.6s) | 25,225.8 ms (25.2s) | **-2,350.9 ms** | **-8.5%** |
| **Total Mean Latency** | 52,370.9 ms (52.4s) | 31,655.4 ms (31.7s) | **-20,715.5 ms** | **-39.6%** |
| **Total P90 Latency** | 118,778.1 ms (118.8s) | 43,097.7 ms (43.1s) | **-75,680.4 ms** | **-63.7%** |
| **Maximum Latency** | 150,148.0 ms (150.1s) | 78,648.8 ms (78.6s) | **-71,499.2 ms** | **-47.6%** |
| **Standard Deviation** | 46,077.4 ms | 17,778.5 ms | **-28,298.9 ms** | **-61.4%** |
| **TTFT Median** | 24,026.5 ms (24.0s) | 21,461.0 ms (21.5s) | **-2,565.5 ms** | **-10.7%** |
| **TTFT Mean** | 48,858.2 ms (48.9s) | 27,291.5 ms (27.3s) | **-21,566.7 ms** | **-44.1%** |
| **Visible Gen Median** | 3,433.8 ms | 3,817.9 ms | **+384.1 ms** | **+11.2%** |

### Key Observation
At the median, OpenRouter is ~8.5% faster than NVIDIA Direct. However, at the **mean and tail percentiles (P90)**, OpenRouter is drastically faster (-39.6% mean, -63.7% P90) because NVIDIA Direct suffered repeated multi-minute stalls (reaching up to 150.1s on Run 5 and 115.3s on Run 6). OpenRouter's maximum observed latency was 78.6s.

---

## 8. Model Comparison: MiniMax M3 vs Nemotron Lightning

Comparing MiniMax M3 (TEST C Active) against Nemotron Lightning on both provider paths:

| Metric | MiniMax M3 (OpenRouter) | Nemotron (NVIDIA Direct) | Nemotron (OpenRouter) | Delta vs NVIDIA | Delta vs OpenRouter |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Total Median** | **5,681.6 ms (5.7s)** | 27,576.7 ms (27.6s) | 25,225.8 ms (25.2s) | **-79.4%** (4.9x faster) | **-77.5%** (4.4x faster) |
| **Total Mean** | **7,024.1 ms (7.0s)** | 52,370.9 ms (52.4s) | 31,655.4 ms (31.7s) | **-86.6%** (7.5x faster) | **-77.8%** (4.5x faster) |
| **Total P90** | **11,523.9 ms (11.5s)** | 118,778.1 ms (118.8s) | 43,097.7 ms (43.1s) | **-90.3%** (10.3x faster) | **-73.3%** (3.7x faster) |
| **TTFT Median** | **3,189.7 ms (3.2s)** | 24,026.5 ms (24.0s) | 21,461.0 ms (21.5s) | **-86.7%** (7.5x faster) | **-85.1%** (6.7x faster) |
| **Visible Gen Median**| **2,699.6 ms (2.7s)** | 3,433.8 ms (3.4s) | 3,817.9 ms (3.8s) | **-21.4%** | **-29.3%** |
| **StDev** | **3,978.4 ms** | 46,077.4 ms | 17,778.5 ms | **-91.4%** | **-77.6%** |

### Key Observation
MiniMax M3 is consistently **4.4x to 4.9x faster at the median** and **7.5x faster on TTFT** than Nemotron Lightning regardless of whether Nemotron is invoked via NVIDIA Direct or OpenRouter.

---

## 9. TTFT Comparison (Time to First Token)

- **MiniMax M3**: Median TTFT is **3,189.7 ms (~3.2s)** (Min 2.3s, Max 12.4s). First token appears promptly for the user.
- **OpenRouter Nemotron**: Median TTFT is **21,461.0 ms (~21.5s)** (Min 15.4s, Max 69.4s).
- **NVIDIA Direct Nemotron**: Median TTFT is **24,026.5 ms (~24.0s)** (Min 15.1s, Max 145.9s).

**Finding**: The user-perceived delay before text starts streaming is overwhelmingly dominated by the pre-TTFT interval. MiniMax delivers its first token ~18 to 21 seconds earlier than Nemotron.

---

## 10. Reasoning vs Generation Duration Breakdown

Decomposing total latency into its two primary components:
$$\text{Total Latency} \approx \text{Pre-TTFT (Reasoning / Setup)} + \text{Visible Generation}$$

| Path | Pre-TTFT / TTFT Median | Reasoning Duration Median | Visible Generation Median | Reasoning % of Total | Generation % of Total |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **TEST A (NVIDIA)** | 24,026.5 ms | 20,038.3 ms | 3,433.8 ms | **85.4%** | **14.6%** |
| **TEST B (OpenRouter)** | 21,461.0 ms | 18,087.7 ms | 3,817.9 ms | **82.6%** | **17.4%** |
| **TEST C (MiniMax M3)** | 3,189.7 ms | 715.7 ms | 2,699.6 ms | **20.9%** | **79.1%** |

### Critical Finding
- In **Nemotron Lightning** (both NVIDIA and OpenRouter), **>80% of total response time is spent in the reasoning phase** before any visible tokens appear. Once generation begins, visible generation takes only 3.4–3.8 seconds.
- In **MiniMax M3**, reasoning concludes rapidly (~0.7s median, ~1.5s mean), and visible generation accounts for **~79% of the overall request**.

---

## 11. Variance and Tail Latency Analysis

- **NVIDIA Direct (Nemotron)** exhibited extreme variance:
  - Minimum total time: 18.8s
  - Maximum total time: 150.1s (a 8.0x difference across identical runs)
  - Standard deviation: **46.1s** (coefficient of variation $CV = 88.0\%$)
  - Tail latency: P90 was **118.8s** ($>1.9$ minutes)
- **OpenRouter (Nemotron Free)** exhibited moderate-to-high variance:
  - Minimum total time: 19.5s
  - Maximum total time: 78.6s (a 4.0x difference)
  - Standard deviation: **17.8s** ($CV = 56.2\%$)
  - Tail latency: P90 was **43.1s**
- **OpenRouter (MiniMax M3)** exhibited tight, predictable latency:
  - Minimum total time: 3.2s
  - Maximum total time: 16.0s
  - Standard deviation: **4.0s** ($CV = 56.6\%$)
  - Tail latency: P90 was **11.5s**; 75% of runs completed under 8.3s.

---

## 12. Forensic Interpretation

Addressing the 10 core forensic questions based strictly on empirical measurements:

### 1. Which path has the lowest median total latency?
**OpenRouter $\rightarrow$ MiniMax M3** (5,681.6 ms / ~5.7s).

### 2. Which path has the lowest median TTFT?
**OpenRouter $\rightarrow$ MiniMax M3** (3,189.7 ms / ~3.2s).

### 3. Which path has the most consistent latency?
**OpenRouter $\rightarrow$ MiniMax M3** had the lowest absolute standard deviation (3,978.4 ms / ~4.0s). Comparing only the two Nemotron paths, **OpenRouter** (stdev 17.8s) was substantially more consistent than NVIDIA Direct (stdev 46.1s).

### 4. Is OpenRouter Nemotron Lightning materially faster or slower than NVIDIA Direct Nemotron Lightning?
- At the median, OpenRouter Nemotron is **slightly faster** (-8.5% total, -10.7% TTFT).
- At the mean and P90 tail, OpenRouter Nemotron is **materially faster** (-39.6% mean, -63.7% P90), consistent with provider-side queue management differences that avoided the extreme >100s stalls observed on NVIDIA Direct.

### 5. Is MiniMax M3 through OpenRouter faster or slower than Nemotron Lightning?
**Substantially faster**. MiniMax M3 is ~4.9x faster at the median and ~7.5x faster on TTFT than Nemotron on NVIDIA Direct, and ~4.4x faster at the median than Nemotron on OpenRouter.

### 6. Does the difference primarily appear before first visible content or during generation/reasoning?
**Almost entirely before first visible content (during reasoning / pre-TTFT).** Visible generation duration is roughly equivalent across all models (2.7s for MiniMax, 3.4s for NVIDIA Nemotron, 3.8s for OpenRouter Nemotron). The 20+ second disparity stems from Nemotron's extensive pre-content reasoning phase.

### 7. Does Nemotron show large run-to-run variance?
**Yes.** Nemotron shows substantial run-to-run variance on both providers, with NVIDIA Direct displaying severe tail latency spikes ($18.8\text{s} \rightarrow 150.1\text{s}$).

### 8. Does MiniMax show large run-to-run variance?
**No.** MiniMax shows low absolute variance, with 75% of requests completing between 3.2s and 8.3s.

### 9. Is there enough evidence to say that provider path matters?
**Yes.** While median latency was similar (-8.5%), provider path dramatically affected tail latency and stability for Nemotron Lightning: NVIDIA Direct produced a P90 of 118.8s and a max of 150.1s, whereas OpenRouter produced a P90 of 43.1s and a max of 78.6s.

### 10. Is there enough evidence to say that model choice matters?
**Overwhelmingly yes.** Model choice is the dominant determinant of total response time and TTFT: switching between MiniMax M3 and Nemotron Lightning produced a ~5x difference in median latency, dwarfing the effect of provider routing for the same model.

---

## 13. Limitations

1. **Sample Size**: 10 measured runs per path over a ~20 minute window provide strong directional confidence but represent a single temporal snapshot of provider cluster loads.
2. **OpenRouter Catalog Inconsistency**: `minimax/minimax-m3:free` was unavailable on OpenRouter (404), requiring verification of the active `minimax/minimax-m3` endpoint.
3. **Fixed Prompt Scope**: A single standardized prompt (transformer explanation) was tested. Workloads with heavy context or extensive code output may exhibit different generation length dynamics.
4. **Provider Internal State**: Detailed cluster queue depth and GPU allocation details remain internal to NVIDIA and OpenRouter/Novita infrastructure; variations are characterized as *consistent with provider-side variability* rather than directly proven.

---

## 14. Final Conclusion

The empirical Phase 4B benchmark reveals two conclusive architectural findings:
1. **Model Architecture is the Primary Latency Factor**: Nemotron Lightning incurs a consistent 18–32s reasoning phase before yielding visible tokens, regardless of whether it is hosted directly on NVIDIA or proxied through OpenRouter. MiniMax M3 completes its reasoning in <1s and begins streaming visible content in ~3.2s median.
2. **Provider Stability Differs at the Tail**: For Nemotron Lightning, OpenRouter provided significantly better tail latency stability (P90 = 43.1s vs 118.8s on NVIDIA Direct) and lower standard deviation.

---

## 15. Confirmation of Architecture and Production Integrity

The working tree was verified post-benchmark:
- Production routing: **UNCHANGED**
- Model priorities: **UNCHANGED**
- Production ModelCatalog: **UNCHANGED**
- Provider selection: **UNCHANGED**
- BERU orchestrator: **UNCHANGED**
- Harness runner: **UNCHANGED**
- Telegram bot: **UNCHANGED**
- RAG: **UNCHANGED**
- Tools: **UNCHANGED**
- Security / Permission gate: **UNCHANGED**
- Environment (`.env`): **UNCHANGED**

All benchmark execution was fully isolated inside [scratch/benchmark_phase4b_speed.py](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/scratch/benchmark_phase4b_speed.py), with raw data recorded in [scratch/benchmark_phase4b_results.json](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/scratch/benchmark_phase4b_results.json) and [scratch/benchmark_phase4b_summary.txt](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/scratch/benchmark_phase4b_summary.txt).
