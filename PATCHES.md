# Munder patches on top of AminForou/mcp-gsc @ d49eea9e5efb

No automatic `git pull`: upgrades are a manual re-vendor + re-review of this file.

1. `SCOPES` = `https://www.googleapis.com/auth/webmasters.readonly` (was `webmasters`). Even a service account
   granted Owner/Full on the property can only read through this server.
2. Removed every write/auth tool: `add_site`, `delete_site`, `submit_sitemap`, `delete_sitemap`,
   `manage_sitemaps`, `reauthenticate`. Remaining tools are the read ones (list_properties, get_search_analytics,
   get_site_details, get_sitemaps, inspect_url_enhanced, batch_url_inspection, check_indexing_issues,
   get_performance_overview, get_advanced_search_analytics, compare_search_periods, get_search_by_page_query,
   list_sitemaps_enhanced, get_sitemap_details, get_capabilities, get_creator_info).
3. `get_capabilities` help text no longer lists the removed tools.

4. New read-only tool `get_core_web_vitals(url_or_origin, form_factor="ALL", include_lab=False)` (+ tests in
   `test_core_web_vitals.py`, run with `.venv/bin/python -m unittest test_core_web_vitals`). Real-user CrUX
   p75 LCP/INP/CLS/FCP/TTFB with good/needs improvement/poor ratings (page URL first, origin fallback), and
   optionally PageSpeed Insights v5 lab data. Standard library HTTP only (urllib): no new dependency.
   API key: read from the FILE `PAGESPEED_KEY_PATH` (default `~/.config/magenio/pagespeed.key`), sent in the
   `X-Goog-Api-Key` header (never in a URL), never returned/logged; errors name the key file by base name only
   and scrub the key from Google's error text. GSC scopes unchanged (readonly).

5. New read-only Google Tag Manager tools (API v2 via the already-present google-api-python-client, no new
   dependency): `gtm_list_accounts`, `gtm_list_containers(account_id)`, `gtm_list_workspaces(account_id, container_id)`,
   `gtm_get_live_version(account_id, container_id, limit=50)`, `gtm_list_versions(account_id, container_id, limit=20)`,
   `gtm_diff_versions(account_id, container_id, version_a, version_b)` (ids or "live"),
   `gtm_list_tags|triggers|variables(account_id, container_id, workspace_id, limit=50)`. Tests: `test_gtm.py`
   (`.venv/bin/python -m unittest test_gtm`). Same key as GSC (`GSC_CREDENTIALS_PATH`) but a separate token with ONLY
   `https://www.googleapis.com/auth/tagmanager.readonly`; GSC `SCOPES` is unchanged. Only list/get/live calls exist.
   Output is compact (limits, `truncated` flags, noise fields such as fingerprint/path dropped). 403 -> "the service
   account has no access to GTM container X", never a path or secret. The service account must be added as a
   Read user in Tag Manager and the Tag Manager API enabled in its Google Cloud project.

Not patched (by design): the OAuth fallback code is still present but unreachable in use because Munder starts the
server with `GSC_SKIP_OAUTH=true` and `GSC_CREDENTIALS_PATH=<project service-account key>`.
`test_gsc_server.py` still contains tests for the removed tools; they are expected to fail and are not run.

Runtime: `uv sync --frozen --python 3.12` (uv-managed Python, local `.venv`, `uv.lock` as shipped).
Launch (Munder consent): command `/opt/homebrew/bin/uv`, args `run --frozen --no-sync --directory /Users/magenio/www/magenio-mcp/gsc-mcp gsc_server.py`.
