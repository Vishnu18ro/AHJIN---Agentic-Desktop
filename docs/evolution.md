# AHJIN 2.0 — System Evolution Roadmap

Capability-gated progression roadmap:

```text
Phase 1: Vertical Spine (Telegram ──► Core ──► BERU ──► Harness ──► NVIDIA)  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 2: Multi-Model Routing + Dynamic Health Tracker + Recovery  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 3: Tools & Security Foundation (ToolRegistry, PermissionGate, SafePathPolicy)  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 4: Knowledge / RAG (Document Ingestion, BGE-M3 Embeddings, SQLite Vector Store)  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 5: Local Compute & Resilient Fallback (Ollama, Gemma 3 4B, Qwen 3 8B with 90s Deadline)  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 6: File Intelligence & Chat Attachment Delivery (PC-Wide Discovery, pypdf, FileSend)  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 7: Live Web Search & Playwright Browser Automation + Chunked Streaming  [STATUS: OPERATIONAL & VERIFIED]
   │
Phase 8: Multimodal Vision & Pixel Computer Use (VLM Screen Parsing, Coordinate Regression)  [STATUS: FUTURE WORK]
   │
Phase 9: Long-Term Episodic Memory (Cross-session user profiling, Graph Memory)  [STATUS: FUTURE WORK]
   │
Phase 10: Autonomous Multi-Agent Swarms (Hierarchical supervisor and worker agents)  [STATUS: FUTURE WORK]
```

## Milestone Log

### Phase 1: Vertical Spine Operational (Completed)
- **Scope Achieved:** Real Telegram message ──► `TelegramAdapter` ──► `TaskDispatcher` ──► `BeruOrchestrator` ──► `HarnessRunner` (`ContextAssembler`) ──► `ProviderGateway` ──► `NvidiaProvider` ──► NVIDIA API ──► Telegram Response.
- **Verification:** Unit tests, static typing (Pyright), linting (Ruff), and real Telegram message verification.

### Phase 2: Multi-Model Routing & Bounded Recovery (Completed)
- **Scope Achieved**:
  - `ModelCatalog` metadata registry (`FAST` vs `HEAVY` tiers, capabilities, limits, quality ratings).
  - In-memory `ModelRouter` (5-pass: Capability Gate ──► Health Filter ──► Max Latency Constraint ──► Tier Match ──► Quality Preference Ranking).
  - Provider-neutral BERU `ExecutionStrategy` (zero hardcoded endpoints).
  - Dynamic `ModelHealthTracker` with circuit breakers and evidence-based recovery.
  - Same-request rerouting bounded by `max_recovery_attempts = 2` with request-local `excluded_models`.

### Phase 3–4: Tool Foundation, Security, & Knowledge RAG (Completed)
- **Scope Achieved**:
  - `ToolRegistry` and `PermissionGate` interfaces.
  - `SafePathPolicy` enforcing authorized user roots (Workspace, Desktop, Documents, Downloads) and sensitive file blacklists.
  - `DocumentIngestor`, page-aware `TextChunker`, `OllamaEmbeddingService` (BGE-M3 1024-dim dense vectors), and `SQLiteVectorStore` (`ahjin_rag.db`).

### Phase 5–6: Local Fleet Fallback, File Intelligence, & Telegram Attachments (Completed)
- **Scope Achieved**:
  - Local Ollama fleet integration with `LocalExecutor` (`gemma3:4b` for FAST, `qwen3:8b` for HEAVY).
  - Application-level 90-second timeout on Qwen with automatic failover to Gemma, preserving the original prompt.
  - PC-wide `FileSearchTool`, page-aware PDF extraction via `FileReadTool` (`pypdf`), and binary file attachment delivery via `FileSendTool` (<50MB).

### Phase 7: Web Search, Browser Automation, & Progressive Streaming (Completed)
- **Scope Achieved**:
  - Live HTTP `WebSearchTool` with DuckDuckGo Lite parsing and domain grounding.
  - `BrowserTool` via Playwright Chromium: headed execution, session reuse, 10-action budget, login wall and CAPTCHA detection.
  - Progressive chunked response streaming (1.0s throttled intermediate edits to Telegram).
  - Test suite expanded to **224 passing automated tests**, Pyright clean (0 errors), Ruff clean.
