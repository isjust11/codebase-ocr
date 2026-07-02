"""Pydantic models khớp 1-1 với contract trong
`codebase-admin/src/queues/ocr-queue.interface.ts`.

Lưu ý: tên field giữ nguyên camelCase (jobId, processedPages, ...) để JSON
publish ra khớp với những gì OcrService.handleResult mong đợi.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

# bbox = danh sách điểm [x, y] (polygon 4 điểm như PaddleOCR trả về).
BBox = List[List[float]]


class OcrJobMessage(BaseModel):
    """Message backend gửi vào `ocr.jobs`."""

    model_config = ConfigDict(extra="ignore")

    jobId: int
    fileUrl: str
    fileKey: Optional[str] = None
    lang: str = "auto"
    pages: Optional[List[int]] = None
    mode: Optional[str] = "layout"
    extractImages: Optional[bool] = True


class OcrLine(BaseModel):
    text: str
    confidence: float
    bbox: BBox


class OcrAssetMessage(BaseModel):
    type: str  # 'image' | 'figure' | 'table'
    bbox: BBox
    imageUrl: Optional[str] = None
    imageKey: Optional[str] = None
    tableHtml: Optional[str] = None
    source: Optional[str] = None  # 'embedded' | 'layout'


class OcrResultPage(BaseModel):
    page: int
    width: int
    height: int
    lines: List[OcrLine] = Field(default_factory=list)
    images: List[OcrAssetMessage] = Field(default_factory=list)
    tables: List[OcrAssetMessage] = Field(default_factory=list)
    # Ảnh raster đầy đủ của trang (đúng pixel space đã dùng để OCR/tính bbox).
    # Client hiển thị ảnh này thay vì tự render lại PDF để bbox luôn khớp
    # chính xác 1:1, tránh lệch do khác biệt engine render (PyMuPDF vs pdfium).
    pageImageUrl: Optional[str] = None
    pageImageKey: Optional[str] = None


class OcrResultMessage(BaseModel):
    """Message worker publish vào `ocr.results`."""

    jobId: int
    status: str  # 'processing' | 'done' | 'failed'
    pages: Optional[List[OcrResultPage]] = None
    processedPages: Optional[int] = None
    totalPages: Optional[int] = None
    error: Optional[str] = None

    def to_json(self) -> str:
        # exclude_none để không gửi field thừa; backend chỉ đọc field nó cần.
        return self.model_dump_json(exclude_none=True)


# ── Export (searchable PDF) ──────────────────────────────────────────────────
class OcrExportLine(BaseModel):
    text: str
    bbox: BBox


class OcrExportPage(BaseModel):
    page: int
    lines: List[OcrExportLine] = Field(default_factory=list)


class OcrExportMessage(BaseModel):
    """Message backend gửi vào `ocr.export`."""

    model_config = ConfigDict(extra="ignore")

    jobId: int
    format: str = "pdf"
    fileUrl: str
    fileKey: Optional[str] = None
    lang: str = "vi"
    pages: List[OcrExportPage] = Field(default_factory=list)


class OcrExportResultMessage(BaseModel):
    """Message worker publish vào `ocr.export.results`."""

    jobId: int
    format: str = "pdf"
    status: str  # 'done' | 'failed'
    url: Optional[str] = None
    key: Optional[str] = None
    error: Optional[str] = None

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)
