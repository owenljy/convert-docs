"""Convert documents to Markdown via markitdown.

Given a folder, recursively converts it and mirrors the structure into a
destination folder. Given a single file, converts just that file.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from importlib import metadata
from pathlib import Path

DEFAULT_EXTENSIONS = [
    "pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "csv",
    "html", "htm", "txt", "epub", "json", "xml", "msg",
]

EXCLUDED_DIR_NAMES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", "env", ".tox", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", ".cache",
}

EXCLUDED_FILE_NAMES = {
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "composer.lock", "Gemfile.lock", "poetry.lock",
}

# Extensions that are never documents, regardless of DEFAULT_EXTENSIONS or a
# user-supplied -e/--ext list: source code, build/config, binaries, archives,
# media, fonts, and design files. These are skipped during the scan entirely
# (never counted as "convertible" or "unsupported"), the same way noise
# directories like node_modules/.git are.
NON_DOCUMENT_EXTENSIONS = {
    # source code
    "py", "pyc", "pyo", "pyw", "ipynb",
    "js", "jsx", "ts", "tsx", "mjs", "cjs", "vue", "svelte",
    "java", "class", "jar", "kt", "kts", "scala", "groovy", "gradle",
    "c", "h", "cpp", "cc", "cxx", "hpp", "hh",
    "cs", "go", "rb", "php", "swift", "rs", "pl", "pm", "lua", "r", "dart",
    "sh", "bash", "zsh", "ps1", "bat", "cmd", "sql",
    # build / config (not documents, even though they're plain text)
    "yml", "yaml", "toml", "ini", "cfg", "conf", "env",
    "lock", "editorconfig", "npmrc", "gitignore", "gitattributes",
    # compiled / executable binaries
    "exe", "dll", "so", "dylib", "bin", "o", "obj", "a", "lib",
    "node", "wasm", "apk", "app", "deb", "rpm", "msi", "dmg",
    # archives
    "zip", "tar", "gz", "tgz", "bz2", "xz", "7z", "rar", "iso",
    # images
    "png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "ico", "tiff", "tif", "heic",
    # audio / video
    "mp3", "wav", "flac", "aac", "m4a", "ogg", "mp4", "mov", "avi", "mkv", "webm", "flv", "wmv",
    # fonts
    "ttf", "otf", "woff", "woff2", "eot",
    # design files
    "psd", "ai", "sketch", "fig", "xd", "indd",
    # databases
    "db", "sqlite", "sqlite3",
}

SEPARATOR = "-" * 40

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

ICON_OK = "✓"
ICON_FAIL = "✗"
ICON_WARN = "⚠"

_color_enabled = False


def _detect_color_support() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    stream = sys.__stdout__
    return bool(stream) and hasattr(stream, "isatty") and stream.isatty()


def _style(text, *codes: str) -> str:
    text = str(text)
    if not _color_enabled or not text:
        return text
    return "".join(codes) + text + _ANSI_RESET


def style_path(text) -> str:
    return _style(text, _ANSI_CYAN)


def style_ok(text) -> str:
    return _style(text, _ANSI_BOLD, _ANSI_GREEN)


def style_fail(text) -> str:
    return _style(text, _ANSI_BOLD, _ANSI_RED)


def style_warn(text) -> str:
    return _style(text, _ANSI_BOLD, _ANSI_YELLOW)


def style_hint(text) -> str:
    return _style(text, _ANSI_DIM)


def style_count(n: int, style_fn) -> str:
    return style_fn(n) if n else style_hint(str(n))


def get_version() -> str:
    try:
        return metadata.version("convert-docs")
    except metadata.PackageNotFoundError:
        return "unknown"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "convert-docs"


def last_run_path() -> Path:
    return config_dir() / "last_run.json"


def save_last_run(src: Path, dest: Path, extensions: list = None, mode: str = "dir") -> None:
    path = last_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": str(src),
        "destination": str(dest),
        "extensions": extensions,
        "mode": mode,
    }, indent=2))


def load_last_run() -> dict:
    path = last_run_path()
    if not path.exists():
        print(
            "Error: no previous run found. Run convert-docs normally first "
            "(with -s/-o or interactively) before using --last.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error: could not read saved config at {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def parse_extensions(raw: str) -> list:
    return [e.strip().lower().lstrip(".") for e in raw.split(",") if e.strip()]


def strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def split_source_input(raw: str) -> tuple:
    """Split "path" or "path, new-name" into (path, custom_name).

    Handles a quoted path that itself contains a comma by matching the
    closing quote before looking for the rename separator.
    """
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            path_part = raw[1:end]
            rest = raw[end + 1:].strip()
            name_part = rest[1:].strip() if rest.startswith(",") else ""
            return path_part, (name_part or None)
    path_part, _, name_part = raw.partition(",")
    return strip_quotes(path_part.strip()), (name_part.strip() or None)


def sanitize_custom_name(raw_name: str) -> str:
    """Reduce a user-supplied rename to a bare filename stem (no path, no .md)."""
    name = Path(raw_name).name
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name


def prompt_source() -> tuple:
    """Returns (path, is_file, custom_name)."""
    while True:
        raw = input("Source file or folder path: ")
        path_part, custom_name = split_source_input(raw)
        candidate = Path(path_part or ".").expanduser()
        if candidate.is_dir():
            if custom_name:
                print("  Note: renaming only applies to a single file; ignoring it.", file=sys.stderr)
            return candidate.resolve(), False, None
        if candidate.is_file():
            name = sanitize_custom_name(custom_name) if custom_name else None
            return candidate.resolve(), True, name
        print(f"  '{path_part}' is not a valid file or directory. Try again.", file=sys.stderr)


def prompt_destination(default: Path, description: str) -> Path:
    print("Destination folder path:")
    print(f"  default: {description}")
    raw = strip_quotes(input(style_hint("[Enter = default] > ")).strip())
    if not raw:
        return default
    return Path(raw).expanduser().resolve()


def prompt_destination_file(default: Path) -> Path:
    print("Destination file path:")
    print(f"  default: {style_path(default.name)} (same folder as the source file)")
    raw = strip_quotes(input(style_hint("[Enter = default] > ")).strip())
    if not raw:
        return default
    candidate = Path(raw).expanduser().resolve()
    if candidate.is_dir():
        return candidate / default.name
    return candidate.with_suffix(".md")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="convert-docs",
        description=__doc__,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {get_version()}",
    )
    parser.add_argument(
        "-s", "--source",
        help="Source file or directory to convert (skips the interactive prompt)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output directory (mirrors source structure) or, when -s is a single "
             "file, an output directory or file path (skips the interactive prompt)",
    )
    parser.add_argument(
        "-e", "--ext",
        default=None,
        help=f"Comma-separated extensions to convert (default: {','.join(DEFAULT_EXTENSIONS)})",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-conversion even if output .md already exists and is up to date",
    )
    parser.add_argument(
        "-l", "--last",
        action="store_true",
        help="Reuse the source, destination, and extensions from the last run "
             "(skips prompts; cannot be combined with -s/-o)",
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="Convert this many files in parallel using separate processes "
             "(default: %(default)s; use 1 for sequential)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Show what would be converted, skipped, or collide without writing any files",
    )
    parser.add_argument(
        "--log-file",
        help="Append this run's output to a file in addition to stdout",
    )
    return parser.parse_args(argv)


def resolve_source(args: argparse.Namespace) -> tuple:
    """Returns (path, is_file, custom_name). custom_name is always None here;
    the rename shortcut is only available at the interactive prompt."""
    if not args.source:
        return prompt_source()
    src = Path(args.source).expanduser()
    if src.is_dir():
        return src.resolve(), False, None
    if src.is_file():
        return src.resolve(), True, None
    print(f"Error: source path '{args.source}' does not exist.", file=sys.stderr)
    sys.exit(1)


def default_destination_dir(src: Path) -> tuple:
    """Default output directory for directory mode: normally a "Context"
    subfolder inside the source. If the source is itself already named
    "Context", nesting would look like a mistake (Context/Context), so fall
    back to a sibling folder instead."""
    if src.name.lower() == "context":
        name = f"{src.name}-md"
        return src.parent / name, f'a sibling folder named "{name}"'
    return src / "Context", 'a "Context" subfolder inside the source folder'


def resolve_destination(args: argparse.Namespace, src: Path, is_file: bool, custom_name: str) -> Path:
    if is_file:
        default_name = (custom_name or src.stem) + ".md"
        default = src.with_name(default_name)
        if not args.output:
            return prompt_destination_file(default)
        out = Path(args.output).expanduser().resolve()
        if out.is_dir():
            return out / default_name
        return out.with_suffix(".md")

    default, description = default_destination_dir(src)
    if not args.output:
        return prompt_destination(default, description)
    return Path(args.output).expanduser().resolve()


def collect_files(src_dir: Path, dest_dir: Path, ext_set: set) -> tuple:
    target_files = []
    other_files = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirpath = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDED_DIR_NAMES and (dirpath / d) != dest_dir
        )
        for filename in sorted(filenames):
            if filename in EXCLUDED_FILE_NAMES:
                continue
            path = dirpath / filename
            suffix = path.suffix.lower().lstrip(".")
            if suffix in NON_DOCUMENT_EXTENSIONS:
                continue
            (target_files if suffix in ext_set else other_files).append(path)
    return target_files, other_files


_worker_md = None


def _init_worker() -> None:
    global _worker_md
    from markitdown import MarkItDown
    _worker_md = MarkItDown()


def _convert_one(path_str: str, out_path_str: str) -> tuple:
    try:
        result = _worker_md.convert(path_str)
        Path(out_path_str).write_text(result.text_content, encoding="utf-8")
        return (True, None)
    except Exception as exc:
        return (False, str(exc))


def plan_conversions(target_files: list, src_dir: Path, dest_dir: Path, force: bool) -> tuple:
    """Decide what to convert, skip, or reject as an output-path collision.

    Pure planning: does not touch the filesystem beyond stat()/exists() checks,
    so it's safe to call for a --dry-run preview.
    """
    to_convert = []
    skipped = []
    collisions = []
    seen = {}
    for path in target_files:
        rel_path = path.relative_to(src_dir)
        out_path = (dest_dir / rel_path).with_suffix(".md")
        if out_path in seen:
            collisions.append((rel_path, seen[out_path]))
            continue
        seen[out_path] = rel_path
        if not force and out_path.exists() and out_path.stat().st_mtime >= path.stat().st_mtime:
            skipped.append(str(rel_path))
            continue
        to_convert.append((path, rel_path, out_path))
    return to_convert, skipped, collisions


def execute_conversions(to_convert: list, jobs: int = 1) -> tuple:
    """Run pre-planned conversions. Returns (converted, failed)."""
    total = len(to_convert)
    converted, failed = [], []

    if total == 0:
        return converted, failed

    for _, _, out_path in to_convert:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    if jobs <= 1 or total == 1:
        from markitdown import MarkItDown
        md = MarkItDown()
        for i, (path, rel_path, out_path) in enumerate(to_convert, start=1):
            print(f"[{i}/{total}] {style_path(rel_path)} ... ", end="", flush=True)
            try:
                result = md.convert(str(path))
                out_path.write_text(result.text_content, encoding="utf-8")
                print(style_ok(f"{ICON_OK} OK"))
                converted.append(str(rel_path))
            except Exception as exc:
                print(style_fail(f"{ICON_FAIL} FAILED"))
                failed.append(f"{rel_path} :: {exc}")
        return converted, failed

    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as executor:
        futures = {
            executor.submit(_convert_one, str(path), str(out_path)): rel_path
            for path, rel_path, out_path in to_convert
        }
        try:
            for i, future in enumerate(as_completed(futures), start=1):
                rel_path = futures[future]
                succeeded, err = future.result()
                if succeeded:
                    print(f"[{i}/{total}] {style_path(rel_path)} ... {style_ok(f'{ICON_OK} OK')}")
                    converted.append(str(rel_path))
                else:
                    print(f"[{i}/{total}] {style_path(rel_path)} ... {style_fail(f'{ICON_FAIL} FAILED')}")
                    failed.append(f"{rel_path} :: {err}")
        except KeyboardInterrupt:
            print("\nInterrupted, cancelling remaining conversions...", file=sys.stderr)
            executor.shutdown(cancel_futures=True)
            raise

    return converted, failed


class _Tee:
    """Writes to multiple streams, stripping ANSI color codes for non-tty ones
    (e.g. a --log-file) so the terminal stays colored but the file stays plain."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            is_tty = getattr(stream, "isatty", lambda: False)()
            stream.write(data if is_tty else _ANSI_RE.sub("", data))

    def flush(self):
        for stream in self.streams:
            stream.flush()


def main(argv=None) -> int:
    global _color_enabled
    _color_enabled = _detect_color_support()

    args = parse_args(argv)

    if args.last and (args.source or args.output):
        print("Error: --last cannot be combined with -s/--source or -o/--output.", file=sys.stderr)
        return 1

    if not args.log_file:
        return _run(args)

    log_path = Path(args.log_file).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    original_stdout = sys.stdout
    with log_path.open("a", encoding="utf-8") as log_fh:
        sys.stdout = _Tee(original_stdout, log_fh)
        try:
            return _run(args)
        finally:
            sys.stdout = original_stdout


def _run(args: argparse.Namespace) -> int:
    if args.last:
        last = load_last_run()
        mode = last.get("mode", "dir")
        src = Path(last["source"])
        dest = Path(last["destination"])

        if mode == "file":
            if not src.is_file():
                print(f"Error: last-used source file no longer exists: {src}", file=sys.stderr)
                return 1
            return _run_single_file(args, src.resolve(), dest)

        if not src.is_dir():
            print(f"Error: last-used source directory no longer exists: {src}", file=sys.stderr)
            return 1
        extensions = parse_extensions(args.ext) if args.ext else last.get("extensions", DEFAULT_EXTENSIONS)
        return _run_directory(args, src.resolve(), dest, extensions)

    src, is_file, custom_name = resolve_source(args)
    dest = resolve_destination(args, src, is_file, custom_name)

    if is_file:
        return _run_single_file(args, src, dest)

    extensions = parse_extensions(args.ext) if args.ext else DEFAULT_EXTENSIONS
    return _run_directory(args, src, dest, extensions)


def _run_single_file(args: argparse.Namespace, src_path: Path, dest_path: Path) -> int:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    save_last_run(src_path, dest_path, mode="file")

    print(SEPARATOR)
    print(f"Source file:      {style_path(src_path)}")
    print(f"Destination file: {style_path(dest_path)}")
    print(SEPARATOR)

    up_to_date = (
        not args.force
        and dest_path.exists()
        and dest_path.stat().st_mtime >= src_path.stat().st_mtime
    )
    if up_to_date:
        print(f"Skipped (up to date): {style_path(dest_path.name)}")
        return 0

    if args.dry_run:
        print(f"Would convert: {style_path(src_path.name)} -> {style_path(dest_path.name)}")
        return 0

    print(f"{style_path(src_path.name)} ... ", end="", flush=True)
    try:
        from markitdown import MarkItDown
        result = MarkItDown().convert(str(src_path))
        dest_path.write_text(result.text_content, encoding="utf-8")
    except Exception as exc:
        print(style_fail(f"{ICON_FAIL} FAILED"))
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(style_ok(f"{ICON_OK} OK"))
    print(SEPARATOR)
    print(f"{style_ok(ICON_OK)} Done. Output written to: {style_path(dest_path)}")
    return 0


def _run_directory(args: argparse.Namespace, src_dir: Path, dest_dir: Path, extensions: list) -> int:
    if not dest_dir.exists():
        print(f"Destination does not exist, creating: {style_path(dest_dir)}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = dest_dir.resolve()

    save_last_run(src_dir, dest_dir, extensions)

    print(SEPARATOR)
    print(f"Source folder:      {style_path(src_dir)}")
    print(f"Destination folder: {style_path(dest_dir)}")
    print(f"Extensions:         {', '.join(extensions)}")
    print(SEPARATOR)

    target_files, other_files = collect_files(src_dir, dest_dir, set(extensions))
    to_convert, skipped, collisions = plan_conversions(target_files, src_dir, dest_dir, args.force)
    print(
        f"Found {style_count(len(target_files), style_ok)} convertible file(s), "
        f"{style_count(len(other_files), style_warn)} file(s) with unsupported extensions."
    )
    print()

    if args.dry_run:
        print("Dry run - no files were written.")
        print(f"  Would convert:          {len(to_convert)}")
        print(f"  Already up to date:     {len(skipped)}")
        print(f"  Name collisions:        {style_count(len(collisions), style_warn)}")
        print(f"  Unsupported extension:  {style_count(len(other_files), style_warn)}")
        _print_collisions(collisions, dest_dir, src_dir)
        _print_unsupported(other_files, src_dir)
        return 1 if collisions else 0

    try:
        converted, failed = execute_conversions(to_convert, jobs=args.jobs)
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        return 130

    print(SEPARATOR)
    print("Summary:")
    print(f"  Converted:              {style_count(len(converted), style_ok)}")
    print(f"  Skipped (up to date):   {len(skipped)}")
    print(f"  Failed:                 {style_count(len(failed), style_fail)}")
    print(f"  Name collisions:        {style_count(len(collisions), style_warn)}")
    print(f"  Unsupported extension:  {style_count(len(other_files), style_warn)}")

    if failed:
        print()
        print(f"{style_fail(ICON_FAIL)} Failed conversions:")
        for item in failed:
            rel_path, _, err = item.partition(" :: ")
            print(f"  - {style_path(rel_path)} :: {err}")

    _print_collisions(collisions, dest_dir, src_dir)
    _print_unsupported(other_files, src_dir)

    print(SEPARATOR)
    if failed or collisions:
        print(
            f"{style_fail(ICON_FAIL + ' Finished with errors.')} "
            f"Output mirrored under: {style_path(dest_dir)}"
        )
    elif not converted and not skipped:
        print(
            f"{style_warn(ICON_WARN + ' Nothing converted.')} "
            f"Output mirrored under: {style_path(dest_dir)}"
        )
    else:
        print(f"{style_ok(ICON_OK)} Done. Output mirrored under: {style_path(dest_dir)}")
    return 1 if (failed or collisions) else 0


def _print_collisions(collisions: list, dest_dir: Path, src_dir: Path) -> None:
    if not collisions:
        return
    print()
    print(f"{style_warn(ICON_WARN)} Name collisions (same output path, not converted):")
    for rel_path, existing_rel_path in collisions:
        out_name = (dest_dir / rel_path).with_suffix(".md").relative_to(dest_dir)
        print(
            f"  - {style_path(rel_path)} skipped: would overwrite {style_path(out_name)} "
            f"(already used by {style_path(existing_rel_path)})"
        )


def _print_unsupported(other_files: list, src_dir: Path) -> None:
    if not other_files:
        return
    print()
    print(f"{style_warn(ICON_WARN)} Unsupported / not attempted:")
    for file_path in other_files:
        print(f"  - {style_path(file_path.relative_to(src_dir))}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
