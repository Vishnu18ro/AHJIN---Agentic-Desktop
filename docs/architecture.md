# AHJIN 2.0 — System Architecture

## 1. Executive Architecture

AHJIN 2.0 separates cognitive decision-making from runtime execution, environmental tools, and model providers:

```text
┌───────────────────────────────────────────────────────────┐
│                    INTERFACE LAYER                        │
│   Telegram Adapter (Streaming, Attachments, /health)      │
└─────────────────────────────┬─────────────────────────────┘
                              │ TaskRequest
┌─────────────────────────────▼─────────────────────────────┐
│                       AHJIN CORE                          │
│     Session Management │ Dispatcher │ Event Routing       │
└─────────────────────────────┬─────────────────────────────┘
                              │ TaskRequest
┌─────────────────────────────▼─────────────────────────────┐
│              BERU (Cognitive Orchestration)               │
│   Intent Extraction │ Capability Analysis                 │
│   LLM Tool Intent Planner │ Deterministic Tool Fallback   │
└─────────────────────────────┬─────────────────────────────┘
                              │ ExecutionPlan
┌─────────────────────────────▼─────────────────────────────┐
│                 HARNESS (Execution Runtime)               │
│   Task Lifecycle │ Step Sequencing │ Retries │ State      │
│                                                           │
│   ┌────────────────────┐   ┌──────────────────────────┐   │
│   │  ContextAssembler  │   │     ProviderGateway      │   │
│   │ (retrieves/builds) │   │ (matches capability)     │   │
│   └─────────┬──────────┘   └────────────┬─────────────┘   │
└─────────────┼───────────────────────────┼─────────────────┘
              │                           │
   ┌──────────▼──────────┐     ┌──────────▼──────────┐
   │ Knowledge Layer     │     │ Model Layer         │
   │ BGE-M3 (1024-dim)   │     │ Cloud: OpenRouter,  │
   │ SQLite Vector Store │     │        NVIDIA NIM   │
   │ Page-Aware RAG      │     │ Local: Ollama Fleet │
   └──────────┬──────────┘     │        (Gemma/Qwen) │
              │                └─────────────────────┘
   ┌──────────▼──────────┐
   │ Tool Layer          │
   │ ToolRegistry        │
   │ SafePathPolicy      │
   │ System, Files,      │
   │ WebSearch, Browser  │
   └─────────────────────┘
```

## 2. Request & Control Flow

1. **Interface Layer:** Normalizes incoming updates to `TaskRequest`. Supports 1.0s throttled streaming and binary file attachment transmission.
2. **AHJIN Core:** Coordinates request lifecycle and hands `TaskRequest` to BERU.
3. **BERU:** Assesses task complexity, extracts capability requirements, and invokes `ToolIntentPlanner` to produce an `ExecutionPlan`.
4. **Harness:** Sequences `PlanStep` execution across tools and model invocations.
5. **Tool Execution:** Tools (`system_info`, `file_search`, `file_read`, `file_send`, `web_search`, `browser`) execute behind `PermissionGate` and `SafePathPolicy`. Observations are recorded into `ExecutionState`.
6. **ContextAssembler (inside Harness):** Combines instructions, prior tool results, and retrieval context into `ContextualizedPrompt`.
7. **ModelRouter (inside ProviderGateway):** Matches `ExecutionStrategy` to optimal healthy models via a 5-pass filter across cloud and local Ollama runtimes.
8. **Provider Gateway:** Dispatches prompts to OpenRouter, NVIDIA NIM, or local Ollama instances.
9. **ResponseVerifier:** Validates structural response integrity before completion.
10. **TaskResult:** Harness packages outputs and file attachments into `TaskResult`, returned via Core to the interface.

## 3. Core Architectural Boundaries

- **BERU vs Harness:** BERU decides *what* to do (strategy and tool intent). Harness decides *how* to sequence and recover execution.
- **Cognitive vs Tool Authority:** The LLM proposes tool parameters; trusted Python infrastructure (`SafePathPolicy`, `PermissionGate`) validates and executes them.
- **Provider Gateway Boundary:** Models are behind abstract interfaces (`BaseModelProvider`). Core and BERU never import concrete providers.
- **Online vs Offline Policy:** Online routes exclusively to cloud models; offline routes exclusively to local Ollama models (Gemma 3 4B, Qwen 3 8B with 90s deadline).
