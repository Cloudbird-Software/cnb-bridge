#!/usr/bin/env python3
"""test_audit_extra.py —— audit_extra.py 离线单测（monkeypatch urllib.request，零网络）。

覆盖：政策页变更检测（fixture 两版页面：基线→变更→告警含 diff 补丁）、
政策页网络失败=通道红（infra 红非绿）、产物真实性抽样（fixture 回执：
锚串在窗口评论中存在=verified / 缺失=作废留痕条目）、周报 fail-closed
（配额通道查询失败=overall red）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
_SIBLING = REPO_ROOT.parent / "cnb-bridge"   # 开发期布局：增量目录与仓体并列
if not (REPO_ROOT / "cnb_pool.py").is_file() and _SIBLING.is_dir():
    sys.path.append(str(_SIBLING))

import audit_extra as ae  # noqa: E402
import ledger  # noqa: E402

ACCOUNTS_YAML = """\
version: 1
accounts:
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
PAGE_V1 = ("workspaces-intro", b"<html><body>CNB workspaces intro v1: free quota 1600 core-hours</body></html>")
PAGE_V2 = ("workspaces-intro", b"<html><body>CNB workspaces intro v2: free quota adjusted to 800 core-hours</body></html>")
PAGE_PRICING_V1 = ("pricing", b"<html><body>pricing v1</body></html>")


class FakeResponse:
    def __init__(self, payload: bytes):
        self._data = payload

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWeb:
    """按 URL 路由：docs.cnb.cool 政策页 / api.cnb.cool quota/build_logs/comments。"""

    def __init__(self, pages=None, quota=None, build_logs=None, comments=None,
                 fail_hosts=()):
        self.pages = dict(pages or {})      # url -> bytes
        self.quota = quota
        self.build_logs = build_logs or []
        self.comments = comments or {}      # (token, window) -> [ {body} ]
        self.fail_hosts = fail_hosts

    def __call__(self, req, timeout=None):
        url = req.full_url
        for host in self.fail_hosts:
            if host in url:
                raise urllib.error.URLError(f"模拟不可达: {host}")
        if url.startswith("https://docs.cnb.cool/"):
            return FakeResponse(self.pages[url])
        tok = req.headers.get("Authorization", "")
        if url.endswith("/-/charge/quota"):
            return FakeResponse(json.dumps(self.quota).encode("utf-8"))
        if url.endswith("/-/build/logs"):
            return FakeResponse(json.dumps(self.build_logs).encode("utf-8"))
        if "/-/issues/" in url and "/comments" in url:
            return FakeResponse(json.dumps(
                self.comments.get((tok, self._window(url)), [])).encode("utf-8"))
        raise AssertionError(f"未预期的请求: {url}")

    @staticmethod
    def _window(url: str) -> int:
        m = re.search(r"/-/issues/(\d+)/comments", url)
        return int(m.group(1)) if m else -1


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / "audit-state"
        self.yaml_path = self.dir / "accounts.yaml"
        self.yaml_path.write_text(ACCOUNTS_YAML, encoding="utf-8")
        self.T0 = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    @contextlib.contextmanager
    def env_tokens(self, tokens=None):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CNB_TOKEN_TEST_P11", None)
            for k, v in (tokens or {}).items():
                os.environ[k] = v
            yield

    def pages_dict(self, intro: bytes) -> dict:
        return {ae.POLICY_PAGES[0]["url"]: intro,
                ae.POLICY_PAGES[1]["url"]: PAGE_PRICING_V1[1]}


class TestPolicy(Base):
    def test_change_detection_with_fixture_pages(self):
        """fixture 两版页面：首跑建基线→页面变更→告警（含 diff 补丁）→再跑稳定。"""
        fake = FakeWeb(pages=self.pages_dict(PAGE_V1[1]))
        with mock.patch("urllib.request.urlopen", new=fake):
            r1 = ae.check_policy(self.state)
        self.assertEqual(r1["status"], "green")
        self.assertEqual(r1["alerts"], [])
        self.assertTrue(all(p.get("baseline_established") for p in r1["pages"]))
        # 页面更新为 v2
        fake.pages = self.pages_dict(PAGE_V2[1])
        with mock.patch("urllib.request.urlopen", new=fake):
            r2 = ae.check_policy(self.state)
        self.assertEqual(r2["status"], "green")     # 检出变更≠通道红
        self.assertEqual(len(r2["alerts"]), 1)
        alert = r2["alerts"][0]
        self.assertEqual(alert["type"], "policy_change")
        self.assertEqual(alert["page"], "workspaces-intro")
        self.assertNotEqual(alert["old_sha256"], alert["new_sha256"])
        self.assertIn("https://docs.cnb.cool/zh/workspaces/intro.html", alert["url"])
        patch_path = self.state / alert["diff"]
        self.assertTrue(patch_path.is_file())
        patch_text = patch_path.read_text(encoding="utf-8")
        self.assertIn("1600", patch_text)          # diff 补丁含旧内容
        self.assertIn("800", patch_text)           # …与新内容
        # 基线已更新：同内容再跑 → 无告警
        with mock.patch("urllib.request.urlopen", new=fake):
            r3 = ae.check_policy(self.state)
        self.assertEqual(r3["alerts"], [])

    def test_network_failure_is_infra_red(self):
        """docs.cnb.cool 拉取失败 → 通道红（INV-04：网络失败=红非绿）。"""
        fake = FakeWeb(pages=self.pages_dict(PAGE_V1[1]),
                       fail_hosts=("docs.cnb.cool",))
        with mock.patch("urllib.request.urlopen", new=fake):
            r = ae.check_policy(self.state)
        self.assertEqual(r["status"], "red")
        self.assertTrue(r["errors"])


class TestSampling(Base):
    def test_receipt_sampling_verify_and_void(self):
        """fixture 回执：锚串在窗口评论存在=verified；缺失=作废留痕（voided.jsonl）。"""
        recv = self.dir / "receipts"
        recv.mkdir()
        (recv / "run1.txt").write_text(
            "AUDIT | cnb-dispatch | account=p11 | tier=light | cpus=1 | card=none"
            " | window=7 | run_id=aa11\n", encoding="utf-8")
        (recv / "run2.json").write_text(json.dumps({
            "account": "p11", "window": 8, "run_id": "bb22",
            "posted_at": "2026-08-24T00:00:00+00:00", "tier": "light"}, indent=2),
            encoding="utf-8")   # 整文件多行 JSON 同样可抽
        fake = FakeWeb(comments={
            ("Bearer " + TOK_P11, 7): [
                {"author": {"is_npc": True}, "body": "$ echo ok\nok\n[run:aa11]\nexit 0"}],
            ("Bearer " + TOK_P11, 8): [
                {"author": {"is_npc": True}, "body": "锚串不符的回复（无对账锚串）"}],
        })
        with self.env_tokens({"CNB_TOKEN_TEST_P11": TOK_P11}), \
                mock.patch("urllib.request.urlopen", new=fake):
            r = ae.sample_artifacts(recv, 100.0, "seed-1", self.yaml_path, self.state)
        self.assertEqual(r["status"], "green")
        self.assertEqual(r["receipts"], 2)
        self.assertEqual(r["sampled"], 2)
        self.assertEqual(r["verified"], 1)
        self.assertEqual(len(r["voided"]), 1)
        self.assertEqual(r["voided"][0]["run_id"], "bb22")
        self.assertEqual(r["voided"][0]["reason"], "anchor_not_found_in_window")
        voided = [json.loads(l) for l in
                  (self.state / ae.VOIDED_FILE).read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(voided), 1)                 # 作废留痕条目已落盘
        self.assertEqual(voided[0]["run_id"], "bb22")

    def test_empty_dir_is_zero_consumption_not_red(self):
        """目录无回执 → 零消费非红（AC-13 语义）。"""
        with self.env_tokens({"CNB_TOKEN_TEST_P11": TOK_P11}):
            r = ae.sample_artifacts(self.dir / "nope", 10.0, "s",
                                    self.yaml_path, self.state)
        self.assertEqual(r["status"], "green")
        self.assertEqual(r["sampled"], 0)


class TestWeeklyFailClosed(Base):
    def test_quota_channel_failure_makes_report_red(self):
        """fail-closed：配额通道查询失败（token 缺失）→ overall=red、CLI 退出 1。"""
        fake = FakeWeb(pages=self.pages_dict(PAGE_V1[1]),
                       quota={"dev_in_sec": {"total": 100, "free": 100}})
        with self.env_tokens(None), \
                mock.patch("urllib.request.urlopen", new=fake):
            rep = ae.weekly_report(self.state, self.yaml_path,
                                   self.dir / "no-fanout", 10.0, "seed", 10.0)
        self.assertEqual(rep["channels"]["policy"]["status"], "green")
        self.assertEqual(rep["channels"]["artifact_sampling"]["status"], "green")
        self.assertEqual(rep["channels"]["quota"]["status"], "red")
        self.assertEqual(rep["overall"], "red")
        rc = ae.main(["--state-dir", str(self.state),
                      "--accounts-yaml", str(self.yaml_path),
                      "--sample-dir", str(self.dir / "no-fanout"),
                      "--seed", "seed", "--out", str(self.dir / "r.json")])
        self.assertEqual(rc, 1)
        self.assertTrue((self.dir / "r.json").is_file())

    def test_green_week_after_two_snapshots(self):
        """两轮周报（建立快照基线）后全通道 green：对账零偏差、hash 链 ok。"""
        quota = {"dev_in_sec": {"total": 5_760_000, "free": 1_000_000}}
        clock = {"t": self.T0}

        def tick():
            clock["t"] = clock["t"] + timedelta(minutes=10)
            return clock["t"]

        fake = FakeWeb(pages=self.pages_dict(PAGE_V1[1]), quota=quota,
                       build_logs=[{"duration": 1000, "labels": {"cpus": 1},
                                    "created_at":
                                    (self.T0 + timedelta(minutes=15)).isoformat()}])
        with self.env_tokens({"CNB_TOKEN_TEST_P11": TOK_P11}), \
                mock.patch("urllib.request.urlopen", new=fake), \
                mock.patch.object(ledger, "_utcnow", new=tick):
            first = ae.weekly_report(self.state, self.yaml_path,
                                     self.dir / "no-fanout", 10.0, "seed", 10.0)
            self.assertEqual(first["channels"]["quota"]["status"], "red",
                             "首周快照不足两次须红（fail-closed）")
            quota["dev_in_sec"]["free"] = 999_000   # 本周消耗 1000 核·秒
            rep = ae.weekly_report(self.state, self.yaml_path,
                                   self.dir / "no-fanout", 10.0, "seed", 10.0)
        self.assertEqual(rep["channels"]["quota"]["status"], "green",
                         rep["channels"]["quota"].get("errors"))
        rec = rep["channels"]["quota"]["reconcile"]
        self.assertEqual(rec["accounts"]["p11"]["deviation_pct"], 0.0)
        self.assertEqual(rep["overall"], "green")


if __name__ == "__main__":
    unittest.main()
