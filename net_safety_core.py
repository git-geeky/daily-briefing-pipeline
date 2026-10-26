"""Outbound-fetch safety for every ``requests`` session that follows URLs supplied by a model or by web content.

The pipeline fetches URLs that a headline, a feed item, a search result or the slate model chose. Anyone who can plant a link
on a monitored page can therefore point the desktop at its own network (``http://192.168.1.1/``, ``http://localhost:8080/``, a
cloud metadata address) and have the response placed in a model prompt and in the evidence corpus. A hostile server can also
answer with a gzip bomb. This module is the single choke point:

* ``check_public_url``: http(s) only, no embedded credentials, and every address the host resolves to must be globally routable
  (loopback, private, link-local, carrier-grade NAT and reserved ranges are refused; decimal/octal/IPv4-mapped spellings are
  normalised by the resolver before the test);
* ``SafeAdapter`` / ``mount_safe``: applies that check to *every* request a session sends, including each redirect hop (requests
  re-enters the adapter per hop), and caps the number of decoded body bytes, which also bounds decompression.

Residual risk, stated plainly: the address is checked at resolution time and the connection resolves again, so DNS rebinding
within a few seconds is not prevented; pinning the connection to the checked address would need a custom transport.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

DEFAULT_MAX_BYTES = 25_000_000


class UnsafeUrl(requests.exceptions.RequestException):
    """The URL was refused before any request was sent."""


class ResponseTooLarge(requests.exceptions.RequestException):
    """The body exceeded the byte cap (declared or actually read after decoding)."""


Resolver = Callable[..., Any]


def check_public_url(url: str, *, resolver: Resolver | None = None) -> None:
    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrl(f"scheme {parts.scheme!r} is not allowed")
    if not parts.hostname:
        raise UnsafeUrl("URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrl("URL carries credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = (resolver or socket.getaddrinfo)(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return  # an unresolvable name is an ordinary dead link; the request itself fails the same way
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            raise UnsafeUrl(f"{parts.hostname} resolved to an unreadable address {text!r}") from None
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        if not address.is_global:
            raise UnsafeUrl(f"{parts.hostname} resolves to a non-public address ({address}); refused")


_NON_PUBLIC_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".localdomain", ".intranet")


def looks_public(url: str) -> bool:
    """Static test with no network: http(s), no credentials, a dotted host that is not an obvious internal name or a non-global IP
    literal (decimal, octal and hex spellings of an address count as IP literals). The fetchers still resolve and re-check."""
    parts = urlsplit(url or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(_NON_PUBLIC_SUFFIXES):
        return False
    try:
        number = int(host, 0)  # "2130706433", "0x7f000001", "017700000001"
    except ValueError:
        number = None
    if number is not None:
        try:
            return ipaddress.ip_address(number).is_global
        except ValueError:
            return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    return "." in host  # a bare single-label name ("router", "nas") is an intranet name


class _CappedRaw:
    """Wraps a urllib3 response so reading it (after content decoding) can never exceed ``cap`` bytes."""

    def __init__(self, raw: Any, cap: int):
        self._raw, self._cap, self._seen = raw, cap, 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def _count(self, size: int) -> None:
        self._seen += size
        if self._seen > self._cap:
            try:
                self._raw.close()
            finally:
                raise ResponseTooLarge(f"response body exceeded {self._cap} bytes")

    def stream(self, amt: int | None = None, decode_content: bool | None = None):
        for chunk in self._raw.stream(amt, decode_content=decode_content):
            self._count(len(chunk))
            yield chunk

    def read(self, amt: int | None = None, decode_content: bool | None = None, cache_content: bool = False) -> bytes:
        data = self._raw.read(amt, decode_content=decode_content)
        self._count(len(data or b""))
        return data


class SafeAdapter(HTTPAdapter):
    def __init__(self, *args: Any, max_bytes: int = DEFAULT_MAX_BYTES, resolver: Resolver | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.max_bytes, self.resolver = max_bytes, resolver

    def _send(self, request: Any, **kwargs: Any) -> Any:
        return super().send(request, **kwargs)

    def send(self, request: Any, **kwargs: Any) -> Any:  # noqa: D401 - requests calls this once per redirect hop
        check_public_url(request.url, resolver=self.resolver)
        kwargs["stream"] = True  # nothing is read before the cap is in place
        response = self._send(request, **kwargs)
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self.max_bytes and not response.headers.get("Content-Encoding"):
            response.close()
            raise ResponseTooLarge(f"declared body of {declared} bytes exceeds the {self.max_bytes} byte cap")
        response.raw = _CappedRaw(response.raw, self.max_bytes)
        return response


def image_pixels_ok(data: bytes, *, max_pixels: int = 60_000_000) -> bool:
    """False for an image whose declared dimensions would decode to more than ``max_pixels`` (a decompression bomb).
    Reads only the header. Without Pillow (or for a format Pillow cannot identify) there is nothing to refuse here."""
    try:
        import io

        from PIL import Image
    except ImportError:
        return True
    try:
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = None  # this function does its own bound; do not let Pillow raise first
        try:
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
        finally:
            Image.MAX_IMAGE_PIXELS = previous
    except Exception:  # noqa: BLE001 - unreadable: the callers' own checks decide
        return True
    return width * height <= max_pixels


def mount_safe(session: requests.Session, *, max_bytes: int = DEFAULT_MAX_BYTES, resolver: Resolver | None = None) -> requests.Session:
    adapter = SafeAdapter(max_bytes=max_bytes, resolver=resolver)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


__all__ = ["DEFAULT_MAX_BYTES", "ResponseTooLarge", "SafeAdapter", "UnsafeUrl", "check_public_url", "image_pixels_ok", "looks_public", "mount_safe"]
