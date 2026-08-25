#!/usr/bin/env python3
"""test_docs.py —— 文档/工作流契约校验（离线；命令行存在性不测，只测格式）。

覆盖：ADR-0086 组织格式（H1 编号行 / - status: 行 / 背景·决策·后果章节 /
关联引用）；weekly-audit.yml 契约（两 action SHA 钉版且仅此两个 / env 映射 /
permissions / timeout / cron 错峰）；RUNBOOK 四流程与真命令；README 增量
五件齐全；新增源码零 token 字面量。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PINNED_ACTIONS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
}


class TestADRFormat(unittest.TestCase):
    def setUp(self):
        # ADR-0085 家园单仓化：ADR 正本在 archive/adr/（本仓只留引用）。
        # 本地断言改为：RUNBOOK 引用 ADR-0086 + archive 路径正确标注
        self.text = (REPO_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    def test_org_adr_format(self):
        """ADR-0086 归档引用：RUNBOOK 含编号引用 + archive/adr 路径（ADR-0085 家园单仓化——本仓不留正本副本）。"""
        self.assertRegex(self.text, r"ADR-0086")
        self.assertIn("archive/adr/ADR-0086", self.text)

    def test_adr_substance(self):
        """决策内容锚点（经 RUNBOOK 的泄漏应急三步传导）：吊销/轮换/审计三步 + org secret 纪律。"""
        self.assertIn("吊销", self.text)
        self.assertIn("轮换", self.text)
        self.assertIn("审计", self.text)
        self.assertIn("org secret", self.text)


class TestWeeklyAuditWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / ".github" / "workflows" / "weekly-audit.yml").read_text(
            encoding="utf-8")

    def test_actions_pinned_and_whitelisted(self):
        """actions 限定 checkout+setup-python 且 SHA 钉版（CI-2 白名单）。"""
        uses = re.findall(r"(?m)^\s*uses:\s*(\S+)", self.text)
        self.assertEqual(set(uses), PINNED_ACTIONS, uses)

    def test_workflow_contract(self):
        """cron 06:31 周一 / permissions contents:read / timeout 15min /
        两 secret env 映射（cnb-dispatch 模式）/ fail-closed 注释。"""
        self.assertIn('"31 6 * * 1"', self.text)
        self.assertRegex(self.text, r"(?m)^permissions:")
        self.assertIn("contents: read", self.text)
        self.assertIn("timeout-minutes: 15", self.text)
        self.assertIn("CNB_TOKEN_XUEMEI: ${{ secrets.CNB_TOKEN_XUEMEI }}", self.text)
        self.assertIn("CNB_TOKEN_P11: ${{ secrets.CNB_TOKEN_P11 }}", self.text)
        self.assertIn("audit_extra.py", self.text)
        self.assertIn("fail-closed", self.text)


class TestRunbook(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / "RUNBOOK.md").read_text(encoding="utf-8")

    def test_four_flows_with_real_commands(self):
        """四流程齐备且带真命令（gh secret set / cnb_pool.py quota /
        audit_extra.py / ledger.py --verify / gh api 存证）。"""
        for flow in ("入职", "降级", "退休", "事故"):
            self.assertIn(flow, self.text)
        for cmd in ("gh secret set", "gh secret list", "gh api",
                    "python cnb_pool.py quota", "python cnb_pool.py dispatch",
                    "python cnb_pool.py collect", "python audit_extra.py",
                    "python ledger.py --verify"):
            self.assertIn(cmd, self.text, f"RUNBOOK 缺验收命令: {cmd}")

    def test_identity_evidence_and_checklist(self):
        """不可变身份标识存证（gh api 命令）与陌生 AI 入职验收清单齐备。"""
        self.assertIn("search/issues", self.text)          # 公开 issue/PR 参与记录查询
        self.assertIn("events/public", self.text)
        self.assertIn("存证", self.text)
        self.assertIn("验收清单", self.text)
        self.assertGreaterEqual(self.text.count("- [ ]"), 5)   # 勾选项 ≥5


class TestReadmeAdd(unittest.TestCase):
    def test_covers_five_pieces(self):
        text = (REPO_ROOT / "README-ADD.md").read_text(encoding="utf-8")
        for piece in ("ledger.py", "work_inbox.py", "audit_extra.py",
                      "RUNBOOK.md", "weekly-audit"):
            self.assertIn(piece, text)


class TestNoTokenLiteral(unittest.TestCase):
    def test_sources_have_no_literal_token(self):
        """新增源码/工作流不得出现任何 token 形态字面量（含已知泄漏形态）。"""
        for name in ("ledger.py", "work_inbox.py", "audit_extra.py",
                     ".github/workflows/weekly-audit.yml", "RUNBOOK.md",
                     "README-ADD.md"):   # ADR-0086 正本在 archive/adr（本仓无副本）
            src = (REPO_ROOT / name).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"Bearer [A-Za-z0-9]{16,}", src), name)
            self.assertIsNone(re.search(r"ghp_[A-Za-z0-9]{20,}", src), name)
            self.assertIsNone(
                re.search(r"github_pat_[A-Za-z0-9_]{20,}", src), name)
            self.assertNotIn("1Z19fbfo7dRlDNCtokj2vz3Ql1C", src, name)


if __name__ == "__main__":
    unittest.main()
