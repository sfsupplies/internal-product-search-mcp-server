# Deploying the product-search MCP server on your LAN

A single Dockerized server on the same Linux VM as the MSSQL MCP server,
serving **Streamable HTTP** on **port 8001**, protected by a **bearer token**.
It calls the SF Supplies product-search API (Typesense-backed) over HTTPS; your
internal chatbot consumes it over the LAN, bridged alongside the MSSQL server.

```
 internal chatbot (LAN) ──► product-search MCP (LAN VM, :8001/mcp) ──► api.sfsupplies.com
        │                ──► MSSQL MCP          (LAN VM, :8000/mcp) ──► Azure SQL (read-only)
        └──► Claude API (api.anthropic.com)
```

Unlike the MSSQL server there are **no database credentials** to set — the only
secret is the bearer token. (Each server has its own token unless you choose to
reuse one.)

---

## 1. Deploy on the Linux VM

```bash
git clone <your repo> product-search-mcp-server && cd product-search-mcp-server
cp .env.deploy.example .env.deploy
# edit .env.deploy: set MCP_AUTH_TOKEN  (openssl rand -hex 32)
# HOST_PORT already defaults to 8001 so it won't clash with the MSSQL server on 8000
docker compose -f docker-compose.deploy.yml --env-file .env.deploy up -d --build
```

Verify:

```bash
curl http://localhost:8001/health                       # -> ok
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://localhost:8001/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'   # -> 401 without token
```

The container restarts on failure (`restart: unless-stopped`) and has a built-in
healthcheck. Logs: `docker compose -f docker-compose.deploy.yml logs -f`.

**Endpoint for clients:** `http://<vm-host>:8001/mcp` with header
`Authorization: Bearer <MCP_AUTH_TOKEN>`.

---

## 1b. Alternative: deploy to Azure App Service (GitHub Actions)

Instead of the LAN VM, this server can run on **Azure App Service** as a public
HTTPS endpoint — the same code-deploy pattern as the internal-chatbot (no Docker;
Azure's Oryx build installs the package). The pipeline is
[.github/workflows/main_sfproductsearch.yml](.github/workflows/main_sfproductsearch.yml);
its header has the full first-time setup.

Summary:
1. Create a Linux **Python (≥3.11)** Web App in the **SF Cloud** account.
2. Run **Deployment Center → GitHub → User-assigned identity (OIDC)**; it creates
   the `AZUREAPPSERVICE_*` repo secrets. Copy their exact names into the
   `azure/login` step and set `AZURE_WEBAPP_NAME`.
3. Add **Application settings**: `SCM_DO_BUILD_DURING_DEPLOYMENT=true`,
   `MCP_TRANSPORT=http`, `MCP_HTTP_HOST=0.0.0.0`, `MCP_HTTP_PORT=8000`,
   `MCP_AUTH_TOKEN=<token>` (plus any optional overrides from §"configuration").
4. Push to `main`. Startup command is `python -m product_search_mcp_server`.

The workflow exports a `requirements.txt` from the Pipfile and **removes the
Pipfile from the deployed copy** so Oryx installs via `pip` (a Pipfile makes Oryx
take a pipenv path that ships an empty virtualenv → `No module named ...` on
boot). The Pipfile stays the source of truth in git.

> ⚠️ On Azure this endpoint is **public** (unlike the LAN deployment), so the
> bearer token is the entire boundary — keep `MCP_AUTH_TOKEN` strong, rotate it,
> and never set `MCP_DISABLE_AUTH`. The chatbot then uses the `https://…` URL.

---

## 2. Point the chatbot at it

The internal-chatbot backend already bridges this server. In `backend/.env`:

```bash
PRODUCT_MCP_URL=http://<vm-host>:8001/mcp/
PRODUCT_MCP_AUTH_TOKEN=<the same MCP_AUTH_TOKEN you set above>
```

Restart the backend. If the server is unreachable the chatbot keeps working with
the MSSQL tools alone (the router skips a down server). Leave `PRODUCT_MCP_URL`
empty to disable the product tools entirely.

---

## Local dev: skip auth

```bash
MCP_TRANSPORT=http MCP_HTTP_PORT=8001 MCP_DISABLE_AUTH=true pipenv run python -m product_search_mcp_server
# clients then connect to http://localhost:8001/mcp with NO Authorization header
```

The server logs a loud `serving WITHOUT authentication` warning. **Never set
`MCP_DISABLE_AUTH` on a shared/network deployment.**

---

## 3. Security checklist

- [x] **Bearer token** — required; the server refuses to start in HTTP mode without it.
- [x] **Read-only by nature** — the tools only issue search queries against the
      product API; there is no write path.
- [ ] **TLS** — bearer tokens over plain http are sniffable. Fine on a trusted
      segment; for `https://` put a reverse proxy (Caddy/nginx/Traefik) in front,
      or share one proxy with the MSSQL server on different paths/ports.
- [ ] **Network scope** — restrict the VM's :8001 to the subnets that need it
      (firewall / security group), not the whole network.
- [ ] **Rotate** `MCP_AUTH_TOKEN` periodically; update the chatbot's
      `PRODUCT_MCP_AUTH_TOKEN` to match.
- [ ] **Egress** — the container needs outbound HTTPS to `api.sfsupplies.com`.

---

## Optional: configuration overrides

All optional — defaults are baked in. Set in `.env.deploy` only if they change:

| Env var | Default | Purpose |
|---|---|---|
| `SEARCH_API_URL` | `https://api.sfsupplies.com/api/Products/Search/v2` | results/filtering endpoint |
| `SUGGEST_API_URL` | `https://api.sfsupplies.com/api/Products/query` | typeahead endpoint |
| `PRODUCT_URL_TEMPLATE` | `https://www.sfsupplies.com/product/{slug}` | product page link template — **verify the path** |
| `SEARCH_API_TIMEOUT` | `15` | HTTP timeout (seconds) |
| `BIND_ADDR` / `HOST_PORT` | `0.0.0.0` / `8001` | where the port is published on the host |
