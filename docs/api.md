# SynoFind API v1

> **历史版本说明：** 本文描述 SynoFind 0.1.0 独立索引服务的 `/api/v1`，不属于 DSM 官方 API。当前主路线为 [Python 官方 API 直连客户端](../clients/python/README.md)，使用 DSM 登录和 File Station API，不使用本文的令牌或 `8787` 服务。本文保留供旧套件参考。

默认根地址为 `http://10.0.0.10:8787`，接口前缀 `/api/v1`。替换为自己的局域网地址和安装端口。时间均为 UTC RFC3339，大小均为字节。

完整机器可读约定：[openapi.json](openapi.json)，运行服务上为 `GET /api/openapi.json`。

## 身份验证

在网页设置页创建令牌，然后为请求添加：

```http
Authorization: Bearer YOUR_TOKEN
```

令牌只允许搜索、文件信息、下载、预览、查看索引及手动扫描。不允许修改设置或管理令牌。不要把令牌放进 URL，也不要把真实令牌提交到代码仓库。

网页使用独立的 HttpOnly 会话 Cookie。登录请求为 `POST /api/v1/auth/login`，JSON `{ "password": "..." }`；会话响应包含 `csrf_token`。网页写操作需要 `X-CSRF-Token` 请求头。脚本推荐使用 Bearer Token，无需模拟网页登录。

## 搜索

```http
GET /api/v1/search?q=合同%202026&ext=pdf,docx&page=1&limit=50&sort=modified&order=desc
```

| 参数 | 含义 |
|---|---|
| `q` | 名称/相对路径的文字片段；空格分隔词按“全部匹配”处理，忽略大小写，不是正则表达式。 |
| `ext` | 不带点的扩展名，逗号分隔，例如 `pdf,docx`。 |
| `min_size` / `max_size` | 最小/最大文件大小，非负字节数。 |
| `modified_after` / `modified_before` | RFC3339 时间边界，例如 `2026-01-01T00:00:00Z`。 |
| `page` | 从 1 开始，默认 1。 |
| `limit` | 默认 50，最多 200。 |
| `sort` | `name`、`modified` 或 `size`，默认 `name`。 |
| `order` | `asc` 或 `desc`，默认 `asc`。 |

返回 `{items, total, page, limit}`，每个 `items` 元素包含：

```json
{
  "id": "opaque-file-id",
  "name": "合同.pdf",
  "path": "项目/合同.pdf",
  "size": 1024,
  "modified_at": "2026-01-01T00:00:00Z",
  "extension": "pdf",
  "preview_type": "pdf",
  "smb_path": "\\\\10.0.0.10\\Wonvy\\项目\\合同.pdf"
}
```

`id` 是不透明标识，不要从路径自行推算；文件移动/删除或切换资料目录后，应重新搜索。索引结果反映上次成功扫描状态，获取文件时还会核实原文件。

## 文件读取

| 接口 | 结果 |
|---|---|
| `GET /api/v1/files/{id}` | 单个文件的信息对象。 |
| `GET /api/v1/files/{id}/content` | 原始字节流，下载文件。 |
| `GET /api/v1/files/{id}/preview` | 支持格式的内联预览；图片、PDF、纯文本。 |

下载响应支持 HTTP `Range`；例如 `Range: bytes=0-1023` 请求第一段字节。有效范围返回 `206`，无效范围返回 `416`。下载前后原文件可能改变；如需严格一致副本，应在来源文件稳定时读取并自行校验。

只可访问配置资料目录中的普通文件。不会因传入绝对路径而读取系统文件；HTML/SVG 等主动内容不作为网页预览执行。纯文本预览限 2 MiB，超限返回 `413`，仍可下载完整文件。

## 索引与设置

- `GET /api/v1/index/status`：`running`、`total_files`、`scanned_files`、`started_at`、`last_completed_at`、`last_duration_ms`、`last_error`、`errors`、`root_path`。尚未发生的时间字段可为空字符串。
- `POST /api/v1/index/scan`：请求一次扫描，成功返回 `202 {"started":true}`；扫描已在进行时返回 `409`。
- `GET /api/v1/settings`：网页会话可读取资料目录、SMB 目录、扫描间隔和端口。
- `PUT /api/v1/settings`：网页会话可保存 `root_path`、`smb_root`、`scan_interval_minutes`。端口由安装配置管理。目录必须存在且可读；扫描中不可切换。切换资料目录会重建该服务的索引。
- `GET /api/v1/tokens`、`POST /api/v1/tokens`、`DELETE /api/v1/tokens/{id}`：网页会话管理令牌；创建请求为 `{ "name": "本地脚本" }`，返回中的 `token` 仅此一次展示。
- `GET /api/v1/health`：不含个人资料的公开存活检查。

## 错误

JSON 错误使用如下结构；文件 Range 协议错误可采用标准 HTTP 响应：

```json
{ "error": { "code": "error_code", "message": "可读的错误说明" } }
```

常见状态码：`400` 参数/设置无效，`401` 未登录或令牌无效，`403` 权限/CSRF 检查失败，`404` 文件不存在，`409` 操作与当前状态冲突，`416` 范围无效，`429` 请求受限，`500` 服务内部错误。

## Python 调用

[fetch_file.py](../examples/fetch_file.py) 仅依赖 Python 标准库，先搜索或列出结果，再用指定 ID 流式下载。它不自动下载模糊搜索的第一条，也不自动覆盖本地已有文件。

```powershell
$env:SYNOFIND_URL = 'http://10.0.0.10:8787'
# 在自己的终端设置令牌；不要把真实值粘贴到共享日志。
$env:SYNOFIND_TOKEN = '<你的令牌>'
py examples/fetch_file.py search '合同'
py examples/fetch_file.py download '<搜索返回的文件 ID>' --output 'D:\Downloads\合同.pdf'
```

更改源地址为 HTTPS 时，Python 使用系统/默认可信 CA 校验证书，不提供跳过证书验证开关。
