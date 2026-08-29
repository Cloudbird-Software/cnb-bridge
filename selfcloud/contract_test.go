package main

import (
	"os"
	"path/filepath"
	"testing"
)

func writeTemp(t *testing.T, name, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

const validContract = `{
  "schema_version": 1,
  "job_id": "job-001",
  "tenant": "cloudbird",
  "card": "Cloudbird-Software/.github#412",
  "wave": "W2",
  "ttl_minutes": 60,
  "commands": ["make card-test CARD=412"],
  "artifacts": ["report.json"],
  "egress_needs": ["api.github.com", "vault.internal"],
  "persistent_state": false,
  "state_volumes": []
}`

func TestValidateOK(t *testing.T) {
	c, err := LoadContract(writeTemp(t, "c.json", validContract))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatalf("合法契约被拒: %v", err)
	}
}

// AC-5a：持状态负载拒置（无状态约束断言）。
func TestValidateRejectsPersistentState(t *testing.T) {
	for name, mutate := range map[string]func(*JobContract){
		"persistent_state=true":     func(c *JobContract) { c.PersistentState = true },
		"state_volumes 非空":          func(c *JobContract) { c.StateVolumes = []string{"/data"} },
	} {
		c, err := LoadContract(writeTemp(t, "c.json", validContract))
		if err != nil {
			t.Fatal(err)
		}
		mutate(c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: 持状态负载未被拒置", name)
		}
	}
}

func TestValidateRejectsBadFields(t *testing.T) {
	cases := map[string]func(*JobContract){
		"schema_version≠1":  func(c *JobContract) { c.SchemaVersion = 2 },
		"job_id 空":          func(c *JobContract) { c.JobID = "" },
		"tenant 空":          func(c *JobContract) { c.Tenant = "" },
		"card 形态非法":       func(c *JobContract) { c.Card = "no-slash" },
		"commands 空":        func(c *JobContract) { c.Commands = nil },
		"ttl=0":             func(c *JobContract) { c.TTLMinutes = 0 },
		"ttl>240（超波次）":    func(c *JobContract) { c.TTLMinutes = 241 },
	}
	for name, mutate := range cases {
		c, err := LoadContract(writeTemp(t, "c.json", validContract))
		if err != nil {
			t.Fatal(err)
		}
		mutate(c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: 非法字段未被拒", name)
		}
	}
}

func TestEgressWithin(t *testing.T) {
	c, err := LoadContract(writeTemp(t, "c.json", validContract))
	if err != nil {
		t.Fatal(err)
	}
	allow := []string{"api.github.com", "vault.internal"}
	if err := c.EgressWithin(allow); err != nil {
		t.Fatalf("⊆ 白名单被拒: %v", err)
	}
	if err := c.EgressWithin([]string{"api.github.com"}); err == nil {
		t.Error("egress 越界未被拒（vault.internal 不在子集白名单）")
	}
}
