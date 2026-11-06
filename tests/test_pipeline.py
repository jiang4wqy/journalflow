import copy
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from journalflow import pipeline
from journalflow.embedding import E5
from journalflow.intake import install_pdf
from journalflow.regional import resolve_active
from journalflow.util import read_json, write_json

from .conftest import make_pdf
from .test_regional import base as regional_base


class SyntheticEncoder:
    """Only synthetic fixtures use these vectors; real replay uses offline E5."""
    calls = []

    def __init__(self, *_):
        self.tokenizer = self

    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=list(text))

    def decode(self, tokens):
        return ''.join(tokens)

    def __call__(self, texts):
        self.calls.extend(texts)
        return [[1., 0.] for _ in texts]


class OfflineHttp:
    def __init__(self, *_args, **_kwargs):
        pass

    def json(self, *_args, **_kwargs):
        pytest.fail('This synthetic control-cycle test must not use the network')

    def close(self):
        pass


def options_for(config, tmp_path, base, *, vision=False, download=False):
    options = {name: tmp_path / name for name in ('library_root', 'output_root', 'model_dir')}
    options.update(base_index=base, active_pointer=tmp_path / 'private/active.json')
    lines = ['[automation]', f'vision_enabled = {str(vision).lower()}',
             f'download_enabled = {str(download).lower()}', '[regional]']
    lines += [f'{name} = {json.dumps(path.as_posix())}' for name, path in options.items()]
    config.path.write_text('\n'.join(lines), encoding='utf-8')
    return options


def completed_visual(config, state, metadata, tmp_path, *, different_title=False, flagged=False, table=False):
    title = 'A different article' if different_title else metadata['title']
    path = make_pdf(tmp_path / 'incoming.pdf', (
        title + ' DOI ' + metadata['doi'] + ' Methods compare unchanged groups.',
        'The second page documents a separate experiment and its assumptions.'))
    pdf = install_pdf(config, state, path, metadata['doi'], 'published', 'https://publisher.example/paper.pdf')
    pdf.update(title=metadata['title'], journal=metadata['journal'], metadata=json.dumps(metadata))
    root = state.root / 'markdown/completed'
    indexed = []
    for number, text in enumerate(('Confirmed first-page prose.\n\n## References\nCitation one.',
                                    'Additional bibliography entries with measured number 7.'), 1):
        for branch in ('primary', 'refinement'):
            folder = root / branch / f'{number:04d}'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'page.md').write_bytes(text.encode())
            write_json(folder / 'result.json', {'parsing_res_list': [
                {'block_label': 'table', 'block_content': '<table><td>7</td></table>'}] if table and number == 2 else []})
            write_json(folder / 'page-status.json', {
                'source_sha256': pdf['sha256'], 'source_pdf_page': number,
                'policy': config.vision.policy_hash, 'recognition_method': 'visual',
                'quality': {'review_flags': ['uncertain_number'] if flagged and number == 2 else []}})
        branch = 'refinement' if number == 2 else 'primary'
        indexed.append((number, text, (root / branch / f'{number:04d}/page.md').relative_to(state.root).as_posix()))
    write_json(root / 'status.json', {'source_sha256': pdf['sha256'], 'pages_expected': 2, 'pages_saved': 2,
               'policy': config.vision.policy_hash, 'selected_pages': [2], 'refined_pages': [2],
               'review_pages': [2] if flagged else []})
    state.index_pages(pdf, indexed)
    state.conversion(pdf['id'], config.vision.policy_hash, 'completed', root)
    return pdf, root


@pytest.fixture
def offline(monkeypatch):
    SyntheticEncoder.calls = []
    monkeypatch.setattr('journalflow.embedding.E5', SyntheticEncoder)
    monkeypatch.setattr(pipeline, 'Http', OfflineHttp)
    monkeypatch.setattr(pipeline, 'discover', lambda *_: {'synthetic_metadata_only': True})


def test_disabled_vision_cannot_convert_or_publish_completed_sources(config, state, paper, tmp_path, offline, monkeypatch):
    base, _ = regional_base.__wrapped__(tmp_path)
    options = options_for(config, tmp_path, base)
    completed_visual(config, state, paper, tmp_path)
    write_json(options['active_pointer'], {'sentinel': 'existing pointer must remain untouched'})
    original = options['active_pointer'].read_bytes()
    monkeypatch.setattr(pipeline, 'convert', lambda *_: pytest.fail('Paused vision called convert'))
    monkeypatch.setattr(pipeline, 'fetch', lambda *_: pytest.fail('Paused downloads called fetch'))
    report = pipeline.run(config)
    assert report['fetch']['state'] == 'paused_download'
    assert report['conversion']['state'] == 'paused_by_configuration'
    assert not report['published'] and not SyntheticEncoder.calls
    assert options['active_pointer'].read_bytes() == original


def test_metadata_updates_preserve_baseline_and_do_not_redownload(config, state, paper, tmp_path, offline, monkeypatch):
    base, _ = regional_base.__wrapped__(tmp_path)
    options_for(config, tmp_path, base, download=True)
    state.fetch_result(paper['doi'], 'baseline_available')
    changed = copy.deepcopy(paper)
    changed.update(title=paper['title'] + ' corrected title', metadata_hash='changed metadata')

    def discovery(_config, current, _http):
        current.upsert(changed)
        return {'updated': 1}

    monkeypatch.setattr(pipeline, 'discover', discovery)
    report = pipeline.run(config)
    assert report['fetch']['downloaded'] == report['new_downloads'] == 0
    assert state.paper(paper['doi'])['fetch_state'] == 'baseline_available'


def test_complete_production_increment_is_not_rebuilt_when_policy_already_published(config, state, paper, tmp_path, offline, monkeypatch):
    base, _ = regional_base.__wrapped__(tmp_path)
    options = options_for(config, tmp_path, base, vision=True)
    pdf, _ = completed_visual(config, state, paper, tmp_path, table=True)
    monkeypatch.setattr(pipeline, 'convert', lambda *_: {'completed': 0})
    first = pipeline.run(config)
    assert first['published'] and first['new_downloads'] == 0
    candidate = resolve_active(options['active_pointer'])
    manifest = read_json(candidate / 'incremental-manifest.json')
    assert manifest['kind'] == 'visual_conversion' and manifest['new_downloads'] is True
    assert manifest['source_counts'] == {'recorded_remote_url': 1} and manifest['downloaded_this_run'] == 0
    document, = manifest['papers']
    assert document['eligible'] and len(document['pages']) == pdf['pages']
    assert document['pages'][1]['variant'] == 'refinement'
    assert document['pages'][1]['needs_review']
    assert not any(chunk['answer_eligible'] for chunk in document['pages'][1]['chunks'])
    assert any(chunk['answer_eligible'] for chunk in document['pages'][0]['chunks'])
    assert any(chunk['content_kind'] == 'references' and not chunk['answer_eligible']
               for chunk in document['pages'][0]['chunks'])
    calls = len(SyntheticEncoder.calls)
    second = pipeline.run(config)
    assert not second['published'] and 'regional' not in second
    assert len(SyntheticEncoder.calls) == calls
    assert len(list(options['output_root'].glob('regional-*'))) == 1


def test_doi_in_a_different_articles_header_does_not_establish_source_identity(config, state, paper, tmp_path, offline):
    base, _ = regional_base.__wrapped__(tmp_path)
    options = options_for(config, tmp_path, base)
    pdf, _ = completed_visual(config, state, paper, tmp_path, different_title=True)
    manifest, _ = pipeline._manifest(config, state, options, [pdf], base)
    incoming, = manifest['papers']
    assert not incoming['eligible']
    assert incoming['source_identity_status'] == 'needs_identity_review'
    assert not any(chunk['answer_eligible'] for page in incoming['pages'] for chunk in page['chunks'])


def test_reconversion_of_the_same_pdf_updates_its_existing_source_id(config, state, paper, tmp_path, offline):
    import sqlite3

    base, _ = regional_base.__wrapped__(tmp_path)
    options = options_for(config, tmp_path, base)
    pdf, _ = completed_visual(config, state, paper, tmp_path)
    with sqlite3.connect(base / 'index.sqlite') as db:
        db.execute('UPDATE papers SET doi=?,source_sha256=?,source_metadata=? WHERE id=?',
                   (pdf['doi'], pdf['sha256'], json.dumps({'legacy_provenance': 'retained'}), 'one'))
    for name in ('body-index-receipt.json', 'embedding-receipt.json'):
        receipt = read_json(base / name)
        receipt['index_sha256'] = hashlib.sha256((base / 'index.sqlite').read_bytes()).hexdigest()
        write_json(base / name, receipt)
    manifest, _ = pipeline._manifest(config, state, options, [pdf], base)
    incoming, = manifest['papers']
    assert incoming['paper_id'] == incoming['article_id'] == 'one'
    assert incoming['source_metadata']['legacy_provenance'] == 'retained'


@pytest.mark.parametrize('tamper', ['physical_page', 'policy', 'markdown', 'selected_pages'])
def test_incomplete_or_changed_selected_pages_never_reach_publication(config, state, paper, tmp_path, offline, monkeypatch, tamper):
    base, _ = regional_base.__wrapped__(tmp_path)
    options = options_for(config, tmp_path, base, vision=True)
    _, root = completed_visual(config, state, paper, tmp_path)
    if tamper in ('physical_page', 'policy'):
        path = root / 'refinement/0002/page-status.json'
        value = read_json(path)
        value['source_pdf_page' if tamper == 'physical_page' else 'policy'] = 99 if tamper == 'physical_page' else 'other'
        write_json(path, value)
    elif tamper == 'markdown':
        (root / 'refinement/0002/page.md').write_text('Unrecorded replacement number 99.', encoding='utf-8')
    else:
        path = root / 'status.json'
        value = read_json(path)
        value['selected_pages'] = [99]
        write_json(path, value)
    monkeypatch.setattr(pipeline, 'convert', lambda *_: {'completed': 0})
    with pytest.raises(ValueError):
        pipeline.run(config)
    assert not options['active_pointer'].exists()
    assert 'error' in read_json(config.data_dir / 'automation/last-run.json')


def test_e5_requires_hashed_model_and_tokenizer_before_loading_optional_dependencies(tmp_path):
    write_json(tmp_path / 'receipt.json', {'repo': 'example/model', 'revision': 'fixed', 'files': []})
    with pytest.raises(ValueError, match='both the ONNX weights and tokenizer'):
        E5(tmp_path, {'model': 'example/model', 'revision': 'fixed'})


def test_e5_rejects_a_changed_pinned_model_file(tmp_path):
    paths = ['model_qint8_avx512_vnni.onnx', 'tokenizer.json']
    for name in paths:
        (tmp_path / name).write_bytes(b'unit fixture')
    write_json(tmp_path / 'receipt.json', {'repo': 'example/model', 'revision': 'fixed',
        'files': [{'file': name, 'sha256': '0' * 64} for name in paths]})
    with pytest.raises(ValueError, match='hash differs'):
        E5(tmp_path, {'model': 'example/model', 'revision': 'fixed'})


def test_e5_never_silently_truncates_over_budget_chunks():
    np = pytest.importorskip('numpy')
    encoder = E5.__new__(E5)
    encoder.np, encoder.dimensions, encoder.lock = np, 2, threading.Lock()
    encoder.tokenizer = SimpleNamespace(encode_batch=lambda _: [SimpleNamespace(ids=[1] * 513)])
    assert encoder.encode([]).shape == (0, 2)
    with pytest.raises(ValueError, match='token budget'):
        encoder.encode(['Too long'])
