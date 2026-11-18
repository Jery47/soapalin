"""Minimal streaming REST client for Zimbra (stdlib only).

Zimbra's REST endpoints are used for mailbox export/import.  We deliberately
avoid a hard dependency on :mod:`requests` so the application can run inside a
bare Python 3 virtualenv on a Zimbra server.
"""

import http.client
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)


class RestError(Exception):
    """Raised for REST transport or HTTP errors."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


REST_RETRIES = 2
REST_RETRY_DELAY = 3.0
RETRYABLE_HTTP_STATUS = {502, 503, 504}


def _retryable_error(exc: BaseException) -> bool:
    if isinstance(exc, RestError):
        return exc.status in RETRYABLE_HTTP_STATUS
    return isinstance(
        exc, (urllib.error.URLError, OSError, http.client.HTTPException)
    )


ProgressCallback = Callable[[int, Optional[int]], None]


def build_ssl_context(verify_ssl: bool, ca_bundle: str = "") -> ssl.SSLContext:
    """Return an SSL context honoring the per-connection settings."""
    if verify_ssl:
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def quote_account(account: str) -> str:
    """URL-quote an account name without escaping the ``@`` sign."""
    return urllib.parse.quote(account, safe="@")


class RestClient:
    """Small wrapper around :mod:`urllib` with TLS and auth token support."""

    def __init__(
        self,
        host: str,
        port: int = 443,
        verify_ssl: bool = False,
        ca_bundle: str = "",
        timeout: int = 300,
    ):
        self.host = host
        self.port = int(port or 443)
        self.timeout = int(timeout or 300)
        self.context = build_ssl_context(verify_ssl, ca_bundle)

    # ------------------------------------------------------------------
    def base_url(self, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return "https://%s:%d%s" % (self.host, self.port, path)

    def home_url(self, account: str) -> str:
        return self.base_url("/home/%s/" % quote_account(account))

    def _open(
        self,
        url: str,
        method: str = "GET",
        data=None,
        headers: Optional[Dict[str, str]] = None,
        auth_token: str = "",
    ) -> http.client.HTTPResponse:
        logger.debug("REST %s %s", method, url)
        if auth_token:
            sep = "&" if "?" in url else "?"
            url = "%s%sauth=qp&zauthtoken=%s" % (
                url,
                sep,
                urllib.parse.quote(auth_token, safe=""),
            )
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", "soaplun/0.1")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            return urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            )
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(4096).decode("utf-8", "replace")
            except Exception:
                pass
            raise RestError(
                "HTTP %s on %s: %s" % (exc.code, url, body or exc.reason),
                status=exc.code,
                body=body,
            ) from exc
        except urllib.error.URLError as exc:
            raise RestError("Cannot reach %s: %s" % (url, exc.reason)) from exc
        except OSError as exc:
            raise RestError("Cannot reach %s: %s" % (url, exc)) from exc

    # ------------------------------------------------------------------
    def get_bytes(
        self,
        path: str,
        auth_token: str = "",
        limit: int = 0,
    ) -> bytes:
        response = self._open(
            self.base_url(path), method="GET", auth_token=auth_token
        )
        try:
            return response.read(limit) if limit else response.read()
        finally:
            response.close()

    def get_text(self, path: str, auth_token: str = "") -> str:
        return self.get_bytes(path, auth_token=auth_token).decode("utf-8", "replace")

    def get_json(self, path: str, auth_token: str = ""):
        import json

        return json.loads(self.get_text(path, auth_token=auth_token))

    # ------------------------------------------------------------------
    def download(
        self,
        url: str,
        destination: Union[Path, str],
        auth_token: str = "",
        progress: Optional[ProgressCallback] = None,
        chunk_size: int = 1024 * 1024,
    ) -> int:
        """Stream ``url`` to ``destination`` and return the number of bytes."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        last_error: Optional[BaseException] = None
        for attempt in range(REST_RETRIES + 1):
            try:
                response = self._open(url, method="GET", auth_token=auth_token)
                total = 0
                try:
                    length = response.headers.get("Content-Length")
                    expected = int(length) if length and length.isdigit() else None
                    with part.open("wb") as handle:
                        while True:
                            chunk = response.read(chunk_size)
                            if not chunk:
                                break
                            handle.write(chunk)
                            total += len(chunk)
                            if progress:
                                progress(total, expected)
                finally:
                    response.close()
                os.replace(part, destination)
                return total
            except Exception as exc:  # noqa: BLE001 - retry transient transport errors
                last_error = exc
                if attempt >= REST_RETRIES or not _retryable_error(exc):
                    try:
                        part.unlink()
                    except OSError:
                        pass
                    raise
                logger.warning(
                    "REST download retry %d/%d after %s",
                    attempt + 1,
                    REST_RETRIES,
                    exc,
                )
                time.sleep(REST_RETRY_DELAY)
        raise last_error  # pragma: no cover

    def _upload_once(
        self,
        url: str,
        source: Union[Path, str],
        auth_token: str = "",
        progress: Optional[ProgressCallback] = None,
        chunk_size: int = 1024 * 1024,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        """Stream ``source`` to ``url``.  Returns ``(status, body)``."""
        source = Path(source)
        size = source.stat().st_size
        logger.debug("REST POST %s (%d bytes)", url, size)
        headers = {"Content-Type": "application/x-gzip"}
        headers.update(extra_headers or {})

        class _ProgressReader:
            def __init__(self, handle):
                self.handle = handle
                self.sent = 0

            def read(self, amount=-1):
                chunk = self.handle.read(amount if amount and amount > 0 else chunk_size)
                self.sent += len(chunk)
                if progress:
                    progress(self.sent, size)
                return chunk

        if auth_token:
            sep = "&" if "?" in url else "?"
            url = "%s%sauth=qp&zauthtoken=%s" % (
                url,
                sep,
                urllib.parse.quote(auth_token, safe=""),
            )
        with source.open("rb") as handle:
            reader = _ProgressReader(handle)
            request = urllib.request.Request(url, data=reader, method="POST")
            request.add_header("User-Agent", "soaplun/0.1")
            request.add_header("Content-Length", str(size))
            for key, value in headers.items():
                request.add_header(key, value)
            try:
                response = urllib.request.urlopen(
                    request, timeout=self.timeout, context=self.context
                )
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read(8192).decode("utf-8", "replace")
                except Exception:
                    pass
                return exc.code, body
            except urllib.error.URLError as exc:
                raise RestError("Cannot upload to %s: %s" % (url, exc.reason)) from exc
            try:
                return response.status, response.read().decode("utf-8", "replace")
            finally:
                response.close()


    def upload(
        self,
        url: str,
        source: Union[Path, str],
        auth_token: str = "",
        progress: Optional[ProgressCallback] = None,
        chunk_size: int = 1024 * 1024,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str]:
        """Upload with retries on transient network and proxy errors."""
        last_error: Optional[BaseException] = None
        for attempt in range(REST_RETRIES + 1):
            try:
                status, body = self._upload_once(
                    url,
                    source,
                    auth_token=auth_token,
                    progress=progress,
                    chunk_size=chunk_size,
                    extra_headers=extra_headers,
                )
                if status in RETRYABLE_HTTP_STATUS and attempt < REST_RETRIES:
                    logger.warning(
                        "REST upload HTTP %s, retry %d/%d",
                        status,
                        attempt + 1,
                        REST_RETRIES,
                    )
                    time.sleep(REST_RETRY_DELAY)
                    continue
                return status, body
            except Exception as exc:  # noqa: BLE001 - retry transient transport errors
                last_error = exc
                if attempt >= REST_RETRIES or not _retryable_error(exc):
                    raise
                logger.warning(
                    "REST upload retry %d/%d after %s",
                    attempt + 1,
                    REST_RETRIES,
                    exc,
                )
                time.sleep(REST_RETRY_DELAY)
        raise last_error  # pragma: no cover


def append_query(url: str, **params) -> str:
    """Append encoded query parameters to a URL."""
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(params)


def wait_for_file(path: Path, min_size: int = 1, timeout: float = 5.0) -> bool:
    """Wait briefly for a file to appear and reach ``min_size`` bytes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.exists() and path.stat().st_size >= min_size:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return os.path.exists(path) and path.stat().st_size >= min_size
