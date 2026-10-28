"""
Tests for get_core_web_vitals (Munder patch). No network and no real key: the API key is a dummy
file in a temp dir, referenced through PAGESPEED_KEY_PATH. Run: python -m unittest test_core_web_vitals -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import io
from unittest.mock import patch

os.environ.setdefault("GSC_SKIP_OAUTH", "true")
import gsc_server as g

FAKE_KEY = "FAKE-KEY-0123456789-abcdef"
WRITE_TOOLS = {"add_site", "delete_site", "submit_sitemap", "delete_sitemap", "manage_sitemaps", "reauthenticate"}


def run(coro):
    return asyncio.run(coro)


def key_file(content=FAKE_KEY):
    d = tempfile.mkdtemp(prefix="cwv-secret-dir-")
    p = os.path.join(d, "pagespeed.key")
    with open(p, "w") as fh:
        fh.write(content)
    return p


def crux_record(lcp=2000, cls="0.30"):
    hist = lambda a, b, c: [{"start": 0, "end": 1, "density": a}, {"start": 1, "end": 2, "density": b}, {"start": 2, "density": c}]
    return {"record": {"metrics": {
        "largest_contentful_paint": {"histogram": hist(0.8, 0.1, 0.1), "percentiles": {"p75": lcp}},
        "cumulative_layout_shift": {"histogram": hist(0.5, 0.2, 0.3), "percentiles": {"p75": cls}},
    }, "collectionPeriod": {"firstDate": {"year": 2026, "month": 10, "day": 1}}}}


class ToolListAndHandshake(unittest.TestCase):
    def test_server_lists_tool_over_stdio_and_no_write_tool(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def go():
            params = StdioServerParameters(command=sys.executable, args=[os.path.join(os.path.dirname(__file__), "gsc_server.py")],
                                           env={**os.environ, "GSC_SKIP_OAUTH": "true", "PAGESPEED_KEY_PATH": "/nonexistent/dir/pagespeed.key"})
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    names = {t.name for t in (await s.list_tools()).tools}
                    out = await s.call_tool("get_core_web_vitals", {"url_or_origin": "https://example.com"})
                    return names, out.content[0].text

        names, text = run(go())
        self.assertIn("get_core_web_vitals", names)
        self.assertFalse(names & WRITE_TOOLS)
        self.assertIn("pagespeed.key", text)
        self.assertNotIn("/nonexistent", text)  # file name only, never the secret path


class KeyHandling(unittest.TestCase):
    def test_missing_key_is_a_clear_error_without_the_directory(self):
        with patch.dict(os.environ, {"PAGESPEED_KEY_PATH": "/secret/place/pagespeed.key"}):
            out = run(g.get_core_web_vitals("https://example.com"))
        self.assertTrue(out.startswith("Error:"))
        self.assertIn("pagespeed.key", out)
        self.assertNotIn("/secret/place", out)

    def test_empty_key_file(self):
        with patch.dict(os.environ, {"PAGESPEED_KEY_PATH": key_file("  \n")}):
            self.assertIn("empty", run(g.get_core_web_vitals("https://example.com")))

    def test_default_path_is_under_config_magenio(self):
        self.assertTrue(g.DEFAULT_PAGESPEED_KEY_PATH.endswith(os.path.join(".config", "magenio", "pagespeed.key")))

    def test_key_only_in_header_never_in_url_and_invalid_key_error_does_not_echo_it(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            body = json.dumps({"error": {"message": f"API key not valid: {FAKE_KEY}"}}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

        with patch.dict(os.environ, {"PAGESPEED_KEY_PATH": key_file()}), patch("urllib.request.urlopen", fake_urlopen):
            out = run(g.get_core_web_vitals("https://example.com/p"))
        self.assertNotIn(FAKE_KEY, seen["url"])
        self.assertEqual(seen["headers"].get("x-goog-api-key"), FAKE_KEY)
        self.assertTrue(out.startswith("Error: Google API returned HTTP 400"))
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn("***", out)


class Behaviour(unittest.TestCase):
    def call(self, args, responder):
        calls = []

        def fake(method, url, key, body=None):
            calls.append((method, url, body))
            return responder(method, url, body)

        with patch.dict(os.environ, {"PAGESPEED_KEY_PATH": key_file()}), patch.object(g, "_cwv_http", fake):
            return run(g.get_core_web_vitals(**args)), calls

    def test_ratings_and_origin_fallback(self):
        def responder(method, url, body):
            if "url" in body:
                raise g._CwvNotFound()
            return crux_record(lcp=2000, cls="0.30")
        out, calls = self.call({"url_or_origin": "https://example.com/page", "form_factor": "phone"}, responder)
        d = json.loads(out)["field_data"]
        self.assertEqual(d["scope"], "origin")
        self.assertEqual(d["metrics"]["LCP"]["rating"], "good")
        self.assertEqual(d["metrics"]["CLS"]["rating"], "poor")
        self.assertEqual(calls[0][2]["url"], "https://example.com/page")
        self.assertEqual(calls[1][2]["origin"], "https://example.com")
        self.assertEqual(calls[0][2]["formFactor"], "PHONE")

    def test_no_data_and_bad_input(self):
        out, _ = self.call({"url_or_origin": "https://example.com"}, lambda *a: (_ for _ in ()).throw(g._CwvNotFound()))
        self.assertIn("No CrUX field data", out)
        for bad in ("example.com", "ftp://x.y", ""):
            self.assertTrue(run(g.get_core_web_vitals(bad)).startswith("Error:"))
        self.assertIn("form_factor", run(g.get_core_web_vitals("https://example.com", form_factor="TABLET")))

    def test_lab_data_runs_mobile_and_desktop_for_all(self):
        def responder(method, url, body):
            if method == "POST":
                return crux_record()
            return {"lighthouseResult": {"categories": {"performance": {"score": 0.87}}, "audits": {"largest-contentful-paint": {"displayValue": "2.1 s"}}}}
        out, calls = self.call({"url_or_origin": "https://example.com", "include_lab": True}, responder)
        lab = json.loads(out)["lab_data"]
        self.assertEqual(lab["mobile"]["performance_score"], 87)
        self.assertEqual(lab["desktop"]["LCP"], "2.1 s")
        self.assertEqual(sum(1 for c in calls if c[0] == "GET"), 2)


if __name__ == "__main__":
    unittest.main()
