"""The test suite never reads a real vault's private site knowledge: careeros/core/private_sites.py reads the synthetic
fixture instead (refactor R4, 2026-10-14)."""
import os
from pathlib import Path

os.environ["CAREEROS_PRIVATE_SITES"] = str(Path(__file__).parent / "fixtures" / "private_sites.yaml")
