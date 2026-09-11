import { createInterface } from "node:readline";
import { spawn, spawnSync } from "node:child_process";
import { accessSync, constants, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Codex } from "@openai/codex-sdk";

const PROTOCOL_VERSION = 1;
const MAX_PROMPT_CHARS = 2_000_000;
const MAX_SCHEMA_CHARS = 200_000;
const MAX_FINAL_RESPONSE_CHARS = 200_000;
const MAX_LIFECYCLE_EVENTS = 128;
const MAX_PROVIDER_ERROR_CHARS = 1_000;
const MAX_WORKER_DIAGNOSTIC_CHARS = 8_000;
const CLI_WRAPPER_MODE_ENV = "CODEX_BAKEOFF_CODEX_WRAPPER";
const CLI_WRAPPER_TARGET_ENV = "CODEX_BAKEOFF_CODEX_TARGET";
const CLI_WRAPPER_OWNER_ENV = "CODEX_BAKEOFF_CODEX_OWNER_PID";
const TREE_KILL_GRACE_MS = 2_000;
const CODEX_VERSION_PROBE_TIMEOUT_MS = 5_000;
const CODEX_VERSION_PROBE_MAX_BUFFER = 64 * 1024;
const SAFE_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const SAFE_MODEL = /^[A-Za-z0-9._:-]{1,128}$/;
const SANDBOX_MODES = new Set(["read-only", "workspace-write"]);
const REASONING_EFFORTS = new Set([
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh"
]);
const SAFE_ITEM_TYPES = new Set([
  "agent_message",
  "command_execution",
  "error",
  "file_change",
  "mcp_tool_call",
  "reasoning",
  "todo_list",
  "web_search"
]);

const SYSTEM_CODES = new Set([
  "ECONNRESET", "ECONNREFUSED", "ETIMEDOUT", "ENOTFOUND", "EAI_AGAIN",
  "ENOENT", "EACCES", "EPERM", "EPIPE"
]);
const PERMANENT_SYSTEM_CODES = new Set(["ENOENT", "EACCES", "EPERM"]);
const PERMANENT_STREAM_ERROR_KINDS = new Set([
  "invalid_request_error",
  "authentication_error",
  "permission_error",
  "not_found_error",
  "model_not_found",
  "insufficient_quota",
  "billing_error",
  "account_deactivated",
  "access_denied"
]);
const RETRYABLE_STREAM_ERROR_KINDS = new Set([
  "connection_error",
  "timeout_error",
  "transport_error",
  "rate_limit_error",
  "server_error",
  "service_unavailable_error",
  "internal_server_error",
  "overloaded_error"
]);

class SafeWorkerError extends Error {
  constructor(code, message, options = {}) {
    const { retryable = false, ...errorOptions } = options;
    super(message, errorOptions);
    this.name = "SafeWorkerError";
    this.code = code;
    this.retryable = retryable;
    this.systemCode = systemErrorCode(errorOptions.cause);
  }
}

function systemErrorCode(error) {
  for (let depth = 0; error instanceof Error && depth < 3; depth++, error = error.cause) {
    if (SYSTEM_CODES.has(error.code)) return error.code;
  }
  return "unknown";
}

export function normalizeRunRequest(input) {
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
  if (
    typeof input.prompt !== "string" ||
    input.prompt.trim().length === 0 ||
    input.prompt.length > MAX_PROMPT_CHARS
  ) {
    throw new SafeWorkerError(
      "invalid_request",
      `prompt must contain between 1 and ${MAX_PROMPT_CHARS} characters.`
    );
  }
  if (
    typeof input.workingDirectory !== "string" ||
    input.workingDirectory.length === 0 ||
    input.workingDirectory.length > 4096 ||
    !path.isAbsolute(input.workingDirectory)
  ) {
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

  if (
    input.reasoningEffort !== undefined &&
    !REASONING_EFFORTS.has(input.reasoningEffort)
  ) {
    throw new SafeWorkerError("invalid_request", "reasoningEffort is not supported.");
  }

  if (input.outputSchema !== undefined) {
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
    sandboxMode: input.sandboxMode,
    networkAccessEnabled,
    ...(input.reasoningEffort === undefined
      ? {}
      : { reasoningEffort: input.reasoningEffort }),
    ...(input.outputSchema === undefined ? {} : { outputSchema: input.outputSchema })
  };
}

export async function executeRunRequest(
  request,
  {
    abortController = new AbortController(),
    codexFactory = defaultCodexFactory,
    emit = () => {},
    diagnostic = () => {}
  } = {}
) {
  const lifecycle = createLifecycleEmitter(request.id, emit);
  let threadId = null;
  let finalResponse = "";
  let finalResponseTruncated = false;
  let usage = null;
  let turnCompleted = false;
  let streamWarnings = 0;
  const itemCounts = {};
  const startedAt = performance.now();
  let stage = "launch";

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
      ...(request.reasoningEffort === undefined
        ? {}
        : { modelReasoningEffort: request.reasoningEffort })
    });
    stage = "turn_start";
    const { events } = await thread.runStreamed(request.prompt, {
      signal: abortController.signal,
      ...(request.outputSchema === undefined
        ? {}
        : { outputSchema: request.outputSchema })
    });
    if (!events || typeof events[Symbol.asyncIterator] !== "function") {
      throw new SafeWorkerError(
        "invalid_sdk_response",
        "Codex SDK did not return an event stream."
      );
    }

    stage = "stream";
    for await (const event of events) {
      if (abortController.signal.aborted) {
        throw abortController.signal.reason;
      }
      if (!isRecord(event) || typeof event.type !== "string") continue;

      if (event.type === "thread.started") {
        threadId = safeThreadId(event.thread_id) ?? safeThreadId(thread.id);
        lifecycle({ phase: "thread_started", ...(threadId ? { threadId } : {}) });
      } else if (event.type === "turn.started") {
        lifecycle({ phase: "turn_started" });
      } else if (event.type === "item.completed" && isRecord(event.item)) {
        const itemType = safeItemType(event.item.type);
        itemCounts[itemType] = (itemCounts[itemType] ?? 0) + 1;
        if (
          event.item.type === "agent_message" &&
          typeof event.item.text === "string"
        ) {
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
        const message = safeProviderErrorMessage(event.error?.message, "Codex turn failed.");
        diagnostic(`Codex turn failed: ${message}`);
        throw new SafeWorkerError("turn_failed", message);
      } else if (event.type === "error") {
        const message = safeProviderErrorMessage(
          event.message,
          "Codex reported an unrecoverable stream error."
        );
        diagnostic(`Codex stream error: ${message}`);
        throw new SafeWorkerError(
          "stream_error",
          message,
          { retryable: isRetryableStreamError(event) }
        );
      }
    }

    if (!turnCompleted) {
      throw new SafeWorkerError(
        "incomplete_stream",
        "Codex event stream ended before turn completion.",
        { retryable: true }
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
      throw new SafeWorkerError("canceled", "Codex run was canceled.", { cause: error });
    }
    const message = error instanceof SafeWorkerError
      ? error.message
      : describeWorkerError(error, diagnostic);
    const failure = error instanceof SafeWorkerError ? error
      : looksLikeMissingCodex(error) ? new SafeWorkerError(
        "codex_unavailable",
        "The local Codex executable could not be started.",
        { cause: error }
      ) : new SafeWorkerError("worker_failed", message, {
        cause: error,
        retryable: stage === "stream" && isRetryableStreamFailure(error)
      });
    failure.stage = stage;
    failure.elapsedMs = performance.now() - startedAt;
    throw failure;
  }
}

export function startStdioWorker({
  input = process.stdin,
  output = process.stdout,
  errorOutput = process.stderr,
  setExitCode = (code) => {
    process.exitCode = code;
  },
  codexFactory = defaultCodexFactory
} = {}) {
  const readline = createInterface({ input, crlfDelay: Infinity });
  let active = null;
  let finished = false;
  let runStarted = false;

  const emit = (message) => {
    output.write(`${JSON.stringify(message)}\n`);
  };
  const finish = (exitCode) => {
    if (finished) return;
    finished = true;
    setExitCode(exitCode);
    readline.close();
    input.pause?.();
  };
  const failProtocol = (error, id = null) => {
    const safeError =
      error instanceof SafeWorkerError
        ? error
        : new SafeWorkerError("invalid_request", "Invalid worker request.");
    emit({
      type: "failed",
      id,
      code: safeError.code,
      message: safeError.message,
      retryable: safeError.retryable,
      systemCode: safeError.systemCode,
      stage: "validation"
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
      diagnostic: (message) => errorOutput.write(`[codex-worker] ${message}\n`)
    })
      .then((result) => {
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
      })
      .catch((error) => {
        const safeError =
          error instanceof SafeWorkerError
            ? error
            : new SafeWorkerError("worker_failed", "Codex worker failed.");
        if (safeError.code === "canceled") {
          const reason = active?.abortController.signal.reason;
          emit({
            type: "canceled",
            id: request.id,
            reason:
              isRecord(reason) && typeof reason.signal === "string"
                ? reason.signal
                : "requested"
          });
          finish(
            isRecord(reason) && reason.signal === "SIGTERM"
              ? 143
              : isRecord(reason) && reason.signal === "SIGINT"
                ? 130
                : 0
          );
          return;
        }
        emit({
          type: "failed",
          id: request.id,
          code: safeError.code,
          message: safeError.message,
          retryable: safeError.retryable,
          systemCode: safeError.systemCode,
          stage: safeError.stage ?? "validation",
          elapsedMs: safeError.elapsedMs ?? null
        });
        finish(1);
      });
  };

  readline.on("line", (line) => {
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
  const target = resolveCodexTarget();
  const env = Object.fromEntries(
    Object.entries(process.env).filter((entry) => entry[1] !== undefined)
  );
  env[CLI_WRAPPER_MODE_ENV] = "1";
  env[CLI_WRAPPER_TARGET_ENV] = target;
  env[CLI_WRAPPER_OWNER_ENV] = String(process.pid);
  return new Codex({
    codexPathOverride: fileURLToPath(import.meta.url),
    env
  });
}

export function resolveCodexTarget({
  env = process.env,
  platform = process.platform,
  applicationRoots = defaultApplicationRoots(env, platform),
  probe = probeCodexCandidate
} = {}) {
  const candidates = [];
  const addCandidate = (candidate) => {
    if (candidate && !candidates.includes(candidate)) candidates.push(candidate);
  };
  const configured = env.CODEX_CLI_PATH?.trim();
  addCandidate(configured);

  const executableName = platform === "win32" ? "codex.exe" : "codex";
  for (const pathMatch of findExecutablesOnPath(executableName, env, platform)) {
    addCandidate(pathMatch);
  }

  if (platform === "darwin") {
    for (const applicationRoot of applicationRoots) {
      for (const applicationName of codexApplicationNames(applicationRoot)) {
        const candidate = path.join(
          applicationRoot,
          applicationName,
          "Contents",
          "Resources",
          "codex"
        );
        if (isExecutableFile(candidate)) addCandidate(candidate);
      }
    }
  }

  for (const candidate of candidates) {
    try {
      if (probe(candidate, env)) return candidate;
    } catch {
      // A failed probe only rejects this candidate.
    }
  }

  const checked = candidates.length > 0 ? ` Checked: ${candidates.join(", ")}.` : "";
  throw new SafeWorkerError(
    "codex_unavailable",
    `No working Codex executable was found.${checked}`
  );
}

function defaultApplicationRoots(env, platform) {
  if (platform !== "darwin") return [];
  const home = env.HOME?.trim() || homedir();
  return ["/Applications", path.join(home, "Applications")];
}

function findExecutablesOnPath(executableName, env, platform) {
  const pathValue =
    platform === "win32"
      ? Object.entries(env).find(([key]) => key.toLowerCase() === "path")?.[1]
      : env.PATH;
  if (!pathValue) return [];
  const matches = [];
  for (const directory of pathValue.split(path.delimiter)) {
    if (!directory) continue;
    const candidate = path.join(directory, executableName);
    if (isExecutableFile(candidate)) matches.push(candidate);
  }
  return matches;
}

function probeCodexCandidate(candidate, env) {
  const completed = spawnSync(candidate, ["--version"], {
    env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: CODEX_VERSION_PROBE_TIMEOUT_MS,
    maxBuffer: CODEX_VERSION_PROBE_MAX_BUFFER,
    windowsHide: true
  });
  return completed.status === 0 && completed.error === undefined;
}

function codexApplicationNames(applicationRoot) {
  try {
    return readdirSync(applicationRoot, { withFileTypes: true })
      .filter(
        (entry) =>
          entry.isDirectory() && /^(?:Codex|ChatGPT).*\.app$/.test(entry.name)
      )
      .map((entry) => entry.name)
      .sort((left, right) => applicationPriority(left) - applicationPriority(right));
  } catch {
    return [];
  }
}

function applicationPriority(name) {
  if (name === "Codex.app") return 0;
  if (name === "ChatGPT.app") return 1;
  if (name.startsWith("Codex")) return 2;
  return 3;
}

function isExecutableFile(candidate) {
  try {
    if (!statSync(candidate).isFile()) return false;
    accessSync(candidate, constants.X_OK);
    return true;
  } catch {
    return false;
  }
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
    "--ignore-rules",
    ...originalArgs.slice(1)
  ];
  const detached = process.platform !== "win32";
  const child = spawn(target, args, {
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
      // The process may have exited between the status check and signal delivery.
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
  const ownerMonitor =
    Number.isInteger(ownerPid) && ownerPid > 1
      ? setInterval(() => {
          try {
            process.kill(ownerPid, 0);
          } catch {
            forwardSignal("SIGTERM");
          }
        }, 250)
      : null;
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
    process.stderr.write(`${error.message}\n`);
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
  if (
    input[canonical] !== undefined &&
    input[alias] !== undefined &&
    input[canonical] !== input[alias]
  ) {
    throw new SafeWorkerError(
      "invalid_request",
      `${canonical} and ${alias} must match when both are provided.`
    );
  }
  return input[canonical] ?? input[alias];
}

function safeItemType(value) {
  return typeof value === "string" && SAFE_ITEM_TYPES.has(value) ? value : "other";
}

function safeThreadId(value) {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,256}$/.test(value)
    ? value
    : null;
}

function safeRequestId(value) {
  if (!isRecord(value)) return null;
  const id = value.id ?? value.requestId;
  return typeof id === "string" && SAFE_ID.test(id) ? id : null;
}

function describeWorkerError(error, diagnostic) {
  const messages = [];
  for (let depth = 0; error instanceof Error && depth < 3; depth++, error = error.cause) {
    if (error.message.trim()) messages.push(error.message.trim());
  }
  const fallback = "Codex worker failed.";
  if (messages.length === 0) return fallback;

  // SDK exceptions include CLI stderr. Keep bounded, redacted context locally,
  // but surface the final cause line rather than pages of startup warnings.
  diagnostic(`Codex worker exception: ${safeProviderErrorMessage(
    messages.join("\nCaused by: "), fallback, MAX_WORKER_DIAGNOSTIC_CHARS
  )}`);
  return safeProviderErrorMessage(messages.at(-1).split(/\r?\n/).at(-1), fallback);
}

function safeProviderErrorMessage(value, fallback, maxChars = MAX_PROVIDER_ERROR_CHARS) {
  if (typeof value !== "string" || !value.trim()) return fallback;

  let message = value;
  const parsed = parseProviderErrorPayload(value);
  if (isRecord(parsed?.error) && typeof parsed.error.message === "string") {
    message = parsed.error.message;
  } else if (isRecord(parsed) && typeof parsed.message === "string") {
    message = parsed.message;
  }

  return redactKnownCredentials(message)
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .trim()
    .slice(0, maxChars) || fallback;
}

function redactKnownCredentials(message) {
  return message
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [REDACTED]")
    .replace(/\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{12,}/g, "[REDACTED]")
    .replace(/\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+/gi, "$1=[REDACTED]");
}

function parseProviderErrorPayload(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function providerErrorObjects(event) {
  if (!isRecord(event)) return [];
  const objects = [event];
  const parsed = parseProviderErrorPayload(event.message);
  if (parsed !== null) objects.push(parsed);
  for (const value of [...objects]) {
    if (isRecord(value.error)) objects.push(value.error);
  }
  return objects;
}

function providerErrorStatus(value) {
  const status = value.status ?? value.status_code ?? value.statusCode;
  if (Number.isInteger(status)) return status;
  if (typeof status === "string" && /^[0-9]{3}$/.test(status)) {
    return Number(status);
  }
  return null;
}

function providerErrorKinds(objects) {
  const kinds = [];
  for (const value of objects) {
    for (const field of ["type", "code", "error_code"]) {
      if (typeof value[field] === "string") kinds.push(value[field]);
    }
  }
  return kinds;
}

function isRetryableStreamError(event) {
  const objects = providerErrorObjects(event);
  const kinds = providerErrorKinds(objects).map((kind) => kind.toLowerCase());
  if (kinds.some((kind) => PERMANENT_STREAM_ERROR_KINDS.has(kind))) {
    return false;
  }
  if (kinds.some((kind) => RETRYABLE_STREAM_ERROR_KINDS.has(kind))) {
    return true;
  }

  for (const value of objects) {
    const status = providerErrorStatus(value);
    if (status === 408 || status === 429 || (status !== null && status >= 500)) {
      return true;
    }
    if (status !== null && status >= 400 && status < 500) {
      return false;
    }
  }

  const message = typeof event?.message === "string" ? event.message : "";
  if (
    /auth|unauthoriz|forbidden|api key|quota|billing|usage limit|model.*(?:access|not found)|invalid request/i.test(
      message
    )
  ) {
    return false;
  }

  // An SDK stream error without a known permanent cause is most likely a
  // transport interruption. Retrying in a fresh workspace is safer than
  // turning a wording change into a terminal Replay failure.
  return true;
}

function isRetryableStreamFailure(error) {
  const code = systemErrorCode(error);
  if (PERMANENT_SYSTEM_CODES.has(code)) return false;
  if (code !== "unknown") return true;
  if (looksLikePermanentLocalFailure(error)) return false;
  return isRetryableStreamError(error);
}

function looksLikePermanentLocalFailure(error) {
  const message = error instanceof Error ? error.message : String(error);
  return /(?:operation not permitted|permission denied|failed to initialize in-process app-server client)/i.test(
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
  return typeof scriptPath === "string" && pathToFileURL(path.resolve(scriptPath)).href === import.meta.url;
}

if (isMainModule()) {
  if (process.env[CLI_WRAPPER_MODE_ENV] === "1") {
    startCodexCliWrapper();
  } else {
    const worker = startStdioWorker();
    process.once("SIGTERM", () => {
      if (worker.cancel("SIGTERM")) {
        setTimeout(() => process.exit(143), 5_000).unref();
      } else {
        process.exitCode = 143;
        worker.close();
      }
    });
    process.once("SIGINT", () => {
      if (worker.cancel("SIGINT")) {
        setTimeout(() => process.exit(130), 5_000).unref();
      } else {
        process.exitCode = 130;
        worker.close();
      }
    });
  }
}
