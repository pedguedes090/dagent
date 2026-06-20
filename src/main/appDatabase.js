const fs = require("fs");
const path = require("path");
let DatabaseSync;
try {
  ({ DatabaseSync } = require("node:sqlite"));
} catch (error) {
  throw new Error(`SQLite runtime is unavailable in this Node/Electron build: ${error.message}`);
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function nowIso() {
  return new Date().toISOString();
}

function parseJson(value, fallback) {
  try {
    if (value === null || value === undefined || value === "") return fallback;
    return JSON.parse(value);
  } catch {
    return fallback;
  }
}

function stringifyJson(value) {
  return JSON.stringify(value ?? null);
}

class AppDatabase {
  constructor(userDataPath) {
    this.userDataPath = userDataPath;
    ensureDir(userDataPath);
    this.filePath = path.join(userDataPath, "agent-state.sqlite");
    this.db = new DatabaseSync(this.filePath);
    this.db.exec("PRAGMA foreign_keys = ON");
    this.db.exec("PRAGMA journal_mode = WAL");
    this.migrate();
  }

  migrate() {
    this._migrateV5_1_ProductGoal();
  }

  _migrateV5_1_ProductGoal() {
    // Add product_goal column to sessions if it doesn't exist yet.
    // SQLite ALTER TABLE with ADD COLUMN is idempotent — it fails
    // with "duplicate column name" on subsequent calls, which we ignore.
    try {
      this.db.exec("ALTER TABLE sessions ADD COLUMN product_goal TEXT NOT NULL DEFAULT ''");
    } catch (e) {
      if (!/duplicate column/i.test(String(e && e.message || ""))) throw e;
    }
  }

  /** @deprecated — actual migration happens in _migrateV5_1_ProductGoal; this
   *  block is the original CREATE TABLE IF NOT EXISTS set that bootstraps the
   *  database on first launch. Keep it clean. */
  _legacyMigrate() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        workspace_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        run_id TEXT,
        error INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        extra_json TEXT NOT NULL DEFAULT '{}',
        position INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        run_id TEXT,
        status TEXT NOT NULL,
        original_task TEXT NOT NULL,
        risk_class TEXT NOT NULL DEFAULT 'high',
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        approved_at TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
      );

      CREATE INDEX IF NOT EXISTS idx_messages_session_position ON messages(session_id, position);
      CREATE INDEX IF NOT EXISTS idx_runs_session_created ON runs(session_id, created_at);
      CREATE INDEX IF NOT EXISTS idx_approvals_session_status ON approvals(session_id, status, created_at);
    `);
  }

  getAppState(key, fallback = null) {
    const row = this.db.prepare("SELECT value FROM app_state WHERE key = ?").get(key);
    return row ? parseJson(row.value, fallback) : fallback;
  }

  setAppState(key, value) {
    this.db
      .prepare(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
      )
      .run(key, stringifyJson(value), nowIso());
  }

  getJsonSetting(key, fallback = null) {
    const row = this.db.prepare("SELECT value FROM settings WHERE key = ?").get(key);
    return row ? parseJson(row.value, fallback) : fallback;
  }

  setJsonSetting(key, value) {
    this.db
      .prepare(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
      )
      .run(key, stringifyJson(value), nowIso());
  }

  transaction(callback) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = callback();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  hasAnySession() {
    const row = this.db.prepare("SELECT COUNT(*) AS count FROM sessions").get();
    return Number(row?.count || 0) > 0;
  }

  close() {
    this.db.close();
  }
}

module.exports = {
  AppDatabase,
  nowIso,
  parseJson,
  stringifyJson
};
