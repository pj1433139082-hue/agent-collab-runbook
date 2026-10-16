#!/usr/bin/env python3
"""board.py — 交接区看板的卡数据源与渲染器（决策 R13）。

问题：`_handoff/BOARD.md` 是手写单文件，结构事故无法机器校验（2026-10-15 真实发生过
「W4 的证据插进 B3 卡」「`## Review` 与 `## Done` 各出现两次」）。

做法：
  - 卡的正文（`### <ID> …` 到下一个标题之间的原文）逐字存进 `_handoff/cards/<ID>.yaml`；
  - 其余骨架（表头、纪律、常驻 worker、镜像纪律、卡模板、各列标题、尾部对应表）存进
    `_handoff/board.skeleton.md`，卡的位置留 `<<<CARD <ID>>>>` 占位；
  - `render` 把骨架 + 卡重新拼回 `BOARD.md`；`check` 断言拼回的字节与盘上**逐字节相同**。

因此迁移是无损的（不靠人读一遍 75 KB），BOARD.md 从此是**产物**，改卡请用本工具或直接改
`cards/<ID>.yaml`。

`cards/*.yaml` 的内容是 **JSON 语法**（YAML 是 JSON 的超集）——故意这样写，好处是标准库
`json` 就能精确往返，外部用 YAML 也能读。

用法：
    python board.py [--handoff DIR] list|show <ID>
    python board.py [--handoff DIR] migrate [--force]
    python board.py [--handoff DIR] render [--check] [--out PATH]
    python board.py [--handoff DIR] move <ID> <列名> [--note TEXT]
    python board.py [--handoff DIR] add <ID> <标题> --column <列名> [--owner W] [--depends A1,B2]
    python board.py [--handoff DIR] set <ID> owner=W column=Review depends_on=A1,A2
    python board.py [--handoff DIR] selftest

退出码：0 正常 / 3 校验失败（漂移、ID 冲突、列名非法）/ 4 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CARD_HEAD = re.compile(r"^###[ \t]+([A-Za-z]{1,3}[0-9]{1,3})(?=[ \t]|$)")
SKELETON_NAME = "board.skeleton.md"
CARDS_DIRNAME = "cards"
CARD_PLACEHOLDER = "<<<CARD {cid}>>>"
PLACEHOLDER_RE = re.compile(r"^<<<CARD ([A-Za-z]{1,3}[0-9]{1,3})>>>\r?\n", re.MULTILINE)
COLUMNS = ("Backlog", "Todo", "Doing", "Review", "Done", "等业主")


def now_stamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def resolve_handoff(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("COLLAB_HANDOFF")
    if env:
        return Path(env).resolve()
    return Path(r"_handoff")


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def split_cards(text: str) -> list[tuple[str, str]]:
    """把 BOARD.md 切成 [(kind, payload)]：kind ∈ {text, card}。

    card 的 payload 是从 `### <ID>` 那一行起、到下一个标题（`##`/`###`）之前为止的**原文**，
    含其后的空行。
    """
    lines = text.splitlines(keepends=True)
    blocks: list[tuple[str, str]] = []
    buffer: list[str] = []
    i = 0
    while i < len(lines):
        match = CARD_HEAD.match(lines[i])
        if match is None:
            buffer.append(lines[i])
            i += 1
            continue
        cid = match.group(1)
        j = i
        while j < len(lines):
            if j > i and (lines[j].startswith("## ") or lines[j].startswith("### ")):
                break
            j += 1
        if buffer:
            blocks.append(("text", "".join(buffer)))
            buffer = []
        blocks.append(("card", "".join(lines[i:j])))
        i = j
    if buffer:
        blocks.append(("text", "".join(buffer)))
    return blocks


def skeleton_of(blocks: list[tuple[str, str]]) -> tuple[str, dict[str, str]]:
    cards: dict[str, str] = {}
    out: list[str] = []
    seen: set[str] = set()
    for kind, payload in blocks:
        if kind == "text":
            out.append(payload)
            continue
        cid = CARD_HEAD.match(payload.splitlines()[0]).group(1)
        if cid in seen:
            raise SystemExit(f"退出码 3：卡 ID 重复：{cid}")
        seen.add(cid)
        cards[cid] = payload
        out.append(CARD_PLACEHOLDER.format(cid=cid) + "\n")
    return "".join(out), cards


def column_of(skeleton: str, cid: str) -> str:
    """按占位符所在的最近一个 `## ` 标题判定列名。"""
    current = ""
    for line in skeleton.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            current = current.split("（")[0].split("(")[0].strip()
        elif line.strip() == CARD_PLACEHOLDER.format(cid=cid):
            return current
    return ""


def card_payload(cid: str, column: str, owner: str, body: str, *, depends=None, scope=None) -> dict:
    return {
        "id": cid,
        "column": column,
        "owner": owner,
        "depends_on": list(depends or []),
        "scope": list(scope or []),
        "updated": now_stamp(),
        "body": body,
    }


OWNER_RE = re.compile(r"^[-*]\s*负责人[：:]\s*([^\s（(]+)", re.MULTILINE)
DEPENDS_RE = re.compile(r"^[-*]\s*依赖[：:]\s*(.+)$", re.MULTILINE)


def owner_of(body: str) -> str:
    match = OWNER_RE.search(body)
    return match.group(1) if match else ""


def depends_of(body: str) -> list[str]:
    match = DEPENDS_RE.search(body)
    if not match:
        return []
    raw = match.group(1).strip()
    if raw in ("无", "none", "-", ""):
        return []
    return [token for token in re.split(r"[、,，/ ]+", raw) if re.fullmatch(r"[A-Za-z]{1,3}[0-9]{1,3}", token)]


def load_card(path: Path) -> dict:
    data = json.loads(read_text(path))
    for key in ("id", "column", "body"):
        if key not in data:
            raise SystemExit(f"退出码 3：卡文件缺字段 {key}：{path}")
    return data


def cards_dir(handoff: Path) -> Path:
    return handoff / CARDS_DIRNAME


def load_cards(handoff: Path) -> dict[str, dict]:
    directory = cards_dir(handoff)
    if not directory.is_dir():
        raise SystemExit(f"退出码 3：找不到卡目录 {directory}（先跑 migrate）")
    out: dict[str, dict] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = load_card(path)
        out[data["id"]] = data
    return out


def render(handoff: Path) -> str:
    skeleton = read_text(handoff / SKELETON_NAME)
    cards = load_cards(handoff)
    used: set[str] = set()

    def sub(match: re.Match) -> str:
        cid = match.group(1)
        if cid not in cards:
            raise SystemExit(f"退出码 3：骨架里的卡 {cid} 没有对应卡片文件")
        used.add(cid)
        return cards[cid]["body"]

    text = PLACEHOLDER_RE.sub(sub, skeleton)
    missing = sorted(set(cards) - used)
    if missing:
        raise SystemExit(f"退出码 3：卡片文件没有出现在骨架里：{', '.join(missing)}")
    return text


def cmd_migrate(handoff: Path, force: bool) -> int:
    board = handoff / "BOARD.md"
    skeleton_path = handoff / SKELETON_NAME
    if skeleton_path.exists() and not force:
        print(f"骨架已存在（{skeleton_path}），加 --force 才覆盖。")
        return 3
    text = read_text(board)
    blocks = split_cards(text)
    skeleton, cards = skeleton_of(blocks)
    directory = cards_dir(handoff)
    directory.mkdir(exist_ok=True)
    for cid, body in cards.items():
        payload = card_payload(cid, column_of(skeleton, cid), owner_of(body), body,
                               depends=depends_of(body))
        write_text(directory / f"{cid}.yaml", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    write_text(skeleton_path, skeleton)
    rebuilt = render(handoff)
    if rebuilt != text:
        a, b = text, rebuilt
        offset = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
        print(f"往返不一致：BOARD.md {len(text)} 字符 / 重建 {len(rebuilt)} 字符，首处差异 offset={offset}")
        print(f"  原文：{a[offset:offset + 60]!r}")
        print(f"  重建：{b[offset:offset + 60]!r}")
        return 3
    print(f"迁移完成：{len(cards)} 张卡 → {directory}；骨架 → {skeleton_path}")
    print(f"往返校验：逐字节相同（{len(text)} 字符）")
    columns = {cid: column_of(skeleton, cid) for cid in cards}
    print("列分布：" + "，".join(f"{col}={sum(1 for value in columns.values() if value == col)}"
                               for col in COLUMNS if any(value == col for value in columns.values())))
    return 0


def cmd_render(handoff: Path, check: bool, out: str | None) -> int:
    built = render(handoff)
    board = handoff / "BOARD.md"
    if check:
        on_disk = read_text(board)
        if built == on_disk:
            print(f"BOARD.md 与 cards/ 一致（{len(built)} 字符，{len(load_cards(handoff))} 张卡）")
            return 0
        print("退出码 3：BOARD.md 与 cards/ 不一致——BOARD.md 被手改过，或卡文件被改后没 render。")
        return 3
    target = Path(out).resolve() if out else board
    write_text(target, built)
    print(f"已渲染 {target}（{len(built)} 字符）")
    return 0


def cmd_list(handoff: Path) -> int:
    cards = load_cards(handoff)
    for col in COLUMNS:
        group = [c for c in cards.values() if c["column"] == col]
        if not group:
            continue
        print(f"[{col}]")
        for card in group:
            title = card["body"].splitlines()[0].lstrip("# ").strip()
            owner = card.get("owner") or "-"
            print(f"  {card['id']:<3} owner={owner:<2} {title[:70]}")
    return 0


def cmd_show(handoff: Path, cid: str) -> int:
    cards = load_cards(handoff)
    if cid not in cards:
        print(f"退出码 3：没有这张卡：{cid}")
        return 3
    print(json.dumps({k: v for k, v in cards[cid].items() if k != "body"}, ensure_ascii=False, indent=2))
    print(cards[cid]["body"])
    return 0


def save_card(handoff: Path, card: dict) -> None:
    card["updated"] = now_stamp()
    write_text(cards_dir(handoff) / f"{card['id']}.yaml",
               json.dumps(card, ensure_ascii=False, indent=2) + "\n")


def cmd_move(handoff: Path, cid: str, column: str, note: str | None, by: str = "A") -> int:
    cards = load_cards(handoff)
    if cid not in cards:
        print(f"退出码 3：没有这张卡：{cid}")
        return 3
    if column not in COLUMNS:
        print(f"退出码 4：列名必须是 {'/'.join(COLUMNS)}")
        return 4
    card = cards[cid]
    card["column"] = column
    if note:
        card["body"] = card["body"].rstrip("\n") + f"\n- {by} 搬列 {now_stamp()}：{column}。{note}\n"
    save_card(handoff, card)
    print(f"{cid} → {column}（记得 render）")
    return 0


def cmd_add(handoff: Path, cid: str, title: str, column: str, owner: str,
            depends: str, scope: str, body_file: str | None) -> int:
    cards = load_cards(handoff)
    if cid in cards:
        print(f"退出码 3：卡 ID 已存在：{cid}")
        return 3
    if column not in COLUMNS:
        print(f"退出码 4：列名必须是 {'/'.join(COLUMNS)}")
        return 4
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8")
    else:
        body = (f"### {cid} {title}\n"
                f"- 负责人：{owner or '（待定）'}\n"
                f"- 验收判据：\n"
                f"  - ① \n"
                f"  - ② \n"
                f"- 证据：\n"
                f"- 依赖：{depends or '无'}\n")
    if not body.endswith("\n"):
        body += "\n"
    payload = card_payload(cid, column, owner, body,
                           depends=[p for p in depends.split(",") if p],
                           scope=[p for p in scope.split(",") if p])
    save_card(handoff, payload)
    skeleton_path = handoff / SKELETON_NAME
    skeleton = read_text(skeleton_path)
    placeholder = CARD_PLACEHOLDER.format(cid=cid)
    column_head = re.compile(rf"^##[ \t]+{re.escape(column)}[ \t]*\r?$", re.MULTILINE)
    if column_head.search(skeleton):
        skeleton = column_head.sub(lambda m: m.group(0) + "\n" + placeholder, skeleton, count=1)
    else:
        skeleton = skeleton.rstrip("\r\n") + f"\n\n## {column}\n{placeholder}\n"
    write_text(skeleton_path, skeleton)
    print(f"新增 {cid} 到「{column}」（骨架已插占位；记得 render）")
    return 0


def cmd_set(handoff: Path, cid: str, assignments: list[str]) -> int:
    cards = load_cards(handoff)
    if cid not in cards:
        print(f"退出码 3：没有这张卡：{cid}")
        return 3
    card = cards[cid]
    for item in assignments:
        if "=" not in item:
            print(f"退出码 4：--set 需要 key=value，收到 {item!r}")
            return 4
        key, value = item.split("=", 1)
        key = key.strip()
        if key in ("depends_on", "scope", "workset"):
            card[key] = [p for p in value.split(",") if p]
        elif key in ("column", "owner", "id"):
            if key == "column" and value not in COLUMNS:
                print(f"退出码 4：列名必须是 {'/'.join(COLUMNS)}")
                return 4
            card[key] = value
        elif key in ("degraded_reason", "worktree"):
            card[key] = value
        else:
            print(f"退出码 4：不支持的字段 {key}"
                  f"（可用 column/owner/depends_on/scope/workset/degraded_reason/worktree）")
            return 4
    save_card(handoff, card)
    print(f"{cid} 已更新（记得 render）")
    return 0


def cmd_selftest() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        handoff = Path(tmp)
        sample = ("# 看板\n\n## 纪律\n- 保持简短\n\n## Todo\n"
                  "### A1 示例卡\n- 负责人：A\n- 验收判据：\n  - ① 甲\n\n"
                  "## Done\n### B2 另一张\n- 负责人：B\n\n## 尾注\n- 完\n")
        write_text(handoff / "BOARD.md", sample)
        if cmd_migrate(handoff, force=False) != 0:
            print("selftest 失败：migrate")
            return 1
        if read_text(handoff / "BOARD.md") != render(handoff):
            print("selftest 失败：往返")
            return 1
        cards = load_cards(handoff)
        if cards["A1"]["column"] != "Todo" or cards["B2"]["column"] != "Done":
            print(f"selftest 失败：列名 {[(k, v['column']) for k, v in cards.items()]}")
            return 1
        if cmd_move(handoff, "A1", "Review", "selftest") != 0 or cmd_render(handoff, check=False, out=None) != 0:
            print("selftest 失败：move+render")
            return 1
        if load_cards(handoff)["A1"]["column"] != "Review":
            print("selftest 失败：搬列未生效")
            return 1
        if cmd_add(handoff, "C3", "新卡", "Todo", "C", "", "", None) != 0 or cmd_render(handoff, False, None) != 0:
            print("selftest 失败：add")
            return 1
        text = read_text(handoff / "BOARD.md")
        if "### C3 新卡" not in text:
            print("selftest 失败：新卡没进 BOARD")
            return 1
        write_text(handoff / "BOARD.md", text + "\n手改一行\n")
        if cmd_render(handoff, check=True, out=None) != 3:
            print("selftest 失败：手改应被 check 抓到")
            return 1
    print("selftest 通过：迁移/往返/列名/搬列/新增/漂移检测 6/6")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="board.py", description="交接区看板的卡数据源与渲染器（R13）")
    parser.add_argument("--handoff", help="交接区目录（默认 $COLLAB_HANDOFF 或 _handoff）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_migrate = sub.add_parser("migrate", help="BOARD.md → cards/*.yaml + board.skeleton.md")
    p_migrate.add_argument("--force", action="store_true")
    p_render = sub.add_parser("render", help="cards + 骨架 → BOARD.md")
    p_render.add_argument("--check", action="store_true", help="只比对，不写")
    p_render.add_argument("--out")
    sub.add_parser("list", help="列出卡")
    p_show = sub.add_parser("show"); p_show.add_argument("card")
    p_move = sub.add_parser("move"); p_move.add_argument("card"); p_move.add_argument("column")
    p_move.add_argument("--note")
    p_move.add_argument("--by", default="A", help="搬列人代号（写进证据行；默认 A）")
    p_add = sub.add_parser("add"); p_add.add_argument("card"); p_add.add_argument("title")
    p_add.add_argument("--column", default="Todo"); p_add.add_argument("--owner", default="")
    p_add.add_argument("--depends", default=""); p_add.add_argument("--scope", default="")
    p_add.add_argument("--body-file")
    p_set = sub.add_parser("set"); p_set.add_argument("card"); p_set.add_argument("assignments", nargs="+")
    sub.add_parser("selftest")
    args = parser.parse_args(argv)

    if args.command == "selftest":
        return cmd_selftest()

    handoff = resolve_handoff(args.handoff)
    if not handoff.is_dir():
        print(f"退出码 4：交接区目录不存在：{handoff}")
        return 4
    if args.command == "migrate":
        return cmd_migrate(handoff, args.force)
    if args.command == "render":
        return cmd_render(handoff, args.check, args.out)
    if args.command == "list":
        return cmd_list(handoff)
    if args.command == "show":
        return cmd_show(handoff, args.card)
    if args.command == "move":
        return cmd_move(handoff, args.card, args.column, args.note, args.by)
    if args.command == "add":
        return cmd_add(handoff, args.card, args.title, args.column, args.owner,
                       args.depends, args.scope, args.body_file)
    if args.command == "set":
        return cmd_set(handoff, args.card, args.assignments)
    print(f"退出码 4：未知命令 {args.command}")
    return 4


if __name__ == "__main__":
    sys.exit(main())
