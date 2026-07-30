import { chmod, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const workerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pluginRoot = path.resolve(workerRoot, "..");
const outputPath = path.join(pluginRoot, "mcp", "codex-worker.mjs");

await mkdir(path.dirname(outputPath), { recursive: true });
await build({
  entryPoints: [path.join(workerRoot, "src", "codex-worker.mjs")],
  outfile: outputPath,
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node18",
  legalComments: "none",
  banner: { js: "#!/usr/bin/env node" }
});
await chmod(outputPath, 0o755);
