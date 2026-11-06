"""Read a reviewed regional index and follow its atomic generation pointer."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from .pdf import Document
from .regional import resolve_active
from .util import digest_file, normalize_doi

SAFE = ("p.eligible=1 AND c.answer_eligible=1 AND (c.page_review_required=0 OR "
        "(c.region_confirmed=1 AND c.region_evidence IS NOT NULL AND length(c.region_evidence)>2))")
SELECT = ("SELECT c.*,p.title,p.doi,p.journal,p.year,p.pdf,p.source_sha256,"
          "p.source_version,p.source_completeness,p.pages AS source_pages FROM chunks c JOIN papers p ON p.id=c.paper_id WHERE ")


class RegionalIndex:
    def __init__(self, library_root, base_index, active_pointer=None, model_dir=None):
        self.root = Path(library_root).resolve()
        self.base = Path(base_index).resolve()
        if self.base.name == 'index.sqlite':
            self.base = self.base.parent
        self.pointer = Path(active_pointer).resolve() if active_pointer else None
        self.model_dir = Path(model_dir).resolve() if model_dir else None
        self.loaded = None
        self.encoder = None
        self.encoder_identity = None
        self.lock = threading.RLock()
        self.pointer_content = object()
        self.accepted_index = None
        self.accepted_stat = None
        self.accepted_sha = None
        self.pdf_cache = {}

    @property
    def index(self):
        with self.lock:
            try:
                content = self.pointer.read_bytes() if self.pointer else None
            except FileNotFoundError:
                content = None
            if content != self.pointer_content or self.accepted_index is None:
                self.accepted_index = resolve_active(self.pointer, self.base) if content is not None else self.base
                self.accepted_sha = json.loads(content)['index_sha256'] if content is not None else None
                self.pointer_content = content
                self.accepted_stat = self._stat(self.accepted_index / 'index.sqlite')
            elif content is not None and self._stat(self.accepted_index / 'index.sqlite') != self.accepted_stat:
                self.accepted_index = resolve_active(self.pointer, self.base)
                self.accepted_stat = self._stat(self.accepted_index / 'index.sqlite')
            return self.accepted_index

    @staticmethod
    def _stat(path):
        value = path.stat()
        return value.st_size, value.st_mtime_ns, value.st_ino

    def connect(self, index):
        db = sqlite3.connect((index / 'index.sqlite').as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return closing(db)

    def source(self, row, text, maximum):
        path = (self.root / row['pdf']).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        if not 1 <= row['pdf_page'] <= row['source_pages']:
            return None
        key = (path, self._stat(path), row['source_sha256'])
        if key not in self.pdf_cache:
            if digest_file(path) != row['source_sha256']:
                self.pdf_cache[key] = None
            else:
                try:
                    with Document(path) as document:
                        self.pdf_cache[key] = len(document)
                except (OSError, RuntimeError, ValueError):
                    self.pdf_cache[key] = None
        if self.pdf_cache[key] != row['source_pages']:
            return None
        return dict(paper_id=row['paper_id'], doi=row['doi'], title=row['title'],
                    journal=row['journal'], year=row['year'], pdf_page=row['pdf_page'],
                    source_sha256=row['source_sha256'], source_version=row['source_version'],
                    source_completeness=row['source_completeness'],
                    page_has_review_flag=bool(row['page_review_required']),
                    text=text[:maximum], truncated=len(text) > maximum, pdf_path=str(path),
                    pdf_url=f'{path.as_uri()}#page={row["pdf_page"]}')

    def search(self, query, journal, year, doi, limit):
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 400
                or type(limit) is not int or not 1 <= limit <= 8):
            raise ValueError('Expected a nonempty query and a limit between 1 and 8')
        if year is not None and (type(year) is not int or not 1 <= year <= 9999):
            raise ValueError('Publication year must be between 1 and 9999')
        if journal is not None and not isinstance(journal, str):
            raise ValueError('Journal filter must be text')
        doi = normalize_doi(doi) if doi else None
        with self.lock:
            return self._search(query.strip(), journal, year, doi, limit)

    def _search(self, query, journal, year, doi, limit):
        index = self.index
        where, params = [SAFE], []
        for field, value in (('p.journal', journal.upper() if journal else None),
                             ('p.year', year), ('p.doi', doi)):
            if value is not None:
                where.append(field + '=? COLLATE NOCASE')
                params.append(value)
        condition = ' AND '.join(where)
        words = re.findall(r'[^\W_]+', query, re.UNICODE)[:16]
        if not words:
            return []
        expression = ' OR '.join('"' + word + '"' for word in words)
        with self.connect(index) as db:
            lexical = [row[0] for row in db.execute(
                'SELECT c.id FROM lexical JOIN chunks c ON c.id=lexical.rowid '
                'JOIN papers p ON p.id=c.paper_id WHERE lexical MATCH ? AND ' + condition +
                ' ORDER BY bm25(lexical) LIMIT 100', [expression, *params])]
            rankings = [lexical]
            if self.model_dir:
                import numpy as np
                signature = (index, self._stat(index / 'index.sqlite'), *[
                    self._stat(index / name) for name in ('vectors.npy', 'vector-ids.npy', 'body-vectors.npy', 'body-vector-ids.npy')])
                if self.loaded != signature:
                    identity = json.loads((index / 'embedding-receipt.json').read_bytes())
                    accepted = self.accepted_sha or digest_file(index / 'index.sqlite')
                    body_receipt = json.loads((index / 'body-index-receipt.json').read_bytes())
                    if identity['index_sha256'] != accepted or body_receipt['index_sha256'] != accepted:
                        raise ValueError('Regional vector receipts are stale')
                    from .embedding import E5
                    model_identity = identity['model'], identity['revision']
                    if self.encoder is None or self.encoder_identity != model_identity:
                        self.encoder = E5(self.model_dir, identity)
                        self.encoder_identity = model_identity
                    self.ids = np.load(index / 'vector-ids.npy', mmap_mode='r')
                    self.vectors = np.load(index / 'vectors.npy', mmap_mode='r')
                    self.body = np.load(index / 'body-vectors.npy', mmap_mode='r')
                    body_ids = np.load(index / 'body-vector-ids.npy', mmap_mode='r')
                    expected = (len(self.ids), identity['dimensions'])
                    if (self.vectors.shape != expected or self.body.shape != expected
                            or not np.array_equal(self.ids, body_ids) or len(np.unique(self.ids)) != len(self.ids)
                            or len(self.ids) != identity['total']):
                        raise ValueError('Regional vector shapes or route IDs differ')
                    if (digest_file(index / 'body-vectors.npy') != body_receipt['vectors_sha256']
                            or digest_file(index / 'body-vector-ids.npy') != body_receipt['ids_sha256']):
                        raise ValueError('Regional body-vector hash differs')
                    checks = {}
                    if (index / 'incremental-receipt.json').is_file():
                        checks = json.loads((index / 'incremental-receipt.json').read_bytes())['files']
                    elif (index / 'region-verification.json').is_file():
                        checks = {name: item['sha256'] for name, item in json.loads(
                            (index / 'region-verification.json').read_bytes())['files'].items()}
                    for name in ('vectors.npy', 'vector-ids.npy'):
                        if name in checks and digest_file(index / name) != checks[name]:
                            raise ValueError('Regional title-vector hash differs')
                    self.loaded = signature
                allowed = {row[0] for row in db.execute('SELECT c.id FROM chunks c JOIN papers p '
                                                       'ON p.id=c.paper_id WHERE ' + condition, params)}
                if not allowed:
                    return []
                vector = self.encoder.encode([query], query=True)[0]
                for matrix in (self.vectors, self.body):
                    positions = np.argsort(matrix @ vector)[::-1]
                    ranking = []
                    for position in positions:
                        chunk_id = int(self.ids[position])
                        if chunk_id in allowed:
                            ranking.append(chunk_id)
                            if len(ranking) == 100:
                                break
                    rankings.append(ranking)
            scores = {}
            for ranking in rankings:
                for rank, chunk_id in enumerate(ranking):
                    scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank + 1)
            results = []
            for chunk_id in sorted(scores, key=scores.get, reverse=True):
                row = db.execute(SELECT + condition + ' AND c.id=?', [*params, chunk_id]).fetchone()
                if row:
                    source = self.source(row, row['text'], 1800)
                    if source:
                        results.append(source)
                if len(results) >= limit:
                    break
            return results

    def get_page(self, paper_id, pdf_page):
        if type(pdf_page) is not int or pdf_page < 1:
            return None
        with self.connect(self.index) as db:
            rows = db.execute(SELECT + SAFE + ' AND c.paper_id=? AND c.pdf_page=? ORDER BY c.id',
                              [paper_id, pdf_page]).fetchall()
        if not rows:
            return None
        first = dict(rows[0])
        first['page_review_required'] = any(row['page_review_required'] for row in rows)
        return self.source(first, '\n\n'.join(dict.fromkeys(row['text'] for row in rows)), 6000)
