"""EDGAR document scrubber: SEC-compliant fetch, field specs, local model runtime."""

from .config import ScrubberConfig, verify_server_runtime
from .crawl import Crawler, CrawlState, SavedQuery, crawl, load_query, save_query
from .document_expand import (
    AccessionDocuments,
    Ex107Facts,
    ExpandedDocument,
    classify_document,
    expand_accession,
    parse_ex107_xbrl,
)
from .edgar_client import EdgarClient, EdgarError, EdgarConfigError, EdgarHTTPError
from .field_spec import load_specs
from .hardware_probe import HardwareProfile, get_nvidia_vram, select_profile
from .normalize import NormalizedDocument, OffsetMap, normalize_html, parse_tables
from .ollama_client import OllamaClient, OllamaConfig
from .output_store import (
    DocumentExtraction,
    FieldValue,
    GraduationError,
    OutputStore,
    OwnershipError,
)
from .reduce import (
    BoilerplateModel,
    ReducedDocument,
    build_boilerplate_model,
    split_sections,
    strip_boilerplate,
    sub_block,
    text_for_sections,
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
    # local output store + ownership boundary (#109)
    "OutputStore",
    "DocumentExtraction",
    "FieldValue",
    "OwnershipError",
    "GraduationError",
    # document expansion + EX-107 inline XBRL (#101)
    "AccessionDocuments",
    "ExpandedDocument",
    "Ex107Facts",
    "classify_document",
    "expand_accession",
    "parse_ex107_xbrl",
    # HTML normalize + span offsets + stage-1 table pre-parse (#101)
    "NormalizedDocument",
    "OffsetMap",
    "normalize_html",
    "parse_tables",
    # stages 2-4: boilerplate dedup, section split, sub-block routing (#101)
    "BoilerplateModel",
    "ReducedDocument",
    "build_boilerplate_model",
    "strip_boilerplate",
    "split_sections",
    "text_for_sections",
    "sub_block",
]
