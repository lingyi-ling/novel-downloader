# 更新与自动更新

桌面、CLI 与安卓客户端都会检查新版本。三者共用同一份**更新清单**，检查逻辑和校验规则一致，只是安装方式不同。

## 检查频率

| 端 | 时机 | 频率上限 |
| --- | --- | --- |
| 桌面 | 启动后由前端发起 | 每 24 小时一次 |
| CLI | 任意命令结束后静默提示（仅 TTY、非 `--json`） | 每 24 小时一次 |
| 安卓 | 启动时 | 每 24 小时一次 |

检查结果会被缓存，24 小时内重复启动不会再次联网。

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

`--yes` 只把文件下载到应用数据目录（`<app_home>/updates/`）并校验，**不会**替你安装。安装方式：

- Windows：双击下载目录里的 `NovelDownloader-setup-<version>-x64.exe`
- 其他平台：解压下载的压缩包，替换已安装的文件

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
| `android` | `novel-downloader-<ver>-android.apk` |

从安装器安装的副本只会更新到安装器，从免安装包解压的副本只会更新到免安装包，两者不会互相切换。

## 已知限制

- **安卓不能静默安装**：那需要系统签名权限，普通应用拿不到。流程是「下载 → 校验 → 拉起系统安装器 → 用户确认」。
- **只发布 Windows 与 Android 产物**：macOS / Linux 需要对应 runner，PyInstaller 不支持交叉编译。
- **清单默认未签名**：当前的信任锚是 HTTPS + 每个资产的 SHA-256。签名机制已内置（Ed25519），启用方式见仓库的 `AGENTS.md`；启用后客户端会拒绝未签名或签名不符的清单。
