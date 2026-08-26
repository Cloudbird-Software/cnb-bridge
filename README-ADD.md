# README 增量 —— 周审计/台账/自起协议五件（并入 cnb-bridge README 末尾）

> 本段为 IR-0004 D 组收尾（rev6，卡 #317-#321）新增件的使用速查；安全边界、
> token 纪律与删除层定位见 README 既有章节，此处不重复。全部命令在仓库根
> 目录运行（Python 3.11+ 标准库 only；token 一律环境变量 `CNB_TOKEN_<ALIAS>`
> 注入，永不入仓）。

## 新五件速查

| 件 | 作用 | 关键命令 |
|---|---|---|
| `ledger.py` | 配额账本（AC-15）：快照差分=时段用量，append-only hash 链 | `python ledger.py --snapshot` / `--reconcile` / `--verify` |
| `work_inbox.py` | PM 自起工作发现协议执行器（AC-17）：租约/幂等/心跳三态 | `python work_inbox.py --check` / `--release <list_id>` / `--sweep` |
| `audit_extra.py` | 周审计补全（AC-18 五项之三）：政策 diff/产物抽样/配额汇总 | `python audit_extra.py` / `--sample fanout/products 10` |
| `RUNBOOK.md` | 账号生命周期 runbook（AC-16）：入职/降级/退休/事故四流程 | 照文档执行；入职验收清单在 §1.6 |
| `.github/workflows/weekly-audit.yml` | 周审计编排：周一 06:31 UTC，fail-closed | 自动运行（actions/手动 `workflow_dispatch`） |

## ledger.py —— 配额账本（AC-15）

```bash
python ledger.py --snapshot          # 逐账号查配额；与上次快照差分 → usage.jsonl（hash 链）
python ledger.py --reconcile --threshold-pct 10   # 对照 build logs 实耗（duration×labels.cpus）对账
python ledger.py --verify            # hash 链校验；篡改=非零退出
```

- 状态目录默认 `ledger-state/`（`--state-dir` 可改）：`snapshots.jsonl` /
  `usage.jsonl` / `alerts.jsonl`。
- **台账真源=GitHub 侧**：本地文件仅暂存；`usage.jsonl` 变更须随 PR 并回本仓，
  git 历史即 append-only 背书（ADR-0062 hash 链思路，INV-05）。
- 对账真源=平台 build logs（IFACE-05）；`labels.cpus` 缺失即告警（自报标签
  不采信）。只对 `status=active` 账号入账。

## work_inbox.py —— PM 自起工作发现协议（AC-17，IR-0004 D16）

```bash
python work_inbox.py --check --holder <会话标识>
# 输出单行 JSON {action: claim|heartbeat|none, item, ...}
python work_inbox.py --release WI-0001 --outcome done --note "产物路径/锚串"
python work_inbox.py --sweep            # 租约到期未释放且未被接管超阈值 → 开 issue 文本
```

- 三态：有 pending 且无活跃租约 → `claim`（置 lease，TTL=60min）；无 pending
  → `heartbeat`（追加心跳行，零派单）；租约被占 → `none`（不重复认领）。
- 幂等：同一 `list_id` 在 history 终态（released/done/abandoned）后再次认领
  即拒绝（退出码 1，人工裁决）。租约过期可接管，原认领作废留痕（history）。
- 全部文件操作原子（tmp+rename）；本模块零网络零凭据。协议语义五则见
  `work-inbox.yaml` 尾注。

## audit_extra.py —— 周审计补全（AC-18 五项之三）

```bash
python audit_extra.py                       # 全项：政策 diff + 产物抽样 + 配额汇总 → JSON 报告
python audit_extra.py --sample fanout/products 10 --seed 42   # 仅产物真实性抽样
python audit_extra.py --out weekly-report.json                # 报告落盘（原子写）
```

- 政策监测：`docs.cnb.cool/zh/workspaces/intro.html` 与
  `docs.cnb.cool/zh/pricing.html`（URL 常量在脚本内）逐页 sha256 与上次快照
  比对；变更 → 告警条目（含 diff 补丁与页面链接）。**网络失败=infra 红非绿**。
- 产物真实性抽样：从派单回执目录随机抽样（BUDGET-03 默认 10%，seed 可注入；
  默认 ISO 周号，周内可复现），逐条核对 `[run:锚]` 真实存在于对应窗口评论
  （复用 cnb_pool 客户端）；不匹配=作废留痕条目（`audit-state/voided.jsonl`）。
- 配额汇总：调 `ledger.py` 的 snapshot+reconcile+verify（周报内嵌对账结果）。
- fail-closed：任一通道查询失败 → `overall=red` 且退出码 1（周工作流据此变红）；
  告警条目是检测成功产出，走 issue 处置不改红绿。

## RUNBOOK.md —— 账号生命周期（AC-16）

入职（含陌生 AI 执行者身份存证：`gh api search/issues` 查公开参与记录）/ 降级
/ 退休 / 事故（泄漏应急三步：吊销→轮换→审计）四流程，每步带真实验收命令与
`lifecycle.jsonl`（hash 链，与 ledger.py 同口径）追加片段；"陌生 AI 仅凭本
runbook 完成入职"验收清单见 §1.6。token 决策依据见
`ADR-0086-cnb-token-decision.md`（#320）。

## weekly-audit.yml —— 周审计编排

周一 06:31 UTC（cron 错峰）自动跑 `audit_extra.py` 全项：报告进 run 的
step summary 与 artifact（runner runtime API 直传，无第三方 action）；任一
通道 red → job 红（fail-closed）。actions 仅 `checkout`/`setup-python` 两个
SHA 钉版；secrets 经 env 映射注入单步进程（同 cnb-dispatch 模式）；
`permissions: contents: read`；`timeout-minutes: 15`。

## 自检（增量后全量跑）

```bash
python -m py_compile cnb_pool.py ledger.py work_inbox.py audit_extra.py
python -m unittest discover -s tests -v
```
