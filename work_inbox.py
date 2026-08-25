#!/usr/bin/env python3
"""work_inbox.py —— PM 自起工作发现协议执行器（IR-0004 D16 / AC-17 / BEH-16）。

定位（见 REMOVAL.md/README）：cnb-bridge **可删除层**。协议语义记录在治理平面
（IR-0004 D16），本脚本只是协议在 work-inbox.yaml 上的机械执行器；强模型 PM
按小时自起 `--check`，三态输出（claim / heartbeat / none）。

协议五则（与 work-inbox.yaml 尾注一致，改动须双侧同步）：
  1. 认领带租约：写 lease（holder+expires_at，TTL=protocol.lease_ttl_min）
     才算占坑，租约内他人不得重派（并发会话不重复认领）。
  2. 幂等：以 list_id（条目 id，anchor 为派单侧锚）判重——同一 list_id 出现
     在 history 的终态条目（released/done/abandoned）后再次认领即拒绝
     （exit 1，须人工裁决），防重复派单。
  3. 心跳续命：执行中每 heartbeat_interval_min 刷新 heartbeat.at。
  4. 心跳退出即释放：--release 落 history、清 lease。
  5. 三段即证据链：pending / lease+heartbeat / history 构成可审计全过程
     （git 历史即 append-only 背书；本地文件为工作副本）。

租约过期：--check 可接管（原认领作废留痕——history 追加
outcome=voided_lease_expired 条目后另立新约）。--sweep 对"到期未释放且
未被接管超阈值"输出告警条目（开 issue 的文本，本脚本不直接开 issue——
开 issue 属治理平面动作）。

文件写读纪律：
  - **全部文件操作原子**（写同目录 .tmp + os.replace；中途 kill 后原文件
    完好、无半行）。重写按内置 TEMPLATE 渲染：注释是协议正文的组成部分，
    改注释=改协议，须同步本脚本 TEMPLATE 与 work-inbox.yaml。
  - 无 token、无网络：本脚本零网络零凭据（派单才用 cnb_pool.py）。

CLI：
  python work_inbox.py --check [--holder <会话标识>] [--inbox work-inbox.yaml]
      # → stdout 单行 JSON {action: claim|heartbeat|none, item, ...}
  python work_inbox.py --release <list_id> [--outcome done|abandoned] [--note "产物写回说明"]
  python work_inbox.py --sweep [--grace-min N]     # 默认 grace=lease_ttl_min

自检：python -m py_compile work_inbox.py && python -m unittest discover -s tests -v
仅依赖 Python 3.11+ 标准库（YAML 用内置极简解析器，不 import yaml）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_INBOX = Path(__file__).resolve().parent / "work-inbox.yaml"
DEFAULT_PROTOCOL = {"heartbeat_interval_min": 30, "lease_ttl_min": 60}
LEASE_FIELDS = ("active", "holder", "list_id", "anchor", "window",
                "claimed_at", "expires_at")
HEARTBEAT_FIELDS = ("at", "window", "note")
# history 终态 outcome：出现即拒绝同 list_id 再认领（幂等键去重）
TERMINAL_OUTCOMES = {"released", "done", "abandoned"}
ITEM_KEYS = ("id", "title", "source", "anchor", "created_at")


class InboxError(Exception):
    """work-inbox.yaml 结构损坏 / 状态机非法迁移（fail-closed）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ────────────────────────── 极简 YAML 解析（固定 schema） ──────────────────────────


def _scalar(raw: str):
    """标量：去引号 → 去行内注释 → null/bool/int/str（写法同 cnb_pool.py）。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    v = v.split(" #", 1)[0].strip()
    if v in ("null", "~", ""):
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_inbox(text: str) -> dict:
    """解析 work-inbox.yaml 固定结构：顶层 kv + 四类段落（protocol/lease/
    heartbeat 为两空格 kv；pending/history 为 `[]` 或两空格 `- k: v` 列表）。"""
    data: dict = {"version": None, "protocol": dict(DEFAULT_PROTOCOL),
                  "pending": [], "lease": {}, "heartbeat": {}, "history": []}
    section: str | None = None
    cur_item: dict | None = None
    target_list: list | None = None

    def close_item():
        nonlocal cur_item
        if cur_item is not None and target_list is not None:
            target_list.append(cur_item)
        cur_item = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m_item = re.match(r"^  - ([\w-]+):\s*(.*)$", raw)
        m_kv = re.match(r"^  ([\w-]+):\s*(.*)$", raw)
        m_sub = re.match(r"^    ([\w-]+):\s*(.*)$", raw)
        m_top = re.match(r"^([\w-]+):\s*(.*)$", raw)
        if m_item and section in ("pending", "history"):
            close_item()
            target_list = data[section]
            cur_item = {m_item.group(1): _scalar(m_item.group(2))}
        elif m_sub and cur_item is not None:
            cur_item[m_sub.group(1)] = _scalar(m_sub.group(2))
        elif m_kv and section in ("protocol", "lease", "heartbeat"):
            data[section][m_kv.group(1)] = _scalar(m_kv.group(2))
        elif m_top:
            close_item()
            key, val = m_top.group(1), m_top.group(2)
            if val.strip() == "":
                section = key if key in ("protocol", "pending", "lease",
                                         "heartbeat", "history") else None
                if section is None and key not in data:
                    data[key] = None
                target_list = data.get(section) if section in ("pending", "history") else None
            elif val.strip() == "[]":
                data[key] = []
                section = None
                target_list = None
            else:
                data[key] = _scalar(val)
                section = None
                target_list = None
        elif raw.strip():
            raise InboxError(f"work-inbox.yaml 第 {lineno} 行无法解析: {raw!r}")
    close_item()
    for must in ("protocol", "lease", "heartbeat"):
        if not isinstance(data.get(must), dict):
            raise InboxError(f"work-inbox.yaml 缺 {must} 段（结构损坏）")
    for lst in ("pending", "history"):
        if not isinstance(data.get(lst), list):
            raise InboxError(f"work-inbox.yaml 的 {lst} 段损坏（非列表）")
    return data


def _fmt(v) -> str:
    """值 → YAML 标量文本。字符串一律 json.dumps 双引号（合法 YAML，免转义纠纷）。"""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _render_kv(section: str, fields: tuple, model: dict) -> list[str]:
    lines = [f"{section}:"]
    for k in fields:
        lines.append(f"  {k}: {_fmt(model.get(k))}")
    return lines


def _render_list(section: str, items: list[dict]) -> list[str]:
    if not items:
        return [f"{section}: []"]
    lines = [f"{section}:"]
    for it in items:
        first = True
        for k, v in it.items():
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{k}: {_fmt(v)}")
            first = False
    return lines


def render_inbox(model: dict) -> str:
    """按内置规范模板渲染全文（注释=协议正文，与种子文件逐句一致）。"""
    pending = [dict(it) for it in model.get("pending") or []]
    history = [dict(it) for it in model.get("history") or []]
    lease = {k: (model.get("lease") or {}).get(k) for k in LEASE_FIELDS}
    heartbeat = {k: (model.get("heartbeat") or {}).get(k)
                 for k in HEARTBEAT_FIELDS}
    proto = dict(DEFAULT_PROTOCOL)
    proto.update(model.get("protocol") or {})
    out: list[str] = [
        "# work-inbox.yaml —— PM 自起工作发现协议种子（IR-0004 D16）",
        "# 属 cnb-bridge 可删除层：本文件只是协议在本工作目录的引导种子；协议语义",
        "# 记录在治理平面（ADR/GOVERNANCE），删除本仓不影响协议存续。",
        "# 本文件由 work_inbox.py 原子重写（tmp+rename）：按脚本内置模板渲染，",
        "# 注释即协议正文的组成部分——改注释=改协议，须同步 work_inbox.py。",
        f"version: {model.get('version') if model.get('version') is not None else 1}",
        "",
    ]
    out += _render_kv("protocol", tuple(DEFAULT_PROTOCOL), proto)
    out += [
        "",
        "# pending —— 待认领工作项。条目结构：id/title/source/anchor/created_at",
        "# （anchor 为幂等锚，与 dispatch run_id 同语义）。",
    ]
    out += _render_list("pending", pending)
    out += [
        "",
        "# lease —— 当前认领租约（同一时刻至多一条活跃；list_id 为幂等键）",
    ]
    out += _render_kv("lease", LEASE_FIELDS, lease)
    out += [
        "",
        "# heartbeat —— 执行心跳记录段（最近一次；协议五则 #3）",
    ]
    out += _render_kv("heartbeat", HEARTBEAT_FIELDS, heartbeat)
    out += [
        "",
        "# history —— 完成/放弃/超时作废条目归档（append-only，供审计；",
        "# 终态 outcome=released/done/abandoned 触发同 list_id 再认领拒绝）",
    ]
    out += _render_list("history", history)
    out += [
        "",
        "# ── 协议五则 ─────────────────────────────────────────────────────────────",
        "# 1. 认领带租约：写 lease（holder+expires_at）才算占坑，租约内他人不得重派。",
        "# 2. 幂等：以 anchor/list_id 判重——同一工作只认领/派发一次，重入按 anchor 合并。",
        "# 3. 心跳续命：执行中每 heartbeat_interval_min 刷新 heartbeat.at，静默即弃单。",
        "# 4. 心跳退出即释放：完成/放弃/超时须清 lease、落 history，窗口随之可复用。",
        "# 5. 三段即证据链：pending / lease+heartbeat / history 构成可审计全过程。",
        "",
    ]
    return "\n".join(out)


def atomic_write_text(path: Path, text: str) -> None:
    """原子写：同目录 .tmp + os.replace；中途失败/被 kill 原文件完好、不留残 tmp。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ────────────────────────── 状态机 ──────────────────────────


def _list_id(item: dict) -> str | None:
    if not isinstance(item, dict):
        return None
    return item.get("id") or item.get("list_id") or item.get("anchor")


def _lease_expired(lease: dict, now: datetime) -> bool:
    if not lease.get("active"):
        return False
    exp = _parse_iso(lease.get("expires_at"))
    return exp is not None and now >= exp


def _duplicate_in_history(history: list, item: dict) -> dict | None:
    key = _list_id(item)
    anchor = item.get("anchor") if isinstance(item, dict) else None
    if key is None and anchor is None:
        return None
    for h in history:
        if not isinstance(h, dict) or h.get("outcome") not in TERMINAL_OUTCOMES:
            continue
        if h.get("list_id") in (key, anchor) or h.get("anchor") in (key, anchor) \
                or h.get("id") in (key, anchor):
            return h
    return None


def _write_lease(model: dict, holder: str, item: dict, now: datetime,
                 ttl_min: int) -> dict:
    lease = {k: None for k in LEASE_FIELDS}
    lease.update({
        "active": True,
        "holder": holder,
        "list_id": _list_id(item),
        "anchor": item.get("anchor") or _list_id(item),
        "window": item.get("window"),
        "claimed_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=int(ttl_min))),
    })
    model["lease"] = lease
    return lease


def _void_expired_lease(model: dict, now: datetime) -> dict | None:
    """原认领作废留痕（lease 过期被接管）：history 追加 voided 条目，清 lease。"""
    lease = model.get("lease") or {}
    if not _lease_expired(lease, now):
        return None
    entry = {
        "type": "work", "list_id": lease.get("list_id") or lease.get("anchor"),
        "anchor": lease.get("anchor"),
        "holder": lease.get("holder"),
        "claimed_at": lease.get("claimed_at"),
        "voided_at": _iso(now),
        "outcome": "voided_lease_expired",
        "note": "原认领作废留痕：租约到期未释放，被接管（协议五则 #1/#2）",
    }
    model.setdefault("history", []).append(entry)
    model["lease"] = {k: None for k in LEASE_FIELDS}
    model["lease"]["active"] = False
    return entry


def _heartbeat(model: dict, now: datetime, note: str = "") -> dict:
    lease = model.get("lease") or {}
    hb = {k: None for k in HEARTBEAT_FIELDS}
    hb.update({"at": _iso(now),
               "window": lease.get("window") if lease.get("active") else None,
               "note": note})
    model["heartbeat"] = hb
    return hb


def check(inbox_path: str | Path, holder: str = "pm",
          now: datetime | None = None) -> dict:
    """三态判定并落盘（原子写）：

    - claim：有 pending 且无活跃 lease → 认领（置 lease；expired lease 先作废留痕）；
      无 pending 但 expired lease 持有未完结 item → 接管（同上前置作废留痕）。
    - heartbeat：无 pending → 刷新 heartbeat 段（追加心跳行）后退出。
    - none：有 pending 但活跃 lease 在他人手里 → 不动文件（并发不重复认领）；
      或 pending 项 list_id 已在 history 终态 → 拒绝二次认领（rc=1，须人工裁决）。
    """
    path = Path(inbox_path)
    if not path.is_file():
        raise InboxError(f"work-inbox.yaml 不存在: {path}")
    model = parse_inbox(path.read_text(encoding="utf-8"))
    now = now or _utcnow()
    lease = model.get("lease") or {}
    pending = model.get("pending") or []
    history = model.get("history") or []
    ttl = int((model.get("protocol") or {}).get("lease_ttl_min")
              or DEFAULT_PROTOCOL["lease_ttl_min"])
    expired = _lease_expired(lease, now)
    active = bool(lease.get("active")) and not expired

    if active:
        if pending:
            return {"action": "none", "item": None, "reason": "lease_busy",
                    "lease": lease,
                    "note": "活跃租约在持（他人工作中），本会话不认领不派单"}
        _heartbeat(model, now, note="执行中（活跃租约心跳续命，协议五则 #3）")
        atomic_write_text(path, render_inbox(model))
        return {"action": "heartbeat", "item": {"list_id": lease.get("list_id"),
                                                "anchor": lease.get("anchor")},
                "lease": lease, "note": "心跳已刷新"}

    # 无活跃 lease（空闲或已过期）
    if pending:
        item = pending[0]
        dup = _duplicate_in_history(history, item)
        if dup is not None:
            return {"action": "none", "item": item,
                    "reason": "duplicate_list_id",
                    "history_ref": dup.get("list_id") or dup.get("anchor"),
                    "rc": 1,
                    "note": "幂等键命中 history 终态：同一 list_id 二次认领拒绝"
                            "（须人工裁决后另立新 list_id）"}
        voided = _void_expired_lease(model, now)
        model["pending"] = pending[1:]
        new_lease = _write_lease(model, holder, item, now, ttl)
        _heartbeat(model, now, note="认领即首跳（协议五则 #3）")
        atomic_write_text(path, render_inbox(model))
        return {"action": "claim", "item": item, "lease": new_lease,
                "voided": voided,
                "note": "已置租约；产物写回后 --release <list_id>"}
    if expired and (lease.get("anchor") or lease.get("list_id")):
        item = {"id": lease.get("list_id") or lease.get("anchor"),
                "anchor": lease.get("anchor"),
                "title": "（接管自过期租约的工作项）"}
        dup = _duplicate_in_history(history, item)
        if dup is not None:
            return {"action": "none", "item": item,
                    "reason": "duplicate_list_id", "rc": 1,
                    "note": "过期租约对应工作已在 history 终态：拒绝接管（人工裁决）"}
        voided = _void_expired_lease(model, now)
        new_lease = _write_lease(model, holder, item, now, ttl)
        _heartbeat(model, now, note="接管过期租约（原认领已作废留痕）")
        atomic_write_text(path, render_inbox(model))
        return {"action": "claim", "item": item, "lease": new_lease,
                "takeover": True, "voided": voided,
                "note": "接管过期租约；原认领作废留痕见 history"}
    _heartbeat(model, now, note="无 pending：追加心跳行退出（零派单）")
    atomic_write_text(path, render_inbox(model))
    return {"action": "heartbeat", "item": None, "note": "无待办——心跳留痕，零派单"}


def release(inbox_path: str | Path, list_id: str, outcome: str = "done",
            note: str = "", now: datetime | None = None) -> dict:
    """产物写回 + 释放租约：history 追加终态条目、lease 清空。"""
    if outcome not in ("done", "abandoned"):
        raise InboxError(f"非法 outcome {outcome!r}（允许 done/abandoned）")
    path = Path(inbox_path)
    if not path.is_file():
        raise InboxError(f"work-inbox.yaml 不存在: {path}")
    model = parse_inbox(path.read_text(encoding="utf-8"))
    now = now or _utcnow()
    lease = model.get("lease") or {}
    cur_key = lease.get("list_id") or lease.get("anchor")
    if not lease.get("active"):
        raise InboxError("无活跃租约可释放（lease.active=false）")
    if list_id not in (lease.get("list_id"), lease.get("anchor")):
        raise InboxError(f"list_id {list_id!r} 与当前租约 {cur_key!r} 不符——拒绝释放他人租约")
    entry = {
        "type": "work", "list_id": cur_key, "anchor": lease.get("anchor"),
        "holder": lease.get("holder"),
        "claimed_at": lease.get("claimed_at"),
        "released_at": _iso(now), "outcome": outcome, "note": note,
    }
    model.setdefault("history", []).append(entry)
    model["lease"] = {k: None for k in LEASE_FIELDS}
    model["lease"]["active"] = False
    _heartbeat(model, now, note="释放即心跳退出（协议五则 #4）")
    atomic_write_text(path, render_inbox(model))
    return {"action": "release", "item": {"list_id": cur_key,
                                          "anchor": lease.get("anchor")},
            "outcome": outcome, "history_entry": entry}


def sweep(inbox_path: str | Path, grace_min: int | None = None,
          now: datetime | None = None) -> dict:
    """lease 到期未释放且未被接管超阈值 → 告警条目（开 issue 的文本）。

    只读不写（接管是 --check 的动作；开 issue 是治理平面动作）。阈值默认
    一个完整 lease_ttl_min（到期后整整一个 TTL 无人接管）。
    """
    path = Path(inbox_path)
    if not path.is_file():
        raise InboxError(f"work-inbox.yaml 不存在: {path}")
    model = parse_inbox(path.read_text(encoding="utf-8"))
    now = now or _utcnow()
    lease = model.get("lease") or {}
    ttl = int((model.get("protocol") or {}).get("lease_ttl_min")
              or DEFAULT_PROTOCOL["lease_ttl_min"])
    grace = ttl if grace_min is None else int(grace_min)
    exp = _parse_iso(lease.get("expires_at"))
    if not lease.get("active") or exp is None or now < exp:
        return {"type": "sweep", "stalled": False,
                "note": "无到期未释放租约（活跃租约在 TTL 内或空闲）"}
    stalled_min = round((now - exp).total_seconds() / 60, 1)
    if (now - exp) < timedelta(minutes=grace):
        return {"type": "sweep", "stalled": False,
                "stalled_min": stalled_min, "grace_min": grace,
                "note": f"已过期 {stalled_min} 分钟 < 宽限 {grace} 分钟：仍可被接管，暂不告警"}
    key = lease.get("list_id") or lease.get("anchor")
    alert = {
        "type": "lease_stall", "severity": "warn", "list_id": key,
        "holder": lease.get("holder"), "claimed_at": lease.get("claimed_at"),
        "expires_at": lease.get("expires_at"),
        "stalled_min": stalled_min, "grace_min": grace,
        "issue_title": f"[work-inbox] 租约停摆告警：{key} 到期未释放且未被接管",
        "issue_body": (
            f"工作项 {key}（holder={lease.get('holder')}）的租约已于 "
            f"{lease.get('expires_at')} 到期，超宽限 {grace} 分钟仍未释放、未被接管"
            f"（已停滞 {stalled_min} 分钟）。\n\n"
            f"处置建议（不得静默停摆，IR-0004 AC-17）：\n"
            f"1. 核对 holder 会话是否仍在执行：python work_inbox.py --check\n"
            f"2. 确认弃单则接管/释放：python work_inbox.py --release {key} --outcome abandoned\n"
            f"3. 该 list_id 的派单与产物核对记录留档本 issue（机械核对锚串 [run:…x]）。\n"
            f"——本告警由 python work_inbox.py --sweep 产出（开 issue 由治理平面执行）。"),
        "note": "到期未释放且未被接管超阈值——输出开 issue 文本（AC-17：不得静默停摆）",
    }
    return {"type": "sweep", "stalled": True, "alert": alert}


# ────────────────────────── CLI ──────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="work_inbox.py",
        description="PM 自起工作发现协议执行器（IR-0004 D16/AC-17）——零网络零凭据，文件全原子写",
    )
    ap.add_argument("--inbox", default=str(DEFAULT_INBOX),
                    help="work-inbox.yaml 路径（默认：脚本同目录）")
    ap.add_argument("--check", action="store_true",
                    help="三态判定：claim（置租约）/ heartbeat（追加心跳行）/ none")
    ap.add_argument("--holder", default=None,
                    help="会话标识（认领者；默认 env CNB_WORK_INBOX_HOLDER 或 'pm'）")
    ap.add_argument("--release", metavar="LIST_ID", default=None,
                    help="产物写回+释放：<list_id>（配 --outcome/--note）")
    ap.add_argument("--outcome", choices=("done", "abandoned"), default="done",
                    help="--release 的终态（默认 done）")
    ap.add_argument("--note", default="", help="--release 的产物写回说明（路径/锚串）")
    ap.add_argument("--sweep", action="store_true",
                    help="到期未释放且未被接管超阈值 → 输出告警条目（开 issue 文本）")
    ap.add_argument("--grace-min", type=int, default=None,
                    help="--sweep 宽限分钟（默认=lease_ttl_min）")
    args = ap.parse_args(argv)
    if not (args.check or args.release or args.sweep):
        ap.error("须至少一个动作：--check / --release <list_id> / --sweep")
    holder = args.holder or os.environ.get("CNB_WORK_INBOX_HOLDER") or "pm"
    try:
        if args.check:
            r = check(args.inbox, holder=holder)
        elif args.release:
            r = release(args.inbox, args.release, outcome=args.outcome,
                        note=args.note)
        else:
            r = sweep(args.inbox, grace_min=args.grace_min)
        print(json.dumps(r, ensure_ascii=False))
        return int(r.get("rc", 0))
    except InboxError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
