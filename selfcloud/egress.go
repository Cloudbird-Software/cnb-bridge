package main

import (
	"context"
	"fmt"
	"net"
	"time"
)

// EgressPolicy worker 出向白名单（AC-5a：worker 不直连公网）。
// 真源=env-defs environments/*.yaml 的 network.egress_allowlist；v0 经 CLI 注入。
type EgressPolicy struct {
	allowed map[string]bool
}

func NewEgressPolicy(hosts []string) *EgressPolicy {
	m := make(map[string]bool, len(hosts))
	for _, h := range hosts {
		m[h] = true
	}
	return &EgressPolicy{allowed: m}
}

// Allowed 精确主机匹配（白名单外=拒）。
func (p *EgressPolicy) Allowed(host string) bool {
	return p.allowed[host]
}

// GuardDial 执法拨号：目的主机不在白名单 → 拒绝连接（worker 侧唯一出向通道）。
func (p *EgressPolicy) GuardDial(ctx context.Context, network, addr string) (net.Conn, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("egress 拒绝：地址非法 %q: %w", addr, err)
	}
	if !p.Allowed(host) {
		return nil, fmt.Errorf("egress 拒绝：%q 不在白名单（AC-5a worker 不直连公网）", host)
	}
	d := net.Dialer{Timeout: 10 * time.Second}
	return d.DialContext(ctx, network, addr)
}
