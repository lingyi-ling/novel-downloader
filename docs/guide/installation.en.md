## Installation

### Python Environment

To avoid package conflicts, using a virtual environment is recommended:

```bash
python -m venv .venv
source .venv/bin/activate
# Windows: .venv\Scripts\activate
```

---

### Install novel-downloader

(1) Install from PyPI

```bash
pip install novel-downloader
```

After installation, the following executable commands are provided:

* `novel-cli`: command-line entry point
* `novel-api`: local REST API entry point (requires the `api` extra, see below)

(2) Install the latest development version (GitHub)

```bash
git clone https://github.com/lingyi-ling/novel-downloader.git
cd novel-downloader
```

If you need to compile multi-language support:

```bash
pip install babel
pybabel compile -d src/novel_downloader/locales
```

Install:

```bash
pip install .
```

---

## Optional Features and Dependencies

`novel-downloader` provides several optional dependency groups that can be installed selectively as needed.

### Optional Dependency Groups

| Group | Install command | Description |
| --- | --- | --- |
| `api` | `pip install "novel-downloader[api]"` | Local REST API (FastAPI + uvicorn) |
| `music` | `pip install "novel-downloader[music]"` | Music downloads (mutagen, pathvalidate, qrcode, websocket-client, etc.) |
| `music-desktop` | `pip install "novel-downloader[music-desktop]"` | Music downloads plus desktop UI dependencies (customtkinter) |
| `image-utils` | `pip install "novel-downloader[image-utils]"` | Image/font processing (pillow, numpy, fonttools, brotli) |
| `httpx` | `pip install "novel-downloader[httpx]"` | httpx HTTP/2 backend |
| `curl_cffi` | `pip install "novel-downloader[curl_cffi]"` | curl_cffi backend and browser impersonation |
| `all-backends` | `pip install "novel-downloader[all-backends]"` | Both httpx and curl_cffi backends |
| `all` | `pip install "novel-downloader[all]"` | Aggregated runtime optional dependencies |
| `docs` | `pip install "novel-downloader[docs]"` | Dependencies for building the documentation |
| `tests` | `pip install "novel-downloader[tests]"` | Dependencies for running the test suite |
| `ci` | `pip install "novel-downloader[ci]"` | Dependencies used in CI |
| `dev` | `pip install "novel-downloader[dev]"` | Local development tools (tests, lint, type checking, etc.) |

> Without any optional group, only the core search, download, and export features of `novel-cli` are available.

### Local REST API (Optional)

The local REST API depends on the `api` group and listens on `127.0.0.1:8081` by default:

```bash
pip install "novel-downloader[api]"
novel-api
```

Common options (see [Local REST API](api.md) for the full reference):

```bash
novel-api --host 127.0.0.1 --port 8081 --token my-secret
```

### Music Download (Optional)

Music downloads depend on the `music` group:

```bash
pip install "novel-downloader[music]"
```

```bash
# List available platforms
novel-cli music platforms

# Search for songs
novel-cli music search "keyword" --platform netease

# Download a chosen search result
novel-cli music download "keyword" --pick 1
```

Install `music-desktop` if you also need the desktop UI dependencies:

```bash
pip install "novel-downloader[music-desktop]"
```

### Pluggable HTTP Backend (Optional)

`aiohttp` is used by default; if you need to switch to another backend, you can install the corresponding dependency.

#### httpx Backend

Supports HTTP/1.1 and HTTP/2:

```bash
pip install "novel-downloader[httpx]"
```

How to enable (`settings.toml`):

```toml
[general]
backend = "httpx"
```

#### curl_cffi Backend

Supports `libcurl` and browser impersonation (impersonate):

```bash
pip install "novel-downloader[curl_cffi]"
```

How to enable:

```toml
[general]
backend = "curl_cffi"
impersonate = "chrome136"
```

---

### Node.js Decryption Support (Required for Some Sites)

VIP chapter decryption logic for sites such as 起点 / QQ 阅读 is based on JavaScript and requires Node.js.

Download and installation page:

[Download Node.js](https://nodejs.org/en/download)

Once installed, it is automatically used for the decryption process of the relevant sites.

---

### Font Obfuscation Restoration and Image Chapter OCR (`enable_ocr`)

To handle "font-obfuscated chapters" or "image chapters -> text", the OCR module must be enabled.

OCR consists of two parts:

* Extra Python dependencies (`image-utils`)
* PaddleOCR inference framework (enables text recognition)

The complete installation process is as follows.

(1) Install the extension dependencies

```bash
pip install novel-downloader[image-utils]
```

This installs:

* pillow
* numpy
* fonttools
* brotli
* and other dependencies for processing fonts and images

(2) Install PaddlePaddle (CPU or GPU)

Choose the CPU or GPU version and CUDA support according to your environment.

Official installation instructions: [documentation](https://www.paddlepaddle.org.cn/install/quick?docurl=/documentation/docs/zh/develop/install/pip/windows-pip.html)

**CPU version:**

```bash
python -m pip install paddlepaddle==3.1.1 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
```

**GPU version (choose according to your CUDA version):**

```bash
python -m pip install paddlepaddle-gpu==3.1.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu118/
```

(3) Install PaddleOCR

Official installation instructions: [documentation](https://www.paddleocr.ai/latest/version3.x/installation.html)

```bash
pip install paddleocr
```

**Versions used in the development/test environment**:

```bash
paddleocr==3.2.0
paddlepaddle==3.1.1
```

(4) Enable font obfuscation restoration (`enable_ocr`)

Edit `settings.toml`:

```toml
[general.parser]
enable_ocr = true  # whether to attempt local decoding of obfuscated fonts
batch_size = 32
model_name = "PP-OCRv5_mobile_rec"
```

---

**Common OCR Errors and Handling**

(1) Model loading fails because the path contains Chinese characters (Windows)

Example error:

```bash
(NotFound) Cannot open file
C:\Users\用户名.paddlex\official_models\PP-OCRv5_mobile_rec\inference.json, please confirm whether the file is normal.
[Hint: Expected paddle::inference::IsFileExists(prog_file_) == true, but received paddle::inference::IsFileExists(prog_file_):0 != true:1.]
```

Solution:

1. Move the model directory to a path that contains no Chinese characters
2. Specify the full path in `settings.toml`:

```toml
[general.parser]
enable_ocr = true
batch_size = 32
model_name = "PP-OCRv5_mobile_rec"
model_dir = 'D:\pdx_models\PP-OCRv5_mobile_rec'  # change to the actual path
```

The model directory usually must contain:

* `inference.pdmodel`
* `inference.pdiparams`
* `inference.json`

---

### OCR Performance Benchmark (Reference)

**Goal**: Evaluate the average time and recognition accuracy of processing a "single chapter".

**Test environment**:

* Intel 12900H
* NVIDIA RTX 3070 (8GB)
* `batch_size = 32`

> Note: in practice, adjust `batch_size` according to your device memory.

#### GPU Devices

| Model `model_name`     | Average Time per Chapter (s) | Accuracy   |
| ---------------------- | ---------------------------- | ---------- |
| `PP-OCRv3_mobile_rec`  | **0.666**                    | 98.01%     |
| `PP-OCRv4_mobile_rec`  | 1.040                        | 99.14%     |
| `PP-OCRv4_server_rec`  | 1.231                        | 99.52%     |
| `PP-OCRv5_mobile_rec`  | 1.111                        | 99.91%     |
| `PP-OCRv5_server_rec`  | 1.890                        | **99.97%** |

#### CPU Devices

| Model `model_name`     | Average Time per Chapter (s) | Accuracy   |
| ---------------------- | ---------------------------- | ---------- |
| `PP-OCRv3_mobile_rec`  | **1.426**                    | 98.01%     |
| `PP-OCRv5_mobile_rec`  | 1.957                        | 99.91%     |
| `PP-OCRv4_mobile_rec`  | 4.135                        | 99.14%     |
| `PP-OCRv5_server_rec`  | 688.163                      | 99.97%     |
| `PP-OCRv4_server_rec`  | 733.195                      | 99.52%     |

#### Known Issues / Notes

* When using `PP-OCRv5`, Traditional Chinese characters are occasionally returned (e.g., recognizing the Simplified "杰" as "傑"), along with occasional anomalous characters (e.g., "口" recognized as "□")
* When using `PP-OCRv3`, the result is occasionally an empty string (no text returned)
* On CPU, the server version is extremely slow; if you do not have a GPU, the server model is not recommended
