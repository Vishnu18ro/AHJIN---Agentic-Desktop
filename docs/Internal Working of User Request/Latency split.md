Absolutely, Commander. Here's a clean **AHJIN 2.0 documentation section** capturing the current latency architecture, tool identification, and the exact behavior we just established.

# AHJIN 2.0 — Latency Telemetry & Tool Identification

## 1. Overview

AHJIN 2.0 uses runtime telemetry to measure the time spent across different stages of a request.

The latency system distinguishes between:

* **AHJIN** — local orchestration/runtime overhead
* **Provider** — provider-side / TTFT latency for the final model
* **Tool execution** — time spent executing tools
* **Model** — model generation after TTFT
* **Other** — remaining unclassified/residual runtime
* **Total** — overall request duration

Tool latency is measured dynamically for every tool execution.

---

# 2. Tool Execution Flow

The execution flow is:

```text
User Request
     │
     ▼
BERU / Orchestrator
     │
     ▼
Tool Intent Planner
     │
     ▼
ToolInvocationRequest
     │
     ▼
HarnessRunner
     │
     ├── Start tool timer
     │
     ├── Execute tool
     │
     ├── Calculate elapsed time
     │
     ├── Record tool name + duration
     │
     └── End tool timer
     │
     ▼
RequestTimer
     │
     ├── Aggregate tool duration
     │
     └── Store individual tool timings
     │
     ▼
TaskDispatcher
     │
     ▼
RuntimeInfo
     │
     ├── timing
     ├── tool_timings
     └── executed_tools
     │
     ▼
Telegram Footer Formatter
```

---

# 3. How Tool Latency Is Measured

Inside `HarnessRunner`, AHJIN measures the exact execution duration using `time.perf_counter()`.

Conceptually:

```python
t0_tool = time.perf_counter()

step_res = await self._execute_tool_step(step)

tool_elapsed_ms = (
    time.perf_counter() - t0_tool
) * 1000.0
```

The result is then recorded:

```python
timer.record_tool(
    tool_name,
    tool_elapsed_ms
)
```

Therefore, the measured duration represents the execution time of the tool step itself.

---

# 4. Tool Identity Is Not Determined by an LLM

A critical architectural point:

> **The LLM does not classify or rename a tool after execution.**

The Tool Intent Planner may decide:

```text
tool_name = "file_search"
```

But once execution begins, AHJIN's deterministic runtime already knows which registered tool is executing.

For example:

```text
FileSearchTool
      ↓
tool_name = "file_search"
      ↓
RequestTimer
      ↓
("file_search", 23211 ms)
```

The user-facing name is then generated through a static mapping:

```python
TOOL_DISPLAY_MAP = {
    "file_search": "File Search",
    "file_read": "File Read",
    "file_send": "File Send",
    "web_search": "Web Search",
    "browser": "Browser",
}
```

Thus:

```text
"file_search"
      ↓
"File Search"
```

is a deterministic Python dictionary lookup.

**No LLM inference is involved.**

---

# 5. Two Tool-Timing Data Structures

AHJIN maintains both aggregate and per-tool timing information.

### Aggregate timing

The timing system maintains an overall tool execution duration:

```text
tool_execution = total time spent executing tools
```

For example:

```text
File Search = 23,538 ms
File Send   = 5 ms

Aggregate:
tool_execution = 23,543 ms
```

### Individual tool timings

AHJIN also maintains:

```python
_tool_timings = [
    ("file_search", 23538.0),
    ("file_send", 5.0),
]
```

This allows the Telegram adapter to display individual tools when appropriate.

---

# 6. Single-Tool vs Multi-Tool Display

This is the source of the behavior observed during testing.

The Telegram footer contains conditional formatting.

## One tool

When exactly **one tool** executes:

```python
if len(tool_timings) == 1:
    _, t_ms = tool_timings[0]
    lines.append(
        f"├─ Tool: {int(round(t_ms))}ms"
    )
```

Notice:

```python
_, t_ms = tool_timings[0]
```

The actual tool name exists, but `_` intentionally discards it **for this particular display line**.

Therefore:

```text
├─ Tool: 23211ms
```

is displayed.

The actual tool identity is subsequently displayed separately:

```text
Tool: File Search
```

### Important

This does **not** mean AHJIN failed to identify the tool.

Internally:

```text
("file_search", 23211)
```

still exists.

The formatter simply chooses the generic word:

```text
Tool
```

for the single-tool latency row.

---

# 7. Multiple Tools

When multiple tools execute:

```python
if len(tool_timings) > 1:
```

AHJIN iterates through the individual timing records and uses their names.

For example:

```text
("file_search", 23538)
("file_send", 5)
```

becomes:

```text
├─ File Search: 23538ms
├─ File Send: 5ms
```

The metadata section then displays:

```text
Tools: File Search, File Send
```

This is necessary because multiple tools need to be distinguished.

---

# 8. Why the Resume Request Displayed `Tool`

For:

```text
Find my Resume-.pdf
```

AHJIN executed only:

```text
file_search
```

Internally:

```text
tool_timings =
[
    ("file_search", 23211)
]
```

Because:

```text
len(tool_timings) == 1
```

the footer selected the single-tool formatting branch:

```text
├─ Tool: 23211ms
```

Then the metadata section identified the actual tool:

```text
Tool: File Search
```

Therefore:

```text
├─ Tool: 23211ms
...
Tool: File Search
```

means:

> **File Search executed for 23.211 seconds.**

There are not two different tools.

---

# 9. Why the Send-Resume Request Displayed `File Search`

For:

```text
send resume
```

AHJIN needed two operations:

```text
1. Find the file
2. Send the file
```

Therefore the orchestrator produced:

```text
file_search
     ↓
file_send
```

The timing data became:

```text
[
    ("file_search", 23538),
    ("file_send", 5)
]
```

Since:

```text
len(tool_timings) == 2
```

AHJIN used the multi-tool branch:

```text
├─ File Search: 23538ms
├─ File Send: 5ms
```

---

# 10. Comparison

| Request     | Tools Executed          | Internal Timing                            | Footer                                   |
| ----------- | ----------------------- | ------------------------------------------ | ---------------------------------------- |
| Find Resume | File Search             | `file_search = 23211ms`                    | `Tool: 23211ms`                          |
| Send Resume | File Search → File Send | `file_search = 23538ms`, `file_send = 5ms` | `File Search: 23538ms`, `File Send: 5ms` |

The difference is **only presentation logic**.

The underlying timing mechanism is the same.

---

# 11. Complete Latency Architecture

AHJIN's current runtime telemetry can be represented as:

```text
                         TOTAL
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
      AHJIN             Provider             Tools
        │                  │                  │
        │                 TTFT          ┌─────┴─────┐
        │                                │           │
        │                           File Search   File Send
        │                                │           │
        │                              23.5s        5ms
        │
        └──────────────────────────────────────────────┐
                                                       │
                                                     Model
                                                       │
                                               Generation time
                                                       │
                                                       ▼
                                                     Other
                                                       │
                                                       ▼
                                                     Total
```

The exact footer presentation depends on which timing records exist.

---

# 12. `Other`

`Other` represents residual request time after the explicitly displayed timing categories.

Conceptually:

```text
Other =
Total
- AHJIN
- Provider
- Tools
- Model
- Telegram
```

It can therefore contain time from stages that are measured internally but aren't displayed as their own footer row, such as planner activity and certain Telegram/runtime overhead.

---

# 13. Important Distinction: Tool Planner vs Tool Execution

These are completely different things.

### Tool Intent Planner

```text
LLM
↓
"What tool should I use?"
↓
file_search
```

Its latency belongs to the **planner/runtime portion**, not File Search latency.

### Tool Execution

```text
FileSearchTool
↓
Actually scans filesystem
↓
23,211 ms
```

That 23.211 seconds belongs to **Tool execution latency**.

Therefore:

> **Planner latency ≠ File Search latency.**

---

# 14. Current Resume Example

The 02:45 request:

```text
├─ AHJIN: 2ms
├─ Provider: 195188ms
├─ Tool: 23211ms
├─ Model: 122952ms
├─ Other: 1475ms
└─ Total: 342828ms

Tool: File Search
```

should be interpreted as:

```text
AHJIN orchestration      =       2 ms
Final provider TTFT      = 195,188 ms
File Search               =  23,211 ms
Final model generation    = 122,952 ms
Other                     =   1,475 ms
Total                     = 342,828 ms
```

And:

```text
Tool: File Search
```

identifies the `Tool` latency row as **File Search**.

---

# 15. Source Locations

Current implementation locations identified during the forensic audit:

```text
Tool execution measurement
src/ahjin/harness/runner.py
HarnessRunner.run()
HarnessRunner.run_stream()

Tool timing storage
src/ahjin/telemetry/timing.py
RequestTimer.record_tool()

Tool identity
src/ahjin/tools/file_search.py
FileSearchTool.tool_name

Timing propagation
src/ahjin/core/dispatcher.py
TaskDispatcher.dispatch()
TaskDispatcher.dispatch_stream()

Tool display mapping
src/ahjin/interfaces/telegram/bot.py
TOOL_DISPLAY_MAP
_tool_display_name()

Footer formatting
src/ahjin/interfaces/telegram/bot.py
_build_runtime_footer()
```

---

# 16. Definitive Architecture Rule

The current AHJIN behavior can be summarized in one rule:

```text
                    TOOL EXECUTES
                         │
                         ▼
             ┌──────────────────────┐
             │ name + duration      │
             │ file_search + 23.2s  │
             └──────────┬───────────┘
                        │
                 Telegram formatter
                        │
             ┌──────────┴───────────┐
             │                      │
          1 tool                 >1 tool
             │                      │
             ▼                      ▼
      "Tool: 23.2s"       "File Search: 23.2s"
             │             "File Send: 5ms"
             ▼
      "Tool: File Search"
```

**Therefore, the current behavior is intentional formatting logic, not tool misclassification, not LLM classification, and not a telemetry measurement error.**
