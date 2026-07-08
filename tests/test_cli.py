import sys
import types

import pytest

from convert_docs import cli


@pytest.fixture(autouse=True)
def isolate_config_dir(tmp_path, monkeypatch):
    """Never touch the real ~/.config/convert-docs while testing."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))


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
    (src / "image.png").write_text("x")
    (src / ".git" / "config").write_text("x")
    (src / "node_modules" / "pkg" / "index.js").write_text("x")
    (src / ".DS_Store").write_text("x")
    (dest / "already.md").write_text("x")

    target_files, other_files = cli.collect_files(src, dest, {"pdf", "txt"})

    target_names = {p.relative_to(src) for p in target_files}
    other_names = {p.relative_to(src) for p in other_files}

    assert target_names == {cli.Path("report.pdf"), cli.Path("sub/notes.txt")}
    assert other_names == {cli.Path("image.png")}


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
