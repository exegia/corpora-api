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

| Conversion endpoint | Result |
| --- | --- |
| `POST /v1/conversions/tei-to-tf` | TEI source, Text-Fabric features, manifest and checksums |
| `POST /v1/conversions/epub-to-tf` | EPUB source, assets, Text-Fabric features and manifest |
| `POST /v1/conversions/pdf-to-tf` | Score-gated PDF extraction, TEI, Text-Fabric and review report |

`GET /v1/health` reports API process health. `GET /v1/capabilities` publicly reports whether a service token is required and currently returns `hub_writable: false` because Hub writes are not hosted here. `GET /v1/openapi.json` is the exact schema used by the [Fumapress docs](docs/README.md); the API tools are mounted at `/v1/mcp/`. The existing `api.exegia.co` service keeps its routes outside `/v1/*`.

The MCP server also advertises a `text_fabric_checker` prompt for a host agent. Upload a ZIP containing one `.tf` root to the REST route, or pass its base64 bytes to the MCP tool. The checker reports an archive SHA-256, slot/node counts and located errors in `otype.tf`, `oslots.tf` and `otext.tf`. It rejects unsafe paths, links, duplicate names, archives above 3 MB, over 3,000 files or over 64 MB unpacked. A `passed` result is a **static WARP preflight only**; use a real Text-Fabric/Context-Fabric load and source comparison for full validation. The local `text-fabric-validator` skill is not bundled in this service because its redistribution license is unspecified.

The CLI's structural operation uses the core default XML options. TEI-specific `StructuralOptions` from the Rust library are not available through that CLI, so this API does not claim to perform a full TEI structural validation. A nonblocking report does not establish schema validity or conversion success. The service never applies suggestions to source text.

The spelling response keeps its existing flat `start` and `end` fields. The API validates those Unicode scalar offsets with the generated `corpora-schema-types` `TextRange` model from the same pinned core commit used by `Dockerfile.vercel`. Update the Git revision in `pyproject.toml` and `CORPORA_CORE_REF` together when adopting a new core contract.

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

The checked-in OpenAPI spec targets `https://api.exegia.co`; every operation path begins with `/v1/`. Vercel routes only that prefix from the existing `corpora-py` project to the new container service, preserving the older API's other endpoints. The docs and API deployments can be released independently, but the OpenAPI export must stay in sync with the Python app.

## Repository conventions

This repository was created from `exegia/corpora-template`. Work on `<type>/<slug>` branches and use Conventional Commit PR titles. The template release workflow and its branch policy remain in [AGENTS.md](AGENTS.md) and [`.github/WORKFLOW.md`](.github/WORKFLOW.md). Keep credentials out of Git and use `.env.example` only for placeholder configuration.

The release generator writes `CHANGELOG.md` with no trailing blank line so the release PR passes the pre-commit hygiene check. Run `make selftest` after changing that generator.

Promotion skips branches with matching trees. The release job builds Python distributions and attaches them when it creates the GitHub Release; later tag-triggered jobs handle registry publishing.
