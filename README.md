# convert-docs

Recursively convert documents (pdf, docx, pptx, xlsx, and more) to Markdown via
[markitdown](https://github.com/microsoft/markitdown), mirroring the source folder
structure into a destination folder.

## Install

```bash
uv tool install convert-docs
```

## Usage

Run with no flags for an interactive prompt (asks for source, then destination):

```bash
convert-docs
```

Or pass flags to skip the prompts:

```bash
convert-docs -s ~/Documents/Reports -o ~/Documents/Reports_MD
convert-docs -e pdf,docx        # restrict which extensions get converted
convert-docs -f                 # force re-conversion, ignoring the up-to-date skip
```

| Flag | Description |
|---|---|
| `-s, --source` | Source directory to scan (skips the interactive prompt) |
| `-o, --output` | Output directory, mirrors source structure (skips the interactive prompt) |
| `-e, --ext` | Comma-separated extensions to convert |
| `-f, --force` | Force re-conversion even if output `.md` already exists and is up to date |

Files with unsupported extensions are listed at the end instead of being silently
skipped, and any conversion failures are reported with the underlying error.

## Update

```bash
uv tool upgrade convert-docs
```
