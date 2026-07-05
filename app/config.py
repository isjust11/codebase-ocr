"""Cấu hình worker đọc từ biến môi trường.

Các tên biến phải khớp với backend NestJS (xem `codebase-admin/.env`):
- RABBITMQ_URL, OCR_JOBS_QUEUE, OCR_RESULTS_QUEUE, OCR_PREFETCH
- S3_ENDPOINT, S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


def _load_dotenv() -> None:
    """Nạp file .env cạnh project root (không ghi đè biến đã có trong shell)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Config:
    # ── RabbitMQ ──────────────────────────────────────────────────────────
    rabbitmq_url: str = field(
        default_factory=lambda: os.getenv(
            "RABBITMQ_URL", "amqp://guest:guest@localhost:5672"
        )
    )
    jobs_queue: str = field(
        default_factory=lambda: os.getenv("OCR_JOBS_QUEUE", "ocr.jobs")
    )
    results_queue: str = field(
        default_factory=lambda: os.getenv("OCR_RESULTS_QUEUE", "ocr.results")
    )
    dlx_queue: str = field(
        default_factory=lambda: os.getenv("OCR_DLX_QUEUE", "ocr.dlx")
    )
    export_queue: str = field(
        default_factory=lambda: os.getenv("OCR_EXPORT_QUEUE", "ocr.export")
    )
    export_results_queue: str = field(
        default_factory=lambda: os.getenv(
            "OCR_EXPORT_RESULTS_QUEUE", "ocr.export.results"
        )
    )
    prefetch: int = field(default_factory=lambda: _get_int("OCR_PREFETCH", 2))
    # heartbeat lớn để OCR chạy lâu không bị rớt kết nối; 0 = tắt heartbeat.
    heartbeat: int = field(default_factory=lambda: _get_int("OCR_RMQ_HEARTBEAT", 600))

    # ── OCR engine ────────────────────────────────────────────────────────
    default_lang: str = field(
        default_factory=lambda: os.getenv("OCR_DEFAULT_LANG", "vi")
    )
    render_dpi: int = field(default_factory=lambda: _get_int("OCR_RENDER_DPI", 240))
    use_gpu: bool = field(default_factory=lambda: _get_bool("OCR_USE_GPU", False))

    # ── Tách ảnh / figure / table (Phase 5b) ──────────────────────────────
    extract_images: bool = field(
        default_factory=lambda: _get_bool("OCR_EXTRACT_IMAGES", True)
    )
    enable_table: bool = field(
        default_factory=lambda: _get_bool("OCR_ENABLE_TABLE", True)
    )
    # Bỏ qua ảnh quá nhỏ (icon/đường kẻ): tỉ lệ diện tích so với trang.
    min_asset_area_ratio: float = field(
        default_factory=lambda: _get_float("OCR_MIN_ASSET_AREA_RATIO", 0.01)
    )
    # Bỏ ảnh/figure phủ gần hết trang (ảnh chụp bị nhận nhầm thành figure).
    max_asset_area_ratio: float = field(
        default_factory=lambda: _get_float("OCR_MAX_ASSET_AREA_RATIO", 0.85)
    )
    # Ngưỡng IoU coi 2 bbox là trùng (khử ảnh embedded vs figure layout).
    dedup_iou: float = field(
        default_factory=lambda: _get_float("OCR_DEDUP_IOU", 0.6)
    )

    # ── Tiền xử lý ảnh trước OCR ───────────────────────────────────────────
    preprocess_enabled: bool = field(
        default_factory=lambda: _get_bool("OCR_PREPROCESS_ENABLED", True)
    )
    # Upscale nếu cạnh ngắn < ngưỡng này (px).
    preprocess_min_short_edge: int = field(
        default_factory=lambda: _get_int("OCR_PREPROCESS_MIN_SHORT_EDGE", 1600)
    )
    preprocess_max_scale: float = field(
        default_factory=lambda: _get_float("OCR_PREPROCESS_MAX_SCALE", 2.5)
    )
    # Laplacian variance dưới ngưỡng → coi là mờ, chạy enhance.
    preprocess_blur_threshold: float = field(
        default_factory=lambda: _get_float("OCR_PREPROCESS_BLUR_THRESHOLD", 120.0)
    )
    preprocess_clahe_clip: float = field(
        default_factory=lambda: _get_float("OCR_PREPROCESS_CLAHE_CLIP", 2.0)
    )
    preprocess_denoise_strength: int = field(
        default_factory=lambda: _get_int("OCR_PREPROCESS_DENOISE_STRENGTH", 4)
    )
    # Cho phép upscale PDF (ảnh nhúng fitz cần nhân bbox thêm coord_scale).
    preprocess_upscale_pdf: bool = field(
        default_factory=lambda: _get_bool("OCR_PREPROCESS_UPSCALE_PDF", False)
    )

    # ── Chỉnh nghiêng / cong giấy (ảnh chụp camera) ─────────────────────────
    rectify_enabled: bool = field(
        default_factory=lambda: _get_bool("OCR_RECTIFY_ENABLED", True)
    )
    rectify_perspective: bool = field(
        default_factory=lambda: _get_bool("OCR_RECTIFY_PERSPECTIVE", True)
    )
    rectify_deskew: bool = field(
        default_factory=lambda: _get_bool("OCR_RECTIFY_DESKEW", True)
    )
    rectify_dewarp: bool = field(
        default_factory=lambda: _get_bool("OCR_RECTIFY_DEWARP", True)
    )
    rectify_min_area_ratio: float = field(
        default_factory=lambda: _get_float("OCR_RECTIFY_MIN_AREA_RATIO", 0.15)
    )
    rectify_deskew_min_angle: float = field(
        default_factory=lambda: _get_float("OCR_RECTIFY_DESKEW_MIN_ANGLE", 0.4)
    )
    rectify_deskew_max_angle: float = field(
        default_factory=lambda: _get_float("OCR_RECTIFY_DESKEW_MAX_ANGLE", 18.0)
    )
    rectify_dewarp_min_deviation: float = field(
        default_factory=lambda: _get_float("OCR_RECTIFY_DEWARP_MIN_DEVIATION", 6.0)
    )
    rectify_dewarp_strength: float = field(
        default_factory=lambda: _get_float("OCR_RECTIFY_DEWARP_STRENGTH", 0.85)
    )
    preprocess_rectify_pdf: bool = field(
        default_factory=lambda: _get_bool("OCR_PREPROCESS_RECTIFY_PDF", False)
    )

    # ── Xử lý lỗi ─────────────────────────────────────────────────────────
    max_retries: int = field(default_factory=lambda: _get_int("OCR_MAX_RETRIES", 2))
    # Gửi tiến độ "processing" sau mỗi N trang.
    progress_every: int = field(
        default_factory=lambda: _get_int("OCR_PROGRESS_EVERY", 1)
    )

    # ── S3 (tái dùng cấu hình của backend) ────────────────────────────────
    s3_endpoint: str = field(default_factory=lambda: os.getenv("S3_ENDPOINT", ""))
    s3_region: str = field(default_factory=lambda: os.getenv("S3_REGION", "us-east-1"))
    s3_access_key: str = field(
        default_factory=lambda: os.getenv("S3_ACCESS_KEY_ID", "")
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.getenv("S3_SECRET_ACCESS_KEY", "")
    )
    s3_bucket: str = field(default_factory=lambda: os.getenv("S3_BUCKET_NAME", ""))
    # Base URL public để dựng link ảnh đã tách. Mặc định path-style: endpoint/bucket/key.
    s3_public_base: str = field(
        default_factory=lambda: os.getenv("S3_PUBLIC_BASE_URL", "")
    )
    s3_prefix: str = field(
        default_factory=lambda: os.getenv("OCR_S3_PREFIX", "ocr/assets")
    )
    s3_export_prefix: str = field(
        default_factory=lambda: os.getenv("OCR_S3_EXPORT_PREFIX", "ocr/export")
    )

    # ── Export searchable PDF ─────────────────────────────────────────────
    # Font Unicode để nhúng lớp text ẩn (cần phủ tiếng Việt). Mặc định DejaVu
    # cài qua apt `fonts-dejavu-core` trong Docker; thiếu file thì fallback 'helv'.
    pdf_font_path: str = field(
        default_factory=lambda: os.getenv(
            "OCR_PDF_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
    )

    # ── Health check ──────────────────────────────────────────────────────
    health_port: int = field(default_factory=lambda: _get_int("OCR_HEALTH_PORT", 8080))

    def paddle_langs(self) -> List[str]:
        """Trả về danh sách lang ưu tiên cho PaddleOCR."""
        return ["vi", "en"]


config = Config()
