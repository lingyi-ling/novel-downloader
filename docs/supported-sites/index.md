# Supported Sites / 支持站点

!!! info "说明"
    本页面列出了 NovelDownloader 当前支持的站点, 并提供各站点的功能支持情况概览。

    点击站点名称进入详情页, 可查看 **URL 示例、站点状态、功能支持级别** 等信息。

---

## 站点统计 (Statistics)

| 指标 | 数量 |
| ---- | ---- |
| 站点条目总数 (`supported_sites.toml`) | 92 |
| 支持下载 (`DOWNLOAD_SUPPORT_SITES`) | 83 |
| 支持搜索 (`SEARCH_SUPPORT_SITES`) | 57 |
| 仅支持下载 (不参与搜索) | 26 |
| 已归档 (archived) | 8 |

!!! note "统计口径"
    * **支持下载 / 支持搜索**: 以 `src/novel_downloader/apps/constants.py`
      中注册的站点集合为准。
    * **优先级**: 搜索结果使用的站点优先级 (`BaseSearcher.priority`),
      **数值越小越靠前**; 未注册搜索器时显示 `—`。
    * 站点条目总数来自 `scripts/data/supported_sites.toml`, 包含已归档站点。

---

## 标签分类 (Tags)

参见：[标签索引](./tags.md)

---

## 支持站点总览 (Overview)

下表展示各站点的支持情况 (`下载` / `搜索` 表示是否已注册对应插件):

* :material-check: = 支持
* :material-close: = 不支持
* :material-help-circle: = 部分支持
* :material-open-in-new: = 站点原生支持 (需站内操作)

状态: :green_circle: Active / :orange_circle: Unstable / :red_circle: Archived

| 站点名称 | 标识符 | 状态 | 下载 | 搜索 | 优先级 | 分卷 | 图片 | 登录 |
| -------- | ------ | ---- | ---- | ---- | ------ | ---- | ---- | ---- |
| [3A电子书](./site-details/aaatxt.md) | `aaatxt` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [暁 ～小説投稿サイト～](./site-details/akatsuki_novels.md) | `akatsuki_novels` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-help-circle: |
| [爱丽丝书屋](./site-details/alicesw.md) | `alicesw` | :green_circle: | :material-check: | :material-check: | 500 | :material-close: | :material-close: | :material-help-circle: |
| [アルファポリス](./site-details/alphapolis.md) | `alphapolis` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-close: | :material-help-circle: |
| [笔趣阁](./site-details/b520.md) | `b520` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [笔趣阁](./site-details/biquge345.md) | `biquge345` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [笔趣阁](./site-details/biquge5.md) | `biquge5` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [笔趣阁小说网](./site-details/biquguo.md) | `biquguo` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [精彩小说](./site-details/biquyuedu.md) | `biquyuedu` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [笔仙阁](./site-details/bixiange.md) | `bixiange` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-close: |
| [笔趣读](./site-details/blqudu.md) | `blqudu` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [笔下文学网](./site-details/bxwx9.md) | `bxwx9` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [思路客](./site-details/ciluke.md) | `ciluke` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [刺猬猫](./site-details/ciweimao.md) | `ciweimao` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-check: |
| [次元姬](./site-details/ciyuanji.md) | `ciyuanji` | :green_circle: | :material-check: | :material-check: | 5 | :material-check: | :material-check: | :material-check: |
| [小说狂人](./site-details/czbooks.md) | `czbooks` | :green_circle: | :material-check: | :material-check: | 500 | :material-close: | :material-close: | :material-help-circle: |
| [得奇小说网](./site-details/deqixs.md) | `deqixs` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-close: |
| [读书](./site-details/dushu.md) | `dushu` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-help-circle: |
| [大熊猫文学网](./site-details/dxmwx.md) | `dxmwx` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [ESJ Zone](./site-details/esjzone.md) | `esjzone` | :green_circle: | :material-check: | :material-check: | 30 | :material-check: | :material-check: | :material-check: |
| [飞卢小说网](./site-details/faloo.md) | `faloo` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-check: | :material-check: |
| [番茄小说网](./site-details/fanqienovel.md) | `fanqienovel` | :green_circle: | :material-check: | :material-check: | 5 | :material-check: | :material-close: | :material-help-circle: |
| [笔趣阁](./site-details/fsshu.md) | `fsshu` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [名著阅读](./site-details/guidaye.md) | `guidaye` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-close: |
| [海外书包](./site-details/haiwaishubao.md) | `haiwaishubao` | :green_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [和图书](./site-details/hetushu.md) | `hetushu` | :green_circle: | :material-check: | :material-check: | 5 | :material-check: | :material-close: | :material-close: |
| [红袖招](./site-details/hongxiuzhao.md) | `hongxiuzhao` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [25中文网](./site-details/i25zw.md) | `i25zw` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [爱下电子书](./site-details/ixdzs8.md) | `ixdzs8` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [精品小说网](./site-details/jpxs123.md) | `jpxs123` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [KadoKado](./site-details/kadokado.md) | `kadokado` | :green_circle: | :material-check: | :material-check: | 30 | :material-check: | :material-check: | :material-check: |
| [八一中文网](./site-details/ktshu.md) | `ktshu` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [鲲弩小说](./site-details/kunnu.md) | `kunnu` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-close: | :material-close: |
| [老幺小说网](./site-details/laoyaoxs.md) | `laoyaoxs` | :orange_circle: | :material-check: | :material-check: | 20 | :material-close: | :material-close: | :material-close: |
| [乐文小说网](./site-details/lewenn.md) | `lewenn` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [轻之文库](./site-details/linovel.md) | `linovel` | :green_circle: | :material-check: | :material-check: | 30 | :material-check: | :material-check: | :material-help-circle: |
| [哔哩轻小说](./site-details/linovelib.md) | `linovelib` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-check: | :material-help-circle: |
| [轻小说百科](./site-details/lnovel.md) | `lnovel` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-help-circle: |
| [绿色小说网](./site-details/lvsewx.md) | `lvsewx` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-close: |
| [追书网](./site-details/mangg_com.md) | `mangg_com` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [追书网](./site-details/mangg_net.md) | `mangg_net` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [三五中文](./site-details/mjyhb.md) | `mjyhb` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [101看书](./site-details/n101kanshu.md) | `n101kanshu` | :green_circle: | :material-check: | :material-check: | 20 | :material-close: | :material-close: | :material-help-circle: |
| [17K小说网](./site-details/n17k.md) | `n17k` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-close: | :material-help-circle: |
| [顶点小说网](./site-details/n23ddw.md) | `n23ddw` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [铅笔小说](./site-details/n23qb.md) | `n23qb` | :green_circle: | :material-check: | :material-check: | 10 | :material-check: | :material-close: | :material-help-circle: |
| [三七轻小说](./site-details/n37yq.md) | `n37yq` | :orange_circle: | :material-check: | :material-check: | 10 | :material-check: | :material-check: | :material-help-circle: |
| [37阅读网](./site-details/n37yue.md) | `n37yue` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [69书吧](./site-details/n69hao.md) | `n69hao` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-close: |
| [69书吧](./site-details/n69shuba.md) | `n69shuba` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [69阅读](./site-details/n69yue.md) | `n69yue` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [新吾爱文学](./site-details/n71ge.md) | `n71ge` | :green_circle: | :material-check: | :material-check: | 20 | :material-close: | :material-close: | :material-help-circle: |
| [无限轻小说](./site-details/n8novel.md) | `n8novel` | :green_circle: | :material-check: | :material-check: | 20 | :material-check: | :material-check: | :material-help-circle: |
| [笔趣阁](./site-details/n8tsw.md) | `n8tsw` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [ノベルピア](./site-details/novelpia.md) | `novelpia` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-check: | :material-help-circle: |
| [飘天文学网](./site-details/piaotia.md) | `piaotia` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [霹雳书屋](./site-details/pilibook.md) | `pilibook` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-close: |
| [全本同人小说](./site-details/qbtr.md) | `qbtr` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [起点中文网](./site-details/qidian.md) | `qidian` | :green_circle: | :material-check: | :material-check: | 0 | :material-check: | :material-close: | :material-check: |
| [七猫中文网](./site-details/qimao.md) | `qimao` | :green_circle: | :material-check: | :material-check: | 5 | :material-close: | :material-close: | :material-close: |
| [QQ 阅读](./site-details/qqbook.md) | `qqbook` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-check: |
| [全本小说网](./site-details/quanben5.md) | `quanben5` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [若初文学网](./site-details/ruochu.md) | `ruochu` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [SF轻小说](./site-details/sfacg.md) | `sfacg` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-check: |
| [少年梦](./site-details/shaoniandream.md) | `shaoniandream` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-help-circle: |
| [大众文学](./site-details/shauthor.md) | `shauthor` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [神凑轻小说](./site-details/shencou.md) | `shencou` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-check: | :material-help-circle: |
| [书林文学](./site-details/shu111.md) | `shu111` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [书海阁小说网](./site-details/shuhaige.md) | `shuhaige` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [小説家になろう / 小説を読もう！](./site-details/syosetu.md) | `syosetu` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-help-circle: |
| [小説家になろう 18禁区 (ムーンライト / ノクターン)](./site-details/syosetu18.md) | `syosetu18` | :green_circle: | :material-check: | :material-check: | 100 | :material-check: | :material-check: | :material-help-circle: |
| [ハーメルン](./site-details/syosetu_org.md) | `syosetu_org` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-help-circle: |
| [天涯书库](./site-details/tianyabooks.md) | `tianyabooks` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-close: |
| [同人圈](./site-details/tongrenquan.md) | `tongrenquan` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [同人社](./site-details/tongrenshe.md) | `tongrenshe` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [同人小说网](./site-details/trxs.md) | `trxs` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-close: |
| [天天看小说](./site-details/ttkan.md) | `ttkan` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-close: |
| [台灣小說網](./site-details/twkan.md) | `twkan` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [有爱爱](./site-details/uaa.md) | `uaa` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-check: |
| [完本神站](./site-details/wanbengo.md) | `wanbengo` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [轻小说文库](./site-details/wenku8.md) | `wenku8` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-check: | :material-check: |
| [西方奇幻小说网](./site-details/westnovel.md) | `westnovel` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-close: |
| [西方奇幻小说网子站点](./site-details/westnovel_sub.md) | `westnovel_sub` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-close: |
| [万相书城](./site-details/wxsck.md) | `wxsck` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [小说屋](./site-details/xiaoshuoge.md) | `xiaoshuoge` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [西瓜书屋](./site-details/xiguashuwu.md) | `xiguashuwu` | :green_circle: | :material-check: | :material-check: | 500 | :material-close: | :material-close: | :material-help-circle: |
| [小说路上](./site-details/xs63b.md) | `xs63b` | :red_circle: | :material-close: | :material-close: | — | :material-close: | :material-close: | :material-help-circle: |
| [小说虎](./site-details/xshbook.md) | `xshbook` | :green_circle: | :material-check: | :material-check: | 30 | :material-close: | :material-close: | :material-help-circle: |
| [百合会](./site-details/yamibo.md) | `yamibo` | :green_circle: | :material-check: | :material-close: | — | :material-check: | :material-close: | :material-check: |
| [一笔阁](./site-details/yibige.md) | `yibige` | :green_circle: | :material-check: | :material-check: | 100 | :material-close: | :material-close: | :material-help-circle: |
| [有度中文网](./site-details/yodu.md) | `yodu` | :green_circle: | :material-check: | :material-check: | 15 | :material-check: | :material-open-in-new: | :material-help-circle: |
| [镇魂小说网](./site-details/zhenhunxiaoshuo.md) | `zhenhunxiaoshuo` | :green_circle: | :material-check: | :material-close: | — | :material-close: | :material-close: | :material-close: |
