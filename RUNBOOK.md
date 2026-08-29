# RUNBOOK.md —— CNB 账号生命周期 runbook（IR-0004 AC-16 / BEH-15）

> 目标：**陌生 AI 仅凭本文件**完成新账号入职，全流程无人工判断步骤；每步带
> 真实验收命令。四流程：入职 / 降级 / 退休 / 事故。一切状态变更留痕于
> append-only 生命周期台账 `lifecycle.jsonl`（hash 链，与 ledger.py 同一
> 规范化），纠错仅以追加方式（INV-05）。
>
> 硬纪律：token 永不入仓、永不入命令行明文、永不进 agent 上下文——只经
> org secret / 环境变量 `CNB_TOKEN_<ALIAS>`（[ADR-0086](https://github.com/Cloudbird-Software/archive/blob/main/adr/ADR-0086-cnb-token-decision.md) 缓解条款；正本=archive/adr/ADR-0086-cnb-token-decision.md，ADR-0085 家园单仓化）。

## 0. 台账规范（四流程共用）

`lifecycle.jsonl` 每行一个 JSON 对象，`prev_hash`/`hash` 构成 sha256 链
（创世行 prev_hash 为 64 个 `0`；hash 口径与 `ledger.py` 完全一致：对去掉
`hash` 字段后的行做 canonical JSON 的 sha256）。追加条目用下面的标准片段：

```bash
python - <<'EOF'
import json, hashlib, pathlib
entry = {                      # ← 只改这段字段（event/onboard|downgrade|retire|incident）
  "ts": "2026-08-24T00:00:00+00:00",
  "event": "onboard", "alias": "newbot", "actor": "<执行者身份>",
  "evidence": "evidence/newbot/",
}
p = pathlib.Path("lifecycle.jsonl")
lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []
prev = json.loads(lines[-1])["hash"] if lines else "0" * 64
entry["prev_hash"] = prev
entry["hash"] = hashlib.sha256(json.dumps(
    entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")
).encode("utf-8")).hexdigest()
with open(p, "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
EOF
```

验收（每流程结束都跑）：

```bash
python ledger.py --verify --state-dir . --file lifecycle.jsonl
# 期望输出 "ok": true；任何篡改/断链 → 非零退出（修复=追加 erratum 行，不改历史行）
```

## 1. 入职（onboard）——陌生 AI 可全流程执行

前置：拿到 owner 授权的 CNB 账号（令牌由 owner 在 CNB 侧创建并经 secret
通道注入，见步骤 2）；本仓（cnb-bridge）已 clone 且 Python 3.11+ 可用。

### 步骤 1.1 执行者不可变身份标识存证（入职前必做，AC-16）

执行入职的 AI 须存证"未参与本组织开发"。以你的公开 GitHub 登录名
`<LOGIN>` 执行并存盘（零命中=独立性成立；**非零命中即停**，改由他人执行）：

```bash
mkdir -p evidence/<ALIAS>
gh api user > evidence/<ALIAS>/executor-profile.json          # 执行者身份（不可变登录名/创建时间）
gh api -X GET search/issues -f q='author:<LOGIN> org:Cloudbird-Software' \
  -f per_page=100 > evidence/<ALIAS>/org-participation.json  # 本组织 issue/PR 参与记录
gh api "users/<LOGIN>/events/public?per_page=100" > evidence/<ALIAS>/public-events.json
python -c "import json;d=json.load(open('evidence/<ALIAS>/org-participation.json'));print('total_count=',d['total_count']);exit(0 if d['total_count']==0 else 1)"
# 验收：total_count=0（存证未参与开发）+ 会话记录一并落盘 evidence/<ALIAS>/
```

### 步骤 1.2 令牌入 org secret（owner 一步；AI 不可见值）

owner 在 CNB 侧创建账号与令牌后，把令牌只写入 org secret（命令行不回显、
不落盘；`--repos` 收敛可见面到两个接缝仓库）：

```bash
printf %s "$CNB_TOKEN_NEWBOT" | gh secret set CNB_TOKEN_NEWBOT \
  --org Cloudbird-Software \
  --repos "Cloudbird-Software/.github,Cloudbird-Software/cnb-bridge"
gh secret list --org Cloudbird-Software          # 验收：列表出现 CNB_TOKEN_NEWBOT（只显名不显值）
```

### 步骤 1.3 登记账号（加账号=改配置零代码）

编辑 `accounts.yaml`，新增条目（status 先置 `pending-access`，canary 过后
转 active）：

```yaml
  - alias: newbot
    secret_ref: CNB_TOKEN_NEWBOT
    description: 第三账号（runbook 入职 2026-08-24）
    quota_monthly_core_hours: 1600
    status: pending-access
    added_at: "2026-08-24"
```

验收（离线+在线各一）：

```bash
python cnb_pool.py status                 # 验收：newbot 出现且 token_present=true
python cnb_pool.py quota --account newbot # 验收：返回 dev_in_sec 等（HTTP 200）
```

### 步骤 1.4 同步接缝登记面（两处，PR 走治理平面）

1. `.github` 仓 `governance/policy/automation-limits.yaml` 的 `cnb.accounts`
   加 `{alias: newbot, secret: CNB_TOKEN_NEWBOT}`（与 accounts.yaml 对账，
   漂移即红）。
2. `.github` 仓 `.github/workflows/cnb-dispatch.yml` 与本仓
   `.github/workflows/weekly-audit.yml` 的 secret env 映射各加一行
   `CNB_TOKEN_NEWBOT: ${{ secrets.CNB_TOKEN_NEWBOT }}`。

### 步骤 1.5 canary 任务（最终验收）

```bash
python cnb_pool.py dispatch "canary：运行 printf 'canary-ok\n' 并原样贴输出与退出码" \
  --account newbot --tier light --run-id canary01
# 记下返回的 window N；回收：
python cnb_pool.py collect --account newbot --window <N> --run-id canary01
# 验收：回复含 [run:canary01]、canary-ok 与 exit 0——锚串不符一律弃收（不采信自报）
# 档位证实（light=1C）由周审计 build logs 对账承担：python ledger.py --reconcile
```

通过后把 `accounts.yaml` 的 newbot `status: pending-access` 改为 `active`，
追加台账条目（步骤 0 片段，event=onboard），并提交 PR 并回（含 evidence/）。

### 步骤 1.6 "陌生 AI 仅凭本 runbook 完成入职"验收清单

- [ ] `evidence/<ALIAS>/` 三份存证文件存在且 org-participation `total_count=0`
- [ ] `python cnb_pool.py status`：新账号 `token_present=true`
- [ ] `python cnb_pool.py quota --account <ALIAS>`：HTTP 200 返回 dev_in_sec
- [ ] canary 回收文本含 `[run:canary01]` 与 `exit 0`
- [ ] `accounts.yaml` status=active；automation-limits/两工作流 env 映射同步
- [ ] `python ledger.py --verify --state-dir . --file lifecycle.jsonl` → ok:true
- [ ] 全程零 token 明文（shell history / 文件 / PR 均无）

## 2. 降级（downgrade）——配额吃紧/风控暂停

1. 编辑 `accounts.yaml`：目标账号 `status: active` → `degraded`（保留条目，
   不删行——审计线索 append-only）。
2. 验收（调度面立即拒用）：

   ```bash
   python cnb_pool.py status                       # 验收：status=degraded
   python cnb_pool.py dispatch "任何任务" --account <ALIAS>   # 验收：非零退出（不可用）
   ```

3. 追加台账条目（event=downgrade，note 写原因：配额低于阈值/风控/Owner 裁决）。
4. 恢复：`status: degraded` → `active` + 台账 event=onboard(note=restore)。

## 3. 退休（retire）——永久退出账号

1. CNB 侧吊销令牌（owner，平台操作，无 gh 命令——留截图/时间戳入 evidence）。
2. 删 org secret：

   ```bash
   gh secret delete CNB_TOKEN_<ALIAS> --org Cloudbird-Software
   gh secret list --org Cloudbird-Software      # 验收：列表不再含该键
   ```

3. `accounts.yaml` 条目置 `status: retired`（保留行；secret_ref 保留供审计），
   台账追加 event=retire。
4. 验收：`python cnb_pool.py status`（status=retired、token_present=false）、
   台账 verify ok。

## 4. 事故（incident）——token 泄漏/异常用量（ADR-0086 泄漏应急三步）

**吊销 → 轮换 → 审计**（顺序不可换；每步完成即追加台账 event=incident）：

1. **吊销**：CNB 侧立即吊销该令牌（止血优先于取证）。
2. **轮换**：owner 新建令牌并覆盖同一 org secret（键名不变，引用面零改动）：

   ```bash
   printf %s "$CNB_TOKEN_<ALIAS>_NEW" | gh secret set CNB_TOKEN_<ALIAS> \
     --org Cloudbird-Software \
     --repos "Cloudbird-Software/.github,Cloudbird-Software/cnb-bridge"
   python cnb_pool.py quota --account <ALIAS>    # 验收：新令牌可用
   ```

3. **审计**（泄漏窗口内的账目与产物全面复核）：

   ```bash
   python ledger.py --verify                     # 台账 hash 链未被动过
   python ledger.py --reconcile --threshold-pct 10   # 异常用量尖峰对账
   python audit_extra.py --sample fanout/products 100 # 泄漏窗口产物全量核对（BUDGET-03 可升不可免）
   python audit_extra.py                          # 周审计全项（fail-closed）
   ```

   检出异常派单/作废产物 → 开 incident issue 留痕，处置记录追加台账
   （event=incident，含 issue 链接与审计报告路径）。

> 本节为 ADR-0086 缓解条款③的执行细则；三步完成前该账号视为不可信，
> `accounts.yaml` 置 `status: degraded` 阻断调度。

## 5. selfcloud 内网调度器 v0（IR-0006 W2-C1 / 卡 #412 / ADR-0103 决策 4）

> 服务器侧 Go 调度器：消费 Job Contract、worker 池内短票据签发（TTL≤波次）、
> egress allowlist 与无状态约束执法。判定与验收锚 GitHub（INV-02：本调度器=
> 可删除执行层，删除断言见 REMOVAL.md §selfcloud）。

### 5.1 部署位与构建

代码=本仓 `selfcloud/`（Go module `cloudbird/software/selfcloud`），构建产物
部署到公网服务器（内网调度器宿主，assets-register `public-server`）：

```bash
cd selfcloud && go vet ./... && go test ./...   # 测试先行（AC 锚）
go build -o /usr/local/bin/selfcloud .
```

### 5.2 日常流程（一次作业）

1. **契约校验**（fail-closed：非法即 exit 3，不签票据不拨号）：

   ```bash
   selfcloud validate --contract job.json --allowlist api.github.com,vault.internal
   # 执法点：schema v1 / card join key 形态 / TTL≤240min（波次上限）/
   #         persistent_state=false + state_volumes=[]（无状态约束）/
   #         egress_needs ⊆ 环境 allowlist
   ```

2. **短票据签发**（worker 零持久凭据；HMAC 密钥从内网 Vault 注入 `--key-file`，
   密钥值永不进 git/agent 上下文——INV-04）：

   ```bash
   selfcloud issue-ticket --contract job.json --key-file /vault/selfcloud-hmac.key
   ```

3. **worker 执行完毕票据到期自动失效**；到期前显式收回则提前 revoke。

4. **事件入统一账本**（schema v1 链式，与 .github 仓 evidence_shadow.py
   逐字节兼容——跨语言一致由 ledger_test.go 金向量锚定）：

   ```bash
   selfcloud emit-ledger --file tickets.jsonl --event-file grant.json
   selfcloud emit-ledger --file tickets.jsonl --event-file revoke.json
   python3 <.github 仓>/governance/evidence_shadow.py verify --file tickets.jsonl
   ```

   真实落盘：服务器持代签令牌将 `tickets.jsonl` 推送本仓 `tickets-ledger`
   分支（append-only；供 evidence-query 统一查询按 subject 消费）。

### 5.3 执法边界（AC-5a）

- **egress allowlist**：worker 侧唯一出向通道经 `EgressPolicy.GuardDial`——
  目的主机不在环境白名单即拒连（`worker 不直连公网`）；白名单真源=
  env 定义仓 `environments/*.yaml` 的 `network.egress_allowlist`，v0 经
  `--allowlist` 注入。
- **无状态约束**：`persistent_state=true` 或 `state_volumes` 非空 → 拒置
  （云电脑池 worker 每作业重置，state 只能存 git/artifacts/内网 blob）。
- **票据 TTL**：签发时 TTL=契约 `ttl_minutes`（上限 240=波次租约上限，
  对齐 arbiter capabilities.yaml defaults.ttl_minutes）；过期即失效，
  验签 `now > expires_at` → 拒（BEH-05"票据是瞬时能力"）。

## 6. PM 凭证收敛：GitHub App 令牌服务器代签（IR-0006 W2-C2 / 卡 #413）

> 目标态（AC-6a/INV-04）：PM 会话在云电脑上零长期凭据——写仓令牌由服务器
> 用 cloudbrid-agent App 私钥（Vault 注入 `--key-file`，密钥值永不进
> git/agent 上下文）代签：单仓作用域（`repositories` 限定，不提供全安装
> 作用域模式）+ 短 TTL（GitHub 固定 1h ≤ 波次 240min，`MintToken` 越界即拒）。
> 个人 PAT 退出日常流程，仅留应急回退通道（见 .github 仓
> `docs/pm-credential-convergence.md`：App 失效→owner PAT，24h 窗口）。

### 6.1 日常流程（PM 会话取令牌）

```bash
# 服务器侧（PM 会话经内网调服务器执行；密钥在服务器，PM 只收令牌）：
selfcloud gh-token --app-id $CB_APP_ID --key-file /vault/cloudbrid-agent.pem \
  --repo .github --card Cloudbird-Software/.github#<n> \
  --ledger /var/lib/selfcloud/tickets.jsonl --expect-token
# stdout 一次性交付 {"token":"ghs_…","expires_at":"…","repo":"…"}（不落日志/账本）
# token.grant 事件已入账（payload 只含 repo/expires_at/ttl，零令牌值）

# PM 会话侧消费：
export GH_TOKEN=<上一步交付的 token>   # 1h 后自动失效；提前收回见 6.2
gh pr create ...                       # 身份=cloudbrid-agent[bot]，ruleset 照样生效
```

### 6.2 收回语义（AC-6b：收回断言日志）

```bash
# 提前收回（作业完成即收回，不等 TTL）：
selfcloud gh-token-revoke --token <t> --card o/r#n --repo o/r \
  --ledger /var/lib/selfcloud/tickets.jsonl
#   → DELETE /installation/token（204）+ 探活断言 HTTP 401 + token.revoke 入账

# TTL 到期自动收回断言（GitHub 侧到期即失效；到期后探活必 401）：
selfcloud gh-token-revoke --token <t> --card o/r#n --repo o/r \
  --ledger /var/lib/selfcloud/tickets.jsonl --expired-only
#   → 跳过 DELETE（已死无需收）；断言 401 + token.revoke(reason=ttl-expired) 入账

# 单纯断言（不动账本）：
selfcloud gh-token-check --token <t> --repo o/r   # 200=有效（exit 0）/ 401=已收回（exit 3）
```

### 6.3 真实演练（drill 工作流）

`selfcloud-tokenagent-drill.yml`（本仓）：workflow_dispatch 手动触发，用 org
secrets（`CB_APP_ID`/`AGENT_APP_SECRET`，Actions secret 面注入——PM 上下文
零接触）走真实全链路：代签→探活 200→（revoke 模式：DELETE+401 断言 /
expiry 模式：等 TTL 到期+401 断言）→事件链式入 `tickets.jsonl`→push
`tickets-ledger` 分支（evidence-query 第 5 源按 subject 可查）。
