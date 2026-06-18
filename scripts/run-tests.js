const { spawnSync } = require("child_process");
const { buildPythonEnv, getProjectRoot, resolvePythonCommand } = require("../src/main/pythonRuntime");

const projectRoot = getProjectRoot();
const python = resolvePythonCommand(projectRoot);
const result = spawnSync(python.command, [...python.args, "-m", "unittest", "discover", "-s", "tests"], {
  cwd: projectRoot,
  stdio: "inherit",
  env: buildPythonEnv({ projectRoot })
});

process.exit(result.status || 0);
