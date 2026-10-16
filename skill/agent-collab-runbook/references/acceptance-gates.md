# 验收与授权

本文件回答两件事：**什么算做完（四查门禁）**，以及**哪些动作必须逐个拿授权**。命令与退出码均在本机实跑核对过（2026-10-15，解释器 `<业务仓库>\.venv-311\Scripts\python.exe` = Python 3.11.15，cwd 一律在仓库根，不写死盘符以外的东西）。

## 1. 四查门禁

> 版本一：这四查取自业主在用的业务仓库 `<业务仓库>`。换成别的仓库时，把路径与资产名替换掉，**判据的形态不变**（退出码 0 + 输出符合判据）。

```powershell
$py = "<业务仓库>\.venv-311\Scripts\python.exe"
Push-Location "<业务仓库>"

# [1] 单元测试全绿
& $py -m unittest discover -s tests            # 判据：OK（允许 skipped=N）

# [2] 离线发布门禁
& $py -m model_lab release-gate .              # 判据：PASSED，且每条子检查 [PASSED]

# [3] 资产目录未漂移（注意：--check 要的是「已生成的目录文档」，不是模型根）
& $py -m model_lab catalog models --expected-count 9 --check "docs/00-入口层/01-在役模型资产目录.md"
                                                # 判据：IN SYNC

# [4] 指定资产的状态契约
& $py -m model_lab status "models/01_OTE数据规则模型"   # 判据：exit 0；看清 Runtime environment 是 OK 还是 BLOCK
Pop-Location
```

### 实跑回执（2026-10-15）

| 查 | 命令要点 | 退出码 | 关键输出 |
| --- | --- | --- | --- |
| 1 | `-m unittest discover -s tests` | **0** | `Ran 576 tests in 438.105s` / `OK (skipped=5)` |
| 2 | `-m model_lab release-gate .` | **0** | `release-gate: PASSED`；`Models: 9`、`Release snapshots: 11`；8 条子检查全 `[PASSED]`（asset_catalog / draft_inventory / catalog_output / behavior_baseline / chronos_contract / publish_preview / git_history / release_metadata） |
| 3 | `catalog models --expected-count 9 --check "docs/00-入口层/01-在役模型资产目录.md"` | **0** | `catalog: IN SYNC` |
| 4 | `status "models/01_OTE数据规则模型"` | **0** | `ote-oxygen-utilization-<厂>: MANUAL`；`Current state: released`；`Safety: read-only`；`Runtime environment: BLOCK` — `[missing_target_environment]` |

### 三个容易踩的坑（都是实跑出来的）

1. **`catalog --check` 的 PATH 不是 `models`**。传 `models` 会得到 `catalog: DRIFTED`；要传已生成的目录文档路径。实测对比：`--check models` 之类指向模型根 → `DRIFTED`；指向 `docs/00-入口层/01-在役模型资产目录.md` → `IN SYNC`。`--check` 必须带参数，省略报 `argument --check: expected one argument`（退出码 2）。
2. **`status` 的 positional 是「模型工作区或 `model.yaml` 路径」**，不是 `models` 根目录。传 `models` → `model-lab: workspace manifest does not exist: models\model.yaml`（退出码 2）。正确写法：`status "models/01_OTE数据规则模型"`，或直接给 `...\model.yaml`。
3. **退出码 0 不等于资产健康**。第 4 查退出码是 0，但资产 `Runtime environment: BLOCK`（缺 `target.environment`）。所以判据要写"exit 0 **且**人读关键行"，不能只看退出码。机器判读用 `--format json`，字段是 `build_host_status`（此处 `blocked`）、`build_environment.issues[].code`（此处 `missing_target_environment`）。

### 什么时候可以不跑满四查

四查是**进 Done 的条件**，不是每次改动的仪式。按改动面裁剪，并在证据里写清跑了哪几查：

- 只改文档、不动代码与清单 → 至少跑 [3]；[1][2] 在提交前跑一次。
- 改了模型资产清单 → [2][3] 必跑。
- 只读定位、只写交接区文件 → 一查都不用跑，但要写"未运行任何门禁，仅只读定位"。

## 2. 逐次授权清单

以下动作**每一次**都要业主就**这一个动作**单独说"可以"：

| 动作 | 例子 | 计划里写了算不算授权 |
| --- | --- | --- |
| 远端写 | `git push`、建/改远端分支 | 不算 |
| 协作平台写 | 开 PR、发 issue、回评论 | 不算 |
| 全局安装 | 把技能/插件装到 `C:\Users\<用户>\.agents\skills`（或任何全局目录） | 不算 |
| 对外发布 | 打包上传、发布产物、改公开分发内容 | 不算 |
| 凭据使用 | 读环境变量里的 token、配置远端 | 不算 |

判据：**授权必须出现在本轮的对话事实里，且指向单个动作**。`BOARD.md`、`PLAN.md`、任务卡上写的"要推送"只是计划。拿不准就停在本地、把结果交回，让业主自己执行最后一步。

## 3. 技能自身的验收（本技能怎么算做完）

本技能的产物是可被安装的目录，验收不看"文档写得全不全"，看四条机检：

```powershell
$sk = "<技能源>\skill\agent-collab-runbook"   # 项目改名为 agent-collab-runbook 后，这里同步改成 ...\agent-collab-runbook\skill\agent-collab-runbook

# ① 结构与元数据齐备：SKILL.md + references/ + scripts/，且 frontmatter 有 name/description
Get-ChildItem -Recurse $sk | Select-Object FullName, Length
Get-Content "$sk\SKILL.md" -TotalCount 4

# ② 无占位词：应 0 命中（关键词拆开写，否则本文件会命中自己）
$pad = 'TO' + 'DO|' + 'TB' + 'D|待填' + '写|待补' + '充|[{]{2}'
Get-ChildItem -Recurse -File $sk | Select-String -Pattern $pad

# ③ 无凭据样式：应 0 命中（同样拆开写）
$sec = 'AK' + 'IA|PRIV' + 'ATE KEY|client_' + 'secret|pass' + 'word\s*='
Get-ChildItem -Recurse -File $sk | Select-String -Pattern $sec

# ④ 被引用的文件都真实存在（SKILL.md 里出现的 references/xxx.md 逐个 Test-Path）
```

判据：① 列出的每个文件都存在且非空；② ③ 各 0 命中；④ 无死链。**安装（`skillshare install`/`sync`）不在验收范围内**——那是第 2 节的授权动作，业主批准后才做。

### 实测（2026-10-15，本技能目录）

| 检查 | 结果 |
| --- | --- |
| ① 结构 | 7 个文件：`SKILL.md` 7102B、`agents/openai.yaml` 369B、`references/{handoff-protocol 25871B, framework-cli 42599B, acceptance-gates 6313B, session-scheduling 5875B}`、`scripts/collab_runbook.py` 5077B；frontmatter 有 `name` + `description` |
| ② 占位词 | 拆词写法下 **0 命中**（直接写完整关键词会让本文件命中自己——这是本技能的已知自伤点，必须拆开） |
| ③ 凭据样式 | **0 命中** |
| ④ 引用完整性 | 4 个 `references/*.md` 全部 `Test-Path=True` |

### 技能改写后要复跑（变更回归）

技能被改写（补 references、改路径、换项目目录名）之后，**必须把上面四条机检再跑一遍**，并且**顺手核对引用里的既有路径**——`references/*.md` 与 `SKILL.md` 里的项目根目录、技能目录、解释器路径是否都还指向真实存在的位置。改写技能的常见失败不是文档写错，而是**引用换了一半**：改完目录名，正文里还留着旧路径，装到别的机器上才炸。

判据：四条机检全过 + 引用路径逐个 `Test-Path=True`。**只在本机成立**——技能未安装、项目也无 git 时，这次改写的影响范围就只有本机，不需要远端验证。

## 3.5 环境就绪自检：权限标签与沙箱（实测，2026-10-15）

会话"能读文件但写不进去"时，先怀疑**完整性标签**，再怀疑 ACL。实测两层成因同时存在：

| 成因 | 表现 | 修法 |
| --- | --- | --- |
| 缺沙箱 ACE | 目标目录没有沙箱账户的写 ACE | `icacls` 补 ACE（或按 `diagnose-windows-sandbox-acl` 技能走一遍） |
| 缺完整性标签 | 目录没有 `Mandatory Label\Low Mandatory Level` | **`icacls '<路径>' /setintegritylevel '(OI)(CI)Low' /T /C /Q`** |

三条实测教训：

1. **`icacls /reset` 修不好标签问题**：它只恢复**继承来的 ACL 条目**，而完整性标签不是继承项。实测在 81 个条目、0 失败的"成功"之后，受限写入仍然全部 DENIED。
2. **改过权限必须复跑原始受限操作**：ACL 看着对不等于能用。本技能自己的措辞是"改完权限后复跑那次被拒的原始操作"，而不是"再 `icacls` 看一眼"。原始受限操作（例如向 `_handoff` 写一个文件）成功，才算修好。
3. **标签缺口通常不止一处**：修好一个目录不代表同类问题消失，其他目录（含业务仓库的 `tests`/`docs`/`src`）可能同样为 0。要按目录逐个确认，别外推。

### 镜像的完整性标签会把解释器一起拖低（2026-10-15 实测）

症状很有欺骗性：**`python` 写不了某个目录，`cmd.exe` 和 `powershell.exe` 在同一个目录里却写得好好的**。实测报错是

```
PermissionError: [WinError 5] 拒绝访问。: '…\skill\agent-collab-runbook\scripts\__pycache__'
```

而那个目录的 ACL 明明给了 `Authenticated Users:(M)`。查下去是三件事叠在一起：

1. 工作区目录被递归打了 `Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)`（沙箱可写机制的一部分）；
2. **`(OI)(CI)` 会继承**，于是 `.venv-311` 里的**每一个文件**都继承了 Low —— 包括 `<业务仓库>\.venv-311\Scripts\python.exe`；
3. Windows 让**进程以镜像自身的 mandatory label 作为完整性级别** ⇒ 这个 venv 启动的每个 python 都是 Low，而 Low 进程**只能写同样带 Low 标签的对象**（no-write-up）⇒ 写 `%TEMP%`、写技能树、写框架仓一律被拒。

判据与修法：

| 步骤 | 命令 | 判据 |
| --- | --- | --- |
| 读标签 | `icacls "<exe 或目录>"` | 出现 `Mandatory Label\Low Mandatory Level` 就是它 |
| 证实"与镜像有关、与 cwd 无关" | 裸解释器（`…\uv\python\cpython-3.11-…\python.exe`，无标签 ⇒ Medium）放进 Low 目录里跑仍是 Medium；反过来的 venv 解释器放到 `E:\` 下跑仍是 Low | 进程级别跟着镜像走 |
| 修 | **`icacls '<exe 路径>' /setintegritylevel Medium`** | 只需修**镜像**，不要动目录——目录必须留 Low 给沙箱写 |
| 批量 | `& '<技能源>\tools\check-exe-integrity.ps1' [-Fix]`（扫 `*.exe`、跳过 `node_modules`；退出码 0 无问题/已修、2 仍有 Low、4 路径不存在） | `-Fix` 后复扫输出 `OK: no Low-labelled executables` |
| 回滚 | `icacls "<file>" /setintegritylevel Low` | 逐个镜像回退 |

实测：`learn-model` 下 **85 个 exe 里 84 个是 Low**（`.venv-311\Scripts\*` 的 `pythonw/model-lab/mlflow/torchrun/jupyter*/debugpy*` 等，以及 `.venv-linux311\…\bin\*`、`torch\bin\protoc.exe`、win32 `pythonservice.exe`）；把 `python.exe` 抬到 Medium 后**原症状当场消失**：`python -m py_compile` exit 0 并生成 `.pyc`。抬高镜像**不牺牲沙箱**——Medium 可以向下写 Low 目录，被拒的只是"Low 写 Medium"这个方向。

两条容易误判的细节：

- **`Get-Acl` 的 `.Sddl` 里看不到标签**（不带 `S:(ML;…)`），会让人以为"没有标签"。读标签用 `icacls`。
- 「ACL 给了 Modify 却报拒绝访问」时，先算**进程的完整性级别**（在孙进程里 `whoami /groups` 看 `Mandatory Label`），再谈 ACL。

## 4. 方案的接受会被"改方案"作废（实测行为）

框架把"业主接受"和**方案当时的内容摘要**绑在一起。所以只要事后改一个字的 `plan.md`，`plan-acceptance` 检查立刻转 `false`，`can_advance` 变 `false`：

```powershell
# 改完 plan.md 后重建索引，检查立刻翻红
& $py -m agent_workflow context <项目> --task-id kickoff --write
# → "plan-acceptance", "passed": false, "errors": ["business_answer_required_or_evidence_changed"]
#   pending_decisions: [{"id": "plan-acceptance", "required_answer": "accept", ...}]
#   can_advance: false, evidence_valid: false
```

这不是故障，是设计：**接受的是一份具体的方案，不是"方案"这个词**。处置按方向分两种：

| 情况 | 正确动作 |
| --- | --- |
| 只是把**已完成的事实**回填进方案（补回执、改正错字、补未执行说明），方案主张没变 | 重建索引 → 把业主**原来那条** accept 按新摘要重新记录一次（`--answer accept --by owner --apply`），并在交接消息里说明"改的是回执不是主张" |
| 改了方案**主张**（范围、要素、判据、风险处置变了） | **必须重新问业主**，不能拿旧 accept 复用；把新方案给业主看，拿到新的回答再记录 |

判据：记录完再查一次 `status`，`plan-acceptance` 回到 `true`、`can_advance: true`。**别为了让检查变绿而删掉方案里的内容**——那才是真的作废了接受。

## 5. 验收记录怎么写

写进卡的「证据」格，格式固定三行：

```text
判据：<可执行命令 或 可观察结果>
证据：<命令输出关键行 / 提交号 / 日志路径>
范围：<跑了哪几查；哪几查未跑及原因>
```

细则：

- 「命令退出 0」不构成证据，要贴**关键输出行**（如 `OK (skipped=5)`、`IN SYNC`）。
- 只有口头结论、没有证据的卡**只能进 Review，不能进 Done**。
- 未执行的检查必须显式写「未执行」，不许留空、不许写「应该没问题」。
- 证据里不写真实点位码、平台 run id、凭据、私有绝对路径。

## 6. 本项目的端到端接口回执（2026-10-15）

```text
collab_runbook.py --framework <fw> --project <ws> --locate
  -> exit 0  {"ok": true, "framework_root": "<工作根>\agent-workflow-framework", "project": "<技能源>", "marker": true, "declared_task_id": "kickoff"}
collab_runbook.py --framework <fw> --project <ws> -- status <ws> --task-id kickoff
  -> exit 0  输出与直接 `-m agent_workflow status` 逐字一致
agent_workflow context <ws> --task-id kickoff --write
  -> exit 0  sources[tasks/kickoff/plan.md] 给出摘要哈希；改过 plan.md 后 plan-acceptance 转 false、errors=["business_answer_required_or_evidence_changed"]
agent_workflow decision <ws> --task-id kickoff --decision-id plan-acceptance --answer accept --by owner --apply
  -> exit 0  把业主原 accept 按新摘要重记
agent_workflow advance <ws> --task-id kickoff --apply
  -> exit 0  {"from_stage": "plan", "to_stage": "ready", "applied": true}
```

判据：改过 `plan.md` 之后必须能看到 `plan-acceptance` 先转 `false` 再被你重记回 `true` —— **一步都不许跳**。跳过重记就直接 `advance`，等于拿改动后的方案冒用旧授权。

## 7. 改完证据后重记 `plan-acceptance`：先重记、再 `advance`（2026-10-15 实测 + 框架侧已修）

**2026-10-15 更新：这一节的原始结论已由框架侧修复推翻。** 修复前，`advance --apply` 走完之后同一任务内**没有**受支持的路径重记 `plan-acceptance`（详细成因与实测见本节末「修复前的实测记录」）；修复后**可以**重记，但**先重记、再 `advance` 仍然更省事**，见下面的顺序。

机制（读到源码，不是猜）：`agent_workflow/engine.py` 的 `decision()` 只在**当前 stage** 的 checks 里找决策定义——

```python
stage = next(stage for stage in verified.pack["stages"] if stage["id"] == verified.task["stage"])
definition = next((check for check in stage["checks"] if check["kind"] == "decision" and check["id"] == decision_id), None)
if definition is None or answer not in definition["answers"]:
    raise WorkflowError("invalid_decision", "Only a current, defined business question with an explicit choice can be answered.")
```

而 `plan-acceptance` 定义在 `plan` 阶段、`ready` 阶段是 `"checks": []`（`packs/project-planning/workflow.json:31` 与 `:34`）。所以任务推进到 `ready` 之后重记它，一律 `exit 2` + `invalid_decision`（实测两次：一次因 cwd 不在框架根报 `No module named agent_workflow`，一次 cwd 正确后仍是 `invalid_decision`）。

### 推荐的操作顺序

1. 改 `plan.md`（或 `task.json` 的 `project_id`，或任何进入证据摘要的东西）；
2. `context <ws> --task-id <id> --write` → 看到 `plan-acceptance` 转 `false`、`errors: ["business_answer_required_or_evidence_changed"]`；
3. 立刻 `decision … --decision-id plan-acceptance --answer accept --by owner --apply`（**此时 stage 还是 `plan`，这一步能成功**）；
4. 复核 `status` 三项全 `true`；
5. **最后**才 `advance <ws> --task-id <id> --apply`。

顺序颠倒（先 `advance`，再回头改 plan）**不再留死结**：把 `plan-acceptance` 重记一次即可（框架侧修复后允许回答已完成阶段里已记录过的决策），状态会回到全绿，`stage` 不变。

### 判据：怎么判断自己踩进去了

- `task.json` 里 `decisions["plan-acceptance"].evidence_digest` 与当前证据摘要不等。当前摘要求法（三行，在框架根下跑）：

```python
import sys, pathlib
sys.path.insert(0, r"<框架根>")
from agent_workflow import engine
print(engine.evidence_digest(pathlib.Path(r"<ws>/tasks/<task-id>")))
```

- 摘要由 `payload.json` + `plan.md` 两文件构成（`engine.py:307-321` 明确排除 `task.json` 与 `workflow-state.json`）。
- **实时 `status` 与 `workflow-state.json` 会不一致**：`status` 说 `plan-acceptance: false` / `evidence_valid: false` / `completed_locally: false` + 一条 `pending_decisions`；`workflow-state.json` 可能仍写着三项全 `true`。**以实时 `status` 为准。**

### 框架侧修复（2026-10-15，已实测）

`<工作根>\agent-workflow-framework\agent_workflow\engine.py` 的 `decision()` 原先只在**当前阶段**的 checks 里找决策定义，这正是「`advance` 之后无法重记」的根因。修复后规则是：

- 当前阶段里定义的决策：照旧可答；
- **已完成阶段里定义的决策：只有它此前已经有记录（`task["decisions"]` 里有它）时才可答** —— 证据改动让它失效后，按新摘要重记是唯一受支持的修复；
- **从未记录过的决策，仍不能从已越过的阶段背后补答**；阶段之外的决策（还没走到的阶段）一律不可答。

`tests\test_task_writes.py` 两个新用例锁住这个行为：`test_decision_can_be_rerecorded_after_advancing_when_evidence_changed`（`advance` 到 `ready` 后改证据 → 重记 → 复核转绿、`stage` 不变）与 `test_decision_answering_is_still_bounded_by_stage_and_definition`（越阶段/未跑过检查的决策仍 `invalid_decision`；同模式空闲恢复仍可用）。

### 不许做的事（这三条都是伪造或绕过，不是修）

- ❌ 手改 `task.json` 的 `evidence_digest` —— 等于伪造业主授权绑定；
- ❌ 手改 `workflow-state.json` 把 `passed` 写成 `true` —— 门禁是算出来的，不是写出来的；
- ❌ 把 `task.json` 的 `stage` 退回 `plan`、重记后再推进 —— 用未受支持的写入绕框架限制。

**正确做法：如实记录不一致 + 报给框架侧**（建议：让 `decision()` 允许回答已完成阶段中定义的决策，或让 `advance()` 在旧决策因证据变化失效时给显式重新确认入口）。在框架修好之前，凡「已 advance 的项目再改 plan」都会留下这个不一致。

## 8. 技能安装与改名：`install` 是复制，不是链接（2026-10-15 实测）

- `skillshare install <技能目录> --global`：把技能**复制**到源目录 `%USERPROFILE%\.agents\skills\<技能名>\`（实测 `LinkType` 为空 = 实体目录，不是符号链接）。`install --help` 与 `sync --help` 都**没有链接模式**（复制是唯一形态），所以改源之后只能重装。
- ⇒ **改了技能源之后必须重装**，否则全局源仍是旧的；然后再 `skillshare sync --global` 分发到各 CLI 目标。
- **一条命令做完这三步（推荐）**：`& '<技能源>\tools\publish-skill.ps1'`。它依次跑 `install --force` → `sync --global` → `collab_runbook.py --sync-check`，退出码 **0** 已发布且复验一致 / **1** 某步失败 / **3** sync 后仍漂移（说明有落点没跟上，别当成功）/ **4** 路径或工具缺失；`-DryRun` 只打印将执行的命令。参数 `-Skill`（默认技能源目录）、`-Skillshare`、`-Python`、`-SkipVerify`。
- 实测：改名后全局源里旧路径残留 15 处、4 个文件与项目源字节数不同（`SKILL.md` 少 2 B、`acceptance-gates.md` 少 4 B、`framework-cli.md` 少 22 B、`handoff-protocol.md` 少 2 B）；重装后**残留 0、逐字节一致**。
- `sync` 的 `codex` 目标有 **include 白名单**（只收指定的 11 个技能），新技能**不会**自动进 codex —— 这是设计如此，不是失败。
- 本机全局安装 CLI：`%USERPROFILE%\AppData\Local\Programs\skillshare\skillshare.exe`；全局家目录是 `%APPDATA%\skillshare`（**不是** help 里写的 `~/.config/skillshare`）；源目录 `%USERPROFILE%\.agents\skills`。
