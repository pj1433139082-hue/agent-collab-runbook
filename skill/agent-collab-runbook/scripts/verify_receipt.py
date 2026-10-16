#!/usr/bin/env python3
"""回执复核器 —— 独立复算「能复算的部分」，给回执一个三态结论。

契约（`references/receipt.schema.json` + `references/handoff-protocol.md`）：
  · 敲门信封   `<outbox>/<knock_id>.history.json`  —— 固化 knock_id / card / criteria_sha256 / target_session
  · 人读回执   `<outbox>/<card>-receipt.md`        —— 必须含 `KNOCK: <id>` 与 `STATUS: OK|FAIL|BLOCKED`
  · 机器回执   `<outbox>/<knock_id>.receipt.json`  —— 必须过 schema，且与 .md 的 STATUS 一致

三态（只报告、不阻断；退出码 0/1/2，用法错误 4）：
  PASS      每一条判据的每一件物证都被**独立复算**过（文件哈希重算、缺失重查）
  DEGRADED  没有失败，但有「无法独立复算」的项（命令未复跑 / 判据漂移 / 覆盖面缺口）
  FAIL      物证与现实矛盾、判据自报未通过、有残留、或散文与结构化块冲突

用法：
    python verify_receipt.py --card W5 --knock <id>
    python verify_receipt.py --card W5 --knock <id> --json
    python verify_receipt.py --selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

MARKERS = "①②③④⑤⑥⑦⑧⑨⑩"
STATUSES = ("OK", "FAIL", "BLOCKED")
CRITERIA_LABELS = ("验收判据", "判据")
EXIT_PASS, EXIT_FAIL, EXIT_DEGRADED, EXIT_USAGE = 0, 1, 2, 4
LEDGER_REL = Path("ledger") / "verdicts.jsonl"
CHINA_TZ = timezone(timedelta(hours=8))

DEFAULT_HANDOFF = Path(os.environ.get("COLLAB_HANDOFF") or r"_handoff")


# --------------------------------------------------------------------------- #
# 最小 schema 校验器（只覆盖 receipt.schema.json 用到的关键字，零依赖）
# --------------------------------------------------------------------------- #
def _type_ok(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate(instance: object, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    if "type" in schema and not _type_ok(instance, schema["type"]):
        errors.append(f"{path}: expected {schema['type']}, got {type(instance).__name__}")
        return errors
    if isinstance(instance, str) and "pattern" in schema:
        if re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']!r}")
    if isinstance(instance, int) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors += validate(item, schema["items"], f"{path}[{index}]")
    return errors


# --------------------------------------------------------------------------- #
# 卡片解析（看板 = 任务语义权威）
# --------------------------------------------------------------------------- #
def _parse_bullet(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*[-*]\s+(.*)$", line)
    if match is None:
        return None
    body = match.group(1).strip()
    if body.startswith("**"):
        end = body.find("**", 2)
        if end > 0:
            label = body[2:end].strip()
            rest = body[end + 2:].lstrip()
            if rest[:1] in (":", "："):
                rest = rest[1:]
            return label, rest.strip()
    parts = re.split(r"[:：]", body, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return None


def card_block(board_text: str, card_id: str) -> str | None:
    pattern = re.compile(rf"(?m)^#{{2,4}}[ \t]+{re.escape(card_id)}(?![0-9A-Za-z_.-])")
    match = pattern.search(board_text)
    if match is None:
        return None
    rest = board_text[match.end():]
    end = re.search(r"(?m)^#{2,4}[ \t]", rest)
    return rest[: end.start()] if end else rest


def _bullet_indent(line: str) -> int | None:
    match = re.match(r"^(\s*)[-*]\s+\S", line)
    if match is None:
        return None
    return len(match.group(1).expandtabs(4))


def field_text(block: str, labels: tuple[str, ...]) -> str:
    """取某个字段的正文：同一行的剩余文本 + **更深的缩进续行**。

    2026-10-16 修：第一版只看同一行，于是「- 验收判据：」下面挂子列表（①②③ 各占一行）的卡
    被解析成空判据——X2 实测 `criteria_sha256=null`，复核只能给 DEGRADED。现在字段正文 =
    同行剩余 + 比该字段缩进更深的后续行，遇到同级或更浅的 bullet 即结束。
    """
    out: list[str] = []
    capturing = False
    field_indent = 0
    for line in block.splitlines():
        parsed = _parse_bullet(line)
        indent = _bullet_indent(line)
        if parsed is not None:
            label, rest = parsed
            if capturing:
                if indent is None or indent <= field_indent:
                    capturing = False
                    if label in labels:
                        capturing = True
                        field_indent = indent if indent is not None else 0
                        out.append(rest)
                    continue
                out.append(line.strip())
                continue
            if label in labels:
                capturing = True
                field_indent = indent if indent is not None else 0
                out.append(rest)
            continue
        if capturing and line.strip():
            out.append(line.strip())
    return "\n".join(part for part in out if part).strip()


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def marker_ids(text: str) -> list[int]:
    found = [(text.index(ch), index + 1) for index, ch in enumerate(MARKERS) if ch in text]
    return [identifier for _, identifier in sorted(found)]


# --------------------------------------------------------------------------- #
# 结构
# --------------------------------------------------------------------------- #
@dataclass
class CriterionResult:
    id: int
    verdict: str
    level: str
    detail: list[str] = field(default_factory=list)


@dataclass
class Report:
    status: str
    reasons: list[str] = field(default_factory=list)
    criteria: list[CriterionResult] = field(default_factory=list)
    residue: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "DEGRADED": EXIT_DEGRADED}[self.status]


def check_evidence(evidence: dict) -> tuple[str, str]:
    """单件物证 -> (verified | attested | failed, 说明)"""
    kind = evidence.get("kind")
    if kind == "file":
        raw = evidence.get("path")
        if not isinstance(raw, str) or not raw:
            return "failed", "file 物证缺 path"
        path = Path(raw)
        if not path.is_file():
            return "failed", f"声称存在的文件不存在：{raw}"
        size = path.stat().st_size
        if "size" in evidence and evidence["size"] != size:
            return "failed", f"size 不符：{raw} 实际 {size} ≠ 声称 {evidence['size']}"
        if "sha256" in evidence:
            actual = sha256_file(path)
            if actual != evidence["sha256"]:
                return "failed", f"sha256 不符：{raw} 实际 {actual} ≠ 声称 {evidence['sha256']}"
            return "verified", f"文件复算通过：{raw}（{size} B）"
        return "attested", f"文件存在但未声明哈希：{raw}（{size} B）"
    if kind == "inline":
        raw = evidence.get("content")
        if not isinstance(raw, str):
            return "failed", "inline 物证缺 content（内容原文）"
        claimed = evidence.get("sha256")
        if not isinstance(claimed, str) or not claimed:
            return "failed", "inline 物证缺 sha256（内容哈希，无法核对）"
        body = raw.encode("utf-8")
        # 2026-10-16 修：工人按「文件字节（含尾换行）」算 sha256/size，却在 content 里省掉了尾换行，
        # 于是逐字比对必然不符（X2 实测：号称 66f519… 实算 da7add…）。两种约定都接受，
        # 但只差一个尾换行；文件仍在时以**文件字节**为准（最强证据）。
        raw_path = evidence.get("path")
        if isinstance(raw_path, str) and raw_path and Path(raw_path).is_file():
            actual = sha256_file(Path(raw_path))
            if actual != claimed:
                return "failed", f"文件字节与声称哈希不符：{raw_path} 实际 {actual} ≠ 声称 {claimed}"
            size = Path(raw_path).stat().st_size
            return "verified", f"按文件字节复算一致：{raw_path}（{size} B，sha256={actual[:16]}…）"
        exact = hashlib.sha256(body).hexdigest()
        padded = hashlib.sha256(body + b"\n").hexdigest() if not raw.endswith("\n") else exact
        if claimed not in (exact, padded):
            return "failed", f"inline 内容哈希不符：实际 {exact} ≠ 声称 {claimed}"
        size_claim = evidence.get("size")
        if isinstance(size_claim, int) and size_claim not in (len(body), len(body) + 1):
            return "failed", f"inline 内容字节数不符：实际 {len(body)} ≠ 声称 {size_claim}"
        note = "" if claimed == exact else "（按「文件字节含尾换行」的约定归一）"
        return "verified", f"内容复算通过{note}（{len(body)} B，sha256={claimed[:16]}…）"
    if kind == "absent":
        raw = evidence.get("path")
        if not isinstance(raw, str) or not raw:
            return "failed", "absent 物证缺 path"
        if Path(raw).exists():
            return "failed", f"声称不存在但实际存在：{raw}"
        if not evidence.get("command"):
            return "attested", f"缺失已复查，但未记录检查命令：{raw}"
        return "verified", f"缺失复查通过：{raw}"
    if kind == "command":
        if not evidence.get("command"):
            return "failed", "command 物证缺 command 原文"
        short = str(evidence["command"]).replace("\n", " ")[:90]
        if "exit_code" in evidence:
            return "attested", f"命令未复跑（自报 exit={evidence['exit_code']}）：{short}"
        return "attested", f"命令未复跑（未记 exit_code）：{short}"
    if kind == "note":
        return "attested", "note 类物证（无机器可验主张）"
    return "failed", f"未知物证类型：{kind!r}"


# --------------------------------------------------------------------------- #
# 复核主流程
# --------------------------------------------------------------------------- #
def assess(
    *,
    card: str,
    knock: str,
    board_path: Path,
    outbox: Path,
    receipt_md: Path | None = None,
    receipt_json: Path | None = None,
    schema_path: Path | None = None,
) -> Report:
    report = Report(status="DEGRADED")
    md_path = receipt_md or outbox / f"{card}-receipt.md"
    json_path = receipt_json or outbox / f"{knock}.receipt.json"
    envelope_path = outbox / f"{knock}.history.json"
    report.facts.update(
        card=card, knock=knock, board=str(board_path), outbox=str(outbox),
        receipt_md=str(md_path), receipt_json=str(json_path), envelope=str(envelope_path),
    )

    # 1. 看板卡片与判据
    if not board_path.is_file():
        report.reasons.append(f"看板不存在：{board_path}")
        return report
    block = card_block(board_path.read_text(encoding="utf-8", errors="replace"), card)
    if block is None:
        report.reasons.append(f"看板里找不到卡片 {card}")
        return report
    criteria_text = field_text(block, CRITERIA_LABELS)
    if not criteria_text:
        report.reasons.append(f"卡片 {card} 没有验收判据")
        return report
    card_ids = marker_ids(criteria_text)
    card_hash = sha256_text(criteria_text)
    report.facts.update(criteria_sha256=card_hash, criteria_ids=card_ids, criteria_text=criteria_text)
    if not card_ids:
        report.reasons.append("判据里没有 ①…⑩ 编号，无法逐条对账")

    # 2. 敲门信封（判据漂移检测）
    drift = False
    if not envelope_path.is_file():
        report.reasons.append(f"缺敲门信封：{envelope_path.name} ⇒ 无法验证判据基线")
        drift = True
    else:
        try:
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            report.reasons.append(f"信封不可读：{error}")
            envelope = {}
        report.facts["envelope"] = {k: envelope.get(k) for k in ("knock_id", "card", "criteria_sha256", "target_session", "issued_at")}
        if envelope.get("card") not in (None, card):
            report.reasons.append(f"信封卡号 {envelope.get('card')!r} 与请求 {card!r} 不一致")
            drift = True
        recorded = envelope.get("criteria_sha256")
        if recorded is None:
            report.reasons.append("信封未记录 criteria_sha256 ⇒ 无法验证判据基线")
            drift = True
        elif recorded != card_hash:
            report.reasons.append(f"判据漂移：看板现文本 sha256={card_hash[:12]}… ≠ 信封发出时 {str(recorded)[:12]}…")
            drift = True

    # 3. 人读回执（与结构化块的冲突检测）
    if not md_path.is_file():
        report.reasons.append(f"缺人读回执：{md_path.name}")
        md_text = ""
    else:
        md_text = md_path.read_text(encoding="utf-8", errors="replace")
        md_knock = re.search(r"(?m)^\s*KNOCK\s*[:：]\s*(\S+)\s*$", md_text)
        md_status = re.search(r"(?m)^\s*STATUS\s*[:：]\s*(OK|FAIL|BLOCKED)\s*$", md_text)
        if md_knock is None:
            report.reasons.append("人读回执缺少 `KNOCK: <id>` 行 ⇒ 无法确认它属于本次敲门")
        elif md_knock.group(1) != knock:
            report.reasons.append(f"人读回执 KNOCK={md_knock.group(1)} 与本次 {knock} 不符")
        report.facts["md_status"] = md_status.group(1) if md_status else None
        if md_status is None:
            report.reasons.append("人读回执缺少 `STATUS: OK|FAIL|BLOCKED` 行")

    # 4. 机器回执 + schema
    payload: dict = {}
    if not json_path.is_file():
        report.reasons.append(f"缺机器回执：{json_path.name}")
    else:
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            report.reasons.append(f"机器回执不可读：{error}")
            payload = {}
        else:
            if schema_path is not None and schema_path.is_file():
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                errors = validate(payload, schema)
                if errors:
                    report.reasons.append("机器回执不符 schema：" + "；".join(errors[:6]))
                    report.status = "FAIL"
                    return report
            if payload.get("knock_id") != knock:
                report.reasons.append(f"机器回执 knock_id={payload.get('knock_id')!r} 与本次 {knock!r} 不符")
                report.status = "FAIL"
                return report
            if payload.get("card") != card:
                report.reasons.append(f"机器回执 card={payload.get('card')!r} 与本次 {card!r} 不符")
                report.status = "FAIL"
                return report

    if not payload:
        report.reasons.append("没有机器回执 ⇒ 只能判 DEGRADED（不假装复核过）")
        return report

    # 5. 散文 vs 结构化块：冲突即失败（不设“以 JSON 为准”的静默规则）
    json_status = payload.get("status")
    md_status = report.facts.get("md_status")
    if md_status is not None and md_status != json_status:
        report.reasons.append(f"散文与结构化块冲突：.md STATUS={md_status} ≠ .json status={json_status}")
        report.status = "FAIL"
        return report

    # 6. 覆盖面
    receipt_ids = [item.get("id") for item in payload.get("criteria", []) if isinstance(item, dict)]
    if sorted(x for x in receipt_ids if isinstance(x, int)) != sorted(card_ids):
        report.reasons.append(f"判据覆盖面不符：看板 {card_ids} vs 回执 {receipt_ids}")

    # 7. 逐条物证
    failed = False
    attested = bool(drift)
    for item in payload.get("criteria", []):
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        verdict = item.get("verdict")
        details: list[str] = []
        levels: list[str] = []
        for evidence in item.get("evidence", []) or []:
            if not isinstance(evidence, dict):
                levels.append("failed")
                details.append("物证不是对象")
                continue
            level, note = check_evidence(evidence)
            levels.append(level)
            details.append(note)
        if "failed" in levels:
            level = "failed"
        elif levels and all(x == "verified" for x in levels):
            level = "verified"
        elif "verified" in levels:
            level = "partial"
        else:
            level = "attested"
        if verdict != "pass":
            level = "failed"
            details.append(f"判据自报 verdict={verdict!r}（未通过）")
        if level == "failed":
            failed = True
        elif level != "verified":
            attested = True
        report.criteria.append(CriterionResult(id=cid if isinstance(cid, int) else -1, verdict=str(verdict), level=level, detail=details))

    # 8. 残留
    for entry in payload.get("residue", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            report.residue.append("残留项缺 path")
            failed = True
            continue
        if Path(raw).exists():
            report.residue.append(f"残留仍在：{raw}")
            failed = True
        else:
            report.residue.append(f"残留复查通过（不存在）：{raw}")

    # 9. 总判
    if failed:
        report.status = "FAIL"
    elif attested or drift or any("覆盖面" in r or "信封" in r or "KNOCK" in r for r in report.reasons):
        report.status = "DEGRADED"
    else:
        report.status = "PASS"
    return report


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
LEVEL_LABEL = {"verified": "独立复算", "partial": "核心复算+自报补充", "attested": "仅自报", "failed": "矛盾"}


def ledger_record(report: Report, *, card: str, knock: str, handoff: Path,
                  receipt_md: Path | None, receipt_json: Path | None,
                  operator: str = "A") -> dict:
    """复核结论的审计记录（R14：结论进 git，回执正文留 outbox）。"""
    def digest(path: Path | None) -> dict | None:
        if path is None or not path.is_file():
            return None
        return {"path": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}

    return {
        "at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
        "operator": operator,
        "card": card,
        "knock_id": knock,
        "status": report.status,
        "exit_code": report.exit_code,
        "reasons": list(report.reasons),
        "criteria": [{"id": c.id, "verdict": c.verdict, "level": c.level} for c in report.criteria],
        "residue": list(report.residue),
        "criteria_sha256": report.facts.get("criteria_sha256"),
        "receipts": [item for item in (digest(receipt_md), digest(receipt_json)) if item],
        "board_sha256": sha256_file(handoff / "BOARD.md") if (handoff / "BOARD.md").is_file() else None,
    }


def append_ledger(handoff: Path, record: dict) -> Path:
    path = handoff / LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def print_report(report: Report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(
            {
                "status": report.status,
                "exit_code": report.exit_code,
                "reasons": report.reasons,
                "criteria": [c.__dict__ for c in report.criteria],
                "residue": report.residue,
                "facts": report.facts,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return
    facts = report.facts
    print(f"复核：卡 {facts.get('card')} / 敲门 {facts.get('knock')}")
    print(f"  看板判据  sha256={str(facts.get('criteria_sha256'))[:16]}…  编号={facts.get('criteria_ids')}")
    for criterion in report.criteria:
        print(f"  [{LEVEL_LABEL.get(criterion.level, criterion.level)}] 判据 {criterion.id}（自报 {criterion.verdict}）")
        for line in criterion.detail:
            print(f"      · {line}")
    for line in report.residue:
        print(f"  残留：{line}")
    for reason in report.reasons:
        print(f"  ! {reason}")
    print(f"结论：{report.status}（exit {report.exit_code}）")


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #
def selftest() -> int:
    cases: list[tuple[str, dict, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outbox = root / "outbox"
        outbox.mkdir()
        probe = root / "probe.txt"
        probe.write_bytes(b"PROBE\n")
        probe_sha = sha256_file(probe)
        probe_size = probe.stat().st_size
        board = root / "BOARD.md"
        criteria = "①探针文件存在且哈希可复算 ②探针目录里没有残留"
        board.write_text(
            "## Review\n\n### T1 自测卡\n"
            "- 负责人：W\n"
            f"- 验收判据：{criteria}\n"
            "- 证据：\n- 状态：Review\n",
            encoding="utf-8",
        )
        schema = Path(__file__).resolve().parents[1] / "references" / "receipt.schema.json"
        envelope_path = outbox / "<id>.history.json"
        md_path = outbox / "T1-receipt.md"
        json_path = outbox / "<id>.receipt.json"

        def build(sha=probe_sha, status="OK", md_status="OK", envelope_hash=sha256_text(criteria), evidence2=None, evidence1=None):
            envelope_path.write_text(json.dumps({
                "knock_id": "<id>", "card": "T1",
                "criteria_sha256": envelope_hash, "target_session": "session-selftest",
                "issued_at": "<时间>",
            }, ensure_ascii=False), encoding="utf-8")
            md_path.write_text(f"# T1 回执\n\nKNOCK: <id>\nSTATUS: {md_status}\n\n散文部分。\n", encoding="utf-8")
            default1 = [{"kind": "file", "path": str(probe), "sha256": sha, "size": probe_size}]
            json_path.write_text(json.dumps({
                "schema_version": "1.0", "knock_id": "<id>", "card": "T1", "round": 1,
                "session": "session-selftest", "status": status,
                "criteria": [
                    {"id": 1, "verdict": "pass", "evidence": evidence1 or default1},
                    {"id": 2, "verdict": "pass", "evidence": evidence2 or [
                        {"kind": "absent", "path": str(root / "gone.txt"), "command": f"Test-Path {root / 'gone.txt'}", "result": "False"}]},
                ],
                "residue": [],
            }, ensure_ascii=False), encoding="utf-8")

        def run() -> Report:
            return assess(card="T1", knock="<id>", board_path=board, outbox=outbox,
                          receipt_md=md_path, receipt_json=json_path, schema_path=schema)

        inline_ok = {"kind": "inline", "content": "PROBE\n", "sha256": hashlib.sha256(b"PROBE\n").hexdigest(), "size": 6}
        build()
        cases.append(("全部可独立复算 → PASS", {"report": run()}, EXIT_PASS))
        build(sha="0" * 64)
        cases.append(("哈希不符 → FAIL", {"report": run()}, EXIT_FAIL))
        build(evidence2=[{"kind": "command", "command": "Get-Date", "exit_code": 0}])
        cases.append(("只有命令自报 → DEGRADED", {"report": run()}, EXIT_DEGRADED))
        build(evidence1=[inline_ok, {"kind": "command", "command": "Get-FileHash x", "exit_code": 0}])
        cases.append(("核心复算 + 自报补充 → DEGRADED(partial)", {"report": run()}, EXIT_DEGRADED))
        build(evidence1=[{"kind": "inline", "content": "PROBE\n", "sha256": "0" * 64}])
        cases.append(("inline 内容哈希不符 → FAIL", {"report": run()}, EXIT_FAIL))
        build(md_status="FAIL")
        cases.append(("散文与 JSON 冲突 → FAIL", {"report": run()}, EXIT_FAIL))
        build(envelope_hash="1" * 64)
        cases.append(("判据漂移 → DEGRADED", {"report": run()}, EXIT_DEGRADED))

        failures = 0
        for title, payload, expected in cases:
            report = payload["report"]
            ok = report.exit_code == expected
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {title}: {report.status}（期望 exit {expected}）")
            if not ok:
                print(f"        reasons={report.reasons}")
        if failures:
            print(f"SELFTEST FAILED: {failures}/{len(cases)}")
            return 1
        print(f"SELFTEST OK: {len(cases)}/{len(cases)}")
        return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回执复核器（三态：PASS / FAIL / DEGRADED）")
    parser.add_argument("--card", help="卡号，如 W5")
    parser.add_argument("--knock", help="敲门 id，如 <id>")
    parser.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF, help="交接区目录（默认 %%COLLAB_HANDOFF%% 或 _handoff）")
    parser.add_argument("--board", type=Path, help="看板路径（默认 <handoff>\\BOARD.md）")
    parser.add_argument("--outbox", type=Path, help="回执目录（默认 <handoff>\\outbox）")
    parser.add_argument("--receipt-md", type=Path)
    parser.add_argument("--receipt-json", type=Path)
    parser.add_argument("--schema", type=Path, help="receipt.schema.json 路径（默认技能内 references/）")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    parser.add_argument("--no-ledger", action="store_true",
                        help="不把本次结论追加进 <handoff>/ledger/verdicts.jsonl（默认追加，R14）")
    parser.add_argument("--operator", default="A", help="复核人代号（默认 A）")
    parser.add_argument("--print-card-facts", action="store_true",
                        help="只打印卡片判据的事实（sha256 / 编号 / 原文），供敲门器组信封用")
    parser.add_argument("--selftest", action="store_true", help="跑内置自测（5 例）")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.print_card_facts:
        if not args.card:
            print("需要 --card", file=sys.stderr)
            return EXIT_USAGE
        board = args.board or args.handoff / "BOARD.md"
        if not board.is_file():
            print(json.dumps({"error": f"board not found: {board}"}, ensure_ascii=False))
            return EXIT_DEGRADED
        block = card_block(board.read_text(encoding="utf-8", errors="replace"), args.card)
        if block is None:
            print(json.dumps({"error": f"card not found: {args.card}"}, ensure_ascii=False))
            return EXIT_DEGRADED
        text = field_text(block, CRITERIA_LABELS)
        owner = field_text(block, ("负责人",))
        owner_line = owner.splitlines()[0].strip() if owner else ""
        owner_code = re.match(r"([A-Za-z][A-Za-z0-9_.-]*)", owner_line)
        print(json.dumps(
            {
                "card": args.card,
                "criteria_sha256": sha256_text(text) if text else None,
                "criteria_ids": marker_ids(text),
                "criteria_text": text,
                "owner": owner_line or None,
                "owner_code": owner_code.group(1) if owner_code else None,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return EXIT_PASS if text else EXIT_DEGRADED
    if not args.card or not args.knock:
        print("需要 --card 与 --knock（或用 --selftest）", file=sys.stderr)
        return EXIT_USAGE

    schema = args.schema or Path(__file__).resolve().parents[1] / "references" / "receipt.schema.json"
    report = assess(
        card=args.card,
        knock=args.knock,
        board_path=args.board or args.handoff / "BOARD.md",
        outbox=args.outbox or args.handoff / "outbox",
        receipt_md=args.receipt_md,
        receipt_json=args.receipt_json,
        schema_path=schema,
    )
    print_report(report, args.json)
    if not args.no_ledger:
        handoff = args.handoff
        record = ledger_record(
            report,
            card=args.card,
            knock=args.knock,
            handoff=handoff,
            receipt_md=args.receipt_md or (args.outbox or handoff / "outbox") / f"{args.card}-receipt.md",
            receipt_json=args.receipt_json or (args.outbox or handoff / "outbox") / f"{args.knock}.receipt.json",
            operator=args.operator,
        )
        path = append_ledger(handoff, record)
        print(f"已记账：{path}（{report.status}）")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
