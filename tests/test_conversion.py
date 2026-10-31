from dataclasses import replace

from journalflow.conversion import check_page, convert, relocate
from journalflow.intake import install_pdf
from journalflow.pdf import Document
from journalflow.quality import assess, select_refinement
from journalflow.util import digest_file, read_json, write_json

from .conftest import TEXT, make_pdf


class FakeVision:
    """Offline test double only. No fake backend is exposed by the production CLI."""

    def __init__(self, primary, refined=None, fail_page=None):
        self.primary = primary
        self.refined = refined or primary
        self.fail_page = fail_page
        self.calls = []

    def recognize(self, images, directories, *, max_pixels):
        for image, directory in zip(images, directories, strict=True):
            number = int(directory.name)
            self.calls.append((directory.parent.name, number))
            if number == self.fail_page:
                raise RuntimeError("simulated interrupted request")
            assert image.ndim == 3 and image.shape[2] == 3
            text = self.refined[number - 1] if directory.parent.name == "refinement" else self.primary[number - 1]
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "page.md").write_text(text, encoding="utf-8")
            write_json(directory / "result.json", {"parsing_res_list": [
                {"block_label": "text", "block_content": text}]})


def test_pdf_is_read_and_rendered_as_bgr(tmp_path):
    with Document(make_pdf(tmp_path / "source.pdf")) as document:
        assert len(document) == 1
        assert "reproducible" in document.text(0)
        image = document.render(0, 144)
        assert image.shape == (1684, 1190, 3)


def test_interrupted_conversion_resumes_and_indexes_once(config, state, paper, tmp_path):
    source = make_pdf(tmp_path / "paper.pdf", (TEXT, TEXT + " UniqueSecondPage."))
    before = digest_file(source)
    install_pdf(config, state, source, paper["doi"])
    failing = FakeVision([TEXT, TEXT + " UniqueSecondPage."], fail_page=2)
    assert convert(config, state, backend=failing)["failed"] == 1
    resumed = FakeVision([TEXT, TEXT + " UniqueSecondPage."])
    assert convert(config, state, backend=resumed)["completed"] == 1
    assert ("primary", 1) not in resumed.calls
    assert state.summary()["indexed_pages"] == 2
    assert len(state.search("UniqueSecondPage", 10)) == 1
    assert convert(config, state, backend=resumed)["completed"] == 0
    assert digest_file(source) == before


def test_refinement_removes_loop_and_preserves_initial(config, state, paper, tmp_path):
    install_pdf(config, state, make_pdf(tmp_path / "paper.pdf"), paper["doi"])
    backend = FakeVision([TEXT * 100], [TEXT])
    assert convert(config, state, backend=backend)["completed"] == 1
    conversion = state.connection.execute("SELECT * FROM conversions").fetchone()
    root = config.data_dir / conversion["output_path"]
    assert len((root / "document.md").read_text(encoding="utf-8")) > 35000
    assert len((root / "document-refined.md").read_text(encoding="utf-8")) < 10000
    status = read_json(root / "status.json")
    assert status["refined_pages"] == [1] and status["selected_pages"] == [1]
    assert status["review_pages"] == []
    assert state.summary()["indexed_pages"] == 1


def test_worse_refinement_is_rejected():
    initial = assess(TEXT, TEXT, [])
    worse = assess(TEXT, "Research methods", [])
    assert not select_refinement(initial, worse)


def test_shortened_repeated_prose_is_flagged_even_with_the_same_vocabulary():
    paragraph = "This synthetic document checks repeated sentences and page checkpoints without containing real research. "
    initial = assess(paragraph * 8, paragraph, [])
    assert "very_little_visual_output" in initial["review_flags"]
    assert initial["refinement_reasons"]
    shorter = assess(paragraph * 8, "This synthetic document checks repeated sentences.", [])
    assert not select_refinement(initial, shorter)


def test_policy_change_preserves_previous_output(config, state, paper, tmp_path):
    install_pdf(config, state, make_pdf(tmp_path / "paper.pdf"), paper["doi"])
    assert convert(config, state, backend=FakeVision([TEXT]))["completed"] == 1
    changed = replace(config, vision=replace(config.vision, render_dpi=360))
    assert changed.vision.policy_hash != config.vision.policy_hash
    assert convert(changed, state, backend=FakeVision([TEXT]))["completed"] == 1
    roots = [config.data_dir / row[0] for row in state.connection.execute("SELECT output_path FROM conversions")]
    assert len(roots) == 2 and all((root / "document.md").is_file() for root in roots)
    assert state.summary()["indexed_pages"] == 1


def test_page_assets_must_exist_and_stay_inside(tmp_path):
    root = tmp_path / "page"
    root.mkdir()
    (root / "page.md").write_text("![](missing.jpg)", encoding="utf-8")
    write_json(root / "result.json", {"parsing_res_list": []})
    import pytest
    with pytest.raises(ValueError, match="missing"):
        check_page(root)
    (root / "page.md").write_text("![](../outside.jpg)", encoding="utf-8")
    with pytest.raises(ValueError):
        check_page(root)
    assert relocate('![](imgs/a.jpg) <img src="imgs/b.jpg">', "primary/0001") == (
        '![](primary/0001/imgs/a.jpg) <img src="primary/0001/imgs/b.jpg">')
