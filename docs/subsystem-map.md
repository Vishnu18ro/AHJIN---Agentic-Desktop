# AHJIN 2.0 — Subsystem Map

| Subsystem | Primary Responsibility | Status | Owns | Does NOT Own |
|---|---|---|---|---|
| **Interfaces** | Adapter conversion to/from `TaskRequest` | OPERATIONAL (Telegram: Streaming, Files) / FUTURE (Web, GUI) | Protocol translation, chat session mapping, attachment dispatch | Core business logic, model routing |
| **AHJIN Core** | Task entry, dispatch, session registry | OPERATIONAL | Session mapping, task dispatch | Cognitive planning, model calls |
| **BERU** | Cognitive orchestration & planning | OPERATIONAL | Intent parsing, capability requirements, ToolIntentPlanner, execution plans | Prompt building, direct HTTP calls, database queries |
| **Harness** | Execution runtime management | OPERATIONAL | Task state machine, step sequencing, retries, recovery budget, attachment tracking | Cognitive planning decisions, concrete model endpoints |
| **ContextAssembler** | Context retrieval & prompt building | OPERATIONAL | ContextualizedPrompt assembly, tool result grounding blocks, anti-hallucination prompts | Model selection, provider formatting |
| **Provider Gateway** | Capability matching & provider dispatch | OPERATIONAL | ModelRouter integration, provider dispatch (OpenRouter, NVIDIA, Ollama) | Direct cognitive reasoning |
| **Model Providers** | Provider API translation | OPERATIONAL (OpenRouter, NVIDIA, Ollama) | HTTP requests, streaming serialization, finish reason mapping | Task lifecycle state, tool execution |
| **Tools Subsystem** | External action execution | OPERATIONAL | ToolRegistry, SystemInfo, FileSearch, FileRead, FileSend, WebSearch, Browser | Direct permission bypass |
| **Security Layer** | Authorization & permission boundary | OPERATIONAL | SafePathPolicy, PermissionGate, sensitive file blocking, path traversal prevention | Tool execution mechanics |
| **Knowledge / RAG** | External document retrieval | OPERATIONAL (Text/PDF) / FUTURE (Multimodal) | DocumentIngestor, TextChunker, BGE-M3 (1024-dim), SQLiteVectorStore (`ahjin_rag.db`) | Host session memory |
| **Local Compute** | On-device model execution | OPERATIONAL | Ollama provider, LocalExecutor, Gemma 3 4B, Qwen 3 8B (90s deadline fallback) | Core orchestrator logic |
| **Observability** | Telemetry & structured logging | OPERATIONAL | Telegram runtime footer, `/health`, `/models`, latency EMA, correlation IDs | Production bypass |
| **Verification Loop**| Result validation & recovery signal | OPERATIONAL | ResponseVerifier, structural integrity checks | Replanning decisions |
| **Memory Subsystem** | User history & preferences | ARCHITECTURALLY DEFINED / FUTURE | Working memory, episodic memory, knowledge graph | Document RAG |
| **Multimodal** | Non-text visual computing | FUTURE | Visual screen parsing, pixel click regression | Text-based tool execution |
