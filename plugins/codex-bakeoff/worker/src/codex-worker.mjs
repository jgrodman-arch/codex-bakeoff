import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Codex } from "@openai/codex-sdk";

const PROTOCOL_VERSION = 1;
const MAX_PROMPT_CHARS = 2_000_000;
const MAX_SCHEMA_CHARS = 200_000;
const MAX_FINAL_RESPONSE_CHARS = 200_000;
const MAX_LIFECYCLE_EVENTS = 128;
const MAX_TIMEOUT_MS = 24 * 60 * 60 * 1000;
const CLI_WRAPPER_MODE_ENV = "CODEX_BAKEOFF_CODEX_WRAPPER";
const CLI_WRAPPER_TARGET_ENV = "CODEX_BAKEOFF_CODEX_TARGET";
const CLI_WRAPPER_OWNER_ENV = "CODEX_BAKEOFF_CODEX_OWNER_PID";
const TREE_KILL_GRACE_MS = 2_000;
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

class SafeWorkerError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = "SafeWorkerError";
    this.code = code;
  }
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

  const timeoutMsValue = resolveTimeoutMs(input);
  if (
    !Number.isInteger(timeoutMsValue) ||
    timeoutMsValue < 1_000 ||
    timeoutMsValue > MAX_TIMEOUT_MS
  ) {
    throw new SafeWorkerError(
      "invalid_request",
      `timeoutMs must be an integer from 1000 through ${MAX_TIMEOUT_MS}.`
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
    timeoutMs: timeoutMsValue,
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
    emit = () => {}
  } = {}
) {
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
      ...(request.reasoningEffort === undefined
        ? {}
        : { modelReasoningEffort: request.reasoningEffort })
    });
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
        throw new SafeWorkerError("turn_failed", "Codex turn failed.");
      } else if (event.type === "error") {
        throw new SafeWorkerError("stream_error", "Codex reported an unrecoverable stream error.");
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

export function startStdioWorker({
  input = process.stdin,
  output = process.stdout,
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
      retryable: false
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

    void executeRunRequest(request, { abortController, codexFactory, emit })
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
          retryable: false
        });
        finish(safeError.code === "timeout" ? 124 : 1);
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
  const target = process.env.CODEX_CLI_PATH?.trim() || "codex";
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

function resolveTimeoutMs(input) {
  if (
    input.timeoutMs !== undefined &&
    (!Number.isInteger(input.timeoutMs) || input.timeoutMs < 0)
  ) {
    throw new SafeWorkerError("invalid_request", "timeoutMs must be a positive integer.");
  }
  if (
    input.timeoutSeconds !== undefined &&
    (typeof input.timeoutSeconds !== "number" ||
      !Number.isFinite(input.timeoutSeconds) ||
      input.timeoutSeconds <= 0)
  ) {
    throw new SafeWorkerError(
      "invalid_request",
      "timeoutSeconds must be a positive number."
    );
  }
  if (input.timeoutMs !== undefined && input.timeoutSeconds !== undefined) {
    if (input.timeoutMs !== input.timeoutSeconds * 1000) {
      throw new SafeWorkerError(
        "invalid_request",
        "timeoutMs and timeoutSeconds must match when both are provided."
      );
    }
  }
  return input.timeoutMs ?? input.timeoutSeconds * 1000;
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
