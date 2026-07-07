# convert-docs

Recursively convert documents (pdf, docx, pptx, xlsx, and more) to Markdown via
[markitdown](https://github.com/microsoft/markitdown), mirroring the source folder
structure into a destination folder.

## Install

Requires [uv](https://docs.astral.sh/uv/). If you don't have it yet:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install the tool:

```bash
uv tool install convert-docs
```

No `uv`? Plain `pip` also works, as long as it's Python 3.10+: `pip install convert-docs`.

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
convert-docs --last             # re-run with the same source/destination/extensions as last time
convert-docs -j 8                # convert 8 files in parallel instead of the default 4
```

| Flag | Description |
|---|---|
| `-s, --source` | Source directory to scan (skips the interactive prompt) |
| `-o, --output` | Output directory, mirrors source structure (skips the interactive prompt) |
| `-e, --ext` | Comma-separated extensions to convert |
| `-f, --force` | Force re-conversion even if output `.md` already exists and is up to date |
| `-l, --last` | Reuse the source, destination, and extensions from the last run (skips prompts; cannot be combined with `-s`/`-o`) |
| `-j, --jobs` | Convert this many files in parallel using separate processes (default: `min(4, cpu_count)`; use `1` for sequential) |

Files with unsupported extensions are listed at the end instead of being silently
skipped, and any conversion failures are reported with the underlying error.

### Parallel conversion

Files convert using separate OS processes (real parallelism across CPU cores, not
`asyncio` — conversion is CPU-bound parsing work, not I/O waiting, so asyncio's
cooperative concurrency wouldn't actually help). Benchmarked on a 10-core Mac converting
12–30 real docx files: `-j 4` (the default) cut wall time by ~2.3x vs. sequential.
Pushing higher didn't help further — `-j 8` was consistently *slower* than `-j 4` in
testing, since each worker process pays a fixed startup cost importing `markitdown`'s
dependencies (onnxruntime, magika, pandas), which outweighs the extra parallelism for
typical batch sizes. If you're converting a very large batch (hundreds of files), it's
worth experimenting with higher `-j` values yourself; for typical folders the default
is a reasonable balance. Progress lines print in completion order, not scan order, since
files finish out of sequence when running in parallel.

### Re-running on new files

Every run saves its source/destination/extensions to `~/.config/convert-docs/last_run.json`.
Since conversion already skips files whose output is up to date, `convert-docs --last` is a
cheap way to pick up newly added files in a folder you've converted before — run it manually
whenever you want to sync, or wire it into a cron job / scheduled task on whatever interval
suits you (just point it at the tool's full path, e.g. `~/.local/bin/convert-docs --last`,
since scheduled jobs don't load your shell profile).

## Update

```bash
uv tool upgrade convert-docs
```
