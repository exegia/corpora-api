# Corpora API docs

Astro/Fumapress site for `docs.exegia.co`. Its `/api` pages are generated from
`openapi.json`, exported from the FastAPI app with
`uv run python scripts/export_openapi.py` at the repository root.
The playground calls `https://api.exegia.co/v1/*` and documents bearer auth,
multipart conversion uploads, ZIP responses, and PDF score-gated reports.

## Development

```sh
npm run dev
```

`npm run types:check` and `npm run build` verify the site. On Vercel, create an
Astro project from `exegia/corpora-api` with Root Directory `docs` and attach
`docs.exegia.co`. The Vercel adapter serves documentation search at `/mcp`
and AI chat; OpenAPI pages are prerendered.

AI chat returns 503 until `AI_GATEWAY_API_KEY`, `UPSTASH_REDIS_REST_URL`, and
`UPSTASH_REDIS_REST_TOKEN` are set in the Vercel project. The request guard
caps body size, message count, and requests per hour. `/llms.txt` and
`/llms-full.txt` are generated automatically.
