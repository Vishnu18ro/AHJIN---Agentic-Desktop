# AHJIN 2.0 — HealthTracker Cooldown & Progressive Recovery
###JUST LIKE ALMOST TEMORRARY EXCLUSION UNTIL IT PERFORMS NEXT TIME###
### Specific Architecture Documentation

This section documents the **exact MiniMax failure/cooldown behavior** observed in the 22:34 and 23:12 Telegram requests.

---

## 1. Failure Threshold

AHJIN does **not** immediately place a model into cooldown after its first failure.

The current health progression is:

```text
Failure #1
    ↓
DEGRADED
    ↓
Still eligible
    ↓
NO cooldown

Failure #2
    ↓
DEGRADED
    ↓
Still eligible
    ↓
NO cooldown

Failure #3
    ↓
UNHEALTHY
    ↓
60-second cooldown STARTS
```

Therefore:

> **The 60-second cooldown begins only when the model reaches 3 consecutive failures.**

---

# 2. What "Consecutive" Means

The failure counter belongs to the model's health state.

A successful invocation resets the counter:

```text
❌ #1
   ↓
❌ #2
   ↓
✅ SUCCESS
   ↓
counter = 0
status = HEALTHY
```

The model therefore gets a fresh three-failure sequence after recovery.

---

# 3. First Telegram Request — 22:34

At the beginning of the first request:

```text
MiniMax = HEALTHY
failures = 0
```

### Planner

```text
MiniMax
   ↓
❌ HTTP 402
   ↓
Failure #1
   ↓
DEGRADED
```

Because this is only failure #1:

```text
60-second cooldown = ❌ NOT STARTED
```

The Planner request-locally excludes MiniMax and moves to Lightning.

---

### Harness

When Harness begins, MiniMax is:

```text
DEGRADED
failures = 1
```

**DEGRADED models remain eligible.**

Therefore Harness can select MiniMax again:

```text
Harness
   ↓
MiniMax
   ↓
❌ HTTP 402
   ↓
Failure #2
   ↓
DEGRADED
```

Again:

```text
60-second cooldown = ❌ NOT STARTED
```

Harness then reroutes to Lightning.

### End of first request

```text
MiniMax failures = 2
Status = DEGRADED
Cooldown = NONE
```

So the first request ends with:

```text
MiniMax ❌ #1
     ↓
Lightning
     ↓
Harness → MiniMax ❌ #2
     ↓
Lightning
```

---

# 4. The ~37–40 Minute Gap

Between the two Telegram requests:

```text
22:34
   ↓
~37–40 minutes
   ↓
23:12
```

**No 60-second cooldown was running during this period.**

Why?

Because MiniMax had only reached:

```text
2 consecutive failures
```

and the cooldown threshold is:

```text
3 failures
```

Therefore:

```text
22:35
MiniMax = DEGRADED
Failures = 2
Cooldown = NONE
```

The AHJIN process remained alive, so this health state persisted in memory.

---

# 5. Second Telegram Request — 23:12

At the beginning of the second request:

```text
MiniMax
Failures = 2
Status = DEGRADED
```

Because DEGRADED models are still eligible:

```text
Planner
   ↓
ModelRouter
   ↓
MiniMax
```

MiniMax is therefore selected again.

---

## 6. Third Failure — The Critical Event

MiniMax fails again:

```text
MiniMax
   ↓
❌ HTTP 402
   ↓
Failure #3
```

Now the threshold is reached:

```text
Failure #3
    ↓
UNHEALTHY
    ↓
60-second cooldown STARTS
```

The cooldown begins **at the exact moment the third failure is recorded**.

It does not begin after some additional delay.

---

# 7. Immediate Planner Recovery

The Planner does not wait 60 seconds.

Immediately after MiniMax becomes unhealthy:

```text
MiniMax
   ↓
UNHEALTHY
   ↓
60s cooldown
```

the Planner moves forward:

```text
MiniMax ❌
    ↓
Nemotron Lightning
```

Lightning successfully returns:

```text
NO_TOOL
```

---

# 8. Harness Starts 13 Seconds Later

This is why the second Telegram message showed **Direct**.

At Harness start:

```text
MiniMax
Status = UNHEALTHY
Cooldown = 60s
Elapsed = ~13s
```

Therefore:

```text
13s < 60s
```

MiniMax is unavailable.

The Harness does **not** retry MiniMax.

Instead:

```text
Harness
   ↓
ModelRouter
   ↓
MiniMax → ❌ unavailable
   ↓
Lightning → ✅
```

Lightning is therefore selected **directly by Harness**.

No Harness failure occurred.

So:

```text
was_rerouted = False
```

and Telegram displays:

```text
Path: Direct
```

---

# 9. Why the Two Telegram Messages Look Different

### 22:34

MiniMax was only `DEGRADED`.

Therefore Harness could try it again:

```text
Planner:
MiniMax ❌ #1
     ↓
Lightning

Harness:
MiniMax ❌ #2
     ↓
Lightning
```

Harness itself experienced a failure.

Therefore:

```text
Path: ↪ Rerouted
From: MiniMax M3
Reason: HTTP 402
```

---

### 23:12

MiniMax reached `UNHEALTHY` during Planner:

```text
Planner:
MiniMax ❌ #3
     ↓
UNHEALTHY
     ↓
60s cooldown
     ↓
Lightning
```

Then Harness started while MiniMax was still inside that cooldown:

```text
Harness:
MiniMax → unavailable
     ↓
Lightning directly
```

Harness itself experienced **no failure**.

Therefore:

```text
Path: Direct
```

---

# 10. Complete Timeline

```text
22:34
│
├─ Planner → MiniMax ❌ #1
│              ↓
│          DEGRADED
│          No cooldown
│
├─ Planner → Lightning
│
├─ Harness → MiniMax ❌ #2
│              ↓
│          DEGRADED
│          No cooldown
│
└─ Harness → Lightning
       
       ↓
   ~37–40 minutes
       ↓

23:12
│
├─ Planner → MiniMax ❌ #3
│              ↓
│          UNHEALTHY
│              ↓
│       60s cooldown START
│
├─ Planner → Lightning
│
│       ↓ ~13 seconds
│
└─ Harness
       ↓
   MiniMax blocked
   (13s < 60s)
       ↓
   Lightning directly
       ↓
   SUCCESS
```

---

# 11. Recovery Behavior After Becoming UNHEALTHY

Once the three-failure threshold has been reached, AHJIN enters a different phase.

```text
UNHEALTHY
    ↓
60s cooldown
    ↓
Next real request after cooldown
    ↓
MiniMax recovery probe
```

If the probe succeeds:

```text
MiniMax
   ↓
✅ SUCCESS
   ↓
HEALTHY
   ↓
consecutive_failures = 0
```

The three-failure cycle is then reset.

If the recovery probe fails:

```text
MiniMax
   ↓
❌ FAILURE
   ↓
remain UNHEALTHY
   ↓
60s cooldown starts/resets
```

No background probing occurs.

---

# 12. Core Architecture Rule

The entire mechanism can be summarized as:

```text
             MODEL FAILURE
                   │
                   ▼
          HealthTracker updates
                   │
          ┌────────┴─────────┐
          │                  │
      #1 or #2              #3+
          │                  │
          ▼                  ▼
      DEGRADED           UNHEALTHY
          │                  │
          │              60s cooldown
          │                  │
          ▼                  ▼
   Still eligible       Temporarily
                         unavailable
          │                  │
          └────────┬─────────┘
                   ▼
            ModelRouter
                   │
                   ▼
        next eligible candidate
```

### The fundamental rule

> **Failures 1–2 make the model DEGRADED but still selectable. Failure 3 makes it UNHEALTHY and starts the 60-second cooldown. After becoming UNHEALTHY, AHJIN progressively uses other eligible models while the failed model is suppressed. Once the cooldown expires, a real request can test that model again. A successful recovery resets the failure counter and returns it to HEALTHY.**

This is the **specific HealthTracker + cooldown behavior** that explains the two Telegram requests.
