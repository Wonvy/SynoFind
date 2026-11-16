package app

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
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
	statePath := filepath.Join(dataDir, "state.db")
	legacyPath := filepath.Join(dataDir, "index.db")
	if _, err := os.Stat(statePath); errors.Is(err, os.ErrNotExist) {
		if _, legacyErr := os.Stat(legacyPath); legacyErr == nil {
			legacy, openErr := sql.Open("sqlite", legacyPath)
			if openErr != nil {
				return nil, openErr
			}
			legacy.SetMaxOpenConns(1)
			if _, openErr = legacy.Exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,name TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,last_used_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
DROP TRIGGER IF EXISTS files_ai; DROP TRIGGER IF EXISTS files_ad; DROP TRIGGER IF EXISTS files_au;
DROP TABLE IF EXISTS files_fts; DROP TABLE IF EXISTS scan_files; DROP TABLE IF EXISTS files;
DELETE FROM meta WHERE key IN ('index_status','last_completed_at');
INSERT INTO meta(key,value) VALUES('schema_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value;`); openErr != nil {
				legacy.Close()
				return nil, openErr
			}
			if _, openErr = legacy.Exec("VACUUM"); openErr != nil {
				legacy.Close()
				return nil, openErr
			}
			if openErr = legacy.Close(); openErr != nil {
				return nil, openErr
			}
			_ = os.Remove(legacyPath + "-wal")
			_ = os.Remove(legacyPath + "-shm")
			if openErr = os.Rename(legacyPath, statePath); openErr != nil {
				return nil, openErr
			}
		} else if legacyErr != nil && !errors.Is(legacyErr, os.ErrNotExist) {
			return nil, legacyErr
		}
	} else if err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", statePath)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	_, err = db.Exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;
 CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,name TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,last_used_at TEXT NOT NULL DEFAULT '');
 CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires INTEGER NOT NULL);
 INSERT INTO meta(key,value) VALUES('schema_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value;
 `)
	if err != nil {
		db.Close()
		return nil, err
	}
	if err = os.Chmod(statePath, 0600); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

func signedFileID(secret, remotePath string) string {
	key := sha256.Sum256([]byte(secret))
	mac := hmac.New(sha256.New, key[:])
	_, _ = mac.Write([]byte(remotePath))
	signature := mac.Sum(nil)[:16]
	return base64.RawURLEncoding.EncodeToString([]byte(remotePath)) + "." + base64.RawURLEncoding.EncodeToString(signature)
}

func verifyFileID(secret, id, root string) (string, error) {
	parts := strings.Split(id, ".")
	if len(parts) != 2 {
		return "", sql.ErrNoRows
	}
	remotePathBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil || len(remotePathBytes) == 0 || len(remotePathBytes) > 4096 {
		return "", sql.ErrNoRows
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil || len(signature) != 16 {
		return "", sql.ErrNoRows
	}
	remotePath := string(remotePathBytes)
	expected := signedFileID(secret, remotePath)
	expectedParts := strings.Split(expected, ".")
	expectedSignature, _ := base64.RawURLEncoding.DecodeString(expectedParts[1])
	if !hmac.Equal(signature, expectedSignature) {
		return "", sql.ErrNoRows
	}
	validated, err := validateDSMPath(remotePath, root)
	if err != nil {
		return "", sql.ErrNoRows
	}
	return validated, nil
}

func (a *App) searchDSM(ctx context.Context, o SearchOptions) (SearchResult, error) {
	result := SearchResult{Items: []File{}, Page: o.Page, Limit: o.Limit}
	cfg := a.Config()
	client, err := newDSMClient(cfg)
	if err != nil {
		a.setDSMStatus(false, err)
		return result, err
	}
	if err = client.connect(ctx); err != nil {
		client.http.CloseIdleConnections()
		a.setDSMStatus(false, err)
		return result, err
	}
	defer func() { client.logout(); client.http.CloseIdleConnections() }()
	var after, before *time.Time
	if o.After != "" {
		value, parseErr := time.Parse(time.RFC3339, o.After)
		if parseErr != nil {
			return result, parseErr
		}
		after = &value
	}
	if o.Before != "" {
		value, parseErr := time.Parse(time.RFC3339, o.Before)
		if parseErr != nil {
			return result, parseErr
		}
		before = &value
	}
	page, err := client.search(ctx, cfg.RootPath, dsmSearchOptions{
		Pattern: o.Query, Extension: o.Ext, MinSize: o.MinSize, MaxSize: o.MaxSize,
		After: after, Before: before, Offset: (o.Page - 1) * o.Limit, Limit: o.Limit,
		Sort: o.Sort, Order: o.Order,
	})
	if err != nil {
		a.setDSMStatus(false, err)
		return result, err
	}
	result.Total = page.Total
	for _, entry := range page.Files {
		rel := strings.TrimPrefix(entry.Path, strings.TrimRight(cfg.RootPath, "/"))
		rel = strings.TrimPrefix(rel, "/")
		if rel == "" {
			continue
		}
		ext := strings.TrimPrefix(strings.ToLower(entry.Additional.Type), ".")
		if ext == "" {
			ext = strings.TrimPrefix(strings.ToLower(filepath.Ext(entry.Name)), ".")
		}
		file := File{ID: signedFileID(cfg.PasswordHash, entry.Path), Name: entry.Name, Path: rel, Size: entry.Additional.Size, Extension: ext}
		decorate(&file, entry.Additional.Time.MTime*int64(time.Second), cfg)
		result.Items = append(result.Items, file)
	}
	a.setDSMStatus(true, nil)
	a.recordDSMSearch()
	return result, nil
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
	case "psd":
		return "psd"
	case "ppt", "pptx":
		return "pptx"
	case "ai":
		return "illustrator"
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
	return a.searchDSM(context.Background(), o)
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
