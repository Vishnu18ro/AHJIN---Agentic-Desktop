In your AHJIN logs, **`deterministic` is not an LLM model.** It means AHJIN handled that part of the request using a **rule-based/local deterministic decision**, rather than asking another LLM to generate the result.

For your `send resume` request:

```text
User
 ↓
Tool Intent Planner
 ↓
MiniMax ❌
 ↓
DeepSeek V4 Flash
 ↓
Tool intent = File Search + File Send
 ↓
Deterministic tool execution
 ↓
Resume sent 📄
```

So when you see:

```text
Model: deterministic
Provider: AHJIN
```

it means:

> **AHJIN itself produced the final tool-operation response without needing an LLM for that final response.**

That's actually a **good thing** for tool actions.

For example, after AHJIN already knows:

```text
intent = SEND_FILE
file = Resume-.pdf
```

there is no reason to spend another LLM call just to say:

> "Sent Resume-.pdf."

AHJIN can deterministically construct that response.

### One UX point

I actually think this is worth making clearer later. Instead of:

```text
Model: deterministic
Provider: AHJIN
```

for tool-only operations, something like:

```text
Model: Tool Execution
Provider: AHJIN
```

or

```text
Model: Deterministic
Provider: AHJIN
```

would be more understandable.

But **don't change it yet**. The underlying behavior is correct; we can polish that label separately after we finish validating the current runtime.
