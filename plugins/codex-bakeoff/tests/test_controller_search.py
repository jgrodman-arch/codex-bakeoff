"""Controller search behavior with the complete browser scripts loaded."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "replay_search_test_harness", Path(__file__).with_name("test_mcp_server.py")
)
if _spec is None or _spec.loader is None:
    raise AssertionError("Cannot load the local controller test harness.")
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
_CONTROLLER_HARNESS = _harness._CONTROLLER_HARNESS
CONTROLLER_PATH = _harness.CONTROLLER_PATH


class ControllerSearchTests(unittest.TestCase):
    def test_controller_search_resets_pages_and_ignores_stale_responses(self) -> None:
        harness = (
            _CONTROLLER_HARNESS
            + r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const handlers = {};
const timers = new Map();
let clock = 0;
let timerId = 0;
const requests = [];
const app = {innerHTML: "", addEventListener: (type, handler) => handlers[type] = handler};
class Input {
  constructor(value) { this.id = "thread-search"; this.value = value; }
}
const window = {
  addEventListener() {},
  setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, at: clock + delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
  callTool(name, args) {
    return new Promise((resolve, reject) => requests.push({name, args, resolve, reject}));
  },
};
const context = vm.createContext({
  window, console, HTMLInputElement: Input, HTMLTextAreaElement: Input,
  document: {getElementById: id => id === "app" ? app : null},
  requestAnimationFrame: fn => fn(),
  localStorage: {getItem: () => null, setItem() {}, removeItem() {}},
});
vm.runInContext(fs.readFileSync(require("node:path").join(
  require("node:path").dirname(process.argv[1]), "controller-ranges.js"), "utf8"), context);
const script = source.split("<script>")[1].split("</script>")[0];
vm.runInContext(script.replace("      initialize();", `
  callTool = window.callTool;
  startControllerHeartbeat = () => {};
  window.test = {state, loadThreads, initialize};
`), context);
const {state, loadThreads, initialize} = window.test;
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const advance = async ms => {
  clock += ms;
  for (const [id, timer] of timers) {
    if (timer.at <= clock) { timers.delete(id); timer.fn(); }
  }
  await flush();
};
const type = value => handlers.input({target: new Input(value)});
const click = (action, source) => handlers.click({target: {
  closest: () => ({dataset: {action, source}}),
}});
const imported = Array.from({length: 71}, (_, i) => ({
  imported_thread_id: `thread-${i}`, title: i === 70 ? "NH game new feature" : `Task ${i}`,
}));
const samples = [{imported_thread_id: "sample-1", title: "Sample task"}];
const reply = async request => {
  const args = request.args;
  const rows = args.source === "sample" ? samples : imported;
  const filtered = rows.filter(row => !args.query || row.title.includes(args.query));
  request.resolve({threads: filtered.slice(args.offset, args.offset + args.limit),
    total: filtered.length, source: args.source || "imported", imported_total: 71, sample_total: 1});
  await flush();
};
const shown = (loaded, total) => assert.ok(app.innerHTML.includes(`${loaded} of ${total} shown`));
const more = () => app.innerHTML.includes('data-action="load-more-threads"');
(async () => {
  // An in-flight bootstrap must not overwrite a search or finish its loading state.
  const initializing = initialize();
  const bootstrap = requests.find(request => request.name === "list_threads");
  type("NH");
  await advance(200);
  const earlySearch = requests.at(-1);
  requests.find(request => request.name === "get_state").resolve({state: {
    controller_session_id: "search-test", models: [], recent_runs: [],
  }});
  await reply(bootstrap);
  await initializing;
  assert.equal(state.loading, true);
  assert.equal(state.threads.length, 0);
  await reply(earlySearch);
  shown(1, 1);

  const retrying = initialize();
  const staleBootstrap = requests.at(-1);
  type("Task"); await advance(200); await reply(requests.at(-1));
  requests.filter(request => request.name === "get_state").at(-1).resolve({state: {
    controller_session_id: "search-test", models: [], recent_runs: [],
  }});
  staleBootstrap.reject(new Error("stale bootstrap failure"));
  await retrying;
  shown(20, 70); assert.equal(state.error, ""); assert.equal(state.hostReady, true);

  type(""); await advance(200); await reply(requests.at(-1)); shown(20, 71);
  assert.equal(more(), true);
  state.selectedThreadId = "thread-0";
  state.selectedThread = state.threads[0];
  // A rapid query edit resets immediately, but sends only one debounced request.
  const count = requests.length;
  type("N"); await advance(100); type("NH game new feature"); await advance(199);
  assert.equal(requests.length, count);
  assert.equal(state.threads.length, 0);
  assert.equal(more(), false);
  await advance(1);
  assert.equal(requests.length, count + 1);
  assert.equal(requests.at(-1).args.offset, 0);
  await reply(requests.at(-1)); shown(1, 1);
  assert.equal(state.threads[0].imported_thread_id, "thread-70");
  assert.equal(more(), false);
  assert.equal(state.selectedThreadId, "thread-0");
  assert.equal(state.selectedThread.imported_thread_id, "thread-0");

  type("Task"); await advance(200); await reply(requests.at(-1)); shown(20, 70);
  click("load-more-threads");
  const page = requests.at(-1);
  assert.equal(page.args.offset, 20);
  assert.equal(page.args.query, "Task");
  click("load-more-threads"); assert.equal(requests.at(-1), page);
  await reply(page); shown(40, 70);
  click("load-more-threads"); const stalePage = requests.at(-1);
  type("");
  await reply(stalePage); // Stale even before the debounce fires.
  assert.equal(state.loading, true); assert.equal(state.threads.length, 0);
  await advance(200); assert.equal(requests.at(-1).args.offset, 0);
  assert.equal(requests.at(-1).args.query, undefined);
  await reply(requests.at(-1)); shown(20, 71);
  click("load-more-threads"); await reply(requests.at(-1)); shown(40, 71);

  type("Task"); await advance(200); const oldQuery = requests.at(-1);
  type("NH"); await advance(200); await reply(requests.at(-1));
  await reply(oldQuery); shown(1, 1);
  // Refresh cancels the queued debounce and supersedes an in-flight request.
  type("Task"); const beforeRefresh = requests.length;
  click("refresh-threads"); const oldRefresh = requests.at(-1);
  await advance(200); assert.equal(requests.length, beforeRefresh + 1);
  click("refresh-threads"); await reply(requests.at(-1));
  oldRefresh.reject(new Error("stale failure")); await flush();
  shown(20, 70); assert.equal(state.error, "");

  click("load-more-threads"); const oldSource = requests.at(-1);
  type("NH"); click("thread-source", "sample");
  const sampleRequest = requests.at(-1);
  assert.equal(sampleRequest.args.source, "sample");
  assert.equal(sampleRequest.args.offset, 0); assert.equal(sampleRequest.args.query, undefined);
  await reply(sampleRequest); await reply(oldSource); await advance(200);
  assert.equal(requests.at(-1), sampleRequest);
  shown(1, 1); assert.equal(state.threadSource, "sample"); assert.equal(state.query, "");
  click("thread-source", "imported"); await reply(requests.at(-1)); shown(20, 71);
  type("absent"); await advance(200); await reply(requests.at(-1)); shown(0, 0);
  assert.equal(more(), false);
  type("Task"); await advance(200); requests.at(-1).reject(new Error("current failure"));
  await flush(); assert.equal(state.error, "Error: current failure");
  click("refresh-threads"); await reply(requests.at(-1)); shown(20, 70);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        )
        result = subprocess.run(
            ["node", "-e", harness, str(CONTROLLER_PATH)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
