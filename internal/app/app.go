package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"io/fs"
	"os"
	"sync"
	"time"
)

type App struct {
	db       *sql.DB
	dataDir  string
	mu       sync.RWMutex
	cfg      Config
	status   IndexStatus
	ctx      context.Context
	cancel   context.CancelFunc
	wg       sync.WaitGroup
	assets   fs.FS
	authMu   sync.Mutex
	attempts map[string]*attempt
}
type IndexStatus struct {
	Running         bool     `json:"running"`
	TotalFiles      int      `json:"total_files"`
	ScannedFiles    int      `json:"scanned_files"`
	StartedAt       string   `json:"started_at"`
	LastCompletedAt string   `json:"last_completed_at"`
	LastDurationMS  int64    `json:"last_duration_ms"`
	LastError       string   `json:"last_error"`
	Errors          []string `json:"errors"`
	RootPath        string   `json:"root_path"`
}

func New(dataDir string, assets fs.FS) (*App, error) {
	if err := os.Chmod(dataDir, 0700); err != nil {
		return nil, err
	}
	cfg, err := LoadConfig(dataDir)
	if err != nil {
		return nil, err
	}
	db, err := openDB(dataDir)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &App{db: db, dataDir: dataDir, cfg: cfg, ctx: ctx, cancel: cancel, assets: assets, attempts: map[string]*attempt{}}
	a.status.Errors = []string{}
	a.status.RootPath = cfg.RootPath
	var saved string
	if db.QueryRow("SELECT value FROM meta WHERE key='index_status'").Scan(&saved) == nil {
		var state IndexStatus
		if json.Unmarshal([]byte(saved), &state) == nil && state.RootPath == cfg.RootPath {
			a.status = state
			a.status.Running = false
		}
	}
	db.QueryRow("SELECT count(*) FROM files").Scan(&a.status.TotalFiles)
	db.QueryRow("SELECT value FROM meta WHERE key='last_completed_at'").Scan(&a.status.LastCompletedAt)
	return a, nil
}
func (a *App) Config() Config { a.mu.RLock(); defer a.mu.RUnlock(); return a.cfg }
func (a *App) Status() IndexStatus {
	a.mu.RLock()
	defer a.mu.RUnlock()
	s := a.status
	s.Errors = append([]string{}, s.Errors...)
	return s
}
func (a *App) Start() {
	a.StartScan()
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		ticker := time.NewTicker(time.Second * 30)
		defer ticker.Stop()
		last := time.Now()
		for {
			select {
			case <-a.ctx.Done():
				return
			case <-ticker.C:
				if time.Since(last) >= time.Duration(a.Config().ScanIntervalMinutes)*time.Minute {
					if a.StartScan() {
						last = time.Now()
					}
				}
				a.db.ExecContext(a.ctx, "DELETE FROM sessions WHERE expires<?", time.Now().Unix())
			}
		}
	}()
}
func (a *App) Close() error { a.mu.Lock(); a.cancel(); a.mu.Unlock(); a.wg.Wait(); return a.db.Close() }
