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

        # OLLAMA_FLASH_ATTENTION / OLLAMA_KV_CACHE_TYPE / OLLAMA_NUM_PARALLEL are
        # read by the ollama SERVER at startup. Setting them here only has any
        # effect if this process later spawns `ollama serve` itself; against an
        # already-running server (the normal case on Windows, where the Ollama app
        # starts at login) they are inert. They are still exported so a
        # process-spawned server inherits them -- but the authority on whether they
        # took is verify_server_runtime(), not this call.
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


def verify_server_runtime(profile, base_url=None, timeout=300):
    """Check what the RUNNING ollama server actually does, not what we asked for.

    Why this exists: the KV-cache and parallelism settings are server-startup env
    vars. A client process cannot change them on a server that is already up, so
    `config` exporting them proves nothing. Measured on an RTX 4070 Ti with the
    server started by the Ollama Windows app, eight concurrent 8k requests moved
    peak VRAM by 17 MiB -- if eight parallel slots at 8192 ctx had really been
    allocated, the KV cache alone would have been 1.8-3.7 GB. The settings had
    silently not applied.

    Returns a dict of findings. `ok` is False when the server's behaviour does not
    match `profile`, so #103's "batch N at 8k resident with no CPU offload" is a
    measurement rather than an assumption.
    """
    import json as _json
    import urllib.request as _url

    base = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    root = base.rsplit("/v1", 1)[0]
    out = {"ok": True, "problems": [], "model": profile.model}

    # Force a load at the configured context so /api/ps reflects real allocation.
    req = _url.Request(
        f"{root}/api/generate",
        data=_json.dumps({
            "model": profile.model, "prompt": "ok", "stream": False,
            "options": {"num_ctx": profile.context_length, "num_predict": 1},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        _url.urlopen(req, timeout=timeout).read()
    except Exception as exc:                      # server down / model absent
        out["ok"] = False
        out["problems"].append(f"cannot reach or load model on {root}: {exc}")
        return out

    try:
        ps = _json.loads(_url.urlopen(f"{root}/api/ps", timeout=30).read())
    except Exception as exc:
        out["ok"] = False
        out["problems"].append(f"/api/ps unreadable: {exc}")
        return out

    entry = next((m for m in ps.get("models", []) if m.get("name") == profile.model), None)
    if entry is None:
        out["ok"] = False
        out["problems"].append(f"{profile.model} not resident after a load request")
        return out

    total = entry.get("size", 0)
    in_vram = entry.get("size_vram", 0)
    out["size_gb"] = total / 2 ** 30
    out["vram_gb"] = in_vram / 2 ** 30
    out["context_length"] = entry.get("context_length")

    # #103 acceptance: resident with no CPU offload.
    if total and in_vram < total:
        out["ok"] = False
        out["problems"].append(
            f"CPU offload: only {in_vram / 2 ** 30:.2f} of {total / 2 ** 30:.2f} GB in VRAM"
        )
    if out["context_length"] and out["context_length"] < profile.context_length:
        out["ok"] = False
        out["problems"].append(
            f"server context {out['context_length']} < requested {profile.context_length}"
        )

    # Measure the KV cache instead of guessing at it. Load the model at two
    # context sizes and read the slope of resident bytes per context token:
    #
    #   slope ~= bytes_per_token_per_slot * effective_parallel_slots
    #
    # For Qwen2.5-7B: 28 layers x 4 KV heads x 128 head_dim x 2 (K+V) = 28,672
    # elements/token -> 28 KB/token at q8_0 (1 byte/elem), 56 KB/token at f16.
    # The slope therefore reveals BOTH the cache type and how many parallel slots
    # the server really allocated -- neither of which any API reports directly.
    small_ctx = 2048
    try:
        _url.urlopen(_url.Request(
            f"{root}/api/generate",
            data=_json.dumps({
                "model": profile.model, "prompt": "ok", "stream": False,
                "options": {"num_ctx": small_ctx, "num_predict": 1},
            }).encode(), headers={"Content-Type": "application/json"},
        ), timeout=timeout).read()
        ps2 = _json.loads(_url.urlopen(f"{root}/api/ps", timeout=30).read())
        e2 = next((m for m in ps2.get("models", []) if m.get("name") == profile.model), None)
    except Exception:
        e2 = None

    if e2 and e2.get("context_length") == small_ctx:
        d_bytes = total - e2.get("size", 0)
        d_tok = profile.context_length - small_ctx
        if d_tok > 0 and d_bytes > 0:
            slope = d_bytes / d_tok
            out["kv_bytes_per_ctx_token"] = slope
            per_slot_q8, per_slot_f16 = 28672, 57344
            want = per_slot_q8 * profile.batch_size
            out["kv_expected_bytes_per_ctx_token"] = want
            # The slope is an UPPER bound on KV: resident size also carries compute
            # and graph buffers that grow with context. So a slope below what the
            # profile requires is conclusive, while the exact slot count is not --
            # report both readings rather than pretending to one.
            out["slots_if_q8_0"] = round(slope / per_slot_q8, 1)
            out["slots_if_f16"] = round(slope / per_slot_f16, 1)
            if slope < want * 0.6:
                out["ok"] = False
                out["problems"].append(
                    f"KV footprint is {slope:,.0f} bytes per context token; "
                    f"batch_size={profile.batch_size} at q8_0 requires {want:,.0f} "
                    f"(~{out['slots_if_q8_0']} slots at q8_0, ~{out['slots_if_f16']} at f16 "
                    f"-- either way short of {profile.batch_size}). OLLAMA_NUM_PARALLEL "
                    f"and OLLAMA_KV_CACHE_TYPE are server-STARTUP env vars: exporting "
                    f"them from this process cannot reach a server that is already "
                    f"running. Set them on the Ollama service and restart it; see SETUP.md."
                )
    else:
        out["problems"].append(
            "could not measure KV slope (second load did not report the smaller "
            "context); parallelism/cache-type unverified"
        )

    return out
