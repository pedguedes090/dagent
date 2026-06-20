const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { nowIso, parseJson, stringifyJson } = require("./appDatabase");

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function readJson(filePath, fallback) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, value) {
  ensureDir(path.dirname(filePath));
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2), "utf8");
}

function makeTitle(text) {
  const compact = String(text || "Phiên mới").replace(/\s+/g, " ").trim();
  return compact.length > 48 ? `${compact.slice(0, 45)}...` : compact || "Phiên mới";
}

class SessionStore {
  constructor(database, userDataPath) {
    this.database = database;
    this.legacyFilePath = path.join(userDataPath, "sessions.json");
    this.migrateLegacyJson();
  }

  migrateLegacyJson() {
    if (this.database.hasAnySession()) return;
    const state = readJson(this.legacyFilePath, null);
    if (!state || !Array.isArray(state.sessions)) return;
    const insertSession = this.database.db.prepare(
      "INSERT OR REPLACE INTO sessions (id, title, workspace_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
    );
    const insertMessage = this.database.db.prepare(
      "INSERT OR REPLACE INTO messages (id, session_id, role, content, run_id, error, created_at, extra_json, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    );
    const insertRun = this.database.db.prepare(
      "INSERT OR REPLACE INTO runs (id, session_id, task, status, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
    );
    const insertApproval = this.database.db.prepare(
      "INSERT OR REPLACE INTO approvals (id, session_id, run_id, status, original_task, risk_class, reason, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    );

    this.database.transaction(() => {
      for (const session of state.sessions) {
        const createdAt = session.createdAt || nowIso();
        const updatedAt = session.updatedAt || createdAt;
        insertSession.run(session.id, session.title || makeTitle(session.messages?.[0]?.content), session.workspacePath || "", createdAt, updatedAt);
        (session.messages || []).forEach((message, index) => {
          insertMessage.run(
            message.id || crypto.randomUUID(),
            session.id,
            message.role || "assistant",
            message.content || "",
            message.runId || null,
            message.error ? 1 : 0,
            message.createdAt || updatedAt,
            stringifyJson({ streaming: Boolean(message.streaming) }),
            index
          );
        });
        (session.runs || []).forEach((run) => {
          const runId = run.id || crypto.randomUUID();
          const runCreatedAt = run.createdAt || updatedAt;
          insertRun.run(runId, session.id, run.task || "", run.error ? "error" : "completed", runCreatedAt, run.updatedAt || runCreatedAt, stringifyJson({ ...run, id: runId, createdAt: runCreatedAt }));
          if (run.humanGate) {
            insertApproval.run(
              run.humanGate.id || crypto.randomUUID(),
              session.id,
              runId,
              run.humanGate.status || "pending",
              run.humanGate.originalTask || run.task || "",
              run.humanGate.riskClass || "high",
              run.humanGate.reason || "",
              run.humanGate.createdAt || runCreatedAt,
              run.humanGate.approvedAt || null
            );
          }
        });
      }
    });
    this.database.setAppState("activeSessionId", state.activeSessionId || state.sessions[0]?.id || null);
  }

  activeSessionId() {
    return this.database.getAppState("activeSessionId", null);
  }

  list() {
    const rows = this.database.db
      .prepare("SELECT id, title, workspace_path, created_at, updated_at FROM sessions ORDER BY updated_at DESC")
      .all();
    return {
      activeSessionId: this.activeSessionId(),
      sessions: rows.map((session) => ({
        id: session.id,
        title: session.title,
        workspacePath: session.workspace_path || "",
        createdAt: session.created_at,
        updatedAt: session.updated_at
      }))
    };
  }

  create(initial = {}) {
    const timestamp = nowIso();
    const session = {
      id: crypto.randomUUID(),
      title: makeTitle(initial.title),
      workspacePath: initial.workspacePath || "",
      productGoal: String(initial.productGoal || ""),
      messages: [],
      runs: [],
      createdAt: timestamp,
      updatedAt: timestamp
    };
    this.database.db
      .prepare("INSERT INTO sessions (id, title, workspace_path, product_goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)")
      .run(session.id, session.title, session.workspacePath, session.productGoal, session.createdAt, session.updatedAt);
    this.database.setAppState("activeSessionId", session.id);
    return session;
  }

  get(sessionId) {
    const row = this.database.db
      .prepare("SELECT id, title, workspace_path, product_goal, created_at, updated_at FROM sessions WHERE id = ?")
      .get(sessionId);
    if (!row) return null;
    const messages = this.database.db
      .prepare("SELECT * FROM messages WHERE session_id = ? ORDER BY position ASC, created_at ASC")
      .all(sessionId)
      .map((message) => ({
        ...parseJson(message.extra_json, {}),
        id: message.id,
        role: message.role,
        content: message.content,
        runId: message.run_id || undefined,
        error: Boolean(message.error),
        createdAt: message.created_at
      }));
    const runs = this.database.db
      .prepare("SELECT payload_json FROM runs WHERE session_id = ? ORDER BY created_at ASC")
      .all(sessionId)
      .map((run) => parseJson(run.payload_json, {}));
    return {
      id: row.id,
      title: row.title,
      workspacePath: row.workspace_path || "",
      productGoal: row.product_goal || "",
      messages,
      runs,
      createdAt: row.created_at,
      updatedAt: row.updated_at
    };
  }

  save(session) {
    const nextSession = {
      ...session,
      title: session.title || makeTitle(session.messages?.[0]?.content),
      messages: Array.isArray(session.messages) ? session.messages : [],
      runs: Array.isArray(session.runs) ? session.runs : [],
      updatedAt: nowIso()
    };

    this.database.transaction(() => {
      this.database.db
        .prepare(
          "INSERT INTO sessions (id, title, workspace_path, product_goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) " +
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, workspace_path = excluded.workspace_path, product_goal = excluded.product_goal, updated_at = excluded.updated_at"
        )
        .run(nextSession.id, nextSession.title, nextSession.workspacePath || "", nextSession.productGoal || "", nextSession.createdAt || nowIso(), nextSession.updatedAt);

      this.database.db.prepare("DELETE FROM messages WHERE session_id = ?").run(nextSession.id);
      const insertMessage = this.database.db.prepare(
        "INSERT INTO messages (id, session_id, role, content, run_id, error, created_at, extra_json, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
      );
      nextSession.messages.forEach((message, index) => {
        const extra = {
          streaming: Boolean(message.streaming)
        };
        insertMessage.run(
          message.id || crypto.randomUUID(),
          nextSession.id,
          message.role || "assistant",
          message.content || "",
          message.runId || null,
          message.error ? 1 : 0,
          message.createdAt || nextSession.updatedAt,
          stringifyJson(extra),
          index
        );
      });

      this.database.db.prepare("DELETE FROM runs WHERE session_id = ?").run(nextSession.id);
      this.database.db.prepare("DELETE FROM approvals WHERE session_id = ? AND run_id IS NOT NULL").run(nextSession.id);
      const insertRun = this.database.db.prepare(
        "INSERT INTO runs (id, session_id, task, status, created_at, updated_at, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)"
      );
      const insertApproval = this.database.db.prepare(
        "INSERT OR REPLACE INTO approvals (id, session_id, run_id, status, original_task, risk_class, reason, created_at, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
      );
      nextSession.runs.forEach((run) => {
        const runId = run.id || crypto.randomUUID();
        const runCreatedAt = run.createdAt || nextSession.updatedAt;
        const payload = { ...run, id: runId, createdAt: runCreatedAt };
        insertRun.run(runId, nextSession.id, run.task || "", run.error ? "error" : "completed", runCreatedAt, run.updatedAt || runCreatedAt, stringifyJson(payload));
        if (run.humanGate) {
          insertApproval.run(
            run.humanGate.id || crypto.randomUUID(),
            nextSession.id,
            runId,
            run.humanGate.status || "pending",
            run.humanGate.originalTask || run.task || "",
            run.humanGate.riskClass || "high",
            run.humanGate.reason || "",
            run.humanGate.createdAt || runCreatedAt,
            run.humanGate.approvedAt || null
          );
        }
      });
    });
    this.database.setAppState("activeSessionId", nextSession.id);
    return nextSession;
  }

  delete(sessionId) {
    this.database.db.prepare("DELETE FROM sessions WHERE id = ?").run(sessionId);
    if (this.activeSessionId() === sessionId) {
      const next = this.database.db.prepare("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1").get();
      this.database.setAppState("activeSessionId", next?.id || null);
    }
    return this.list();
  }

  getPendingApproval(sessionId) {
    const row = this.database.db
      .prepare(
        "SELECT id, session_id, run_id, status, original_task, risk_class, reason, created_at FROM approvals " +
          "WHERE session_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1"
      )
      .get(sessionId);
    if (!row) return null;
    const runRow = row.run_id ? this.database.db.prepare("SELECT payload_json FROM runs WHERE id = ?").get(row.run_id) : null;
    const runPayload = parseJson(runRow?.payload_json, {});
    return {
      id: row.id,
      sessionId: row.session_id,
      runId: row.run_id,
      status: row.status,
      originalTask: row.original_task,
      riskClass: row.risk_class,
      reason: row.reason,
      correlationId: runPayload.humanGate?.correlationId || runPayload.correlationId || null,
      executionId: runPayload.humanGate?.executionId || runPayload.executionId || runPayload.id || null,
      kind: runPayload.humanGate?.kind || "risk_approval",
      retryCount: Number(runPayload.humanGate?.retryCount || 0),
      reworkCycle: Number(runPayload.humanGate?.reworkCycle || 0),
      grantAdditionalAttempts: Number(runPayload.humanGate?.grantAdditionalAttempts || 0),
      createdAt: row.created_at
    };
  }

  approvePendingApproval(sessionId, approvalId) {
    const approvedAt = nowIso();
    this.database.db
      .prepare("UPDATE approvals SET status = 'approved', approved_at = ? WHERE id = ? AND session_id = ?")
      .run(approvedAt, approvalId, sessionId);
    const row = this.database.db.prepare("SELECT run_id FROM approvals WHERE id = ?").get(approvalId);
    if (row?.run_id) {
      const runRow = this.database.db.prepare("SELECT payload_json FROM runs WHERE id = ?").get(row.run_id);
      const payload = parseJson(runRow?.payload_json, {});
      if (payload.humanGate) {
        payload.humanGate = {
          ...payload.humanGate,
          status: "approved",
          approvedAt
        };
        this.database.db
          .prepare("UPDATE runs SET payload_json = ?, updated_at = ? WHERE id = ?")
          .run(stringifyJson(payload), approvedAt, row.run_id);
      }
    }
    return approvedAt;
  }

  reconcileStartupState() {
    const recoveredAt = nowIso();
    const rows = this.database.db
      .prepare("SELECT id, payload_json FROM runs WHERE status IN ('queued', 'running', 'needs_rework')")
      .all();
    if (!rows.length) return { recoveredRuns: 0 };

    const updateRun = this.database.db.prepare("UPDATE runs SET status = ?, payload_json = ?, updated_at = ? WHERE id = ?");
    this.database.transaction(() => {
      rows.forEach((row) => {
        const payload = parseJson(row.payload_json, {});
        updateRun.run(
          "recovered",
          stringifyJson({
            ...payload,
            status: "recovered",
            recoveredAt,
            recoveryReason: "App restarted while this UI run was not terminal. Engine broker/checkpoint DB remains authoritative for execution recovery."
          }),
          recoveredAt,
          row.id
        );
      });
    });
    return { recoveredRuns: rows.length };
  }
}

module.exports = {
  SessionStore
};
