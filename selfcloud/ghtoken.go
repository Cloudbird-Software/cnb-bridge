package main

// ghtoken.go —— PM 凭证收敛：GitHub App 安装令牌服务器代签（IR-0006 W2-C2 / 卡 #413 / ADR-0044 机制上收）
//
// 目标态（AC-6a/INV-04）：PM 会话在云电脑上不持任何长期凭据；写仓令牌由内网
// 服务器用 cloudbrid-agent App 私钥（Vault 注入）代签——单仓作用域 + 短 TTL
// （GitHub 固定 1h ≤ 波次租约上限 240min）；个人 PAT 退出日常流程（仅应急
// 回退通道，见 .github 仓 docs/pm-credential-convergence.md）。
//
// 令牌值只经 stdout 一次性交付调用方，永不进账本/日志/git（账本只记
// 作用域+到期时刻）。收回语义（AC-6b）：DELETE /installation/token 提前收回；
// TTL 到期 GitHub 侧自动失效——两者断言统一经 ProbeToken（401=已收回）。
import (
	"bytes"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// TokenTTLWaveCap 令牌 TTL 波次上限断言值（对齐 WaveMaxTTLMinutes=240；
// GitHub 安装令牌固定 1h，天然满足）。
const TokenTTLWaveCap = time.Duration(WaveMaxTTLMinutes) * time.Minute

// MintedToken 代签结果（token 值仅 stdout 交付，不入账本）。
type MintedToken struct {
	Token     string    `json:"token"`
	ExpiresAt time.Time `json:"expires_at"`
	Repo      string    `json:"repo"` // 单仓作用域
}

// LoadPrivateKey 解析 App 私钥 PEM（PKCS1 或 PKCS8 均可——GitHub App 下载
// 私钥为 PKCS1，注册清单可能 PKCS8）。
func LoadPrivateKey(pemBytes []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, fmt.Errorf("私钥 PEM 解码失败（非 PEM 格式）")
	}
	if k, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return k, nil
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("私钥解析失败（PKCS1/PKCS8 均不支持）: %w", err)
	}
	rk, ok := k.(*rsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("私钥非 RSA（得到 %T）", k)
	}
	return rk, nil
}

// AppJWT 生成 App JWT（RS256，9 分钟有效——同 gh-app-token.sh 语义）。
func AppJWT(key *rsa.PrivateKey, appID string, now time.Time) (string, error) {
	header := b64JSON(map[string]string{"alg": "RS256", "typ": "JWT"})
	payload := b64JSON(map[string]any{
		"iat": now.Add(-60 * time.Second).Unix(),
		"exp": now.Add(480 * time.Second).Unix(),
		"iss": appID,
	})
	signingInput := header + "." + payload
	digest := sha256.Sum256([]byte(signingInput))
	sum, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		return "", fmt.Errorf("JWT 签名失败: %w", err)
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sum), nil
}

func b64JSON(v any) string {
	b, _ := json.Marshal(v)
	return base64.RawURLEncoding.EncodeToString(b)
}

func ghAPI(method, apiBase, path, auth string, body []byte) (int, []byte, error) {
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, apiBase+path, rdr)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, b, err
}

// FindInstallation 定位 org 的 installation id（App JWT 认证）。
func FindInstallation(apiBase, jwt, org string) (int64, error) {
	st, b, err := ghAPI("GET", apiBase, "/app/installations?per_page=100", "Bearer "+jwt, nil)
	if err != nil {
		return 0, fmt.Errorf("installation 清单查询失败: %w", err)
	}
	if st != 200 {
		return 0, fmt.Errorf("installation 清单查询 HTTP %d: %s", st, truncBody(b))
	}
	var installs []struct {
		ID      int64 `json:"id"`
		Account struct {
			Login string `json:"login"`
		} `json:"account"`
	}
	if err := json.Unmarshal(b, &installs); err != nil {
		return 0, fmt.Errorf("installation 清单解析失败: %w", err)
	}
	for _, ins := range installs {
		if strings.EqualFold(ins.Account.Login, org) {
			return ins.ID, nil
		}
	}
	return 0, fmt.Errorf("找不到 %s 的 installation（App 未安装或作用域不含该 org）", org)
}

// MintToken 用 App JWT 换单仓作用域安装令牌（repositories 限定——最小权限，
// 不提供全安装作用域模式，同 gh-app-token.sh 评审项）。fail-closed：TTL 超过
// 波次上限即拒（防御性断言，GitHub 固定 1h 时恒过）。
func MintToken(apiBase, jwt string, installID int64, repo, card string, now time.Time) (*MintedToken, error) {
	body, _ := json.Marshal(map[string][]string{"repositories": {repo}})
	st, b, err := ghAPI("POST", apiBase, fmt.Sprintf("/app/installations/%d/access_tokens", installID), "Bearer "+jwt, body)
	if err != nil {
		return nil, fmt.Errorf("换令牌请求失败: %w", err)
	}
	if st != 201 {
		return nil, fmt.Errorf("换令牌 HTTP %d: %s", st, truncBody(b))
	}
	var resp struct {
		Token     string    `json:"token"`
		ExpiresAt time.Time `json:"expires_at"`
	}
	if err := json.Unmarshal(b, &resp); err != nil || resp.Token == "" {
		return nil, fmt.Errorf("令牌响应解析失败（token 缺失）: %s", truncBody(b))
	}
	ttl := resp.ExpiresAt.Sub(now)
	if ttl <= 0 || ttl > TokenTTLWaveCap {
		return nil, fmt.Errorf("令牌 TTL 越界（%v，须 0<TTL≤%v——短 TTL≤波次）", ttl, TokenTTLWaveCap)
	}
	_ = card // 绑定卡仅入账本事件（见 EmitTokenEvent），令牌本体不含
	return &MintedToken{Token: resp.Token, ExpiresAt: resp.ExpiresAt, Repo: repo}, nil
}

// RevokeToken 提前收回（DELETE /installation/token；204=成功）。
func RevokeToken(apiBase, token string) error {
	st, b, err := ghAPI("DELETE", apiBase, "/installation/token", "Bearer "+token, nil)
	if err != nil {
		return fmt.Errorf("收回请求失败: %w", err)
	}
	if st != 204 {
		return fmt.Errorf("收回 HTTP %d（预期 204）: %s", st, truncBody(b))
	}
	return nil
}

// ProbeToken 令牌有效性断言（GET /repos/{owner}/{repo}：200=有效；401=已收回/
// 已过期——AC-6b 收回断言锚点）。返回 HTTP 状态码（非 200 不作 error——
// 401 正是断言目标，由调用方按状态码判定）。
func ProbeToken(apiBase, token, ownerRepo string) (int, error) {
	st, _, err := ghAPI("GET", apiBase, "/repos/"+ownerRepo, "Bearer "+token, nil)
	if err != nil {
		return 0, fmt.Errorf("探活请求失败: %w", err)
	}
	return st, nil
}

func truncBody(b []byte) string {
	s := string(b)
	if len(s) > 300 {
		s = s[:300] + "…"
	}
	return s
}

// EmitTokenEvent 令牌生命周期事件入账（AC-6b：JSONL 记录按 schema v1 进统一
// 账本——payload 只含作用域/到期/原因，零令牌值）。
func EmitTokenEvent(ledgerPath, action, card, repo string, expiresAt time.Time, extra map[string]string, ts time.Time) error {
	payload := map[string]string{"repo": repo}
	if !expiresAt.IsZero() {
		payload["expires_at"] = expiresAt.UTC().Format("2006-01-02T15:04:05Z")
	}
	for k, v := range extra {
		payload[k] = v
	}
	pb, _ := json.Marshal(payload)
	ev := Event{
		Ts: ts.UTC().Format("2006-01-02T15:04:05Z"), Kind: "approval", Action: action,
		Verdict: "pass",
		Subject: EventSubject{Card: card, Tenant: "cloudbird"},
		Actor:   EventActor{Identity: "selfcloud-tokenagent", Role: "bot"},
		Payload: string(pb),
	}
	return Append(ledgerPath, ev)
}
