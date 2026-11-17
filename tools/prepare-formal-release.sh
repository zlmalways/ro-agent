#!/usr/bin/env bash
# Installed in D:/ro-agent-data/releases; implementation is maintained with v1.
set -euo pipefail
source_dir="${RO_AGENT_SOURCE_DIR:-/d/codex_chat/2026-10-25/new-chat/ro-agent-mac-recovery}"
releases_dir="${RO_AGENT_RELEASES_DIR:-/d/ro-agent-data/releases}"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    printf '%s\n' '用法：bash prepare-formal-release.sh [--check]' \
        '复制 v1 当前工作目录（含未提交改动）、启动程序和配置，准备正式发布副本。' \
        '--check 只检查，不写文件；服务启停、数据库迁移仍由启动 SH 完成。'
    exit 0
fi
export RO_AGENT_PREPARE_PROFILE
RO_AGENT_PREPARE_PROFILE="$(cygpath -w "$source_dir/config/formal-runtime.local.json")"
python_path="$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command '
    $ErrorActionPreference = "Stop"
    $profile = Get-Content -LiteralPath $env:RO_AGENT_PREPARE_PROFILE -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $profile.python) { throw "正式环境配置缺少 python" }
    [Console]::Write($profile.python)
')"
python_path="${python_path//$'\r'/}"
exec "$(cygpath -u "$python_path")" -B -u \
    "$(cygpath -w "$source_dir/tools/prepare_formal_release.py")" \
    --source "$(cygpath -w "$source_dir")" --releases "$(cygpath -w "$releases_dir")" "$@"
