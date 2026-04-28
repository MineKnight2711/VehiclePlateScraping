from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .csgt_scraper import CsgtTrafficFineScraper, ScraperConfig, ScraperError
from .html_parser import parse_violations_html

BACKEND_DIR = Path(__file__).resolve().parents[1]


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class LookupResult:
    data: list[dict[str, Any]]
    source: str
    message: str
    stale: bool = False


class TrafficFineLookupService:
    def __init__(self) -> None:
        self.cache = TrafficFineCache.from_env()
        self.providers: list[TrafficFineProvider] = []

        if _env_bool("SCRAPER_ENABLE_PHATNGUOI", default=True):
            self.providers.append(PhatNguoiProvider())

        if _env_bool("SCRAPER_ENABLE_HCMC", default=False):
            self.providers.append(HcmcCsgtProvider())

        if _env_bool("SCRAPER_ENABLE_CSGT", default=True):
            self.providers.append(CsgtNationalProvider())

    async def check(
        self,
        license_plate: str,
        vehicle_type: str,
        *,
        force_refresh: bool = False,
    ) -> LookupResult:
        plate = _normalize_plate(license_plate)
        vehicle_code = _vehicle_code(vehicle_type)
        cache_key = f"{plate}:{vehicle_code}"

        cached = None if force_refresh else self.cache.get(cache_key)
        # Only short-circuit when cache contains violation data.
        # Empty cache entries are treated as hints and we still re-check live providers.
        if cached and not cached.stale and cached.data:
            return cached

        errors: list[str] = []
        for provider in self.providers:
            try:
                result = await provider.check(plate, vehicle_code)
                self.cache.set(cache_key, result)
                return result
            except ProviderError as exc:
                errors.append(f"{provider.name}: {exc}")
            except ScraperError as exc:
                errors.append(f"{provider.name}: {exc}")

        if cached:
            return cached

        details = "; ".join(errors) if errors else "No traffic fine providers are enabled"
        raise ProviderError(details)


class TrafficFineProvider:
    name = "provider"

    async def check(self, license_plate: str, vehicle_code: str) -> LookupResult:
        raise NotImplementedError


class CsgtNationalProvider(TrafficFineProvider):
    name = "csgt.vn"

    def __init__(self) -> None:
        self.scraper = CsgtTrafficFineScraper(ScraperConfig.from_env())

    async def check(self, license_plate: str, vehicle_code: str) -> LookupResult:
        data = await self.scraper.check(
            license_plate=license_plate,
            vehicle_type=_csgt_vehicle_type(vehicle_code),
        )
        return LookupResult(
            data=data,
            source=self.name,
            message="Tra cuu thanh cong" if data else "Khong tim thay vi pham",
        )


class PhatNguoiProvider(TrafficFineProvider):
    name = "api.phatnguoi.vn"

    def __init__(self) -> None:
        self.timeout = int(os.getenv("PHATNGUOI_TIMEOUT_SECONDS", "30"))
        self.base_url = os.getenv(
            "PHATNGUOI_API_URL",
            "https://api.phatnguoi.vn/web/tra-cuu/{plate}/{vehicle_code}",
        )

    async def check(self, license_plate: str, vehicle_code: str) -> LookupResult:
        return await asyncio.to_thread(self._check_sync, license_plate, vehicle_code)

    def _check_sync(self, license_plate: str, vehicle_code: str) -> LookupResult:
        url = self.base_url.format(
            plate=quote(license_plate),
            vehicle_code=quote(vehicle_code),
        )
        response = requests.get(
            url,
            timeout=self.timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        html = response.text
        canonical = _canonical_text(html)
        if "[tk]" in html or "co loi say ra" in canonical or "co loi xay ra" in canonical:
            raise ProviderError("upstream returned an internal lookup error")

        data = parse_violations_html(
            html,
            fallback_plate=license_plate,
            fallback_vehicle_type=_display_vehicle_type(vehicle_code),
        )
        for item in data:
            item["source"] = self.name

        return LookupResult(
            data=data,
            source=self.name,
            message="Tra cuu thanh cong" if data else "Khong tim thay vi pham",
        )


class HcmcCsgtProvider(TrafficFineProvider):
    name = "csgt-tphcm"

    def __init__(self) -> None:
        self.timeout = int(os.getenv("HCMC_TIMEOUT_SECONDS", "30"))
        self.portal_url = os.getenv(
            "HCMC_LOOKUP_URL",
            "https://csgt-congan.hochiminhcity.gov.vn/wps/portal/Home/tra-cuu-vi-pham",
        )
        self.captcha_url = os.getenv(
            "HCMC_CAPTCHA_URL",
            "https://csgt-congan.hochiminhcity.gov.vn/wps/VPGT/kaptcha.jpg",
        )
        self.action_url = os.getenv(
            "HCMC_ACTION_URL",
            "https://csgt-congan.hochiminhcity.gov.vn/wps/VPGT/view/getListViPham.do",
        )
        self.captcha_solver_url = os.getenv("HCMC_CAPTCHA_SOLVER_URL", "")

    async def check(self, license_plate: str, vehicle_code: str) -> LookupResult:
        return await asyncio.to_thread(self._check_sync, license_plate)

    def _check_sync(self, license_plate: str) -> LookupResult:
        if not self.captcha_solver_url:
            raise ProviderError("HCMC captcha solver is not configured")

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Referer": self.portal_url,
            }
        )
        session.get(self.portal_url, timeout=self.timeout).raise_for_status()
        captcha = session.get(self.captcha_url, timeout=self.timeout)
        captcha.raise_for_status()

        captcha_text = self._solve_captcha(captcha.content)
        payload = {"BIEN_SO": license_plate, "CAPTCHA": captcha_text}
        response = session.post(self.action_url, data=payload, timeout=self.timeout)
        response.raise_for_status()

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("HCMC response is not JSON") from exc

        status = body.get("status")
        if status == "captcha":
            raise ProviderError("HCMC captcha was rejected")
        if status != "success":
            raise ProviderError(f"HCMC returned status {status}")

        rows = body.get("data") or body.get("rows") or body.get("result") or []
        if not isinstance(rows, list):
            rows = []

        data = [_hcmc_row_to_violation(row) for row in rows if isinstance(row, dict)]
        return LookupResult(
            data=data,
            source=self.name,
            message="Tra cuu thanh cong" if data else "Khong tim thay vi pham",
        )

    def _solve_captcha(self, image_bytes: bytes) -> str:
        response = requests.post(
            self.captcha_solver_url,
            json={"image_base64": base64.b64encode(image_bytes).decode("ascii")},
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        text = body.get("text") or body.get("captcha") or body.get("result")
        if not text:
            raise ProviderError("captcha solver returned no text")
        return str(text).strip()


@dataclass(frozen=True)
class CachedResult:
    data: list[dict[str, Any]]
    source: str
    message: str
    stale: bool


class TrafficFineCache:
    def __init__(self, path: Path, max_age_seconds: int) -> None:
        self.path = path
        self.max_age_seconds = max_age_seconds

    @classmethod
    def from_env(cls) -> "TrafficFineCache":
        configured_path = Path(os.getenv("TRAFFIC_FINE_CACHE_PATH", ".cache/traffic_fines.json"))
        path = configured_path if configured_path.is_absolute() else (BACKEND_DIR / configured_path)
        max_age_hours = float(os.getenv("TRAFFIC_FINE_CACHE_MAX_AGE_HOURS", "12"))
        return cls(path=path, max_age_seconds=int(max_age_hours * 3600))

    def get(self, key: str) -> LookupResult | None:
        store = self._read()
        item = store.get(key)
        if not isinstance(item, dict):
            return None

        created_at = float(item.get("created_at", 0))
        stale = (time.time() - created_at) > self.max_age_seconds
        data = item.get("data") if isinstance(item.get("data"), list) else []
        return LookupResult(
            data=data,
            source=f"{item.get('source', 'cache')}:cache",
            message=item.get("message", "Tra cuu tu cache"),
            stale=stale,
        )

    def set(self, key: str, result: LookupResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        store = self._read()
        store[key] = {
            "created_at": time.time(),
            "data": result.data,
            "source": result.source,
            "message": result.message,
        }
        self.path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def _hcmc_row_to_violation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "license_plate": row.get("BIEN_SO", ""),
        "license_plate_color": row.get("MAU_BIEN", ""),
        "vehicle_type": row.get("LOAI_XE", ""),
        "time": row.get("NGAY_VI_PHAM", ""),
        "location": row.get("TUYEN_DUONG_VI_PHAM", ""),
        "offense": row.get("LOI_VI_PHAM", ""),
        "status": row.get("TRANG_THAI", ""),
        "complainant": row.get("CO_QUAN_XU_LY", ""),
        "place_of_resolutions": [],
        "source": "csgt-tphcm",
    }


def _normalize_plate(value: str) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def _vehicle_code(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"2", "motorbike", "moto", "xe may", "xe máy"}:
        return "2"
    if normalized in {"3", "electricbike", "electric_bike", "xe dap dien", "xe đạp điện"}:
        return "3"
    return "1"


def _csgt_vehicle_type(vehicle_code: str) -> str:
    return {"1": "car", "2": "motorbike", "3": "electricbike"}.get(vehicle_code, "car")


def _display_vehicle_type(vehicle_code: str) -> str:
    return {"1": "O to", "2": "Xe may", "3": "Xe dap dien"}.get(vehicle_code, "")


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _canonical_text(value: str) -> str:
    replacements = {
        "ả": "a",
        "á": "a",
        "à": "a",
        "ã": "a",
        "ạ": "a",
        "ă": "a",
        "ắ": "a",
        "ằ": "a",
        "ẵ": "a",
        "ẳ": "a",
        "ặ": "a",
        "â": "a",
        "ấ": "a",
        "ầ": "a",
        "ẫ": "a",
        "ẩ": "a",
        "ậ": "a",
        "đ": "d",
        "é": "e",
        "è": "e",
        "ẽ": "e",
        "ẻ": "e",
        "ẹ": "e",
        "ê": "e",
        "ế": "e",
        "ề": "e",
        "ễ": "e",
        "ể": "e",
        "ệ": "e",
        "í": "i",
        "ì": "i",
        "ĩ": "i",
        "ỉ": "i",
        "ị": "i",
        "ó": "o",
        "ò": "o",
        "õ": "o",
        "ỏ": "o",
        "ọ": "o",
        "ô": "o",
        "ố": "o",
        "ồ": "o",
        "ỗ": "o",
        "ổ": "o",
        "ộ": "o",
        "ơ": "o",
        "ớ": "o",
        "ờ": "o",
        "ỡ": "o",
        "ở": "o",
        "ợ": "o",
        "ú": "u",
        "ù": "u",
        "ũ": "u",
        "ủ": "u",
        "ụ": "u",
        "ư": "u",
        "ứ": "u",
        "ừ": "u",
        "ữ": "u",
        "ử": "u",
        "ự": "u",
        "ý": "y",
        "ỳ": "y",
        "ỹ": "y",
        "ỷ": "y",
        "ỵ": "y",
    }
    lowered = (value or "").lower()
    return "".join(replacements.get(ch, ch) for ch in lowered)
