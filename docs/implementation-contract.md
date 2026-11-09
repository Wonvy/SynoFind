# Internal implementation contract

Personal Synology native SPK. Go backend; React+TS frontend embedded from `internal/webui/dist`. Public API base `/api/v1`. All timestamps RFC3339 UTC. Errors `{ "error": { "code": "...", "message": "中文可读信息" } }`.

## Frontend contract
- `POST /auth/login` JSON `{password}` -> `{authenticated:true,csrf_token}`; session cookie HttpOnly SameSite=Strict. `GET /auth/session` returns same or 401. `POST /auth/logout` returns 204.
- Cookie mutations send `X-CSRF-Token` from session. Login exempt. Bearer API tokens are for search/file/index only, not settings/auth/token management.
- `GET /search?q=&ext=&min_size=&max_size=&modified_after=&modified_before=&page=1&limit=50&sort=name&order=asc` -> `{items:File[],total,page,limit}`. ext comma separated without dots; dates RFC3339; sort name/modified/size. Words are AND literal case-insensitive substrings of relative path. max limit 200.
- File: `{id,name,path,size,modified_at,extension,preview_type,smb_path}`. path is root-relative with `/`; preview_type `image`/`pdf`/`text`/empty. Download `GET /files/{id}/content`; preview `/files/{id}/preview`.
- `GET /index/status` -> `{running,total_files,scanned_files,started_at,last_completed_at,last_duration_ms,last_error,errors,root_path}` (empty timestamp strings allowed, errors array of strings).
- `POST /index/scan` -> 202 `{started:true}`; 409 if active.
- `GET /settings` -> `{root_path,smb_root,scan_interval_minutes,port}`. `PUT /settings` accepts same except port ignored/read-only. Invalid/unreadable root rejected with 400. Root changes reset file index; not permitted during active scan (409).
- `GET /tokens` -> `{items:[{id,name,created_at,last_used_at}]}`; `POST /tokens` JSON `{name}` -> `{id,name,token,created_at}` token revealed once; `DELETE /tokens/{id}` ->204.
- `GET /health` minimal public liveness. `/api/openapi.json` public contract.
- No self registration or open web bootstrap; initial password supplied by installer/CLI.

## CLI / package contract
Executable `synofind`. Commands:
`synofind init --data-dir PATH --port 8787 --root /volume1/Wonvy --smb-root \\10.0.0.10\Wonvy`: reads initial password from stdin (one line), requires >=12 chars, does nothing to credentials if initialized. No secrets in argv/logs. Writes config.json plus index.db with restrictive modes.
`synofind serve --data-dir PATH [--listen 127.0.0.1:8787]`: default listens `:config.port`; graceful SIGINT/SIGTERM. Automatically begins scan then interval scheduler, single scanner.
`synofind version`: reports version.
Go root module `synofind`; build `./cmd/synofind` with `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOAMD64=v1`, ldflags `-X main.version=0.1.0`.
SPK binary payload `bin/synofind`; package id `synofind`; default data `$SYNOPKG_PKGVAR`; config/store persisted there. No root service; source shared-folder ACL manually granted to package account. UI port default8787. Initial install uses wizard password/port, upgrades preserve credentials/config. No Docker/Node/Python runtime on NAS. Source data never written.

Search backend is modular: local index implementation first; evaluating whether a supported DSM native index adapter is feasible. Do not claim built-in integration is implemented.
