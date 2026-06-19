from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _target_dir(spec: dict[str, Any]) -> str:
    value = str(spec.get("targetProjectDir") or spec.get("projectRoot") or ".").strip().replace("\\", "/").strip("/")
    while value.startswith("./"):
        value = value[2:]
    if not value or value == ".":
        return "."
    if value.startswith("../") or value == ".." or ":" in value:
        return "."
    return value


def _package_name(target: str) -> str:
    name = "todo-app" if target == "." else target.rsplit("/", 1)[-1]
    name = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return name or "todo-app"


def _scaffold_intent_text(spec: dict[str, Any]) -> str:
    authoritative_fields = {
        "objective": spec.get("objective"),
        "targetProjectDir": spec.get("targetProjectDir"),
        "projectRoot": spec.get("projectRoot"),
        "acceptanceCriteria": spec.get("acceptanceCriteria"),
    }
    return json.dumps(authoritative_fields, ensure_ascii=False, default=str).lower()


def should_scaffold_todo_fallback(spec: dict[str, Any]) -> bool:
    stack = str(spec.get("projectStack") or "").lower()
    text = _scaffold_intent_text(spec)
    vocabulary_signals = (
        "vocabulary",
        "từ vựng",
        "tu vung",
        "học tiếng anh",
        "hoc tieng anh",
        "english word",
        "flashcard",
    )
    todo_signals = ("todo", "to-do", "task list", "danh sách việc", "danh sach viec")
    requests_vocabulary = any(signal in text for signal in vocabulary_signals)
    requests_todo = any(signal in text for signal in todo_signals)
    return stack in {"node", "web", "generic", ""} and requests_todo and not requests_vocabulary


def scaffold_todo_app(workspace: str, spec: dict[str, Any]) -> dict[str, Any]:
    target = _target_dir(spec)
    root = Path(workspace).resolve()
    app_root = root if target == "." else root / target
    app_root.mkdir(parents=True, exist_ok=True)
    (app_root / "src").mkdir(exist_ok=True)
    (app_root / "scripts").mkdir(exist_ok=True)

    files = {
        "package.json": _package_json(_package_name(target)),
        "index.html": _index_html(),
        "src/app.js": _app_js(),
        "src/styles.css": _styles_css(),
        "scripts/build.js": _build_js(),
        "README.md": _readme(),
    }
    written = []
    for relative_path, content in files.items():
        path = app_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(relative_path if target == "." else f"{target}/{relative_path}")
    return {"targetProjectDir": target, "writtenFiles": written}


def scaffold_project_fallback(workspace: str, spec: dict[str, Any]) -> dict[str, Any]:
    if should_scaffold_todo_fallback(spec):
        result = scaffold_todo_app(workspace, spec)
        return {"used": True, "kind": "todo_app", **result}
    return {"used": False, "reason": "No deterministic scaffold is available for this task."}


def _package_json(name: str) -> str:
    return json.dumps(
        {
            "name": name,
            "version": "1.0.0",
            "private": True,
            "type": "module",
            "scripts": {
                "build": "node scripts/build.js",
                "start": "node scripts/build.js && node -e \"console.log('Open index.html or dist/index.html in your browser')\"",
            },
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _index_html() -> str:
    return """<!doctype html>
<html lang="vi">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Todo App</title>
    <link rel="stylesheet" href="./src/styles.css" />
  </head>
  <body>
    <main class="shell">
      <section class="toolbar" aria-label="Todo controls">
        <div>
          <p class="eyebrow">Daily planner</p>
          <h1>Todo App</h1>
        </div>
        <div class="stats">
          <span id="total-count">0</span>
          <small>tasks</small>
        </div>
      </section>

      <form id="todo-form" class="composer" autocomplete="off">
        <input id="todo-input" name="todo" type="text" maxlength="120" placeholder="Add a task..." aria-label="Task name" />
        <select id="todo-priority" aria-label="Priority">
          <option value="normal">Normal</option>
          <option value="high">High</option>
          <option value="low">Low</option>
        </select>
        <button type="submit">Add</button>
      </form>

      <section class="filters" aria-label="Task filters">
        <button class="filter active" data-filter="all" type="button">All</button>
        <button class="filter" data-filter="active" type="button">Active</button>
        <button class="filter" data-filter="done" type="button">Done</button>
        <input id="search-input" type="search" placeholder="Search..." aria-label="Search tasks" />
      </section>

      <ul id="todo-list" class="todo-list" aria-live="polite"></ul>
      <p id="empty-state" class="empty-state">No tasks yet. Add one to start.</p>
    </main>
    <script src="./src/app.js" type="module"></script>
  </body>
</html>
"""


def _app_js() -> str:
    return """const storageKey = 'modern-todo-items';

const form = document.querySelector('#todo-form');
const input = document.querySelector('#todo-input');
const priority = document.querySelector('#todo-priority');
const list = document.querySelector('#todo-list');
const emptyState = document.querySelector('#empty-state');
const totalCount = document.querySelector('#total-count');
const searchInput = document.querySelector('#search-input');
const filterButtons = Array.from(document.querySelectorAll('.filter'));

let todos = loadTodos();
let currentFilter = 'all';
let searchTerm = '';

function loadTodos() {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveTodos() {
  localStorage.setItem(storageKey, JSON.stringify(todos));
}

function createTodo(text, level) {
  return {
    id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
    text: text.trim(),
    priority: level,
    done: false,
    createdAt: new Date().toISOString(),
  };
}

function visibleTodos() {
  return todos.filter((todo) => {
    const matchesFilter =
      currentFilter === 'all' ||
      (currentFilter === 'active' && !todo.done) ||
      (currentFilter === 'done' && todo.done);
    const matchesSearch = todo.text.toLowerCase().includes(searchTerm.toLowerCase());
    return matchesFilter && matchesSearch;
  });
}

function render() {
  const items = visibleTodos();
  list.replaceChildren(...items.map(renderItem));
  emptyState.hidden = items.length > 0;
  totalCount.textContent = String(todos.length);
}

function renderItem(todo) {
  const item = document.createElement('li');
  item.className = `todo-item ${todo.done ? 'done' : ''}`;

  const checkbox = document.createElement('button');
  checkbox.className = 'check';
  checkbox.type = 'button';
  checkbox.setAttribute('aria-label', todo.done ? 'Mark active' : 'Mark done');
  checkbox.textContent = todo.done ? '✓' : '';
  checkbox.addEventListener('click', () => {
    todo.done = !todo.done;
    saveTodos();
    render();
  });

  const content = document.createElement('div');
  content.className = 'todo-content';

  const title = document.createElement('span');
  title.className = 'todo-title';
  title.textContent = todo.text;

  const meta = document.createElement('span');
  meta.className = `priority ${todo.priority}`;
  meta.textContent = todo.priority;

  content.append(title, meta);

  const remove = document.createElement('button');
  remove.className = 'remove';
  remove.type = 'button';
  remove.setAttribute('aria-label', `Delete ${todo.text}`);
  remove.textContent = 'Delete';
  remove.addEventListener('click', () => {
    todos = todos.filter((item) => item.id !== todo.id);
    saveTodos();
    render();
  });

  item.append(checkbox, content, remove);
  return item;
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  todos.unshift(createTodo(text, priority.value));
  input.value = '';
  priority.value = 'normal';
  saveTodos();
  render();
});

filterButtons.forEach((button) => {
  button.addEventListener('click', () => {
    currentFilter = button.dataset.filter;
    filterButtons.forEach((item) => item.classList.toggle('active', item === button));
    render();
  });
});

searchInput.addEventListener('input', () => {
  searchTerm = searchInput.value.trim();
  render();
});

render();
"""


def _styles_css() -> str:
    return """:root {
  color-scheme: light;
  --bg: #f4f7fb;
  --panel: #ffffff;
  --text: #172033;
  --muted: #667085;
  --line: #d9e2ef;
  --accent: #2563eb;
  --accent-dark: #1d4ed8;
  --green: #0f9f6e;
  --red: #dc2626;
  --shadow: 0 20px 60px rgba(28, 41, 61, 0.14);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  min-height: 100vh;
  margin: 0;
  background: radial-gradient(circle at top left, #e7f0ff 0, transparent 34%), var(--bg);
  color: var(--text);
}

button,
input,
select {
  font: inherit;
}

.shell {
  width: min(960px, calc(100% - 28px));
  margin: 0 auto;
  padding: 28px 0 48px;
}

.toolbar,
.composer,
.filters,
.todo-list,
.empty-state {
  width: min(760px, 100%);
  margin-inline: auto;
}

.toolbar {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.eyebrow {
  margin: 0 0 6px;
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 800;
  text-transform: uppercase;
}

h1 {
  margin: 0;
  font-size: clamp(2rem, 5vw, 4rem);
  line-height: 1;
}

.stats {
  min-width: 84px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
  text-align: center;
}

.stats span {
  display: block;
  font-size: 1.6rem;
  font-weight: 800;
}

.stats small {
  color: var(--muted);
}

.composer,
.filters,
.todo-item,
.empty-state {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: var(--shadow);
}

.composer {
  display: grid;
  grid-template-columns: 1fr 132px auto;
  gap: 10px;
  padding: 12px;
}

.composer input,
.composer select,
.filters input {
  min-height: 46px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0 14px;
  color: var(--text);
  background: #fff;
}

.composer button,
.filter,
.remove,
.check {
  min-height: 42px;
  border: 0;
  border-radius: 8px;
  cursor: pointer;
  font-weight: 800;
}

.composer button {
  padding: 0 22px;
  color: #fff;
  background: var(--accent);
}

.composer button:hover {
  background: var(--accent-dark);
}

.filters {
  display: grid;
  grid-template-columns: repeat(3, auto) 1fr;
  gap: 8px;
  margin-top: 14px;
  padding: 10px;
}

.filter {
  padding: 0 14px;
  color: var(--muted);
  background: #eef4ff;
}

.filter.active {
  color: #fff;
  background: var(--text);
}

.todo-list {
  display: grid;
  gap: 10px;
  padding: 0;
  margin-top: 16px;
  list-style: none;
}

.todo-item {
  display: grid;
  grid-template-columns: 44px 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 12px;
}

.check {
  width: 38px;
  min-height: 38px;
  border: 2px solid var(--line);
  background: #fff;
  color: var(--green);
  font-size: 1.2rem;
}

.todo-content {
  min-width: 0;
  display: grid;
  gap: 5px;
}

.todo-title {
  overflow-wrap: anywhere;
  font-weight: 750;
}

.done .todo-title {
  color: var(--muted);
  text-decoration: line-through;
}

.priority {
  width: fit-content;
  border-radius: 999px;
  padding: 3px 8px;
  color: #334155;
  background: #e2e8f0;
  font-size: 0.74rem;
  font-weight: 800;
  text-transform: uppercase;
}

.priority.high {
  color: #991b1b;
  background: #fee2e2;
}

.priority.low {
  color: #166534;
  background: #dcfce7;
}

.remove {
  padding: 0 12px;
  color: var(--red);
  background: #fff1f2;
}

.empty-state {
  margin-top: 16px;
  padding: 24px;
  color: var(--muted);
  text-align: center;
}

[hidden] {
  display: none !important;
}

@media (max-width: 720px) {
  .toolbar {
    align-items: stretch;
  }

  .composer,
  .filters {
    grid-template-columns: 1fr;
  }

  .todo-item {
    grid-template-columns: 40px 1fr;
  }

  .remove {
    grid-column: 2;
    justify-self: start;
  }
}
"""


def _build_js() -> str:
    return """import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const dist = path.join(root, 'dist');
fs.rmSync(dist, { recursive: true, force: true });
fs.mkdirSync(path.join(dist, 'src'), { recursive: true });

for (const file of ['index.html']) {
  fs.copyFileSync(path.join(root, file), path.join(dist, file));
}

for (const file of ['app.js', 'styles.css']) {
  fs.copyFileSync(path.join(root, 'src', file), path.join(dist, 'src', file));
}

console.log('Build completed successfully');
"""


def _readme() -> str:
    return """# Todo App

Responsive todo app built with plain HTML, CSS and JavaScript.

## Run

```powershell
npm run build
```

Open `index.html` or `dist/index.html` in a browser.
"""
