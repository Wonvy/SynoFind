# SynoFind DSM API 客户端 0.5.0

通过群晖官方 File Station API 列出共享、搜索文件名和下载文件。客户端运行在你的电脑上，不需要在 NAS 安装 SynoFind，也不建立独立索引。

需要 Python 3.10 或更新版本，无第三方运行依赖。所有地址、账户和共享名称均由你提供，不预置个人 NAS。

## 上手：先列共享，再选择范围

解压 `SynoFind-DSM-API-Client-0.5.0.zip`，进入包含 `synofind_dsm/` 的目录。将下面的示例地址和用户名替换成自己的值：

```powershell
# 查询公开 API，不登录
py -3 -m synofind_dsm --url https://nas.example.com:5001 info

# 登录后列出账户可见的共享，无需指定目录
py -3 -m synofind_dsm --url https://nas.example.com:5001 --user reader shares

# 假设列表中包含 /Documents，由你明确选定后搜索
py -3 -m synofind_dsm --url https://nas.example.com:5001 --user reader --root /Documents search '合同'
```

`shares` 不自动选择第一项，也不要求预先知道共享名称。除 `info`、`shares` 外，文件操作必须通过 `--root` 或 `SYNOLOGY_ROOT` 明确限定范围。

可从解压目录直接运行；也可安装到虚拟环境，以便使用 `synofind-dsm` 命令：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\synofind-dsm.exe --help
```

安装时可能下载 setuptools 构建工具，客户端本身无第三方运行依赖。Windows 本机网页的用法见 `DESKTOP.md`。

## 参数与认证

全局参数放在子命令之前。

| 参数 | 用途 |
| --- | --- |
| `--url` / `SYNOLOGY_URL` | 必须提供自己的 DSM 基础地址，无默认值 |
| `--user` / `SYNOLOGY_USERNAME` | DSM 用户名 |
| `--root` / `SYNOLOGY_ROOT` | 文件操作必须提供的共享路径，无默认值；`info` 和 `shares` 不要求 |
| `--ca-file` | 指定受信任的 PEM CA/证书集合，继续验证主机名和有效期 |
| `--cert-sha256` | 显式固定已通过可信渠道核对的服务器证书指纹，与 `--ca-file` 互斥 |
| `--timeout` | 单次 HTTP 请求超时秒数 |
| `--otp-prompt` | 在终端隐藏输入一次性验证码 |
| `--allow-http` | 明确允许明文 HTTP，不能代替修复 HTTPS 证书 |

默认通过隐藏输入获取密码；非交互运行可由安全运行环境注入 `SYNOLOGY_PASSWORD`。二次验证码可用 `--otp-prompt` 或 `SYNOLOGY_OTP_CODE`。没有命令行密码参数，不将密码或验证码保存到配置文件。

会话只存在当前进程内，通过 `Cookie: id=...`、可用时的 `X-SYNO-TOKEN` 请求头和 POST 参数传递，不写入 URL 或磁盘，调用结束后注销。CLI 与 Windows 网页的 DPAPI 保存账户是两种独立使用方式。

## HTTPS 证书

默认验证信任链、有效期和访问主机名。应使用证书匹配的 NAS 地址。私有 CA 或自签名证书可通过可信渠道导出并核对后，用 `--ca-file` 指定；它不会忽略主机名不匹配或过期。

```powershell
py -3 -m synofind_dsm --url https://nas.example.com:5001 --ca-file .\nas-ca.pem info
```

如需使用 IP 地址，可显式固定已经核对的服务器证书 DER SHA-256：

```powershell
py -3 -c "import hashlib,pathlib,ssl; print(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pathlib.Path('nas-server.pem').read_text(encoding='ascii'))).hexdigest())"
$taskNasCertSha256 = '替换为已通过可信渠道核对的64位十六进制指纹'
py -3 -m synofind_dsm --url https://192.0.2.10:5001 --cert-sha256 $taskNasCertSha256 info
```

`192.0.2.10` 是文档示例地址，必须替换。指纹来源应是已确认身份的 DSM 导出证书或其他可信渠道，不能把一次未经验证的网络连接返回的指纹直接当作可信值。

指纹模式依靠精确证书匹配替代 CA/主机名/有效期检查，TLS 握手后、发送任何 HTTP 请求或凭据前比对；失配立即关闭连接。证书更新后需重新核对。它不修改浏览器信任设置，也不修复 NAS 证书。没有无条件跳过证书校验的选项。

## 文件命令

以下环境变量仅设置地址、用户名与选定范围，不含密码。`/Documents` 是你从 `shares` 结果中选择的示例路径，应换成自己的共享：

```powershell
$env:SYNOLOGY_URL = 'https://nas.example.com:5001'
$env:SYNOLOGY_USERNAME = 'reader'
$env:SYNOLOGY_ROOT = '/Documents'

py -3 -m synofind_dsm search '合同' --extension pdf --limit 100 --offset 0
py -3 -m synofind_dsm list
py -3 -m synofind_dsm stat '/Documents/资料/采购 合同.pdf'
py -3 -m synofind_dsm download '/Documents/资料/采购 合同.pdf' '.\采购合同.pdf'
```

下载默认不覆盖本地文件，明确需要时添加 `--overwrite`。文件路径是 File Station 路径，如 `/Documents/...`，不是 NAS 系统路径 `/volume1/Documents/...` 或 Windows UNC 路径。选定范围不能提升 DSM 账户本身的权限。

## Python 调用与示例

```python
from getpass import getpass
from synofind_dsm import DSMClient

url = input('DSM HTTPS 地址：').strip()
username = input('DSM 用户名：').strip()
password = getpass('DSM 密码：')

# 未选范围时只列出共享；不会替你选择其中某个文件夹。
with DSMClient(url, username=username, password=password) as client:
    print(client.list_shares())

selected_root = input('从列表中选择共享完整路径：').strip()
with DSMClient(url, username=username, password=password, allowed_root=selected_root) as client:
    result = client.search('合同', limit=20)
    for file in result.files:
        print(file['path'])
    # 选定具体文件后再显式下载：
    # client.download('/Documents/选定文件.pdf', '选定文件.pdf')
```

完整示例位于分发包 `examples/direct_search.py`（源码仓库根目录也有该文件）：

```powershell
$env:SYNOLOGY_URL = 'https://nas.example.com:5001'
$env:SYNOLOGY_USERNAME = 'reader'
py -3 -m examples.direct_search
```

未设置 `SYNOLOGY_ROOT` 时，示例只显示共享列表。选定共享并设置该变量后，重新执行才搜索；不会自动使用第一项。下载还需同时设置 `SYNOLOGY_DOWNLOAD_PATH` 和 `SYNOLOGY_DOWNLOAD_TO`。示例支持 `SYNOLOGY_CA_FILE`、`SYNOLOGY_CERT_SHA256`（二者互斥）及 `SYNOLOGY_OTP_CODE`。

## 范围与验证

采用官方搜索的启动、读取和清理任务流程。文件名模式可用 `*合同*` 或 `?`；多个模式按 DSM 规则解释，不把父目录路径或正文作为搜索内容。不承诺完全免扫描、秒级返回或下载断点续传。[群晖官方 API 文档](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf)。

其他编程语言或 HTTP 工具可直接按官方文档接入，无需依赖本库。

0.5.0 已在一台真实 NAS 上验证共享枚举、明确选择共享、搜索及桌面映射盘预览与文件定位；重启后保留已保存的映射和 DPAPI 自动登录。旧版 API 下载验证与交付已存档；结果不代表所有 NAS、账户权限组合和文件规模均已验收。

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m synofind_dsm --help
```

旧 `.spk` 索引套件不是本客户端的依赖，不会被自动停止或卸载。
