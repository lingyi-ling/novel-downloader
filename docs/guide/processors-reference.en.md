## processors Configuration

A list of text-processing stages executed in order before export.

Typical uses: **regex cleanup**, **Simplified/Traditional Chinese conversion (OpenCC)**, **text correction (pycorrector)**, **translation (translate)**, and so on.

> **All processors are optional**; when none are configured or the list is empty, no text processing runs.

### Built-in Processor Overview (Summary)

#### `cleaner`

Removes invisible characters, deletes unwanted text fragments, and performs literal replacements (optionally on titles and body text).

| Parameter                 | Type   | Default | Description |
| ------------------------- | ------ | ------- | ----------- |
| `remove_invisible`        | `bool` | true    | Remove common invisible characters (such as zero-width characters) |
| `remove_format_chars`     | `bool` | follows `remove_invisible` | Remove Unicode format characters (Cf) and private-use characters (Co), i.e. site watermarks |
| `strip_leading_title`     | `bool` | true    | Strip a chapter title repeated at the start of the body (only when followed by whitespace, a colon, or end of text) |
| `remove_empty_tags`       | `bool` | false   | Remove empty `<p></p>` and repeated `<br/>`; only safe when the body still carries real HTML (`attr = "html"`), otherwise it deletes literal markup from plain text |
| `normalize_chapter_title` | `bool` | false   | Normalize `1. Name` / `1、Name` / `第一章Name` into `第1章 Name` |
| `title_removes`           | `str`  | -       | **Optional**; path to a JSON file containing a **string array** (regex patterns) to remove one by one |
| `title_replace`           | `str`  | -       | **Optional**; path to a JSON file containing an **object** (`{"old": "new"}`) of replacements |
| `content_removes`         | `str`  | -       | Same as above, applied to body text |
| `content_replace`         | `str`  | -       | Same as above, applied to body text |
| `overwrite`               | `bool` | false   | Whether to force rebuilding when a stage with the same name already exists |

> `*_removes`: JSON array; `*_replace`: JSON object.
>
> `remove_format_chars` only drops `Cf`/`Co` characters. Newlines, tabs, combining marks (Mn) and ideographic spaces (Zs) are preserved, so CJK indentation and accented Latin text stay intact.

**Example**

Suppose the configuration contains:

```toml
[[general.processors]]
name = "cleaner"
remove_invisible = true
title_removes = "title-remove.json"
content_replace = "content-replace.json"
```

Then create the corresponding JSON files in the same directory:

**title-remove.json**

```json
[
  "\\[广告\\]",
  "\\(无弹窗小说网\\)",
  "PS：.*$"
]
```

**content-replace.json**

```json
{
  "请记住本书首发网址": "",
  "(本章完)": "",
  "li子": "例子",
  "pinbi词": "屏蔽词"
}
```

#### `zh_convert`

Performs Simplified/Traditional Chinese conversion based on **OpenCC**.

| Parameter      | Type   | Default | Description |
| -------------- | ------ | ------- | ----------- |
| `direction`    | `str`  | `t2s`   | Conversion direction (see below) |
| `apply_title`  | `bool` | true    | Whether to apply to titles |
| `apply_content`| `bool` | true    | Whether to apply to body text |
| `overwrite`    | `bool` | false   | Whether to force rebuilding |

**Available conversion directions** (`direction`):

| Code    | Meaning |
| ------- | ------- |
| `hk2s`  | Traditional (Hong Kong) -> Simplified |
| `s2hk`  | Simplified -> Traditional (Hong Kong) |
| `s2t`   | Simplified -> Traditional |
| `s2tw`  | Simplified -> Traditional (Taiwan) |
| `s2twp` | Simplified -> Traditional (Taiwan, with phrase conversion) |
| `t2hk`  | Traditional -> Traditional (Hong Kong) |
| `t2s`   | Traditional -> Simplified |
| `t2tw`  | Traditional -> Traditional (Taiwan) |
| `tw2s`  | Traditional (Taiwan) -> Simplified |
| `tw2sp` | Traditional (Taiwan) -> Simplified (with phrase conversion) |

> For more directions and details, see:
> [OpenCC official documentation](https://github.com/yichen0831/opencc-python?tab=readme-ov-file#conversions-%E8%BD%89%E6%8F%9B)
>
> Dependency: `opencc-python-reimplemented`.

#### `translator.google`

| Parameter | Type    | Default |
| --------- | ------- | ------- |
| `source`  | `str`   | `auto`  |
| `target`  | `str`   | `zh-CN` |
| `sleep`   | `float` | 2.0     |

??? note "Supported languages (click to expand)"

    --8<-- "docs/data/google_languages.md"

#### `translator.edge`

| Parameter | Type    | Default  |
| --------- | ------- | -------- |
| `source`  | `str`   | `auto`   |
| `target`  | `str`   | `zh-Hans`|
| `sleep`   | `float` | 1.0      |

??? note "Supported languages (click to expand)"

    --8<-- "docs/data/edge_languages.md"

#### `translator.youdao`

| Parameter | Type    | Default  |
| --------- | ------- | -------- |
| `source`  | `str`   | `auto`   |
| `target`  | `str`   | `zh-CHS` |
| `sleep`   | `float` | 1.0      |

??? note "Supported languages (click to expand)"

    --8<-- "docs/data/youdao_languages.md"

#### `corrector`

Chinese text correction based on [**pycorrector**](https://github.com/shibing624/pycorrector).

Multiple correction engines are available, including `kenlm`, `macbert`, `t5`, `ernie_csc`, `gpt`, and `mucgec_bart`.

> Note: correction results on novel text vary significantly by model and are often poor.

| Parameter        | Type      | Default   | Description |
| ---------------- | --------- | --------- | ----------- |
| `engine`         | str       | `"kenlm"` | Correction engine type |
| `apply_title`    | bool      | true      | Whether to apply to titles |
| `apply_content`  | bool      | true      | Whether to apply to body text |
| `apply_author`   | bool      | false     | Whether to apply to author names |
| `apply_tags`     | bool      | false     | Whether to apply to tags |
| `skip_if_len_le` | int\|None | None      | Skip processing when the text length is less than or equal to this value |
| `overwrite`      | bool      | false     | Whether to force rebuilding |

> Dependency: `pycorrector` plus the corresponding model; the first load may be slow.
>
> See the table below for each engine's parameters and official documentation.

**Engine support and parameters**

| Engine key      | Description | Documentation link | Extra parameters |
| --------------- | ----------- | ------------------ | ---------------- |
| **kenlm**       | Statistical language-model-based Chinese correction | [kenlm model (statistical)](https://github.com/shibing624/pycorrector?tab=readme-ov-file#kenlm%E6%A8%A1%E5%9E%8B%E7%BB%9F%E8%AE%A1%E6%A8%A1%E5%9E%8B) | `language_model_path`, `custom_confusion_path_or_dict`, `proper_name_path`, `common_char_path`, `same_pinyin_path`, `same_stroke_path` |
| **macbert**     | Transformer-based spelling correction model | [MacBERT model](https://github.com/shibing624/pycorrector?tab=readme-ov-file#macbert4csc%E6%A8%A1%E5%9E%8B) | `model_name_or_path` |
| **t5**          | T5-based Chinese correction model | [T5 model](https://github.com/shibing624/pycorrector?tab=readme-ov-file#t5%E6%A8%A1%E5%9E%8B) | `model_name_or_path` |
| **ernie_csc**   | ERNIE-based Chinese correction model | [ErnieCSC model](https://github.com/shibing624/pycorrector?tab=readme-ov-file#erniecsc%E6%A8%A1%E5%9E%8B) | `model_name_or_path` |
| **gpt**         | Correction based on ChatGLM / Qwen and other LLMs | [GPT model](https://github.com/shibing624/pycorrector?tab=readme-ov-file#gpt%E6%A8%A1%E5%9E%8B) | `model_name_or_path`, `model_type`, `peft_name` |
| **mucgec_bart** | Bart-based Chinese correction model | [Bart / MuCGEC Bart model](https://github.com/shibing624/pycorrector?tab=readme-ov-file#bart%E6%A8%A1%E5%9E%8B) | `model_name_or_path` |