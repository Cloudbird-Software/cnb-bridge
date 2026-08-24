#!/usr/bin/env python3
"""cnb_pool.py —— CNB 免费算力桥：账号池 + 客户端 + 派单/回收闭环（合一模块）。

定位（见 REMOVAL.md）：本模块属 cnb-bridge **可删除层**——独立于核心治理
（gate / org-gate / conductor），整体删除不影响任何治理语义。仅服务于
"发散与执行侧"的临时算力需求；判定（评审/裁决/合并）永不外置到 CNB。

闭环（参考已实战验证的最小客户端 dogfood/cnb.py）：
  dispatch —— 在 talk 仓空闲窗口（issue #1-#100）发 @CodeBuddy 评论
              （请求体 {"body": "@CodeBuddy ...", "work_mode": true}）
  collect  —— 轮询同窗口，直到出现含 [run:xxxx] 锚串的 NPC 回复
  quota / windows / build_logs —— 配额观测（dev_in_sec 等）与窗口占用观测

窗口占用判定：issue 最后一条评论 author.is_npc == true → 空闲；
无评论 → 空闲；最后一条是人类评论 → 占用中（已派单、NPC 未回）。

纪律：
  - token 只经环境变量 CNB_TOKEN_<ALIAS> 注入（org secret 同名），永不入仓、
    永不硬编码、缺失即抛错、无任何 fallback。
  - fail-closed：API 非 2xx / 网络不可达 / 配置损坏一律抛
    CnbApiError / AccountPoolError，绝不用 try 吞错后假装成功。
  - NPC 自报数字一律不采信：产物必须由调用方机械核对（README 安全边界）。

CLI（token 从 env）：
  python cnb_pool.py status                        # 账号池离线状态
  python cnb_pool.py quota     [--account 别名]     # 配额
  python cnb_pool.py windows   [--account 别名]     # 窗口占用
  python cnb_pool.py dispatch "任务正文" [--tier light] [--account 别名] [--run-id X]
  python cnb_pool.py collect --account 别名 --window N --run-id X [--timeout 秒]

自检：python -m py_compile cnb_pool.py
      python -m unittest discover -s tests -v
仅依赖 Python 3.11+ 标准库；YAML 用内置极简解析器（不 import yaml）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

API_HOST = "https://api.cnb.cool"
MAX_WINDOWS = 100  # talk 仓窗口上限：issue #1-#100
DEFAULT_ACCOUNTS_YAML = Path(__file__).resolve().parent / "accounts.yaml"

# 算力档位规则（与 accounts.yaml dispatch.default_tier 注释一致；8C 禁用）
TIER_RULES = {
    "light": "1C，默认档，无需附加条件",
    "std": "2C，任务正文须包含『理由』字样",
    "heavy": "4C，任务正文须引用 ADR 编号（如 ADR-0042）",
}


class CnbApiError(Exception):
    """CNB API 错误（fail-closed）。status 携带 HTTP 状态码；网络不可达时为 None。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AccountPoolError(Exception):
    """账号池/配置错误：文件缺失、结构损坏、token 环境变量缺失、无可用账号等。"""


class DispatchError(AccountPoolError):
    """派单失败：档位非法、超出单账号并发上限、全部窗口占用等。"""


def _warn(msg: str) -> None:
    sys.stderr.write(f"[cnb_pool] 警告: {msg}\n")


# ────────────────────────── 极简 YAML 解析 ──────────────────────────


def _scalar(raw: str):
    """标量解析：去引号 → 去行内注释 → int/str（写法参考 verify_migration.py）。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]  # 引号值可能内含 '#'，先整体去引号、不再切注释
    v = v.split(" #", 1)[0].strip()  # 去行内注释（如 default_tier: light # ...）
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_accounts_yaml(text: str) -> dict:
    """解析 accounts.yaml 的固定结构（顶层 key: value / `accounts:` 下两空格
    缩进 `- key: value` 列表项与四空格 kv / `dispatch:` 下两空格 kv）。

    降级解析器写法参考 archive/scripts/verify_migration.py；不 import yaml。
    结构漂移的行只告警跳过（sys.stderr），关键缺失由 AccountPoolError 拦截。
    """
    data: dict = {"version": None, "accounts": [], "dispatch": {}}
    cur: dict | None = None  # 当前列表项（accounts 条目）
    section: str | None = None  # 当前顶层段落头（accounts / dispatch）
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m_item = re.match(r"^\s*-\s+(\w[\w.]*):\s*(.*)$", raw)
        m_kv = re.match(r"^\s+(\w[\w.]*):\s*(.*)$", raw)
        m_top = re.match(r"^(\w[\w.]*):\s*(.*)$", raw)
        if m_item:
            if section == "accounts":
                if cur is not None:
                    data["accounts"].append(cur)
                cur = {m_item.group(1): _scalar(m_item.group(2))}
            else:
                _warn(f"accounts.yaml 第 {lineno} 行：列表项出现在段 {section!r} 之外，已跳过")
        elif m_kv:
            key, val = m_kv.group(1), _scalar(m_kv.group(2))
            if cur is not None and section == "accounts":
                cur[key] = val
            elif section == "dispatch":
                data["dispatch"][key] = val
            else:
                _warn(f"accounts.yaml 第 {lineno} 行：键 {key!r} 无所属段落，已跳过")
        elif m_top:
            if cur is not None:  # 段落头意味着上一列表收尾
                data["accounts"].append(cur)
                cur = None
            val = _scalar(m_top.group(2))
            if val == "":
                section = m_top.group(1)  # 段落头（accounts: / dispatch:）
                if section not in ("accounts", "dispatch"):
                    _warn(f"accounts.yaml 第 {lineno} 行：未知段落 {section!r}（仅支持 accounts/dispatch）")
            else:
                section = None
                data[m_top.group(1)] = val
        else:
            _warn(f"accounts.yaml 第 {lineno} 行无法解析（已跳过）: {raw!r}")
    if cur is not None:
        data["accounts"].append(cur)
    return data


# ────────────────────────── CNB 客户端 ──────────────────────────


class CnbClient:
    """CNB API（api.cnb.cool）最小客户端。

    - 认证：Bearer token；Content-Type/Accept 均 application/vnd.cnb.api+json
    - 错误：HTTPError/URLError 一律转 CnbApiError（status 带状态码），不吞错。
    """

    def __init__(self, token: str, repo: str, timeout: int = 60):
        if not token:
            raise ValueError("CnbClient 拒绝空 token（token 须经环境变量注入）")
        self.token = token
        self.repo = repo  # 形如 Cloudbird-Software/talk
        self.timeout = timeout
        self.base = f"{API_HOST}/{repo}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/vnd.cnb.api+json",
            "Accept": "application/vnd.cnb.api+json",
        }

    def _request(self, url: str, data: dict | None = None, method: str | None = None):
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers=self._headers,
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise CnbApiError(
                f"CNB API HTTP {e.code} {url}: {e.reason}", status=e.code
            ) from e
        except urllib.error.URLError as e:
            raise CnbApiError(f"CNB API 不可达 {url}: {e.reason}", status=None) from e
        if not payload.strip():
            return None
        return json.loads(payload)  # 非法 JSON 让其抛错——fail-closed

    # ── 窗口（issue）与评论 ──
    def list_issues(self) -> list:
        return self._request(f"{self.base}/-/issues?page_size=200") or []

    def comments(self, issue_n: int) -> list:
        return self._request(f"{self.base}/-/issues/{issue_n}/comments?page_size=50") or []

    def last_comment(self, issue_n: int) -> dict | None:
        cs = self.comments(issue_n)
        return cs[-1] if cs else None

    def is_free(self, issue_n: int) -> bool:
        """窗口占用判定：无评论 → 空闲；最后评论 author.is_npc==true → 空闲；
        最后评论是人类（is_npc==false）→ 占用中。"""
        c = self.last_comment(issue_n)
        if c is None:
            return True
        author = c.get("author") or {}
        return author.get("is_npc") is True

    def post_comment(self, issue_n: int, body: str, work_mode: bool = True) -> dict:
        """派单：POST {"body": body, "work_mode": work_mode}。"""
        return self._request(
            f"{self.base}/-/issues/{issue_n}/comments",
            data={"body": body, "work_mode": work_mode},
        )

    # ── 配额与沙箱观测 ──
    def quota(self) -> dict:
        """GET /{group}/-/charge/quota（group 取 repo 的 owner 段），如 dev_in_sec。"""
        group = self.repo.split("/", 1)[0]
        return self._request(f"{API_HOST}/{group}/-/charge/quota")

    def build_logs(self) -> list:
        """沙箱观测：GET /{repo}/-/build/logs。"""
        return self._request(f"{self.base}/-/build/logs") or []


def windows_status(client: CnbClient, max_windows: int = MAX_WINDOWS) -> list:
    """逐窗口占用全景：只查前 max_windows（默认 100）个窗口。

    返回 [{"number": n, "free": bool}, ...]（按窗口号升序）。
    占用判定见 CnbClient.is_free；API 错误直接抛 CnbApiError（fail-closed）。
    """
    issues = client.list_issues()
    nums = set()
    for i in issues:
        n = int(i.get("number") or 0)
        if 1 <= n <= max_windows:
            nums.add(n)
    if not nums:
        nums = set(range(1, max_windows + 1))  # 列表为空时退化为直查 #1-#N
    return [{"number": n, "free": client.is_free(n)} for n in sorted(nums)]


def busy_count(statuses: list) -> int:
    return sum(1 for s in statuses if not s["free"])


# ────────────────────────── 账号池 ──────────────────────────


@dataclass
class Account:
    alias: str
    secret_ref: str
    description: str = ""
    quota_monthly_core_hours: int = 0
    status: str = "active"
    added_at: str = ""


class AccountPool:
    """多账号池：登记于 accounts.yaml（无明文 token），token 经环境变量注入。"""

    def __init__(self, accounts_yaml_path: str | Path = DEFAULT_ACCOUNTS_YAML):
        path = Path(accounts_yaml_path)
        if not path.is_file():
            raise AccountPoolError(f"accounts.yaml 不存在: {path}")
        data = parse_accounts_yaml(path.read_text(encoding="utf-8"))
        self.version = data.get("version")
        raw_accounts = data.get("accounts") or []
        if not raw_accounts:
            raise AccountPoolError("accounts.yaml 未解析到任何账号（结构损坏或为空）")
        self.accounts: list[Account] = []
        for i, rc in enumerate(raw_accounts, 1):
            alias, secret_ref = rc.get("alias"), rc.get("secret_ref")
            if not alias or not secret_ref:
                raise AccountPoolError(f"第 {i} 个账号缺少 alias/secret_ref: {rc}")
            self.accounts.append(
                Account(
                    alias=str(alias),
                    secret_ref=str(secret_ref),
                    description=str(rc.get("description") or ""),
                    quota_monthly_core_hours=int(rc.get("quota_monthly_core_hours") or 0),
                    status=str(rc.get("status") or "active"),
                    added_at=str(rc.get("added_at") or ""),
                )
            )
        aliases = [a.alias for a in self.accounts]
        if len(set(aliases)) != len(aliases):
            raise AccountPoolError(f"alias 重复: {aliases}")
        d = data.get("dispatch") or {}
        self.dispatch_cfg = {
            "repo": str(d.get("repo") or "Cloudbird-Software/talk"),
            "windows": str(d.get("windows") or f"#1-#{MAX_WINDOWS}"),
            "default_tier": str(d.get("default_tier") or "light"),
            "max_concurrent_per_account": int(d.get("max_concurrent_per_account") or 8),
            "poll_interval_sec": int(d.get("poll_interval_sec") or 20),
            "reply_timeout_sec": int(d.get("reply_timeout_sec") or 600),
        }

    # ── 查找与凭据 ──
    def by_alias(self, account: str | Account) -> Account:
        if isinstance(account, Account):
            return account
        for a in self.accounts:
            if a.alias == account:
                return a
        raise AccountPoolError(f"未知账号别名: {account!r}（登记于 accounts.yaml）")

    def token_for(self, account: str | Account) -> str:
        """token 从 os.environ[secret_ref] 取；缺失即抛错（无任何硬编码 fallback）。"""
        acct = self.by_alias(account)
        try:
            return os.environ[acct.secret_ref]
        except KeyError:
            raise AccountPoolError(
                f"环境变量 {acct.secret_ref} 未设置（账号 {acct.alias}）——"
                f"token 永不入仓，须运行前注入（本地 export / CI org secret 同名）；"
                f"不存在任何硬编码 fallback"
            ) from None

    def client_for(self, account: str | Account) -> CnbClient:
        acct = self.by_alias(account)
        return CnbClient(self.token_for(acct), self.dispatch_cfg["repo"])

    def _usable(self, account: Account) -> bool:
        return account.status == "active" and bool(os.environ.get(account.secret_ref))

    def available(self) -> list[Account]:
        """可用账号 = status==active 且 token 环境变量已注入。"""
        return [a for a in self.accounts if self._usable(a)]

    # ── 调度 ──
    def pick(self, prefer: str | None = None) -> Account:
        """选派单账号。prefer 指定别名（必须可用）；默认在可用账号中选
        当前占用窗口数最少者（经 windows_status 计数；并列取登记顺序）。
        网络查询失败直接抛错（fail-closed，不降级猜数）。"""
        if prefer is not None:
            acct = self.by_alias(prefer)
            if not self._usable(acct):
                raise AccountPoolError(
                    f"账号 {prefer} 不可用（status={acct.status} 或环境变量 "
                    f"{acct.secret_ref} 未注入）"
                )
            return acct
        avail = self.available()
        if not avail:
            raise AccountPoolError(
                "无可用账号：需至少一个 status=active 且 token 环境变量已注入的账号"
            )
        best: Account | None = None
        best_busy = -1
        for a in avail:
            busy = busy_count(windows_status(self.client_for(a)))
            if best is None or busy < best_busy:
                best, best_busy = a, busy
        return best  # type: ignore[return-value]

    def _check_tier(self, tier: str, task_body: str) -> None:
        if tier not in TIER_RULES:
            raise DispatchError(
                f"非法算力档位 {tier!r}：允许 {sorted(TIER_RULES)}（8C 禁用）"
            )
        if tier == "std" and "理由" not in task_body:
            raise DispatchError("std 档（2C）须在任务正文写明理由（含『理由』字样）")
        if tier == "heavy" and not re.search(r"ADR-\d+", task_body):
            raise DispatchError("heavy 档（4C）须在任务正文引用 ADR 编号（如 ADR-0042）")

    def dispatch(
        self,
        task_body: str,
        tier: str = "light",
        account: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        """完整派单闭环：选账号 → 找空闲窗口 → post @CodeBuddy 评论。

        返回 {"account", "window", "run_id", "posted_at", "tier"}。
        正文自动拼装：@CodeBuddy 前缀 + 真实执行纪律句 + [run:xxxx] 对账锚串。
        """
        self._check_tier(tier, task_body)
        run_id = run_id or secrets.token_hex(4)
        if account is not None:
            candidates = [self.by_alias(account)]
            if not self._usable(candidates[0]):
                raise AccountPoolError(
                    f"账号 {account} 不可用（status={candidates[0].status} 或环境变量 "
                    f"{candidates[0].secret_ref} 未注入）"
                )
        else:
            candidates = self.available()
            if not candidates:
                raise AccountPoolError(
                    "无可用账号：需至少一个 status=active 且 token 环境变量已注入的账号"
                )
        chosen: Account | None = None
        chosen_statuses: list = []
        chosen_busy = -1
        for a in candidates:  # 逐账号查占用，选最少者（指定 account 时仅一个）
            st = windows_status(self.client_for(a))
            b = busy_count(st)
            if chosen is None or b < chosen_busy:
                chosen, chosen_statuses, chosen_busy = a, st, b
        assert chosen is not None
        cap = self.dispatch_cfg["max_concurrent_per_account"]
        if chosen_busy >= cap:
            raise DispatchError(
                f"账号 {chosen.alias} 占用 {chosen_busy} 窗口，已达单账号并发上限 {cap}"
            )
        free = [s["number"] for s in chosen_statuses if s["free"]]
        if not free:
            raise DispatchError(
                f"账号 {chosen.alias}：{MAX_WINDOWS} 个窗口全部占用中——稍后重试或换账号"
            )
        window = free[0]  # 升序取最靠前的空闲窗口（确定性，便于对账）
        body = build_task_body(task_body, tier, run_id)
        self.client_for(chosen).post_comment(window, body, work_mode=True)
        return {
            "account": chosen.alias,
            "window": window,
            "run_id": run_id,
            "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tier": tier,
        }

    def collect(
        self,
        account: str | Account,
        window: int,
        run_id: str,
        timeout: int | None = None,
    ) -> str | None:
        """轮询窗口直至 NPC 回复（is_npc 且正文含 [run:run_id]），返回回复全文。

        超时返回 None（并向 stderr 打印说明：任务仍在执行/派单未生效/丢锚串）。
        """
        timeout = self.dispatch_cfg["reply_timeout_sec"] if timeout is None else timeout
        interval = max(1, self.dispatch_cfg["poll_interval_sec"])
        acct = self.by_alias(account)
        client = self.client_for(acct)
        anchor = f"[run:{run_id}]"
        deadline = time.monotonic() + timeout
        while True:
            c = client.last_comment(window)
            if c is not None:
                author = c.get("author") or {}
                body = c.get("body") or ""
                if author.get("is_npc") is True and anchor in body:
                    return body
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
        _warn(
            f"collect 超时（{timeout}s）：账号 {acct.alias} 窗口 #{window} 未见含 "
            f"{anchor} 的 NPC 回复——任务可能仍在执行、派单未生效或回复丢锚串；"
            f"可用 build_logs 观测沙箱后重试"
        )
        return None


def build_task_body(task_body: str, tier: str, run_id: str) -> str:
    """派单正文：以 @CodeBuddy 开头；含执行纪律句与 [run:xxxx] 对账锚串。"""
    return (
        f"@CodeBuddy {task_body}\n\n"
        f"执行纪律：必须真实执行并贴出原始输出与退出码（不要只描述、不要推演，"
        f"在沙箱内实际运行后原样粘贴）。\n"
        f"回复必须原样包含对账锚串 [run:{run_id}]，并注明所用算力档位 {tier}。"
    )


# ────────────────────────── CLI ──────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cnb_pool.py",
        description="CNB 免费算力桥 CLI——token 经环境变量 CNB_TOKEN_<ALIAS> 注入，永不入仓",
    )
    ap.add_argument(
        "--accounts-yaml",
        default=str(DEFAULT_ACCOUNTS_YAML),
        help="accounts.yaml 路径（默认：脚本同目录）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_quota = sub.add_parser("quota", help="查询配额（GET /{group}/-/charge/quota，如 dev_in_sec）")
    p_quota.add_argument("--account", default=None, help="账号别名（默认自动选占用最少者）")

    p_win = sub.add_parser("windows", help="列出窗口占用全景（#1-#100）")
    p_win.add_argument("--account", default=None, help="账号别名（默认自动选占用最少者）")

    p_dis = sub.add_parser("dispatch", help="派单：选账号 → 占空闲窗口 → 发 @CodeBuddy 评论")
    p_dis.add_argument("body", help="任务正文（自动加 @CodeBuddy 前缀、执行纪律句与锚串）")
    p_dis.add_argument("--tier", default="light",
                       help="light=1C 默认 / std=2C 正文须含理由 / heavy=4C 正文须引用 ADR；8C 禁用")
    p_dis.add_argument("--account", default=None, help="账号别名（默认自动选占用最少者）")
    p_dis.add_argument("--run-id", default=None, help="指定锚串 id（默认随机 8 位 hex）")

    p_col = sub.add_parser("collect", help="回收：轮询窗口至含 [run:xxxx] 锚串的 NPC 回复")
    p_col.add_argument("--account", required=True, help="账号别名")
    p_col.add_argument("--window", type=int, required=True, help="窗口（issue 号）")
    p_col.add_argument("--run-id", required=True, help="派单返回的 run_id")
    p_col.add_argument("--timeout", type=int, default=None,
                       help="秒（默认 accounts.yaml 的 reply_timeout_sec）")

    sub.add_parser("status", help="账号池状态（离线，不查网络）")

    args = ap.parse_args(argv)
    try:
        pool = AccountPool(args.accounts_yaml)
        if args.cmd == "status":
            rows = [
                {
                    "alias": a.alias,
                    "status": a.status,
                    "token_present": bool(os.environ.get(a.secret_ref)),
                    "secret_ref": a.secret_ref,
                    "quota_monthly_core_hours": a.quota_monthly_core_hours,
                    "added_at": a.added_at,
                }
                for a in pool.accounts
            ]
            print(json.dumps(
                {"version": pool.version, "dispatch": pool.dispatch_cfg, "accounts": rows},
                ensure_ascii=False, indent=2))
        elif args.cmd == "quota":
            acct = pool.pick(args.account)
            q = pool.client_for(acct).quota()
            print(json.dumps({"account": acct.alias, "quota": q},
                             ensure_ascii=False, indent=2))
            # 审计契约行（cnb-audit grep 消费）：仅在字段齐备可算时输出数值——
            # 未知结构不假装（remaining_pct 行缺席=审计不告警，fail-open 面由
            # cnb-audit 的"配额查询失败即红"与周人工核对面兜底）
            dev = q.get("dev_in_sec") if isinstance(q, dict) else None
            used = None
            if isinstance(q, dict):
                used = q.get("used_in_sec") or q.get("dev_in_sec_used")                        or (q.get("used") or {}).get("dev_in_sec")                        if isinstance(q.get("used"), dict) else q.get("used_in_sec")
            if isinstance(dev, (int, float)) and dev > 0 and isinstance(used, (int, float)):
                print(f"account={acct.alias} remaining_pct={max(0, round((dev - used) / dev * 100))}")
        elif args.cmd == "windows":
            acct = pool.pick(args.account)
            sts = windows_status(pool.client_for(acct))
            busy = [s["number"] for s in sts if not s["free"]]
            print(json.dumps(
                {"account": acct.alias, "total": len(sts),
                 "free": len(sts) - len(busy), "busy_windows": busy},
                ensure_ascii=False, indent=2))
            # 审计契约行（cnb-audit windows 步 grep status= 消费）
            for s in sts:
                print(f"window={s['number']} status={'free' if s['free'] else 'occupied'}")
        elif args.cmd == "dispatch":
            r = pool.dispatch(args.body, tier=args.tier,
                              account=args.account, run_id=args.run_id)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            print(
                f"回收：python cnb_pool.py collect --account {r['account']} "
                f"--window {r['window']} --run-id {r['run_id']}",
                file=sys.stderr,
            )
        elif args.cmd == "collect":
            body = pool.collect(args.account, args.window, args.run_id,
                                timeout=args.timeout)
            if body is None:
                return 1  # 超时说明已由 collect 输出到 stderr
            print(body)
    except (CnbApiError, AccountPoolError) as e:
        status = getattr(e, "status", None)
        prefix = f"[HTTP {status}] " if status is not None else ""
        print(f"{prefix}错误: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
