package app

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"golang.org/x/crypto/bcrypt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type attempt struct {
	Count int
	Until time.Time
}

func randomSecret() string {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
func secretHash(s string) string { sum := sha256.Sum256([]byte(s)); return hex.EncodeToString(sum[:]) }
func sameOrigin(r *http.Request) bool {
	o := r.Header.Get("Origin")
	if o == "" {
		return r.Header.Get("Sec-Fetch-Site") != "cross-site"
	}
	u, err := url.Parse(o)
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return err == nil && u.Scheme == scheme && u.Host == r.Host && u.User == nil
}
func (a *App) authenticate(r *http.Request) (csrf string, bearer bool, ok bool) {
	if auth := r.Header.Get("Authorization"); auth != "" {
		if !strings.HasPrefix(auth, "Bearer ") || len(auth) > 256 {
			return "", true, false
		}
		token := strings.TrimPrefix(auth, "Bearer ")
		var id string
		err := a.db.QueryRow("SELECT id FROM tokens WHERE hash=?", secretHash(token)).Scan(&id)
		if err != nil {
			return "", true, false
		}
		a.db.Exec("UPDATE tokens SET last_used_at=? WHERE id=?", time.Now().UTC().Format(time.RFC3339), id)
		return "", true, true
	}
	cookie, err := r.Cookie("synofind_session")
	if err != nil || len(cookie.Value) != 64 {
		return "", false, false
	}
	err = a.db.QueryRow("SELECT csrf FROM sessions WHERE hash=? AND expires>?", secretHash(cookie.Value), time.Now().Unix()).Scan(&csrf)
	return csrf, false, err == nil
}
func (a *App) protected(personal bool, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		csrf, bearer, ok := a.authenticate(r)
		if !ok {
			apiError(w, 401, "unauthorized", "请先登录或提供有效 API 令牌")
			return
		}
		if bearer && personal {
			apiError(w, 403, "session_required", "此操作仅限网页个人账号")
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" && !bearer {
			if !sameOrigin(r) || r.Header.Get("X-CSRF-Token") != csrf {
				apiError(w, 403, "csrf", "请求校验失败，请刷新后重试")
				return
			}
		}
		h(w, r)
	}
}
func (a *App) login(w http.ResponseWriter, r *http.Request) {
	if !sameOrigin(r) {
		apiError(w, 403, "origin", "不允许跨站登录")
		return
	}
	var body struct {
		Password string `json:"password"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	ip, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		ip = r.RemoteAddr
	}
	now := time.Now()
	a.authMu.Lock()
	for k, v := range a.attempts {
		if now.After(v.Until) {
			delete(a.attempts, k)
		}
	}
	v := a.attempts[ip]
	if v != nil && v.Count >= 10 {
		a.authMu.Unlock()
		w.Header().Set("Retry-After", "900")
		apiError(w, 429, "rate_limited", "登录失败次数过多，请稍后重试")
		return
	}
	if v == nil {
		if len(a.attempts) >= 1024 {
			a.authMu.Unlock()
			apiError(w, 429, "rate_limited", "请稍后重试")
			return
		}
		v = &attempt{Until: now.Add(15 * time.Minute)}
		a.attempts[ip] = v
	}
	v.Count++
	a.authMu.Unlock()
	cfg := a.Config()
	if len(body.Password) > 72 || bcrypt.CompareHashAndPassword([]byte(cfg.PasswordHash), []byte(body.Password)) != nil {
		apiError(w, 401, "invalid_password", "密码不正确")
		return
	}
	a.authMu.Lock()
	delete(a.attempts, ip)
	a.authMu.Unlock()
	token, csrf := randomSecret(), randomSecret()
	expires := now.Add(24 * time.Hour)
	if _, err = a.db.Exec("INSERT INTO sessions(hash,csrf,expires) VALUES(?,?,?)", secretHash(token), csrf, expires.Unix()); err != nil {
		apiError(w, 500, "database", "无法创建登录会话")
		return
	}
	http.SetCookie(w, &http.Cookie{Name: "synofind_session", Value: token, Path: "/", HttpOnly: true, Secure: r.TLS != nil, SameSite: http.SameSiteStrictMode, MaxAge: 86400})
	writeJSON(w, 200, map[string]any{"authenticated": true, "csrf_token": csrf})
}
func (a *App) session(w http.ResponseWriter, r *http.Request) {
	csrf, bearer, ok := a.authenticate(r)
	if !ok || bearer {
		apiError(w, 401, "unauthorized", "请先登录")
		return
	}
	writeJSON(w, 200, map[string]any{"authenticated": true, "csrf_token": csrf})
}
func (a *App) logout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie("synofind_session"); err == nil {
		a.db.Exec("DELETE FROM sessions WHERE hash=?", secretHash(c.Value))
	}
	http.SetCookie(w, &http.Cookie{Name: "synofind_session", Value: "", Path: "/", MaxAge: -1, HttpOnly: true, Secure: r.TLS != nil, SameSite: http.SameSiteStrictMode})
	w.WriteHeader(204)
}

type Token struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	CreatedAt  string `json:"created_at"`
	LastUsedAt string `json:"last_used_at"`
}

func (a *App) tokens(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case "GET":
		rows, err := a.db.Query("SELECT id,name,created_at,last_used_at FROM tokens ORDER BY created_at DESC")
		if err != nil {
			apiError(w, 500, "database", "无法读取令牌")
			return
		}
		defer rows.Close()
		items := []Token{}
		for rows.Next() {
			var t Token
			if err = rows.Scan(&t.ID, &t.Name, &t.CreatedAt, &t.LastUsedAt); err != nil {
				apiError(w, 500, "database", "无法读取令牌")
				return
			}
			items = append(items, t)
		}
		if rows.Err() != nil {
			apiError(w, 500, "database", "无法读取令牌")
			return
		}
		writeJSON(w, 200, map[string]any{"items": items})
	case "POST":
		var body struct {
			Name string `json:"name"`
		}
		if !decodeJSON(w, r, &body) {
			return
		}
		body.Name = strings.TrimSpace(body.Name)
		if body.Name == "" || len([]rune(body.Name)) > 80 {
			apiError(w, 400, "invalid_name", "请输入1至80个字符的令牌名称")
			return
		}
		var count int
		if err := a.db.QueryRow("SELECT count(*) FROM tokens").Scan(&count); err != nil {
			apiError(w, 500, "database", "无法读取令牌数量")
			return
		}
		if count >= 50 {
			apiError(w, 400, "token_limit", "最多可创建50个令牌")
			return
		}
		id := randomSecret()[:24]
		token := "sf_" + randomSecret()
		created := time.Now().UTC().Format(time.RFC3339)
		if _, err := a.db.Exec("INSERT INTO tokens(id,name,hash,created_at) VALUES(?,?,?,?)", id, body.Name, secretHash(token), created); err != nil {
			apiError(w, 500, "database", "无法创建令牌")
			return
		}
		writeJSON(w, 201, map[string]string{"id": id, "name": body.Name, "token": token, "created_at": created})
	case "DELETE":
		if _, err := a.db.Exec("DELETE FROM tokens WHERE id=?", r.PathValue("id")); err != nil {
			apiError(w, 500, "database", "无法撤销令牌")
			return
		}
		w.WriteHeader(204)
	}
}
