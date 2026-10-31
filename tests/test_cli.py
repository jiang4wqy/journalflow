import json

from filelock import FileLock

from journalflow.cli import main

from .conftest import make_pdf


def test_installable_cli_init_import_status(tmp_path, capsys):
    path = tmp_path / "journalflow.toml"
    common = ["--config", str(path)]
    assert main(common + ["init"]) == 0
    assert main(common + ["init"]) == 2  # never overwrite someone's configuration
    source = make_pdf(tmp_path / "example.pdf")
    assert main(common + ["import-pdf", str(source), "--doi", "10.1234/demo",
                          "--journal", "IJOC", "--title", "Synthetic demo"]) == 0
    capsys.readouterr()
    assert main(common + ["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["papers"] == 1 and status["pdf_files"] == 1 and status["pdf_pages"] == 1


def test_single_writer_lock(tmp_path, capsys):
    path = tmp_path / "journalflow.toml"
    common = ["--config", str(path)]
    main(common + ["init"])
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    with FileLock(str(data / ".worker.lock")):
        assert main(common + ["convert"]) == 2
    assert "Another JournalFlow process" in capsys.readouterr().out


def test_invalid_search_reports_error_without_traceback(tmp_path, capsys):
    path = tmp_path / "journalflow.toml"
    common = ["--config", str(path)]
    main(common + ["init"])
    assert main(common + ["search", '"unclosed']) == 2
    assert "error" in capsys.readouterr().out


def test_unicode_and_spaces_in_workspace_paths(tmp_path, capsys):
    root = tmp_path / "论文 library"
    root.mkdir()
    path = root / "journalflow.toml"
    common = ["--config", str(path)]
    assert main(common + ["init"]) == 0
    source = make_pdf(root / "示例 paper.pdf")
    assert main(common + ["import-pdf", str(source), "--doi", "10.1234/unicode",
                          "--journal", "IJOC", "--title", "合成文档"]) == 0
    capsys.readouterr()
    assert main(common + ["status"]) == 0
    assert json.loads(capsys.readouterr().out)["pdf_files"] == 1
