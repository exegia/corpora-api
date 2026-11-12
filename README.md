# Corpora API

FastAPI and FastMCP interfaces for [corpora-core](https://github.com/exegia/corpora-core). The Python service sends JSON Lines requests to the Rust project's supported `corpora-text-check` executable. It does not reimplement parsing, language selection, metadata policy, or spelling.

## What is exposed

| REST operation | MCP tool | Core operation |
| --- | --- | --- |
| `POST /v1/checks/structure` | `check_xml_structure` | Strict XML check and source-bound diagnostics |
| `POST /v1/checks/tei-metadata` | `check_tei_purpose` | TEI date and primary identifier policy |
| `POST /v1/language/detect` | `detect_text_language` | Language evidence and dictionary selection |
| `POST /v1/checks/spelling` | `check_text_spelling` | Optional installed-dictionary spelling report |

`GET /health` reports API process health. `GET /openapi.json` is the exact schema used by the [Fumapress docs](docs/README.md); the API tools are mounted at `/mcp/`.

The CLI's structural operation uses the core default XML options. TEI-specific `StructuralOptions` from the Rust library are not available through that CLI, so this API does not claim to perform a full TEI structural validation. A nonblocking report does not establish schema validity or conversion success. The service never applies suggestions to source text.

## Local development

Requirements: Python 3.12+, uv, Rust 1.91+ to build the core executable, Node.js 22.23.4+ for docs.

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

Set `CORPORA_DICTIONARIES_DIR` to a directory installed by [the core dictionary installer](https://github.com/exegia/corpora-core/tree/main/crates/corpora-core/tools) to enable spelling. Without it, the spelling endpoint returns 503; the other checks remain available. For a public API host, set `CORPORA_API_TOKEN`; requests to `/v1/` and `/mcp/` then require `Authorization: Bearer <token>`.

The service limits text to one million characters. Each core request has a 10-second deadline (30 seconds for spelling). It rejects CLI errors, invalid JSON, unsupported diagnostic schema versions, and stale source hashes.

## Docs and OpenAPI

The Astro/Fumapress site lives in `docs/`. It generates `/api` operation pages from the checked-in `docs/openapi.json` file. The pages include request bodies, response schemas, errors, examples, and the OpenAPI playground. `/mcp` on the docs host is a separate documentation-search MCP server, not the core-checking API MCP server.

After changing Python routes or models:

```sh
uv run python scripts/export_openapi.py
make ci
```

`make ci` installs both Python and docs dependencies, runs template checks and Python tests, type-checks docs, and builds the Vercel output. The optional real-core integration test runs when `CORPORA_TEXT_CHECK_BIN` points to a built executable. The docs site can be built directly with `npm ci --prefix docs && npm run build --prefix docs`.

Fumapress AI chat is included with a request-size cap, an eight-message cap, and a five-request-per-hour Upstash limit per client. It returns 503 until `AI_GATEWAY_API_KEY`, `UPSTASH_REDIS_REST_URL`, and `UPSTASH_REDIS_REST_TOKEN` are set in the Vercel docs project. Fumapress also publishes `/llms.txt` and `/llms-full.txt`.

## Deploying

The `Dockerfile` builds a pinned commit of `corpora-core` and packages its executable with the Python API. Deploy that image to a container host, configure `CORPORA_API_TOKEN`, and optionally mount installed dictionaries. The docs site is a separate Vercel project with root directory `docs` and Astro preset. Its target domain is `docs.exegia.co`, with the generated API reference at `/api`.

The checked-in OpenAPI spec targets `http://localhost:8000`. Set a real API origin and regenerate the spec before enabling a hosted playground. Do not point the site at a nonexistent production API. The docs and API deployments can be released independently, but the OpenAPI export must stay in sync with the Python app.

## Repository conventions

This repository was created from `exegia/corpora-template`. Work on `<type>/<slug>` branches and use Conventional Commit PR titles. The template release workflow and its branch policy remain in [AGENTS.md](AGENTS.md) and [`.github/WORKFLOW.md`](.github/WORKFLOW.md). Keep credentials out of Git and use `.env.example` only for placeholder configuration.
