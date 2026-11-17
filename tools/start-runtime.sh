#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
environment="$1"
shift
case "$environment" in
    test) runtime_file="$project_dir/test-db/local-runtime.json" ;;
    formal) runtime_file="$project_dir/config/formal-runtime.local.json" ;;
    *) printf '%s\n' '环境必须是 test 或 formal。' >&2; exit 2 ;;
esac
check_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) [[ $# -ge 2 ]] || exit 2; runtime_file="$2"; shift 2 ;;
        --check|--check-db) check_args+=("$1"); shift ;;
        --help|-h)
            printf '%s\n' '用法：bash start-{dev,formal}.sh [--config JSON路径] [--check|--check-db]' \
                '--check 校验配置和依赖；--check-db 额外只读核对数据库。两者都不启停服务或执行迁移。' \
                '正常启动：核对配置→启动或复用数据库→核对连接→应用执行迁移并启动。Ctrl+C 只停止平台。'
            exit 0 ;;
        *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
    esac
done
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) printf '%s\n' '此启动入口用于 Windows Git Bash。' >&2; exit 1 ;;
esac
export RO_AGENT_RUNTIME_CONFIG
RO_AGENT_RUNTIME_CONFIG="$(cygpath -w "$runtime_file")"
metadata="$(powershell.exe -NoLogo -NoProfile -NonInteractive -Command '
    $ErrorActionPreference = "Stop"
    $runtime = Get-Content -LiteralPath $env:RO_AGENT_RUNTIME_CONFIG -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $runtime.python -or -not $runtime.log_directory) { throw "启动配置缺少 python 或 log_directory" }
    [Console]::WriteLine($runtime.python)
    [Console]::Write($runtime.log_directory)
')"
metadata="${metadata//$'\r'/}"
python_path="${metadata%%$'\n'*}"
log_dir="${metadata#*$'\n'}"
if [[ ${#check_args[@]} -eq 0 ]]; then
    log_dir="$(cygpath -u "$log_dir")"
    mkdir -p -- "$log_dir"
    prefix="ro-agent-formal"
    [[ "$environment" != test ]] || prefix="TEST-ro-agent"
    log_file="$log_dir/$prefix-$(date '+%Y%m%d-%H%M%S')-$$.log"
    # tee survives Ctrl+C so the normal shutdown result is also saved.
    exec > >(tee -a -i -- "$log_file") 2>&1
    printf '本次 %s 启动日志：%s\n' "$environment" "$log_file"
fi
exec "$(cygpath -u "$python_path")" -B -u \
    "$(cygpath -w "$project_dir/tools/dev_runtime.py")" \
    --environment "$environment" --config "$RO_AGENT_RUNTIME_CONFIG" "${check_args[@]}"
