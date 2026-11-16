// Isolated in-memory test only; the server binds an ephemeral loopback port.
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const require = createRequire(join(process.env.CORPORA_PGLITE_ROOT, "package.json"));
const { PGlite } = await import(pathToFileURL(require.resolve("@electric-sql/pglite")));
const { PGLiteSocketServer } = await import(
  pathToFileURL(require.resolve("@electric-sql/pglite-socket"))
);
const db = await PGlite.create();
const server = new PGLiteSocketServer({ db, host: "127.0.0.1", port: 0 });
await server.start();
console.log(server.server.address().port);
process.on("SIGTERM", async () => {
  await server.stop();
  await db.close();
  process.exit(0);
});
