from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium


class Document:
    def __init__(self, path: Path):
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise ValueError("File is not a PDF; an HTML login page is not a PDF")
        try:
            self.pdf = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as error:
            raise ValueError("PDF cannot be opened; damaged or password-protected documents require user action") from error
        if len(self.pdf) == 0:
            self.pdf.close()
            raise ValueError("PDF has no pages")

    def __len__(self):
        return len(self.pdf)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.pdf.close()

    def text(self, index: int):
        page = self.pdf[index]
        try:
            textpage = page.get_textpage()
            try:
                return textpage.get_text_bounded()
            finally:
                textpage.close()
        finally:
            page.close()

    def render(self, index: int, dpi: int):
        # PDFium uses BGR here, matching Paddle's image reader. Copy before closing the bitmap.
        page = self.pdf[index]
        try:
            bitmap = page.render(scale=dpi / 72, force_bitmap_format=pdfium.raw.FPDFBitmap_BGR)
            try:
                return bitmap.to_numpy().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
