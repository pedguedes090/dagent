const { spawn } = require("child_process");
const crypto = require("crypto");
const { buildPythonEnv, getProjectRoot, resolvePythonCommand } = require("./pythonRuntime");

function parseJsonLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

class AgentBackendService {
  constructor(userDataPath) {
    this.userDataPath = userDataPath;
    this.child = null;
    this.endpoint = null;
    this.stderr = "";
    this.readyPromise = null;
  }

  start() {
    if (this.readyPromise) return this.readyPromise;

    this.readyPromise = new Promise((resolve, reject) => {
      const projectRoot = getProjectRoot();
      const python = resolvePythonCommand(projectRoot);
      const child = spawn(
        python.command,
        [...python.args, "-m", "agent_engine.server", "--host", "127.0.0.1", "--port", "0"],
        {
          cwd: projectRoot,
          windowsHide: true,
          env: buildPythonEnv({ projectRoot, userDataPath: this.userDataPath })
        }
      );

      this.child = child;
      let stdoutBuffer = "";
      let settled = false;

      const fail = (error) => {
        if (settled) return;
        settled = true;
        this.child = null;
        this.endpoint = null;
        this.readyPromise = null;
        reject(error instanceof Error ? error : new Error(String(error)));
      };

      child.stdout.on("data", (chunk) => {
        stdoutBuffer += chunk.toString("utf8");
        const lines = stdoutBuffer.split(/\r?\n/);
        stdoutBuffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          const message = parseJsonLine(line);
          if (message?.type === "ready" && message.host && message.port) {
            this.endpoint = `http://${message.host}:${message.port}`;
            settled = true;
            resolve(this.endpoint);
          }
        }
      });

      child.stderr.on("data", (chunk) => {
        this.stderr += chunk.toString("utf8");
        if (this.stderr.length > 20000) this.stderr = this.stderr.slice(-20000);
      });

      child.on("error", fail);
      child.on("exit", (code) => {
        this.child = null;
        this.endpoint = null;
        this.readyPromise = null;
        if (!settled) {
          fail(new Error(this.stderr || `Agent backend exited before ready with code ${code}`));
        }
      });
    });

    return this.readyPromise;
  }

  async runPipeline({ settings, workspacePath, messages, userText, sessionId, humanGateApproval, emitProgress }) {
    const endpoint = await this.start();
    const correlationId = humanGateApproval?.correlationId || crypto.randomUUID();
    const response = await fetch(`${endpoint}/v1/runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Correlation-Id": correlationId
      },
      body: JSON.stringify({
        sessionId,
        correlationId,
        content: userText,
        workspacePath,
        settings,
        messages,
        humanGateApproval
      })
    });

    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`Agent backend loi ${response.status}: ${body.slice(0, 800)}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    let engineError = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const message = parseJsonLine(line);
        if (!message) continue;
        if (message.type === "progress" && typeof emitProgress === "function") {
          emitProgress({
            stage: message.stage,
            detail: message.detail,
            at: message.at
          });
        }
        if (message.type === "result") result = message.result;
        if (message.type === "error") engineError = message.error;
      }
    }

    if (buffer.trim()) {
      const message = parseJsonLine(buffer.trim());
      if (message?.type === "result") result = message.result;
      if (message?.type === "error") engineError = message.error;
    }

    if (!result) throw new Error(engineError || "Agent backend khong tra ve ket qua.");
    return {
      ...result,
      id: result.id,
      correlationId: result.correlationId || correlationId,
      createdAt: new Date().toISOString(),
      workspacePath,
      settings: {
        serverUrl: settings.serverUrl,
        model: settings.model
      }
    };
  }

  stop() {
    if (!this.child) return;
    this.child.kill();
    this.child = null;
    this.endpoint = null;
    this.readyPromise = null;
  }
}

module.exports = {
  AgentBackendService
};
