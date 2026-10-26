"""Outbound-fetch safety: non-public addresses, redirects into them, body caps (gzip bombs), image-bomb dimensions. No network."""
from __future__ import annotations

import gzip
import io
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import urllib3

from _shared_imports import REPO_ROOT  # noqa: F401

import net_safety_core as ns  # noqa: E402
import dossier_core  # noqa: E402
import photo_core  # noqa: E402


def resolver_for(mapping):
    def resolve(host, port, **kwargs):
        if host not in mapping:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
    return resolve


class CheckPublicUrlTests(unittest.TestCase):
    def test_non_public_addresses_are_refused_however_they_are_spelled(self) -> None:
        for url in ("http://127.0.0.1/", "http://localhost/", "http://2130706433/", "http://0/", "http://0x7f.1/", "http://192.168.1.1/admin",
                    "http://10.0.0.5/", "http://172.16.3.4/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://100.64.0.1/", "http://[fe80::1]/", "http://[fd00::1]/"):
            with self.subTest(url=url), self.assertRaises(ns.UnsafeUrl):
                ns.check_public_url(url)

    def test_only_http_and_https_and_no_embedded_credentials(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.org/x", "gopher://example.org/", "http://user:pw@example.org/", "http:///nohost"):
            with self.subTest(url=url), self.assertRaises(ns.UnsafeUrl):
                ns.check_public_url(url, resolver=resolver_for({"example.org": "93.184.216.34"}))

    def test_a_public_address_passes_and_a_name_resolving_to_a_private_one_does_not(self) -> None:
        ns.check_public_url("https://example.org/paper", resolver=resolver_for({"example.org": "93.184.216.34"}))
        with self.assertRaises(ns.UnsafeUrl):
            ns.check_public_url("https://rebind.example/", resolver=resolver_for({"rebind.example": "10.1.2.3"}))

    def test_an_unresolvable_name_is_an_ordinary_dead_link_not_a_refusal(self) -> None:
        ns.check_public_url("https://no-such-host.invalid/", resolver=resolver_for({}))


class FakeSend(ns.SafeAdapter):
    """Serves canned responses instead of touching the network."""

    def __init__(self, routes, **kwargs):
        super().__init__(**kwargs)
        self.routes, self.requested = routes, []

    def _send(self, request, **kwargs):
        self.requested.append(request.url)
        status, headers, body = self.routes[request.url]
        raw = urllib3.response.HTTPResponse(body=io.BytesIO(body), headers=headers, status=status, preload_content=False)
        response = requests.Response()
        response.status_code, response.headers, response.raw, response.url = status, requests.structures.CaseInsensitiveDict(headers), raw, request.url
        response.request = request
        return response


def session_with(routes, resolver, max_bytes=1_000_000):
    session = requests.Session()
    adapter = FakeSend(routes, max_bytes=max_bytes, resolver=resolver)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session, adapter


class SessionTests(unittest.TestCase):
    PUBLIC = {"start.example": "93.184.216.34", "other.example": "93.184.216.35", "evil.example": "192.168.0.10"}

    def test_a_redirect_into_a_private_address_is_stopped_at_the_second_hop(self) -> None:
        routes = {"http://start.example/a": (302, {"Location": "http://evil.example/secret"}, b""),
                  "http://evil.example/secret": (200, {}, b"router admin page")}
        session, adapter = session_with(routes, resolver_for(self.PUBLIC))
        with self.assertRaises(ns.UnsafeUrl):
            session.get("http://start.example/a", timeout=5)
        self.assertEqual(adapter.requested, ["http://start.example/a"], "the private hop must never be requested")

    def test_a_redirect_between_public_hosts_is_followed(self) -> None:
        routes = {"http://start.example/a": (302, {"Location": "http://other.example/b"}, b""),
                  "http://other.example/b": (200, {"Content-Type": "text/plain"}, b"hello")}
        session, _ = session_with(routes, resolver_for(self.PUBLIC))
        self.assertEqual(session.get("http://start.example/a", timeout=5).content, b"hello")

    def test_an_oversize_body_is_cut_off_even_without_a_content_length(self) -> None:
        routes = {"http://start.example/big": (200, {}, b"x" * 3_000_000)}
        session, _ = session_with(routes, resolver_for(self.PUBLIC), max_bytes=1_000_000)
        with self.assertRaises(ns.ResponseTooLarge):
            _ = session.get("http://start.example/big", timeout=5).content

    def test_a_declared_oversize_body_is_refused_before_reading(self) -> None:
        routes = {"http://start.example/big": (200, {"Content-Length": "50000000"}, b"x" * 10)}
        session, _ = session_with(routes, resolver_for(self.PUBLIC), max_bytes=1_000_000)
        with self.assertRaises(ns.ResponseTooLarge):
            session.get("http://start.example/big", timeout=5)

    def test_a_gzip_bomb_is_bounded_by_the_decoded_size(self) -> None:
        bomb = gzip.compress(b"\0" * 60_000_000)
        self.assertLess(len(bomb), 200_000)
        routes = {"http://start.example/bomb": (200, {"Content-Encoding": "gzip"}, bomb)}
        session, _ = session_with(routes, resolver_for(self.PUBLIC), max_bytes=1_000_000)
        with self.assertRaises(ns.ResponseTooLarge):
            _ = session.get("http://start.example/bomb", timeout=5).content


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"internal service")

    def log_message(self, *args):  # noqa: D401
        pass


class RealLoopbackTests(unittest.TestCase):
    """A real server on 127.0.0.1: the fetchers the pipeline uses must refuse it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def test_the_dossier_fetcher_refuses_loopback_and_never_reaches_the_prompt(self) -> None:
        fetcher = photo_core.RequestsFetcher(timeout=5, retries=0)
        for host in ("127.0.0.1", "localhost", "2130706433"):
            doc = dossier_core.fetch_source(f"http://{host}:{self.port}/", fetcher, max_chars=9000)
            self.assertNotEqual(doc.status, "ok", host)
            self.assertNotIn("internal service", doc.text)
            self.assertIn("refused", doc.note)

    def test_the_citation_checker_reports_a_non_public_citation_as_dead(self) -> None:
        import provenance_core

        checker = provenance_core.default_url_checker(timeout=5)
        self.assertEqual(checker(f"http://127.0.0.1:{self.port}/"), -1)
        report = provenance_core.check_article(f"## T\n\n### Primary Sources\n\n- Local page http://127.0.0.1:{self.port}/x\n", [], url_checker=checker)
        self.assertEqual(report.dead_urls[0]["status"], "non-public address")
        self.assertTrue(report.needs_repair())


class ImageBombTests(unittest.TestCase):
    def test_a_header_declaring_enormous_dimensions_is_refused_without_decoding(self) -> None:
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("1", (20000, 20000)).save(buffer, format="PNG")  # ~50 KB, 400 megapixels
        self.assertLess(len(buffer.getvalue()), 200_000)
        self.assertFalse(ns.image_pixels_ok(buffer.getvalue()))
        small = io.BytesIO()
        Image.new("RGB", (800, 600)).save(small, format="PNG")
        self.assertTrue(ns.image_pixels_ok(small.getvalue()))
        self.assertTrue(ns.image_pixels_ok(b"not an image at all"))


class LooksPublicTests(unittest.TestCase):
    """Static screen applied to the slate model's URLs before anything is fetched (review T10)."""

    def test_internal_spellings_are_refused_without_a_network(self) -> None:
        for url in ("http://localhost:8080/", "http://127.0.0.1/", "http://2130706433/", "http://0x7f000001/", "http://017700000001/",
                    "http://192.168.1.1/admin", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                    "http://router/", "http://nas.local/", "http://printer.internal/x", "ftp://example.org/x", "file:///etc/passwd",
                    "https://user:pw@example.org/", "http:///nohost"):
            self.assertFalse(ns.looks_public(url), url)

    def test_ordinary_public_urls_pass(self) -> None:
        for url in ("https://www.nature.com/articles/s41586-026-00001-x", "http://example.org/a?b=c", "https://8.8.8.8/dns"):
            self.assertTrue(ns.looks_public(url), url)


if __name__ == "__main__":
    unittest.main()
