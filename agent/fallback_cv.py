#!/usr/bin/env python3
"""fallback_cv.py — аварийный CV-детектор пожара (HSV/RGB-пороги).

Используется когда VLM недоступна (timeout, HTTP 5xx, invalid JSON).
Возвращает структуру совместимую с _parse_vlm_response.
"""
from __future__ import annotations

from pathlib import Path

_RED_R_THRESHOLD = 180
_RED_G_MAX = 100
_RED_B_MAX = 100
_CENTER_ZONE_RATIO = 0.4
_MIN_RED_RATIO = 0.01


def _is_red_pixel(r: int, g: int, b: int) -> bool:
    return r > _RED_R_THRESHOLD and g < _RED_G_MAX and b < _RED_B_MAX


def detect_fire(image_path: str) -> tuple[bool, float]:
    """Анализ изображения на наличие красных/оранжевых пикселей (огонь).

    Возвращает (fire_detected: bool, confidence: float).
    """
    try:
        from PIL import Image
    except ImportError:
        return False, 0.0

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception:
        return False, 0.0

    w, h = img.size
    pixels = img.load()

    cx1 = int(w * (0.5 - _CENTER_ZONE_RATIO / 2))
    cx2 = int(w * (0.5 + _CENTER_ZONE_RATIO / 2))
    cy1 = int(h * (0.5 - _CENTER_ZONE_RATIO / 2))
    cy2 = int(h * (0.5 + _CENTER_ZONE_RATIO / 2))

    red_count = 0
    total_center = 0

    for x in range(cx1, cx2):
        for y in range(cy1, cy2):
            r, g, b = pixels[x, y]
            total_center += 1
            if _is_red_pixel(r, g, b):
                red_count += 1

    if total_center == 0:
        return False, 0.0

    ratio = red_count / total_center
    detected = ratio > _MIN_RED_RATIO
    confidence = round(min(ratio * 50, 0.6), 4) if detected else 0.0
    return detected, confidence


def fallback_vlm_result(photo_path: str, drone_cell: list[int]) -> dict:
    """Эмуляция ответа VLM через CV-детектор.

    Возвращает результат для _parse_vlm_response.
    """
    fire_detected, confidence = detect_fire(photo_path)
    if fire_detected:
        return {
            "fire": True,
            "count": 1,
            "confidence": max(0.5, confidence),
            "direction": "center",
            "summary": "Detected via Fallback CV Detector",
        }
    return {
        "fire": False,
        "count": 0,
        "confidence": 0.5,
        "direction": "none",
        "summary": "No fire — Fallback CV",
    }