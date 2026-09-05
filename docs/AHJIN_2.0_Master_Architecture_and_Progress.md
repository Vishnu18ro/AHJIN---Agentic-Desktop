# AHJIN 2.0 — Master Architecture & Technical Progress Report

## A Comprehensive Guide for First-Time Viewers, Engineers, and Evaluators

---

## 1. Executive Summary & Core Philosophy

### What is AHJIN 2.0?
**AHJIN 2.0** stands for:

$$\mathbf{A}\text{gentic }\mathbf{H}\text{ybrid }\mathbf{J}\text{ustified }\mathbf{I}\text{ntelligence }\mathbf{N}\text{etwork}$$

AHJIN 2.0 is a personal **Agentic AI Operating Layer (AIOS)** built in Python 3.12+. It acts as an autonomous digital operating interface between a human user and their computing environment (operating system, local files, web resources, automated browsers, and neural inference engines).

### The Foundational Golden Rule
> **THE MODEL IS NOT AHJIN. AHJIN IS THE OPERATING LAYER SURROUNDING INTELLIGENCE.**

A Large Language Model (such as Nemotron, MiniMax, DeepSeek, Gemma, or Qwen) is merely an **intelligence resource** used by AHJIN. AHJIN handles task understanding, tool planning, sandboxed system execution, context assembly, retrieval, security boundaries, model routing, observation tracking, streaming, and failure recovery.

```text
                  TRADITIONAL CONVERSATIONAL CHATBOT:
                  User ──► Single LLM ──► Text Only (No System Action)

                  AHJIN 2.0 AGENTIC OPERATING LAYER:
                  User ──► Natural Language Goal
                             │
                             ▼
                           BERU (Intent & Capability Analysis)
                             │
                             ▼
                    Tool Intent Planning & Security Gate
                             │
                             ▼
                    Agentic Harness (Step Orchestration)
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
       Model Layer       Tool Layer     Knowledge / RAG
     (Hybrid Routing)  (Files/Web/GUI)    (BGE-M3/SQLite)
            │                │                │
            └────────────────┼────────────────┘
                             ▼
                Observations & Execution State
                             │
                             ▼
                  Model Reasoning & Grounding
                             │
                             ▼
           Verified Response & File Delivery ──► User
```

### The THINK → KNOW → ACT Mental Model
For any user request, AHJIN coordinates three core operational pillars:

```text
                                  HUMAN
                                    │
                                    ▼
                             Natural Language
                                    │
                                    ▼
                                  AHJIN
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       │                            │                            │
       ▼                            ▼                            ▼
     THINK                        KNOW                          ACT
       │                            │                            │
       ▼                            ▼                            ▼
  Model Layer                  Knowledge/RAG                 Tool Layer
┌──────────────┐             ┌──────────────┐             ┌──────────────┐
│ ModelCatalog │             │  BGE-M3 Dense│             │  SystemInfo  │
│ ModelRouter  │             │  SQLite Store│             │  FileSearch  │
│ Cloud Fleet  │             │  Page-Aware  │             │  FileRead    │
│ Local Ollama │             │  Context     │             │  FileSend    │
└──────────────┘             └──────────────┘             │  WebSearch   │
                                                          │  Browser     │
                                                          └──────────────┘
```

---

## 2. High-Level System Architecture

AHJIN 2.0 is architected as a modular monolith in `src/ahjin/`. It enforces clean, unidirectional boundaries between cognitive orchestration, runtime execution, model selection, environmental tools, knowledge stores, and user interfaces.

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           INTERFACE LAYER                               │
│  Telegram Bot Adapter (`src/ahjin/interfaces/telegram/`)                │
│  ├── Progressive Chunked Streaming (1.0s throttled intermediate edits)  │
│  ├── Document Attachment Dispatcher (up to 50MB binary files)           │
│  └── Live Diagnostics (`/health`, `/models`)                            │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ TaskRequest
┌────────────────────────────────────▼────────────────────────────────────┐
│                               AHJIN CORE                                │
│  TaskDispatcher (`src/ahjin/core/dispatcher.py`)                        │
│  └── Normalizes inputs, manages session state, dispatches TaskResult    │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ TaskRequest
┌────────────────────────────────────▼────────────────────────────────────┐
│                    BERU (Cognitive Orchestration)                       │
│  Orchestrator (`src/ahjin/beru/orchestrator.py`)                        │
│  ├── Capability Analysis (requires_code, reasoning, vision)            │
│  ├── Execution Strategy Formulator (FAST vs. HEAVY tiering)             │
│  ├── LLM Tool Intent Planner (`src/ahjin/beru/tool_planner.py`)        │
│  └── Deterministic Tool Intent Fallback (`src/ahjin/beru/tools.py`)     │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ ExecutionPlan
┌────────────────────────────────────▼────────────────────────────────────┐
│                         AGENTIC HARNESS RUNNER                          │
│  HarnessRunner (`src/ahjin/harness/runner.py`)                          │
│  ├── Step Sequencing Loop & ExecutionState                              │
│  ├── ContextAssembler (injects [TOOL RESULTS] & grounding prompts)      │
│  ├── ResponseVerifier (validates structural integrity)                  │
│  ├── Same-Request Cloud Rerouting Budget (max_recovery_attempts = 2)   │
│  └── Local Fallback Controller (Ollama on cloud exhaustion or offline) │
└─────────┬──────────────────────────┬──────────────────────────┬─────────┘
          │ ToolInvocation           │ CapabilityRequirements   │ Retrieval
┌─────────▼──────────────┐ ┌─────────▼──────────────┐ ┌─────────▼──────────────┐
│       TOOL LAYER       │ │      MODEL LAYER       │ │   KNOWLEDGE LAYER      │
│ ToolRegistry           │ │ ModelCatalog           │ │ DocumentIngestor       │
│ PermissionGate         │ │ ModelRouter (5-pass)   │ │ TextChunker (pypdf)    │
│ SafePathPolicy         │ │ ModelHealthTracker     │ │ BGE-M3 (1024-dim)      │
│ ├── SystemInfoTool     │ │ ProviderGateway        │ │ SQLiteVectorStore      │
│ ├── FileSearchTool     │ │ ├── OpenRouter (Cloud) │ │ (`ahjin_rag.db`)       │
│ ├── FileReadTool       │ │ ├── NVIDIA NIM (Cloud) │ └────────────────────────┘
│ ├── FileSendTool       │ │ └── Ollama (Local)     │
│ ├── WebSearchTool      │ └────────────────────────┘
│ └── BrowserTool        │
└─────────┬──────────────┘
          │ Observation / StepResult
          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      OBSERVATION / EXECUTION STATE                      │
│  ExecutionState (`src/ahjin/harness/state.py`)                          │
│  └── Accumulates tool outputs, timings, attachment paths, and errors   │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ Assembled Context
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                             MODEL REASONING                             │
│  Selected Model processes grounded context and generates synthesis      │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ Final Response & Attachments
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       FINAL RESPONSE & ATTACHMENTS                      │
│  Telegram Client receives markdown response + runtime footer + files    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Deep-Dive & Responsibilities

### 1. Interface Layer (`src/ahjin/interfaces/`)
- **Telegram Bot Adapter (`bot.py`)**: Translates Telegram updates into standard `TaskRequest` domain objects. 
- **Progressive Streaming**: Implements chunked response streaming with 1.0-second throttled edits to respect Telegram API rate limits.
- **File Attachments**: Detects binary files staged in `TaskResult.file_attachments` (up to 50MB) and dispatches real document attachments directly to the chat window.
- **Diagnostic Commands**: Renders live `/health` and `/models` operational footers showing latency Exponential Moving Averages (EMA) and model circuit-breaker states.

### 2. AHJIN Core (`src/ahjin/core/`)
- **TaskDispatcher (`dispatcher.py`)**: Manages the request lifecycle, correlation IDs, and TaskResult packaging without making cognitive planning or model routing decisions.

### 3. BERU Cognitive Orchestration (`src/ahjin/beru/`)
- **Capability Extractor**: Evaluates task text for capability flags:
  - `requires_code`: Programming syntax, functions, algorithms.
  - `requires_reasoning`: Math, logic, analytical explanations.
  - `requires_vision`: Image references or visual queries.
- **Execution Strategy Generator**: Emits `ExecutionStrategy` governing tier preference (`FAST` vs. `HEAVY`), quality weightings (`quality` vs. `speed`), recovery policies (`REROUTE` vs. `FAIL_FAST`), and retry limits.
- **LLM Tool Intent Planner (`tool_planner.py`)**: Uses an LLM to generate structured JSON tool invocation requests. Enforces JSON schema validation, parameter whitelisting (`SAFE_FIELDS_WHITELIST`), and subpath normalization.
- **Deterministic Tool Fallback (`tools.py`)**: If the planner encounters malformed JSON or times out, execution gracefully falls back to deterministic regex pattern matching (`detect_tool_intent`).

### 4. Agentic Harness Runtime (`src/ahjin/harness/`)
- **HarnessRunner (`runner.py`)**: The central execution coordinator. Sequences `PlanStep` execution across tools and models.
- **ExecutionState (`state.py`)**: Thread-safe container accumulating tool outputs, step latencies, errors, and staged attachment paths.
- **ContextAssembler (`context.py`)**: Assembles `ContextualizedPrompt` objects. Formats prior tool observations into `[TOOL RESULTS]` blocks with explicit instructions: *"Base your response strictly on the tool observation results above... Do not invent details."*
- **ResponseVerifier (`verifier.py`)**: Structural boundary checking model outputs for non-emptiness and absence of corrupted truncation markers.

### 5. Hybrid Model Layer (`src/ahjin/models/` & `src/ahjin/local/`)
- **ModelCatalog (`catalog.py`)**: In-memory registry of active cloud descriptors and local Ollama descriptors.
- **ModelRouter (`router.py`)**: Zero-latency, in-memory 5-pass selection pipeline:
  1. *Hard Capability Eligibility Gate* (incapable models can **never** beat capable models)
  2. *Health Availability Filter* (excludes unhealthy or failed models)
  3. *Hard Latency Constraint Pass* (`max_latency_ms`)
  4. *Tier Preference Match* (`FAST` vs `HEAVY`)
  5. *Two-Key Ranking Pass* (priority ordinal key + blended quality/latency score)
- **ModelHealthTracker (`health.py`)**: Dynamic operational circuit breaker managing model health states (`HEALTHY`, `DEGRADED`, `UNHEALTHY`, `RECOVERY_PROBE_ELIGIBLE`).
- **LocalExecutor (`src/ahjin/local/executor.py`)**: Manages air-gapped local Ollama execution (`gemma3:4b` for FAST, `qwen3:8b` for HEAVY). Protects Qwen with an application-level **90-second timeout**, cleanly falling back to Gemma with the original prompt preserved.

### 6. Tool Ecosystem & Security (`src/ahjin/tools/` & `src/ahjin/security/`)
- **SafePathPolicy (`security/path_policy.py`)**: Restricts file operations strictly to authorized user roots (`Workspace`, `Desktop`, `Documents`, `Downloads`). Blocks path traversal (`..`), sensitive files (`.env`, `.pem`, private keys, tokens), and system paths (`C:\Windows`, `Program Files`).
- **PermissionGate (`security/gate.py`)**: Context-aware authorization checking tool names and parameters prior to execution.
- **Registered Tools**:
  - `system_info`: Safe host OS, Python, CPU, memory, and cwd reporting.
  - `file_search`: Recursive filename, path, and text search across user directories (skipping build/git caches).
  - `file_read`: Text file reading, page-aware PDF text extraction (`pypdf`), and safe ZIP inspection.
  - `file_send`: Validates disk files (<50MB) and stages them for Telegram chat delivery.
  - `web_search`: Live HTTP queries with DuckDuckGo Lite HTML parsing and domain grounding.
  - `browser`: Playwright-driven Chromium automation with session reuse, 10-action budget, login wall detection (e.g. WhatsApp QR), and CAPTCHA handling.

### 7. Knowledge / RAG Layer (`src/ahjin/rag/`)
- **DocumentIngestor (`ingestor.py`)**: Parses PDFs while preserving document titles and 1-indexed page numbers.
- **TextChunker (`chunker.py`)**: Generates overlapping text chunks (`ChunkDescriptor`).
- **BGE-M3 Embedding Service (`embedding.py`)**: Local Ollama-backed embedding service producing **1024-dimensional dense vectors**.
- **SQLiteVectorStore (`vector_store.py`)**: Persistent SQLite database (`ahjin_rag.db`) storing document metadata and chunk vectors, providing in-memory cosine similarity search.

---

## 4. End-to-End Execution Flow Example

Consider the multi-step request: **"Find my resume, summarize my strongest project, and send me the file."**

```text
USER (Telegram)
  │
  │ "Find my resume, summarize my strongest project, and send me the file."
  ▼
TELEGRAM ADAPTER
  │ Construct TaskRequest
  ▼
BERU ORCHESTRATOR
  │ Capability Analysis: requires_reasoning=True, requires_code=False
  │ ToolIntentPlanner -> ToolInvocationRequest("file_search", {"query": "resume"})
  │ ExecutionPlan:
  │   Step 1: TOOL_INVOCATION (file_search)
  │   Step 2: TOOL_INVOCATION (file_read)
  │   Step 3: TOOL_INVOCATION (file_send)
  │   Step 4: MODEL_INVOCATION (summarize resume text)
  ▼
AGENTIC HARNESS RUNNER
  │
  ├─► [STEP 1: file_search]
  │     PermissionGate & SafePathPolicy authorize search in Desktop/Documents/Downloads
  │     FileSearchTool discovers "c:/Users/.../Desktop/resume.pdf"
  │
  ├─► [STEP 2: file_read]
  │     FileReadTool parses PDF text page-by-page using pypdf
  │     Extracted text stored in ExecutionState
  │
  ├─► [STEP 3: file_send]
  │     FileSendTool validates path containment & size < 50MB
  │     Stages "Desktop/resume.pdf" in TaskResult.file_attachments
  │
  ├─► [STEP 4: MODEL_INVOCATION]
  │     ContextAssembler injects prompt + [TOOL RESULTS] (Extracted Resume Text)
  │     ModelRouter selects minimax/minimax-m3:free (Cloud HEAVY #1)
  │     Model generates project evaluation & summary
  │
  ▼
TELEGRAM ADAPTER
  │ 1. Renders progressive markdown summary in chat
  │ 2. Appends observability footer:
  │    ⚡ AHJIN Runtime | Model: MiniMax M3 | Route: HEAVY | AHJIN: 18ms | Model: 1420ms | Total: 1438ms
  │ 3. Dispatches binary `resume.pdf` document attachment directly into Telegram chat
  ▼
USER (Receives analysis + actual PDF document attachment)
```

---

## 5. Verified Implementation & Progress Status

### Capability Matrix (Implemented vs Future Work)

| Subsystem / Feature | Implementation Status | Owns / Features |
|---|:---:|---|
| **Telegram Adapter** | **IMPLEMENTED** | Chunked streaming, binary file attachments, `/health` & `/models` commands |
| **BERU Capability Analysis** | **IMPLEMENTED** | Deterministic token extraction (`requires_code`, `requires_reasoning`, `requires_vision`) |
| **LLM Tool Intent Planner** | **IMPLEMENTED** | JSON schema validation, parameter whitelisting, deterministic fallback |
| **Agentic Harness Runner** | **IMPLEMENTED** | Step sequencing, `ExecutionState`, `ContextAssembler`, `ResponseVerifier` |
| **5-Pass Model Router** | **IMPLEMENTED** | Hard capability gate, latency filter, priority ranking, tier matching |
| **Dynamic Health Tracker** | **IMPLEMENTED** | Thread-safe circuit breaker states (`HEALTHY`, `DEGRADED`, `UNHEALTHY`) |
| **Cloud Model Fleet** | **IMPLEMENTED** | MiniMax M3, Nemotron Ultra, Nemotron Lightning 30B, Kimi K3, DeepSeek Pro/Flash |
| **Local Ollama Fleet** | **IMPLEMENTED** | Gemma 3 4B (`FAST`), Qwen 3 8B (`HEAVY` with 90s deadline & Gemma fallback) |
| **Tool Registry & Security** | **IMPLEMENTED** | `SafePathPolicy` (authorized roots, traversal prevention, sensitive file blocking) |
| **System Diagnostics Tool** | **IMPLEMENTED** | Host OS, Python, CPU, memory, cwd reporting with `SAFE_FIELDS_WHITELIST` |
| **PC-Wide File Search Tool** | **IMPLEMENTED** | Recursive discovery across Desktop, Documents, Downloads, Workspace |
| **Document Reader & PDF Tool** | **IMPLEMENTED** | Text files, `pypdf` page-aware extraction, ZIP archive listing |
| **File Staging & Attachment Tool**| **IMPLEMENTED** | File validation (<50MB) and Telegram binary document transmission |
| **Live Web Search Tool** | **IMPLEMENTED** | Live HTTP search, DuckDuckGo Lite parsing, domain grounding |
| **Playwright Browser Tool** | **IMPLEMENTED** | Headed Chromium, 10-action budget, login wall (WhatsApp QR) & CAPTCHA detection |
| **BGE-M3 Dense RAG** | **IMPLEMENTED** | Page-aware chunking, BGE-M3 1024-dim vectors, SQLite vector store (`ahjin_rag.db`) |
| **Vision-Guided Computer Use** | **PLANNED / FUTURE** | Visual pixel clicking, UI coordinate regression models |
| **Arbitrary Desktop GUI Control**| **PLANNED / FUTURE** | Win32/X11 window automation beyond Playwright browser |
| **Multimodal Vision RAG** | **PLANNED / FUTURE** | Image embeddings, chart/diagram OCR, scanned doc analysis |
| **Long-Term Episodic Memory** | **PLANNED / FUTURE** | Cross-session user profiling, knowledge graph stores |

---

## 6. Engineering Validation Metrics

The AHJIN 2.0 codebase is validated using industry-standard automated test suites and static analysis tools:

- **Pytest Suite**: **224 automated unit and integration tests passing (100% pass rate)** in ~11.29 seconds.
- **Pyright Static Type Analysis**: **0 errors, 0 warnings, 0 informations**.
- **Ruff Code Formatting & Linting**: **All checks passed! Clean code style.**

```text
============================== test session starts ==============================
platform win32 -- Python 3.12.3, pytest-8.3.4, pluggy-1.5.0
rootdir: C:\Users\vishn\Downloads\AHJIN 2.0 - Copy
collected 224 items

tests/test_beru_orchestrator.py .........................                 [ 11%]
tests/test_beru_tool_planner.py ........................                  [ 22%]
tests/test_file_intelligence.py ...................................       [ 37%]
tests/test_local_executor.py .............................                [ 50%]
tests/test_model_router.py ......................................         [ 67%]
tests/test_rag_pipeline.py ............................                   [ 80%]
tests/test_security_policy.py ................................            [ 94%]
tests/test_web_and_browser.py ..............                              [100%]

============================== 224 passed in 11.29s ==============================
```

---

## 7. How to Run and Verify the System

### Prerequisites
- Python 3.12+
- Virtual Environment activated (`.venv`)
- Ollama (for local offline inference with `gemma3:4b` and `qwen3:8b`)

### Running the Test Suite
```bash
# Run pytest suite
.venv\Scripts\pytest

# Run static type checking
.venv\Scripts\pyright

# Run code linter
.venv\Scripts\ruff check src tests
```

### Starting the AHJIN Telegram Bot Interface
```bash
# Set your Telegram bot token in .env or environment
$env:TELEGRAM_BOT_TOKEN="your_telegram_bot_token"

# Launch AHJIN 2.0 system entrypoint
.venv\Scripts\python src/ahjin/main.py
```
