// Package main —— selfcloud 调度器 v0（IR-0006 W2-C1 / 卡 #412 / ADR-0103 决策 4）
//
// 云内网执行面调度器：消费 Job Contract（jobcontract/contract.schema.yaml）、
// worker 短票据签发（HMAC + TTL≤波次）、egress allowlist 执法、无状态约束执法。
// 判定与验收仍锚 GitHub（INV-02：本程序=可删除执行层，删除后 gate/org-gate/
// conductor 语义不变——REMOVAL.md 断言）。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
)

// WaveMaxTTLMinutes 波次租约上限（对齐 arbiter capabilities.yaml defaults.ttl_minutes
// = 240 分钟；短票据 TTL 不得超过波次——BEH-05）。
const WaveMaxTTLMinutes = 240

var cardRe = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$`)

// JobContract 作业契约 v0（schema 见 ../jobcontract/contract.schema.yaml）。
type JobContract struct {
	SchemaVersion   int      `json:"schema_version"`
	JobID           string   `json:"job_id"`
	Tenant          string   `json:"tenant"`
	Card            string   `json:"card"` // 绑定卡 join key（台账 subject.card 口径）
	Wave            string   `json:"wave"`
	TTLMinutes      int      `json:"ttl_minutes"`
	Commands        []string `json:"commands"`
	Artifacts       []string `json:"artifacts"`
	EgressNeeds     []string `json:"egress_needs"` // 需出向的主机（须 ⊆ env allowlist）
	PersistentState bool     `json:"persistent_state"`
	StateVolumes    []string `json:"state_volumes"`
}

// LoadContract 从 JSON 文件加载 Job Contract。
func LoadContract(path string) (*JobContract, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读契约失败: %w", err)
	}
	var c JobContract
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("契约 JSON 解析失败: %w", err)
	}
	return &c, nil
}

// Validate 执法无状态约束 + 必填完整性（fail-closed：任一非法即拒）。
func (c *JobContract) Validate() error {
	if c.SchemaVersion != 1 {
		return fmt.Errorf("schema_version 须为 1（得到 %d）", c.SchemaVersion)
	}
	if c.JobID == "" {
		return fmt.Errorf("job_id 必填")
	}
	if c.Tenant == "" {
		return fmt.Errorf("tenant 必填（宪法 §14a 计量分离）")
	}
	if !cardRe.MatchString(c.Card) {
		return fmt.Errorf("card 必填且形如 owner/repo#issue（join key，得到 %q）", c.Card)
	}
	if len(c.Commands) == 0 {
		return fmt.Errorf("commands 不得为空")
	}
	if c.TTLMinutes <= 0 || c.TTLMinutes > WaveMaxTTLMinutes {
		return fmt.Errorf("ttl_minutes 越界（1..%d，得到 %d——短票据 TTL≤波次）", WaveMaxTTLMinutes, c.TTLMinutes)
	}
	// 无状态约束（AC-5a：持状态负载拒置）
	if c.PersistentState {
		return fmt.Errorf("持状态负载拒置（无状态约束：persistent_state 必须为 false）")
	}
	if len(c.StateVolumes) > 0 {
		return fmt.Errorf("持状态负载拒置（无状态约束：state_volumes 必须为空，得到 %v）", c.StateVolumes)
	}
	return nil
}

// EgressWithin 校验契约出向需求 ⊆ 环境白名单（env-defs environments/*.yaml 的
// network.egress_allowlist——v0 经 --allowlist 注入）。
func (c *JobContract) EgressWithin(allowlist []string) error {
	p := NewEgressPolicy(allowlist)
	for _, h := range c.EgressNeeds {
		if !p.Allowed(h) {
			return fmt.Errorf("egress 越界：%q 不在环境白名单（worker 不直连公网——AC-5a）", h)
		}
	}
	return nil
}
