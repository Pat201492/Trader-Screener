"""Data-accuracy suite for the EDGAR scrubber -- a debugging tool, not a feature.

    python -m accuracy golden        # offline: every extractor vs hand-checked filings
    python -m accuracy cross         # the same fact from two independent sources
    python -m accuracy sanity        # impossible values across the local stores
    python -m accuracy canary [N]    # LIVE: N fresh filings per form, read and sanity-checked
    python -m accuracy all           # golden + cross + sanity (no network)

Run from tools/edgar_scrubber (or with it on sys.path). Exit status 1 when a
golden value is wrong, so it can gate a merge; cross / sanity / canary report
findings without failing, because the stores hold history from older code.
"""
