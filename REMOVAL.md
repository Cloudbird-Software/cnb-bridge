# REMOVAL.md —— cnb-bridge 整体删除清单（一页）

> 本仓从立项起就是**可删除层**：独立于核心治理（gate / org-gate / conductor），
> 服务于"发散与执行侧"的临时免费算力需求。当 CNB 配额消失、政策变化或维护
> 成本超过收益时，按本清单一次性删净，不留悬空引用。

## 删除步骤（按序执行）

1. **org secrets**：删除 `CNB_TOKEN_XUEMEI`、`CNB_TOKEN_P11`（及后续按
   `CNB_TOKEN_<ALIAS>` 命名新增的全部条目）。删除前先在 CNB 侧吊销对应令牌。
2. **删除本仓** `Cloudbird-Software/cnb-bridge`（含 Actions 运行历史）。
3. **.github 仓**：删除两个工作流 `cnb-dispatch` 与 `cnb-audit`
   （`.github/workflows/cnb-dispatch.yml`、`.github/workflows/cnb-audit.yml`）。
4. **.github 仓 GOVERNANCE**：删除 CNB 桥接例外条目 **EX-1**。
   必须先提一个 ADR（决策：终止 CNB 免费算力桥接）再删条目——GOVERNANCE
   变更不可绕过 ADR，ADR 中引用本文件作为执行清单。
5. **talk 仓窗口**：#1-#100 中残留的派单/NPC 评论不清理（CNB 侧自然沉淀，
   无治理语义，不影响任何判定）。

## 删除后断言：gate / org-gate / conductor 语义不变

- **gate**：输入集是治理平面文件（GOVERNANCE/ADR/工作流清单），从未包含
  accounts.yaml / cnb_pool.py / CNB API 调用——删除零影响。
- **org-gate**：组织级校验规则中与本仓相关的仅 GOVERNANCE **EX-1** 一条，
  已随步骤 4 走 ADR 删除；其余规则不含 cnb-bridge 引用。
- **conductor**：编排输入为治理平面产物清单，不含本仓任何文件或工作流。
- **判定性验证**：组织内全仓搜索 `cnb`、`CNB_TOKEN`、`cnb-bridge` 零命中
  （org-gate 全绿）即认定删净；talk 仓 issue 评论除外（见步骤 5）。

## 不可随仓删除（治理平面 append-only 记录）

- 步骤 4 产生的终止 ADR 本身（历史不可变）；
- work-inbox 协议语义（IR-0004 D16）——若协议仍在用，其登记处迁回治理平面，
  仅失去本仓这份种子文件。
