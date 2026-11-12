#!/usr/bin/env python3
"""
Shared PyInstaller collection helpers for the three app specs.

It also builds the Windows version resource (``FileVersion`` /
``ProductVersion``) from ``novel_downloader.__version__`` so every executable
reports the same version as the package, the installer and ``latest.json``.

The site plugins live in namespace-style directories (no ``__init__.py``) and
are imported by name at runtime, so PyInstaller's static analysis and even
``collect_submodules`` cannot see them.  Walking the source tree is the only
reliable way to produce the hidden imports.  The same applies to the vendored
musicdl package, whose submodules are imported lazily by platform key and
which cannot be fully imported at build time (optional extras such as ``av``
and ``faster_whisper`` are intentionally absent).

The helper is imported from every spec; PyInstaller executes specs with the
spec directory available via ``SPECPATH``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

PACKAGE = "novel_downloader"

#: ``__version__ = "1.2.3"`` as written in ``novel_downloader/__init__.py``.
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)

#: Vendor name embedded in the Windows version resource of every executable.
VENDOR = "lingyi-ling"
LICENSE_TEXT = "MIT License"

#: Vendored musicdl tree.  Its platform adapters are imported by module name
#: (``musicdl.py`` builds the path from the selected platform key), so
#: PyInstaller's entry-point analysis cannot see them.
MUSIC_VENDOR_RELATIVE = "apps/music/providers/_vendor/musicdl"
MUSIC_VENDOR_PACKAGE = f"{PACKAGE}.{MUSIC_VENDOR_RELATIVE.replace('/', '.')}"

#: (source-relative directory, importable package name) pairs whose modules
#: are selected at runtime rather than by a regular ``import`` statement.
DYNAMIC_TREES: tuple[tuple[str, str], ...] = (
    ("plugins/sites", f"{PACKAGE}.plugins.sites"),
    ("plugins/processors", f"{PACKAGE}.plugins.processors"),
    ("plugins/rule", f"{PACKAGE}.plugins.rule"),
    (MUSIC_VENDOR_RELATIVE, MUSIC_VENDOR_PACKAGE),
)

#: The optional ``image-utils`` extra (``numpy``, ``Pillow``, ``fontTools``).
#: PyInstaller's bundled hooks cover ``numpy`` and Pillow's image plugins, but
#: ``fontTools`` has no hook: ``TTFont`` reaches its table modules through a
#: generated import block, and a hook change could silently drop them.  The
#: imports are lazy in the source tree (``libs/image_utils.py`` degrades to a
#: clear RuntimeError when they are absent), so analysis of the entry point
#: alone is not enough either -- the bundle must carry them explicitly.  None
#: of the three packages ships data files that have to be collected.
#: ``scripts/verify_frozen.py`` asserts these names exist in the built PYZ.
IMAGE_STACK_HIDDEN_IMPORTS: tuple[str, ...] = (
    "numpy",
    "numpy.typing",
    "PIL.Image",
    "PIL.ImageOps",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "fontTools",
    "fontTools.ttLib",
    "fontTools.ttLib.tables._p_o_s_t",
)

#: Heavy music backends imported at module-load time by the vendored adapters.
#:
#: The music stack is separate from the image stack because its failure mode is
#: different: the vendors import these packages at *module load* time (TIDAL ->
#: ``aigpy``, YouTube -> ``ytmusicapi``, Apple/SoundCloud -> ``pywidevine`` ->
#: ``pymp4``/``construct``).  Because the adapters themselves are hidden
#: imports, PyInstaller can follow those direct imports -- but only while the
#: adapter modules are actually in the hidden-import list.  Listing the
#: packages here keeps the promise explicit, and ``scripts/verify_frozen.py``
#: asserts every one of them (and every vendor module) reaches the built PYZ.
#: ``Crypto``/``google.protobuf`` are shared with the core (NetEase) and the
#: generated Widevine protobuf module, and are required by the DRM adapters.
#: The submodules are listed explicitly as well: ``pywidevine/__init__`` and
#: ``aigpy/__init__`` import them eagerly, and a partial PYZ (top-level package
#: only) would register the platform and then fail on the first search.
MUSIC_STACK_HIDDEN_IMPORTS: tuple[str, ...] = (
    "aigpy",
    "aigpy.downloadHelper",
    "aigpy.modelHelper",
    "aigpy.stringHelper",
    "aigpy.tagHelper",
    "ytmusicapi",
    "ytmusicapi.ytmusic",
    "pywidevine",
    "pywidevine.cdm",
    "pywidevine.device",
    "pywidevine.license_protocol_pb2",
    "pywidevine.pssh",
    "pymp4",
    "pymp4.parser",
    "construct",
    "mutagen",
    "google.protobuf",
    "Crypto",
)

#: Modules imported dynamically by string (importlib.resources / lazy imports).
EXTRA_HIDDEN_IMPORTS: tuple[str, ...] = (
    *IMAGE_STACK_HIDDEN_IMPORTS,
    *MUSIC_STACK_HIDDEN_IMPORTS,
    # ``importlib.resources.files("novel_downloader.locales")``
    f"{PACKAGE}.locales",
    # ``importlib.resources.files("novel_downloader.resources")``
    f"{PACKAGE}.resources",
    # ``from novel_downloader.apps.browser import ...`` inside site searchers
    f"{PACKAGE}.apps.browser",
    # ``from novel_downloader.apps.login import ...`` inside the login command
    f"{PACKAGE}.apps.login",
    # Third-party modules imported by the vendored music adapters. They are
    # reached through hidden-import trees, so PyInstaller cannot discover
    # them by walking the entry point alone.
    "m3u8",
    "emoji",
    "bleach",
    "filetype",
    "puremagic",
    "fake_useragent",
    "json_repair",
    "tinytag",
    "tabulate",
    "prettytable",
    "wcwidth",
    "prompt_toolkit",
)


#: Development-only, removed-UI and unused heavy packages.  Keeping these out
#: keeps the bundles small; none of them is reachable from novel-cli/novel-api.
EXCLUDES: tuple[str, ...] = (
    # Development tooling
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "pytest_aiohttp",
    "pytest_cov",
    "pytest_mock",
    "mypy",
    "mypy_extensions",
    "ruff",
    "coverage",
    "babel",
    "mkdocs",
    "mkdocs_material",
    "mkdocstrings",
    "pymdownx",
    "material",
    # Removed desktop/web UI (the API no longer serves a web frontend)
    "nicegui",
    "customtkinter",
    # The pywebview client renders with Edge WebView2, not Tk; the only
    # remaining importer is the vendored musicdl (lazy, fallback-only paths).
    "tkinter",
    "_tkinter",
    # Notebook / scientific stacks not used by the downloader runtime
    "matplotlib",
    "pandas",
    "scipy",
    "IPython",
    "jupyter",
    # Build-time only.  ``cffi._shimmed_dist_utils`` is the sole runtime
    # importer and it is reached only from ``ffi.verify()``, which cannot work
    # in a frozen bundle (no compiler, no package metadata).  Dropping
    # setuptools also removes PyInstaller's setuptools runtime hook and the
    # ``_distutils_hack`` shim.
    "setuptools",
    "_distutils_hack",
    # NumPy exposes these through a lazy ``numpy.__getattr__``; ``import
    # numpy`` never loads them and no module in this repository references
    # them.  ``numpy.random`` additionally carries ~2 MiB of generator
    # extension modules (removed from ``binaries`` by prune_bundle_toc).
    "numpy.testing",
    "numpy.f2py",
    "numpy.random",
    # Not reachable from the registry (kept in-tree for reference only)
    f"{PACKAGE}.plugins.archived",
    # Android-only adapter: the embedded loopback server is loaded by name
    # from Kotlin via Chaquopy (which ships the source tree directly, see
    # android/app/build.gradle.kts), never from a PyInstaller bundle.
    f"{PACKAGE}.apps.embedded",
)

#: Console-bundle-only guards.  The desktop UI is reachable only through
#: ``packaging/desktop_entry.py``; nothing under ``apps.cli``/``apps.api``
#: imports it, and excluding it keeps a future accidental cross-import from
#: dragging pywebview into the CLI/API bundles.
CONSOLE_EXCLUDES: tuple[str, ...] = (f"{PACKAGE}.apps.desktop",)

#: Hook-collected payload that no runtime path reads.  PyInstaller hooks and
#: ``collect_dynamic_libs`` run during analysis, so ``EXCLUDES`` alone cannot
#: drop these files; every spec prunes them with :func:`prune_bundle_toc`.
#:
#: * ``emoji/unicode_codes/emoji_*.json`` -- the bundled ``hook-emoji`` copies
#:   every per-language table.  ``import emoji`` itself opens the English base
#:   table ``emoji.json``, which stays; the ``emoji_<lang>.json`` overlays are
#:   read only by ``emoji.config.load_language()`` / ``demojize(language=...)``,
#:   which no code path in this repository calls (~3.9 MiB).  The prefix ends
#:   in ``emoji_`` so the base ``emoji.json`` never matches.
#: * ``numpy/random/*`` -- ``hook-numpy`` collects every dynamic library below
#:   the package.  ``numpy.random`` is excluded above, so its generator
#:   extensions are dead weight (~2.0 MiB).
UNUSED_DATA_PREFIXES: tuple[str, ...] = ("emoji/unicode_codes/emoji_",)
UNUSED_BINARY_PREFIXES: tuple[str, ...] = ("numpy/random/",)


def prune_bundle_toc(
    toc: list[tuple[str, str, str]],
    prefixes: tuple[str, ...],
) -> list[tuple[str, str, str]]:
    """
    Drop TOC entries whose bundle-relative destination starts with *prefixes*.

    PyInstaller writes destinations with the host separator (``\\`` on
    Windows), so entries are normalised to forward slashes before comparing.

    :param toc: ``Analysis.datas`` or ``Analysis.binaries``.
    :param prefixes: Forward-slash destination prefixes to remove.
    :return: The entries to keep, preserving their original order.
    """
    kept: list[tuple[str, str, str]] = []
    for entry in toc:
        destination = str(entry[0]).replace("\\", "/")
        if any(destination.startswith(prefix) for prefix in prefixes):
            continue
        kept.append(entry)
    return kept


def modules_from_tree(package_dir: Path, package_name: str) -> list[str]:
    """
    Return every importable module name under ``package_dir``.

    Directories without ``__init__.py`` are treated as namespace packages; the
    resulting names still resolve at runtime (PyInstaller records the parent
    directories as PEP 420 namespace packages).

    :param package_dir: Absolute path of the package directory on disk.
    :param package_name: Fully qualified package name for that directory.
    :return: Sorted, de-duplicated module names.
    """
    modules = {package_name}
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir).with_suffix("")
        parts = [part for part in relative.parts if part != "__init__"]
        modules.add(".".join([package_name, *parts]) if parts else package_name)
    return sorted(modules)


def music_vendor_modules(src_dir: Path) -> list[str]:
    """
    Return every module in the vendored musicdl tree, platform adapters included.

    The list is the frozen counterpart of the registry's platform table: a
    missing entry means the platform is advertised but cannot be imported at
    runtime.  Both the spec and ``scripts/verify_frozen.py`` use this helper so
    the two can never drift apart.

    :param src_dir: Repository ``src/`` directory.
    """
    return modules_from_tree(
        Path(src_dir) / PACKAGE / MUSIC_VENDOR_RELATIVE,
        MUSIC_VENDOR_PACKAGE,
    )


def _iter_non_python_files(package_dir: Path) -> Iterator[Path]:
    """Yield data files (not source/bytecode) below ``package_dir``."""
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix in {".py", ".pyc", ".pyo"}:
            continue
        yield path


def project_datas(src_dir: Path) -> list[tuple[str, str]]:
    """
    Build the ``datas`` list for runtime resources shipped with the package.

    Collected payloads:

    * ``locales/**`` (``.mo``, ``.po`` and the template),
    * ``resources/**`` (icons, CSS, JS, JSON maps, sample config),
    * ``plugins/rule/builtin/*.json``,
    * ``py.typed`` and the vendored musicdl ``SOURCE.txt``.

    The site plugin ``.py`` files are copied alongside their compiled modules
    on purpose: ``importlib.resources.contents()`` in the plugin registry
    enumerates site directories on disk, and PyInstaller does not reconstruct
    directories that contain only source files.  Keeping the sources also
    provides a filesystem fallback if a namespace module ever misses the PYZ.
    """
    package_dir = src_dir / PACKAGE
    datas: list[tuple[str, str]] = []
    for path in _iter_non_python_files(package_dir):
        # PyInstaller's ``datas`` destination is a directory; the source
        # basename is appended automatically.
        relative = path.relative_to(package_dir)
        datas.append((str(path), str(Path(PACKAGE) / relative.parent)))

    sites_dir = package_dir / "plugins" / "sites"
    for path in sorted(sites_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_dir)
        datas.append((str(path), str(Path(PACKAGE) / relative.parent)))
    return datas


def common_hidden_imports(src_dir: Path) -> list[str]:
    """Return all hidden imports required by the CLI and the API."""
    package_dir = src_dir / PACKAGE
    hidden: list[str] = list(EXTRA_HIDDEN_IMPORTS)
    for relative, package_name in DYNAMIC_TREES:
        hidden.extend(modules_from_tree(package_dir / relative, package_name))
    return sorted(set(hidden))


def collect_common(src_dir: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Return ``(datas, hiddenimports)`` shared by both specifications.

    :param src_dir: Repository ``src/`` directory.
    """
    return project_datas(src_dir), common_hidden_imports(src_dir)


# ---------------------------------------------------------------------------
# Windows version resource (FileVersion / ProductVersion)
# ---------------------------------------------------------------------------


def read_package_version(src_dir: Path) -> str:
    """
    Return ``__version__`` from ``<src_dir>/novel_downloader/__init__.py``.

    The source tree is read directly rather than importing the package: the
    spec bundles the source tree, so it is the authority even when the running
    interpreter has a different (e.g. non-editable) install.

    :raises RuntimeError: when the package or its version string is missing.
    """
    init_py = Path(src_dir) / PACKAGE / "__init__.py"
    if not init_py.is_file():
        raise RuntimeError(f"package version file not found: {init_py}")
    match = _VERSION_RE.search(init_py.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(f"no __version__ string in {init_py}")
    return match.group(1)


def version_numbers(version: str) -> tuple[int, int, int, int]:
    """
    Convert a PEP 440-ish version into the 4-tuple Windows expects.

    Only the numeric release prefix counts: ``1.2.3`` becomes
    ``(1, 2, 3, 0)`` and pre-release/local suffixes (``-rc1``, ``+build``)
    are ignored, because the fixed ``VS_FIXEDFILEINFO`` fields have no room
    for them.  Missing components are padded with zero.
    """
    core = re.split(r"[-+]", version.strip(), maxsplit=1)[0]
    numbers: list[int] = []
    for part in core.split(".")[:4]:
        match = re.match(r"\d+", part)
        numbers.append(int(match.group()) if match else 0)
    numbers.extend([0] * (4 - len(numbers)))
    return (numbers[0], numbers[1], numbers[2], numbers[3])


def windows_version_resource(
    version: str,
    *,
    app_name: str,
    description: str | None = None,
) -> str:
    """
    Render a PyInstaller ``version=`` resource file body for ``app_name``.

    PyInstaller evaluates the file with its ``versioninfo`` classes in scope;
    the layout below is the canonical one produced by ``grab_version.py``.
    ``FileVersion``/``ProductVersion`` carry ``version`` verbatim while the
    fixed fields carry :func:`version_numbers` (what Windows' file-properties
    dialog and the installer's ``UninstallDisplayVersion`` read).
    """
    major, minor, build, revision = version_numbers(version)
    fixed = f"({major}, {minor}, {build}, {revision})"
    text = description or f"{app_name} application"
    # Windows expects the launcher's file name here, not the bundle directory.
    launcher = f"{app_name}.exe"
    # ``repr`` keeps the template valid Python even if a name ever contains a
    # quote; the values themselves are repository constants.
    return f"""# Generated by packaging/_spec_common.py -- do not edit.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={fixed},
    prodvers={fixed},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', {VENDOR!r}),
            StringStruct('FileDescription', {text!r}),
            StringStruct('FileVersion', {version!r}),
            StringStruct('InternalName', {app_name!r}),
            StringStruct('LegalCopyright', {LICENSE_TEXT!r}),
            StringStruct('OriginalFilename', {launcher!r}),
            StringStruct('ProductName', {app_name!r}),
            StringStruct('ProductVersion', {version!r}),
          ],
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def write_version_resource(
    work_dir: Path,
    src_dir: Path,
    *,
    app_name: str,
    description: str | None = None,
) -> Path:
    """
    Write the version resource for ``app_name`` under *work_dir* and return it.

    The file lands in the PyInstaller work directory (``workpath`` inside a
    spec), never in the source tree, so a build leaves no tracked file behind.
    It is rewritten on every build, so a version bump cannot leave a stale
    resource in place.

    :param work_dir: PyInstaller's ``workpath`` for the current build.
    :param src_dir: Repository ``src/`` directory.
    :param app_name: Executable base name (``novel-cli``, ``NovelDownloader``).
    :param description: Optional ``FileDescription`` string.
    :return: Path of the written version resource file.
    """
    version = read_package_version(src_dir)
    target = Path(work_dir) / "version_info" / f"{app_name}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        windows_version_resource(
            version,
            app_name=app_name,
            description=description,
        ),
        encoding="utf-8",
    )
    return target
