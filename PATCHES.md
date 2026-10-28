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

Not patched (by design): the OAuth fallback code is still present but unreachable in use because Munder starts the
server with `GSC_SKIP_OAUTH=true` and `GSC_CREDENTIALS_PATH=<project service-account key>`.
`test_gsc_server.py` still contains tests for the removed tools; they are expected to fail and are not run.

Runtime: `uv sync --frozen --python 3.12` (uv-managed Python, local `.venv`, `uv.lock` as shipped).
Launch (Munder consent): command `/opt/homebrew/bin/uv`, args `run --frozen --no-sync --directory /Users/magenio/www/magenio-mcp/gsc-mcp gsc_server.py`.
