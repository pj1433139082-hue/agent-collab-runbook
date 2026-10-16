#!/usr/bin/env python3
"""collab_console.py — 协作控制台（决策 R7 / R8 / R9 / R10）。

七个动词，全部只走公开接口（dsh-cron HTTP API + 交接区文件），不碰宿主内部状态：

    dispatch   把一张卡敲给它的负责人会话（默认**异步**：1 分钟后由插件自己触发，投递即返回）
    collect    刷新并列出所有敲门的执行事实（任务状态/耗时/token），可选顺手复核
    verify     跑独立复核（薄封装 verify_receipt.py，结论自动记账到 ledger/verdicts.jsonl）
    close      复核完成后收口：删 cron 任务 + 在信封上记 closed_at（可选把卡搬进 Done）
    status     一屏：谁在跑、哪些卡在等、今天的敲门花了多少
    budget     预算闸：把今天的 token/成本与上限比，超限 exit 3（派活前先跑它）
    lease      占用声明：派活前先占 `<card>.lease.json`，防止 A/B 同派一张卡

设计要点（都有实测依据）：
  · **异步投递用 `schedule: in 1m`，不用同步 run**——同步 `POST /tasks/<id>/run` 一旦客户端断开，
    宿主会 dispose 掉那一轮（2026-10-15 探针 600 s 超时即被 aborted/disposed）。
  · **信封落盘**在投递那一刻（`outbox/<knock_id>.history.json`），执行事实由 `collect` 回填；
    cron 任务保留到 `close` 才删（Q10：删早了就没取证窗口）。
  · **brief 化**（R9）：投递同时写 `outbox/<knock_id>.brief.md`，只含该卡判据/占用/回执格式，
    worker 不必读整本 75 KB 看板。
  · 三条守卫与 knock.ps1 v2 同源：负责人绑定（卡上负责人 == 目标会话）、未归档、未在运行。

退出码：0 正常 / 2 被守卫拒绝或用法问题 / 3 检查不通过（超预算、复核失败等）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # 技能目录里不留 __pycache__（发布审计会把它当"未知文件"）

CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_HANDOFF = Path(os.environ.get("COLLAB_HANDOFF") or r"_handoff")
DEFAULT_BASE = os.environ.get("COLLAB_BASE") or "http://127.0.0.1:19387"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROVIDER = "custom-workbuddy"
DEFAULT_MODEL = "cn:deepseek-v4.1-flash"
RECEIPT_HINT = ("回执按两件套交：`outbox/<card>-receipt.md`（人读，必须含 `KNOCK: <id>` 与 "
                "`STATUS: OK|FAIL|BLOCKED`）与 `outbox/<knock_id>.receipt.json`（机器读，"
                "schema 见技能 `references/receipt.schema.json`）。")


def now() -> datetime:
    return datetime.now(CHINA_TZ)


def stamp() -> str:
    return now().isoformat(timespec="seconds")


def out(text: str = "") -> None:
    print(text)


# --------------------------------------------------------------------------- #
# 公开接口（HTTP + 文件）
# --------------------------------------------------------------------------- #
def api(base: str, method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path, data=data, method=method,
        headers={"Origin": base.rstrip("/"), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            return {"http_status": error.code, **json.loads(raw)}
        except json.JSONDecodeError:
            return {"http_status": error.code, "error": raw[:400]}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"http_status": 0, "error": f"{type(error).__name__}: {error}"}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"http_status": 200, "error": f"非 JSON 响应：{raw[:200]}"}
    return payload if isinstance(payload, dict) else {"data": payload}


def archived_sessions() -> list[str]:
    path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / "storages" / "workspace.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    ids = data.get("global", {}).get("archivedSessionIds", [])
    return [item for item in ids if isinstance(item, str)]


def running_agents(base: str) -> list[str]:
    status = api(base, "GET", "/dsh-market/status", timeout=10.0)
    agents = status.get("runningAgents") or status.get("status", {}).get("runningAgents") or []
    return [item for item in agents if isinstance(item, str)]


def read_board(handoff: Path) -> str:
    return (handoff / "BOARD.md").read_text(encoding="utf-8", errors="replace")


WORKER_ROW = re.compile(r"^\|\s*([A-Za-z][A-Za-z0-9_]{0,7})\s*\|\s*([^|]*?)\s*\|", re.MULTILINE)
# 会话 id 有两种形态：宿主原生 `session-…`，以及 cron 铸出来的 `cron-exec-…`（W/P 就是后者）。
SESSION_TOKEN = re.compile(r"`?((?:session|cron-exec)-[A-Za-z0-9_-]+)`?")


def worker_table(board_text: str) -> dict[str, str]:
    """从看板「常驻 worker」表解析 代号 → 会话 id（认 `session-…` 与 `cron-exec-…`，`本会话` 之类跳过）。"""
    table: dict[str, str] = {}
    for code, cell in WORKER_ROW.findall(board_text):
        match = SESSION_TOKEN.search(cell)
        if match:
            table[code] = match.group(1)
    return table


def load_verifier():
    """按路径加载 verify_receipt.py。

    必须先登记进 sys.modules：`@dataclass` 会在装饰时回查 `sys.modules[cls.__module__]`，
    没登记就会 AttributeError: 'NoneType' object has no attribute '__dict__'（实测踩过）。
    """
    name = "collab_verify_receipt"
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPT_DIR / "verify_receipt.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def card_facts(handoff: Path, card: str) -> dict:
    verifier = load_verifier()
    block = verifier.card_block(read_board(handoff), card)
    if block is None:
        return {"error": f"卡 {card} 不在看板里"}
    text = verifier.field_text(block, verifier.CRITERIA_LABELS)
    owner = verifier.field_text(block, ("负责人",))
    owner_line = owner.splitlines()[0].strip() if owner else ""
    match = re.match(r"([A-Za-z][A-Za-z0-9_.-]*)", owner_line)
    session_match = SESSION_TOKEN.search(owner_line)
    return {
        "card": card,
        "block": block,
        "criteria_text": text,
        "criteria_sha256": verifier.sha256_text(text) if text else None,
        "criteria_ids": verifier.marker_ids(text),
        "owner": owner_line or None,
        "owner_code": match.group(1) if match else None,
        "owner_session": session_match.group(1) if session_match else None,
    }


# --------------------------------------------------------------------------- #
# 信封 / brief / lease
# --------------------------------------------------------------------------- #
def envelope_path(handoff: Path, knock: str) -> Path:
    return handoff / "outbox" / f"{knock}.history.json"


def write_envelope(handoff: Path, envelope: dict) -> Path:
    path = envelope_path(handoff, envelope["knock_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def load_envelope(handoff: Path, knock: str) -> dict | None:
    path = envelope_path(handoff, knock)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def all_envelopes(handoff: Path) -> list[dict]:
    items: list[dict] = []
    for path in sorted((handoff / "outbox").glob("knock_*.history.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            items.append(data)
    return items


def write_brief(handoff: Path, knock: str, card: str, facts: dict, board_text: str, extra: str = "") -> Path:
    """R9：worker 只读 brief，不读整本看板。"""
    block = facts.get("block") or ""
    lines = [
        f"# 敲门 brief · {card} · {knock}",
        "",
        f"- 发出时间：{stamp()}",
        f"- 回执两件套：`outbox/{card}-receipt.md`、`outbox/{knock}.receipt.json`",
        f"- 判据哈希（信封已固化）：`{facts.get('criteria_sha256')}`，改判据会让本轮回执 DEGRADED",
        "",
        "## 这张卡（原文）",
        "",
        block.strip(),
        "",
        "## 交回执的硬要求",
        "",
        "1. 回执**由你的 shell 直接写出**（`[System.IO.File]::WriteAllText`），不要用宿主的写文件工具代笔。",
        "2. `.md` 里必须有 `KNOCK: " + knock + "` 与 `STATUS: OK|FAIL|BLOCKED` 两行；",
        "   结构化块 `.json` 的 `status` 必须与 `.md` 一致（不一致 = 复核 FAIL）。",
        "3. 每条判据都要给**可被独立复算**的物证：文件给 path + sha256（+size）；临时产物用 "
        "`kind:\"inline\"` 把内容与 sha256 记进回执；删除类判据用 `kind:\"absent\"` 记复查命令与结果；"
        "只能自报的命令用 `kind:\"command\"`（复核器会标成「仅自报」，不可独立复算）。",
        "   用 `inline` 时 `content` 必须与 `sha256`/`size` **自洽**：哈希按文件字节算（含尾换行就把它算进去，"
        "或在 `content` 里显式写出 `\\n`）；文件跑完还在的话再给一个 `path`，复核器会直接对文件字节复算。",
        "4. `residue` 列出你留下的东西（应当为空）；复核器会逐条复查它们确实不存在。",
        "5. 做完把卡状态与证据写回 `BOARD.md`（**用 `board.py move --by <你的代号>`，别手改**），并在 `LOG.md` 追加一行 `[W]`。",
        "6. **不要自己记账**：复核由发起方做。你要自查就 `verify_receipt.py … --no-ledger`——不带这个开关会往 "
        "`ledger/verdicts.jsonl` 写一条记账（默认 `operator: A`），那会把结论记到发起方名下。",
        "7. 卡外的事不做；远端写（push / issue / 发布 / 装插件）一律不做。",
    ]
    if extra:
        lines += ["", "## 补充", "", extra]
    path = handoff / "outbox" / f"{knock}.brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def lease_path(handoff: Path, card: str) -> Path:
    return handoff / "outbox" / f"{card}.lease.json"


def read_lease(handoff: Path, card: str) -> dict | None:
    path = lease_path(handoff, card)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- #
# R18：一卡一工作树 + 工作集声明；R9：工人会话轮转
# --------------------------------------------------------------------------- #
def git(args: list[str], cwd: Path | None = None, timeout: float = 120.0) -> tuple[int, str]:
    """跑一条本机 git（只读检查 + worktree 增删；**不做远端写**），返回 (exit, 输出)。"""
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, f"{type(error).__name__}: {error}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def worktree_path(card: str, at: datetime | None = None) -> Path:
    """工作树落点：**必须在受限会话的沙箱可写根之内**（默认 `_worktrees`）。

    2026-10-16 实测事故：落在 `%TEMP%` 的工作树，工人 shell 一写就 `Access to the path … is denied`
    （DSH 文件沙箱只放行工作区根），工人转而申请提权 → 无人应答 → 整轮挂到超时。
    """
    stamp_text = (at or now()).strftime("%H%M%S")
    return worktree_root() / f"{card}-{stamp_text}"


def worktree_root() -> Path:
    return Path(os.environ.get("COLLAB_WORKTREE_ROOT", r"_worktrees"))


def sandbox_roots() -> tuple[Path, ...]:
    raw = os.environ.get("COLLAB_SANDBOX_ROOTS", r".")
    return tuple(Path(item) for item in raw.split(os.pathsep) if item.strip())


def inside_sandbox(path: Path | str) -> bool:
    """路径是否落在受限会话的可写根之内（判定用的纯函数，便于自测）。"""
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for root in sandbox_roots():
        try:
            target.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        return True
    return False


def changed_files(worktree: Path, base: str) -> list[str]:
    """工作树里相对 base 的改动（已提交 + 未提交 + 未跟踪），去重排序。"""
    files: set[str] = set()
    for args in (["diff", "--name-only", base], ["diff", "--name-only"],
                 ["ls-files", "--others", "--exclude-standard"]):
        code, text = git(args, cwd=worktree)
        if code != 0:
            continue
        files.update(line.strip() for line in text.splitlines() if line.strip())
    return sorted(files)


def workset_violations(changed: list[str], workset: list[str]) -> list[str]:
    """改了工作集之外的文件的清单（纯函数，便于自测）。

    工作集条目按**路径前缀**匹配：`model_lab/` 覆盖其下全部文件；空工作集 = 未声明 = 不判违规。
    """
    if not workset:
        return []
    prefixes = [item.replace("\\", "/").strip().strip("/") for item in workset if item.strip()]
    violations: list[str] = []
    for path in changed:
        norm = path.replace("\\", "/")
        if any(norm == prefix or norm.startswith(prefix + "/") for prefix in prefixes):
            continue
        violations.append(path)
    return violations


def rewrite_worker_session(text: str, old: str, new: str) -> str:
    """把看板骨架里的会话 id 换成新的（纯函数，便于自测）。"""
    if not old or old == new:
        return text
    return text.replace(old, new)


def rotations_path(handoff: Path) -> Path:
    return handoff / "ledger" / "rotations.jsonl"


def append_rotation(handoff: Path, record: dict) -> Path:
    path = rotations_path(handoff)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


BOOT_PROMPT = ("你被唤醒为一个新的协作工人会话。只做一件事：回复一行 `READY` 然后停止。"
               "不要读写任何文件、不要跑命令、不要动看板。")


def cmd_rotate(args) -> int:
    """R9：换掉某个常驻工人的会话（老会话上下文越来越贵；轮转只换身份，不换职责）。"""
    handoff = Path(args.handoff)
    skeleton_path = handoff / "board.skeleton.md"
    if not skeleton_path.is_file():
        return fail("skeleton_missing", f"没有 {skeleton_path}（看板尚未产物化）")
    workers = worker_table(read_board(handoff))
    old = args.old_session or workers.get(args.worker, "")
    if not old:
        return fail("worker_unknown", f"看板 worker 表里没有代号 {args.worker} 的会话；用 --old-session 指定。")
    boot = f"rotate_{now().strftime('%H%M%S')}_{random.randint(100, 999)}"
    task = {
        "id": boot,
        "title": f"[轮转] {args.worker}",
        "schedule": "in 1m",
        "prompt": BOOT_PROMPT,
        "cwd": args.cwd,
        "permissionPreset": args.permission,
        "provider": args.provider,
        "model": args.model,
    }
    if args.dry_run:
        out(json.dumps({"dry_run": True, "worker": args.worker, "old_session": old, "boot_task": task},
                       ensure_ascii=False, indent=2))
        return 0
    created = api(args.base, "POST", "/dsh-cron/tasks", task, timeout=60.0)
    if created.get("http_status") not in (200, 201) and not created.get("ok"):
        return fail("boot_create_failed", json.dumps(created, ensure_ascii=False)[:400])
    result = api(args.base, "POST", f"/dsh-cron/tasks/{boot}/run", timeout=args.timeout)
    history = api(args.base, "GET", f"/dsh-cron/tasks/{boot}/history?limit=1", timeout=30.0)
    last = (history.get("history") or [{}])[0] if isinstance(history.get("history"), list) else {}
    new = last.get("sessionId") or result.get("sessionId") or ""
    if not new:
        api(args.base, "DELETE", f"/dsh-cron/tasks/{boot}", timeout=30.0)
        return fail("boot_failed", f"新会话没拿到 sessionId：{json.dumps(last, ensure_ascii=False)[:300]}")
    text = skeleton_path.read_text(encoding="utf-8")
    if old not in text:
        return fail("skeleton_stale", f"骨架里找不到旧会话 {old}——先 `board.py render` 同步看板，再轮转。")
    skeleton_path.write_text(rewrite_worker_session(text, old, new), encoding="utf-8", newline="\n")
    board = load_sibling("board")
    code = board.cmd_render(handoff, False, None)
    record = {"at": stamp(), "worker": args.worker, "old_session": old, "new_session": new,
              "boot_task": boot, "by": args.by, "reason": args.reason or "",
              "render_exit": code}
    path = append_rotation(handoff, record)
    api(args.base, "DELETE", f"/dsh-cron/tasks/{boot}", timeout=30.0)
    out(f"已轮转 {args.worker}：{old} → {new}")
    out(f"  骨架已改、看板已重渲染（exit {code}）；轮转记录 {path}")
    out(f"  提示：正在等回执的旧敲门仍在旧会话里，轮转前先 `collect` 确认没有在途的卡。")
    return 0


# --------------------------------------------------------------------------- #
# 动 词
# --------------------------------------------------------------------------- #
def cmd_dispatch(args) -> int:
    handoff = Path(args.handoff)
    facts = card_facts(handoff, args.card)
    if facts.get("error"):
        return fail("card_missing", facts["error"])
    board_text = read_board(handoff)
    workers = worker_table(board_text)
    owner_code = args.owner or facts.get("owner_code") or ""
    declared = facts.get("owner_session")
    registered = workers.get(owner_code, "")
    target = args.session or declared or registered
    if not target:
        return fail("owner_unbound", f"看板里没有代号 {owner_code!r} 的常驻会话；用 --session 指定或用 --owner 换人。")
    if args.expected_owner and facts.get("owner_code") and facts["owner_code"] != args.expected_owner:
        return fail("owner_mismatch", f"卡上负责人是 {facts['owner_code']}，--expected-owner 要求 {args.expected_owner}。")
    allowed = {item for item in (declared, registered) if item}
    if target not in allowed and not args.force:
        return fail("owner_binding", f"卡 {args.card} 的负责人绑定是 {sorted(allowed)}，与目标 {target} 不一致"
                                     f"（--force 才越权）。")
    archived = archived_sessions()
    if target in archived:
        return fail("archived_target", f"目标会话在 global.archivedSessionIds 里（共 {len(archived)} 条）——"
                                       f"投进去必被宿主闸门拒（收下即 blocked、零 token）。")
    running = running_agents(args.base)
    if target in running and not args.force:
        return fail("running_target", f"目标会话正在运行（{len(running)} 个）：{target}（-Force 才硬试）。")
    lease = read_lease(handoff, args.card)
    if lease and not args.force:
        return fail("leased", f"卡 {args.card} 已被占用：{lease.get('by')} @ {lease.get('at')}（-Force 才抢占）。")

    # R18：一卡一工作树（只有显式 --worktree 才隔离；工作树里的改动要人来合，不自动 merge）
    worktree_info = None
    if args.worktree:
        repo = Path(args.worktree)
        if not is_git_repo(repo):
            return fail("not_a_repo", f"{repo} 不是 git 仓库（--worktree 要指向仓库根）")
        if not inside_sandbox(repo):
            return fail("repo_outside_sandbox",
                        f"仓库 {repo} 不在受限会话的可写根内（{os.pathsep.join(str(r) for r in sandbox_roots())}）："
                        f"git 的对象与引用也会写到沙箱外，工人提交必然失败。把仓库放到可写根内，或用 COLLAB_SANDBOX_ROOTS 显式声明。")
        code, head = git(["rev-parse", "HEAD"], cwd=repo)
        if code != 0:
            return fail("git_failed", head[:300])
        base_head = head.splitlines()[0].strip()
        branch = args.branch or f"card/{args.card}"
        path = Path(args.worktree_path) if args.worktree_path else worktree_path(args.card)
        if not inside_sandbox(path):
            return fail("worktree_outside_sandbox",
                        f"工作树落点 {path} 不在可写根内（默认请用 {worktree_root()}）：工人会在第一次写入时被沙箱拒绝，"
                        f"然后申请提权（无人应答即挂到超时）。")
        worktree_info = {"repo": str(repo), "path": str(path), "branch": branch, "base_head": base_head}
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            code, text = git(["worktree", "add", "-b", branch, str(path)], cwd=repo)
            if code != 0:
                return fail("worktree_failed", text[:400])
            out(f"工作树：{path}（分支 {branch}，基点 {base_head[:12]}）")

    workset = list(args.workset.split(",")) if args.workset else []
    if not workset:
        cards = load_sibling("board").load_cards(handoff)
        workset = list((cards.get(args.card) or {}).get("workset") or [])
    knock = f"knock_{now().strftime('%H%M%S')}_{random.randint(100, 999)}"
    prompt = args.message or (
        f"看板敲门：请读 `{handoff / ('outbox/' + knock + '.brief.md')}`（本卡的 brief），按其中的卡与要求执行；"
        f"回执写成两件套，做完把卡状态与证据写回看板（用 `board.py move`）、在 `LOG.md` 追加一行 `[W]`。"
        f"卡外的事不做，远端写一律不做。")
    brief_extra = args.brief_note or ""
    if worktree_info or workset:
        lines = []
        if workset:
            lines += ["- **工作集**（只允许改这些路径，越界会在收口时被点出来）：" + "、".join(f"`{item}`" for item in workset)]
        if worktree_info:
            lines += [f"- **你的工作树**：`{worktree_info['path']}`（分支 `{worktree_info['branch']}`，"
                      f"基点 `{worktree_info['base_head'][:12]}`）——在这里改代码，"
                      f"收工把改动 **commit 到本分支**（不要 push、不要切分支、不要动主工作树）。",
                      f"- 回执里请写明本分支的 commit id（`git -C \"{worktree_info['path']}\" rev-parse HEAD`）。"]
        lines += ["- 说明：工作集之外的改动会让这张卡在收口时被标「越界」，需要发起方确认后才能合。",
                  "- **不要申请沙箱提权**：若出现 `Access to the path … is denied`（写入被沙箱拒绝），立即停止该动作，"
                  "把命令原文、退出码与错误原样写进回执、该判据标 `fail`、状态写 `BLOCKED`——"
                  "无人值守的敲门没有人会批准提权，申请提权会把整轮挂到超时。",
                  "- **一卡一个执行者**：只做这一张卡；卡外的事不做，远端写（push / PR / issue / 装插件）一律不做。"]
        brief_extra = (brief_extra + "\n\n" if brief_extra else "") + "\n".join(lines)
    task = {
        "id": knock,
        "title": f"[敲] {args.card}",
        "schedule": args.schedule,
        "prompt": prompt,
        "targetSessionId": target,
        "provider": args.provider,
        "model": args.model,
        "permissionPreset": args.permission,
        "overlapPolicy": "skip",
    }
    if worktree_info:
        task["cwd"] = worktree_info["path"]
    elif args.cwd:
        task["cwd"] = args.cwd
    if args.dry_run:
        out(json.dumps({"dry_run": True, "task": task, "owner_code": owner_code,
                        "criteria_sha256": facts.get("criteria_sha256"),
                        "workset": workset, "worktree": worktree_info}, ensure_ascii=False, indent=2))
        return 0
    created = api(args.base, "POST", "/dsh-cron/tasks", task, timeout=60.0)
    if created.get("http_status") not in (200, 201) and not created.get("ok"):
        return fail("task_create_failed", json.dumps(created, ensure_ascii=False)[:400])
    brief = write_brief(handoff, knock, args.card, facts, board_text, brief_extra)
    envelope = {
        "knock_id": knock,
        "card": args.card,
        "target_session": target,
        "owner_code": owner_code,
        "issued_at": stamp(),
        "criteria_sha256": facts.get("criteria_sha256"),
        "criteria_ids": facts.get("criteria_ids"),
        "provider": args.provider,
        "model": args.model,
        "schedule": args.schedule,
        "mode": "sync" if args.wait else "async",
        "brief": str(brief),
        "workset": workset,
        "worktree": worktree_info,
        "task_kept": True,
        "history": None,
    }
    path = write_envelope(handoff, envelope)
    out(f"已投递：{knock} → {target}（卡 {args.card}，负责人 {owner_code}）")
    out(f"  信封 {path}")
    out(f"  brief {brief}")
    if args.wait:
        result = api(args.base, "POST", f"/dsh-cron/tasks/{knock}/run", timeout=args.timeout)
        envelope["history"] = {k: result.get(k) for k in ("ok", "status", "durationMs", "error") if k in result} or result
        write_envelope(handoff, envelope)
        out(f"  同步执行：{json.dumps(envelope['history'], ensure_ascii=False)[:300]}")
    else:
        out(f"  异步：插件将在 {args.schedule} 后触发（不等它跑完）；用 `collect --knock {knock}` 收执行事实。")
    out(f"  复核：`verify --card {args.card} --knock {knock}`；收口：`close --knock {knock} --card {args.card}`")
    return 0


def cmd_collect(args) -> int:
    handoff = Path(args.handoff)
    envelopes = all_envelopes(handoff)
    if args.knock:
        envelopes = [item for item in envelopes if item.get("knock_id") == args.knock]
    if args.card:
        envelopes = [item for item in envelopes if item.get("card") == args.card]
    tasks: dict[str, dict] = {}
    listing = api(args.base, "GET", "/dsh-cron/tasks", timeout=20.0)
    for item in listing.get("tasks", []) if isinstance(listing.get("tasks"), list) else []:
        if isinstance(item, dict) and item.get("id"):
            tasks[item["id"]] = item
    if not envelopes:
        out("没有匹配的信封。")
        return 0
    rows = []
    for envelope in envelopes:
        knock = envelope.get("knock_id", "")
        task = tasks.get(knock)
        if task is not None:
            envelope["history"] = {
                "lastStatus": task.get("lastStatus"),
                "lastDurationMs": task.get("lastDurationMs"),
                "lastError": task.get("lastError"),
                "tokens": task.get("tokens") or task.get("lastTokens"),
                "captured_at": stamp(),
            }
            envelope["task_present"] = True
            if not args.no_write:
                write_envelope(handoff, envelope)
        receipt_md = handoff / "outbox" / f"{envelope.get('card')}-receipt.md"
        receipt_json = handoff / "outbox" / f"{knock}.receipt.json"
        state = "已收件" if (receipt_md.is_file() and receipt_json.is_file()) else (
            "半套" if (receipt_md.is_file() or receipt_json.is_file()) else "等回执")
        rows.append({
            "knock": knock, "card": envelope.get("card"), "issued": envelope.get("issued_at"),
            "status": (envelope.get("history") or {}).get("lastStatus") or "-",
            "ms": (envelope.get("history") or {}).get("lastDurationMs") or "-",
            "tokens": (envelope.get("history") or {}).get("tokens") or "-",
            "receipt": state, "closed": envelope.get("closed_at"),
        })
    out(f"{'knock':<22}{'卡':<5}{'任务状态':<9}{'耗时ms':<9}{'tokens':<10}{'回执':<8}closed")
    for row in rows:
        out(f"{row['knock']:<22}{str(row['card']):<5}{str(row['status']):<9}{str(row['ms']):<9}"
            f"{str(row['tokens']):<10}{row['receipt']:<8}{row['closed'] or ''}")
    if args.verify:
        verifier = load_verifier()
        for row in rows:
            if row["receipt"] != "已收件":
                continue
            report = verifier.assess(
                card=row["card"], knock=row["knock"],
                board_path=handoff / "BOARD.md", outbox=handoff / "outbox",
                receipt_md=handoff / "outbox" / f"{row['card']}-receipt.md",
                receipt_json=handoff / "outbox" / f"{row['knock']}.receipt.json",
                schema_path=SCRIPT_DIR.parent / "references" / "receipt.schema.json",
            )
            record = verifier.ledger_record(report, card=row["card"], knock=row["knock"], handoff=handoff,
                                            receipt_md=handoff / "outbox" / f"{row['card']}-receipt.md",
                                            receipt_json=handoff / "outbox" / f"{row['knock']}.receipt.json",
                                            operator=args.operator)
            verifier.append_ledger(handoff, record)
            out(f"  {row['knock']} ⇒ {report.status}（已记账）" + (f" reasons={report.reasons}" if report.reasons else ""))
    return 0


def cmd_verify(args) -> int:
    verifier = load_verifier()
    handoff = Path(args.handoff)
    report = verifier.assess(
        card=args.card, knock=args.knock, board_path=handoff / "BOARD.md", outbox=handoff / "outbox",
        receipt_md=Path(args.receipt_md) if args.receipt_md else None,
        receipt_json=Path(args.receipt_json) if args.receipt_json else None,
        schema_path=SCRIPT_DIR.parent / "references" / "receipt.schema.json",
    )
    verifier.print_report(report, args.json)
    if not args.no_ledger:
        record = verifier.ledger_record(
            report, card=args.card, knock=args.knock, handoff=handoff,
            receipt_md=Path(args.receipt_md) if args.receipt_md else handoff / "outbox" / f"{args.card}-receipt.md",
            receipt_json=Path(args.receipt_json) if args.receipt_json else handoff / "outbox" / f"{args.knock}.receipt.json",
            operator=args.operator,
        )
        path = verifier.append_ledger(handoff, record)
        out(f"已记账：{path}（{report.status}）")
    return report.exit_code


def load_sibling(name: str):
    """按路径加载同目录脚本（登记进 sys.modules，见 load_verifier 的说明）。"""
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def cmd_close(args) -> int:
    handoff = Path(args.handoff)
    envelope = load_envelope(handoff, args.knock)
    if envelope is None:
        return fail("envelope_missing", f"没有信封 {args.knock}")
    card = args.card or envelope.get("card")
    board = load_sibling("board")
    card_data = (board.load_cards(handoff).get(card) or {})
    verdict = last_verdict(handoff, args.knock)
    status = (verdict or {}).get("status")
    if not args.force:
        if verdict is None:
            return fail("not_verified", f"{args.knock} 还没有复核结论——先 `verify --card {card} --knock {args.knock}`。")
        if status == "FAIL":
            return fail("verdict_failed", f"复核结论 FAIL：{'；'.join(verdict.get('reasons') or []) or '见账本'}"
                                           f"——补物证后重新交回执再复核。")
        if status == "DEGRADED":
            if args.accept_degraded:
                board.cmd_set(handoff, card, [f"degraded_reason={args.accept_degraded}"])
                out(f"降级已被接受并写进卡 {card}：{args.accept_degraded}")
            elif not card_data.get("degraded_reason"):
                return fail("degraded_unaccepted",
                            "复核结论是 DEGRADED（有判据只能自报、无法独立复算），而卡上没有 degraded_reason"
                            "——用 `--accept-degraded \"为什么可以接受\"` 明确接受，或补可复算物证后重新复核。")
            else:
                out(f"卡 {card} 已声明降级理由：{card_data['degraded_reason']}")

    # R18：工作树核对（改了工作集之外的什么？）+ 可选拆除
    violations: list[str] = []
    info = envelope.get("worktree") or {}
    if info.get("path"):
        wt = Path(info["path"])
        if wt.is_dir():
            workset = list(envelope.get("workset") or card_data.get("workset") or [])
            changed = changed_files(wt, info.get("base_head") or "HEAD")
            violations = workset_violations(changed, workset)
            head_code, head = git(["rev-parse", "HEAD"], cwd=wt)
            dirty = [line for line in git(["status", "--porcelain"], cwd=wt)[1].splitlines() if line.strip()]
            out(f"工作树 {wt}：分支 {info.get('branch')}，HEAD {head[:12] if head_code == 0 else '?'}，"
                f"改动 {len(changed)} 个文件，未提交 {len(dirty)} 条")
            if workset:
                out(f"  工作集：{'、'.join(workset)}"
                    + (f"；**越界 {len(violations)} 个**：{'、'.join(violations)}" if violations else "；无越界"))
            else:
                out("  未声明工作集（`board.py set <卡> workset=a,b`）——无法判越界。")
            if args.worktree_remove:
                remove_args = ["worktree", "remove", str(wt)] + (["--force"] if dirty or violations else [])
                code, text = git(remove_args, cwd=Path(info["repo"]))
                out(f"  拆除工作树：exit {code}" + (f" {text[:200]}" if text else ""))
                if code == 0:
                    envelope["worktree"] = {**info, "removed_at": stamp(),
                                            "changed": changed, "violations": violations}
        else:
            out(f"信封记的工作树 {wt} 已经不在（`git worktree list` 里可能有残留记录）。")

    deleted = api(args.base, "DELETE", f"/dsh-cron/tasks/{args.knock}", timeout=30.0)
    envelope["task_kept"] = False
    envelope["closed_at"] = stamp()
    envelope["close"] = {"http_status": deleted.get("http_status"), "ok": deleted.get("ok"),
                         "verdict": status, "violations": violations}
    write_envelope(handoff, envelope)
    lease = lease_path(handoff, card)
    if lease.is_file():
        lease.unlink()
    out(f"已收口 {args.knock}（卡 {card}，结论 {status}）：任务已删，信封记 closed_at，lease 已释放。")
    if args.move_done:
        code = board.cmd_move(handoff, card, "Done", f"复核 {status}（{args.knock}）", args.by)
        if code == 0:
            board.cmd_render(handoff, False, None)
        else:
            out(f"搬列失败（exit {code}）")
    if args.strict and violations:
        out(f"STRICT：工作集之外有 {len(violations)} 个改动，需要发起方确认后再合分支。")
        return 3
    return 0


def last_verdict(handoff: Path, knock: str) -> dict | None:
    path = handoff / "ledger" / "verdicts.jsonl"
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("knock_id") == knock:
            found = record
    return found


def landings_path(handoff: Path) -> Path:
    return handoff / "ledger" / "landings.jsonl"


def append_landing(handoff: Path, record: dict) -> Path:
    path = landings_path(handoff)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def land_plan(repo: Path, into: Path, base: str | None, branch: str) -> tuple[str, str]:
    """决定「怎么把分支落回主检出」：ff / cherry-pick / noop，或 error。

    只认三种情形，其余一律拒绝——落地是本机唯一会改主检出的动作，宁可停下问人。
    """
    if not base:
        return "error", "信封没记基点（base_head），无法判断快进还是摘取 ⇒ 拒绝落地（可 `--base <sha>` 显式给出）。"
    code, branch_head = git(["rev-parse", "--verify", f"{branch}^{{commit}}"], cwd=repo)
    if code != 0:
        return "error", f"仓库 {repo} 里没有分支 {branch}"
    code, head = git(["rev-parse", "HEAD"], cwd=into)
    if code != 0:
        return "error", f"目标检出 {into} 读不到 HEAD：{head[:200]}"
    head = head.strip()
    branch_head = branch_head.strip()
    # 「已经并进去了」先判：重复落地必须幂等，哪怕信封丢了基点也算 noop。
    if git(["merge-base", "--is-ancestor", branch_head, head], cwd=into)[0] == 0:
        return "noop", f"分支 {branch}（{branch_head[:12]}）已经落在目标 HEAD {head[:12]} 里了"
    if git(["merge-base", "--is-ancestor", base, head], cwd=into)[0] != 0:
        return "error", (f"目标 HEAD {head[:12]} 不是基点 {base[:12]} 的后代——主检出在卡之外动过"
                         f"（回退或换分支），需要人工决定，脚本不猜。")
    count = len([line for line in git(["rev-list", f"{base}..{branch}"], cwd=repo)[1].splitlines() if line.strip()])
    if count == 0:
        return "noop", f"分支 {branch} 相对基点 {base[:12]} 没有新提交"
    if head == base:
        return "ff", f"目标 HEAD 就是基点 {head[:12]} ⇒ 快进合并 {branch}（{count} 个提交）"
    return "cherry-pick", (f"目标 HEAD {head[:12]} 已在基点之后 ⇒ 把 {base[:12]}..{branch} 的 {count} 个提交"
                           f"逐个摘到 {head[:12]} 之上")


def land_apply(repo: Path, into: Path, mode: str, branch: str, base: str, dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, f"[dry-run] 将执行：{mode}"
    if mode == "ff":
        return git(["merge", "--ff-only", branch], cwd=into)
    if mode == "cherry-pick":
        code, text = git(["cherry-pick", f"{base}..{branch}"], cwd=into, timeout=300.0)
        if code != 0:
            git(["cherry-pick", "--abort"], cwd=into)
            return code, f"摘取失败（已 `cherry-pick --abort`，主检出回到动手前的状态）：{text[:400]}"
        return code, text
    return 0, "无需动作"


def delete_branch(repo: Path, branch: str, mode: str) -> tuple[bool, str]:
    """删掉已落地的卡分支；把 git 的拒绝原因原样带回来，不假装成功。"""
    code, text = git(["branch", "-d", branch], cwd=repo)
    if code == 0:
        return True, f"分支 {branch} 已删除（-d）"
    if mode == "cherry-pick":
        code, text = git(["branch", "-D", branch], cwd=repo)
        if code == 0:
            return True, (f"分支 {branch} 已用 -D 删除（摘取落地不留 merged 标记；"
                          f"原提交 sha 在 ledger/landings.jsonl 里）")
    reason = (text.strip().splitlines() or ["未知原因"])[0][:140]
    return False, (f"分支 {branch} 没删掉：{reason} —— 多半它还被某个工作树占着；"
                   f"先 `git worktree remove <工作树>` 再删，或留着给人看")


def cmd_land(args) -> int:
    handoff = Path(args.handoff)
    envelope = load_envelope(handoff, args.knock)
    if envelope is None:
        return fail("envelope_missing", f"没有信封 {args.knock}")
    card = args.card or envelope.get("card")
    board = load_sibling("board")
    card_data = board.load_cards(handoff).get(card) or {}
    verdict = last_verdict(handoff, args.knock)
    if not args.force:
        if verdict is None:
            return fail("not_verified", f"{args.knock} 还没有复核结论——先 `verify`。")
        if verdict.get("status") == "FAIL":
            return fail("verdict_failed", "复核 FAIL 的分支不落地：先补物证重新交回执。")
        if verdict.get("status") == "DEGRADED" and not card_data.get("degraded_reason"):
            return fail("degraded_unaccepted",
                        "复核 DEGRADED 且卡上没有 degraded_reason——先 `close --accept-degraded \"为什么可以接受\"`。")
    info = envelope.get("worktree") or {}
    repo = Path(args.repo or info.get("repo") or args.into or "")
    into = Path(args.into or info.get("repo") or "")
    if not str(into) or not into.is_dir():
        return fail("into_missing", "不知道落到哪个检出：信封没记 repo，就用 `--into <主检出路径>`。")
    branch = args.branch or info.get("branch") or f"card/{card}"
    base = args.base or info.get("base_head")
    tracked_dirty = [line for line in git(["status", "--porcelain"], cwd=into)[1].splitlines()
                     if line.strip() and not line.startswith("??")]
    if tracked_dirty and not args.allow_dirty:
        return fail("into_dirty",
                    f"主检出 {into} 有 {len(tracked_dirty)} 处已跟踪的未提交改动（第一条：{tracked_dirty[0][:80]}）"
                    f"——先提交或 stash；确实要带着脏工作树落地就加 `--allow-dirty`。")
    mode, note = land_plan(repo, into, base, branch)
    out(f"落地计划：{mode} —— {note}")
    if mode == "error":
        return fail("land_refused", note)
    if mode == "noop":
        out(f"无需落地：{note}")
        if args.delete_branch:
            deleted_now, note_del = delete_branch(repo, branch, mode)
            out(note_del)
            if deleted_now:
                head_now = git(["rev-parse", "HEAD"], cwd=into)[1].strip()
                append_landing(handoff, {
                    "at": stamp(), "by": args.by, "card": card, "knock_id": args.knock,
                    "repo": str(repo), "into": str(into), "branch": branch, "base": base,
                    "mode": "noop", "before_head": head_now, "after_head": head_now,
                    "landed_commits": [], "dirty_allowed": bool(args.allow_dirty),
                    "branch_deleted": True, "note": "已经落地过，本次只清理分支",
                })
        return 0
    before = git(["rev-parse", "HEAD"], cwd=into)[1].strip()
    code, text = land_apply(repo, into, mode, branch, base, args.dry_run)
    if code != 0:
        return fail("land_failed", text)
    if args.dry_run:
        out(f"[dry-run] {into} 的 HEAD 保持 {before[:12]}")
        return 0
    after = git(["rev-parse", "HEAD"], cwd=into)[1].strip()
    commits = [line.strip() for line in git(["rev-list", f"{base}..{branch}"], cwd=repo)[1].splitlines() if line.strip()]
    record = {
        "at": stamp(), "by": args.by, "card": card, "knock_id": args.knock,
        "repo": str(repo), "into": str(into), "branch": branch, "base": base,
        "mode": mode, "before_head": before, "after_head": after,
        "landed_commits": commits, "dirty_allowed": bool(args.allow_dirty),
    }
    deleted = False
    if args.delete_branch:
        deleted, note_del = delete_branch(repo, branch, mode)
        out(note_del)
    record["branch_deleted"] = deleted
    append_landing(handoff, record)
    envelope["landed"] = record
    write_envelope(handoff, envelope)
    out(f"已落地：{mode} → {into}（HEAD {before[:12]} → {after[:12]}，{len(commits)} 个提交）")
    out(f"记账：{landings_path(handoff)}" + ("；分支已删" if deleted else f"；分支 {branch} 保留"))
    return 0


def awaiting_receipts(handoff: Path, envelopes: list[dict]) -> list[dict]:
    """还在等回执的敲门：没交回执，也没被收口（收口过的信封带 closed_at）。"""
    waiting = []
    for item in envelopes:
        if item.get("closed_at"):
            continue
        if (handoff / "outbox" / f"{item.get('knock_id')}.receipt.json").is_file():
            continue
        waiting.append(item)
    return waiting


def cmd_status(args) -> int:
    handoff = Path(args.handoff)
    running = running_agents(args.base)
    listing = api(args.base, "GET", "/dsh-cron/tasks", timeout=20.0)
    tasks = listing.get("tasks", []) if isinstance(listing.get("tasks"), list) else []
    envelopes = all_envelopes(handoff)
    pending = awaiting_receipts(handoff, envelopes)
    cards = sorted((handoff / "cards").glob("*.yaml"))
    counts: dict[str, int] = {}
    for path in cards:
        try:
            column = json.loads(path.read_text(encoding="utf-8")).get("column", "?")
        except (OSError, json.JSONDecodeError):
            column = "?"
        counts[column] = counts.get(column, 0) + 1
    out(f"== {stamp()}")
    out(f"在跑的会话（{len(running)}）：" + (", ".join(item.replace('session-', '')[:12] for item in running) or "无"))
    out(f"cron 任务（{len(tasks)}）：")
    for task in tasks:
        out(f"  {task.get('id'):<24}{task.get('schedule', ''):<18}last={task.get('lastStatus', '-')}"
            f"  {str(task.get('title'))[:40]}")
    out(f"看板卡：{', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}")
    out(f"等回执的敲门（{len(pending)}）：" + (", ".join(item.get("knock_id", "") for item in pending) or "无"))
    ledger = handoff / "ledger" / "verdicts.jsonl"
    if ledger.is_file():
        lines = [line for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        out(f"复核账目（{len(lines)} 条），最近三条：")
        for line in lines[-3:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            out(f"  {record.get('at')}  {record.get('card')}  {record.get('knock_id')}  {record.get('status')}")
    if args.budget_tokens:
        return cmd_budget(argparse.Namespace(handoff=args.handoff, daily_tokens=args.budget_tokens, json=False))
    return 0


def today_tokens(handoff: Path) -> dict:
    day = now().strftime("%Y-%m-%d")
    total = 0
    knocks = 0
    for envelope in all_envelopes(handoff):
        issued = str(envelope.get("issued_at") or "")
        if not issued.startswith(day):
            continue
        tokens = (envelope.get("history") or {}).get("tokens")
        if isinstance(tokens, int):
            total += tokens
            knocks += 1
    return {"day": day, "tokens": total, "knocks": knocks}


def cmd_budget(args) -> int:
    handoff = Path(args.handoff)
    used = today_tokens(handoff)
    limit = args.daily_tokens
    payload = {**used, "limit": limit, "ok": used["tokens"] <= limit}
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        out(f"今日（{used['day']}）已用 {used['tokens']:,} tokens / 上限 {limit:,}（{used['knocks']} 次敲门）"
            + ("—— 可以继续派活。" if payload["ok"] else "—— 已超限，暂停派活（R10）。"))
    return 0 if payload["ok"] else 3


def cmd_lease(args) -> int:
    handoff = Path(args.handoff)
    path = lease_path(handoff, args.card)
    if args.release:
        if path.is_file():
            path.unlink()
            out(f"已释放 {args.card} 的占用。")
            return 0
        out(f"{args.card} 本来就没有占用。")
        return 0
    existing = read_lease(handoff, args.card)
    if existing and not args.force:
        return fail("leased", f"{args.card} 已被 {existing.get('by')} 占用（{existing.get('at')}）。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"card": args.card, "by": args.by, "at": stamp(),
                                "note": args.note or ""}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    out(f"已占用 {args.card}（by {args.by}）。派活/收工后记得 `lease {args.card} --release`。")
    return 0


def fail(code: str, message: str) -> int:
    out(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False))
    return 2


# --------------------------------------------------------------------------- #
def selftest() -> int:
    failures = 0

    def check(title: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        if not condition:
            failures += 1
        print(f"  {'ok  ' if condition else 'FAIL'} {title}" + (f"  {detail}" if detail and not condition else ""))

    board = ("| 代号 | 会话 ID | 工作区 | 职责 | 唤醒方式 |\n| --- | --- | --- | --- | --- |\n"
             "| A | session-aaaa-bbbb | x | 主会话 | 用户开口 |\n"
             "| W | `session-cccc-dddd` | y | worker | knock |\n"
             "| P | session-eeee-ffff | z | 巡检 | cron |\n")
    workers = worker_table(board)
    check("worker 表解析（含反引号）", workers.get("W") == "session-cccc-dddd" and len(workers) == 3, str(workers))

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        handoff = Path(tmp)
        (handoff / "outbox").mkdir()
        envelope = {"knock_id": "knock_010101_001", "card": "X1", "issued_at": stamp(),
                    "history": {"tokens": 1000, "lastStatus": "success"}}
        write_envelope(handoff, envelope)
        (handoff / "ledger").mkdir()
        (handoff / "ledger" / "verdicts.jsonl").write_text(
            json.dumps({"knock_id": "knock_010101_001", "status": "PASS"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        check("信封往返", (load_envelope(handoff, "knock_010101_001") or {}).get("card") == "X1")
        check("今日 token 汇总", today_tokens(handoff)["tokens"] == 1000, str(today_tokens(handoff)))
        check("账目查询", (last_verdict(handoff, "knock_010101_001") or {}).get("status") == "PASS")
        facts = {"block": "### X1 自测\n- 负责人：W\n- 验收判据：①甲\n", "criteria_sha256": "0" * 64}
        brief = write_brief(handoff, "knock_010101_002", "X1", facts, board)
        text = brief.read_text(encoding="utf-8")
        check("brief 含 KNOCK 与判据", "KNOCK: knock_010101_002" in text and "①甲" in text)
        lease = argparse.Namespace(handoff=str(handoff), card="X1", release=False, force=False, by="A", note="")
        check("占用声明", cmd_lease(lease) == 0 and read_lease(handoff, "X1") is not None)
        check("重复占用被拒", cmd_lease(lease) == 2)
        lease.release = True
        check("释放占用", cmd_lease(lease) == 0 and read_lease(handoff, "X1") is None)

        # R18：工作集越界判定（纯函数）
        changed = ["model_lab/cli.py", "model_lab/sub/deep.py", "docs/x.md", "tests/test_cli.py"]
        check("工作集：前缀命中不算越界",
              workset_violations(changed, ["model_lab/", "tests/test_cli.py"]) == ["docs/x.md"],
              str(workset_violations(changed, ["model_lab/", "tests/test_cli.py"])))
        check("工作集：精确文件命中", workset_violations(["a/b.py"], ["a/b.py"]) == [])
        check("工作集：空声明不判越界", workset_violations(changed, []) == [])
        check("工作集：反斜杠归一", workset_violations([r"model_lab\cli.py"], ["model_lab"]) == [])

        # R18：工作树必须落在沙箱可写根内（2026-10-16 实测事故：落在 %TEMP% 的工作树被沙箱拒绝）
        check("沙箱：根内是 True", inside_sandbox(Path(r"_worktrees\X9-010101")) is True)
        check("沙箱：根外是 False", inside_sandbox(Path(r"C:\Users\example\AppData\Local\Temp\wt-X9")) is False)
        check("沙箱：默认工作树落点在根内", inside_sandbox(worktree_path("X9")) is True)

        # R9：轮转时的骨架改写（纯函数）
        skeleton = "| W | `cron-exec-worker_boot-aaa` | x |\n| P | `cron-exec-patrol_bbb` | y |\n"
        rotated = rewrite_worker_session(skeleton, "cron-exec-worker_boot-aaa", "cron-exec-worker_boot-ccc")
        check("轮转：只换目标会话",
              "cron-exec-worker_boot-ccc" in rotated and "cron-exec-patrol_bbb" in rotated
              and "cron-exec-worker_boot-aaa" not in rotated)
        check("轮转：old==new 不动", rewrite_worker_session(skeleton, "x", "x") == skeleton)

        # R11-4：DEGRADED 未接受时必须拦住
        (handoff / "ledger" / "verdicts.jsonl").write_text(
            json.dumps({"knock_id": "knock_010101_003", "card": "X2", "status": "DEGRADED"},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        write_envelope(handoff, {"knock_id": "knock_010101_003", "card": "X2", "issued_at": stamp()})
        check("账目：取到 DEGRADED", (last_verdict(handoff, "knock_010101_003") or {}).get("status") == "DEGRADED")
        # 收口过的敲门不该继续显示成「等回执」（2026-10-16 实测：close 之后 status 仍在催）
        write_envelope(handoff, {"knock_id": "knock_010101_004", "card": "X2", "issued_at": stamp(),
                                 "closed_at": stamp()})
        waiting_ids = [item.get("knock_id") for item in awaiting_receipts(handoff, all_envelopes(handoff))]
        check("等回执：已收口的不算在等",
              "knock_010101_004" not in waiting_ids and "knock_010101_003" in waiting_ids, str(waiting_ids))

    # R18 第二阶段：落地计划（ff / cherry-pick / noop / 拒绝）——用真 git 仓库，别用假的判据
    # 真实拓扑：repo 是主检出（main），卡分支活在另一个目录的工作树里；落地＝把分支并回主检出。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, wt = root / "repo", root / "wt"

        def seed(path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)
            git(["init", "-q", "-b", "main"], cwd=path)
            git(["config", "user.name", "selftest"], cwd=path)
            git(["config", "user.email", "selftest@local"], cwd=path)
            git(["config", "core.autocrlf", "false"], cwd=path)
            (path / "seed.txt").write_text("seed\n", encoding="utf-8")
            git(["add", "-A"], cwd=path)
            git(["commit", "-q", "-m", "seed"], cwd=path)

        def commit_in(path: Path, name: str, text: str, message: str) -> str:
            (path / name).write_text(text, encoding="utf-8")
            git(["add", "-A"], cwd=path)
            git(["commit", "-q", "-m", message], cwd=path)
            return git(["rev-parse", "HEAD"], cwd=path)[1].strip()

        seed(repo)
        base = git(["rev-parse", "HEAD"], cwd=repo)[1].strip()
        git(["worktree", "add", "-q", str(wt), "-b", "card/X9"], cwd=repo)
        branch_head = commit_in(wt, "card9.txt", "card9\n", "card X9 work")
        check("落地：主检出停在基点 ⇒ ff", land_plan(repo, repo, base, "card/X9")[0] == "ff",
              str(land_plan(repo, repo, base, "card/X9")))
        check("落地：没记基点 ⇒ 拒绝", land_plan(repo, repo, None, "card/X9")[0] == "error")
        check("落地：分支不存在 ⇒ 拒绝", land_plan(repo, repo, base, "card/nope")[0] == "error")
        tree = git(["rev-parse", "HEAD^{tree}"], cwd=repo)[1].strip()
        orphan = git(["commit-tree", tree, "-m", "unrelated root"], cwd=repo)[1].strip()
        check("落地：基点不是 HEAD 祖先 ⇒ 拒绝", land_plan(repo, repo, orphan, "card/X9")[0] == "error")
        code, text = land_apply(repo, repo, "ff", "card/X9", base, False)
        check("落地：ff 后主检出 HEAD = 分支头",
              code == 0 and git(["rev-parse", "HEAD"], cwd=repo)[1].strip() == branch_head, text[:120])
        check("落地：重复落地幂等 ⇒ noop", land_plan(repo, repo, base, "card/X9")[0] == "noop",
              str(land_plan(repo, repo, base, "card/X9")))
        ok, why = delete_branch(repo, "card/X9", "ff")
        check("落地：分支被工作树占着 ⇒ 拒绝删除且带原因", ok is False and "没删掉" in why, why)
        git(["worktree", "remove", "--force", str(wt)], cwd=repo)
        ok, why = delete_branch(repo, "card/X9", "ff")
        check("落地：工作树移除后删分支成功", ok is True and "已删除" in why, why)
        # 第二个卡分支：基点之后的提交应走 cherry-pick
        moved = commit_in(repo, "mainside.txt", "mainside\n", "main moves on")
        git(["worktree", "add", "-q", str(root / "wt2"), "-b", "card/X10", base], cwd=repo)
        commit_in(root / "wt2", "card10.txt", "card10\n", "card X10 work")
        check("落地：主检出已前进 ⇒ cherry-pick", land_plan(repo, repo, base, "card/X10")[0] == "cherry-pick",
              str(land_plan(repo, repo, base, "card/X10")))
        code, text = land_apply(repo, repo, "cherry-pick", "card/X10", base, False)
        check("落地：摘取成功且两边内容都在",
              code == 0 and (repo / "card10.txt").is_file() and (repo / "mainside.txt").is_file()
              and git(["rev-parse", "HEAD"], cwd=repo)[1].strip() != moved, text[:120])
        check("落地：摘取后分支未并入 ⇒ 计划变 noop 之外的正确态",
              land_plan(repo, repo, base, "card/X10")[0] in ("noop", "cherry-pick"))
    print(f"SELFTEST {'OK' if not failures else 'FAILED'}: {26 - failures}/26")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collab_console.py", description="协作控制台（R7/R8/R9/R10）")
    parser.add_argument("--handoff", default=str(DEFAULT_HANDOFF))
    parser.add_argument("--base", default=DEFAULT_BASE)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dispatch", help="把一张卡敲给它的负责人会话")
    p.add_argument("card")
    p.add_argument("--session"); p.add_argument("--owner"); p.add_argument("--expected-owner")
    p.add_argument("--message"); p.add_argument("--brief-note")
    p.add_argument("--provider", default=DEFAULT_PROVIDER); p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--permission", default="workspace-write")
    p.add_argument("--schedule", default="in 1m", help="异步派活的触发时刻（默认 in 1m）")
    p.add_argument("--cwd", help="任务 cwd（不给 --worktree 时用）")
    p.add_argument("--worktree", help="R18：在这里建一卡一工作树（仓库根路径）")
    p.add_argument("--worktree-path", help="工作树落点（默认 _worktrees\\<卡>-<时刻>，必须在沙箱可写根内）")
    p.add_argument("--branch", help="工作树分支名（默认 card/<卡>）")
    p.add_argument("--workset", help="工作集（逗号分隔的路径前缀，覆盖卡上的 workset）")
    p.add_argument("--wait", action="store_true", help="同步等它跑完（默认不等）")
    p.add_argument("--timeout", type=float, default=900.0)
    p.add_argument("--force", action="store_true"); p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("rotate", help="R9：换掉某个常驻工人的会话（只换身份，不换职责）")
    p.add_argument("worker", help="代号，如 W / P")
    p.add_argument("--old-session", help="显式指定旧会话 id（默认从看板 worker 表读）")
    p.add_argument("--cwd", default=r".")
    p.add_argument("--permission", default="workspace-write")
    p.add_argument("--provider", default=DEFAULT_PROVIDER); p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--by", default="A"); p.add_argument("--reason")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("collect", help="刷新并列出敲门的执行事实")
    p.add_argument("--knock"); p.add_argument("--card")
    p.add_argument("--verify", action="store_true"); p.add_argument("--no-write", action="store_true")
    p.add_argument("--operator", default="A")

    p = sub.add_parser("verify", help="独立复核并按 R14 记账")
    p.add_argument("--card", required=True); p.add_argument("--knock", required=True)
    p.add_argument("--receipt-md"); p.add_argument("--receipt-json")
    p.add_argument("--json", action="store_true"); p.add_argument("--no-ledger", action="store_true")
    p.add_argument("--operator", default="A")

    p = sub.add_parser("close", help="复核完成后收口（删任务 + 记 closed_at）")
    p.add_argument("--knock", required=True); p.add_argument("--card")
    p.add_argument("--move-done", action="store_true"); p.add_argument("--force", action="store_true")
    p.add_argument("--by", default="A")
    p.add_argument("--accept-degraded", help="R11-4：明确接受 DEGRADED 结论并把理由写进卡")
    p.add_argument("--worktree-remove", action="store_true", help="R18：收口时拆掉该卡的工作树")
    p.add_argument("--strict", action="store_true", help="工作集越界即 exit 3")

    p = sub.add_parser("land", help="R18 第二阶段：把卡的分支落回主检出（本机合并，绝不推送）")
    p.add_argument("--knock", required=True); p.add_argument("--card")
    p.add_argument("--into", help="落到哪个检出（默认取信封记的 repo）")
    p.add_argument("--repo", help="分支所在的仓库（默认取信封记的 repo）")
    p.add_argument("--branch"); p.add_argument("--base", help="基点 sha（默认取信封的 base_head）")
    p.add_argument("--delete-branch", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--by", default="A")
    p.add_argument("--force", action="store_true"); p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("status", help="一屏状态")
    p.add_argument("--budget-tokens", type=int)

    p = sub.add_parser("budget", help="预算闸（超限 exit 3）")
    p.add_argument("--daily-tokens", type=int, default=int(os.environ.get("COLLAB_DAILY_TOKENS", 12_000_000)))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("lease", help="占用声明")
    p.add_argument("card"); p.add_argument("--by", default="A"); p.add_argument("--note")
    p.add_argument("--release", action="store_true"); p.add_argument("--force", action="store_true")

    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return selftest()
    handler = {
        "dispatch": cmd_dispatch, "collect": cmd_collect, "verify": cmd_verify,
        "close": cmd_close, "status": cmd_status, "budget": cmd_budget, "lease": cmd_lease,
        "rotate": cmd_rotate, "land": cmd_land,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
