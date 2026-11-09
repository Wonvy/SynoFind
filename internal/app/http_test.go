package app

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type testCredentials struct {
	cookie      *http.Cookie
	csrf, token string
}

func request(t testing.TB, h http.Handler, method, target string, body any, creds testCredentials) *httptest.ResponseRecorder {
	t.Helper()
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		reader = bytes.NewReader(b)
	}
	req := httptest.NewRequest(method, "http://nas.test"+target, reader)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if creds.cookie != nil {
		req.AddCookie(creds.cookie)
	}
	if creds.csrf != "" {
		req.Header.Set("X-CSRF-Token", creds.csrf)
	}
	if creds.token != "" {
		req.Header.Set("Authorization", "Bearer "+creds.token)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

func requireStatus(t testing.TB, response *httptest.ResponseRecorder, want int) {
	t.Helper()
	if response.Code != want {
		t.Fatalf("HTTP status %d, want %d: %s", response.Code, want, response.Body.String())
	}
}

func loginForTest(t testing.TB, h http.Handler) testCredentials {
	t.Helper()
	r := request(t, h, "POST", "/api/v1/auth/login", map[string]string{"password": testPassword}, testCredentials{})
	requireStatus(t, r, 200)
	var body struct {
		Authenticated bool   `json:"authenticated"`
		CSRF          string `json:"csrf_token"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	cookies := r.Result().Cookies()
	if !body.Authenticated || body.CSRF == "" || len(cookies) != 1 {
		t.Fatalf("invalid login response: %s", r.Body.String())
	}
	return testCredentials{cookie: cookies[0], csrf: body.CSRF}
}

func TestHTTPSessionCSRFAndLogout(t *testing.T) {
	a := newTestApp(t)
	h := a.Handler()
	requireStatus(t, request(t, h, "GET", "/api/v1/health", nil, testCredentials{}), 200)
	requireStatus(t, request(t, h, "GET", "/", nil, testCredentials{}), 404)
	requireStatus(t, request(t, h, "GET", "/api/v1/search", nil, testCredentials{}), 401)
	requireStatus(t, request(t, h, "POST", "/api/v1/auth/login", map[string]string{"password": "wrong-password"}, testCredentials{}), 401)
	creds := loginForTest(t, h)
	if !creds.cookie.HttpOnly || creds.cookie.SameSite != http.SameSiteStrictMode || creds.cookie.Path != "/" {
		t.Fatalf("unsafe session cookie: %+v", creds.cookie)
	}
	var stored string
	if err := a.db.QueryRow("SELECT hash FROM sessions").Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if stored == creds.cookie.Value {
		t.Fatal("raw session secret stored in database")
	}
	r := request(t, h, "GET", "/api/v1/auth/session", nil, creds)
	requireStatus(t, r, 200)
	if !strings.Contains(r.Body.String(), creds.csrf) {
		t.Fatal("session did not return matching CSRF token")
	}
	withoutCSRF := creds
	withoutCSRF.csrf = ""
	requireStatus(t, request(t, h, "POST", "/api/v1/index/scan", nil, withoutCSRF), 403)
	wrongCSRF := creds
	wrongCSRF.csrf = "wrong"
	requireStatus(t, request(t, h, "POST", "/api/v1/index/scan", nil, wrongCSRF), 403)
	req := httptest.NewRequest("POST", "http://nas.test/api/v1/index/scan", nil)
	req.AddCookie(creds.cookie)
	req.Header.Set("X-CSRF-Token", creds.csrf)
	req.Header.Set("Origin", "http://attacker.test")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	requireStatus(t, w, 403)
	req = httptest.NewRequest("POST", "http://nas.test/api/v1/auth/login", strings.NewReader(`{"password":"`+testPassword+`"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "http://attacker.test")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, req)
	requireStatus(t, w, 403)
	requireStatus(t, request(t, h, "POST", "/api/v1/auth/logout", nil, creds), 204)
	requireStatus(t, request(t, h, "GET", "/api/v1/auth/session", nil, creds), 401)
	requireStatus(t, request(t, h, "GET", "/api/v1/search", nil, creds), 401)
}

func TestHTTPAPITokenScopeAndRevocation(t *testing.T) {
	a := newTestApp(t)
	h := a.Handler()
	session := loginForTest(t, h)
	r := request(t, h, "POST", "/api/v1/tokens", map[string]string{"name": "automation"}, session)
	requireStatus(t, r, 201)
	var token struct{ ID, Token string }
	if err := json.Unmarshal(r.Body.Bytes(), &token); err != nil {
		t.Fatal(err)
	}
	if token.ID == "" || !strings.HasPrefix(token.Token, "sf_") {
		t.Fatalf("missing token: %s", r.Body.String())
	}
	bearer := testCredentials{token: token.Token}
	var stored string
	if err := a.db.QueryRow("SELECT hash FROM tokens WHERE id=?", token.ID).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if stored == token.Token || strings.Contains(stored, "sf_") {
		t.Fatal("raw API token stored in database")
	}
	for _, path := range []string{"/api/v1/search", "/api/v1/index/status"} {
		requireStatus(t, request(t, h, "GET", path, nil, bearer), 200)
	}
	for _, tc := range []struct{ method, path string }{
		{"GET", "/api/v1/settings"}, {"PUT", "/api/v1/settings"}, {"GET", "/api/v1/tokens"},
		{"POST", "/api/v1/tokens"}, {"DELETE", "/api/v1/tokens/" + token.ID}, {"POST", "/api/v1/auth/logout"},
	} {
		requireStatus(t, request(t, h, tc.method, tc.path, nil, bearer), 403)
	}
	requireStatus(t, request(t, h, "GET", "/api/v1/auth/session", nil, bearer), 401)
	r = request(t, h, "GET", "/api/v1/tokens", nil, session)
	requireStatus(t, r, 200)
	if strings.Contains(r.Body.String(), token.Token) || strings.Contains(r.Body.String(), `"hash"`) || strings.Contains(r.Body.String(), `"token"`) {
		t.Fatal("token list disclosed credentials")
	}
	requireStatus(t, request(t, h, "POST", "/api/v1/index/scan", nil, bearer), 202)
	requireStatus(t, request(t, h, "DELETE", "/api/v1/tokens/"+token.ID, nil, session), 204)
	requireStatus(t, request(t, h, "GET", "/api/v1/search", nil, bearer), 401)
}

func TestHTTPDownloadsRangesAndSafePreviews(t *testing.T) {
	a := newTestApp(t)
	root := a.Config().RootPath
	writeFixture(t, root, "数字.txt", "0123456789")
	writeFixture(t, root, "unsafe.html", "<script>alert(1)</script>")
	writeFixture(t, root, "large.txt", strings.Repeat("x", 2*1024*1024+1))
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	h := a.Handler()
	creds := loginForTest(t, h)
	id := queryAll(t, a, "数字").Items[0].ID
	path := "/api/v1/files/" + id
	requireStatus(t, request(t, h, "GET", path+"/content", nil, testCredentials{}), 401)
	r := request(t, h, "GET", path+"/content", nil, creds)
	requireStatus(t, r, 200)
	if r.Body.String() != "0123456789" || !strings.HasPrefix(r.Header().Get("Content-Disposition"), "attachment;") {
		t.Fatalf("invalid download: %s / %s", r.Body.String(), r.Header().Get("Content-Disposition"))
	}
	req := httptest.NewRequest("GET", "http://nas.test"+path+"/content", nil)
	req.AddCookie(creds.cookie)
	req.Header.Set("Range", "bytes=2-5")
	r = httptest.NewRecorder()
	h.ServeHTTP(r, req)
	requireStatus(t, r, 206)
	if r.Body.String() != "2345" || r.Header().Get("Content-Range") != "bytes 2-5/10" {
		t.Fatalf("invalid partial response: %+v %q", r.Header(), r.Body.String())
	}
	req.Header.Set("Range", "bytes=100-200")
	r = httptest.NewRecorder()
	h.ServeHTTP(r, req)
	requireStatus(t, r, 416)
	r = request(t, h, "HEAD", path+"/content", nil, creds)
	requireStatus(t, r, 200)
	if r.Body.Len() != 0 || r.Header().Get("Content-Length") != "10" {
		t.Fatal("HEAD returned contents or wrong length")
	}
	r = request(t, h, "GET", path+"/preview", nil, creds)
	requireStatus(t, r, 200)
	if r.Header().Get("Content-Type") != "text/plain; charset=utf-8" || r.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("unsafe preview headers: %+v", r.Header())
	}
	htmlID := queryAll(t, a, "unsafe").Items[0].ID
	requireStatus(t, request(t, h, "GET", "/api/v1/files/"+htmlID+"/preview", nil, creds), 415)
	largeID := queryAll(t, a, "large").Items[0].ID
	requireStatus(t, request(t, h, "GET", "/api/v1/files/"+largeID+"/preview", nil, creds), 413)
	writeFixture(t, root, "数字.txt", "new longer content")
	r = request(t, h, "GET", path+"/content", nil, creds)
	requireStatus(t, r, 200)
	if r.Body.String() != "new longer content" {
		t.Fatal("download used stale indexed content/size")
	}
	if err := os.Remove(filepath.Join(root, "数字.txt")); err != nil {
		t.Fatal(err)
	}
	requireStatus(t, request(t, h, "GET", path+"/content", nil, creds), 404)
	requireStatus(t, request(t, h, "GET", "/api/v1/files/not-an-id/content", nil, creds), 404)
}

func TestHTTPValidationAndRootSettings(t *testing.T) {
	a := newTestApp(t)
	h := a.Handler()
	creds := loginForTest(t, h)
	for _, q := range []string{"page=0", "limit=201", "page=99999999999999999999", "min_size=-1", "min_size=20&max_size=10", "modified_after=not-a-date", "sort=unknown", "order=sideways", "modified_after=2026-02-01T00:00:00Z&modified_before=2026-01-01T00:00:00Z"} {
		t.Run(q, func(t *testing.T) { requireStatus(t, request(t, h, "GET", "/api/v1/search?"+q, nil, creds), 400) })
	}
	r := request(t, h, "GET", "/api/v1/settings", nil, creds)
	requireStatus(t, r, 200)
	if strings.Contains(r.Body.String(), "password_hash") || strings.Contains(r.Body.String(), a.Config().PasswordHash) {
		t.Fatal("settings leaked password hash")
	}
	writeFixture(t, a.Config().RootPath, "old-root.txt", "old root")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	old := a.Config()
	body := map[string]any{"root_path": a.dataDir, "smb_root": old.SMBRoot, "scan_interval_minutes": 15}
	requireStatus(t, request(t, h, "PUT", "/api/v1/settings", body, creds), 400)
	if queryAll(t, a, "old-root").Total != 1 {
		t.Fatal("rejected settings erased index")
	}
	newRoot := filepath.Join(filepath.Dir(a.dataDir), "replacement")
	if err := os.Mkdir(newRoot, 0700); err != nil {
		t.Fatal(err)
	}
	body["root_path"] = newRoot
	body["port"] = 9999
	a.mu.Lock()
	a.status.Running = true
	a.mu.Unlock()
	r = request(t, h, "PUT", "/api/v1/settings", body, creds)
	a.mu.Lock()
	a.status.Running = false
	a.mu.Unlock()
	requireStatus(t, r, 409)
	r = request(t, h, "PUT", "/api/v1/settings", body, creds)
	requireStatus(t, r, 200)
	if a.Config().RootPath != newRoot || a.Config().Port != old.Port {
		t.Fatalf("settings did not preserve read-only port: %+v", a.Config())
	}
	if queryAll(t, a, "").Total != 0 {
		t.Fatal("old root index was retained after switching source")
	}
	persisted, err := LoadConfig(a.dataDir)
	if err != nil || persisted.RootPath != newRoot || persisted.PasswordHash != old.PasswordHash {
		t.Fatalf("settings did not persist safely: %+v %v", persisted, err)
	}
}

func TestHTTPExpiredSessionAndHTTPSCookie(t *testing.T) {
	a := newTestApp(t)
	h := a.Handler()
	creds := loginForTest(t, h)
	if _, err := a.db.Exec("UPDATE sessions SET expires=0"); err != nil {
		t.Fatal(err)
	}
	requireStatus(t, request(t, h, "GET", "/api/v1/search", nil, creds), 401)
	requireStatus(t, request(t, h, "GET", "/api/v1/auth/session", nil, creds), 401)
	req := httptest.NewRequest("POST", "https://nas.test/api/v1/auth/login", strings.NewReader(`{"password":"`+testPassword+`"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "https://nas.test")
	r := httptest.NewRecorder()
	h.ServeHTTP(r, req)
	requireStatus(t, r, 200)
	cookies := r.Result().Cookies()
	if len(cookies) != 1 || !cookies[0].Secure || !cookies[0].HttpOnly {
		t.Fatalf("HTTPS login issued unsafe cookie: %+v", cookies)
	}
}
