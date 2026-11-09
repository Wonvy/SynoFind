package app

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"fmt"
	_ "modernc.org/sqlite"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type File struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Path        string `json:"path"`
	Size        int64  `json:"size"`
	ModifiedAt  string `json:"modified_at"`
	Extension   string `json:"extension"`
	PreviewType string `json:"preview_type"`
	SMBPath     string `json:"smb_path"`
}
type SearchOptions struct {
	Query, Ext, After, Before, Sort, Order string
	MinSize, MaxSize                       *int64
	Page, Limit                            int
}
type SearchResult struct {
	Items []File `json:"items"`
	Total int    `json:"total"`
	Page  int    `json:"page"`
	Limit int    `json:"limit"`
}

func openDB(dataDir string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", filepath.Join(dataDir, "index.db"))
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;
 CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 INSERT OR IGNORE INTO meta VALUES('schema_version','1');
 CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,name TEXT NOT NULL,path_fold TEXT NOT NULL,size INTEGER NOT NULL,mtime INTEGER NOT NULL,extension TEXT NOT NULL);
 CREATE INDEX IF NOT EXISTS files_ext ON files(extension);
 CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
 CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(path_fold,content='files',content_rowid='rowid',tokenize='trigram');
 CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN INSERT INTO files_fts(rowid,path_fold) VALUES(new.rowid,new.path_fold); END;
 CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN INSERT INTO files_fts(files_fts,rowid,path_fold) VALUES('delete',old.rowid,old.path_fold); END;
 CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF path_fold ON files WHEN old.path_fold!=new.path_fold BEGIN INSERT INTO files_fts(files_fts,rowid,path_fold) VALUES('delete',old.rowid,old.path_fold); INSERT INTO files_fts(rowid,path_fold) VALUES(new.rowid,new.path_fold); END;
 CREATE TABLE IF NOT EXISTS scan_files(id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,name TEXT NOT NULL,path_fold TEXT NOT NULL,size INTEGER NOT NULL,mtime INTEGER NOT NULL,extension TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,name TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,last_used_at TEXT NOT NULL DEFAULT '');
 CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
 `)
	if err != nil {
		db.Close()
		return nil, err
	}
	if err = os.Chmod(filepath.Join(dataDir, "index.db"), 0600); err != nil {
		db.Close()
		return nil, err
	}
	var version string
	if err = db.QueryRow("SELECT value FROM meta WHERE key='schema_version'").Scan(&version); err != nil || version != "1" {
		db.Close()
		return nil, fmt.Errorf("不支持的索引版本：%s", version)
	}
	return db, nil
}

func fileID(root, path string) string {
	sum := sha256.Sum256([]byte(filepath.Clean(root) + "\x00" + path))
	return hex.EncodeToString(sum[:16])
}
func previewType(ext string) string {
	switch strings.ToLower(ext) {
	case "png", "jpg", "jpeg", "gif", "webp", "bmp":
		return "image"
	case "pdf":
		return "pdf"
	case "txt", "md", "csv", "tsv", "log", "json", "yaml", "yml", "xml", "ini", "conf", "go", "py", "js", "ts", "css":
		return "text"
	}
	return ""
}
func decorate(f *File, mtime int64, cfg Config) {
	f.ModifiedAt = time.Unix(0, mtime).UTC().Format(time.RFC3339Nano)
	f.PreviewType = previewType(f.Extension)
	if cfg.SMBRoot != "" {
		f.SMBPath = strings.TrimRight(cfg.SMBRoot, "/\\") + "\\" + strings.ReplaceAll(f.Path, "/", "\\")
	}
}

func (a *App) Search(o SearchOptions) (SearchResult, error) {
	return a.search(context.Background(), o)
}

func (a *App) search(ctx context.Context, o SearchOptions) (SearchResult, error) {
	result := SearchResult{Items: []File{}, Page: o.Page, Limit: o.Limit}
	where := []string{"1=1"}
	args := []any{}
	terms := strings.Fields(strings.ToLower(o.Query))
	fts := []string{}
	for _, term := range terms {
		where = append(where, "instr(f.path_fold,?)>0")
		args = append(args, term)
		if len([]rune(term)) >= 3 {
			fts = append(fts, `"`+strings.ReplaceAll(term, `"`, `""`)+`"`)
		}
	}
	if len(fts) > 0 {
		where = append(where, "f.rowid IN (SELECT rowid FROM files_fts WHERE files_fts MATCH ?)")
		args = append(args, strings.Join(fts, " AND "))
	}
	if o.Ext != "" {
		parts := strings.Split(strings.ToLower(o.Ext), ",")
		marks := []string{}
		for _, p := range parts {
			p = strings.TrimPrefix(strings.TrimSpace(p), ".")
			if p != "" {
				marks = append(marks, "?")
				args = append(args, p)
			}
		}
		if len(marks) > 0 {
			where = append(where, "f.extension IN ("+strings.Join(marks, ",")+")")
		}
	}
	if o.MinSize != nil {
		where = append(where, "f.size>=?")
		args = append(args, *o.MinSize)
	}
	if o.MaxSize != nil {
		where = append(where, "f.size<=?")
		args = append(args, *o.MaxSize)
	}
	for _, filter := range []struct{ value, op string }{{o.After, ">="}, {o.Before, "<="}} {
		if filter.value != "" {
			t, err := time.Parse(time.RFC3339, filter.value)
			if err != nil {
				return result, err
			}
			where = append(where, "f.mtime"+filter.op+"?")
			args = append(args, t.UnixNano())
		}
	}
	clause := strings.Join(where, " AND ")
	// Read count and page from the same SQLite snapshot, even during scan publication.
	cfg := a.Config()
	tx, err := a.db.BeginTx(ctx, nil)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()
	if err = tx.QueryRowContext(ctx, "SELECT count(*) FROM files f WHERE "+clause, args...).Scan(&result.Total); err != nil {
		return result, err
	}
	col := "f.path_fold"
	if o.Sort == "modified" {
		col = "f.mtime"
	} else if o.Sort == "size" {
		col = "f.size"
	}
	order := "ASC"
	if o.Order == "desc" {
		order = "DESC"
	}
	query := "SELECT f.id,f.name,f.path,f.size,f.mtime,f.extension FROM files f WHERE " + clause + " ORDER BY " + col + " " + order + ",f.id ASC LIMIT ? OFFSET ?"
	args = append(args, o.Limit, (o.Page-1)*o.Limit)
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var f File
		var m int64
		if err = rows.Scan(&f.ID, &f.Name, &f.Path, &f.Size, &m, &f.Extension); err != nil {
			rows.Close()
			return result, err
		}
		decorate(&f, m, cfg)
		result.Items = append(result.Items, f)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return result, err
	}
	return result, tx.Commit()
}

func (a *App) lookup(id string) (File, error) {
	var f File
	var m int64
	err := a.db.QueryRow("SELECT id,name,path,size,mtime,extension FROM files WHERE id=?", id).Scan(&f.ID, &f.Name, &f.Path, &f.Size, &m, &f.Extension)
	if err == nil {
		decorate(&f, m, a.Config())
	}
	return f, err
}
