import json
import os
import sys
import time
import types

import pytest

from convert_docs import cli


@pytest.fixture(autouse=True)
def isolate_config_dir(tmp_path, monkeypatch):
    """Never touch the real ~/.config/convert-docs while testing."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    # Keep output plain and deterministic regardless of how the terminal
    # running pytest happens to be configured.
    monkeypatch.setenv("NO_COLOR", "1")


def install_fake_markitdown(monkeypatch, results):
    """results: {path_str: (True, text) | (False, error_message)}"""
    fake_module = types.ModuleType("markitdown")

    class FakeResult:
        def __init__(self, text):
            self.text_content = text

    class FakeMarkItDown:
        def convert(self, path_str):
            ok, payload = results[path_str]
            if ok:
                return FakeResult(payload)
            raise RuntimeError(payload)

    fake_module.MarkItDown = FakeMarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake_module)


# --- small pure helpers -----------------------------------------------------

def test_parse_extensions():
    assert cli.parse_extensions("pdf, .DOCX ,, txt") == ["pdf", "docx", "txt"]


def test_strip_quotes():
    assert cli.strip_quotes('"hello"') == "hello"
    assert cli.strip_quotes("'hello'") == "hello"
    assert cli.strip_quotes("hello") == "hello"
    assert cli.strip_quotes("'mismatched\"") == "'mismatched\""


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_args(["--version"])
    assert exc_info.value.code == 0
    assert "convert-docs" in capsys.readouterr().out


# --- collect_files -----------------------------------------------------------

def test_collect_files_excludes_noise_and_dest(tmp_path):
    src = tmp_path / "src"
    dest = src / "Context"
    (src / "sub").mkdir(parents=True)
    (src / ".git").mkdir()
    (src / "node_modules" / "pkg").mkdir(parents=True)
    dest.mkdir()

    (src / "report.pdf").write_text("x")
    (src / "sub" / "notes.txt").write_text("x")
    (src / "notes.rtf").write_text("x")
    (src / ".git" / "config").write_text("x")
    (src / "node_modules" / "pkg" / "index.js").write_text("x")
    (src / ".DS_Store").write_text("x")
    (dest / "already.md").write_text("x")

    target_files, other_files = cli.collect_files(src, dest, {"pdf", "txt"})

    target_names = {p.relative_to(src) for p in target_files}
    other_names = {p.relative_to(src) for p in other_files}

    assert target_names == {cli.Path("report.pdf"), cli.Path("sub/notes.txt")}
    assert other_names == {cli.Path("notes.rtf")}


def test_collect_files_hides_non_document_extensions_entirely(tmp_path):
    """Code, config, binary, media, etc. are skipped outright -- never
    counted as convertible OR unsupported, unlike a genuinely undocumented
    extension such as .rtf."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "report.pdf").write_text("x")
    (src / "app.py").write_text("x")
    (src / "Button.tsx").write_text("x")
    (src / "image.png").write_text("x")
    (src / "archive.zip").write_text("x")
    (src / "config.yaml").write_text("x")

    target_files, other_files = cli.collect_files(src, src / "Context", {"pdf"})

    assert {p.relative_to(src) for p in target_files} == {cli.Path("report.pdf")}
    assert other_files == []


def test_collect_files_denylist_overrides_explicit_ext_set(tmp_path):
    """Even if a code extension is explicitly requested via -e, it's still
    treated as a non-document and skipped."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x")
    (src / "report.pdf").write_text("x")

    target_files, other_files = cli.collect_files(src, src / "Context", {"pdf", "py"})

    assert {p.relative_to(src) for p in target_files} == {cli.Path("report.pdf")}
    assert other_files == []


# --- plan_conversions ---------------------------------------------------------

def test_plan_conversions_convert_and_skip(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    fresh = src / "fresh.txt"
    fresh.write_text("hello")

    stale = src / "stale.txt"
    stale.write_text("hello")
    stale_out = dest / "stale.md"
    stale_out.write_text("old")
    # make the existing output newer than the source -> should be skipped
    import os
    import time
    now = time.time()
    os.utime(stale, (now - 100, now - 100))
    os.utime(stale_out, (now, now))

    to_convert, skipped, collisions = cli.plan_conversions(
        [fresh, stale], src, dest, force=False,
    )

    assert [rel for _, rel, _ in to_convert] == [cli.Path("fresh.txt")]
    assert skipped == ["stale.txt"]
    assert collisions == []


def test_plan_conversions_force_reconverts(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    stale = src / "stale.txt"
    stale.write_text("hello")
    (dest / "stale.md").write_text("old")

    to_convert, skipped, collisions = cli.plan_conversions(
        [stale], src, dest, force=True,
    )

    assert len(to_convert) == 1
    assert skipped == []


def test_plan_conversions_detects_output_collision(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    docx = src / "report.docx"
    docx.write_text("x")
    pdf = src / "report.pdf"
    pdf.write_text("x")

    to_convert, skipped, collisions = cli.plan_conversions(
        [docx, pdf], src, dest, force=False,
    )

    assert [rel for _, rel, _ in to_convert] == [cli.Path("report.docx")]
    assert collisions == [(cli.Path("report.pdf"), cli.Path("report.docx"))]


# --- execute_conversions -------------------------------------------------------

def test_execute_conversions_sequential_success_and_failure(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    good = src / "good.txt"
    good.write_text("x")
    bad = src / "bad.txt"
    bad.write_text("x")

    good_out = dest / "good.md"
    bad_out = dest / "bad.md"

    install_fake_markitdown(monkeypatch, {
        str(good): (True, "converted text"),
        str(bad): (False, "boom"),
    })

    to_convert = [
        (good, cli.Path("good.txt"), good_out),
        (bad, cli.Path("bad.txt"), bad_out),
    ]
    converted, failed = cli.execute_conversions(to_convert, jobs=1)

    assert converted == ["good.txt"]
    assert failed == ["bad.txt :: boom"]
    assert good_out.read_text() == "converted text"
    assert not bad_out.exists()


def test_execute_conversions_parallel_real_markitdown(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()

    files = []
    for name in ("one.txt", "two.txt"):
        path = src / name
        path.write_text(f"content of {name}")
        files.append((path, cli.Path(name), dest / name.replace(".txt", ".md")))

    converted, failed = cli.execute_conversions(files, jobs=2)

    assert failed == []
    assert sorted(converted) == ["one.txt", "two.txt"]
    for _, _, out_path in files:
        assert out_path.exists()


# --- main() end-to-end ----------------------------------------------------------

def test_main_end_to_end(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "notes.txt").write_text("hello world")
    out = tmp_path / "out"

    exit_code = cli.main(["-s", str(src), "-o", str(out), "-e", "txt"])

    assert exit_code == 0
    assert (out / "sub" / "notes.md").exists()
    assert cli.last_run_path().exists()


def test_main_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.txt").write_text("hello world")
    out = tmp_path / "out"

    exit_code = cli.main(["-s", str(src), "-o", str(out), "-e", "txt", "--dry-run"])

    assert exit_code == 0
    assert not (out / "notes.md").exists()


def test_main_rejects_last_with_source(capsys):
    exit_code = cli.main(["-l", "-s", "/tmp/whatever"])
    assert exit_code == 1
    assert "--last cannot be combined" in capsys.readouterr().err


def test_main_last_without_prior_run_exits():
    with pytest.raises(SystemExit):
        cli.main(["--last"])


def test_main_log_file_captures_output(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.txt").write_text("hello world")
    out = tmp_path / "out"
    log_file = tmp_path / "run.log"

    exit_code = cli.main([
        "-s", str(src), "-o", str(out), "-e", "txt", "--log-file", str(log_file),
    ])

    assert exit_code == 0
    assert log_file.exists()
    assert "Done. Output mirrored under" in log_file.read_text()


# --- single-file mode: parsing helpers ------------------------------------------

def test_split_source_input_plain():
    assert cli.split_source_input("/a/b.pdf") == ("/a/b.pdf", None)


def test_split_source_input_with_name():
    assert cli.split_source_input("/a/b.pdf, custom") == ("/a/b.pdf", "custom")


def test_split_source_input_quoted_path_with_comma():
    path, name = cli.split_source_input('"/a/My, File.pdf", custom')
    assert path == "/a/My, File.pdf"
    assert name == "custom"


def test_split_source_input_quoted_no_name():
    assert cli.split_source_input('"/a/b.pdf"') == ("/a/b.pdf", None)


def test_sanitize_custom_name_strips_path_and_suffix():
    assert cli.sanitize_custom_name("sub/dir/My Name.md") == "My Name"
    assert cli.sanitize_custom_name("plain") == "plain"


# --- single-file mode: interactive prompt ---------------------------------------

def test_prompt_source_single_file_with_rename(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("x")
    inputs = iter([f"{src_file}, custom-name"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    path, is_file, custom_name = cli.prompt_source()

    assert path == src_file.resolve()
    assert is_file is True
    assert custom_name == "custom-name"


def test_prompt_source_directory_ignores_rename(tmp_path, monkeypatch, capsys):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    inputs = iter([f"{src_dir}, custom-name"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    path, is_file, custom_name = cli.prompt_source()

    assert path == src_dir.resolve()
    assert is_file is False
    assert custom_name is None
    assert "ignoring" in capsys.readouterr().err.lower()


def test_prompt_source_retries_on_invalid_path(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("x")
    inputs = iter(["/no/such/path", str(src_file)])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    path, is_file, custom_name = cli.prompt_source()

    assert path == src_file.resolve()
    assert is_file is True


# --- single-file mode: end to end ------------------------------------------------

def test_main_single_file_default_name(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # accept default destination

    exit_code = cli.main(["-s", str(src_file)])

    assert exit_code == 0
    assert (tmp_path / "report.md").exists()


def test_main_single_file_explicit_output_dir(tmp_path):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = cli.main(["-s", str(src_file), "-o", str(out_dir)])

    assert exit_code == 0
    assert (out_dir / "report.md").exists()


def test_main_single_file_explicit_output_file(tmp_path):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    out_file = tmp_path / "custom.md"

    exit_code = cli.main(["-s", str(src_file), "-o", str(out_file)])

    assert exit_code == 0
    assert out_file.exists()


def test_main_single_file_dry_run_writes_nothing(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    exit_code = cli.main(["-s", str(src_file), "--dry-run"])

    assert exit_code == 0
    assert not (tmp_path / "report.md").exists()


def test_main_single_file_skips_when_up_to_date(tmp_path, monkeypatch, capsys):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    out_file = tmp_path / "report.md"
    out_file.write_text("existing")
    now = time.time()
    os.utime(src_file, (now - 100, now - 100))
    os.utime(out_file, (now, now))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    exit_code = cli.main(["-s", str(src_file)])

    assert exit_code == 0
    assert out_file.read_text() == "existing"
    assert "Skipped" in capsys.readouterr().out


def test_main_single_file_force_reconverts(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    out_file = tmp_path / "report.md"
    out_file.write_text("existing")
    now = time.time()
    os.utime(src_file, (now - 100, now - 100))
    os.utime(out_file, (now, now))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    exit_code = cli.main(["-s", str(src_file), "-f"])

    assert exit_code == 0
    assert out_file.read_text() != "existing"


def test_main_interactive_single_file_with_rename(tmp_path, monkeypatch):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    inputs = iter([f"{src_file}, custom-name", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    exit_code = cli.main([])

    assert exit_code == 0
    assert (tmp_path / "custom-name.md").exists()


def test_main_last_replays_file_mode(tmp_path):
    src_file = tmp_path / "report.txt"
    src_file.write_text("hello world")
    out_file = tmp_path / "custom.md"

    cli.main(["-s", str(src_file), "-o", str(out_file)])
    assert out_file.exists()
    out_file.unlink()

    exit_code = cli.main(["--last"])

    assert exit_code == 0
    assert out_file.exists()


def test_last_run_backward_compat_defaults_to_dir_mode(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "a.txt").write_text("hi")
    out_dir = tmp_path / "out"

    cli.last_run_path().parent.mkdir(parents=True, exist_ok=True)
    cli.last_run_path().write_text(json.dumps({
        "source": str(src_dir),
        "destination": str(out_dir),
        "extensions": ["txt"],
    }))

    exit_code = cli.main(["--last"])

    assert exit_code == 0
    assert (out_dir / "a.md").exists()
