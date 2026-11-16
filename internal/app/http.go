package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"log"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"synofind/docs"
	"time"
	"unicode/utf8"
)

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(value)
}
func apiError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": message}})
}
func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	mt, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if mt != "application/json" {
		apiError(w, 415, "content_type", "请使用 application/json")
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 65536)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(dst); err != nil {
		apiError(w, 400, "invalid_json", "JSON 请求格式无效")
		return false
	}
	if decoder.Decode(new(any)) != io.EOF {
		apiError(w, 400, "invalid_json", "请求只能包含一个 JSON 对象")
		return false
	}
	return true
}

func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Write(docs.OpenAPI)
	})
	mux.HandleFunc("GET /api/v1/health", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("POST /api/v1/auth/login", a.login)
	mux.HandleFunc("GET /api/v1/auth/session", a.session)
	mux.HandleFunc("POST /api/v1/auth/logout", a.protected(true, a.logout))
	mux.HandleFunc("GET /api/v1/search", a.protected(false, a.searchHTTP))
	mux.HandleFunc("GET /api/v1/files/{id}", a.protected(false, a.fileHTTP))
	mux.HandleFunc("GET /api/v1/files/{id}/content", a.protected(false, a.contentHTTP))
	mux.HandleFunc("GET /api/v1/files/{id}/preview", a.protected(false, a.contentHTTP))
	mux.HandleFunc("GET /api/v1/index/status", a.protected(false, func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, a.Status()) }))
	mux.HandleFunc("GET /api/v1/settings", a.protected(true, a.settings))
	mux.HandleFunc("PUT /api/v1/settings", a.protected(true, a.settings))
	mux.HandleFunc("GET /api/v1/tokens", a.protected(true, a.tokens))
	mux.HandleFunc("POST /api/v1/tokens", a.protected(true, a.tokens))
	mux.HandleFunc("DELETE /api/v1/tokens/{id}", a.protected(true, a.tokens))
	mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) { apiError(w, 404, "not_found", "接口不存在") })
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" && r.Method != "HEAD" {
			w.Header().Set("Allow", "GET, HEAD")
			apiError(w, 405, "method_not_allowed", "请求方法不支持")
			return
		}
		a.static(w, r)
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recover() != nil {
				log.Print("request failed: internal panic")
				apiError(w, 500, "internal", "服务器内部错误")
			}
		}()
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Frame-Options", "SAMEORIGIN")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; worker-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
		w.Header().Set("Cache-Control", "no-store")
		mux.ServeHTTP(w, r)
	})
}

func (a *App) static(w http.ResponseWriter, r *http.Request) {
	if a.assets == nil {
		http.NotFound(w, r)
		return
	}
	p := strings.TrimPrefix(r.URL.Path, "/")
	if p == "" {
		p = "index.html"
	}
	if !fs.ValidPath(p) {
		http.NotFound(w, r)
		return
	}
	b, err := fs.ReadFile(a.assets, p)
	if err != nil {
		if filepath.Ext(p) != "" {
			http.NotFound(w, r)
			return
		}
		p = "index.html"
		b, err = fs.ReadFile(a.assets, p)
	}
	if err != nil {
		http.NotFound(w, r)
		return
	}
	mt := mime.TypeByExtension(filepath.Ext(p))
	if filepath.Ext(p) == ".mjs" || filepath.Ext(p) == ".js" {
		mt = "text/javascript; charset=utf-8"
	}
	if mt == "" {
		mt = "application/octet-stream"
	}
	w.Header().Set("Content-Type", mt)
	w.Write(b)
}

func (a *App) searchHTTP(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	o := SearchOptions{Query: q.Get("q"), Ext: q.Get("ext"), After: q.Get("modified_after"), Before: q.Get("modified_before"), Sort: q.Get("sort"), Order: q.Get("order"), Page: 1, Limit: 50}
	if !utf8.ValidString(o.Query) || len([]rune(o.Query)) > 512 || len(strings.Fields(o.Query)) > 32 || len(o.Ext) > 512 {
		apiError(w, 400, "invalid_query", "搜索条件过长或无效")
		return
	}
	if strings.ContainsAny(o.Query, "/\\\r\n\x00") {
		apiError(w, 400, "invalid_query", "只搜索文件名，关键词不能包含路径分隔符")
		return
	}
	for key, ptr := range map[string]*int{"page": &o.Page, "limit": &o.Limit} {
		if q.Has(key) {
			v, err := strconv.Atoi(q.Get(key))
			if err != nil || v < 1 || (key == "limit" && v > 200) || (key == "page" && v > 1000000) {
				apiError(w, 400, "invalid_page", "分页参数无效，limit范围为1–200")
				return
			}
			*ptr = v
		}
	}
	for key, ptr := range map[string]**int64{"min_size": &o.MinSize, "max_size": &o.MaxSize} {
		if q.Has(key) && q.Get(key) != "" {
			v, err := strconv.ParseInt(q.Get(key), 10, 64)
			if err != nil || v < 0 {
				apiError(w, 400, "invalid_size", "文件大小须为非负字节数")
				return
			}
			*ptr = &v
		}
	}
	if o.MinSize != nil && o.MaxSize != nil && *o.MinSize > *o.MaxSize {
		apiError(w, 400, "invalid_size", "最小大小不能大于最大大小")
		return
	}
	if o.Sort != "" && o.Sort != "name" && o.Sort != "modified" && o.Sort != "size" || o.Order != "" && o.Order != "asc" && o.Order != "desc" {
		apiError(w, 400, "invalid_sort", "排序参数无效")
		return
	}
	for _, v := range []string{o.After, o.Before} {
		if v != "" {
			if _, err := time.Parse(time.RFC3339, v); err != nil {
				apiError(w, 400, "invalid_date", "日期须使用RFC3339格式")
				return
			}
		}
	}
	if o.After != "" && o.Before != "" {
		start, _ := time.Parse(time.RFC3339, o.After)
		end, _ := time.Parse(time.RFC3339, o.Before)
		if start.After(end) {
			apiError(w, 400, "invalid_date", "开始日期不能晚于结束日期")
			return
		}
	}
	if strings.TrimSpace(o.Query) == "" && o.Ext == "" && o.MinSize == nil && o.MaxSize == nil && o.After == "" && o.Before == "" {
		writeJSON(w, 200, SearchResult{Items: []File{}, Page: o.Page, Limit: o.Limit})
		return
	}
	result, err := a.searchDSM(r.Context(), o)
	if err != nil {
		log.Printf("search failed: %v", err)
		apiError(w, 500, "search_failed", "搜索失败，请重试")
		return
	}
	writeJSON(w, 200, result)
}

func fileError(w http.ResponseWriter, err error) {
	if errors.Is(err, sql.ErrNoRows) || errors.Is(err, os.ErrNotExist) {
		apiError(w, 404, "file_missing", "文件已不存在，请重新搜索")
	} else if errors.Is(err, os.ErrPermission) {
		apiError(w, 403, "file_forbidden", "没有读取权限，或文件路径不在允许范围内")
	} else {
		apiError(w, 409, "file_unavailable", "文件暂时无法读取，请检查 DSM 只读账户的目录权限后重试")
	}
}
func (a *App) fileHTTP(w http.ResponseWriter, r *http.Request) {
	cfg := a.Config()
	remotePath, err := verifyFileID(cfg.PasswordHash, r.PathValue("id"), cfg.RootPath)
	if err != nil {
		apiError(w, 404, "file_missing", "文件标识无效或已失效，请重新搜索")
		return
	}
	client, file, err := a.connectAndGetFile(r.Context(), cfg, remotePath)
	if err != nil {
		a.setDSMStatus(false, err)
		fileError(w, err)
		return
	}
	client.logout()
	client.http.CloseIdleConnections()
	a.setDSMStatus(true, nil)
	f := a.presentFile(file, cfg)
	writeJSON(w, 200, f)
}

func (a *App) connectAndGetFile(ctx context.Context, cfg Config, remotePath string) (*dsmClient, dsmFile, error) {
	client, err := newDSMClient(cfg)
	if err != nil {
		return nil, dsmFile{}, err
	}
	if err = client.connect(ctx); err != nil {
		client.http.CloseIdleConnections()
		return nil, dsmFile{}, err
	}
	file, err := client.fileInfo(ctx, remotePath, cfg.RootPath)
	if err != nil {
		client.logout()
		client.http.CloseIdleConnections()
		return nil, dsmFile{}, err
	}
	return client, file, nil
}

func (a *App) presentFile(entry dsmFile, cfg Config) File {
	root := strings.TrimRight(cfg.RootPath, "/")
	rel := strings.TrimPrefix(entry.Path, root)
	rel = strings.TrimPrefix(rel, "/")
	ext := strings.TrimPrefix(strings.ToLower(entry.Additional.Type), ".")
	if ext == "" {
		ext = strings.TrimPrefix(strings.ToLower(filepath.Ext(entry.Name)), ".")
	}
	f := File{ID: signedFileID(cfg.PasswordHash, entry.Path), Name: entry.Name, Path: rel, Size: entry.Additional.Size, Extension: ext}
	decorate(&f, entry.Additional.Time.MTime*int64(time.Second), cfg)
	return f
}

func (a *App) contentHTTP(w http.ResponseWriter, r *http.Request) {
	cfg := a.Config()
	remotePath, err := verifyFileID(cfg.PasswordHash, r.PathValue("id"), cfg.RootPath)
	if err != nil {
		apiError(w, 404, "file_missing", "文件标识无效或已失效，请重新搜索")
		return
	}
	client, err := newDSMClient(cfg)
	if err != nil {
		a.setDSMStatus(false, err)
		fileError(w, err)
		return
	}
	if err = client.connect(r.Context()); err != nil {
		client.http.CloseIdleConnections()
		a.setDSMStatus(false, err)
		fileError(w, err)
		return
	}
	defer func() { client.logout(); client.http.CloseIdleConnections() }()
	entry, stream, err := client.download(r.Context(), remotePath, cfg.RootPath)
	if err != nil {
		a.setDSMStatus(false, err)
		fileError(w, err)
		return
	}
	defer stream.Close()
	a.setDSMStatus(true, nil)
	f := a.presentFile(entry, cfg)
	preview := strings.HasSuffix(r.URL.Path, "/preview")
	if preview && f.PreviewType == "" {
		apiError(w, 415, "preview_unsupported", "此格式暂不支持预览，请下载后打开")
		return
	}
	disposition := "attachment"
	contentType := "application/octet-stream"
	if preview {
		disposition = "inline"
		switch f.PreviewType {
		case "image":
			contentType = mime.TypeByExtension("." + f.Extension)
		case "pdf":
			contentType = "application/pdf"
		case "pptx":
			contentType = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
		case "text":
			contentType = "text/plain; charset=utf-8"
			if f.Size > 2*1024*1024 {
				apiError(w, 413, "preview_too_large", "文本超过2MB，请下载后查看")
				return
			}
		}
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", mime.FormatMediaType(disposition, map[string]string{"filename": f.Name}))
	if preview {
		w.Header().Set("Content-Security-Policy", "default-src 'none'; sandbox; frame-ancestors 'self'")
	}
	if f.ModifiedAt != "" {
		mtime, parseErr := time.Parse(time.RFC3339Nano, f.ModifiedAt)
		if parseErr == nil {
			w.Header().Set("ETag", fmtETag(f.Size, mtime))
		}
	}
	if _, err = io.Copy(w, stream); err != nil {
		log.Printf("DSM file stream failed for an authenticated request")
	}
}
func fmtETag(size int64, mtime time.Time) string {
	return `"` + strconv.FormatInt(size, 16) + "-" + strconv.FormatInt(mtime.UnixNano(), 16) + `"`
}

func (a *App) settings(w http.ResponseWriter, r *http.Request) {
	if r.Method == "GET" {
		cfg := a.Config()
		writeJSON(w, 200, publicSettings(cfg))
		return
	}
	var body struct {
		RootPath    string `json:"root_path"`
		SMBRoot     string `json:"smb_root"`
		DSMURL      string `json:"dsm_url"`
		DSMUsername string `json:"dsm_username"`
		DSMPassword string `json:"dsm_password"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	body.RootPath = normalizeFileStationRoot(body.RootPath)
	if err := validateFileStationRoot(body.RootPath); err != nil {
		apiError(w, 400, "invalid_root", err.Error())
		return
	}
	if body.SMBRoot != "" && (!strings.HasPrefix(body.SMBRoot, `\\`) || strings.ContainsAny(body.SMBRoot, "\r\n\x00")) {
		apiError(w, 400, "invalid_smb", "共享路径示例：\\\\NAS\\共享文件夹")
		return
	}
	old := a.Config()
	cfg := old
	cfg.RootPath = body.RootPath
	cfg.SMBRoot = body.SMBRoot
	cfg.DSMURL = strings.TrimSpace(body.DSMURL)
	cfg.DSMUsername = strings.TrimSpace(body.DSMUsername)
	if body.DSMPassword != "" {
		cfg.DSMPassword = body.DSMPassword
	} else if cfg.DSMUsername != old.DSMUsername {
		cfg.DSMPassword = ""
	}
	client, err := newDSMClient(cfg)
	if err == nil {
		ctx, cancel := context.WithTimeout(r.Context(), 40*time.Second)
		err = client.connect(ctx)
		if err == nil {
			err = client.checkRoot(ctx, cfg.RootPath)
		}
		cancel()
		client.logout()
		client.http.CloseIdleConnections()
	}
	if err != nil {
		apiError(w, 400, "dsm_connection", err.Error())
		return
	}
	if err = SaveConfig(a.dataDir, cfg); err != nil {
		apiError(w, 500, "config_write", "无法保存设置")
		return
	}
	a.mu.Lock()
	a.cfg = cfg
	a.status.RootPath = cfg.RootPath
	a.status.Connected = true
	a.status.LastError = ""
	a.mu.Unlock()
	writeJSON(w, 200, publicSettings(cfg))
}

func publicSettings(cfg Config) map[string]any {
	return map[string]any{
		"root_path": cfg.RootPath, "smb_root": cfg.SMBRoot, "dsm_url": cfg.DSMURL,
		"dsm_username": cfg.DSMUsername, "has_dsm_password": cfg.DSMPassword != "", "port": cfg.Port,
	}
}
