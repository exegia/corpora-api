# Corpora API

FastAPI and FastMCP interfaces for [corpora-core](https://github.com/exegia/corpora-core). Text checks call the Rust project's supported `corpora-text-check` executable. Conversion requests call the core project's TEI, EPUB, and PDF converter packages and return their source-preserving bundles.

## What is exposed

| REST operation | MCP tool | Core operation |
| --- | --- | --- |
| `POST /v1/checks/structure` | `check_xml_structure` | Strict XML check and source-bound diagnostics |
| `POST /v1/checks/tei-metadata` | `check_tei_purpose` | TEI date and primary identifier policy |
| `POST /v1/language/detect` | `detect_text_language` | Language evidence and dictionary selection |
| `POST /v1/checks/spelling` | `check_text_spelling` | Optional installed-dictionary spelling report |
| `POST /v1/checks/text-fabric` | `check_text_fabric_archive` | Bounded static WARP preflight for a TF ZIP |
| `POST /v1/validate` | — | User-authenticated archive validation with real loaders or CTEI/C-USX profile checks |
| `POST /v1/ingest` | — | User-authenticated document ingestion to a schema-validated Context Fabric v1 graph |
| `GET /v1/storage`, `GET /v1/storage/{filename}`, `GET /v1/storage/{filename}/download`, `GET /v1/storage/{filename}/manifest`, `GET /v1/storage/{filename}/index`, `GET /v1/storage/{filename}/sections`, `GET /v1/storage/{filename}/content`, `GET /v1/storage/{filename}/nodes/{node}` | — | User-authenticated archive metadata, bytes, manifest, Text-Fabric structure, passages, and node detail |
| `POST /v1/storage`, `DELETE /v1/storage/{filename}` | — | Publish an owned, validated `/convert` artifact and delete a stored archive |

| Conversion endpoint | Result |
| --- | --- |
| `POST /v1/conversions/tei-to-tf` | TEI source, Text-Fabric features, manifest and checksums |
| `POST /v1/conversions/epub-to-tf` | EPUB source, assets, Text-Fabric features and manifest |
| `POST /v1/conversions/pdf-to-tf` | Score-gated PDF extraction, TEI, Text-Fabric and review report |

`GET /v1/health` reports API process health. `GET /v1/capabilities` publicly reports whether a service token is required and whether the configured Hub backend has a separate write credential. `GET /v1/openapi.json` is the exact schema used by the [Fumapress docs](docs/README.md); the API tools are mounted at `/v1/mcp/`. The existing `api.exegia.co` service keeps its routes outside `/v1/*`.

The MCP server also advertises a `text_fabric_checker` prompt for a host agent. Upload a ZIP containing one `.tf` root to the REST route, or pass its base64 bytes to the MCP tool. The checker reports an archive SHA-256, slot/node counts and located errors in `otype.tf`, `oslots.tf` and `otext.tf`. It rejects unsafe paths, links, duplicate names, archives above 3 MB, over 3,000 files or over 64 MB unpacked. A `passed` result is a **static WARP preflight only**; use a real Text-Fabric/Context-Fabric load and source comparison for full validation. The local `text-fabric-validator` skill is not bundled in this service because its redistribution license is unspecified.

The CLI's structural operation uses the core default XML options. TEI-specific `StructuralOptions` from the Rust library are not available through that CLI, so this API does not claim to perform a full TEI structural validation. A nonblocking report does not establish schema validity or conversion success. The service never applies suggestions to source text.

The spelling response keeps its existing flat `start` and `end` fields. The API validates those Unicode scalar offsets with the generated `corpora-schema-types` `TextRange` model from the same pinned core commit used by `Dockerfile.vercel`. Update the Git revision in `pyproject.toml` and `CORPORA_CORE_REF` together when adopting a new core contract.

`POST /v1/validate` accepts a multipart `file` upload named `.corpus`, `.zip`, `.ctei`, or `.cusx` and requires a verified Supabase user access token. The response uses the legacy `corpus`, `path`, `valid`, `stats`, and `reasons` fields; `path` is the uploaded filename, never a server path. `checks` is a named map with `null` for non-applicable shipped-cache checks. CTEI reports `source_verified: false` because archive validation does not prove fidelity to an external source. Invalid supported archives return HTTP 200 with `valid: false`; unsupported formats return 415 and uploads over 3 MB return 413. `.corpus` and raw TF `.zip` data run real Text-Fabric and Context-Fabric loads, including mmap cache reload. CTEI and C-USX use their package profiles. The validator route does not accept legacy `path`, loaded `corpus`, or conversion `job_id` targets; the storage publish route separately accepts only an owner-checked, succeeded conversion job and revalidates its stored archive.

`POST /v1/ingest` accepts one multipart `file` plus optional `name`, `language` (BCP 47, default `und`), and `corpus_id` (UUID). UTF-8 `.txt` is the first hosted format: its exact decoded text is preserved in one fragment under a document root and block node. PDF, DOCX, PPTX, HTML, and Markdown require an optional Docling Python worker; without it, those uploads return 503. Unsupported formats return 415, invalid inputs 422, and uploads over 3 MB return 413. Ingestion finishes and its graph is stored before the 202 response; `GET /v1/ingest/{job_id}` polls the saved job and `GET /v1/ingest/{job_id}/download` returns `graph.json`. The result is capped at 4 MB. All three routes require a verified Supabase user token; another user's job returns 404.

Ingestion uses the existing private `conversion_jobs` PostgREST table and result bucket from `corpora-py` (see its `packages/admin/sql/conversion_jobs.sql`). Set `SUPABASE_URL` or `PROJECT_REF`, `SUPABASE_SERVICE_ROLE_KEY`, and `SUPABASE_JOBS_BUCKET` (or `SUPABASE_STORAGE_BUCKET`) in the API project. A completed graph is saved at `conversion-jobs/{job_id}.graph.json`; the job row records its verified owner and result key. The service role key stays server-side. The optional Docling worker runs in a separate Python environment with this API package and `corpora-admin[docling]` installed; point `CORPORA_INGEST_WORKER_PYTHON` to that interpreter. Worker output is revalidated against the pinned Context Fabric v1 JSON Schemas from `corpora-schema-types` before storage.

Storage uses `STORAGE_BACKEND=huggingface` by default, matching the current legacy `/storage` deployment. Set `HF_STORAGE_REPO`, `HF_STORAGE_REPO_TYPE` (`bucket`, `model`, `dataset`, or `space`), and `HF_TOKEN` for private reads. Hub publishing and deletion require a separate, write-scoped `HF_STORAGE_WRITE_TOKEN`; the read token is never used for writes. Publishing uses a conditional Hub commit to prevent concurrent overwrites. Hub buckets remain readable but do not support publishing because their upload API has no equivalent conditional commit. `STORAGE_BACKEND=supabase` uses the existing private `SUPABASE_STORAGE_BUCKET` with `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_URL` or `PROJECT_REF`. A verified Supabase user JWT is required on every storage route. Publishing accepts only a completed `/convert` `job_id` owned by the caller; the API fetches and revalidates its private stored archive. It never accepts a server path. Hub archives are shared, while Supabase objects are addressed only at `{verified-user-id}/{filename}`. A delete is audit-logged with the verified user ID. List and inspect return the legacy `filename`, `size_bytes`, `repo_id`, and `url` fields, and download streams bytes without staging on the API server. Manifest, index, section, content, node, and version-history reads fetch only the verified owner's archive, read it with the packaged Context-Fabric cache, and keep no cross-request cache. Missing configuration returns 503, a missing archive or reference returns 404, corrupt archives return 422, and an upstream storage failure returns 502.

Authenticated `/v1/ai/drafts` endpoints create, list, inspect and export private
working drafts from a caller-owned completed conversion. They use the existing
`corpora_ai_storage` Supabase RPC and private `corpus-ai-drafts` bucket from
the legacy `corpora-py` draft migrations. Archives are staged under immutable
owner/document/revision keys and checked against their SHA-256 digest before
publication and download. The API requires the server-side Supabase service key;
no caller-supplied storage path is accepted.
The source-pinned legacy database setup is in `supabase/migrations/`; apply it
only after reviewing it against the target database and verifying its existing
tables and policies. The API does not apply migrations automatically.


Authenticated `/v1/ai/providers` lists the legacy provider guidance without
accepting API keys. `/v1/ai/validate` validates a version-pinned scope against
an accessible stored corpus or owned completed conversion, returning located
findings and rejecting stale version or content hashes.
Corpus detail, reference, and scope reads run the native loader in a separate
short-lived process per request so its state cannot cross server worker threads.
Results exceeding the worker's 3.9 MB encoded JSON limit return 413 rather than
being truncated; request a smaller page where the route supports pagination.

The three `/v1/linking/{space_id}/references` operations are disabled by default.
They require a server-controlled `LINKING_DATABASE_URL`, `LINKING_INVENTORY`
file, `LINKING_ENABLED=true`, and the existing Supabase JWT configuration.
Every operation rechecks the verified session, space membership and exact
resource grants; clients cannot supply the inventory or database path. The
legacy schema in `tests/fixtures/reference-linking-proposal.sql` is an **offline
test proposal**, not a migration. Production requires an approved schema and
an authoritative inventory and entitlement provider. Native HTML, EPUB and PDF locators require an
explicit server-controlled `asset_id` in their inventory record; absent or
mismatched IDs do not resolve. Native PDF selection
resolution additionally requires the optional `linking-pdf` extra; PyMuPDF is
available under AGPL or commercial terms.
The server adapter and offline schema fixture are derived from `corpora-py`
revision `663eec793b384bca22afbb065678573e21ed5b97`; the shared reference
models and resolver are provided by `corpora-linking`.
The same authority can retrieve exact selections, map browser DOM ranges, export
audited reference snapshots, and record publication or withdrawal decisions.
Publication events record an acknowledgment or withdrawal intent; they do not
assert that an artifact was delivered or removed remotely.
An optional isolated PostgreSQL authorization test starts a fresh in-memory
PGlite server on a loopback port. It does not connect to or modify a Supabase
project, and its single connection does not establish concurrency guarantees:

```bash
npm install --prefix /tmp/corpora-pglite --ignore-scripts @electric-sql/pglite@0.5.16 @electric-sql/pglite-socket@0.2.19
CORPORA_PGLITE_ROOT=/tmp/corpora-pglite uv run pytest tests/api/test_linking_pglite.py
```


The authenticated `/v1/refs` endpoints create and resolve versioned
Text-Fabric references and compact tokens from stored corpora. Resolution
rejects stale versions and ambiguous compact corpus names; access follows the
configured storage backend's rules.

## Local development

Requirements: Python 3.13+, uv, Git for the pinned core schema package, Rust 1.91+ to build the core executables, Node.js 22.23.4+ for docs. PDF conversion also needs Poppler and Tesseract.

1. Build the executable in a `corpora-core` checkout:

   ```sh
   cargo build --locked -p corpora-core --features spellcheck --bin corpora-text-check
   ```

2. Install and run this service:

   ```sh
   make doctor
   make setup
   export CORPORA_TEXT_CHECK_BIN=/absolute/path/to/corpora-core/target/debug/corpora-text-check
   uv run uvicorn corpora_api.app:app --reload
   ```

3. Open `http://localhost:8000/docs` or call an endpoint:

   ```sh
   curl -s http://localhost:8000/v1/checks/structure \
     -H 'Content-Type: application/json' \
     -d '{"source_id":"book.xml","text":"<root/>"}'
   ```

Set `CORPORA_DICTIONARIES_DIR` to a directory installed by [the core dictionary installer](https://github.com/exegia/corpora-core/tree/main/crates/corpora-core/tools) to enable spelling. Without it, the spelling endpoint returns 503; the other checks remain available. For a public API host, set `CORPORA_API_TOKEN`; check, conversion, and MCP requests then require `Authorization: Bearer <service token>`. `GET /v1/auth/me` instead requires a verified Supabase user access token and returns its user ID. Configure `PROJECT_REF` to select the project's JWKS and issuer, or set `SUPABASE_JWKS_URL` and `SUPABASE_JWT_ISSUER` for a self-hosted project. `SUPABASE_JWT_AUDIENCE` defaults to `authenticated`. Future user-owned routes must use `require_user` from `corpora_api.auth` and be added to the user-auth route policy. A service token never supplies a user identity; missing JWKS configuration rejects user tokens. Health, capabilities, OpenAPI, and the Swagger UI stay public.

The service limits text to one million characters. Each core request has a 10-second deadline (30 seconds for spelling). It rejects CLI errors, invalid JSON, unsupported diagnostic schema versions, and stale source hashes.

Conversion endpoints accept one multipart upload. The TEI route exposes choice, apparatus, witness, notes, and namespace policy as query parameters; EPUB offers audited `repair`; PDF offers installed OCR languages. Uploads are capped at 3 MB, TEI/EPUB at 200,000 slots, PDF at 10 pages, and returned ZIPs at 4 MB. The ZIP contains the converter's original report and bundle, including the uploaded source. PDF results with `X-Conversion-Status: gated` contain a report but no TF. These synchronous limits fit [Vercel's function payload limit](https://vercel.com/docs/functions/limitations); larger conversions need a storage-backed job API.

For local archive validation, install `packages/corpus-validate` from a matching `corpora-core` checkout into this API environment and set `CORPORA_VALIDATE_BIN` to its `corpora-validate` executable. The Vercel image installs it from the same pinned core commit as the Rust binaries.

## Docs and OpenAPI

The Astro/Fumapress site lives in `docs/`. It generates `/api` operation pages from the checked-in `docs/openapi.json` file. The pages include request bodies, response schemas, errors, examples, and the OpenAPI playground. `/mcp` on the docs host is a separate documentation-search MCP server, not the core-checking API MCP server.

The docs install applies a version-checked patch to `fumadocs-openapi` 12.6.2 so proxy-backed request examples work in its playground. Review or remove this patch when upgrading the package.

After changing Python routes or models:

```sh
uv run python scripts/export_openapi.py
make ci
```

`make ci` installs both Python and docs dependencies, runs template checks and Python tests, type-checks docs, and builds the Vercel output. The optional real-core integration test runs when `CORPORA_TEXT_CHECK_BIN` points to a built executable. The docs site can be built directly with `npm ci --prefix docs && npm run build --prefix docs`.

Fumapress AI chat is included with a request-size cap, an eight-message cap, and a five-request-per-hour Upstash limit per client. Vercel deployments use their OIDC token for AI Gateway; local runs can set `AI_GATEWAY_API_KEY`. Chat returns 503 until the docs project has `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. Fumapress also publishes `/llms.txt` and `/llms-full.txt`.

## Deploying

The root `Dockerfile.vercel` builds a pinned commit of `corpora-core`, its three Rust executables, the converter packages, OCR system tools, and pinned dictionaries. Vercel selects its container runtime from this file. Set `CORPORA_API_TOKEN` on the API project before public deployment. The docs site is a separate Vercel project with root directory `docs` and Astro preset. Its domain is `docs.exegia.co`, with the generated API reference at `/api`.

For the frozen endpoint inventory and the routing verification and rollback process, see the [core migration cutover guide](docs/CORE-MIGRATION-CUTOVER.md).

The checked-in OpenAPI spec targets `https://api.exegia.co`; every operation path begins with `/v1/`. Vercel routes only that prefix from the existing `corpora-py` project to the new container service, preserving the older API's other endpoints. The docs and API deployments can be released independently, but the OpenAPI export must stay in sync with the Python app.

## Repository conventions

This repository was created from `exegia/corpora-template`. Work on `<type>/<slug>` branches and use Conventional Commit PR titles. The template release workflow and its branch policy remain in [AGENTS.md](AGENTS.md) and [`.github/WORKFLOW.md`](.github/WORKFLOW.md). Keep credentials out of Git and use `.env.example` only for placeholder configuration.

The release generator writes `CHANGELOG.md` with no trailing blank line so the release PR passes the pre-commit hygiene check. Run `make selftest` after changing that generator.

Promotion skips branches with matching trees. The release job builds Python distributions and attaches them when it creates the GitHub Release; later tag-triggered jobs handle registry publishing.
