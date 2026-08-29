package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

// Ledger schema v1 链式台账发射器（AC-5b：短票据签发/收回事件进统一账本）。
//
// 记录形态与 .github 仓 governance/evidence_shadow.py 逐字节兼容（canonical JSON：
// 键排序 + 紧凑分隔符 + 不转义 HTML + UTF-8 原样；hash=sha256(去 hash 字段的
// canonical JSON)）——Go 侧产出的账本文件可直接被 evidence_shadow.py verify 验链
// （跨语言一致由 ledger_test.go 金向量锚定）。真实落盘：服务器持代签令牌推送
// tickets-ledger 分支（RUNBOOK §selfcloud）。

// Event 事件骨架（kind=approval；subject.card=绑定卡 join key）。
type Event struct {
	Ts      string       `json:"ts"` // 2026-08-29T10:00:00Z
	Kind    string       `json:"kind"`
	Action  string       `json:"action"` // ticket.grant / ticket.revoke
	Verdict string       `json:"verdict"`
	Subject EventSubject `json:"subject"`
	Actor   EventActor   `json:"actor"`
	Payload string       `json:"payload"` // JSON 字符串（schema v1 纪律）
}

type EventSubject struct {
	Card   string `json:"card"`
	Tenant string `json:"tenant"`
}

type EventActor struct {
	Identity string `json:"identity"`
	Role     string `json:"role"` // bot
}

// canonicalJSON 与 Python json.dumps(obj, sort_keys=True, ensure_ascii=False,
// separators=(",", ":")) 逐字节等价。
func canonicalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func contentHash(rec map[string]any) (string, error) {
	delete(rec, "hash")
	b, err := canonicalJSON(rec)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}

func readLines(path string) ([]string, error) {
	lines := []string{}
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return lines, nil
		}
		return nil, fmt.Errorf("读账本失败: %w", err)
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.TrimSpace(l) != "" {
			lines = append(lines, l)
		}
	}
	return lines, nil
}

// Append 读既有链、追加一条链式记录（seq 连续 + prev_hash 链 + hash 重算锚）。
// 末行畸形=账本已损坏 → 拒追加（fail-closed，同 evidence_shadow.py）。
func Append(path string, ev Event) error {
	lines, err := readLines(path)
	if err != nil {
		return err
	}
	var prevHash any = nil
	if n := len(lines); n > 0 {
		var last map[string]any
		if err := json.Unmarshal([]byte(lines[n-1]), &last); err != nil {
			return fmt.Errorf("末行畸形（账本已损坏，拒追加——fail-closed）: %w", err)
		}
		if h, ok := last["hash"].(string); ok {
			prevHash = h
		}
	}
	rec := map[string]any{
		"ts": ev.Ts, "kind": ev.Kind, "action": ev.Action, "verdict": ev.Verdict,
		"subject": map[string]any{"card": ev.Subject.Card, "tenant": ev.Subject.Tenant},
		"actor":   map[string]any{"identity": ev.Actor.Identity, "role": ev.Actor.Role},
		"payload": ev.Payload,
		"seq":     len(lines) + 1,
		"prev_hash": prevHash,
	}
	h, err := contentHash(rec)
	if err != nil {
		return err
	}
	rec["hash"] = h
	line, err := canonicalJSON(rec)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return err
	}
	return nil
}
