# 存储与备份

`novel-cli storage` 负责查看应用数据目录的磁盘占用，并提供章节数据库的
`backup` / `verify` / `restore` 子命令。本页说明**备份到底包含什么、失败时会发生
什么、如何校验与恢复**；完整的选项速查表见
[CLI 用法 - storage 子命令](./cli-examples.md#storage-subcommand)。

## 备份覆盖什么 {#what-is-backed-up}

`storage backup` 只备份 `raw_data` 下的章节数据库，不是整机迁移工具：

| 位置 | 内容 | 进入备份 |
| --- | --- | --- |
| `raw_data/<site>/<book>/chapter.<stage>.sqlite` | 章节正文、章节元数据、续爬标记 | **是** |
| `raw_data/<site>/<book>/book_info.<stage>.json` | 书籍信息与目录 JSON | 否 |
| `settings.toml` | 应用配置 | 否 |
| `cache/`（或配置的 `cache_dir`） | Cookie、token、登录态、缓存 | 否 |
| `downloads/`（或配置的 `output_dir`） | 导出的 TXT / EPUB / HTML 等成品 | 否 |
| 日志、封面、图片、音频 | 运行产物与媒体文件 | 否 |

因此：

* 恢复备份**不会**恢复登录态；换机或清理 `cache/` 后需要重新登录。
* 恢复的是章节数据；书籍信息 JSON 可以重新抓取，导出的成品需要重新导出。
* 备份文件是**明文 SQLite**，可能包含付费或私密正文。请放在受控目录，不要提交到
  Git，也不要放到公开网盘。Cookie 与凭据的存储位置见
  [安全说明](./security.md#cookie-credential-storage)。

## 先看磁盘占用与清理 {#disk-usage}

```bash
# 四个数据目录的占用、所在卷剩余空间、最大的子目录
novel-cli storage

# 机器可读输出
novel-cli storage --json

# 只预览 --clean 会删除什么，不删除任何文件
novel-cli storage --clean --dry-run
```

`--clean` 会删除整个 `raw_data`、`cache`、`downloads`、`logs` 目录，属于批量清理，
**会连同章节数据库一起删除**。在清理 `raw_data` 前先做一次 `storage backup`，并确认
备份已完成校验。`--dry-run` 的完整输出结构见
[CLI 用法](./cli-examples.md#storage-subcommand)。

## 备份：storage backup {#storage-backup}

```bash
novel-cli storage backup --out ~/novel-backups
novel-cli storage backup --out ~/novel-backups --keep 7
novel-cli storage backup --out ~/novel-backups --keep 7 --dry-run
novel-cli storage backup --out ./backups --json
```

### 源发现规则 {#source-discovery}

备份从当前配置的 `general.raw_data_dir` 开始，规则是固定的：

1. 递归查找文件名严格匹配 `chapter.<stage>.sqlite` 的文件；`<stage>` 不能包含
   `.`、`/`、`\`。因此 `chapter.raw.sqlite`、`chapter.cleaner.sqlite` 会被备份，
   `chapter.raw.sqlite.bak`、`chapter.raw.20260102T030405Z.sqlite` 不会被当作源。
2. 只接受**普通文件**。符号链接会被忽略，避免链接到数据目录之外的文件被卷入备份。
3. 结果按路径排序后逐个处理，源数据库全程只读。
4. 如果 `--out` 是 `raw_data` 的真子目录，该子树会从源发现中排除，避免把旧备份
   再备份一遍。若 `--out` 恰好等于 `raw_data` 根目录，不做排除，但备份文件名带
   时间戳，本来就不匹配源文件名规则。
5. 一个数据库都没找到时，命令成功退出（`0`），报告 `count: 0`，并且**不执行**
   `--keep` 轮转，已有旧备份保持原样。

`--out` 支持 `~` 与环境变量（Windows 下为 `%VAR%`），不存在时会创建。若该路径
已存在但不是目录，属于用法错误，退出码 `2`，不会写入任何备份。

### 输出布局与命名 {#backup-layout}

每个源数据库生成一个完整快照，镜像原来的站点/书籍目录：

```text
<out>/<site>/<book>/chapter.<stage>.<YYYYMMDDTHHMMSSZ>.sqlite
```

时间戳是 UTC。两个备份可能落在同一秒（并发运行、定时任务与手动任务重叠、
时钟精度不足），此时用 `O_CREAT|O_EXCL` 原子抢占文件名，后来者依次尝试
`chapter.<stage>.<时间戳>.1.sqlite`、`.2`、……最多到 `.1000`。

* **绝不覆盖已有文件**。已存在的同名备份、占位目录或其他进程正在创建的文件
  都会被跳过。
* 同一秒的 1001 个可用名字全部被占用时，命令以 `sequence_exhausted` 失败，
  并按整组回滚处理（见下文）。
* 备份是**完整快照**，不是增量；`--keep` 只做轮转，不会压缩或合并历史备份。

### 默认写后校验 {#post-write-verify}

每个副本发布后都会**重新读取已写出的字节**并校验，而不是只确认复制过程没报错。
校验规则与 `storage verify` 相同：

* WAL `-wal` sidecar 的校验和（备份输出本身会切到 `journal_mode=DELETE`，通常
  没有 sidecar）；
* SQLite `PRAGMA integrity_check`；
* schema 版本：旧版本可以接受，**新于当前构建**的版本不接受；
* 必需表 `chapters` 与必需列 `id`、`title`、`content`。

只有整组副本都通过，才算备份成功；报告中 `files[].verified` 与顶层 `verified`
为 `true`。

### `--no-verify` 的确切含义 {#no-verify}

`--no-verify` 跳过的是上面这套**发布后完整校验**（与 `storage verify` 同一套规则）。
快照写入路径本身仍会重新读取临时文件做基础完整性检查，但不会检查 schema 版本与
必需列。

它只适用于一种场景：数据库由**更新版本**写入，当前构建会把它判为“schema 过新”，
但你又必须保留这些字节。此时：

* 备份仍会写出，报告中 `verified` 为 `false`；
* 当前构建的 `storage restore` 会以 schema 过新拒绝恢复它；
* 需要用更新版本恢复，或按 [校验归档副本](#verify-archived-backup) 自行确认后
  由更新版本处理。

常规备份不要使用 `--no-verify`。未经完整校验的副本不能假定可恢复。

### 失败时整组回滚（含 Ctrl+C） {#rollback}

备份集只有成套才有意义，残缺的一组会让下一次重试误以为备份已经完整。因此：

* 当前副本写入或校验失败时，先删除该副本及其 `-wal` / `-shm` / `-journal`；
* 再删除**本次运行已经写出的全部副本**；
* 命令以 `1` 退出，**不执行** `--keep` 轮转；
* `--out` 下原有的旧备份和 `raw_data` 中的源数据库都不受影响；源库只读；
* 回滚删除的是文件，本次刚创建的目录可能留下空目录。

**Ctrl+C 也走同一套回滚**：中断发生在备份或写后校验期间时，已写出的整组副本
会被删除，随后异常继续向上抛出，CLI 按惯例输出 `Aborted.` 并以 `1` 退出。
与普通运行期失败不同，`--json` 在 Ctrl+C 场景下**不会**再补一行失败报告；如果
脚本需要机器可读的失败原因，应把中断视为独立的退出条件。

### `--keep` 轮转 {#keep-retention}

`--keep N` 在**成功发布并校验**新备份之后执行：

* `0`（默认）关闭轮转；负数退出 `2`。
* 只考虑文件名严格匹配 `chapter.<stage>.<UTC>.sqlite` 或
  `chapter.<stage>.<UTC>.<n>.sqlite` 的**普通文件**。目录、符号链接、手工改名
  的文件、旧版本留下的其他命名一律不碰。
* 按“所在书籍目录 + stage”分组。同一本书的 `raw` 与 `cleaner` 各自保留 `N` 份，
  不会互相挤掉。
* 每组按 `(UTC 时间戳, 序号, 路径)` 从新到旧排序，保留最新 `N` 份。
* 本次运行刚写出的文件永远受保护；即使系统时钟回拨，也只会多留一份，不会删掉
  新备份。
* `--dry-run` 只报告会删除哪些旧文件，**不删除任何东西**，但新备份仍会正常写入。
  它必须与 `--keep N`（`N > 0`）同用，否则退出 `2`。

### `retention_failed` 的语义 {#retention-failed}

轮转失败与备份失败是两种不同结果：

* 新备份已经完整写出并通过校验，**会被保留**；
* 某个旧备份删除失败，或某个目录无法读取，轮转无法完成；
* 命令以 `1` 退出，提醒定时任务/监控目录正在增长；
* `--json` 为 `ok: false`、`error: "retention_failed"`、`count` 非零、
  `files` 仍列出新备份，`retention.failed` 列出删除失败或无法读取的路径；
* 不会回滚新备份；已成功删除的旧备份不会恢复，删除失败的旧备份保留原地。

修复权限、磁盘或目录问题后重新运行即可；重跑会再生成一份新备份，并再次尝试轮转。

### 退出码 {#backup-exit-codes}

| 退出码 | 含义 |
| --- | --- |
| `0` | 备份成功；包括“没有找到任何章节数据库”的情况 |
| `1` | 运行期失败：源不可读/损坏、WAL 损坏、schema 过新、序号耗尽、I/O 失败、写后校验失败、配置无法读取、内部错误，或 `retention_failed` |
| `2` | 用法错误：缺少 `--out`、`--out` 指向文件、`--keep` 为负数、`--dry-run` 未带 `--keep` |

用法错误不会输出 `--json` 报告。

## 校验：storage verify {#storage-verify}

```bash
novel-cli storage verify
novel-cli storage verify --json
```

它只扫描 `raw_data` 下的**在用**章节数据库，不扫描 `--out` 的备份目录，也不会
复制、重命名或修复任何文件。

### 只读与实际副作用 {#verify-read-only}

校验以只读方式打开数据库（SQLite URI `mode=ro`），并额外设置
`PRAGMA query_only = ON`。遇到无法以只读方式打开的 WAL 数据库时，命令会把主文件
与 `-wal` / `-shm` 复制到私有临时目录，在副本上打开；源文件字节不变。

需要知道的 SQLite 行为：**以只读方式打开 WAL 模式的数据库，SQLite 可能创建空的
`-wal` / `-shm` sidecar**，用于共享内存协调。这是正常的空文件，不是数据，也不代表
数据库被修改或损坏。`storage verify` 不会改变已有主文件与 WAL 的内容。如果目录
权限不允许创建这些 sidecar，代码会走上面的临时副本路径。

### 检查项与什么算 corrupt {#verify-checks}

每个数据库依次检查：

1. **WAL 校验和**：在 SQLite 打开主文件之前检查 `-wal`。文件末尾因崩溃被截断的
   torn tail 是安全的，SQLite 会自行丢弃；只有“坏帧之后还有完好帧”的中段损坏
   才会报告为 `wal_damaged`。这样可以避免 SQLite 静默忽略坏 WAL、丢掉尚未
   checkpoint 的已提交章节。
2. **SQLite 完整性**：`PRAGMA integrity_check` 必须返回 `ok`。
3. **Schema 版本**：旧于当前构建可以接受；新于当前构建不接受。
4. **必需结构**：必须存在 `chapters` 表，以及 `id`、`title`、`content` 列。

任何一项不通过都会计入 `corrupt`，并在 `errors[]` 中给出 `path` 与人类可读的
`reason`。这里的 “corrupt” 表示**当前构建无法安全使用该数据库**，包括：

* WAL 中段损坏；
* 文件不是 SQLite 数据库（`not a database`）；
* 完整性检查失败、文件 malformed；
* schema 过新；
* 缺少 `chapters` 表或必需列；
* 其他 SQLite 错误、I/O 或权限错误导致无法读取。

所以 `corrupt` 不等于“物理磁盘一定坏了”，但都意味着需要恢复或重新获取。

### JSON 字段、进度与中断 {#verify-json}

`--json` 只向 stdout 输出一行：

```json
{"ok":true,"total":12,"healthy":12,"corrupt":0,"errors":[]}
```

| 字段 | 含义 |
| --- | --- |
| `ok` | `true` 当且仅当未被中断且 `corrupt == 0` |
| `total` | 扫描时发现的数据库总数 |
| `healthy` | 已完成校验且通过的数据库数 |
| `corrupt` | 已完成校验但失败的数据库数（`checked - healthy`） |
| `errors[]` | 每个损坏项含 `path`（家目录前缀显示为 `~`）与 `reason` |
| `interrupted` | 仅在 Ctrl+C 中断时出现，值为 `true` |

人类可读模式下，扫描开始和每个数据库的进度都写到 **stderr**：

```text
Scanning for chapter databases...
Checking 3/12: ~/.../chapter.raw.sqlite
~/.../chapter.raw.sqlite: ok
```

因此大库或慢盘不会看起来像卡死，stdout 也不会被进度污染。按 Ctrl+C 时：

* 中断后停止继续遍历；汇总只统计已经返回结果的数据库，正在检查的数据库不会被
  算作 healthy；
* stderr 打印 `Interrupted after checking X of Y database(s): H healthy, C corrupt.`；
* 随后按 CLI 惯例输出 `Aborted.` 并以 `1` 退出；
* `--json` 仍输出完整一行，`ok` 为 `false`、`interrupted` 为 `true`；
  `total` 仍是扫描总数，`healthy` / `corrupt` 只反映**已完成**的部分，`errors`
  只含已确认的损坏项；
* 如果中断发生在扫描/发现阶段（还没打开任何数据库），`--json` 同样输出一行，
  此时 `total` 为 `0`、`interrupted` 为 `true`，不会出现空 stdout；
* 部分结果**永不**以 `0` 退出。

退出码：全部健康（包括一个数据库都没有）为 `0`；任意损坏或被中断为 `1`。

`errors[].reason` 是给人看的本地化文本，不是稳定错误码。需要按类别分支时，应使用
`storage backup --json` 的 `error` 字段，而不是解析 verify 的 `reason`。

## `--json` 失败契约 {#backup-json-failure}

`storage backup --json` 在**运行期失败**时仍只向 stdout 输出一行，不会出现空输出：

```json
{"command":"storage backup","ok":false,"error":"integrity_failed",
 "message":"...","source":"...","timestamp":"20260102T030405Z",
 "out":"...","count":0,"bytes":0,"files":[],"verified":false,
 "verification_failed":false,"retention":{"keep":0,"dry_run":false,
 "candidates":[],"removed":[],"failed":[]}}
```

关键字段：

| 字段 | 失败时的值 |
| --- | --- |
| `ok` | `false` |
| `error` | 稳定类别，见下表 |
| `message` | 人类可读原因（普通运行期失败还会写入 stderr） |
| `source` | 触发失败的源数据库；扫描源目录失败或配置加载失败时为 `null` |
| `count` / `bytes` / `files` | 整组回滚后为 `0` / `0` / `[]` |
| `verified` | `false` |
| `verification_failed` | 写后校验失败时为 `true`，否则 `false` |
| `verification_error` | 仅写后校验失败时出现：`source`、`target`、`reason` |
| `retention` | 失败时为空报告；`retention_failed` 例外，见下文 |

稳定 `error` 类别：

| `error` | 含义 |
| --- | --- |
| `not_database` | 源文件不是 SQLite 数据库 |
| `integrity_failed` | 主库完整性检查失败 |
| `wal_damaged` | `-wal` 校验和链在中段损坏 |
| `schema_unsupported` | schema 新于当前构建，或缺少必需表/列 |
| `sequence_exhausted` | 同一秒可用的备份文件名全部被占用 |
| `io_error` | 源目录、目标目录或文件的 I/O 失败 |
| `database_error` | 其他 SQLite 错误 |
| `verification_failed` | 写后校验的兜底类别（正常情况下会细化到上面的具体类别） |
| `retention_failed` | 新备份已成功保留，仅旧备份轮转失败；**不回滚**，`count` 非零 |
| `config_error` | 配置无法读取；`source` 为 `null`，没有备份写出 |
| `internal_error` | 未预期的异常 |

补充约定：

* 所有运行期失败都会先删除本次已经写出的副本，再输出报告，因此除
  `retention_failed` 外，`count` 为 `0`、`files` 为空。
* 写后校验失败还会带 `verification_failed: true` 与
  `verification_error`；`error` 取该失败的具体类别。
* `retention_failed` 是唯一保留新备份的 `ok: false`：`count` / `files` 仍描述
  新备份，失败信息在 `retention.failed`。它也是唯一**不含 `source`** 的失败报告，
  因为失败发生在轮转而不是某个源数据库。
* **用法错误**（缺少 `--out`、`--out` 指向文件、`--keep` 为负数、`--dry-run`
  未带 `--keep`）以 `2` 退出，且不输出 JSON。
* **Ctrl+C** 会整组回滚，但不会输出上述 JSON 失败行；CLI 直接按中断处理并
  以 `1` 退出。

## 在线备份、WAL 与只读打开 {#wal}

`storage backup` 使用 SQLite 的**在线备份 API**（`Connection.backup()`），不是直接
复制文件：

* 它读取一个一致的事务快照，包含已经提交、仍停留在 `-wal` 中的页；不包含尚未
  提交的事务。
* 因此应用正在运行时也可以备份，不会因为 WAL 尚未 checkpoint 而漏掉已提交章节。
* 备份输出会被切到 `journal_mode=DELETE`，得到一个自包含的单文件，不带
  `-wal` / `-shm`。
* 临时文件写入后会 `fsync`，再以原子替换发布；失败时临时文件会被删除。

**不要手工只复制 `chapter.<stage>.sqlite` 主文件**。如果数据库处于 WAL 模式，
只复制主文件会丢掉 `-wal` 中尚未 checkpoint 的已提交数据，得到一个看起来正常、
实际缺章的副本。需要快照时用 `storage backup`，或至少把主文件与 `-wal` / `-shm`
作为一个整体处理。

WAL 损坏的处理：

* 备份和校验都会在打开主库前检查 `-wal` 的帧校验和。
* 文件末尾被崩溃截断的 torn tail 是安全的，SQLite 会丢弃；**不要**把它当成损坏。
* “坏帧之后还有完好帧”的中段损坏会被拒绝（`wal_damaged`）。不要为了“修好”而
  直接删除 WAL，否则会永久丢掉其中已提交的章节；应先从备份恢复，或交给能读取
  该 WAL 的版本处理。
* 如果 `-shm` 无法创建/打开（例如目录只读，或文件被单独复制走），代码会把主文件
  与 sidecar 复制到临时目录，在副本上恢复 WAL；源文件仍不被写入。
* `storage backup` 读取源库时同样可能留下空的 `-wal` / `-shm` sidecar；这是正常
  的只读打开行为，不改变源库内容，也不影响备份结果。

为了获得更可预测的“某一时刻”快照，建议在备份时暂停正在进行的下载/处理任务。
在线备份本身是安全的，但运行中的任务会让快照对应一个移动中的状态（数据库仍然
事务一致）。

## 常见恢复流程 {#restore}

### 恢复前 {#before-restore}

1. **停止所有写入者**：桌面端、正在下载的 CLI、`follow` 轮询、本地 API 服务等。
   恢复会用原子替换安装目标文件并删除旧的 `-wal` / `-shm` / `-journal`；如果有
   进程仍打开目标库，Windows 上替换可能失败，其他平台上旧连接也可能继续写入。
2. 先跑一次 `novel-cli storage verify`，确认损坏范围，避免恢复错书或错 stage。
3. 确认备份可用。默认 `storage backup` 已在写入后校验；如果是移动过、旧版本生成
   或 `--no-verify` 写出的副本，先按
   [校验归档副本](#verify-archived-backup) 检查。

### 从备份恢复一个数据库 {#restore-one}

```bash
# 备份布局完整时，目标路径由 <site>/<book> 推导
novel-cli storage restore \
  --from ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite \
  --force

# 文件被移动/改名，或想恢复到别的位置时，显式给出 --to
novel-cli storage restore \
  --from ./backup.sqlite \
  --to ./raw_data/qidian/1010868264/chapter.raw.sqlite \
  --force

# 只预览会恢复哪些文件，不写入任何内容
novel-cli storage restore --from ~/novel-backups --dry-run --json
```

规则：

* `--from` 可以是**单个备份文件**或**备份目录**。文件必须是有效章节数据库：
  完整性、schema 版本、必需列都会先校验，失败则不写入任何东西；目录则逐个校验
  其中所有备份，任何一个不通过都会在写入前中止整组。
* 目标必须位于 `raw_data_dir` 内，文件名必须形如 `chapter.<stage>.sqlite`。
  目标（或其任一路径组件）是符号链接时拒绝写入；把备份恢复到源自身、或把整组
  恢复到备份目录内部，同样拒绝。
* 目标已存在时必须加 `--force`（`-y` / `--yes` 是别名），否则以用法错误 `2`
  退出，且不写入；目标不存在时不需要 `--force`。
* 覆盖已有数据库前，旧库会保存为同目录的
  `chapter.<stage>.sqlite.pre-restore-<UTC>.bak`。旧库可读时用在线快照保存；
  旧库本身已损坏、SQLite 打不开时，退化为逐字节复制（连同 sidecar），确保原始
  数据还在。
* 安装新库后会删除目标旁的旧 `-wal` / `-shm` / `-journal`，避免 SQLite 把属于
  旧文件的日志应用到新库。
* `--dry-run` 只校验并报告计划，**不写入任何文件**；它不要求 `--force`，因此可以
  安全地预演一次覆盖。校验失败仍以 `1` 退出并给出 `--json` 失败报告。
* `--no-verify` 只放宽 **schema** 检查（允许恢复由更新版本写出的库），SQLite
  完整性检查始终执行，物理损坏的文件永远不会被安装。
* `--json` 成功报告含 `command`、`ok`、`from`、`target`、`restored`、`files[]`、
  `skipped`、`dry_run`、`error`、`message`；单文件恢复时还会带上原有的
  `replaced_existing`、`pre_restore_backup`、`schema_version`、`chapters`、
  `bytes`、`sha256`。失败时 `ok` 为 `false`、`error` 为稳定类别（见下），
  `restored` 为 `0`。

稳定 `error` 类别：

| `error` | 含义 |
| --- | --- |
| `source_missing` | `--from` 指向的文件或目录不存在 |
| `no_backups` | 目录里没有任何符合备份命名的文件 |
| `not_database` | 源文件不是 SQLite 数据库 |
| `integrity_failed` | 源库完整性检查失败 |
| `wal_damaged` | `-wal` 校验和链在中段损坏 |
| `schema_unsupported` | schema 新于当前构建，或缺少必需表/列 |
| `database_error` | 其他 SQLite 错误 |
| `io_error` | 读取源、写临时文件或原子替换失败 |
| `config_error` | 配置无法读取；没有恢复任何内容 |
| `internal_error` | 未预期的异常 |

**整组回滚**：目录恢复在写入前先校验全部源；安装阶段任一目标失败（含 Ctrl+C）
都会把本次已替换的目标按相反顺序恢复原状——有 `.pre-restore-*.bak` 的移回，
原本不存在的目标删除——并清理新文件留下的 sidecar。回滚完成后中断继续向上抛出，
CLI 按惯例输出 `Aborted.` 并以 `1` 退出。

### 从备份集恢复整本书或全部 {#restore-set}

把 `--from` 指向备份目录即可整组恢复：

```bash
# 恢复整个备份集（只取每个 <book, stage> 的最新副本）
novel-cli storage restore --from ~/novel-backups --force

# 先预演
novel-cli storage restore --from ~/novel-backups --dry-run
```

备份集按 `<site>/<book>/chapter.<stage>.<时间戳>.sqlite` 组织：

* 命令按「书籍目录 + stage」分组，每组只恢复**最新**的一份；`--keep` 留下的
  历史副本计入 `skipped`，不会被旧副本覆盖新副本。
* 同一本书可能有 `raw`、`cleaner` 等多个 stage，会分别恢复到各自的
  `chapter.<stage>.sqlite`；不要把一个 stage 的文件恢复成另一个 stage 的目标名。
* 备份集内不同数据库的时间戳可能不同（例如后来新增了一本书）；按每本书选择
  所需时间点，不要假设整套文件来自同一秒。
* 如果备份目录被整体移动，`--from` 仍可指向新位置，目标推导只看最后两级目录；
  布局已被打乱时用 `--to` 指定目标根目录或单个数据库路径。
* 恢复完成后重新运行 `novel-cli storage verify`，确认全部健康。

### 撤销一次恢复（使用 `.pre-restore-*.bak`） {#restore-pre-restore}

覆盖恢复时留下的旧库可以直接再恢复回去：

```bash
novel-cli storage restore \
  --from raw_data/qidian/1010868264/chapter.raw.sqlite.pre-restore-20260102T030405Z.bak \
  --to raw_data/qidian/1010868264/chapter.raw.sqlite \
  --force
```

`.bak` 文件名不符合备份命名规则，所以**必须显式给 `--to`**。如果这个 `.bak`
本身就是损坏库的逐字节副本，`storage restore` 会拒绝它；此时只能在停止所有进程
后手工把 `.bak`（以及可能存在的 `.bak-wal` / `.bak-shm`）复制回目标，并删除目标
旁的 `-wal` / `-shm` / `-journal`，再运行 `storage verify`。手工复制期间不要让
应用打开目标库。

### 没有可用备份 {#no-backup}

确认损坏且没有可恢复副本时：

1. 停止应用；
2. 把损坏的主文件及其 `-wal` / `-shm` / `-journal` 移出 `raw_data`
   （不要留在原地让应用继续打开）；
3. 重新下载/处理该书，让应用重建章节数据库。

这会丢失本地已有的该库章节；如果章节来自已失效的源，可能无法完全找回。

### 校验归档副本 {#verify-archived-backup}

`storage verify` 只扫描在用的 `raw_data`，不会遍历备份目录。检查一个备份文件可以：

```bash
# 校验字节完整性（与 backup --json 中的 sha256 对照）
sha256sum ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite

# 让 SQLite 做完整性检查
sqlite3 ~/novel-backups/qidian/1010868264/chapter.raw.20260102T030405Z.sqlite \
  'PRAGMA integrity_check;'
```

`schema_version` 与章节行数也可在 `storage backup --json` 的 `files[]` 中查到。
需要由当前构建按 `raw_data` 规则完整验证时，可临时把 `general.raw_data_dir`
指向一个只含该副本的临时目录再运行 `storage verify`，完成后改回；不要把副本混入
真实数据目录。

## 相关文档 {#related}

* [CLI 用法 - storage 子命令](./cli-examples.md#storage-subcommand)：完整选项与
  输出字段表。
* [文件保存](./file-saving.md)：`raw_data` 目录结构与导出文件位置。
* [故障排查](./troubleshooting.md)：数据库误删、清理与恢复的快速命令。
* [安全说明](./security.md#cookie-credential-storage)：Cookie、凭据与明文数据的
  存放建议。