# 回执与复核契约 v1

> 这份文件是**契约**：看板卡把「怎样算完成」写成判据，工人按本契约交回执，复核器（`scripts/verify_receipt.py`）独立复算能复算的部分，给一个三态结论。
> 它解决的是 W1–W5 暴露的三件真事：①散文回执**没法当复核接口**（W1 1,978 B → W5 14,484 B，约 2 KB/判据）；②同一份回执里「逐字」引用的代码会被插值污染；③声称的哈希没人复算就等于没声称。

## 1. 角色与不变量

| 名词 | 含义 |
| --- | --- |
| 判据 | 看板卡上「怎样算完成」的可证伪陈述，编号 ①…⑩（对应 `criteria[].id` = 1…n） |
| 敲门信封 | `outbox\<knock_id>.history.json`：发出时固化的判据基线 + 目标会话 + execution fact |
| 回执两件套 | 人读 `<card>-receipt.md` + 机器 `<knock_id>.receipt.json` |
| 物证 | 回执里对每条判据给出的、**可被第三方复算**的主张（文件哈希 / 内容哈希 / 缺失复查 / 命令自报） |
| 判据漂移 | 看板卡的判据文本在回执前被改过 ⇒ 信封里的 `criteria_sha256` 对不上 ⇒ 本次回执无法对账（DEGRADED） |

三条不变量：

1. **只有声明了哈希的主张才算「可被证伪」**。散文、截图、口头结论都只是线索。
2. **散文与结构化块冲突 = 复核失败**。不设「以 JSON 为准」的静默规则——那会奖励把散文写虚。
3. **复核器只报告，不阻断**（与 ADR 0027 决策 8 一致）。判 FAIL 而人仍判 Done，必须在卡上写明理由；**阻断的是「默默不阻断」**。

## 2. 敲门信封（`knock.ps1` v2 写入）

```json
{
  "knock_id": "<敲门 id>",
  "card": "W6",
  "target_session": "cron-exec-worker_boot-...",
  "issued_at": "<时间>",
  "criteria_sha256": "<卡片判据文本的 utf8-lf 归一化 sha256>",
  "criteria_ids": [1, 2],
  "card_owner": "W（常驻 worker 会话 `...`）",
  "provider": "custom-workbuddy", "model": "cn:deepseek-v4.1-flash",
  "task_kept": true,
  "history": { "status": "success", "sessionId": "...", "durationMs": 200000, "error": null },

  "timeout_seconds": 900, "max_retries": 0, "budget_tokens": 1500000,
  "delivery": "delivered", "queued_at": null, "queue_deadline": null,
  "delivered_at": "<时间>",
  "prompt": "<投递用的完整提示词（排队后重建任务体要用）>",
  "task_template": { "schedule": "in 1m", "type": "llm", "cwd": "...",
                     "permissionPreset": "workspace-write", "provider": "custom-workbuddy",
                     "model": "cn:deepseek-v4.1-flash", "overlapPolicy": "skip" }
}
```

- **R52 新增字段（2026-10-16）**：`delivery` 三态 `pending`/`delivered`/`abandoned`；`pending` 表示「信封已写、任务未投」（排队，见 §7.10），**老信封没有该字段＝当时同步投递，按已投处理**；`queued_at`/`queue_deadline` 只在排队时出现（`queue_deadline` 默认 4 小时）；`delivered_at` 是投递成功的机器时间；`timeout_seconds`/`max_retries` 进任务体（`timeoutSeconds`/`maxRetries`），**超时到点中止是唯一的止损手段**；`budget_tokens` 只进账（`budget` 按卡汇总）不进任务体；`prompt`/`task_template` 让 `collect` 能原样重建任务体，不必回头读卡。
- 判据哈希由 `verify_receipt.py --card <卡> --print-card-facts` 计算（**与复核器同源**：CRLF→LF、逐行去尾空白、首尾去空白），敲门器不自己实现第二套归一化。
- **cron 任务保留到复核完成**（`task_kept: true`）。任务本身就是 execution fact 的取证窗口；复核后收口：`knock.ps1 -Card <卡> -DeleteTask -TaskId <knock_id>`。
- 负责人绑定：卡上「负责人」代号必须与目标会话在 `BOARD.md` 常驻 worker 表里的代号一致，否则拒投（`-Force` 可越权，但会在日志里留痕）。

## 3. 回执两件套

### 3.1 人读 `<card>-receipt.md`

必须含两行（复核器按行解析）：

```text
KNOCK: <敲门 id>
STATUS: OK
```

`STATUS ∈ {OK, FAIL, BLOCKED}`，且**必须与机器回执的 `status` 逐字一致**。散文部分自由：讲清做了什么、命令原文（**用不插值的单引号 here-string**，否则「逐字」不成立）、失败与自查订正、边界（没做什么）。**历史轮次全部留在 `.md` 里**，机器回执只写本轮。

### 3.2 机器 `<knock_id>.receipt.json`

必须过 [`references/receipt.schema.json`](receipt.schema.json)。最小形状：

```json
{
  "schema_version": "1.0",
  "knock_id": "<敲门 id>",
  "card": "W6",
  "round": 1,
  "session": "cron-exec-worker_boot-...",
  "status": "OK",
  "criteria": [
    {"id": 1, "verdict": "pass", "note": "…",
     "evidence": [{"kind": "inline", "content": "…", "sha256": "<64位小写>", "size": 6}]},
    {"id": 2, "verdict": "pass",
     "evidence": [{"kind": "absent", "path": "…", "command": "Test-Path …", "result": "False"}]}
  ],
  "residue": []
}
```

- `criteria[].id` 必须与看板判据编号 **一一对应**（缺一条、多一条都判 DEGRADED）。
- `verdict ∈ {pass, fail, na}`；只要不是 `pass`，整卡判 FAIL（卡不该进 Done）。
- **同卡多轮**：`round` 递增，只有**最后一轮**能进 Done；历史轮次留在 `.md`，不许覆盖。
- `residue[]` 里列出的路径，复核器会逐个复查**必须不存在**。

### 3.3 物证类型（`evidence[].kind`）

| kind | 必填 | 复核器做什么 | 结果等级 |
| --- | --- | --- | --- |
| `file` | `path`；建议 `sha256`+`size` | 复查存在性、重算 sha256、比字节数 | `verified`（带哈希）／`attested`（无哈希） |
| `inline` | `content` + `sha256`（可选 `size`；文件仍在时建议再给 `path`） | **对内容原文重算 sha256**——探针删掉后仍可复算，这是「临时产物」的正确记法。`content` 必须与 `sha256`/`size` 自洽：哈希按**文件字节**算（含尾换行就把它算进去）。只差一个尾换行（`content` 省了、哈希算了）时，复核器按约定归一并在结论里注明；若给了 `path` 且文件仍在，则**以文件字节为准**（更强证据） | `verified` |
| `absent` | `path`；建议 `command` | 复查该路径确实不存在 | `verified`（带命令）／`attested`（无命令） |
| `command` | `command`；建议 `exit_code` | **不复跑**任意命令，只记「自报」 | `attested` |
| `note` | — | 无机器可验主张 | `attested` |

判据级等级：`failed`（任一物证矛盾）> `verified`（全部物证都被复算）> `partial`（核心复算 + 自报补充）> `attested`（全为自报）。

## 4. 三态结论与退出码

| 结论 | 条件 | 退出码 |
| --- | --- | --- |
| `PASS` | 每条判据都 `verified`，无残留，信封与 `.md` 都对得上 | 0 |
| `FAIL` | 物证矛盾／判据自报未通过／有残留／`.md` 与 `.json` 冲突／schema 不过／id 不匹配 | 1 |
| `DEGRADED` | 没有失败，但有无法独立复算的项（命令自报、缺信封、判据漂移、覆盖面缺口） | 2 |
| 用法错误 | 缺 `--card`/`--knock` | 4 |

用法：

```powershell
$py = "<业务仓库>\.venv-311\Scripts\python.exe"
& $py "<技能目录>\scripts\verify_receipt.py" --card W6 --knock <敲门 id>
& $py "<技能目录>\scripts\verify_receipt.py" --card W6 --knock <敲门 id> --json   # 机器可读
& $py "<技能目录>\scripts\verify_receipt.py" --selftest                                 # 7 例回归
```

> `DEGRADED` 不是「没事」：它精确地说「我复算了能复算的，剩下的只能采信」。够不够进 Done 由复核人裁量并写进卡里。

### 4.1 物证怎么挑（2026-10-15 实测订正）

一条判据的物证，要选**记下去之后不会自己变**的那种。同一张卡里这两类文件必须分开处理：

| 物证对象 | 正确记法 | 为什么 |
| --- | --- | --- |
| 单卡产物（`<card>-receipt.md`、探针内容、被检查的仓库文件） | `kind="file"` + `sha256` + `size` | 对象本身就该冻结 |
| **共享的活文件**（`BOARD.md`、`LOG.md`） | `kind="inline"` + **有界切片**（卡片块 / 该卡那一行） | 每张卡都会改它 ⇒ 整文件哈希记下去那刻起就开始过期 |

翻车实例：W1 的首版机器回执把 `BOARD.md` 记成整文件 `file` 物证（`size=59311`），随后别的卡把看板写到 61,039 B ⇒ 复核器判 `size 不符` ⇒ **整卡 FAIL**（不是判据不成立，是物证选错了）。改成卡片块切片后 W1 立刻回到 **PASS**。

配套：复核器取看板切片用的是「命中 `^#{2,4} <卡号>` 到下一个 `#{2,4}` 标题」，所以**往卡片块里追加散文不会让判据漂移**（判据文本取的是 `- 验收判据：…` 那一段）。

判据的**写法**两种都支持（2026-10-16 起）：写在字段同一行（`- 验收判据：①… ②…`），或写成它下面**缩进更深**的子列表（`- 验收判据：` + `  - ①…` 各占一行）。第二版解析只读同一行，于是子列表写法的卡被判成「没有验收判据」——X2 卡实测 `criteria_sha256=null`，复核只能给 DEGRADED。修好后更深的缩进续行会被收进判据正文，遇到同级或更浅的字段行即结束；X1 的旧哈希在修复前后逐字不变（回归已验）。

## 4.2 六张 W 卡的复核状态（2026-10-15 复核，B 执行）

| 卡 | 命令 | 结论 | 说明 |
| --- | --- | --- | --- |
| W1 | `--card W1 --knock <敲门 id>` | **PASS** | 证据模型订正后三条判据全部 `verified` |
| W2 | `--card W2 --knock <敲门 id>` | **PASS** | 同上 |
| W3 | `--card W3 --knock <敲门 id>` | **FAIL** | 判据①②③ 自报未通过 ⇒ 如实 FAIL；价值在机制定位，修复由 W4 实证 |
| W4 | `--card W4 --knock <敲门 id>` | **DEGRADED** | 判据②（no-op 追加）不可事后复算 ⇒ T2 |
| W5 | `--card W5 --knock <敲门 id>` | **DEGRADED** | 判据⑥（目录列表替代口径）是自报 ⇒ T2 |
| W6 | `--card W6 --knock <敲门 id>` | **PASS** | 信封补上后从 DEGRADED 变 PASS，三态区分生效 |

六卡均 `residue=[]`、`reasons=[]`、无判据漂移；复核器 `--selftest` 7/7。

## 4.3 控制台时代的三张试验卡（2026-10-16 复核，A 执行）

| 卡 | 敲门 | 结论 | 说明 |
| --- | --- | --- | --- |
| X1 | `<敲门 id>` | **PASS** | 全流程走控制台（异步派活 + 回执 + 复核 + 记账 + 收口），判据①② 全部独立复算 |
| X2 | `<敲门 id>` | **DEGRADED**（已受理降级） | 判据① 按文件字节复算通过；判据②③ 物证是 git 命令（仅自报）；收口时由发起方独立跑了工作树审计（diff 只有 `A probe.md`、`status` 干净、越界 0），并把理由写进卡 |

X2 是一次真实的「改造后能力验收」：它同时验了三件事——**会话轮转**（W 换到新会话仍被敲门唤醒）、**一卡一工作树**（任务 cwd 指向 `_worktrees\X2-011454`，主检出不动）、**工作集声明**（声明 `probe.md`，收口核对越界 0 项）。它也是这套东西第一次**先失败后修好**：第一版工作树落在 `%TEMP%`（沙箱可写根之外），工人写不进去、申请提权、无人批准 → 挂到超时；改到 `_worktrees\` 并加派发前落点守卫后，同样的卡一次跑通。

## 5. 验收档位与三段式升级

- **T3（可复核档）**：判据的物证以 `file`/`inline`/`absent` 为主 ⇒ 复核器能给出 `PASS`。**能做 T3 就做 T3**：把临时产物用 `inline` 记内容哈希，把「已清理」用 `absent` 记复查。
- **T2（信息档）**：结论只依赖命令自报或外部系统 ⇒ 复核器多半给 `DEGRADED`，需要在卡上写明「为什么这轮只能到 T2」。
- **三段式升级**：工人 `BLOCKED` → 升级给 A；A 也 blocked → 只在 `BOARD.md` 的「等业主」区留**一个问题**。不无限往下猜。
- **超时**：敲门后 2 小时没回执，用 `<交接区>\tools\wait-for-receipt.ps1` 盯；超时它写 `<knock_id>.timeout.md` + `LOG.md` 一行，**不擅自改卡、不擅自重敲**。

## 6. 两套看板的权威（单向投影）

- `BOARD.md` = **任务语义权威**：做什么、谁做、怎样算完成、证据是什么。
- DSH 任务看板台账 = **只读的执行事实附件**：哪张卡在什么时候被谁跑过。**不写回、不同步、不进门禁**；两处不一致时以 `BOARD.md` 为准，并在 `LOG.md` 提改单。

## 7. 技能源 ↔ 安装副本

技能源 `<技能源>\skill\agent-collab-runbook\` 是**唯一事实源**；`~\.agents\skills\`、`~\.claude\skills\` 下的是安装副本，**会悄悄落后**（实测：`acceptance-gates.md` 源 17,652 B vs 副本 16,247 B）。`skillshare install` 是**复制**、没有链接模式，所以**改完源必须重装**，否则全局源与所有 CLI 落点继续服务旧字节（而且是静默的）。

一条命令做完「重装 → 分发 → 复验」：

```powershell
& '<技能源>\tools\publish-skill.ps1'   # 0 一致 / 3 仍漂移 / 1 失败 / 4 缺工具
& $py "<技能目录>\scripts\collab_runbook.py" --sync-check          # 逐文件哈希比对，drifted ⇒ exit 3
```

`--sync-check` 的落点集合取自 `%APPDATA%\skillshare\config.yaml`（逐个 target 的 `… → skills → path`）；`codex` 有 include 白名单、目录可能根本不存在，那种情况算 `skipped` 不算漂移。发现 `drifted` 就重装；**不自动改副本**——两个写着「唯一事实源」的地方等于没有事实源。

## 8. 复核记账与角色纪律（R14）

复核结论由**发起方**记账：`verify_receipt.py` 默认把一条 JSON 追加进 `_handoff\ledger\verdicts.jsonl`（**进 git**，只增不改；状态仍在 `cards/*.yaml`，账本不是状态源）。一条记录长这样：

```json
{"at":"<时间>","operator":"A","card":"X1","knock_id":"<敲门 id>",
 "status":"PASS","exit_code":0,"reasons":[],
 "criteria":[{"id":1,"verdict":"pass","level":"verified"},{"id":2,"verdict":"pass","level":"verified"}],
 "residue":[],
 "criteria_sha256":"dcb1e479…","board_sha256":"933d9132…",
 "receipts":[{"path":"X1-receipt.md","sha256":"3b59b4ab…","size":3377},
             {"path":"<敲门 id>.receipt.json","sha256":"a6920380…","size":1286}]}
```

三个哈希各钉一头：`criteria_sha256` 钉「被复核的是哪一版判据」，`board_sha256` 钉当时的卡面，`receipts[].sha256` 钉被复核的两件套——合起来让这条结论**可被事后证伪**：重算不一致，就说明卡面/判据/回执在结论之后变过。

**角色纪律（实测踩过）**：工人**不要自己记账**。`operator` 默认写作 `A`，工人自查时若不带 `--no-ledger`，就会把自己的结论记成发起方的结论。2026-10-16 的 X1 卡出现过这个情况：账本前四条里有两条 X1 记录来自 W 自查（00:20:18 / 00:21:01），`operator` 却是 `A`。所以 `dispatch` 写出的 brief 明写「复核由发起方做；自查请加 `--no-ledger`」。凭这条纪律，账本才配当「谁在什么时候、按哪一版判据、认了哪一份回执」的凭据。
