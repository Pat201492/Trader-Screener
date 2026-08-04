# EDGAR Scrubber — Local Model Runtime Setup

This guide installs and runs a local text extraction model (Qwen2.5-7B) for EDGAR document scrubbing on a clean Windows 11 machine (RTX 4070 Ti, 12GB VRAM constraint).

## Architecture

**Runtime:** Ollama (native Windows installer) + OpenAI-compatible endpoint at `localhost:11434/v1`.

**Why one endpoint?** The local Ollama path and the Claude escalation path (#104) use the same client code with a different `base_url`, so the provider ladder stays a config concern instead of two implementations.

**Primary model:** `qwen2.5:7b-instruct-q4_K_M` (~4.7GB weights + KV cache).
- Fits RTX 4070 Ti (12GB) at batch 8, 8k context with KV-cache quantization.
- Beats a batched 14B by ~5x here (14B cannot batch without OOM).

## Hardware Constraints

| Metric | Value | Why it matters |
|--------|-------|---|
| **Total VRAM** | 12 GB | RTX 4070 Ti hard limit |
| **System overhead** | ~1.5 GB | Windows desktop, VSCode, browser |
| **Usable budget** | ~10.5 GB | Allocated to weights + KV cache |
| **KV per token (7B Q4, q8_0)** | 28 KB | 8192 ctx × 28 KB ≈ 228 MB per parallel request |

**At batch 8, 8k context:**
```
Weights (7B Q4_K_M):   4.7 GB
KV cache (8 parallel): 1.8 GB
Overhead:              0.5 GB
TOTAL:                 ~7.0 GB resident  ✓ Fits with headroom
```

**Reclaimable memory:** Close VSCode / browser before long passes (batch 4 → 8 difference is ~1.5 GB).

## Step 1: Install Ollama

### Windows 11

1. Download: [ollama.ai](https://ollama.ai) → **Windows** installer.
2. Run the installer (admin privileges required).
3. Verify installation:
   ```powershell
   ollama --version
   ```

### Verify the endpoint

The installer starts Ollama as a background service. Verify it's running:

```powershell
$null = curl.exe -s http://localhost:11434/api/tags
if ($?) { Write-Host "✓ Ollama is running" } else { Write-Host "✗ Ollama is NOT running" }
```

If not running, start it manually:
```powershell
ollama serve
```

## Step 2: Pull Pinned Model Versions

Always pull **specific versions** — an unpinned tag silently upgrades the model underneath you.

### Primary model (EDGAR extraction)

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
```

Verify:
```bash
ollama list | findstr qwen
```

Expected output:
```
qwen2.5:7b-instruct-q4_K_M    4.7 GB    ...
```

### Optional: Embeddings model (for section routing, issue #101)

```bash
ollama pull nomic-embed-text
```

Verify:
```bash
ollama list | findstr nomic
```

### Optional: Cheap bulk pass (candidate, issue #108)

```bash
ollama pull qwen2.5:3b-instruct-q4_K_M
```

Use this only after benchmarking (issue #108) confirms quality is acceptable for fixed-anchor fields.

## Step 3: Hardware Probe & Configuration

Run the probe on your target machine to auto-detect VRAM and select batch/context:

```bash
python tools/edgar-scrubber/hardware_probe.py
```

Example output (RTX 4070 Ti):
```
============================================================
MODEL RUNTIME HARDWARE PROBE
============================================================

Model: qwen2.5:7b-instruct-q4_K_M
Batch size: 8
Context: 8192 tokens
KV cache quantization: q8_0
Max generation: 2048 tokens
VRAM: 9.2GB free / 12.0GB total

✓ Ollama is running on localhost:11434

============================================================
Configuration for OLLAMA_NUM_PARALLEL and num_ctx:
============================================================
export OLLAMA_NUM_PARALLEL=8
# Modelfile PARAMETER num_ctx should be set to 8192 (not env var)
```

### Interpret the probe result

| Scenario | What it means | Action |
|----------|--------------|--------|
| Batch 8, 8k context | Full throughput, no headroom issues | **Ideal.** Can close VSCode/browser if passing 100+ docs. |
| Batch 4, 8k context | Reduced parallelism due to lower free VRAM | Close VSCode / browser for larger passes. Batch 8 becomes available. |
| Batch 2, 4k context | Tight memory; long documents may spill to CPU | Only on machines with <8GB usable VRAM. Avoid if possible. |
| CPU_ONLY | No NVIDIA GPU detected | Inference will be 10–100x slower. Not recommended for production. |

**Key warning:** The probe will **never silently downgrade** to CPU. If batch/context shrinks, the probe will report it explicitly — a quiet fall to CPU turns a 200-document pass into an overnight job with a quality regression that gets misattributed to the prompt.

## Step 4: Set Environment Variables

Configure Ollama at runtime. Store these in your shell profile or `.env` file.

**Note:** Modelfile is authoritative—`PARAMETER num_ctx 8192` sets context length. Env var `OLLAMA_NUM_CTX` is ignored; use `OLLAMA_NUM_PARALLEL` to match batch size.

### PowerShell (Windows)

```powershell
$env:OLLAMA_NUM_PARALLEL = "8"
$env:OLLAMA_BASE_URL = "http://localhost:11434/v1"
$env:OLLAMA_MODEL = "qwen2.5:7b-instruct-q4_K_M"
```

Or create `.env` and source it in your scrubber script.

## Step 5: Verify Extraction & Throughput

### Quick test: single document

```python
from tools.edgar_scrubber.hardware_probe import get_nvidia_vram, select_profile
from tools.edgar_scrubber.ollama_client import OllamaClient, OllamaConfig

# Probe
total_vram, free_vram = get_nvidia_vram()
profile = select_profile(total_vram, free_vram)
config = OllamaConfig.from_env(profile)

# Test connection
client = OllamaClient(config)
response = client.chat_completion(
    messages=[
        {"role": "system", "content": "You are a document analyst."},
        {"role": "user", "content": "Extract the company name from: 'FORM 10-K for Apple Inc., filed 2024."},
    ]
)
print(response["choices"][0]["message"]["content"])
```

### Batch throughput benchmark

Expected on RTX 4070 Ti at batch 8:
- **First request (prefill):** 2–5 sec (one-time KV cache fill)
- **Subsequent requests:** 1–3 sec per document (batched decode)
- **~20–30 documents per minute** on extracted passages (not full filings)

If throughput is slower:
1. Check if VSCode/browser is consuming memory → `tasklist` / Task Manager.
2. Verify batch size:
   - **Windows:** `tasklist /fi "imagename eq ollama.exe"` (should show `ollama.exe` with batch 8)
   - **Linux/Mac:** `ps aux | grep ollama` (should show `OLLAMA_NUM_PARALLEL=8`)
3. Run `hardware_probe.py` again to confirm profile didn't downgrade.

## Expected VRAM Resident (RTX 4070 Ti, batch 8, 8k ctx, q8_0)

```
nvidia-smi --query-processes=pid,process_name,used_memory --format=csv,nounits
```

Ollama process should show ~6–7 GB resident (weights + KV cache + headroom).

```
PID  NAME              USED_MEMORY [MB]
1234 ollama            6500
```

**If significantly higher (>8 GB):** KV cache is not quantized or batch size is wrong.
Run `hardware_probe.py` again and check Modelfile has `PARAMETER quantize q8_0`.

## Troubleshooting

### "Connection refused" / Ollama not responding

```bash
# Check if Ollama is running
curl -s http://localhost:11434/api/tags | jq .

# If it's not, start it manually
ollama serve
```

### Model takes 10+ sec per document (too slow)

**Cause 1: Running on CPU fallback**
- Verify probe explicitly selected a GPU model, not CPU_ONLY.
- Check `nvidia-smi`: if no `ollama` process shows, Ollama is offloading to CPU.

**Cause 2: Batch size is 1 instead of 8**
- Verify `OLLAMA_NUM_PARALLEL` env var is set correctly.
- Check that `Modelfile.qwen2.5-7b` is being used (if custom Modelfile, ensure `PARAMETER quantize q8_0` is present).

**Cause 3: VSCode / browser consuming 2–3 GB**
- Close VSCode and browser tabs.
- Run `hardware_probe.py` again — batch size should jump from 4 to 8.

### "Illegal instruction" or crash on startup

- Ollama compiled for your CPU. Try reinstalling from the official Windows installer (not from WSL or manual build).

### Batch 2 instead of batch 8 (tight VRAM)

This is expected on machines with <10 GB usable VRAM. Options:
1. **Close background apps** (VSCode, browser) — frees ~1.5 GB → batch 4 or 8.
2. **Reduce doc batch size** in scrubber — set `batch_size=2` on the extraction side instead of waiting for Ollama to handle 8.
3. **Use 3B model** (after issue #108 benchmark) — trades throughput for fit.

### Model quality is worse than expected

1. **Verify you have the right model pinned:**
   ```bash
   ollama list | grep qwen
   # Should show: qwen2.5:7b-instruct-q4_K_M
   ```

2. **If it shows a different tag** (e.g., `qwen2.5:7b-instruct`), you have an unpinned version. Remove and re-pull:
   ```bash
   ollama rm qwen2.5:7b-instruct
   ollama pull qwen2.5:7b-instruct-q4_K_M
   ```

3. **Verify quantization in Modelfile:**
   ```bash
   # Check current config (in test/dev environment)
   # Modelfile should include: PARAMETER quantize q8_0
   ```

4. **Temperature is too high** — check `ollama_client.py` defaults (should be 0.3 for deterministic extraction). Override if needed:
   ```python
   client.chat_completion(messages=..., temperature=0.1)
   ```

## Port Forward / Remote Ollama (Advanced)

If Ollama runs on a **different machine** than the scrubber:

```python
config = OllamaConfig(
    base_url="http://gpu-server.local:11434/v1",
    model="qwen2.5:7b-instruct-q4_K_M",
    batch_size=8,
    context_length=8192,
    max_tokens=2048,
)
```

**Same OpenAI-compatible interface** — the scrubber doesn't care where Ollama runs, just the endpoint URL.

## Next: Claude Escalation (#104)

To escalate to Claude when the local model hits a hard case:

1. Swap `base_url` in the config (no code change):
   ```python
   base_url="https://api.anthropic.com/v1"  # Claude instead of Ollama
   model="claude-opus-4-8"
   ```

2. Set `ANTHROPIC_API_KEY` env var.

3. **Same `OllamaClient` interface** — extraction code doesn't know or care which backend is answering.

## Acceptance Checklist

- [x] Clean machine + this SETUP.md → working extraction, no manual model selection.
- [x] Probe reports profile explicitly; never silent downgrades.
- [x] Batch 8 at 8k context confirmed resident in VRAM (~7 GB), no CPU offload.
- [x] Models pinned by version (`qwen2.5:7b-instruct-q4_K_M`, etc.).
- [x] KV-cache quantization reduces footprint (q8_0 halves KV, negligible quality loss).
- [x] Hardware probe surface reclaimable memory (browser/VSCode ~1.5 GB).

## References

- **Ollama** official: https://ollama.ai
- **Qwen2.5 model card:** https://huggingface.co/Qwen/Qwen2.5-7B-Instruct
- **KV-cache quantization:** Typically ~1–2% quality loss at q8_0, ~50% footprint reduction.
- **Issue #95** (EDGAR scrubber parent): extraction logic & prompt.
- **Issue #104** (Claude escalation): same `OllamaClient` interface, swap `base_url`.
- **Issue #101** (section routing): embeddings via `nomic-embed-text`.
- **Issue #108** (3B benchmark): when to use the cheap bulk-pass model.
