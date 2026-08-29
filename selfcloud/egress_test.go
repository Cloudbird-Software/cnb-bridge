package main

import (
	"context"
	"strings"
	"testing"
)

// AC-5a：非 allowlist 目的连接被拒（egress allowlist 执法断言）。
func TestGuardDialRejectsNonAllowlist(t *testing.T) {
	p := NewEgressPolicy([]string{"api.github.com"})
	_, err := p.GuardDial(context.Background(), "tcp", "evil.example.com:443")
	if err == nil {
		t.Fatal("非白名单主机未被拒——worker 可直连公网=红")
	}
	if !strings.Contains(err.Error(), "egress 拒绝") {
		t.Errorf("拒绝错误形态不符: %v", err)
	}
}

func TestGuardDialRejectsBadAddr(t *testing.T) {
	p := NewEgressPolicy([]string{"api.github.com"})
	if _, err := p.GuardDial(context.Background(), "tcp", "no-port"); err == nil {
		t.Fatal("无端口地址未被拒")
	}
}
