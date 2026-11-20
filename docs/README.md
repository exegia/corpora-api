# Corpora API docs

Astro/Fumapress site for `docs.exegia.co`. Its `/api` pages are generated from
`openapi.json`, exported from the FastAPI app with
`uv run python scripts/export_openapi.py` at the repository root.
The playground calls `https://api.exegia.co/v1/*` and documents bearer auth,
multipart conversion uploads, ZIP responses, and PDF score-gated reports.
The `/api` reference also documents `POST /v1/checks/text-fabric` for a
bounded static TF ZIP preflight. The API MCP server at `api.exegia.co/v1/mcp/`
exposes the matching `check_text_fabric_archive` tool and `text_fabric_checker`
host prompt; the docs site's `/mcp` remains documentation search.

## Development

```sh
npm run dev
```

`npm run types:check` and `npm run build` verify the site. On Vercel, create an
Astro project from `exegia/corpora-api` with Root Directory `docs` and attach
`docs.exegia.co`. The Vercel adapter serves documentation search at `/mcp`
and AI chat; OpenAPI pages are prerendered.

`npm install` applies the version-checked `fumadocs-openapi` 12.6.2 example
patch. It unwraps sampled JSON examples before the playground clones them.
Remove the patch after upgrading to a version that fixes this upstream.

AI chat uses Vercel's deployment OIDC token for AI Gateway authentication, or
`AI_GATEWAY_API_KEY` outside Vercel. It returns 503 until
`UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are set in the Vercel project. The request guard
caps body size, message count, and requests per hour. `/llms.txt` and
`/llms-full.txt` are generated automatically.

## Content map

- `content/api/index.mdx`: beginner API overview.
- `content/api/`: quickstart, authentication, corpus lifecycle, architecture, testing, endpoint directory.
- `content/cli/`: the `corpora` command line: install, every command and option, exit codes.
  Owned by `exegia/homebrew-corpora` changes; its agents update this page when a CLI change
  merges to `dev`. Terminal previews in `public/images/cli/` come from that repository's
  `docs/record.py`.
- `content/core/`: core introduction and shared QA cleanup instructions.

The API repository owns these public pages. Core repository contributor markdown
is linked explicitly; it is not automatically mirrored onto this website.
The endpoint directory links to the generated operation pages under `/api`.
