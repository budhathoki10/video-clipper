"""Tests use their own data folder so they never touch your real clips."""
import os
from pathlib import Path

TEST_DATA = Path(__file__).resolve().parents[2] / "data" / "test-run"
os.environ["CLIPPER_DATA_DIR"] = str(TEST_DATA)
os.environ["CLIPPER_NO_WORKER"] = "1"
os.environ["CLIPPER_AUTH_DISABLED"] = "1"
