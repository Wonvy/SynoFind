# SynoFind API v1

SynoFind provides a read-only HTTP API over its own web interface. Search requests call the configured Synology File Station API; this service does not build or keep a local file index. The API matches file names and metadata, not parent paths or file contents.

Base path: `/api/v1`. Responses use UTF-8 JSON unless returning file bytes.

## Authentication

- `POST /auth/login` accepts `{"password":"..."}` using the independent SynoFind web password.
- Browser sessions use an `HttpOnly` cookie and a CSRF token for state-changing requests.
- API tokens are sent as `Authorization: Bearer TOKEN`. They permit search, metadata, download, preview, and DSM search status. Settings and token management require the web session and CSRF protection.
- DSM account credentials are configured only in the web settings. They are not accepted by search requests and the password is never returned by the settings read API.

## Search

`GET /search` accepts `q`, `ext`, `min_size`, `max_size`, `modified_after`, `modified_before`, `page`, `limit`, `sort`, and `order`.

- `q` is passed to DSM `SYNO.FileStation.Search` as a case-insensitive filename pattern. DSM accepts `*` and `?` wildcards and automatically adds substring wildcards when the query has none.
- `ext` is a comma-separated extension filter, such as `pdf,docx`.
- Size filters are bytes. Date filters use RFC3339 timestamps.
- `page` is one-based; `limit` is 1–200. `sort` is `name`, `modified`, or `size`; `order` is `asc` or `desc`.
- Search is asynchronous on DSM. SynoFind waits for completion, fetches the requested page, then cleans up the temporary DSM search result.

Example:

```http
GET /api/v1/search?q=合同&limit=20
Authorization: Bearer TOKEN
```

Results contain a root-relative `path`, name, size, modification time, extension, preview type, and signed file ID. File IDs are scoped to the configured File Station directory and are not durable across a SynoFind password change.

## File operations

- `GET /files/{id}` rechecks metadata through File Station.
- `GET /files/{id}/content` streams the file through File Station.Download.
- `GET /files/{id}/preview` streams supported images, PDFs, and small text files inline. Unsupported formats return `415`; text files above 2 MiB return `413`.
- File Station permissions remain the access boundary. SynoFind also restricts file IDs to the configured root path.

Downloads do not promise HTTP Range support or resumable transfers.

## Status and settings

- `GET /index/status` reports the DSM connection state, last error, most recent search time, and configured root. The endpoint does not report or manage a SynoFind index.
- `GET /settings` returns `root_path`, `smb_root`, `dsm_url`, `dsm_username`, `has_dsm_password`, and `port`. The password itself is never returned.
- `PUT /settings` accepts those writable fields plus optional `dsm_password`. An empty password retains the saved password for the same DSM username. The request tests DSM login and read access to the selected File Station path before saving.
- `POST /tokens` creates a bearer token and reveals it once. `GET /tokens` lists token metadata; `DELETE /tokens/{id}` revokes one immediately.

Use HTTPS for remote DSM API URLs. Plain HTTP is accepted only for loopback URLs. Treat SynoFind API tokens and DSM credentials as secrets; never put tokens in URLs or commit credentials to source control.
