"""EDGAR document scrubber: SEC-compliant fetch, field specs, local model runtime."""

from .config import ScrubberConfig, verify_server_runtime
from .crawl import Crawler, CrawlState, SavedQuery, crawl, load_query, save_query
from .edgar_client import EdgarClient, EdgarError, EdgarConfigError, EdgarHTTPError
from .extraction_ladder import (
    ExtractionLadder,
    GateResult,
    GateSignal,
    LadderResult,
    LogEntry,
    MalformedOutputError,
    Provenance,
    RuleMatch,
    RunLog,
    build_wire_schema,
    chat_json,
    estimate_cost,
    evaluate_gate,
)
from .field_spec import load_specs
from .hardware_probe import HardwareProfile, get_nvidia_vram, select_profile
from .ollama_client import OllamaClient, OllamaConfig
from .output_store import (
    DocumentExtraction,
    FieldValue,
    GraduationError,
    OutputStore,
    OwnershipError,
)
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
    # date-chunked resumable crawl (#100)
    "SavedQuery",
    "Crawler",
    "CrawlState",
    "crawl",
    "load_query",
    "save_query",
    # field specs (#102)
    "load_specs",
    "REGISTRY",
    # extraction provider ladder: rule -> XBRL -> local 7B -> Claude (#104)
    "ExtractionLadder",
    "RuleMatch",
    "GateResult",
    "GateSignal",
    "evaluate_gate",
    "Provenance",
    "LogEntry",
    "RunLog",
    "LadderResult",
    "build_wire_schema",
    "chat_json",
    "estimate_cost",
    "MalformedOutputError",
    # local output store + ownership boundary (#109)
    "OutputStore",
    "DocumentExtraction",
    "FieldValue",
    "OwnershipError",
    "GraduationError",
]
