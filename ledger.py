#!/usr/bin/env python3
"""ledger.py —— CNB 配额账本（IR-0004 AC-15 / BEH-14；台账真源=GitHub 侧）。

定位（见 REMOVAL.md/README）：cnb-bridge **可删除层**的观测件——只读观测 CNB
配额与 build logs，不做任何判定。台账 append-only（usage.jsonl 每行含
prev_hash sha256 链，hash 链思路承接 ADR-0062，INV-05）。

**真源声明**：本地 state-dir 里的台账只是暂存（staging）；append-only 的
权威真源是 GitHub 侧——usage.jsonl 的变更须随 PR 并回本仓，git 历史
即不可改写账本（本地被删/被改以远端为准恢复）。

数据模型：
  snapshots.jsonl  逐账号配额快照（每次 --snapshot 追加一行；普通追加）
  usage.jsonl      快照差分=时段用量（append-only hash 链；篡改=--verify 非零退出）
  alerts.jsonl     对账偏差/标签缺失告警条目（普通追加，供周审计消费）
  hash = sha256(本行去掉 hash 字段的 canonical JSON)；
  prev_hash = 上一行 hash（创世行为 64 个 0）。

口径（IFACE-05）：核·秒消耗与档位证实以平台 build logs（duration×labels.cpus）
为对账真源；本地快照差分仅作交叉验证——两者偏差超阈值（--threshold-pct，
默认 10）即输出告警条目。labels.cpus 缺失即告警（AC-15：标签与实耗不一致
或缺失即判红，自报标签不采信）。

纪律（同 cnb_pool.py）：
  - token 只经环境变量 CNB_TOKEN_<ALIAS> 注入（org secret 同名），永不入仓、
    永不硬编码、缺失即抛错（LedgerError），无任何 fallback。
  - fail-closed：API 非 2xx / 网络不可达 / 配额结构未知 / 快照不足——
    一律抛错退出，绝不吞错后假装成功。
  - 只对 accounts.yaml 中 status=active 的账号入账（pending-access/retired
    不产生用量；其历史行保留）。

CLI（token 从 env；复用 cnb_pool 的 AccountPool/CnbClient）：
  python ledger.py --snapshot  [--accounts-yaml P] [--state-dir D]
  python ledger.py --reconcile [--threshold-pct 10] [--accounts-yaml P] [--state-dir D]
  python ledger.py --verify    [--state-dir D] [--file usage.jsonl]

自检：python -m py_compile ledger.py && python -m unittest discover -s tests -v
仅依赖 Python 3.11+ 标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# cnb_pool 从同级目录导入（并入 cnb-bridge 后本脚本与 cnb_pool.py 同目录）；
# 开发期布局（build-d 与 cnb-bridge 并列）回退到兄弟目录找 cnb_pool.py。
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE, _HERE.parent / "cnb-bridge"):
    if (_cand / "cnb_pool.py").is_file():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
else:
    raise ImportError("找不到 cnb_pool.py（须与 ledger.py 同目录，或兄弟目录 cnb-bridge/）")

import cnb_pool  # noqa: E402
from cnb_pool import AccountPool, AccountPoolError, CnbApiError  # noqa: E402

DEFAULT_STATE_DIR = _HERE / "ledger-state"
GENESIS_HASH = "0" * 64
USAGE_FILE = "usage.jsonl"
SNAPSHOTS_FILE = "snapshots.jsonl"
ALERTS_FILE = "alerts.jsonl"
# build logs 条目里可当作时间戳的字段（按序尝试；ISO 字符串，兼容 Z 后缀）
_TS_KEYS = ("created_at", "started_at", "finished_at", "updated_at")


class LedgerError(Exception):
    """账本错误（fail-closed）：token 缺失、配额结构未知、快照不足等。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime | None:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ────────────────────────── hash 链（ADR-0062 思路） ──────────────────────────


def canonical_hash(entry: dict) -> str:
    """sha256(去掉 hash 字段后的 canonical JSON)。RUNBOOK 的手工追加片段
    必须用同一规范化（sort_keys + 紧凑分隔符 + ensure_ascii=False）。"""
    payload = {k: v for k, v in entry.items() if k != "hash"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _atomic_append(path: Path, entry: dict, chain: bool) -> dict:
    """整读-追加-写 tmp-rename：单行损坏不扩散，中途崩溃不留半行。"""
    lines = _read_lines(path)
    if chain:
        prev = GENESIS_HASH
        if lines:
            try:
                prev = json.loads(lines[-1])["hash"]
            except (json.JSONDecodeError, KeyError, IndexError) as e:
                raise LedgerError(f"{path.name} 末行损坏，拒绝追加（先 --verify）: {e}")
        entry = dict(entry)
        entry["prev_hash"] = prev
        entry["hash"] = canonical_hash(entry)
    new_text = "".join(ln + "\n" for ln in lines) + \
        (json.dumps(entry, ensure_ascii=False) + "\n")
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return entry


def load_chain(path: Path) -> list[dict]:
    out = []
    for i, ln in enumerate(_read_lines(path), 1):
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            raise LedgerError(f"{path.name} 第 {i} 行不是合法 JSON: {e}")
        if not isinstance(obj, dict):
            raise LedgerError(f"{path.name} 第 {i} 行不是对象")
        out.append(obj)
    return out


def verify_chain(path: Path) -> dict:
    """校验 hash 链：prev_hash 链接 + 行 hash 可复算。篡改=ok False（CLI 非零退出）。

    台账文件不存在=尚未建立（首次 --snapshot 前为空属正常，ok True 附 note）；
    真源在 GitHub 侧，本地缺失以远端 git 历史恢复。
    """
    if not path.is_file():
        return {"ok": True, "entries": 0, "first_bad_line": None,
                "note": f"{path.name} 尚未建立（首次 --snapshot 前为空属正常）"}
    prev = GENESIS_HASH
    for i, ln in enumerate(_read_lines(path), 1):
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            return {"ok": False, "entries": i - 1, "first_bad_line": i,
                    "reason": f"第 {i} 行非法 JSON: {e}"}
        if not isinstance(obj, dict) or "hash" not in obj:
            return {"ok": False, "entries": i - 1, "first_bad_line": i,
                    "reason": f"第 {i} 行缺 hash 字段"}
        if obj.get("prev_hash") != prev:
            return {"ok": False, "entries": i - 1, "first_bad_line": i,
                    "reason": f"第 {i} 行 prev_hash 断链（期望 {prev[:12]}…，"
                              f"实得 {str(obj.get('prev_hash'))[:12]}…）"}
        if canonical_hash(obj) != obj["hash"]:
            return {"ok": False, "entries": i - 1, "first_bad_line": i,
                    "reason": f"第 {i} 行 hash 与内容不符（内容被篡改或手工改行未重算）"}
        prev = obj["hash"]
    return {"ok": True, "entries": len(_read_lines(path)), "first_bad_line": None}


# ────────────────────────── 配额解析与 build logs 口径 ──────────────────────────


def quota_free_total(q: dict) -> tuple[float, float]:
    """从 quota 响应提取 (total, free) 核·秒。结构未知即抛错（fail-closed，
    不假装 0）。实测结构（2026-08-25 p11）：{dev_in_sec: {total, free}}；兼容
    扁平数值 + used_in_sec/dev_in_sec_used。"""
    if not isinstance(q, dict):
        raise LedgerError(f"配额响应非对象: {type(q).__name__}")
    dev = q.get("dev_in_sec")
    if isinstance(dev, dict):
        total, free = dev.get("total"), dev.get("free")
        if isinstance(total, (int, float)) and isinstance(free, (int, float)):
            return float(total), float(free)
    elif isinstance(dev, (int, float)) and dev > 0:
        used = q.get("used_in_sec")
        if not isinstance(used, (int, float)):
            used = q.get("dev_in_sec_used")
        if isinstance(used, (int, float)):
            return float(dev), float(dev - used)
    raise LedgerError(f"配额结构未知（无 dev_in_sec.total/free），拒绝入账: "
                      f"{json.dumps(q, ensure_ascii=False)[:200]}")


def build_logs_core_sec(logs: list, start: datetime, end: datetime) -> dict:
    """按 IFACE-05 口径汇总：duration（秒）× labels.cpus = 核·秒。

    - 条目时间戳（created_at/started_at/…）可解析 → 只计 [start, end] 窗口内；
      不可解析 → 计入（保守）并计入 unfiltered 计数。
    - labels.cpus 缺失/非法 → 不计入合计，计入 label_missing（AC-15：缺失即告警）。
    """
    total = 0.0
    label_missing = 0
    unfiltered = 0
    for entry in logs:
        if not isinstance(entry, dict):
            label_missing += 1
            continue
        ts = None
        for key in _TS_KEYS:
            ts = _parse_iso(entry.get(key))
            if ts is not None:
                break
        if ts is None:
            unfiltered += 1
        elif not (start <= ts <= end):
            continue
        duration = entry.get("duration")
        if duration is None:
            duration = entry.get("duration_sec")
        if not isinstance(duration, (int, float)) or duration < 0:
            label_missing += 1
            continue
        cpus = (entry.get("labels") or {}).get("cpus") if isinstance(
            entry.get("labels"), dict) else None
        try:
            cpus = int(cpus)
        except (TypeError, ValueError):
            label_missing += 1
            continue
        if cpus <= 0:
            label_missing += 1
            continue
        total += float(duration) * cpus
    return {"core_sec": total, "label_missing": label_missing, "unfiltered": unfiltered}


# ────────────────────────── 三个动作 ──────────────────────────


def _active_accounts(pool: AccountPool) -> list:
    accounts = [a for a in pool.accounts if a.status == "active"]
    if not accounts:
        raise LedgerError("accounts.yaml 无 status=active 账号——无可入账对象")
    return accounts


def take_snapshot(accounts_yaml: str | Path, state_dir: str | Path) -> dict:
    """逐账号查 quota（cnb_pool 客户端），追加快照行；与上一快照差分出
    时段用量并追加 usage.jsonl（hash 链）。"""
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    pool = AccountPool(accounts_yaml)
    now = _utcnow()
    current: dict[str, dict] = {}
    for a in _active_accounts(pool):
        q = pool.client_for(a).quota()  # token 缺失/API 失败即抛错（fail-closed）
        total, free = quota_free_total(q)
        current[a.alias] = {"dev_total": total, "dev_free": free}
    snaps_path = state / SNAPSHOTS_FILE
    prev_snaps = load_chain(snaps_path)
    seq = (prev_snaps[-1].get("seq", 0) + 1) if prev_snaps else 1
    snap_entry = _atomic_append(snaps_path, {
        "type": "snapshot", "seq": seq, "recorded_at": _iso(now),
        "accounts": current}, chain=False)
    usage_entry = None
    if prev_snaps:
        period_start = prev_snaps[-1]["recorded_at"]
        accounts_usage: dict[str, dict] = {}
        for alias, cur in current.items():
            prev = (prev_snaps[-1].get("accounts") or {}).get(alias)
            if prev is None:
                continue  # 新入职账号：无基线，本周期不入账（下周期起算）
            delta = float(prev["dev_free"]) - float(cur["dev_free"])
            accounts_usage[alias] = {
                "used_core_sec": max(0.0, round(delta, 3)),
                "delta_free_sec": round(delta, 3),
                "quota_reset_suspected": delta < 0,  # 月度重置：余量回升
            }
        usage_entry = _atomic_append(state / USAGE_FILE, {
            "type": "usage", "seq": seq, "recorded_at": _iso(now),
            "period_start": period_start, "period_end": _iso(now),
            "accounts": accounts_usage}, chain=True)
    return {"snapshot": snap_entry, "usage_entry": usage_entry}


def reconcile(accounts_yaml: str | Path, state_dir: str | Path,
              threshold_pct: float = 10.0) -> dict:
    """快照差分 vs build logs 实耗（duration×labels.cpus）逐账号对账。

    偏差 = |snapshot−build_logs| / build_logs × 100（build_logs 为真源分母）；
    超 --threshold-pct（默认 10）→ 告警条目（stdout + alerts.jsonl）。
    labels.cpus 缺失 → 告警条目（AC-15 红）。快照不足两次 → LedgerError。
    """
    state = Path(state_dir)
    snaps = load_chain(state / SNAPSHOTS_FILE)
    if len(snaps) < 2:
        raise LedgerError(f"快照不足两次（现有 {len(snaps)}），无法对账——"
                          f"先再跑一次 --snapshot")
    prev, cur = snaps[-2], snaps[-1]
    start = _parse_iso(prev["recorded_at"])
    end = _parse_iso(cur["recorded_at"])
    if start is None or end is None or start >= end:
        raise LedgerError("快照时间戳缺失或乱序，拒绝对账")
    pool = AccountPool(accounts_yaml)
    now = _utcnow()
    per_account: dict[str, dict] = {}
    alerts: list[dict] = []
    for a in _active_accounts(pool):
        p = (prev.get("accounts") or {}).get(a.alias)
        c = (cur.get("accounts") or {}).get(a.alias)
        snapshot_sec = (round(float(p["dev_free"]) - float(c["dev_free"]), 3)
                        if p and c else None)
        logs = pool.client_for(a).build_logs()  # fail-closed
        agg = build_logs_core_sec(logs, start, end)
        actual = agg["core_sec"]
        if snapshot_sec is None:
            deviation = None
        elif actual > 0:
            deviation = round(abs(snapshot_sec - actual) / actual * 100, 3)
        else:
            deviation = None  # 无实耗记录：快照有消耗即告警，否则视为一致
        alert = (deviation is not None and deviation > threshold_pct) or \
                (deviation is None and (snapshot_sec or 0) > 0) or \
                agg["label_missing"] > 0
        row = {"snapshot_core_sec": snapshot_sec,
               "build_logs_core_sec": round(actual, 3),
               "deviation_pct": deviation,
               "label_missing": agg["label_missing"],
               "unfiltered_entries": agg["unfiltered"],
               "alert": bool(alert)}
        per_account[a.alias] = row
        if deviation is not None and deviation > threshold_pct:
            alerts.append({"type": "reconcile_deviation", "account": a.alias,
                           "snapshot_core_sec": snapshot_sec,
                           "build_logs_core_sec": round(actual, 3),
                           "deviation_pct": deviation,
                           "threshold_pct": threshold_pct,
                           "recorded_at": _iso(now)})
        if agg["label_missing"] > 0:
            alerts.append({"type": "build_label_missing", "account": a.alias,
                           "count": agg["label_missing"],
                           "recorded_at": _iso(now),
                           "note": "labels.cpus/duration 缺失或非法——档位证实"
                                   "不成立（AC-15：自报标签不采信，缺失即红）"})
    for al in alerts:
        _atomic_append(state / ALERTS_FILE, al, chain=False)
    return {"period": {"start": prev["recorded_at"], "end": cur["recorded_at"]},
            "threshold_pct": threshold_pct,
            "truth_source": "build_logs(duration×labels.cpus)",
            "accounts": per_account, "alerts": alerts}


# ────────────────────────── CLI ──────────────────────────


def _die(msg: str, code: int = 1) -> int:
    print(json.dumps({"error": str(msg)}, ensure_ascii=False), file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ledger.py",
        description="CNB 配额账本——快照差分/对账/hash 链校验（台账真源=GitHub 侧，本地仅暂存）",
    )
    ap.add_argument("--accounts-yaml", default=str(cnb_pool.DEFAULT_ACCOUNTS_YAML),
                    help="accounts.yaml 路径（默认：cnb_pool.py 同目录）")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                    help=f"账本目录（默认 {DEFAULT_STATE_DIR}）")
    ap.add_argument("--snapshot", action="store_true",
                    help="逐账号查配额（cnb_pool 客户端），快照差分写入 usage.jsonl（hash 链）")
    ap.add_argument("--reconcile", action="store_true",
                    help="对照 build logs 实耗（duration×labels.cpus）对账；可与 --snapshot 同跑（先快照后对账）")
    ap.add_argument("--threshold-pct", type=float, default=10.0,
                    help="对账偏差告警阈值百分比（默认 10；对应 automation-limits.yaml "
                         "cnb.ledger_reconcile_deviation_pct）")
    ap.add_argument("--verify", action="store_true",
                    help="hash 链校验（篡改=非零退出）")
    ap.add_argument("--file", default=USAGE_FILE,
                    help=f"--verify 的台账文件名（默认 {USAGE_FILE}；生命周期台账传 lifecycle.jsonl）")

    args = ap.parse_args(argv)
    if not (args.snapshot or args.reconcile or args.verify):
        ap.error("须至少一个动作：--snapshot / --reconcile / --verify")
    state = Path(args.state_dir)
    rc = 0
    try:
        if args.snapshot:
            r = take_snapshot(args.accounts_yaml, state)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        if args.reconcile:
            r = reconcile(args.accounts_yaml, state, args.threshold_pct)
            print(json.dumps(r, ensure_ascii=False, indent=2))
        if args.verify:
            r = verify_chain(state / args.file)
            print(json.dumps(r, ensure_ascii=False, indent=2))
            rc = 0 if r["ok"] else 2
    except (CnbApiError, AccountPoolError, LedgerError, OSError) as e:
        return _die(e)
    return rc


if __name__ == "__main__":
    sys.exit(main())
