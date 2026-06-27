"""
Bybit V5 signed HTTP client — low-level request primitives for the trading engine.

All authenticated endpoints require HMAC-SHA256 signing. The sign string is:
    timestamp + api_key + recv_window + (query_string for GET | json_body for POST)

Both signed_get() and signed_post() raise ValueError on non-zero Bybit retCode
and requests.HTTPError on non-2xx HTTP status, so callers only need to handle
the business-level ValueError to detect API-layer failures.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

import requests

from config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL

logger = logging.getLogger(__name__)

# Default request timeout and signing window in milliseconds
_DEFAULT_TIMEOUT_SECONDS: int = 10
_RECV_WINDOW_MS: int = 5_000

# Shared session for connection pooling across all signed requests
_session: requests.Session = requests.Session()

# Bybit retCode for "too many visits" — transient rate-limit hit, safe to retry
_RATE_LIMIT_RET_CODE: int = 10006

# Retry policy for transient failures (rate limits, brief network/server hiccups)
_MAX_ATTEMPTS: int = 3
_RETRY_BACKOFF_SECONDS: tuple = (2, 5)


def _is_transient_http_error(error: requests.HTTPError) -> bool:
    """Returns True if the HTTP error is a rate-limit or server-side hiccup worth retrying."""
    status = error.response.status_code if error.response is not None else None
    return status == 429 or (status is not None and 500 <= status < 600)


def _request_with_retry(
    send: Callable[[], requests.Response],
    description: str,
    retry_network_errors: bool = True,
) -> Dict:
    """
    Sends a request and retries on transient Bybit/network failures.

    Always retries on Bybit retCode 10006 (rate limit) and HTTP 429/5xx, since in
    those cases the server explicitly rejected the request before acting on it.
    Connection/timeout errors are only retried when retry_network_errors is True —
    for a non-idempotent call like order creation, the outcome of a timed-out
    request is unknown, so retrying could place a duplicate order.

    Args:
        send:                  Zero-arg callable that performs the HTTP request.
        description:           Short label for log messages, e.g. "POST /v5/order/create".
        retry_network_errors:  Whether to retry on connection/timeout errors. Set False
                                for non-idempotent endpoints (order create/close).

    Returns:
        Parsed JSON response body as a dict.

    Raises:
        requests.HTTPError: On a persistent non-2xx HTTP response.
        ValueError: On a persistent non-zero Bybit retCode.
    """
    last_error: Exception = RuntimeError("unreachable")

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = send()
            response.raise_for_status()
            payload = response.json()

            ret_code = payload.get("retCode", -1)
            if ret_code != 0:
                if ret_code == _RATE_LIMIT_RET_CODE and attempt < _MAX_ATTEMPTS:
                    wait_seconds = _RETRY_BACKOFF_SECONDS[attempt - 1]
                    logger.warning(
                        f"[{description}] Bybit rate limit hit (retCode=10006). "
                        f"Retrying in {wait_seconds}s (attempt {attempt}/{_MAX_ATTEMPTS})."
                    )
                    time.sleep(wait_seconds)
                    continue
                raise ValueError(
                    f"[Bybit {description}] retCode={ret_code} — {payload.get('retMsg')}"
                )

            return payload

        except requests.HTTPError as http_error:
            last_error = http_error
            if _is_transient_http_error(http_error) and attempt < _MAX_ATTEMPTS:
                wait_seconds = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    f"[{description}] Transient HTTP error: {http_error}. "
                    f"Retrying in {wait_seconds}s (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                time.sleep(wait_seconds)
                continue
            raise

        except (requests.ConnectionError, requests.Timeout) as network_error:
            last_error = network_error
            if retry_network_errors and attempt < _MAX_ATTEMPTS:
                wait_seconds = _RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.warning(
                    f"[{description}] Network error: {network_error}. "
                    f"Retrying in {wait_seconds}s (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                time.sleep(wait_seconds)
                continue
            raise

    raise last_error


def _build_signature(timestamp_ms: int, payload: str) -> str:
    """
    Builds the HMAC-SHA256 signature for a Bybit V5 authenticated request.

    The sign string concatenates timestamp, API key, recv_window, and the
    payload (query string for GET, JSON body for POST) in that exact order.

    Args:
        timestamp_ms: Current Unix timestamp in milliseconds.
        payload:      Query string (GET) or serialised JSON body (POST).

    Returns:
        Lowercase hex-encoded HMAC-SHA256 digest.
    """
    sign_str = f"{timestamp_ms}{BYBIT_API_KEY}{_RECV_WINDOW_MS}{payload}"
    return hmac.new(
        BYBIT_API_SECRET.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_headers(timestamp_ms: int, signature: str) -> Dict[str, str]:
    """
    Builds the four Bybit authentication headers required for signed requests.

    Args:
        timestamp_ms: Unix timestamp in milliseconds used to generate the signature.
        signature:    HMAC-SHA256 hex digest from _build_signature().

    Returns:
        Dict of HTTP headers containing API key, signature, sign type, timestamp,
        and recv_window.
    """
    return {
        "X-BAPI-API-KEY":      BYBIT_API_KEY,
        "X-BAPI-SIGN":         signature,
        "X-BAPI-SIGN-TYPE":    "2",
        "X-BAPI-TIMESTAMP":    str(timestamp_ms),
        "X-BAPI-RECV-WINDOW":  str(_RECV_WINDOW_MS),
        "Content-Type":        "application/json",
    }


def signed_get(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> Dict:
    """
    Executes a signed GET request against the Bybit V5 REST API.

    The query string is used as the signing payload. Parameters are URL-encoded
    in the order provided; Bybit does not require alphabetical sorting.

    Args:
        endpoint: API path relative to BYBIT_BASE_URL, e.g. "/v5/account/wallet-balance".
        params:   Optional dict of query parameters.
        timeout:  HTTP request timeout in seconds.

    Returns:
        Parsed JSON response body as a dict (the full Bybit envelope).

    Raises:
        requests.HTTPError: On any non-2xx HTTP response.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    params = params or {}
    url = f"{BYBIT_BASE_URL}{endpoint}"

    def send() -> requests.Response:
        timestamp_ms = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = _build_signature(timestamp_ms, query_string)
        headers = _auth_headers(timestamp_ms, signature)
        return _session.get(url, headers=headers, params=params, timeout=timeout)

    return _request_with_retry(send, description=f"GET {endpoint}")


def signed_post(
    endpoint: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    retry_network_errors: bool = False,
) -> Dict:
    """
    Executes a signed POST request against the Bybit V5 REST API.

    The JSON-serialised request body (compact, no spaces) is used as the signing
    payload. The body is also sent as the request body with Content-Type: application/json.

    Args:
        endpoint:              API path relative to BYBIT_BASE_URL, e.g. "/v5/order/create".
        body:                  Optional dict that will be JSON-serialised and sent as the
                                request body.
        timeout:                HTTP request timeout in seconds.
        retry_network_errors:  Whether to retry on connection/timeout errors, where the
                                request's effect on Bybit is unknown. Defaults to False
                                since most POSTs here place or cancel orders — retrying a
                                timed-out order-create could place a duplicate. Bybit
                                retCode 10006 (rate limit) is always retried regardless,
                                since that confirms the request was rejected, not executed.

    Returns:
        Parsed JSON response body as a dict (the full Bybit envelope).

    Raises:
        requests.HTTPError: On any non-2xx HTTP response.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    body = body or {}
    url = f"{BYBIT_BASE_URL}{endpoint}"

    def send() -> requests.Response:
        body_str = json.dumps(body, separators=(",", ":"))
        timestamp_ms = int(time.time() * 1000)
        signature = _build_signature(timestamp_ms, body_str)
        headers = _auth_headers(timestamp_ms, signature)
        return _session.post(url, headers=headers, data=body_str, timeout=timeout)

    return _request_with_retry(
        send, description=f"POST {endpoint}", retry_network_errors=retry_network_errors
    )
