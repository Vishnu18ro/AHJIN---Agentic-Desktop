# Phase 5 — Controlled Model Capability Characterization Report

> **Forensic Investigation & Capability Profile Benchmark**  
> **Status**: COMPLETE  
> **Scope**: Controlled capability characterization across 9 task categories. Strictly isolated, research-only experiment. Zero production routing, model priorities, or configuration changes.

---

## 1. Objective

Phase 5 evaluates the functional capabilities of three candidate model paths under strictly controlled, identical conditions:

1. **MODEL A**: NVIDIA Direct $\rightarrow$ Nemotron 3.5 Lightning 30B (`nvidia/nemotron-3.5-lightning-30b-a3b`)
2. **MODEL B**: OpenRouter $\rightarrow$ Nemotron 3.5 Lightning Free (`nvidia/nemotron-3.5-lightning:free`)
3. **MODEL C**: OpenRouter $\rightarrow$ MiniMax M3 (`minimax/minimax-m3`)

The core research question is not *"Which model is better?"*, but:
> **"For which categories of tasks does each model appear most suitable based on controlled observations?"**

The purpose is to build an objective empirical capability profile comparing **Capability vs. Latency**, providing the Chief Architect with evidence for future capability-aware routing decisions.

---

## 2. Models and Providers Tested

| Model Key | Provider | Model Identifier | Configured Endpoint | Connection Architecture |
| :--- | :--- | :--- | :--- | :--- |
| **MODEL A** | NVIDIA Direct | `nvidia/nemotron-3.5-lightning-30b-a3b` | `https://integrate.api.nvidia.com/v1/chat/completions` | Persistent `httpx.AsyncClient` with keepalive pooling |
| **MODEL B** | OpenRouter | `nvidia/nemotron-3.5-lightning:free` | `https://openrouter.ai/api/v1/chat/completions` | Per-request `httpx.AsyncClient` (AHJIN production standard) |
| **MODEL C** | OpenRouter | `minimax/minimax-m3` | `https://openrouter.ai/api/v1/chat/completions` | Per-request `httpx.AsyncClient` (Active endpoint verified in 4B) |

*Note: Per Phase 4B findings, the active `minimax/minimax-m3` endpoint was used, as the deprecated `:free` slug returns HTTP 404.*

---

## 3. Exact Benchmark Tasks

Nine distinct task categories were designed with deterministic ground-truth requirements:

1. **Simple Instruction Following (`TASK_1_INSTRUCTION`)**
   - *Prompt*: *"Give exactly 5 bullet points explaining what an API is. Do not add any introductory text, titles, concluding remarks, or any other words besides the 5 bullet points."*
   - *Target*: Exactly 5 bullet points; zero preamble, titles, or postscript.

2. **Basic Knowledge / Explanation (`TASK_2_EXPLANATION`)**
   - *Prompt*: *"Explain the difference between an operating system process and a thread to a second-year computer science student. Cover memory address space, context switching overhead, and communication mechanisms in under 200 words."*
   - *Target*: Technical accuracy across memory, context switching, IPC, and $\le 200$ words.

3. **Mathematical Reasoning (`TASK_3_MATH`)**
   - *Prompt*: *"A server cluster has 3 types of worker nodes: Type A, Type B, and Type C. There are 26 nodes in total. The number of Type B nodes is twice the number of Type A nodes. The number of Type C nodes is 4 less than the number of Type B nodes. Each Type A node processes 50 requests/sec, Type B processes 80 requests/sec, and Type C processes 120 requests/sec. What is the total request processing capacity of the entire cluster in requests/sec? State the final numerical answer clearly at the end as 'TOTAL_CAPACITY: X'."*
   - *Ground Truth*: $A=6, B=12, C=8 \implies (6\times 50) + (12\times 80) + (8\times 120) = 300 + 960 + 960 = 2220$. Output: `TOTAL_CAPACITY: 2220`.

4. **Logical Reasoning (`TASK_4_LOGIC`)**
   - *Prompt*: *"Four software engineers (Alice, Bob, Charlie, and Diana) are assigned distinct primary on-call days: Monday, Tuesday, Wednesday, and Thursday. 1. Alice's on-call day is earlier in the week than Charlie's. 2. Bob is on call exactly two days after Diana. 3. Charlie is not on call on Thursday. Which engineer is on call on Wednesday? State your final answer clearly as 'WEDNESDAY_ENGINEER: Name'."*
   - *Ground Truth*: Diana=Tue, Bob=Thu, Alice=Mon, Charlie=Wed. Output: `WEDNESDAY_ENGINEER: Charlie`.

5. **Coding (`TASK_5_CODING`)**
   - *Prompt*: *"Write a Python function named 'is_palindrome_sentence(s: str) -> bool' that determines whether a string is a palindrome, considering only alphanumeric characters and ignoring cases. Include 3 test assertions (using assert statements) testing: a valid palindrome phrase with punctuation, a non-palindrome phrase, and an empty string. Output only valid executable Python code in a single python code block."*
   - *Target*: Function passes automated AST validation and executes cleanly with passing assertions.

6. **Debugging (`TASK_6_DEBUGGING`)**
   - *Prompt*: Intentionally broken running average function slicing past list bounds (`window = data[i : i + window_size]`) dividing by fixed `window_size`.
   - *Target*: Pinpoint out-of-bounds slicing near tail, explain truncated window division, and supply corrected code (`range(len(data) - window_size + 1)`).

7. **Structured Output (`TASK_7_STRUCTURED`)**
   - *Prompt*: *"Extract the entities from the following text and return ONLY a valid JSON object with the exact keys: 'system_name' (string), 'version' (string), 'release_year' (integer), 'active_modules' (array of strings), and 'status' (string: 'active' or 'deprecated'). Do not include markdown code fences, backticks, or any explanation text before or after the JSON."*
   - *Target*: Pure JSON string parseable by `json.loads()` directly; zero markdown fences; exact schema adherence.

8. **Agentic Planning (`TASK_8_PLANNING`)**
   - *Prompt*: Multi-step desktop assistant task: locate resume PDF, read text, summarize skills in 3 bullets, send via Telegram.
   - *Target*: Correct sequential ordering (Search $\rightarrow$ Read $\rightarrow$ Summarize $\rightarrow$ Send), tool mapping, dependency awareness, and explicit security/permission awareness for network transmission.

9. **Complex Reasoning (`TASK_9_COMPLEX`)**
   - *Prompt*: Distributed Raft consensus scenario involving a leader partition, uncommitted log entries from Term 2 on Node 3, partition resolution, and Term 3 leader overwriting.
   - *Ground Truth*: 1. Node 1 can overwrite uncommitted entries because uncommitted entries from prior terms are truncated during log reconciliation. 2. Exact log terms at indexes 1–4: `INDEX_TERMS: [1, 1, 1, 3]`.

---

## 4. Methodology

1. **Isolation**: Implemented in [scratch/benchmark_phase5_capability.py](file:///c:/Users/vishn/Downloads/AHJIN%202.0%20-%20Copy/scratch/benchmark_phase5_capability.py).
2. **Parameters**: Identical system prompt (`"You are AHJIN 2.0, an Agentic AI Operating Layer."`), temperature `0.7`, max tokens `4096`, and streaming enabled.
3. **Execution**: Round-robin interleaved across models and tasks.
4. **Volume**: 9 tasks $\times$ 3 models $\times$ 2 runs = **54 total measured requests**.
5. **Validation**: Automated sandboxed Python execution for coding tasks, regex ground-truth verification for math/logic, and JSON schema parsing.

---

## 5. Evaluation Criteria

For each execution, three core dimensions were scored on a 0.00 to 5.00 scale:
- **Correctness (0–5)**: Factual, mathematical, logical, and semantic validity.
- **Instruction Following (0–5)**: Strict adherence to formatting constraints, word limits, JSON syntax, and exclusions.
- **Output Quality (0–5)**: Clarity, conciseness, technical depth, and clean presentation.
- **Composite Score**: Arithmetic mean of the three dimensions.

---

## 6. Raw and Aggregated Results

All 54 requests completed with 100% network/HTTP success (0 HTTP errors).

### Table 1: Composite Capability Score by Category (0.00 – 5.00)

| Task Category | NVIDIA Direct Nemotron 30B | OpenRouter Nemotron Free | OpenRouter MiniMax M3 |
| :--- | :---: | :---: | :---: |
| **1. Simple Instruction Following** | 5.00 | 5.00 | 5.00 |
| **2. Basic Knowledge / Explanation** | 4.83 | 4.83 | 4.83 |
| **3. Mathematical Reasoning** | 5.00 | 5.00 | 5.00 |
| **4. Logical Reasoning** | 5.00 | 5.00 | 5.00 |
| **5. Coding** | 5.00 | 5.00 | 5.00 |
| **6. Debugging** | 4.83 | 4.83 | 4.83 |
| **7. Structured Output** | 5.00 | 5.00 | 5.00 |
| **8. Agentic Planning** | 4.42 | 5.00 | 5.00 |
| **9. Complex Reasoning** | **5.00** | **5.00** | **3.33** |
| **Overall Average Score** | **4.90 / 5.00** | **4.96 / 5.00** | **4.78 / 5.00** |

---

## 7. Latency Comparison Across Categories

### Table 2: Average Total Latency (Wall-Clock ms)

| Task Category | NVIDIA Direct Nemotron 30B | OpenRouter Nemotron Free | OpenRouter MiniMax M3 | MiniMax Speedup vs NVIDIA |
| :--- | :--- | :--- | :--- | :---: |
| **1. Simple Instruction Following** | 40,628.7 ms (40.6s) | 45,280.5 ms (45.3s) | **4,219.4 ms (4.2s)** | **9.6x faster** |
| **2. Basic Knowledge / Explanation** | 56,598.8 ms (56.6s) | 36,089.2 ms (36.1s) | **8,275.6 ms (8.3s)** | **6.8x faster** |
| **3. Mathematical Reasoning** | 16,712.5 ms (16.7s) | 27,636.1 ms (27.6s) | **9,563.1 ms (9.6s)** | **1.7x faster** |
| **4. Logical Reasoning** | 26,681.8 ms (26.7s) | 23,815.8 ms (23.8s) | **6,141.1 ms (6.1s)** | **4.3x faster** |
| **5. Coding** | 43,205.4 ms (43.2s) | 18,227.3 ms (18.2s) | **9,190.0 ms (9.2s)** | **4.7x faster** |
| **6. Debugging** | 111,017.4 ms (111.0s)| 134,509.8 ms (134.5s)| **20,594.0 ms (20.6s)**| **5.4x faster** |
| **7. Structured Output** | 28,118.8 ms (28.1s) | 17,530.0 ms (17.5s) | **2,626.7 ms (2.6s)** | **10.7x faster** |
| **8. Agentic Planning** | 100,540.9 ms (100.5s)| 114,637.1 ms (114.6s)| **17,450.9 ms (17.5s)**| **5.8x faster** |
| **9. Complex Reasoning** | 84,829.1 ms (84.8s) | 128,919.4 ms (128.9s)| **121,548.9 ms (121.5s)**| Comparable |
| **Overall Average Latency** | **56,481.5 ms (56.5s)**| **60,738.4 ms (60.7s)**| **22,178.9 ms (22.2s)**| **2.5x faster overall** |

---

## 8. Task-by-Task Analysis

### 1. Simple Instruction Following
- **Nemotron (both paths)**: Followed constraints perfectly (exactly 5 bullet points, zero extraneous text). However, it took 40.6s (NVIDIA) and 45.3s (OpenRouter) due to extended pre-response reasoning.
- **MiniMax M3**: Flawless output in **4.2s** (9.6x faster). Followed the zero-intro/outro rule strictly.

### 2. Basic Knowledge / Explanation
- All models delivered technically accurate CS-level explanations covering memory isolation, context switching overhead, and IPC mechanisms under 200 words.
- MiniMax completed in **8.3s**, whereas Nemotron took 36.1s to 56.6s.

### 3. Mathematical Reasoning
- All three models computed the exact correct cluster capacity of **2220 req/sec** ($A=6, B=12, C=8$).
- MiniMax reached the correct numerical answer in **9.6s** vs 16.7s for NVIDIA Direct and 27.6s for OpenRouter.

### 4. Logical Reasoning
- All three models solved the on-call schedule puzzle deterministically, identifying **Charlie** on Wednesday with clear step-by-step constraint elimination.
- MiniMax completed in **6.1s** vs ~24–27s for Nemotron.

### 5. Coding
- All three models produced valid Python code defining `is_palindrome_sentence` with automated execution test passes.
- MiniMax completed in **9.2s** vs 43.2s on NVIDIA Direct.

### 6. Debugging
- All models correctly diagnosed the slice boundary problem in moving average calculations (`window_size` exceeding remaining elements near list tail) and supplied working fixes.
- Nemotron took **111s to 134s** (averaging ~2 minutes per run) while deeply analyzing the bug, whereas MiniMax identified and resolved it in **20.6s**.

### 7. Structured Output
- All models achieved 5.00/5.00, returning strictly valid JSON matching all 5 keys without markdown code fences.
- MiniMax generated valid schema JSON in **2.6s** (10.7x faster than NVIDIA Direct's 28.1s).

### 8. Agentic Planning
- All models structured an effective 4-stage sequential plan (FileSearch $\rightarrow$ FileRead $\rightarrow$ LLMSummarize $\rightarrow$ TelegramSend).
- OpenRouter Nemotron and MiniMax explicitly demarcated security/permission boundaries between local read operations and external network transmissions (5.00/5.00). NVIDIA Nemotron Run 2 omitted explicit permission gate labeling (3.83/5.00).
- MiniMax completed the entire planning sequence in **17.5s** vs **100.5s to 114.6s** for Nemotron.

### 9. Complex Reasoning (Distributed Raft Consensus)
- **Nemotron Lightning (NVIDIA & OpenRouter)**: Scored **5.00 / 5.00** on both runs. Accurately deduced that Term 2 entries are uncommitted and must be truncated/overwritten by the Term 3 leader, correctly concluding `INDEX_TERMS: [1, 1, 1, 3]`.
  - *Observation*: On Run 1 (OpenRouter) and Run 2 (NVIDIA), Nemotron leaked its internal thought process header (`"Here's a thinking process:\n\n1. Analyze User Input..."`) directly into the visible content delta.
- **MiniMax M3**:
  - *Run 1*: Encountered an internal reasoning timeout / cutoff after 132.9s of reasoning, producing an empty visible response (score: 1.67).
  - *Run 2*: Produced an outstanding analytical answer (score: 5.00), correctly concluding `INDEX_TERMS: [1, 1, 1, 3]`, tracing log reconciliation, and insightfully noting that Node 3's vote for Node 1 technically required specific Raft election restriction conditions.
  - Overall category score: **3.33 / 5.00** due to Run 1's empty output.

---

## 9. Model Capability Profiles

### Nemotron 3.5 Lightning 30B Profile
- **Primary Strength**: Highly robust on complex, multi-step deterministic reasoning (e.g., distributed consensus, complex logic). Reached 5.00 across math, logic, coding, and complex reasoning.
- **Primary Weakness**: Pervasive, heavy latency floor across all task types. Even trivial tasks (5 bullet points, simple JSON extraction) trigger 20–40 seconds of internal reasoning before visible tokens stream. Under complex reasoning, it occasionally leaks internal scratchpad reasoning into visible output.
- **Average Latency**: **56.5s to 60.7s**.

### MiniMax M3 Profile
- **Primary Strength**: Outstanding speed and high responsiveness. On 8 of the 9 categories, MiniMax matched Nemotron's quality (5.00/5.00 on instruction following, math, logic, coding, structured output, and planning) while delivering answers **2x to 10x faster** (typically 2.6s to 9.5s).
- **Primary Weakness**: On extreme edge-case complex reasoning (Task 9 Run 1), it exhibited an unhandled reasoning stall that produced an empty response before succeeding on retry.
- **Average Latency**: **22.2s** overall (and **8.6s** median across the first 8 categories).

---

## 10. Final Capability Matrix

| Capability Dimension | Nemotron Lightning | MiniMax M3 | Better Observed |
| :--- | :---: | :---: | :--- |
| **Instruction following** | 5.00 / 5.00 | 5.00 / 5.00 | **Tie** (MiniMax **9.6x faster**) |
| **Explanation** | 4.83 / 5.00 | 4.83 / 5.00 | **Tie** (MiniMax **6.8x faster**) |
| **Mathematics** | 5.00 / 5.00 | 5.00 / 5.00 | **Tie** (MiniMax **1.7x faster**) |
| **Logic** | 5.00 / 5.00 | 5.00 / 5.00 | **Tie** (MiniMax **4.3x faster**) |
| **Coding** | 5.00 / 5.00 | 5.00 / 5.00 | **Tie** (MiniMax **4.7x faster**) |
| **Debugging** | 4.83 / 5.00 | 4.83 / 5.00 | **Tie** (MiniMax **5.4x faster**) |
| **Structured output (JSON)** | 5.00 / 5.00 | 5.00 / 5.00 | **Tie** (MiniMax **10.7x faster**) |
| **Agentic planning** | 4.71 / 5.00 | 5.00 / 5.00 | **MiniMax M3** (Higher consistency + **5.8x faster**) |
| **Complex reasoning** | 5.00 / 5.00 | 3.33 / 5.00 | **Nemotron Lightning** (Consistent across runs) |
| **Operational Latency** | 56.5s – 60.7s | 22.2s | **MiniMax M3** (Overwhelmingly faster) |

---

## 11. Architectural Interpretation

Answering the 7 core architectural questions from empirical data:

### 1. Does Nemotron appear to have a capability that MiniMax does not demonstrate as strongly?
**Yes.** On extreme distributed systems reasoning (Task 9 Raft consensus), Nemotron achieved 100% correctness across both runs, whereas MiniMax suffered an empty response on Run 1 due to an internal reasoning cutoff.

### 2. Does MiniMax appear to match or exceed Nemotron on the tested reasoning tasks?
**Yes.** Across 8 out of 9 categories (instruction following, knowledge explanation, mathematics, logic, coding, debugging, structured output, and agentic planning), MiniMax matched Nemotron's capability (5.00 or 4.83) while executing at **2x to 10x faster latency**.

### 3. Is Nemotron's current FAST designation justified by capability/latency evidence?
**No.** Nemotron Lightning averages 56.5s to 60.7s across typical queries (40.6s on simple instructions, 28.1s on JSON, 100.5s on planning). Calling Nemotron the "FAST" execution tier model in AHJIN conflicts with empirical user-perceived response times.

### 4. Is MiniMax's current HEAVY designation justified?
MiniMax M3 is capable of heavy reasoning, but its operational latency (2.6s for JSON, 4.2s for instructions, 6.1s for logic, 9.2s for code) makes it perform like an agile, ultra-fast general execution model.

### 5. Are there tasks where Nemotron should remain preferred?
Nemotron remains preferred for non-interactive, deep background reasoning tasks where multi-minute latency is acceptable and maximum fault-tolerance against empty generation is needed.

### 6. Are there tasks where MiniMax should remain preferred?
MiniMax is overwhelmingly preferred for interactive user-facing workflows: Telegram chat responses, deterministic tool-planning, structured JSON parameter extraction, coding assistance, and desktop orchestration.

### 7. Is the evidence strong enough to justify changing routing?
The empirical evidence indicates that AHJIN's current tiering designation is inverted relative to actual user latency. However, in accordance with Phase 5 non-negotiable constraints, **no production routing changes are made**.

> **Potential Future Routing Implication**: The Chief Architect may consider promoting MiniMax M3 to the default primary FAST tier (or hybrid fast-reasoning tier) for interactive operations, while retaining Nemotron as a secondary heavy reasoning fallback.

---

## 12. Confirmation of Architecture and Production Integrity

Post-benchmark git inspection confirms:
- Production routing: **UNCHANGED**
- Model priorities: **UNCHANGED**
- ModelCatalog production definitions: **UNCHANGED**
- Provider selection logic: **UNCHANGED**
- BERU: **UNCHANGED**
- Harness: **UNCHANGED**
- Telegram UX: **UNCHANGED**
- RAG: **UNCHANGED**
- Tools: **UNCHANGED**
- Security & Permission gates: **UNCHANGED**
- `.env` values: **UNCHANGED**
- No hidden reasoning content was permanently logged or exposed to users.
