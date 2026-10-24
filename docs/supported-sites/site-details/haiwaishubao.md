---
title: 海外书包
tags:
  - 简体中文
  - 转载站
  - 成人向
  - NSFW

---



## 基本信息

* 标识符: `haiwaishubao`
* 主页: https://www.haiwaishubao.com/
* 备用域名:
    * https://www.haiwaishubao1.com/
* 语言: 简体中文
* 站点状态: :green_circle: Active
* 支持分卷: :material-close: 否
* 支持插图: :material-close: 否
* 支持登录: :material-help-circle: 部分支持
* 支持搜索: :material-close: 否

---

## URL 示例

### Book URL

URL:

```
https://www.haiwaishubao.com/book/102659/
```

* Book ID: `102659`

### TOC URL

URL:

```
https://www.haiwaishubao.com/index/102659/
```

* Book ID: `102659`

### Chapter URL

URL:

```
https://www.haiwaishubao.com/book/102659/5335635.html
```

* Book ID: `102659`
* Chapter ID: `5335635`

---

## 适配器注册状态

* 站点适配器位于 `plugins/sites/haiwaishubao/`，已注册下载；实测详情页、目录分页、章节分页均可抓取并解析。
* 站点主域名 `www.haiwaishubao.com` 当前 301 跳转至 `www.haiwaishubao1.com`，抓取时会自动跟随跳转。
* 搜索未注册：搜索请求为 POST，旧域名 301 跳转后 `aiohttp` 会改为 GET 并丢失表单，返回空结果；新域名直连搜索也受 30 秒限流且未返回可用结果。
