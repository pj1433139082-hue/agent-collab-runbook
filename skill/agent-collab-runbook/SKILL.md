---
name: agent-collab-runbook
description: 让多个智能体会话在同一份业务仓库上安全并行的运行手册：公共交接区协议（BOARD/PLAN/LOG/status.ps1）、工人与监督两个角色的写作用域切分、用任务看板做排班与唤醒（reuseSession + 可改 promptPrefix，文件当信封）、四查验收门禁、远端写逐次授权、一卡一 worktree。与 agent-workflow-framework 搭配使用，只调框架公开 CLI。触发词：多会话协作、两个会话一起干、交接区、看板排班、巡检会话、占用声明、一卡一工作区、collab runbook。
---

# 多会话协作运行手册

两个（或更多）智能体会话要动同一份业务仓库时，用它把「谁在改什么、谁验收、怎么唤醒、什么算做完」变成可执行的文件纪律，而不是靠口头相传。技能只做三件事：**按交接区协议记账、按框架公开 CLI 定位与推进、按门禁验收**；它不自建调度器、消息总线、队列、审计或策略引擎。

## 什么时候用

- 有 ≥2 个会话（或人 + 会话）要改同一个 git 工作树。
- 需要「一个会话干活、另一个会话巡检/验收」的分工。
- 需要把一张卡按时间重复排（如每日巡检），并且要求下一次运行能看见上一次的基线。

只做只读定位或单会话单卡任务时不必用本技能，直接读项目自己的 `AGENTS.md` 即可。

## 冷启动：先恢复事实，不依赖聊天记忆

按顺序做完这五步，任何一步失败就停在那一步如实报告，不猜、不重建、不覆盖：

```powershell
# 0) 定住解释器与工作目录（本手册全部示例沿用）
$py = "<业务仓库>\.venv-311\Scripts\python.exe"
$fw = "<工作根>\agent-workflow-framework"

# 1) 读交接区定位（只读）：一次打印顶端/分支/worktree + PLAN.md 全文 + LOG.md 末尾
& "<交接区>\status.ps1"

# 2) 定位本机框架（只读，不写盘）
& $py "<技能目录>\scripts\collab_runbook.py" --framework $fw --locate

# 3) 读项目任务事实（只读；workspace 一律绝对路径，task-id 与项目声明一致）
Push-Location $fw
& $py -m agent_workflow status  <项目绝对路径> --task-id kickoff
& $py -m agent_workflow context <项目绝对路径> --task-id kickoff
Pop-Location
```

判据：`--locate` 输出 `{"ok": true, "framework_root": "..."}`；`status` 返回 `ok: true` 且能看到 `stage`/`checks`/`pending_decisions`。**只读定位不需要用户重新授权**；`context --write` 与 `advance/decision --apply` 是写操作，只在确有必要时用。

三个当场就会踩的坑（实测）：

- **交接区不存在就先停下**：第 1 步要读的 `_handoff\status.ps1` 若不在本工作区，说明这套协作还没建立，**不要自己造一个** —— 如实报"交接区未建立"并问业主。
- **`--locate` 要给 `--framework`**：本机框架根不是固定值，先有它才能定住 CLI。
- **写输出要显式 UTF-8**：`Set-Content -Encoding UTF8`（在 Windows PowerShell 5.1 上写 `.ps1` 还必须带 BOM，否则中文注释会被当 ANSI 读成乱码并触发语法错误）。

### 唤醒一个空闲会话（可选，前置条件多）

排期是"到点起新会话"；**唤醒**是"投给一个已存在且空闲的会话，让它开新回合"。本机已有封装（`knock.ps1` v2），但硬约束不少，违反就是静默失败：

```powershell
pwsh -File <交接区>\knock.ps1 -SessionId <会话id> -Card <卡号>
# 复核（cron 任务保留到复核完成：任务本身就是 execution fact 的取证窗口）
& $py "<技能目录>\scripts\verify_receipt.py" --card <卡号> --knock <knock_id>
# 复核后收口
pwsh -File <交接区>\knock.ps1 -Card <卡号> -DeleteTask -TaskId <knock_id>
```

- 依赖 `dsh-cron` 的 `targetSessionId` 与**本地补丁**（把任务字段 `sessionId` 映射成 `resumeSessionId`，否则传不下去）。
- **归档会话投不进去**：宿主 `ArchivedSessionGate` 命中即拒，回合以 `blocked` 结束、零 token；投之前查 `~\.dsh\storages\workspace.json` 的 `global.archivedSessionIds`。
- **正在跑的会话不能 resume**：默认拒投，`-Force` 才硬试。
- **负责人绑定**：卡的「负责人」代号必须与目标会话在 `BOARD.md` 常驻 worker 表里登记的代号一致，否则拒投——防止"自己敲门给自己"。
- **判据会被固化**：发出时把卡片判据文本的 sha256 写进信封（`outbox\<knock_id>.history.json`）；回执前若改过判据，复核器判 DEGRADED（判据漂移）。
- **回执要自己拉**：投递单向，不通知发起方；投完自己要回去确认结果。

端到端实测（2026-10-15）：`<id>` → 工人交回执两件套 → 复核器给出 **PASS（exit 0）**，两条判据都被独立复算（内容哈希 + 缺失复查）。细节见 `references/session-scheduling.md` 与 `references/receipt-contract.md`。

### 端到端实测（本机，2026-10-15）

```text
collab_runbook.py --framework <fw> --project <ws> --locate
  -> exit 0  {"ok": true, "framework_root": "...", "project": "...agent-collab-runbook", "marker": true, "declared_task_id": "kickoff"}   # 不写盘
collab_runbook.py --framework <fw> --project <ws> -- status <ws> --task-id kickoff
  -> exit 0  与直接 `python -m agent_workflow status` 输出逐字一致
agent_workflow context <ws> --task-id kickoff --write
  -> exit 0  sources 给出 plan.md 摘要；改过 plan.md 后 plan-acceptance 转 false（设计如此，见 references/acceptance-gates.md 第 4 节）
agent_workflow advance <ws> --task-id kickoff --apply
  -> exit 0  {"from_stage": "plan", "to_stage": "ready", "applied": true}
```

注意 `--locate` 不带 `--framework` 时报 `framework_location_error` 并退 2 —— 定位不到框架时**它不会写盘**，如实报错就是正确行为。

## 主流程：认领 → 干活 → 四查 → 交接 → 收尾

1. **认领一张卡**：读 `BOARD.md`，确认自己没有别的 Doing 卡（每人 WIP = 1），卡上四件齐全（负责人、验收判据、占用文件/分支、依赖），依赖未清不开工。换列后在 `LOG.md` 追加一行。
2. **划工作区**：写卡一卡一个 worktree（见 `references/handoff-protocol.md` 的隔离命令）；只改自己「占用文件」列里的路径。
3. **干活**：只在自己分支上提交；提交用 `git commit -F <信息文件> -- <paths>` 一步走完，不留「已 add 未 commit」的窗口。
4. **四查门禁**：按 `references/acceptance-gates.md` 逐条跑，逐条记 exit code 与关键输出行。四查没过不进 Done。
5. **交接**：把事实与命令写进 `PLAN.md` 交接点（"我落了 X、顶端是 Y、你可以 `git cherry-pick Y`、期望看到 Z"），不写意图。
6. **收尾记账**：`LOG.md` 追加一行；把卡移到 Review，证据齐全且被独立复现后才进 Done。

## 八个运行要素（细节在 references，这里给判据）

| 要素 | 一句话判据 | 细节 |
| --- | --- | --- |
| 公共交接区协议 | `BOARD.md` 五列 + 每卡四件齐全；`PLAN.md` 覆写保留结构；`LOG.md` 只追加且旧行不变 | `references/handoff-protocol.md` |
| 两角色写作用域 | 工人卡 `reuseSession: false`、监督卡 `reuseSession: true`；跨占用先在交接点声明并等确认 | `references/handoff-protocol.md`、`references/session-scheduling.md` |
| 看板排班与唤醒 | 只用看板现有排期能力；信封是文件；「到点出现新回合」才是投递成功的判定线 | `references/session-scheduling.md` |
| 回执与复核 | 回执两件套（`<card>-receipt.md` + `<knock_id>.receipt.json`）；复核器给 `PASS/FAIL/DEGRADED`，散文与 JSON 冲突即 FAIL | `references/receipt-contract.md` |
| 四查验收 | 四条命令各自 exit 0 且输出符合判据 | `references/acceptance-gates.md` |
| 远端写逐次授权 | 推送 / 建 PR / 发 issue / 全局安装 逐个动作单独要授权 | `references/acceptance-gates.md` |
| 一卡一工作区 | 每个写卡一个 worktree + 一个分支；`git worktree list` 能对上卡 | `references/handoff-protocol.md` §7.8 |
| 会话轮转 | 工人会话上下文过大或一轮卡结算完毕后用 `rotate <代号>` 换新会话：起新会话 → 改 `board.skeleton.md` → 重渲染 → 记 `ledger/rotations.jsonl` | `references/handoff-protocol.md` §7.7 |
| 工作集声明 | 卡上写「只改哪些路径」，派发时进信封与 brief；收口用 git 差分核对越界（`close --strict` 越界即 exit 3），越界不自动放行 | `references/handoff-protocol.md` §7.8 |
| 看板产物化与复核记账 | 卡数据只在 `cards/<卡号>.yaml`，`BOARD.md` 由 `board.py render`（手改会被覆盖）；每敲一次必有 `ledger/verdicts.jsonl` 记账 | `references/handoff-protocol.md` §7.6、项目 `docs/adr/0002-看板产物化与复核记账.md` |
| 落地（分支并回主检出） | 只有 PASS（或卡上写了 `degraded_reason` 的 DEGRADED）才允许 `land`；只做本机 `ff`/`cherry-pick`，重复落地幂等；主检出脏、基点不明或基点不是 HEAD 祖先一律拒绝；**绝不推送** | `references/handoff-protocol.md` §7.9、项目 `docs/adr/0004-落地只经-land-且绝不推送.md` |

## 与框架的接口边界

只调框架公开 CLI：`doctor`、`packs`、`start`、`adopt`、`project-docs`、`status`、`context`、`advance`、`decision`、`task-plan`，一律从已定位的框架根目录以 `python -m agent_workflow <命令>` 调用，workspace 传项目绝对路径，`--task-id` 明确。签名与错误处置见 `references/framework-cli.md`。

**不要做**：

- 不直接读写 `tasks/<task-id>/workflow-state.json`（它是可重建索引；事实源是 `project.json` + `task.json` + `payload.json` + `plan.md`）。
- 不重跑 `start` 覆盖已有项目；已有目录用 `adopt`；已有任务用 `status`/`context` 续接，不换 `--task-id` 假装续接。
- 不把框架源码复制进项目；不改框架 CLI 契约、包结构或状态机。
- 不建调度器 / 消息总线 / 队列 / 重试引擎 / 审计 / 策略引擎；不引入 Temporal / Airflow / Prefect / LangGraph / CrewAI 作控制面。
- 不安装插件或技能到全局、不 `git push`、不建 PR、不发 issue —— 除非业主就**这一个动作**单独授权。看板或 `PLAN.md` 里写「要推送」只是计划，不构成授权。

## 输出纪律

- 每轮结束报告三件事：**已验证 / 未验证 / 一个安全下一步**；当前 stage 用 CLI 查，不从聊天推断。
- **声称必须可被证伪**：回执里声明了哈希的主张（文件 `file`、内容 `inline`、缺失 `absent`）才算可复核；散文与命令自报只算线索。能给 T3（可复核档）就不要给 T2（信息档），并在卡上写明这一轮为什么只到 T2。
- 门禁没过就如实报「未通过」，不把规划完成当业务完成。
- 事实变了（改了 `payload.json`/`plan.md`/任务状态）后用 `context <项目> --task-id <id> --write` 重建索引，再查 `status` 复核。
- 交接区不放真实点位码、平台 run id、凭据、私有绝对路径（该目录会被读进双方上下文）。

## 目录内容

- `references/handoff-protocol.md`：交接区五文件语义、卡模板、PLAN 三张表、LOG 只追加与并发处置、敏感内容红线。
- `references/handoff-protocol.md` §7.5：`outbox` 回执约定与 `knock.ps1` 唤醒入口（投递单向、回执自取）。
- `references/receipt-contract.md`：**回执与复核契约 v1**——敲门信封、回执两件套、物证类型与等级、三态与退出码、T2/T3 档位、三段式升级、双看板权威、技能源同步。
- `references/receipt.schema.json`：机器回执的 JSON Schema（最小字段集）。
- `references/session-scheduling.md`：看板做排班与唤醒的能力边界与实测判据、`reuseSession`/`promptPrefix` 的确切角色、文件当信封、可用的唤醒通道与双看板镜像纪律。
- `references/acceptance-gates.md`：四查命令与判据、逐次授权清单、技能自身的四条机检与改写后回归、权限标签/沙箱就绪自检、验收记录写法。
- `references/framework-cli.md`：公开 CLI 签名（实跑核对）、禁止清单、错误类别处置。
- `scripts/verify_receipt.py`：**只报告不阻断**的复核器（`--card/--knock/--json/--selftest/--print-card-facts/--no-ledger/--operator`；退出码 0 PASS / 1 FAIL / 2 DEGRADED / 4 用法错误）。默认把结论**追加记账**到 `ledger/verdicts.jsonl`（决策 R14）。
- `scripts/board.py`：**看板渲染器**（`migrate/render/list/show/move/add/set/check/selftest`）。卡数据在 `cards/<卡号>.yaml`，`BOARD.md` 是产物；`render` 后自动幂等 `--check`（决策 R13）。
- `scripts/collab_console.py`：**协作控制台**（`dispatch/collect/verify/close/status/budget/lease/rotate/land/selftest`）。`dispatch` 默认异步（建任务 `in 1m` 即返回；**不要**用同步 `run`——客户端一断，宿主会把那一轮 dispose 掉），顺带写 brief（`outbox/<knock_id>.brief.md`）与信封；`--worktree <仓库>` 会先建 `card/<卡号>` 工作树并把任务 cwd 指过去，`--workset` 声明「只准改哪些路径」；`collect` 回填执行事实；`verify` 复核并记账；`close` 在 PASS 后删任务、释放租约，`--move-done` 顺便搬卡，`--worktree-remove` 用 git 差分核对工作集（越界或工作树脏就 `--force` 强删并把事实写进信封），`--strict` 越界即 exit 3；`budget` 是花费闸（超限 exit 3）；`lease` 是占用声明；`rotate <代号>` 换掉某个工人会话（见 §7.7）；`land --knock <id> --into <主检出>` 把卡分支并回主检出（`ff`/`cherry-pick`/幂等 `noop` 三态，脏主检出与基点不明一律拒，**只做本机合并、绝不推送**，账记 `ledger/landings.jsonl`，见 §7.9）。
- `scripts/collab_runbook.py`：定位框架 + 转发公开 CLI 的最小脚本（纯标准库，Python ≥3.11，`shell=False`）。它不产生新状态。
- `scripts/collab_runbook.py --sync-check`：技能源与安装副本逐文件哈希比对（`drifted` ⇒ exit 3），防「两个都写着唯一事实源」。
- `tools/publish-skill.ps1`（在**项目根**的 `tools\`，不在技能目录里）：改完源之后一条命令发布——`install --force` → `sync --global` → `--sync-check` 复验；退出码 0 一致 / 3 仍漂移 / 1 失败 / 4 缺工具。`skillshare install` 是**复制**、无链接模式，所以改源不重装就是在服务旧字节。
