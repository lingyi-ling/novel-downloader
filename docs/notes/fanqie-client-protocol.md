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

## 4. 内容接口已打通（`observed`，2026-11-06 实测）

单个章节的完整请求形状如下（**参数顺序、编码一个字符都不能变**，签名覆盖的是原始
query 串）：

```
GET https://reading.snssdk.com/reading/reader/full/v/
    ?aid=1967&app_name=novelapp&channel=0&device_platform=android
    &device_id={device_id}&device_type=Honor10
    &item_id={item_id}&os_version=0&version_code=66.9
Headers: User-Agent: com.dragon.read
         X-Gorgon: 8404xxxx...（52 位 hex）
         X-Khronos: <unix 秒，与签名同源>
```

响应 `data` 里 `crypt_status=0` 时，`content` 是 base64（前 16 字节 IV，其余
AES-128-CBC 密文），用设备身份里的 `content_key` 解密即得正文 HTML。

实测证据：

| 章节 | 结果 |
| --- | --- |
| `7704667598306165273`（第 280 章，网页端 `isChapterLock=true`） | 响应 25809 字节 → 解密 3359 字符完整正文 |
| 同上，`version_code=9050` | `{"code":110,"message":"ILLEGAL_ACCESS"}` |

三个关键点：

1. **签名算法是 `8404` 变体**：`md5(query)[:4] + 4×0 + [0,0,0,0] + [0,1,7,4] + 时间戳`
   共 20 字节，用两个随机字节派生 8 字节密钥做 KSA + 两轮变换，最后加 6 字节头
   `84 04 <a1> <a2> 00 00`。随机数写在头里，服务端据此重建密钥流。
   移植件在 `src/novel_downloader/plugins/sites/fanqienovel/appapi.py`，
   并由 `tests/plugins/fanqienovel/test_appapi.py` 用固定向量逐字节锁住。
2. **`version_code` 必须低**。新版号会打开 `x-medusa` / `x-helios` 校验，那两个
   签名要跑官方 `.so`；旧版号只看 `x-gorgon`。`66.9` 是社区长期沿用的值。
3. **只需要三个头**，不需要 `x-argus` / `x-ladon` / `x-ss-stub`，也不需要 Cookie。

## 4.5 设备身份的真实结构（`observed`）

App 把注册结果写在 `%APPDATA%/com.pofl.fanqienoveldownloader/fanqie_primary_device.json`
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

## 5. 仍未打通：内容密钥的签发（`observed`）

### 5.1 抓到过完整请求（2026-11-07 运行时内存）

把 `fanqie_primary_device.json` 移走再启动 Windows 客户端，它会在 5 秒内自动注册一台新
设备（含 `content_key` / `content_key_version` / `privilege_expire_time`）。趁这段窗口扫进程
私有堆，可以拿到**未经日志脱敏的原始 HTTP 请求**：

```
POST /reading/crypt/registerkey?device_id=<19位>&iid=<19位>&device_type=SM-G9750
     &aid=1967&app_name=novelapp&channel=0&device_platform=android
     &os_version=0&version_code=6.3.9.32 HTTP/1.1
user-agent: com.dragon.read/74132 (Linux; U; Android 14; zh_CN; SM-G9750;
            Build/<ROM> release-keys; Cronet/TTNetVersion:8d40f833 2026-03-03
            QuicVersion:21ac1950 2025-11-18)
accept: application/json
accept-encoding: gzip
x-ss-req-ticket: <毫秒时间戳>
x-argus: <240 字符 base64>
x-ladon: <48 字符 base64>
x-khronos: <unix 秒>
x-ss-stub: <小写 md5>
content-type: application/json; charset=utf-8
content-encoding: gzip
```

三个与文档旧结论不同的地方：

1. **`version_code=6.3.9.32`**，是带点的版本**字符串**，不是 `66.9` 也不是数字。
   （社区说的「版本号里加小数点」在这里是整串版本名。）
2. **没有 `x-gorgon`**，改带 `x-argus`（长签，240 字符）+ `x-ladon`（48 字符）。
3. 请求体是 gzip 后的 JSON，`x-ss-stub` 为 **小写** md5。

### 5.2 但复现仍然失败：门槛就是 `x-argus`

按上述形状逐项复现（含 gzip 体、小写 stub、`6.3.9.32`、同一台设备、全新无密钥设备）：
**全部 200 + 空 body**。

| 变体 | 结果 |
| --- | --- |
| 完全照抄形状，`x-argus` 用短签（`base64(pack('<I', khronos))`） | 空 |
| 不带 `x-argus` / `x-ladon` | 空 |
| `x-argus` / `x-ladon` 换成**长度一致的随机 base64**（240 / 48） | 空 |
| 换 `Content-Type`（json / octet-stream / form）、换 GET / PUT | 空 |
| 换主机（reading / i / security / api5-normal-* / novel / novelfm-hl） | 空 |
| 换 TLS 指纹（urllib 与 curl_cffi 的 chrome / safari 指纹） | 空 |
| `device_register` 改用 TTEncrypt 编码体 | 响应 `device_id: 0`（不认编码体） |

**长度一致但内容随机 → 仍然空**，说明服务端是在做真实的密码学校验，而不是检查长度或存在性。
配合 §5.1 抓到的「真实请求必然是 240 字符 argus」，结论收敛为一条：

> `/crypt/registerkey` 要求**由 `libmetasec` 生成的 `x-argus` 长签**；
> 阅读接口（`version_code=66.9`）不要求它，所以取正文本身是通的。

密钥只能从这一步来：用我们的明文 `device_register` 注册的新设备，响应里只有
`device_id/install_id/device_token/klink_egdi`，没有任何密钥字段；而 App 注册的设备
`crypt_status=0` 且能用 `content_key` 解密。两者之差就是这一次带签名的 registerkey。

### 5.3 无法自签，但身份可以「导入」

因此本项目的边界没有变，只是原因写实了：

- 有设备身份：App 接口取下全文（含锁定章节），失败自动回退网页
- 无设备身份：与以前一致，锁定章节记为缺失并进入缺章账本
- 不再产出 200 字假章节

身份文件的提供方式见 `docs/supported-sites/site-details/fanqienovel.md`。
若要真正「零配置自注册」，只剩两条路：移植 `x-argus` 纯算（需要从客户端二进制里
逆出算法与 sign key），或用 unidbg 仿真 `libmetasec`（社区已有工程）。
