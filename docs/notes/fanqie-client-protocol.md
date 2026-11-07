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

## 5. 已打通：自签名 + 自注册（2026-11-07）

内容密钥不再需要官方客户端。两步都在本项目里完成：

```
1. POST log5-applog.fqnovel.com/service/2/device_register/?<query>
      X-Gorgon(8404) + X-Khronos + x-ss-stub(md5 body, 大写)
      -> device_id / install_id
2. POST reading.snssdk.com/reading/crypt/registerkey?<query>
      version_code=6.3.9.32
      X-Argus(长签) + X-Ladon(48 字符) + x-khronos
      体 = gzip({"content": <AES(device_id)>, "keyver": 1})
      x-ss-stub = md5(gzip 体) 小写
      -> {"code":0,"data":{"key":<16 字节密钥被 master key 包裹>,"keyver":N}}
```

`data.key` 用 master key（`ac25c67d…`，AES-128-CBC，前 16 字节 IV）解出 16 字节内容密钥，
再去 `full/v?version_code=66.9` 取正文，AES-128-CBC 解密即可。

### 5.1 X-Argus：已逐字节复现

从客户端运行时抓到的 argus 用公开根密钥解帧，得到完整 bean，字段与取值一一对上；
再用同一套算法重算，**与抓包逐字节相同**（`tests/plugins/fanqienovel/test_argus.py`
把它钉成了固定向量）。算法：

```
protobuf bean（字段 1..16,20,21,23,25）
  -> PKCS#7 -> Simon-128/256（72 轮，rotr 常量 0x3DC94C3A046D678B, %62）
  -> 8 字节魔术 f2f7fcff ×2 + 9 字节尾 + 逐字节异或 -> 反转 -> "ao"
  -> AES-128-CBC(key=md5(root[:16]), iv=md5(root[16:])) -> 前缀 f281 -> base64
```

关键字段：`f1=0x40401252`、`f4="1967"`、`f5=device_id`、`f7=version_name`、
`f12=ts<<1`、`f13=SM3(MD5(body) 原字节)[:6]`、`f14=SM3(query)[:6]`。

注意 `f7` 取的是**客户端自身的版本名**（如 `7.4.1.32`），query 里没有也要填。

### 5.2 X-Ladon：结构已还原，调度仍未对齐

运行时钩子把这条链路看到了底：

**明文**（加密循环入口直接读到）：

```
1794018144-1611921764-1967     28 字节 + PKCS#7(0x06×6) = 32 字节
（另有 aid=3019 的变体，两种都被服务端接受）
```

**密钥表** = `md5(salt + str(aid))` 的**十六进制串**（32 字节），`salt` 是输出前 4 字节，
随机生成。钩到的第一段轮密钥正是该十六进制串的前 8 个字符（如 `7ffaf4ac`）。

**帧**：`4 字节 salt + 32 字节密文`，base64 后 48 字符。

**轮函数**（反汇编逐条确认，34 轮）：

```
y = (ror8(y) + x) ^ k
x = rol3(x) ^ y
```

**卡点**：轮密钥的排列顺序。反汇编里调度表在栈上被反复左移 8 字节再追加，
首轮用的是 `q0`、次轮取自窗口第二格……我们按公开实现的两种排布都试过，
用抓到的 `(salt, 密文)` 逐 ts 爆破都不匹配。也就是说轮函数和密钥派生都对，
**只差调度表那 34 个字的顺序**。

当前 `register.py` 用两枚抓包种子；实测服务端**不校验 ladon 与时间戳/设备/query 的绑定**
（旧种子配新 ts、新设备、新 argus 依然通过），所以种子可长期复用。
`NOVELDL_FANQIE_LADON` 可覆盖。

### 5.3 结果

全新环境（无任何设备文件）首次使用时会自动注册一台设备、落盘到
`<缓存目录>/fanqienovel/device.json`，随后直接读取包括锁定章节在内的全文。
`NOVELDL_FANQIE_AUTO_REGISTER=0` 可关闭自动注册。
