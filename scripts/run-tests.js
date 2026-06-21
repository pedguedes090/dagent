const { spawnSync } = require("child_process");
const { buildPythonEnv, getProjectRoot, resolvePythonCommand } = require("../src/main/pythonRuntime");

const projectRoot = getProjectRoot();
const python = resolvePythonCommand(projectRoot);
// pytest is the canonical runner for this project. It honors pyproject.toml's
// marker policy, so Windows-only heavy integration tests tagged `slow` are not
// accidentally executed by the default CI gate.
const result = spawnSync(python.command, [...python.args, "-m", "pytest", "tests"], {
  cwd: projectRoot,
  stdio: "inherit",
  env: buildPythonEnv({ projectRoot })
});

process.exit(result.status || 0);
