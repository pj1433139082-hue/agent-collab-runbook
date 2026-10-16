# agent-collab-runbook

多个智能体会话在**同一份业务仓库**上安全并行的运行手册，配套 `agent-workflow-framework` 使用。

## 它解决什么

两个（或更多）会话同时改一个仓库，最容易出的不是写错代码，而是：互相不知道对方在改什么、成果堆在各自分支上没人合、"做完了"没有可证伪的证据。本手册把这三件事变成机械可核对的纪律：

- **公共交接区**：`BOARD.md`（做什么）/ `PLAN.md`（谁占了什么）/ `LOG.md`（发生过什么），文件当信封。
- **看板只经脚本改**：卡数据在 `cards/<卡号>.yaml`，`BOARD.md` 由 `board.py render` 生成；手改会被下一次渲染覆盖。
- **回执两件套 + 复核器**：工人交 `<卡号>-receipt.md`（人读）+ `<knock_id>.receipt.json`（机器读）；`verify_receipt.py` 给出 `PASS / FAIL / DEGRADED` 三态，只报告不阻断，并把结论记进 `ledger/verdicts.jsonl`。
- **一卡一工作树 + 工作集声明**：每张写卡一个 `git worktree` + 一个 `card/<卡号>` 分支，卡上声明"只准改哪些路径"，收口用 git 差分核对越界。
- **落地只经 `land`，绝不推送**：分支并回主检出只有 `ff` / `cherry-pick` / 幂等 `noop` 三种计划，主检出脏、基点不明一律拒绝；远端写永远由人逐次授权。

## 安装

把 `skill/agent-collab-runbook` 放进你的技能目录（或经 skillshare 同步）。技能只依赖 Python 3.11+ 标准库与 `git`，不需要任何第三方包。

## 目录

| 路径 | 内容 |
| --- | --- |
| `SKILL.md` | 技能入口：要素表、冷启动五步、目录内容、输出纪律 |
| `references/handoff-protocol.md` | 交接区完整协议（五列看板、卡的字段、LOG 纪律、工作树、回执、落地、敏感内容清单） |
| `references/receipt-contract.md` | 回执契约：两件套、五种物证、四级判据等级、三态与退出码 |
| `references/acceptance-gates.md` | 四查验收与远端写授权纪律 |
| `references/session-scheduling.md` | 用任务看板做排班与唤醒（含"投递成功"的判定线） |
| `references/framework-cli.md` | 与 `agent-workflow-framework` 的接口边界与命令签名 |
| `scripts/board.py` | 看板渲染器（migrate/render/check/list/show/move/set/add） |
| `scripts/collab_console.py` | 协作控制台（dispatch/collect/verify/close/status/budget/lease/rotate/land） |
| `scripts/verify_receipt.py` | 只报告不阻断的复核器 |
| `scripts/collab_runbook.py` | 框架入口转发 + `--sync-check`（技能源 ↔ 安装副本逐文件比对） |
| `docs/adr/` | 为什么这样设计（两套看板的权威、看板产物化、会话轮转与工作树、落地） |

## 三条不变量

1. 只有声明了哈希/命令的物证才算可证伪的证据；散文与结构化块冲突即判失败。
2. 复核只报告不阻断；`FAIL` 而仍判 Done 必须在卡上写理由。
3. 远端写（推送 / 建 PR / 发 issue / 全局安装）逐次单独授权——工具链里没有任何自动推送。

## 自测

```console
python scripts/collab_console.py selftest   # 26/26
python scripts/board.py --handoff <交接区> selftest
python scripts/verify_receipt.py --selftest
```

## 许可证

MIT，全文见 [LICENSE](LICENSE)。