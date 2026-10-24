# 小说下载器 · Android 客户端

原生 Android 客户端（Kotlin + Jetpack Compose），通过本机的 **本地 REST API**（`novel-api`）工作：
搜索、下载任务队列、历史文件、多平台音乐下载。

当前版本：**1.1.0**（`versionCode 2`，与主项目 `novel_downloader.__version__` 一致）。

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

## 构建与测试

需要 JDK 17（示例）：

```bash
export JAVA_HOME="C:/Android/jdk/msjdk/jdk-17.0.20.1+1"
export PATH="$JAVA_HOME/bin:$PATH"
cd android

./gradlew testDebugUnitTest assembleDebug   # 单元测试 + Debug APK
./gradlew installDebug                      # 安装到已连接的设备/模拟器
```

构建产物：

| 构建 | 命令 | 产物 |
| --- | --- | --- |
| Debug | `./gradlew assembleDebug` | `app/build/outputs/apk/debug/app-debug.apk` |
| Release | `./gradlew assembleRelease` | `app/build/outputs/apk/release/app-release.apk` |

手动安装：`adb install app/build/outputs/apk/debug/app-debug.apk`。

### 发布签名

Release 签名通过 `android/keystore.properties`（已在 `.gitignore` 中排除）配置，不要提交密钥：

```properties
storeFile=release.jks        # 绝对路径，或相对 android/ 的路径
storePassword=你的仓库密码
keyAlias=你的别名
keyPassword=你的密钥密码
```

文件缺失或不完整时，`assembleRelease` 会**回退到 debug 签名**继续构建（仅用于本地验证，不要这样分发）。
生成密钥示例：

```bash
keytool -genkeypair -v -keystore android/release.jks -alias noveldl \
  -keyalg RSA -keysize 2048 -validity 10000
```

## 页面

| 页面 | 功能 |
| --- | --- |
| 搜索 | 关键字 + 站点过滤（逗号分隔，留空=全部 57 个站点），结果一键入队 |
| 任务 | 进行中 / 排队 / 完成分区，进度条与章节数，2 秒自动刷新，可取消 |
| 历史 | 导出文件列表（名称/大小/时间），点击经系统 DownloadManager 下载到公共 Downloads 目录；设置了 API 密钥时自动附加 `X-API-Key` 请求头 |
| 音乐 | 25 个平台，搜索、多选、创建下载任务并轮询进度 |
| 设置 | API 地址与密钥（DataStore 持久化） |

## 说明

- App 只访问 API，不含任何站点解析逻辑；站点支持随电脑端更新而变化。
- 本地 API 使用 HTTP（`usesCleartextTraffic=true`），仅建议在可信局域网使用；对外暴露务必设置 token。
- 图标为矢量自适应图标，无第三方图片资源。
