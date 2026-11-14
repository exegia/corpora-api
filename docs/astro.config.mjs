// @ts-check
import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";
import press from "@fumapress/astro";
import vercel from "@astrojs/vercel";

export default defineConfig({
  site: "https://docs.exegia.co",
  adapter: vercel(),
  integrations: [
    press({
      collections: { docs: { dir: "content" } },
      lastModified: true,
      includeProcessedMarkdown: true,
    }),
  ],
  vite: {
    plugins: [tailwindcss()],
  },
});
