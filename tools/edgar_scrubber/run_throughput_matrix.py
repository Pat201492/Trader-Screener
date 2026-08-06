"""
One-off runner: fetch real 424B2 filings from EDGAR, run the throughput_bench
matrix against a live local Ollama server, dump results as JSON.

Not part of the module's public surface (not imported anywhere) -- a script
to produce tools/edgar_scrubber/throughput_results/2026-08-06_rtx4070ti.json,
kept here so the exact fetch/measurement steps are reproducible/auditable.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from edgar_client import EdgarClient, parse_hit
from field_spec import load_specs
from normalize import normalize_html
from reduce import split_sections
from validation import RenderDocument, build_field_context
from throughput_bench import BenchAxes, run_cell, build_prompt

USER_AGENT = "Trader-Screener-Study pegan604@gmail.com"
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".bench_cache")
OLLAMA_BASE_URL = "http://localhost:11434"
N_DOCS = 5
MODELS = ("qwen2.5:3b-instruct-q4_K_M", "qwen2.5:7b-instruct-q4_K_M")
# batch is not varied in this real pass: run_cell (throughput_bench.py) issues
# generate_fn calls strictly sequentially regardless of axes.batch (batch only
# changes VRAM-sample chunking), so batch=1 vs batch=4 would replay identical
# calls with this harness as shipped -- no additional signal for double the
# runtime. True concurrent-batch measurement is a follow-up.
BATCHES = (1,)
ROUTING = ("whole_section", "sub_block")
PREPROCESSING = ("raw_table", "flattened_pairs")
FIELD_NAME = "barrier_pct"


def fetch_real_docs(client, n=N_DOCS):
    """Real 424B2 structured-note filings via the same endpoints edgar_client
    wraps for the crawler -- full-text search -> accession index -> primary
    document HTML. Cached under .bench_cache/ so a re-run costs zero requests."""
    docs = []
    for raw_hit in client.iter_hits(forms="424B2", max_results=40):
        if len(docs) >= n:
            break
        hit = parse_hit(raw_hit)
        cik = hit.get("cik")
        accession = hit.get("accession")
        if not cik or not accession:
            continue
        try:
            idx = client.filing_index(cik, accession)
            doc_name = EdgarClient.primary_document(idx)
            if not doc_name:
                continue
            html_bytes = client.archive_document(cik, accession, doc_name)
            html = html_bytes.decode("utf-8", "replace")
            docs.append({"issuer": hit.get("issuer"), "accession": accession,
                        "document": doc_name, "html": html})
        except Exception as e:
            print(f"  skip {cik}/{accession}: {e}", file=sys.stderr)
            continue
    return docs


def build_real_prompts(docs, *, routing, preprocessing):
    specs = load_specs()
    field_def = specs["structured_note"].field(FIELD_NAME)
    prompts = []
    for d in docs:
        nd = normalize_html(d["html"])
        render_doc = RenderDocument.from_normalized(nd)
        sections = split_sections(nd)
        try:
            build_field_context(render_doc, sections, field_def)
        except Exception:
            continue
        prompts.append(build_prompt(field_def, render_doc, sections,
                                    routing=routing, preprocessing=preprocessing))
    return prompts


def main():
    client = EdgarClient(user_agent=USER_AGENT, cache_dir=CACHE_DIR)
    print("Fetching real 424B2 filings from EDGAR...")
    docs = fetch_real_docs(client)
    print(f"Fetched {len(docs)} real filings: "
         f"{[d['issuer'] for d in docs]}")
    if not docs:
        print("No documents fetched -- aborting", file=sys.stderr)
        sys.exit(1)

    kv_quant = os.environ.get("OLLAMA_KV_CACHE_TYPE", "fp16 (server default, not overridden)")

    def generate_fn(payload):
        import requests
        resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json()

    results = []
    t0 = time.time()
    for model in MODELS:
        for batch in BATCHES:
            for routing in ROUTING:
                for preprocessing in PREPROCESSING:
                    prompts = build_real_prompts(docs, routing=routing, preprocessing=preprocessing)
                    if not prompts:
                        print(f"  no prompts for routing={routing} prep={preprocessing}, skipping")
                        continue
                    axes = BenchAxes(model=model, batch=batch, kv_quant=kv_quant,
                                     routing=routing, preprocessing=preprocessing)
                    print(f"Running {axes.cell_id()} ({len(prompts)} real docs)...")
                    cell = run_cell(axes, prompts, generate_fn=generate_fn)
                    results.append(cell.as_dict())
                    print(f"  prefill={cell.prefill_tok_s} tok/s decode={cell.decode_tok_s} tok/s "
                         f"tokens/doc={cell.tokens_per_doc} sec/doc={cell.seconds_per_doc} "
                         f"peak_vram={cell.peak_vram_gb}GB")
    elapsed = time.time() - t0

    out = {
        "hardware": "RTX 4070 Ti (12GB), measured via nvidia-smi at run time",
        "kv_quant_note": ("Only the server's already-running KV-cache setting was measured "
                          "(no OLLAMA_KV_CACHE_TYPE restart performed in this pass) -- "
                          "q8_0 comparison is a follow-up."),
        "documents": [{"issuer": d["issuer"], "accession": d["accession"], "document": d["document"]}
                     for d in docs],
        "n_documents": len(docs),
        "field_measured": FIELD_NAME,
        "elapsed_seconds": round(elapsed, 1),
        "cells": results,
    }
    out_path = os.path.join(os.path.dirname(__file__), "throughput_results",
                            "2026-08-06_rtx4070ti.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
