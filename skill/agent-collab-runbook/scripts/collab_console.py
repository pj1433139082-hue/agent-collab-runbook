#!/usr/bin/env python3
"""collab_console.py — 协作控制台（决策 R7 / R8 / R9 / R10）。

动词一律只走公开接口（dsh-cron HTTP API + 交接区文件），不碰宿主内部状态：

    dispatch   把一张卡敲给它的负责人会话（默认**异步**：1 分钟后由插件自己触发，投递即返回）
               `--when-idle`：目标在跑时只写信封（delivery=pending），等 collect 择机投递
    collect    刷新并列出所有敲门的执行事实（任务状态/耗时/token）；顺带投递排队信封，可选复核
    verify     跑独立复核（薄封装 verify_receipt.py，结论自动记账到 ledger/verdicts.jsonl）
    close      复核完成后收口：删 cron 任务 + 在信封上记 closed_at（可选把卡搬进 Done）
    status     一屏：谁在跑、哪些卡在等、排队与通道健康、今天的敲门花了多少
    budget     预算闸：把今天的 token/成本与上限比，超限 exit 3（派活前先跑它）
    lease      占用声明：派活前先占 `<card>.lease.json`，防止 A/B 同派一张卡
    rotate     R9：换掉某个常驻工人的会话（只换身份，不换职责）
    land       R18：把卡分支落回主检出（ff / cherry-pick / 幂等 noop，**绝不推送**）
    workers    R52-2：列出候选会话（名册 ∩ 未归档）与当前登记
    bind       R52-2：登记/换绑某代号的工人会话（写骨架 + 重渲染 + 记轮换账）
    stop-all   R52-3：删掉非人类定时任务并打印在跑会话与剩余排期（**不中断**已启动的那一轮）

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

# R52-1：超时与排队（不新增存储——排队就是"信封在、任务未投"这一状态本身）
DEFAULT_TASK_TIMEOUT = 900            # 任务体 timeoutSeconds：到点中止，这是唯一的"停止"手段
DEFAULT_QUEUE_TIMEOUT = 4 * 3600      # --when-idle 的排队超期上限（秒）
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_ABANDONED = "abandoned"
# 控制台自己铸的任务 id 前缀；stop-all 只删这些，人类在看板上建的排期一律不碰
AGENT_TASK_PREFIXES = ("knock_", "rotate_", "probe_", "worker_boot", "palimpsest_probe")
KEEP_BY_DEFAULT = ("patrol",)         # 巡检是常设排期：停下它等于关掉看门狗，所以默认保留
CRON_PLUGIN_PACKAGE = "@goodandready/dsh-cron"


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


def workspace_state() -> dict:
    """宿主的工作区注册表（`~/.dsh/storages/workspace.json`）：名册与归档名单的唯一来源。"""
    path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / "storages" / "workspace.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def archived_sessions() -> list[str]:
    ids = workspace_state().get("global", {}).get("archivedSessionIds", [])
    return [item for item in ids if isinstance(item, str)]


def session_store_ids(root: Path | None = None) -> list[str]:
    """会话存贮里的会话 id（`~/.dsh/sessions/<cwd 桶>/<id>`，含每日重置变体的基名）。

    调度器铸出来的会话（`cron-exec-*`）不一定写进工作区注册表，只在存贮里留目录；
    只认注册表会把健康登记误报成「不在名册里」，所以名册取两者的并集。root 可注入便于自测。
    """
    root = root or Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / "sessions"
    ids: list[str] = []
    try:
        buckets = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return ids
    for bucket in buckets:
        try:
            names = [item.name for item in bucket.iterdir() if item.is_dir()]
        except OSError:
            continue
        for name in names:
            ids.append(name)
            base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", name)
            if base != name:
                ids.append(base)
    return sorted(set(ids))


def session_roster() -> list[str]:
    """会话名册：注册表 ∪ 会话存贮（含归档；调用方自行过滤）。"""
    tables = workspace_state().get("tables", {}).get("workspaces", {})
    ids: list[str] = []
    if isinstance(tables, dict):
        for record in tables.values():
            if not isinstance(record, dict):
                continue
            for item in record.get("sessionIds", []) or []:
                if isinstance(item, str):
                    ids.append(item)
            for item in record.get("pinnedSessionIds", []) or []:
                if isinstance(item, str):
                    ids.append(item)
    ids.extend(session_store_ids())
    return sorted(set(ids))


def running_agents(base: str) -> list[str]:
    status = api(base, "GET", "/dsh-market/status", timeout=10.0)
    agents = status.get("runningAgents") or status.get("status", {}).get("runningAgents") or []
    return [item for item in agents if isinstance(item, str)]


def plugin_runner_path(profile: str | None = None) -> Path | None:
    """dsh-cron 的 runner.js —— 唤醒通道就活在它那一行本地补丁上。"""
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    name = profile or os.environ.get("DSH_PROFILE") or "desktop"
    candidate = Path(os.environ.get("DSH_PROFILE_DIR") or (home / ".dsh" / "profiles" / name)) \
        / "node_modules" / "@goodandready" / "dsh-cron" / "lib" / "runner.js"
    return candidate if candidate.is_file() else None


def board_render_check(handoff: Path) -> tuple[int, str]:
    """看板一致性：交给 board.py 的 `render --check`，不在控制台里重写这条判据。"""
    try:
        proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "board.py"), "--handoff", str(handoff),
                               "render", "--check"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - 环境异常
        return 2, f"{type(error).__name__}: {error}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def patch_state(text: str, path: Path | None = None) -> dict:
    """唤醒补丁的三态判定（纯函数，便于自测）：补丁活在 dsh-cron 的 runner.js 那一行上。"""
    patched = "resumeSessionId: prep.targetSessionId" in text
    stale = re.search(r"(?<!resume)sessionId:\s*prep\.targetSessionId", text) is not None
    where = f"：{path}" if path else ""
    if patched and not stale:
        return {"id": "patch", "state": "OK", "detail": f"唤醒补丁在位（resumeSessionId 契约）{where}"}
    if patched and stale:
        return {"id": "patch", "state": "注意", "detail": "新旧键名同时出现，可能重复投递"}
    return {"id": "patch", "state": "注意", "detail": f"唤醒补丁不在（插件升级会覆盖）{where}"}


def binding_problems(workers: dict[str, str], roster: list[str], archived: list[str]) -> list[str]:
    """登记健康：每个非 A 代号必须在名册里且未归档。纯函数。"""
    problems: list[str] = []
    for code, session in sorted(workers.items()):
        if code == "A":
            continue  # 主会话由业主开，不在名册里也可能是正常的
        if session in archived:
            problems.append(f"{code} 已被归档")
        elif session not in roster:
            problems.append(f"{code} 不在会话名册里")
    return problems


def channel_health(handoff: Path, base: str, board_text: str, running: list[str] | None = None) -> list[dict]:
    """R52-4 通道自检三条判据（三态 OK / 注意 / 未知，未知优先）。全程只读。"""
    running = running_agents(base) if running is None else running
    archived = archived_sessions()
    results: list[dict] = []

    path = plugin_runner_path()
    if path is None:
        results.append({"id": "patch", "state": "未知",
                        "detail": "找不到 dsh-cron 的 runner.js（插件未装，或 profile 不是当前 profile）"})
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            results.append({"id": "patch", "state": "未知", "detail": f"读不到 {path}：{error}"})
        else:
            results.append(patch_state(text, path))

    workers = worker_table(board_text)
    if not workers:
        results.append({"id": "bindings", "state": "未知", "detail": "看板里没有常驻 worker 表"})
    else:
        problems = binding_problems(workers, session_roster(), archived)
        results.append({"id": "bindings", "state": "注意" if problems else "OK",
                        "detail": "；".join(problems) if problems
                        else f"{len(workers) - 1} 个登记会话都在名册且未归档"})

    code, text = board_render_check(handoff)
    results.append({"id": "board", "state": "OK" if code == 0 else "注意",
                    "detail": "看板与卡数据源逐字一致" if code == 0 else f"render --check 失败：{text.strip()[:200]}"})
    return results


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
# R52-1：排队语义（排队＝"信封已写、任务未投"这一状态本身，不新增存储）
# --------------------------------------------------------------------------- #
def delivery_decision(envelope: dict, running: list[str], archived: list[str],
                      at: datetime | None = None) -> tuple[str, str]:
    """排队信封的下一步：(动作, 原因)，动作 ∈ wait / deliver / abandon。纯函数，便于自测。"""
    target = str(envelope.get("target_session") or "")
    if target in archived:
        return "abandon", "archived_target"
    moment = at or now()
    deadline = envelope.get("queue_deadline")
    if isinstance(deadline, str) and deadline:
        try:
            if moment >= datetime.fromisoformat(deadline):
                return "abandon", "queue_timeout"
        except ValueError:
            pass
    if target in running:
        return "wait", "target_running"
    return "deliver", "target_idle"


def queue_deadline(seconds: int, at: datetime | None = None) -> str:
    return ((at or now()) + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def pending_envelopes(envelopes: list[dict]) -> list[dict]:
    """仍在排队的信封（delivery=pending）；老信封没有这个字段＝当时就已投递。"""
    return [item for item in envelopes if item.get("delivery") == DELIVERY_PENDING]


ROLE_LABELS = {"W": "常驻工人", "P": "巡检", "A": "主会话", "B": "并行会话"}


def role_title(code: object, fallback: str = "看板任务") -> str:
    """任务标题＝会话名的来源（同一个角色永远同一个标题）。

    dsh-cron 0.3.8 起用任务字段 `sessionTitle` 显式命名目标会话；`[` 开头的
    `title` 只是旧版回落（且只在新建会话时生效，续会话不覆盖）。因此载荷里
    必须**同时**给 `title` 与 `sessionTitle`，否则续会话拿不到名字。
    标题随卡变会把好名字换成上一张卡的名字，所以只按角色取，不按卡取。
    """
    code = str(code or "").strip().upper() or "?"
    return f"[{code}] {ROLE_LABELS.get(code, fallback)}"


def task_from_envelope(envelope: dict) -> dict:
    """按信封里固化的模板重建任务体（排队时没建任务，投递时才建）。"""
    template = envelope.get("task_template")
    task = dict(template) if isinstance(template, dict) else {}
    task.setdefault("id", envelope.get("knock_id"))
    task.setdefault("title", role_title(envelope.get("owner_code")))
    # dsh-cron 0.3.8：显式标题字段；续会话也生效（`[` 标题只在新会话生效）。
    task.setdefault("sessionTitle", task["title"])
    task.setdefault("schedule", envelope.get("schedule") or "in 1m")
    task.setdefault("targetSessionId", envelope.get("target_session"))
    task.setdefault("overlapPolicy", "skip")
    task["timeoutSeconds"] = int(envelope.get("timeout_seconds") or DEFAULT_TASK_TIMEOUT)
    task["maxRetries"] = int(envelope.get("max_retries") or 0)
    prompt = envelope.get("prompt")
    if isinstance(prompt, str) and prompt:
        task["prompt"] = prompt
    return task


def deliver_pending(handoff: Path, base: str, *, dry_run: bool = False,
                    running: list[str] | None = None) -> list[dict]:
    """把排队中的信封择机投出去：目标空闲就投，归档/超期就放弃并留痕。"""
    running = running_agents(base) if running is None else running
    archived = archived_sessions()
    listing = api(base, "GET", "/dsh-cron/tasks", timeout=20.0)
    tasks_raw = listing.get("tasks")
    existing = {item.get("id") for item in tasks_raw if isinstance(item, dict)} if isinstance(tasks_raw, list) else set()
    results: list[dict] = []
    for envelope in pending_envelopes(all_envelopes(handoff)):
        knock = str(envelope.get("knock_id") or "")
        if knock in existing:  # 任务其实已建（上次投递后没写回）：只补状态，不重复建
            envelope["delivery"] = DELIVERY_DELIVERED
            envelope["delivered_at"] = envelope.get("delivered_at") or stamp()
            if not dry_run:
                write_envelope(handoff, envelope)
            results.append({"knock": knock, "action": "deliver", "reason": "task_present"})
            continue
        action, reason = delivery_decision(envelope, running, archived)
        if action == "wait":
            results.append({"knock": knock, "action": "wait", "reason": reason})
            continue
        if action == "abandon":
            envelope["delivery"] = DELIVERY_ABANDONED
            envelope["abandoned_at"] = stamp()
            envelope["abandon_reason"] = reason
            if not dry_run:
                write_envelope(handoff, envelope)
            results.append({"knock": knock, "action": "abandon", "reason": reason})
            continue
        if dry_run:
            results.append({"knock": knock, "action": "deliver", "reason": reason})
            continue
        created = api(base, "POST", "/dsh-cron/tasks", task_from_envelope(envelope), timeout=60.0)
        if created.get("http_status") not in (200, 201) and not created.get("ok"):
            results.append({"knock": knock, "action": "error",
                            "reason": json.dumps(created, ensure_ascii=False)[:200]})
            continue
        envelope["delivery"] = DELIVERY_DELIVERED
        envelope["delivered_at"] = stamp()
        write_envelope(handoff, envelope)
        results.append({"knock": knock, "action": "deliver", "reason": reason})
    return results


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


def default_sandbox_root() -> Path:
    """没显式声明可写根时用的默认值。

    Windows（本项目的生产形态）用约定的工作根；POSIX（CI / Linux 试验）用临时目录 ——
    那里本来就是可写的，判据「工作树必须落在可写根内」在两边都能成立。"""
    if os.name == "nt":
        return Path(r".")
    return Path(tempfile.gettempdir())


def worktree_root() -> Path:
    return Path(os.environ.get("COLLAB_WORKTREE_ROOT", str(default_sandbox_root() / "_worktrees")))


def sandbox_roots() -> tuple[Path, ...]:
    raw = os.environ.get("COLLAB_SANDBOX_ROOTS")
    if raw:
        return tuple(Path(item) for item in raw.split(os.pathsep) if item.strip())
    return (default_sandbox_root(),)


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
        "title": role_title(args.worker, "常驻工人"),
        "sessionTitle": role_title(args.worker, "常驻工人"),
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
    queued = False
    if target in running and not args.force:
        if not args.when_idle:
            return fail("running_target", f"目标会话正在运行（{len(running)} 个）：{target}"
                                          f"（要排队加 --when-idle，要硬试加 --force）。")
        queued = True
    if queued and args.wait:
        return fail("bad_usage", "--when-idle 是异步排队，不能和 --wait 一起用。")
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
        "title": role_title(owner_code),
        "sessionTitle": role_title(owner_code),
        "schedule": args.schedule,
        "prompt": prompt,
        "targetSessionId": target,
        "provider": args.provider,
        "model": args.model,
        "permissionPreset": args.permission,
        "overlapPolicy": "skip",
        "timeoutSeconds": int(args.timeout),
        "maxRetries": int(args.max_retries),
    }
    if worktree_info:
        task["cwd"] = worktree_info["path"]
    elif args.cwd:
        task["cwd"] = args.cwd
    if args.dry_run:
        out(json.dumps({"dry_run": True, "task": task, "owner_code": owner_code,
                        "criteria_sha256": facts.get("criteria_sha256"),
                        "workset": workset, "worktree": worktree_info,
                        "delivery": DELIVERY_PENDING if queued else DELIVERY_DELIVERED,
                        "timeout_seconds": int(args.timeout), "max_retries": int(args.max_retries),
                        "budget_tokens": args.budget_tokens,
                        "queue_deadline": queue_deadline(args.queue_timeout) if queued else None},
                       ensure_ascii=False, indent=2))
        return 0
    brief = write_brief(handoff, knock, args.card, facts, board_text, brief_extra)
    if queued:
        out(f"排队中（目标在跑，未建任务）：{knock} → {target}")
    else:
        created = api(args.base, "POST", "/dsh-cron/tasks", task, timeout=60.0)
        if created.get("http_status") not in (200, 201) and not created.get("ok"):
            return fail("task_create_failed", json.dumps(created, ensure_ascii=False)[:400])
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
        "timeout_seconds": int(args.timeout),
        "max_retries": int(args.max_retries),
        "budget_tokens": args.budget_tokens,
        "delivery": DELIVERY_PENDING if queued else DELIVERY_DELIVERED,
        "queued_at": stamp() if queued else None,
        "queue_deadline": queue_deadline(args.queue_timeout) if queued else None,
        "delivered_at": None if queued else stamp(),
        "prompt": prompt,
        "task_template": {k: v for k, v in task.items() if k != "prompt"},
    }
    path = write_envelope(handoff, envelope)
    if not queued:
        out(f"已投递：{knock} → {target}（卡 {args.card}，负责人 {owner_code}）")
    out(f"  信封 {path}")
    out(f"  brief {brief}")
    out(f"  超时 {int(args.timeout)}s（到点中止，这是唯一的停止手段）；重试 {int(args.max_retries)}")
    if queued:
        out(f"  排队超期：{envelope['queue_deadline']}；`collect` 会在目标空闲时投递，超期则记 abandoned。")
        out(f"  复核前置：`collect --knock {knock}` 看投递结果；`verify`/`close` 在拿到回执后再跑。")
        return 0
    if args.wait:
        result = api(args.base, "POST", f"/dsh-cron/tasks/{knock}/run", timeout=float(args.timeout) + 60.0)
        envelope["history"] = {k: result.get(k) for k in ("ok", "status", "durationMs", "error") if k in result} or result
        write_envelope(handoff, envelope)
        out(f"  同步执行：{json.dumps(envelope['history'], ensure_ascii=False)[:300]}")
    else:
        out(f"  异步：插件将在 {args.schedule} 后触发（不等它跑完）；用 `collect --knock {knock}` 收执行事实。")
    out(f"  复核：`verify --card {args.card} --knock {knock}`；收口：`close --knock {knock} --card {args.card}`")
    return 0


def cmd_collect(args) -> int:
    handoff = Path(args.handoff)
    if not args.no_deliver:
        for item in deliver_pending(handoff, args.base):
            if item["action"] == "wait":
                continue
            out(f"排队信封 {item['knock']}：{item['action']}（{item['reason']}）")
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
            "delivery": envelope.get("delivery") or DELIVERY_DELIVERED,
        })
    out(f"{'knock':<22}{'卡':<5}{'投递':<11}{'任务状态':<9}{'耗时ms':<9}{'tokens':<10}{'回执':<8}closed")
    for row in rows:
        out(f"{row['knock']:<22}{str(row['card']):<5}{str(row['delivery']):<11}{str(row['status']):<9}"
            f"{str(row['ms']):<9}{str(row['tokens']):<10}{row['receipt']:<8}{row['closed'] or ''}")
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


def board_render(handoff: Path) -> tuple[int, str]:
    """重渲染看板（写 BOARD.md）——唯一权威仍是 board.py。"""
    try:
        proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "board.py"), "--handoff", str(handoff), "render"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover - 环境异常
        return 2, f"{type(error).__name__}: {error}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# --------------------------------------------------------------------------- #
# R52-2 / R52-3：工人登记与停机
# --------------------------------------------------------------------------- #
def cmd_workers(args) -> int:
    handoff = Path(args.handoff)
    workers = worker_table(read_board(handoff))
    archived = set(archived_sessions())
    roster = session_roster()
    out(f"当前登记（{len(workers)}）：")
    for code, session in sorted(workers.items()):
        mark = "已归档" if session in archived else ("在名册" if session in roster else "不在名册")
        out(f"  {code:<4}{session:<54}{mark}")
    candidates = [item for item in roster if item not in archived]
    free = [item for item in candidates if item not in set(workers.values())]
    out(f"候选（名册 ∩ 未归档 ∩ 未登记）：{len(free)}")
    for item in free[: max(0, int(args.limit))]:
        out(f"  {item}")
    out("换绑：`bind <代号> --session <id>`（写骨架 + 重渲染 + 记轮换账）")
    return 0


def bind_worker(handoff: Path, code: str, session: str, *, by: str = "A", reason: str | None = None,
                apply: bool = True, roster: list[str] | None = None,
                archived: list[str] | None = None) -> tuple[int, str]:
    """把某代号的会话换掉：写骨架 + 重渲染 + 记轮换账。失败即回滚骨架。

    roster / archived 可注入，便于自测脱网跑（默认读真实名册与归档名单）。
    """
    skeleton = handoff / "board.skeleton.md"
    try:
        text = skeleton.read_text(encoding="utf-8")
    except OSError as error:
        return 2, f"读不到骨架 {skeleton}：{error}"
    workers = worker_table(text) or worker_table(read_board(handoff))
    old = workers.get(code)
    if not old:
        return 2, f"骨架的 worker 表里没有代号 {code}"
    if old == session:
        return 2, f"{code} 已经绑在 {session} 上"
    known = session_roster() if roster is None else roster
    gone = archived_sessions() if archived is None else archived
    if session not in known:
        return 2, f"会话 {session} 不在名册里（没有在任何工作区登记过）"
    if session in gone:
        return 2, f"会话 {session} 已被归档，投进去只会变成 blocked"
    if not apply:
        return 0, f"干跑：{code} {old} → {session}"
    rewritten = rewrite_worker_session(text, old, session)
    if rewritten == text:
        return 2, f"骨架里找不到 {old} 的登记行（skeleton_stale：先核对看板 worker 表）"
    skeleton.write_text(rewritten, encoding="utf-8", newline="\n")
    code_exit, message = board_render(handoff)
    if code_exit != 0:
        skeleton.write_text(text, encoding="utf-8", newline="\n")  # 回滚，不留半改状态
        return 2, f"重渲染失败，骨架已回滚：{message.strip()[:200]}"
    append_rotation(handoff, {"at": stamp(), "worker": code, "old_session": old, "new_session": session,
                              "boot_task": None, "by": by, "reason": reason or "bind 换绑", "render_exit": code_exit})
    return 0, f"已换绑：{code} {old} → {session}（看板已重渲染，轮换账已记）"


def cmd_bind(args) -> int:
    handoff = Path(args.handoff)
    code_exit, message = bind_worker(handoff, args.worker, args.session, by=args.by, reason=args.reason,
                                     apply=not args.dry_run)
    out(message)
    return code_exit


def stopper_selection(tasks: list[dict], keep: tuple[str, ...] = KEEP_BY_DEFAULT) -> tuple[list[dict], list[dict]]:
    """停机清单：只挑控制台铸的任务（按 id 前缀），保留业主排期与 keep 前缀。纯函数。"""
    doomed: list[dict] = []
    kept: list[dict] = []
    for task in tasks:
        ident = str(task.get("id") or "")
        if ident.startswith(AGENT_TASK_PREFIXES) and not ident.startswith(keep):
            doomed.append(task)
        else:
            kept.append(task)
    return doomed, kept


def cmd_stop_all(args) -> int:
    handoff = Path(args.handoff)
    listing = api(args.base, "GET", "/dsh-cron/tasks", timeout=20.0)
    tasks_raw = listing.get("tasks")
    tasks = [item for item in tasks_raw if isinstance(item, dict)] if isinstance(tasks_raw, list) else []
    keep = tuple(item.strip() for item in (args.keep or "").split(",") if item.strip()) or KEEP_BY_DEFAULT
    doomed, kept = stopper_selection(tasks, keep)
    out(f"定时任务共 {len(tasks)}：将删 {len(doomed)}、保留 {len(kept)}（保留前缀：{', '.join(keep)}）")
    for task in doomed:
        ident = str(task.get("id") or "")
        if args.dry_run:
            out(f"  待删 {ident}  {task.get('title')}")
            continue
        result = api(args.base, "DELETE", f"/dsh-cron/tasks/{ident}", timeout=20.0)
        ok = result.get("http_status") in (200, 204) or bool(result.get("ok"))
        out(f"  {'已删' if ok else '删除失败'} {ident}  {task.get('title')}")
    for task in kept:
        out(f"  保留 {task.get('id')}  {task.get('schedule')}  {str(task.get('title'))[:40]}")
    running = running_agents(args.base)
    out(f"在跑的会话（{len(running)}）：" + (", ".join(running) or "无"))
    out("注意：宿主没有中断能力——**已经开始的那一轮不会被这条命令停掉**，只能等它自己跑完"
        "（或撞上任务体的 timeoutSeconds 到点中止）。")
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
    queued = pending_envelopes(envelopes)
    out(f"排队中（{len(queued)}）：" + (", ".join(f"{item.get('knock_id')}→{item.get('target_session')}"
                                                 for item in queued) or "无"))
    out(f"等回执的敲门（{len(pending)}）：" + (", ".join(item.get("knock_id", "") for item in pending) or "无"))
    try:
        board_text = read_board(handoff)
    except OSError:
        board_text = ""
    out("通道健康（R52-4）：")
    for item in channel_health(handoff, args.base, board_text, running=running):
        out(f"  [{item['state']}] {item['id']}：{item['detail']}")
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


def card_tokens(handoff: Path) -> list[dict]:
    """按卡汇总今日消耗（R52：budget 的按卡视图）。纯读取，缺 tokens 的信封跳过。"""
    day = now().strftime("%Y-%m-%d")
    totals: dict[str, dict] = {}
    for envelope in all_envelopes(handoff):
        issued = str(envelope.get("issued_at") or "")
        if not issued.startswith(day):
            continue
        card = str(envelope.get("card") or "?")
        entry = totals.setdefault(card, {"card": card, "tokens": 0, "knocks": 0, "budget": 0})
        tokens = (envelope.get("history") or {}).get("tokens")
        if isinstance(tokens, int):
            entry["tokens"] += tokens
            entry["knocks"] += 1
        budget = envelope.get("budget_tokens")
        if isinstance(budget, int):
            entry["budget"] += budget
    return sorted(totals.values(), key=lambda item: (-item["tokens"], item["card"]))


def cmd_budget(args) -> int:
    handoff = Path(args.handoff)
    used = today_tokens(handoff)
    limit = args.daily_tokens
    per_card = card_tokens(handoff)
    payload = {**used, "limit": limit, "ok": used["tokens"] <= limit, "per_card": per_card}
    if args.json:
        out(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        out(f"今日（{used['day']}）已用 {used['tokens']:,} tokens / 上限 {limit:,}（{used['knocks']} 次敲门）"
            + ("—— 可以继续派活。" if payload["ok"] else "—— 已超限，暂停派活（R10）。"))
        if per_card:
            out("按卡汇总：")
            for entry in per_card:
                cap = f" / 卡预算 {entry['budget']:,}" if entry["budget"] else ""
                out(f"  {entry['card']}: {entry['tokens']:,} tokens（{entry['knocks']} 次）{cap}")
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
    total = 0

    def check(title: str, condition: bool, detail: str = "") -> None:
        nonlocal failures, total
        total += 1
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
        check("按卡汇总：单卡单次",
              [item["card"] for item in card_tokens(handoff)] == ["X1"]
              and card_tokens(handoff)[0]["tokens"] == 1000, str(card_tokens(handoff)))
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
        # 判据按平台取默认根，别写死 E:\ —— 否则 Linux/CI 上会假红（2026-10-16 CI 抓到的第一个跨平台缺陷）
        check("沙箱：根内是 True", inside_sandbox(worktree_root() / "X9-010101") is True)
        outside_probe = (Path(tempfile.gettempdir()) / "wt-X9") if os.name == "nt" else (Path.home() / "acr-outside-probe")
        check("沙箱：根外是 False", inside_sandbox(outside_probe) is False)
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

    # ---- R52：排队决策 / 通道自检 / 停机清单 / 换绑（纯函数与临时目录，不碰真实环境）----
    queued = {"knock_id": "knock_020202_001", "card": "X9", "target_session": "cron-exec-w1",
              "delivery": DELIVERY_PENDING, "queue_deadline": queue_deadline(DEFAULT_QUEUE_TIMEOUT),
              "issued_at": stamp(), "history": {"tokens": 700}, "budget_tokens": 5000,
              "prompt": "跑 X9", "timeout_seconds": 300, "max_retries": 2,
              "task_template": {"id": "knock_020202_001", "title": "[W] 常驻工人", "schedule": "in 1m",
                                "targetSessionId": "cron-exec-w1", "permissionPreset": "workspace-write",
                                "overlapPolicy": "skip"}}
    check("排队：目标在跑 ⇒ 等", delivery_decision(queued, ["cron-exec-w1"], []) == ("wait", "target_running"),
          str(delivery_decision(queued, ["cron-exec-w1"], [])))
    check("排队：目标空闲 ⇒ 投", delivery_decision(queued, [], []) == ("deliver", "target_idle"),
          str(delivery_decision(queued, [], [])))
    check("排队：目标已归档 ⇒ 放弃",
          delivery_decision(queued, [], ["cron-exec-w1"]) == ("abandon", "archived_target"),
          str(delivery_decision(queued, [], ["cron-exec-w1"])))
    check("排队：超期 ⇒ 放弃（原因 queue_timeout）",
          delivery_decision(dict(queued, queue_deadline="2020-01-01T00:00:00+08:00"), [], []) == ("abandon", "queue_timeout"),
          str(delivery_decision(dict(queued, queue_deadline="2020-01-01T00:00:00+08:00"), [], [])))
    check("排队：deadline 不是合法时间 ⇒ 不误判超期",
          delivery_decision(dict(queued, queue_deadline="???"), [], [])[0] == "deliver",
          str(delivery_decision(dict(queued, queue_deadline="???"), [], [])))
    check("排队：只挑 delivery=pending（老信封视为已投）",
          [item["knock_id"] for item in pending_envelopes([queued, {"knock_id": "old"}])] == ["knock_020202_001"])
    rebuilt = task_from_envelope(queued)
    check("排队：任务体重建带超时/重试/提示词",
          rebuilt["timeoutSeconds"] == 300 and rebuilt["maxRetries"] == 2 and rebuilt["prompt"] == "跑 X9"
          and rebuilt["targetSessionId"] == "cron-exec-w1", str(rebuilt))
    check("排队：老信封没有超时字段 ⇒ 落到默认 900",
          task_from_envelope({"knock_id": "k", "target_session": "s"})["timeoutSeconds"] == DEFAULT_TASK_TIMEOUT)
    check("命名：角色标题稳定且以 [ 开头（dsh-cron 补丁据此命名会话）",
          role_title("W") == "[W] 常驻工人" and role_title("p") == "[P] 巡检"
          and role_title(None) == "[?] 看板任务", role_title("W"))
    check("命名：老信封没有标题 ⇒ 用角色标题兜底（同角色永远同名）",
          task_from_envelope({"knock_id": "k", "target_session": "s",
                              "owner_code": "W"})["title"] == "[W] 常驻工人")
    check("命名：任务体带显式 sessionTitle（dsh-cron 0.3.8 按字段命名，续会话也生效）",
          task_from_envelope({"knock_id": "k", "target_session": "s",
                              "owner_code": "W"})["sessionTitle"] == "[W] 常驻工人"
          and task_from_envelope({"knock_id": "k", "target_session": "s",
                                  "owner_code": "P"})["sessionTitle"] == "[P] 巡检")

    check("通道：补丁在位 ⇒ OK", patch_state("    resumeSessionId: prep.targetSessionId,")["state"] == "OK")
    check("通道：补丁不在 ⇒ 注意", patch_state("    sessionId: prep.targetSessionId,")["state"] == "注意")
    check("通道：新旧键名并存 ⇒ 注意（可能重复投递）",
          patch_state("resumeSessionId: prep.targetSessionId,\n    sessionId: prep.targetSessionId,")["state"] == "注意")
    check("通道：登记健康点名归档", binding_problems({"W": "w1", "P": "p1"}, ["w1", "p1"], ["p1"]) == ["P 已被归档"],
          str(binding_problems({"W": "w1", "P": "p1"}, ["w1", "p1"], ["p1"])))
    check("通道：登记健康点名不在名册", binding_problems({"W": "w9"}, ["w1"], []) == ["W 不在会话名册里"],
          str(binding_problems({"W": "w9"}, ["w1"], [])))
    check("通道：健康登记不报问题", binding_problems({"W": "w1", "A": "a1"}, ["w1"], []) == [])

    sample = [{"id": "knock_020202_001", "title": "敲"}, {"id": "rotate_020202_001", "title": "轮转"},
              {"id": "worker_boot", "title": "起工人"}, {"id": "patrol", "title": "巡检"},
              {"id": "cron_owner_1", "title": "业主排期"}]
    doomed, kept = stopper_selection(sample)
    check("停机：只删控制台铸的，保留巡检与业主排期",
          [item["id"] for item in doomed] == ["knock_020202_001", "rotate_020202_001", "worker_boot"]
          and [item["id"] for item in kept] == ["patrol", "cron_owner_1"], str([item["id"] for item in doomed]))
    check("停机：keep 加前缀即保留",
          [item["id"] for item in stopper_selection(sample, ("patrol", "rotate_"))[1]]
          == ["rotate_020202_001", "patrol", "cron_owner_1"],
          str([item["id"] for item in stopper_selection(sample, ("patrol", "rotate_"))[1]]))

    with tempfile.TemporaryDirectory() as tmp3:
        store = Path(tmp3) / "sessions"
        (store / "--E-a--" / "cron-exec-w1").mkdir(parents=True)
        (store / "--E-a--" / "cron-exec-patrol-2026-10-15").mkdir(parents=True)
        found = session_store_ids(store)
        check("名册：会话存贮按 cwd 桶枚举", "cron-exec-w1" in found, str(found))
        check("名册：每日重置变体折回基名", "cron-exec-patrol" in found, str(found))
        check("名册：存贮不存在 ⇒ 空且不抛", session_store_ids(Path(tmp3) / "nope") == [])

    with tempfile.TemporaryDirectory() as tmp2:
        bind_dir = Path(tmp2)
        (bind_dir / "cards").mkdir()
        (bind_dir / "outbox").mkdir()
        (bind_dir / "board.skeleton.md").write_text(
            "# 看板骨架\n\n## 常驻 worker\n\n| 代号 | 会话 ID |\n| --- | --- |\n"
            "| A | `session-aaaa` |\n| W | `cron-exec-w1` |\n", encoding="utf-8", newline="\n")
        code, message = bind_worker(bind_dir, "W", "cron-exec-w2", roster=["cron-exec-w1", "cron-exec-w2"],
                                    archived=[], apply=False)
        check("换绑：干跑只说不写",
              code == 0 and "cron-exec-w2" in message
              and "cron-exec-w2" not in (bind_dir / "board.skeleton.md").read_text(encoding="utf-8"), message)
        check("换绑：不在名册 ⇒ 拒绝",
              bind_worker(bind_dir, "W", "cron-exec-nope", roster=["cron-exec-w1"], archived=[])[0] == 2)
        check("换绑：已归档 ⇒ 拒绝",
              bind_worker(bind_dir, "W", "cron-exec-w2", roster=["cron-exec-w2"], archived=["cron-exec-w2"])[0] == 2)
        check("换绑：代号不在 worker 表 ⇒ 拒绝",
              bind_worker(bind_dir, "Z", "cron-exec-w2", roster=["cron-exec-w2"], archived=[])[0] == 2)
        code, message = bind_worker(bind_dir, "W", "cron-exec-w2", roster=["cron-exec-w2"], archived=[])
        rendered = (bind_dir / "board.skeleton.md").read_text(encoding="utf-8")
        check("换绑：真换 ⇒ 骨架改写 + 记轮换账",
              code == 0 and "cron-exec-w2" in rendered and (bind_dir / "ledger" / "rotations.jsonl").is_file(),
              message)

    print(f"SELFTEST {'OK' if not failures else 'FAILED'}: {total - failures}/{total}")
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
    p.add_argument("--timeout", type=int, default=DEFAULT_TASK_TIMEOUT,
                   help=f"任务体 timeoutSeconds：到点中止（默认 {DEFAULT_TASK_TIMEOUT}，这是唯一的停止手段）")
    p.add_argument("--max-retries", type=int, default=0, help="任务体 maxRetries（默认 0：不重试）")
    p.add_argument("--when-idle", action="store_true",
                   help="R52-1：目标在跑时不报错，只写信封排队（delivery=pending），由 collect 择机投递")
    p.add_argument("--queue-timeout", type=int, default=DEFAULT_QUEUE_TIMEOUT,
                   help=f"排队超期秒数（默认 {DEFAULT_QUEUE_TIMEOUT}）；超期记 abandoned 并留痕")
    p.add_argument("--budget-tokens", type=int, help="本卡预算（写进信封，供按卡汇总与预算闸）")
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

    p = sub.add_parser("collect", help="刷新并列出敲门的执行事实（顺带投递排队信封）")
    p.add_argument("--knock"); p.add_argument("--card")
    p.add_argument("--verify", action="store_true"); p.add_argument("--no-write", action="store_true")
    p.add_argument("--no-deliver", action="store_true", help="只读，不投递排队中的信封")
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

    p = sub.add_parser("workers", help="R52-2：列出候选会话（名册 ∩ 未归档）与当前登记")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("bind", help="R52-2：登记/换绑某代号的工人会话（写骨架 + 重渲染 + 记轮换账）")
    p.add_argument("worker"); p.add_argument("--session", required=True)
    p.add_argument("--by", default="A"); p.add_argument("--reason")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("stop-all", help="R52-3：删掉非人类定时任务（不中断已启动的那一轮）")
    p.add_argument("--keep", help=f"要保留的 id 前缀（逗号分隔；默认 {'/'.join(KEEP_BY_DEFAULT)}）")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return selftest()
    handler = {
        "dispatch": cmd_dispatch, "collect": cmd_collect, "verify": cmd_verify,
        "close": cmd_close, "status": cmd_status, "budget": cmd_budget, "lease": cmd_lease,
        "rotate": cmd_rotate, "land": cmd_land,
        "workers": cmd_workers, "bind": cmd_bind, "stop-all": cmd_stop_all,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
