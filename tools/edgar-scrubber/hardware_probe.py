#!/usr/bin/env python3
"""
Hardware probe for local model runtime. Reports free VRAM, selects model/batch size.
Designed for RTX 4070 Ti (12GB VRAM constraint).
"""

import subprocess
import json
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass
class HardwareProfile:
    total_vram_gb: float
    free_vram_gb: float
    model: str
    batch_size: int
    context_length: int
    kv_cache_quantization: str
    max_tokens: int

    def summary(self) -> str:
        """Human-readable profile summary."""
        return (
            f"Model: {self.model}\n"
            f"Batch size: {self.batch_size}\n"
            f"Context: {self.context_length} tokens\n"
            f"KV cache quantization: {self.kv_cache_quantization}\n"
            f"Max generation: {self.max_tokens} tokens\n"
            f"VRAM: {self.free_vram_gb:.1f}GB free / {self.total_vram_gb:.1f}GB total"
        )


def get_nvidia_vram() -> tuple[float, float]:
    """
    Query NVIDIA GPU VRAM using nvidia-smi.
    Returns (total_gb, free_gb) or (0, 0) if no GPU or nvidia-smi unavailable.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.free",
                "--format=csv,nounits,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(",")
            total_mb = float(parts[0])
            free_mb = float(parts[1])
            return total_mb / 1024, free_mb / 1024
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError, ValueError):
        pass
    return 0, 0


def select_profile(total_vram: float, free_vram: float) -> HardwareProfile:
    """
    Select model and batch configuration based on available VRAM.

    Assumes ~1.0-1.5GB held by Windows desktop/VSCode/browser.
    Usable ~= free - 1.0GB for safety.

    - 7B Q4_K_M: ~4.7GB weights + KV cache
    - 3B Q4: ~2.0GB weights + KV cache
    """
    # Conservatively assume 1.5GB held by system.
    usable = max(0, free_vram - 1.5)

    if total_vram < 4:
        # Below 4GB total: CPU inference, no model selection.
        return HardwareProfile(
            total_vram_gb=total_vram,
            free_vram_gb=free_vram,
            model="CPU_ONLY",
            batch_size=1,
            context_length=4096,
            kv_cache_quantization="q8_0",
            max_tokens=512,
        )

    if usable >= 8.0:
        # 7B fits comfortably: batch 8, 8k context, q8_0 quantization.
        return HardwareProfile(
            total_vram_gb=total_vram,
            free_vram_gb=free_vram,
            model="qwen2.5:7b-instruct-q4_K_M",
            batch_size=8,
            context_length=8192,
            kv_cache_quantization="q8_0",
            max_tokens=2048,
        )

    if usable >= 5.0:
        # 7B fits with reduced batch: batch 4, 8k context.
        return HardwareProfile(
            total_vram_gb=total_vram,
            free_vram_gb=free_vram,
            model="qwen2.5:7b-instruct-q4_K_M",
            batch_size=4,
            context_length=8192,
            kv_cache_quantization="q8_0",
            max_tokens=2048,
        )

    if usable >= 4.0:
        # 7B fits with further batch reduction: batch 2, 4k context.
        return HardwareProfile(
            total_vram_gb=total_vram,
            free_vram_gb=free_vram,
            model="qwen2.5:7b-instruct-q4_K_M",
            batch_size=2,
            context_length=4096,
            kv_cache_quantization="q8_0",
            max_tokens=2048,
        )

    # Fallback to 3B if space is very tight.
    return HardwareProfile(
        total_vram_gb=total_vram,
        free_vram_gb=free_vram,
        model="qwen2.5:3b-instruct-q4_K_M",
        batch_size=4,
        context_length=8192,
        kv_cache_quantization="q8_0",
        max_tokens=1024,
    )


def check_ollama_running() -> bool:
    """Check if Ollama is running on localhost:11434."""
    try:
        result = subprocess.run(
            ["curl", "-s", "http://localhost:11434/api/tags"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def main():
    """Probe hardware and report configuration."""
    total_vram, free_vram = get_nvidia_vram()

    if total_vram == 0:
        print("No NVIDIA GPU detected. Using CPU inference.", file=sys.stderr)

    profile = select_profile(total_vram, free_vram)

    # Report profile
    print("=" * 60)
    print("MODEL RUNTIME HARDWARE PROBE")
    print("=" * 60)
    print()
    print(profile.summary())
    print()

    # Check Ollama
    if check_ollama_running():
        print("[OK] Ollama is running on localhost:11434")
    else:
        print("[WARN] Ollama is NOT running. Start it before running scrubber.")
        print("  Windows: run 'ollama serve' in a terminal")
        print("  Linux/Mac: 'ollama serve' or use the daemon")

    print()
    print("=" * 60)
    print("Configuration for OLLAMA_NUM_PARALLEL and num_ctx:")
    print("=" * 60)
    print(f"export OLLAMA_NUM_PARALLEL={profile.batch_size}")
    print(f"# Modelfile num_ctx should be set to {profile.context_length}")
    print()

    return json.dumps(
        {
            "total_vram_gb": profile.total_vram_gb,
            "free_vram_gb": profile.free_vram_gb,
            "model": profile.model,
            "batch_size": profile.batch_size,
            "context_length": profile.context_length,
            "kv_cache_quantization": profile.kv_cache_quantization,
            "max_tokens": profile.max_tokens,
        }
    )


if __name__ == "__main__":
    result = main()
    # Print JSON to stdout for programmatic use
    print(result)
