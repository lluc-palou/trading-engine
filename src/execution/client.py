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
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

from config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL

# Default request timeout and signing window in milliseconds
_DEFAULT_TIMEOUT_SECONDS: int = 10
_RECV_WINDOW_MS: int = 5_000

# Shared session for connection pooling across all signed requests
_session: requests.Session = requests.Session()


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
    query_string = urlencode(params)
    timestamp_ms = int(time.time() * 1000)
    signature = _build_signature(timestamp_ms, query_string)
    headers = _auth_headers(timestamp_ms, signature)

    url = f"{BYBIT_BASE_URL}{endpoint}"
    response = _session.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()

    payload = response.json()
    if payload.get("retCode", -1) != 0:
        raise ValueError(
            f"[Bybit GET {endpoint}] retCode={payload.get('retCode')} — {payload.get('retMsg')}"
        )
    return payload


def signed_post(
    endpoint: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> Dict:
    """
    Executes a signed POST request against the Bybit V5 REST API.

    The JSON-serialised request body (compact, no spaces) is used as the signing
    payload. The body is also sent as the request body with Content-Type: application/json.

    Args:
        endpoint: API path relative to BYBIT_BASE_URL, e.g. "/v5/order/create".
        body:     Optional dict that will be JSON-serialised and sent as the request body.
        timeout:  HTTP request timeout in seconds.

    Returns:
        Parsed JSON response body as a dict (the full Bybit envelope).

    Raises:
        requests.HTTPError: On any non-2xx HTTP response.
        ValueError: If the Bybit API returns a non-zero retCode.
    """
    body = body or {}
    body_str = json.dumps(body, separators=(",", ":"))
    timestamp_ms = int(time.time() * 1000)
    signature = _build_signature(timestamp_ms, body_str)
    headers = _auth_headers(timestamp_ms, signature)

    url = f"{BYBIT_BASE_URL}{endpoint}"
    response = _session.post(url, headers=headers, data=body_str, timeout=timeout)
    response.raise_for_status()

    payload = response.json()
    if payload.get("retCode", -1) != 0:
        raise ValueError(
            f"[Bybit POST {endpoint}] retCode={payload.get('retCode')} — {payload.get('retMsg')}"
        )
    return payload
