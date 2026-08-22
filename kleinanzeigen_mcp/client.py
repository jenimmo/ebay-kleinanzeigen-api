"""Asynchronous client for the canonical persistent ``/jobs/*`` API.

The client is intentionally small: it performs HTTP transport, validates the
stable response envelope and maps failures to sanitised machine-readable
exceptions.  It does not call legacy scraping routes and contains no MCP
transport concerns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any, TypeVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .models import JobAccepted, JobResultsPage, JobStatus


_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_URL_IN_MESSAGE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_SENSITIVE_HEADER_IN_MESSAGE = re.compile(
    r"\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|api-key|idempotency-key)\s*[:=]",
    re.IGNORECASE,
)
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class KleinanzeigenClientError(RuntimeError):
    """Base error with a stable, serialisable and non-sensitive shape."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_seconds: float | int | None = None,
        status_code: int | None = None,
    ) -> None:
        self.code = _normalise_error_code(code, fallback="client_error")
        self.message = _sanitise_message(message, fallback="Client request failed")
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable error fields intended for an MCP tool response."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after_seconds": self.retry_after_seconds,
            "status_code": self.status_code,
        }


class KleinanzeigenAPIError(KleinanzeigenClientError):
    """The jobs API returned a non-success HTTP status."""


class KleinanzeigenTransportError(KleinanzeigenClientError):
    """The jobs API could not be reached or timed out."""


class KleinanzeigenProtocolError(KleinanzeigenClientError):
    """A successful response did not match the documented jobs contract."""


class KleinanzeigenJobClient:
    """Thin async HTTP client restricted to the persistent job endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive number")
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self._timeout = float(timeout)
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def __aenter__(self) -> KleinanzeigenJobClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close only the internally-created HTTP client."""

        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def create_search_job(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> JobAccepted:
        """Create a durable search job."""

        if not isinstance(payload, Mapping):
            raise _invalid_argument("payload must be an object")
        return await self._request_model(
            "POST",
            "/jobs/search",
            JobAccepted,
            json=dict(payload),
            headers=_idempotency_headers(idempotency_key),
        )

    async def create_detail_job(
        self,
        *,
        listing_ids: Sequence[str] | None = None,
        source_search_job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobAccepted:
        """Create details from exactly one source: IDs or a search job."""

        if (listing_ids is None) == (source_search_job_id is None):
            raise _invalid_argument(
                "provide exactly one of listing_ids or source_search_job_id"
            )

        body: dict[str, Any]
        if listing_ids is not None:
            if isinstance(listing_ids, (str, bytes)):
                raise _invalid_argument("listing_ids must be a list of strings")
            values = list(listing_ids)
            if not values or any(
                not isinstance(value, str) or not value for value in values
            ):
                raise _invalid_argument("listing_ids must contain non-empty strings")
            body = {"listing_ids": values}
        else:
            if not isinstance(source_search_job_id, str) or not source_search_job_id:
                raise _invalid_argument(
                    "source_search_job_id must be a non-empty string"
                )
            body = {"source_search_job_id": source_search_job_id}

        return await self._request_model(
            "POST",
            "/jobs/details",
            JobAccepted,
            json=body,
            headers=_idempotency_headers(idempotency_key),
        )

    async def get_job(self, job_id: str) -> JobStatus:
        """Read one persisted job status."""

        return await self._request_model(
            "GET",
            _job_path(job_id),
            JobStatus,
        )

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobResultsPage:
        """Read one native cursor page, with the API's maximum page size."""

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise _invalid_argument("limit must be an integer between 1 and 200")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise _invalid_argument("cursor must be a non-empty string when provided")
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._request_model(
            "GET",
            f"{_job_path(job_id)}/results",
            JobResultsPage,
            params=params,
        )

    async def continue_job(
        self,
        job_id: str,
        continuation_token: str,
        *,
        after_cursor: str | None = None,
    ) -> JobAccepted:
        """Authorise exactly one persisted search continuation tranche."""

        _require_nonempty_token(continuation_token, "continuation_token")
        if after_cursor is not None:
            _require_nonempty_token(after_cursor, "after_cursor")
        body = {"continuation_token": continuation_token}
        if after_cursor is not None:
            body["after_cursor"] = after_cursor
        return await self._request_model(
            "POST",
            f"{_job_path(job_id)}/continue",
            JobAccepted,
            json=body,
        )

    async def resume_job(self, job_id: str, resume_token: str) -> JobAccepted:
        """Retry exactly one persisted needs-attention cycle."""

        _require_nonempty_token(resume_token, "resume_token")
        return await self._request_model(
            "POST",
            f"{_job_path(job_id)}/resume",
            JobAccepted,
            json={"resume_token": resume_token},
        )

    async def cancel_job(self, job_id: str) -> JobAccepted:
        """Cancel a durable job idempotently."""

        return await self._request_model(
            "DELETE",
            _job_path(job_id),
            JobAccepted,
        )

    async def _request_model(
        self,
        method: str,
        path: str,
        model: type[_ResponseModel],
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> _ResponseModel:
        response = await self._request(
            method,
            path,
            json=json,
            params=params,
            headers=headers,
        )
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise KleinanzeigenProtocolError(
                code="invalid_response_json",
                message="The jobs API returned an invalid JSON response",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise KleinanzeigenProtocolError(
                code="invalid_response_shape",
                message="The jobs API returned an invalid response shape",
                status_code=response.status_code,
            )
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise KleinanzeigenProtocolError(
                code="invalid_response_schema",
                message="The jobs API response did not match the expected contract",
                status_code=response.status_code,
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        _assert_jobs_path(path)
        request_headers = {**self._headers, **dict(headers or {})}
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params=params,
                headers=request_headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise KleinanzeigenTransportError(
                code="transport_timeout",
                message="The jobs API request timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise KleinanzeigenTransportError(
                code="transport_error",
                message="The jobs API could not be reached",
                retryable=True,
            ) from exc

        if not 200 <= response.status_code < 300:
            raise _api_error(response)
        return response


def _validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("base_url must be a non-empty HTTP(S) URL")
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("base_url must be an HTTP(S) URL without credentials or query")
    return candidate


def _assert_jobs_path(path: str) -> None:
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/jobs/")
    ):
        raise RuntimeError("client attempted to access a non-jobs endpoint")


def _job_path(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id:
        raise _invalid_argument("job_id must be a non-empty string")
    return f"/jobs/{quote(job_id, safe='')}"


def _require_nonempty_token(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise _invalid_argument(f"{name} must be a non-empty string")


def _idempotency_headers(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, str) or not value:
        raise _invalid_argument("idempotency_key must be a non-empty string")
    return {"Idempotency-Key": value}


def _invalid_argument(message: str) -> KleinanzeigenClientError:
    return KleinanzeigenClientError(code="invalid_argument", message=message)


def _api_error(response: httpx.Response) -> KleinanzeigenAPIError:
    status_code = response.status_code
    default_code, default_message = _default_http_error(status_code)
    code = default_code
    message = default_message
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None

    detail: object = payload
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
    if isinstance(detail, dict):
        raw_code = detail.get("error", detail.get("code"))
        if isinstance(raw_code, str):
            code = _normalise_error_code(raw_code, fallback=default_code)
        raw_message = detail.get("message")
        if isinstance(raw_message, str):
            message = _sanitise_message(raw_message, fallback=default_message)
    elif isinstance(detail, str):
        message = _sanitise_message(detail, fallback=default_message)

    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    return KleinanzeigenAPIError(
        code=code,
        message=message,
        retryable=status_code in _RETRYABLE_STATUS_CODES,
        retry_after_seconds=retry_after,
        status_code=status_code,
    )


def _default_http_error(status_code: int) -> tuple[str, str]:
    known = {
        400: ("invalid_request", "The jobs API rejected the request"),
        401: ("unauthorized", "Authentication with the jobs API failed"),
        403: ("forbidden", "The jobs API denied the request"),
        404: ("job_not_found", "The requested job was not found"),
        409: ("job_conflict", "The requested job action conflicts with its state"),
        413: ("payload_too_large", "The jobs API request was too large"),
        422: ("invalid_argument", "The jobs API rejected an argument"),
        429: ("rate_limited", "The jobs API rate limit was reached"),
        503: ("service_unavailable", "The jobs API is temporarily unavailable"),
        504: ("upstream_timeout", "The jobs API timed out upstream"),
    }
    if status_code in known:
        return known[status_code]
    if status_code >= 500:
        return "server_error", "The jobs API failed to process the request"
    return "api_error", "The jobs API rejected the request"


def _parse_retry_after(value: str | None) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if number.is_integer():
        return int(number)
    return number


def _normalise_error_code(value: str, *, fallback: str) -> str:
    candidate = value.strip()
    if _SAFE_ERROR_CODE.fullmatch(candidate):
        return candidate
    return fallback


def _sanitise_message(value: str, *, fallback: str) -> str:
    lowered = value.lower()
    if (
        "traceback (most recent call last)" in lowered
        or "stack trace:" in lowered
        or _SENSITIVE_HEADER_IN_MESSAGE.search(value)
    ):
        return fallback
    candidate = _CONTROL_CHARACTERS.sub(" ", value)
    candidate = _URL_IN_MESSAGE.sub("[redacted-url]", candidate)
    candidate = " ".join(candidate.split())
    if not candidate:
        return fallback
    return candidate[:500]


__all__ = [
    "KleinanzeigenAPIError",
    "KleinanzeigenClientError",
    "KleinanzeigenJobClient",
    "KleinanzeigenProtocolError",
    "KleinanzeigenTransportError",
]
