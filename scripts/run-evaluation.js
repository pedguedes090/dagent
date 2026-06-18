const { spawnSync } = require("child_process");
const { buildPythonEnv, getProjectRoot, resolvePythonCommand } = require("../src/main/pythonRuntime");

const projectRoot = getProjectRoot();
const python = resolvePythonCommand(projectRoot);
const args = process.argv.slice(2);
const commandArgs = [...python.args, "-m", "agent_engine.evaluation", ...args];

const result = spawnSync(python.command, commandArgs, {
  cwd: projectRoot,
  stdio: "inherit",
  env: buildPythonEnv({ projectRoot })
});

process.exit(result.status || 0);
