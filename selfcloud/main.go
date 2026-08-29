package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"
)

// main CLI——v0 子命令：
//
//	selfcloud validate      --contract c.json --allowlist api.github.com,vault.internal
//	selfcloud issue-ticket  --contract c.json --key-file k.bin [--ts RFC3339]
//	selfcloud verify-ticket --key-file k.bin --token <token> [--ts RFC3339]
//	selfcloud emit-ledger   --file f.jsonl --event-file e.json
//	selfcloud gh-token      --app-id N --key-file k.pem --org O --repo R --card o/r#n
//	                         [--ledger f.jsonl] [--api base] [--expect-token]（W2-C2 代签）
//	selfcloud gh-token-check  --token <t> --repo o/r [--api base]（断言：200=有效）
//	selfcloud gh-token-revoke --token <t> --card o/r#n --repo o/r [--ledger f.jsonl]
//	                         [--api base]（提前收回 + 401 收回断言日志）
//
// 退出码：0=成功 | 3=执法拒绝（契约非法/egress 越界/票据无效/令牌断言失败——fail-closed）
func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var rc int
	switch os.Args[1] {
	case "validate":
		rc = cmdValidate(os.Args[2:])
	case "issue-ticket":
		rc = cmdIssueTicket(os.Args[2:])
	case "verify-ticket":
		rc = cmdVerifyTicket(os.Args[2:])
	case "emit-ledger":
		rc = cmdEmitLedger(os.Args[2:])
	case "gh-token":
		rc = cmdGhToken(os.Args[2:])
	case "gh-token-check":
		rc = cmdGhTokenCheck(os.Args[2:])
	case "gh-token-revoke":
		rc = cmdGhTokenRevoke(os.Args[2:])
	default:
		usage()
		rc = 2
	}
	os.Exit(rc)
}

func usage() {
	fmt.Fprintln(os.Stderr, "用法: selfcloud <validate|issue-ticket|verify-ticket|emit-ledger|gh-token|gh-token-check|gh-token-revoke> [flags]")
}

func fail(rc int, format string, a ...any) int {
	fmt.Fprintf(os.Stderr, "FAIL "+format+"\n", a...)
	return rc
}

func cmdValidate(args []string) int {
	fs := flag.NewFlagSet("validate", flag.ExitOnError)
	contractPath := fs.String("contract", "", "Job Contract JSON 文件")
	allowlist := fs.String("allowlist", "", "环境 egress 白名单（逗号分隔主机）")
	fs.Parse(args)
	if *contractPath == "" {
		return fail(2, "--contract 必填")
	}
	c, err := LoadContract(*contractPath)
	if err != nil {
		return fail(3, "%v", err)
	}
	if err := c.Validate(); err != nil {
		return fail(3, "%v", err)
	}
	if *allowlist != "" {
		hosts := splitComma(*allowlist)
		if err := c.EgressWithin(hosts); err != nil {
			return fail(3, "%v", err)
		}
	}
	b, _ := json.Marshal(map[string]string{"verdict": "ok", "job_id": c.JobID})
	fmt.Println(string(b))
	return 0
}

func cmdIssueTicket(args []string) int {
	fs := flag.NewFlagSet("issue-ticket", flag.ExitOnError)
	contractPath := fs.String("contract", "", "Job Contract JSON 文件")
	keyFile := fs.String("key-file", "", "HMAC 密钥文件（内网 Vault 注入）")
	ts := fs.String("ts", "", "签发时刻 RFC3339（测试注入；缺省=当前）")
	fs.Parse(args)
	if *contractPath == "" || *keyFile == "" {
		return fail(2, "--contract 与 --key-file 必填")
	}
	c, err := LoadContract(*contractPath)
	if err != nil {
		return fail(3, "%v", err)
	}
	key, err := os.ReadFile(*keyFile)
	if err != nil {
		return fail(2, "读密钥失败: %v", err)
	}
	now := time.Now().UTC()
	if *ts != "" {
		t, err := time.Parse(time.RFC3339, *ts)
		if err != nil {
			return fail(2, "--ts 非法 RFC3339: %v", err)
		}
		now = t.UTC()
	}
	t, token, err := IssueTicket(key, c, now)
	if err != nil {
		return fail(3, "%v", err)
	}
	b, _ := json.Marshal(map[string]any{"ticket": t, "token": token})
	fmt.Println(string(b))
	return 0
}

func cmdVerifyTicket(args []string) int {
	fs := flag.NewFlagSet("verify-ticket", flag.ExitOnError)
	keyFile := fs.String("key-file", "", "HMAC 密钥文件")
	token := fs.String("token", "", "待验票据 token")
	ts := fs.String("ts", "", "验证时刻 RFC3339（测试注入；缺省=当前）")
	fs.Parse(args)
	if *keyFile == "" || *token == "" {
		return fail(2, "--key-file 与 --token 必填")
	}
	key, err := os.ReadFile(*keyFile)
	if err != nil {
		return fail(2, "读密钥失败: %v", err)
	}
	now := time.Now().UTC()
	if *ts != "" {
		t, err := time.Parse(time.RFC3339, *ts)
		if err != nil {
			return fail(2, "--ts 非法 RFC3339: %v", err)
		}
		now = t.UTC()
	}
	t, err := VerifyTicket(key, *token, now)
	if err != nil {
		return fail(3, "%v", err)
	}
	b, _ := json.Marshal(map[string]any{"verdict": "valid", "ticket": t})
	fmt.Println(string(b))
	return 0
}

func cmdEmitLedger(args []string) int {
	fs := flag.NewFlagSet("emit-ledger", flag.ExitOnError)
	file := fs.String("file", "", "账本 jsonl（链式追加）")
	eventFile := fs.String("event-file", "", "事件 JSON 文件")
	fs.Parse(args)
	if *file == "" || *eventFile == "" {
		return fail(2, "--file 与 --event-file 必填")
	}
	b, err := os.ReadFile(*eventFile)
	if err != nil {
		return fail(2, "读事件失败: %v", err)
	}
	var ev Event
	if err := json.Unmarshal(b, &ev); err != nil {
		return fail(2, "事件 JSON 解析失败: %v", err)
	}
	if ev.Ts == "" || ev.Kind == "" || ev.Action == "" || ev.Verdict == "" ||
		ev.Subject.Card == "" || ev.Subject.Tenant == "" ||
		ev.Actor.Identity == "" || ev.Actor.Role == "" {
		return fail(3, "事件字段不齐（schema v1：ts/kind/action/verdict/subject/actor 必填——fail-closed）")
	}
	if _, err := time.Parse("2006-01-02T15:04:05Z", ev.Ts); err != nil {
		return fail(3, "ts 须为 2026-08-29T10:00:00Z 形态: %v", err)
	}
	if err := Append(*file, ev); err != nil {
		return fail(3, "%v", err)
	}
	fmt.Println("OK append 1 行（链式）")
	return 0
}

// cmdGhToken 代签单仓作用域短 TTL 安装令牌（W2-C2 / AC-6a）。
// 令牌值仅 stdout 交付（--expect-token=必须输出令牌给调用方消费的 drill 模式；
// 缺省只输出去敏感化的元信息）；账本事件（若有 --ledger）零令牌值。
func cmdGhToken(args []string) int {
	fs := flag.NewFlagSet("gh-token", flag.ExitOnError)
	appID := fs.String("app-id", "", "GitHub App ID（cloudbrid-agent）")
	keyFile := fs.String("key-file", "", "App 私钥 PEM 文件（Vault 注入路径）")
	org := fs.String("org", "Cloudbird-Software", "目标组织（installation 定位）")
	repo := fs.String("repo", "", "单仓作用域目标仓（如 .github）")
	card := fs.String("card", "", "绑定卡 join key（台账 subject.card 口径）")
	ledger := fs.String("ledger", "", "schema v1 账本 jsonl（token.grant 事件落账）")
	apiBase := fs.String("api", "https://api.github.com", "GitHub API base（drill 可指向桩）")
	expectToken := fs.Bool("expect-token", false, "stdout 输出令牌 JSON（调用方消费；缺省只输元信息）")
	fs.Parse(args)
	if *appID == "" || *keyFile == "" || *repo == "" || *card == "" {
		return fail(2, "--app-id/--key-file/--repo/--card 必填")
	}
	if !cardRe.MatchString(*card) {
		return fail(3, "--card 形态须为 owner/repo#n（join key）")
	}
	keyPEM, err := os.ReadFile(*keyFile)
	if err != nil {
		return fail(2, "读私钥失败: %v", err)
	}
	key, err := LoadPrivateKey(keyPEM)
	if err != nil {
		return fail(3, "%v", err)
	}
	now := time.Now().UTC()
	jwt, err := AppJWT(key, *appID, now)
	if err != nil {
		return fail(3, "%v", err)
	}
	installID, err := FindInstallation(*apiBase, jwt, *org)
	if err != nil {
		return fail(3, "%v", err)
	}
	mt, err := MintToken(*apiBase, jwt, installID, *repo, *card, now)
	if err != nil {
		return fail(3, "%v", err)
	}
	if *ledger != "" {
		if err := EmitTokenEvent(*ledger, "token.grant", *card, *repo, mt.ExpiresAt,
			map[string]string{"ttl_minutes": fmt.Sprintf("%d", int(mt.ExpiresAt.Sub(now).Minutes()))}, now); err != nil {
			return fail(3, "token.grant 入账失败: %v", err)
		}
		fmt.Fprintf(os.Stderr, "OK token.grant 入账（%s，payload 零令牌值）\n", *ledger)
	}
	if *expectToken {
		b, _ := json.Marshal(mt)
		fmt.Println(string(b)) // 令牌一次性交付（不落 stderr/账本）
	} else {
		fmt.Fprintf(os.Stderr, "OK 令牌已签发：作用域=单仓 %s，到期=%s，TTL=%dm（≤波次）\n",
			*repo, mt.ExpiresAt.Format(time.RFC3339), int(mt.ExpiresAt.Sub(now).Minutes()))
	}
	return 0
}

// cmdGhTokenCheck 令牌断言（AC-6b 收回断言锚点）：200=有效（exit 0）；
// 401=已收回/已过期（exit 3——断言日志即 FAIL 行）。
func cmdGhTokenCheck(args []string) int {
	fs := flag.NewFlagSet("gh-token-check", flag.ExitOnError)
	token := fs.String("token", "", "待断言令牌")
	repo := fs.String("repo", "", "作用域仓 owner/name（探活端点）")
	apiBase := fs.String("api", "https://api.github.com", "GitHub API base")
	fs.Parse(args)
	if *token == "" || *repo == "" {
		return fail(2, "--token 与 --repo 必填")
	}
	st, err := ProbeToken(*apiBase, *token, *repo)
	if err != nil {
		return fail(3, "%v", err)
	}
	if st == 200 {
		fmt.Fprintln(os.Stderr, "OK 令牌有效（HTTP 200）")
		return 0
	}
	return fail(3, "令牌已收回/已过期（HTTP %d——收回断言成立，fail-closed）", st)
}

// cmdGhTokenRevoke 收回 + 断言 + token.revoke 入账。
// 缺省=提前收回（DELETE /installation/token → 204）；--expired-only=到期收回
// 断言模式（TTL 已到，令牌应已自动失效——跳过 DELETE，探活必 401）。
func cmdGhTokenRevoke(args []string) int {
	fs := flag.NewFlagSet("gh-token-revoke", flag.ExitOnError)
	token := fs.String("token", "", "待收回令牌")
	card := fs.String("card", "", "绑定卡 join key")
	repo := fs.String("repo", "", "作用域仓 owner/name")
	ledger := fs.String("ledger", "", "schema v1 账本 jsonl（token.revoke 事件落账）")
	apiBase := fs.String("api", "https://api.github.com", "GitHub API base")
	expiredOnly := fs.Bool("expired-only", false, "到期收回断言模式（跳过 DELETE，断言 401）")
	fs.Parse(args)
	if *token == "" || *card == "" || *repo == "" {
		return fail(2, "--token/--card/--repo 必填")
	}
	reason := "revoked"
	if !*expiredOnly {
		if err := RevokeToken(*apiBase, *token); err != nil {
			return fail(3, "%v", err)
		}
		fmt.Fprintln(os.Stderr, "OK DELETE /installation/token → 204（提前收回）")
	} else {
		reason = "ttl-expired"
	}
	// 收回断言：探活必 401（机械锚点——沙箱/LLM 不判定，HTTP 状态码判定）。
	// GitHub 侧 DELETE→失效存在秒级传播延迟（run 33250323872 实测：204 后 ~0.3s
	// 仍 200）——退避轮询至 401；超时仍非 401=红（fail-closed，无默认绿）。
	const (
		retryWait   = 3 * time.Second
		retryBudget = 90 * time.Second
	)
	deadline := time.Now().Add(retryBudget)
	st, err := ProbeToken(*apiBase, *token, *repo)
	for err == nil && st != 401 && time.Now().Before(deadline) {
		time.Sleep(retryWait)
		st, err = ProbeToken(*apiBase, *token, *repo)
	}
	if err != nil {
		return fail(3, "收回后探活失败: %v", err)
	}
	if st != 401 {
		return fail(3, "收回断言失败：探活 HTTP %d（预期 401——%v 内令牌未死=红）", st, retryBudget)
	}
	fmt.Fprintf(os.Stderr, "OK 收回断言：探活 HTTP 401（令牌已失效，reason=%s——AC-6b 断言日志）\n", reason)
	if *ledger != "" {
		if err := EmitTokenEvent(*ledger, "token.revoke", *card, *repo, time.Time{},
			map[string]string{"reason": reason}, time.Now().UTC()); err != nil {
			return fail(3, "token.revoke 入账失败: %v", err)
		}
		fmt.Fprintf(os.Stderr, "OK token.revoke 入账（%s）\n", *ledger)
	}
	return 0
}

func splitComma(s string) []string {
	var out []string
	cur := ""
	for _, r := range s {
		if r == ',' {
			if cur != "" {
				out = append(out, cur)
			}
			cur = ""
		} else {
			cur += string(r)
		}
	}
	if cur != "" {
		out = append(out, cur)
	}
	return out
}
