#!/usr/bin/env python3
"""test_work_inbox.py —— work_inbox.py 离线单测（零网络：本模块无任何网络面）。

覆盖：--check 三态（claim/heartbeat/none）、幂等（同 list_id 二次认领拒绝）、
租约过期接管（原认领作废留痕）、--release 终态落 history、--sweep 停摆告警、
原子写（kill 模拟后原文件不损、无残 tmp）、round-trip（写后可再解析）。
"""

from __future__ import annotations

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

import work_inbox as wi  # noqa: E402

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

HEAD = """\
version: 1

protocol:
  heartbeat_interval_min: 30
  lease_ttl_min: 60
"""

PENDING_ONE = """\
pending:
  - id: WI-0001
    title: "对账脚本补测试"
    source: pm-self
    anchor: "anc-0001"
    created_at: "2026-08-24T00:00:00+00:00"
"""

LEASE_IDLE = """\
lease:
  active: false
  holder: null
  list_id: null
  anchor: null
  window: null
  claimed_at: null
  expires_at: null
"""

LEASE_ACTIVE_A = """\
lease:
  active: true
  holder: "pm-a"
  list_id: WI-0009
  anchor: "anc-0009"
  window: 7
  claimed_at: "2026-08-24T11:00:00+00:00"
  expires_at: "2026-08-24T13:00:00+00:00"
"""

HEARTBEAT_NULL = """\
heartbeat:
  at: null
  window: null
  note: null
"""

HISTORY_DONE_0001 = """\
history:
  - type: work
    list_id: WI-0001
    anchor: "anc-0001"
    holder: "pm-old"
    outcome: released
    released_at: "2026-08-23T00:00:00+00:00"
"""


def inbox(pending="pending: []\n", lease=LEASE_IDLE,
          heartbeat=HEARTBEAT_NULL, history="history: []\n") -> str:
    return (HEAD + "\n" + pending + "\n" + lease + "\n" + heartbeat + "\n"
            + history)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "work-inbox.yaml"
        self.write(inbox())
        self.orig = None

    def write(self, text):
        self.path.write_text(text, encoding="utf-8", newline="\n")

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def parse(self) -> dict:
        return wi.parse_inbox(self.read())

    def no_tmp_left(self):
        self.assertEqual(list(self.dir.glob("*.tmp")), [])


class TestThreeStates(Base):
    def test_claim(self):
        """有 pending 且无活跃 lease → claim：置租约、pending 清空。"""
        self.write(inbox(pending=PENDING_ONE))
        r = wi.check(self.path, holder="pm-b", now=T0)
        self.assertEqual(r["action"], "claim")
        self.assertEqual(r["item"]["id"], "WI-0001")
        m = self.parse()
        self.assertEqual(m["pending"], [])
        lease = m["lease"]
        self.assertTrue(lease["active"])
        self.assertEqual(lease["holder"], "pm-b")
        self.assertEqual(lease["list_id"], "WI-0001")
        self.assertEqual(lease["anchor"], "anc-0001")
        self.assertEqual(lease["expires_at"],
                         (T0 + timedelta(minutes=60)).isoformat(timespec="seconds"))
        self.no_tmp_left()

    def test_heartbeat_when_no_pending(self):
        """无 pending → 追加心跳行退出（heartbeat.at 刷新，零派单）。"""
        r = wi.check(self.path, holder="pm-b", now=T0)
        self.assertEqual(r["action"], "heartbeat")
        self.assertIsNone(r["item"])
        m = self.parse()
        self.assertEqual(m["heartbeat"]["at"],
                         T0.isoformat(timespec="seconds"))
        self.assertFalse(m["lease"]["active"])
        self.no_tmp_left()

    def test_none_when_lease_busy(self):
        """有 pending 但活跃租约在他人手里 → none：不动文件（并发不重复认领）。"""
        self.write(inbox(pending=PENDING_ONE, lease=LEASE_ACTIVE_A))
        before = self.read()
        r = wi.check(self.path, holder="pm-b", now=T0)   # 12:00 < 13:00 未过期
        self.assertEqual(r["action"], "none")
        self.assertEqual(r["reason"], "lease_busy")
        self.assertEqual(self.read(), before)            # 文件字节不变
        self.no_tmp_left()


class TestIdempotency(Base):
    def test_duplicate_list_id_rejected(self):
        """同 list_id 已在 history 终态 → 二次认领拒绝（rc=1，pending 不动）。"""
        self.write(inbox(pending=PENDING_ONE, history=HISTORY_DONE_0001))
        before = self.read()
        r = wi.check(self.path, holder="pm-b", now=T0)
        self.assertEqual(r["action"], "none")
        self.assertEqual(r["reason"], "duplicate_list_id")
        self.assertEqual(r["rc"], 1)
        self.assertEqual(self.read(), before)
        rc = wi.main(["--check", "--inbox", str(self.path), "--holder", "pm-b"])
        self.assertEqual(rc, 1)

    def test_voided_history_does_not_block(self):
        """作废（voided_lease_expired）非终态：不阻断再认领（接管语义）。"""
        hist = """\
history:
  - type: work
    list_id: WI-0009
    anchor: "anc-0009"
    holder: "pm-old"
    outcome: voided_lease_expired
    voided_at: "2026-08-24T10:00:00+00:00"
"""
        lease = """\
lease:
  active: true
  holder: "pm-old"
  list_id: WI-0009
  anchor: "anc-0009"
  window: 7
  claimed_at: "2026-08-24T09:00:00+00:00"
  expires_at: "2026-08-24T10:00:00+00:00"
"""
        self.write(inbox(lease=lease, history=hist))
        r = wi.check(self.path, holder="pm-new", now=T0)   # 已过期 → 接管
        self.assertEqual(r["action"], "claim")
        self.assertTrue(r["takeover"])
        m = self.parse()
        self.assertEqual(m["lease"]["holder"], "pm-new")
        outcomes = [h["outcome"] for h in m["history"]]
        self.assertIn("voided_lease_expired", outcomes)    # 原认领作废留痕
        voided = [h for h in m["history"] if h["outcome"] == "voided_lease_expired"][-1]
        self.assertEqual(voided["holder"], "pm-old")


class TestTakeover(Base):
    def test_expired_lease_taken_over_with_trace(self):
        """lease 过期可接管：history 追加作废留痕 + 新租约。"""
        self.write(inbox(lease=LEASE_ACTIVE_A))   # 13:00 过期
        now = T0 + timedelta(minutes=90)          # 13:30
        r = wi.check(self.path, holder="pm-b", now=now)
        self.assertEqual(r["action"], "claim")
        self.assertTrue(r["takeover"])
        self.assertEqual(r["item"]["id"], "WI-0009")
        self.assertIsNotNone(r["voided"])
        m = self.parse()
        self.assertEqual(m["lease"]["holder"], "pm-b")
        self.assertEqual(m["lease"]["expires_at"],
                         (now + timedelta(minutes=60)).isoformat(timespec="seconds"))
        self.assertEqual(len(m["history"]), 1)
        self.assertEqual(m["history"][0]["outcome"], "voided_lease_expired")
        self.assertEqual(m["history"][0]["holder"], "pm-a")
        self.no_tmp_left()


class TestRelease(Base):
    def test_release_clears_lease_and_appends_history(self):
        self.write(inbox(lease=LEASE_ACTIVE_A))
        r = wi.release(self.path, "WI-0009", outcome="done",
                       note="产物已写回 fanout/products/", now=T0)
        self.assertEqual(r["action"], "release")
        m = self.parse()
        self.assertFalse(m["lease"]["active"])
        self.assertIsNone(m["lease"]["holder"])
        self.assertEqual(len(m["history"]), 1)
        entry = m["history"][0]
        self.assertEqual(entry["list_id"], "WI-0009")
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(entry["note"], "产物已写回 fanout/products/")
        # 释放后再次 release 拒绝（无活跃租约）
        with self.assertRaises(wi.InboxError):
            wi.release(self.path, "WI-0009", now=T0)


class TestSweep(Base):
    def test_sweep_alert_after_grace(self):
        """到期未释放且未被接管超阈值 → 告警条目（开 issue 文本）。"""
        self.write(inbox(lease=LEASE_ACTIVE_A))   # 13:00 到期，TTL=60 → 宽限至 14:00
        r = wi.sweep(self.path, now=T0 + timedelta(minutes=130))   # 14:10
        self.assertTrue(r["stalled"])
        alert = r["alert"]
        self.assertEqual(alert["type"], "lease_stall")
        self.assertIn("WI-0009", alert["issue_title"])
        self.assertIn("--release", alert["issue_body"])   # 处置指引含真命令
        self.assertEqual(self.read(), inbox(lease=LEASE_ACTIVE_A))  # sweep 只读

    def test_sweep_quiet_within_grace(self):
        self.write(inbox(lease=LEASE_ACTIVE_A))
        r = wi.sweep(self.path, now=T0 + timedelta(minutes=45))    # 12:45 未到期
        self.assertFalse(r["stalled"])
        r = wi.sweep(self.path, now=T0 + timedelta(minutes=80))    # 13:20 < 14:00 宽限内
        self.assertFalse(r["stalled"])


class TestAtomicWrite(Base):
    def test_kill_simulation_leaves_file_intact(self):
        """rename 前被 kill（os.replace 抛错）→ 原文件字节不变、无残 tmp。"""
        self.write(inbox(pending=PENDING_ONE))
        before = self.read()
        with mock.patch("work_inbox.os.replace",
                        side_effect=OSError("模拟 kill：rename 前崩溃")):
            with self.assertRaises(OSError):
                wi.check(self.path, holder="pm-b", now=T0)
        self.assertEqual(self.read(), before)
        self.no_tmp_left()


class TestRoundTrip(Base):
    def test_render_then_parse_round_trip(self):
        """写出的文件可再解析且语义一致（claim→release 全程字段不漂移）。"""
        self.write(inbox(pending=PENDING_ONE))
        wi.check(self.path, holder="pm-b", now=T0)
        wi.release(self.path, "WI-0001", outcome="done", note="x",
                   now=T0 + timedelta(minutes=5))
        m = self.parse()
        self.assertEqual(m["lease"]["holder"], None)
        self.assertEqual(m["history"][-1]["list_id"], "WI-0001")
        # 渲染文本本身可整体再解析（模板无畸变）
        m2 = wi.parse_inbox(wi.render_inbox(m))
        self.assertEqual(m2["history"], m["history"])
        self.assertEqual(m2["lease"], m["lease"])
        self.assertEqual(m2["pending"], m["pending"])


if __name__ == "__main__":
    unittest.main()
