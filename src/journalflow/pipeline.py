"""Control cycle with explicit pauses, reviewed publication, and live-trial acceptance."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tomllib
import unicodedata
from contextlib import closing
from pathlib import Path

from .conversion import check_page, convert
from .discovery import discover
from .http import Http
from .intake import fetch
from .pdf import Document
from .state import State
from .util import digest_file, now, read_json, slug, write_json


def settings(config):
    raw = tomllib.loads(config.path.read_text(encoding='utf-8'))
    regional = raw.get('regional', {})
    result = {key: (config.path.parent / value).resolve() for key, value in regional.items()
              if key in ('base_index', 'library_root', 'output_root', 'active_pointer', 'model_dir')}
    return result, raw.get('automation', {}).get('vision_enabled', False) is True


def regional_build(base_index, manifest, output_root, model_dir=None, active_pointer=None):
    from .regional import build_incremental, publish_generation
    identity = read_json(Path(base_index) / 'embedding-receipt.json')
    encoder = None
    if model_dir:
        from .embedding import E5
        encoder = E5(Path(model_dir), identity)
    result = build_incremental(Path(base_index), manifest, Path(output_root), encoder,
                               embedding_identity={'model': identity['model'], 'revision': identity['revision']})
    if active_pointer:
        result['publication'] = publish_generation(Path(result['index']), Path(active_pointer), result['validation'])
    return result


def _manifest(config, state, options, pdfs, base):
    """Bridge complete selected visual pages; uncertain pages remain unavailable."""
    from .embedding import E5
    identity = read_json(base / 'embedding-receipt.json')
    encoder = E5(options['model_dir'], identity)
    tokenizer = encoder.tokenizer
    result = {'schema_version': 1, 'kind': 'visual_conversion', 'new_downloads': False,
              'new_downloads_semantics': 'new production sources; downloaded_this_run is the actual cycle intake count',
              'source_kind': 'production_sources', 'source_counts': {},
              'embedding_identity': {'model': identity['model'], 'revision': identity['revision']}, 'papers': []}
    for pdf in pdfs:
        source = state.root / pdf['path']
        if digest_file(source) != pdf['sha256']:
            raise ValueError('Source PDF hash differs from its downloaded receipt')
        with Document(source) as doc:
            if len(doc) != pdf['pages']:
                raise ValueError('Source PDF physical-page count changed')
            header = '\n'.join(doc.text(i) for i in range(min(2, len(doc))))
        normalized_header = ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', header).casefold()))
        normalized_title = ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', pdf['title']).casefold()))
        doi_in_header = bool(re.search(r'(?<![\w.])' + re.escape(pdf['doi']) + r'(?![\w./])', header, re.I))
        identified = bool(doi_in_header and normalized_title and normalized_title in normalized_header)
        source_kind = ('recorded_remote_url' if pdf['source_url'].startswith(('https://', 'http://'))
                       else 'authorized_local_import')
        result['source_counts'][source_kind] = result['source_counts'].get(source_kind, 0) + 1
        relative = Path('incremental-sources') / slug(pdf['doi']) / (pdf['sha256'] + '.pdf')
        target = options['library_root'] / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_suffix('.part')
            shutil.copyfile(source, temporary)
            if digest_file(temporary) != pdf['sha256']:
                raise ValueError('New PDF asset hash changed during copying')
            temporary.replace(target)
        elif digest_file(target) != pdf['sha256']:
            raise ValueError('Refusing to overwrite an existing different source PDF')
        conversion = state.connection.execute('SELECT output_path FROM conversions WHERE pdf_id=? AND policy=? '
                                              "AND state='completed'", (pdf['id'], config.vision.policy_hash)).fetchone()
        if conversion is None:
            raise ValueError('A completed conversion for this policy is required')
        root = (state.root / conversion[0]).resolve()
        if not root.is_relative_to(state.root.resolve()):
            raise ValueError('Conversion output leaves its private state directory')
        status = read_json(root / 'status.json')
        if (status['source_sha256'] != pdf['sha256'] or status['pages_saved'] != pdf['pages']
                or status['pages_expected'] != pdf['pages'] or status['policy'] != config.vision.policy_hash):
            raise ValueError('Conversion receipt does not cover the source PDF')
        valid_pages = set(range(1, pdf['pages'] + 1))
        if (len(status['selected_pages']) != len(set(status['selected_pages']))
                or not set(status['selected_pages']).issubset(set(status['refined_pages']))
                or not set(status['refined_pages']).issubset(valid_pages)
                or len(status['review_pages']) != len(set(status['review_pages']))
                or not set(status['review_pages']).issubset(valid_pages)):
            raise ValueError('Conversion selected/review page numbers are inconsistent')
        recorded = state.connection.execute('SELECT page,text,markdown FROM page_search WHERE doi=? AND pdf_sha=? ORDER BY CAST(page AS INTEGER)',
                                            (pdf['doi'], pdf['sha256'])).fetchall()
        if [int(row['page']) for row in recorded] != list(range(1, pdf['pages'] + 1)):
            raise ValueError('Conversion selected-page records are incomplete or duplicated')
        title_ids = tokenizer.encode(pdf['title'], add_special_tokens=False).ids[:56]
        title = tokenizer.decode(title_ids)
        with closing(sqlite3.connect((base / 'index.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            existing = db.execute('SELECT * FROM papers WHERE doi=? COLLATE NOCASE AND source_sha256=? '
                                  'ORDER BY eligible DESC,id LIMIT 1', (pdf['doi'], pdf['sha256'])).fetchone()
        source_metadata = json.loads(existing['source_metadata'] or '{}') if existing else {}
        source_metadata.update(conversion_policy=config.vision.policy_hash, method='visual', source_kind=source_kind)
        paper = {'paper_id': existing['id'] if existing else slug(pdf['doi']) + '__' + pdf['sha256'][:12],
                 'article_id': existing['article_id'] if existing else slug(pdf['doi']),
                 'doi': pdf['doi'], 'title': pdf['title'], 'embedding_title': title, 'journal': pdf['journal'],
                 'year': _year(json.loads(pdf['metadata'])), 'pdf_path': relative.as_posix(),
                 'pdf_sha256': pdf['sha256'], 'page_count': pdf['pages'], 'eligible': identified,
                 'source_version': existing['source_version'] if existing and pdf['version'] == 'unknown' else pdf['version'],
                 'source_completeness': 'all_downloaded_pdf_pages',
                 'source_identity_status': 'doi_and_title_in_pdf_text' if identified else 'needs_identity_review',
                 'source_metadata': source_metadata, 'pages': []}
        references_started = False
        for number in range(1, pdf['pages'] + 1):
            branch = 'refinement' if number in status['selected_pages'] else 'primary'
            folder = root / branch / f'{number:04d}'
            text, blocks = check_page(folder)
            page_status = read_json(folder / 'page-status.json')
            if (page_status['source_sha256'] != pdf['sha256'] or page_status['source_pdf_page'] != number
                    or page_status['policy'] != config.vision.policy_hash or page_status['recognition_method'] != 'visual'):
                raise ValueError('Selected page source, physical page, or recognition policy differs')
            original_flags = page_status['quality']['review_flags']
            if bool(original_flags) != (number in status['review_pages']):
                raise ValueError('Selected page review state differs from its conversion receipt')
            row = recorded[number - 1]
            if row['text'] != text or (state.root / row['markdown']).resolve() != (folder / 'page.md').resolve():
                raise ValueError('Selected Markdown hash/path differs from the completed conversion')
            uncertain = [block.get('block_label') for block in blocks if block.get('block_label') in
                         ('table', 'display_formula', 'inline_formula', 'algorithm', 'chart')]
            flagged = bool(original_flags or uncertain)
            page = {'pdf_page': number, 'markdown_path': (Path('incremental') / paper['paper_id'] /
                    f'{number:04d}.md').as_posix(), 'markdown_text': text,
                    'markdown_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'variant': 'refinement' if branch == 'refinement' else 'initial', 'needs_review': flagged,
                    'review_reasons': list(original_flags) + ['unverified_layout:' + label for label in uncertain], 'chunks': []}
            if re.search(r'(?im)^\s*#{0,6}\s*(?:online\s+|internet\s+)?appendix\b', text):
                references_started = False
            heading = re.search(r'(?im)^\s*#{0,6}\s*(?:\d+[.)]?\s+)?(?:references|bibliography)\s*$', text)
            sections = [('references' if references_started else 'prose', text)]
            if heading:
                sections = [('references' if references_started else 'prose', text[:heading.start()]),
                            ('references', text[heading.start():])]
                references_started = True
            for kind, section in sections:
                tokens = tokenizer.encode(section, add_special_tokens=False).ids
                for start in range(0, len(tokens), 352):
                    body = tokenizer.decode(tokens[start:start + 400])
                    if not body.strip():
                        continue
                    page['chunks'].append({'text': body, 'sha256': hashlib.sha256(body.encode()).hexdigest(),
                        'page_review_required': int(flagged), 'answer_eligible': int(identified and not flagged and kind == 'prose'),
                        'region_confirmed': 0, 'region_evidence': None, 'content_kind': kind})
            paper['pages'].append(page)
        result['papers'].append(paper)
    result['new_downloads'] = bool(result['papers'])
    result['new_documents'] = len(result['papers'])
    return result, encoder


def _year(metadata):
    for name in ('published', 'published-online', 'published-print'):
        values = metadata.get('dates', {}).get(name, {}).get('date-parts', [])
        if values and values[0]:
            return values[0][0]
    return None


def run(config, limit=10):
    options, vision_enabled = settings(config)
    state = State(config.data_dir)
    http = Http(config, state=state)
    download_enabled = tomllib.loads(config.path.read_text(encoding='utf-8')).get('automation', {}).get('download_enabled', False) is True
    report = {'started_at': now(), 'vision_enabled': vision_enabled, 'download_enabled': download_enabled, 'new_downloads': 0,
              'run_kind': 'live_pipeline', 'config_sha256': digest_file(config.path),
              'policy': config.vision.policy_hash, 'new_pdf_ids': [],
              'published': False, 'weekly_enabled': False}
    try:
        before = {row[0] for row in state.connection.execute('SELECT id FROM pdfs')}
        report['discovery'] = discover(config, state, http)
        report['fetch'] = (fetch(config, state, http, limit) if download_enabled
                           else {'state': 'paused_download', 'downloaded': 0})
        new_ids = [row[0] for row in state.connection.execute('SELECT id FROM pdfs') if row[0] not in before]
        report['new_downloads'] = len(new_ids)
        report['new_pdf_ids'] = new_ids
        if not vision_enabled:
            report['conversion'] = {'state': 'paused_by_configuration', 'queued': len(state.conversion_queue(
                config.vision.policy_hash, limit))}
        else:
            report['conversion'] = convert(config, state, limit)
            needed = {'base_index', 'library_root', 'output_root', 'active_pointer', 'model_dir'}
            if needed.issubset(options):
                from .regional import build_incremental, publish_generation, resolve_active
                base = resolve_active(options['active_pointer'], options['base_index'])
                candidates = [dict(row) for row in state.connection.execute('SELECT x.*,p.title,p.journal,p.metadata '
                    'FROM pdfs x JOIN papers p ON p.doi=x.doi JOIN conversions c ON c.pdf_id=x.id '
                    "WHERE c.policy=? AND c.state='completed' ORDER BY x.id", (config.vision.policy_hash,))]
                with closing(sqlite3.connect((base / 'index.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
                    known = {(doi, sha, title) for doi, sha, title, metadata in db.execute(
                        'SELECT doi,source_sha256,title,source_metadata FROM papers')
                        if metadata and json.loads(metadata).get('conversion_policy') == config.vision.policy_hash}
                candidates = [pdf for pdf in candidates if (pdf['doi'], pdf['sha256'], pdf['title']) not in known]
                # A generation receipt is the durable deduplication boundary for converted inputs.
                if candidates:
                    manifest, encoder = _manifest(config, state, options, candidates, base)
                    manifest['downloaded_this_run'] = sum(pdf['id'] in new_ids for pdf in candidates)
                    built = build_incremental(base, manifest, options['output_root'], encoder,
                                               embedding_identity=manifest['embedding_identity'])
                    report['regional'] = built
                    publish_generation(Path(built['index']), options['active_pointer'], built['validation'])
                    report['published'] = True
        report['status'] = state.summary()
        report['finished_at'] = now()
        from .trial_gate import issue_gate
        report['trial_gate'] = issue_gate(config, state, report, options)
        report['finished_at'] = now()
        write_json(config.data_dir / 'automation/last-run.json', report)
        return report
    except Exception as error:
        report.update(error=type(error).__name__ + ': ' + str(error), finished_at=now())
        write_json(config.data_dir / 'automation/last-run.json', report)
        raise
    finally:
        http.close()
        state.close()
