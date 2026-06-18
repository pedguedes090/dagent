const { spawnSync } = require("child_process");
const { getProjectRoot, resolvePythonCommand, buildPythonEnv } = require("../src/main/pythonRuntime");

const projectRoot = getProjectRoot();
const python = resolvePythonCommand(projectRoot);
const result = spawnSync(python.command, [...python.args, "-m", "compileall", "engine"], {
  cwd: projectRoot,
  stdio: "inherit",
  env: buildPythonEnv({ projectRoot })
});

process.exit(result.status || 0);
