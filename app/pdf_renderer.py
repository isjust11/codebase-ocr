"""Render tài liệu thành ảnh từng trang.

- PDF: dùng PyMuPDF (fitz) render mỗi trang ở DPI cấu hình.
- Ảnh (png/jpg/...): coi như tài liệu 1 trang, đọc trực tiếp.

Trả về danh sách `RenderedPage` gồm chỉ số trang (1-based), ảnh numpy (RGB),
kích thước pixel và (nếu là PDF) tham chiếu `fitz.Page` để tách ảnh embedded.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import List, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger("ocr.render")

PDF_EXTS = {".pdf"}


@dataclass
class RenderedPage:
    page_number: int  # 1-based
    image: np.ndarray  # RGB HxWx3 uint8
    width: int
    height: int
    fitz_page: Optional["fitz.Page"] = None  # chỉ có với PDF (để lấy ảnh embedded)
    coord_scale: float = 1.0  # hệ số upscale tiền xử lý (bbox fitz * scale)


def _open_image(path: str) -> Image.Image:
    """Đọc ảnh và áp dụng EXIF Orientation (ảnh chụp camera thường lưu pixel ngang)."""
    with open(path, "rb") as fh:
        img = Image.open(io.BytesIO(fh.read()))
        img.load()
    return ImageOps.exif_transpose(img)


def _pil_to_rgb_np(img: Image.Image) -> np.ndarray:
    if img.mode != "RGB":
        img = img.convert("RGB")
    return np.asarray(img)


def is_pdf(path: str, mime: Optional[str] = None) -> bool:
    if mime and "pdf" in mime.lower():
        return True
    return path.lower().endswith(".pdf")


class DocumentRenderer:
    """Mở tài liệu và render trang theo yêu cầu. Dùng như context manager."""

    def __init__(self, path: str, dpi: int, mime: Optional[str] = None) -> None:
        self._path = path
        self._dpi = dpi
        self._is_pdf = is_pdf(path, mime)
        self._doc: Optional[fitz.Document] = None

    def __enter__(self) -> "DocumentRenderer":
        if self._is_pdf:
            self._doc = fitz.open(self._path)
        return self

    def __exit__(self, *exc) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    @property
    def doc(self) -> Optional[fitz.Document]:
        return self._doc

    def page_count(self) -> int:
        if self._is_pdf and self._doc is not None:
            return self._doc.page_count
        return 1

    def select_pages(self, pages: Optional[List[int]]) -> List[int]:
        """Chuẩn hoá danh sách trang 1-based, lọc ngoài phạm vi."""
        total = self.page_count()
        if not pages:
            return list(range(1, total + 1))
        return [p for p in pages if 1 <= p <= total]

    def render_page(self, page_number: int) -> RenderedPage:
        if self._is_pdf:
            return self._render_pdf_page(page_number)
        return self._render_image_file()

    def _render_pdf_page(self, page_number: int) -> RenderedPage:
        assert self._doc is not None
        page = self._doc.load_page(page_number - 1)
        zoom = self._dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        arr = _pil_to_rgb_np(img)
        return RenderedPage(
            page_number=page_number,
            image=arr,
            width=pix.width,
            height=pix.height,
            fitz_page=page,
        )

    def _render_image_file(self) -> RenderedPage:
        img = _open_image(self._path)
        arr = _pil_to_rgb_np(img)
        h, w = arr.shape[:2]
        return RenderedPage(page_number=1, image=arr, width=w, height=h, fitz_page=None)
