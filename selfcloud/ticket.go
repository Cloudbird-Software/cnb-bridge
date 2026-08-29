package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"
)

// Ticket worker 短票据（BEH-05：按作业契约签发，TTL≤波次；到期/收回即失效）。
// worker 零持久凭据（assets-register：cloud-desktop-pool secrets_location=none）——
// 票据即作业期唯一执行凭证，HMAC 签名密钥在内网域 Vault（本程序经 --key-file 注入）。
type Ticket struct {
	TicketID   string    `json:"ticket_id"`
	JobID      string    `json:"job_id"`
	Card       string    `json:"card"`
	Tenant     string    `json:"tenant"`
	Capability string    `json:"capability"` // v0 固定 worker-execute
	IssuedAt   time.Time `json:"issued_at"`
	ExpiresAt  time.Time `json:"expires_at"`
}

// IssueTicket 按契约签发短票据（TTL=契约 ttl_minutes，已 Validate 上限≤波次）。
// 返回票据与 token（base64(票据 JSON) + "." + HMAC-SHA256 签名）。
func IssueTicket(key []byte, c *JobContract, now time.Time) (*Ticket, string, error) {
	if err := c.Validate(); err != nil {
		return nil, "", fmt.Errorf("拒签：契约非法: %w", err)
	}
	t := &Ticket{
		TicketID:   fmt.Sprintf("tick-%s-%s", now.UTC().Format("20060102-150405"), c.JobID),
		JobID:      c.JobID, Card: c.Card, Tenant: c.Tenant,
		Capability: "worker-execute",
		IssuedAt:   now.UTC(),
		ExpiresAt:  now.UTC().Add(time.Duration(c.TTLMinutes) * time.Minute),
	}
	b, err := json.Marshal(t)
	if err != nil {
		return nil, "", err
	}
	body := base64.RawURLEncoding.EncodeToString(b)
	return t, body + "." + signTicket(key, body), nil
}

func signTicket(key []byte, body string) string {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(body))
	return hex.EncodeToString(m.Sum(nil))
}

// VerifyTicket 验签 + TTL 执法（到期=失效——AC-9d 同款"提权是瞬时能力"语义）。
func VerifyTicket(key []byte, token string, now time.Time) (*Ticket, error) {
	dot := -1
	for i := len(token) - 1; i >= 0; i-- {
		if token[i] == '.' {
			dot = i
			break
		}
	}
	if dot < 0 {
		return nil, fmt.Errorf("票据形态非法")
	}
	body, sig := token[:dot], token[dot+1:]
	if !hmac.Equal([]byte(sig), []byte(signTicket(key, body))) {
		return nil, fmt.Errorf("票据签名不符（拒）")
	}
	b, err := base64.RawURLEncoding.DecodeString(body)
	if err != nil {
		return nil, fmt.Errorf("票据体解码失败: %w", err)
	}
	var t Ticket
	if err := json.Unmarshal(b, &t); err != nil {
		return nil, fmt.Errorf("票据体解析失败: %w", err)
	}
	if now.UTC().After(t.ExpiresAt) {
		return nil, fmt.Errorf("票据已过期（TTL 收回，expired_at=%s）", t.ExpiresAt.Format(time.RFC3339))
	}
	return &t, nil
}
