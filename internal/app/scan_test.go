package app

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func TestScanPublishesAddsChangesRenamesAndRemovals(t *testing.T) {
	a := newTestApp(t)
	root := a.Config().RootPath
	original := writeFixture(t, root, "docs/original.txt", "initial")
	writeFixture(t, root, "keep.txt", "unchanged")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	oldID := queryAll(t, a, "original").Items[0].ID
	if err := os.Rename(original, filepath.Join(root, "docs", "renamed.txt")); err != nil {
		t.Fatal(err)
	}
	writeFixture(t, root, "docs/renamed.txt", "replacement contents")
	if err := os.Remove(filepath.Join(root, "keep.txt")); err != nil {
		t.Fatal(err)
	}
	writeFixture(t, root, "new.txt", "new")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	if got := queryAll(t, a, "original").Total; got != 0 {
		t.Fatalf("removed name remains in search: %d", got)
	}
	if got := queryAll(t, a, "keep").Total; got != 0 {
		t.Fatalf("deleted file remains: %d", got)
	}
	r := queryAll(t, a, "renamed")
	if r.Total != 1 || r.Items[0].Size != int64(len("replacement contents")) || r.Items[0].ID == oldID {
		t.Fatalf("rename/update was not published: %+v", r)
	}
	if got := queryAll(t, a, "").Total; got != 2 {
		t.Fatalf("unexpected total: %d", got)
	}
	if a.Status().TotalFiles != 2 {
		t.Fatalf("incorrect status: %+v", a.Status())
	}
	contents, err := os.ReadFile(filepath.Join(root, "docs", "renamed.txt"))
	if err != nil || string(contents) != "replacement contents" {
		t.Fatalf("scanner changed source contents: %q %v", contents, err)
	}
}

func TestScanFailureAndCancellationRetainPublishedIndex(t *testing.T) {
	a := newTestApp(t)
	root := a.Config().RootPath
	writeFixture(t, root, "published.txt", "keep this result")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	t.Run("cancelled", func(t *testing.T) {
		writeFixture(t, root, "new.txt", "not published yet")
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if err := a.scan(ctx, a.Config()); !errors.Is(err, context.Canceled) {
			t.Fatalf("expected cancellation, got %v", err)
		}
		if got := queryAll(t, a, "").Total; got != 1 {
			t.Fatalf("cancelled scan changed published index: %d", got)
		}
	})
	t.Run("missing root", func(t *testing.T) {
		moved := root + "-unavailable"
		if err := os.Rename(root, moved); err != nil {
			t.Fatal(err)
		}
		defer func() {
			if err := os.Rename(moved, root); err != nil {
				t.Error(err)
			}
		}()
		if err := a.scan(context.Background(), a.Config()); err == nil {
			t.Fatal("missing root was accepted")
		}
		if got := queryAll(t, a, "published").Total; got != 1 {
			t.Fatal("missing root erased previous results")
		}
	})
	t.Run("database publication failure", func(t *testing.T) {
		if _, err := a.db.Exec(`CREATE TRIGGER test_reject_publish BEFORE INSERT ON files WHEN new.path='new.txt' BEGIN SELECT RAISE(ABORT,'simulated disk/database failure'); END`); err != nil {
			t.Fatal(err)
		}
		defer a.db.Exec("DROP TRIGGER test_reject_publish")
		if err := a.scan(context.Background(), a.Config()); err == nil {
			t.Fatal("simulated publication failure was ignored")
		}
		if got := queryAll(t, a, "").Total; got != 1 {
			t.Fatalf("failed publication changed previous index: %d", got)
		}
	})
}

func TestScanExcludesSystemDirectoriesAndSymbolicLinks(t *testing.T) {
	a := newTestApp(t)
	root := a.Config().RootPath
	writeFixture(t, root, "visible.txt", "visible")
	for _, dir := range []string{"@eaDir", "#recycle", "@tmp", ".snapshot", "#snapshot"} {
		writeFixture(t, root, dir+"/hidden.txt", "excluded")
	}
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	if r := queryAll(t, a, ""); r.Total != 1 || r.Items[0].Name != "visible.txt" {
		t.Fatalf("system directories indexed: %+v", r)
	}
	t.Run("symlink", func(t *testing.T) {
		outside := writeFixture(t, t.TempDir(), "secret.txt", "outside")
		if err := os.Symlink(outside, filepath.Join(root, "linked.txt")); err != nil {
			t.Skipf("host cannot create symbolic links: %v", err)
		}
		if err := a.scan(context.Background(), a.Config()); err != nil {
			t.Fatal(err)
		}
		if got := queryAll(t, a, "linked").Total; got != 0 {
			t.Fatal("symbolic link entered index")
		}
	})
}

func TestUnreadableSubtreeRetainsOldRows(t *testing.T) {
	if runtime.GOOS == "windows" || os.Geteuid() == 0 {
		t.Skip("requires Unix non-root file permissions")
	}
	a := newTestApp(t)
	root := a.Config().RootPath
	writeFixture(t, root, "protected/retained.txt", "keep")
	writeFixture(t, root, "deleted.txt", "remove")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(root, "deleted.txt")); err != nil {
		t.Fatal(err)
	}
	protected := filepath.Join(root, "protected")
	if err := os.Chmod(protected, 0); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(protected, 0700)
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	if r := queryAll(t, a, ""); r.Total != 1 || r.Items[0].Path != "protected/retained.txt" {
		t.Fatalf("unreadable subtree not protected: %+v", r)
	}
	if len(a.Status().Errors) == 0 {
		t.Fatal("incomplete scan was not reported")
	}
}

func TestOpenFileEnforcesRootAndRejectsStaleSymlinks(t *testing.T) {
	a := newTestApp(t)
	root := a.Config().RootPath
	writeFixture(t, root, "safe.txt", "0123456789")
	if err := a.scan(context.Background(), a.Config()); err != nil {
		t.Fatal(err)
	}
	id := queryAll(t, a, "safe").Items[0].ID
	_, handle, err := a.openFile(id)
	if err != nil {
		t.Fatal(err)
	}
	contents, err := io.ReadAll(handle)
	handle.Close()
	if err != nil || string(contents) != "0123456789" {
		t.Fatalf("valid file could not be read: %q %v", contents, err)
	}
	outside := writeFixture(t, filepath.Dir(root), "outside.txt", "private")
	for _, relative := range []string{"../outside.txt", filepath.ToSlash(outside), "#recycle/hidden.txt"} {
		badID := fileID(root, relative)
		if _, err := a.db.Exec("INSERT INTO files(id,path,name,path_fold,size,mtime,extension) VALUES(?,?,?,?,?,?,?)", badID, relative, "outside.txt", relative, 7, 0, "txt"); err != nil {
			t.Fatal(err)
		}
		if _, f, err := a.openFile(badID); err == nil {
			f.Close()
			t.Errorf("unsafe path opened: %s", relative)
		}
	}
	t.Run("file replaced with escaping symlink", func(t *testing.T) {
		probe := filepath.Join(root, "link-probe")
		if err := os.Symlink(outside, probe); err != nil {
			t.Skipf("host cannot create symbolic links: %v", err)
		}
		if err := os.Remove(probe); err != nil {
			t.Fatal(err)
		}
		if err := os.Remove(filepath.Join(root, "safe.txt")); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(outside, filepath.Join(root, "safe.txt")); err != nil {
			t.Fatal(err)
		}
		if _, f, err := a.openFile(id); err == nil {
			f.Close()
			t.Fatal("indexed file replacement escaped the allowed root")
		}
	})
}

func TestSingleScannerCancellationAndRestartMetadata(t *testing.T) {
	a := newTestApp(t)
	writeFixture(t, a.Config().RootPath, "persisted.txt", "persisted")
	if !a.StartScan() {
		t.Fatal("initial scan did not start")
	}
	a.wg.Wait()
	if a.Status().LastCompletedAt == "" || a.Status().TotalFiles != 1 {
		t.Fatalf("successful scan not recorded: %+v", a.Status())
	}
	reopened, err := New(a.dataDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.Status().TotalFiles != 1 || reopened.Status().LastCompletedAt != a.Status().LastCompletedAt {
		reopened.Close()
		t.Fatal("restart lost published index status")
	}
	if err := reopened.Close(); err != nil {
		t.Fatal(err)
	}
	// Occupy the single writer connection so the worker cannot finish before
	// the overlap check. Cancellation must still release the blocked worker.
	conn, err := a.db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if !a.StartScan() {
		t.Fatal("second scan did not start")
	}
	if a.StartScan() {
		t.Fatal("overlapping scan was accepted")
	}
	a.cancel()
	done := make(chan struct{})
	go func() { a.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		conn.Close()
		t.Fatal("cancelled scanner did not exit")
	}
	conn.Close()
	if a.StartScan() {
		t.Fatal("scanner accepted work after shutdown cancellation")
	}
	if a.Status().Running || a.Status().LastError == "" {
		t.Fatalf("cancelled scan status incorrect: %+v", a.Status())
	}
	if queryAll(t, a, "persisted").Total != 1 {
		t.Fatal("shutdown cancellation damaged the published index")
	}
}
