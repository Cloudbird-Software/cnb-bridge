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
//
// 退出码：0=成功 | 3=执法拒绝（契约非法/egress 越界/票据无效——fail-closed）
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
	default:
		usage()
		rc = 2
	}
	os.Exit(rc)
}

func usage() {
	fmt.Fprintln(os.Stderr, "用法: selfcloud <validate|issue-ticket|verify-ticket|emit-ledger> [flags]")
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
