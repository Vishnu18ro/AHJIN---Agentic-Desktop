harness_and_tool_2LLM_CALLS.md check for little detailed info
Yes, Commander. Let's ignore the UX for a moment and understand **the actual internal execution model**. The key is:

> **One user message can cause multiple LLM calls. The Tool Planner and HarnessRunner are separate stages, and each can independently select/retry models.**

## 1. The core architecture

```text
USER MESSAGE
     │
     ▼
   Telegram
     │
     ▼
    BERU
     │
     ▼
┌─────────────────────┐
│  TOOL INTENT        │
│  PLANNER            │
│                     │
│ "Do I need tools?"  │
└──────────┬──────────┘
           │
           ▼
      LLM CALL #1
           │
       ┌───┴────┐
       │        │
    TOOL      NO_TOOL
       │        │
       ▼        ▼
Tool execution  ┌──────────────┐
       │        │ HARNESS      │
       └───────►│ RUNNER       │
                └──────┬───────┘
                       │
                       ▼
                  LLM CALL #2
                       │
                       ▼
                 FINAL ANSWER
```

There can be **more calls** if a model fails and AHJIN reroutes.

---

# 2. What exactly does the Tool Planner do?

The Planner's job is **not to answer the user**.

It asks:

> **"What action should AHJIN take?"**

For example:

### User:

> `hi`

Planner LLM receives something like:

```text
User request: hi

Determine whether a tool is required.
Return structured intent.
```

LLM responds:

```text
NO_TOOL
```

That's it.

It doesn't generate the final `Hello...` response.

---

# 3. Then HarnessRunner takes over

HarnessRunner's job is:

> **"Now actually produce the answer."**

It receives the user's request + whatever context/tool results are relevant.

Then ModelRouter selects the best available model.

For a healthy system:

```text
Planner
   ↓
MiniMax M3
   ↓
NO_TOOL

HarnessRunner
   ↓
MiniMax M3
   ↓
Final answer
```

So one simple `hi` can involve:

```text
LLM #1 = Planner
LLM #2 = Final answer
```

---

# 4. But they DON'T have to use the same model

This is important.

Because **both call ModelRouter independently**.

Example:

```text
             USER
               │
               ▼
           PLANNER
               │
          ModelRouter
               │
               ▼
        MiniMax M3
               │
            NO_TOOL
               │
               ▼
          HARNESS
               │
          ModelRouter
               │
               ▼
      Nemotron Lightning
               │
               ▼
            ANSWER
```

Why could that happen?

Because between the two selections:

* health can change
* a model can fail
* request-local exclusions can change
* capability requirements can differ
* tier requirements can differ

So **Planner model ≠ necessarily final model**.

---

# 5. Normal request — no tools, no errors

Example:

> "Explain recursion."

```text
USER
 ↓
BERU
 ↓
Tool Planner
 ↓
LLM #1
 ↓
NO_TOOL
 ↓
HarnessRunner
 ↓
ModelRouter
 ↓
LLM #2
 ↓
FINAL ANSWER
```

Example actual models:

```text
Planner:  MiniMax M3
Harness:  MiniMax M3
```

Two LLM calls.

---

# 6. Normal tool request

Example:

> "Find my Resume.pdf."

Now the Planner decides:

```text
LLM #1
 ↓
FILE_SEARCH
```

Then AHJIN executes the tool:

```text
FileSearchTool
 ↓
find Resume.pdf
```

Then the Harness can use the result.

```text
USER
 ↓
BERU
 ↓
Planner
 ↓
LLM #1
 ↓
FILE_SEARCH
 ↓
FileSearchTool
 ↓
Resume.pdf found
 ↓
HarnessRunner
 ↓
LLM #2
 ↓
FINAL RESPONSE
```

If the task requires sending it:

```text
Planner
 ↓
file_search
 ↓
FileSearchTool
 ↓
file_read / file_send
 ↓
HarnessRunner / response
```

So **tools are not LLMs**. They are actual AHJIN capabilities executed between the reasoning stages.

---

# 7. Now what happens if Planner's model fails?

This is where your 23:33 case comes from.

User:

> `hi`

Planner wants an LLM.

```text
Planner
 ↓
Router
 ↓
MiniMax
 ↓
❌ 402
```

Planner has its **request-local fallback mechanism**.

It excludes MiniMax for this request and asks Router again:

```text
MiniMax ❌
    ↓
Lightning ✅
    ↓
NO_TOOL
```

Then Harness starts.

At this point MiniMax has also been marked unhealthy by the health tracker.

So:

```text
Harness
 ↓
Router
 ↓
MiniMax ❌ 60s cooldown
 ↓
Lightning ✅
 ↓
FINAL ANSWER
```

Therefore:

```text
LLM CALLS:

#1 Planner:
MiniMax ❌
Lightning ✅

#2 Harness:
Lightning ✅

Final:
Lightning response
```

That's why **three actual model invocations** occurred for that one `hi`:

```text
MiniMax       ❌
Lightning     ✅  ← Planner
Lightning     ✅  ← Final answer
```

---

# 8. What if Harness itself fails?

Different situation.

Suppose Planner succeeds:

```text
Planner → MiniMax → NO_TOOL
```

Then Harness:

```text
Harness → MiniMax → ❌ 402
```

Harness has its own request-local fallback:

```text
MiniMax ❌
   ↓
Lightning
   ↓
FINAL ANSWER
```

Then:

```text
LLM CALL #1
Planner → MiniMax → NO_TOOL

LLM CALL #2
Harness → MiniMax → ❌

LLM CALL #3
Harness → Lightning → ✅

FINAL → Lightning
```

Here the **Harness itself performed the reroute**.

That's the kind of reroute the current footer knows how to label.

---

# 9. What if BOTH Planner and Harness have failures?

Then you can get something like:

```text
USER
 ↓
PLANNER
 ↓
MiniMax ❌
 ↓
Lightning ❌
 ↓
Next eligible model ✅
 ↓
NO_TOOL
 ↓
HARNESS
 ↓
That model ❌
 ↓
Next eligible model ✅
 ↓
FINAL ANSWER
```

Potentially many LLM calls.

Your current architecture isn't limited to:

> "try only two models."

It can continue through eligible catalog candidates, with the defensive loop guard preventing an infinite loop.

---

# 10. The three major scenarios side-by-side

| Scenario            | Planner                         | Harness                   | Final        |
| ------------------- | ------------------------------- | ------------------------- | ------------ |
| **Normal**          | MiniMax → NO_TOOL               | MiniMax → ✅               | MiniMax      |
| **Planner failure** | MiniMax ❌ → Lightning → NO_TOOL | Lightning → ✅             | Lightning    |
| **Harness failure** | MiniMax → NO_TOOL               | MiniMax ❌ → Lightning → ✅ | Lightning    |
| **Both fail**       | A ❌ → B ❌ → C → NO_TOOL         | C ❌ → D → ✅               | D            |
| **Tool request**    | MiniMax → FILE_SEARCH           | tool result → LLM         | Final answer |

---

# 11. And this explains your two `hi`s

### 🟥 23:33 `hi`

**Proven from your logs:**

```text
                 USER
                  │
                  ▼
               PLANNER
                  │
              MiniMax
                  │
                402 ❌
                  │
             Lightning
                  │
               NO_TOOL
                  │
                  ▼
               HARNESS
                  │
          MiniMax unavailable
          (new 60s cooldown)
                  │
             Lightning
                  │
                  ▼
               ANSWER
```

So the Planner rerouted.

The Harness didn't need to reroute because **Lightning was already the best eligible model when it started**.

That's why current telemetry says `Direct`.

---

### 🟦 A genuinely direct request

If MiniMax is healthy:

```text
USER
 ↓
Planner
 ↓
MiniMax → NO_TOOL
 ↓
Harness
 ↓
MiniMax → ANSWER
```

No fallback anywhere.

```text
Path: Direct
```

---

### 🟨 A Harness-level reroute

```text
USER
 ↓
Planner
 ↓
MiniMax → NO_TOOL
 ↓
Harness
 ↓
MiniMax ❌
 ↓
Lightning
 ↓
ANSWER
```

Here the Runner knows:

> **"I personally had to reroute."**

So:

```text
Path: ↪ Rerouted
```

---

## The single most important thing

Think of it as:

```text
             ONE USER REQUEST
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
     PLANNER STAGE       FINAL STAGE
       "WHAT?"             "ANSWER"
          │                   │
       LLM call             LLM call
          │                   │
       may retry            may retry
          │                   │
          └─────────┬─────────┘
                    ▼
              FINAL MESSAGE
```

**Planner and Harness are separate consumers of LLMs. Both use ModelRouter. Both can independently encounter failures and reroute. And the final Telegram message currently reports mainly what happened in the final Harness stage — which is why the Planner's hidden reroute can disappear from the UX.**
