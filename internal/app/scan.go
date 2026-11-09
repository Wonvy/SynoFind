package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"
)

var excluded = map[string]bool{"@eaDir": true, "#recycle": true, "@tmp": true, ".snapshot": true, "#snapshot": true}

func (a *App) StartScan() bool {
	a.mu.Lock()
	if a.status.Running || a.ctx.Err() != nil {
		a.mu.Unlock()
		return false
	}
	a.status.Running = true
	a.status.ScannedFiles = 0
	a.status.StartedAt = time.Now().UTC().Format(time.RFC3339)
	a.status.LastError = ""
	a.status.Errors = []string{}
	cfg := a.cfg
	a.wg.Add(1)
	a.mu.Unlock()
	go func() {
		defer a.wg.Done()
		started := time.Now()
		err := a.scan(a.ctx, cfg)
		a.mu.Lock()
		a.status.LastDurationMS = time.Since(started).Milliseconds()
		if err != nil {
			a.status.LastError = err.Error()
		} else {
			a.status.LastCompletedAt = time.Now().UTC().Format(time.RFC3339)
		}
		s := a.status
		s.Running = false
		a.mu.Unlock()
		if err == nil {
			a.db.ExecContext(a.ctx, "INSERT INTO meta(key,value) VALUES('last_completed_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", s.LastCompletedAt)
		}
		if raw, e := json.Marshal(s); e == nil {
			a.db.ExecContext(a.ctx, "INSERT INTO meta(key,value) VALUES('index_status',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
		}
		a.mu.Lock()
		a.status.Running = false
		a.mu.Unlock()
		log.Printf("scan completed: files=%d elapsed_ms=%d errors=%d success=%t", s.ScannedFiles, s.LastDurationMS, len(s.Errors), err == nil)
	}()
	return true
}

func (a *App) scan(ctx context.Context, cfg Config) error {
	if err := validateRoot(cfg.RootPath, a.dataDir); err != nil {
		return err
	}
	root, err := os.OpenRoot(cfg.RootPath)
	if err != nil {
		return err
	}
	defer root.Close()
	if _, err = a.db.ExecContext(ctx, "DELETE FROM scan_files"); err != nil {
		return err
	}
	dirs := []string{"."}
	protected := []string{}
	batch := make([][]any, 0, 500)
	count := 0
	issue := func(p string, e error) {
		protected = append(protected, p)
		a.mu.Lock()
		if len(a.status.Errors) < 50 {
			a.status.Errors = append(a.status.Errors, fmt.Sprintf("%s: %v", p, e))
		}
		a.mu.Unlock()
	}
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		tx, e := a.db.BeginTx(ctx, nil)
		if e != nil {
			return e
		}
		defer tx.Rollback()
		stmt, e := tx.PrepareContext(ctx, "INSERT OR REPLACE INTO scan_files(id,path,name,path_fold,size,mtime,extension) VALUES(?,?,?,?,?,?,?)")
		if e != nil {
			return e
		}
		defer stmt.Close()
		for _, row := range batch {
			if _, e = stmt.ExecContext(ctx, row...); e != nil {
				return e
			}
		}
		if e = tx.Commit(); e != nil {
			return e
		}
		batch = batch[:0]
		a.mu.Lock()
		a.status.ScannedFiles = count
		a.mu.Unlock()
		return nil
	}
	for len(dirs) > 0 {
		if err = ctx.Err(); err != nil {
			return err
		}
		dir := dirs[len(dirs)-1]
		dirs = dirs[:len(dirs)-1]
		// Recheck each directory via the root handle; no symlink traversal.
		info, e := root.Lstat(filepath.FromSlash(dir))
		if e != nil {
			issue(dir, e)
			continue
		}
		if info.Mode()&os.ModeSymlink != 0 {
			continue
		}
		f, e := root.Open(filepath.FromSlash(dir))
		if e != nil {
			issue(dir, e)
			continue
		}
		for {
			entries, readErr := f.ReadDir(256)
			for _, entry := range entries {
				if ctx.Err() != nil {
					f.Close()
					return ctx.Err()
				}
				name := entry.Name()
				rel := path.Join(dir, name)
				if excluded[name] || entry.Type()&os.ModeSymlink != 0 {
					continue
				}
				if entry.IsDir() {
					dirs = append(dirs, rel)
					continue
				}
				st, e := root.Lstat(filepath.FromSlash(rel))
				if e != nil {
					issue(rel, e)
					continue
				}
				if !st.Mode().IsRegular() {
					continue
				}
				ext := strings.TrimPrefix(strings.ToLower(filepath.Ext(name)), ".")
				batch = append(batch, []any{fileID(cfg.RootPath, rel), rel, name, strings.ToLower(rel), st.Size(), st.ModTime().UnixNano(), ext})
				count++
				if len(batch) >= 500 {
					if e = flush(); e != nil {
						f.Close()
						return e
					}
				}
			}
			if readErr != nil {
				if !errors.Is(readErr, io.EOF) {
					if dir == "." {
						f.Close()
						return fmt.Errorf("无法完整读取资料根目录：%w", readErr)
					}
					issue(dir, readErr)
				}
				break
			}
		}
		f.Close()
	}
	if err = flush(); err != nil {
		return err
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	// Publish a complete snapshot atomically. Failed subtrees retain their prior rows.
	tx, err := a.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	_, err = tx.ExecContext(ctx, `INSERT INTO files(id,path,name,path_fold,size,mtime,extension) SELECT id,path,name,path_fold,size,mtime,extension FROM scan_files WHERE true ON CONFLICT(id) DO UPDATE SET path=excluded.path,name=excluded.name,path_fold=excluded.path_fold,size=excluded.size,mtime=excluded.mtime,extension=excluded.extension WHERE files.path!=excluded.path OR files.size!=excluded.size OR files.mtime!=excluded.mtime`)
	if err != nil {
		return err
	}
	// A temp table avoids unbounded SQL parameters when many folders are unreadable.
	if _, err = tx.ExecContext(ctx, "CREATE TEMP TABLE IF NOT EXISTS protected_paths(path TEXT PRIMARY KEY); DELETE FROM protected_paths"); err != nil {
		return err
	}
	for _, p := range protected {
		if p == "." {
			return errors.New("根目录不可读，保留上次索引")
		}
		if _, err = tx.ExecContext(ctx, "INSERT OR IGNORE INTO protected_paths VALUES(?)", p); err != nil {
			return err
		}
	}
	_, err = tx.ExecContext(ctx, `DELETE FROM files WHERE NOT EXISTS(SELECT 1 FROM scan_files s WHERE s.id=files.id) AND NOT EXISTS(SELECT 1 FROM protected_paths p WHERE files.path=p.path OR substr(files.path,1,length(p.path)+1)=p.path||'/')`)
	if err != nil {
		return err
	}
	var total int
	if err = tx.QueryRowContext(ctx, "SELECT count(*) FROM files").Scan(&total); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "DELETE FROM scan_files"); err != nil {
		return err
	}
	if err = tx.Commit(); err != nil {
		return err
	}
	a.mu.Lock()
	a.status.TotalFiles = total
	a.status.ScannedFiles = count
	if len(protected) > 0 {
		a.status.LastError = "部分目录无法读取，已保留这些目录的旧索引"
	}
	a.mu.Unlock()
	return nil
}

// Open files through an OS directory handle so even concurrent symlink replacement
// cannot escape the configured root. Explicit symlinks are not offered by SynoFind.
func (a *App) openFile(id string) (File, *os.File, error) {
	f, err := a.lookup(id)
	if err != nil {
		return f, nil, err
	}
	cfg := a.Config()
	if fileID(cfg.RootPath, f.Path) != f.ID {
		return f, nil, sql.ErrNoRows
	}
	root, err := os.OpenRoot(cfg.RootPath)
	if err != nil {
		return f, nil, err
	}
	defer root.Close()
	parts := strings.Split(f.Path, "/")
	current := ""
	for _, part := range parts {
		if part == ".." || part == "" || part == "." || excluded[part] {
			return f, nil, os.ErrPermission
		}
		current = path.Join(current, part)
		info, e := root.Lstat(filepath.FromSlash(current))
		if e != nil {
			return f, nil, e
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return f, nil, os.ErrPermission
		}
	}
	handle, err := root.Open(filepath.FromSlash(f.Path))
	if err != nil {
		return f, nil, err
	}
	st, err := handle.Stat()
	if err != nil {
		handle.Close()
		return f, nil, err
	}
	if !st.Mode().IsRegular() {
		handle.Close()
		return f, nil, os.ErrPermission
	}
	f.Size = st.Size()
	f.ModifiedAt = st.ModTime().UTC().Format(time.RFC3339Nano)
	return f, handle, nil
}
