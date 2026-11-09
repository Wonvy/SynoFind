package app

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
)

func TestInitPersistsConfigWithoutPlaintextAndIsIdempotent(t *testing.T) {
	a := newTestApp(t)
	before, err := os.ReadFile(filepath.Join(a.dataDir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(before, []byte(testPassword)) {
		t.Fatal("configuration contains plaintext password")
	}
	if a.Config().RootPath == "" || a.Config().PasswordHash == "" {
		t.Fatal("missing persisted configuration")
	}
	if err := Init(a.dataDir, Config{}, "replacement-password"); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(filepath.Join(a.dataDir, "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("reinitialization overwrote existing credentials/configuration")
	}
}

func TestInitRejectsInvalidCredentialsAndSettings(t *testing.T) {
	for _, tc := range []struct {
		name, password string
		port           int
		relative       bool
	}{
		{"short password", "short", 8787, false},
		{"oversize password", string(bytes.Repeat([]byte("x"), 73)), 8787, false},
		{"privileged port", testPassword, 80, false},
		{"port outside range", testPassword, 65536, false},
		{"relative root", testPassword, 8787, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			base := t.TempDir()
			root := filepath.Join(base, "source")
			if tc.relative {
				root = "relative/source"
			}
			data := filepath.Join(base, "data")
			if err := Init(data, Config{RootPath: root, Port: tc.port}, tc.password); err == nil {
				t.Fatal("invalid initialization accepted")
			}
			if _, err := os.Stat(filepath.Join(data, "config.json")); !os.IsNotExist(err) {
				t.Fatalf("invalid initialization left config behind: %v", err)
			}
		})
	}
	if _, err := New(t.TempDir(), nil); err == nil {
		t.Fatal("New accepted an uninitialized installation")
	}
}

func TestRootValidationRejectsOverlappingDataAndNonDirectories(t *testing.T) {
	a := newTestApp(t)
	child := filepath.Join(a.dataDir, "child")
	if err := os.MkdirAll(child, 0700); err != nil {
		t.Fatal(err)
	}
	file := writeFixture(t, a.Config().RootPath, "plain.txt", "data")
	for _, path := range []string{a.dataDir, child, filepath.Dir(a.dataDir), file, filepath.Join(a.Config().RootPath, "missing")} {
		if err := validateRoot(path, a.dataDir); err == nil {
			t.Errorf("unsafe root accepted: %s", path)
		}
	}
	if err := validateRoot(a.Config().RootPath, a.dataDir); err != nil {
		t.Fatalf("valid sibling root rejected: %v", err)
	}
}
