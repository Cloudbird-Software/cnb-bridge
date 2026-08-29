package main

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// githubStub 本地 GitHub API 桩（协议级测试：JWT→installation→scoped token→
// probe→revoke→401 断言——全链路机械可测，不碰真实 GitHub）。
type githubStub struct {
	srv        *httptest.Server
	key        *rsa.PrivateKey
	validToken string
	revoked    bool
	mintBody   []byte
}

func newGithubStub(t *testing.T) *githubStub {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	st := &githubStub{key: key}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /app/installations", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `[{"id":99999,"account":{"login":"Cloudbird-Software"}}]`)
	})
	mux.HandleFunc("POST /app/installations/99999/access_tokens", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Repositories []string `json:"repositories"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		if len(body.Repositories) != 1 || body.Repositories[0] != "cnb-bridge" {
			w.WriteHeader(422)
			fmt.Fprint(w, `{"message":"repositories scope required"}`)
			return
		}
		st.validToken = "ghs_stub_" + fmt.Sprintf("%x", time.Now().UnixNano())
		st.mintBody, _ = json.Marshal(map[string]any{
			"token":      st.validToken,
			"expires_at": time.Now().UTC().Add(time.Hour).Format(time.RFC3339),
		})
		w.WriteHeader(201)
		w.Write(st.mintBody)
	})
	mux.HandleFunc("GET /repos/Cloudbird-Software/cnb-bridge", func(w http.ResponseWriter, r *http.Request) {
		tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if tok == st.validToken && !st.revoked {
			fmt.Fprint(w, `{"full_name":"Cloudbird-Software/cnb-bridge"}`)
			return
		}
		w.WriteHeader(401)
		fmt.Fprint(w, `{"message":"Bad credentials"}`)
	})
	mux.HandleFunc("DELETE /installation/token", func(w http.ResponseWriter, r *http.Request) {
		st.revoked = true
		w.WriteHeader(204)
	})
	st.srv = httptest.NewServer(mux)
	t.Cleanup(st.srv.Close)
	return st
}

func (st *githubStub) keyFile(t *testing.T) string {
	t.Helper()
	b := x509.MarshalPKCS1PrivateKey(st.key)
	p := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: b})
	f := filepath.Join(t.TempDir(), "key.pem")
	if err := os.WriteFile(f, p, 0o600); err != nil {
		t.Fatal(err)
	}
	return f
}

func TestAppJWTSignVerify(t *testing.T) {
	st := newGithubStub(t)
	jwt, err := AppJWT(st.key, "12345", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(jwt, ".") != 2 {
		t.Fatalf("JWT 形态非法: %s", jwt)
	}
}

func TestLoadPrivateKeyPKCS8(t *testing.T) {
	st := newGithubStub(t)
	b, _ := x509.MarshalPKCS8PrivateKey(st.key)
	p := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: b})
	if _, err := LoadPrivateKey(p); err != nil {
		t.Fatalf("PKCS8 解析失败: %v", err)
	}
	if _, err := LoadPrivateKey([]byte("not-pem")); err == nil {
		t.Fatal("非 PEM 未拒")
	}
}

// 全链路：代签（单仓作用域）→ 探活 200 → 收回 → 探活 401（AC-6b 断言）。
func TestMintProbeRevoke(t *testing.T) {
	st := newGithubStub(t)
	now := time.Now().UTC()
	jwt, err := AppJWT(st.key, "12345", now)
	if err != nil {
		t.Fatal(err)
	}
	inst, err := FindInstallation(st.srv.URL, jwt, "cloudbird-software") // 大小写不敏感
	if err != nil {
		t.Fatal(err)
	}
	if inst != 99999 {
		t.Fatalf("installation id 不符: %d", inst)
	}
	mt, err := MintToken(st.srv.URL, jwt, inst, "cnb-bridge", "Cloudbird-Software/.github#413", now)
	if err != nil {
		t.Fatal(err)
	}
	if mt.ExpiresAt.Sub(now) > TokenTTLWaveCap {
		t.Fatal("TTL 超波次上限未被拒")
	}
	if code, _ := ProbeToken(st.srv.URL, mt.Token, "Cloudbird-Software/cnb-bridge"); code != 200 {
		t.Fatalf("有效令牌探活非 200: %d", code)
	}
	if err := RevokeToken(st.srv.URL, mt.Token); err != nil {
		t.Fatal(err)
	}
	if code, _ := ProbeToken(st.srv.URL, mt.Token, "Cloudbird-Software/cnb-bridge"); code != 401 {
		t.Fatalf("收回后探活非 401（收回断言失败）: %d", code)
	}
}

// TTL 超波次上限（>240min）→ 拒签（fail-closed 防御断言）。
func TestMintRejectsTTLOverWaveCap(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	// 自建桩：到期=now+300min（>240 波次上限）
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/access_tokens") {
			w.WriteHeader(201)
			fmt.Fprintf(w, `{"token":"ghs_x","expires_at":"%s"}`,
				time.Now().UTC().Add(300*time.Minute).Format(time.RFC3339))
			return
		}
		if strings.Contains(r.URL.Path, "/app/installations") {
			fmt.Fprint(w, `[{"id":1,"account":{"login":"o"}}]`)
			return
		}
	}))
	defer srv.Close()
	jwt, _ := AppJWT(key, "1", time.Now())
	if _, err := MintToken(srv.URL, jwt, 1, "r", "o/r#1", time.Now()); err == nil {
		t.Fatal("TTL>240min 未被拒签")
	}
}

// 令牌事件入账：payload 零令牌值（INV-04）。
func TestEmitTokenEventNoSecret(t *testing.T) {
	f := filepath.Join(t.TempDir(), "tickets.jsonl")
	exp := time.Date(2026, 8, 29, 13, 0, 0, 0, time.UTC)
	if err := EmitTokenEvent(f, "token.grant", "Cloudbird-Software/.github#413", "cnb-bridge",
		exp, map[string]string{"ttl_minutes": "60"}, exp.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(f)
	line := string(b)
	if strings.Contains(line, "ghs_") {
		t.Fatal("账本行泄漏令牌值")
	}
	if !strings.Contains(line, `"action":"token.grant"`) || !strings.Contains(line, `"identity":"selfcloud-tokenagent"`) {
		t.Fatalf("事件形态不符: %s", line)
	}
	// Python 验链器可直接验（跨语言兼容复用 tickets 源）
	if err := Append(f, Event{Ts: "2026-08-29T12:00:00Z", Kind: "approval", Action: "token.revoke",
		Verdict: "pass", Subject: EventSubject{Card: "c", Tenant: "t"},
		Actor:   EventActor{Identity: "i", Role: "bot"}}); err != nil {
		t.Fatal(err)
	}
}
