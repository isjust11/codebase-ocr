"""Chỉnh ảnh chụp tài liệu: phối cảnh (nghiêng) + deskew + làm phẳng cong nhẹ.

Áp dụng cho ảnh chụp camera (trang giấy hình thang, hơi cong). PDF render
sẵn mặc định bỏ qua trừ khi bật OCR_PREPROCESS_RECTIFY_PDF.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .config import config

logger = logging.getLogger("ocr.rectify")


def rectify_document(rgb: np.ndarray) -> tuple[np.ndarray, bool]:
    """Trả về (ảnh đã chỉnh, đã_thay_đổi)."""
    if rgb is None or rgb.size == 0:
        return rgb, False

    out = rgb
    changed = False

    if config.rectify_perspective:
        warped, ok = _perspective_correct(out)
        if ok:
            out = warped
            changed = True

    if config.rectify_deskew:
        deskewed, ok = _deskew(out)
        if ok:
            out = deskewed
            changed = True

    if config.rectify_dewarp:
        dewarped, ok = _dewarp_curvature(out)
        if ok:
            out = dewarped
            changed = True

    if changed:
        logger.info(
            "Rectify: %dx%d -> %dx%d",
            rgb.shape[1],
            rgb.shape[0],
            out.shape[1],
            out.shape[0],
        )

    return out, changed


def _perspective_correct(rgb: np.ndarray) -> tuple[np.ndarray, bool]:
    h, w = rgb.shape[:2]
    if h < 80 or w < 80:
        return rgb, False

    scale = min(1.0, 1200.0 / max(h, w))
    small = rgb
    if scale < 1.0:
        small = cv2.resize(
            rgb,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    corners = _find_document_quad(small)
    if corners is None:
        return rgb, False

    # Scale corners back to full resolution.
    corners = (corners / scale).astype(np.float32)
    if _quad_area_ratio(corners, w, h) < config.rectify_min_area_ratio:
        return rgb, False

    dst_w, dst_h = _target_size(corners)
    if dst_w < 80 or dst_h < 80:
        return rgb, False

    dst = np.array(
        [
            [0, 0],
            [dst_w - 1, 0],
            [dst_w - 1, dst_h - 1],
            [0, dst_h - 1],
        ],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(corners, dst)
    warped = cv2.warpPerspective(
        rgb,
        matrix,
        (dst_w, dst_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return warped, True


def _find_document_quad(rgb: np.ndarray) -> np.ndarray | None:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(gray, 50, 150)
    edged = cv2.dilate(edged, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(
        edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:12]
    img_area = gray.shape[0] * gray.shape[1]

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < img_area * config.rectify_min_area_ratio:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return _order_quad(approx.reshape(4, 2))

    # Fallback: min-area rectangle của contour lớn nhất.
    if contours:
        rect = cv2.minAreaRect(contours[0])
        box = cv2.boxPoints(rect)
        return _order_quad(box)

    return None


def _order_quad(pts: np.ndarray) -> np.ndarray:
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(diff)]
    bl = pts[np.argmax(diff)]
    return np.array([tl, tr, br, bl], dtype=np.float32)


def _quad_area_ratio(corners: np.ndarray, w: int, h: int) -> float:
    area = cv2.contourArea(corners.reshape(-1, 1, 2))
    return float(area) / float(w * h) if w * h > 0 else 0.0


def _target_size(corners: np.ndarray) -> tuple[int, int]:
    tl, tr, br, bl = corners
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    return max(width, 1), max(height, 1)


def _deskew(rgb: np.ndarray) -> tuple[np.ndarray, bool]:
    angle = _estimate_skew_angle(rgb)
    if angle is None or abs(angle) < config.rectify_deskew_min_angle:
        return rgb, False

    logger.info("Deskew: xoay %.2f°", angle)
    return _rotate_rgb_expanded(rgb, angle), True


def _estimate_skew_angle(rgb: np.ndarray) -> float | None:
    """Ước lượng góc nghiêng: projection profile → Hough → minAreaRect text."""
    angle = _skew_projection_profile(rgb)
    if angle is not None:
        return angle
    angle = _skew_hough(rgb)
    if angle is not None:
        return angle
    return _skew_text_min_area(rgb)


def _prepare_binary(gray: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        35,
        11,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)


def _skew_projection_profile(rgb: np.ndarray) -> float | None:
    """Tìm góc làm biến thiên hàng projection lớn nhất — ổn với trang sách nhiều chữ."""
    h, w = rgb.shape[:2]
    scale = min(1.0, 900.0 / max(h, w))
    small = rgb
    if scale < 1.0:
        small = cv2.resize(
            rgb,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    binary = _prepare_binary(gray)
    sh, sw = binary.shape
    max_a = config.rectify_deskew_max_angle

    def score(angle: float) -> float:
        center = (sw / 2.0, sh / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            binary,
            matrix,
            (sw, sh),
            flags=cv2.INTER_LINEAR,
            borderValue=0,
        )
        proj = rotated.sum(axis=1).astype(np.float64)
        if proj.size < 2:
            return 0.0
        return float(np.var(proj) + 0.35 * np.var(np.diff(proj)))

    best_angle = 0.0
    baseline = score(0.0)
    best_score = baseline

    for step in (1.0, 0.1):
        if step == 1.0:
            angles = np.arange(-max_a, max_a + 0.01, step)
        else:
            angles = np.arange(best_angle - 2.0, best_angle + 2.01, step)
        for angle in angles:
            s = score(float(angle))
            if s > best_score:
                best_score = s
                best_angle = float(angle)

    if abs(best_angle) < config.rectify_deskew_min_angle:
        return None
    # Chỉ xoay khi score cải thiện rõ rệt so với ảnh gốc (tránh xoay nhầm).
    if best_score < baseline * 1.06:
        return None
    return best_angle


def _skew_text_min_area(rgb: np.ndarray) -> float | None:
    """Fallback: góc min-area rect của pixel chữ."""
    h, w = rgb.shape[:2]
    scale = min(1.0, 1000.0 / max(h, w))
    small = rgb
    if scale < 1.0:
        small = cv2.resize(
            rgb,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    binary = _prepare_binary(gray)
    coords = np.column_stack(np.where(binary > 0))
    if len(coords) < 400:
        return None

    rect = cv2.minAreaRect(coords.astype(np.float32))
    angle = float(rect[-1])
    if angle < -45.0:
        angle = 90.0 + angle
    elif angle > 45.0:
        angle = angle - 90.0

    if abs(angle) < config.rectify_deskew_min_angle:
        return None
    return angle


def _skew_hough(rgb: np.ndarray) -> float | None:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 50, 150)
    min_len = max(24, gray.shape[1] // 16)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=60,
        minLineLength=min_len,
        maxLineGap=24,
    )
    if lines is None:
        return None

    angles: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0]:
        if abs(x2 - x1) < 10:
            continue
        ang = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if abs(ang) <= config.rectify_deskew_max_angle:
            angles.append(ang)

    if len(angles) < 3:
        return None

    angle = float(np.median(angles))
    if abs(angle) < config.rectify_deskew_min_angle:
        return None
    return angle


def _rotate_rgb_expanded(rgb: np.ndarray, angle: float) -> np.ndarray:
    """Xoay ảnh, mở rộng canvas để không cắt góc khi nghiêng nhẹ."""
    h, w = rgb.shape[:2]
    center = (w / 2.0, h / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_w = int(h * sin + w * cos)
    new_h = int(h * cos + w * sin)
    matrix[0, 2] += (new_w - w) / 2.0
    matrix[1, 2] += (new_h - h) / 2.0
    return cv2.warpAffine(
        rgb,
        matrix,
        (new_w, new_h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _dewarp_curvature(rgb: np.ndarray) -> tuple[np.ndarray, bool]:
    """Làm phẳng cong nhẹ bằng cách thẳng hoá mép trên/dưới vùng giấy."""
    h, w = rgb.shape[:2]
    if h < 120 or w < 120:
        return rgb, False

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Giả định giấy sáng hơn nền; nếu ngược thì đảo.
    if np.mean(mask) < 127:
        mask = cv2.bitwise_not(mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    step = max(4, w // 100)
    top_x: list[float] = []
    top_y: list[float] = []
    bot_x: list[float] = []
    bot_y: list[float] = []

    for x in range(0, w, step):
        col = mask[:, x]
        white = np.where(col > 127)[0]
        if len(white) < h * 0.25:
            continue
        top_x.append(float(x))
        top_y.append(float(white[0]))
        bot_x.append(float(x))
        bot_y.append(float(white[-1]))

    if len(top_x) < 10:
        return rgb, False

    xs = np.arange(w, dtype=np.float32)
    top_curve = np.interp(xs, np.array(top_x), np.array(top_y))
    bot_curve = np.interp(xs, np.array(bot_x), np.array(bot_y))

    top_curve = cv2.GaussianBlur(top_curve.reshape(1, -1), (0, 0), 7).flatten()
    bot_curve = cv2.GaussianBlur(bot_curve.reshape(1, -1), (0, 0), 7).flatten()

    linear_top = np.polyval(np.polyfit(xs, top_curve, 1), xs)
    linear_bot = np.polyval(np.polyfit(xs, bot_curve, 1), xs)

    dev = max(
        float(np.max(np.abs(top_curve - linear_top))),
        float(np.max(np.abs(bot_curve - linear_bot))),
    )
    if dev < config.rectify_dewarp_min_deviation:
        return rgb, False

    strength = config.rectify_dewarp_strength
    top_off = (top_curve - linear_top) * strength
    bot_off = (bot_curve - linear_bot) * strength

    map_x = np.tile(np.arange(w, dtype=np.float32), (h, 1))
    y_grid = np.arange(h, dtype=np.float32).reshape(-1, 1)
    top_grid = top_curve.reshape(1, -1)
    bot_grid = bot_curve.reshape(1, -1)
    height = np.maximum(bot_grid - top_grid, 1.0)
    t = np.clip((y_grid - top_grid) / height, 0.0, 1.0)
    offset = (1.0 - t) * top_off + t * bot_off
    inside = (y_grid >= top_grid) & (y_grid <= bot_grid)
    map_y = np.where(inside, y_grid - offset, y_grid).astype(np.float32)

    dewarped = cv2.remap(
        rgb,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return dewarped, True
