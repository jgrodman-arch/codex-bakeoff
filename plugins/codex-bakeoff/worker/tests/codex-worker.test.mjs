import assert from "node:assert/strict";
import { once } from "node:events";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  executeRunRequest,
  normalizeRunRequest
} from "../src/codex-worker.mjs";

const workerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pluginRoot = path.resolve(workerRoot, "..");
const builtWorkerPath = path.join(pluginRoot, "mcp", "codex-worker.mjs");
const temporaryRoots = [];

test.after(async () => {
  await Promise.all(
    temporaryRoots.map(async (root) => {
      await rm(root, { recursive: true, force: true });
    })
  );
});

test("normalizes server aliases and keeps SDK events sanitized", async () => {
  const request = normalizeRunRequest({
    type: "run",
    requestId: "fixture-1",
    model: "gpt-5.6-sol",
    prompt: "private prompt",
    workingDirectory: "/private/fixture",
    timeoutSeconds: 30,
    sandboxMode: "workspace-write",
    networkAccess: false,
    outputSchema: { type: "object" }
  });
  assert.equal(request.id, "fixture-1");
  assert.equal(request.timeoutMs, 30_000);
  assert.equal(request.networkAccessEnabled, false);

  const emitted = [];
  let observedOptions;
  let observedTurnOptions;
  const result = await executeRunRequest(request, {
    emit: (event) => emitted.push(event),
    codexFactory: () => ({
      startThread(options) {
        observedOptions = options;
        return {
          id: "fixture-thread",
          async runStreamed(prompt, turnOptions) {
            assert.equal(prompt, "private prompt");
            observedTurnOptions = turnOptions;
            return {
              events: (async function* () {
                yield { type: "thread.started", thread_id: "fixture-thread" };
                yield { type: "turn.started" };
                yield {
                  type: "item.completed",
                  item: {
                    type: "command_execution",
                    command: "secret command",
                    aggregated_output: "secret output",
                    status: "completed"
                  }
                };
                yield {
                  type: "item.completed",
                  item: { type: "agent_message", text: "finished" }
                };
                yield {
                  type: "turn.completed",
                  usage: {
                    input_tokens: 11,
                    cached_input_tokens: 2,
                    output_tokens: 3,
                    reasoning_output_tokens: 1
                  }
                };
              })()
            };
          }
        };
      }
    })
  });

  assert.equal(observedOptions.model, "gpt-5.6-sol");
  assert.equal(observedOptions.approvalPolicy, "never");
  assert.equal(observedOptions.networkAccessEnabled, false);
  assert.equal(observedOptions.sandboxMode, "workspace-write");
  assert.deepEqual(observedTurnOptions.outputSchema, { type: "object" });
  assert.equal(result.threadId, "fixture-thread");
  assert.equal(result.finalResponse, "finished");
  assert.equal(result.usage.input_tokens, 11);
  const serializedEvents = JSON.stringify(emitted);
  assert.doesNotMatch(serializedEvents, /private prompt|secret command|secret output/);
  assert.match(serializedEvents, /thread_started|item_completed/);
});

test("logs an SDK stream error without exposing its detail in the protocol error", async () => {
  const request = normalizeRunRequest({
    type: "run",
    id: "stream-error",
    model: "gpt-5.6-sol",
    prompt: "private prompt",
    workingDirectory: "/private/fixture",
    timeoutMs: 30_000,
    sandboxMode: "read-only",
    networkAccessEnabled: false
  });

  const diagnostics = [];
  await assert.rejects(
    executeRunRequest(request, {
      diagnostic: (message) => diagnostics.push(message),
      codexFactory: () => ({
        startThread() {
          return {
            id: "fixture-thread",
            async runStreamed() {
              return {
                events: (async function* () {
                  yield { type: "thread.started", thread_id: "fixture-thread" };
                  yield { type: "error", message: "sensitive provider detail" };
                  yield {
                    type: "turn.completed",
                    usage: {
                      input_tokens: 0,
                      cached_input_tokens: 0,
                      output_tokens: 0,
                      reasoning_output_tokens: 0
                    }
                  };
                })()
              };
            }
          };
        }
      })
    }),
    (error) =>
      error instanceof Error &&
      error.code === "stream_error" &&
      error.message === "Codex reported an unrecoverable stream error." &&
      !error.message.includes("sensitive provider detail")
  );
  assert.deepEqual(diagnostics, ["Codex stream error: sensitive provider detail"]);
});

test("logs an SDK turn failure without exposing its detail in the protocol error", async () => {
  const request = normalizeRunRequest({
    type: "run",
    id: "turn-failure",
    model: "gpt-5.6-sol",
    prompt: "private prompt",
    workingDirectory: "/private/fixture",
    timeoutMs: 30_000,
    sandboxMode: "read-only",
    networkAccessEnabled: false
  });
  const diagnostics = [];

  await assert.rejects(
    executeRunRequest(request, {
      diagnostic: (message) => diagnostics.push(message),
      codexFactory: () => ({
        startThread() {
          return {
            id: "fixture-thread",
            async runStreamed() {
              return {
                events: (async function* () {
                  yield { type: "thread.started", thread_id: "fixture-thread" };
                  yield {
                    type: "turn.failed",
                    error: { message: "sensitive turn detail" }
                  };
                })()
              };
            }
          };
        }
      })
    }),
    (error) =>
      error instanceof Error &&
      error.code === "turn_failed" &&
      error.message === "Codex turn failed." &&
      !error.message.includes("sensitive turn detail")
  );
  assert.deepEqual(diagnostics, ["Codex turn failed: sensitive turn detail"]);
});

test("writes SDK error detail only to worker stderr", async () => {
  const fixture = await fakeCodexFixture();
  const child = spawn(process.execPath, [builtWorkerPath], {
    env: {
      ...process.env,
      CODEX_CLI_PATH: fixture.executablePath
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const stdout = collectLines(child.stdout);
  const stderr = collectText(child.stderr);
  child.stdin.end(
    `${JSON.stringify({
      type: "run",
      id: "stdio-stream-error",
      model: "gpt-5.6-sol",
      prompt: "STREAM_ERROR",
      workingDirectory: fixture.root,
      timeoutMs: 10_000,
      sandboxMode: "workspace-write",
      networkAccessEnabled: false
    })}\n`
  );

  const [code, signal] = await once(child, "exit");
  assert.equal(code, 1);
  assert.equal(signal, null);
  const messages = await stdout;
  const failure = messages.at(-1);
  assert.equal(failure.type, "failed");
  assert.equal(failure.message, "Codex reported an unrecoverable stream error.");
  assert.doesNotMatch(JSON.stringify(messages), /fixture provider detail/);
  assert.match(await stderr, /Codex stream error: fixture provider detail/);
});

test("built JSONL worker executes one SDK run and returns the observed result", async () => {
  const fixture = await fakeCodexFixture();
  const child = spawn(process.execPath, [builtWorkerPath], {
    env: {
      ...process.env,
      CODEX_CLI_PATH: fixture.executablePath,
      FAKE_CODEX_ARGS_FILE: fixture.argsPath
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const stdout = collectLines(child.stdout);
  const stderr = collectText(child.stderr);
  child.stdin.end(
    `${JSON.stringify({
      type: "run",
      requestId: "stdio-1",
      model: "gpt-5.6-sol",
      prompt: "fixture prompt",
      workingDirectory: fixture.root,
      timeoutSeconds: 10,
      sandboxMode: "workspace-write",
      networkAccess: false
    })}\n`
  );

  const [code, signal] = await once(child, "exit");
  assert.equal(code, 0, await stderr);
  assert.equal(signal, null);
  const messages = await stdout;
  assert.equal(messages[0].type, "ready");
  assert.equal(messages[1].type, "accepted");
  const completed = messages.find((message) => message.type === "completed");
  assert.ok(completed);
  assert.equal(completed.id, "stdio-1");
  assert.equal(completed.threadId, "fixture-thread-id");
  assert.equal(completed.finalResponse, "fixture final response");
  assert.equal(completed.usage.input_tokens, 7);
  const serialized = JSON.stringify(messages);
  assert.doesNotMatch(serialized, /fixture-secret-command|fixture-secret-output/);
  const cliArgs = JSON.parse(await readFile(fixture.argsPath, "utf8"));
  assert.deepEqual(cliArgs.slice(0, 3), [
    "exec",
    "--ignore-user-config",
    "--ignore-rules"
  ]);
});

test("SIGTERM cancels the active SDK run without retrying", async () => {
  const fixture = await fakeCodexFixture();
  const child = spawn(process.execPath, [builtWorkerPath], {
    env: {
      ...process.env,
      CODEX_CLI_PATH: fixture.executablePath,
      FAKE_TREE_TERMINATED_FILE: fixture.terminationPath
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const messages = [];
  let buffered = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffered += chunk;
    const lines = buffered.split("\n");
    buffered = lines.pop() ?? "";
    for (const line of lines) {
      if (!line) continue;
      const message = JSON.parse(line);
      messages.push(message);
      if (message.type === "lifecycle" && message.phase === "thread_started") {
        child.kill("SIGTERM");
      }
    }
  });
  child.stdin.write(
    `${JSON.stringify({
      type: "run",
      id: "stdio-cancel",
      model: "gpt-5.6-sol",
      prompt: "HANG",
      workingDirectory: fixture.root,
      timeoutMs: 10_000,
      sandboxMode: "workspace-write",
      networkAccessEnabled: false
    })}\n`
  );

  const [code, signal] = await once(child, "exit");
  assert.equal(code, 143);
  assert.equal(signal, null);
  const canceled = messages.find((message) => message.type === "canceled");
  assert.ok(canceled);
  assert.equal(canceled.reason, "SIGTERM");
  assert.equal(messages.filter((message) => message.type === "accepted").length, 1);
  await waitForFile(fixture.terminationPath);
});

test("timeout terminates the active Codex process tree", async () => {
  const fixture = await fakeCodexFixture();
  const child = spawn(process.execPath, [builtWorkerPath], {
    env: {
      ...process.env,
      CODEX_CLI_PATH: fixture.executablePath,
      FAKE_TREE_TERMINATED_FILE: fixture.terminationPath
    },
    stdio: ["pipe", "pipe", "pipe"]
  });
  const stdout = collectLines(child.stdout);
  const stderr = collectText(child.stderr);
  child.stdin.end(
    `${JSON.stringify({
      type: "run",
      id: "stdio-timeout",
      model: "gpt-5.6-sol",
      prompt: "HANG",
      workingDirectory: fixture.root,
      timeoutMs: 1_000,
      sandboxMode: "workspace-write",
      networkAccessEnabled: false
    })}\n`
  );

  const [code, signal] = await once(child, "exit");
  assert.equal(code, 124, await stderr);
  assert.equal(signal, null);
  const messages = await stdout;
  assert.equal(messages.at(-1)?.type, "failed");
  assert.equal(messages.at(-1)?.code, "timeout");
  await waitForFile(fixture.terminationPath);
});

async function fakeCodexFixture() {
  const root = await mkdtemp(path.join(tmpdir(), "codex-bakeoff-worker-"));
  temporaryRoots.push(root);
  const executablePath = path.join(root, "fake-codex.mjs");
  const grandchildPath = path.join(root, "fake-grandchild.mjs");
  const argsPath = path.join(root, "args.json");
  const terminationPath = path.join(root, "tree-terminated");
  await writeFile(
    grandchildPath,
    [
      "#!/usr/bin/env node",
      "import { writeFileSync } from 'node:fs';",
      "process.on('SIGTERM', () => {",
      "  writeFileSync(process.env.FAKE_TREE_TERMINATED_FILE, 'terminated');",
      "  process.exit(0);",
      "});",
      "console.log('ready');",
      "setInterval(() => {}, 1000);",
      ""
    ].join("\n")
  );
  await chmod(grandchildPath, 0o755);
  await writeFile(
    executablePath,
    [
      "#!/usr/bin/env node",
      "import { spawn } from 'node:child_process';",
      "import { writeFileSync } from 'node:fs';",
      "if (process.env.FAKE_CODEX_ARGS_FILE) writeFileSync(process.env.FAKE_CODEX_ARGS_FILE, JSON.stringify(process.argv.slice(2)));",
      "let prompt = '';",
      "for await (const chunk of process.stdin) prompt += chunk;",
      `if (prompt.includes('HANG')) {`,
      `  const grandchild = spawn(${JSON.stringify(grandchildPath)}, [], { stdio: ['ignore', 'pipe', 'ignore'] });`,
      "  await new Promise((resolve, reject) => { grandchild.stdout.once('data', resolve); grandchild.once('error', reject); });",
      "}",
      "console.log(JSON.stringify({ type: 'thread.started', thread_id: 'fixture-thread-id' }));",
      "console.log(JSON.stringify({ type: 'turn.started' }));",
      "if (prompt.includes('HANG')) { setInterval(() => {}, 1000); await new Promise(() => {}); }",
      "if (prompt.includes('STREAM_ERROR')) { console.log(JSON.stringify({ type: 'error', message: 'fixture provider detail' })); process.exit(0); }",
      "console.log(JSON.stringify({ type: 'item.completed', item: { type: 'command_execution', command: 'fixture-secret-command', aggregated_output: 'fixture-secret-output', status: 'completed' } }));",
      "console.log(JSON.stringify({ type: 'item.completed', item: { type: 'agent_message', text: 'fixture final response' } }));",
      "console.log(JSON.stringify({ type: 'turn.completed', usage: { input_tokens: 7, cached_input_tokens: 2, output_tokens: 3, reasoning_output_tokens: 1 } }));",
      ""
    ].join("\n")
  );
  await chmod(executablePath, 0o755);
  return { root, executablePath, argsPath, terminationPath };
}

async function waitForFile(filePath) {
  const deadline = Date.now() + 3_000;
  while (Date.now() < deadline) {
    try {
      return await readFile(filePath, "utf8");
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  }
  assert.fail(`Timed out waiting for ${filePath}`);
}

function collectLines(stream) {
  return new Promise((resolve, reject) => {
    const messages = [];
    let buffered = "";
    stream.setEncoding("utf8");
    stream.on("data", (chunk) => {
      buffered += chunk;
      const lines = buffered.split("\n");
      buffered = lines.pop() ?? "";
      try {
        for (const line of lines) {
          if (line) messages.push(JSON.parse(line));
        }
      } catch (error) {
        reject(error);
      }
    });
    stream.on("end", () => {
      if (buffered.trim()) {
        try {
          messages.push(JSON.parse(buffered));
        } catch (error) {
          reject(error);
          return;
        }
      }
      resolve(messages);
    });
    stream.on("error", reject);
  });
}

function collectText(stream) {
  return new Promise((resolve, reject) => {
    let text = "";
    stream.setEncoding("utf8");
    stream.on("data", (chunk) => {
      text += chunk;
    });
    stream.on("end", () => resolve(text));
    stream.on("error", reject);
  });
}
