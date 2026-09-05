# AHJIN 2.0 — Current Implementation State

## 1. Subsystem State Matrix

| Subsystem | State | Notes |
|---|---|---|
| **Architecture** | LOCKED | Master System Blueprint & Multi-Tier Agentic Architecture |
| **Documentation** | OPERATIONAL | Updated for Phase 7 (Browser, Files, RAG, Local Fleet & Tools) |
| **Python Runtime** | OPERATIONAL | Python 3.12+, uv, Pydantic v2, asyncio |
| **Package Structure** | OPERATIONAL | Modular monolith (`src/ahjin`) |
| **Model Intelligence** | OPERATIONAL | ModelCatalog, ModelRouter (5-pass in-memory), ModelHealthTracker |
| **Cognitive Orchestration** | OPERATIONAL | BERU Orchestrator, Capability Extraction, ToolIntentPlanner |
| **Execution Runtime** | OPERATIONAL | HarnessRunner, ContextAssembler, ResponseVerifier, ExecutionState |
| **Provider Gateway** | OPERATIONAL | OpenRouter, NVIDIA NIM, Local Ollama Fleet |
| **Local Offline Inference** | OPERATIONAL | LocalExecutor (Gemma 3 4B FAST, Qwen 3 8B HEAVY with 90s deadline) |
| **Interface Adapter** | OPERATIONAL | TelegramAdapter with chunked streaming, file attachments, & `/health` / `/models` |
| **Tool Ecosystem** | OPERATIONAL | ToolRegistry, SystemInfoTool, FileSearchTool, FileReadTool, FileSendTool, WebSearchTool, BrowserTool |
| **Security & Containment** | OPERATIONAL | SafePathPolicy, PermissionGate, sensitive file blocking, path traversal prevention |
| **Knowledge / RAG** | OPERATIONAL | DocumentIngestor, page-aware chunking, BGE-M3 (1024-dim), SQLite vector store (`ahjin_rag.db`) |
| **Validation Suite** | OPERATIONAL | **224 automated tests passing (100%)**, Pyright clean (0 errors), Ruff clean |

---

## 2. Implemented System Architecture

```text
USER (Telegram Client)
    │
    ▼
TELEGRAM ADAPTER (`src/ahjin/interfaces/telegram/bot.py`)
    │  ├── Renders progressive chunked streaming (1.0s interval edits)
    │  ├── Dispatches binary file attachments (up to 50MB)
    │  └── Serves diagnostic commands (`/health`, `/models`)
    ▼
CORE DISPATCHER (`src/ahjin/core/dispatcher.py`)
    │  └── Lifecycle forwarding & request routing (zero business decisions)
    ▼
BERU ORCHESTRATOR (`src/ahjin/beru/orchestrator.py`)
    │  ├── Capability Analysis ──► Emits ExecutionStrategy & CapabilityRequirements
    │  ├── LLM Tool Intent Planner (`src/ahjin/beru/tool_planner.py`) ──► JSON Tool Invocation Intent
    │  └── Deterministic Fallback (`src/ahjin/beru/tools.py`)
    ▼
HARNESS RUNNER (`src/ahjin/harness/runner.py`)
    │  ├── ContextAssembler (`src/ahjin/harness/context.py`) [injects [TOOL RESULTS] & grounding]
    │  ├── Step Sequencing Loop & ExecutionState (`src/ahjin/harness/state.py`)
    │  ├── ResponseVerifier (`src/ahjin/harness/verifier.py`)
    │  ├── Same-Request Cloud Rerouting Budget (max_recovery_attempts = 2)
    │  └── Local Fallback Controller (Ollama on cloud exhaustion or offline mode)
    ▼
 ┌─────────────────────────────┬─────────────────────────────┬─────────────────────────────┐
 │        MODEL LAYER          │         TOOL LAYER          │      KNOWLEDGE LAYER        │
 │ ModelRouter (5-pass)        │ ToolRegistry                │ DocumentIngestor (pypdf)    │
 │ ModelCatalog                │ PermissionGate              │ TextChunker (page-aware)    │
 │ ModelHealthTracker          │ SafePathPolicy              │ BGE-M3 Dense Embeddings     │
 │ ProviderGateway             │ ├── SystemInfoTool          │ SQLite Vector Store         │
 │ ├── OpenRouter (Cloud)      │ ├── FileSearchTool          │ (`ahjin_rag.db`)            │
 │ ├── NVIDIA NIM (Cloud)      │ ├── FileReadTool            │                             │
 │ └── Ollama (Local Fleet)    │ ├── FileSendTool            │                             │
 │      ├── Gemma 3 4B (FAST)  │ ├── WebSearchTool           │                             │
 │      └── Qwen 3 8B (HEAVY)  │ └── BrowserTool (Playwright)│                             │
 └─────────────────────────────┴─────────────────────────────┴─────────────────────────────┘
    │
    ▼
OBSERVATIONS & EXECUTION STATE
    │
    ▼
MODEL REASONING & RESPONSE VERIFICATION
    │
    ▼
TASK RESULT WITH OPTIONAL FILE ATTACHMENTS ──► TELEGRAM
```

---

## 3. Subsystem Responsibility Boundaries

- **Core Dispatcher**: Lifecycle management and request forwarding. Pure entry point; contains zero cognitive or model routing logic.
- **BERU Orchestrator**: Strategic cognitive decision engine. Analyzes task text to produce provider-agnostic `ExecutionStrategy` and `CapabilityRequirements`. Coordinates `ToolIntentPlanner` to generate structured JSON tool intents bounded by schemas.
- **ModelCatalog**: In-memory registry of static `ModelDescriptor` metadata across active cloud candidates and local Ollama models.
- **ModelRouter**: In-memory, zero-latency model selection engine. Evaluates models through a strict 5-pass pipeline:
  1. *Hard Capability Eligibility Gate* (incapable models can **never** beat capable models)
  2. *Health Availability Filter* (excludes unhealthy or already-failed models)
  3. *Hard Latency Constraint Pass* (`max_latency_ms`)
  4. *Tier Preference Match* (`FAST` vs `HEAVY`)
  5. *Two-Key Ranking Pass* (priority ordinal key + blended quality/latency score)
- **ModelHealthTracker**: Dynamic operational health tracking. Manages model health states (`HEALTHY`, `DEGRADED`, `UNHEALTHY`, `RECOVERY_PROBE_ELIGIBLE`), consecutive failures, circuit breakers, and latency Exponential Moving Average (EMA). Thread-safe with locks.
- **Harness Runner**: Step sequencing, verification, and failure recovery loop. Executes strategy policies (`require_verification`, `recovery_policy`, `max_recovery_attempts`) and tracks intermediate tool observations.
- **Local Executor**: Handles air-gapped local execution via Ollama (`gemma3:4b` for FAST, `qwen3:8b` for HEAVY). Enforces an application-level **90-second timeout** on Qwen, safely falling back to Gemma with the original prompt preserved.
- **Tool Ecosystem & SafePathPolicy**: Sandboxed execution across authorized roots (`Workspace`, `Desktop`, `Documents`, `Downloads`). Blocks traversal (`..`), sensitive files (`.env`, `.pem`, credentials), and system directories (`C:\Windows`, `Program Files`).
- **Knowledge / RAG Subsystem**: Self-contained semantic retrieval using page-aware document chunking, BGE-M3 1024-dimensional dense embeddings, and persistent SQLite vector storage.
- **Telegram Adapter**: Remote interface handling 4096-character chunking, 1.0s throttled progressive streaming, binary document attachment delivery, and diagnostic commands (`/health`, `/models`).

---

## 4. Operational Model Fleet

### A. Active Cloud Models (Online Runtime)
1. **`nvidia/nemotron-3.5-lightning-30b-a3b`** (FAST #1, Priority 200, Quality 85)
2. **`minimax/minimax-m3:free`** (HEAVY #1 via OpenRouter, Priority 250, Quality 95)
3. **`nvidia/nemotron-3-ultra-550b-a55b:free`** (HEAVY #2 via OpenRouter, Priority 230, Quality 95)
4. **`nvidia/nemotron-3-ultra-550b-a55b`** (HEAVY #3 via NVIDIA NIM, Priority 200, Quality 95)
5. **`moonshotai/kimi-k3`** (HEAVY #4 via NVIDIA NIM, Priority 170, Quality 87)
6. **`deepseek-ai/deepseek-v4-pro-0813`** (HEAVY #5 via NVIDIA NIM, Priority 150, Quality 92)
7. **`deepseek-ai/deepseek-v4-flash-0731`** (HEAVY #6 via NVIDIA NIM, Priority 130, Quality 90)

### B. Active Local Models (Offline Runtime via Ollama)
1. **`gemma3:4b`** (FAST Local, Priority 100, Quality 80)
2. **`qwen3:8b`** (HEAVY Local, Priority 120, Quality 85; 90s timeout with Gemma fallback)

---

## 5. Validation Status
- **Pytest**: `224 passed in 35.79s` (100% pass rate across unit and integration suites)
- **Ruff**: `All checks passed! Clean code style.`
- **Pyright**: `0 errors, 0 warnings, 0 informations`
- **Subsystem Validations**: RAG E2E, live Ollama inference, BGE-M3 vector search, file discovery, PDF parsing, Telegram file attachment delivery, web search, and Playwright browser navigation.

---

## 6. Current Limitations & Planned Roadmap
- **DOM-Based Browser Automation**: The Playwright `BrowserTool` relies on CSS selectors and DOM text elements. Pure vision-guided pixel clicking is planned as Future Work.
- **Text-Only RAG**: Document processing supports text files and extractable PDF layers; scanned bitmap OCR and multimodal diagram understanding remain Future Work.
- **Long-Term Episodic Memory**: Session memory persists execution state within a request; cross-session knowledge graph memory is planned for subsequent architectural phases.
