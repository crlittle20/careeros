"""Rebuild samples/analytics_demo/: the analytics export of a fresh four-day demo. Synthetic data only.

    .venv\\Scripts\\python.exe tools\\build_demo_export.py

The committed sample lets anyone build the Power BI report (docs/powerbi_dashboard_spec.md) without running CareerOS.
It is produced exactly as a user would produce it: `careeros demo tour`, then `careeros export` with Demo Mode on, in
a throwaway demo vault. tests/test_analytics.py fails when the sample no longer matches the code; run this to refresh it.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAMPLE = ROOT / "samples" / "analytics_demo"


def demo_export(scratch: Path) -> Path:
    """Run the demo tour and the export in a demo vault under scratch; returns the export folder."""
    from careeros import cli
    from careeros.demo import sandbox
    with mock.patch.dict(os.environ, {sandbox.VAULT_ENV: str(scratch / "demo-vault"), sandbox.ENV: ""}), redirect_stdout(io.StringIO()):
        cli.main(["demo", "tour"])
        cli.main(["export"])
    for handler in list(cli.log.handlers):   # cli.main logs into the demo vault; let the folder go
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(scratch):
            handler.close()
            cli.log.removeHandler(handler)
    return scratch / "demo-vault" / "exports" / "analytics"


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="careeros-demo-export-"))
    try:
        out = demo_export(scratch)
        SAMPLE.mkdir(parents=True, exist_ok=True)
        for old in SAMPLE.iterdir():   # the folder only ever holds an export; replace its files, keep the folder
            old.unlink()
        for new in out.iterdir():
            shutil.copy2(new, SAMPLE / new.name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"samples/analytics_demo rebuilt: {', '.join(sorted(p.name for p in SAMPLE.iterdir()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
