"""Recursively convert documents to Markdown via markitdown, mirroring folder structure."""

import argparse
import sys
from pathlib import Path

DEFAULT_EXTENSIONS = [
    "pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "csv",
    "html", "htm", "txt", "epub", "json", "xml", "msg",
]

SEPARATOR = "-" * 40


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
        "-s", "--source",
        help="Source directory to scan (skips the interactive prompt)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output directory, mirrors source structure (skips the interactive prompt)",
    )
    parser.add_argument(
        "-e", "--ext",
        default=",".join(DEFAULT_EXTENSIONS),
        help="Comma-separated extensions to convert (default: %(default)s)",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Force re-conversion even if output .md already exists and is up to date",
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
    for path in sorted(src_dir.rglob("*")):
        if not path.is_file():
            continue
        if dest_dir == path or dest_dir in path.parents:
            continue
        suffix = path.suffix.lower().lstrip(".")
        (target_files if suffix in ext_set else other_files).append(path)
    return target_files, other_files


def convert_files(target_files: list, src_dir: Path, dest_dir: Path, force: bool) -> tuple:
    from markitdown import MarkItDown

    md = MarkItDown()
    total = len(target_files)
    converted, failed, skipped = [], [], []

    for i, path in enumerate(target_files, start=1):
        rel_path = path.relative_to(src_dir)
        out_path = (dest_dir / rel_path).with_suffix(".md")
        print(f"[{i}/{total}] {rel_path} ... ", end="", flush=True)

        if not force and out_path.exists() and out_path.stat().st_mtime >= path.stat().st_mtime:
            print("skipped (up to date)")
            skipped.append(str(rel_path))
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = md.convert(str(path))
            out_path.write_text(result.text_content, encoding="utf-8")
            print("OK")
            converted.append(str(rel_path))
        except Exception as exc:
            print("FAILED")
            failed.append(f"{rel_path} :: {exc}")

    return converted, failed, skipped


def main(argv=None) -> int:
    args = parse_args(argv)
    extensions = [e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()]

    src_dir = resolve_source(args)
    dest_dir = resolve_destination(args, src_dir)

    if not dest_dir.exists():
        print(f"Destination does not exist, creating: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_dir = dest_dir.resolve()

    print(SEPARATOR)
    print(f"Source folder:      {src_dir}")
    print(f"Destination folder: {dest_dir}")
    print(f"Extensions:         {', '.join(extensions)}")
    print(SEPARATOR)

    target_files, other_files = collect_files(src_dir, dest_dir, set(extensions))
    print(f"Found {len(target_files)} convertible file(s), {len(other_files)} file(s) with unsupported extensions.")
    print(SEPARATOR)

    converted, failed, skipped = convert_files(target_files, src_dir, dest_dir, args.force)

    print(SEPARATOR)
    print("Summary:")
    print(f"  Converted:              {len(converted)}")
    print(f"  Skipped (up to date):   {len(skipped)}")
    print(f"  Failed:                 {len(failed)}")
    print(f"  Unsupported extension:  {len(other_files)}")

    if failed:
        print()
        print("Failed conversions:")
        for item in failed:
            print(f"  - {item}")

    if other_files:
        print()
        print("Unsupported / not attempted:")
        for path in other_files:
            print(f"  - {path.relative_to(src_dir)}")

    print(SEPARATOR)
    print(f"Done. Output mirrored under: {dest_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
