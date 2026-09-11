# AHJIN 2.0 — Model Routing, Fallback & Health System

### Technical Documentation Brief

## 1. Purpose

AHJIN 2.0 uses a **dynamic ModelRouter + HealthTracker architecture** to select the best available model for each request and automatically recover from provider/model failures.

The system is designed around four principles:

1. **MiniMax M3 is the primary model.**
2. **Failures trigger immediate request-local rerouting.**
3. **Failed models are temporarily suppressed for 60 seconds.**
4. **Local Ollama models provide the final execution fallback.**

---

# 2. Complete Request Architecture

```text
                         USER REQUEST
                              │
                              ▼
                            BERU
                              │
                              ▼
                    ┌───────────────────┐
                    │ Tool Intent       │
                    │ Planner           │
                    │ LLM #1            │
                    └─────────┬─────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                 TOOL                NO_TOOL
                    │                   │
                    ▼                   │
              Tool Execution            │
                    │                   │
                    └─────────┬─────────┘
                              ▼
                    ┌───────────────────┐
                    │ HarnessRunner     │
                    │ LLM #2            │
                    └─────────┬─────────┘
                              │
                              ▼
                       FINAL RESPONSE
```

Both the **Tool Intent Planner** and **HarnessRunner** independently use:

```text
ModelRouter
     +
HealthTracker
     +
ModelCatalog
```

They share health information but maintain their own **request-local excluded-model state**.

---

# 3. Primary Model Strategy

Current cloud priority:

### ALL / Primary

```text
MiniMax M3
minimax/minimax-m3
OpenRouter
Priority: 300
```

MiniMax is the preferred starting point for both **FAST/LIGHT** and **HEAVY** tasks when its capabilities satisfy the request.

AHJIN does **not permanently replace MiniMax** after a failure.

Instead:

```text
MiniMax fails
      ↓
temporarily excluded
      ↓
next eligible model
      ↓
after cooldown expires
      ↓
MiniMax becomes eligible again
```

---

# 4. Model Tiers

## FAST / LIGHT

```text
MiniMax M3
      ↓
Nemotron 3.5 Lightning — OpenRouter Free
      ↓
Nemotron 3.5 Lightning — NVIDIA Direct
      ↓
other eligible FAST/ALL models
      ↓
Gemma 3 4B — Local
```

## HEAVY

```text
MiniMax M3
      ↓
Nemotron 3 Ultra — OpenRouter Free
      ↓
Nemotron 3 Ultra — NVIDIA Direct
      ↓
Kimi K3
      ↓
DeepSeek V4 Pro
      ↓
DeepSeek V4 Flash
      ↓
Qwen 3 8B — Local
```

The exact candidate selected is ultimately determined by **ModelRouter**, health, capabilities, tier, and priority—not by hardcoded fallback calls.

---

# 5. How Model Selection Works

The ModelRouter conceptually evaluates:

```text
Request
   │
   ▼
Required capabilities
   │
   ▼
Health / availability
   │
   ▼
Preferred tier
   │
   ▼
Tier candidates
   │
   ▼
Cross-tier candidates if necessary
   │
   ▼
Priority
   │
   ▼
Selected model
```

### Important distinction

**Tier preference is flexible.**

**Capability requirements are hard constraints.**

Therefore:

> AHJIN may leave the requested tier, but it should not select a model that fails the required capability gate.

---

# 6. LIGHT → HEAVY Fallback

If a LIGHT request has no suitable FAST models:

```text
LIGHT request
     │
     ▼
FAST candidates exhausted
     │
     ▼
HEAVY candidates
     │
     ▼
Capability check
     │
 ┌───┴────┐
YES       NO
 │         │
 ▼         ▼
Use       Skip
model     model
```

Therefore:

**LIGHT → HEAVY is allowed.**

But the HEAVY candidate must still satisfy the request's hard capability requirements.

---

# 7. HEAVY → LIGHT Fallback

The reverse is also possible.

```text
HEAVY request
      │
      ▼
HEAVY candidates exhausted
      │
      ▼
LIGHT candidates
      │
      ▼
Capability check
      │
 ┌────┴─────┐
YES         NO
 │           │
 ▼           ▼
Use        SKIP
```

Therefore:

**HEAVY → LIGHT is allowed only when the LIGHT candidate is capable enough for that request.**

A model is **never used simply because it is the only remaining model** if it fails the hard capability requirements.

---

# 8. 60-Second Health Cooldown

AHJIN uses a **per-model 60-second cooldown**.

Health states:

```text
HEALTHY
   │
   │ failure
   ▼
DEGRADED
   │
   │ repeated failures
   ▼
UNHEALTHY
```

Current behavior:

```text
Failure #1 → DEGRADED
Failure #2 → DEGRADED
Failure #3+ → UNHEALTHY
```

An UNHEALTHY model becomes unavailable for normal selection until:

```text
60 seconds elapsed
```

---

# 9. IMPORTANT: Cooldown Does NOT Mean Waiting

AHJIN does **not** do this:

```text
MiniMax fails
    ↓
WAIT 60 seconds
    ↓
try Lightning
```

Instead:

```text
MiniMax fails
    ↓
MiniMax cooldown starts
    ↓
IMMEDIATELY select next eligible model
    ↓
Lightning
```

The 60 seconds only controls **when MiniMax can become eligible again**.

---

# 10. Recovery Probe

After the 60-second cooldown expires, MiniMax can be selected again on a future request.

Example:

```text
23:33:50
MiniMax fails
     ↓
60-second cooldown

23:34:10
New request
     ↓
MiniMax still excluded

23:34:51+
     ↓
MiniMax eligible again
```

AHJIN does **not continuously ping MiniMax in the background**.

Recovery occurs **on demand**, when a real request causes the Router to consider the model again.

If the recovery probe succeeds:

```text
MiniMax → SUCCESS
          ↓
HEALTHY
          ↓
normal priority restored
```

If it fails again:

```text
MiniMax → FAILURE
          ↓
UNHEALTHY
          ↓
new 60-second cooldown
```

---

# 11. Multiple Models Can Be in Cooldown

Each model has its **own independent health state**.

Example:

```text
MiniMax      ❌ 60s cooldown
Lightning    ❌ 60s cooldown
Ultra        ❌ 60s cooldown
Kimi         ❌ 60s cooldown
DeepSeek     ❌ 60s cooldown
```

AHJIN does **not wait for them**.

It searches for another currently eligible candidate.

---

# 12. Failure Switching Latency

When a provider fails quickly, switching itself can typically happen in roughly:

**~1–3 seconds**

depending on how quickly the failed provider returns the error and how much routing/orchestration work is required.

This is **not a guaranteed 1–3 second limit**.

For example:

```text
MiniMax
   ↓
HTTP 402 returned in ~1.5s
   ↓
HealthTracker records failure
   ↓
ModelRouter selects Lightning
   ↓
Lightning request starts
```

There is **no 60-second delay between models**.

The 60 seconds belongs only to the failed model's future eligibility.

---

# 13. Planner vs Harness Fallback

AHJIN has two major LLM execution stages.

### Stage 1 — Tool Intent Planner

The Planner determines:

```text
TOOL
or
NO_TOOL
```

It uses ModelRouter and can reroute when its selected model fails.

Its model failures are request-local:

```text
excluded_models = {
    failed_model
}
```

The Planner can continue through eligible candidates, with only a **50-iteration defensive safety guard** to prevent an infinite loop.

That 50 is **not a normal fallback limit**.

---

### Stage 2 — HarnessRunner

HarnessRunner generates the actual final answer.

It also uses ModelRouter and its own request-local exclusion state.

However, the current implementation has:

```text
max_recovery_attempts = 2
```

for cloud recovery before falling back to the local execution path.

This is an important distinction:

```text
Planner
→ broad candidate iteration
→ defensive 50-iteration guard

Harness
→ maximum 2 cloud recovery attempts
→ LocalExecutor
```

---

# 14. Shared HealthTracker

Planner and Harness share the same global in-process HealthTracker.

Example:

```text
Planner
   │
   ├── MiniMax fails
   │
   ▼
HealthTracker
   │
   ▼
MiniMax marked unhealthy
   │
   ▼
Harness starts
   │
   ▼
ModelRouter sees MiniMax unavailable
   │
   ▼
select another model
```

Therefore, a failure discovered by the Planner can influence the Harness immediately.

---

# 15. Request-Local Exclusion vs Global Health

These are two different mechanisms.

### HealthTracker

Global, process-level state:

> “MiniMax is currently unhealthy.”

### Request-local exclusion

Current request state:

> “We already tried MiniMax for this request and it failed, so don't retry it during this attempt.”

Together they prevent unnecessary repeated failures.

---

# 16. Local Fallback

Cloud models are not the final safety net.

AHJIN has local Ollama execution.

### HEAVY

```text
Cloud candidates
      ↓
No suitable cloud candidate
      ↓
Qwen 3 8B
      ↓
90-second execution budget
```

If Qwen fails/times out:

```text
Qwen 3 8B
      ↓
Gemma 3 4B
```

Gemma has a:

```text
60-second execution budget
```

---

### FAST / General

The local fallback is:

```text
Gemma 3 4B
```

---

# 17. Complete HEAVY Failure Scenario

This is the important end-to-end case:

```text
             HEAVY REQUEST
                   │
                   ▼
              MiniMax M3
                   │
                  ❌
                   │
                   ▼
           Ultra OpenRouter
                   │
                  ❌
                   │
                   ▼
            Ultra NVIDIA
                   │
                  ❌
                   │
                   ▼
                Kimi K3
                   │
                  ❌
                   │
                   ▼
            DeepSeek Pro
                   │
                  ❌
                   │
                   ▼
           DeepSeek Flash
                   │
                  ❌
                   │
                   ▼
       Can remaining LIGHT model
       satisfy hard capabilities?
              /          \
            YES           NO
             │             │
             ▼             ▼
          LIGHT         SKIP
                           │
                           ▼
                     Local Qwen 3 8B
                           │
                         failure
                           ▼
                     Local Gemma 3 4B
                           │
                         failure
                           ▼
                         ERROR
```

Final error:

```text
INVOCATION_FAILED
"All execution paths failed"
```

---

# 18. What Happens If a LIGHT Model Is Incapable?

Suppose:

```text
HEAVY request
     ↓
All HEAVY cloud models unavailable
     ↓
Lightning available
```

But Lightning does **not** satisfy the hard capability requirements.

AHJIN does:

```text
Lightning
   ↓
❌ capability gate
   ↓
SKIP
   ↓
next candidate
```

It does **not** force Lightning to answer.

If no capable cloud candidate remains:

```text
Cloud exhausted
     ↓
LocalExecutor
```

---

# 19. What Happens If EVERYTHING Fails?

The final execution chain is:

```text
Cloud models
      ↓
Cross-tier capable models
      ↓
Local Qwen / Gemma
      ↓
Everything fails
      ↓
AhjinError
      ↓
INVOCATION_FAILED
```

Therefore AHJIN has a genuine **last-resort local execution path** before returning an error.

---

# 20. Timeout Architecture

There are several independent timeout budgets.

| Component             | Timeout/Budget |
| --------------------- | -------------: |
| Tool Intent Planner   |        **15s** |
| OpenRouter HTTP       |        **90s** |
| NVIDIA HTTP           |        **90s** |
| Ollama HTTP           |        **60s** |
| Local Qwen execution  |        **90s** |
| Local Gemma execution |        **60s** |

The **90-second rule has two distinct roles**:

1. Cloud HTTP timeout.
2. Qwen local execution budget.

They should not be treated as the same timeout.

---

# 21. No Background Health Polling

AHJIN currently does **not** continuously probe unhealthy models.

There is no:

```text
every 60 seconds
    ping MiniMax
```

Instead:

```text
Model fails
    ↓
60s cooldown
    ↓
wait naturally
    ↓
next real request
    ↓
Router may select model again
    ↓
recovery probe
```

This keeps the system simple and avoids unnecessary API calls.

---

# 22. Example: MiniMax Failure During Planner

```text
User
 ↓
BERU
 ↓
Planner
 ↓
MiniMax
 ↓
❌ 402
 ↓
HealthTracker → MiniMax unhealthy
 ↓
ModelRouter
 ↓
Lightning
 ↓
NO_TOOL
 ↓
Harness
 ↓
Lightning
 ↓
Final answer
```

The user still receives an answer.

---

# 23. Example: MiniMax Failure During Harness

```text
User
 ↓
BERU
 ↓
Planner → MiniMax → NO_TOOL
 ↓
Harness → MiniMax
 ↓
❌ failure
 ↓
HealthTracker
 ↓
ModelRouter
 ↓
Lightning
 ↓
Final answer
```

Here the Harness itself performed a reroute.

---

# 24. Example: Both Stages See Different Models

It is completely valid for:

```text
Planner → MiniMax
Harness → Lightning
```

or:

```text
Planner → Lightning
Harness → Ultra
```

because Planner and Harness make **independent ModelRouter selections** at different points in the request lifecycle.

They are not required to use the same LLM.

---

# 25. Current Observability Limitation

The current Telegram footer primarily reports the **HarnessRunner route**.

Therefore this situation:

```text
Planner:
MiniMax ❌ → Lightning

Harness:
Lightning → SUCCESS
```

may display:

```text
Path: Direct
```

even though a reroute occurred earlier during Planner.

This is an **observability/UX issue**, not necessarily a routing failure.

A better future representation would expose the complete request path:

```text
Planner: MiniMax → Lightning
Harness: Lightning
Path: ↪ Rerouted
```

---

# 26. Latency Interpretation

AHJIN's latency measurements should be interpreted carefully.

### AHJIN

Local orchestration/routing/context-processing time.

### Tools

Actual dynamic tool execution time.

Example:

```text
File Search: 18004ms
File Send: 10ms
```

These are measured per request and should remain dynamic.

### Model

Observable model-stream duration.

It includes things such as:

* request/network transport
* provider queue/routing
* prefill
* time to first token
* generation
* streaming transport

It is **not automatically pure GPU inference time**.

### Total

Actual wall-clock request duration.

---

# 27. Final Architecture

```text
                         ┌─────────────────────┐
                         │     USER REQUEST    │
                         └──────────┬──────────┘
                                    │
                                    ▼
                              ┌───────────┐
                              │   BERU    │
                              └─────┬─────┘
                                    │
                                    ▼
                         ┌────────────────────┐
                         │ TOOL INTENT        │
                         │ PLANNER             │
                         └─────────┬──────────┘
                                   │
                              ModelRouter
                                   │
                         ┌─────────┴─────────┐
                         │                   │
                       TOOL              NO_TOOL
                         │                   │
                         ▼                   │
                    ToolRegistry             │
                         │                   │
                         └─────────┬─────────┘
                                   ▼
                         ┌────────────────────┐
                         │   HARNESS RUNNER   │
                         └─────────┬──────────┘
                                   │
                              ModelRouter
                                   │
                                   ▼
                         ┌────────────────────┐
                         │  CLOUD MODEL       │
                         │  FALLBACK CHAIN    │
                         └─────────┬──────────┘
                                   │
                         no suitable candidate
                                   │
                                   ▼
                         ┌────────────────────┐
                         │  LOCAL EXECUTOR    │
                         │  Qwen → Gemma      │
                         └─────────┬──────────┘
                                   │
                              all fail
                                   │
                                   ▼
                    INVOCATION_FAILED ERROR
```

### Core control plane

```text
              ┌──────────────────┐
              │   ModelCatalog   │
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐
              │   ModelRouter    │
              └────────┬─────────┘
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
      Capability Gate       HealthTracker
             │                   │
             └─────────┬─────────┘
                       ▼
                Selected Model
```

## Final rule set

> **MiniMax first.**

> **Failure → immediately reroute.**

> **Failed model → 60-second temporary suppression.**

> **Cooldown ≠ waiting.**

> **After cooldown → model can be recovery-probed on a real request.**

> **Tier preference can be relaxed.**

> **Hard capability requirements cannot be relaxed.**

> **Planner and Harness select independently.**

> **Health state is shared.**

> **Exclusions are request-local.**

> **Cloud exhaustion → LocalExecutor.**

> **Qwen → Gemma for HEAVY local fallback.**

> **Everything fails → `INVOCATION_FAILED`.**

That is the **current AHJIN 2.0 fallback/routing behavior** in one consolidated specification.
