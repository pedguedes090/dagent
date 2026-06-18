const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("agentApp", {
  getInitialState: () => ipcRenderer.invoke("app:getInitialState"),
  saveSettings: (settings) => ipcRenderer.invoke("settings:save", settings),
  chooseWorkspace: () => ipcRenderer.invoke("workspace:choose"),
  listSessions: () => ipcRenderer.invoke("sessions:list"),
  createSession: (initial) => ipcRenderer.invoke("sessions:create", initial),
  loadSession: (sessionId) => ipcRenderer.invoke("sessions:load", sessionId),
  updateSessionWorkspace: (sessionId, workspacePath) =>
    ipcRenderer.invoke("sessions:updateWorkspace", sessionId, workspacePath),
  deleteSession: (sessionId) => ipcRenderer.invoke("sessions:delete", sessionId),
  sendMessage: (payload) => ipcRenderer.invoke("agent:send", payload),
  getObservability: () => ipcRenderer.invoke("agent:observability"),
  getAutonomyStatus: () => ipcRenderer.invoke("agent:autonomy-status"),
  runAutonomyScan: (payload) => ipcRenderer.invoke("agent:autonomy-scan", payload),
  onProgress: (callback) => {
    const listener = (_event, progress) => callback(progress);
    ipcRenderer.on("agent:progress", listener);
    return () => ipcRenderer.removeListener("agent:progress", listener);
  }
});
