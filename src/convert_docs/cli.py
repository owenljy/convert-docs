"""Recursively convert documents to Markdown via markitdown, mirroring folder structure."""

import argparse
import json
import os
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

EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

SEPARATOR = "-" * 40


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


def save_last_run(src_dir: Path, dest_dir: Path, extensions: list) -> None:
    path = last_run_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": str(src_dir),
        "destination": str(dest_dir),
        "extensions": extensions,
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


def prompt_source() -> Path:
    while True:
        raw = strip_quotes(input("Source folder path: ").strip())
        candidate = Path(raw or ".").expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        print(f"  '{raw}' is not a valid directory. Try again.", file=sys.stderr)


def prompt_destination(default: Path) -> Path:
    print("Destination folder path (press Enter to use the default):")
    print(f"  default: {default}")
    raw = strip_quotes(input("> ").strip())
    if not raw:
        return default
    return Path(raw).expanduser().resolve()


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
        help="Source directory to scan (skips the interactive prompt)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output directory, mirrors source structure (skips the interactive prompt)",
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


def resolve_source(args: argparse.Namespace) -> Path:
    if not args.source:
        return prompt_source()
    src_dir = Path(args.source).expanduser()
    if not src_dir.is_dir():
        print(f"Error: source directory '{args.source}' does not exist.", file=sys.stderr)
        sys.exit(1)
    return src_dir.resolve()


def resolve_destination(args: argparse.Namespace, src_dir: Path) -> Path:
    default = src_dir / "Context"
    if not args.output:
        return prompt_destination(default)
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
            print(f"[{i}/{total}] {rel_path} ... ", end="", flush=True)
            try:
                result = md.convert(str(path))
                out_path.write_text(result.text_content, encoding="utf-8")
                print("OK")
                converted.append(str(rel_path))
            except Exception as exc:
                print("FAILED")
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
                ok, err = future.result()
                if ok:
                    print(f"[{i}/{total}] {rel_path} ... OK")
                    converted.append(str(rel_path))
                else:
                    print(f"[{i}/{total}] {rel_path} ... FAILED")
                    failed.append(f"{rel_path} :: {err}")
        except KeyboardInterrupt:
            print("\nInterrupted, cancelling remaining conversions...", file=sys.stderr)
            executor.shutdown(cancel_futures=True)
            raise

    return converted, failed


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def main(argv=None) -> int:
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
        src_dir = Path(last["source"])
        if not src_dir.is_dir():
            print(f"Error: last-used source directory no longer exists: {src_dir}", file=sys.stderr)
            return 1
        src_dir = src_dir.resolve()
        dest_dir = Path(last["destination"])
        extensions = parse_extensions(args.ext) if args.ext else last.get("extensions", DEFAULT_EXTENSIONS)
    else:
        extensions = parse_extensions(args.ext) if args.ext else DEFAULT_EXTENSIONS
        src_dir = resolve_source(args)
        dest_dir = resolve_destination(args, src_dir)

    if not dest_dir.exists():
        print(f"Destination does not exist, creating: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = dest_dir.resolve()

    save_last_run(src_dir, dest_dir, extensions)

    print(SEPARATOR)
    print(f"Source folder:      {src_dir}")
    print(f"Destination folder: {dest_dir}")
    print(f"Extensions:         {', '.join(extensions)}")
    print(SEPARATOR)

    target_files, other_files = collect_files(src_dir, dest_dir, set(extensions))
    to_convert, skipped, collisions = plan_conversions(target_files, src_dir, dest_dir, args.force)
    print(f"Found {len(target_files)} convertible file(s), {len(other_files)} file(s) with unsupported extensions.")
    print(SEPARATOR)

    if args.dry_run:
        print("Dry run - no files were written.")
        print(f"  Would convert:          {len(to_convert)}")
        print(f"  Already up to date:     {len(skipped)}")
        print(f"  Name collisions:        {len(collisions)}")
        print(f"  Unsupported extension:  {len(other_files)}")
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
    print(f"  Converted:              {len(converted)}")
    print(f"  Skipped (up to date):   {len(skipped)}")
    print(f"  Failed:                 {len(failed)}")
    print(f"  Name collisions:        {len(collisions)}")
    print(f"  Unsupported extension:  {len(other_files)}")

    if failed:
        print()
        print("Failed conversions:")
        for item in failed:
            print(f"  - {item}")

    _print_collisions(collisions, dest_dir, src_dir)
    _print_unsupported(other_files, src_dir)

    print(SEPARATOR)
    print(f"Done. Output mirrored under: {dest_dir}")
    return 1 if (failed or collisions) else 0


def _print_collisions(collisions: list, dest_dir: Path, src_dir: Path) -> None:
    if not collisions:
        return
    print()
    print("Name collisions (same output path, not converted):")
    for rel_path, existing_rel_path in collisions:
        out_name = (dest_dir / rel_path).with_suffix(".md").relative_to(dest_dir)
        print(f"  - {rel_path} skipped: would overwrite {out_name} (already used by {existing_rel_path})")


def _print_unsupported(other_files: list, src_dir: Path) -> None:
    if not other_files:
        return
    print()
    print("Unsupported / not attempted:")
    for path in other_files:
        print(f"  - {path.relative_to(src_dir)}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
