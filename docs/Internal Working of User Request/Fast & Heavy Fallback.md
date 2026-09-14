YES 😭 — let's separate this **very clearly**, because there are TWO different routing chains: **LIGHT/FAST** and **HEAVY**.

## 🟢 LIGHT / FAST classification

Current fallback order is:

```text
1. MiniMax M3                 ← PRIMARY
        ↓ fail
2. Nemotron 3.5 Lightning Free (OpenRouter)
        ↓ fail
3. Nemotron 3.5 Lightning 30B (NVIDIA Direct)
        ↓ fail
4. Gemma 3 4B (Ollama)        ← local fallback
```

So **Gemma is the LAST LIGHT/FAST fallback**, not something that is automatically used immediately after Nemotron.

---

## 🔴 HEAVY classification

For a request classified HEAVY:

```text
1. MiniMax M3                 ← PRIMARY
        ↓ fail
2. Nemotron 3 Ultra Free      (OpenRouter)
        ↓ fail
3. Nemotron 3 Ultra           (NVIDIA Direct)
        ↓ fail
4. Kimi K3                    (NVIDIA)
        ↓ fail
5. DeepSeek V4 Pro            (NVIDIA)
        ↓ fail
6. DeepSeek V4 Flash          (NVIDIA)
        ↓ fail
7. Qwen 3 8B                  (Ollama) ← local fallback
```

So **HEAVY does NOT normally go through Gemma**.

Gemma belongs to **FAST/LIGHT**.

---

# 🔥 Now your specific `00:24` request

You asked whether:

> MiniMax → Nemotron → Gemma?

**NO.**

It was:

### Planner

```text
MiniMax M3
   ❌ 402
       ↓
Nemotron Lightning
   ✅ SUCCESS
       ↓
STOP
```

**Gemma was NOT used by the Planner.**

### Final Harness

The final response had:

```text
Nemotron Lightning
   ❌ 30s timeout
       ↓
Gemma 3 4B
   ✅ SUCCESS
```

So **yes, Gemma was directly next after Nemotron in the Harness's FAST fallback chain**, because the other FAST candidate(s) had already been excluded/unhealthy at that point.

It did **NOT** go:

```text
Nemotron → Ultra → Kimi → DeepSeek → Gemma
```

And it did **NOT** enter HEAVY for this request.

### The clean picture

```text
                 CLASSIFICATION
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
         🟢 LIGHT              🔴 HEAVY
          / FAST
             │                   │
       MiniMax M3           MiniMax M3
             ↓                   ↓
      Nemotron Lightning    Nemotron Ultra
             ↓                   ↓
        Gemma 3 4B          Kimi K3
             │                   ↓
             │              DeepSeek Pro
             │                   ↓
             │              DeepSeek Flash
             │                   ↓
             │                Qwen 3 8B
```

**And the critical point:** the Planner and Harness have their **own independent fallback attempts**. That's why the Planner can succeed on Nemotron while the Harness later falls back to Gemma.
