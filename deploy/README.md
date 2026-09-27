# Deploy the three services behind one HTTPS origin

`compose.production.yaml` is a standalone Compose file for a Docker host. Caddy exposes ports 80/443; Next.js, FastAPI, and pgvector PostgreSQL communicate on the private Compose network. Frontend requests go to `/api`, so the browser does not need separate frontend/API domains. PostgreSQL and Caddy certificates use named persistent volumes. This uses the same application code as development.

**Status:** the configuration is prepared for a host. Remote deployment requires a chosen host/project, access, a domain (or equivalent private routing), and an access policy. No remote URL has been published. A separate local deployment smoke stack uses loopback port 8080; that is not an internet deployment.

Verified on 2026-09-27: the full Edge smoke passed through Caddy at `http://localhost:8080`, including real SSE drafts, citation/source/PDF links, errors/cancellation, and mobile layout. Proxied `/api/docs` and its OpenAPI document worked. Replacing the smoke database container preserved the original example PDF and all five embedded chunks in its named volume. The first proxy smoke exposed a test-harness relative-URL capture mismatch; the corrected harness passed. Remote TLS/domain/access and backup restoration remain unverified.

## Deploy on an existing host

1. Install Docker with current Compose v2 (including environment-sourced secrets), copy/clone this repository to the host, and point your domain's DNS to it. Allow inbound 80/443. Keep application/database ports closed.
2. Copy `deploy/production.env.example` to `.env.production` in the project root. Set `SITE_ADDRESS` to the hostname, `FRONTEND_ORIGIN` to its exact HTTPS origin, a new database password, a matching URL-encoded `DATABASE_URL`, and a backend OpenAI key. Protect this file with host filesystem permissions; never commit or print it. Use your hosting platform's secret store when available.
3. Start the deployment from the root:

```bash
docker compose --env-file .env.production -p inside-agent-prod -f compose.production.yaml config --quiet
docker compose --env-file .env.production -p inside-agent-prod -f compose.production.yaml up -d --build
docker compose --env-file .env.production -p inside-agent-prod -f compose.production.yaml ps
```

Use this standalone file, not an override merged with `compose.yaml`, which publishes localhost development database/API ports. On a fresh database, the backend creates the schema and applies migrations transactionally. Existing production volumes receive only unapplied migrations. Never use `down -v` during an update. Rotate an existing PostgreSQL role's password in the database as well as configuration; changing `POSTGRES_PASSWORD` alone does not change an initialized database.

Caddy obtains HTTPS certificates for a reachable domain and preserves them in its data volume ([automatic HTTPS](https://caddyserver.com/docs/automatic-https)). `handle_path /api/*` removes the prefix before proxying to FastAPI; Uvicorn's `/api` root path makes OpenAPI docs work at `/api/docs`. `flush_interval -1` forwards streaming events promptly ([reverse proxy configuration](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)). The frontend is built with `/api`; changing client configuration requires a rebuild.

## Access and data boundary

The application has no authentication, rate limiting, tenant isolation, or spending cap. Anyone with access to the API can upload documents and cause provider usage; document UUIDs are identifiers, not access controls. Put the whole origin (including `/api` and PDF routes) behind your host's access gateway for a restricted demo. An intentionally open demo needs an agreed usage policy and provider budget controls before launch. Use only the bundled fictional documents for public demonstrations. Original PDFs now persist in PostgreSQL along with extracted text and vectors; include them in your retention and backup policy.

Both Compose files use [mounted Docker secrets](https://docs.docker.com/compose/how-tos/use-secrets/), sourced from the ignored environment file. Only Postgres receives `postgres_password`; only the backend receives `database_url` and `openai_api_key`; the frontend and proxy receive no secrets. Container environment metadata and resolved Compose configuration contain secret names/file paths instead of credential values. The backend reads files through `app/config.py`, using `DATABASE_URL_FILE` and `OPENAI_API_KEY_FILE`. Postgres uses its native `POSTGRES_PASSWORD_FILE` support. No secret values are passed as build arguments or baked into images.

The host environment file still contains plaintext secrets. Protect it with host permissions and never share it, logs containing credentials, or Docker diagnostics from older containers. Mounted secrets do not hide credentials from host/Docker administrators or code inside an authorized container. Prefer platform-managed secrets in hosted environments. When changing a secret, recreate the affected containers with `up -d --force-recreate` so environment-sourced mounts refresh. Keep existing database passwords aligned with the actual role; this change does not rotate credentials or replace database volumes.

## Smoke checks after deployment

Open `/api/health`, `/api/ready`, and `/api/docs`, then use the frontend. Upload `examples/it-support-guide.pdf` at 50 tokens / 10 overlap, embed all five chunks, and ask about VPN access. Confirm the running trace, provisional text, cited page/chunk, original PDF link, and source permalink. Reload and verify saved data. The original PDF may download rather than open at a page on browsers without an inline PDF viewer.

With Node.js, Edge, and the browser harness installed, run from the repository root in PowerShell:

```powershell
npm.cmd install --prefix .verification --no-save playwright-core
$env:DEMO_URL = 'https://YOUR_DOMAIN'
$env:DEMO_API_URL = 'https://YOUR_DOMAIN/api'
node scripts/trace-smoke.cjs
```

The harness makes real paid calls and uploads only the fictional example. A restricted deployment may need a browser session authenticated through its access gateway; the harness does not bypass access controls.

Health checks verify startup and database readiness; they do not test provider billing/access. Backend upload limit remains 10 MB. There is no durable queue: proxy/client disconnects signal cancellation, while a blocking provider call or commit can still finish. Successful batches remain stored.

Back up PostgreSQL with your host's backup system or `pg_dump`, keep encrypted copies off-host, and test restore before relying on them. The Caddy volumes hold certificate state. Neither remote backup/restore nor domain/TLS provisioning has been verified without an actual target host.

## Local deployment smoke

To test the same topology without publishing it, use a separate ignored environment file and project name, with `SITE_ADDRESS=:80`, `FRONTEND_ORIGIN=http://localhost:8080`, `HTTP_BIND=127.0.0.1:8080`, and `HTTPS_BIND=127.0.0.1:8443`. Supply a separate database password/URL and a backend key. Run the same Compose command with that file and `-p inside-agent-release`. Set browser smoke URLs to `http://localhost:8080` and `http://localhost:8080/api`. HTTP here is only for the loopback smoke; production uses the domain/HTTPS configuration above.
