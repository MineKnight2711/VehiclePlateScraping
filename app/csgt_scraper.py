from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .html_parser import parse_violations_html


class ScraperError(RuntimeError):
    """Raised when the upstream CSGT lookup cannot be completed."""


@dataclass(frozen=True)
class ScraperConfig:
    lookup_url: str
    action_path: str
    timeout_seconds: int
    rate_limit_seconds: float
    use_playwright: bool
    headless: bool
    recaptcha_token: str

    @classmethod
    def from_env(cls) -> "ScraperConfig":
        return cls(
            lookup_url=os.getenv("CSGT_LOOKUP_URL", "https://www.csgt.vn/tra-cuu-phat-nguoi"),
            action_path=os.getenv("CSGT_ACTION_PATH", "/tra-cuu-vi-pham-qua-hinh-anh"),
            timeout_seconds=int(os.getenv("SCRAPER_TIMEOUT_SECONDS", "45")),
            rate_limit_seconds=float(os.getenv("SCRAPER_RATE_LIMIT_SECONDS", "2")),
            use_playwright=_env_bool("SCRAPER_USE_PLAYWRIGHT", default=True),
            headless=_env_bool("SCRAPER_HEADLESS", default=True),
            recaptcha_token=os.getenv("CSGT_RECAPTCHA_TOKEN", ""),
        )


class CsgtTrafficFineScraper:
    def __init__(self, config: ScraperConfig) -> None:
        self.config = config
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def check(self, license_plate: str, vehicle_type: str) -> list[dict[str, Any]]:
        plate = _normalize_plate(license_plate)
        csgt_vehicle_type = _to_csgt_vehicle_type(vehicle_type)

        if not plate:
            raise ScraperError("License plate is required")

        await self._throttle()

        if self.config.use_playwright:
            return await self._check_with_playwright(plate, csgt_vehicle_type)

        return await asyncio.to_thread(self._check_with_requests, plate, csgt_vehicle_type)

    async def _throttle(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            wait_for = self.config.rate_limit_seconds - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at = time.monotonic()

    async def _check_with_playwright(
        self,
        plate: str,
        vehicle_type: str,
    ) -> list[dict[str, Any]]:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ScraperError("Playwright is not installed. Run: pip install -r backend/requirements.txt") from exc

        timeout_ms = self.config.timeout_seconds * 1000
        action_url_part = self.config.action_path

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=self.config.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = await browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/123.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 900},
                    locale="vi-VN",
                )

                await page.goto(self.config.lookup_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_selector("#violationsForm", timeout=timeout_ms)
                await page.select_option("#vehicle_type", vehicle_type)
                await page.fill("#plate_number", plate)

                await page.wait_for_function(
                    "() => window.grecaptcha && typeof window.grecaptcha.execute === 'function'",
                    timeout=timeout_ms,
                )

                async with page.expect_response(
                    lambda response: action_url_part in response.url
                    and response.request.method.upper() == "POST",
                    timeout=timeout_ms,
                ) as response_info:
                    await page.click("#submitBtn")

                response = await response_info.value
                if response.status == 422:
                    raise ScraperError("CSGT rejected the lookup captcha/session")
                if response.status == 429:
                    raise ScraperError("CSGT daily lookup limit exceeded")
                if response.status >= 400:
                    raise ScraperError(f"CSGT returned HTTP {response.status}")

                payload = await _response_payload(response)
                result_html = await _result_html_from_page(page, timeout_ms)
                if result_html:
                    payload = {"resultHtml": result_html}
                await browser.close()
        except PlaywrightTimeoutError as exc:
            raise ScraperError("Timed out while waiting for the CSGT lookup response") from exc
        except Exception as exc:
            raise ScraperError(f"CSGT lookup failed: {exc}") from exc

        return _parse_csgt_payload(
            payload,
            fallback_plate=plate,
            fallback_vehicle_type=_display_vehicle_type(vehicle_type),
        )

    def _check_with_requests(self, plate: str, vehicle_type: str) -> list[dict[str, Any]]:
        if not self.config.recaptcha_token:
            raise ScraperError(
                "Requests mode needs CSGT_RECAPTCHA_TOKEN. "
                "Use SCRAPER_USE_PLAYWRIGHT=true for the normal page flow."
            )

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.config.lookup_url,
            }
        )

        page = session.get(self.config.lookup_url, timeout=self.config.timeout_seconds)
        page.raise_for_status()

        soup = BeautifulSoup(page.text, "html.parser")
        form = soup.select_one("#violationsForm")
        token = ""
        action = urljoin(self.config.lookup_url, self.config.action_path)
        if form:
            token_el = form.select_one('input[name="_token"]')
            token = token_el.get("value", "") if token_el else ""
            action = urljoin(self.config.lookup_url, form.get("action") or self.config.action_path)

        response = session.post(
            action,
            data={
                "_token": token,
                "g-recaptcha-response": self.config.recaptcha_token,
                "vehicle_type": vehicle_type,
                "plate_number": plate,
            },
            timeout=self.config.timeout_seconds,
        )
        if response.status_code == 422:
            raise ScraperError("CSGT rejected the lookup captcha/session")
        if response.status_code == 429:
            raise ScraperError("CSGT daily lookup limit exceeded")
        response.raise_for_status()

        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"resultHtml": response.text}

        return _parse_csgt_payload(
            payload,
            fallback_plate=plate,
            fallback_vehicle_type=_display_vehicle_type(vehicle_type),
        )


async def _response_payload(response: Any) -> Any:
    try:
        return await response.json()
    except Exception:
        try:
            return {"resultHtml": await response.text()}
        except Exception:
            return {}


async def _result_html_from_page(page: Any, timeout_ms: int) -> str:
    try:
        await page.wait_for_function(
            """
            () => {
              const el = document.querySelector('#result');
              if (!el) return false;
              const text = (el.innerText || '').trim();
              return text.length > 0 && !text.includes('Đang tra cứu');
            }
            """,
            timeout=timeout_ms,
        )
        return await page.locator("#result").inner_html()
    except Exception:
        return ""


def _parse_csgt_payload(
    payload: Any,
    *,
    fallback_plate: str,
    fallback_vehicle_type: str,
) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [dict(item) for item in payload["data"] if isinstance(item, dict)]

        html = payload.get("resultHtml") or payload.get("html") or payload.get("result") or ""
        message = payload.get("message") or payload.get("error") or ""
        if not html and message:
            html = str(message)
    else:
        html = str(payload or "")

    return parse_violations_html(
        html,
        fallback_plate=fallback_plate,
        fallback_vehicle_type=fallback_vehicle_type,
    )


def _normalize_plate(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _to_csgt_vehicle_type(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"2", "motorbike", "moto", "xe may", "xe máy"}:
        return "motorbike"
    if normalized in {"3", "electricbike", "electric_bike", "xe dap dien", "xe đạp điện"}:
        return "electricbike"
    return "car"


def _display_vehicle_type(value: str) -> str:
    return {
        "car": "O to",
        "motorbike": "Xe may",
        "electricbike": "Xe dap dien",
    }.get(value, value)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
