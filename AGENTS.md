# AGENTS.md —— cnb-bridge 索引

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
