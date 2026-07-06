"""MCP server exposing the SF Supplies product search (Typesense-backed website
API) to an LLM tool loop.

It wraps the website's results/filtering endpoint
(POST /api/Products/Search/v2) as two tools:

  * search_products      — run a search, optionally filtered/sorted/paged
  * list_product_filters — discover the facets (filterable attributes + values)
                           available for a query, so the model can refine

Transport mirrors the MSSQL MCP server: stdio by default (local, e.g. Claude
Desktop) or bearer-auth Streamable-HTTP for a shared LAN deployment.
"""
import asyncio
import html
import json
import logging
import os

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("product_search_mcp_server")

# --- Configuration ------------------------------------------------------------

def get_search_url() -> str:
    """Results/filtering endpoint (the real Typesense-backed product data)."""
    return os.getenv(
        "SEARCH_API_URL",
        "https://api.sfsupplies.com/api/Products/Search/v2",
    )


def get_suggest_url() -> str:
    """Query/typeahead endpoint (search-bar autocomplete; raw Typesense hits)."""
    return os.getenv(
        "SUGGEST_API_URL",
        "https://api.sfsupplies.com/api/Products/query",
    )


def get_product_url_template() -> str:
    """Template for a product page URL from its urlSlug. '{slug}' is filled in.
    VERIFY this matches your real site path before relying on the links."""
    return os.getenv(
        "PRODUCT_URL_TEMPLATE",
        "https://www.sfsupplies.com/product/{slug}",
    )


def get_timeout() -> float:
    try:
        return float(os.getenv("SEARCH_API_TIMEOUT", "15"))
    except ValueError:
        return 15.0


MAX_PAGE_SIZE = 50
DEFAULT_PAGE_SIZE = 8  # show only the most relevant top matches by default

# Attributes worth surfacing in the LLM-facing text (in priority order). The UI
# cards carry the full attribute set; the text stays lean.
KEY_ATTRS = ["Brand", "Size", "Color", "Material", "Finish", "Series"]

# Maps the API's `productStatus` integer to its meaning.
PRODUCT_STATUS = {
    0: "Null",
    1: "StockItem",
    2: "InHouse",
    3: "NonStock",
    4: "Discontinued",
    5: "Development",
}
# Reverse map for the product_status filter. Excludes 0/Null: as a SEARCH FILTER,
# productStatus:0 matches NOTHING (it's the unused Null status), so we only ever
# send a real status (1-5) and omit the field otherwise.
STATUS_NAME_TO_INT = {v.lower(): k for k, v in PRODUCT_STATUS.items() if k != 0}
STATUS_FILTER_NAMES = [PRODUCT_STATUS[k] for k in range(1, 6)]


def status_filter_int(value):
    """Resolve a product_status arg (name like 'StockItem' or int 1-5) to its
    integer, or None to mean 'no status filter'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 5 else None
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 5 else None
    return STATUS_NAME_TO_INT.get(s.lower())


# --- HTTP call ----------------------------------------------------------------

async def post_search(payload: dict) -> dict:
    """POST the search payload and return the parsed JSON `data` block.

    Raises RuntimeError with a readable message on transport/HTTP errors."""
    url = get_search_url()
    headers = {"Content-Type": "application/json"}
    # Optional pass-through auth, in case the endpoint is ever locked down.
    api_key = os.getenv("SEARCH_API_KEY")
    if api_key:
        headers[os.getenv("SEARCH_API_KEY_HEADER", "Authorization")] = api_key

    try:
        async with httpx.AsyncClient(timeout=get_timeout()) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Search API returned {e.response.status_code}: {e.response.text[:300]}"
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach search API at {url}: {e}")
    except json.JSONDecodeError:
        raise RuntimeError("Search API returned a non-JSON response.")

    errors = body.get("errors") or []
    if errors:
        logger.warning(f"Search API reported errors: {errors}")
    return body.get("data") or {}


async def get_suggest(query: str) -> list:
    """GET the typeahead endpoint and return the parsed JSON `data` list
    (one entry per searched collection, each with `hits`)."""
    url = get_suggest_url()
    headers = {}
    api_key = os.getenv("SEARCH_API_KEY")
    if api_key:
        headers[os.getenv("SEARCH_API_KEY_HEADER", "Authorization")] = api_key
    try:
        async with httpx.AsyncClient(timeout=get_timeout()) as client:
            resp = await client.get(url, params={"query": query}, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(
            f"Suggest API returned {e.response.status_code}: {e.response.text[:300]}"
        )
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach suggest API at {url}: {e}")
    except json.JSONDecodeError:
        raise RuntimeError("Suggest API returned a non-JSON response.")

    data = body.get("data")
    return data if isinstance(data, list) else []


def build_payload(arguments: dict, default_page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """Translate tool arguments into the API request body."""
    search = (arguments.get("search") or "").strip()

    page = arguments.get("page", 0)
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0

    page_size = arguments.get("page_size", default_page_size)
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = default_page_size
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    filters = arguments.get("filters") or {}
    if not isinstance(filters, dict):
        filters = {}
    # Normalize each value to a list of strings (the API expects arrays).
    norm_filters = {}
    for key, val in filters.items():
        if isinstance(val, list):
            norm_filters[key] = [str(v) for v in val]
        elif val is not None:
            norm_filters[key] = [str(val)]

    payload = {
        "search": search,
        "pageSize": page_size,
        "page": page,
        "sortDirection": (arguments.get("sort_direction") or "").strip(),
        "sortColumn": (arguments.get("sort_column") or "").strip(),
        "attributeFilter": norm_filters,
        "categorySlug": arguments.get("category_slug") or None,
    }
    # Optional filters — only sent when set. productStatus:0 matches nothing, so
    # omit unless a real status (1-5) is requested.
    status_int = status_filter_int(arguments.get("product_status"))
    if status_int is not None:
        payload["productStatus"] = status_int
    # inventoryQty: True = in stock, False = out of stock; omit for both.
    in_stock = arguments.get("in_stock")
    if isinstance(in_stock, bool):
        payload["inventoryQty"] = in_stock
    return payload


# --- Formatting ---------------------------------------------------------------

def _clean(s):
    """Decode HTML entities (&reg; &trade; &amp; …) the API embeds in text."""
    return html.unescape(s) if isinstance(s, str) else s


def format_product(p: dict) -> str:
    """One compact line per product for the model. The UI cards carry the full
    detail (image, URL, every attribute), so the text stays terse on purpose."""
    name = _clean(p.get("productName")) or "(unnamed)"
    attrs = {
        a.get("attributeName"): a.get("attributeValue")
        for a in (p.get("attributesDTOs") or [])
        if a.get("attributeName") and a.get("attributeValue")
    }

    # Price is intentionally omitted — internal staff use negotiated, per-customer
    # pricing, so the chatbot never surfaces a list price.
    tags = []
    if p.get("inventoryQty") is not None:
        tags.append("in stock" if p.get("inventoryQty") else "out of stock")
    status = p.get("productStatus")
    if status is not None:
        tags.append(str(PRODUCT_STATUS.get(status, status)))

    key_bits = [f"{k} {attrs[k]}" for k in KEY_ATTRS if attrs.get(k)][:3]

    line = f"- {name}"
    if tags:
        line += f" [{', '.join(tags)}]"
    if key_bits:
        line += " — " + ", ".join(key_bits)
    return line


def product_to_dict(p: dict) -> dict:
    """Structured card data for the UI (returned as the tool's structuredContent;
    NOT sent to the LLM)."""
    slug = p.get("urlSlug") or ""
    status = p.get("productStatus")
    attrs = {}
    for a in p.get("attributesDTOs") or []:
        n, v = a.get("attributeName"), a.get("attributeValue")
        if n and v:
            attrs[n] = _clean(v)
    # Price is intentionally excluded — see format_product (negotiated pricing).
    return {
        "name": _clean(p.get("productName")) or "(unnamed)",
        "slug": slug,
        "url": get_product_url_template().format(slug=slug) if slug else None,
        "imageUrl": p.get("imageUrl"),
        "inStock": bool(p["inventoryQty"]) if p.get("inventoryQty") is not None else None,
        "status": PRODUCT_STATUS.get(status, status) if status is not None else None,
        "attributes": attrs,
    }


def suggestions_from_data(data: list, limit: int) -> tuple[list, int]:
    """Flatten typeahead hits across collections, dedupe by name.
    Returns (items, total_found) where each item is {name, slug, url, imageUrl}."""
    total = max((res.get("found", 0) for res in data), default=0)
    seen = set()
    items = []
    for res in data:
        for hit in res.get("hits") or []:
            doc = hit.get("document") or {}
            name = doc.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            slug = doc.get("urlSlug")
            items.append({
                "name": _clean(name),
                "slug": slug,
                "url": get_product_url_template().format(slug=slug) if slug else None,
                "imageUrl": doc.get("imageUrl"),
            })
            if len(items) >= limit:
                return items, total
    return items, total


def format_facets(facets: list) -> str:
    """Render the available filter fields and their values + counts."""
    if not facets:
        return "No filterable attributes returned for this query."
    out = []
    for f in facets:
        field = f.get("fieldName")
        counts = f.get("counts") or []
        if not field or not counts:
            continue
        vals = ", ".join(
            f"{c.get('value')} ({c.get('count')})"
            for c in counts
            if c.get("value") is not None
        )
        out.append(f"{field}: {vals}")
    return "\n".join(out) if out else "No filterable attributes returned."


# --- MCP server ---------------------------------------------------------------

app = Server("product_search_mcp_server")

_FILTERS_SCHEMA = {
    "type": "object",
    "description": (
        "Attribute filters to narrow results. Keys are facet field names "
        "(see list_product_filters), values are arrays of allowed values, "
        'e.g. {"Brand": ["Avery"], "Color": ["Plum"], "Size": ["15\\" x 10 Yards"]}. '
        "Multiple values for one field are OR-ed; different fields are AND-ed."
    ),
    "additionalProperties": {"type": "array", "items": {"type": "string"}},
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_products",
            description=(
                "Search the SF Supplies product catalog (vinyl, films, signage "
                "supplies, etc.). Returns matching products with their name, "
                "page URL, stock status, product status, and attributes (Brand, "
                "Color, Size, Material, Finish, Series, …). Use the optional "
                "'filters' to narrow by attribute — call list_product_filters "
                "first if you need to know which attributes/values are available. "
                "Two distinct availability signals: 'in stock' reflects "
                "inventoryQty — whether the item is ACTUALLY in stock in the "
                "warehouse right now (yes/no). 'status' is the catalog "
                "classification: StockItem (normally stocked), InHouse (made/held "
                "in house), NonStock (orderable, not stocked), Discontinued (no "
                "longer offered), Development (not yet released), or Null. A "
                "product can be a StockItem yet currently out of stock — when "
                "asked about availability, use 'in stock' (inventoryQty), not "
                "'status'. To FILTER by these, pass 'in_stock' (true = only "
                "in-stock, false = only out-of-stock) and/or 'product_status'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Free-text query, e.g. 'Avery translucent vinyl'. May be empty to browse by filters/category alone.",
                    },
                    "filters": _FILTERS_SCHEMA,
                    "in_stock": {
                        "type": "boolean",
                        "description": "Filter by real warehouse availability: true = only items currently in stock, false = only out-of-stock. Omit to include both.",
                    },
                    "product_status": {
                        "type": "string",
                        "enum": STATUS_FILTER_NAMES,
                        "description": "Filter to one catalog status (StockItem, InHouse, NonStock, Discontinued, Development). Omit for all. Distinct from in_stock — a StockItem can be out of stock.",
                    },
                    "category_slug": {
                        "type": "string",
                        "description": "Restrict to a category by its slug (optional).",
                    },
                    "sort_column": {
                        "type": "string",
                        "description": "Column to sort by (optional; leave empty for relevance).",
                    },
                    "sort_direction": {
                        "type": "string",
                        "description": "'asc' or 'desc' (optional).",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Zero-based page index (default 0).",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": f"Results per page (1-{MAX_PAGE_SIZE}, default {DEFAULT_PAGE_SIZE}).",
                    },
                },
                "required": ["search"],
            },
        ),
        Tool(
            name="suggest_products",
            description=(
                "Fast typeahead/autocomplete lookup (the search-bar suggester). "
                "Given a partial query it returns matching product NAMES (and "
                "page URLs) — lightweight, no attributes or filters. Use it to "
                "resolve a vague or partial term to concrete product names, then "
                "call search_products with the chosen term for full details."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Partial or full search text, e.g. 'hp750'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max suggestions to return (1-25, default 10).",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_product_filters",
            description=(
                "Discover the filterable attributes (facets) available for a "
                "given search/category, with each value and how many products "
                "have it. Use this to learn what you can pass to "
                "search_products' 'filters' argument and to suggest refinements."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Free-text query to scope the facets (optional).",
                    },
                    "filters": _FILTERS_SCHEMA,
                    "category_slug": {
                        "type": "string",
                        "description": "Restrict facets to a category slug (optional).",
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    """Returns text content for the LLM. search_products / suggest_products also
    return a (content, structuredContent) tuple — the structured part feeds the
    UI's product cards and is not seen by the model."""
    logger.info(f"Calling tool: {name} with arguments: {arguments}")
    arguments = arguments or {}

    if name == "search_products":
        payload = build_payload(arguments)
        # post_search raises RuntimeError on failure; let it propagate so the MCP
        # layer marks the result isError=True (no error-text sentinel needed).
        data = await post_search(payload)

        paged = data.get("pagedData") or {}
        total = paged.get("totalCount", 0)
        products = paged.get("data") or []
        facets = data.get("facets") or []

        if not products:
            hint = format_facets(facets)
            return [TextContent(type="text", text=(
                f"No products found for '{payload['search']}'"
                + (f" with filters {payload['attributeFilter']}" if payload["attributeFilter"] else "")
                + ".\n\nAvailable filters for this query:\n" + hint
            ))]

        header = (
            f"{total} match '{payload['search']}'"
            + (f" + filters {payload['attributeFilter']}" if payload["attributeFilter"] else "")
            + f" — top {len(products)} shown to the user as cards:"
        )
        body = "\n".join(format_product(p) for p in products)
        # structuredContent feeds the UI's product cards; the LLM only sees the text.
        structured = {
            "kind": "product_results",
            "query": payload["search"],
            "filters": payload["attributeFilter"],
            "total": total,
            "page": payload["page"],
            "shown": len(products),
            "products": [product_to_dict(p) for p in products],
        }
        return [TextContent(type="text", text=f"{header}\n{body}")], structured

    if name == "suggest_products":
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ValueError("'query' is required")
        limit = arguments.get("limit", 10)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        data = await get_suggest(query)  # raises on failure -> isError
        items, total = suggestions_from_data(data, limit)
        lines = []
        for it in items:
            lines.append(f"- {it['name']}" + (f"\n    url: {it['url']}" if it["url"] else ""))
        body = "\n".join(lines) if lines else "No suggestions."
        header = f"Suggestions for '{query}' (~{total} total matches; showing up to {limit}):"
        structured = {
            "kind": "product_suggestions",
            "query": query,
            "total": total,
            "suggestions": items,
        }
        return [TextContent(type="text", text=f"{header}\n{body}")], structured

    if name == "list_product_filters":
        # Only need facets, so request a single product.
        payload = build_payload(arguments, default_page_size=1)
        payload["pageSize"] = 1
        data = await post_search(payload)  # raises on failure -> isError

        total = (data.get("pagedData") or {}).get("totalCount", 0)
        facets = data.get("facets") or []
        header = (
            f"Filterable attributes for '{payload['search'] or '(all)'}'"
            + (f" in category '{payload['categorySlug']}'" if payload["categorySlug"] else "")
            + f" ({total} matching products):"
        )
        return [TextContent(type="text", text=f"{header}\n{format_facets(facets)}")]

    raise ValueError(f"Unknown tool: {name}")


# --- Transports (mirrors mssql_mcp_server) ------------------------------------

def _log_startup():
    logger.info(
        f"Product search MCP -> {get_search_url()} "
        f"(product URL template: {get_product_url_template()})"
    )


async def run_stdio():
    """Run over stdio (local single-client use, e.g. Claude Desktop)."""
    from mcp.server.stdio import stdio_server
    logger.info("Starting product-search MCP server (stdio)...")
    _log_startup()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def build_http_app():
    """Starlette ASGI app: Streamable-HTTP MCP at /mcp (bearer-auth) + /health."""
    import contextlib
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import JSONResponse, Response
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    disable_auth = os.getenv("MCP_DISABLE_AUTH", "false").lower() == "true"
    token = os.getenv("MCP_AUTH_TOKEN")
    if disable_auth:
        logger.warning("MCP_DISABLE_AUTH=true — serving WITHOUT authentication (dev only!).")
    elif not token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN must be set when MCP_TRANSPORT=http. "
            "For local dev only, set MCP_DISABLE_AUTH=true instead."
        )

    session_manager = StreamableHTTPSessionManager(app=app, json_response=False)

    async def handle_mcp(scope, receive, send):
        if not disable_auth:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            if headers.get("authorization", "") != f"Bearer {token}":
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await session_manager.handle_request(scope, receive, send)

    async def health(_request):
        return Response("ok", media_type="text/plain")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            logger.info("Streamable-HTTP session manager started.")
            yield

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )


def run_http():
    """Run over Streamable HTTP (shared network deployment)."""
    import uvicorn
    host = os.getenv("MCP_HTTP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_HTTP_PORT", os.getenv("PORT", "8000")))
    logger.info(f"Starting product-search MCP server (streamable-http) on {host}:{port}, path /mcp ...")
    _log_startup()
    uvicorn.run(build_http_app(), host=host, port=port, log_level="info")


def main():
    """Entry point. MCP_TRANSPORT selects stdio (default) or http."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable-http", "sse"):
        run_http()
    else:
        asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
