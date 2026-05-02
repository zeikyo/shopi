from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 12 * 1024 * 1024
BLUR_VARIANCE_THRESHOLD = 55.0  # TODO: ajuster avec vos photos reelles de cartes.


class CardAnalysisError(Exception):
    """Erreur attendue pendant l'analyse d'une carte."""


class InvalidImageError(CardAnalysisError):
    pass


class BlurryImageError(CardAnalysisError):
    pass


class ContourNotDetectedError(CardAnalysisError):
    pass


@dataclass(slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(slots=True)
class DetectedBox:
    points: np.ndarray
    rect: Rect
    warped_rect: Rect | None = None
    warped_size: tuple[int, int] | None = None


@dataclass(slots=True)
class CenteringResult:
    left_percent: float
    right_percent: float
    top_percent: float
    bottom_percent: float
    left_margin: int
    right_margin: int
    top_margin: int
    bottom_margin: int


@dataclass(slots=True)
class CardAnalysisResult:
    outer_box: DetectedBox
    inner_box: DetectedBox
    centering: CenteringResult
    grade_estimate: str
    sharpness: float
    annotated_image: np.ndarray


async def download_attachment(attachment: Any, directory: Path) -> Path:
    """Telecharge temporairement une piece jointe Discord image."""
    if attachment is None:
        raise InvalidImageError("Aucune image envoyee.")

    filename = getattr(attachment, "filename", "") or ""
    suffix = Path(filename).suffix.lower()
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    size = int(getattr(attachment, "size", 0) or 0)

    if suffix not in SUPPORTED_IMAGE_EXTENSIONS and content_type not in SUPPORTED_CONTENT_TYPES:
        raise InvalidImageError("Format invalide. Envoyez une image JPG, PNG ou WEBP.")
    if size > MAX_IMAGE_BYTES:
        raise InvalidImageError("Image trop lourde. Limite actuelle: 12 Mo.")

    directory.mkdir(parents=True, exist_ok=True)
    output_path = directory / f"pokemon_card_{uuid.uuid4().hex}{suffix or '.jpg'}"
    await attachment.save(output_path)
    return output_path


def detect_card_contour(image: np.ndarray) -> DetectedBox:
    """Detecte le contour exterieur de la carte dans la photo."""
    if image is None or image.size == 0:
        raise InvalidImageError("Image illisible ou vide.")

    height, width = image.shape[:2]
    image_area = height * width
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 45, 140)
    kernel = np.ones((5, 5), np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[float, np.ndarray]] = []

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * 0.08:
            continue

        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            points = approx.reshape(4, 2).astype("float32")
        else:
            rect = cv2.minAreaRect(contour)
            points = cv2.boxPoints(rect).astype("float32")

        ordered = _order_points(points)
        ratio = _box_short_long_ratio(ordered)
        if 0.52 <= ratio <= 0.86:
            candidates.append((area, ordered))

    if not candidates:
        raise ContourNotDetectedError("Contour exterieur de la carte non detecte.")

    _, best_points = max(candidates, key=lambda item: item[0])
    return DetectedBox(points=best_points, rect=_bounding_rect(best_points))


def detect_inner_border(image: np.ndarray, card_box: DetectedBox) -> DetectedBox:
    """Detecte le cadre interieur / zone imprimee dans la carte redressee."""
    warped, matrix = _warp_card(image, card_box.points)
    warped_height, warped_width = warped.shape[:2]
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 35, 120)
    kernel = np.ones((3, 3), np.uint8)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    card_area = warped_width * warped_height
    candidates: list[tuple[float, Rect]] = []

    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        area = width * height
        if area < card_area * 0.35 or area > card_area * 0.93:
            continue
        if x < warped_width * 0.02 or y < warped_height * 0.02:
            continue
        if x + width > warped_width * 0.98 or y + height > warped_height * 0.98:
            continue
        ratio = min(width, height) / max(width, height)
        if 0.50 <= ratio <= 0.90:
            candidates.append((area, Rect(x=x, y=y, width=width, height=height)))

    if candidates:
        _, inner_rect = max(candidates, key=lambda item: item[0])
    else:
        # TODO: remplacer ce fallback par une detection calibree sur un dataset de cartes Pokemon.
        # Il garde la commande exploitable quand le cadre imprime est peu contraste.
        inner_rect = Rect(
            x=int(warped_width * 0.075),
            y=int(warped_height * 0.055),
            width=int(warped_width * 0.85),
            height=int(warped_height * 0.89),
        )

    inner_points_warped = _rect_to_points(inner_rect)
    inverse_matrix = np.linalg.inv(matrix)
    original_points = cv2.perspectiveTransform(
        inner_points_warped.reshape(1, 4, 2).astype("float32"),
        inverse_matrix,
    ).reshape(4, 2)

    return DetectedBox(
        points=original_points,
        rect=_bounding_rect(original_points),
        warped_rect=inner_rect,
        warped_size=(warped_width, warped_height),
    )


def calculate_centering(inner_box: DetectedBox) -> CenteringResult:
    """Calcule les ratios gauche/droite et haut/bas a partir du cadre interieur."""
    if inner_box.warped_rect is None or inner_box.warped_size is None:
        raise CardAnalysisError("Donnees de centrage incompletes.")

    card_width, card_height = inner_box.warped_size
    inner = inner_box.warped_rect
    left_margin = max(inner.x, 0)
    right_margin = max(card_width - (inner.x + inner.width), 0)
    top_margin = max(inner.y, 0)
    bottom_margin = max(card_height - (inner.y + inner.height), 0)

    horizontal_total = left_margin + right_margin
    vertical_total = top_margin + bottom_margin
    if horizontal_total <= 0 or vertical_total <= 0:
        raise CardAnalysisError("Marges impossibles a calculer.")

    return CenteringResult(
        left_percent=left_margin / horizontal_total * 100,
        right_percent=right_margin / horizontal_total * 100,
        top_percent=top_margin / vertical_total * 100,
        bottom_percent=bottom_margin / vertical_total * 100,
        left_margin=left_margin,
        right_margin=right_margin,
        top_margin=top_margin,
        bottom_margin=bottom_margin,
    )


def estimate_grade(centering: CenteringResult) -> str:
    """Estime une note theorique selon une tolerance face 60/40 ou mieux."""
    horizontal_best = max(centering.left_percent, centering.right_percent)
    vertical_best = max(centering.top_percent, centering.bottom_percent)
    if horizontal_best <= 60.0 and vertical_best <= 60.0:
        return "PSA 10 theorique"
    return "PSA 9 ou moins theorique"


def generate_annotated_image(
    image: np.ndarray,
    outer_box: DetectedBox,
    inner_box: DetectedBox,
    centering: CenteringResult,
    grade_estimate: str,
    sharpness: float,
) -> np.ndarray:
    """Genere l'image annotee retournee dans Discord."""
    annotated = image.copy()
    cv2.polylines(annotated, [_int_points(outer_box.points)], True, (0, 0, 255), 4)
    cv2.polylines(annotated, [_int_points(inner_box.points)], True, (255, 0, 0), 4)

    line_1 = (
        f"G/D: {centering.left_percent:.1f}% / {centering.right_percent:.1f}%  "
        f"H/B: {centering.top_percent:.1f}% / {centering.bottom_percent:.1f}%"
    )
    line_2 = f"{grade_estimate} - estimation uniquement, non garantie"
    line_3 = f"Nettete: {sharpness:.1f}"
    return _draw_footer(annotated, [line_1, line_2, line_3])


def analyze_card_image(image_path: Path) -> CardAnalysisResult:
    image = cv2.imread(str(image_path))
    if image is None:
        raise InvalidImageError("Format invalide ou image impossible a lire.")

    sharpness = _measure_sharpness(image)
    if sharpness < BLUR_VARIANCE_THRESHOLD:
        raise BlurryImageError(
            f"Image trop floue pour une analyse fiable (nettete {sharpness:.1f})."
        )

    outer_box = detect_card_contour(image)
    inner_box = detect_inner_border(image, outer_box)
    centering = calculate_centering(inner_box)
    grade_estimate = estimate_grade(centering)
    annotated_image = generate_annotated_image(
        image,
        outer_box,
        inner_box,
        centering,
        grade_estimate,
        sharpness,
    )
    return CardAnalysisResult(
        outer_box=outer_box,
        inner_box=inner_box,
        centering=centering,
        grade_estimate=grade_estimate,
        sharpness=sharpness,
        annotated_image=annotated_image,
    )


def _measure_sharpness(image: np.ndarray) -> float:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _warp_card(image: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ordered = _order_points(points)
    top_width = _distance(ordered[0], ordered[1])
    bottom_width = _distance(ordered[3], ordered[2])
    left_height = _distance(ordered[0], ordered[3])
    right_height = _distance(ordered[1], ordered[2])
    target_width = max(int(top_width), int(bottom_width), 1)
    target_height = max(int(left_height), int(right_height), 1)

    destination = np.array(
        [
            [0, 0],
            [target_width - 1, 0],
            [target_width - 1, target_height - 1],
            [0, target_height - 1],
        ],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(ordered, destination)
    warped = cv2.warpPerspective(image, matrix, (target_width, target_height))
    return warped, matrix


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    diffs = np.diff(points, axis=1)
    rect[0] = points[np.argmin(sums)]
    rect[2] = points[np.argmax(sums)]
    rect[1] = points[np.argmin(diffs)]
    rect[3] = points[np.argmax(diffs)]
    return rect


def _box_short_long_ratio(points: np.ndarray) -> float:
    widths = [_distance(points[0], points[1]), _distance(points[3], points[2])]
    heights = [_distance(points[0], points[3]), _distance(points[1], points[2])]
    width = sum(widths) / 2
    height = sum(heights) / 2
    return min(width, height) / max(width, height)


def _distance(first: np.ndarray, second: np.ndarray) -> float:
    return math.dist((float(first[0]), float(first[1])), (float(second[0]), float(second[1])))


def _bounding_rect(points: np.ndarray) -> Rect:
    x, y, width, height = cv2.boundingRect(points.astype("float32"))
    return Rect(x=int(x), y=int(y), width=int(width), height=int(height))


def _rect_to_points(rect: Rect) -> np.ndarray:
    return np.array(
        [
            [rect.x, rect.y],
            [rect.x + rect.width, rect.y],
            [rect.x + rect.width, rect.y + rect.height],
            [rect.x, rect.y + rect.height],
        ],
        dtype="float32",
    )


def _int_points(points: np.ndarray) -> np.ndarray:
    return np.round(points).astype(np.int32)


def _draw_footer(image: np.ndarray, lines: list[str]) -> np.ndarray:
    height, width = image.shape[:2]
    footer_height = 118
    output = cv2.copyMakeBorder(image, 0, footer_height, 0, 0, cv2.BORDER_CONSTANT, value=(20, 20, 20))
    y = height + 34
    font_scale = max(0.55, min(width / 1250, 0.9))
    for line in lines:
        cv2.putText(
            output,
            line,
            (18, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        y += 34
    return output
