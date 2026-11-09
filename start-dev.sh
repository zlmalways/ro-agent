#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
runtime_file="$project_dir/test-db/local-runtime.json"
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    printf '%s\n' 'Usage: bash start-dev.sh [--config path/to/local-runtime.json]'
    exit 0
fi
if [[ $# -gt 0 ]]; then
    if [[ $# -ne 2 || "$1" != "--config" ]]; then
        printf '%s\n' 'Usage: bash start-dev.sh [--config path/to/local-runtime.json]' >&2
        exit 2
    fi
    runtime_file="$2"
fi
if [[ ! -f "$runtime_file" ]]; then
    printf 'Missing local runtime configuration: %s\n' "$runtime_file" >&2
    exit 1
fi

# Windows Git Bash has no Python on PATH on this machine. Read only the
# configured executable with PowerShell; all startup decisions stay in Python.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        export RO_AGENT_DEV_RUNTIME_CONFIG
        RO_AGENT_DEV_RUNTIME_CONFIG="$(cygpath -w "$runtime_file")"
        python_path="$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command '
            $ErrorActionPreference = "Stop"
            $runtime = Get-Content -LiteralPath $env:RO_AGENT_DEV_RUNTIME_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not $runtime.python) { throw "Missing python in local runtime configuration" }
            [Console]::Write($runtime.python)
        ')"
        python_path="${python_path//$'\r'/}"
        exec "$(cygpath -u "$python_path")" -B -u \
            "$(cygpath -w "$project_dir/tools/dev_runtime.py")" \
            --config "$RO_AGENT_DEV_RUNTIME_CONFIG"
        ;;
    *)
        printf '%s\n' 'This launcher currently supports Windows Git Bash.' >&2
        exit 1
        ;;
esac
