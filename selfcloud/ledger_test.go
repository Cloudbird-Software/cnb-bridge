package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func goldenEvent() Event {
	return Event{
		Ts: "2026-08-29T10:00:00Z", Kind: "approval", Action: "ticket.grant", Verdict: "pass",
		Subject: EventSubject{Card: "Cloudbird-Software/.github#412", Tenant: "cloudbird"},
		Actor:   EventActor{Identity: "selfcloud-scheduler", Role: "bot"},
		Payload: `{"job_id": "job-001"}`,
	}
}

// 跨语言金向量：Go 产出的首行须与 Python evidence_shadow.py 的 canonical JSON +
// sha256 链逐字节一致（Python 侧预计算，锚定跨语言哈希一致性——AC-5b 统一账本兼容）。
func TestAppendGoldenVector(t *testing.T) {
	f := filepath.Join(t.TempDir(), "ledger.jsonl")
	if err := Append(f, goldenEvent()); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(f)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"action":"ticket.grant","actor":{"identity":"selfcloud-scheduler","role":"bot"},"hash":"fd4d361b3d08e37bf978088445b3142a5fe8ecc8cd8996ed501d5daca38305ae","kind":"approval","payload":"{\"job_id\": \"job-001\"}","prev_hash":null,"seq":1,"subject":{"card":"Cloudbird-Software/.github#412","tenant":"cloudbird"},"ts":"2026-08-29T10:00:00Z","verdict":"pass"}`
	if got := strings.TrimRight(string(b), "\n"); got != want {
		t.Fatalf("金向量不符（跨语言 canonical JSON 漂移）:\n got: %s\nwant: %s", got, want)
	}
}

// 链式追加：seq 连续 + prev_hash 指向前行 hash。
func TestAppendChain(t *testing.T) {
	f := filepath.Join(t.TempDir(), "ledger.jsonl")
	ev1 := goldenEvent()
	ev2 := goldenEvent()
	ev2.Action = "ticket.revoke"
	ev2.Ts = "2026-08-29T11:00:00Z"
	if err := Append(f, ev1); err != nil {
		t.Fatal(err)
	}
	if err := Append(f, ev2); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(f)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimRight(string(b), "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("应 2 行，得到 %d", len(lines))
	}
	var r1, r2 map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &r1); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(lines[1]), &r2); err != nil {
		t.Fatal(err)
	}
	if r2["seq"].(float64) != 2 {
		t.Errorf("seq 不连续: %v", r2["seq"])
	}
	if r2["prev_hash"] != r1["hash"] {
		t.Errorf("prev_hash 断链: %v != %v", r2["prev_hash"], r1["hash"])
	}
}

// 只增不改：追加后前行字节不变。
func TestAppendOnly(t *testing.T) {
	f := filepath.Join(t.TempDir(), "ledger.jsonl")
	if err := Append(f, goldenEvent()); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(f)
	ev2 := goldenEvent()
	ev2.Action = "ticket.revoke"
	ev2.Ts = "2026-08-29T11:00:00Z"
	if err := Append(f, ev2); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(f)
	if !strings.HasPrefix(string(after), string(before)) {
		t.Fatal("追加改写了前行（违反 append-only——INV-03）")
	}
}

// 末行畸形（账本已损坏）→ 拒追加（fail-closed）。
func TestAppendRejectsCorruptTail(t *testing.T) {
	f := filepath.Join(t.TempDir(), "ledger.jsonl")
	if err := os.WriteFile(f, []byte("not-json\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := Append(f, goldenEvent()); err == nil {
		t.Fatal("畸形末行未拒追加")
	}
}
