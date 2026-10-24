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
    * 命令行: `novel-cli search -s fanqienovel <关键字>`; Web UI: 在搜索页勾选番茄
    * 下载器会复用登录会话启动一个隐藏浏览器窗口, 由站点自身 JS 发起已签名的搜索请求, 再通过 DevTools 协议截获结果 JSON
    * 截获结果中的书名/作者经过字体混淆, 下载器会用书籍详情页回填真实书名与作者
* 使用浏览器辅助搜索前需要先建立登录会话, 任选其一:
    * 用内置登录窗口一键登录: 命令行执行 `novel-cli login fanqienovel`, 或在 Web UI 搜索页点击「登录」按钮; 登录成功后 Cookie 自动写入 `<缓存目录>/fanqienovel/`
    * 设置环境变量 `FANQIENOVEL_COOKIE`
* 浏览器辅助搜索需要安装 `desktop` 额外依赖 (`pip install 'novel-downloader[desktop]'`)
