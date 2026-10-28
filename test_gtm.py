"""
Tests for the read-only Tag Manager tools (Munder patch). No network, no real key: the GTM service is a mock.
Run: .venv/bin/python -m unittest test_gtm -v
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("GSC_SKIP_OAUTH", "true")
import gsc_server as g
from googleapiclient.errors import HttpError

GTM_TOOLS = {"gtm_list_accounts", "gtm_list_containers", "gtm_list_workspaces", "gtm_get_live_version",
             "gtm_list_versions", "gtm_diff_versions", "gtm_list_tags", "gtm_list_triggers", "gtm_list_variables"}
RO_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"


def run(c):
    return asyncio.run(c)


def http_err(status):
    r = MagicMock()
    r.status = status
    r.reason = "x"
    return HttpError(r, b'{"error":{"message":"detail with /secret/path/key.json"}}')


def version(vid, tags=(), triggers=(), variables=()):
    return {"containerVersionId": vid, "name": f"v{vid}", "container": {"publicId": "GTM-AAA", "name": "c", "usageContext": ["web"]},
            "tag": list(tags), "trigger": list(triggers), "variable": list(variables)}


def tag(i, name, url="a", **kw):
    return {"tagId": str(i), "name": name, "type": "html", "firingTriggerId": ["1"], "fingerprint": f"fp{url}",
            "parameter": [{"type": "template", "key": "html", "value": url}], **kw}


class Tools(unittest.TestCase):
    def test_handshake_lists_gtm_tools_and_no_write_tool(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def go():
            p = StdioServerParameters(command=sys.executable, args=[os.path.join(os.path.dirname(__file__), "gsc_server.py")],
                                      env={**os.environ, "GSC_SKIP_OAUTH": "true"})
            async with stdio_client(p) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    return {t.name for t in (await s.list_tools()).tools}
        names = run(go())
        self.assertTrue(GTM_TOOLS <= names)
        bad = [n for n in names if n.startswith("gtm_") and n not in GTM_TOOLS]
        self.assertEqual(bad, [])
        self.assertFalse([n for n in names if n.startswith("gtm_") and any(w in n for w in ("create", "update", "delete", "publish", "edit"))])

    def test_scope_is_exactly_readonly(self):
        self.assertEqual(g.GTM_SCOPES, [RO_SCOPE])
        self.assertEqual(g.SCOPES, ["https://www.googleapis.com/auth/webmasters.readonly"])
        with patch.object(g, "POSSIBLE_CREDENTIAL_PATHS", [__file__]), \
             patch.object(g, "GSC_CREDENTIALS_PATH", None), \
             patch.object(g.service_account.Credentials, "from_service_account_file") as f, \
             patch.object(g, "build") as b:
            g.get_gtm_service()
        self.assertEqual(f.call_args.kwargs["scopes"], [RO_SCOPE])
        self.assertEqual(b.call_args.args[:2], ("tagmanager", "v2"))

    def test_403_is_clean(self):
        svc = MagicMock()
        svc.accounts().containers().versions().live().execute.side_effect = http_err(403)
        with patch.object(g, "get_gtm_service", return_value=svc):
            out = run(g.gtm_get_live_version("1", "22"))
        self.assertIn("no access to GTM container 22", out)
        self.assertNotIn("/secret", out)

    def test_missing_credentials_message_has_no_path(self):
        with patch.object(g, "get_gtm_service", side_effect=FileNotFoundError("/secret/key.json")):
            out = run(g.gtm_list_accounts())
        self.assertNotIn("/secret", out)
        self.assertTrue(out.startswith("Error:"))

    def test_bad_ids_rejected(self):
        self.assertTrue(run(g.gtm_list_containers("1/../2")).startswith("Error:"))

    def test_diff_versions(self):
        va = version(1, tags=[tag(1, "GA", "a"), tag(2, "Old")], variables=[{"variableId": "9", "name": "v", "type": "c"}])
        vb = version(2, tags=[tag(1, "GA", "b", paused=True), tag(3, "New")], variables=[{"variableId": "9", "name": "v", "type": "c"}])
        svc = MagicMock()
        get = svc.accounts().containers().versions().get
        get.side_effect = lambda path: MagicMock(execute=lambda: {"1": va, "2": vb}[path.rsplit("/", 1)[1]])
        with patch.object(g, "get_gtm_service", return_value=svc):
            d = json.loads(run(g.gtm_diff_versions("1", "2", "1", "2")))
        t = d["tags"]
        self.assertEqual([x["name"] for x in t["added"]], ["New"])
        self.assertEqual([x["name"] for x in t["removed"]], ["Old"])
        ch = t["modified"][0]["changed"]
        self.assertEqual(ch["parameter"]["html"], {"from": "a", "to": "b"})
        self.assertIn("paused", ch)
        self.assertNotIn("fingerprint", ch)
        self.assertNotIn("variables", d)  # unchanged section omitted

    def test_live_version_synthesised_and_limited(self):
        v = version(5, tags=[tag(i, f"t{i}") for i in range(1, 8)])
        svc = MagicMock()
        svc.accounts().containers().versions().live().execute.return_value = v
        with patch.object(g, "get_gtm_service", return_value=svc):
            d = json.loads(run(g.gtm_get_live_version("1", "2", limit=3)))
        self.assertEqual(d["counts"]["tags"], 7)
        self.assertEqual(len(d["tags"]), 3)
        self.assertEqual(d["tags_truncated"], 4)

    def test_list_tags_paginates_with_limit(self):
        svc = MagicMock()
        res = svc.accounts().containers().workspaces().tags()
        res.list.return_value = MagicMock(execute=lambda: {"tag": [tag(1, "a"), tag(2, "b")], "nextPageToken": "n"})
        res.list_next.return_value = MagicMock(execute=lambda: {"tag": [tag(3, "c")]})
        with patch.object(g, "get_gtm_service", return_value=svc):
            d = json.loads(run(g.gtm_list_tags("1", "2", "3", limit=3)))
        self.assertEqual([x["name"] for x in d["tags"]], ["a", "b", "c"])
        self.assertFalse(d["truncated"])


if __name__ == "__main__":
    unittest.main()
