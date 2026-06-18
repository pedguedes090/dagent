const fs = require("fs");
const path = require("path");

function getProjectRoot() {
  return path.resolve(__dirname, "../..");
}

function venvPythonPath(projectRoot = getProjectRoot()) {
  return process.platform === "win32"
    ? path.join(projectRoot, ".venv", "Scripts", "python.exe")
    : path.join(projectRoot, ".venv", "bin", "python");
}

function resolvePythonCommand(projectRoot = getProjectRoot()) {
  const explicit = String(process.env.AGENT_PYTHON || process.env.PYTHON || "").trim();
  if (explicit) return { command: explicit, args: [] };

  const local = venvPythonPath(projectRoot);
  if (fs.existsSync(local)) return { command: local, args: [] };

  if (process.platform === "win32") {
    return { command: "py", args: ["-3"] };
  }
  return { command: "python3", args: [] };
}

function buildPythonEnv({ projectRoot = getProjectRoot(), userDataPath } = {}) {
  const enginePath = path.join(projectRoot, "engine");
  const localBinPath = path.join(projectRoot, "node_modules", ".bin");
  const nextPath = process.env.PATH
    ? `${localBinPath}${path.delimiter}${process.env.PATH}`
    : localBinPath;

  return {
    ...process.env,
    PATH: nextPath,
    AGENT_ENGINE_STATE_DIR: userDataPath || process.env.AGENT_ENGINE_STATE_DIR || path.join(projectRoot, ".agent-state"),
    CODEGRAPH_TELEMETRY: "0",
    LANGGRAPH_STRICT_MSGPACK: "true",
    OPENHANDS_SUPPRESS_BANNER: "1",
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    PYTHONPATH: process.env.PYTHONPATH ? `${enginePath}${path.delimiter}${process.env.PYTHONPATH}` : enginePath
  };
}

module.exports = {
  buildPythonEnv,
  getProjectRoot,
  resolvePythonCommand,
  venvPythonPath
};
