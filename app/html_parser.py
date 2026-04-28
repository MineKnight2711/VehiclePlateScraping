from __future__ import annotations

import re
import unicodedata
from typing import Any

from bs4 import BeautifulSoup, Tag


_LABEL_TO_KEY = {
    "bien kiem soat": "license_plate",
    "bien so xe": "license_plate",
    "bien so": "license_plate",
    "mau bien": "license_plate_color",
    "loai phuong tien": "vehicle_type",
    "thoi gian vi pham": "time",
    "thoi gian": "time",
    "dia diem vi pham": "location",
    "dia diem": "location",
    "hanh vi vi pham": "offense",
    "hanh vi": "offense",
    "trang thai": "status",
    "don vi phat hien vi pham": "complainant",
    "don vi phat hien": "complainant",
    "noi giai quyet vu viec": "place_of_resolutions",
    "noi giai quyet": "place_of_resolutions",
}


def parse_violations_html(
    html: str,
    *,
    fallback_plate: str = "",
    fallback_vehicle_type: str = "",
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html or "", "html.parser")
    text = _clean_text(soup.get_text(" "))

    if not text or _looks_like_empty_result(text):
        return []

    cards = soup.select(".violation-card")
    if not cards:
        cards = soup.select(".result")
    if not cards:
        cards = [soup]

    violations: list[dict[str, Any]] = []
    for card in cards:
        pairs = _extract_label_value_pairs(card)
        item = _build_violation_item(
            pairs,
            fallback_plate=fallback_plate,
            fallback_vehicle_type=fallback_vehicle_type,
        )
        if _has_violation_content(item):
            violations.append(item)

    return violations


def _extract_label_value_pairs(card: Tag | BeautifulSoup) -> dict[str, str]:
    pairs: dict[str, str] = {}

    for label_el in card.select(".label"):
        label = _clean_text(label_el.get_text(" "))
        value = ""
        container = label_el.parent
        if isinstance(container, Tag):
            value_el = container.select_one(".value")
            if value_el:
                value = _clean_text(value_el.get_text(" "))
            else:
                raw = _clean_text(container.get_text(" "))
                value = _strip_label_from_value(raw, label)

        key = _key_for_label(label)
        if key and value:
            pairs[key] = value

    for status_el in card.select(".status, .status-pending, .status-resolved"):
        status = _clean_text(status_el.get_text(" "))
        if status:
            pairs.setdefault("status", status)

    for row in card.find_all(["p", "div", "li", "tr"]):
        text = _clean_text(row.get_text(" "))
        if ":" not in text:
            continue
        label, value = text.split(":", 1)
        key = _key_for_label(label)
        if key and value.strip():
            pairs.setdefault(key, _clean_text(value))

    return pairs


def _build_violation_item(
    pairs: dict[str, str],
    *,
    fallback_plate: str,
    fallback_vehicle_type: str,
) -> dict[str, Any]:
    place = pairs.get("place_of_resolutions", "")

    return {
        "license_plate": pairs.get("license_plate", fallback_plate),
        "license_plate_color": pairs.get("license_plate_color", ""),
        "vehicle_type": pairs.get("vehicle_type", fallback_vehicle_type),
        "time": pairs.get("time", ""),
        "location": pairs.get("location", ""),
        "offense": pairs.get("offense", ""),
        "status": pairs.get("status", ""),
        "complainant": pairs.get("complainant", ""),
        "place_of_resolutions": _parse_resolution_places(place),
        "source": "csgt_scraper",
    }


def _parse_resolution_places(raw: str) -> list[dict[str, str]]:
    if not raw:
        return []

    chunks = [
        _clean_text(chunk)
        for chunk in re.split(r"(?:\n|;|\s{2,}|\d+\.\s*)", raw)
        if _clean_text(chunk)
    ]
    if not chunks:
        chunks = [raw]

    return [
        {
            "name": chunk,
            "address": "",
            "contact_phone_number": "",
        }
        for chunk in chunks
    ]


def _looks_like_empty_result(text: str) -> bool:
    canonical = _canonicalize(text)
    empty_markers = (
        "khong tim thay ket qua",
        "khong co ket qua",
        "khong co vi pham",
        "khong tim thay vi pham",
    )
    return any(marker in canonical for marker in empty_markers)


def _has_violation_content(item: dict[str, Any]) -> bool:
    keys = ("time", "location", "offense", "status", "complainant")
    return any(str(item.get(key, "")).strip() for key in keys)


def _key_for_label(label: str) -> str | None:
    normalized = _canonicalize(label).rstrip(":")
    return _LABEL_TO_KEY.get(normalized)


def _strip_label_from_value(raw: str, label: str) -> str:
    value = raw
    if value.startswith(label):
        value = value[len(label) :]
    return value.lstrip(":").strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _canonicalize(value: str) -> str:
    ascii_value = unicodedata.normalize("NFD", value)
    ascii_value = "".join(ch for ch in ascii_value if unicodedata.category(ch) != "Mn")
    ascii_value = ascii_value.replace("đ", "d").replace("Đ", "D")
    ascii_value = re.sub(r"[^a-zA-Z0-9\s]", " ", ascii_value)
    return _clean_text(ascii_value).lower()
