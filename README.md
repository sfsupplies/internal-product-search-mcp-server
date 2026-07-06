# product-search-mcp-server

An MCP server that exposes the SF Supplies **product search** (the Typesense-backed
website search API) to an LLM tool loop. It is a sibling of `mssql-mcp-server`
and uses the same transport/auth/deploy pattern, so the internal chatbot can
bridge both servers at once.

## Tools

| Tool | What it does |
|------|--------------|
| `search_products` | Runs a search against `POST /api/Products/Search/v2`. Supports free-text query, attribute `filters`, `category_slug`, sorting, and pagination. Returns each product's name, page URL, stock status, and attributes (Brand, Color, Size, Material, …). |
| `suggest_products` | Fast typeahead/autocomplete (`GET /api/Products/query?query=…`). Returns matching product **names** + URLs only — lightweight. Use it to resolve a vague/partial term, then hand the chosen term to `search_products`. |
| `list_product_filters` | Returns the **facets** (filterable attributes and their values + counts) available for a query/category — so the model knows what it can pass to `search_products`' `filters`. |

The `filters` argument maps facet field names to arrays of values, matching the
API's `attributeFilter`, e.g.:

```json
{"Brand": ["Avery"], "Color": ["Plum"], "Size": ["15\" x 10 Yards"]}
```

## Run locally (stdio — e.g. Claude Desktop)

```bash
pipenv install
pipenv run python -m product_search_mcp_server
```

## Run as a shared HTTP service (LAN)

```bash
cp .env.deploy.example .env.deploy   # set MCP_AUTH_TOKEN (openssl rand -hex 32)
docker compose -f docker-compose.deploy.yml up -d --build
```

Consumers connect to `http://<host>:8001/mcp` with header
`Authorization: Bearer <MCP_AUTH_TOKEN>`. Defaults to port **8001** to avoid
clashing with the MSSQL MCP server on 8000.

`examples/chatbot_bridge.py` shows the client-side bridge the chatbot uses.

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_AUTH_TOKEN` | — | Required bearer token in `http` mode |
| `MCP_DISABLE_AUTH` | `false` | Dev-only: serve HTTP with no auth |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `8000` | HTTP bind (container) |
| `SEARCH_API_URL` | `https://api.sfsupplies.com/api/Products/Search/v2` | Results/filtering endpoint |
| `SUGGEST_API_URL` | `https://api.sfsupplies.com/api/Products/query` | Typeahead/autocomplete endpoint |
| `PRODUCT_URL_TEMPLATE` | `https://www.sfsupplies.com/product/{slug}` | Product page link template — **verify the path** |
| `SEARCH_API_TIMEOUT` | `15` | HTTP timeout (seconds) |
| `SEARCH_API_KEY` / `SEARCH_API_KEY_HEADER` | — | Optional, only if the API gets locked down |
