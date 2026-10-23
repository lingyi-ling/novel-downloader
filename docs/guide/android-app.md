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

## 首次配置

打开 App 右上角「设置」：

| 场景 | API 地址 |
| --- | --- |
| 模拟器访问电脑 | `http://10.0.2.2:8081/` |
| 真机访问电脑 | `http://<电脑局域网 IP>:8081/` |

API 设置了 token 时，把同一个值填入「API 密钥」。

## 功能

- **搜索**：关键字 + 站点过滤（逗号分隔，留空搜索全部站点），结果一键加入下载队列。
- **任务**：进行中 / 排队 / 完成分区展示，进度条与章节数，自动刷新（2 秒），可取消。
- **历史**：导出文件列表，点击交给系统浏览器/下载器保存。
- **音乐**：选择平台、搜索歌曲、多选后创建下载任务并显示进度。

## 安全说明

App 使用 HTTP 访问本地 API（`usesCleartextTraffic=true`）。只建议在可信局域网内使用；
若需跨网络访问，请通过 VPN（如 Tailscale）或在 API 前加一层 HTTPS 反向代理，并始终设置 token。
