#!/usr/bin/env python3
"""test_cnb_pool.py —— cnb_pool 离线单测（monkeypatch urllib.request，零网络）。

运行（仓库根目录）：python -m unittest discover -s tests -v
覆盖：accounts.yaml 解析、token 缺失抛错、is_free 三态判定、dispatch 正文
（@CodeBuddy 前缀 + 执行纪律句 + [run:xxxx] 锚串）、pick 最少占用账号、
collect 回收/超时、API 错误带状态码、档位纪律。
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
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cnb_pool  # noqa: E402
from cnb_pool import (  # noqa: E402
    AccountPool,
    AccountPoolError,
    CnbApiError,
    CnbClient,
    DispatchError,
    build_task_body,
    windows_status,
)

ACCOUNTS_YAML = """\
version: 1
accounts:
  - alias: xuemei
    secret_ref: CNB_TOKEN_TEST_XUEMEI
    description: 测试甲
    quota_monthly_core_hours: 1600
    status: active
    added_at: "2026-08-25"
  - alias: p11
    secret_ref: CNB_TOKEN_TEST_P11
    description: 测试乙
    quota_monthly_core_hours: 1600
    status: active
    added_at: "2026-08-25"
dispatch:
  repo: Cloudbird-Software/talk
  windows: "#1-#100（issue 即窗口）"
  default_tier: light        # 行内注释应被剥离
  max_concurrent_per_account: 8
  poll_interval_sec: 20
  reply_timeout_sec: 600
"""

TOK_XUEMEI = "Bearer tok-xuemei"
TOK_P11 = "Bearer tok-p11"


class FakeResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCnbAPI:
    """按 Authorization 区分账号、按 URL 路由的最小 CNB API 假体。"""

    def __init__(self, issues=None, last_comment_by=None, quota=None):
        self.issues = issues if issues is not None else [{"number": n} for n in range(1, 101)]
        # (Authorization, issue_n) -> 该窗口评论列表（取最后一条判定）
        self.last_comment_by = last_comment_by or {}
        self.quota = quota or {"dev_in_sec": 1234}
        self.posts = []  # (Authorization, issue_n, body_dict)
        self.calls = []  # 全部请求 URL

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        token = req.headers.get("Authorization", "")
        m = re.search(r"/-/issues/(\d+)/comments", url)
        if m:
            n = int(m.group(1))
            if req.get_method() == "POST":
                body = json.loads(req.data.decode("utf-8"))
                self.posts.append((token, n, body))
                return FakeResponse({"id": 9000 + len(self.posts)})
            return FakeResponse(self.last_comment_by.get((token, n), []))
        if url.endswith("/-/charge/quota"):
            return FakeResponse(self.quota)
        if "/-/issues?" in url and req.get_method() == "GET":
            return FakeResponse(self.issues)
        if url.endswith("/-/build/logs"):
            return FakeResponse([])
        raise AssertionError(f"未预期的请求: {url}")


def human(body="占用中"):
    return {"author": {"is_npc": False}, "body": body}


def npc(body="NPC 回复"):
    return {"author": {"is_npc": True}, "body": body}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "accounts.yaml"
        path.write_text(ACCOUNTS_YAML, encoding="utf-8")
        self.yaml_path = str(path)
        self.env_both = {
            "CNB_TOKEN_TEST_XUEMEI": TOK_XUEMEI.removeprefix("Bearer "),
            "CNB_TOKEN_TEST_P11": TOK_P11.removeprefix("Bearer "),
        }

    @contextlib.contextmanager
    def env(self, tokens=None):
        """环境变量补丁：tokens 为 None 表示两个 secret 均未注入。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CNB_TOKEN_TEST_XUEMEI", None)
            os.environ.pop("CNB_TOKEN_TEST_P11", None)
            for k, v in (tokens or {}).items():
                os.environ[k] = v
            yield


class TestAccountsParsing(Base):
    def test_parse_repo_accounts_yaml(self):
        """仓库真实 accounts.yaml 可被极简解析器解析且关键字段无漂移。"""
        pool = AccountPool(REPO_ROOT / "accounts.yaml")
        self.assertEqual([a.alias for a in pool.accounts], ["xuemei", "p11"])
        self.assertEqual([a.secret_ref for a in pool.accounts],
                         ["CNB_TOKEN_XUEMEI", "CNB_TOKEN_P11"])
        self.assertEqual(pool.version, 1)
        self.assertEqual(pool.dispatch_cfg["repo"], "Cloudbird-Software/talk")
        self.assertEqual(pool.dispatch_cfg["default_tier"], "light")  # 行内注释已剥离
        self.assertEqual(pool.dispatch_cfg["max_concurrent_per_account"], 8)
        self.assertEqual(pool.dispatch_cfg["poll_interval_sec"], 20)
        self.assertEqual(pool.dispatch_cfg["reply_timeout_sec"], 600)

    def test_parse_temp_accounts_types(self):
        """引号字符串与整数的标量类型正确（added_at 保持字符串、配额为 int）。"""
        pool = AccountPool(self.yaml_path)
        a = pool.accounts[0]
        self.assertEqual(a.alias, "xuemei")
        self.assertEqual(a.secret_ref, "CNB_TOKEN_TEST_XUEMEI")
        self.assertEqual(a.added_at, "2026-08-25")  # 去引号后仍为 str
        self.assertEqual(a.quota_monthly_core_hours, 1600)  # int
        self.assertEqual(a.status, "active")
        self.assertEqual(pool.dispatch_cfg["windows"], "#1-#100（issue 即窗口）")

    def test_missing_file_raises(self):
        with self.assertRaises(AccountPoolError):
            AccountPool(Path(self.yaml_path).parent / "nope.yaml")


class TestTokenDiscipline(Base):
    def test_token_missing_raises_no_fallback(self):
        """secret 环境变量缺失即抛错——不得 fallback 到任何硬编码。"""
        with self.env(None):
            pool = AccountPool(self.yaml_path)
            with self.assertRaises(AccountPoolError) as cm:
                pool.token_for("xuemei")
            self.assertIn("CNB_TOKEN_TEST_XUEMEI", str(cm.exception))
            with self.assertRaises(AccountPoolError):
                pool.client_for("p11")
            with self.assertRaises(AccountPoolError):
                pool.pick()

    def test_token_from_env(self):
        with self.env(self.env_both):
            pool = AccountPool(self.yaml_path)
            client = pool.client_for("xuemei")
            self.assertIsInstance(client, CnbClient)
            self.assertEqual(client.token, "tok-xuemei")
            self.assertEqual(client.base, "https://api.cnb.cool/Cloudbird-Software/talk")


class TestClient(unittest.TestCase):
    def setUp(self):
        self.client = CnbClient("tok-t", "Cloudbird-Software/talk")

    def test_is_free_three_states(self):
        """三态：最后评论 npc → 空闲；最后评论人类 → 占用；无评论 → 空闲。"""
        fake = FakeCnbAPI(last_comment_by={
            ("Bearer tok-t", 1): [npc()],
            ("Bearer tok-t", 2): [human()],
            ("Bearer tok-t", 3): [npc(), human()],  # 最后一条是人类
            ("Bearer tok-t", 4): [human(), npc()],  # 最后一条是 NPC
        })
        with mock.patch("urllib.request.urlopen", new=fake):
            self.assertTrue(self.client.is_free(1))
            self.assertFalse(self.client.is_free(2))
            self.assertFalse(self.client.is_free(3))
            self.assertTrue(self.client.is_free(4))
            self.assertTrue(self.client.is_free(99))  # 无评论

    def test_urls_headers_and_quota_group(self):
        """URL 拼装、vnd.cnb.api+json 头、quota 取 repo owner 段。"""
        fake = FakeCnbAPI()
        with mock.patch("urllib.request.urlopen", new=fake):
            self.client.list_issues()
            self.client.comments(7)
            self.client.quota()
            self.client.post_comment(7, "@CodeBuddy x", work_mode=True)
        self.assertIn("https://api.cnb.cool/Cloudbird-Software/talk/-/issues?page_size=200",
                      fake.calls)
        self.assertIn("https://api.cnb.cool/Cloudbird-Software/talk/-/issues/7/comments?page_size=100&page=1",
                      fake.calls)   # 全量分页（实测教训：单页 50 漏最新评论）
        self.assertIn("https://api.cnb.cool/Cloudbird-Software/-/charge/quota",
                      fake.calls)
        self.assertIn("https://api.cnb.cool/Cloudbird-Software/talk/-/issues/7/comments",
                      fake.calls)
        token, n, body = fake.posts[-1]
        self.assertEqual(token, "Bearer tok-t")
        self.assertEqual(n, 7)
        self.assertEqual(body, {"body": "@CodeBuddy x", "work_mode": True})

    def test_api_error_carries_status(self):
        """HTTPError 转 CnbApiError 且携带状态码（fail-closed）。"""
        def raise_401(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, None)

        with mock.patch("urllib.request.urlopen", new=raise_401):
            with self.assertRaises(CnbApiError) as cm:
                self.client.list_issues()
        self.assertEqual(cm.exception.status, 401)

    def test_windows_status_shape(self):
        fake = FakeCnbAPI(last_comment_by={
            ("Bearer tok-t", 1): [human()],
            ("Bearer tok-t", 3): [human()],
        })
        with mock.patch("urllib.request.urlopen", new=fake):
            sts = windows_status(self.client)
        self.assertEqual(len(sts), 100)
        self.assertEqual([s["number"] for s in sts], list(range(1, 101)))  # 升序
        by_n = {s["number"]: s["free"] for s in sts}
        self.assertFalse(by_n[1])
        self.assertTrue(by_n[2])
        self.assertFalse(by_n[3])
        self.assertTrue(by_n[100])


class TestDispatchAndPick(Base):
    def busy_setup(self):
        """xuemei 占 3 窗（1/2/3），p11 占 1 窗（1）。"""
        return FakeCnbAPI(last_comment_by={
            (TOK_XUEMEI, 1): [human()], (TOK_XUEMEI, 2): [human()], (TOK_XUEMEI, 3): [human()],
            (TOK_P11, 1): [human()],
        })

    def test_pick_least_busy(self):
        """默认选当前占用窗口数最少的账号。"""
        fake = self.busy_setup()
        with self.env(self.env_both), mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            self.assertEqual(pool.pick().alias, "p11")  # 1 占用 < 3 占用

    def test_pick_prefer(self):
        """prefer 指定别名优先（即使占用更多）。"""
        fake = self.busy_setup()
        with self.env(self.env_both), mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            self.assertEqual(pool.pick(prefer="xuemei").alias, "xuemei")

    def test_dispatch_body_and_result(self):
        """自动选最少占用账号、占最靠前空闲窗、正文含 @CodeBuddy/纪律句/锚串。"""
        fake = self.busy_setup()
        with self.env(self.env_both), mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            r = pool.dispatch("在沙箱运行 make check", tier="light", run_id="ab12cd34")
        self.assertEqual(r["account"], "p11")       # 占用最少者
        self.assertEqual(r["window"], 2)            # p11 窗口 1 占用 → 取 2
        self.assertEqual(r["run_id"], "ab12cd34")
        self.assertTrue(r["posted_at"])
        self.assertEqual(len(fake.posts), 1)
        token, n, payload = fake.posts[0]
        self.assertEqual(token, TOK_P11)
        self.assertEqual(n, 2)
        self.assertTrue(payload["work_mode"])
        body = payload["body"]
        self.assertTrue(body.startswith("@CodeBuddy"))
        self.assertIn("真实执行并贴出原始输出与退出码", body)
        self.assertIn("[run:ab12cd34]", body)
        self.assertIn("make check", body)

    def test_dispatch_specified_account(self):
        fake = self.busy_setup()
        with self.env(self.env_both), mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            r = pool.dispatch("跑冒烟", account="xuemei")
        self.assertEqual(r["account"], "xuemei")
        self.assertEqual(r["window"], 4)  # xuemei 1/2/3 占用 → 取 4
        self.assertEqual(fake.posts[0][0], TOK_XUEMEI)

    def test_dispatch_all_busy_raises(self):
        busy = {(TOK_P11, n): [human()] for n in range(1, 101)}
        fake = FakeCnbAPI(last_comment_by=busy)
        with self.env({"CNB_TOKEN_TEST_P11": "tok-p11"}), \
                mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            with self.assertRaises(DispatchError):
                pool.dispatch("无窗可占", account="p11")

    def test_tier_discipline(self):
        """heavy 须 ADR、非法档位（含 8C）拒绝、std 须理由。"""
        fake = FakeCnbAPI()
        with self.env(self.env_both), mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            with self.assertRaises(DispatchError):
                pool.dispatch("缺 ADR 的重活", tier="heavy", account="p11")
            with self.assertRaises(DispatchError):
                pool.dispatch("8C 尝试", tier="8c", account="p11")
            with self.assertRaises(DispatchError):
                pool.dispatch("两核跑批（未写原因）", tier="std", account="p11")
            r = pool.dispatch("重活（依据 ADR-0042）", tier="heavy", account="p11")
            self.assertEqual(r["tier"], "heavy")

    def test_build_task_body_anchor(self):
        body = build_task_body("任务", "light", "deadbeef")
        self.assertTrue(body.startswith("@CodeBuddy 任务"))
        self.assertIn("[run:deadbeef]", body)


class TestCollect(Base):
    def test_collect_npc_reply(self):
        """NPC 回复含锚串 → 返回全文。"""
        fake = FakeCnbAPI(last_comment_by={
            (TOK_XUEMEI, 5): [npc(body="$ make check\nall ok\n[run:ab12cd34]\nexit 0")],
        })
        with self.env({"CNB_TOKEN_TEST_XUEMEI": "tok-xuemei"}), \
                mock.patch("urllib.request.urlopen", new=fake):
            pool = AccountPool(self.yaml_path)
            out = pool.collect("xuemei", 5, "ab12cd34", timeout=1)
        self.assertEqual(out, "$ make check\nall ok\n[run:ab12cd34]\nexit 0")

    def test_collect_timeout_returns_none(self):
        """无含锚串的 NPC 回复 → 超时返回 None（且不悬挂轮询）。"""
        fake = FakeCnbAPI(last_comment_by={
            (TOK_XUEMEI, 5): [npc(body="锚串不符的回复")],
        })
        with self.env({"CNB_TOKEN_TEST_XUEMEI": "tok-xuemei"}), \
                mock.patch("urllib.request.urlopen", new=fake), \
                mock.patch("time.sleep"):
            pool = AccountPool(self.yaml_path)
            self.assertIsNone(pool.collect("xuemei", 5, "ab12cd34", timeout=0))


class TestNoHardcodedToken(unittest.TestCase):
    def test_source_has_no_literal_token(self):
        """源码不得出现任何 Bearer 字面量 token / 硬编码 secret。"""
        src = (REPO_ROOT / "cnb_pool.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"Bearer [A-Za-z0-9]{16,}", src))
        self.assertNotIn("1Z19fbfo7dRlDNCtokj2vz3Ql1C", src)  # 参考客户端里的真实 token 绝不带入


if __name__ == "__main__":
    unittest.main()
