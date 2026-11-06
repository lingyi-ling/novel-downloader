---
title: 番茄小说网
tags:
  - 简体中文
  - 原创
  - 免费
  - 男性向
  - 女性向
  - 抖音

---

番茄小说是抖音旗下免费阅读产品，拥有海量正版小说资源，支持听书畅读，提供书荒广场、优质书圈社区，推广优秀原创网络小说文学，全方位满足读者需求。

番茄小说网提供玄幻小说,武侠小说,原创小说,网游小说,都市小说,言情小说,青春小说,历史小说,军事小说,网游小说,科幻小说,恐怖小说

## 基本信息

* 标识符: `fanqienovel`
* 主页: https://fanqienovel.com/
* 语言: 简体中文
* 站点状态: :green_circle: Active
* 支持分卷: :material-check: 是
* 支持插图: :material-close: 否
* 支持登录: :material-help-circle: 部分支持
* 支持搜索: :material-check: 是

---

## URL 示例

### Book URL

URL:

```
https://fanqienovel.com/page/7276384138653862966
```

* Book ID: `7276384138653862966`

### Chapter URL

URL:

```
https://fanqienovel.com/reader/7276663560427471412
```

* Chapter ID: `7276663560427471412`

---

## 搜索说明 (Search Notes)

* 番茄的搜索接口需要站点自身 JS 生成的签名, 纯 HTTP 请求即使带上有效 Cookie 也拿不到结果, 因此**聚合搜索不会包含番茄的结果** (不影响其它站点)
* 显式指定番茄时才会启用**浏览器辅助搜索**:
    * 命令行: `novel-cli search -s fanqienovel <关键字>`; 桌面客户端: 在搜索页站点过滤中填入 `fanqienovel`
    * 下载器会复用登录会话启动一个隐藏浏览器窗口, 由站点自身 JS 发起已签名的搜索请求, 再通过 DevTools 协议截获结果 JSON
    * 截获结果中的书名/作者经过字体混淆, 下载器会用书籍详情页回填真实书名与作者
* 使用浏览器辅助搜索前需要先建立登录会话, 任选其一:
    * 用内置登录窗口一键登录: 命令行执行 `novel-cli login fanqienovel`; 登录成功后 Cookie 自动写入 `<缓存目录>/fanqienovel/`
    * 设置环境变量 `FANQIENOVEL_COOKIE`
* 浏览器辅助搜索依赖 pywebview (已随基础安装携带, Windows 使用 Edge WebView2)

---

## 锁定章节与设备身份 (Locked Chapters)

番茄的网页阅读页对锁定章节只返回约 200 字预览。官方 App 的内容接口会给**持有内容密钥的设备**下发全文，本项目实现了同一套协议（请求签名 → 设备身份 → AES 解密），因此配置了可用设备身份就能下载锁定章节。

!!! warning "合规提示"
    该能力等价于「在自己设备上使用官方客户端阅读」，仅用于个人离线阅读。请遵守站点服务条款，不要用于批量抓取或再分发。

### 提供设备身份

按优先级依次尝试，找到第一个可用文件即停止：

| 顺序 | 位置 |
| --- | --- |
| 1 | 环境变量 `NOVELDL_FANQIE_DEVICE` 指向的 JSON 文件 |
| 2 | `<缓存目录>/fanqienovel/device.json` |
| 3 | 本机已安装的 FanqieNovelDownloader（`com.pofl.fanqienoveldownloader`）自己注册的身份文件 |

文件只需要三个字段：

```json
{
  "device_id": "<19 位数字>",
  "install_id": "<19 位数字>",
  "content_key": "<base64 或 32 位 hex，解码后 16 字节>"
}
```

多出来的字段（机型、版本号等）会被忽略。文件只在本机读取，不会随任何请求上传。

### 行为差异

* **有身份**：先走 App 内容接口；成功即返回全文。请求失败或密钥不匹配时自动回退网页阅读页，不会让整本书失败
* **无身份**：与以前完全一致——可匿名读的章节正常下载，锁定章节记为缺失并进入缺章账本

### 常见问题

* 日志出现 `content key rejected`：密钥与该设备不匹配（换过设备或密钥被刷新），重新提供一份身份文件即可
* 日志出现 `no app device identity found`：三个位置都没找到可用文件，当前按网页模式运行
