# AHJIN 2.0 — New Latency Telemetry Documentation

## 1. Purpose

This document defines the current **AHJIN 2.0 latency telemetry contract**.

It explains exactly what each footer latency field measures, where Planner and Harness fallback time is attributed, and how the **30-second inactivity watchdog** affects latency.

---

# 2. Runtime Latency Architecture

AHJIN has two distinct LLM stages:

1. **Tool Intent Planner** — determines tool intent and creates an execution plan.
2. **Harness / Final Response** — performs the final response generation.

They may use the **same model/provider back-to-back**, but they are still **separate LLM requests** with independent provider latency, streaming behavior, health state, and watchdog timing.

```text
User Message
     │
     ▼
┌──────────────────────────────┐
│ TOOL INTENT PLANNER          │
│                              │
│ Determines tool intent       │
│ Creates execution plan       │
└──────────────┬───────────────┘
               │
               ▼
        Execution Plan
               │
               ▼
┌──────────────────────────────┐
│ HARNESS / FINAL RESPONSE     │
│                              │
│ Generates final response     │
└──────────────┬───────────────┘
               │
               ▼
        Telegram Response
```

---

# 3. Latency Telemetry Contract

| Field        | Meaning                                                                                          | Fallback included?              | Watchdog included?           |
| ------------ | ------------------------------------------------------------------------------------------------ | ------------------------------- | ---------------------------- |
| **AHJIN**    | Internal AHJIN orchestration/processing overhead                                                 | No                              | No                           |
| **Tool LLM** | Entire Tool Intent Planner stage                                                                 | **Yes — Planner fallbacks**     | **Yes — Planner watchdogs**  |
| **Provider** | Provider-side latency of the **final successful Harness candidate**                              | Only final successful candidate | No failed-candidate watchdog |
| **Model**    | Generation/processing latency of the **final successful Harness candidate**                      | Only final successful candidate | No failed-candidate watchdog |
| **Other**    | Failed Harness attempts + watchdog time + fallback/routing overhead + residual/unattributed time | **Yes — Harness fallbacks**     | **Yes — Harness watchdogs**  |
| **Total**    | Complete end-to-end request latency                                                              | All                             | All                          |

---

# 4. Tool LLM — Planner Latency

**Tool LLM is a stage-level aggregate for the complete Tool Intent Planner operation.**

It is **not merely the generation time of the last Planner model**.

It contains:

* Planner candidate selection/routing
* Every Planner provider/model attempt
* Provider startup, queueing and TTFT for Planner attempts
* Planner reasoning and generation
* Planner failures such as HTTP 402
* Planner fallback attempts
* Planner 30-second inactivity watchdog time
* Planner completion
* Small residual Planner overhead

### Example

```text
MiniMax M3
    ↓
  402 ❌
    ↓
Nemotron Lightning
    ↓
 Planner success ✅
```

The **complete elapsed Planner operation** is attributed to:

```text
Tool LLM
```

---

# 5. Planner 30-Second Watchdog

The Planner watchdog is a **per-candidate inactivity threshold**.

It is **NOT**:

* a fixed 30-second delay
* a minimum execution time
* a total Planner wall-clock limit

Each Planner candidate receives its own **30-second inactivity window**.

### Legitimate progress includes:

* Reasoning chunks
* Visible content
* Legitimate stream/SSE progress

Every legitimate progress event **resets the 30-second inactivity deadline**.

### Example — no timeout

```text
0s   Request starts
5s   Reasoning chunk       → RESET
9s   Reasoning chunk       → RESET
14s  Reasoning chunk       → RESET
18s  Content chunk         → RESET
25s  Content chunk         → RESET
31s  Reasoning chunk       → RESET
40s  Completion
     ↓
     SUCCESS
```

The candidate can therefore run **longer than 30 seconds** without timing out.

### Example — timeout

```text
0s   Request starts
     ↓
     No legitimate progress
     ↓
10s  Nothing
     ↓
20s  Nothing
     ↓
30s  Nothing
     ↓
❌ TIMEOUT
```

Then:

```text
Current candidate
      ↓
Request-local exclusion
      ↓
Next eligible fallback
```

The elapsed timeout time is included in:

> **Tool LLM**

---

# 6. Harness — Final Response Latency

Harness is the **final-response generation stage**.

Its latency is not exposed as a single `Harness` row.

Instead, the Harness portion is attributed across:

```text
Provider
Model
Other
```

Conceptually:

```text
HARNESS
   │
   ├── Failed candidates
   │      ↓
   │    OTHER
   │
   └── Final successful candidate
          │
          ├── Provider
          │
          └── Model
```

---

# 7. Provider

**Provider** represents the provider-side latency attributed to the **final successful Harness candidate**.

It can include:

* Request handling
* Provider startup
* Queueing
* Infrastructure initialization
* Time-to-first-token / usable response acquisition

Examples:

```text
OpenRouter
    ↓
Nemotron Lightning
```

```text
NVIDIA
    ↓
Nemotron Lightning 30B
```

```text
Ollama
    ↓
Gemma 3 4B
```

### Important

`Provider` does **not** mean "cloud provider."

Ollama is also a provider in AHJIN's telemetry because it is the runtime responsible for serving the model.

Therefore:

```text
Provider = Ollama
Model    = Gemma 3 4B
```

is completely valid.

---

# 8. Model

**Model** represents the generation/processing latency attributed to the **final successful Harness model**.

Conceptually:

```text
Provider request
       ↓
Provider starts returning response
       ↓
Model generation
       ↓
Completion
```

For example:

```text
Nemotron Lightning

Provider ≈ 13.5s
Model    ≈ 2.1s
```

This means most of the waiting happened during provider startup/TTFT, rather than during actual generation.

---

# 9. Other

**Other is a residual attribution bucket.**

It is **not exclusively watchdog time**.

It can contain:

* Failed Harness candidate elapsed time
* Harness 30-second inactivity watchdog time
* Failed-provider/model time from candidates that did not become the final successful candidate
* Harness fallback/routing overhead
* Small residual or otherwise unattributed timing

### Most important interpretation

When `Other` is very large, the major cause is usually:

> **One or more Harness candidates consumed the full 30-second inactivity watchdog before fallback.**

For example:

```text
Harness Candidate A
        ↓
30s no progress
        ↓
❌ TIMEOUT

Harness Candidate B
        ↓
30s no progress
        ↓
❌ TIMEOUT

Harness Candidate C
        ↓
SUCCESS
```

The two failed 30-second periods contribute to:

```text
Other
```

They do **not** become the final candidate's `Provider` or `Model` time.

---

# 10. Harness 30-Second Watchdog

Harness uses the same **per-candidate inactivity** principle.

### Progress resets the timer

```text
Request
  ↓
Reasoning chunk
  ↓
RESET 30s
  ↓
Reasoning chunk
  ↓
RESET 30s
  ↓
Content
  ↓
RESET 30s
  ↓
Completion
```

### No progress causes fallback

```text
Request
  ↓
No progress
  ↓
30 seconds
  ↓
❌ TIMEOUT
  ↓
Request-local exclusion
  ↓
Next candidate
```

The elapsed time of the failed Harness candidate contributes to:

> **Other**

---

# 11. Same Model Used Back-to-Back

Planner and Harness can use the **same model and provider consecutively**, but they are still two independent LLM requests.

Example:

```text
Planner
   ↓
Nemotron Lightning / OpenRouter
   ↓
SUCCESS
```

Then:

```text
Harness
   ↓
Nemotron Lightning / OpenRouter
   ↓
NEW REQUEST
```

The second request does **not** inherit:

* Provider latency
* Stream progress
* Watchdog state
* Connection timing
* Model execution state

Therefore, the same model can experience different latency in Planner and Harness.

---

# 12. Worked Example — 12:52 `HI`

Observed:

```text
Total = 210,953 ms
       ≈ 210.95 seconds
       ≈ 3.52 minutes
```

## Planner chain

```text
MiniMax M3
    ↓
402 ❌

Nemotron 3.5 Lightning
    ↓
30s watchdog ❌

Nemotron Lightning 30B
    ↓
30s watchdog ❌

Gemma 3 4B
    ↓
30s watchdog ❌

Nemotron Ultra
    ↓
30s watchdog ❌

Nemotron Ultra 550B
    ↓
30s watchdog ❌

Kimi K3
    ↓
SUCCESS ✅
```

Therefore:

```text
Tool LLM = 173,050 ms
```

This includes:

* MiniMax failure
* Five 30-second Planner watchdog periods
* Planner fallback routing
* Kimi Planner execution
* Associated Planner overhead

### All of that belongs to:

> **Tool LLM**

---

## Harness chain

```text
MiniMax M3
    ↓
402 ❌

Nemotron 3.5 Lightning
    ↓
SUCCESS ✅
```

Therefore:

```text
Provider = 34,312 ms
Model    = 1,438 ms
Other    = 2,151 ms
```

There were **no Harness 30-second watchdog failures** in this request.

Therefore `Other` remains small.

---

# 13. Reading the Telegram Footer

### High `Tool LLM`

Means:

> Planner and/or Planner fallback consumed significant time.

Especially if several Planner candidates hit the 30-second watchdog.

---

### High `Provider`

Means:

> The final successful Harness provider had significant startup/queue/TTFT latency.

---

### High `Model`

Means:

> The final successful Harness model spent significant time generating/processing.

---

### `Other ≈ 30s`

Usually indicates:

> One Harness candidate hit the 30-second inactivity watchdog.

---

### `Other ≈ 60s`

Usually indicates:

> Two Harness candidates hit the 30-second inactivity watchdog.

---

### Important

`Other` is **not guaranteed to be an exact multiple of 30 seconds**, because it can also contain:

* HTTP failures such as 402
* fallback/routing overhead
* failed-provider/model time
* residual timing

---

# 14. Complete Latency Model

```text
                    TOTAL
                      │
       ┌──────────────┼────────────────┐
       │              │                │
       ▼              ▼                ▼
    AHJIN          TOOL LLM         HARNESS
       │              │                │
       │              │         ┌──────┴──────┐
       │              │         │             │
       │              │      FAILED        SUCCESS
       │              │      ATTEMPTS          │
       │              │         │          ┌───┴───┐
       │              │         ▼          ▼       ▼
       │              │       OTHER     PROVIDER  MODEL
       │              │
       │              ├── Planner attempts
       │              ├── Provider time
       │              ├── Model time
       │              ├── Planner fallbacks
       │              └── Planner watchdogs
       │
       └── Internal AHJIN overhead
```

---

# 15. Final Quick Reference

```text
TOOL LLM
= Entire Planner operation
+ Planner provider/model time
+ Planner failures
+ Planner fallback attempts
+ Planner watchdog time
+ Planner overhead


PROVIDER
= Provider-side latency
  of the final successful Harness candidate


MODEL
= Generation/processing latency
  of the final successful Harness candidate


OTHER
= Failed Harness attempts
+ Harness watchdog time
+ Failed-provider/model time
+ Harness fallback/routing overhead
+ Residual/unattributed time


TOTAL
= Complete end-to-end request latency
```

## Core Principle

> **The 30-second watchdog is not a mandatory 30-second wait. It is the maximum allowed period of inactivity for one candidate.**

If legitimate reasoning/content/stream progress arrives:

```text
Progress → RESET 30s timer
```

If there is no legitimate progress for 30 seconds:

```text
30s silence → TIMEOUT → FALLBACK
```

Therefore:

**Planner watchdog → Tool LLM**

**Harness watchdog → Other**

**Final successful Harness provider wait → Provider**

**Final successful Harness generation → Model**

**Everything → Total**
