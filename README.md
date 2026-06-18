# Hệ Thống Agent

Ứng dụng desktop Electron chỉ là lớp điều khiển tối giản. Phần chính của dự án là **backend Python tách riêng** dùng LangGraph + OpenHands SDK thật: nhận task qua HTTP NDJSON nội bộ, đọc repo, lập kế hoạch, phản biện, tạo worker task spec, cho một worker OpenHands duy nhất sửa file, review tự động, rework có giới hạn và báo cáo lại.

## Chạy ứng dụng

```powershell
npm install
npm start
```

Python engine dùng `.venv` Python 3.12 và các dependency trong `pyproject.toml`:

```powershell
python -m uv venv --python 3.12 .venv
python -m uv pip install --python .venv\Scripts\python.exe -e .
```

Trên Linux/macOS, dùng `.venv/bin/python` ở lệnh install tương ứng.

Mặc định app dùng OpenAI-compatible endpoint:

- Server: `http://localhost:20128/v1`
- Model: `gemini/gemini-3.1-flash-lite`

Server, model và API key có thể đổi và lưu ngay trong giao diện.

Nếu provider yêu cầu auth, nhập API key trong giao diện. App lưu cấu hình trong SQLite tại thư mục `userData` của Electron, không còn ghi `settings.json`/`sessions.json` mới.

## Pipeline Chính

```text
User Task
  -> Preflight / Repo Snapshot
  -> Read-only Intake Committee
       - User Intent
       - Ambiguity & Edge Cases
       - Trusted Repo Context
  -> Intake Synthesizer
       - Problem Statement
       - Repro / Constraints / Risk Class
  -> Read-only Planning Committee
       - Minimal Plan
       - Robust Plan
       - Test-first Plan
  -> Critique Layer
       - Risk
       - Test Coverage
       - Security / Regression
  -> Plan Arbiter
       - Final Plan
       - Acceptance Criteria
       - Worker Task Spec
  -> Planner Agent
       - Task Graph
       - Role Contracts
       - SQLite Broker Dispatch
  -> Researcher / Context Agent
       - Trusted Context
       - CodeGraph Grounding
  -> Governance Service
       - Approval Policy
       - Sensitive Action Routing
  -> Human Gate for High-risk Tasks
  -> Coder Agent
       - Sandboxed OpenHands Worker
       - allowedFiles Merge Policy
  -> Tester Agent
       - Sandboxed Verification Commands
       - Affected Tests
  -> Security Reviewer Agent
       - Policy / Secret / Permission Review
  -> Code Reviewer Agent
       - Correctness / Regression Review
  -> Release / Deploy Agent
       - Release Notes
       - Rollback Plan
  -> Reviewer Decision
       - Merge or Rework
  -> Bounded Rework Loop
  -> Reporter
```

## Nguyên Tắc

- Mỗi role có ownership, input/output contract, memory scope, tool scope, approval policy và sandbox policy riêng.
- Planner Agent tạo task graph; Orchestrator dispatch subtask qua SQLite broker cục bộ.
- Chỉ `Coder Agent` được đề xuất thay đổi file, và Coder chạy OpenHands trong workspace sandbox.
- Coder chỉ được merge thay đổi trong `allowedFiles`; thay đổi ngoài policy bị rollback trong sandbox và báo blocker.
- Tester Agent chạy verification command trong sandbox, không làm bẩn workspace thật.
- Security Reviewer, Code Reviewer và Release/Deploy Agent là các role độc lập quyết định risk, merge readiness và rollback/deploy notes.
- Nếu task bị đánh dấu `high` risk, pipeline dừng ở human gate cho tới khi người dùng xác nhận.
- Tester không chạy dev server như `npm start`; chỉ chạy các lệnh verification an toàn như `npm run check`, `npm test`, `npm run build`, `pytest`, `go test`.
- Các yêu cầu chỉ đọc như “đọc”, “giải thích”, “tóm tắt”, “trả lời” sẽ không ghi file.

## Streaming Và OpenHands

- Trong lúc chạy, progress của LangGraph và event của OpenHands được stream vào một message tạm trong khung chat.
- Stage `task_intent` cho biết hệ thống đã nhận task thành `read-only`, `modify`, `create_project` hay `command` trước khi để LLM committee suy luận tiếp.
- Stage `codegraph_context` cho biết pipeline có dùng được semantic code context từ CodeGraph hay không.
- Event OpenHands được rút gọn thành các dòng dễ đọc như `terminal: npm run build`, `file_editor: edit src/App.jsx`, `task_tracker: ...`.
- Các stage mới như `planner_agent`, `researcher_agent`, `governance`, `coder_agent`, `tester_agent`, `security_reviewer`, `code_reviewer`, `release_deploy_agent` và `reviewer_decision` cho biết role nào đang sở hữu bước hiện tại.
- Khi pipeline hoàn tất, message stream tạm biến mất và được thay bằng báo cáo cuối đã lưu trong session.
- Coder bật `LLMSummarizingCondenser` để giảm rủi ro tràn context ở các task dài.
- Coder vẫn giữ `tool_concurrency_limit=1` để chỉ có một luồng ghi file trong sandbox.
- Bật `Auto xác nhận` trong giao diện để Human Gate tự pass các tác vụ high-risk.
- Nếu Human Gate dừng tác vụ high-risk, gửi `xác nhận` trong cùng phiên sẽ phê duyệt approval bền trong SQLite rồi chạy lại task gốc thay vì tạo task mới.
- LangGraph checkpoint dùng SQLite qua `langgraph-checkpoint-sqlite`.

## Observability

Backend Python có instrumentation OpenTelemetry trong `engine/agent_engine/telemetry.py`.

- Mỗi request `/v1/runs` tạo một `correlationId`, trả về header `X-Correlation-Id` và đưa id này vào run result.
- Mỗi task có trace root `agent.task`; mỗi node LangGraph là span `agent.step`.
- Các tool call chính có child span: LLM chat, CodeGraph, file read, verification command, sandbox create/merge/command và OpenHands events/conversation.
- SQLite broker lưu `correlation_id` trên runs, subtasks và events; payload event cũng mang `correlationId`.
- Metrics nền gồm run latency/success status, queue latency, verification pass/fail, rework count, token usage, sandbox failures, approval latency, crash recovery count và broker message count.

Cấu hình export:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
$env:OTEL_CONSOLE_EXPORTER = "true"
npm start
```

Nếu không cấu hình exporter, instrumentation vẫn chạy no-op/SDK-local và không làm nhiễu NDJSON stdout của backend.

## Evaluation Và Self-improvement Gate

Repo có benchmark nội bộ trong `benchmarks/internal_benchmark.json` với đủ 7 nhóm task: read-only, bugfix, refactor, scaffold project, security patch, migration và CI repair. Tất cả case dùng cùng rubric cố định:

- functional correctness
- diff minimality
- test pass
- security regressions
- latency
- cost

Chạy validate cấu trúc benchmark:

```powershell
npm run eval:validate
```

Chạy smoke evaluator không cần model server. Lệnh này dùng `solutionFiles` trong benchmark để kiểm thử scorer và ghi kết quả vào SQLite registry:

```powershell
npm run eval:smoke
```

Chạy benchmark thật qua pipeline agent hiện tại:

```powershell
node scripts/run-evaluation.js run --mode live --server-url http://localhost:20128/v1 --model gemini/gemini-3.1-flash-lite --prompt-version local-current --policy-version agent-contracts-v1 --keep-artifacts
```

So sánh các experiment đã log:

```powershell
npm run eval:compare
```

Kết quả được lưu ở `.agent-state/evaluation-registry.sqlite`, gồm experiment, case result, rubric scores, latency, token usage, changed files, verification output và model/prompt/policy version. Online adaptation mặc định vẫn tắt; `engine/agent_engine/self_improvement.py` chỉ cho phép bật adaptation khi có live evaluation pass đủ các category với điểm trung bình đạt ngưỡng.

## CodeGraph Acceleration

Dự án cài `@colbymchenry/codegraph` như dependency local để tăng tốc pha repo context/planning. Pipeline tự tạo project index bằng `codegraph init` khi workspace chưa có `.codegraph/`, nhưng không chạy installer global và không tự sửa config Codex/Claude/Cursor trên máy.

Cách hoạt động:

- Nếu workspace chưa có `.codegraph/`, node `codegraph_context` tự chạy `codegraph init .` một lần.
- Sau đó node `codegraph_context` gọi `codegraph explore` để lấy source liên quan, relationship map và blast radius cho task hiện tại.
- CodeGraph context được đưa vào Intake Synthesizer, Planning Committee, read-only reporter và OpenHands worker như **code data**, không phải repo instruction.
- Sau khi OpenHands sửa file, review stack gọi `codegraph affected --json` để gợi ý test liên quan tới changed files.
- Nếu CodeGraph init/query lỗi, pipeline bỏ qua CodeGraph và vẫn chạy bình thường.
- Telemetry được tắt bằng `CODEGRAPH_TELEMETRY=0` khi app khởi động backend Python.

Bật thủ công cho một workspace nếu muốn chuẩn bị trước:

```powershell
codegraph init
```

Xem trạng thái:

```powershell
codegraph status
```

## Cấu Hình Plugin / MCP / Skill

OpenHands SDK có thể load plugin trực tiếp từ workspace. Tạo file `.openhands/plugins.json` trong repo đang mở:

```json
{
  "plugins": [
    "github:owner/repo",
    {
      "source": "./local-openhands-plugin",
      "ref": "main",
      "repo_path": "plugins/web",
      "enabled": true
    }
  ]
}
```

Plugin có thể đóng gói skills, hooks, MCP config, agent và commands. Với MCP trực tiếp, tạo `.openhands/mcp.json` hoặc `.mcp.json` theo cấu trúc MCP config mà OpenHands SDK nhận vào `Agent(mcp_config=...)`.

Khuyến nghị thực tế:

- Đặt `AGENTS.md` ở root repo để mô tả convention, lệnh test/build, vùng cấm sửa, checklist review.
- Dùng plugin/skill cho tri thức lặp lại theo domain, ví dụ React/Vite, Python packaging, test policy, security checklist.
- Dùng MCP khi cần tool thật như docs nội bộ, issue tracker, database schema read-only, browser automation, hoặc package registry.
- Không bật MCP/plugin nặng mặc định cho mọi repo; chỉ bật theo workspace để giữ tốc độ và giảm nhiễu context.

## Cấu Trúc

- `engine/agent_engine/server.py`: backend HTTP NDJSON nội bộ để Electron gọi task API tách khỏi renderer/main UI.
- `engine/agent_engine/graph.py`: LangGraph orchestration, SQLite checkpoint, role graph, broker dispatch, human gate, sandboxed Coder, Tester/Security/Reviewer/Release agents và bounded rework loop.
- `engine/agent_engine/telemetry.py`: OpenTelemetry tracing/metrics helpers, correlation id propagation và exporter config.
- `engine/agent_engine/evaluation.py`: benchmark runner, rubric scorer và SQLite experiment registry cho evaluation-driven improvement.
- `engine/agent_engine/self_improvement.py`: safety gate cho online adaptation dựa trên live evaluation evidence.
- `engine/agent_engine/agent_contracts.py`: role contracts cho Planner, Researcher/Context, Coder, Tester, Security Reviewer, Code Reviewer và Release/Deploy Agent.
- `engine/agent_engine/broker.py`: SQLite-backed local broker cho agent runs, subtasks và events.
- `engine/agent_engine/multi_agent.py`: task graph, governance, review aggregation và release/deploy planning helpers.
- `engine/agent_engine/openhands_worker.py`: sandboxed Coder adapter dùng `LLM`, `Agent`, `Conversation`, `TerminalTool`, `FileEditorTool`, `TaskTrackerTool`.
- `engine/agent_engine/llm_client.py`: OpenAI-compatible client cho các committee read-only.
- `src/main/backendService.js`: khởi động backend Python và đọc stream NDJSON từ `/v1/runs`.
- `src/main/pythonRuntime.js`: chọn Python portable cho Windows/Linux/macOS, ưu tiên `.venv` rồi fallback `py -3`/`python3`.
- `src/main/appDatabase.js`: SQLite schema cho settings, sessions, messages, runs và approvals.
- `src/main/pythonEngine.js`: JSONL runner cũ còn giữ làm fallback/manual entrypoint, dùng chung Python runtime portable.
- `src/main/main.js`: Electron main process và IPC.
- `src/main/sessionStore.js`: lưu phiên chat, runs và approval state vào SQLite.
- `src/main/settingsStore.js`: lưu server/model/API key vào SQLite.
- `src/renderer/`: giao diện chat tối giản.
- `archive/`: pipeline JS cũ và helper cũ đã archive để tránh drift với runtime Python hiện tại.
- `benchmarks/`: benchmark nội bộ và fixture specs cho evaluator.
