#!/usr/bin/env python3
"""test_ledger.py —— ledger.py 离线单测（monkeypatch urllib.request，零网络）。

覆盖：快照差分→usage.jsonl hash 链（追加+校验）、篡改检出（非零退出）、
对账偏差计算与告警（阈值口径 IFACE-05）、labels.cpus 缺失告警、快照不足
fail-closed、pending-access 账号不入账。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
_SIBLING = REPO_ROOT.parent / "cnb-bridge"   # 开发期布局：增量目录与仓体并列
if not (REPO_ROOT / "cnb_pool.py").is_file() and _SIBLING.is_dir():
    sys.path.append(str(_SIBLING))

import ledger  # noqa: E402
from cnb_pool import AccountPoolError  # noqa: E402

ACCOUNTS_YAML = """\
version: 1
accounts:
  - alias: xuemei
    secret_ref: CNB_TOKEN_TEST_XUEMEI
    quota_monthly_core_hours: 1600
    status: pending-access
    added_at: "2026-08-25"
  - alias: p11
    secret_ref: CNB_TOKEN_TEST_P11
    quota_monthly_core_hours: 1600
    status: active
    added_at: "2026-08-25"
dispatch:
  repo: Cloudbird-Software/talk
  default_tier: light
  max_concurrent_per_account: 8
  poll_interval_sec: 20
  reply_timeout_sec: 600
"""

TOK_P11 = "tok-p11"


class FakeResponse:
    def __init__(self, payload):
        self._data = payload if isinstance(payload, bytes) else \
            json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCnbWeb:
    """按 URL 路由的最小假体：quota（按 token 可变）/ build_logs / comments。"""

    def __init__(self, quota_by_token=None, build_logs_by_token=None):
        self.quota_by_token = quota_by_token or {}
        self.build_logs_by_token = build_logs_by_token or {}
        self.quota_calls: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        tok = req.headers.get("Authorization", "")
        if url.endswith("/-/charge/quota"):
            self.quota_calls.append(tok)
            return FakeResponse(self.quota_by_token[tok])
        if url.endswith("/-/build/logs"):
            return FakeResponse(self.build_logs_by_token.get(tok, []))
        if "/-/issues/" in url and "/comments" in url:
            return FakeResponse([])
        raise AssertionError(f"未预期的请求: {url}")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / "ledger-state"
        self.yaml_path = self.dir / "accounts.yaml"
        self.yaml_path.write_text(ACCOUNTS_YAML, encoding="utf-8")
        self.env = {"CNB_TOKEN_TEST_P11": TOK_P11}
        self.T0 = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    @contextlib.contextmanager
    def env_tokens(self, tokens=None):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CNB_TOKEN_TEST_P11", None)
            for k, v in (tokens or {}).items():
                os.environ[k] = v
            yield


class TestSnapshotChain(Base):
    def test_two_snapshots_delta_and_chain(self):
        """两次快照之差=时段用量：usage.jsonl 一行、used_core_sec=差值、hash 链 ok。"""
        quota = {"dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: quota})
        times = iter([self.T0, self.T0 + timedelta(minutes=10)])
        with self.env_tokens(self.env), \
                mock.patch("urllib.request.urlopen", new=fake), \
                mock.patch.object(ledger, "_utcnow", lambda: next(times)):
            r1 = ledger.take_snapshot(self.yaml_path, self.state)
            quota["dev_in_sec"]["free"] = 999_000   # 消耗 1000 核·秒
            r2 = ledger.take_snapshot(self.yaml_path, self.state)
        self.assertIsNone(r1["usage_entry"])          # 首次无基线：只建快照
        self.assertIsNotNone(r2["usage_entry"])
        self.assertEqual(r2["usage_entry"]["accounts"]["p11"]["used_core_sec"], 1000.0)
        self.assertEqual(r2["usage_entry"]["prev_hash"], ledger.GENESIS_HASH)
        ver = ledger.verify_chain(self.state / ledger.USAGE_FILE)
        self.assertTrue(ver["ok"], ver)
        self.assertEqual(ver["entries"], 1)

    def test_pending_access_account_not_queried(self):
        """status=pending-access 账号不入账（不查配额、不进快照）。"""
        quota = {"dev_in_sec": {"total": 100, "free": 100}}
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: quota})
        with self.env_tokens({"CNB_TOKEN_TEST_P11": TOK_P11}), \
                mock.patch("urllib.request.urlopen", new=fake):
            ledger.take_snapshot(self.yaml_path, self.state)
        self.assertEqual(fake.quota_calls, ["Bearer " + TOK_P11])  # 只查了 p11

    def test_token_missing_fail_closed(self):
        """active 账号 token 缺失 → 抛错（无任何 fallback）。"""
        with self.env_tokens(None), mock.patch("urllib.request.urlopen", new=FakeCnbWeb()):
            with self.assertRaises(AccountPoolError):
                ledger.take_snapshot(self.yaml_path, self.state)

    def test_tamper_detected_nonzero_exit(self):
        """篡改台账行内容 → verify ok=False 且 CLI --verify 非零退出。"""
        quota = {"dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: quota})
        times = iter([self.T0, self.T0 + timedelta(minutes=10)])
        with self.env_tokens(self.env), \
                mock.patch("urllib.request.urlopen", new=fake), \
                mock.patch.object(ledger, "_utcnow", lambda: next(times)):
            ledger.take_snapshot(self.yaml_path, self.state)
            quota["dev_in_sec"]["free"] = 999_000
            ledger.take_snapshot(self.yaml_path, self.state)
        usage = self.state / ledger.USAGE_FILE
        self.assertTrue(ledger.verify_chain(usage)["ok"])
        lines = usage.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        obj["accounts"]["p11"]["used_core_sec"] = 1.0   # 篡改：不重算 hash
        lines[0] = json.dumps(obj, ensure_ascii=False)
        usage.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ver = ledger.verify_chain(usage)
        self.assertFalse(ver["ok"])
        self.assertEqual(ver["first_bad_line"], 1)
        rc = ledger.main(["--verify", "--state-dir", str(self.state)])
        self.assertNotEqual(rc, 0)   # 篡改=非零退出


class TestReconcile(Base):
    def _snapshots(self, fake):
        times = iter([self.T0, self.T0 + timedelta(minutes=10)])
        with mock.patch.object(ledger, "_utcnow", lambda: next(times)):
            ledger.take_snapshot(self.yaml_path, self.state)
            fake.quota_by_token["Bearer " + TOK_P11]["dev_in_sec"]["free"] = 999_000
            ledger.take_snapshot(self.yaml_path, self.state)

    def test_deviation_over_threshold_alerts(self):
        """快照差分 1000 核·秒 vs build logs 800 核·秒（400s×2C）→ 偏差 25%>10 → 告警。"""
        quota = {"dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}
        logs = [
            {"duration": 400, "labels": {"cpus": 2},
             "created_at": (self.T0 + timedelta(seconds=30)).isoformat()},
            {"duration": 10, "created_at": (self.T0 + timedelta(seconds=60)).isoformat()},  # 无 labels
        ]
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: quota},
                          build_logs_by_token={"Bearer " + TOK_P11: logs})
        with self.env_tokens(self.env), \
                mock.patch("urllib.request.urlopen", new=fake):
            self._snapshots(fake)
            r = ledger.reconcile(self.yaml_path, self.state, threshold_pct=10.0)
        row = r["accounts"]["p11"]
        self.assertEqual(row["build_logs_core_sec"], 800.0)   # 400×2C；无标签条目不计入
        self.assertEqual(row["snapshot_core_sec"], 1000.0)
        self.assertEqual(row["deviation_pct"], 25.0)          # |1000-800|/800
        types = {a["type"] for a in r["alerts"]}
        self.assertIn("reconcile_deviation", types)
        self.assertIn("build_label_missing", types)           # labels.cpus 缺失即告警（AC-15）
        self.assertEqual(row["label_missing"], 1)
        alerts_file = self.state / ledger.ALERTS_FILE
        self.assertTrue(alerts_file.is_file())                # 告警条目已落盘

    def test_deviation_within_threshold_no_alert(self):
        quota = {"dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}
        logs = [{"duration": 500, "labels": {"cpus": 2},
                 "created_at": (self.T0 + timedelta(seconds=30)).isoformat()}]  # 1000 核·秒
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: quota},
                          build_logs_by_token={"Bearer " + TOK_P11: logs})
        with self.env_tokens(self.env), \
                mock.patch("urllib.request.urlopen", new=fake):
            self._snapshots(fake)
            r = ledger.reconcile(self.yaml_path, self.state, threshold_pct=10.0)
        self.assertEqual(r["accounts"]["p11"]["deviation_pct"], 0.0)
        self.assertFalse(r["accounts"]["p11"]["alert"])
        self.assertEqual(r["alerts"], [])

    def test_insufficient_snapshots_fail_closed(self):
        fake = FakeCnbWeb(quota_by_token={"Bearer " + TOK_P11: {
            "dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}})
        with self.env_tokens(self.env), mock.patch("urllib.request.urlopen", new=fake):
            ledger.take_snapshot(self.yaml_path, self.state)
            with self.assertRaises(ledger.LedgerError):
                ledger.reconcile(self.yaml_path, self.state)


if __name__ == "__main__":
    unittest.main()
