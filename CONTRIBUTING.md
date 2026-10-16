# 参与贡献

这是一份**运行手册**：它规定多个智能体会话在同一份仓库上怎么协作。改它的门槛比改普通代码高一点——
因为它一旦变松，别的仓库会照着放松。

## 动手之前

跑一遍全部检查，确认基线是绿的：

```console
$ python skill/agent-collab-runbook/scripts/collab_console.py selftest     # 期望 SELFTEST OK: 26/26
$ python skill/agent-collab-runbook/scripts/verify_receipt.py --selftest  # 期望 SELFTEST OK: 7/7
$ python skill/agent-collab-runbook/scripts/board.py --handoff <临时目录> selftest
$ python .github/scripts/check_public.py
```

只依赖 Python 3.11+ 标准库与 `git`，不需要第三方包，也不需要联网。

## 三条不变量（改动的红线）

1. **可证伪**：只有声明了哈希或命令的物证才算证据；散文与结构化块冲突即判失败。
2. **只报告不阻断**：复核器不替人做决定；`FAIL` 而仍判 Done 必须在卡上写理由。
3. **不自动远端写**：推送、开 PR、发 issue、全局安装一律由人逐次授权。

改动**变严**随时欢迎（新的拒绝线、更强的复算）；改动**变松**必须在 PR 描述里说明理由与风险，
并且通常需要先有一个真实的失败案例作为依据。

## 提交与 PR

- 一个 PR 只做一件事；描述里写清动机与"原来哪里不对"。
- 新行为必须有自测用例（只改实现、不加用例的 PR 会被要求补上）。
- 自测期望值必须来自**独立的真值来源**，不要用实现算出来的值去断言实现。
- 改了协议或契约，同步改 `references/` 下对应文档；新增术语进 `CONTEXT.md`。
- 别把私有环境的名字、路径、凭据带进仓库——`check_public.py` 会通用扫描，但它是兜底，不是许可。

## 复现与最小样例

报 bug 时请贴**命令原文 + 逐字输出 + 退出码**。改写过的输出没法当证据——
这正是本项目对"证据"的定义，对我们自己也一样。
