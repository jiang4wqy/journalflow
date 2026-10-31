from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import pytest

from journalflow.config import Config, Journal, Vision
from journalflow.discovery import normalize
from journalflow.state import State

TEXT = ("Research methods compare evidence across experiments and document reproducible outcomes. "
        "Authors describe sampling measurement estimation uncertainty validation limitations assumptions "
        "analysis collaboration evaluation consistency reliability transparency datasets computation "
        "algorithms institutions communication performance accuracy provenance replication models "
        "observations information learning decisions sensitivity interpretation development testing.")


def make_pdf(path, texts=(TEXT,)):
    """Generate synthetic, public test input; never bundle real journal PDFs."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in texts:
        stream = "BT /F1 11 Tf 45 790 Td\n"
        for line in textwrap.wrap(text, 75):
            line = line.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
            stream += f"({line}) Tj 0 -16 Td\n"
        body = (stream + "ET").encode("ascii")
        content_id, page_id = len(objects) + 1, len(objects) + 2
        objects.append(b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream")
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode())
        kids.append(f"{page_id} 0 R")
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, content in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode() + content + b"\nendobj\n")
    start = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    path.write_bytes(output)
    return path


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "config.toml", tmp_path / "data",
                  (Journal("DEMO", "Demo Journal", ("1234-5678", "8765-4321")),),
                  request_interval_seconds=0, vision=Vision(batch_pages=1))


@pytest.fixture
def state(config):
    database = State(config.data_dir)
    yield database
    database.close()


@pytest.fixture
def raw_record():
    today = datetime.now(timezone.utc).date()
    return dict(DOI="10.1234/example", title=["Reproducible research"], type="journal-article",
                published={"date-parts": [[today.year, today.month, today.day]]},
                author=[dict(given="Example", family="Author")])


@pytest.fixture
def paper(config, state, raw_record):
    record = normalize(raw_record, config.journals[0])
    state.upsert(record)
    return record
