#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

command -v python3 >/dev/null 2>&1 || {
  printf '%s\n' 'payment-control-plane: python3 is required' >&2
  exit 1
}

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import ast
from pathlib import Path

for filename in ("control_plane.py", "test_control_plane.py"):
    ast.parse(Path(filename).read_text(encoding="utf-8"), filename=filename)
print("payment-control-plane Python syntax validation: ok")
PY
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRIPT_DIR" python3 -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py' -v
python3 - <<'PY'
import sqlite3
from pathlib import Path

schema = Path("schema.sql").read_text(encoding="utf-8")
connection = sqlite3.connect(":memory:")
connection.execute("PRAGMA foreign_keys = ON")
connection.executescript(schema)
columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(manual_recharges)")}
assert columns["request_id"] == 1
assert columns["business_ref"] == 1
assert not connection.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('balances', 'account_balances')"
).fetchone()
print("payment-control-plane schema validation: ok")
PY
