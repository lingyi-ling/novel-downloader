## 配置

`novel-downloader` 的行为由 `settings.toml` 控制。

本章节介绍如何创建、修改和使用配置文件。

### 1. 初始化配置文件

在任意工作目录下, 通过命令生成默认配置文件:

```bash
novel-cli config init
```

若已存在同名文件并需覆盖:

```bash
novel-cli config init --force
```

生成内容包括:

* `settings.toml`: 核心配置文件

---

### 2. 编辑 `settings.toml`

基础结构如下 (站点名按需替换 `<site_name>`):

```toml
[sites.<site_name>]
book_ids = [
  "123456",
  "234567"
]
login_required = false
```

最少配置只需为目标站点填写 `book_ids`:

```toml
[sites.linovelib]
book_ids = ["1234"]
```

随后即可通过 CLI 启动下载:

```bash
novel-cli download --site linovelib
```

> **说明**:
>
> 若 CLI 同时传入了 Book ID, 例如 `novel-cli download --site linovelib 5555`,
> 则 **命令行参数会覆盖配置文件中的 `book_ids`**。

---

### 3. 登录与 Cookie 配置

部分站点需要登录才能访问全部内容, 可按以下方式配置。

#### Cookie 缓存机制

程序会将登录后获取的 Cookie 自动缓存在缓存目录下 (按后端命名):

```bash
<cache_dir>/<site_name>/aiohttp.cookies
<cache_dir>/<site_name>/curl_cffi.cookies
<cache_dir>/<site_name>/httpx.cookies
```

`cache_dir` 未设置时默认为应用数据目录下的 `cache/` (Windows 为
`%LOCALAPPDATA%\novel_downloader\cache`); 如需旧的「当前目录」行为, 可在
`settings.toml` 中设置 `cache_dir = "./novel_cache"`。

如果遇到登录异常、Cookie 失效, 或不希望继续使用缓存内容, 可以直接删除对应的 `.cookies` 文件, 程序会在下次访问时尝试重新登录或提示输入 Cookie。

#### 3.1 使用账号密码登录

适用于: ESJ Zone、百合会等支持表单登录的站点。

```toml
[sites.<site_name>]
login_required = true
username = "your_username"
password = "your_password"
```

也可不写入配置, CLI 会在需要时提示输入。

---

#### 3.2 使用 Cookie 登录

适用于需要复杂登录逻辑 (如验证码、扫码登录) 的站点。

步骤:

1. 先在浏览器登录目标站点
2. 打开开发者工具 (`F12` / `CTRL + SHITF + I`)
3. 从网络请求中复制完整 Cookie 字符串
4. 写入配置:

```toml
[sites.<site_name>]
login_required = true
cookies = "完整的 Cookie 字符串"
```

> 当前 Cookie 方式不支持自动续期; 过期后请重新复制。

更多说明见: [复制 Cookies](./copy-cookies.md)

---

#### 3.3 旧配置迁移: `cookie` → `cookies`

配置加载器只读取 `cookies`。旧版文档中出现过的 `cookie` 键仍会让文件正常加载, 但**不会被使用**, 配置校验 (`validate_config`) 会在加载时输出类似告警:

```text
Configuration problem: sites.<site>.cookie: 'cookie' is not read by the loader (supported key is 'cookies').
```

推荐直接手工改名:

```toml
[sites.<site_name>]
login_required = true
cookies = "完整的 Cookie 字符串"   # 旧键 cookie 已废弃
```

需要批量检查/预览迁移时, 可调用内置的校验与迁移函数 (二者都不会自行写回文件):

```bash
# 列出当前配置中的所有校验问题（含 cookie、use_truncation 等废弃写法）
python -c "from novel_downloader.infra.config import load_config, validate_config; [print(p) for p in validate_config(load_config())]"

# 预览 migrate_config 会做的改动（返回 (迁移后的 dict, 改动说明列表)）
python -c "from novel_downloader.infra.config import load_config, migrate_config; print(migrate_config(load_config())[1])"
```

`migrate_config()` 会执行以下迁移, 且是幂等的:

| 旧写法 | 新写法 |
| --- | --- |
| `sites.<site>.cookie` | `sites.<site>.cookies` |
| `sites.<site>.use_truncation` | `sites.<site>.parser.use_truncation` |
| 缺少 `general.download_retry_times` | 填入默认值 `2` |

> 目前没有独立的 `config validate` / `config migrate` 子命令；校验在每次加载配置时自动进行（仅告警，不阻止加载），迁移通过上面的 Python 函数完成。迁移结果需要自行写回 `settings.toml`。

### 4. 配置文件查找顺序

CLI 每次运行时会按以下优先级查找配置 (高 -> 低):

(1) **命令行参数指定的文件**

```bash
novel-cli download --config path/to/settings.toml
```

(2) **当前目录下的 `./settings.toml`**

(3) **应用主目录下的 `settings.toml`** (`NOVEL_HOME` 或平台数据目录，见 [文件保存](./file-saving.md))

若存在多个匹配项, 上级优先级配置会覆盖下级。
