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
from .eval_harness import (
    EvalReport,
    EvalReportStore,
    FieldMetrics,
    FieldRegression,
    GateReport,
    HeldOutCase,
    evaluate_regression,
    reserve_held_out_set,
    run_eval,
    select_held_out,
)
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
from .throughput_bench import (
    BenchAxes,
    CellResult,
    build_prompt,
    build_prompt_text,
    compare_accuracy_tradeoff,
    matrix_axes,
    prefix_cache_check,
    run_cell,
    run_matrix,
    summarize_matrix,
    token_reduction_check,
)
from .validation import (
    ACCEPT,
    CORRECT,
    REJECT,
    FieldProposal,
    FieldVerdict,
    LadderExtractor,
    RenderDocument,
    RuleSeed,
    ValidationSession,
    ValidationStore,
    build_field_context,
    derive_anchor,
    locate_span,
    render_highlight,
)

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
    # human-in-loop validation loop (#105)
    "ValidationSession",
    "ValidationStore",
    "LadderExtractor",
    "RenderDocument",
    "FieldProposal",
    "FieldVerdict",
    "RuleSeed",
    "ACCEPT",
    "CORRECT",
    "REJECT",
    "build_field_context",
    "derive_anchor",
    "locate_span",
    "render_highlight",
    # extraction eval harness: held-out set + per-field precision/recall + regression gate (#108)
    "EvalReport",
    "EvalReportStore",
    "FieldMetrics",
    "FieldRegression",
    "GateReport",
    "HeldOutCase",
    "evaluate_regression",
    "reserve_held_out_set",
    "run_eval",
    "select_held_out",
    # throughput benchmark: measured prefill/decode tok/s + peak VRAM matrix (#108)
    "BenchAxes",
    "CellResult",
    "build_prompt",
    "build_prompt_text",
    "compare_accuracy_tradeoff",
    "matrix_axes",
    "prefix_cache_check",
    "run_cell",
    "run_matrix",
    "summarize_matrix",
    "token_reduction_check",
]
