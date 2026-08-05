"""
Runtime configuration for EDGAR scrubber.
Loads hardware profile at startup; reports and persists settings.
"""

import os
import json
from pathlib import Path
from dataclasses import asdict
from typing import Optional

from .hardware_probe import get_nvidia_vram, select_profile, HardwareProfile
from .ollama_client import OllamaConfig


class ScrubberConfig:
    """Centralized scrubber configuration."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize config from environment or file.

        Args:
            config_path: optional path to saved config JSON (from prior probe).
                If not provided, runs probe and saves to ~/.edgar-scrubber/config.json
        """
        self.config_path = Path(config_path or Path.home() / ".edgar-scrubber" / "config.json")
        self.hardware_profile: HardwareProfile = None
        self.ollama_config: OllamaConfig = None

        self._load_or_probe()
        self._setup_environment()

    def _load_or_probe(self):
        """Load saved profile or run probe."""
        if self.config_path.exists():
            try:
                with open(self.config_path) as f:
                    saved = json.load(f)
                # Reconstruct HardwareProfile from saved dict
                self.hardware_profile = HardwareProfile(**saved)
                return
            except (json.JSONDecodeError, TypeError, KeyError):
                pass  # Fall through to probe

        # Probe hardware
        total_vram, free_vram = get_nvidia_vram()
        self.hardware_profile = select_profile(total_vram, free_vram)

        # Save for next run
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            json.dump(asdict(self.hardware_profile), f, indent=2)

    def _setup_environment(self):
        """Set environment variables from profile."""
        os.environ.setdefault("OLLAMA_NUM_PARALLEL", str(self.hardware_profile.batch_size))
        os.environ.setdefault("OLLAMA_NUM_CTX", str(self.hardware_profile.context_length))
        os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        os.environ.setdefault("OLLAMA_MODEL", self.hardware_profile.model)

        # KV-cache quantization is a SERVER setting, not a Modelfile PARAMETER.
        # `PARAMETER quantize q8_0` is silently ignored by Ollama (`quantize` is a
        # flag on `ollama create`, and it quantizes weights, not the KV cache), so
        # without these two the server runs fp16 KV at 56 KB/token while the probe
        # budgets 28 KB/token -- a 2x error in the headroom that sizes batch_size.
        # q8_0 KV requires flash attention; setting the type without it is a no-op.
        if self.hardware_profile.kv_cache_quantization != "f16":
            os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
            os.environ.setdefault(
                "OLLAMA_KV_CACHE_TYPE", self.hardware_profile.kv_cache_quantization
            )

        self.ollama_config = OllamaConfig.from_env(asdict(self.hardware_profile))

    def report(self) -> str:
        """Human-readable profile report."""
        lines = [
            "=" * 60,
            "EDGAR SCRUBBER CONFIGURATION",
            "=" * 60,
            "",
            self.hardware_profile.summary(),
            "",
            "Runtime env vars:",
            f"  OLLAMA_BASE_URL={os.getenv('OLLAMA_BASE_URL')}",
            f"  OLLAMA_MODEL={os.getenv('OLLAMA_MODEL')}",
            f"  OLLAMA_NUM_PARALLEL={os.getenv('OLLAMA_NUM_PARALLEL')}",
            f"  OLLAMA_NUM_CTX={os.getenv('OLLAMA_NUM_CTX')}",
            f"  OLLAMA_FLASH_ATTENTION={os.getenv('OLLAMA_FLASH_ATTENTION')}",
            f"  OLLAMA_KV_CACHE_TYPE={os.getenv('OLLAMA_KV_CACHE_TYPE')}",
            "",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<ScrubberConfig model={self.hardware_profile.model} batch={self.hardware_profile.batch_size}>"
