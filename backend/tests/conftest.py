"""Tests use their own data folder and database so they never touch your real clips or accounts."""
import os
from pathlib import Path

TEST_DATA = Path(__file__).resolve().parents[2] / "data" / "test-run"
os.environ["CLIPPER_DATA_DIR"] = str(TEST_DATA)
os.environ["CLIPPER_NO_WORKER"] = "1"
os.environ["CLIPPER_AUTH_DISABLED"] = "1"
os.environ["MONGODB_DB"] = "clipper-test"
if os.environ.get("CLIPPER_TEST_MONGODB_URI"):  # optional: run the same tests against a real MongoDB
    os.environ["MONGODB_URI"] = os.environ["CLIPPER_TEST_MONGODB_URI"]
else:  # default: an in-memory database, no server and no network
    os.environ["CLIPPER_MONGO_MOCK"] = "1"
    os.environ.pop("MONGODB_URI", None)
