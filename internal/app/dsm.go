package app

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	dsmAuthAPI     = "SYNO.API.Auth"
	dsmSearchAPI   = "SYNO.FileStation.Search"
	dsmListAPI     = "SYNO.FileStation.List"
	dsmDownloadAPI = "SYNO.FileStation.Download"
)

var dsmAPIPathPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$`)

type dsmAPIInfo struct {
	Path          string
	MinVersion    int
	MaxVersion    int
	RequestFormat string
}

type dsmClient struct {
	baseURL   string
	http      *http.Client
	apis      map[string]dsmAPIInfo
	sid       string
	synoToken string
	username  string
	password  string
}

type dsmReply struct {
	Success bool                       `json:"success"`
	Data    map[string]json.RawMessage `json:"data"`
	Error   struct {
		Code int `json:"code"`
	} `json:"error"`
}

type dsmSearchPage struct {
	Total    int       `json:"total"`
	Offset   int       `json:"offset"`
	Finished *bool     `json:"finished"`
	Files    []dsmFile `json:"files"`
}

type dsmFile struct {
	Path       string `json:"path"`
	Name       string `json:"name"`
	IsDir      bool   `json:"isdir"`
	Additional struct {
		RealPath string `json:"real_path"`
		Size     int64  `json:"size"`
		Type     string `json:"type"`
		Time     struct {
			MTime int64 `json:"mtime"`
		} `json:"time"`
	} `json:"additional"`
}

type dsmPrefixedReadCloser struct {
	io.Reader
	io.Closer
}

func newDSMClient(cfg Config) (*dsmClient, error) {
	u, err := url.Parse(strings.TrimSpace(cfg.DSMURL))
	if err != nil || u == nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") {
		return nil, errors.New("DSM API 地址无效，请填写不带路径的 HTTP(S) 地址")
	}
	if u.Scheme != "https" && u.Scheme != "http" {
		return nil, errors.New("DSM API 地址须使用 HTTPS；仅本机回环地址允许 HTTP")
	}
	if u.Scheme == "http" {
		ip := net.ParseIP(u.Hostname())
		if !strings.EqualFold(u.Hostname(), "localhost") && (ip == nil || !ip.IsLoopback()) {
			return nil, errors.New("HTTP 仅允许连接 NAS 本机回环地址；远程地址请使用 HTTPS")
		}
	}
	if strings.TrimSpace(cfg.DSMUsername) == "" || cfg.DSMPassword == "" {
		return nil, errors.New("请在设置中填写 DSM 只读账户和密码")
	}
	transport := &http.Transport{
		Proxy:           nil,
		TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12},
	}
	return &dsmClient{
		baseURL: strings.TrimRight(u.String(), "/"),
		http: &http.Client{
			Transport: transport,
			Timeout:   35 * time.Second,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		apis:     map[string]dsmAPIInfo{},
		username: cfg.DSMUsername,
		password: cfg.DSMPassword,
	}, nil
}

func safeDSMAPIPath(value string) (string, error) {
	if !dsmAPIPathPattern.MatchString(value) {
		return "", errors.New("群晖返回了无效的 API 地址")
	}
	for _, part := range strings.Split(value, "/") {
		if part == "." || part == ".." {
			return "", errors.New("群晖返回了无效的 API 地址")
		}
	}
	return value, nil
}

func (c *dsmClient) endpoint(api string) (dsmAPIInfo, int, error) {
	info, ok := c.apis[api]
	if !ok {
		return dsmAPIInfo{}, 0, fmt.Errorf("此 DSM 未提供 %s API", api)
	}
	versions := []int{2}
	if api == dsmAuthAPI {
		versions = []int{6, 3, 7, 5, 4}
	}
	for _, version := range versions {
		if version >= info.MinVersion && version <= info.MaxVersion {
			return info, version, nil
		}
	}
	return dsmAPIInfo{}, 0, fmt.Errorf("此 DSM 的 %s API 版本不受支持", api)
}

func (c *dsmClient) request(ctx context.Context, endpoint string, fields url.Values, authenticated bool, maxBytes int64) ([]byte, http.Header, error) {
	endpoint, err := safeDSMAPIPath(endpoint)
	if err != nil {
		return nil, nil, err
	}
	requestURL := c.baseURL + "/webapi/" + endpoint
	if authenticated {
		if c.sid == "" {
			return nil, nil, errors.New("尚未登录 DSM")
		}
		fields.Set("_sid", c.sid)
		if c.synoToken != "" {
			fields.Set("SynoToken", c.synoToken)
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL, strings.NewReader(fields.Encode()))
	if err != nil {
		return nil, nil, errors.New("无法构造 DSM API 请求")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("User-Agent", "SynoFind-DSM-Suite/0.2.0")
	if authenticated {
		req.Header.Set("Cookie", "id="+c.sid)
		if c.synoToken != "" {
			req.Header.Set("X-SYNO-TOKEN", c.synoToken)
		}
	}
	response, err := c.http.Do(req)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return nil, nil, err
		}
		var urlErr *url.Error
		if errors.As(err, &urlErr) && strings.Contains(strings.ToLower(urlErr.Err.Error()), "redirect") {
			return nil, nil, errors.New("DSM API 返回了重定向；为保护凭据，本请求已停止")
		}
		return nil, nil, errors.New("无法连接 DSM API，请检查地址、证书和 DSM 状态")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode >= 300 && response.StatusCode < 400 {
			return nil, nil, errors.New("DSM API 返回了重定向；为保护凭据，本请求已停止")
		}
		return nil, nil, fmt.Errorf("DSM API 返回 HTTP %d", response.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, maxBytes+1))
	if err != nil {
		return nil, nil, errors.New("无法完整读取 DSM API 响应")
	}
	if int64(len(body)) > maxBytes {
		return nil, nil, errors.New("DSM API 响应超过大小限制")
	}
	return body, response.Header.Clone(), nil
}

func (c *dsmClient) call(ctx context.Context, api, method string, params map[string]any, authenticated bool) (map[string]json.RawMessage, error) {
	info, version, err := c.endpoint(api)
	if err != nil {
		return nil, err
	}
	fields := url.Values{"api": {api}, "version": {strconv.Itoa(version)}, "method": {method}}
	for key, value := range params {
		if value == nil {
			continue
		}
		var encoded string
		switch typed := value.(type) {
		case string:
			encoded = typed
		case int:
			encoded = strconv.Itoa(typed)
		case int64:
			encoded = strconv.FormatInt(typed, 10)
		case bool:
			encoded = strconv.FormatBool(typed)
		default:
			bytes, marshalErr := json.Marshal(value)
			if marshalErr != nil {
				return nil, errors.New("无法编码 DSM API 参数")
			}
			encoded = string(bytes)
		}
		if info.RequestFormat == "JSON" && !isJSONValue(value) {
			bytes, marshalErr := json.Marshal(value)
			if marshalErr != nil {
				return nil, errors.New("无法编码 DSM API 参数")
			}
			encoded = string(bytes)
		}
		fields.Set(key, encoded)
	}
	body, _, err := c.request(ctx, info.Path, fields, authenticated, 16*1024*1024)
	if err != nil {
		return nil, err
	}
	var reply dsmReply
	if err = json.Unmarshal(body, &reply); err != nil {
		return nil, errors.New("DSM API 返回了无效响应")
	}
	if !reply.Success {
		if reply.Error.Code == 106 || reply.Error.Code == 107 {
			return nil, fmt.Errorf("DSM 会话已失效或账户无权访问 File Station（错误代码 %d）", reply.Error.Code)
		}
		if api == dsmAuthAPI && (reply.Error.Code == 403 || reply.Error.Code == 406) {
			return nil, errors.New("DSM 拒绝了登录；该账户可能需要双重验证，或没有 File Station 权限")
		}
		return nil, fmt.Errorf("DSM 拒绝了 %s.%s 请求（错误代码 %d）", api, method, reply.Error.Code)
	}
	if reply.Data == nil {
		reply.Data = map[string]json.RawMessage{}
	}
	return reply.Data, nil
}

func isJSONValue(value any) bool {
	switch value.(type) {
	case []string, []any, map[string]any, bool:
		return true
	default:
		return false
	}
}

func (c *dsmClient) discover(ctx context.Context) error {
	fields := url.Values{
		"api":     {"SYNO.API.Info"},
		"version": {"1"},
		"method":  {"query"},
		"query":   {strings.Join([]string{dsmAuthAPI, dsmSearchAPI, dsmListAPI, dsmDownloadAPI}, ",")},
	}
	body, _, err := c.request(ctx, "entry.cgi", fields, false, 1024*1024)
	if err != nil {
		return err
	}
	var envelope struct {
		Success bool `json:"success"`
		Data    map[string]struct {
			Path          string `json:"path"`
			MinVersion    int    `json:"minVersion"`
			MaxVersion    int    `json:"maxVersion"`
			RequestFormat string `json:"requestFormat"`
		} `json:"data"`
		Error struct {
			Code int `json:"code"`
		} `json:"error"`
	}
	if err = json.Unmarshal(body, &envelope); err != nil || !envelope.Success {
		if envelope.Error.Code != 0 {
			return fmt.Errorf("无法发现 DSM Web API（错误代码 %d）", envelope.Error.Code)
		}
		return errors.New("DSM 未返回有效的 Web API 信息")
	}
	for api, value := range envelope.Data {
		apiPath, pathErr := safeDSMAPIPath(value.Path)
		if pathErr != nil || value.MinVersion < 1 || value.MaxVersion < value.MinVersion {
			return errors.New("DSM 返回了无效的 Web API 信息")
		}
		if value.RequestFormat != "" && value.RequestFormat != "JSON" {
			return errors.New("DSM 声明了不受支持的 API 参数格式")
		}
		c.apis[api] = dsmAPIInfo{Path: apiPath, MinVersion: value.MinVersion, MaxVersion: value.MaxVersion, RequestFormat: value.RequestFormat}
	}
	for _, api := range []string{dsmAuthAPI, dsmSearchAPI, dsmListAPI, dsmDownloadAPI} {
		if _, _, err = c.endpoint(api); err != nil {
			return err
		}
	}
	return nil
}

func (c *dsmClient) connect(ctx context.Context) error {
	defer func() { c.password = "" }()
	if err := c.discover(ctx); err != nil {
		return err
	}
	_, version, err := c.endpoint(dsmAuthAPI)
	if err != nil {
		return err
	}
	params := map[string]any{"account": c.username, "passwd": c.password, "session": "FileStation", "format": "sid"}
	if version >= 6 {
		params["enable_syno_token"] = "yes"
	}
	data, err := c.call(ctx, dsmAuthAPI, "login", params, false)
	if err != nil {
		return err
	}
	var login struct {
		SID       string `json:"sid"`
		SynoToken string `json:"synotoken"`
	}
	if err = json.Unmarshal(mustRaw(data["sid"]), &login.SID); err != nil {
		return errors.New("DSM 登录响应缺少有效会话")
	}
	if raw, ok := data["synotoken"]; ok && string(raw) != "null" {
		if err = json.Unmarshal(raw, &login.SynoToken); err != nil {
			return errors.New("DSM 登录响应包含无效安全令牌")
		}
	}
	if login.SID == "" || strings.ContainsAny(login.SID, "\r\n; ,") || strings.ContainsAny(login.SynoToken, "\r\n") {
		return errors.New("DSM 登录响应包含无效会话")
	}
	c.sid, c.synoToken = login.SID, login.SynoToken
	return nil
}

func mustRaw(value json.RawMessage) []byte {
	if len(value) == 0 {
		return []byte("null")
	}
	return value
}

func (c *dsmClient) logout() {
	if c.sid == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, _ = c.call(ctx, dsmAuthAPI, "logout", map[string]any{"session": "FileStation"}, true)
	c.sid, c.synoToken = "", ""
}

func validateDSMPath(value, root string) (string, error) {
	if value == "" || !strings.HasPrefix(value, "/") || strings.Contains(value, `\`) || strings.Contains(value, "//") {
		return "", errors.New("群晖返回了无效的文件路径")
	}
	for _, part := range strings.Split(strings.TrimPrefix(value, "/"), "/") {
		if part == "" || part == "." || part == ".." {
			return "", errors.New("群晖返回了无效的文件路径")
		}
		for _, char := range part {
			if char < 0x20 || char == 0x7f {
				return "", errors.New("群晖返回了无效的文件路径")
			}
		}
	}
	if value != root && !strings.HasPrefix(value, strings.TrimRight(root, "/")+"/") {
		return "", errors.New("群晖返回了搜索范围以外的文件")
	}
	return path.Clean(value), nil
}

type dsmSearchOptions struct {
	Pattern   string
	Extension string
	MinSize   *int64
	MaxSize   *int64
	After     *time.Time
	Before    *time.Time
	Offset    int
	Limit     int
	Sort      string
	Order     string
}

func (c *dsmClient) search(ctx context.Context, root string, options dsmSearchOptions) (dsmSearchPage, error) {
	if err := validateFileStationRoot(root); err != nil {
		return dsmSearchPage{}, err
	}
	params := map[string]any{"folder_path": []string{root}, "recursive": true, "filetype": "file"}
	if options.Pattern != "" {
		params["pattern"] = options.Pattern
	}
	if options.Extension != "" {
		params["extension"] = options.Extension
	}
	if options.MinSize != nil {
		params["size_from"] = *options.MinSize
	}
	if options.MaxSize != nil {
		params["size_to"] = *options.MaxSize
	}
	if options.After != nil {
		params["mtime_from"] = options.After.Unix()
	}
	if options.Before != nil {
		params["mtime_to"] = options.Before.Unix()
	}
	started, err := c.call(ctx, dsmSearchAPI, "start", params, true)
	if err != nil {
		return dsmSearchPage{}, err
	}
	var task struct {
		ID string `json:"taskid"`
	}
	if err = json.Unmarshal(mustRaw(started["taskid"]), &task.ID); err != nil || task.ID == "" || strings.ContainsAny(task.ID, "\r\n\x00") {
		return dsmSearchPage{}, errors.New("DSM 没有返回有效的搜索任务编号")
	}
	finished := false
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if !finished {
			_, _ = c.call(cleanupCtx, dsmSearchAPI, "stop", map[string]any{"taskid": []string{task.ID}}, true)
		}
		_, _ = c.call(cleanupCtx, dsmSearchAPI, "clean", map[string]any{"taskid": []string{task.ID}}, true)
	}()
	deadline := time.NewTimer(120 * time.Second)
	defer deadline.Stop()
	poll := time.NewTicker(300 * time.Millisecond)
	defer poll.Stop()
	for {
		state, callErr := c.call(ctx, dsmSearchAPI, "list", map[string]any{"taskid": task.ID, "limit": 0, "offset": 0}, true)
		if callErr != nil {
			return dsmSearchPage{}, callErr
		}
		var done bool
		if err = json.Unmarshal(mustRaw(state["finished"]), &done); err != nil {
			return dsmSearchPage{}, errors.New("DSM 返回了无效的搜索状态")
		}
		if done {
			finished = true
			break
		}
		select {
		case <-ctx.Done():
			return dsmSearchPage{}, ctx.Err()
		case <-deadline.C:
			return dsmSearchPage{}, errors.New("群晖搜索超时，请缩小搜索范围后重试")
		case <-poll.C:
		}
	}
	if options.Sort == "modified" {
		options.Sort = "mtime"
	}
	if options.Sort == "" {
		options.Sort = "name"
	}
	if options.Order == "" {
		options.Order = "asc"
	}
	data, err := c.call(ctx, dsmSearchAPI, "list", map[string]any{
		"taskid": task.ID, "limit": options.Limit, "offset": options.Offset,
		"sort_by": options.Sort, "sort_direction": options.Order,
		"filetype": "file", "additional": []string{"real_path", "size", "time", "type"},
	}, true)
	if err != nil {
		return dsmSearchPage{}, err
	}
	var page dsmSearchPage
	if err = json.Unmarshal(mustRaw(data["total"]), &page.Total); err != nil || page.Total < 0 {
		return dsmSearchPage{}, errors.New("DSM 返回了无效的结果总数")
	}
	if raw := data["offset"]; len(raw) > 0 {
		if err = json.Unmarshal(raw, &page.Offset); err != nil || page.Offset != options.Offset {
			return dsmSearchPage{}, errors.New("DSM 返回了无效的结果分页")
		}
	} else {
		page.Offset = options.Offset
	}
	if err = json.Unmarshal(mustRaw(data["files"]), &page.Files); err != nil {
		return dsmSearchPage{}, errors.New("DSM 返回了无效的文件列表")
	}
	for i := range page.Files {
		entry := &page.Files[i]
		validated, pathErr := validateDSMPath(entry.Path, root)
		if pathErr != nil {
			return dsmSearchPage{}, pathErr
		}
		entry.Path = validated
		if entry.IsDir || entry.Name == "" || path.Base(entry.Path) != entry.Name {
			return dsmSearchPage{}, errors.New("DSM 返回了无效的文件信息")
		}
	}
	return page, nil
}

func (c *dsmClient) fileInfo(ctx context.Context, remotePath, root string) (dsmFile, error) {
	remotePath, err := validateDSMPath(remotePath, root)
	if err != nil {
		return dsmFile{}, err
	}
	data, err := c.call(ctx, dsmListAPI, "getinfo", map[string]any{
		"path": []string{remotePath}, "additional": []string{"real_path", "size", "time", "type"},
	}, true)
	if err != nil {
		return dsmFile{}, err
	}
	var response struct {
		Files []dsmFile `json:"files"`
	}
	if err = json.Unmarshal(mustRaw(data["files"]), &response.Files); err != nil || len(response.Files) != 1 {
		return dsmFile{}, errors.New("DSM 没有返回请求文件的信息")
	}
	file := response.Files[0]
	file.Path, err = validateDSMPath(file.Path, root)
	if err != nil || file.Path != remotePath || file.IsDir || path.Base(file.Path) != file.Name {
		return dsmFile{}, errors.New("DSM 返回的文件信息与请求不一致")
	}
	return file, nil
}

func (c *dsmClient) checkRoot(ctx context.Context, root string) error {
	remotePath, err := validateDSMPath(root, root)
	if err != nil {
		return err
	}
	data, err := c.call(ctx, dsmListAPI, "getinfo", map[string]any{
		"path": []string{remotePath}, "additional": []string{"real_path"},
	}, true)
	if err != nil {
		return err
	}
	var response struct {
		Files []dsmFile `json:"files"`
	}
	if err = json.Unmarshal(mustRaw(data["files"]), &response.Files); err != nil || len(response.Files) != 1 || !response.Files[0].IsDir || response.Files[0].Path != root {
		return errors.New("DSM 账户无法读取所选的 File Station 目录")
	}
	return nil
}

func (c *dsmClient) download(ctx context.Context, remotePath, root string) (dsmFile, io.ReadCloser, error) {
	file, err := c.fileInfo(ctx, remotePath, root)
	if err != nil {
		return dsmFile{}, nil, err
	}
	info, _, err := c.endpoint(dsmDownloadAPI)
	if err != nil {
		return dsmFile{}, nil, err
	}
	fields := url.Values{
		"api":     {dsmDownloadAPI},
		"version": {"2"},
		"method":  {"download"},
		"path":    {mustJSON([]string{file.Path})},
		"mode":    {"download"},
	}
	fields.Set("_sid", c.sid)
	if c.synoToken != "" {
		fields.Set("SynoToken", c.synoToken)
	}
	requestURL := c.baseURL + "/webapi/" + info.Path
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, requestURL, strings.NewReader(fields.Encode()))
	if err != nil {
		return dsmFile{}, nil, errors.New("无法构造 DSM 下载请求")
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("User-Agent", "SynoFind-DSM-Suite/0.2.0")
	req.Header.Set("Cookie", "id="+c.sid)
	if c.synoToken != "" {
		req.Header.Set("X-SYNO-TOKEN", c.synoToken)
	}
	response, err := c.http.Do(req)
	if err != nil {
		return dsmFile{}, nil, errors.New("DSM 文件下载失败，请检查 NAS 连接和读取权限")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		response.Body.Close()
		if response.StatusCode >= 300 && response.StatusCode < 400 {
			return dsmFile{}, nil, errors.New("DSM 下载请求返回了重定向")
		}
		return dsmFile{}, nil, fmt.Errorf("DSM 下载请求返回 HTTP %d", response.StatusCode)
	}
	contentType := strings.ToLower(strings.TrimSpace(strings.Split(response.Header.Get("Content-Type"), ";")[0]))
	if strings.Contains(contentType, "json") && response.Header.Get("Content-Disposition") == "" {
		prefix, readErr := io.ReadAll(io.LimitReader(response.Body, 65537))
		if readErr != nil {
			response.Body.Close()
			return dsmFile{}, nil, errors.New("无法读取 DSM 下载响应")
		}
		if len(prefix) <= 65536 {
			var reply dsmReply
			if json.Unmarshal(prefix, &reply) == nil && !reply.Success {
				response.Body.Close()
				return dsmFile{}, nil, fmt.Errorf("DSM 拒绝了文件下载（错误代码 %d）", reply.Error.Code)
			}
		}
		response.Body = dsmPrefixedReadCloser{Reader: io.MultiReader(bytes.NewReader(prefix), response.Body), Closer: response.Body}
	}
	return file, response.Body, nil
}

func mustJSON(value any) string {
	b, _ := json.Marshal(value)
	return string(b)
}
