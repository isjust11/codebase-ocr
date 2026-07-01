# codebase-ocr — PaddleOCR Worker

Worker Python xử lý OCR bất đồng bộ cho hệ thống (Phase 5 + 5b của
`codebase-admin/docs/OCR_BACKEND_PLAN.md`).

- Consume job từ RabbitMQ queue `ocr.jobs` (do NestJS backend publish).
- Render PDF/ảnh → OCR (PaddleOCR, vi/en) → (tuỳ chọn) tách figure/table
  (PP-Structure + ảnh nhúng PyMuPDF) → upload ảnh lên S3.
- Publish kết quả (text + bbox + assets) vào queue `ocr.results`.

## Kiến trúc

> Chi tiết luồng dữ liệu end-to-end: [docs/DATA_FLOW.md](docs/DATA_FLOW.md)

```
NestJS OcrModule --ocr.jobs--> RabbitMQ --consume--> [worker.py]
                                                        | render (pdf_renderer)
                                                        | OCR    (ocr_engine: PaddleOCR)
                                                        | extract(image_extractor: PP-Structure + PyMuPDF)
                                                        | upload (s3_client)
   <--ocr.results-- RabbitMQ <--publish---------------/
```

## Cấu trúc

| File | Vai trò |
|---|---|
| `app/config.py` | Đọc cấu hình từ env (RMQ, S3, OCR, layout) |
| `app/schemas.py` | Pydantic models khớp contract `ocr-queue.interface.ts` |
| `app/s3_client.py` | Tải file gốc (HTTP/boto3) + upload ảnh tách |
| `app/pdf_renderer.py` | PyMuPDF render trang PDF / đọc ảnh |
| `app/ocr_engine.py` | PaddleOCR singleton + warm-up + PP-Structure |
| `app/image_extractor.py` | Phase 5b: tách ảnh embedded + figure/table layout, dedup |
| `app/processor.py` | Pipeline 1 job: download → render → OCR → publish |
| `app/pdf_export.py` | Phase 6: dựng searchable PDF (ảnh trang + lớp text ẩn) |
| `app/worker.py` | Vòng lặp RabbitMQ: consume `ocr.jobs` + `ocr.export`, publish, reconnect, retry, DLX |
| `app/health.py` | HTTP `/health` cho Docker healthcheck |

## Chạy local (không Docker)

```bash
cd codebase-ocr
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # điền S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY
# RabbitMQ phải đang chạy (xem codebase-admin/docker-compose.yml)
set -a && source .env && set +a
python -m app.worker
```

> Lần chạy đầu PaddleOCR sẽ tải model (vi/en + layout/table) về máy nên hơi lâu.

## Chạy bằng Docker Compose (khuyến nghị)

Service đã được khai báo trong `codebase-admin/docker-compose.yml`:

```bash
cd ../codebase-admin
docker compose up -d            # rabbitmq + codebase-ocr
docker compose logs -f codebase-ocr
```

Scale nhiều worker:

```bash
docker compose up -d --scale codebase-ocr=3
```

## Hợp đồng message

**Job (`ocr.jobs`)** — backend → worker:
```json
{ "jobId": 1, "fileUrl": "https://.../file.pdf", "fileKey": "ocr/abc.pdf",
  "lang": "vi", "pages": [1,2], "mode": "layout", "extractImages": true }
```

**Result (`ocr.results`)** — worker → backend:
```json
{ "jobId": 1, "status": "processing|done|failed",
  "processedPages": 1, "totalPages": 2,
  "pages": [{ "page": 1, "width": 1654, "height": 2339,
    "lines": [{ "text": "...", "confidence": 0.98, "bbox": [[x,y],...] }],
    "images": [{ "type": "image|figure", "bbox": [[x,y],...], "imageUrl": "...", "imageKey": "...", "source": "embedded|layout" }],
    "tables": [{ "type": "table", "bbox": [[x,y],...], "imageUrl": "...", "tableHtml": "<table>...", "source": "layout" }]
  }],
  "error": null }
```

## Export searchable PDF (Phase 6)

Worker còn consume queue `ocr.export` để dựng **searchable PDF** (ảnh gốc + lớp
text ẩn theo bbox) rồi publish `ocr.export.results`.

**Export job (`ocr.export`)** — backend → worker:
```json
{ "jobId": 1, "format": "pdf", "fileUrl": "https://.../file.pdf",
  "fileKey": "ocr/abc.pdf", "lang": "vi",
  "pages": [{ "page": 1, "lines": [{ "text": "...", "bbox": [[x,y],...] }] }] }
```

**Export result (`ocr.export.results`)** — worker → backend:
```json
{ "jobId": 1, "format": "pdf", "status": "done|failed", "url": "...", "key": "...", "error": null }
```

> Lớp text ẩn dùng font Unicode (`OCR_PDF_FONT`, mặc định DejaVu trong Docker) để
> phủ tiếng Việt; thiếu font sẽ fallback Latin và bỏ qua dòng không encode được.

## Biến môi trường chính

Xem `.env.example`. Quan trọng: `RABBITMQ_URL`, `OCR_JOBS_QUEUE`, `OCR_RESULTS_QUEUE`,
`OCR_EXPORT_QUEUE`, `OCR_EXPORT_RESULTS_QUEUE`, `S3_*`, `OCR_RENDER_DPI`,
`OCR_EXTRACT_IMAGES`, `OCR_PDF_FONT`, `OCR_USE_GPU`.
