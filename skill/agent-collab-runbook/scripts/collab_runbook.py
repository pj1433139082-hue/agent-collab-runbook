"""Locate a framework checkout for a collaboration project and forward CLI arguments.

Standard library only, Python >= 3.11. Forwards to ``python -m agent_workflow``
with ``shell=False`` so arguments are never re-parsed by a shell. Produces no
state of its own: it never writes project files, indexes, or task facts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import NoReturn


class LocateError(ValueError):
    pass


class ArgumentsError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ArgumentsError("Invalid helper arguments; use --help.")


def read_location(path: Path) -> dict:
    if path.stat().st_size > 100_000:
        raise LocateError("Location metadata exceeds the size limit.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise LocateError("Location metadata is invalid.")
    return data


def verified(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not all((root / relative).is_file() for relative in (
        "AGENTS.md", "agent_workflow/cli.py", "agent_workflow/project_start.py",
        "agent_workflow/packs/project-planning/workflow.json",
    )):
        raise LocateError("No usable local agent-workflow checkout at this location; pass --framework once.")
    return root


def locate(explicit: str | None, marker: Path) -> Path:
    if explicit:
        return verified(explicit)
    configured = os.environ.get("AGENT_WORKFLOW_FRAMEWORK")
    if configured:
        return verified(configured)
    if marker.is_file():
        root = read_location(marker).get("framework_root")
        if not isinstance(root, str):
            raise LocateError("Location metadata has no framework_root.")
        return verified(root)
    parents = Path(__file__).resolve().parents
    if len(parents) > 4 and (parents[4] / "agent_workflow/project_start.py").is_file():
        return verified(parents[4])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    registration = base / "agent-workflow/framework.json"
    if registration.is_file():
        root = read_location(registration).get("framework_root")
        if isinstance(root, str):
            return verified(root)
    raise LocateError("Framework not located; ask once for its checkout path and pass --framework.")


def read_task_id(marker: Path) -> str | None:
    if not marker.is_file():
        return None
    value = read_location(marker).get("task_id")
    return value if isinstance(value, str) and value else None


def fail(code: str, message: str) -> int:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}))
    return 2


# --------------------------------------------------------------------------- #
# sync-check：技能源 ↔ 各安装副本的逐文件比对（Q9）
#
# 动机（实测）：技能源 `acceptance-gates.md` 已改到 17,652 B，而全局安装副本仍是
# 16,247 B——安装副本会悄悄落后。源是唯一事实源，本命令只**报告**漂移，不自动改。
#
# 目标清单从 **skillshare 自己的配置**读（`%APPDATA%\skillshare\config.yaml`），
# 而不是内置一份手写常量：配置是分发的权威，内置清单必然漏（原来只扫 3 个根，
# 漏掉 hermes / mimo / qwen / workbuddy）。读不到配置时回落到内置根。
#
# 修于 2026-10-15（B7 盘出的缺陷）：**`--skill` 给的是全路径时**，
# `skill_root / name` 会因 `Path` 语义吞掉前缀、退化成路径本身 ⇒ 三个发现目标
# 全等于源自己、逐一自比、恒报 `in-sync`。现在 `--skill` 既接受技能名，也接受
# 技能目录的全路径（后者自动推出 `skill_root` 与 `name`）。
#
# 另注：**不拿“目标目录不存在”当缺陷**——include 白名单（如 codex）本就不收新技能；
# 这类目标报 `skipped` 且不计入返码，只报「期望存在却缺席」的。
# --------------------------------------------------------------------------- #
DEFAULT_SKILL_ROOT = Path(r"<技能源>\skill")
DEFAULT_SKILL_NAME = "agent-collab-runbook"

# 读不到 skillshare 配置时的回落清单（保持向后兼容）。
INSTALL_ROOTS = (
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
    Path.home() / ".codex" / "skills",
)


def skillshare_config_path() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "skillshare" / "config.yaml"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "skillshare" / "config.yaml"


def config_install_roots(config: Path) -> list[Path]:
    """按 skillshare 配置推出各 CLI 的 skills 父目录。"""
    try:
        if not config.is_file() or config.stat().st_size > 2_000_000:
            return []
        lines = config.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    target_paths: dict[str, str] = {}
    current: str | None = None
    stack: list[tuple[int, str]] = []
    in_targets = False
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if stripped == "targets:":
            in_targets = True
            continue
        if not in_targets:
            continue
        if indent == 0:
            in_targets = False
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = stripped.split(":", 1)[0].strip()
        value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        if not value:
            stack.append((indent, key))
            if indent == 2:
                current = key
                target_paths.setdefault(current, "")
            continue
        # 只认 <target> → skills → path 这一条链，别的 path（如 extras/agents）一概不取。
        if key == "path" and current is not None and [scope for _, scope in stack] == [current, "skills"]:
            target_paths[current] = value.strip().strip("'\"")
    roots = [Path(path) for path in target_paths.values() if path]
    return roots


def skill_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def discover_targets(name: str, roots: list[Path]) -> list[Path]:
    """只返回「该目标确实带了本技能」的目录。

    目标**整个目录都不存在**时也一律不报：skillshare 的 include/exclude 白名单本来
    就可能不给某个 CLI 分发本技能（实测 `~/.codex/skills` 连目录都没建）。报出来只会
    是噪声 —— 拿「没分发」当缺陷，正是本命令以前恒报一致的反面。
    """
    found: list[Path] = []
    for base in roots:
        candidate = base / name
        if (candidate / "SKILL.md").is_file():
            found.append(candidate)
    return found


def resolve_source(skill_root: Path, name: str) -> tuple[Path, str]:
    """`--skill` 可以是技能名，也可以是技能目录全路径。"""
    raw = Path(name).expanduser()
    if raw.is_dir():
        source = raw.resolve()
        return (source.parent, source.name)
    return (skill_root, name)


def sync_check(skill_root: Path, name: str, targets: list[Path], config: Path) -> int:
    if config.is_file():
        roots = config_install_roots(config)
        origin = str(config)
    else:
        roots, origin = list(INSTALL_ROOTS), "built-in fallback (skillshare config not found)"
    source = skill_root / name
    if not (source / "SKILL.md").is_file():
        return fail("sync_skill_missing", f"No skill source at {source}.")
    wanted = skill_files(source)
    report: list[dict] = []
    drift = 0
    for target in targets:
        entry = {"target": str(target)}
        if not target.is_dir():
            entry["state"] = "absent"
            drift += 1
        elif not (target / "SKILL.md").is_file():
            # 目录在、技能不在：分发白名单没把本技能给这个 CLI。不是缺陷，不计入返码。
            entry["state"] = "skipped"
            entry["reason"] = "Skill was not distributed to this target (include/exclude list); not a defect."
        else:
            actual = skill_files(target)
            differs = sorted(k for k in wanted if k in actual and actual[k] != wanted[k])
            missing = sorted(k for k in wanted if k not in actual)
            extra = sorted(k for k in actual if k not in wanted)
            stale = sorted(k for k, digest in wanted.items() if k in actual and actual[k] != digest)
            if differs or missing or extra:
                drift += 1
            entry.update({
                "state": "in-sync" if not (differs or missing or extra) else "drifted",
                "files": len(actual),
                "differs": differs,
                "missing": missing,
                "extra": extra,
                "stale_hint": stale,
            })
        report.append(entry)
    checked = [item for item in report if item["state"] in ("in-sync", "drifted")]
    print(json.dumps({
        "ok": drift == 0,
        "skill": name,
        "source": str(source),
        "files": len(wanted),
        "target_origin": origin,
        "targets": report,
        "checked": len(checked),
        "skipped": len(report) - len(checked),
        "note": (
            "源是唯一事实源；发现 drifted 就 `install --force` + `sync`（本命令不自动改）。"
            if drift else
            f"{len(checked)} 个落点与源逐文件一致。" + (f" 另有 {len(report) - len(checked)} 个未分发（白名单，不是缺陷）。" if len(report) - len(checked) else "")
        ),
    }, ensure_ascii=False, indent=2))
    return 0 if drift == 0 else 3


def main() -> int:
    parser = Parser()
    parser.add_argument("--framework", help="Framework checkout path; overrides auto-detection.")
    parser.add_argument("--project", help="Collaboration project workspace; defaults to the current directory.")
    parser.add_argument("--locate", action="store_true", help="Print the located framework and project, then exit.")
    parser.add_argument("--sync-check", action="store_true", help="Compare the skill source against its installed copies, then exit.")
    parser.add_argument("--skill", default=None, help="Skill name, or the skill directory itself, for --sync-check.")
    parser.add_argument("--skill-root", type=Path, default=None, help="Skill source root for --sync-check (default: the project's skill/ directory).")
    parser.add_argument("--target", action="append", default=[], help="Installed skill copy for --sync-check (repeatable).")
    parser.add_argument("--config", type=Path, default=None, help="skillshare config.yaml used to find install roots for --sync-check.")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Public CLI arguments to forward after --.")
    try:
        options = parser.parse_args()
        if sys.version_info < (3, 11):
            return fail("python_version", "This helper requires Python >=3.11; business runtimes stay independent. Nothing was written.")
        if options.sync_check:
            skill_root, name = resolve_source(options.skill_root or DEFAULT_SKILL_ROOT, options.skill or DEFAULT_SKILL_NAME)
            config = options.config or skillshare_config_path()
            if options.target:
                targets = [Path(item).expanduser() for item in options.target]
            else:
                roots = config_install_roots(config) if config.is_file() else list(INSTALL_ROOTS)
                targets = discover_targets(name, roots)
            return sync_check(skill_root, name, targets, config)
        project = Path(options.project).expanduser().resolve() if options.project else Path.cwd()
        marker = project / ".agent-workflow.local.json"
        root = locate(options.framework, marker)
        command = options.command
        if command[:1] == ["--"]:
            command = command[1:]
        if options.locate or not command:
            print(json.dumps({
                "ok": True,
                "framework_root": str(root),
                "project": str(project),
                "marker": marker.is_file(),
                "declared_task_id": read_task_id(marker),
            }, ensure_ascii=True))
            return 0
    except ArgumentsError:
        return fail("helper_arguments_error", "Invalid helper arguments; use --help. Nothing was written.")
    except (LocateError, OSError, ValueError, TypeError):
        # Never echo caller arguments or metadata: they may hold credentials or private paths.
        return fail("framework_location_error", "Local framework unavailable; check its location or metadata and pass --framework. Nothing was written.")
    try:
        return subprocess.run([sys.executable, "-m", "agent_workflow", *command], cwd=root, check=False).returncode
    except OSError:
        return fail("framework_invocation_error", "Could not run the located framework command. Inspect task status before retrying an operation.")


if __name__ == "__main__":
    raise SystemExit(main())
