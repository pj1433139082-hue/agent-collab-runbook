## 这次改了什么

<!-- 一句话说清动机；如果是修 bug，写清"原来错在哪"而不是"改了哪一行"。 -->

## 自测证据

<!-- CI 会跑；本地也请贴一次逐字输出（含退出码）。 -->

```console
$ python skill/agent-collab-runbook/scripts/collab_console.py selftest
$ python skill/agent-collab-runbook/scripts/verify_receipt.py --selftest
$ python skill/agent-collab-runbook/scripts/board.py --handoff <临时目录> selftest
$ python .github/scripts/check_public.py
```

## 对不变量的影响

- [ ] 不改变三条不变量（可证伪证据 / 只报告不阻断 / 远端写逐次授权）
- [ ] 变**严**了（说明收紧在哪）
- [ ] 变**松**了（必须说明理由，并在描述里写清风险）

## 检查清单

- [ ] 新行为有对应的自测用例（不是只改实现）
- [ ] 没有把私有环境的名字、路径、凭据带进仓库
- [ ] 若改了协议或契约，同步更新了 `references/` 下对应文档与 `CONTEXT.md` 术语
