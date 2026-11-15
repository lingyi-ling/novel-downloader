# 更新清单签名

更新清单 `latest.json` 支持 **Ed25519** 签名。默认**未启用**：`updater.TRUSTED_UPDATE_KEYS`
为空，清单未签名也照常接受，此时的信任锚是 HTTPS 加每个资产的 SHA-256。

一旦在客户端内置可信公钥，清单就成为**唯一的信任锚**：未签名、签名不符、未知密钥，
或者清单干脆不可用，客户端都会拒绝这次更新，并且**不再回退 GitHub API**。

本文面向两类读者：想理解"它到底保护什么"的用户，和要真正启用/换钥的维护者。
发布流程本身见[更新与自动更新](updates.md)。

!!! note "当前状态"
    签名机制已实现并经过测试，但**尚未启用**（`TRUSTED_UPDATE_KEYS` 为空）。
    因此现有客户端的行为与没有签名时完全一致。下文第 3 节是启用步骤。

---

## 1. 清单格式

清单是每个 release 附带的一份小型 JSON 文档，稳定地址为：

```text
https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json
```

### 字段

| 字段 | 类型 | 是否被签名 | 说明 |
| --- | --- | --- | --- |
| `schema` | 整数 | 是 | 清单格式版本，当前恒为 `1`；必须**恰好**等于客户端认识的版本，`true`、`1.0`、`2` 都会被拒绝 |
| `version` | 字符串 | 是 | 版本号，例如 `1.4.1` |
| `tag` | 字符串 | 是 | Git 标签，例如 `v1.4.1`；去掉 `v` 后必须与 `version` 一致 |
| `repo` | 字符串 | 是 | `owner/name`；必须与客户端查询的仓库一致 |
| `generated_from` | 字符串 | 是 | 生成来源（通常是 release 页面地址），仅作说明 |
| `platforms` | 对象 | 是 | 键为 `<平台>[-<形态>]`，值为 `{name, url, sha256, size}` |
| `notes_url` | 字符串 | 是（可选） | 发布说明地址 |
| `pub_date` | 字符串 | 是（可选） | 发布时间，仅作说明 |
| `key_id` | 字符串 | **否** | 签名用的密钥标识，客户端靠它查公钥 |
| `signature` | 字符串 | **否** | 签名的 base64 编码 |

`platforms` 的每个条目都完整参与签名：

| 成员 | 约束 | 违反时 |
| --- | --- | --- |
| `name` | 纯文件名，不含 `/`、`\`、NUL | 该条目不可用（跳过） |
| `name` | 必须与键所声明的形态相符（`windows-x64-installer` 不能指向 APK） | **整份清单被拒** |
| `url` | 必须是 `https://`，无查询串或片段 | 该条目不可用（跳过） |
| `url` | 必须是 `https://github.com/<repo>/releases/download/<tag>/<name>`，且 repo、tag、文件名都与清单自身一致 | **整份清单被拒** |
| `sha256` | 64 位小写十六进制 | 该条目不可用（跳过） |
| `size` | 可选正整数 | 缺失或非法只是没有大小提示；超过 1 GiB 的条目不可用 |

"整份清单被拒"与"条目被跳过"的差别是刻意的：结构上不可用的条目无法安全使用，忽略它即可；
而一个指向别处的条目说明清单本身不可信，不能只丢一条了事。发布脚本生成的清单不会出现
这两种情况（缺摘要会让发布中止）。

### 签名覆盖范围与 canonical JSON

签名覆盖的是**除 `key_id` 和 `signature` 之外的全部字段**，包括 `platforms` 里每一项的
`name` / `url` / `sha256` / `size`。因此改下载地址、换摘要、删掉一个平台、加一个字段，
都会让签名失效。

签名输入不是磁盘上的字节，而是把解析后的对象**重新序列化**得到的规范形式：

```python
json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

即：键递归排序、`:` 和 `,` 后不留空格、非 ASCII 字符按 UTF-8 原样输出（不做 `\u` 转义）。
磁盘上的文件是这份规范字节再加一个结尾换行；**换行不参与签名**，因为它不属于重新序列化
的结果。

!!! warning "验证方必须用同一套序列化"
    签名是对"解析后的文档"做的，不是对文件字节做的。任何读取清单的验证器都必须按上面的
    规则重新序列化，否则只会得到"签名不符"。目前的验证器只有 Python 客户端
    （`updater.verify_manifest_signature`）与发布脚本（`build_update_manifest.verify_document`），
    两者共用同一份实现。

### 密钥格式与 `key_id`

| 项 | 格式 |
| --- | --- |
| 私钥 | 32 字节 Ed25519 **seed**，64 位十六进制，`--keygen` 写入文件 |
| 公钥 | 32 字节**原始** Ed25519 公钥，64 位十六进制 |
| 签名 | 64 字节 RFC 8032 签名，标准 base64（解码时 `validate=True`，不接受换行或空格） |
| `key_id` | `sha256(32 字节原始公钥).hexdigest()[:16]`，即 16 个十六进制字符 |

`key_id` 只是"用哪把公钥"的选择器，本身不被签名覆盖。客户端用它去
`TRUSTED_UPDATE_KEYS` 里查公钥，再用那把公钥验证签名。所以把一份合法签名改标成另一个
**同样受信**的 `key_id` 也会失败——查到的公钥对不上，验签自然不通过。

### 示例

未签名的清单（默认状态）：

```json
{"generated_from":"https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.4.1","platforms":{"windows-x64-installer":{"name":"NovelDownloader-setup-1.4.1-x64.exe","sha256":"…","size":43259516,"url":"https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/NovelDownloader-setup-1.4.1-x64.exe"}},"repo":"lingyi-ling/novel-downloader","schema":1,"tag":"v1.4.1","version":"1.4.1"}
```

签名后只多出两个字段：

```json
{"key_id":"0123456789abcdef","signature":"<base64, 64 字节>", "...": "其余字段不变"}
```

---

## 2. 启用后的行为

`TRUSTED_UPDATE_KEYS` 非空时，`check_for_update` 把清单当作唯一信任锚。所有拒绝都发生在
"下载任何资产之前"：

| 情况 | 结果 | `UpdateCheck.error` |
| --- | --- | --- |
| 清单有合法签名，且 `key_id` 在表里 | 正常返回，`source="manifest"` | `None` |
| 清单没有 `signature` / `key_id` | 拒绝 | `untrusted-manifest: manifest is not signed but a trusted key is configured` |
| `key_id` 不在 `TRUSTED_UPDATE_KEYS` 里 | 拒绝 | `untrusted-manifest: manifest is signed with an unknown key: …` |
| 签名不匹配（内容被改、密钥不对、base64 坏了） | 拒绝 | `untrusted-manifest: manifest signature does not verify: …` |
| 表里的公钥本身写错（非十六进制、长度不对） | 拒绝（fail-closed，不会崩溃） | `untrusted-manifest: manifest signature does not verify: …` |
| 签名有效但 `schema` / `tag` / `repo` / URL 不合法 | 拒绝 | `untrusted-manifest: …`（具体原因） |
| 清单**根本取不到**（404、网络失败、非 HTTPS 重定向、超过 1 MiB） | 拒绝 | `manifest-unavailable` |

关键点：以上每一种都**不会**再去请求 `https://api.github.com/repos/…/releases/latest`。
这正是设计目的——否则攻击者只要删掉 `latest.json`，就能把客户端逼回那条他也控制得住的
API 路径，验签形同虚设。

!!! danger "启用后的硬约束：每次发布都必须带 `latest.json`"
    启用签名后，漏传清单等于对所有已固定公钥的客户端**关闭自动更新**。发布脚本
    （`scripts/release.py`）本来就会从实际上传的资产重建并上传 `latest.json`，并且把它
    放在**最后**上传，确保清单指向的文件都已就位；不要手工删除或跳过它。

### 私钥丢失的后果

私钥是唯一能生成新清单签名的东西，客户端只认公钥。因此：

- 私钥丢失 = **无法再为任何新版本生成客户端接受的清单**。已固定公钥的客户端会一直
  报 `untrusted-manifest` 或 `manifest-unavailable`，并且**不会**回退 API。
- 没有远程"重置"手段。恢复只能靠**手工分发**一个内置了新公钥的新版本：用户下载安装后，
  新版本才会重新信任新的签名。
- 旧版本不会自行恢复，它们会停留在最后一个能验证的版本上。

所以私钥必须离线备份（至少两份、异地存放），并且绝不进入仓库、CI 变量、日志或截图。

---

## 3. 启用步骤

### 3.1 生成密钥对

```bash
python scripts/build_update_manifest.py --keygen ~/.novel_downloader/update.key
```

输出形如：

```text
private seed: C:\Users\<you>\.novel_downloader\update.key (keep it out of the repository)
public key  : <64 位十六进制>
key id      : <16 位十六进制>
trusted key entry for updater.TRUSTED_UPDATE_KEYS:
    "<key id>": "<public key>",
```

行为细节：

- 私钥以 `O_EXCL` 创建，**拒绝覆盖已有文件**；想换路径请先自行移走旧文件。
- POSIX 上以 `0600` 创建；Windows 忽略创建模式，脚本会再 `chmod 0600`（尽力而为），
  实际权限继承父目录 ACL，请自行确认。
- 只打印私钥的**路径**，从不打印内容。

### 3.2 把公钥内置到客户端

把输出里的那一行填进 `src/novel_downloader/apps/common/updater.py`：

```python
TRUSTED_UPDATE_KEYS: Mapping[str, str] = {
    "<key id>": "<public key>",
}
```

这是一个**编译进程序**的常量，不是 `settings.toml` 选项，也没有环境变量或运行时开关。
改它必须重新构建并发布客户端——这正是"固定公钥"的意义：运行中的程序无法被说服去信任
另一把钥匙。

填完后建议跑一遍相关测试，它们锁定了格式与 fail-closed 行为：

```bash
.venv/Scripts/python.exe -m pytest tests/apps/test_update_manifest_signature.py -q
.venv/Scripts/python.exe -m pytest tests/test_build_update_manifest.py -q
```

### 3.3 发布时签名

```bash
# 一站式发版：构建 → 重建 latest.json → 签名 → 审计 → 打 tag → 上传
python scripts/release.py --sign-key ~/.novel_downloader/update.key
```

成功时会打印 `==> latest.json signed (key id <id>)`；`--dry-run` 会提前告诉你这次会不会签名。
签名失败（文件读不到、不是十六进制、不是 32 字节）会让发布在**打 tag 之前**中止。

只想单独构建/签名清单时：

```bash
python scripts/build_update_manifest.py \
    --release-json payload.json --version 1.4.1 --tag v1.4.1 \
    --repo lingyi-ling/novel-downloader \
    --sign-key ~/.novel_downloader/update.key \
    --out latest.json
```

### 3.4 复核已发布的清单 {#verify-published-manifest}

`--check` 会用 release payload 重新推导一份清单并与现有文件逐字段比较（忽略
`signature` / `key_id`），`--public-key` 额外验证签名：

```bash
python scripts/build_update_manifest.py \
    --release-json payload.json --version 1.4.1 --tag v1.4.1 \
    --repo lingyi-ling/novel-downloader \
    --check latest.json --public-key <64 位十六进制公钥>
```

`payload.json` 是 `releases/tags/v1.4.1` 的 API 响应。退出码 `0` 表示一致且签名有效；
不一致会打印 `stale: <字段>`，签名不符会打印
`stale: the existing manifest signature does not verify against the given public key`。

### 3.5 启用检查单

- [ ] 私钥已离线备份，且确认不在仓库、CI 变量、日志或截图中。
- [ ] 公钥与 `key_id` 已填进 `TRUSTED_UPDATE_KEYS`，测试通过。
- [ ] 发布流程固定带 `--sign-key`，并且每次都上传 `latest.json`。
- [ ] 发布后用 `--check --public-key` 复核过至少一次。
- [ ] 明确知道：启用后清单缺失 = 该版本客户端不再自动更新。

---

## 4. 滚动发布与换钥

### 4.1 为什么不能"直接换一把钥匙"

清单只能携带**一个**签名和一个 `key_id`。所以不存在"新旧签名并存"的过渡格式。
如果新版本一上线就用新密钥签名，那么仍在信任旧密钥的客户端会：

1. 看到 `key_id` 不在表里 → 拒绝清单；
2. **不回退** API；
3. 从此不再自动更新。

这是刻意的 fail-closed，但也意味着换钥必须有先后顺序，不能一步到位。

### 4.2 三阶段换钥

| 阶段 | 客户端改动 | 清单用哪把钥匙签名 | 结果 |
| --- | --- | --- | --- |
| A（过渡版本） | `TRUSTED_UPDATE_KEYS` 同时包含**旧**、**新**两把公钥 | 旧 | 现有客户端照常升级到这个过渡版本；过渡版本同时信任新旧两把 |
| B（切换） | 无 | **新** | 过渡版本及更新的客户端验签通过；比过渡版本更旧的客户端开始拒绝清单并停止自动更新 |
| C（收尾） | 从表中移除**旧**公钥 | 新 | 只剩新钥匙受信；此时再泄露旧私钥也没有影响 |

阶段 A 的版本必须**先于**阶段 B 发布，并且要给用户留出更新时间。因为清单里没有
"最低客户端版本"字段，客户端也无法上报自己的版本，所以这个门槛只能靠**时间窗口人工判断**，
不能由清单强制。

### 4.3 关于客户端版本门槛

清单 schema（当前 `1`）没有 `min_client_version` 之类的字段，客户端也不会因为版本太旧而
"请求升级"——它只是拒绝清单。因此：

- 阶段 B 之后，停留在阶段 A 之前的用户会**静默地**失去自动更新能力（桌面端不弹错误条），
  需要手动下载新版本。
- 如果这个代价不可接受，就不要换钥；或者把阶段 A 的版本当作一次"强制分发"机会，
  在发布说明里写清楚。
- 未来若要在清单里表达版本门槛，需要同时改 `schema` 和客户端解析器；`schema` 不匹配会被
  当前客户端直接拒绝，所以升级 `schema` 本身就是一次全量换钥级别的操作。

---

## 5. 客户端差异

| 客户端 | 读取 `latest.json` | 验签 | 清单不可用时的行为 |
| --- | --- | --- | --- |
| 桌面端 | 是（`update_state` / `check_update_async` 走 `check_for_update`） | 是 | 不回退 API，`error` 记录在 `update_state` 的 payload 里 |
| CLI | 是（`novel-cli update` 与命令结束后的静默提示都走 `check_for_update`） | 是 | 不回退 API；`novel-cli update` 打印 `Update check failed: untrusted-manifest: …`，静默提示则什么都不说 |
| 安卓 | 是 | **否** | 始终回退 GitHub API |

几点必须如实说明：

- **安卓目前不验签**。`UpdateChecker.parseManifest` 只检查 `schema`、`version`、`platforms`
  和 HTTPS/摘要，完全忽略 `key_id` 与 `signature`；未知字段会被忽略，所以签名清单不会让
  安卓解析失败，但也**不会**给它任何额外保护。安卓的信任锚仍然是 HTTPS 加 APK 的 SHA-256。
  在安卓实现验签之前，启用清单签名只保护桌面端与 CLI。
- 桌面端旧桥接 `check_update`（仅当前端缺少 `check_update_async` 时才会用到）直接读
  GitHub API，不经过 `check_for_update`，因此不在固定公钥的保护范围内。桌面端与前端资源
  是一起发布的，实际构建里走不到这条路径，但它是"验签只在 `check_for_update` 里生效"
  这一事实的边界。
- 桌面端 UI 不会把检查错误显示成提示条：验签失败时侧栏只是不出现"有新版本"。要看到
  具体原因，用 `novel-cli update --json`（同一套逻辑）或读 `update_state` 的 `error` 字段。
- 库调用方与本地 API 不做更新检查，与此机制无关。

---

## 6. 故障排查 {#signing-troubleshooting}

先记住两条：**不要为了排查去改密钥**，**不要打印或粘贴私钥内容**。`key_id` 和公钥都是
公开信息，可以安全地出现在日志和 issue 里。

### 症状对照

| 现象 | 多半是 | 处理 |
| --- | --- | --- |
| CLI 报 `untrusted-manifest: manifest is not signed but a trusted key is configured` | 发布时没带 `--sign-key`，或清单被换成了未签名版本 | 用 `--sign-key` 重新生成并重新上传 `latest.json` |
| CLI 报 `untrusted-manifest: manifest is signed with an unknown key: '<id>'` | 签名用的钥匙与客户端内置的不一致（旧钥匙、测试钥匙、或客户端还没更新到信任新钥匙的版本） | 比对 `key_id` 与表里的键；确认该客户端版本是否已内置这把钥匙 |
| CLI 报 `untrusted-manifest: manifest signature does not verify: …` | 签名后文档又被改过（重新生成、手工编辑、上传了旧文件），或表里的公钥与私钥不配对 | 从 release payload 重新构建并签名，再复核；不要手改清单 |
| CLI 报 `manifest-unavailable` | 清单取不到：release 没带 `latest.json`、仍是草稿、上传失败、网络不通、或被重定向到明文 HTTP | 确认 release 已发布且资产存在；确认网络能访问 `github.com` |
| 桌面端"有新版本"的提示条不出现，也没有报错 | 检查被拒绝，而桌面端不展示检查错误 | 用 `novel-cli update --json` 看 `error` |
| `release.py` 在打 tag 前报 `cannot sign latest.json: …` | 私钥文件读不到、不是十六进制、不是 32 字节 | 确认路径与权限；确认文件内容是 `--keygen` 写出的那 64 个字符 |

### 定位步骤

1. **确认清单确实带签名**（只看公开字段，不要看私钥）：

   ```bash
   python -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('key_id'), 'signature' in d)" latest.json
   ```

2. **确认 `key_id` 与内置公钥匹配**。`key_id` 是公钥摘要的前 16 个十六进制字符：

   ```bash
   python -c "import hashlib,sys; print(hashlib.sha256(bytes.fromhex(sys.argv[1])).hexdigest()[:16])" <公钥十六进制>
   ```

   两者不一致，说明签名用的不是这把钥匙——先查发布时 `--sign-key` 指向了哪个文件，
   而不是去改客户端里的公钥。

3. **用公钥复核签名**，不接触私钥：见 [3.4](#verify-published-manifest)。

4. **确认没有"签名后又被改"**：签名覆盖全部字段，任何重新生成（哪怕内容等价）都会
   改变规范字节。正确做法是从 release payload 重新构建，而不是编辑已签名的文件。

5. **区分"验签失败"与"清单不可用"**：前者是 `untrusted-manifest: …`，说明清单拿到了但
   不可信；后者是 `manifest-unavailable`，说明根本没拿到。两者的修复方向完全不同。

### 不要做的事

- 不要为了让更新恢复而清空 `TRUSTED_UPDATE_KEYS`——那等于永久关闭验签，且旧客户端
  无法远程改回。
- 不要把私钥写进仓库、CI secret 之外的任何地方，也不要为了"验证一下"把它 `cat` 到终端
  或贴进 issue。
- 不要手工编辑已签名的 `latest.json`；重新构建、重新签名。
- 不要删除或跳过 `latest.json` 来"临时绕一下"。

---

## 7. 它保护什么，不保护什么

**保护**：仓库或发布账号被攻破时，攻击者可以改写 `latest.json`、替换资产，但没有私钥就
签不出客户端接受的清单。由于验签失败不回退 API，攻击者也无法通过删除清单把客户端逼回
另一条他控制的路径。签名把 `name` / `url` / `sha256` / `size` 绑在一起，因此"指向别处的
安装包 + 自洽的摘要"这种替换会被拒绝。

**不保护**：

- **安卓客户端**——目前不验签（见第 5 节）。
- **签名机器或私钥本身被攻破**——能拿到私钥的人就能签出合法清单，这是该机制的信任根。
- **用户手动下载安装**——绕过更新链路的一切约束。
- **客户端二进制被替换**——公钥是编译进去的，能替换二进制的人已经不需要走更新链路。
- **可用性**——`manifest-unavailable` 会让该轮检查失败且不回退；这是 fail-closed 的代价，
  不是缺陷。

更完整的更新链路威胁模型见[安全说明 § 更新链的威胁模型](security.md#update-chain-threat-model)，
发布侧的门禁见[更新与自动更新 § 发布前的资产审计](updates.md#pre-release-asset-audit)。
