package main

import (
	"strings"
	"testing"
	"time"
)

func validContractForTicket() *JobContract {
	return &JobContract{
		SchemaVersion: 1, JobID: "job-001", Tenant: "cloudbird",
		Card: "Cloudbird-Software/.github#412", Wave: "W2", TTLMinutes: 30,
		Commands: []string{"make test"},
	}
}

func TestTicketIssueAndVerify(t *testing.T) {
	key := []byte("test-hmac-key")
	now := time.Date(2026, 8, 29, 10, 0, 0, 0, time.UTC)
	tk, token, err := IssueTicket(key, validContractForTicket(), now)
	if err != nil {
		t.Fatal(err)
	}
	if tk.ExpiresAt.Sub(tk.IssuedAt) != 30*time.Minute {
		t.Errorf("TTL 不等于契约 ttl_minutes")
	}
	got, err := VerifyTicket(key, token, now.Add(29*time.Minute))
	if err != nil {
		t.Fatalf("有效期内验签失败: %v", err)
	}
	if got.JobID != "job-001" || got.Card != "Cloudbird-Software/.github#412" {
		t.Errorf("票据字段回读不符: %+v", got)
	}
}

// BEH-05：到期即失效（TTL 收回）。
func TestTicketExpired(t *testing.T) {
	key := []byte("test-hmac-key")
	now := time.Date(2026, 8, 29, 10, 0, 0, 0, time.UTC)
	_, token, err := IssueTicket(key, validContractForTicket(), now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := VerifyTicket(key, token, now.Add(31*time.Minute)); err == nil {
		t.Fatal("过期票据未被拒")
	} else if !strings.Contains(err.Error(), "过期") {
		t.Errorf("过期错误形态不符: %v", err)
	}
}

func TestTicketBadSignature(t *testing.T) {
	key := []byte("test-hmac-key")
	now := time.Date(2026, 8, 29, 10, 0, 0, 0, time.UTC)
	_, token, err := IssueTicket(key, validContractForTicket(), now)
	if err != nil {
		t.Fatal(err)
	}
	// 换密钥验签 → 拒
	if _, err := VerifyTicket([]byte("other-key"), token, now); err == nil {
		t.Fatal("签名不符未被拒")
	}
	// 篡改 body → 拒
	dot := strings.LastIndex(token, ".")
	tampered := "X" + token[1:dot] + token[dot:]
	if _, err := VerifyTicket(key, tampered, now); err == nil {
		t.Fatal("篡改票据未被拒")
	}
}

// 非法契约拒签（fail-closed：调度器只给合法契约发凭证）。
func TestTicketRejectsInvalidContract(t *testing.T) {
	c := validContractForTicket()
	c.TTLMinutes = 999
	if _, _, err := IssueTicket([]byte("k"), c, time.Now()); err == nil {
		t.Fatal("非法契约未被拒签")
	}
}
