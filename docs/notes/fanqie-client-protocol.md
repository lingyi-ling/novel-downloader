# 番茄客户端协议：从发布物还原的接口地图

> 来源：`FanqieNovelDownloader-2026.11.5-1322-android-arm64-v8a.apk` 的静态分析（`lib/arm64-v8a/libfanqie_novel_downloader_tauri.so`，stripped）。
> 目的：记录**已观察到的**接口形状，供后续实现或与作者核对，而不是保留反编译代码。
> 标注约定：`observed` = 有命令输出；`inferred` = 由观察推理；`unverified` = 未复现。

---

## 1. 主机与路径（`observed`）

字符串表里出现的常量：

| 常量 | 含义 |
| --- | --- |
| `FANQIE_PRIMARY_BASE_URL` | 主 API 主机 |
| `FANQIE_WEB_BASE_URL` / `FANQIE_OFFICIAL_BASE_URL` | 网页 / 官方站 |
| `FANQIE_REDCANDLE_BASE_URL` | 红烛（评论/互动） |
| `FANQIE_COMMENT_BASE_URL` | 评论 |
| `FANQIE_PASSPORT_BASE_URL` | 登录 |
| `FANQIE_DOUYIN_BASE_URL` | 抖音互通 |
| `FANQIE_DEVICE_REGISTER_URLS` | 设备注册回退链 |

主机集合：

```
api5-normal.fqnovel.com            api5-normal-sinfonlinec.fqnovel.com
api5-normal-sinfonlinea.fqnovel.com api5-normal-lf.fqnovel.com
api5-normal-hl.fqnovel.com         api3-normal.fqnovel.com
api5-sinfonlinec.jxbhmy.com        api-sinfonlinea.fanqiesdk.com
novel.snssdk.com                   novelfm-hl.snssdk.com
reading.snssdk.com                 i.snssdk.com
security.snssdk.com                open-client.douyin.com
```

内容接口：

```
/api/novel/book/reader/content/v1    单章
/reading/reader/full/v               单章（网页同款路径）
/reading/reader/batch_full/v         批量（可一次取多章）
/api/novel/channel/homepage/search/search/v1/   搜索
/bookapi/... /reading/bookapi/...    书架、进度、评论等
```

## 2. 请求参数（`observed`）

寄存器级反汇编里可以直接读到拼接模板：

```
item_id={item_id}&aid={aid}&version_code=9050&version_name={version}
&device_platform=android&os=android&device_type=Pixel%207&os_api=33&support_image=1
```

搜索：

```
/api/novel/channel/homepage/search/search/v1/?aid=1967&q={keyword}&offset=0
```

即客户端**冒充官方安卓端**：`aid=1967`、`package=com.dragon.read`、`version_code=9050`（9.5.0）、`device_type=Pixel 7`。

## 3. 设备身份（`inferred`）

持久化文件：

```
fanqie_primary_device.json   设备身份
fanqie_iid_cache.json        install_id
fanqie_passport_session.json 登录态（可选）
```

注册：

```
POST https://i.snssdk.com/service/2/device_register/?tt_data=a
```

回退链（同一路径依次尝试）：

```
log5-applog.fqnovel.com → log3-applog.fqnovel.com → log.snssdk.com
→ log.isnssdk.com → log.tiktokv.com → log.byteoversea.com
```

载荷字段（`observed`，来自字符串表）：

```
magic_tag=ss_app_log  _gen_time  manifest_version_code  device_model
device_manufacturer   device_platform  device_id_str  install_id_str
serial  sim_serial_number  sim_region  tz_offset  density_dpi
display_density  pre_install  not_request_sender  registerkey  seed
```

## 3.5 注册的真实流程（`observed`，反汇编确认）

设备注册不是「直接 POST 一个 JSON」，而是**两步**：

```
1. POST/GET  {i.snssdk.com | log.snssdk.com}/crypt/registerkey
       → 返回 16 字节密钥（AES-128）+ keyver
       → 错误分支：missing_key / invalid_key / keyver
2. POST      /service/2/device_register/?tt_data=a
       → body 用上一步的 16 字节密钥加密
       → 字段：magic_tag=ss_app_log、header（JSON 字符串）、_gen_time、register_key、seed
```

证据：`0x738110` 处引用 `register_key`，`0x73835c` 有 `cmp x9, #0x10`（长度为 16 才继续，
即密钥长度），`0x73826c/0x7382f0/0x7383a8` 引用 `/crypt/registerkey`，并格式化了
`missing_key` / `invalid_key` / `keyver` 三条错误。

进一步（`0x7380f0` 起）：代码把 `(path='/crypt/registerkey', len=18)` 与
`(value, 'register_key', len=12)` 一起写进一个请求描述结构，然后调用 `0x740624`。
说明 **`register_key` 是请求里携带的字段**，不是响应字段——客户端先生成 16 字节密钥，
带去 `/crypt/registerkey` 换取 `keyver`，再用它加密第二步的 body。

注册是一个**状态机**（入口 `0x737758`，状态字节 `[x1,#0x29]` 取 0–3），按状态在
`api5-normal-sinfonlinec.fqnovel.com`(35 字符) 与 `novelfm-hl.snssdk.com`(21 字符)
之间做主机故障转移；失败落到终止状态 3。

**之前「二进制里没有 AES」的判断是错的**：ring 在 arm64 上走硬件 AES 指令
（`aese`/`aesmc`），不会在 `.rodata` 留下 S-box。密钥长度 16 字节也与 AES-128 吻合。

## 4. 我们验证过、但没走通的部分（`observed`）

| 尝试 | 结果 |
| --- | --- |
| `POST log.snssdk.com/service/2/device_register/?tt_data=a`，`header` 为对象 | 200，`device_id: 0` |
| 同上，`header` 为 JSON 字符串 | 200，`device_id: 0` |
| `POST i.snssdk.com/...`（两种形状） | 200，`device_id: 0` |
| `POST api5-normal-sinfonlinea / api5-normal-lf` 的注册路径 | 404 |
| `/api/novel/book/reader/content/v1` + 随机 `device_id`/`install_id` cookie | 404（网关 TLB） |
| `/reading/reader/full/v` + `x-ss-stub` + 匿名 cookie | 200，空 body |
| `POST/GET i.snssdk.com/crypt/registerkey`（空体 / seed 表单 / JSON） | **504**（路由存在，上游不接） |
| 同上，`api5-normal-*` 主机 | 404 |

二进制里**没有 AES/des**（AES S-box 不存在，`PKCS#7` 属于 rustls 的 X.509 解析），也没有 `a_bogus` / `X-Bogus` / `msToken` 的实现痕迹。因此：

- 该 App **不走网页签名接口**（`a_bogus` 那套），走官方客户端 API；
- 身份签发被服务端拒绝，说明还差一个我们没能从字符串还原的环节。

**卡点已收敛到一处**：`/crypt/registerkey` 的请求形状，以及第二步的 AES 模式（CBC/GCM）
与 IV 约定。

补充实测（`observed`）：本机出口是 **124.64.18.88 / 北京联通**（直连，无代理），
`/crypt/registerkey` 仍然 504；东京机器同样 504。所以**不是地域问题**，
而是请求形状未对上。`register_key` 以 hex/base64/JSON 三种形式提交均为 504。

**推论修正**：App 侧确实持有已注册身份（见 §4.5），所以「不登录看全部」成立。
我们缺的不是身份，而是**请求签名/头部**——网关对缺签名的请求返回
`200 + Content-Length: 0`（静默拒绝），而不是 403。

已排除：地域（北京联通直连同样 504/空体）、Cookie（带真实 device_id/install_id 仍空）、
查询参数（把 device_profile 全字段铺进 query 仍空）、主机（7 个 API 主机全试）。

## 4.5 设备身份的真实结构（`observed`）

App 把注册结果写在 `%APPDATA%\com.pofl.fanqienoveldownloaderanqie_primary_device.json`
（值已脱敏，仅记录字段）：

```json
{
  "install_id": "<19 位数字>",
  "device_id":  "<19 位数字>",
  "registered": true,
  "device_profile": {
    "device_type": "<机型>", "device_brand": "<品牌>",
    "os_version": "<版本>", "os_api": "<API 级别>", "host_abi": "arm64-v8a",
    "resolution": "<宽*高>", "dpi": "<dpi>",
    "cdid": "<UUID>", "openudid": "<16 位 hex>",
    "rom_version": "<ROM>", "language": "zh", "channel": "<渠道号>",
    "version_code": "<官方版本号>", "version_name": "<官方版本名>",
    "dragon_device_type": "0", "is_android_pad_screen": "0"
  },
  "content_key": "<base64，解码后 16 字节>",
  "content_key_version": <整数>,
  "privilege_expire_time": "<unix 秒>"
}
```

三个字段直接印证了 §3.5 的推断：

- `content_key` 解码后 **16 字节** → 就是 `/crypt/registerkey` 换来的 AES-128 密钥；
- `content_key_version` → 响应里的 `keyver`；
- `privilege_expire_time` → 注册设备获得的**免费阅读权限到期时间**，这正是「不登录也能看全部」的来源。

同目录还有 `rust_state.json`，其中 `"download_channel": "official"`、
`"experimental_device_pool"` 两个开关说明：官方通道 + 设备池是设计内的能力。

## 5. 结论与下一步

匿名读全文的关键是**一个有效的设备身份**。要补齐，成本从低到高：

1. **向作者要一段样例**（最省）：一个 `fanqie_primary_device.json`，或注册请求的抓包。有源的人在对面，逆向是下策。
2. **动态抓包**：root 设备/模拟器 + Frida hook 注册响应；本项目所在的机器没有 adb/frida，且包只有 arm64。
3. **继续静态**：从 `x-ss-stub` 等字符串的 ADRP 引用反向追完整个 header 构造，数小时级且不保证成功。

在拿到身份之前，本项目的番茄支持边界是：**匿名可读的章节正常下载；`isChapterLock` 的章节判为失败并进入缺章账本**，不再产出 200 字假章节。
