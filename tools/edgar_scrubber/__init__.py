"""EDGAR document scrubber: SEC-compliant fetch, field specs, local model runtime."""

from .config import ScrubberConfig, verify_server_runtime
from .edgar_client import EdgarClient, EdgarError, EdgarConfigError, EdgarHTTPError
from .field_spec import load_specs
from .hardware_probe import HardwareProfile, get_nvidia_vram, select_profile
from .ollama_client import OllamaClient, OllamaConfig
from .schema_registry import REGISTRY

__all__ = [
    # runtime (#103)
    "HardwareProfile",
    "get_nvidia_vram",
    "select_profile",
    "OllamaClient",
    "OllamaConfig",
    "ScrubberConfig",
    "verify_server_runtime",
    # EDGAR fetch (#99)
    "EdgarClient",
    "EdgarError",
    "EdgarConfigError",
    "EdgarHTTPError",
    # field specs (#102)
    "load_specs",
    "REGISTRY",
]
