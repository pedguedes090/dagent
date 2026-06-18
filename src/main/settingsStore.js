const fs = require("fs");
const path = require("path");
const { parseJson } = require("./appDatabase");

const DEFAULT_SETTINGS = {
  serverUrl: "http://localhost:20128/v1",
  model: "gemini/gemini-3.1-flash-lite",
  apiKey: "",
  autoConfirmHumanGate: false
};

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

class SettingsStore {
  constructor(database, userDataPath) {
    this.database = database;
    this.legacyFilePath = path.join(userDataPath, "settings.json");
    this.migrateLegacyJson();
  }

  migrateLegacyJson() {
    if (this.database.getJsonSetting("modelConfig")) return;
    const legacy = readJson(this.legacyFilePath, null);
    if (legacy && typeof legacy === "object") {
      this.database.setJsonSetting("modelConfig", legacy);
    }
  }

  get() {
    const stored = this.database.getJsonSetting("modelConfig", {});
    return {
      ...DEFAULT_SETTINGS,
      ...stored
    };
  }

  save(nextSettings) {
    const current = this.get();
    const cleaned = {
      serverUrl: String(nextSettings.serverUrl || current.serverUrl).trim().replace(/\/+$/, ""),
      model: String(nextSettings.model || current.model).trim(),
      apiKey: String(nextSettings.apiKey ?? current.apiKey ?? "").trim(),
      autoConfirmHumanGate: Boolean(nextSettings.autoConfirmHumanGate)
    };
    const finalSettings = {
      ...DEFAULT_SETTINGS,
      ...cleaned
    };
    this.database.setJsonSetting("modelConfig", finalSettings);
    return finalSettings;
  }
}

module.exports = {
  DEFAULT_SETTINGS,
  SettingsStore
};
