#!/usr/bin/env python3
"""audit_extra.py —— 周审计补全（IR-0004 AC-18 五项之三；fail-closed）。

周审计五项：配额余量与速率 / 活性 canary / 延迟分位 / **产物真实性抽样** /
**平台政策页 diff 监测**。前两项中配额活性与窗口停扰已由 .github 仓
cnb-audit 工作流承担；本脚本补全三项并汇总输出周审计 JSON 报告：

  ① 政策监测   拉 docs.cnb.cool 两页（URL 常量化，见 POLICY_PAGES），
               页面 sha256 与上次快照比对；变更 → 告警条目（含 diff 补丁
               链接与页面链接）。网络失败 = 该通道 infra **红**非绿（INV-04）。
  ② 产物真实性抽样  --sample <dir> <pct>：从 fanout/products 或派单回执目录
               随机抽样（seed 可注入，默认=ISO 周号，周内可复现），逐条核对
               [run:锚] 在对应窗口评论中真实存在（复用 cnb_pool 客户端）；
               不匹配 = 作废留痕条目（append voided.jsonl）。
  ③ 配额汇总   调 ledger.py 的 snapshot+reconcile（等价 CLI
               `python ledger.py --snapshot --reconcile`），并做 usage.jsonl
               hash 链校验；台账真源=GitHub 侧（本地仅暂存，见 ledger.py）。

fail-closed（INV-04）：任一通道**查询失败**（网络/API/token/解析）= 报告
overall=red 且退出码 1（周工作流据此把 job 变红）。告警条目（政策变更/
抽样作废/对账偏差）是检测成功的产出，不改变通道红绿——它们走 issue 处置。

纪律：token 只经环境变量 CNB_TOKEN_<ALIAS> 注入（org secret 同名），永不
入仓、永不硬编码；报告与告警文本不含任何凭据形态。

CLI（token 从 env；抽样/配额通道需要）：
  python audit_extra.py                                    # 周审计全项 → JSON 报告
  python audit_extra.py --sample <dir> <pct> [--seed N]    # 仅产物真实性抽样
  # 可选：--state-dir D --accounts-yaml P --sample-dir fanout/products
  #       --sample-pct 10 --threshold-pct 10 --out weekly-report.json

自检：python -m py_compile audit_extra.py && python -m unittest discover -s tests -v
仅依赖 Python 3.11+ 标准库。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# cnb_pool/ledger 从同级目录导入（并入 cnb-bridge 后三者同目录）；开发期布局
# （build-d 与 cnb-bridge 并列）回退到兄弟目录找 cnb_pool.py，ledger 在本目录。
for _cand in (_HERE, _HERE.parent / "cnb-bridge"):
    if (_cand / "cnb_pool.py").is_file():
        if str(_cand) not in sys.path:
            sys.path.insert(0, str(_cand))
        break
else:
    raise ImportError("找不到 cnb_pool.py（须与 audit_extra.py 同目录，或兄弟目录 cnb-bridge/）")
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import cnb_pool  # noqa: E402
import ledger  # noqa: E402
from cnb_pool import AccountPool, AccountPoolError, CnbApiError  # noqa: E402

DEFAULT_STATE_DIR = _HERE / "audit-state"
# 政策监测页（真实 URL 常量化——改页=改这一处；新增页面加一行）
POLICY_PAGES = (
    {"key": "workspaces-intro", "url": "https://docs.cnb.cool/zh/workspaces/intro.html"},
    {"key": "pricing", "url": "https://docs.cnb.cool/zh/pricing.html"},
)
POLICY_TIMEOUT_SEC = 30
VOIDED_FILE = "voided.jsonl"
DEFAULT_SAMPLE_DIR = "fanout/products"
DEFAULT_SAMPLE_PCT = 10.0   # BUDGET-03：默认 10%、可降不可免
# 派单回执行两种已知形态（cnb-dispatch 审计行 / cnb_pool dispatch JSON 输出）
_RX_AUDIT_LINE = re.compile(
    r"account=(?P<account>[\w.-]+).*?\bwindow=(?P<window>\d+).*?\brun_id=(?P<run_id>[0-9A-Za-z]+)")
_RX_JSON_LINE = re.compile(
    r'"account"\s*:\s*"(?P<account>[\w.-]+)".*?"window"\s*:\s*(?P<window>\d+)'
    r'.*?"run_id"\s*:\s*"(?P<run_id>[0-9A-Za-z]+)"')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _atomic_append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [ln for ln in (path.read_text(encoding="utf-8").splitlines()
                           if path.is_file() else []) if ln.strip()]
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(ln + "\n" for ln in lines) +
                    json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# ────────────────────────── ① 政策监测 ──────────────────────────


def fetch_policy_page(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "cnb-bridge-weekly-audit (+https://github.com/Cloudbird-Software/cnb-bridge)"})
    with urllib.request.urlopen(req, timeout=POLICY_TIMEOUT_SEC) as resp:
        return resp.read()


def check_policy(state_dir: Path) -> dict:
    """两政策页 sha256 比对上次快照；变更 → 告警条目（diff 补丁+页面链接）。

    首次运行建立基线（非告警）。网络失败 → errors 记录、status=red（infra
    红非绿——INV-04：平台政策失效检测通道自身失效必须可见）。
    """
    pages_dir = state_dir / "policy_pages"
    diffs_dir = state_dir / "policy_diffs"
    baseline_path = state_dir / "policy_pages.json"
    baseline: dict = {}
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return {"status": "red", "errors": [f"基线文件损坏: {e}"], "alerts": []}
    pages, alerts, errors = [], [], []
    now = _utcnow()
    for page in POLICY_PAGES:
        key, url = page["key"], page["url"]
        try:
            body = fetch_policy_page(url)
        except Exception as e:  # URLError/HTTPError/timeout——一律 infra 红
            errors.append(f"{key}: 拉取失败 {url}: {e}")
            pages.append({"key": key, "url": url, "fetched": False})
            continue
        sha = hashlib.sha256(body).hexdigest()
        old = baseline.get(key)
        row = {"key": key, "url": url, "fetched": True, "sha256": sha,
               "size": len(body)}
        if old is None:
            row["baseline_established"] = True
        elif old.get("sha256") != sha:
            diffs_dir.mkdir(parents=True, exist_ok=True)
            pages_dir.mkdir(parents=True, exist_ok=True)
            old_path = pages_dir / f"{key}.html"
            old_text = (old_path.read_text(encoding="utf-8", errors="replace")
                        if old_path.is_file() else "（基线内容文件缺失）")
            new_text = body.decode("utf-8", errors="replace")
            stamp = now.strftime("%Y%m%dT%H%M%SZ")
            patch_rel = f"policy_diffs/{key}-{stamp}.patch"
            patch_path = state_dir / patch_rel
            patch = "\n".join(difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f"{key}.html@{old.get('fetched_at', 'unknown')}",
                tofile=url, lineterm="")) + "\n"
            patch_path.write_text(patch, encoding="utf-8", newline="\n")
            alerts.append({
                "type": "policy_change", "page": key, "url": url,
                "old_sha256": old.get("sha256"), "new_sha256": sha,
                "diff": patch_rel,
                "issue_title": f"[cnb-bridge] CNB 政策页变更：{key}",
                "issue_body": (
                    f"政策监测检出页面变更（{url}）：\n"
                    f"- 旧 sha256: {old.get('sha256')}\n- 新 sha256: {sha}\n"
                    f"- diff 补丁: {patch_rel}（相对 audit-state 快照）\n"
                    f"- 页面链接: {url}\n\n"
                    f"处置（IR-0004 AC-18/ASSUMPTION-01）：人工确认政策变化是否影响免费额度"
                    f"/NPC 行为假设；影响则触发回退付费 API 决策并更新 GOVERNANCE EX-1。"),
                "recorded_at": _iso(now)})
        baseline[key] = {"url": url, "sha256": sha,
                         "fetched_at": _iso(now), "size": len(body)}
        pages_dir.mkdir(parents=True, exist_ok=True)
        (pages_dir / f"{key}.html").write_bytes(body)
        pages.append(row)
    if not errors:
        tmp = baseline_path.with_name(baseline_path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(baseline, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, baseline_path)
        finally:
            if tmp.exists():
                tmp.unlink()
    return {"status": "red" if errors else "green", "pages": pages,
            "alerts": alerts, "errors": errors}


# ────────────────────────── ② 产物真实性抽样 ──────────────────────────


def collect_receipts(sample_dir: Path) -> list[dict]:
    """从派单回执目录收集回执：整文件 JSON（对象或数组）+ 逐行（cnb-dispatch
    审计行 / JSONL 单行 JSON 两种形态）；按 run_id 去重。"""
    receipts: dict[str, dict] = {}

    def add(account: str, window, run_id: str, fname: str) -> None:
        receipts.setdefault(run_id, {
            "run_id": run_id, "account": str(account),
            "window": int(window), "receipt_file": fname})

    if not sample_dir.is_dir():
        return []
    for f in sorted(p for p in sample_dir.iterdir() if p.is_file()):
        text = f.read_text(encoding="utf-8", errors="replace")
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
        docs = doc if isinstance(doc, list) else ([doc] if isinstance(doc, dict) else [])
        for item in docs:
            if isinstance(item, dict) and all(
                    k in item for k in ("account", "window", "run_id")):
                add(item["account"], item["window"], str(item["run_id"]), f.name)
        for line in text.splitlines():
            m = _RX_AUDIT_LINE.search(line) or _RX_JSON_LINE.search(line)
            if m:
                add(m.group("account"), m.group("window"), m.group("run_id"), f.name)
    return [receipts[k] for k in sorted(receipts)]


def sample_artifacts(sample_dir: Path, pct: float, seed,
                     accounts_yaml: str | Path, state_dir: Path) -> dict:
    """随机抽样核对：[run:锚] 须在对应窗口评论中真实存在（NPC 自报不采信，
    机械复核）。不匹配 = 作废留痕条目（voided.jsonl）。查询失败 = 通道红。"""
    now = _utcnow()
    receipts = collect_receipts(sample_dir)
    result: dict = {"sample_dir": str(sample_dir), "pct": pct, "seed": str(seed),
                    "receipts": len(receipts), "dir_exists": sample_dir.is_dir()}
    if not receipts:
        result.update({"status": "green", "sampled": 0, "verified": 0,
                       "voided": [], "errors": [],
                       "note": "目录无回执：零消费非红（AC-13 语义——fanout 产物"
                               "生产者可选，空目录零消费）"})
        return result
    k = min(len(receipts), max(1, math.ceil(len(receipts) * pct / 100.0)))
    chosen = random.Random(seed).sample(receipts, k)
    result["sampled"] = k
    pool = AccountPool(accounts_yaml)
    verified, voided, errors = 0, [], []
    for r in chosen:
        anchor = f"[run:{r['run_id']}]"
        try:
            client = pool.client_for(r["account"])
            comments = client.comments(r["window"])
        except (CnbApiError, AccountPoolError) as e:
            errors.append(f"{r['run_id']}: 查询失败（account={r['account']} "
                          f"window={r['window']}）: {e}")
            continue
        found = any(anchor in (c.get("body") or "") for c in comments
                    if isinstance(c, dict))
        if found:
            verified += 1
        else:
            entry = {"type": "artifact_voided", "run_id": r["run_id"],
                     "account": r["account"], "window": r["window"],
                     "receipt_file": r["receipt_file"],
                     "reason": "anchor_not_found_in_window",
                     "note": "回执声明该派单，但窗口评论中不存在 [run:锚]——"
                             "产物作废留痕（INV-01：自报数字不采信）",
                     "recorded_at": _iso(now)}
            _atomic_append_jsonl(state_dir / VOIDED_FILE, entry)
            voided.append(entry)
    result.update({"status": "red" if errors else "green",
                   "verified": verified, "voided": voided, "errors": errors})
    return result


# ────────────────────────── ③ 配额汇总（调 ledger） ──────────────────────────


def quota_channel(state_dir: Path, accounts_yaml: str | Path,
                  threshold_pct: float = 10.0) -> dict:
    """ledger 快照+对账+hash 链校验（等价 `python ledger.py --snapshot
    --reconcile` + `--verify`）。任何失败 = 通道红（fail-closed）。"""
    ledger_dir = state_dir / "ledger"
    try:
        snap = ledger.take_snapshot(accounts_yaml, ledger_dir)
        rec = ledger.reconcile(accounts_yaml, ledger_dir, threshold_pct)
        ver = ledger.verify_chain(ledger_dir / ledger.USAGE_FILE)
        status = "green" if ver["ok"] else "red"
        errors = [] if ver["ok"] else [f"usage.jsonl hash 链校验失败: "
                                       f"{ver.get('reason')}"]
        return {"status": status, "snapshot": snap, "reconcile": rec,
                "chain": ver, "errors": errors,
                "note": "台账真源=GitHub 侧（本地 audit-state/ledger 仅暂存，"
                        "变更随 PR 并回本仓 usage.jsonl）"}
    except (CnbApiError, AccountPoolError, ledger.LedgerError, OSError) as e:
        return {"status": "red", "errors": [f"配额通道失败: {e}"]}


# ────────────────────────── 周报汇总 ──────────────────────────


def weekly_report(state_dir: Path, accounts_yaml: str | Path,
                  sample_dir: Path, sample_pct: float, seed,
                  threshold_pct: float) -> dict:
    channels = {
        "policy": check_policy(state_dir),
        "artifact_sampling": sample_artifacts(sample_dir, sample_pct, seed,
                                              accounts_yaml, state_dir),
        "quota": quota_channel(state_dir, accounts_yaml, threshold_pct),
    }
    alerts = []
    for name, ch in channels.items():
        for al in ch.get("alerts", []) or []:
            al = dict(al)
            al.setdefault("channel", name)
            alerts.append(al)
        for v in ch.get("voided", []) or []:
            alerts.append(dict(v, channel=name))
    overall = "red" if any(c["status"] == "red" for c in channels.values()) else "green"
    return {
        "report": "cnb-bridge weekly-audit（AC-18 补全三项：政策 diff/产物抽样/配额汇总）",
        "generated_at": _iso(_utcnow()),
        "overall": overall,
        "channels": channels,
        "alerts": alerts,
        "fail_closed": "任一通道查询失败=red 且退出码 1；告警条目（政策变更/作废/"
                       "偏差）为检测成功产出，走 issue 处置不改红绿",
        "ledger_truth": "台账真源=GitHub 侧（本地仅暂存）",
    }


# ────────────────────────── CLI ──────────────────────────


def _default_seed() -> str:
    now = _utcnow().isocalendar()
    return f"{now[0]}-W{now[1]:02d}"   # ISO 周号：周内可复现、跨周轮换


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="audit_extra.py",
        description="周审计补全（AC-18 五项之三）：政策 diff / 产物真实性抽样 / 配额汇总；fail-closed",
    )
    ap.add_argument("--sample", nargs=2, metavar=("DIR", "PCT"), default=None,
                    help="仅产物真实性抽样：目录 + 百分比（如 fanout/products 10）")
    ap.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                    help=f"审计状态目录（默认 {DEFAULT_STATE_DIR}）")
    ap.add_argument("--accounts-yaml", default=str(cnb_pool.DEFAULT_ACCOUNTS_YAML),
                    help="accounts.yaml 路径（默认：cnb_pool.py 同目录）")
    ap.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR,
                    help=f"周审计全项时的抽样目录（默认 {DEFAULT_SAMPLE_DIR}）")
    ap.add_argument("--sample-pct", type=float, default=DEFAULT_SAMPLE_PCT,
                    help=f"抽样百分比（默认 {DEFAULT_SAMPLE_PCT}；BUDGET-03 可降不可免）")
    ap.add_argument("--seed", default=None,
                    help="抽样随机种子（默认 ISO 周号，周内可复现）")
    ap.add_argument("--threshold-pct", type=float, default=10.0,
                    help="配额对账偏差阈值（默认 10，透传 ledger）")
    ap.add_argument("--out", default=None, help="报告写入路径（同时打印 stdout）")
    args = ap.parse_args(argv)
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    seed = args.seed if args.seed is not None else _default_seed()
    try:
        if args.sample:
            sample_dir, pct = Path(args.sample[0]), float(args.sample[1])
            r = sample_artifacts(sample_dir, pct, seed, args.accounts_yaml, state)
            r = {"report": "cnb-bridge artifact-sampling", "channel": r}
            r["overall"] = r["channel"]["status"]
        else:
            r = weekly_report(state, args.accounts_yaml, Path(args.sample_dir),
                              args.sample_pct, seed, args.threshold_pct)
    except (CnbApiError, AccountPoolError, ledger.LedgerError, OSError) as e:
        r = {"overall": "red", "error": f"审计失败（fail-closed）: {e}",
             "generated_at": _iso(_utcnow())}
    text = json.dumps(r, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        tmp = out.with_name(out.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, out)
        finally:
            if tmp.exists():
                tmp.unlink()
    return 0 if r.get("overall") == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
