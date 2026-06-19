const appApi = window.agentApp;

const state = {
  settings: null,
  sessions: [],
  activeSession: null,
  progress: [],
  observability: null,
  autonomy: null,
  autonomyScanning: false,
  autonomyAutoScanScheduled: false,
  running: false,
  activeTask: ""
};

const WORKFLOW_GROUPS = [
  {
    title: "Intake",
    stages: ["preflight", "task_intent", "actr_memory", "codegraph_context", "intake_user_intent", "intake_ambiguity", "intake_repo_context", "intake_synthesizer"]
  },
  {
    title: "Planning",
    stages: ["planning_minimal", "planning_robust", "planning_test_first", "critique_risk", "critique_test_coverage", "critique_security_regression", "plan_arbiter"]
  },
  {
    title: "Governance",
    stages: ["planner_task_graph", "researcher_context_agent", "governance_service", "human_gate", "environment_gate"]
  },
  {
    title: "Execution",
    stages: ["workspace_mode", "load_context_files", "setup_commands", "openhands_worker", "tester_agent"]
  },
  {
    title: "Review",
    stages: ["security_reviewer_agent", "code_reviewer_agent", "release_deploy_agent", "reviewer_decision", "execution_gate"]
  },
  {
    title: "Release",
    stages: ["reporter", "finalize_workspace", "reporter_end"]
  }
];

const STAGE_LABELS = {
  preflight: "Preflight",
  task_intent: "Task intent",
  actr_memory: "ACT-R memory",
  codegraph_context: "CodeGraph",
  codegraph_affected: "Affected tests",
  intake_user_intent: "User intent",
  intake_ambiguity: "Ambiguity",
  intake_repo_context: "Repo context",
  intake_synthesizer: "Synthesis",
  planning_minimal: "Plan: minimal",
  planning_robust: "Plan: robust",
  planning_test_first: "Plan: test-first",
  critique_risk: "Risk critique",
  critique_test_coverage: "Test critique",
  critique_security_regression: "Security critique",
  plan_arbiter: "Plan arbiter",
  planner_task_graph: "Task graph",
  researcher_context_agent: "Researcher",
  governance_service: "Governance",
  human_gate: "Human gate",
  environment_gate: "Environment gate",
  workspace_mode: "Workspace mode",
  load_context_files: "Context files",
  context: "Context",
  setup_commands: "Setup & install",
  openhands_worker: "OpenHands coder",
  openhands_context: "OpenHands context",
  openhands_plugins: "Plugins",
  openhands_mcp: "MCP",
  openhands_message: "OpenHands message",
  openhands_action: "OpenHands action",
  openhands_observation: "OpenHands observation",
  automated_review: "Automated review",
  tester_agent: "Tester",
  security_reviewer_agent: "Security reviewer",
  code_reviewer_agent: "Code reviewer",
  release_deploy_agent: "Release plan",
  reviewer_decision: "Review decision",
  execution_gate: "Execution gate",
  queued: "Queued",
  running: "Running",
  resume: "Resume",
  reporter: "Reporter",
  finalize_workspace: "Final merge",
  reporter_end: "Complete",
  done: "Done",
  error: "Error"
};

const elements = {
  workspaceLabel: document.querySelector("#workspaceLabel"),
  chooseFolderBtn: document.querySelector("#chooseFolderBtn"),
  sessionSelect: document.querySelector("#sessionSelect"),
  newSessionBtn: document.querySelector("#newSessionBtn"),
  serverInput: document.querySelector("#serverInput"),
  modelInput: document.querySelector("#modelInput"),
  apiKeyInput: document.querySelector("#apiKeyInput"),
  autoConfirmInput: document.querySelector("#autoConfirmInput"),
  directWorkspaceInput: document.querySelector("#directWorkspaceInput"),
  saveSettingsBtn: document.querySelector("#saveSettingsBtn"),
  saveStatus: document.querySelector("#saveStatus"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#messageInput"),
  sendBtn: document.querySelector("#sendBtn"),
  dashboardMetrics: document.querySelector("#dashboardMetrics"),
  dagBoard: document.querySelector("#dagBoard"),
  runSummary: document.querySelector("#runSummary"),
  artifactList: document.querySelector("#artifactList"),
  progressList: document.querySelector("#progressList"),
  runState: document.querySelector("#runState"),
  refreshObservabilityBtn: document.querySelector("#refreshObservabilityBtn"),
  systemLogList: document.querySelector("#systemLogList"),
  autonomyScanBtn: document.querySelector("#autonomyScanBtn"),
  autonomyStatus: document.querySelector("#autonomyStatus"),
  autonomySummary: document.querySelector("#autonomySummary")
};

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit"
  });
}

function compactText(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function getWorkspacePath() {
  return state.activeSession?.workspacePath || "";
}

function latestRun() {
  const runs = Array.isArray(state.activeSession?.runs) ? state.activeSession.runs : [];
  return runs.length ? runs[runs.length - 1] : null;
}

function latestAutonomyReport() {
  return state.autonomy?.report || state.autonomy?.lastReport || null;
}

function autonomyWorkspaceMatches() {
  const report = latestAutonomyReport();
  return Boolean(report?.workspacePath && getWorkspacePath() && report.workspacePath === getWorkspacePath());
}

function timeline() {
  const run = latestRun();
  const persisted = Array.isArray(run?.progressEvents) ? run.progressEvents : [];
  return state.running || state.progress.length ? state.progress : persisted;
}

function stageLabel(stage) {
  return STAGE_LABELS[stage] || String(stage || "Step").replaceAll("_", " ");
}

function runStatus(run = latestRun()) {
  if (state.running) return "running";
  if (!run) return "idle";
  if (run.error) return "error";
  if (run.humanGate?.status === "pending") return "waiting";
  const blockers = run.review?.blockers || [];
  if (Array.isArray(blockers) && blockers.length) return "blocked";
  return "completed";
}

function statusLabel(status) {
  const labels = {
    idle: "Sẵn sàng",
    running: "Đang chạy",
    waiting: "Chờ phê duyệt",
    blocked: "Bị chặn",
    error: "Lỗi",
    completed: "Hoàn tất"
  };
  return labels[status] || status;
}

function timelineIndex() {
  const events = timeline();
  const byStage = new Map();
  events.forEach((item, index) => {
    byStage.set(item.stage, { ...item, index });
  });
  return { events, byStage, latest: events[events.length - 1] || null };
}

function nodeStatus(stage, indexData) {
  const event = indexData.byStage.get(stage);
  if (!event) return "idle";
  if (stage === "error" || event.stage === "error") return "error";
  if (indexData.latest?.stage === stage && state.running) return "running";
  if (/approval|phê duyệt|pending|chờ/i.test(String(event.detail || ""))) return "waiting";
  return "done";
}

function renderSettings() {
  elements.serverInput.value = state.settings?.serverUrl || "";
  elements.modelInput.value = state.settings?.model || "";
  elements.apiKeyInput.value = state.settings?.apiKey || "";
  elements.autoConfirmInput.checked = Boolean(state.settings?.autoConfirmHumanGate);
  elements.directWorkspaceInput.checked = state.settings?.directWorkspaceMode !== false;
}

function renderWorkspace() {
  const workspacePath = getWorkspacePath();
  elements.workspaceLabel.textContent = workspacePath || "Chưa mở thư mục";
}

function renderSessions() {
  elements.sessionSelect.innerHTML = "";
  if (!state.sessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Chưa có phiên";
    elements.sessionSelect.appendChild(option);
    return;
  }

  for (const session of state.sessions) {
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = session.title || "Phiên mới";
    elements.sessionSelect.appendChild(option);
  }

  elements.sessionSelect.value = state.activeSession?.id || state.sessions[0]?.id || "";
}

function renderMetrics() {
  const run = latestRun();
  const status = runStatus(run);
  const events = timeline();
  const changedFiles = Array.isArray(run?.changedFiles) ? run.changedFiles : [];
  const metrics = [
    ["Trạng thái", statusLabel(status)],
    ["Node hiện tại", stageLabel(events[events.length - 1]?.stage || "idle")],
    ["Changed files", String(changedFiles.length)],
    ["Token usage", String(run?.tokenUsage || 0)]
  ];

  elements.dashboardMetrics.innerHTML = "";
  for (const [label, value] of metrics) {
    const card = document.createElement("div");
    card.className = `metric-card metric-${status}`;
    const labelNode = document.createElement("div");
    labelNode.className = "metric-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "metric-value";
    valueNode.textContent = value;
    card.append(labelNode, valueNode);
    elements.dashboardMetrics.appendChild(card);
  }
}

function renderDagBoard() {
  const indexData = timelineIndex();
  elements.dagBoard.innerHTML = "";
  for (const group of WORKFLOW_GROUPS) {
    const column = document.createElement("section");
    column.className = "dag-column";
    const heading = document.createElement("h3");
    heading.textContent = group.title;
    column.appendChild(heading);

    for (const stage of group.stages) {
      const event = indexData.byStage.get(stage);
      const status = nodeStatus(stage, indexData);
      const card = document.createElement("article");
      card.className = `dag-node ${status}`;
      const title = document.createElement("div");
      title.className = "dag-node-title";
      const dot = document.createElement("span");
      dot.className = "status-dot";
      const label = document.createElement("span");
      label.textContent = stageLabel(stage);
      title.append(dot, label);
      const detail = document.createElement("p");
      detail.textContent = event ? compactText(event.detail, 110) : "Chưa chạy";
      card.append(title, detail);
      column.appendChild(card);
    }

    elements.dagBoard.appendChild(column);
  }
}

function renderRunSummary() {
  const run = latestRun();
  elements.runSummary.innerHTML = "";
  if (!run) {
    elements.runSummary.textContent = "Chưa có run nào. Mở workspace rồi chạy một tác vụ để xem trạng thái.";
    return;
  }

  const rows = [
    ["Task", run.task || state.activeTask || state.activeSession?.title],
    ["Execution", run.executionId || run.id],
    ["Correlation", run.correlationId],
    ["Review", run.review?.passed === false ? "Không đạt" : run.review?.passed === true ? "Đạt" : "Không có"],
    ["Human gate", run.humanGate?.status || "Không"]
  ];
  for (const [label, value] of rows) {
    if (!value) continue;
    const row = document.createElement("div");
    row.className = "summary-row";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = compactText(value, 160);
    row.append(key, val);
    elements.runSummary.appendChild(row);
  }

  if (run.assistantText) {
    const text = document.createElement("p");
    text.className = "summary-text";
    text.textContent = compactText(run.assistantText, 520);
    elements.runSummary.appendChild(text);
  }
}

function renderArtifacts() {
  const run = latestRun();
  elements.artifactList.innerHTML = "";
  const changedFiles = Array.isArray(run?.changedFiles) ? run.changedFiles : [];
  const blockers = Array.isArray(run?.review?.blockers) ? run.review.blockers : [];
  const items = [
    ...changedFiles.map((file) => ({
      label: file.path || file,
      detail: file.status || "changed",
      type: "file"
    })),
    ...blockers.map((blocker) => ({
      label: blocker.agent || blocker.type || "blocker",
      detail: blocker.detail || blocker.message || JSON.stringify(blocker),
      type: "blocker"
    }))
  ];
  if (run?.humanGate?.status === "pending") {
    items.push({
      label: "human_gate",
      detail: run.humanGate.reason || "Cần xác nhận trước khi chạy tiếp",
      type: "blocker"
    });
  }
  if (!items.length) {
    elements.artifactList.textContent = "Chưa có tệp thay đổi hoặc blocker.";
    return;
  }
  for (const item of items.slice(0, 20)) {
    const row = document.createElement("div");
    row.className = `artifact-item ${item.type}`;
    const label = document.createElement("strong");
    label.textContent = item.label;
    const detail = document.createElement("span");
    detail.textContent = compactText(item.detail, 160);
    row.append(label, detail);
    elements.artifactList.appendChild(row);
  }
}

function renderProgress() {
  const events = timeline();
  const latestStage = events[events.length - 1]?.stage;
  elements.runState.textContent = latestStage === "queued" ? "Đang chờ" : statusLabel(runStatus());
  elements.progressList.innerHTML = "";

  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "progress-detail";
    empty.textContent = "Chưa có event.";
    elements.progressList.appendChild(empty);
    return;
  }

  for (const item of events.slice(-50)) {
    const row = document.createElement("div");
    row.className = `progress-item ${item.stage === "done" || item.stage === "error" ? "" : "active"}`;

    const stage = document.createElement("div");
    stage.className = "progress-stage";
    stage.textContent = `${formatTime(item.at)} ${stageLabel(item.stage)}`.trim();

    const detail = document.createElement("div");
    detail.className = "progress-detail";
    detail.textContent = item.detail || "";

    row.append(stage, detail);
    elements.progressList.appendChild(row);
  }

  elements.progressList.scrollTop = elements.progressList.scrollHeight;
}

function renderSystemLog() {
  elements.systemLogList.innerHTML = "";
  const events = Array.isArray(state.observability?.recentEvents) ? state.observability.recentEvents : [];
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "progress-detail";
    empty.textContent = "Chưa có backend event.";
    elements.systemLogList.appendChild(empty);
    return;
  }
  for (const event of events.slice(-24).reverse()) {
    const row = document.createElement("div");
    row.className = "system-log-item";
    const type = document.createElement("strong");
    type.textContent = event.eventType || event.stage || "event";
    const detail = document.createElement("span");
    detail.textContent = compactText(event.detail || event.error || event.taskPreview || JSON.stringify(event), 180);
    row.append(type, detail);
    elements.systemLogList.appendChild(row);
  }
}

function renderAutonomy() {
  const report = latestAutonomyReport();
  const memory = state.autonomy?.memory || report?.memory || {};
  const findings = Array.isArray(report?.findings) ? report.findings : [];
  const initiatives = Array.isArray(report?.longHorizonPlan?.initiatives) ? report.longHorizonPlan.initiatives : [];
  const proposals = Array.isArray(report?.skillProposals) ? report.skillProposals : [];

  elements.autonomySummary.innerHTML = "";
  elements.autonomyScanBtn.textContent = state.autonomyScanning ? "Đang quét…" : "Quét idle";

  if (!getWorkspacePath()) {
    elements.autonomyStatus.textContent = "Mở workspace để bật quét nợ kỹ thuật khi idle.";
    return;
  }

  if (state.autonomyScanning) {
    elements.autonomyStatus.textContent = "Autonomy đang quét read-only; không ghi workspace và không execute command.";
  } else if (!report) {
    elements.autonomyStatus.textContent = "Chưa có báo cáo L4/L5. Dashboard sẽ tự quét khi hệ thống idle, hoặc bấm Quét idle.";
  } else {
    const workspaceNote = autonomyWorkspaceMatches() ? "" : " · báo cáo thuộc workspace khác";
    elements.autonomyStatus.textContent = `${findings.length} finding · ${initiatives.length} initiative · ${proposals.length} skill proposal · memory ${memory.total || 0}${workspaceNote}`;
  }

  const metrics = [
    ["Memory activation", memory.averageActivation ?? "—"],
    ["Findings", String(findings.length)],
    ["Initiatives", String(initiatives.length)],
    ["L5 proposals", String(proposals.length)]
  ];
  const metricGrid = document.createElement("div");
  metricGrid.className = "autonomy-metrics";
  for (const [label, value] of metrics) {
    const item = document.createElement("div");
    item.className = "autonomy-metric";
    const key = document.createElement("span");
    key.textContent = label;
    const val = document.createElement("strong");
    val.textContent = String(value);
    item.append(key, val);
    metricGrid.appendChild(item);
  }
  elements.autonomySummary.appendChild(metricGrid);

  const topFinding = findings[0];
  const topInitiative = initiatives[0];
  const topProposal = proposals[0];
  const rows = [
    topFinding && {
      label: "Priority finding",
      title: `${topFinding.severity || "risk"} · ${topFinding.title || topFinding.category}`,
      detail: `${topFinding.source || ""} — ${topFinding.recommendation || topFinding.evidence || ""}`
    },
    topInitiative && {
      label: "Long horizon",
      title: topInitiative.title,
      detail: topInitiative.strategicTradeoff || topInitiative.objective
    },
    topProposal && {
      label: "L5 proposal",
      title: topProposal.name,
      detail: topProposal.proposedModel
    }
  ].filter(Boolean);

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "progress-detail";
    empty.textContent = report ? "Không có vấn đề ưu tiên cao trong lần quét gần nhất." : "Đang chờ báo cáo autonomy.";
    elements.autonomySummary.appendChild(empty);
    return;
  }

  for (const item of rows) {
    const row = document.createElement("div");
    row.className = "autonomy-item";
    const label = document.createElement("span");
    label.textContent = item.label;
    const title = document.createElement("strong");
    title.textContent = compactText(item.title, 120);
    const detail = document.createElement("p");
    detail.textContent = compactText(item.detail, 220);
    row.append(label, title, detail);
    elements.autonomySummary.appendChild(row);
  }
}

function renderControls() {
  const disabled = state.running;
  elements.chooseFolderBtn.disabled = disabled;
  elements.newSessionBtn.disabled = disabled;
  elements.sessionSelect.disabled = disabled;
  elements.saveSettingsBtn.disabled = disabled;
  elements.serverInput.disabled = disabled;
  elements.modelInput.disabled = disabled;
  elements.apiKeyInput.disabled = disabled;
  elements.autoConfirmInput.disabled = disabled;
  elements.directWorkspaceInput.disabled = disabled;
  elements.messageInput.disabled = disabled;
  elements.sendBtn.disabled = disabled;
  elements.refreshObservabilityBtn.disabled = disabled;
  elements.autonomyScanBtn.disabled = disabled || state.autonomyScanning || !getWorkspacePath();
}

function renderDashboard() {
  renderMetrics();
  renderDagBoard();
  renderRunSummary();
  renderArtifacts();
  renderProgress();
  renderSystemLog();
  renderAutonomy();
}

function render() {
  renderSettings();
  renderWorkspace();
  renderSessions();
  renderDashboard();
  renderControls();
}

function setStatus(text, timeout = 1800) {
  elements.saveStatus.textContent = text;
  if (timeout) {
    setTimeout(() => {
      if (elements.saveStatus.textContent === text) elements.saveStatus.textContent = "";
    }, timeout);
  }
}

async function refreshObservability() {
  const [observabilityResult, autonomyResult] = await Promise.allSettled([
    appApi.getObservability(),
    appApi.getAutonomyStatus()
  ]);

  if (observabilityResult.status === "fulfilled") {
    state.observability = observabilityResult.value;
  } else {
    state.observability = {
      recentEvents: [
        {
          eventType: "observability.error",
          error: observabilityResult.reason?.message || String(observabilityResult.reason)
        }
      ]
    };
  }

  if (autonomyResult.status === "fulfilled") {
    state.autonomy = autonomyResult.value;
  } else {
    state.autonomy = {
      error: autonomyResult.reason?.message || String(autonomyResult.reason),
      memory: state.autonomy?.memory || { total: 0 },
      lastReport: state.autonomy?.lastReport || null
    };
  }
  renderDashboard();
}

async function performAutonomyScan({ automatic = false } = {}) {
  if (!getWorkspacePath() || state.running || state.autonomyScanning) return;
  state.autonomyScanning = true;
  renderDashboard();
  renderControls();
  try {
    state.autonomy = await appApi.runAutonomyScan({ workspacePath: getWorkspacePath(), automatic });
    if (!automatic) setStatus("Đã quét autonomy L4/L5", 2200);
  } catch (error) {
    state.autonomy = {
      ...state.autonomy,
      error: error.message,
      lastReport: latestAutonomyReport()
    };
    if (!automatic) setStatus(`Autonomy chưa quét được: ${error.message}`, 3600);
  } finally {
    state.autonomyScanning = false;
    renderDashboard();
    renderControls();
  }
}

function scheduleIdleAutonomyScan() {
  if (state.autonomyAutoScanScheduled || state.autonomyScanning || state.running || !getWorkspacePath()) return;
  if (autonomyWorkspaceMatches()) return;
  state.autonomyAutoScanScheduled = true;
  setTimeout(() => {
    state.autonomyAutoScanScheduled = false;
    if (!state.running && !state.autonomyScanning && getWorkspacePath() && !autonomyWorkspaceMatches()) {
      performAutonomyScan({ automatic: true });
    }
  }, 1800);
}

async function ensureSession() {
  if (state.activeSession) return state.activeSession;
  const result = await appApi.createSession({ workspacePath: "" });
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  return state.activeSession;
}

async function saveSettings() {
  state.settings = await appApi.saveSettings({
    serverUrl: elements.serverInput.value,
    model: elements.modelInput.value,
    apiKey: elements.apiKeyInput.value,
    autoConfirmHumanGate: elements.autoConfirmInput.checked,
    directWorkspaceMode: elements.directWorkspaceInput.checked
  });
  renderSettings();
  setStatus("Đã lưu");
}

async function chooseWorkspace() {
  const workspacePath = await appApi.chooseWorkspace();
  if (!workspacePath) return;
  const session = await ensureSession();
  const result = await appApi.updateSessionWorkspace(session.id, workspacePath);
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.activeTask = "";
  render();
  scheduleIdleAutonomyScan();
}

async function createSession() {
  const result = await appApi.createSession({ workspacePath: getWorkspacePath() });
  state.activeSession = result.session;
  state.sessions = result.sessions.sessions;
  state.progress = [];
  state.activeTask = "";
  render();
  scheduleIdleAutonomyScan();
  elements.messageInput.focus();
}

async function loadSession(sessionId) {
  if (!sessionId || sessionId === state.activeSession?.id) return;
  const session = await appApi.loadSession(sessionId);
  if (!session) return;
  state.activeSession = session;
  state.progress = [];
  state.activeTask = "";
  render();
  scheduleIdleAutonomyScan();
}

async function sendMessage(event) {
  event.preventDefault();
  const content = elements.messageInput.value.trim();
  if (!content || state.running) return;

  await saveSettings();
  const session = await ensureSession();
  if (!getWorkspacePath()) {
    setStatus("Hãy mở thư mục trước", 2600);
    return;
  }

  state.running = true;
  state.progress = [
    {
      stage: "running",
      detail: "Bắt đầu chạy pipeline",
      at: new Date().toISOString()
    }
  ];
  state.activeTask = content;
  elements.messageInput.value = "";
  elements.messageInput.style.height = "auto";
  render();

  try {
    const result = await appApi.sendMessage({
      sessionId: session.id,
      workspacePath: getWorkspacePath(),
      settings: state.settings,
      content
    });
    state.activeSession = result.session;
    state.sessions = result.sessions.sessions;
    if (result.error) setStatus(result.error, 3600);
    await refreshObservability();
    scheduleIdleAutonomyScan();
  } catch (error) {
    setStatus(`Chưa gửi được yêu cầu: ${error.message}`, 3600);
  } finally {
    state.running = false;
    render();
    elements.messageInput.focus();
  }
}

function autoResizeInput() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 170)}px`;
}

async function init() {
  const initial = await appApi.getInitialState();
  state.settings = initial.settings;
  state.sessions = initial.sessions.sessions || [];
  state.activeSession = initial.activeSession || null;

  appApi.onProgress((progress) => {
    if (state.activeSession && progress.sessionId !== state.activeSession.id) return;
    state.progress.push(progress);
    renderDashboard();
    renderControls();
  });

  render();
  await refreshObservability();
  scheduleIdleAutonomyScan();
  elements.messageInput.focus();
}

elements.chooseFolderBtn.addEventListener("click", chooseWorkspace);
elements.newSessionBtn.addEventListener("click", createSession);
elements.saveSettingsBtn.addEventListener("click", saveSettings);
elements.sessionSelect.addEventListener("change", (event) => loadSession(event.target.value));
elements.composer.addEventListener("submit", sendMessage);
elements.messageInput.addEventListener("input", autoResizeInput);
elements.refreshObservabilityBtn.addEventListener("click", refreshObservability);
elements.autonomyScanBtn.addEventListener("click", () => performAutonomyScan({ automatic: false }));
elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

init().catch((error) => {
  document.body.textContent = `Không khởi động được dashboard: ${error.message}`;
});
