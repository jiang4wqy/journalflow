import json
import sqlite3
from pathlib import Path

import pytest

from journalflow import regional_index
from journalflow.regional import build_incremental, publish_generation
from journalflow.regional_index import RegionalIndex
from journalflow.util import digest_file, read_json, write_json

from .conftest import make_pdf
from .test_regional import base as regional_base
from .test_regional import batch, page, paper


def library(tmp_path):
    index, papers = regional_base.__wrapped__(tmp_path)
    root = tmp_path / '中文 空格 library'
    root.mkdir()
    with sqlite3.connect(index / 'index.sqlite') as db:
        for source in papers:
            path = make_pdf(root / source['pdf_path'], tuple(selected['markdown_text'] for selected in source['pages']))
            source['pdf_sha256'] = digest_file(path)
            db.execute('UPDATE papers SET source_sha256=? WHERE id=?', (source['pdf_sha256'], source['paper_id']))
        db.execute("UPDATE papers SET journal='MS' WHERE id='two'")
    for name in ('embedding-receipt.json', 'body-index-receipt.json'):
        receipt = read_json(index / name)
        receipt['index_sha256'] = digest_file(index / 'index.sqlite')
        write_json(index / name, receipt)
    return root, index, papers


def test_lexical_filters_isolation_physical_pages_and_encoded_pdf_links(tmp_path):
    root, index, _ = library(tmp_path)
    source = RegionalIndex(root, index)
    isr = source.search('confirmed', 'isr', 2025, None, 8)
    assert isr and {item['paper_id'] for item in isr} == {'one'}
    assert {item['paper_id'] for item in source.search('confirmed', 'MS', 2025, None, 8)} == {'two'}
    assert not source.search('confirmed', 'ISR', 2024, None, 8)
    assert source.search('confirmed', 'ISR', None, 'https://doi.org/10.9999/ONE', 1)[0]['doi'] == '10.9999/one'
    assert not source.search('disputed', None, None, None, 8)
    assert source.get_page('one', 2) is None
    for number in (0, -1, 999, '1'):
        assert source.get_page('one', number) is None
    assert source.get_page('missing', 1) is None
    result = source.get_page('one', 1)
    assert result['pdf_url'] == (root / 'one.pdf').as_uri() + '#page=1'
    assert '%20' in result['pdf_url'] and not result['page_has_review_flag']
    assert Path(result['pdf_path']).is_relative_to(root)
    with pytest.raises(ValueError):
        source.search('confirmed', None, None, None, 0)


def test_missing_tampered_outside_or_physically_short_pdf_is_never_returned(tmp_path):
    root, index, _ = library(tmp_path)
    source = RegionalIndex(root, index)
    assert source.get_page('one', 1)
    (root / 'one.pdf').write_bytes(b'not a PDF')
    assert source.get_page('one', 1) is None
    with sqlite3.connect(index / 'index.sqlite') as db:
        db.execute("UPDATE papers SET pdf='../outside.pdf' WHERE id='two'")
    make_pdf(tmp_path / 'outside.pdf')
    assert source.get_page('two', 1) is None
    path = make_pdf(root / 'one.pdf', ('Only a single physical page.',))
    with sqlite3.connect(index / 'index.sqlite') as db:
        db.execute("UPDATE papers SET source_sha256=? WHERE id='one'", (digest_file(path),))
    assert source.get_page('one', 1) is None  # papers.pages says two; the actual PDF says one.


def test_reviewed_region_does_not_release_other_chunks_on_the_same_page(tmp_path):
    root, index, original = library(tmp_path)
    revised = batch(original[:1])
    confirmed = revised['papers'][0]['pages'][1]['chunks'][0]
    confirmed.update(answer_eligible=True, region_confirmed=True,
                     region_evidence={'method': 'strict_match_to_source'})
    result = build_incremental(index, revised, tmp_path / 'generations')
    view = RegionalIndex(root, result['index'])
    assert view.get_page('one', 2)['page_has_review_flag']
    assert view.search('disputed', None, None, None, 8)
    with sqlite3.connect(Path(result['index']) / 'index.sqlite') as db:
        assert db.execute('SELECT answer_eligible FROM chunks WHERE id=2').fetchone() == (0,)


def test_pointer_is_verified_once_then_reloaded_on_atomic_change(tmp_path, monkeypatch):
    root, index, _ = library(tmp_path)
    pointer = tmp_path / 'private/active.json'
    source = RegionalIndex(root, index, pointer)
    assert source.get_page('one', 1)
    incoming = paper('three', [page(1, 'New unique event evidence.')])
    path = make_pdf(root / 'three.pdf', ('New unique event evidence.',))
    incoming['pdf_sha256'] = digest_file(path)
    result = build_incremental(index, batch([incoming]), tmp_path / 'generations', lambda texts: [[1., 0.] for _ in texts])
    publish_generation(result['index'], pointer, result['validation'])
    original = regional_index.resolve_active
    calls = []

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(regional_index, 'resolve_active', counted)
    assert source.get_page('three', 1)
    assert source.get_page('three', 1)
    assert len(calls) == 1
    pointer.unlink()
    assert source.get_page('three', 1) is None
    pointer.write_text(json.dumps({'index': result['index'], 'index_sha256': '0' * 64}), encoding='utf-8')
    with pytest.raises(ValueError, match='Active database'):
        source.get_page('one', 1)


def test_semantic_route_checks_alignment_and_reload_cache(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    root, index, _ = library(tmp_path)
    created = []

    class QueryEncoder:
        def __init__(self, *args):
            created.append(args)

        def encode(self, texts, *, query=False):
            assert query
            return np.asarray([[1., 0.]], dtype=np.float32)

    monkeypatch.setattr('journalflow.embedding.E5', QueryEncoder)
    source = RegionalIndex(root, index, model_dir=tmp_path / 'synthetic model only')
    assert source.search('a semantic synonym absent from lexical text', 'ISR', 2025, None, 1)
    assert source.search('another semantic synonym', 'ISR', 2025, None, 1)
    assert len(created) == 1
    values = np.load(index / 'body-vector-ids.npy')
    np.save(index / 'body-vector-ids.npy', values[::-1])
    with pytest.raises(ValueError, match='route IDs differ'):
        source.search('another semantic synonym', 'ISR', 2025, None, 1)
