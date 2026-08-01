#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

root_dir="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
if [[ "$#" -gt 0 ]]; then
  shift
fi
if [[ ! -d "$root_dir" ]]; then
  echo "release scan: BLOCKED: repository root does not exist" >&2
  exit 1
fi

if ! command -v rg >/dev/null 2>&1; then
  echo "release scan: BLOCKED: rg is required" >&2
  exit 1
fi

cd "$root_dir"

# These patterns detect values, not ordinary field names or the word "Key".
secret_patterns=(
  'sk-[A-Za-z0-9_-]{16,}'
  'sk-ant-[A-Za-z0-9_-]{16,}'
  '-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----'
  "(?i)(key|api[-_]?key|access[-_]?token|token|secret|password|credential|private[-_]?key|client[-_]?secret|authorization|cookie)[[:space:]]*[:=][[:space:]]*[\"'](?!replace-with-|replace_with-|placeholder|example[.]invalid)[A-Za-z0-9._/+={}-]{12,}[\"']"
  '(?i)(key|api[-_]?key|access[-_]?token|token|secret|password|credential|private[-_]?key|client[-_]?secret|authorization|cookie)[[:space:]]*[:=][[:space:]]*(?!replace-with-|replace_with-|placeholder|example[.]invalid)[A-Za-z0-9_+/=-]{12,}([[:space:];,}]|$)'
  'Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}'
)

# Build the runtime marker from pieces so this scanner does not match its own source.
flow_marker="local_"
flow_marker="${flow_marker}flow_id"

scan_paths() {
  local category="$1"
  local mode="$2"
  local include_flow_marker="$3"
  shift 3
  local pattern
  local -a rg_args=(--pcre2 --text -l --hidden --glob '!.git/**' --glob '!node_modules/**')
  if [[ "$mode" == "source" ]]; then
    rg_args+=(
      --no-ignore
      --glob '!release/**'
      --glob '!dist/**'
      --glob '!build/**'
      --glob '!out/**'
      --glob '!artifacts/**'
      --glob '!unpacked/**'
      --glob '!.artifacts/**'
      --glob '!src-tauri/target/**'
    )
  else
    rg_args+=(--no-ignore)
  fi
  local -a patterns=("${secret_patterns[@]}")
  if [[ "$include_flow_marker" == "yes" ]]; then
    patterns+=("$flow_marker")
    patterns+=("full_""log" "complete_""log" "raw_""log" "request_""log" "response_""log")
  fi
  for pattern in "${patterns[@]}"; do
    if rg "${rg_args[@]}" -e "$pattern" "$@" >/dev/null 2>&1; then
      echo "release scan: BLOCKED: sensitive value or forbidden runtime marker in ${category}; contents withheld" >&2
      return 1
    else
      local status=$?
      if [[ "$status" -gt 1 ]]; then
        echo "release scan: BLOCKED: scanner failed while reading ${category}" >&2
        return 1
      fi
    fi
  done
}

# --no-ignore plus --hidden deliberately includes tracked, untracked, ignored,
# README/plan files, deployment templates, and every .env under the repository.
scan_paths "repository source boundary" source no .

artifact_paths=()
for candidate in \
  dist \
  release \
  build \
  out \
  artifacts \
  unpacked \
  .artifacts \
  src-tauri/target/debug/bundle \
  src-tauri/target/release/bundle \
  src-tauri/target/debug/bundle/macos \
  src-tauri/target/release/bundle/macos \
  src-tauri/target/debug/friend-agent-launcher \
  src-tauri/target/release/friend-agent-launcher; do
  if [[ -e "$candidate" ]]; then
    artifact_paths+=("$candidate")
  fi
done

for candidate in "$@"; do
  if [[ ! -e "$candidate" ]]; then
    echo "release scan: BLOCKED: artifact path does not exist" >&2
    exit 1
  fi
  artifact_paths+=("$candidate")
done

if [[ "${#artifact_paths[@]}" -gt 0 ]]; then
  if find "${artifact_paths[@]}" -type f \( -name '*.log' -o -name '*.trace' -o -name '*.ndjson' -o -name '*.jsonl' -o -name '*.log.gz' -o -name '*.log.zip' \) -print -quit | grep -q .; then
    echo "release scan: BLOCKED: complete-log file found in unpacked or artifact paths" >&2
    exit 1
  fi
  scan_paths "unpacked/artifact paths" artifacts yes "${artifact_paths[@]}"
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "release scan: BLOCKED: Git history is unavailable" >&2
  exit 1
fi

commits=$(git rev-list --all) || {
  echo "release scan: BLOCKED: Git history cannot be enumerated" >&2
  exit 1
}
while IFS= read -r commit; do
  [[ -z "$commit" ]] && continue
  for pattern in "${secret_patterns[@]}"; do
    if git grep --text -I -q -P -e "$pattern" "$commit" -- . >/dev/null 2>&1; then
      echo "release scan: BLOCKED: sensitive value or forbidden marker in Git history; contents withheld" >&2
      exit 1
    else
      status=$?
      if [[ "$status" -gt 1 ]]; then
        echo "release scan: BLOCKED: scanner failed while reading Git history" >&2
        exit 1
      fi
    fi
  done
done <<<"$commits"

echo "release scan: PASS: source, Git history, and unpacked/artifact paths"
