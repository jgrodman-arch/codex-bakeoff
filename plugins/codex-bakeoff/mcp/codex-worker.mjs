#!/usr/bin/env node

// src/codex-worker.mjs
import { createInterface } from "node:readline";
import { spawn as spawn2 } from "node:child_process";
import path3 from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// node_modules/@openai/codex-sdk/dist/index.js
import { promises as fs } from "fs";
import os from "os";
import path from "path";
import { spawn } from "child_process";
import { statSync } from "fs";
import path2 from "path";
import readline from "readline";
import { createRequire } from "module";
async function createOutputSchemaFile(schema) {
  if (schema === void 0) {
    return { cleanup: async () => {
    } };
  }
  if (!isJsonObject(schema)) {
    throw new Error("outputSchema must be a plain JSON object");
  }
  const schemaDir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-output-schema-"));
  const schemaPath = path.join(schemaDir, "schema.json");
  const cleanup = async () => {
    try {
      await fs.rm(schemaDir, { recursive: true, force: true });
    } catch {
    }
  };
  try {
    await fs.writeFile(schemaPath, JSON.stringify(schema), "utf8");
    return { schemaPath, cleanup };
  } catch (error) {
    await cleanup();
    throw error;
  }
}
function isJsonObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
var Thread = class {
  _exec;
  _options;
  _id;
  _threadOptions;
  /** Returns the ID of the thread. Populated after the first turn starts. */
  get id() {
    return this._id;
  }
  /* @internal */
  constructor(exec, options, threadOptions, id = null) {
    this._exec = exec;
    this._options = options;
    this._id = id;
    this._threadOptions = threadOptions;
  }
  /** Provides the input to the agent and streams events as they are produced during the turn. */
  async runStreamed(input, turnOptions = {}) {
    return { events: this.runStreamedInternal(input, turnOptions) };
  }
  async *runStreamedInternal(input, turnOptions = {}) {
    const { schemaPath, cleanup } = await createOutputSchemaFile(turnOptions.outputSchema);
    const options = this._threadOptions;
    const { prompt, images } = normalizeInput(input);
    const generator = this._exec.run({
      input: prompt,
      baseUrl: this._options.baseUrl,
      apiKey: this._options.apiKey,
      threadId: this._id,
      images,
      model: options?.model,
      sandboxMode: options?.sandboxMode,
      workingDirectory: options?.workingDirectory,
      skipGitRepoCheck: options?.skipGitRepoCheck,
      outputSchemaFile: schemaPath,
      modelReasoningEffort: options?.modelReasoningEffort,
      signal: turnOptions.signal,
      networkAccessEnabled: options?.networkAccessEnabled,
      webSearchMode: options?.webSearchMode,
      webSearchEnabled: options?.webSearchEnabled,
      approvalPolicy: options?.approvalPolicy,
      additionalDirectories: options?.additionalDirectories
    });
    try {
      for await (const item of generator) {
        let parsed;
        try {
          parsed = JSON.parse(item);
        } catch (error) {
          throw new Error(`Failed to parse item: ${item}`, { cause: error });
        }
        if (parsed.type === "thread.started") {
          this._id = parsed.thread_id;
        }
        yield parsed;
      }
    } finally {
      await cleanup();
    }
  }
  /** Provides the input to the agent and returns the completed turn. */
  async run(input, turnOptions = {}) {
    const generator = this.runStreamedInternal(input, turnOptions);
    const items = [];
    let finalResponse = "";
    let usage = null;
    let turnFailure = null;
    for await (const event of generator) {
      if (event.type === "item.completed") {
        if (event.item.type === "agent_message") {
          finalResponse = event.item.text;
        }
        items.push(event.item);
      } else if (event.type === "turn.completed") {
        usage = event.usage;
      } else if (event.type === "turn.failed") {
        turnFailure = event.error;
        break;
      }
    }
    if (turnFailure) {
      throw new Error(turnFailure.message);
    }
    return { items, finalResponse, usage };
  }
};
function normalizeInput(input) {
  if (typeof input === "string") {
    return { prompt: input, images: [] };
  }
  const promptParts = [];
  const images = [];
  for (const item of input) {
    if (item.type === "text") {
      promptParts.push(item.text);
    } else if (item.type === "local_image") {
      images.push(item.path);
    }
  }
  return { prompt: promptParts.join("\n\n"), images };
}
var INTERNAL_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE";
var TYPESCRIPT_SDK_ORIGINATOR = "codex_sdk_ts";
var CODEX_NPM_NAME = "@openai/codex";
var PLATFORM_PACKAGE_BY_TARGET = {
  "x86_64-unknown-linux-musl": "@openai/codex-linux-x64",
  "aarch64-unknown-linux-musl": "@openai/codex-linux-arm64",
  "x86_64-apple-darwin": "@openai/codex-darwin-x64",
  "aarch64-apple-darwin": "@openai/codex-darwin-arm64",
  "x86_64-pc-windows-msvc": "@openai/codex-win32-x64",
  "aarch64-pc-windows-msvc": "@openai/codex-win32-arm64"
};
var moduleRequire = createRequire(import.meta.url);
var CodexExec = class {
  executablePath;
  pathDirs;
  envOverride;
  configOverrides;
  constructor(executablePath = null, env, configOverrides) {
    if (executablePath) {
      this.executablePath = executablePath;
      this.pathDirs = [];
    } else {
      const resolved = findCodexPath();
      this.executablePath = resolved.executablePath;
      this.pathDirs = resolved.pathDirs;
    }
    this.envOverride = env;
    this.configOverrides = configOverrides;
  }
  async *run(args) {
    const commandArgs = ["exec", "--experimental-json"];
    if (this.configOverrides) {
      for (const override of serializeConfigOverrides(this.configOverrides)) {
        commandArgs.push("--config", override);
      }
    }
    if (args.baseUrl) {
      commandArgs.push(
        "--config",
        `openai_base_url=${toTomlValue(args.baseUrl, "openai_base_url")}`
      );
    }
    if (args.model) {
      commandArgs.push("--model", args.model);
    }
    if (args.sandboxMode) {
      commandArgs.push("--sandbox", args.sandboxMode);
    }
    if (args.workingDirectory) {
      commandArgs.push("--cd", args.workingDirectory);
    }
    if (args.additionalDirectories?.length) {
      for (const dir of args.additionalDirectories) {
        commandArgs.push("--add-dir", dir);
      }
    }
    if (args.skipGitRepoCheck) {
      commandArgs.push("--skip-git-repo-check");
    }
    if (args.outputSchemaFile) {
      commandArgs.push("--output-schema", args.outputSchemaFile);
    }
    if (args.modelReasoningEffort) {
      commandArgs.push("--config", `model_reasoning_effort="${args.modelReasoningEffort}"`);
    }
    if (args.networkAccessEnabled !== void 0) {
      commandArgs.push(
        "--config",
        `sandbox_workspace_write.network_access=${args.networkAccessEnabled}`
      );
    }
    if (args.webSearchMode) {
      commandArgs.push("--config", `web_search="${args.webSearchMode}"`);
    } else if (args.webSearchEnabled === true) {
      commandArgs.push("--config", `web_search="live"`);
    } else if (args.webSearchEnabled === false) {
      commandArgs.push("--config", `web_search="disabled"`);
    }
    if (args.approvalPolicy) {
      commandArgs.push("--config", `approval_policy="${args.approvalPolicy}"`);
    }
    if (args.threadId) {
      commandArgs.push("resume", args.threadId);
    }
    if (args.images?.length) {
      for (const image of args.images) {
        commandArgs.push("--image", image);
      }
    }
    const env = {};
    if (this.envOverride) {
      Object.assign(env, this.envOverride);
    } else {
      for (const [key, value] of Object.entries(process.env)) {
        if (value !== void 0) {
          env[key] = value;
        }
      }
    }
    if (!env[INTERNAL_ORIGINATOR_ENV]) {
      env[INTERNAL_ORIGINATOR_ENV] = TYPESCRIPT_SDK_ORIGINATOR;
    }
    if (args.apiKey) {
      env.CODEX_API_KEY = args.apiKey;
    }
    if (this.pathDirs.length > 0) {
      prependPathDirs(env, this.pathDirs);
    }
    const child = spawn(this.executablePath, commandArgs, {
      env,
      signal: args.signal
    });
    let spawnError = null;
    child.once("error", (err) => spawnError = err);
    if (!child.stdin) {
      child.kill();
      throw new Error("Child process has no stdin");
    }
    child.stdin.write(args.input);
    child.stdin.end();
    if (!child.stdout) {
      child.kill();
      throw new Error("Child process has no stdout");
    }
    const stderrChunks = [];
    if (child.stderr) {
      child.stderr.on("data", (data) => {
        stderrChunks.push(data);
      });
    }
    const exitPromise = new Promise(
      (resolve) => {
        child.once("exit", (code, signal) => {
          resolve({ code, signal });
        });
      }
    );
    const rl = readline.createInterface({
      input: child.stdout,
      crlfDelay: Infinity
    });
    try {
      for await (const line of rl) {
        yield line;
      }
      if (spawnError) throw spawnError;
      const { code, signal } = await exitPromise;
      if (code !== 0 || signal) {
        const stderrBuffer = Buffer.concat(stderrChunks);
        const detail = signal ? `signal ${signal}` : `code ${code ?? 1}`;
        throw new Error(`Codex Exec exited with ${detail}: ${stderrBuffer.toString("utf8")}`);
      }
    } finally {
      rl.close();
      child.removeAllListeners();
      try {
        if (!child.killed) child.kill();
      } catch {
      }
    }
  }
};
function serializeConfigOverrides(configOverrides) {
  const overrides = [];
  flattenConfigOverrides(configOverrides, "", overrides);
  return overrides;
}
function flattenConfigOverrides(value, prefix, overrides) {
  if (!isPlainObject(value)) {
    if (prefix) {
      overrides.push(`${prefix}=${toTomlValue(value, prefix)}`);
      return;
    } else {
      throw new Error("Codex config overrides must be a plain object");
    }
  }
  const entries = Object.entries(value);
  if (!prefix && entries.length === 0) {
    return;
  }
  if (prefix && entries.length === 0) {
    overrides.push(`${prefix}={}`);
    return;
  }
  for (const [key, child] of entries) {
    if (!key) {
      throw new Error("Codex config override keys must be non-empty strings");
    }
    if (child === void 0) {
      continue;
    }
    const path32 = prefix ? `${prefix}.${key}` : key;
    if (isPlainObject(child)) {
      flattenConfigOverrides(child, path32, overrides);
    } else {
      overrides.push(`${path32}=${toTomlValue(child, path32)}`);
    }
  }
}
function toTomlValue(value, path32) {
  if (typeof value === "string") {
    return JSON.stringify(value);
  } else if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new Error(`Codex config override at ${path32} must be a finite number`);
    }
    return `${value}`;
  } else if (typeof value === "boolean") {
    return value ? "true" : "false";
  } else if (Array.isArray(value)) {
    const rendered = value.map((item, index) => toTomlValue(item, `${path32}[${index}]`));
    return `[${rendered.join(", ")}]`;
  } else if (isPlainObject(value)) {
    const parts = [];
    for (const [key, child] of Object.entries(value)) {
      if (!key) {
        throw new Error("Codex config override keys must be non-empty strings");
      }
      if (child === void 0) {
        continue;
      }
      parts.push(`${formatTomlKey(key)} = ${toTomlValue(child, `${path32}.${key}`)}`);
    }
    return `{${parts.join(", ")}}`;
  } else if (value === null) {
    throw new Error(`Codex config override at ${path32} cannot be null`);
  } else {
    const typeName = typeof value;
    throw new Error(`Unsupported Codex config override value at ${path32}: ${typeName}`);
  }
}
var TOML_BARE_KEY = /^[A-Za-z0-9_-]+$/;
function formatTomlKey(key) {
  return TOML_BARE_KEY.test(key) ? key : JSON.stringify(key);
}
function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function findCodexPath() {
  const { platform, arch } = process;
  let targetTriple = null;
  switch (platform) {
    case "linux":
    case "android":
      switch (arch) {
        case "x64":
          targetTriple = "x86_64-unknown-linux-musl";
          break;
        case "arm64":
          targetTriple = "aarch64-unknown-linux-musl";
          break;
        default:
          break;
      }
      break;
    case "darwin":
      switch (arch) {
        case "x64":
          targetTriple = "x86_64-apple-darwin";
          break;
        case "arm64":
          targetTriple = "aarch64-apple-darwin";
          break;
        default:
          break;
      }
      break;
    case "win32":
      switch (arch) {
        case "x64":
          targetTriple = "x86_64-pc-windows-msvc";
          break;
        case "arm64":
          targetTriple = "aarch64-pc-windows-msvc";
          break;
        default:
          break;
      }
      break;
    default:
      break;
  }
  if (!targetTriple) {
    throw new Error(`Unsupported platform: ${platform} (${arch})`);
  }
  const platformPackage = PLATFORM_PACKAGE_BY_TARGET[targetTriple];
  if (!platformPackage) {
    throw new Error(`Unsupported target triple: ${targetTriple}`);
  }
  let vendorRoot;
  try {
    const codexPackageJsonPath = moduleRequire.resolve(`${CODEX_NPM_NAME}/package.json`);
    const codexRequire = createRequire(codexPackageJsonPath);
    const platformPackageJsonPath = codexRequire.resolve(`${platformPackage}/package.json`);
    vendorRoot = path2.join(path2.dirname(platformPackageJsonPath), "vendor");
  } catch {
    throw new Error(
      `Unable to locate Codex CLI binaries. Ensure ${CODEX_NPM_NAME} is installed with optional dependencies.`
    );
  }
  const codexBinaryName = process.platform === "win32" ? "codex.exe" : "codex";
  const nativePackage = resolveNativePackage(vendorRoot, targetTriple, codexBinaryName);
  if (!nativePackage) {
    throw new Error(
      `Unable to locate Codex CLI binaries for ${targetTriple}. Ensure ${CODEX_NPM_NAME} is installed with optional dependencies.`
    );
  }
  return nativePackage;
}
function resolveNativePackage(vendorRoot, targetTriple, codexBinaryName) {
  const packageRoot = path2.join(vendorRoot, targetTriple);
  const packageBinaryPath = path2.join(packageRoot, "bin", codexBinaryName);
  if (isFile(packageBinaryPath) && isFile(path2.join(packageRoot, "codex-package.json"))) {
    return {
      executablePath: packageBinaryPath,
      pathDirs: existingDirs(path2.join(packageRoot, "codex-path"))
    };
  }
  const legacyBinaryPath = path2.join(packageRoot, "codex", codexBinaryName);
  if (isFile(legacyBinaryPath)) {
    return {
      executablePath: legacyBinaryPath,
      pathDirs: existingDirs(path2.join(packageRoot, "path"))
    };
  }
  return null;
}
function existingDirs(...dirs) {
  return dirs.filter(isDirectory);
}
function prependPathDirs(env, pathDirs, platform = process.platform) {
  const pathKey = pathEnvKey(env, platform);
  if (platform === "win32") {
    for (const key of Object.keys(env)) {
      if (key.toLowerCase() === "path" && key !== pathKey) {
        delete env[key];
      }
    }
  }
  const existingEntries = (env[pathKey] ?? "").split(path2.delimiter).filter((entry) => entry.length > 0 && !pathDirs.includes(entry));
  env[pathKey] = [...pathDirs, ...existingEntries].join(path2.delimiter);
}
function pathEnvKey(env, platform) {
  if (platform !== "win32") {
    return "PATH";
  }
  const matchingKeys = Object.keys(env).filter((key) => key.toLowerCase() === "path");
  return matchingKeys.includes("Path") ? "Path" : matchingKeys.at(-1) ?? "PATH";
}
function isFile(filePath) {
  try {
    return statSync(filePath).isFile();
  } catch {
    return false;
  }
}
function isDirectory(filePath) {
  try {
    return statSync(filePath).isDirectory();
  } catch {
    return false;
  }
}
var Codex = class {
  exec;
  options;
  constructor(options = {}) {
    const { codexPathOverride, env, config } = options;
    this.exec = new CodexExec(codexPathOverride, env, config);
    this.options = options;
  }
  /**
   * Starts a new conversation with an agent.
   * @returns A new thread instance.
   */
  startThread(options = {}) {
    return new Thread(this.exec, this.options, options);
  }
  /**
   * Resumes a conversation with an agent based on the thread id.
   * Threads are persisted in ~/.codex/sessions.
   *
   * @param id The id of the thread to resume.
   * @returns A new thread instance.
   */
  resumeThread(id, options = {}) {
    return new Thread(this.exec, this.options, options, id);
  }
};

// src/codex-worker.mjs
var PROTOCOL_VERSION = 1;
var MAX_PROMPT_CHARS = 2e6;
var MAX_SCHEMA_CHARS = 2e5;
var MAX_FINAL_RESPONSE_CHARS = 2e5;
var MAX_LIFECYCLE_EVENTS = 128;
var MAX_TIMEOUT_MS = 24 * 60 * 60 * 1e3;
var CLI_WRAPPER_MODE_ENV = "CODEX_BAKEOFF_CODEX_WRAPPER";
var CLI_WRAPPER_TARGET_ENV = "CODEX_BAKEOFF_CODEX_TARGET";
var CLI_WRAPPER_OWNER_ENV = "CODEX_BAKEOFF_CODEX_OWNER_PID";
var TREE_KILL_GRACE_MS = 2e3;
var SAFE_ID = /^[A-Za-z0-9._:-]{1,128}$/;
var SAFE_MODEL = /^[A-Za-z0-9._:-]{1,128}$/;
var SANDBOX_MODES = /* @__PURE__ */ new Set(["read-only", "workspace-write"]);
var REASONING_EFFORTS = /* @__PURE__ */ new Set([
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh"
]);
var SAFE_ITEM_TYPES = /* @__PURE__ */ new Set([
  "agent_message",
  "command_execution",
  "error",
  "file_change",
  "mcp_tool_call",
  "reasoning",
  "todo_list",
  "web_search"
]);
var SafeWorkerError = class extends Error {
  constructor(code, message, options = {}) {
    const { retryable = false, ...errorOptions } = options;
    super(message, errorOptions);
    this.name = "SafeWorkerError";
    this.code = code;
    this.retryable = retryable;
  }
};
function normalizeRunRequest(input) {
  if (!isRecord(input) || input.type !== "run") {
    throw new SafeWorkerError("invalid_request", 'Expected a JSON object with type "run".');
  }
  const id = resolveAlias(input, "id", "requestId");
  if (typeof id !== "string" || !SAFE_ID.test(id)) {
    throw new SafeWorkerError(
      "invalid_request",
      "id or requestId must contain only letters, numbers, dot, underscore, colon, or dash."
    );
  }
  if (typeof input.model !== "string" || !SAFE_MODEL.test(input.model)) {
    throw new SafeWorkerError("invalid_request", "model must be a non-empty model identifier.");
  }
  if (typeof input.prompt !== "string" || input.prompt.trim().length === 0 || input.prompt.length > MAX_PROMPT_CHARS) {
    throw new SafeWorkerError(
      "invalid_request",
      `prompt must contain between 1 and ${MAX_PROMPT_CHARS} characters.`
    );
  }
  if (typeof input.workingDirectory !== "string" || input.workingDirectory.length === 0 || input.workingDirectory.length > 4096 || !path3.isAbsolute(input.workingDirectory)) {
    throw new SafeWorkerError("invalid_request", "workingDirectory must be an absolute path.");
  }
  if (!SANDBOX_MODES.has(input.sandboxMode)) {
    throw new SafeWorkerError(
      "invalid_request",
      'sandboxMode must be "read-only" or "workspace-write".'
    );
  }
  const networkAccessEnabled = resolveAlias(
    input,
    "networkAccessEnabled",
    "networkAccess"
  );
  if (typeof networkAccessEnabled !== "boolean") {
    throw new SafeWorkerError(
      "invalid_request",
      "networkAccessEnabled or networkAccess must be a boolean."
    );
  }
  const timeoutMsValue = resolveTimeoutMs(input);
  if (!Number.isInteger(timeoutMsValue) || timeoutMsValue < 1e3 || timeoutMsValue > MAX_TIMEOUT_MS) {
    throw new SafeWorkerError(
      "invalid_request",
      `timeoutMs must be an integer from 1000 through ${MAX_TIMEOUT_MS}.`
    );
  }
  if (input.reasoningEffort !== void 0 && !REASONING_EFFORTS.has(input.reasoningEffort)) {
    throw new SafeWorkerError("invalid_request", "reasoningEffort is not supported.");
  }
  if (input.outputSchema !== void 0) {
    if (!isRecord(input.outputSchema)) {
      throw new SafeWorkerError("invalid_request", "outputSchema must be a JSON object.");
    }
    let serializedSchema;
    try {
      serializedSchema = JSON.stringify(input.outputSchema);
    } catch {
      throw new SafeWorkerError("invalid_request", "outputSchema must be JSON serializable.");
    }
    if (serializedSchema.length > MAX_SCHEMA_CHARS) {
      throw new SafeWorkerError(
        "invalid_request",
        `outputSchema must not exceed ${MAX_SCHEMA_CHARS} serialized characters.`
      );
    }
  }
  return {
    type: "run",
    id,
    model: input.model,
    prompt: input.prompt,
    workingDirectory: input.workingDirectory,
    timeoutMs: timeoutMsValue,
    sandboxMode: input.sandboxMode,
    networkAccessEnabled,
    ...input.reasoningEffort === void 0 ? {} : { reasoningEffort: input.reasoningEffort },
    ...input.outputSchema === void 0 ? {} : { outputSchema: input.outputSchema }
  };
}
async function executeRunRequest(request, {
  abortController = new AbortController(),
  codexFactory = defaultCodexFactory,
  emit = () => {
  },
  diagnostic = () => {
  }
} = {}) {
  const lifecycle = createLifecycleEmitter(request.id, emit);
  const timeout = setTimeout(() => {
    abortController.abort({ kind: "timeout" });
  }, request.timeoutMs);
  timeout.unref?.();
  let threadId = null;
  let finalResponse = "";
  let finalResponseTruncated = false;
  let usage = null;
  let turnCompleted = false;
  let streamWarnings = 0;
  const itemCounts = {};
  try {
    const codex = codexFactory();
    const thread = codex.startThread({
      model: request.model,
      sandboxMode: request.sandboxMode,
      approvalPolicy: "never",
      networkAccessEnabled: request.networkAccessEnabled,
      webSearchMode: "disabled",
      skipGitRepoCheck: true,
      workingDirectory: request.workingDirectory,
      ...request.reasoningEffort === void 0 ? {} : { modelReasoningEffort: request.reasoningEffort }
    });
    const { events } = await thread.runStreamed(request.prompt, {
      signal: abortController.signal,
      ...request.outputSchema === void 0 ? {} : { outputSchema: request.outputSchema }
    });
    if (!events || typeof events[Symbol.asyncIterator] !== "function") {
      throw new SafeWorkerError(
        "invalid_sdk_response",
        "Codex SDK did not return an event stream."
      );
    }
    for await (const event of events) {
      if (abortController.signal.aborted) {
        throw abortController.signal.reason;
      }
      if (!isRecord(event) || typeof event.type !== "string") continue;
      if (event.type === "thread.started") {
        threadId = safeThreadId(event.thread_id) ?? safeThreadId(thread.id);
        lifecycle({ phase: "thread_started", ...threadId ? { threadId } : {} });
      } else if (event.type === "turn.started") {
        lifecycle({ phase: "turn_started" });
      } else if (event.type === "item.completed" && isRecord(event.item)) {
        const itemType = safeItemType(event.item.type);
        itemCounts[itemType] = (itemCounts[itemType] ?? 0) + 1;
        if (event.item.type === "agent_message" && typeof event.item.text === "string") {
          const truncated = truncate(event.item.text, MAX_FINAL_RESPONSE_CHARS);
          finalResponse = truncated.value;
          finalResponseTruncated = truncated.truncated;
        }
        lifecycle({
          phase: "item_completed",
          itemType,
          itemCount: itemCounts[itemType]
        });
      } else if (event.type === "turn.completed") {
        usage = normalizeUsage(event.usage);
        turnCompleted = true;
      } else if (event.type === "turn.failed") {
        diagnostic(`Codex turn failed: ${event.error?.message ?? "No detail provided."}`);
        throw new SafeWorkerError("turn_failed", "Codex turn failed.");
      } else if (event.type === "error") {
        diagnostic(`Codex stream error: ${event.message ?? "No detail provided."}`);
        throw new SafeWorkerError(
          "stream_error",
          "Codex reported an unrecoverable stream error.",
          { retryable: isRetryableStreamError(event.message) }
        );
      }
    }
    if (!turnCompleted) {
      throw new SafeWorkerError(
        "incomplete_stream",
        "Codex event stream ended before turn completion."
      );
    }
    return {
      threadId: threadId ?? safeThreadId(thread.id),
      finalResponse,
      finalResponseTruncated,
      usage,
      itemCounts,
      streamWarnings
    };
  } catch (error) {
    if (abortController.signal.aborted) {
      const reason = abortController.signal.reason;
      if (isRecord(reason) && reason.kind === "timeout") {
        throw new SafeWorkerError("timeout", "Codex run exceeded its configured timeout.", {
          cause: error
        });
      }
      throw new SafeWorkerError("canceled", "Codex run was canceled.", { cause: error });
    }
    if (error instanceof SafeWorkerError) throw error;
    if (looksLikeMissingCodex(error)) {
      throw new SafeWorkerError(
        "codex_unavailable",
        "The local Codex executable could not be started.",
        { cause: error }
      );
    }
    throw new SafeWorkerError("worker_failed", "Codex worker failed.", { cause: error });
  } finally {
    clearTimeout(timeout);
  }
}
function startStdioWorker({
  input = process.stdin,
  output = process.stdout,
  errorOutput = process.stderr,
  setExitCode = (code) => {
    process.exitCode = code;
  },
  codexFactory = defaultCodexFactory
} = {}) {
  const readline2 = createInterface({ input, crlfDelay: Infinity });
  let active = null;
  let finished = false;
  let runStarted = false;
  const emit = (message) => {
    output.write(`${JSON.stringify(message)}
`);
  };
  const finish = (exitCode) => {
    if (finished) return;
    finished = true;
    setExitCode(exitCode);
    readline2.close();
    input.pause?.();
  };
  const failProtocol = (error, id = null) => {
    const safeError = error instanceof SafeWorkerError ? error : new SafeWorkerError("invalid_request", "Invalid worker request.");
    emit({
      type: "failed",
      id,
      code: safeError.code,
      message: safeError.message,
      retryable: safeError.retryable
    });
    finish(2);
  };
  const startRun = (inputRequest) => {
    let request;
    try {
      request = normalizeRunRequest(inputRequest);
    } catch (error) {
      failProtocol(error, safeRequestId(inputRequest));
      return;
    }
    runStarted = true;
    const abortController = new AbortController();
    active = { id: request.id, abortController };
    emit({ type: "accepted", id: request.id });
    void executeRunRequest(request, {
      abortController,
      codexFactory,
      emit,
      diagnostic: (message) => errorOutput.write(`[codex-worker] ${message}
`)
    }).then((result) => {
      emit({
        type: "completed",
        id: request.id,
        threadId: result.threadId,
        finalResponse: result.finalResponse,
        finalResponseTruncated: result.finalResponseTruncated,
        usage: result.usage,
        itemCounts: result.itemCounts,
        streamWarnings: result.streamWarnings
      });
      finish(0);
    }).catch((error) => {
      const safeError = error instanceof SafeWorkerError ? error : new SafeWorkerError("worker_failed", "Codex worker failed.");
      if (safeError.code === "canceled") {
        const reason = active?.abortController.signal.reason;
        emit({
          type: "canceled",
          id: request.id,
          reason: isRecord(reason) && typeof reason.signal === "string" ? reason.signal : "requested"
        });
        finish(
          isRecord(reason) && reason.signal === "SIGTERM" ? 143 : isRecord(reason) && reason.signal === "SIGINT" ? 130 : 0
        );
        return;
      }
      emit({
        type: "failed",
        id: request.id,
        code: safeError.code,
        message: safeError.message,
        retryable: safeError.retryable
      });
      finish(safeError.code === "timeout" ? 124 : 1);
    });
  };
  readline2.on("line", (line) => {
    if (finished || line.trim().length === 0) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      if (!runStarted) {
        failProtocol(new SafeWorkerError("invalid_json", "Input must be valid JSON."));
      } else {
        emit({
          type: "protocol_error",
          id: active?.id ?? null,
          code: "invalid_json",
          message: "Input must be valid JSON."
        });
      }
      return;
    }
    if (isRecord(message) && message.type === "cancel") {
      if (!active || message.id !== active.id) {
        emit({
          type: "protocol_error",
          id: safeRequestId(message),
          code: "unknown_run",
          message: "No matching run is active."
        });
        return;
      }
      active.abortController.abort({ kind: "cancel" });
      return;
    }
    if (runStarted) {
      emit({
        type: "protocol_error",
        id: safeRequestId(message),
        code: "run_already_started",
        message: "This worker accepts exactly one run request."
      });
      return;
    }
    startRun(message);
  });
  emit({ type: "ready", protocolVersion: PROTOCOL_VERSION });
  return {
    cancel(signal = "SIGTERM") {
      if (!active || active.abortController.signal.aborted) return false;
      active.abortController.abort({ kind: "signal", signal });
      return true;
    },
    close() {
      if (!finished) finish(0);
    }
  };
}
function defaultCodexFactory() {
  const target = process.env.CODEX_CLI_PATH?.trim() || "codex";
  const env = Object.fromEntries(
    Object.entries(process.env).filter((entry) => entry[1] !== void 0)
  );
  env[CLI_WRAPPER_MODE_ENV] = "1";
  env[CLI_WRAPPER_TARGET_ENV] = target;
  env[CLI_WRAPPER_OWNER_ENV] = String(process.pid);
  return new Codex({
    codexPathOverride: fileURLToPath(import.meta.url),
    env
  });
}
function startCodexCliWrapper() {
  const target = process.env[CLI_WRAPPER_TARGET_ENV]?.trim();
  if (!target) {
    process.stderr.write("Missing isolated Codex CLI target.\n");
    process.exitCode = 127;
    return;
  }
  const originalArgs = process.argv.slice(2);
  if (originalArgs[0] !== "exec") {
    process.stderr.write("The isolated Codex wrapper only supports exec.\n");
    process.exitCode = 2;
    return;
  }
  const args = [
    "exec",
    "--ignore-user-config",
    "--ignore-rules",
    ...originalArgs.slice(1)
  ];
  const detached = process.platform !== "win32";
  const child = spawn2(target, args, {
    detached,
    env: process.env,
    stdio: "inherit"
  });
  let killTimer = null;
  const killTree = (signal) => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try {
      if (detached && Number.isInteger(child.pid)) {
        process.kill(-child.pid, signal);
      } else {
        child.kill(signal);
      }
    } catch {
    }
  };
  const forwardSignal = (signal) => {
    killTree(signal);
    if (killTimer === null) {
      killTimer = setTimeout(() => killTree("SIGKILL"), TREE_KILL_GRACE_MS);
      killTimer.unref?.();
    }
  };
  const signalHandlers = new Map(
    ["SIGTERM", "SIGINT", "SIGHUP"].map((signal) => {
      const handler = () => forwardSignal(signal);
      process.on(signal, handler);
      return [signal, handler];
    })
  );
  const ownerPid = Number.parseInt(process.env[CLI_WRAPPER_OWNER_ENV] ?? "", 10);
  const ownerMonitor = Number.isInteger(ownerPid) && ownerPid > 1 ? setInterval(() => {
    try {
      process.kill(ownerPid, 0);
    } catch {
      forwardSignal("SIGTERM");
    }
  }, 250) : null;
  ownerMonitor?.unref?.();
  const cleanup = () => {
    if (killTimer !== null) clearTimeout(killTimer);
    if (ownerMonitor !== null) clearInterval(ownerMonitor);
    for (const [signal, handler] of signalHandlers) {
      process.off(signal, handler);
    }
  };
  child.once("error", (error) => {
    cleanup();
    process.stderr.write(`${error.message}
`);
    process.exitCode = 127;
  });
  child.once("exit", (code, signal) => {
    cleanup();
    process.exitCode = code ?? signalExitCode(signal);
  });
}
function signalExitCode(signal) {
  return {
    SIGHUP: 129,
    SIGINT: 130,
    SIGTERM: 143,
    SIGKILL: 137
  }[signal] ?? 1;
}
function createLifecycleEmitter(id, emit) {
  let emitted = 0;
  return (event) => {
    if (emitted >= MAX_LIFECYCLE_EVENTS) return;
    emitted += 1;
    emit({ type: "lifecycle", id, sequence: emitted, ...event });
  };
}
function normalizeUsage(value) {
  if (!isRecord(value)) return null;
  const usage = {};
  for (const key of [
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens"
  ]) {
    if (Number.isSafeInteger(value[key]) && value[key] >= 0) {
      usage[key] = value[key];
    }
  }
  return Object.keys(usage).length > 0 ? usage : null;
}
function resolveAlias(input, canonical, alias) {
  if (input[canonical] !== void 0 && input[alias] !== void 0 && input[canonical] !== input[alias]) {
    throw new SafeWorkerError(
      "invalid_request",
      `${canonical} and ${alias} must match when both are provided.`
    );
  }
  return input[canonical] ?? input[alias];
}
function resolveTimeoutMs(input) {
  if (input.timeoutMs !== void 0 && (!Number.isInteger(input.timeoutMs) || input.timeoutMs < 0)) {
    throw new SafeWorkerError("invalid_request", "timeoutMs must be a positive integer.");
  }
  if (input.timeoutSeconds !== void 0 && (typeof input.timeoutSeconds !== "number" || !Number.isFinite(input.timeoutSeconds) || input.timeoutSeconds <= 0)) {
    throw new SafeWorkerError(
      "invalid_request",
      "timeoutSeconds must be a positive number."
    );
  }
  if (input.timeoutMs !== void 0 && input.timeoutSeconds !== void 0) {
    if (input.timeoutMs !== input.timeoutSeconds * 1e3) {
      throw new SafeWorkerError(
        "invalid_request",
        "timeoutMs and timeoutSeconds must match when both are provided."
      );
    }
  }
  return input.timeoutMs ?? input.timeoutSeconds * 1e3;
}
function safeItemType(value) {
  return typeof value === "string" && SAFE_ITEM_TYPES.has(value) ? value : "other";
}
function safeThreadId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,256}$/.test(value) ? value : null;
}
function safeRequestId(value) {
  if (!isRecord(value)) return null;
  const id = value.id ?? value.requestId;
  return typeof id === "string" && SAFE_ID.test(id) ? id : null;
}
function isRetryableStreamError(message) {
  if (typeof message !== "string") return false;
  if (/auth|unauthoriz|forbidden|api key|quota|billing|usage limit|model.*(?:access|not found)|invalid request/i.test(
    message
  )) {
    return false;
  }
  return /connection|network|stream|transport|reset|closed|timed? out|temporar|unavailable|overload|internal server error|\b(?:429|502|503|504)\b/i.test(
    message
  );
}
function truncate(value, maxChars) {
  if (value.length <= maxChars) return { value, truncated: false };
  return { value: value.slice(0, maxChars), truncated: true };
}
function looksLikeMissingCodex(error) {
  const message = error instanceof Error ? error.message : String(error);
  return /(?:ENOENT|Unable to locate Codex CLI binaries|spawn .* not found)/i.test(message);
}
function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isMainModule() {
  const scriptPath = process.argv[1];
  return typeof scriptPath === "string" && pathToFileURL(path3.resolve(scriptPath)).href === import.meta.url;
}
if (isMainModule()) {
  if (process.env[CLI_WRAPPER_MODE_ENV] === "1") {
    startCodexCliWrapper();
  } else {
    const worker = startStdioWorker();
    process.once("SIGTERM", () => {
      if (worker.cancel("SIGTERM")) {
        setTimeout(() => process.exit(143), 5e3).unref();
      } else {
        process.exitCode = 143;
        worker.close();
      }
    });
    process.once("SIGINT", () => {
      if (worker.cancel("SIGINT")) {
        setTimeout(() => process.exit(130), 5e3).unref();
      } else {
        process.exitCode = 130;
        worker.close();
      }
    });
  }
}
export {
  executeRunRequest,
  normalizeRunRequest,
  startStdioWorker
};
