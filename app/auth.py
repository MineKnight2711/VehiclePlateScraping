from __future__ import annotations

import hmac
import os
from functools import lru_cache

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class AuthConfig:
    def __init__(self) -> None:
        self.enabled = _env_bool("API_AUTH_ENABLED", default=True)
        self.tokens = _read_tokens()

        if self.enabled and not self.tokens:
            raise RuntimeError(
                "API authorization is enabled but no token is configured. "
                "Set API_AUTH_TOKEN or API_AUTH_TOKENS."
            )


@lru_cache
def get_auth_config() -> AuthConfig:
    return AuthConfig()


def verify_api_authorization(
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    api_key: str | None = Security(_api_key_header),
) -> None:
    config = get_auth_config()
    if not config.enabled:
        return

    candidates: list[str] = []
    if bearer and bearer.scheme.lower() == "bearer" and bearer.credentials:
        candidates.append(bearer.credentials.strip())
    if api_key:
        candidates.append(api_key.strip())

    for candidate in candidates:
        if _matches_known_token(candidate, config.tokens):
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _matches_known_token(candidate: str, known_tokens: list[str]) -> bool:
    for token in known_tokens:
        if hmac.compare_digest(candidate, token):
            return True
    return False


def _read_tokens() -> list[str]:
    values: list[str] = []

    single = os.getenv("API_AUTH_TOKEN", "").strip()
    if single:
        values.append(single)

    multiple = os.getenv("API_AUTH_TOKENS", "")
    if multiple.strip():
        values.extend(token.strip() for token in multiple.split(",") if token.strip())

    # Preserve order while deduplicating.
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
