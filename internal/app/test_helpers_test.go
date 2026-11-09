package app

import (
	"os"
	"path/filepath"
	"testing"
)

const testPassword = "test-only-password-2026"

func newTestApp(t testing.TB) *App {
	t.Helper()
	base := t.TempDir()
	root := filepath.Join(base, "source")
	data := filepath.Join(base, "data")
	if err := os.MkdirAll(root, 0700); err != nil {
		t.Fatal(err)
	}
	cfg := Config{RootPath: root, SMBRoot: `\\nas\Wonvy`, ScanIntervalMinutes: 15, Port: 8787}
	if err := Init(data, cfg, testPassword); err != nil {
		t.Fatal(err)
	}
	a, err := New(data, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := a.Close(); err != nil {
			t.Error(err)
		}
	})
	return a
}

func writeFixture(t testing.TB, root, relative, content string) string {
	t.Helper()
	name := filepath.Join(root, filepath.FromSlash(relative))
	if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(name, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return name
}

func queryAll(t testing.TB, a *App, query string) SearchResult {
	t.Helper()
	r, err := a.Search(SearchOptions{Query: query, Page: 1, Limit: 200})
	if err != nil {
		t.Fatal(err)
	}
	return r
}
