# cnb-bridge —— CNB 免费算力桥（可删除层）

Cloudbird-Software 把 CNB 平台（api.cnb.cool）提供的免费 AI 沙箱（CodeBuddy
NPC）当作**执行平面**的临时算力：`talk` 仓的 issue #1-#100 是 100 个"窗口"，
向空闲窗口发一条以 `@CodeBuddy` 开头的评论（`work_mode: true`）即完成派单，
NPC 异步执行后回帖。本仓是这套路面的全部桥接代码与登记。

## 定位：临时措施、可删除层

- **临时措施**：依赖 CNB 免费配额（`GET /Cloudbird-Software/-/charge/quota`
  返回 `dev_in_sec` 等，随时可能收紧或消失）。
- **可删除层**：独立于核心治理（gate / org-gate / conductor），随时可整体
  删除，删除清单见 [REMOVAL.md](REMOVAL.md)。
- **发散与执行侧专用**：只服务探索性任务与机械执行（跑命令、贴原始输出）。
  **判定（评审/裁决/合并决定）永不外置到 CNB**——判定永远留在治理平面。

## 三平面

```
+-----------------------------------------------+
|  规划平面 · 强模型 PM                           |
|  work-inbox.yaml 自起工作发现 / 派单决策 / 产物机械核对 |
+------------------------+----------------------+
                         |  dispatch(task) / collect(run_id)
+------------------------v----------------------+
|  治理平面 · GitHub（Cloudbird-Software/*）      |
|  org-gate / conductor / ADR / Actions secrets  |
+------------------------+----------------------+
                         |  CNB_TOKEN_<ALIAS> 注入 + api.cnb.cool REST
+------------------------v----------------------+
|  执行平面 · CNB（talk 仓 #1-#100 窗口）         |
|  CodeBuddy NPC 免费沙箱 / 配额 / build logs     |
+-----------------------------------------------+
```

## 快速上手

token 永不入仓：本地经环境变量注入（CI 用 org secret 同名注入）：

```bash
export CNB_TOKEN_XUEMEI=<token>      # bash
$env:CNB_TOKEN_XUEMEI = "<token>"    # PowerShell
```

四个命令（仓库根目录，Python 3.11+ 标准库 only）：

```bash
python cnb_pool.py quota                            # 配额（dev_in_sec 等）
python cnb_pool.py windows                          # 窗口占用全景（#1-#100）
python cnb_pool.py dispatch "跑 X 并贴原始输出"       # 派单（自动占窗+锚串）
python cnb_pool.py collect --account xuemei --window 7 --run-id <id>
                                                    # 回收 NPC 回复
```

（另有 `status`：离线查看账号池与 token 注入情况。）

派单返回 `{account, window, run_id, posted_at, tier}`；`collect` 轮询至含
`[run:<run_id>]` 锚串的 NPC 回复并打印全文，超时返回 None 并在 stderr 说明。

## 账号池

见 [accounts.yaml](accounts.yaml)：多账号登记（别名 / secret 引用 / 月度核时
配额），**无任何明文 token**。调度策略：默认选当前占用窗口数最少的可用账号，
`--account` 指定别名；单账号并发上限 `max_concurrent_per_account: 8`。
档位纪律：`light`(1C) 默认 / `std`(2C) 正文须含理由 / `heavy`(4C) 正文须引用
ADR / **8C 禁用**（dispatch 直接拒绝）。

## 安全边界

1. **token 纪律**：token 只经环境变量 `CNB_TOKEN_<ALIAS>`（org secret 同名）
   注入，仓内只存 `secret_ref`；缺失即抛错，无任何 fallback，永不硬编码。
2. **NPC 产物必须机械核对**：沙箱自报数字（耗时/成功率/配额消耗）一律不采信；
   以退出码、原始输出与本地复跑对账；锚串 `[run:xxxx]` 不匹配的回复一律弃收。
3. **fail-closed**：API 非 2xx、网络不可达、配置损坏一律抛错退出（异常带
   HTTP 状态码），绝不吞错假装成功。
4. **判定不外置**：CNB 只执行；一切评审与合并决定留在治理平面。

## 自检

```bash
python -m py_compile cnb_pool.py
python -m unittest discover -s tests -v
```
