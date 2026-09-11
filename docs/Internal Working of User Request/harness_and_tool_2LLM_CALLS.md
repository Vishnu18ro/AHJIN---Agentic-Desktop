**Exactly — but not “randomly.”** That's the important correction.

Both the **ToolIntentPlanner** and **HarnessRunner** ask the **ModelRouter** for a model. The Router deterministically chooses the **best eligible model according to priority, tier, capabilities, health, latency constraints, and request-local exclusions**.

### Normal flow

```text
USER
 ↓
BERU
 ↓
ToolIntentPlanner
 ↓
ModelRouter
 ↓
Best eligible FAST model
 ↓
LLM
 ↓
NO_TOOL / TOOL
 ↓
HarnessRunner
 ↓
ModelRouter
 ↓
Best eligible response model
 ↓
LLM
 ↓
ANSWER
```

With your current catalog, **MiniMax M3 is priority #1**, so when it's healthy/eligible:

```text
Planner → MiniMax
Harness → MiniMax
```

Not random.

---

## 🔥 What if the LLM errors?

Then yes — **internal request-local rerouting happens.**

For example:

```text
Planner
   ↓
MiniMax
   ↓
❌ 402
   ↓
exclude MiniMax FOR THIS REQUEST
   ↓
ModelRouter
   ↓
Nemotron Lightning
   ↓
✅
```

If Lightning also fails:

```text
MiniMax ❌
   ↓
Lightning ❌
   ↓
next eligible candidate
   ↓
next...
   ↓
until candidates exhausted
```

There is **no artificial "only 2 or 3 retries" limit** in your current architecture. The `50` defensive guard exists only to prevent an infinite loop.

---

## And the Harness does the same

Suppose the Planner succeeds:

```text
Planner → MiniMax → NO_TOOL ✅
```

Then Harness starts:

```text
Harness
   ↓
MiniMax
   ↓
❌ provider error
   ↓
exclude MiniMax
   ↓
Router
   ↓
Lightning
   ↓
✅
   ↓
ANSWER
```

So **both layers have their own request-local recovery**.

### One important distinction

The Planner's failure does **not permanently tell the Harness**:

> "Never use MiniMax."

Instead, the **health tracker** records MiniMax's failure globally for the current AHJIN process, while the Planner also maintains its own **request-local exclusion set**.

That's why your 23:33 case was:

```text
Planner:
MiniMax ❌
   ↓
Lightning ✅

Harness:
MiniMax is now unhealthy/cooldown
   ↓
Lightning selected
```

So the architecture is basically:

**ModelRouter = who should I try?**
**ProviderGateway = execute the call + record health**
**Planner/Harness = if my call fails, exclude that model and ask Router for another.**

That's the internal fallback machinery you've built.
**Exactly.** You've got it now.

There are **two internal LLM selections**, and the user currently only sees the **final Harness model** in the footer.

### Internally

```text
USER REQUEST
     │
     ▼
   BERU
     │
     ▼
┌──────────────────┐
│ ToolIntentPlanner│
│                  │
│ ModelRouter      │
└────────┬─────────┘
         │
         ▼
   LLM #1
   (e.g. MiniMax)
         │
      NO_TOOL
         │
         ▼
┌──────────────────┐
│  HarnessRunner   │
│                  │
│  ModelRouter     │
└────────┬─────────┘
         │
         ▼
   LLM #2
   (e.g. Lightning)
         │
         ▼
      ANSWER
```

### And YES — they can be different models.

For example:

```text
Planner:
MiniMax M3
   ↓
NO_TOOL

Harness:
Nemotron Lightning
   ↓
Final answer
```

Or:

```text
Planner:
Nemotron Lightning
   ↓
NO_TOOL

Harness:
MiniMax M3
   ↓
Final answer
```

Or even:

```text
Planner:
MiniMax ❌
   ↓
Lightning
   ↓
NO_TOOL

Harness:
Lightning
   ↓
Answer
```

The Router makes a **fresh selection for each stage** based on what is currently eligible.

---

### Why you don't see this

Your current Telegram footer basically reports:

```text
⚡ AHJIN Runtime
Model: Nemotron Lightning
Route: FAST
...
Path: Direct
```

That `Model:` is primarily the **final Harness model**.

The Planner's internal model selection is largely invisible.

So your 23:33 request was actually:

```text
                    "hi"
                      │
                      ▼
                  PLANNER
                      │
                MiniMax M3
                  ❌ 402
                      │
                ↪ Lightning
                      │
                  NO_TOOL
                      │
                      ▼
                  HARNESS
                      │
                  Lightning
                      │
                      ▼
                    ANSWER
```

Yet the user sees:

> `Model: Nemotron Lightning`
> `Path: Direct`

That's why we called it a **UX/observability issue**.

The architecture is doing more internally than the current footer communicates.



/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

**2 LLM CALL BUT SAME HEALTH BAR**
**Exactly.** You've got the architecture now. ✅

### Two stages, one shared health system

```text
USER REQUEST
     │
     ▼
┌──────────────────┐
│ ToolIntentPlanner│
│                  │
│ ModelRouter ─────┼──────┐
└────────┬─────────┘      │
         │                │
      LLM call            │
         │                │
      Tool/No Tool        │
                          │
         ▼                │
┌──────────────────┐      │
│  HarnessRunner   │      │
│                  │      │
│ ModelRouter ─────┼──────┤
└────────┬─────────┘      │
         │                │
      LLM call            │
         │                │
      FINAL ANSWER        │
                          ▼
                 Shared HealthTracker
```

### `Path` specifically

The current footer's logic is essentially:

```text
Harness starts
     ↓
Does Harness itself have to fallback?
     │
   YES ─────→ Path: ↪ Rerouted
     │
   NO
     ↓
Path: Direct
```

So:

**Planner reroutes → current footer may still say `Direct`.**

**Harness reroutes → footer says `↪ Rerouted`.**

That's the UX issue we identified.

### And yes — shared health

Both Planner and Harness use the **same `ModelHealthTracker` through the ModelRouter/ProviderGateway architecture**.

So if Planner's MiniMax call gets a 402:

```text
Planner → MiniMax ❌
             ↓
      Shared HealthTracker
             ↓
      MiniMax unhealthy
             ↓
      60s cooldown
```

Then milliseconds later Harness asks:

```text
Harness → ModelRouter
             ↓
      Shared HealthTracker
             ↓
      MiniMax unavailable
             ↓
      Lightning
```

That's why your **23:33 case** behaved exactly that way.

So the clean mental model is:

> **Planner and Harness are two separate LLM consumers → both use ModelRouter → both share the same health state → each can independently reroute → current `Path` only reports Harness-level rerouting.**
