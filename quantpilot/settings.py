import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("QUANTPILOT_HOME", str(ROOT))).resolve()
ARTIFACTS = HOME / "artifacts"
DB = HOME / "data" / "quantpilot.sqlite3"
