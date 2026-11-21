# 更新与自动更新

桌面、CLI 与安卓客户端都会检查新版本。三者共用同一份**更新清单**，检查逻辑和校验规则一致，只是安装方式不同。

## 检查频率

| 端 | 时机 | 频率上限 |
| --- | --- | --- |
| 桌面 | 启动后由前端发起 | 每 24 小时一次 |
| CLI | 任意命令结束后静默提示（仅 TTY、非 `--json`） | 每 24 小时一次 |
| 安卓 | 启动时 | 每 24 小时一次 |

检查结果会被缓存，24 小时内重复启动不会再次联网。桌面端在请求发出前就写入 `last_update_check`，因此离线时也不会反复重试；详见[桌面端更新提示](#desktop-update-notice)。

## 关闭检查

```toml
[general]
update_check = "auto"   # 默认：命令结束后静默检查
# update_check = "manual"  # 只在运行 novel-cli update 时检查
# update_check = "off"     # 从不检查
```

桌面端另有「自动检查更新」开关，安卓端在设置页有同名开关。

## 手动检查

```bash
novel-cli update --check   # 只检查，不下载
novel-cli update           # 检查并询问是否下载
novel-cli update --yes     # 直接下载并校验（不会执行或安装）
novel-cli update --json    # 机器可读输出
```

`--yes` 只把文件下载到应用数据目录（`<app_home>/updates/`）并校验，**不会**替你安装。`novel-cli update` 显式请求 `shape="cli"`，只会下载 `novel-cli-<version>-<platform>.zip`，不会下载或运行桌面安装器；发布里没有匹配的 CLI 资产时直接停止（`--json --yes` 的 `error` 为 `no-cli-asset`），不会回退到 `NovelDownloader-setup-*.exe`。

安装 CLI：关闭正在运行的 CLI，解压该 zip 并用其中的文件替换已安装的 CLI 目录（Windows 独立版通常为 `%LOCALAPPDATA%\Programs\novel-downloader\novel-cli\`）。桌面端的安装版/便携版由下面的自动更新流程处理。

## 桌面端自动更新

桌面端默认每 24 小时检查一次；发现新版本后在后台自动下载，侧栏显示字节级进度。下载使用 HTTP `Range` 复用 `.part` 文件，网络中断后从断点继续，失败最多重试 3 次。

- **安装版**：退出应用时启动已校验的 Inno Setup 安装器（静默、无向导），原地覆盖安装目录；静默安装结束后安装器会直接启动新版本，无需手动打开。
- **便携版**：下载的是 zip；退出后由 PowerShell 辅助脚本等待进程结束，解压到 `%TEMP%`，删除当前文件夹中的 `_internal` 后再覆盖并重新启动，因此已移除的插件不会残留，`app_home` 下的设置与下载不受影响；不会另装一份到 `%LOCALAPPDATA%`。
- 更新目录中的 `.part` 超过 1 天、完整安装包超过 7 天会被清理。

## 桌面端更新提示：「稍后」与「跳过此版本」 {#desktop-update-notice}

更新提示条右侧的两个按钮都不是「永久关闭所有更新」，区别在于是否写入偏好文件：

| 按钮 | 作用范围 | 何时恢复提示 |
| --- | --- | --- |
| **稍后 (Later)** | 仅当前页面实例的内存状态，不写偏好文件 | 重启应用、重新加载页面，或在同一会话中检测到另一个版本时 |
| **跳过此版本 (Skip this version)** | 持久化到偏好文件，只针对**这一个**版本 | 发布更新的版本后自动恢复，无需手动解除 |

「跳过此版本」调用 `skip_update_version`，把规范化后的版本号（去掉 `v` 前缀与首尾空白）写入 `desktop_prefs.json` 的 `skipped_update_version` 键。之后再检查到同一版本时，`available` 为 `false`，提示条不再出现，**后台自动下载也会跳过它**——不为用户明确不想装的版本消耗带宽。等下一个版本发布，缓存的 `latest` 与存储值不再相等，提示条自动恢复；旧的 `skipped_update_version` 仍留在文件里但不再生效，不需要手动清空。

### 自动检查与手动检查

- 桌面端启动后由前端调用 `check_update_async`，默认每 24 小时最多联网一次（`_UPDATE_CHECK_INTERVAL`）；检查结果会缓存，24 小时内重启应用直接复用缓存。
- 每次**真正发请求之前**先把当前时间写入偏好键 `last_update_check`，请求超时 8 秒。因此离线或 GitHub 不可达时，这次失败也会被记录，不会在每次打开面板时反复重试；想立刻重试请用「立即检查」。
- 关闭设置页的「自动检查更新」后，启动检查与非强制检查都不再发起（`update_state` 不会启动新检查，`check_update_async` 返回 `reason: "disabled"`）；但设置页的 **立即检查 (Check now)** 仍然可用——它强制跳过 24 小时节流和该开关。
- 手动检查的入口是**设置 → 更新 (Updates) → 立即检查**。

### 跳过版本的存储与清除

- 文件：`desktop_prefs.json`，默认位于音乐数据目录（Windows 通常为 `%LOCALAPPDATA%\novel_downloader\music\desktop_prefs.json`；设置 `NOVEL_MUSIC_HOME` 时以该目录为准）。
- 键：`skipped_update_version`，值为规范化后的版本号（例如 `1.42.0`）。
- 设置页目前**没有**单独的「取消跳过」按钮；「设置导出 / 导入」只覆盖六个白名单键，也**不会**清除这个键。要提前解除，先退出应用，用文本编辑器删除 `skipped_update_version`（或把它改成另一个版本号），保存后重新打开应用并点 **立即检查**。直接删除整个 `desktop_prefs.json` 会同时重置主题、更新开关等全部桌面偏好。

### 已知限制

- **不中断已开始的下载**：点击 Skip 只影响之后的提示和自动下载。如果后台传输已经开始，它会继续完成并留在更新目录中；已经下载好的安装包也不会被删除，若开启了「退出时安装」，退出应用时仍可能启动它。
- **旧版检查不展示错误详情**：当前端只能走旧的阻塞式 `check_update`（后端没有 `check_update_async`）时，页面只在 `available` 为真时展示提示条，离线、限流或清单解析失败的 `error` 不会出现在界面上，表现为静默无提示；新路径的失败会记录在 `update_state` 的 `error` 字段中，排查时请查看日志。

## 校验规则

更新链路只接受满足以下条件的包：

1. 清单里的下载地址必须是 **HTTPS**；
2. 必须提供 **SHA-256**，缺失则不下载（宁可让用户手动更新，也不装一个无法验证的包）；
3. 下载完成后校验一次，**启动安装器前再校验一次**（防止文件在两次之间被替换）；
4. 校验失败的文件会被删除，不会留下半成品。

桌面端的安装器由 Inno Setup 生成，进程退出后才会替换文件；安卓端由系统安装器负责，应用本身无法静默安装。

## 更新清单

每个 release 附带一份 `latest.json`：

```
https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
```

它列出每个平台资产的确切文件名、大小、SHA-256 与下载地址，并由发布脚本根据**实际上传的资产**生成。客户端优先读它，读不到才回退 GitHub API——API 未认证时每小时只有 60 次配额，清单没有这个限制。

> 注意：如果配置了可信签名公钥，客户端**不再回退** API——清单缺失或验签失败会直接报错。这是刻意的：允许回退等于让攻击者删掉清单来绕过验签。启用签名后，每次发布都必须带上 `latest.json`。

清单里按**包形态**区分条目：

| 键 | 对应产物 |
| --- | --- |
| `windows-x64-installer` | `NovelDownloader-setup-<ver>-x64.exe` |
| `windows-x64-portable` | `NovelDownloader-<ver>-windows-x64.zip` |
| `windows-x64-cli` | `novel-cli-<ver>-windows-x64.zip` |
| `windows-x64-api` | `novel-api-<ver>-windows-x64.zip` |
| `android` | `novel-downloader-<ver>-android.apk` |

`novel-cli update` 固定选择 `cli` 条目；没有当前平台的 `*-cli` 条目时不会回退到
installer / portable / api。副本的安装形态由卸载注册表的 `InstallLocation` /
`DisplayIcon`（回退 `unins000.exe`）识别，所以装到自定义目录的安装版仍会更新到
installer。从安装器安装的副本只会更新到安装器，从免安装包解压的副本只会更新到
免安装包，两者不会互相切换。

## 发布前的资产审计 {#pre-release-asset-audit}

`scripts/release.py` 在上传前会对 `dist/release/` 里的产物运行 `scripts/audit_release_assets.py`：zip / apk（以及实际是 zip 的 `.exe`）中的文本成员会逐行套用 `scripts/scan_secrets.py` 的凭据规则，命中以 `secret-marker` 标记，只报 `资产: 成员:行号: 规则`，不回显明文。它和构建前对 Git 跟踪文件的工作区扫描互补——工作区扫描看源码，资产审计看真正要上传的字节。

发布链路上的两道扫描是串行门禁：

1. **构建前**：`scan_release_secrets`（`scripts/scan_secrets.py` 的工作区模式）扫描 Git 跟踪文件；`--skip-secret-scan` 可以关闭这一步（不推荐）。
2. **构建后、上传前**：`audit_release_assets.py` 扫描 `dist/release/` 中的实际产物；没有跳过开关。
3. 两项都通过才会打 tag 并上传；任一命中都会在打 tag 前中止。

v1.34.0 的发布产物通过资产内容扫描，0 命中。

边界与退出码：

- 单个成员超过 2 MiB 跳过；单个资产的文本成员扫描上限 64 MiB；整次审计上限 256 MiB。
- 二进制成员（前 8 KiB 含 NUL）、嵌套 `.zip` / `.exe` / `.apk`、目录和符号链接不扫描；嵌套归档从不解压，读不出的成员会作为问题报告。
- 单独运行 `audit_release_assets.py` 时，有问题退出 `1`，参数或 I/O 错误退出 `2`；在 `release.py` 中命中会让发布以 `ReleaseError` 中止（退出码 `2`），发生在打 tag 与上传之前。

更完整的规则和威胁模型见[安全说明](security.md#release-asset-scan)。

## 已知限制

- **安卓不能静默安装**：那需要系统签名权限，普通应用拿不到。流程是「下载 → 校验 → 拉起系统安装器 → 用户确认」。
- **只发布 Windows 与 Android 产物**：macOS / Linux 需要对应 runner，PyInstaller 不支持交叉编译。
- **清单默认未签名**：当前的信任锚是 HTTPS + 每个资产的 SHA-256。Ed25519 签名机制已内置但尚未启用，格式、启用步骤、换钥流程与各端差异见[更新清单签名](update-signing.md)；启用后客户端会拒绝未签名或签名不符的清单。
