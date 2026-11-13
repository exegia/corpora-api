import { getCollection } from "astro:content";
import { toFumadocsSource } from "@fumadocs/astro/source";
import { createPress } from "@fumapress/astro/config";
import { aiPlugin, mcpPlugin } from "@fumapress/ai";
import { Redis } from "@upstash/redis";
import { Ratelimit } from "@upstash/ratelimit";
import { createOpenAPI } from "fumadocs-openapi/server";
import openapiPlugin from "fumadocs-openapi/press";
import type { Document } from "fumadocs-openapi";
import apiSpec from "./openapi.json";

const openapi = createOpenAPI({ input: { corpora: apiSpec as Document } });

async function guardChat(request: Request): Promise<Response | undefined> {
  if (
    (!process.env.AI_GATEWAY_API_KEY && !process.env.VERCEL_OIDC_TOKEN) ||
    !process.env.UPSTASH_REDIS_REST_URL ||
    !process.env.UPSTASH_REDIS_REST_TOKEN
  ) {
    return new Response("AI chat is not configured", { status: 503 });
  }

  const reader = request.clone().body?.getReader();
  if (!reader) return new Response("Missing request body", { status: 400 });
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 20_000) {
      await reader.cancel();
      return new Response("Request is too large", { status: 413 });
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const body = new TextDecoder().decode(bytes);
  try {
    const messages: unknown = (JSON.parse(body) as { messages?: unknown }).messages;
    if (!Array.isArray(messages) || messages.length > 8) {
      return new Response("Message limit exceeded", { status: 400 });
    }
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const limiter = new Ratelimit({
    redis: Redis.fromEnv(),
    limiter: Ratelimit.slidingWindow(5, "1 h"),
    prefix: "corpora-docs-ai",
  });
  const client = request.headers.get("x-vercel-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
  const { success } = await limiter.limit(client);
  if (!success) return new Response("Too many requests", { status: 429 });
}

export default createPress({
  docs: toFumadocsSource(await getCollection("docs"), await getCollection("meta")),
  openapi: await openapi.staticSource({ baseDir: "api" }),
}).plugins(
  openapiPlugin({ server: openapi }),
  mcpPlugin(),
  aiPlugin({
    model: "openai/gpt-5",
    systemPrompt: "Answer questions about the Corpora API using these docs. Cite the relevant page.",
    beforeRequest: guardChat,
  }),
).config({
  site: {
    name: "Corpora API",
  },
});
