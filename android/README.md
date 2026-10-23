# 小说下载器 · Android 客户端

原生 Android 客户端（Kotlin + Jetpack Compose），通过本机的 **本地 REST API**（`novel-api`）工作：
搜索、下载任务队列、历史文件、多平台音乐下载。

## 环境要求

- Android Studio（Koala 或更新）或 JDK 17 + Android SDK（Platform 35、Build-Tools 35）
- 手机/模拟器与运行 API 的电脑网络互通

## 首次使用

1. 在电脑上启动 API（默认只监听 127.0.0.1）：

   ```bash
   novel-api --host 0.0.0.0 --port 8081
   # 建议同时设置 token：novel-api --host 0.0.0.0 --token <你的密钥>
   ```

2. 打开 App → 右上角「设置」：
   - **模拟器**：`http://10.0.2.2:8081/`
   - **真机**：`http://<电脑局域网 IP>:8081/`
   - 如果 API 设置了 token，在「API 密钥」填入同一个值。
3. 回到「搜索」页即可使用。

## 构建

```bash
cd android
./gradlew assembleDebug        # 产物：app/build/outputs/apk/debug/app-debug.apk
./gradlew installDebug         # 安装到已连接的设备/模拟器
```

`adb install app/build/outputs/apk/debug/app-debug.apk` 也可以手动安装。

## 页面

| 页面 | 功能 |
| --- | --- |
| 搜索 | 关键字 + 站点过滤（逗号分隔，留空=全部 57 个站点），结果一键入队 |
| 任务 | 进行中 / 排队 / 完成分区，进度条与章节数，2 秒自动刷新，可取消 |
| 历史 | 导出文件列表（名称/大小/时间），点击用系统浏览器下载 |
| 音乐 | 25 个平台，搜索、多选、创建下载任务并轮询进度 |
| 设置 | API 地址与密钥（DataStore 持久化） |

## 说明

- App 只访问 API，不含任何站点解析逻辑；站点支持随电脑端更新而变化。
- 本地 API 使用 HTTP（`usesCleartextTraffic=true`），仅建议在可信局域网使用；对外暴露务必设置 token。
- 图标为矢量自适应图标，无第三方图片资源。
