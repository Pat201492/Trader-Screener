#!/usr/bin/env python3
"""
Capture script to fetch a real 8-K Item 2.02 EX-99.1 exhibit for a given ticker
and write it to the fixtures directory. Runs only when EDGAR_LIVE=1 and
EDGAR_USER_AGENT are both set, matching the gating convention used by
test_edgar_client.py.

This script supports guidance-chain fixture regeneration by fetching real
earnings press releases from EDGAR.

Usage:
  python tools/edgar_scrubber/capture_8k_ex99.py TICKER

Example:
  EDGAR_LIVE=1 EDGAR_USER_AGENT="Name name@example.com" \\
    python tools/edgar_scrubber/capture_8k_ex99.py AAPL

Set EDGAR_LIVE=1 and EDGAR_USER_AGENT='<name> <email>' to run against live EDGAR.
See SETUP.md#edgar_user_agent-sec-live-tests for persistent configuration.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import edgar_client as ec
import earnings_releases as er


def main():
    # Check arguments early
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} TICKER")
        if os.environ.get("EDGAR_LIVE") != "1":
            print("  [skip] set EDGAR_LIVE=1 and EDGAR_USER_AGENT='<name> <email>' to run this")
        return 1

    ticker = sys.argv[1].strip().upper()

    # Gate on live environment variables
    if os.environ.get("EDGAR_LIVE") != "1":
        print("  [skip] set EDGAR_LIVE=1 and EDGAR_USER_AGENT='<name> <email>' to run this")
        return 0

    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        print("  [error] EDGAR_USER_AGENT not set; set both EDGAR_LIVE=1 and EDGAR_USER_AGENT")
        return 1

    # Create EDGAR client with live transport
    cache_dir = tempfile.mkdtemp(prefix="capture_8k_")
    try:
        client = ec.EdgarClient(ua, cache_dir)

        # Resolve ticker to CIK
        try:
            cik = client.ticker_to_cik(ticker)
        except ec.EdgarLookupError as e:
            print(f"  [error] {e}")
            return 1

        print(f"  [info] {ticker} -> CIK {cik}")

        # Get earnings release 8-K filings
        filings = er.earnings_release_filings(client, cik)
        if not filings:
            print(f"  [error] no 8-K Item 2.02 filings found for {ticker}")
            return 1

        # Get the most recent filing
        filing = filings[0]
        accession = filing["accession"]
        filing_date = filing["filing_date"]

        print(f"  [info] using filing from {filing_date} (accession {accession})")

        # Get the EX-99.1 exhibit
        exhibit = er.press_release_document(filing)
        if not exhibit:
            print(f"  [error] no EX-99 exhibit found in this filing")
            return 1

        exhibit_name = exhibit["name"]
        print(f"  [info] fetching exhibit {exhibit_name}")

        # Fetch the exhibit content
        content = client.archive_document(cik, accession, exhibit_name)
        if not content:
            print(f"  [error] failed to fetch exhibit {exhibit_name}")
            return 1

        # Determine output filename (guidance_<ticker>_<date>.txt)
        date_str = filing_date.replace("-", "")
        output_name = f"guidance_{ticker.lower()}_{date_str}.txt"
        fixtures_dir = Path(__file__).parent / "fixtures"
        fixtures_dir.mkdir(exist_ok=True)

        output_path = fixtures_dir / output_name

        # Decode bytes to text (handle both HTML and text formats)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            # Try latin-1 as fallback
            text = content.decode("latin-1")

        # Write to fixture file
        output_path.write_text(text, encoding="utf-8")

        print(f"  [ok] wrote {len(text)} chars to {output_path}")
        return 0

    finally:
        # Clean up temp cache dir
        if os.path.exists(cache_dir):
            shutil.rmtree(cache_dir)


if __name__ == "__main__":
    sys.exit(main())
