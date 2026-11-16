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

Ingestion uses the private `conversion_jobs` PostgREST table and result bucket. Set `SUPABASE_URL` or `PROJECT_REF`, `SUPABASE_INGEST_API_KEY` (a publishable gateway key), and `SUPABASE_JOBS_BUCKET=conversion-jobs`. The API forwards the verified signed-in user's token to Supabase only during the request; row-level policies restrict jobs, retry reservations, and graph objects to their owner. Graphs are stored at `conversion-jobs/{user_id}/{job_id}.graph.json`. A user can write their own allowed objects and rows directly, so stored graphs are revalidated when read. Each ingestion request checks that the user session remains active; the jobs, reservation, and Storage policies also enforce that check. If the session check is unavailable the API returns 503. A session revoked during an already-running operation may take effect only on a subsequent database statement. A token expiring during an operation can be refreshed and the request retried. The optional Docling worker runs in a separate Python environment with this API package and `corpora-admin[docling]` installed; point `CORPORA_INGEST_WORKER_PYTHON` to that interpreter. Worker output is revalidated against the pinned Context Fabric v1 JSON Schemas from `corpora-schema-types` before storage.

Clients can send `Idempotency-Key` with an ingestion request and reuse it for an exact retry; keys are scoped to the signed-in user, must contain 1–128 visible ASCII characters, and return 409 when reused with different input. This requires the `corpora_ingest_requests` migration and owner-scoped reservation SELECT/INSERT policies. Use a new key for a new operation; an absent key creates a new job on each request.

Reservations are retained indefinitely for this release so an old retry cannot silently create a duplicate job. They contain the owner, hashes, job ID, and creation time, not the source document or raw key. There is no background cleanup or automatic retry: abandoned reservations remain until the same user retries the same file, filename, options, and key. If only the reservation exists, that retry runs ingestion again; if a valid graph was stored but its job row was not, it finishes the job from that graph. An invalid or inaccessible stored graph fails closed and requires investigation. Reservations and abandoned graph objects consume storage, so monitor their growth; do not delete reservations without a separate retention policy that accounts for late retries and in-flight work.

Storage uses `STORAGE_BACKEND=huggingface` by default, matching the current legacy `/storage` deployment. Set `HF_STORAGE_REPO`, `HF_STORAGE_REPO_TYPE` (`bucket`, `model`, `dataset`, or `space`), and `HF_TOKEN` for private reads. Hub publishing and deletion require a separate, write-scoped `HF_STORAGE_WRITE_TOKEN`; the read token is never used for writes. Publishing uses a conditional Hub commit to prevent concurrent overwrites. Hub buckets remain readable but do not support publishing because their upload API has no equivalent conditional commit. `STORAGE_BACKEND=supabase` uses the existing private `SUPABASE_STORAGE_BUCKET` with `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_URL` or `PROJECT_REF`. A verified Supabase user JWT is required on every storage route. Publishing accepts only a completed `/convert` `job_id` owned by the caller; the API fetches and revalidates its private stored archive. It never accepts a server path. Hub archives are shared, while Supabase objects are addressed only at `{verified-user-id}/{filename}`. A delete is audit-logged with the verified user ID. List and inspect return the legacy `filename`, `size_bytes`, `repo_id`, and `url` fields, and download streams bytes without staging on the API server. Manifest, index, section, content, node, and version-history reads fetch only the verified owner's archive, read it with the packaged Context-Fabric cache, and keep no cross-request cache. Missing configuration returns 503, a missing archive or reference returns 404, corrupt archives return 422, and an upstream storage failure returns 502.

The draft `corpora_corpus_metadata` migration and Python registry prepare the
chosen per-corpus model: Supabase stores metadata, ownership, a unique Hub
repository identifier and revision; the configured Hub organization owns the
actual corpus files. The server generates a stable repository identifier from a
caller-stable corpus UUID. Records begin in `provisioning`; this component does
not create a Hub repository, prove that it is private, or mark it ready. It has
no HTTP routes and callers must supply a verified live session. Browser roles
have no direct table access. `CORPORA_SUPERADMIN_USER_ID` is a server-controlled
user UUID, never a user-editable profile field. Review the migration, session
enforcement, Hub provisioning and namespace before enabling this registry; the
API does not apply its migrations.
The draft registry can update title, description, language and structured
properties with a revision check; properties are replaced as one object. It
does not expose these edits through an API or enforce a live session itself.
Properties are user-editable metadata and must not determine authorization.
The configured Hub organization is part of each immutable repository link.
Changing it will make existing records fail closed. A namespace transfer needs
a separately reviewed Hub transfer and metadata migration or a trusted
multi-namespace transition; this component does not perform either operation.
The draft Hub provisioning adapter uses the server-only
`HF_CORPUS_PROVISION_TOKEN`, verifies the configured namespace is an actual Hub
organization, requests a private repository, and checks its returned identity
and privacy. The receipt includes the observed Git head, but this observation
does not establish the initial commit or authorize recording a bootstrap: other
writers may have changed the repository. Durable one-shot provisioning and
independent verification are still needed. It does not adopt an existing name
or retry an ambiguous write.
An unwired POSIX worker wrapper applies a request deadline, bounds worker
output, and accepts a trusted live-session lease. If the request times out,
an acquired lease stays held until the worker stops while its database
connection remains alive; the Hub write may still have been accepted and needs
reconciliation. If that connection is lost, PostgreSQL releases its lock and
the worker can still send a Hub write after logout. The current design does not
fence that case and must not be enabled for user-facing writes. Abrupt
application-process death and the hosted worker environment are also unverified.
This adapter is not exposed by an API or background job: a safe session-bound
write architecture, reconciliation, upload and readiness transitions are still
required. Privacy must also be checked at later sensitive operations; one successful observation does not keep
a repository private.

An unwired corpus-file reader checks owner-bound metadata, a ready state, a
specific Hub commit and point-in-time private visibility before streaming a
bounded file. It uses server-only `HF_CORPUS_READ_TOKEN` and does not forward
that token to redirected CDN hosts. It does not establish corpus readiness,
authenticate sessions, guarantee continuous privacy or provide a total request
deadline. Routes need a reviewed start-time session admission check and a trusted
revision before enabling it. The hosted runtime is unverified.

An additional unwired async transfer adapter applies one deadline across the
metadata lookup, Hub verification, file transfer and sink backpressure. It uses
a dedicated HTTP client for each transfer and allows a short cleanup period
after the deadline. Client setup is synchronous and must not block; its
transport and sink must cooperate with cancellation. Partial bytes already
delivered cannot be recalled. It does not integrate a live-session guard,
authorize revisions, or replace the existing reader.

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
For conversion jobs, the API validates the downloaded archive again before
using it; a saved result from an earlier validation is not sufficient.
Corpus detail, reference, and scope reads run the native loader in a separate
short-lived process per request so its state cannot cross server worker threads.
Results exceeding the worker's 3.9 MB encoded JSON limit return 413 rather than
being truncated; request a smaller page where the route supports pagination.


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

This repository was created from `exegia/corpora-template`. Work on `<type>/<slug>` branches and use Conventional Commit PR titles. Multiple feature branches are permitted only for related work in a dependent PR stack; otherwise merge the current feature into `dev` before starting another. The template release workflow and its branch policy remain in [AGENTS.md](AGENTS.md) and [`.github/WORKFLOW.md`](.github/WORKFLOW.md). Keep credentials out of Git and use `.env.example` only for placeholder configuration.

The release generator writes `CHANGELOG.md` with no trailing blank line so the release PR passes the pre-commit hygiene check. Run `make selftest` after changing that generator.

Promotion skips branches with matching trees. The release job builds Python distributions and attaches them when it creates the GitHub Release; later tag-triggered jobs handle registry publishing.
