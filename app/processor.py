"""Xử lý một job OCR: tải file → render trang → OCR (+ tách ảnh) → publish kết quả.

Phát message theo tiến độ:
- 'processing' kèm pages đã xong (upsert idempotent ở backend) + processedPages.
- 'done' khi hoàn tất (backend tự set processedPages = totalPages).
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from typing import Callable, List
from urllib.parse import urlparse

from PIL import Image

from .config import config
from .image_extractor import ImageExtractor
from .ocr_engine import OcrEngine
from .pdf_renderer import DocumentRenderer, RenderedPage
from .s3_client import S3Client
from .schemas import OcrJobMessage, OcrResultMessage, OcrResultPage

logger = logging.getLogger("ocr.processor")

PublishFn = Callable[[OcrResultMessage], None]


def _suffix_from_url(file_url: str, file_key: str | None) -> str:
    for candidate in (file_url, file_key or ""):
        path = urlparse(candidate).path if "://" in candidate else candidate
        _, ext = os.path.splitext(path)
        if ext:
            return ext
    return ".bin"


class JobProcessor:
    def __init__(self, engine: OcrEngine, s3: S3Client) -> None:
        self._engine = engine
        self._s3 = s3
        self._extractor = ImageExtractor(engine, s3)

    def process(self, job: OcrJobMessage, publish: PublishFn) -> None:
        suffix = _suffix_from_url(job.fileUrl, job.fileKey)
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            self._s3.download(job.fileUrl, job.fileKey, tmp_path)
            self._run(job, tmp_path, publish)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _run(self, job: OcrJobMessage, path: str, publish: PublishFn) -> None:
        mode = (job.mode or "layout").lower()
        extract = bool(job.extractImages) and config.extract_images

        with DocumentRenderer(path, config.render_dpi) as renderer:
            pages = renderer.select_pages(job.pages)
            total = len(pages)
            if total == 0:
                publish(
                    OcrResultMessage(
                        jobId=job.jobId,
                        status="failed",
                        error="Tài liệu không có trang hợp lệ để OCR.",
                    )
                )
                return

            publish(
                OcrResultMessage(
                    jobId=job.jobId,
                    status="processing",
                    processedPages=0,
                    totalPages=total,
                )
            )

            buffer: List[OcrResultPage] = []
            processed = 0
            for page_no in pages:
                rp = renderer.render_page(page_no)
                lines = self._engine.ocr_page(rp.image, job.lang)

                images, tables = [], []
                if extract:
                    images, tables = self._extractor.extract(
                        rp, renderer.doc, mode=mode
                    )

                page_image_url, page_image_key = self._upload_page_image(rp)

                buffer.append(
                    OcrResultPage(
                        page=page_no,
                        width=rp.width,
                        height=rp.height,
                        lines=lines,
                        images=images,
                        tables=tables,
                        pageImageUrl=page_image_url,
                        pageImageKey=page_image_key,
                    )
                )
                processed += 1

                if len(buffer) >= max(1, config.progress_every):
                    publish(
                        OcrResultMessage(
                            jobId=job.jobId,
                            status="processing",
                            pages=buffer,
                            processedPages=processed,
                            totalPages=total,
                        )
                    )
                    buffer = []

            publish(
                OcrResultMessage(
                    jobId=job.jobId,
                    status="done",
                    pages=buffer or None,
                    processedPages=total,
                    totalPages=total,
                )
            )
            logger.info("Job #%s hoàn tất: %s trang.", job.jobId, total)

    def _upload_page_image(self, rp: RenderedPage) -> tuple[str | None, str | None]:
        """Upload ảnh raster đầy đủ của trang (dùng để OCR) lên S3.

        Client dùng ảnh này để hiển thị preview thay vì tự render lại PDF,
        đảm bảo bbox khớp tuyệt đối với vị trí hiển thị.
        """
        try:
            buf = io.BytesIO()
            Image.fromarray(rp.image).save(buf, format="JPEG", quality=85)
            return self._s3.upload_bytes(
                buf.getvalue(), ext="jpg", content_type="image/jpeg"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Upload ảnh trang %s lỗi (bỏ qua, client sẽ tự render): %s",
                rp.page_number,
                exc,
            )
            return None, None
