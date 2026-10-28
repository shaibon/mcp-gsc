from typing import Any, Dict, List, Optional
import asyncio
import logging
import os
import re
import json
import sys
import shutil
from datetime import datetime, timedelta

# Fail loudly on unsupported Python. The mcp/FastMCP dependency requires 3.11+.
# On Python 3.10 or older the server dies during import, and when it is launched
# by a GUI client (e.g. Claude Desktop) that failure is silent — no tools appear
# and no log file is written, which is impossible to diagnose. See issue #19.
if sys.version_info < (3, 11):
    sys.exit(
        "mcp-gsc requires Python 3.11 or newer, but you are running "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        "Install Python 3.11+ (https://www.python.org/downloads/) and recreate "
        "your virtual environment, or use the uvx install method which manages "
        "the Python version for you."
    )

from platformdirs import user_config_dir

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Suppress the noisy file_cache warning from google-api-python-client.
# Some MCP hosts (e.g. GitHub Copilot CLI) treat any stderr output as a
# fatal error, so this prevents false crashes.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# MCP
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("gsc-server")

def _expand_path(path: Optional[str]) -> Optional[str]:
    """Expand ``~`` and environment variables in a path, returning None for empty input.

    This lets users write ``GSC_CREDENTIALS_PATH=~/creds.json`` or
    ``GSC_CREDENTIALS_PATH=$HOME/creds.json`` without hitting silent lookup failures.
    """
    if not path:
        return None
    return os.path.expandvars(os.path.expanduser(path))


# Path to your service account JSON or user credentials JSON
# First check if GSC_CREDENTIALS_PATH environment variable is set
# Then try looking in the script directory and current working directory as fallbacks
GSC_CREDENTIALS_PATH = _expand_path(os.environ.get("GSC_CREDENTIALS_PATH"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POSSIBLE_CREDENTIAL_PATHS = [
    GSC_CREDENTIALS_PATH,  # First try the environment variable if set
    os.path.join(SCRIPT_DIR, "service_account_credentials.json"),
    os.path.join(os.getcwd(), "service_account_credentials.json"),
    # Add any other potential paths here
]

# OAuth client secrets file path
OAUTH_CLIENT_SECRETS_FILE = _expand_path(os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE"))
# Track whether the user explicitly set the env var (vs. using the SCRIPT_DIR fallback).
# Needed so get_gsc_service() can fail-fast when the explicit path is wrong, while
# leaving the fallback behavior unchanged for clone-install users.
GSC_OAUTH_CLIENT_SECRETS_FILE_EXPLICIT = OAUTH_CLIENT_SECRETS_FILE is not None
if not OAUTH_CLIENT_SECRETS_FILE:
    OAUTH_CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")

# Token file path for storing OAuth tokens.
# Stored in the user config directory so it survives uvx updates (which replace SCRIPT_DIR).
# Override with GSC_CONFIG_DIR env var for Docker/power users.
_CONFIG_DIR = os.environ.get("GSC_CONFIG_DIR") or user_config_dir("mcp-gsc")
os.makedirs(_CONFIG_DIR, exist_ok=True)
TOKEN_FILE = os.path.join(_CONFIG_DIR, "token.json")

# Silently migrate token from old location (SCRIPT_DIR) on first run after upgrade.
# Existing users never need to re-authenticate.
_OLD_TOKEN = os.path.join(SCRIPT_DIR, "token.json")
if os.path.exists(_OLD_TOKEN) and not os.path.exists(TOKEN_FILE):
    shutil.move(_OLD_TOKEN, TOKEN_FILE)

# Environment variable to skip OAuth authentication
SKIP_OAUTH = os.environ.get("GSC_SKIP_OAUTH", "").lower() in ("true", "1", "yes")

# Safety flag for destructive operations (add_site, delete_site, delete_sitemap).
# Default is false — set GSC_ALLOW_DESTRUCTIVE=true to enable these tools.
ALLOW_DESTRUCTIVE = os.environ.get("GSC_ALLOW_DESTRUCTIVE", "false").lower() in ("true", "1", "yes")

# Data state for search analytics queries.
# "all"   → includes fresh/unconfirmed data, matches the GSC dashboard (default)
# "final" → only confirmed data, which lags 2-3 days behind the dashboard
_raw_data_state = os.environ.get("GSC_DATA_STATE", "all").lower().strip()
if _raw_data_state not in ("all", "final"):
    raise ValueError(
        f"Invalid GSC_DATA_STATE value '{_raw_data_state}'. "
        "Accepted values are 'all' (default, matches GSC dashboard) or 'final' (2-3 day lag)."
    )
DATA_STATE = _raw_data_state

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]  # PATCH(munder): read-only

def get_gsc_service():
    """
    Returns an authorized Search Console service object.
    First tries OAuth authentication, then falls back to service account.
    """
    # Fail-fast if credential env vars are set but point to files that don't exist.
    # Without this, a typo'd or uvx-incompatible path would silently fall through to
    # SCRIPT_DIR/cwd fallbacks (which don't work under uvx) and emit a misleading
    # "file not found" error with no mention of the env var the user actually set.
    # We rely on the module-level captured values (GSC_CREDENTIALS_PATH,
    # GSC_OAUTH_CLIENT_SECRETS_FILE_EXPLICIT) rather than re-reading os.environ at
    # call time, because the env var may have been cleared by the caller after module
    # import (e.g. by test frameworks or MCP hosts that scope env vars narrowly).
    if GSC_CREDENTIALS_PATH and not os.path.exists(GSC_CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"GSC_CREDENTIALS_PATH is set to {GSC_CREDENTIALS_PATH!r} but the file "
            f"does not exist. "
            f"If running via uvx, this MUST be an absolute path to your service account "
            f"credentials JSON file — placing the file in your project folder is not "
            f"sufficient because uvx runs the code from an internal cache directory."
        )
    if GSC_OAUTH_CLIENT_SECRETS_FILE_EXPLICIT and not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"GSC_OAUTH_CLIENT_SECRETS_FILE is set to {OAUTH_CLIENT_SECRETS_FILE!r} "
            f"but the file does not exist. "
            f"If running via uvx, this MUST be an absolute path to your OAuth "
            f"client_secrets.json file."
        )

    # Try OAuth authentication first if not skipped
    if not SKIP_OAUTH:
        try:
            return get_gsc_service_oauth()
        except Exception as e:
            # If OAuth fails, try service account
            logging.warning("OAuth authentication failed: %s", e)
            pass
    
    # Try service account authentication
    for cred_path in POSSIBLE_CREDENTIAL_PATHS:
        if cred_path and os.path.exists(cred_path):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    cred_path, scopes=SCOPES
                )
                return build("searchconsole", "v1", credentials=creds, cache_discovery=False)
            except Exception as e:
                continue  # Try the next path if this one fails
    
    # If we get here, none of the authentication methods worked.
    # Note: uvx users can't place files "in the script directory" because uvx runs
    # the code from ~/.cache/uv/archive-v0/<hash>/lib/python*/site-packages/ — an
    # internal cache they cannot reach. They must use env vars with absolute paths.
    raise FileNotFoundError(
        f"Authentication failed. Please either:\n"
        f"1. Set up OAuth by setting GSC_OAUTH_CLIENT_SECRETS_FILE to an absolute path, "
        f"or (for clone installs) placing a client_secrets.json file in the script "
        f"directory, then call the 'reauthenticate' tool to open a browser login window "
        f"and complete authentication, or\n"
        f"2. Set GSC_CREDENTIALS_PATH to an absolute path, or (for clone installs) "
        f"place a service account credentials file in one of these locations: "
        f"{', '.join([p for p in POSSIBLE_CREDENTIAL_PATHS[1:] if p])}\n"
        f"\n"
        f"If you installed via uvx, the 'script directory' is an internal uv cache "
        f"that you cannot access — you MUST use the environment variables with "
        f"absolute paths."
    )

def get_gsc_service_oauth():
    """
    Returns an authorized Search Console service object using OAuth.
    """
    creds = None
    
    # Check if token file exists
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            # If token file is corrupted, delete it
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            creds = None
    
    # If credentials don't exist or are invalid, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save the refreshed credentials
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                # If refresh fails, delete the bad token and trigger new OAuth flow
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
                # Fall through to the OAuth flow below
                creds = None
        
        # Start new OAuth flow if we don't have valid credentials
        if not creds or not creds.valid:
            # Check if client secrets file exists
            if not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    f"OAuth client secrets file not found. Please place a client_secrets.json file in the script directory "
                    f"or set the GSC_OAUTH_CLIENT_SECRETS_FILE environment variable."
                )

            # Start OAuth flow — opens a browser window on macOS even from MCP subprocess.
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
            
            # Save the credentials for future use
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
    
    # Build and return the service
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _site_not_found_error(site_url: str) -> str:
    """Return a helpful message when a GSC property returns 404."""
    lines = [f"Property '{site_url}' not found (404). Possible causes:\n"]
    lines.append(
        "1. The site_url doesn't exactly match what is in GSC. "
        "Run list_properties to get the exact string to use."
    )
    if site_url.startswith("sc-domain:"):
        lines.append(
            "2. Domain properties require the service account to be explicitly added "
            "under GSC Settings > Users and permissions for that specific domain property. "
            "OAuth users must also have verified access to it."
        )
    else:
        lines.append(
            "2. If your property is a domain property (covers all subdomains), "
            "the correct format is 'sc-domain:example.com', not a full URL."
        )
    lines.append(
        "3. The authenticated account may not have access to this property."
    )
    return "\n".join(lines)


@mcp.tool()
async def get_capabilities() -> str:
    """
    Get a full list of all available tools, current auth status, and how to get started.
    ALWAYS call this first when asked what tools are available, what this server can do,
    or how to get started. Returns all tool names grouped by category in a single call —
    faster than searching individually. Also shows if authentication is needed.
    """
    # Check auth status
    try:
        get_gsc_service()
        auth_status = "✅ Authenticated — ready to use all tools."
    except Exception as e:
        auth_status = f"❌ Not authenticated — call the 'reauthenticate' tool first to open a browser login window.\nDetails: {e}"

    return f"""Google Search Console MCP Server

AUTH STATUS:
{auth_status}

GETTING STARTED:
1. If not authenticated, call the 'reauthenticate' tool to complete OAuth login.
2. Call 'list_properties' to see all your GSC sites and get the exact site_url for other tools.
3. Use any tool below with the site_url from step 2.

AVAILABLE TOOLS:

Authentication:

Properties:
  - list_properties: List all GSC sites/properties you have access to (start here)
  - get_site_details: Get verification and ownership details for a site

Analytics & Reporting:
  - get_search_analytics: Top queries and pages with clicks, impressions, CTR, position
  - get_performance_overview: Summary of site performance for a time period
  - compare_search_periods: Compare performance between two time periods
  - get_search_by_page_query: Search terms driving traffic to a specific page
  - get_advanced_search_analytics: Advanced filtering by country, device, query, page

URL Inspection & Indexing:
  - inspect_url_enhanced: Detailed crawl/index status for a specific URL
  - batch_url_inspection: Inspect up to 10 URLs at once
  - check_indexing_issues: Check multiple URLs for indexing problems

Core Web Vitals:
  - get_core_web_vitals: Real-user CrUX p75 LCP/INP/CLS/FCP/TTFB (+ optional PageSpeed lab data); needs PAGESPEED_KEY_PATH key file

Tag Manager (read-only, same service account):
  - gtm_list_accounts, gtm_list_containers, gtm_list_workspaces, gtm_get_live_version, gtm_list_versions, gtm_diff_versions, gtm_list_tags, gtm_list_triggers, gtm_list_variables

Sitemaps:
  - get_sitemaps: List all sitemaps for a site
  - list_sitemaps_enhanced: Detailed sitemap info including errors and warnings

"""


@mcp.tool()
async def list_properties() -> str:
    """
    List all Google Search Console (GSC) properties and sites the user has access to.
    Use this to see all verified sites, domain properties, and URL-prefix properties
    in the connected Google Search Console account. Always call this first to get the
    exact site_url needed for other tools.
    """
    try:
        service = get_gsc_service()
        site_list = service.sites().list().execute()

        # site_list is typically something like:
        # {
        #   "siteEntry": [
        #       {"siteUrl": "...", "permissionLevel": "..."},
        #       ...
        #   ]
        # }
        sites = site_list.get("siteEntry", [])

        if not sites:
            return "No Search Console properties found."

        return json.dumps({
            "count": len(sites),
            "properties": [
                {
                    "site_url": site.get("siteUrl", "Unknown"),
                    "permission_level": site.get("permissionLevel", "Unknown"),
                }
                for site in sites
            ],
        })
    except FileNotFoundError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error retrieving properties: {str(e)}"

@mcp.tool()
async def get_search_analytics(site_url: str, days: int = 28, dimensions: str = "query", row_limit: int = 20) -> str:
    """
    Get search analytics data for a specific property.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        days: Number of days to look back (default: 28)
        dimensions: Dimensions to group by (default: query). Options: query, page, device, country, date
                   You can provide multiple dimensions separated by comma (e.g., "query,page")
        row_limit: Number of rows to return (default: 20, max: 500). Use 5-20 for quick overviews,
                   50-200 for deeper analysis, up to 500 for comprehensive reports. For bulk exports
                   beyond 500 rows, use get_advanced_search_analytics which supports pagination.
    """
    try:
        service = get_gsc_service()
        
        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]
        
        # Build request
        request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": dimension_list,
            "rowLimit": min(max(1, row_limit), 500),
            "dataState": DATA_STATE
        }
        
        # Execute request
        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()
        
        if not response.get("rows"):
            return f"No search analytics data found for {site_url} in the last {days} days."

        rows = []
        for row in response.get("rows", []):
            entry = {}
            for i, dim in enumerate(dimension_list):
                entry[dim] = row.get("keys", [])[i] if i < len(row.get("keys", [])) else None
            entry["clicks"] = row.get("clicks", 0)
            entry["impressions"] = row.get("impressions", 0)
            entry["ctr"] = round(row.get("ctr", 0), 4)
            entry["position"] = round(row.get("position", 0), 1)
            rows.append(entry)

        return json.dumps({
            "site_url": site_url,
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
                "days": days,
            },
            "dimensions": dimension_list,
            "row_count": len(rows),
            "rows": rows,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving search analytics: {str(e)}"

@mcp.tool()
async def get_site_details(site_url: str) -> str:
    """
    Get detailed information about a specific Search Console property.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
    """
    try:
        service = get_gsc_service()
        
        # Get site details
        site_info = service.sites().get(siteUrl=site_url).execute()
        
        result = {
            "site_url": site_url,
            "permission_level": site_info.get("permissionLevel", "Unknown"),
        }

        if "siteVerificationInfo" in site_info:
            verify_info = site_info["siteVerificationInfo"]
            result["verification"] = {
                "state": verify_info.get("verificationState", "Unknown"),
                "verified_user": verify_info.get("verifiedUser"),
                "method": verify_info.get("verificationMethod"),
            }

        if "ownershipInfo" in site_info:
            owner_info = site_info["ownershipInfo"]
            result["ownership"] = {
                "owner": owner_info.get("owner", "Unknown"),
                "verification_method": owner_info.get("verificationMethod"),
            }

        return json.dumps(result)
    except Exception as e:
        return f"Error retrieving site details: {str(e)}"

@mcp.tool()
async def get_sitemaps(site_url: str) -> str:
    """
    List all sitemaps for a specific Search Console property.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
    """
    try:
        service = get_gsc_service()
        
        # Get sitemaps list
        sitemaps = service.sitemaps().list(siteUrl=site_url).execute()
        
        if not sitemaps.get("sitemap"):
            return f"No sitemaps found for {site_url}."

        sitemap_list = []
        for sitemap in sitemaps.get("sitemap", []):
            last_downloaded = sitemap.get("lastDownloaded")
            if last_downloaded:
                try:
                    dt = datetime.fromisoformat(last_downloaded.replace("Z", "+00:00"))
                    last_downloaded = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            errors = int(sitemap.get("errors", 0))
            warnings = int(sitemap.get("warnings", 0))

            status = "Valid"
            if errors > 0:
                status = "Has errors"
            elif warnings > 0:
                status = "Has warnings"

            indexed_urls = None
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        indexed_urls = content.get("submitted")
                        break

            sitemap_list.append({
                "path": sitemap.get("path", "Unknown"),
                "last_downloaded": last_downloaded,
                "status": status,
                "indexed_urls": indexed_urls,
                "errors": errors,
                "warnings": warnings,
            })

        return json.dumps({
            "site_url": site_url,
            "count": len(sitemap_list),
            "sitemaps": sitemap_list,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving sitemaps: {str(e)}"

def _rich_results_summary(inspection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Shape the richResultsResult of a URL inspection into a reportable summary.

    Shared by inspect_url_enhanced and batch_url_inspection so both tools report the
    same thing for the same page.

    Issues are nested under detectedItems[].items[].issues[] and the message key is
    "issueMessage". Reading a top-level "richResultsIssues" key -- which the API never
    returns -- made this list always empty, hiding every rich-result problem.
    """
    if "richResultsResult" not in inspection:
        return None

    rich = inspection["richResultsResult"]
    return {
        "verdict": rich.get("verdict", "UNKNOWN"),
        "detected_types": [
            item.get("richResultType", "Unknown")
            for item in rich.get("detectedItems", [])
        ],
        "issues": [
            {
                "type": detected.get("richResultType", "Unknown"),
                "item": item.get("name"),
                "severity": issue.get("severity"),
                "message": issue.get("issueMessage"),
            }
            for detected in rich.get("detectedItems", [])
            for item in detected.get("items", [])
            for issue in item.get("issues", [])
        ],
    }

@mcp.tool()
async def inspect_url_enhanced(site_url: str, page_url: str) -> str:
    """
    Enhanced URL inspection to check indexing status and rich results in Google.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        page_url: The specific URL to inspect
    """
    try:
        service = get_gsc_service()
        
        # Build request
        request = {
            "inspectionUrl": page_url,
            "siteUrl": site_url
        }
        
        # Execute request
        response = service.urlInspection().index().inspect(body=request).execute()
        
        if not response or "inspectionResult" not in response:
            return f"No inspection data found for {page_url}."

        inspection = response["inspectionResult"]
        index_status = inspection.get("indexStatusResult", {})

        last_crawled = None
        if "lastCrawlTime" in index_status:
            try:
                crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace("Z", "+00:00"))
                last_crawled = crawl_time.strftime("%Y-%m-%d %H:%M")
            except Exception:
                last_crawled = index_status["lastCrawlTime"]

        rich_results = _rich_results_summary(inspection)

        return json.dumps({
            "page_url": page_url,
            "site_url": site_url,
            "inspection_result_link": inspection.get("inspectionResultLink"),
            "verdict": index_status.get("verdict", "UNKNOWN"),
            "coverage_state": index_status.get("coverageState"),
            "last_crawled": last_crawled,
            "page_fetch_state": index_status.get("pageFetchState"),
            "robots_txt_state": index_status.get("robotsTxtState"),
            "indexing_state": index_status.get("indexingState"),
            "google_canonical": index_status.get("googleCanonical"),
            "user_canonical": index_status.get("userCanonical"),
            "crawled_as": index_status.get("crawledAs"),
            "referring_urls": index_status.get("referringUrls", [])[:5],
            "rich_results": rich_results,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error inspecting URL: {str(e)}"

# Max URLs inspected concurrently in batch_url_inspection. The URL Inspection API
# allows 600 queries/minute per site, so 10 in-flight is comfortably within limits.
_BATCH_INSPECTION_CONCURRENCY = 10


def _inspect_single_url(site_url: str, page_url: str) -> Dict[str, Any]:
    """Inspect one URL and shape the result into a row.

    Builds its own Search Console service so it is safe to run in a worker thread —
    google-api-python-client's underlying http transport is not thread-safe to share
    across threads. Never raises: failures are returned as an ``error`` row so one bad
    URL doesn't sink the whole batch.
    """
    try:
        service = get_gsc_service()
        request = {"inspectionUrl": page_url, "siteUrl": site_url}
        response = service.urlInspection().index().inspect(body=request).execute()

        if not response or "inspectionResult" not in response:
            return {"url": page_url, "error": "No inspection data found"}

        inspection = response["inspectionResult"]
        index_status = inspection.get("indexStatusResult", {})

        verdict = index_status.get("verdict", "UNKNOWN")
        coverage = index_status.get("coverageState", "Unknown")
        last_crawl = "Never"

        if "lastCrawlTime" in index_status:
            try:
                crawl_time = datetime.fromisoformat(index_status["lastCrawlTime"].replace('Z', '+00:00'))
                last_crawl = crawl_time.strftime('%Y-%m-%d')
            except Exception:
                last_crawl = index_status["lastCrawlTime"]

        # Rich results, in the same shape inspect_url_enhanced reports.
        return {
            "url": page_url,
            "index_verdict": verdict,
            "coverage_state": coverage,
            "last_crawled": last_crawl,
            "rich_results": _rich_results_summary(inspection),
        }
    except Exception as e:
        return {"url": page_url, "error": str(e)}


@mcp.tool()
async def batch_url_inspection(site_url: str, urls: str) -> str:
    """
    Inspect multiple URLs in batch (within API limits).

    URLs are inspected concurrently, so a full batch stays well under the MCP
    client's request timeout even on slower sc-domain:* properties.

    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        urls: List of URLs to inspect, one per line
    """
    try:
        # Validate auth once up front. This also refreshes the OAuth token a single
        # time so the concurrent workers below reuse it without racing on token.json.
        get_gsc_service()

        # Parse URLs
        url_list = [url.strip() for url in urls.split('\n') if url.strip()]

        if not url_list:
            return "No URLs provided for inspection."

        if len(url_list) > 10:
            return f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."

        # Inspect concurrently. The URL Inspection API is slow (especially on
        # sc-domain:* properties); running 10 URLs sequentially blew past the MCP
        # client's default 60s timeout (#31). google-api-python-client is blocking,
        # so each call runs in a worker thread with its own service instance.
        semaphore = asyncio.Semaphore(_BATCH_INSPECTION_CONCURRENCY)

        async def _run(page_url: str) -> Dict[str, Any]:
            async with semaphore:
                return await asyncio.to_thread(_inspect_single_url, site_url, page_url)

        # asyncio.gather preserves the order of url_list.
        results = await asyncio.gather(*[_run(u) for u in url_list])

        return json.dumps({
            "site_url": site_url,
            "count": len(results),
            "results": list(results),
        })

    except Exception as e:
        return f"Error performing batch inspection: {str(e)}"

def _check_indexing_single_url(site_url: str, page_url: str) -> Dict[str, Optional[str]]:
    """Inspect one URL and classify it into indexing-issue buckets.

    Builds its own service so it is safe to run in a worker thread. Never raises:
    failures are recorded under ``not_indexed``. Returns one string (or None) per
    category so the caller can aggregate results in input order.
    """
    result: Dict[str, Optional[str]] = {
        "not_indexed": None,
        "canonical_issues": None,
        "robots_blocked": None,
        "fetch_issues": None,
        "indexed": None,
    }
    try:
        service = get_gsc_service()
        request = {"inspectionUrl": page_url, "siteUrl": site_url}
        response = service.urlInspection().index().inspect(body=request).execute()

        if not response or "inspectionResult" not in response:
            result["not_indexed"] = f"{page_url} - No inspection data found"
            return result

        inspection = response["inspectionResult"]
        index_status = inspection.get("indexStatusResult", {})

        verdict = index_status.get("verdict", "UNKNOWN")
        coverage = index_status.get("coverageState", "Unknown")

        if verdict != "PASS" or "not indexed" in coverage.lower() or "excluded" in coverage.lower():
            result["not_indexed"] = f"{page_url} - {coverage}"
        else:
            result["indexed"] = page_url

        google_canonical = index_status.get("googleCanonical", "")
        user_canonical = index_status.get("userCanonical", "")
        if google_canonical and user_canonical and google_canonical != user_canonical:
            result["canonical_issues"] = (
                f"{page_url} - Google chose: {google_canonical} instead of user-declared: {user_canonical}"
            )

        robots_state = index_status.get("robotsTxtState", "")
        if robots_state == "BLOCKED":
            result["robots_blocked"] = page_url

        fetch_state = index_status.get("pageFetchState", "")
        if fetch_state != "SUCCESSFUL":
            result["fetch_issues"] = f"{page_url} - {fetch_state}"
    except Exception as e:
        result["not_indexed"] = f"{page_url} - Error: {str(e)}"
    return result


@mcp.tool()
async def check_indexing_issues(site_url: str, urls: str) -> str:
    """
    Check for specific indexing issues across multiple URLs.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        urls: List of URLs to check, one per line
    """
    try:
        # Validate auth once up front (also refreshes the token so the concurrent
        # workers below reuse it without racing on token.json).
        get_gsc_service()

        # Parse URLs
        url_list = [url.strip() for url in urls.split('\n') if url.strip()]

        if not url_list:
            return "No URLs provided for inspection."

        if len(url_list) > 10:
            return f"Too many URLs provided ({len(url_list)}). Please limit to 10 URLs per batch to avoid API quota issues."

        # Inspect concurrently — same reasoning as batch_url_inspection (#31): the
        # sequential loop timed out on sc-domain:* properties near the 10-URL cap.
        semaphore = asyncio.Semaphore(_BATCH_INSPECTION_CONCURRENCY)

        async def _run(page_url: str) -> Dict[str, Optional[str]]:
            async with semaphore:
                return await asyncio.to_thread(_check_indexing_single_url, site_url, page_url)

        # asyncio.gather preserves url_list order.
        per_url = await asyncio.gather(*[_run(u) for u in url_list])

        # Aggregate the per-URL classifications by category, in input order.
        issues_summary = {
            "not_indexed": [],
            "canonical_issues": [],
            "robots_blocked": [],
            "fetch_issues": [],
            "indexed": []
        }
        for entry in per_url:
            for category, value in entry.items():
                if value is not None:
                    issues_summary[category].append(value)

        return json.dumps({
            "site_url": site_url,
            "summary": {
                "total_checked": len(url_list),
                "indexed": len(issues_summary["indexed"]),
                "not_indexed": len(issues_summary["not_indexed"]),
                "canonical_issues": len(issues_summary["canonical_issues"]),
                "robots_blocked": len(issues_summary["robots_blocked"]),
                "fetch_issues": len(issues_summary["fetch_issues"]),
            },
            "issues": {
                "not_indexed": issues_summary["not_indexed"],
                "canonical_issues": issues_summary["canonical_issues"],
                "robots_blocked": issues_summary["robots_blocked"],
                "fetch_issues": issues_summary["fetch_issues"],
            },
            "indexed_urls": issues_summary["indexed"],
        })

    except Exception as e:
        return f"Error checking indexing issues: {str(e)}"

@mcp.tool()
async def get_performance_overview(site_url: str, days: int = 28) -> str:
    """
    Get a performance overview for a specific property.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        days: Number of days to look back (default: 28)
    """
    try:
        service = get_gsc_service()
        
        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        # Get total metrics
        total_request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": [],  # No dimensions for totals
            "rowLimit": 1,
            "dataState": DATA_STATE
        }
        
        total_response = service.searchanalytics().query(siteUrl=site_url, body=total_request).execute()
        
        # Get by date for trend
        date_request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": ["date"],
            "rowLimit": days,
            "dataState": DATA_STATE
        }
        
        date_response = service.searchanalytics().query(siteUrl=site_url, body=date_request).execute()
        
        if not total_response.get("rows"):
            return f"No performance data available for {site_url} in the last {days} days."

        totals_row = total_response["rows"][0]
        totals = {
            "clicks": totals_row.get("clicks", 0),
            "impressions": totals_row.get("impressions", 0),
            "ctr": round(totals_row.get("ctr", 0), 4),
            "position": round(totals_row.get("position", 0), 1),
        }

        daily_trend = []
        if date_response.get("rows"):
            sorted_rows = sorted(date_response["rows"], key=lambda x: x["keys"][0])
            for row in sorted_rows:
                daily_trend.append({
                    "date": row["keys"][0],
                    "clicks": row.get("clicks", 0),
                    "impressions": row.get("impressions", 0),
                    "ctr": round(row.get("ctr", 0), 4),
                    "position": round(row.get("position", 0), 1),
                })

        return json.dumps({
            "site_url": site_url,
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
                "days": days,
            },
            "totals": totals,
            "daily_trend": daily_trend,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving performance overview: {str(e)}"

@mcp.tool()
async def get_advanced_search_analytics(
    site_url: str, 
    start_date: str = None, 
    end_date: str = None, 
    dimensions: str = "query", 
    search_type: str = "WEB",
    row_limit: int = 1000,
    start_row: int = 0,
    sort_by: str = "clicks",
    sort_direction: str = "descending",
    filter_dimension: str = None,
    filter_operator: str = "contains", 
    filter_expression: str = None,
    filters: str = None,
    data_state: str = None
) -> str:
    """
    Get advanced search analytics data with sorting, filtering, and pagination.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        start_date: Start date in YYYY-MM-DD format (defaults to 28 days ago)
        end_date: End date in YYYY-MM-DD format (defaults to today)
        dimensions: Dimensions to group by, comma-separated (e.g., "query,page,device")
        search_type: Type of search results (WEB, IMAGE, VIDEO, NEWS, DISCOVER)
        row_limit: Maximum number of rows to return (max 25000)
        start_row: Starting row for pagination
        sort_by: Metric to sort by (clicks, impressions, ctr, position). Applied
                 client-side to the returned rows, since Google only sorts by clicks.
        sort_direction: Sort direction (ascending or descending). For position,
                 ascending puts the best (lowest) average position first.
        filter_dimension: Single filter dimension (query, page, country, device). Use 'filters' instead for multiple filters.
        filter_operator: Single filter operator (contains, equals, notContains, notEquals)
        filter_expression: Single filter expression value
        filters: JSON array of filter objects for AND logic across multiple dimensions. Overrides
                 filter_dimension/filter_operator/filter_expression when provided. Each object must
                 have 'dimension', 'operator', and 'expression' keys. Valid dimensions: query, page,
                 country, device. Valid operators: contains, equals, notContains, notEquals.
                 Example: [{"dimension":"country","operator":"equals","expression":"usa"},
                           {"dimension":"device","operator":"equals","expression":"MOBILE"}]
        data_state: Data freshness — "all" (default, matches GSC dashboard) or "final" (confirmed data only, 2-3 day lag)
    """
    try:
        service = get_gsc_service()
        
        # Calculate date range if not provided
        if not end_date:
            end_date = datetime.now().date().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now().date() - timedelta(days=28)).strftime("%Y-%m-%d")
        
        # Resolve and validate data_state (per-call override or fall back to global setting)
        resolved_data_state = (data_state or DATA_STATE).lower().strip()
        if resolved_data_state not in ("all", "final"):
            return (
                f"Invalid data_state value '{data_state}'. "
                "Accepted values are 'all' (matches GSC dashboard) or 'final' (2-3 day lag)."
            )
        
        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]
        
        # Build request
        request = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimension_list,
            "rowLimit": min(row_limit, 25000),  # Cap at API maximum
            "startRow": start_row,
            "searchType": search_type.upper(),
            "dataState": resolved_data_state
        }
        
        # Sorting is applied client-side below. The Search Analytics query API has no
        # orderBy field (it always returns rows sorted by clicks descending), so an
        # orderBy in the request body was silently ignored (#54).
        
        # Build filter groups — multi-filter JSON takes priority over single-filter params
        active_filters = []
        if filters:
            try:
                filter_list = json.loads(filters)
            except json.JSONDecodeError:
                return "Invalid filters JSON. Please provide a valid JSON array of filter objects."
            if not isinstance(filter_list, list) or len(filter_list) == 0:
                return "Invalid filters value. Expected a non-empty JSON array of filter objects."
            for f in filter_list:
                if not all(k in f for k in ("dimension", "operator", "expression")):
                    return (
                        "Each filter object must have 'dimension', 'operator', and 'expression' keys. "
                        f"Invalid filter: {f}"
                    )
            request["dimensionFilterGroups"] = [{"filters": filter_list}]
            active_filters = filter_list
        elif filter_dimension and filter_expression:
            single_filter = {
                "dimension": filter_dimension,
                "operator": filter_operator,
                "expression": filter_expression
            }
            request["dimensionFilterGroups"] = [{"filters": [single_filter]}]
            active_filters = [single_filter]
        
        # Execute request
        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()
        
        if not response.get("rows"):
            no_data_msg = (
                f"No search analytics data found for {site_url} with the specified parameters.\n\n"
                f"Parameters used:\n"
                f"- Date range: {start_date} to {end_date}\n"
                f"- Dimensions: {dimensions}\n"
                f"- Search type: {search_type}\n"
            )
            if active_filters:
                no_data_msg += "- Filters:\n"
                for f in active_filters:
                    no_data_msg += f"    {f['dimension']} {f['operator']} '{f['expression']}'\n"
            else:
                no_data_msg += "- No filter applied\n"
            return no_data_msg
        
        rows = []
        for row in response.get("rows", []):
            entry = {}
            for i, dim in enumerate(dimension_list):
                entry[dim] = row.get("keys", [])[i] if i < len(row.get("keys", [])) else None
            entry["clicks"] = row.get("clicks", 0)
            entry["impressions"] = row.get("impressions", 0)
            entry["ctr"] = round(row.get("ctr", 0), 4)
            entry["position"] = round(row.get("position", 0), 1)
            rows.append(entry)

        # Sort the returned rows client-side. Google only sorts by clicks descending
        # server-side, so without this the sort_by/sort_direction args were silently
        # ignored (#54). Sorting applies within the returned page of rows.
        valid_sort_metrics = ("clicks", "impressions", "ctr", "position")
        if sort_by in valid_sort_metrics:
            reverse = sort_direction.lower() != "ascending"
            rows.sort(key=lambda r: r.get(sort_by, 0), reverse=reverse)

        has_more = len(response.get("rows", [])) == row_limit
        return json.dumps({
            "site_url": site_url,
            "date_range": {"start": start_date, "end": end_date},
            "search_type": search_type,
            "dimensions": dimension_list,
            "filters_applied": active_filters,
            "pagination": {
                "start_row": start_row,
                "row_count": len(rows),
                "has_more": has_more,
                "next_start_row": start_row + row_limit if has_more else None,
            },
            "rows": rows,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving advanced search analytics: {str(e)}"

@mcp.tool()
async def compare_search_periods(
    site_url: str,
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    dimensions: str = "query",
    limit: int = 10
) -> str:
    """
    Compare search analytics data between two time periods.

    Direction: Period 1 is the period you are analyzing and Period 2 is the baseline
    you compare it against. All deltas are Period 1 relative to Period 2, and the
    percentage baseline is Period 2. A positive value means Period 1 outperformed
    Period 2 — more clicks/impressions, higher CTR, or a better (lower) average
    position. For a current-vs-previous comparison, pass the current period as
    Period 1 and the previous period as Period 2. Percentages are null when the
    Period 2 baseline is zero.

    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        period1_start: Start date for period 1 / the analyzed period (YYYY-MM-DD)
        period1_end: End date for period 1 / the analyzed period (YYYY-MM-DD)
        period2_start: Start date for period 2 / the baseline period (YYYY-MM-DD)
        period2_end: End date for period 2 / the baseline period (YYYY-MM-DD)
        dimensions: Dimensions to group by (default: query)
        limit: Number of top results to compare (default: 10)
    """
    try:
        service = get_gsc_service()
        
        # Parse dimensions
        dimension_list = [d.strip() for d in dimensions.split(",")]
        
        # Build requests for both periods
        period1_request = {
            "startDate": period1_start,
            "endDate": period1_end,
            "dimensions": dimension_list,
            "rowLimit": 1000,  # Get more to ensure we can match items between periods
            "dataState": DATA_STATE
        }
        
        period2_request = {
            "startDate": period2_start,
            "endDate": period2_end,
            "dimensions": dimension_list,
            "rowLimit": 1000,
            "dataState": DATA_STATE
        }
        
        # Execute requests
        period1_response = service.searchanalytics().query(siteUrl=site_url, body=period1_request).execute()
        period2_response = service.searchanalytics().query(siteUrl=site_url, body=period2_request).execute()
        
        period1_rows = period1_response.get("rows", [])
        period2_rows = period2_response.get("rows", [])
        
        if not period1_rows and not period2_rows:
            return f"No data found for either period for {site_url}."
        
        # Create dictionaries for easy lookup
        period1_data = {tuple(row.get("keys", [])): row for row in period1_rows}
        period2_data = {tuple(row.get("keys", [])): row for row in period2_rows}
        
        # Find common keys and calculate differences
        all_keys = set(period1_data.keys()) | set(period2_data.keys())
        comparison_data = []
        
        for key in all_keys:
            p1_row = period1_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            p2_row = period2_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            
            # Differences are Period 1 relative to Period 2, with Period 2 as the
            # baseline. A positive value always means Period 1 outperformed Period 2
            # (more clicks/impressions, higher CTR, or a better — i.e. lower — position).
            click_diff = p1_row.get("clicks", 0) - p2_row.get("clicks", 0)
            click_pct = (click_diff / p2_row.get("clicks", 1)) * 100 if p2_row.get("clicks", 0) > 0 else float('inf')
            
            imp_diff = p1_row.get("impressions", 0) - p2_row.get("impressions", 0)
            imp_pct = (imp_diff / p2_row.get("impressions", 1)) * 100 if p2_row.get("impressions", 0) > 0 else float('inf')
            
            ctr_diff = p1_row.get("ctr", 0) - p2_row.get("ctr", 0)
            pos_diff = p2_row.get("position", 0) - p1_row.get("position", 0)  # lower position is better, so positive = Period 1 improved
            
            comparison_data.append({
                "key": key,
                "p1_clicks": p1_row.get("clicks", 0),
                "p2_clicks": p2_row.get("clicks", 0),
                "click_diff": click_diff,
                "click_pct": click_pct,
                "p1_impressions": p1_row.get("impressions", 0),
                "p2_impressions": p2_row.get("impressions", 0),
                "imp_diff": imp_diff,
                "imp_pct": imp_pct,
                "p1_ctr": p1_row.get("ctr", 0),
                "p2_ctr": p2_row.get("ctr", 0),
                "ctr_diff": ctr_diff,
                "p1_position": p1_row.get("position", 0),
                "p2_position": p2_row.get("position", 0),
                "pos_diff": pos_diff
            })
        
        # Sort by absolute click difference
        comparison_data.sort(key=lambda x: abs(x["click_diff"]), reverse=True)

        serialisable = []
        for item in comparison_data[:limit]:
            click_pct = item["click_pct"] if item["click_pct"] != float("inf") else None
            imp_pct = item["imp_pct"] if item["imp_pct"] != float("inf") else None
            serialisable.append({
                "key": list(item["key"]),
                "p1_clicks": item["p1_clicks"],
                "p2_clicks": item["p2_clicks"],
                "click_diff": item["click_diff"],
                "click_pct": round(click_pct, 1) if click_pct is not None else None,
                "p1_impressions": item["p1_impressions"],
                "p2_impressions": item["p2_impressions"],
                "imp_diff": item["imp_diff"],
                "imp_pct": round(imp_pct, 1) if imp_pct is not None else None,
                "p1_ctr": round(item["p1_ctr"], 4),
                "p2_ctr": round(item["p2_ctr"], 4),
                "ctr_diff": round(item["ctr_diff"], 4),
                "p1_position": round(item["p1_position"], 1),
                "p2_position": round(item["p2_position"], 1),
                "position_diff": round(item["pos_diff"], 1),
            })

        return json.dumps({
            "site_url": site_url,
            "period1": {"start": period1_start, "end": period1_end},
            "period2": {"start": period2_start, "end": period2_end},
            "dimensions": dimension_list,
            "total_items": len(comparison_data),
            "showing": len(serialisable),
            "comparison": serialisable,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error comparing search periods: {str(e)}"

@mcp.tool()
async def get_search_by_page_query(
    site_url: str,
    page_url: str,
    days: int = 28,
    row_limit: int = 20
) -> str:
    """
    Get search analytics data for a specific page, broken down by query.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        page_url: The specific page URL to analyze
        days: Number of days to look back (default: 28)
        row_limit: Number of rows to return (default: 20, max: 500). Use 5-20 for quick overviews,
                   50-200 for deeper analysis, up to 500 for comprehensive reports. For bulk exports
                   beyond 500 rows, use get_advanced_search_analytics which supports pagination.
    """
    try:
        service = get_gsc_service()
        
        # Calculate date range
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        # Build request with page filter
        request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": ["query"],
            "dimensionFilterGroups": [{
                "filters": [{
                    "dimension": "page",
                    "operator": "equals",
                    "expression": page_url
                }]
            }],
            "rowLimit": min(max(1, row_limit), 500),
            # Note: the Search Analytics query API has no orderBy field — it always
            # returns rows sorted by clicks descending (which is what we want here).
            "dataState": DATA_STATE
        }
        
        # Execute request
        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()
        
        if not response.get("rows"):
            return f"No search data found for page {page_url} in the last {days} days."
        
        rows = []
        for row in response.get("rows", []):
            rows.append({
                "query": row.get("keys", ["Unknown"])[0],
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": round(row.get("ctr", 0), 4),
                "position": round(row.get("position", 0), 1),
            })

        total_clicks = sum(r["clicks"] for r in rows)
        total_impressions = sum(r["impressions"] for r in rows)

        return json.dumps({
            "site_url": site_url,
            "page_url": page_url,
            "date_range": {
                "start": start_date.strftime("%Y-%m-%d"),
                "end": end_date.strftime("%Y-%m-%d"),
                "days": days,
            },
            "totals": {
                "clicks": total_clicks,
                "impressions": total_impressions,
                "avg_ctr": round(total_clicks / total_impressions, 4) if total_impressions > 0 else 0,
            },
            "row_count": len(rows),
            "rows": rows,
        })
    except Exception as e:
        return f"Error retrieving page query data: {str(e)}"

@mcp.tool()
async def list_sitemaps_enhanced(site_url: str, sitemap_index: str = None) -> str:
    """
    List all sitemaps for a specific Search Console property with detailed information.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        sitemap_index: Optional sitemap index URL to list child sitemaps
    """
    try:
        service = get_gsc_service()
        
        # Get sitemaps list
        if sitemap_index:
            sitemaps = service.sitemaps().list(siteUrl=site_url, sitemapIndex=sitemap_index).execute()
            source = f"child sitemaps from index: {sitemap_index}"
        else:
            sitemaps = service.sitemaps().list(siteUrl=site_url).execute()
            source = "all submitted sitemaps"
        
        if not sitemaps.get("sitemap"):
            return f"No sitemaps found for {site_url}" + (f" in index {sitemap_index}" if sitemap_index else ".")

        def _fmt_date(raw):
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return raw

        sitemap_list = []
        for sitemap in sitemaps.get("sitemap", []):
            errors = int(sitemap.get("errors", 0))
            warnings = int(sitemap.get("warnings", 0))
            url_count = None
            if "contents" in sitemap:
                for content in sitemap["contents"]:
                    if content.get("type") == "web":
                        url_count = content.get("submitted")
                        break
            sitemap_list.append({
                "path": sitemap.get("path", "Unknown"),
                "last_submitted": _fmt_date(sitemap.get("lastSubmitted")),
                "last_downloaded": _fmt_date(sitemap.get("lastDownloaded")),
                "type": "Index" if sitemap.get("isSitemapsIndex", False) else "Sitemap",
                "is_pending": sitemap.get("isPending", False),
                "url_count": url_count,
                "errors": errors,
                "warnings": warnings,
            })

        pending_count = sum(1 for s in sitemap_list if s["is_pending"])

        return json.dumps({
            "site_url": site_url,
            "sitemap_index": sitemap_index,
            "count": len(sitemap_list),
            "pending_count": pending_count,
            "sitemaps": sitemap_list,
        })
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving sitemaps: {str(e)}"

@mcp.tool()
async def get_sitemap_details(site_url: str, sitemap_url: str) -> str:
    """
    Get detailed information about a specific sitemap.
    
    Args:
        site_url: Exact GSC property URL from list_properties (e.g. "https://example.com/" or
                  "sc-domain:example.com"). Domain properties cover all subdomains — use the
                  domain property as site_url and filter by page to analyze a specific subdomain.
        sitemap_url: The full URL of the sitemap to inspect
    """
    try:
        service = get_gsc_service()
        
        # Get sitemap details
        details = service.sitemaps().get(siteUrl=site_url, feedpath=sitemap_url).execute()
        
        if not details:
            return f"No details found for sitemap {sitemap_url}."

        def _fmt_date(raw):
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            except Exception:
                return raw

        is_index = details.get("isSitemapsIndex", False)
        content_breakdown = [
            {
                "type": content.get("type", "unknown").upper(),
                "submitted": content.get("submitted", 0),
                "indexed": content.get("indexed"),
            }
            for content in details.get("contents", [])
        ]

        return json.dumps({
            "sitemap_url": sitemap_url,
            "site_url": site_url,
            "type": "Index" if is_index else "Sitemap",
            "status": "pending" if details.get("isPending", False) else "processed",
            "last_submitted": _fmt_date(details.get("lastSubmitted")),
            "last_downloaded": _fmt_date(details.get("lastDownloaded")),
            "errors": int(details.get("errors", 0)),
            "warnings": int(details.get("warnings", 0)),
            "content_breakdown": content_breakdown,
            "is_index": is_index,
        })
    except Exception as e:
        return f"Error retrieving sitemap details: {str(e)}"

# ---------------------------------------------------------------------------
# PATCH(munder): Core Web Vitals (CrUX field data + optional PageSpeed lab data).
# Read-only. The API key lives in a FILE (PAGESPEED_KEY_PATH, default
# ~/.config/magenio/pagespeed.key), is sent in the X-Goog-Api-Key header (never in a URL),
# and is never returned, logged or echoed in an error.
# ---------------------------------------------------------------------------
import urllib.error
import urllib.request
from urllib.parse import urlparse

CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
DEFAULT_PAGESPEED_KEY_PATH = os.path.join("~", ".config", "magenio", "pagespeed.key")
_CWV_TIMEOUT_S = 30

# metric -> (label, good_max, poor_min); a p75 <= good_max is good, > poor_min is poor.
_CRUX_METRICS = {
    "largest_contentful_paint": ("LCP", 2500, 4000),
    "interaction_to_next_paint": ("INP", 200, 500),
    "cumulative_layout_shift": ("CLS", 0.1, 0.25),
    "first_contentful_paint": ("FCP", 1800, 3000),
    "experimental_time_to_first_byte": ("TTFB", 800, 1800),
}


class _CwvError(Exception):
    """An error message that is safe to show to the agent (never contains the key)."""


def _read_pagespeed_key() -> str:
    raw = os.environ.get("PAGESPEED_KEY_PATH") or DEFAULT_PAGESPEED_KEY_PATH
    path = os.path.expandvars(os.path.expanduser(raw))
    name = os.path.basename(path) or "pagespeed.key"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
    except FileNotFoundError:
        raise _CwvError(f"PageSpeed/CrUX API key file not found: {name}. Set PAGESPEED_KEY_PATH or create the file.")
    except OSError:
        raise _CwvError(f"PageSpeed/CrUX API key file could not be read: {name}.")
    if not key:
        raise _CwvError(f"PageSpeed/CrUX API key file is empty: {name}.")
    return key


def _cwv_http(method: str, url: str, key: str, body: Optional[dict] = None) -> dict:
    """One Google API call. Raises _CwvError (key-free) on failure; HTTP 404 -> _CwvNotFound."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Goog-Api-Key": key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_CWV_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _CwvNotFound()
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        detail = detail.replace(key, "***")
        hint = " (check the API key: valid, and CrUX/PageSpeed APIs enabled for it)" if e.code in (400, 401, 403) else ""
        raise _CwvError(f"Google API returned HTTP {e.code}{hint}: {detail[:300]}".rstrip(": "))
    except (urllib.error.URLError, TimeoutError, OSError):
        raise _CwvError("Could not reach the Google API (network error or timeout).")
    except ValueError:
        raise _CwvError("Google API returned an unreadable response.")


class _CwvNotFound(Exception):
    pass


def _rate(value: float, good_max: float, poor_min: float) -> str:
    return "good" if value <= good_max else ("poor" if value > poor_min else "needs improvement")


def _parse_crux(record: dict) -> dict:
    out = {}
    for name, (label, good_max, poor_min) in _CRUX_METRICS.items():
        m = (record.get("metrics") or {}).get(name)
        if not m:
            continue
        try:
            p75 = float((m.get("percentiles") or {})["p75"])
        except (KeyError, TypeError, ValueError):
            continue
        entry = {"p75": p75, "rating": _rate(p75, good_max, poor_min)}
        hist = m.get("histogram") or []
        if len(hist) == 3:
            entry["distribution"] = {k: round(float(h.get("density", 0)), 4) for k, h in zip(("good", "needs_improvement", "poor"), hist)}
        out[label] = entry
    return out


def _cwv_target(url_or_origin: str):
    p = urlparse((url_or_origin or "").strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        raise _CwvError("url_or_origin must be an absolute http(s) URL or origin, e.g. https://example.com or https://example.com/page")
    origin = f"{p.scheme}://{p.netloc}"
    is_page = p.path not in ("", "/") or bool(p.query)
    return origin, (url_or_origin.strip() if is_page else None)


def _crux_query(key: str, origin: str, page: Optional[str], form_factor: str) -> dict:
    base = {"metrics": list(_CRUX_METRICS)}
    if form_factor in ("PHONE", "DESKTOP"):
        base["formFactor"] = form_factor
    attempts = ([("url", page)] if page else []) + [("origin", origin)]
    for scope, target in attempts:
        try:
            rec = _cwv_http("POST", CRUX_ENDPOINT, key, {**base, scope: target}).get("record", {})
        except _CwvNotFound:
            continue
        return {"scope": scope, "target": target, "metrics": _parse_crux(rec), "collection_period": rec.get("collectionPeriod")}
    return {"scope": None, "metrics": {}, "note": "No CrUX field data for this URL or origin (not enough real-user traffic)."}


def _url_short(u: Any, n: int = 80) -> str:
    """URL for display: no query/fragment (may carry tokens), truncated."""
    u = str(u or "").split("?", 1)[0].split("#", 1)[0]
    return u if len(u) <= n else u[: n - 1] + "…"


def _items(audit: dict) -> list:
    return ((audit or {}).get("details") or {}).get("items") or []


def _node(it: dict) -> str:
    n = (it or {}).get("node") or {}
    return str(n.get("selector") or n.get("snippet") or "")[:120]


def _psi_diagnostics(audits: dict, cls_value: Optional[float]) -> dict:
    """Compact Lighthouse diagnostics from a PSI response (all parts optional)."""
    d: Dict[str, Any] = {}
    opps = []
    for a in audits.values():
        det = a.get("details") or {}
        if det.get("type") == "opportunity" and (det.get("overallSavingsMs") or 0) > 0:
            o = {"audit": a.get("title"), "saves_ms": round(det["overallSavingsMs"])}
            if det.get("overallSavingsBytes"):
                o["saves_kb"] = round(det["overallSavingsBytes"] / 1024)
            res = [_url_short(i.get("url")) for i in det.get("items", []) if i.get("url")][:3]
            if res:
                o["resources"] = res
            opps.append(o)
    if opps:
        d["opportunities"] = sorted(opps, key=lambda o: -o["saves_ms"])[:5]
    lcp = audits.get("largest-contentful-paint-element")
    if lcp:
        for blk in _items(lcp):
            rows = blk.get("items") or []
            if blk.get("type") == "table" and rows and "node" in rows[0]:
                d["lcp_element"] = _node(rows[0])
            elif blk.get("type") == "table" and rows and "phase" in rows[0]:
                d["lcp_phases_ms"] = {r["phase"]: round(r.get("timing", 0)) for r in rows if "phase" in r}
    boot = sorted(_items(audits.get("bootup-time")), key=lambda i: -(i.get("total") or 0))[:5]
    if boot:
        d["slow_scripts"] = [{"url": _url_short(i.get("url")), "total_ms": round(i.get("total") or 0), "scripting_ms": round(i.get("scripting") or 0)} for i in boot]
    mt = sorted(_items(audits.get("mainthread-work-breakdown")), key=lambda i: -(i.get("duration") or 0))[:5]
    if mt:
        d["main_thread_ms"] = {i.get("groupLabel") or i.get("group"): round(i.get("duration") or 0) for i in mt}
    tp = sorted(_items(audits.get("third-party-summary")), key=lambda i: -(i.get("blockingTime") or 0))[:5]
    if tp:
        d["third_parties"] = [{"entity": str(i.get("entity") if isinstance(i.get("entity"), str) else (i.get("entity") or {}).get("text", ""))[:60],
                               "blocking_ms": round(i.get("blockingTime") or 0), "transfer_kb": round((i.get("transferSize") or 0) / 1024)} for i in tp]
    if cls_value is not None and cls_value > 0.1:
        ls = [{"element": _node(i), "score": round(i.get("score") or 0, 3)} for i in _items(audits.get("layout-shift-elements"))][:5]
        if ls:
            d["layout_shift_elements"] = ls
    return d


def _psi_lab(key: str, page_url: str, strategy: str) -> dict:
    from urllib.parse import urlencode
    qs = urlencode({"url": page_url, "strategy": strategy, "category": "performance"})
    try:
        d = _cwv_http("GET", f"{PSI_ENDPOINT}?{qs}", key)
    except _CwvNotFound:
        return {"error": "PageSpeed could not analyse this URL."}
    lh = d.get("lighthouseResult", {})
    audits = lh.get("audits", {})
    pick = {"LCP": "largest-contentful-paint", "CLS": "cumulative-layout-shift", "TBT": "total-blocking-time",
            "FCP": "first-contentful-paint", "SpeedIndex": "speed-index"}
    res = {k: audits[a].get("displayValue") for k, a in pick.items() if a in audits}
    score = (lh.get("categories", {}).get("performance") or {}).get("score")
    if score is not None:
        res["performance_score"] = round(score * 100)
    cls = (audits.get("cumulative-layout-shift") or {}).get("numericValue")
    res.update(_psi_diagnostics(audits, cls))
    return res


@mcp.tool()
async def get_core_web_vitals(url_or_origin: str, form_factor: str = "ALL", include_lab: bool = False, lab_strategy: str = "both") -> str:
    """
    Core Web Vitals from real Chrome users (CrUX field data), optionally plus PageSpeed Insights lab data.

    Reports p75 LCP, INP, CLS, FCP and TTFB with a good / needs improvement / poor rating. A page URL is
    tried first and falls back to its origin when CrUX has no data for that page.

    Args:
        url_or_origin: Absolute URL (https://example.com/page) or origin (https://example.com).
        form_factor: PHONE, DESKTOP or ALL (default ALL).
        include_lab: Also run PageSpeed Insights (slower; mobile and desktop for ALL) on the page URL
                     (the origin when only an origin was given). Adds a compact Lighthouse diagnosis per strategy:
                     top opportunities (estimated saving, resources), LCP element and phases, slowest scripts,
                     main-thread breakdown, third parties, layout-shift elements when CLS > 0.1.
        lab_strategy: mobile, desktop or both (default). "both" follows form_factor (PHONE/DESKTOP narrow it);
                     mobile or desktop forces that single run.
    """
    try:
        ff = (form_factor or "ALL").upper()
        if ff not in ("PHONE", "DESKTOP", "ALL"):
            return "Error: form_factor must be PHONE, DESKTOP or ALL."
        origin, page = _cwv_target(url_or_origin)
        key = _read_pagespeed_key()
        field = await asyncio.to_thread(_crux_query, key, origin, page, ff)
        result: Dict[str, Any] = {"form_factor": ff, "field_data": field}
        if include_lab:
            lab_url = page or origin
            ls = (lab_strategy or "both").lower()
            if ls not in ("mobile", "desktop", "both"):
                return "Error: lab_strategy must be mobile, desktop or both."
            strategies = [ls] if ls != "both" else {"PHONE": ["mobile"], "DESKTOP": ["desktop"], "ALL": ["mobile", "desktop"]}[ff]
            result["lab_data"] = {"url": lab_url}
            for s in strategies:
                result["lab_data"][s] = await asyncio.to_thread(_psi_lab, key, lab_url, s)
        return json.dumps(result)
    except _CwvError as e:
        return f"Error: {e}"
    except Exception:
        # Never surface raw exception text here: it could carry request details.
        return "Error: unexpected failure while fetching Core Web Vitals."


# ---------------------------------------------------------------------------
# PATCH(munder): Google Tag Manager, READ-ONLY (API v2). Same service-account key as GSC
# (GSC_CREDENTIALS_PATH), but its own token with ONLY the tagmanager.readonly scope. Only
# list/get/live calls are made; nothing here can create, update, publish or delete.
# ---------------------------------------------------------------------------
GTM_SCOPES = ["https://www.googleapis.com/auth/tagmanager.readonly"]
_GTM_DEFAULT_LIMIT = 50
_GTM_MAX_LIMIT = 200
_GTM_NOISE = {"fingerprint", "path", "tagManagerUrl", "accountId", "containerId", "workspaceId",
              "containerVersionId", "tagId", "triggerId", "variableId", "clientId", "templateId",
              "folderId", "transformationId", "zoneId"}
# version content collections: key in the API response -> (label, id field)
_GTM_COLLECTIONS = {
    "tag": ("tags", "tagId"), "trigger": ("triggers", "triggerId"), "variable": ("variables", "variableId"),
    "client": ("clients", "clientId"), "customTemplate": ("templates", "templateId"),
    "transformation": ("transformations", "transformationId"), "folder": ("folders", "folderId"),
}


def get_gtm_service():
    """Authorised Tag Manager v2 service: the project's service-account key, tagmanager.readonly only."""
    if GSC_CREDENTIALS_PATH and not os.path.exists(GSC_CREDENTIALS_PATH):
        raise FileNotFoundError("GSC_CREDENTIALS_PATH is set but the file does not exist.")
    for cred_path in POSSIBLE_CREDENTIAL_PATHS:
        if cred_path and os.path.exists(cred_path):
            try:
                creds = service_account.Credentials.from_service_account_file(cred_path, scopes=GTM_SCOPES)
                return build("tagmanager", "v2", credentials=creds, cache_discovery=False)
            except Exception:
                continue
    raise FileNotFoundError("No usable service-account credentials for Tag Manager.")


def _gtm_err(e: Exception, what: str) -> str:
    """Key/path-free error text."""
    if isinstance(e, HttpError):
        status = getattr(e.resp, "status", None)
        if status == 403:
            return f"Error: the service account has no access to {what} (grant it Read access in Tag Manager, and enable the Tag Manager API)."
        if status == 404:
            return f"Error: {what} not found."
        if status == 429:
            return "Error: Tag Manager API quota exceeded, retry later."
        return f"Error: Tag Manager API returned HTTP {status} for {what}."
    if isinstance(e, FileNotFoundError):
        return "Error: Tag Manager credentials are not configured."
    return f"Error: unexpected failure while reading {what}."


def _gtm_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _GTM_DEFAULT_LIMIT
    return max(1, min(n, _GTM_MAX_LIMIT))


def _gtm_collect(request, key: str, limit: int, list_next) -> tuple:
    """Follow nextPageToken until `limit` items; returns (items, truncated)."""
    items: List[dict] = []
    resp = request.execute()
    while True:
        items.extend(resp.get(key, []))
        if len(items) >= limit:
            return items[:limit], len(items) > limit or bool(resp.get("nextPageToken"))
        request = list_next(request, resp)
        if request is None:
            return items, False
        resp = request.execute()


_GTM_SECRET_KEY = re.compile(r"secret|token|key|password|passwd|auth|credential|bearer", re.I)
_GTM_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _short(v: Any, n: int = 120) -> Any:
    """Compact display of a value; long token-like runs (keys pasted in Custom HTML/JS) are masked."""
    s = v if isinstance(v, str) else json.dumps(v, sort_keys=True)
    s = _GTM_TOKENISH.sub("***", s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _params(entity: dict) -> dict:
    """Flat key -> short value for simple (non-nested) parameters; secret-looking names show ***."""
    out = {}
    for p in entity.get("parameter", []) or []:
        if "key" in p and "value" in p:
            out[p["key"]] = "***" if _GTM_SECRET_KEY.search(p["key"]) else _short(p["value"])
    return out


def _brief(kind: str, e: dict) -> dict:
    idf = _GTM_COLLECTIONS.get(kind, (None, kind + "Id"))[1]
    b = {"id": e.get(idf), "name": e.get("name"), "type": e.get("type")}
    if kind == "tag":
        b["firing_triggers"] = e.get("firingTriggerId", [])
        if e.get("blockingTriggerId"):
            b["blocking_triggers"] = e["blockingTriggerId"]
        if e.get("paused"):
            b["paused"] = True
    if kind == "trigger" and e.get("filter"):
        b["conditions"] = len(e["filter"])
    pr = _params(e)
    if pr:
        b["params"] = dict(list(pr.items())[:8])
    return {k: v for k, v in b.items() if v not in (None, [], {})}


def _ws_path(account_id: str, container_id: str, workspace_id: str = "") -> str:
    base = f"accounts/{account_id}/containers/{container_id}"
    return f"{base}/workspaces/{workspace_id}" if workspace_id else base


def _gtm_ids(*vals: str) -> Optional[str]:
    for v in vals:
        if not re.fullmatch(r"[0-9]+", str(v)):
            return "Error: account_id, container_id, workspace_id and version ids must be numeric ids (see the gtm_list_* tools)."
    return None


@mcp.tool()
async def gtm_list_accounts() -> str:
    """List the Google Tag Manager accounts the service account can read."""
    try:
        svc = get_gtm_service()
        accounts, trunc = _gtm_collect(svc.accounts().list(), "account", _GTM_DEFAULT_LIMIT, lambda r, rs: svc.accounts().list_next(r, rs))
        return json.dumps({"accounts": [{"account_id": a.get("accountId"), "name": a.get("name")} for a in accounts], "truncated": trunc})
    except Exception as e:
        return _gtm_err(e, "Tag Manager accounts")


@mcp.tool()
async def gtm_list_containers(account_id: str) -> str:
    """List containers of a GTM account, with their type (web, server, ...) and public id (GTM-XXXX)."""
    bad = _gtm_ids(account_id)
    if bad:
        return bad
    try:
        svc = get_gtm_service()
        c = svc.accounts().containers()
        items, trunc = _gtm_collect(c.list(parent=f"accounts/{account_id}"), "container", _GTM_MAX_LIMIT, lambda r, rs: c.list_next(r, rs))
        return json.dumps({"containers": [{"container_id": x.get("containerId"), "name": x.get("name"),
                                           "public_id": x.get("publicId"), "type": x.get("usageContext", [])} for x in items], "truncated": trunc})
    except Exception as e:
        return _gtm_err(e, f"GTM account {account_id}")


@mcp.tool()
async def gtm_list_workspaces(account_id: str, container_id: str) -> str:
    """List the workspaces of a GTM container (needed for gtm_list_tags/triggers/variables)."""
    bad = _gtm_ids(account_id, container_id)
    if bad:
        return bad
    try:
        svc = get_gtm_service()
        w = svc.accounts().containers().workspaces()
        items, trunc = _gtm_collect(w.list(parent=_ws_path(account_id, container_id)), "workspace", _GTM_MAX_LIMIT, lambda r, rs: w.list_next(r, rs))
        return json.dumps({"workspaces": [{"workspace_id": x.get("workspaceId"), "name": x.get("name"),
                                           "description": _short(x.get("description", ""), 100)} for x in items], "truncated": trunc})
    except Exception as e:
        return _gtm_err(e, f"GTM container {container_id}")


def _version_summary(v: dict, limit: int) -> dict:
    out: Dict[str, Any] = {"version_id": v.get("containerVersionId"), "name": v.get("name"),
                           "container": {k: v.get("container", {}).get(k) for k in ("publicId", "name", "usageContext")}}
    counts, sections = {}, {}
    for kind, (label, _idf) in _GTM_COLLECTIONS.items():
        items = v.get(kind, []) or []
        counts[label] = len(items)
        if items:
            sections[label] = [_brief(kind, e) for e in items[:limit]]
            if len(items) > limit:
                sections[label + "_truncated"] = len(items) - limit
    out["counts"] = counts
    out.update(sections)
    return out


@mcp.tool()
async def gtm_get_live_version(account_id: str, container_id: str, limit: int = 50) -> str:
    """
    The live (published) version of a GTM container, synthesised: counts plus a compact line per
    tag / trigger / variable / client / template / folder (web and server containers).

    Args:
        limit: max entries listed per section (default 50, max 200); counts are always complete.
    """
    bad = _gtm_ids(account_id, container_id)
    if bad:
        return bad
    try:
        v = get_gtm_service().accounts().containers().versions().live(parent=_ws_path(account_id, container_id)).execute()
        return json.dumps(_version_summary(v, _gtm_limit(limit)))
    except Exception as e:
        return _gtm_err(e, f"GTM container {container_id}")


@mcp.tool()
async def gtm_list_versions(account_id: str, container_id: str, limit: int = 20) -> str:
    """Version headers of a GTM container (id, name, counts, deleted flag), newest first as returned by the API."""
    bad = _gtm_ids(account_id, container_id)
    if bad:
        return bad
    try:
        svc = get_gtm_service()
        vh = svc.accounts().containers().version_headers()
        items, trunc = _gtm_collect(vh.list(parent=_ws_path(account_id, container_id)), "containerVersionHeader", _gtm_limit(limit), lambda r, rs: vh.list_next(r, rs))
        keep = ("containerVersionId", "name", "numTags", "numTriggers", "numVariables", "numClients", "numCustomTemplates", "deleted")
        return json.dumps({"versions": [{k: x[k] for k in keep if k in x} for x in items], "truncated": trunc})
    except Exception as e:
        return _gtm_err(e, f"GTM container {container_id}")


def _get_version(svc, account_id: str, container_id: str, version: str) -> dict:
    vs = svc.accounts().containers().versions()
    base = _ws_path(account_id, container_id)
    if str(version).lower() == "live":
        return vs.live(parent=base).execute()
    return vs.get(path=f"{base}/versions/{version}").execute()


def _diff_entity(a: dict, b: dict) -> dict:
    changed = {}
    for k in sorted((set(a) | set(b)) - _GTM_NOISE):
        if a.get(k) != b.get(k):
            if k == "parameter":
                pa, pb = _params(a), _params(b)
                keys = sorted(k2 for k2 in set(pa) | set(pb) if pa.get(k2) != pb.get(k2))
                changed["parameter"] = {k2: {"from": pa.get(k2), "to": pb.get(k2)} for k2 in keys[:10]}
            else:
                changed[k] = {"from": _short(a.get(k)), "to": _short(b.get(k))}
    return changed


@mcp.tool()
async def gtm_diff_versions(account_id: str, container_id: str, version_a: str, version_b: str) -> str:
    """
    Compare two versions of a GTM container: tags, triggers, variables, clients, templates, folders
    added / removed / modified (with the changed fields). version_a is the base ("from"), version_b the
    target ("to"); either may be the word "live".
    """
    bad = _gtm_ids(account_id, container_id) or _gtm_ids(*[v for v in (version_a, version_b) if str(v).lower() != "live"])
    if bad:
        return bad
    try:
        svc = get_gtm_service()
        va, vb = _get_version(svc, account_id, container_id, version_a), _get_version(svc, account_id, container_id, version_b)
        out: Dict[str, Any] = {"from": va.get("containerVersionId"), "to": vb.get("containerVersionId")}
        for kind, (label, idf) in _GTM_COLLECTIONS.items():
            ma = {e.get(idf): e for e in va.get(kind, []) or []}
            mb = {e.get(idf): e for e in vb.get(kind, []) or []}
            added = [_brief(kind, mb[i]) for i in mb if i not in ma]
            removed = [_brief(kind, ma[i]) for i in ma if i not in mb]
            modified = []
            for i in ma:
                if i in mb:
                    ch = _diff_entity(ma[i], mb[i])
                    if ch:
                        modified.append({"id": i, "name": mb[i].get("name"), "changed": ch})
            if added or removed or modified:
                out[label] = {"added": added[:_GTM_DEFAULT_LIMIT], "removed": removed[:_GTM_DEFAULT_LIMIT], "modified": modified[:_GTM_DEFAULT_LIMIT]}
        if len(out) == 2:
            out["note"] = "No differences."
        return json.dumps(out)
    except Exception as e:
        return _gtm_err(e, f"GTM container {container_id}")


async def _gtm_workspace_list(kind: str, account_id: str, container_id: str, workspace_id: str, limit: int) -> str:
    bad = _gtm_ids(account_id, container_id, workspace_id)
    if bad:
        return bad
    try:
        svc = get_gtm_service()
        res = getattr(svc.accounts().containers().workspaces(), _GTM_COLLECTIONS[kind][0])()
        n = _gtm_limit(limit)
        items, trunc = _gtm_collect(res.list(parent=_ws_path(account_id, container_id, workspace_id)), kind, n, lambda r, rs: res.list_next(r, rs))
        return json.dumps({_GTM_COLLECTIONS[kind][0]: [_brief(kind, e) for e in items], "truncated": trunc})
    except Exception as e:
        return _gtm_err(e, f"GTM workspace {workspace_id}")


@mcp.tool()
async def gtm_list_tags(account_id: str, container_id: str, workspace_id: str, limit: int = 50) -> str:
    """Tags of a GTM workspace (id, name, type, firing/blocking trigger ids, paused, key parameters)."""
    return await _gtm_workspace_list("tag", account_id, container_id, workspace_id, limit)


@mcp.tool()
async def gtm_list_triggers(account_id: str, container_id: str, workspace_id: str, limit: int = 50) -> str:
    """Triggers of a GTM workspace (id, name, type, number of conditions)."""
    return await _gtm_workspace_list("trigger", account_id, container_id, workspace_id, limit)


@mcp.tool()
async def gtm_list_variables(account_id: str, container_id: str, workspace_id: str, limit: int = 50) -> str:
    """Variables of a GTM workspace (id, name, type, key parameters)."""
    return await _gtm_workspace_list("variable", account_id, container_id, workspace_id, limit)


@mcp.tool()
async def get_creator_info() -> str:
    """
    Provides information about Amin Foroutan, the creator of the MCP-GSC tool.
    """
    creator_info = """
# About the Creator: Amin Foroutan

Amin Foroutan is an SEO consultant with over a decade of experience, specializing in technical SEO, Python-driven tools, and data analysis for SEO performance.

## Connect with Amin:

- **LinkedIn**: [Amin Foroutan](https://www.linkedin.com/in/ma-foroutan/)
- **Personal Website**: [aminforoutan.com](https://aminforoutan.com/)
- **YouTube**: [Amin Forout](https://www.youtube.com/channel/UCW7tPXg-rWdH4YzLrcAdBIw)
- **X (Twitter)**: [@aminfseo](https://x.com/aminfseo)

## Notable Projects:

Amin has created several popular SEO tools including:
- Advanced GSC Visualizer (6.4K+ users)
- SEO Render Insight Tool (3.5K+ users)
- Google AI Overview Impact Analysis (1.2K+ users)
- Google AI Overview Citation Analysis (900+ users)
- SEMRush Enhancer (570+ users)
- SEO Page Inspector (115+ users)

## Expertise:

Amin combines technical SEO knowledge with programming skills to create innovative solutions for SEO challenges.
"""
    return creator_info

def main():
    """Entry point for the MCP server. Supports stdio (default) and SSE transports."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MCP_PORT", "3001"))
    except ValueError:
        raise ValueError("MCP_PORT must be an integer")

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport in {"sse", "http"}:
        # mcp SDK >= 1.27 removed the host/port kwargs from run() (they must be
        # set on mcp.settings instead) and enabled DNS-rebinding protection with
        # a Host allowlist of localhost only — which 421s the remote/Docker
        # binding the operator explicitly opted into via MCP_HOST. Disable that
        # check for the documented remote path. See issues #30 and #33.
        mcp.settings.host = host
        mcp.settings.port = port
        try:
            mcp.settings.transport_security.enable_dns_rebinding_protection = False
        except Exception:
            pass
        mcp.run(transport="sse")
    else:
        raise ValueError(
            f"Unknown MCP_TRANSPORT '{transport}'. "
            "Use 'stdio' (default) or 'sse'."
        )


if __name__ == "__main__":
    main()
