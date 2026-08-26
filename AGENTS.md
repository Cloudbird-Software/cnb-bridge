# AGENTS.md —— cnb-bridge 索引

<!-- entry-protocol v2 -->

### 入口协议（陌生 agent 从这里开始——宪法 §11 / ADR-0055/0095）

0. **按意图定角色**（指引=.github 仓 `docs/agent/ROLE-*.md`，ADR-0095）：开新意图→ROLE-IR · 把已签署 IR 写成 spec→ROLE-SPEC · 实现卡片→ROLE-IMPLEMENT · 验收/人类让你处理 issues→ROLE-ACCEPT
1. 取 ghcb（钉 SHA，禁浮动 main）：`curl -fsS -o ghcb https://raw.githubusercontent.com/Cloudbird-Software/.github/f72d9520706c8fca974d92456f65cae5c1412bb7/scripts/ghcb && chmod +x ghcb`（凭据用你自己的：`gh auth login` 或 `export GH_TOKEN=<PAT>`；`-f` 必带——404 时 curl 无 -f 仍退出 0，会把错误页当脚本落盘）
2. 找活：`bash ghcb next [owner/repo]` → 列 state:ready 卡（卡 issue 是唯一工作凭证，无卡不开工）
3. 认领：`bash ghcb claim <n> [owner/repo]` → 评论 /claim——conductor 转介 arbiter 原子 CAS 租约，先到先得；败者换下一张（`bash ghcb status <n>` 看持有者）
4. 开工：`make card-test CARD=<n>`（读卡 AC、测试先行）→ `make gates-pr`（本地复现 CI 关卡）
5. 提 PR：body 必带一行卡元数据 `Card: <owner>/<repo>#<n>`（`bash ghcb card-meta <n>` 生成；缺失=后续关卡 exit 3）
6. front-desk 命令（卡 issue 评论，conductor 转介 arbiter 处理）：/claim 认领 · /release 释放租约 · /retry 隔离回流

<!-- /entry-protocol -->

## 角色路由（按你的意图选路——ADR-0095；指引文件在 .github 治理仓 docs/agent/）

- 开 IR：feature 意图=本仓 issue（issue 即 IR，无需 PR）；治理意图=.github 仓 → [ROLE-IR.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IR.md)
- IR→spec：spec PR 必带测试设计逐类讨论（差分/属性/模糊…）+ holdout；**spec agent 不得直接实现** → [ROLE-SPEC.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-SPEC.md)
- 实现卡片（PM 职责）：弱模型优先（子 agent / CNB 池）· fan-out=工具非流程 · 边做边推 PR · 3 次熔断自己接手 → [ROLE-IMPLEMENT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IMPLEMENT.md)
- 验收 / 人类让你处理 issues：卡/IR 完成度检查 · bug 复现三值判定 → [ROLE-ACCEPT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-ACCEPT.md)

这是什么：Cloudbird-Software 的 CNB 免费算力桥接仓（**可删除层**）。把 CNB
talk 仓 issue 窗口（#1-#100）当作免费 CodeBuddy NPC 沙箱的派单/回收通道；
独立于核心治理（gate/org-gate/conductor），可整体删除（清单见 REMOVAL.md）。

硬规则：
1. token 永不入仓——只经 org secret / 环境变量 CNB_TOKEN_<ALIAS> 注入；accounts.yaml 只登记 secret_ref。
2. NPC 产物必须机械核对——沙箱自报数字不采信，以退出码与原始输出对账。
3. 派单必须带 [run:xxxx] 锚串；collect 按锚串匹配，丢锚串的回复一律弃收。
4. 窗口占用判定以最后一条评论 author.is_npc 为准；档位纪律 light/std/heavy/8C 禁见 accounts.yaml。

CLI 速查（仓库根目录，token 先注入环境变量）：
  python cnb_pool.py status                                  # 账号池离线状态
  python cnb_pool.py quota      [--account 别名]              # 配额 dev_in_sec 等
  python cnb_pool.py windows    [--account 别名]              # 窗口占用
  python cnb_pool.py dispatch "任务正文" [--tier light] [--account 别名]
  python cnb_pool.py collect --account 别名 --window N --run-id xxxx

自检：python -m py_compile cnb_pool.py && python -m unittest discover -s tests -v
文档：README.md（定位/三平面/安全边界）· REMOVAL.md（删除清单）· work-inbox.yaml（PM 自起协议）
