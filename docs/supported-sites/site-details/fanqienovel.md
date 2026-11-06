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

## 锁定章节 (Locked Chapters)

番茄的网页阅读页对锁定章节只返回约 200 字预览。官方 App 的内容接口会给持有内容密钥的设备下发全文，本项目实现了同一套协议：**首次使用时自动注册一台设备并换取内容密钥**，之后直接读取全文——不需要安装官方 App，也不需要登录。

!!! warning "合规提示"
    该能力等价于「在自己设备上使用官方客户端阅读」，仅用于个人离线阅读。请遵守站点服务条款，不要用于批量抓取或再分发。

### 它是怎么工作的

1. 向 applog 注册一台虚拟设备（`device_id` / `install_id`）
2. 用自己的 `X-Argus` / `X-Ladon` 签名换取服务端下发的内容密钥
3. 把身份保存到 `<缓存目录>/fanqienovel/device.json`，后续复用

整个流程约一次往返，失败时静默回退到网页模式，不影响免费章节。

### 高级选项

| 需求 | 做法 |
| --- | --- |
| 使用自己的设备身份 | 设 `NOVELDL_FANQIE_DEVICE` 指向 JSON（字段：`device_id` / `install_id` / `content_key`） |
| 关闭自动注册 | 设 `NOVELDL_FANQIE_AUTO_REGISTER=0` |
| 更换 `x-ladon` 种子 | 设 `NOVELDL_FANQIE_LADON=<48 字符>` |

### 常见日志

* `registered a new device …`：自动注册成功
* `automatic device registration failed`：注册被拒或网络不通，本次按网页模式运行
* `content key rejected for chapter …`：密钥与该设备不匹配，删除 `device.json` 后会自动重新注册
