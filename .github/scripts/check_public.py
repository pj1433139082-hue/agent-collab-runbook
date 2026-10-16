#!/usr/bin/env python3
"""公开副本的结构核查与通用脱敏扫描（CI 与本地共用）。

用法：
    python .github/scripts/check_public.py [--root .]

退出码：0 = 通过；1 = 有失败项；2 = 用法/环境问题。

这里**只放通用规则**——具体的私有面名称（客户名、厂名、模型 id、机器路径）留在
私有导出器的扫描清单里，那份清单不进公开仓，否则扫描器本身就泄露了它要藏的东西。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REQUIRED_AT_REPO_ROOT = (
    "README.md",
    "LICENSE",
    "CONTEXT.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
)

REQUIRED_FILES = (
    "SKILL.md",
    "references/handoff-protocol.md",
    "references/receipt-contract.md",
    "references/receipt.schema.json",
    "references/acceptance-gates.md",
    "references/session-scheduling.md",
    "references/framework-cli.md",
    "scripts/board.py",
    "scripts/collab_console.py",
    "scripts/collab_runbook.py",
    "scripts/verify_receipt.py",
)

# 通用脱敏规则：不点名任何私有对象，只抓"形状"。
# 注意：公开文档里 `C:\Users\<用户>\...`、裸盘符 `E:\` 都是正常写法，不算泄漏；
# 只抓"泄漏签名"——真实用户目录名与私有根目录。
PATTERNS = (
    ("私有盘符路径", re.compile(
        r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+(?:Users[\\/]+(?!example\b|<)[A-Za-z0-9._-]+"
        r"|beikong-code|obsidian_work|cangku|repos(?![A-Za-z0-9]))", re.IGNORECASE)),
    ("Unix 家目录", re.compile(r"/(?:home|Users)/(?!example|runner|<)[A-Za-z0-9._-]+")),
    # 必须整条 IPv4（四段），否则 "10.0" 这类普通小数会被误判（实测 collab_console.py 里的 10.0）
    ("内网地址", re.compile(r"(?<![\d.])(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){3}(?![\d.])")),
    ("真实会话 id", re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}")),
    ("凭据形状", re.compile(r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("邮箱（只允许 example.com / noreply）", re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.com|example\.org|users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)

ALLOW_PATH_MARKERS = ("C:\\Users\\example", "D:\\example", "C:/Users/example", "D:/example")
SKIP_DIRS = {".git", "__pycache__", ".github/workflows"}
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".txt", ".ps1", ".mjs", ".js", ".cfg", ".toml"}


def tracked_text_files(root: Path) -> list[Path]:
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore", ".gitattributes"}:
            out.append(path)
    return out


def check_required(root: Path, repository: Path, failures: list[str]) -> None:
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            failures.append(f"缺少必需文件：{rel}")
    looks_like_public_layout = (repository / "README.md").is_file() and (repository / "LICENSE").is_file()
    if looks_like_public_layout:
        for rel in REQUIRED_AT_REPO_ROOT:
            if not (repository / rel).is_file():
                failures.append(f"缺少必需文件（仓库根）：{rel}")
        adr_dir = repository / "docs" / "adr"
    else:
        adr_dir = root / "docs" / "adr"
    adr = sorted(adr_dir.glob("*.md")) if adr_dir.is_dir() else []
    if len(adr) < 4:
        failures.append(f"docs/adr/ 下的 ADR 少于 4 篇（现有 {len(adr)}）")


def check_syntax(root: Path, failures: list[str]) -> None:
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            failures.append(f"语法错误：{path.relative_to(root)}:{error.lineno} {error.msg}")


def check_hygiene(root: Path, failures: list[str]) -> None:
    for path in tracked_text_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            failures.append(f"不是 UTF-8：{rel}")
            continue
        for number, line in enumerate(lines, start=1):
            if any(marker in line for marker in ALLOW_PATH_MARKERS):
                continue
            for label, pattern in PATTERNS:
                for hit in pattern.finditer(line):
                    failures.append(f"{rel}:{number} [{label}] {hit.group(0)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开副本结构核查与通用脱敏扫描")
    parser.add_argument(
        "--root",
        default=None,
        help="技能根（公开布局是 skill/agent-collab-runbook；默认自动探测，找不到就用当前目录）",
    )
    args = parser.parse_args(argv)

    if args.root is None:
        nested = Path("skill/agent-collab-runbook")
        root = (nested if nested.is_dir() else Path(".")).resolve()
    else:
        root = Path(args.root).resolve()
    if not (root / "SKILL.md").is_file():
        print(f"错误：{root} 看起来不是技能根（没有 SKILL.md）", file=sys.stderr)
        return 2
    repository = root.parent.parent if root.parent.name == "skill" else root

    failures: list[str] = []
    check_required(root, repository, failures)
    check_syntax(root, failures)
    check_hygiene(root, failures)

    scanned = len(tracked_text_files(root))
    if failures:
        print(f"结构核查失败（扫描 {scanned} 个文本文件，{len(failures)} 项）：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"结构核查通过：{scanned} 个文本文件，必需文件齐备，无语法错误，通用脱敏扫描 0 命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
