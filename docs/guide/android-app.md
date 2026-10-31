# Android 客户端

原生 Android App（Kotlin + Jetpack Compose），通过**本地 REST API** 与电脑端通信：
搜索小说、管理下载任务、浏览导出文件、下载多平台音乐。

## 前置条件

1. 电脑端启动 API 服务：

   ```bash
   # 仅本机使用（模拟器可访问 10.0.2.2）
   novel-api --port 8081

   # 真机访问：绑定局域网，并建议设置 token
   novel-api --host 0.0.0.0 --port 8081 --token <你的密钥>
   ```

2. 手机与电脑在同一局域网；Windows 防火墙放行 8081 端口。

## 安装 App

源码位于仓库 `android/` 目录：

```bash
cd android
./gradlew assembleDebug      # 生成 app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug       # 或直接安装到已连接设备
```

用 Android Studio 打开 `android/` 亦可直接 Sync 并运行。

### 正式签名包（Release）

Release 构建需要签名材料。在 `android/` 下创建 **git-ignored** 的
`keystore.properties`（模板见 `app/build.gradle.kts` 头部注释）：

```properties
storeFile=release.jks
storePassword=...
keyAlias=...
keyPassword=...
```

```bash
./gradlew assembleRelease    # 产出 app/build/outputs/apk/release/app-release.apk
```

密钥文件缺失或不完整时，Release 包会回退到 debug 签名（仅适合本机测试，
不能对外发布）。**`keystore.properties` 与密钥文件必须留在本地，切勿提交。**

## 运行单元测试

App 自带不依赖设备的 JVM 单元测试（API 客户端、追更排队逻辑、导航等）：

```bash
cd android
./gradlew testDebugUnitTest
```

## 首次配置

打开 App 右上角「设置」：

| 场景 | API 地址 |
| --- | --- |
| 模拟器访问电脑 | `http://10.0.2.2:8081/` |
| 真机访问电脑 | `http://<电脑局域网 IP>:8081/` |
| 独立模式（「独立引擎」开启） | 无需配置，自动使用本机 `http://127.0.0.1:8081/` |

API 设置了 token 时，把同一个值填入「API 密钥」。

## 功能

底部页签：**搜索 / 任务 / 历史 / 音乐 / 追更**。

- **搜索**：关键字 + 站点过滤（逗号分隔，留空搜索全部站点），结果一键加入下载队列，或点「追更」加入追更列表。
- **追更**：维护追更列表（列表 / 添加 / 移除）；点「检查更新」逐本搜索最新章节，发现新章节的条目可一键加入下载队列。
- **任务**：进行中 / 排队 / 完成分区展示，进度条与章节数，自动刷新（2 秒），可取消；失败任务可一键重试。
- **登录凭据请求**：站点需要登录（账号/Cookie）而本地又没有可用凭据时，任务会挂起并在任务页顶部出现「登录凭据请求」卡片；点「提供凭据」按字段填写（密码/Cookie 类字段输入掩码显示，必填校验、预填值回带），提交后任务自动继续。
- **历史**：导出文件列表，点击通过系统 DownloadManager 下载到系统的「下载」（Downloads）目录。
- **音乐**：选择平台、搜索歌曲、多选后创建下载任务并显示进度。

任务结束（完成 / 失败）时会发送系统通知。Android 13+ 首次启动会请求「通知」权限（`POST_NOTIFICATIONS`）；拒绝后可在系统设置中重新开启。

## 独立引擎（无需电脑）

App 支持两种运行模式，可在设置页通过「独立引擎（无需电脑）」开关随时切换：

- **远程模式（默认，开关关闭）**：手机遥控电脑端 novel-api，搜索、下载等都在电脑上执行；电脑需运行 API 服务并与手机同处一个局域网，即上文描述的原有模式。
- **独立模式（开关开启）**：无需电脑，手机本地运行完整的下载引擎。APK 通过 Chaquopy 内嵌 Python 3.12 运行时（arm64-v8a + x86_64 双 ABI），把整个 novel_downloader 服务层打进包内；引擎启动后在本机环回地址 `http://127.0.0.1:8081/` 上提供与 novel-api 相同的 REST 接口。搜索、任务、历史、追更等全部功能都在手机上完成，数据、任务与历史也只保存在手机本地。

开关开启时，App 启动会先启动本地引擎，再初始化 API 客户端（API 地址自动固定为本机环回地址，无需配置，详见「首次配置」）；关闭开关即切回远程模式，重新连接电脑端 novel-api。

注意事项：

- **音乐页为空态**：音乐扩展未打包进 APK，独立模式下音乐相关端点返回空列表，音乐页显示空态。
- **APK 体积增大**：因打包 Python 运行时及其依赖（aiohttp、lxml、cssselect、pycryptodome、rich、platformdirs 等），APK 体积从约 12MB 增至约 68MB。
- 本地引擎仅绑定环回地址 `127.0.0.1`，只有 App 自身可以访问，局域网内其他设备无法连接。

## 安全说明

App 使用 HTTP 访问本地 API（`usesCleartextTraffic=true`）。只建议在可信局域网内使用；
若需跨网络访问，请通过 VPN（如 Tailscale）或在 API 前加一层 HTTPS 反向代理，并始终设置 token。
