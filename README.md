# SynoFind

通过群晖官方 File Station API，在 Windows 本机网页中搜索、预览 NAS 文件，也可用 API 或命令行获取文件。

当前版本 **0.5.0** 不预设任何 NAS 地址、账户或共享目录。解压后双击 `SynoFind.exe`，填写自己的 NAS 地址和账户，登录后从官方 API 返回的共享列表中选择文件夹，再开始使用。电脑无需安装 Python，NAS 无需安装 SynoFind 套件。

## 开始使用

1. 解压 `outputs/SynoFind-Windows-0.5.0.zip`，双击 `SynoFind.exe`。
2. 浏览器打开 `http://127.0.0.1:8788`，网页仅供当前电脑访问。
3. 填写自己的 DSM HTTPS 地址、账户和密码，登录并读取该账户可见的共享文件夹。
4. 明确选择一个共享文件夹后保存，不会自动选中第一个共享或某个固定名称。
5. 如需切换共享文件夹，在连接设置中重新获取列表并选择。

建议使用有 File Station 应用权限和目标目录读取权限的独立账户。选择记住账户后，凭据以 Windows DPAPI 当前用户加密保存；一次性验证码不保存。已有版本的已保存配置继续保留，无需为升级删除数据目录。

默认使用网页预览。也可配置共享目录对应的 Windows 本地盘符或 UNC 路径，使用已安装的 QuickLook 或 Seer；不自动安装预览软件或映射网络驱动器。详细说明见 [Windows 使用说明](clients/python/DESKTOP.md)。

使用后点击「退出应用」，或在运行窗口按 `Ctrl+C`。只关闭浏览器标签页不会停止程序。没有 Windows 开机自启，也不会对局域网开放本机网页端口。

## Python 与其他工具

[Python 客户端和命令行](clients/python/README.md)仍可独立使用。以下地址、账户和目录都是示例，必须替换为自己的值：

```powershell
py -3 -m synofind_dsm --url https://nas.example.com:5001 info
py -3 -m synofind_dsm --url https://nas.example.com:5001 --user reader shares
py -3 -m synofind_dsm --url https://nas.example.com:5001 --user reader --root /Documents search '合同'
```

所有命令都需要 `--url` 或 `SYNOLOGY_URL`。`info` 和 `shares` 不需要共享目录；搜索、列目录、文件信息和下载需要 `--root` 或 `SYNOLOGY_ROOT`，防止无意间访问错误范围。

其他语言或 HTTP 工具可以直接按[群晖官方 File Station API 文档](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf)接入，无需依赖本项目。会话仅在内存中通过请求头和 POST 参数传递，不写入 URL 或磁盘。

## 搜索边界

搜索由 NAS 官方异步任务处理。SynoFind 不自建索引、不定时扫描文件、不提取正文；这不代表 NAS 处理搜索时完全免扫描，也不承诺复用 Universal Search 索引或秒级返回。当前按文件名搜索，不做正文、OCR、拼音或父目录路径全文搜索。

下载不承诺 HTTP Range 断点续传。本工具不提供 NAS 文件上传、修改或删除。

0.5.0 已在一台真实 NAS 上验证共享列表、明确选择共享、搜索、映射盘文本预览、文件资源管理器定位，以及更新 EXE 后保留映射和 DPAPI 自动登录。QuickLook/Seer 通过官方协议核对与模拟测试，尚未安装进行真实软件预览验收；结果不代表所有设备或文件格式均已验证。完整旧版交付继续存档。

## 开发与构建

服务客户端与本机网页入口位于 `clients/python/synofind_dsm/`，静态网页位于 `desktop_web/`，测试位于 `clients/python/tests/`，调用示例为 `examples/direct_search.py`。

```powershell
cd clients/python
py -3 -m unittest discover -s tests -v
py -3 -m synofind_dsm --help
py -3 -m synofind_dsm.desktop
```

Windows x64 构建需要 Python 3.12 和官方 PyInstaller，在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_desktop.ps1
```

构建中间文件在 `work/desktop-build`，交付 ZIP、源码与校验文件在 `outputs`。打包排除本机配置、凭据、缓存与虚拟环境。Python 包提供 `synofind-desktop` 和 `synofind-dsm` 两个入口。

## 历史版本

旧的 0.1.0 `.spk` 独立索引方案和源码保留在项目中，当前应用不需要它运行。旧 [安装说明](docs/install.md)、[API v1 文档](docs/api.md)仅对应历史套件。若要停止旧扫描，请手动在 DSM 套件中心停止它；新应用不会自动卸载或修改旧套件。

本项目为第三方工具，与 Synology 官方产品无隶属关系。

0.5.0 Windows 版新增本机文件夹数据源与 NAS 合并搜索、本机输入即时查询、搜索停止与替换取消、悬停大预览和亮暗主题。使用方法见 `clients/python/DESKTOP.md`。
