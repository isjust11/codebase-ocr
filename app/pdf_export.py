"""Phase 6 — Tạo PDF export.

Hai chế độ:
- **text-only** (mặc định): nền trắng + text đã chỉnh theo bbox + ảnh/figure đã tách.
  Không chèn ảnh scan/PDF gốc — phù hợp xuất tài liệu sau biên tập OCR.
- **searchable** (includeSourceImage): ảnh gốc từng trang + lớp text ẩn (render_mode=3).

Dùng PyMuPDF: toạ độ pixel bbox chia cho zoom = dpi/72 ra điểm PDF.
"""

from __future__ import annotations

import io
import logging
import os
from typing import List, Optional, TYPE_CHECKING

import fitz  # PyMuPDF
from PIL import Image

from .config import config
from .pdf_renderer import DocumentRenderer
from .schemas import OcrExportAsset, OcrExportPage

if TYPE_CHECKING:
    from .s3_client import S3Client

logger = logging.getLogger("ocr.export")


def _encode_png(image) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    return buf.getvalue()


def _poly_bounds(poly: List[List[float]]):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


class PdfExporter:
    def __init__(self) -> None:
        font_path = config.pdf_font_path
        self._font_file: Optional[str] = (
            font_path if font_path and os.path.exists(font_path) else None
        )
        self._font_name = "ocrfont" if self._font_file else "helv"
        if not self._font_file:
            logger.warning(
                "Không thấy font Unicode (%s); fallback 'helv' (có thể "
                "thiếu dấu tiếng Việt).",
                font_path,
            )

    def build(self, src_path: str, pages: List[OcrExportPage]) -> bytes:
        """Searchable PDF: render ảnh gốc + lớp text ẩn."""
        zoom = config.render_dpi / 72.0
        lines_by_page = {p.page: p.lines for p in pages}

        out = fitz.open()
        try:
            with DocumentRenderer(src_path, config.render_dpi) as renderer:
                page_numbers = (
                    [p.page for p in pages]
                    if pages
                    else renderer.select_pages(None)
                )
                for page_no in page_numbers:
                    rp = renderer.render_page(page_no)
                    w_pt = rp.width / zoom
                    h_pt = rp.height / zoom
                    new_page = out.new_page(width=w_pt, height=h_pt)
                    rect = fitz.Rect(0, 0, w_pt, h_pt)
                    new_page.insert_image(rect, stream=_encode_png(rp.image))
                    self._overlay_text_hidden(
                        new_page, lines_by_page.get(page_no, []), zoom
                    )
            return out.tobytes()
        finally:
            out.close()

    def build_text_only(
        self,
        pages: List[OcrExportPage],
        s3: Optional["S3Client"] = None,
    ) -> bytes:
        """PDF sạch: nền trắng + text hiển thị + ảnh tách (không ảnh scan gốc)."""
        zoom = config.render_dpi / 72.0
        out = fitz.open()
        try:
            for page_data in sorted(pages, key=lambda p: p.page):
                w = page_data.width or 1
                h = page_data.height or 1
                w_pt = w / zoom
                h_pt = h / zoom
                new_page = out.new_page(width=w_pt, height=h_pt)

                if page_data.assets and s3:
                    self._place_assets(new_page, page_data.assets, s3, zoom)

                self._overlay_text_visible(new_page, page_data.lines, zoom)
            return out.tobytes()
        finally:
            out.close()

    def _place_assets(
        self,
        page,
        assets: List[OcrExportAsset],
        s3: "S3Client",
        zoom: float,
    ) -> None:
        for asset in assets:
            if not asset.bbox or not (asset.imageUrl or asset.imageKey):
                continue
            try:
                data = s3.download_bytes(asset.imageUrl, asset.imageKey)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bỏ qua asset export: %s", exc)
                continue
            x1, y1, x2, y2 = _poly_bounds(asset.bbox)
            rect = fitz.Rect(x1 / zoom, y1 / zoom, x2 / zoom, y2 / zoom)
            try:
                page.insert_image(rect, stream=data)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Không chèn được asset vào PDF: %s", exc)

    def _overlay_text_hidden(self, page, lines, zoom: float) -> None:
        for line in lines:
            text = (line.text or "").strip()
            if not text or not line.bbox:
                continue
            x1, y1, x2, y2 = _poly_bounds(line.bbox)
            point = fitz.Point(x1 / zoom, y2 / zoom)
            fontsize = max(6.0, ((y2 - y1) / zoom) * 0.8)
            try:
                page.insert_text(
                    point,
                    text,
                    fontsize=fontsize,
                    fontname=self._font_name,
                    fontfile=self._font_file,
                    render_mode=3,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bỏ qua dòng text ẩn trong PDF: %s", exc)

    def _overlay_text_visible(self, page, lines, zoom: float) -> None:
        for line in lines:
            text = (line.text or "").strip()
            if not text or not line.bbox:
                continue
            x1, y1, x2, y2 = _poly_bounds(line.bbox)
            rect = fitz.Rect(x1 / zoom, y1 / zoom, x2 / zoom, y2 / zoom)
            fontsize = max(6.0, ((y2 - y1) / zoom) * 0.85)
            try:
                overflow = page.insert_textbox(
                    rect,
                    text,
                    fontsize=fontsize,
                    fontname=self._font_name,
                    fontfile=self._font_file,
                    align=fitz.TEXT_ALIGN_LEFT,
                    color=(0, 0, 0),
                )
                if overflow < 0:
                    page.insert_text(
                        fitz.Point(x1 / zoom, y2 / zoom),
                        text,
                        fontsize=fontsize,
                        fontname=self._font_name,
                        fontfile=self._font_file,
                        color=(0, 0, 0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bỏ qua dòng text trong PDF: %s", exc)
