"""Copy current worktree and its startup/configuration files; never operate services."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid


class PrepareError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PrepareError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def git(source, *args):
    result = subprocess.run(["git", "-C", str(source), *args], capture_output=True, check=False)
    require(result.returncode == 0, "无法核对 v1 Git 工作目录。")
    return result.stdout.decode("utf-8").strip()


def source_files(source):
    names = git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    files = {}
    for name in sorted(set(filter(None, names))):
        path = source / name
        require(path.resolve().is_relative_to(source), f"源文件越出项目目录：{name}")
        if not path.exists():
            continue  # Include uncommitted deletions as well as uncommitted edits.
        require(path.is_file(), f"发布源不是普通文件：{name}")
        require(not path.is_symlink(), f"发布暂不复制符号链接：{name}")
        if name.split("/", 1)[0] in {".git", ".agents", ".codex", "test-db", ".venv"}:
            continue
        files[name] = path.read_bytes()
    # These two ignored local files contain configuration and credential paths,
    # not credential values. Runtime data/keys/virtual environments are excluded.
    profile_name = "config/formal-runtime.local.json"
    profile = json.loads((source / profile_name).read_text(encoding="utf-8-sig"))
    application = Path(profile["application_config"])
    require(not application.is_absolute(), "正式 application_config 必须相对配置文件，才能随发布副本移动。")
    application = (source / "config" / application).resolve()
    require(application.is_relative_to(source / "config"), "正式业务配置必须在项目 config 目录内。")
    dev = json.loads((source / "test-db/local-runtime.json").read_text(encoding="utf-8-sig"))
    dev_application = Path(dev["application_config"])
    if not dev_application.is_absolute():
        dev_application = source / "test-db" / dev_application
    require(dev_application.resolve() == application, "测试与正式引用了不同业务 JSON；请先对齐配置引用。")
    for path in (source / profile_name, application):
        files[path.relative_to(source).as_posix()] = path.read_bytes()
    for name in ("start-dev.sh", "start-formal.sh", "tools/start-runtime.sh", "tools/dev_runtime.py",
                 "pyproject.toml", "config/dev-runtime.example.json", "config/formal-runtime.example.json"):
        require(name in files, f"缺少发布所需文件：{name}")
    for name, content in files.items():
        if name.endswith(".py"):
            try:
                compile(content, name, "exec")
            except SyntaxError:
                raise PrepareError(f"Python 语法检查失败：{name}") from None
    return files


def verify_manifest(directory):
    manifest = json.loads((directory / "source-manifest.json").read_text(encoding="utf-8-sig"))
    require(isinstance(manifest, dict) and manifest, "原发布文件清单无效。")
    for name, expected in manifest.items():
        path = (directory / name).resolve()
        require(path.is_relative_to(directory) and path.is_file(), "原发布清单引用了越界或缺失文件。")
        require(digest(path.read_bytes()) == expected, f"原发布文件已改变：{name}")


def check_migrations(previous, files):
    old = {p.name: p.read_bytes() for p in (previous / "db/migrations").glob("*.sql")}
    require(bool(old), "原发布目录没有迁移文件。")
    current = {name.removeprefix("db/migrations/"): value for name, value in files.items()
               if name.startswith("db/migrations/") and name.endswith(".sql")}
    for name, content in old.items():
        require(current.get(name) == content, f"已发布迁移被删除或改写：{name}")
    require(all(re.fullmatch(r"\d{4}_[^/]+\.sql", name) for name in current), "迁移文件名格式无效。")
    versions = [int(name[:4]) for name in current]
    require(len(versions) == len(set(versions)), "数据库迁移编号重复。")
    added = sorted(set(current) - set(old))
    require(all(int(name[:4]) > max(int(n[:4]) for n in old) for name in added), "新增迁移编号必须递增。")
    return added


def validate_launch(directory):
    result = subprocess.run([sys.executable, "-B", str(directory / "tools/dev_runtime.py"),
                             "--environment", "formal", "--config",
                             str(directory / "config/formal-runtime.local.json"), "--check"],
                            cwd=directory, capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    # The shared launcher prints only paths/field names; never dump credentials.
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="", file=sys.stderr)
        raise PrepareError("正式启动配置或现有依赖检查未通过。")


def fixed_launcher_content(destination):
    """Bake the selected release into the fixed entry during preparation."""
    target = shlex.quote((destination / "start-formal.sh").as_posix())
    return ("#!/usr/bin/env bash\n"
            "# Generated by prepare-formal-release.sh; target is fixed at preparation time.\n"
            "set -euo pipefail\n"
            f'exec bash {target} "$@"\n').encode("utf-8")


def validate_fixed_launcher(path):
    bash = shutil.which("bash")
    require(bash is not None, "找不到 Git Bash，无法检查固定启动入口。")
    for arguments in (["-n", str(path)], [str(path), "--check"]):
        result = subprocess.run([bash, *arguments], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=60)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, end="", file=sys.stderr)
            raise PrepareError("固定启动入口检查未通过；尚未切换入口。")


def prepare(args):
    source = args.source.resolve()
    releases = args.releases.resolve()
    require(source.is_dir() and releases.is_dir(), "源码或发布父目录不存在。")
    require(releases == Path("D:/ro-agent-data/releases").resolve(), "发布目标不是已约定的目录。")
    pointer = releases / "current-formal.txt"
    launcher = releases / "start-formal.sh"
    pointer_before = pointer.read_bytes() if pointer.exists() else None
    launcher_before = launcher.read_bytes() if launcher.exists() else None
    if args.previous_release:
        previous = args.previous_release.resolve()
        if pointer_before:
            require(previous == Path(pointer_before.decode("utf-8").strip()).resolve(), "指定基线与当前发布记录不一致。")
    else:
        require(pointer_before is not None, "首次准备需用 --previous-release 指定现有发布目录。")
        previous = Path(pointer_before.decode("utf-8").strip()).resolve()
    require(previous.parent == releases and previous.name.startswith("ro-agent-v1-"), "原发布目录越界。")
    branch = git(source, "branch", "--show-current")
    commit = git(source, "rev-parse", "HEAD")
    require(branch == "feat/robot-agent-v1", "源码不是约定的 feat/robot-agent-v1 分支。")
    print("[1/4] 读取当前 v1 工作目录，包含未提交改动。")
    verify_manifest(previous)
    files = source_files(source)
    added = check_migrations(previous, files)
    print(f"源码：{source}；分支：{branch}；基准提交：{commit[:12]}")
    print(f"复制 {len(files)} 个项目文件，以及其中的 SH、共用启动程序和配置模板。")
    print("[2/4] 使用同版本启动程序检查正式配置、凭据引用和现有 Python 依赖。")
    validate_launch(source)
    print("新增迁移（不在准备阶段执行）：" + ("、".join(added) or "无"))
    print(f"新副本校验通过后，将具体版本路径写入固定入口：{launcher}")
    if args.check:
        print("PREPARE CHECK PASS：未写文件、未启停服务、未执行迁移。")
        return
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = releases / f"ro-agent-v1-{tag}-{commit[:8]}-{uuid.uuid4().hex[:6]}"
    temporary_pointer = releases / f".current-formal-{uuid.uuid4().hex}.tmp"
    temporary_launcher = releases / f".start-formal-{uuid.uuid4().hex}.tmp"
    launcher_bytes = fixed_launcher_content(destination)
    changed_pointer = False
    created = False
    try:
        require(source_files(source) == files and git(source, "rev-parse", "HEAD") == commit
                and git(source, "branch", "--show-current") == branch, "准备期间工作区发生变化，请重试。")
        destination.mkdir()
        created = True
        print("[3/4] 复制完整项目运行文件；逐文件核对内容。")
        for name, content in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        manifest = {name: digest(content) for name, content in sorted(files.items())}
        manifest_bytes = encode(manifest)
        (destination / "source-manifest.json").write_bytes(manifest_bytes)
        verify_manifest(destination)
        validate_launch(destination)
        temporary_launcher.write_bytes(launcher_bytes)
        validate_fixed_launcher(temporary_launcher)
        # Config and source may be edited in another terminal during copying.
        require(source_files(source) == files and git(source, "rev-parse", "HEAD") == commit
                and git(source, "branch", "--show-current") == branch, "复制期间源码或配置变化，请重试。")
        require((pointer.read_bytes() if pointer.exists() else None) == pointer_before,
                "发布记录已被另一操作更新，请重试。")
        require((launcher.read_bytes() if launcher.exists() else None) == launcher_before,
                "固定启动入口已被另一操作修改，请重试。")
        receipt = {"release": str(destination), "source": str(source), "source_branch": branch,
                   "source_commit": commit, "source_kind": "working_tree",
                   "includes_uncommitted_changes": bool(git(source, "status", "--porcelain")),
                   "source_manifest_sha256": digest(manifest_bytes), "source_files": len(files),
                   "previous_release": str(previous), "new_migration_files": added,
                   "fixed_launcher": str(launcher), "fixed_launcher_sha256": digest(launcher_bytes),
                   "application_started": False, "database_migrations_run": False,
                   "prepared_at": datetime.now().astimezone().isoformat()}
        (destination / "release-receipt.json").write_bytes(encode(receipt))
        temporary_pointer.write_text(str(destination) + "\n", encoding="utf-8")
        os.replace(temporary_pointer, pointer)
        changed_pointer = True
        try:
            os.replace(temporary_launcher, launcher)
        except OSError:
            # Keep the old entry and restore its release record if installation
            # fails. If restoration itself fails, keep the new release intact.
            if pointer_before is None:
                pointer.unlink()
            else:
                temporary_pointer.write_bytes(pointer_before)
                os.replace(temporary_pointer, pointer)
            changed_pointer = False
            raise PrepareError("固定启动入口更新失败；已恢复原发布记录，旧入口保持不变。") from None
    finally:
        temporary_pointer.unlink(missing_ok=True)
        temporary_launcher.unlink(missing_ok=True)
        if created and not changed_pointer:
            require(destination.resolve().parent == releases and destination.name.startswith("ro-agent-v1-"),
                    "失败清理目标越界。")
            shutil.rmtree(destination)
    print("[4/4] PREPARE PASS：发布文件就绪；平台和数据库均未启停，迁移尚未执行。")
    print(f"新版本：{destination}")
    print(f"固定启动入口已写入本次版本路径：{launcher}")
    print("停止旧平台后，在 Git Bash 执行：")
    print(f"bash {shlex.quote(launcher.as_posix())}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--releases", required=True, type=Path)
    parser.add_argument("--previous-release", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        prepare(args)
    except PrepareError as exc:
        print(f"准备停止：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"准备停止：{type(exc).__name__}；请检查文件路径、权限或配置格式。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
