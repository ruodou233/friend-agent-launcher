#!/bin/zsh
set -euo pipefail
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$BASE/macOS/Install-And-Start.command"
