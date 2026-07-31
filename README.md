# Codex Bakeoff

Codex Bakeoff compares one imported historical Claude coding task with a fresh
Codex implementation from the same baseline. A local browser controller guides
selection, approval, execution, verification, blinded review, and reporting.

## Prerequisites

- macOS or Linux with Python 3.9 or newer, Node.js 18 or newer, Git, and
  `lsof`.
- The current Codex CLI installed, authenticated, and available as `codex`, or
  its path set in `CODEX_CLI_PATH`.
- At least one imported historical Claude coding task in Codex, with its source
  checkout still available locally.
- For workspace installation, a ChatGPT workspace that supports GitHub plugin
  imports and a workspace admin.

The installed plugin is prebuilt. Node.js is needed to run the bundled Codex
worker; npm is only needed when rebuilding the worker from source.

## Install in Codex

1. Open **Plugins → Add → Add a marketplace**.
2. Set **Source** to `https://github.com/jgrodman-arch/codex-bakeoff` and
   leave the other fields empty.
3. Search Plugins for **Codex Bakeoff**, then install it.
4. Open a new chat and invoke the **@Codex Bakeoff** plugin. The controller UI
   should open in Chrome.

The plugin never downloads or replaces its own source. After a release is
published, a workspace admin can force a refresh with **Update plugin** from the
plugin row menu.

## Install with the Codex CLI

```sh
codex plugin marketplace add jgrodman-arch/codex-bakeoff --ref main
codex plugin add codex-bakeoff@codex-bakeoff
```

Start a fresh Codex task after installation.

## Development

From the repository root:

```sh
cd plugins/codex-bakeoff/worker
npm ci --registry=https://registry.npmjs.org
npm test

cd ..
python3 -m unittest discover -s tests -p 'test_*.py'
```

The runtime worker is committed at
`plugins/codex-bakeoff/mcp/codex-worker.mjs`. Rebuild it before every release,
then run the Codex plugin validator against `plugins/codex-bakeoff`.

## License

Apache-2.0. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
