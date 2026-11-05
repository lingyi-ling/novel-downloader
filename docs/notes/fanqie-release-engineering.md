# 竞品发布工程审计：Fanqie-novel-Downloader

> 审计对象：[`POf-L/Fanqie-novel-Downloader`](https://github.com/POf-L/Fanqie-novel-Downloader)（仅发布运维仓部分）
> 审计日期：2026-11-05
> 审计范围：`scripts/`（19 个脚本）、`.github/workflows/`（6 份工作流，其中 `build-release.yml` 约 3400 行）、`tests/` 中的发布契约测试

该仓库**没有应用源码**，只包含发布工程与分发运维：产物终结、未签名/稳定双渠道、更新器元数据规范化、资产泄漏审计、macOS 交叉打包、issue 自动化。本文只记录设计与取舍，不含可复制的源码。

---

## 1. 它解决的问题

一个 Tauri 多平台应用（Windows / macOS / Linux / Android / iOS）的发布链路，涉及 8 个构建平台、10 个更新器 target、两种渠道（签名稳定版 / 未签名版）和 5 个平台产物命名空间。手工维护这条链路会不断出现三类事故：

1. **半发布**：资产上传到一半，Release 已经公开；
2. **错指向**：更新器清单指向上一版的包，或把便携版用户更新成安装版；
3. **泄漏**：构建产物里混入私有源码、调试符号或凭据。

它的整体答案是：**Draft 先建、按 Release ID 上传、verify 与构建并行、finalizer 审计后才公开**。

---

## 2. 值得借鉴的五点

### 2.1 Draft-first + 只认 Release ID

先创建一个草稿 Release 并解析出数字 ID，之后所有 job 只按 ID 上传（`release-assets.py upload --release-id`），**禁止按 tag 查找或新建**。缺失 ID 直接失败。

好处：并行 job 不会各自按 tag 查不到就新建，把一个发布拆成两个同 tag 的草稿；finalizer 找不到唯一 Release 时也只会报错，不会随便挑一个。

### 2.2 清单由「实际资产」重建，不信任生产者

`tauri-action` 先写一版 `latest.json`，finalizer 再把 Release 上**真实存在的资产**下载回来，重新生成 `platforms`，并用 `--check` 做严格相等校验。

这条对我们最有用：任何「构建时写元数据」的设计都会在部分失败、重跑、并行时产生偏差。发布后的对账是唯一能收敛的手段。

### 2.3 包形态专属的 target key

`windows-x86_64-nsis` 与 `windows-x86_64-portable` 是两个独立条目，Linux 的 `deb` 与 `AppImage` 也是。客户端按自己安装时的形态查找，因此**安装版与免安装版各自更新、不会互换**。

反例是「一个平台一个 key」：一旦两种形态共用 key，某次构建顺序变化就会让用户被静默换成另一种形态。

### 2.4 资产泄漏审计

`audit-release-assets.py` 做三件事，且都很实用：

- **正面允许清单**（产品名前缀 + 扩展名白名单），而不是黑名单；
- **归档成员校验**：拒绝 `..`、绝对路径、盘符、符号链接逃逸、`.git/`、`target/`、`.env`、`id_ed25519`、`*.pdb`、`*.map`、`*.rs`；
- **原始字节扫描**：1 MiB 分块，块间保留 96 字节重叠，命中私钥头或 GitHub token 正则即失败，且只报标记类型、不回显明文。

同时用「本地下载目录的文件集合必须精确等于线上资产集合」来兜住孤儿文件与缺失文件。

### 2.5 把 workflow 当规格做契约测试

`test_release_workflow.py` 不解析 YAML，而是把工作流读成文本做不变量断言：action 必须 pin 40 位 SHA、`run:` 内禁止 `${{ }}` 插值、`persist-credentials: false`、权限最小化、禁止 `pull_request_target`、文件集合固定。语法层交给 actionlint。

更进一步的做法值得学：把工作流里 heredoc 的内嵌 Python / shell **抠出来在 mock 环境里真跑**，测行为而不是测文本。

---

## 3. 不该照抄的四点

1. **Tauri 专属的清单 schema 与签名**：`platforms` 的 key 是 Tauri 的 OS-arch-bundle 语义，`signature` 是 Minisign 文本的 base64。Python 客户端读不了，直接照搬只会得到一份只有 Tauri 能用的文件。
2. **GitHub Release 别名渠道**：用 `stable` / `unsigned` 两个固定别名 Release 承载渠道。概念是对的，但依赖 `prerelease` + `make_latest` 字符串枚举与 CDN 传播，对小项目偏重。
3. **私有源码 + 大规模日志隐藏**：7 处私有 checkout、sparse wrapper、silent 包装、缓存清理，是为「公开仓库调度私有源码」服务的；自包含仓库照搬只会增加故障面。
4. **3400 行单文件工作流 + 精确计数断言**：四套流程（签名/回退/未签名/macOS 独立通道）大段复制，契约测试里 `count(...) == 9` 遍地，任何重构都要同步改测试。

---

## 4. 我们的落地结果

| 借鉴点 | 我们的实现 |
| --- | --- |
| 清单由实际资产重建 | `scripts/build_update_manifest.py`，release.py 在打包后按 `AssetSpec` 列表生成 `latest.json` 并作为资产上传 |
| 包形态专属 key | `windows-x64-installer` / `windows-x64-portable` / `android`，客户端按 `installer → portable → cli → api` 顺序查找 |
| 稳定端点 | `https://github.com/<repo>/releases/latest/download/latest.json`，客户端优先读取，GitHub API 作为回退 |
| 资产泄漏审计 | `scripts/audit_release_assets.py`，接入 release.py 的硬门禁 |
| 归档/字节扫描 | 1 MiB 分块 + 96 字节重叠，命中只报标记名 |
| workflow 契约测试 | `tests/test_workflow_contract.py`，10 条不变量，顺带把 23 个 action 全部 pin 到真实 SHA |
| 设备兼容门禁 | `scripts/check_android_alignment.py`，同时校验 zip 偏移与 ELF `p_align`（Android 15+ 的 16 KB 页） |

**没有照搬的**：别名渠道（我们用 `releases/latest`，GitHub 自己维护）、Taure 签名格式（我们的清单后续可加 Ed25519 签名，见下）、私有源码流水线。

---

## 5. 仍然存在的差距

1. **清单未签名**。竞品的 `latest.json` 也没有签名，只签安装包。我们目前依靠 HTTPS + 每个资产的 SHA-256。若要做到「仓库被攻破也无法投递恶意更新」，需要给清单加 Ed25519 签名并把公钥内置到客户端——这是下一步。
2. **没有 macOS / Linux 产物**。PyInstaller 不支持交叉编译，竞品用 zig + macOS SDK 编 Rust 可以，我们不行；只能上 macOS runner。
3. **没有未签名渠道**。当前只有正式签名发布，构建失败即中止。
