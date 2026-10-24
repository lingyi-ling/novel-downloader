---
title: 有爱爱
tags:
  - 简体中文
  - 成人向
  - NSFW

---



## 基本信息

* 标识符: `uaa`
* 主页: https://www.uaa.com/
* 语言: 简体中文
* 站点状态: :orange_circle: Unstable
* 支持分卷: :material-check: 是
* 支持插图: :material-close: 否
* 支持登录: :material-check: 是
* 支持搜索: :material-close: 否

---

## URL 示例

### Book URL

URL:

```
https://www.uaa.com/novel/intro?id=11304099
```

* Book ID: `11304099`

### Chapter URL

URL:

```
https://www.uaa.com/novel/chapter?id=234639
```

* Chapter ID: `234639`

---

## 适配器注册状态

* 站点适配器与搜索器均已实现（`plugins/sites/uaa/`）；下载仍在 `DOWNLOAD_SUPPORT_SITES` 中保留，但未注册 `SEARCH_SUPPORT_SITES`。
* 实测（2026-10-24）：站点由 Cloudflare 托管，详情页、章节页与搜索接口均返回 `403` 和 `Just a moment...` 人机验证页，`aiohttp` / `httpx` / `curl_cffi` 均无法直接抓取。
* 因此搜索不可用；下载适配器当前也无法在无浏览器 Cookie 的环境中工作，建议在获得有效 Cookie 后重新验证，或由维护者评估是否从下载列表移除。
