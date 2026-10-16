# 框架公开 CLI：签名、白名单、禁止清单与错误处置

本文件是 `agent-collab-runbook` 技能与 `agent-workflow-framework` 之间的唯一接口契约。技能**只**调用下面列出的公开 CLI；不读框架内部模块、不改状态机、不复制框架源码。

## 0. 取证环境与运行约定

```powershell
# 定住解释器与两个根目录（后续所有示例沿用）

$py = "<业务仓库>\.venv-311\Scripts\python.exe"   # 实测 Python 3.11.15
$fw = "<工作根>\agent-workflow-framework"                  # cwd 固定为框架根
$ws = "<技能源>"                        # 本机被纳管的业务项目（绝对路径）
```

| 约定 | 值 | 依据 |
| --- | --- | --- |
| 解释器 | `Python 3.11.15`（`& $py --version` 实测，exit 0） | 框架要求 `>=3.11`，见 `agent_workflow\cli.py:75` |
| 工作目录 | 必须是 `$fw`，否则 `python -m agent_workflow` 找不到包 | `agent_workflow\__main__.py` 依赖包在 `sys.path` 上 |
| 调用形态 | `& $py -m agent_workflow <子命令> <workspace绝对路径> --task-id <id>` | 全部子命令的 `workspace` 为位置参数，`--task-id` 为必填（`--help` 实测） |
| 输出通道 | stdout 单行 JSON；成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": {"code": ..., "message": ...}}` | `agent_workflow\cli.py:92`、`cli.py:95` |
| 退出码 | `0` = 成功；`2` = 业务/参数/本地错误（含 `error.code`） | `cli.py:93`、`cli.py:101`、`cli.py:104` |
| 版本 | 框架 `0.1.0`（`doctor` 实测 `"version": "0.1.0"`） | `& $py -m agent_workflow doctor` 实测 exit 0 |

**读 JSON，不要读退出码以外的东西。** 框架从不向 stderr 输出业务信息；任何非 JSON 的 stdout 都不是框架输出，按"框架调用失败"处理。

## 1. 命令白名单表

子命令全集取自 `& $py -m agent_workflow --help`（实测 exit 0）的 usage 行，共 11 个，无遗漏、无额外。

| # | 子命令 | 作用 | 本技能可用？ | 参数顺序（去折行，逐字来自 `--help`） |
| --- | --- | --- | --- | --- |
| 1 | `doctor` | 打印框架版本、解释器版本、框架根、可用 pack、外部集成 | 可用（只读自检） | `[-h]` |
| 2 | `packs` | 只列出已安装业务 pack | 可用（只读） | `[-h]` |
| 3 | `new` | 从零创建一个新工作区 | **框架侧，本技能不使用** | `[-h] --project-id PROJECT_ID --workflow WORKFLOW --task-id TASK_ID [--mode {assisted,full-auto,manual-review}] [--apply] workspace` |
| 4 | `start` | 一键启动新项目（模板 + 入口 + 规划任务） | 可用，仅预览 | `[-h] --project-id PROJECT_ID --name NAME [--brief BRIEF] [--task-id TASK_ID] [--mode {assisted,full-auto,manual-review}] [--with-docs {architecture,environment,progress,results,summary}] [--apply] workspace` |
| 5 | `adopt` | 非破坏性纳管存量项目 | 可用，仅预览 | `[-h] --project-id PROJECT_ID [--name NAME] [--brief BRIEF] [--task-id TASK_ID] [--mode {assisted,full-auto,manual-review}] [--with-docs {architecture,environment,progress,results,summary}] [--apply] workspace` |
| 6 | `project-docs` | 只补齐缺失的可选项目文档 | 可用，仅预览 | `[-h] --task-id TASK_ID --with-docs {architecture,environment,progress,results,summary} [--apply] workspace` |
| 7 | `status` | 任务当前视图（stage/checks/can_advance/文档清单） | 可用（只读，主力） | `[-h] --task-id TASK_ID workspace` |
| 8 | `context` | 与 `status` 同源，另给 `snapshot_matches`、`framework_root`、`documents_absolute` | 可用（只读，主力） | `[-h] --task-id TASK_ID [--write \| --check] workspace` |
| 9 | `advance` | 推进到下一 stage | 可用，仅预览 | `[-h] --task-id TASK_ID [--apply] workspace` |
| 10 | `decision` | 回答当前 stage 定义的业务问题 | 可用，仅预览 | `[-h] --task-id TASK_ID [--apply] --decision-id DECISION_ID --answer ANSWER --by BY workspace` |
| 11 | `task-plan` | 改本任务档位 / 下一动作 | 可用，仅预览 | `[-h] --task-id TASK_ID [--apply] [--mode {assisted,full-auto,manual-review}] [--next-action NEXT_ACTION] workspace` |

关于"仅预览"：`--apply` 是唯一的写入开关。`advance` / `decision` / `task-plan` 走 `engine.change_task(..., apply=False)`，只调用 `prepare(verify_task(...))` 并返回 `applied: false`，不落盘（`agent_workflow\task_writes.py:93-95`）；`project-docs` 在 `apply=False` 时跳过全部 `_publish`（`agent_workflow\project_documents.py:91-93`）；`start` / `adopt` 在 `apply=False` 时于写盘前返回预览（`agent_workflow\engine.py:501-502`、`agent_workflow\project_start.py:213-214`）。**本技能的默认形态就是这些预览形态。**

## 2. usage 原文（`--help` 逐字捕获）

捕获条件：`COLUMNS` 未设置，argparse 默认 80 列折行。每条命令实测 `exit 0`。

### 顶层

```
usage: agent-workflow [-h]
                      {doctor,packs,new,start,adopt,project-docs,status,context,advance,decision,task-plan}
                      ...

positional arguments:
  {doctor,packs,new,start,adopt,project-docs,status,context,advance,decision,task-plan}
    adopt               Adopt an existing workspace non-destructively into the
                        framework.
    project-docs        Preview or create only missing optional project
                        documents.

options:
  -h, --help            show this help message and exit
```

说明：只有 `adopt` 与 `project-docs` 在顶层帮助里带描述，其余 9 个只出现在 usage 的选项集合中——这是因为 `cli.py:38` 与 `cli.py:47` 显式传了 `help=`，其余 `add_parser` 没传。**子命令全集以 usage 花括号内那一行为准，不以描述列表为准。**

### doctor

```
usage: agent-workflow doctor [-h]

options:
  -h, --help  show this help message and exit
```

### packs

```
usage: agent-workflow packs [-h]

options:
  -h, --help  show this help message and exit
```

### new

```
usage: agent-workflow new [-h] --project-id PROJECT_ID --workflow WORKFLOW
                          --task-id TASK_ID
                          [--mode {assisted,full-auto,manual-review}]
                          [--apply]
                          workspace

positional arguments:
  workspace

options:
  -h, --help            show this help message and exit
  --project-id PROJECT_ID
  --workflow WORKFLOW
  --task-id TASK_ID
  --mode {assisted,full-auto,manual-review}
  --apply
```

### start

```
usage: agent-workflow start [-h] --project-id PROJECT_ID --name NAME
                            [--brief BRIEF] [--task-id TASK_ID]
                            [--mode {assisted,full-auto,manual-review}]
                            [--with-docs {architecture,environment,progress,results,summary}]
                            [--apply]
                            workspace

positional arguments:
  workspace

options:
  -h, --help            show this help message and exit
  --project-id PROJECT_ID
  --name NAME
  --brief BRIEF
  --task-id TASK_ID
  --mode {assisted,full-auto,manual-review}
  --with-docs {architecture,environment,progress,results,summary}
  --apply
```

### adopt

```
usage: agent-workflow adopt [-h] --project-id PROJECT_ID [--name NAME]
                            [--brief BRIEF] [--task-id TASK_ID]
                            [--mode {assisted,full-auto,manual-review}]
                            [--with-docs {architecture,environment,progress,results,summary}]
                            [--apply]
                            workspace

positional arguments:
  workspace

options:
  -h, --help            show this help message and exit
  --project-id PROJECT_ID
  --name NAME
  --brief BRIEF
  --task-id TASK_ID
  --mode {assisted,full-auto,manual-review}
  --with-docs {architecture,environment,progress,results,summary}
  --apply
```

### project-docs

```
usage: agent-workflow project-docs [-h] --task-id TASK_ID --with-docs
                                   {architecture,environment,progress,results,summary}
                                   [--apply]
                                   workspace

Existing valid workspace/task only. Default is read-only; existing ordinary
files are never overwritten. Task facts and index remain unchanged.

positional arguments:
  workspace             Existing framework-created workspace outside this
                        checkout.

options:
  -h, --help            show this help message and exit
  --task-id TASK_ID     Explicit existing task ID, not a path.
  --with-docs {architecture,environment,progress,results,summary}
                        Repeat to select documents; duplicate selections are
                        deduplicated.
  --apply               Create only missing documents; report
                        created/preserved/pending progress.
```

### status

```
usage: agent-workflow status [-h] --task-id TASK_ID workspace

positional arguments:
  workspace

options:
  -h, --help         show this help message and exit
  --task-id TASK_ID
```

### context

```
usage: agent-workflow context [-h] --task-id TASK_ID [--write | --check]
                              workspace

positional arguments:
  workspace

options:
  -h, --help         show this help message and exit
  --task-id TASK_ID
  --write
  --check
```

`--write` 与 `--check` 互斥（源码 `cli.py:59-61` 的 `add_mutually_exclusive_group`）。

### advance

```
usage: agent-workflow advance [-h] --task-id TASK_ID [--apply] workspace

positional arguments:
  workspace

options:
  -h, --help         show this help message and exit
  --task-id TASK_ID
  --apply
```

### decision

```
usage: agent-workflow decision [-h] --task-id TASK_ID [--apply] --decision-id
                               DECISION_ID --answer ANSWER --by BY
                               workspace

positional arguments:
  workspace

options:
  -h, --help            show this help message and exit
  --task-id TASK_ID
  --apply
  --decision-id DECISION_ID
  --answer ANSWER
  --by BY
```

### task-plan

```
usage: agent-workflow task-plan [-h] --task-id TASK_ID [--apply]
                                [--mode {assisted,full-auto,manual-review}]
                                [--next-action NEXT_ACTION]
                                workspace

positional arguments:
  workspace

options:
  -h, --help            show this help message and exit
  --task-id TASK_ID
  --apply
  --mode {assisted,full-auto,manual-review}
  --next-action NEXT_ACTION
```

## 3. 允许调用的命令与典型用法（每条实跑）

每条都是可原样复制的完整命令；`workspace` 一律绝对路径；行尾为本次实测退出码。

### 3.1 自检与枚举

```powershell
& $py -m agent_workflow doctor                                              # 实测 exit 0
& $py -m agent_workflow packs                                               # 实测 exit 0
```

`doctor` 实测返回：`version=0.1.0`、`python=3.11.15`、`framework_root=<工作根>\agent-workflow-framework`、`runtime_scope=framework_tools_only`、`business_runtime_constraint=null`、`business_runtime_checked=false`、`packs` 三项（`document-delivery` / `ecommerce-catalog` / `project-planning`）、`external_integrations=[]`。

`business_runtime_checked=false` 是设计结论而不是缺陷：框架只声明"业务运行时与本工具无关"，不检查业务环境。

### 3.2 恢复任务事实（主力只读）

```powershell
Push-Location $fw
& $py -m agent_workflow status  $ws --task-id kickoff                       # 实测 exit 0
& $py -m agent_workflow context $ws --task-id kickoff                       # 实测 exit 0
& $py -m agent_workflow context $ws --task-id kickoff --check               # 实测 exit 0
Pop-Location
```

实测 `status` 关键字段（`agent-collab-runbook` / `kickoff`）：`stage=plan`、`next_stage=ready`、`can_advance=true`、`evidence_valid=true`、`completed_locally=false`、`pending_decisions=[]`、三项 check 全 `passed=true`（`project-scope`、`project-plan`、`plan-acceptance`）。

`context` 在 `status` 之外多给三个字段，实测：
- `snapshot_matches: true` —— `tasks/kickoff/workflow-state.json` 与本次重算视图一致。
- `framework_root: "<工作根>\agent-workflow-framework"`。
- `documents_absolute: [...]` —— 把 `documents` 里的框架相对路径（如 `agent_workflow/packs/project-planning/SOP.md`）解析成框架内的绝对路径。**`documents` 是框架文档，不是项目文档**（`engine.py:31` 的 `BASE_DOCS` 就是框架根下的 `AGENTS.md` 与 `docs/00-入口层/00-协作总览.md`）。

`--check` 的用途：只要索引与事实不一致就直接 `exit 2` + `snapshot_drift`，而不是给一个可能过期的视图。只读巡检用 `--check`，不要用 `--write`。

### 3.3 预览推进（不落盘）

```powershell
& $py -m agent_workflow advance $ws --task-id kickoff                       # 实测 exit 0（applied=false）
& $py -m agent_workflow decision $ws --task-id kickoff --decision-id plan-acceptance --answer accept --by probe   # 实测 exit 0（applied=false）
& $py -m agent_workflow task-plan $ws --task-id kickoff --mode assisted --next-action "probe only"                # 实测 exit 0（applied=false）
```

- `advance` 实测输出 `{"ok": true, "from_stage": "plan", "to_stage": "ready", "applied": false}`。
- `decision` 的 `--decision-id` 与 `--answer` 必须命中当前 stage 的 `workflow.json` 定义；`project-planning` 的 `plan` stage 只定义了 `plan-acceptance`，可选答案 `accept` / `revise`，`required_answer=accept`（`agent_workflow\packs\project-planning\workflow.json:31`）。
- `task-plan` 实测输出把 `next_action` 改成传入值、`applied=false`，**原 `task.json` 未被修改**。

### 3.4 可选文档预览

```powershell
& $py -m agent_workflow project-docs $ws --task-id kickoff --with-docs environment   # 实测 exit 0
```

实测输出 `progress={"created":[],"preserved":[],"pending":["environment"]}`、`document_files={"environment":"docs/project/environment.md"}`、`scope="optional documents only; task facts and index unchanged"`。`--with-docs` 可重复传，取值只能是 `architecture` / `environment` / `progress` / `results` / `summary`。

失败时是 `DocumentError`，退出码 2，并且 JSON 里**多一个 `progress` 字段**（`cli.py:98-99`）。按 `progress` 里的 `created` / `preserved` / `pending` 决定下一步，只补 `pending`。

### 3.5 启动 / 纳管的预览形态

```powershell
& $py -m agent_workflow start $ws --project-id agent-collab-runbook --name probe      # 实测 exit 2 → workspace_exists
& $py -m agent_workflow adopt $ws --project-id agent-collab-runbook                   # 实测 exit 2 → task_exists
```

这两条在本机被充分使用：对一个**已经存在**的目录跑 `start` 预览，框架拒绝覆盖并报 `workspace_exists`（`engine.py:495-496`）；对一个**已经有 `tasks/kickoff/`** 的项目跑 `adopt` 预览，框架拒绝并报 `task_exists`（`project_start.py:113-115`）。这两次实测均未写盘，命令后 `context --check` 仍为 `snapshot_matches: true`。

真正写入时在命令末尾追加 `--apply`。**本技能不使用该形态**：本技能对已有项目一律只用 `status` / `context` 续接，不新建、不覆盖、不纳管第二次。

### 3.6 `new` 的处理

`new` 是框架侧建库命令，本技能不使用，因此只保留已实测的只读帮助入口：

```powershell
& $py -m agent_workflow new --help                                          # 实测 exit 0
```

## 4. 禁止清单与反例

### 4.1 禁止直接读写 `tasks/<task-id>/workflow-state.json`

它是**可重建索引**，不是事实源。事实源是 `project.json` + `tasks/<task-id>/task.json` + `tasks/<task-id>/payload.json` + `tasks/<task-id>/plan.md`。

反例与正确做法：

```powershell
# 反例（禁止）
Set-Content "$ws\tasks\kickoff\workflow-state.json" '{"stage":"ready"}'   # 未执行：反例，禁止运行

# 正确：改了事实后用框架重建索引，再复核
Push-Location $fw
& $py -m agent_workflow context $ws --task-id kickoff --write               # 未执行：这是写操作，超出本文只读取证范围
& $py -m agent_workflow status  $ws --task-id kickoff                       # 实测 exit 0（复核）
Pop-Location
```

`context --write` 的行为依据源码而不是本次实跑：`cli.py:88-89` 把它分发给 `engine.context(write=True)`，再进 `task_writes.repair_index`（`task_writes.py:120-132`）——拿 `_task_lock` 后重算视图并原子写回索引，**只动 `workflow-state.json`**，不动 `task.json` / `payload.json` / `plan.md`。

判据：框架自己会在索引与事实不一致时报错——`context --check` → `exit 2` + `snapshot_drift`，消息原文 "Generated state is missing or stale; use context --write, not a repeated business operation."（`engine.py:468`）。**看到 `snapshot_drift` 就说明有人绕过 CLI 改了东西。**

补充：`workflow-state.json` 若被换成链接/联接点，`safe_path` 会直接报 `unsafe_path` 并拒绝继续处理（`engine.py:57-67`、`engine.py:461-463`）。

### 4.2 不重跑 `start` 覆盖已有目录

反例：

```powershell
# 反例（禁止）：以为重跑会"刷新"项目——实际只会被拒，或误加到 --apply 后仍被拒
& $py -m agent_workflow start $ws --project-id agent-collab-runbook --name retry --apply   # 未执行：反例，禁止运行
```

已有目录的纳管入口是 `adopt`；已有任务的续接入口是 `status` / `context`。`start` 只在**目录尚不存在**时才有意义。

### 4.3 已有 `tasks/kickoff/` 时 `adopt` 报 `task_exists`：不要换 `--task-id` 假装续接

实测：`adopt $ws --project-id agent-collab-runbook` → `exit 2`，`{"code": "task_exists", "message": "Task 'kickoff' already exists in workspace; choose another task-id or resume existing task."}`。

**错误处置**：不要为了"让 adopt 通过"而改成 `--task-id kickoff-2`。那等于在同一项目里**新增一个独立任务**，不会续接 `kickoff` 的 stage、decisions 与证据；后续 `status` 会看到一个空档位的新任务，历史工作全部脱钩。

正确做法：只读续接，`--task-id` 用项目 `.agent-workflow.local.json` 里声明的那个 id。

### 4.4 不把框架源码复制进项目

框架是可定位的外部依赖（默认 `<工作根>\agent-workflow-framework`），不是项目的一部分。

反例（禁止）：

```powershell
Copy-Item -Recurse "$fw\agent_workflow" "$ws\agent_workflow"                  # 未执行：反例，禁止运行
```

`engine.external_workspace` 会拒绝框架目录自身、其子目录，以及**包含**框架目录的路径作为 workspace（`engine.py:82-88`，`private_workspace_required`）。把框架塞进项目还会让"框架升级"变成"改项目文件"，并且把框架的验收规则与项目事实混在一处。

### 4.5 业务错误不得拿同一参数重放

框架所有失败都是**确定性返回**：同一 workspace + 同一 `--task-id` + 同一参数，结果一致。因此重放同一命令既不改结果，还会掩盖真实原因（尤其是 `--apply` 形态下的一次不确定写）。

正确路径：读 `error.code` → 按第 5 节处置 → 若报错涉及"写了一半"，先 `status` 看事实、必要时 `context --write` 修索引，**确认没有半成品后**再决定是否重试，绝不原样重放。

`change_task` 的完成回执已经把这条写死：`"repeat_operation": false`，并给出 `"recovery": ["context", <workspace>, "--task-id", <id>, "--write"]`（`agent_workflow\task_writes.py:55-63`）。**回执里的 `recovery` 才是下一步，不是重放原命令。**

### 4.6 其它红线

- 不改框架 CLI 契约、包结构、`workflow.json`、状态机源码。
- 不建调度器 / 消息总线 / 队列 / 重试引擎 / 审计 / 策略引擎。
- 不引入 Temporal / Airflow / Prefect / LangGraph / CrewAI 作控制面。
- 不安装插件或技能到全局、不 `git push`、不建 PR、不发 issue（逐次单独授权才做）。
- 不把 `--apply` 写进任何批处理循环；一次只对一个动作、一个任务、一次显式授权。

## 5. 错误处置表

所有错误都从 stdout JSON 的 `error.code` 读取，退出码统一为 `2`。`error.message` 是框架的固定英文提示，可直接引用给用户；框架**不回显调用参数**（`cli.py:13-14` 的设计意图，避免凭据泄漏）。

### 5.1 必查六项

| 错误码 | 出处（源码行） | 含义 | 处置 | 明确禁止的重放动作 | 本机实测 |
| --- | --- | --- | --- | --- | --- |
| `task_exists` | `agent_workflow\project_start.py:115` | 目标 `tasks/<task-id>/` 已存在，`adopt` 拒绝纳管 | 改用 `status` / `context`，`--task-id` 用 `.agent-workflow.local.json` 里声明的值续接 | 禁止换 `--task-id` 重新 `adopt` 当成"续接"（那是新增任务）；禁止加 `--apply` 再试；禁止删/移已有 `tasks/<task-id>/` | 实测 exit 2（`adopt $ws --project-id agent-collab-runbook`） |
| `transition_blocked` | `agent_workflow\engine.py:565` | 没有下一 stage，或必需证据/业务答案缺失（`can_advance=false`） | 跑 `status` 看 `checks` 里 `passed=false` 的项与 `pending_decisions`，先补齐证据/答题 | 禁止重放 `advance`；禁止直接改 `task.json` 的 `stage`；禁止把终态 stage 再推一次 | 实测 exit 2（`advance <工作根>\wwtp-hub-replication --task-id kickoff`，该任务 `stage=ready`、`next_stage=null`） |
| `invalid_arguments` | `agent_workflow\cli.py:14` | 参数不被 argparse 接受（缺必填、拼错、不在 choices 内） | 跑 `<子命令> --help` 对照 usage 修正参数 | 禁止凭记忆猜参数后反复试；禁止把原始 argv 写进日志或交接区（可能含凭据） | 实测 exit 2（`status` 不带任何参数） |
| `framework_location_error` | `%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py:92` | **转发脚本**找不到可用框架：`--framework` 路径缺少标志文件、`.agent-workflow.local.json` 缺 `framework_root` 或不是字符串、文件超 100 KB、元数据非 UTF-8 | 显式传 `--framework "<工作根>\agent-workflow-framework"`，或用 `--locate` 先确认 | 禁止重放同一 `--framework` 值；禁止在项目里新建/覆盖 `.agent-workflow.local.json` 来"修好"它；禁止把框架源码复制进项目当作可用框架 | 实测 exit 2（`--framework <工作根>\does-not-exist-probe --locate`） |
| `framework_invocation_error` | `start_project.py:96` | 定位成功但**子进程启动失败**（解释器/可执行不可用、无法访问） | 核对 `$py` 是否真实存在且 ≥3.11；用 `& $py --version` 验证；修正解释器路径后**先**跑 `status` 看事实 | 禁止当作"框架缺失"去重新定位或复制框架；禁止在未看 `status` 前重放原命令 | **未在本机复现**：仅 POSIX 别名场景可达，其回归用例被 `@unittest.skipUnless(os.name == "posix", ...)` 跳过（`tests\test_project_start_helper.py:86-101`） |
| `python_version` | `agent_workflow\cli.py:76`（CLI）、`start_project.py:80`（转发脚本） | 解释器 `< 3.11`，协作工具拒绝运行 | 换用 ≥3.11 解释器（本机 `$py` = 3.11.15） | 禁止在旧解释器上重放；禁止因为这条去改业务运行时版本（消息原文明确 "business runtimes are independent"） | **未在本机复现**：本机只有 3.11.15 与 3.14（`py -0p` 实测），无 `<3.11` 解释器；条件来自 `sys.version_info < (3, 11)` |

**必查六项中的一处命名澄清**：任务清单里写的 `invalid_arguments_error` **未在源码中出现**。对 `agent_workflow` 包全量检索 `agent_workflow\`、`tests\`、`docs\`、`scripts\`，只有 `invalid_arguments`（`cli.py:14`）与转发脚本的 `helper_arguments_error`（`start_project.py:89`）。`invalid_arguments_error` 不是任何一层实际返回的类别，不要写进判据或分支逻辑。

### 5.2 其它可从公开 CLI 到达的错误码（同一处置原则）

| 错误码 | 出处（源码行） | 含义 | 处置 | 禁止的重放动作 |
| --- | --- | --- | --- | --- |
| `snapshot_drift` | `engine.py:468` | 索引缺失或过期（`context --check`） | `context <ws> --task-id <id> --write` 重建索引，再 `status` 复核 | 禁止手改 `workflow-state.json`；禁止用 `advance` 之类的业务动作"顺手修" |
| `missing_file` | `engine.py:122` | 任务必需 JSON 缺失（通常是 `--task-id` 写错） | 核对 `.agent-workflow.local.json` 的 `task_id` 与 `tasks/` 下真实目录名 | 禁止重放同一 `--task-id`；禁止为此新建任务 |
| `private_workspace_required` | `engine.py:85` | workspace 指向框架目录、其子目录，或包含框架目录 | 传业务项目根目录的绝对路径 | 禁止把框架复制/移动到项目内来"绕过" |
| `unsafe_path` | `engine.py:66` / `72` / `75` / `78` / `87` | 路径含链接/联接点、含 `\` 或 `:`、跳出 workspace、或指向盘根 | 改用普通目录下的正常相对路径；检查是否有 junction | 禁止删掉链接后重放（先查清链接指向什么） |
| `workspace_exists` | `engine.py:496` / `532` | 目标目录已存在，`start` / `new` 拒绝覆盖 | 已有目录改走 `adopt`；已有任务改走 `status` / `context` | 禁止加 `--apply` 重试；禁止先删目录再建 |
| `workspace_creation_incomplete` | `engine.py:512` / `518` / `537` / `540` | 创建中途失败，有半成品 | 保留现场并检查，按消息要求 inspect 而非 replay | 禁止重放；禁止递归删除 staging 目录 |
| `task_busy` | `task_writes.py:35` | 另一写入者持有 `tasks/<id>/.task-write-lock` | 等对方结束，再 `status` 看事实 | 禁止重放；禁止删除锁目录 |
| `local_write_error` | `task_writes.py:86` | 本地写未提交（OS 级错误） | 按回执看 `completion`；先 `status`，必要时 `context --write` | 禁止直接重放 `--apply` 命令 |
| `workspace_not_found` | `project_start.py:100` | `adopt` 的目标目录不存在 | 核对绝对路径；新建目录应该用 `start` 而不是 `adopt` | 禁止先手工建空目录再 `adopt` |
| `invalid_project_facts` | `project_start.py:122` / `124` | 已有 `project.json` schema 不是 `1.0` 或损坏 | 只读检查 `project.json`，人工确认后再决定 | 禁止覆盖 `project.json`；禁止重放 `adopt` |
| `invalid_decision` | `engine.py:583` | `--decision-id` 不在当前 stage，或 `--answer` 不在允许集合 | 先 `status` 读 `pending_decisions`，按其 `answers` 取值 | 禁止猜答案重试；禁止直接写 `task.json` 的 `decisions` |
| `invalid_project_documents` | `project_start.py:26`、`project_documents.py:81` | `--with-docs` 取值非法或为空 | 只用 `architecture` / `environment` / `progress` / `results` / `summary` | 禁止用未声明类别重试 |
| `invalid_document_target` / `document_target_changed` / `document_cleanup_incomplete` | `project_documents.py:26` / `60` / `72` | 目标不是普通文件、被并发改动、staging 被改 | 按返回的 `progress` 只补 `pending` | 禁止覆盖非普通文件；禁止删 staging |
| `document_target_changed` 之后的补建 | 同上 | 有并发写入者同时补同一份文档 | 只补 `progress.pending`，`preserved` 的保持原样 | 禁止用 `--apply` 重放整个 `--with-docs` 列表 |
| `materials_changed` | `verification.py:155` | 校验期间本地材料被改动，未返回连贯视图 | 等编辑停止后重跑一次只读命令 | 禁止在写入进行中并发跑只读查询 |
| `invalid_mode` | `engine.py:494` / `596`、`project_start.py:104` | `--mode` 不在 `{assisted, full-auto, manual-review}` | 用三选一中的值 | 禁止自造档位名重试 |
| `workflow_changed` | `engine.py:276` | 业务 pack 变更，现有任务规则不再匹配 | 显式评审并迁移任务 | 禁止静默改 `task.json` 的 `workflow` 字段 |
| `task_identity_mismatch` / `invalid_task` / `invalid_project` | `engine.py:270` / `272` / `278` / `264` | 任务与项目身份或 stage 不一致 | 只读核对 `project.json` 与 `task.json` | 禁止手改事实文件对齐 |
| `invalid_identity` | `engine.py:43` / `45` | id 不符合 `[a-z0-9][a-z0-9_-]{0,63}`，或是 Windows 保留名 | 用小写、文件系统安全的 id | 禁止传路径当 id；禁止大写或带空格重试 |
| `invalid_workflow` / `unknown_workflow` | `engine.py:170` / `173` 起 | pack 不存在或 schema 不受支持 | 用 `packs` 看已安装 id | 禁止手改 `workflow.json` |
| `invalid_project_template` | `engine.py:489` / `492`、`project_start.py:38` / `41` | 模板缺失、过大、含未替换占位符，或试图替换事实文件 | 用框架自带模板；不要用项目模板覆盖 `project.json` / `tasks` | 禁止绕过模板检查直接写目标文件 |
| `local_input_error` | `cli.py:102-103` | 未归类的本地路径/事实/schema 问题（`OSError` / `KeyError` / `TypeError` / `ValueError`） | 只读复核路径与事实文件是否存在、可读、schema 正确 | 禁止重放；禁止据此推断业务失败 |
| `helper_arguments_error` | `start_project.py:89` | 转发脚本自身参数错（不是框架参数错） | 跑 `start_project.py --help`；确认 `--` 分隔符位置正确 | 禁止把脚本参数错误当成框架错误去查框架 |
| `sensitive_text` / `invalid_text` | `engine.py:53` / `51` | 传入的元数据含控制字符或疑似凭据，或为空/超长 | 去掉控制字符与凭据，控制长度 | **禁止把凭据写进任务元数据再重试** |
| `unsupported_check` | `engine.py:248` | pack 声明了未实现的 check 类型 | 用框架自带 pack | 禁止期望框架执行动态代码 |

**通用处置顺序**：读 `error.code` → 读 `error.message` 原文 → 跑对应的**只读**命令（`status` / `context` / `<子命令> --help`）取证 → 只在确有必要且已授权时用一次写入 → 写入后 `context --write` + `status` 复核。

## 6. 定位框架

### 6.1 `.agent-workflow.local.json`：两个字段的分工

本机 `<技能源>\.agent-workflow.local.json` 实测内容：

```json
{
  "framework_root": "<工作根>\\agent-workflow-framework",
  "schema_version": "1.0",
  "task_id": "kickoff"
}
```

| 字段 | 谁读它 | 用法 | 约束 |
| --- | --- | --- | --- |
| `framework_root` | **转发脚本**（`start_project.py:25-31` 的 `read_location`） | 作为"框架在哪"的默认答案；`--locate` 与无 `--framework` 的调用都走它 | 必须是字符串；文件 ≤ 100000 字节；按 `utf-8-sig` 解码（允许 BOM）；指向的目录必须同时含 `AGENTS.md`、`agent_workflow\cli.py`、`agent_workflow\project_start.py`、`agent_workflow\packs\project-planning\workflow.json`、`docs\00-入口层\03-一键启动新项目.md`（`start_project.py:36-41`） |
| `task_id` | **框架 CLI 不读，转发脚本也不读** | 项目侧对"本项目的默认任务是谁"的声明，供人和技能读取；技能据此决定 `--task-id` 传什么 | 框架每个子命令的 `--task-id` 都是 `required=True`（`cli.py:57`），**没有任何子命令会从该文件取默认值** |

**实测确认**：`status $ws --task-id nonexistent` → `exit 2` + `missing_file`。即框架不会回退到 `.agent-workflow.local.json` 的 `task_id`。把 `task_id` 读出来、显式传 `--task-id`，是调用方（本技能）的责任。

转发脚本的完整定位顺序（`start_project.py:45-64`）：显式 `--framework` → 环境变量 `AGENT_WORKFLOW_FRAMEWORK` → **当前工作目录**下的 `.agent-workflow.local.json` → 脚本自身相对位置的框架 → `%LOCALAPPDATA%\agent-workflow\framework.json`（非 Windows 用 `$XDG_CONFIG_HOME`）。全部失败才抛 `framework_location_error`。

注意：`--locate` 读的是**cwd** 下的标记文件，所以要在项目目录里跑，或直接给 `--framework`。本机实测 `$env:AGENT_WORKFLOW_FRAMEWORK` 为空。

### 6.2 project-start 转发脚本

脚本：`%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py`

```powershell
# 帮助与定位（只读）
& $py "%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py" --help                     # 实测 exit 0
Push-Location $ws
& $py "%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py" --locate                   # 实测 exit 0 → {"ok": true, "framework_root": "<工作根>\\agent-workflow-framework"}
Pop-Location

# 显式定位 + 转发公开子命令（`--` 之后原样转发给 agent_workflow）
& $py "%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py" --framework "<工作根>\agent-workflow-framework" -- doctor                                # 实测 exit 0
& $py "%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py" --framework "<工作根>\agent-workflow-framework" -- status $ws --task-id kickoff          # 实测 exit 0
```

脚本自身的 usage 原文（`--help` 逐字捕获，实测 exit 0）：

```
usage: start_project.py [-h] [--framework FRAMEWORK] [--locate] ...

positional arguments:
  command

options:
  -h, --help            show this help message and exit
  --framework FRAMEWORK
  --locate
```

`command` 用 `argparse.REMAINDER` 收集（`start_project.py:76`），所以 **`--` 是必须的分隔符**：它把后面全部原样交给 `agent_workflow`，脚本自己不再解析。

转发语义（逐条实测核对）：
- 脚本用 `subprocess.run([sys.executable, "-m", "agent_workflow", *command], cwd=<framework_root>, check=False)` 启动子进程（`start_project.py:94`），**不经 shell**。
- 脚本直接返回子进程的退出码：实测 `-- doctor` → exit 0，`-- status $ws --task-id kickoff` → exit 0，输出与直接调用逐字相同（回归用例见 `tests\test_project_start_helper.py:71-84`）。
- 脚本自己**不产生任何状态**：不建目录、不写文件、不改索引。
- 参数错误由脚本自己拦下并报 `helper_arguments_error`，**不回显原始 argv**：实测 `--unknown-probe-secret` → exit 2 + `{"code": "helper_arguments_error", ...}`，输出中不含该字符串。
- 定位失败同样不回显输入：实测 `--framework <工作根>\does-not-exist-probe --locate` → exit 2 + `{"code": "framework_location_error", ...}`。

**什么时候用转发脚本、什么时候不用**：只有需要"自动定位框架"或需要沿 project-start 技能的入口统一调用时才用它。已经知道 `$fw` 时，直接用 `Push-Location $fw; & $py -m agent_workflow ...` 更短、少一层进程，也少一类错误码。

## 7. 取证记录

### 7.1 实跑命令与退出码

| 命令（cwd = `<工作根>\agent-workflow-framework`，`$py` = 3.11.15） | 退出码 | 结论 |
| --- | --- | --- |
| `& $py --version` | 0 | `Python 3.11.15` |
| `& $py -m agent_workflow --help` | 0 | 子命令全集 11 个 |
| `& $py -m agent_workflow doctor --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow packs --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow new --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow start --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow adopt --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow project-docs --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow status --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow context --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow advance --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow decision --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow task-plan --help` | 0 | usage 逐字记录 |
| `& $py -m agent_workflow doctor` | 0 | 版本 / 解释器 / 框架根 / 3 个 pack |
| `& $py -m agent_workflow packs` | 0 | 同上（仅 packs） |
| `& $py -m agent_workflow status $ws --task-id kickoff` | 0 | `stage=plan`、`can_advance=true` |
| `& $py -m agent_workflow context $ws --task-id kickoff` | 0 | `snapshot_matches=true` |
| `& $py -m agent_workflow context $ws --task-id kickoff --check` | 0 | 索引与事实一致 |
| `& $py -m agent_workflow advance $ws --task-id kickoff` | 0 | 预览 `plan` → `ready`，`applied=false` |
| `& $py -m agent_workflow decision $ws --task-id kickoff --decision-id plan-acceptance --answer accept --by probe` | 0 | 预览，`applied=false` |
| `& $py -m agent_workflow project-docs $ws --task-id kickoff --with-docs environment` | 0 | 预览，`pending=["environment"]` |
| `& $py -m agent_workflow task-plan $ws --task-id kickoff --mode assisted --next-action "probe only"` | 0 | 预览，`applied=false` |
| `& $py -m agent_workflow status $ws --task-id nonexistent` | 2 | `missing_file` |
| `& $py -m agent_workflow status` | 2 | `invalid_arguments` |
| `& $py -m agent_workflow adopt $ws --project-id agent-collab-runbook` | 2 | `task_exists` |
| `& $py -m agent_workflow start $ws --project-id agent-collab-runbook --name probe` | 2 | `workspace_exists` |
| `& $py -m agent_workflow decision $ws --task-id kickoff --decision-id does-not-exist --answer accept --by probe` | 2 | `invalid_decision` |
| `& $py -m agent_workflow status <工作根>\quote-cost-platform --task-id kickoff` | 0 | `stage=scope`、`can_advance=true`（对照样本） |
| `& $py -m agent_workflow advance <工作根>\quote-cost-platform --task-id kickoff` | 0 | 预览 `scope` → `plan`，`applied=false`（对照样本） |
| `& $py -m agent_workflow status <工作根>\wwtp-hub-replication --task-id kickoff` | 0 | `stage=ready`、`next_stage=null`、`can_advance=false`（构造 `transition_blocked` 的对照样本） |
| `& $py -m agent_workflow advance <工作根>\wwtp-hub-replication --task-id kickoff` | 2 | `transition_blocked` |
| `& $py "%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py" --help` | 0 | usage 逐字记录 |
| `& $py "...\start_project.py" --locate`（cwd = `$ws`） | 0 | `{"ok": true, "framework_root": "<工作根>\\agent-workflow-framework"}` |
| `& $py "...\start_project.py" --framework "<工作根>\agent-workflow-framework" -- doctor` | 0 | 与直接调用逐字相同 |
| `& $py "...\start_project.py" --framework "<工作根>\agent-workflow-framework" -- status $ws --task-id kickoff` | 0 | 与直接调用逐字相同 |
| `& $py "...\start_project.py" --framework "<工作根>\agent-workflow-framework" --unknown-probe-secret` | 2 | `helper_arguments_error`，无回显 |
| `& $py "...\start_project.py" --framework "<工作根>\does-not-exist-probe" --locate` | 2 | `framework_location_error`，无回显 |

### 7.2 只读性证明

本次全部命令可用"写入开关缺席"与"事后状态未变"双重证明：

1. 上表中**没有任何命令带 `--apply`，也没有 `context --write`**。
2. 项目 `<技能源>` 的事实摘要（`status` 的 `sources` 字段）在全部命令前后**逐字节相同**：
   - `project.json` = `361eb53e45d946f5e3decabe33a79e82b61943283fa0884e1d825c671147ae29`
   - `tasks/kickoff/task.json` = `1ab33632aa57f17cc034065c3a5c6e3a32357d2412ac2ddbd1cead3b61676355`
   - `tasks/kickoff/payload.json` = `6dcd453b9dbe69e8c1199ef9a7e587223d691450b803547284f65715920f3937`
   - `tasks/kickoff/plan.md` = `673ddc4af7b37fa808d0249566c914323656b6fb33e18d27a45f12f18649077c`
3. `tasks\kickoff\` 下四个文件的 `LastWriteTime` 全部早于本次运行，且全程无 `.task-write-lock` 出现。
4. 命令结束后补跑 `context $ws --task-id kickoff --check` → exit 0，`snapshot_matches=true`。

### 7.3 未能本机复现的两项（如实标注）

| 项 | 状态 | 原因 | 依据 |
| --- | --- | --- | --- |
| `framework_invocation_error` | 未复现 | 只在子进程启动失败时触发；其回归用例要求 POSIX 下伪造 `argv[0]` 别名，本机为 Windows 且该用例带 `@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), ...)` | `start_project.py:96`；`tests\test_project_start_helper.py:86-101` |
| `python_version` | 未复现 | 触发条件是解释器 `< 3.11`；本机可用解释器为 3.11.15 与 3.14（`py -0p` 实测），无 `<3.11` 可用 | `cli.py:74-76`；`start_project.py:79-80`；`tests\test_project_start_helper.py:30-44`（依赖外部 `AGENT_WORKFLOW_TEST_PYTHON310`） |

### 7.4 与公开帮助输出的一致性核查

逐条把 §2 的 usage 原文与本次 `--help` 捕获结果比对，**无一处不一致**。两处需要读者注意的、非错误的现象：

1. 顶层 `--help` 的"positional arguments"描述列表只列 `adopt` 与 `project-docs`，另外 9 个子命令没有描述。这不是缺项：usage 花括号内已列出全部 11 个，且 `cli.py:20-21`（`doctor` / `packs`）与 `cli.py:54-55`（`status` / `context` / `advance` / `decision` / `task-plan`）创建子解析器时没有传 `help=`。
2. `status` / `context` 的 `usage` 行把 `workspace` 排在后、把 `--task-id` 排在前，而 `start` / `adopt` / `project-docs` 把 `workspace` 排到最后一行——这是 argparse 按"必填选项在前、位置参数在后"的排版结果，不影响实际参数顺序（位置参数永远是 `workspace`）。

### 7.5 源码核实清单

错误码与行为全部来自下列文件的逐行核对，未凭印象书写：

- `<工作根>\agent-workflow-framework\agent_workflow\cli.py`（1-104 行，全部）
- `<工作根>\agent-workflow-framework\agent_workflow\project_start.py`（1-248 行，全部）
- `<工作根>\agent-workflow-framework\agent_workflow\project_documents.py`（1-100 行，全部）
- `<工作根>\agent-workflow-framework\agent_workflow\task_writes.py`（1-132 行，全部）
- `<工作根>\agent-workflow-framework\agent_workflow\engine.py`（1-90、412-608 行；错误码全量检索）
- `<工作根>\agent-workflow-framework\agent_workflow\verification.py`（1-60 行）
- `<工作根>\agent-workflow-framework\agent_workflow\packs\project-planning\workflow.json`（1-36 行，全部）
- `%USERPROFILE%\.agents\skills\project-start\scripts\start_project.py`（1-100 行，全部）
- `<工作根>\agent-workflow-framework\tests\test_project_start_helper.py`（1-125 行，全部）

错误码检索方式：对框架根全量匹配 `WorkflowError("…")` 与 `error["code"]`，得到 85 处构造点，全部并入 §5.2 的去重清单。
