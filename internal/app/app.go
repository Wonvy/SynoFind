package app

import (
	"context"
	"database/sql"
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
	Connected       bool     `json:"connected"`
	LastSearchAt    string   `json:"last_search_at"`
	LastError       string   `json:"last_error"`
	RootPath        string   `json:"root_path"`
	Running         bool     `json:"-"`
	TotalFiles      int      `json:"-"`
	ScannedFiles    int      `json:"-"`
	StartedAt       string   `json:"-"`
	LastCompletedAt string   `json:"-"`
	LastDurationMS  int64    `json:"-"`
	Errors          []string `json:"-"`
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
	a.status.RootPath = cfg.RootPath
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
	a.wg.Add(1)
	go func() {
		defer a.wg.Done()
		ticker := time.NewTicker(time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-a.ctx.Done():
				return
			case <-ticker.C:
				a.db.ExecContext(a.ctx, "DELETE FROM sessions WHERE expires<?", time.Now().Unix())
			}
		}
	}()
}

func (a *App) setDSMStatus(connected bool, err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.status.Connected = connected
	if err != nil {
		a.status.LastError = err.Error()
		return
	}
	a.status.LastError = ""
}

func (a *App) recordDSMSearch() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.status.LastSearchAt = time.Now().UTC().Format(time.RFC3339)
}
func (a *App) Close() error { a.mu.Lock(); a.cancel(); a.mu.Unlock(); a.wg.Wait(); return a.db.Close() }
