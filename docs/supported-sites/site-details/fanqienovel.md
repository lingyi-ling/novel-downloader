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

* 显式指定番茄时会先走**官方 App 搜索接口**：不需要登录，也不需要弹出浏览器。首次使用会自动注册设备身份（见上节），之后 `novel-cli search -s fanqienovel <关键字>` 直接返回明文书名与作者
* 聚合搜索（不带 `-s`）仍会跳过番茄，以保持「快、不弹窗」的既有约定
* 若 App 搜索不可用（无身份且注册失败），自动回退到浏览器辅助搜索：
    * 需要先建立登录会话：`novel-cli login fanqienovel`，或设 `FANQIENOVEL_COOKIE`
    * 下载器会复用会话启动隐藏浏览器窗口，由站点自身 JS 发起已签名请求，再通过 DevTools 协议截获结果 JSON
    * 截获结果的标题/作者经过字体混淆，回退路径会用书籍详情页回填真实值

## 浏览 (Browse)

* 「排行榜」：抓取 `/rank` 服务端渲染页，取总榜 10 条，标题/作者用书籍详情页回填成明文
* 「分类」：分类列表来自榜单页的 `rankCategoryTypeList`（男频 19 + 女频 18），书目取 `/api/library/book_list`，**不需要签名**，只要参数里带上 `word_count=0&sort=0`；书名、作者、封面、更新日期本身就都是明文
