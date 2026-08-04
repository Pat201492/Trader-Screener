"""EDGAR document scrubber with local model runtime."""

from .hardware_probe import HardwareProfile, get_nvidia_vram, select_profile
from .ollama_client import OllamaClient, OllamaConfig
from .config import ScubberConfig

__all__ = [
    "HardwareProfile",
    "get_nvidia_vram",
    "select_profile",
    "OllamaClient",
    "OllamaConfig",
    "ScubberConfig",
]
