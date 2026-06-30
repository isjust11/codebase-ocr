"""Entry point worker: kết nối RabbitMQ, consume `ocr.jobs`, publish `ocr.results`.

Thiết kế chịu lỗi:
- Tự reconnect khi rớt kết nối (vòng lặp + back-off).
- Khai báo queue `durable=True` KHỚP với backend (không thêm args dead-letter vào
  `ocr.jobs`/`ocr.results` để tránh PRECONDITION_FAILED). Message lỗi được publish
  sang `ocr.dlx` (queue riêng của worker) để debug, sau đó ack để không loop.
- Mỗi job: retry tối đa `OCR_MAX_RETRIES` lần trong tiến trình trước khi báo failed.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from typing import Optional

import pika
from pika.exceptions import AMQPConnectionError

import os
import tempfile

from .config import config
from .health import start_health_server
from .ocr_engine import OcrEngine
from .pdf_export import PdfExporter
from .processor import JobProcessor
from .s3_client import S3Client
from .schemas import (
    OcrExportMessage,
    OcrExportResultMessage,
    OcrJobMessage,
    OcrResultMessage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ocr.worker")


class Worker:
    def __init__(self) -> None:
        self._engine = OcrEngine.instance()
        self._s3 = S3Client()
        self._processor = JobProcessor(self._engine, self._s3)
        self._exporter = PdfExporter()
        self._connection: Optional[pika.BlockingConnection] = None
        self._channel = None
        self._stopping = False

    # ── Vòng đời ──────────────────────────────────────────────────────────
    def start(self) -> None:
        start_health_server(config.health_port, self._engine.is_ready)
        logger.info("Đang warm-up OCR engine...")
        self._engine.warm_up()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while not self._stopping:
            try:
                self._connect_and_consume()
            except AMQPConnectionError as exc:
                logger.warning("Mất kết nối RabbitMQ: %s. Thử lại sau 5s...", exc)
                time.sleep(5)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Lỗi không mong đợi: %s. Thử lại sau 5s...", exc)
                time.sleep(5)

        logger.info("Worker dừng.")

    def _handle_signal(self, *_args) -> None:
        logger.info("Nhận tín hiệu dừng, đang đóng kết nối...")
        self._stopping = True
        try:
            if self._channel and self._channel.is_open:
                self._channel.stop_consuming()
        except Exception:  # noqa: BLE001
            pass

    # ── Kết nối + consume ─────────────────────────────────────────────────
    def _connect_and_consume(self) -> None:
        params = pika.URLParameters(config.rabbitmq_url)
        params.heartbeat = config.heartbeat
        params.blocked_connection_timeout = 300

        logger.info("Kết nối RabbitMQ...")
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()

        # Khớp khai báo với backend: durable=True, không thêm arguments.
        self._channel.queue_declare(queue=config.jobs_queue, durable=True)
        self._channel.queue_declare(queue=config.results_queue, durable=True)
        self._channel.queue_declare(queue=config.export_queue, durable=True)
        self._channel.queue_declare(
            queue=config.export_results_queue, durable=True
        )
        # Queue debug riêng của worker (không bind vào jobs).
        try:
            self._channel.queue_declare(queue=config.dlx_queue, durable=True)
        except Exception:  # noqa: BLE001
            pass

        self._channel.basic_qos(prefetch_count=config.prefetch)
        self._channel.basic_consume(
            queue=config.jobs_queue,
            on_message_callback=self._on_message,
            auto_ack=False,
        )
        self._channel.basic_consume(
            queue=config.export_queue,
            on_message_callback=self._on_export_message,
            auto_ack=False,
        )
        logger.info(
            "Sẵn sàng. Consume '%s' + '%s', publish '%s' + '%s'.",
            config.jobs_queue,
            config.export_queue,
            config.results_queue,
            config.export_results_queue,
        )
        self._channel.start_consuming()

    # ── Xử lý message ─────────────────────────────────────────────────────
    def _on_message(self, channel, method, _properties, body: bytes) -> None:
        job: Optional[OcrJobMessage] = None
        try:
            payload = json.loads(body.decode("utf-8"))
            job = OcrJobMessage(**payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Message job hỏng, bỏ qua: %s", exc)
            self._to_dlx(body, reason=f"invalid_message: {exc}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info("Nhận job #%s (lang=%s, mode=%s)", job.jobId, job.lang, job.mode)
        last_error: Optional[str] = None
        for attempt in range(1, config.max_retries + 2):  # 1 lần đầu + retries
            try:
                self._processor.process(job, self._publish_result)
                channel.basic_ack(delivery_tag=method.delivery_tag)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning(
                    "Job #%s lỗi (lần %s/%s): %s",
                    job.jobId,
                    attempt,
                    config.max_retries + 1,
                    exc,
                )
                time.sleep(min(2 * attempt, 10))

        # Hết retry → báo failed + đẩy DLX + ack (tránh loop vô hạn).
        logger.error("Job #%s thất bại sau khi retry: %s", job.jobId, last_error)
        self._publish_result(
            OcrResultMessage(
                jobId=job.jobId,
                status="failed",
                error=last_error or "OCR thất bại.",
            )
        )
        self._to_dlx(body, reason=last_error or "failed")
        channel.basic_ack(delivery_tag=method.delivery_tag)

    # ── Xử lý export (searchable PDF) ─────────────────────────────────────
    def _on_export_message(self, channel, method, _properties, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
            msg = OcrExportMessage(**payload)
        except Exception as exc:  # noqa: BLE001
            logger.error("Message export hỏng, bỏ qua: %s", exc)
            self._to_dlx(body, reason=f"invalid_export: {exc}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        logger.info("Nhận export #%s (%s)", msg.jobId, msg.format)
        tmp = tempfile.NamedTemporaryFile(suffix=".src", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            self._s3.download(msg.fileUrl, msg.fileKey, tmp_path)
            pdf_bytes = self._exporter.build(tmp_path, msg.pages)
            url, key = self._s3.upload_bytes(
                pdf_bytes,
                ext="pdf",
                content_type="application/pdf",
                prefix=config.s3_export_prefix,
            )
            self._publish_export_result(
                OcrExportResultMessage(
                    jobId=msg.jobId, format="pdf", status="done", url=url, key=key
                )
            )
            logger.info("Export #%s xong: %s", msg.jobId, url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Export #%s lỗi: %s", msg.jobId, exc)
            self._publish_export_result(
                OcrExportResultMessage(
                    jobId=msg.jobId, format="pdf", status="failed", error=str(exc)
                )
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            channel.basic_ack(delivery_tag=method.delivery_tag)

    def _publish_export_result(self, message: OcrExportResultMessage) -> None:
        if not self._channel or not self._channel.is_open:
            raise RuntimeError("Channel đóng, không publish được export result.")
        self._channel.basic_publish(
            exchange="",
            routing_key=config.export_results_queue,
            body=message.to_json().encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
            ),
        )

    # ── Publish ───────────────────────────────────────────────────────────
    def _publish_result(self, message: OcrResultMessage) -> None:
        if not self._channel or not self._channel.is_open:
            raise RuntimeError("Channel đóng, không publish được kết quả.")
        self._channel.basic_publish(
            exchange="",
            routing_key=config.results_queue,
            body=message.to_json().encode("utf-8"),
            properties=pika.BasicProperties(
                delivery_mode=2,  # persistent
                content_type="application/json",
            ),
        )

    def _to_dlx(self, body: bytes, reason: str) -> None:
        try:
            if self._channel and self._channel.is_open:
                self._channel.basic_publish(
                    exchange="",
                    routing_key=config.dlx_queue,
                    body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=2,
                        headers={"x-error": reason[:512]},
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Không đẩy được message vào DLX: %s", exc)


def main() -> None:
    Worker().start()


if __name__ == "__main__":
    main()
