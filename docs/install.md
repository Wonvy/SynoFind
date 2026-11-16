# SynoFind DSM 套件安装说明

SynoFind DSM 套件使用群晖公开的 File Station API 搜索文件名和属性。套件不遍历共享目录、不建立本地文件索引，也不提供全文搜索。

## 安装和首次设置

1. 在 DSM「套件中心 → 手动安装」选择 SynoFind `.spk` 并启动。
2. 安装向导中设置 SynoFind 网页登录密码和服务端口。这个密码与 DSM 账户密码相互独立。
3. 在「控制面板 → 用户与群组」创建或选择一个 DSM 只读账户，为其启用 File Station 应用权限，并仅授予目标共享文件夹的读取权限。
4. 从套件中心打开 SynoFind，进入「设置与 API」，填写 DSM API 地址、DSM 用户名和密码，以及 File Station 目录路径。
5. 默认 DSM API 地址为 `http://127.0.0.1:5000`。如果 DSM 使用自定义端口，请修改地址。远程 DSM 地址必须使用 HTTPS，并由系统信任其证书。
6. File Station 路径从共享文件夹开始，例如 `/Wonvy` 或 `/Wonvy/资料`；不要填写系统路径 `/volume1/Wonvy`。
7. 点击「测试连接并保存」。SynoFind 会验证登录和目录读取权限，通过后即可搜索。

Windows 共享路径可选填，只影响「复制共享路径」功能。

DSM 密码以明文保存在套件私有数据目录的 `config.json` 中，文件权限为 `0600`，仅套件账户可读写；设置读取接口不会返回密码。建议使用单独的只读账户，不要填写 DSM 管理员账户。

## 搜索和文件读取

- 搜索仅匹配文件名，不查找父目录路径或文件正文。
- 搜索范围及权限由设置的 File Station 路径和 DSM 只读账户共同决定。
- DSM 执行异步搜索；SynoFind 获取所需分页后会停止或清理群晖搜索任务。
- 文件信息、下载和预览也通过该 DSM 账户调用 File Station API。
- DSM API 的搜索时延、文件名匹配细节和权限错误以当前 DSM 版本的响应为准。
- 这里不需要手动刷新或安排扫描。Universal Search 是否配置文件名索引由 DSM 自己管理；File Station.Search 的公开文档没有承诺每次请求都直接复用 Universal Search 的索引。

SynoFind 网页服务默认通过 HTTP 提供，仅适合可信局域网。不要将网页端口直接映射到公网。远程 DSM API 连接必须使用 HTTPS；本机默认地址通过回环接口访问。

## 升级、停止和卸载

- 套件中心的停止和启动操作会停止或启动网页服务，不会启动 SynoFind 自建扫描。
- 从 0.1.0 升级时，套件会移除旧版本地文件索引表，保留 SynoFind 网页密码、API 令牌和设置；首次使用新版请在设置里添加 DSM API 只读账户。
- DSM 文件路径会从旧格式 `/volumeN/共享名/...` 转换为 File Station 格式 `/共享名/...`。
- 卸载默认保留套件数据目录，其中包含网页密码哈希、API 令牌和 DSM 账户配置。需要完全移除时，请在 DSM 套件卸载选项中选择删除套件数据。
- 原始共享文件始终由 DSM 管理，SynoFind 不修改文件。

## 故障定位

| 现象 | 检查内容 |
|---|---|
| 登录失败或 File Station API 不可用 | 检查 DSM 地址和端口、账号密码、File Station 应用权限；支持 DSM API 的账户必须能登录 File Station。 |
| 找不到指定目录 | 确认填写的是 `/共享文件夹/子目录` 格式，并给 DSM 账户授予只读权限。 |
| HTTPS 连接失败 | 检查证书是否受系统信任、地址是否与证书匹配。 |
| 搜索结果不完整或等待较久 | 检查 DSM File Station 的搜索结果，并缩小 SynoFind 的搜索目录或增加筛选条件。 |
| 文件移动或删除后仍有旧页面 | 重新执行搜索；下载和预览会重新向 DSM 请求文件。 |
| 套件启动失败 | 查看 DSM 套件日志、服务日志和端口占用情况。 |

### 官方参考

- [Synology File Station 官方 API 文档](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf)
- [DSM 登录 Web API 指南](https://kb.synology.com/en-global/DG/DSM_Login_Web_API_Guide)
