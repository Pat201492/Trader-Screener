# EDGAR Scrubber — Local Model Runtime

Production-ready local inference for EDGAR document extraction. Ollama + Qwen2.5-7B optimized for RTX 4070 Ti (12GB VRAM).

## Files

| File | Purpose |
|------|---------|
| **SETUP.md** | Installation & configuration guide (start here) |
| **hardware_probe.py** | Auto-detect VRAM; select model/batch/context (run on target machine) |
| **ollama_client.py** | OpenAI-compatible client (same interface for Ollama or Claude escalation) |
| **config.py** | Unified configuration loader (probe → save → env setup) |
| **Modelfile.qwen2.5-7b** | Qwen2.5-7B with KV-cache quantization for Ollama |
| **example_usage.py** | Sample extraction workflow |
| **requirements.txt** | Python dependencies |

## Quick Start

### 1. Install Ollama
```bash
# Windows: download from https://ollama.ai
ollama --version  # verify
```

### 2. Run hardware probe
```bash
python tools/edgar_scrubber/hardware_probe.py
```

Example output:
```
Model: qwen2.5:7b-instruct-q4_K_M
Batch size: 8
Context: 8192 tokens
KV cache quantization: q8_0
VRAM: 10.2GB free / 12.0GB total
```

### 3. Pull model
```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
```

### 4. Use in your code
```python
from tools.edgar_scrubber.config import ScrubberConfig
from tools.edgar_scrubber.ollama_client import OllamaClient

config = ScrubberConfig()  # probes hardware, saves config
client = OllamaClient(config.ollama_config)

response = client.chat_completion(
    messages=[{"role": "user", "content": "Extract company name from EDGAR text..."}]
)
print(response["choices"][0]["message"]["content"])
```

## Key Design

**12GB VRAM Constraint (RTX 4070 Ti):**
- Qwen2.5-7B: 4.7 GB weights + KV cache
- Batch 8 × 8k context: ~7 GB resident total
- KV-cache quantization (q8_0): 50% footprint reduction

**Single Endpoint:** OpenAI-compatible `/v1/chat/completions` at `localhost:11434/v1` works for both:
- Local Ollama (this repo)
- Claude escalation via `base_url` swap (`OllamaConfig.for_claude()`, issue #104 — see [EXTRACTION_LADDER.md](EXTRACTION_LADDER.md))

**Hardware Probe:** Never silently downgrades to CPU. Reports profile explicitly so tight-VRAM machines can make informed decisions (close VSCode/browser to free ~1.5 GB).

## Model Versions

Always use pinned versions (unpinned tags silently upgrade):

| Model | Size | Purpose |
|-------|------|---------|
| `qwen2.5:7b-instruct-q4_K_M` | ~4.7 GB | Primary EDGAR extraction |
| `qwen2.5:3b-instruct-q4_K_M` | ~2.0 GB | Cheap bulk pass (issue #108, benchmark first) |

Section routing (issue #101) turned out to be deterministic keyword/heading
classification, not an embedding model — see [DOCUMENT_REDUCTION.md](DOCUMENT_REDUCTION.md).
No embedding model is loaded by this runtime.

## Troubleshooting

**"Connection refused"**
```bash
ollama serve  # Start Ollama manually
```

**Batch 4 instead of 8**
- Close VSCode / browser tabs (~1.5 GB freed)
- Run probe again

**Model too slow (CPU fallback)**
- Verify `nvidia-smi` shows Ollama process with VRAM
- Check `hardware_probe.py` didn't select CPU_ONLY

**Model quality regression**
- Verify pinned model: `ollama list | grep qwen`
- Check Modelfile has `PARAMETER quantize q8_0`

See **SETUP.md** for full troubleshooting + remote Ollama setup.

## Related docs

- **[FIELD_SPEC.md](FIELD_SPEC.md)** — *what* to extract (issue #102)
- **[DOCUMENT_REDUCTION.md](DOCUMENT_REDUCTION.md)** — document expansion + the four-stage token-budget reduction (issue #101)
- **[EXTRACTION_LADDER.md](EXTRACTION_LADDER.md)** — *how* it escalates: rule → XBRL → local 7B → confidence gate → Claude (issue #104)
- **[OUTPUT_STORE.md](OUTPUT_STORE.md)** — *where* it lands + the local-only / graduation boundary (issue #109)
- **[VALIDATION_UI.md](VALIDATION_UI.md)** — *teach it first*: the human-in-loop side-by-side validation loop that seeds exemplars/rules/eval (issue #105)
- **[EVAL_HARNESS.md](EVAL_HARNESS.md)** — *is it still right*: held-out set + per-field precision/recall/span-accuracy regression gate (issue #108)
- **[THROUGHPUT_BENCHMARK.md](THROUGHPUT_BENCHMARK.md)** — *is it actually fast*: measured prefill/decode tok/s + peak VRAM, replacing every estimate (issue #108)
- **[RESEARCH_MAPS.md](RESEARCH_MAPS.md)** — *what does the output say*: issuance-by-underlying, barrier clustering vs spot, issuer markup league table — chart + written finding each, no modeling/controls/p-values (issue #130, Part A of #110)

## References

- Issue #95 (EDGAR scrubber parent)
- Issue #104 (Claude escalation)
- Issue #101 (document expansion + span-preserving reduction to the #103 token budget)
- Issue #108 (eval harness: held-out regression gate + measured throughput benchmark, including the 3B model comparison)
- Issue #130 (structured-note issuance maps, Part A of #110 — descriptive only)
- Ollama: https://ollama.ai
- Qwen2.5: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct
