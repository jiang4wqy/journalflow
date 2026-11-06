import copy
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from journalflow import pipeline, trial_gate
from journalflow.util import digest_file, read_json, write_json


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    """Only synthetic receipts/SQLite and a mocked heavy validator; no live cycle."""
    config = SimpleNamespace(path=tmp_path / 'config.toml', data_dir=tmp_path / 'data',
                             vision=SimpleNamespace(policy_hash='test-policy'))
    config.path.write_text('[automation]\ndownload_enabled=true\nvision_enabled=true\n', encoding='utf-8')
    config.data_dir.mkdir()
    connection = sqlite3.connect(':memory:')
    connection.executescript('''
        CREATE TABLE pdfs(id INTEGER,doi TEXT,sha256 TEXT,path TEXT,source_url TEXT,created_at TEXT);
        CREATE TABLE conversions(pdf_id INTEGER,policy TEXT,state TEXT,updated_at TEXT);
        CREATE TABLE baseline_sources(doi TEXT,pdf_sha256 TEXT,pdf_path TEXT,source_index TEXT,available INTEGER);
    ''')
    state = SimpleNamespace(root=config.data_dir, connection=connection)
    baseline_pdf, baseline_index = tmp_path / 'baseline.pdf', tmp_path / 'baseline.sqlite'
    baseline_pdf.write_bytes(b'fixture baseline source')
    baseline_index.write_bytes(b'fixture baseline index already registered')
    connection.execute('INSERT INTO baseline_sources VALUES(?,?,?,?,1)',
                       ('10.9999/baseline', digest_file(baseline_pdf), str(baseline_pdf), str(baseline_index)))
    candidate = tmp_path / 'generation'
    candidate.mkdir()
    papers = []
    with closing(sqlite3.connect(candidate / 'index.sqlite')) as db:
        db.executescript('''
            CREATE TABLE papers(id TEXT,doi TEXT,source_sha256 TEXT,eligible INTEGER);
            CREATE TABLE chunks(paper_id TEXT,answer_eligible INTEGER,page_review_required INTEGER,
                                region_confirmed INTEGER,region_evidence TEXT);
        ''')
        for number in range(10):
            pid, doi = f'paper-{number}', f'10.9999/live-{number}'
            path = config.data_dir / f'{number}.pdf'
            path.write_bytes(f'Synthetic HTTP intake fixture {number}'.encode())
            sha = digest_file(path)
            connection.execute('INSERT INTO pdfs VALUES(?,?,?,?,?,?)',
                               (number + 1, doi, sha, path.name, 'https://publisher.example/authorized.pdf',
                                '2026-01-01T00:10:00+00:00'))
            connection.execute('INSERT INTO conversions VALUES(?,?,?,?)',
                               (number + 1, 'test-policy', 'completed', '2026-01-01T00:20:00+00:00'))
            papers.append(dict(paper_id=pid, doi=doi, pdf_sha256=sha, eligible=True))
            db.execute('INSERT INTO papers VALUES(?,?,?,1)', (pid, doi, sha))
            db.execute('INSERT INTO chunks VALUES(?,1,0,0,NULL)', (pid,))
        # Real legacy corpora can include answerable non-DOI front matter.
        db.execute('INSERT INTO papers VALUES(?,NULL,?,1)', ('old-front-matter', 'not-a-trial-source'))
        db.execute('INSERT INTO chunks VALUES(?,1,0,0,NULL)', ('old-front-matter',))
        db.commit()
    manifest = dict(kind='visual_conversion', source_kind='production_sources', new_downloads=True, papers=papers)
    write_json(candidate / 'incremental-manifest.json', manifest)
    verified = dict(success=True, index_sha256=digest_file(candidate / 'index.sqlite'),
                    manifest_sha256=hashlib.sha256(b'fixture manifest').hexdigest())
    options = {'active_pointer': tmp_path / 'active.json'}
    write_json(options['active_pointer'], {'index': str(candidate), **verified})
    report = dict(run_kind='live_pipeline', config_sha256=digest_file(config.path), policy='test-policy',
                  published=True, new_pdf_ids=list(range(1, 11)), fetch={'downloaded': 10},
                  conversion={'completed': 10}, started_at='2026-01-01T00:00:00+00:00',
                  finished_at='2026-01-01T01:00:00+00:00',
                  regional={'index': str(candidate), 'manifest_sha256': verified['manifest_sha256'],
                            'validation': copy.deepcopy(verified)})
    validator = Mock(return_value=verified)
    monkeypatch.setattr(trial_gate, 'validate_generation', validator)
    yield SimpleNamespace(config=config, state=state, report=report, options=options, candidate=candidate,
                          manifest=manifest, verified=verified, validator=validator, baseline_index=baseline_index)
    connection.close()


def test_same_ten_live_sources_issue_gate_without_installing_a_schedule(evidence):
    e = evidence
    result = trial_gate.issue_gate(e.config, e.state, e.report, e.options)
    assert result['state'] == 'approved' and result['schedule_enabled'] is False
    gate = read_json(Path(result['gate']))
    assert gate['trial']['kind'] == 'live_download_conversion'
    assert gate['trial']['documents'] == len({row['doi'] for row in gate['trial']['sources']}) == 10
    assert {row['pdf_id'] for row in gate['trial']['sources']} == set(range(1, 11))
    assert gate['baseline']['passed'] and gate['regional']['passed']
    assert gate['config_sha256'] == digest_file(e.config.path)
    e.validator.assert_called_once_with(e.candidate)


@pytest.mark.parametrize('case', [
    'paused', 'config', 'policy', 'few_ids', 'holdout', 'old_backlog', 'sha', 'identity',
    'local_import', 'duplicate_doi', 'baseline_copy', 'baseline_without_doi', 'changed_pdf', 'regional', 'pointer',
    'not_answerable', 'missing_baseline', 'old_timestamp', 'validator_error',
])
def test_incomplete_evidence_stays_pending_and_preserves_prior_gate(evidence, case):
    e = evidence
    gate_path = e.config.data_dir / 'automation/gate.json'
    write_json(gate_path, {'previous': 'do not fabricate or overwrite a completed proof'})
    previous = gate_path.read_bytes()
    if case == 'paused':
        e.config.path.write_text('[automation]\ndownload_enabled=false\nvision_enabled=true\n', encoding='utf-8')
    elif case == 'config':
        e.report['config_sha256'] = 'changed'
    elif case == 'policy':
        e.report['policy'] = 'other-policy'
    elif case == 'few_ids':
        e.report['new_pdf_ids'] = list(range(1, 10))
    elif case == 'holdout':
        e.manifest['kind'] = 'baseline_holdout'  # Even a forged new_downloads=true cannot approve it.
    elif case == 'old_backlog':
        e.state.connection.execute('DELETE FROM conversions WHERE pdf_id=10')
        # Report still says ten conversions: only nine belong to the actual new IDs.
    elif case == 'sha':
        e.manifest['papers'][-1]['pdf_sha256'] = 'different version'
    elif case == 'identity':
        e.manifest['papers'][-1]['eligible'] = False
    elif case == 'local_import':
        e.state.connection.execute("UPDATE pdfs SET source_url='local_import' WHERE id=10")
    elif case == 'duplicate_doi':
        e.state.connection.execute("UPDATE pdfs SET doi='10.9999/live-0' WHERE id=10")
        e.manifest['papers'][-1]['doi'] = '10.9999/live-0'
    elif case in ('baseline_copy', 'baseline_without_doi'):
        one = e.manifest['papers'][0]
        e.state.connection.execute('INSERT INTO baseline_sources VALUES(?,?,?,?,1)',
                                  (one['doi'] if case == 'baseline_copy' else None, one['pdf_sha256'],
                                   str(e.config.data_dir / '0.pdf'), str(e.baseline_index)))
    elif case == 'changed_pdf':
        (e.config.data_dir / '0.pdf').write_bytes(b'changed since the HTTP intake')
    elif case == 'regional':
        e.verified['index_sha256'] = 'changed regional snapshot'
    elif case == 'pointer':
        value = read_json(e.options['active_pointer'])
        value['index_sha256'] = 'another snapshot'
        write_json(e.options['active_pointer'], value)
    elif case == 'not_answerable':
        with closing(sqlite3.connect(e.candidate / 'index.sqlite')) as db:
            db.execute("UPDATE papers SET eligible=0 WHERE id='paper-9'")
            db.commit()
    elif case == 'missing_baseline':
        e.baseline_index.unlink()
    elif case == 'old_timestamp':
        e.state.connection.execute("UPDATE conversions SET updated_at='2025-12-31T23:00:00+00:00' WHERE pdf_id=10")
    else:
        e.validator.side_effect = ValueError('fresh validation failed')
    write_json(e.candidate / 'incremental-manifest.json', e.manifest)
    result = trial_gate.issue_gate(e.config, e.state, e.report, e.options)
    assert result['state'] == 'pending' and result['gate_unchanged']
    assert gate_path.read_bytes() == previous


def test_pipeline_records_observed_ids_configuration_and_policy_before_acceptance(tmp_path, monkeypatch):
    config = SimpleNamespace(path=tmp_path / 'config.toml', data_dir=tmp_path / 'data',
                             vision=SimpleNamespace(policy_hash='test-policy'))
    config.path.write_text('[automation]\ndownload_enabled=true\nvision_enabled=true\n', encoding='utf-8')
    state = SimpleNamespace(connection=Mock(), summary=Mock(return_value={}), close=Mock())
    state.connection.execute.side_effect = [[], [(value,) for value in range(1, 11)]]
    http = SimpleNamespace(close=Mock())
    monkeypatch.setattr(pipeline, 'State', lambda *_: state)
    monkeypatch.setattr(pipeline, 'Http', lambda *_args, **_kwargs: http)
    monkeypatch.setattr(pipeline, 'discover', Mock(return_value={'discovered': 10}))
    monkeypatch.setattr(pipeline, 'fetch', Mock(return_value={'downloaded': 10}))
    monkeypatch.setattr(pipeline, 'convert', Mock(return_value={'completed': 10}))
    acceptance = Mock(return_value={'state': 'pending', 'reason': 'No regional publication in this mock'})
    monkeypatch.setattr(trial_gate, 'issue_gate', acceptance)
    report = pipeline.run(config)
    assert report['new_pdf_ids'] == list(range(1, 11))
    assert report['config_sha256'] == digest_file(config.path) and report['policy'] == 'test-policy'
    assert report['trial_gate']['state'] == 'pending'
    acceptance.assert_called_once()
    state.close.assert_called_once()
    http.close.assert_called_once()
