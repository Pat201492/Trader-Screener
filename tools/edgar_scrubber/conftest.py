"""
Path shim for issue #138.

`__init__.py` makes this dir a package and imports its siblings with
relative imports (`from .edgar_client import ...`). The `test_*.py`
modules import the same siblings flat (`import edgar_client as ec`),
which only resolves when this directory is on `sys.path` directly.

Script mode (`python tools/edgar_scrubber/test_edgar_client.py`) gets
that for free -- Python puts the script's own directory on `sys.path`.
`python -m pytest` from the repo root does not: with `__init__.py`
present, pytest's default "prepend" import mode walks up to the first
parent *without* an `__init__.py` (here, `tools/`) and inserts that
instead, so flat imports of `edgar_client`/`field_spec`/etc. 404.

Adding this directory to `sys.path` explicitly makes both invocations
resolve the same set of flat imports.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
