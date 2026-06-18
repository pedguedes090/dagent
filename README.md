# Hệ Thống Agent

Ứng dụng desktop Electron chỉ là lớp điều khiển tối giản. Phần chính của dự án là **backend Python tách riêng** dùng LangGraph + OpenHands SDK thật: nhận task qua HTTP NDJSON nội bộ, đọc repo, lập kế hoạch, phản biện, tạo worker task spec, cho một worker OpenHands duy nhất sửa file, review tự động, rework có giới hạn và báo cáo lại.

## Chạy ứng dụng

```powershell
corepack enable
pnpm install
pnpm start
```

Repo khóa `pnpm@11.7.0` trong `packageManager`. `pnpm-workspace.yaml` bật
`enableGlobalVirtualStore: true`, vì vậy các git worktree dùng chung kho ảo
toàn cục thay vì nhân bản `node_modules/.pnpm`.

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
       - YAML/Jinja2 deterministic route
       - Execution Gate after fixed retry budget
  -> Reporter
```

## Nguyên Tắc

- Mỗi role có ownership, input/output contract, memory scope, tool scope, approval policy và sandbox policy riêng.
- Toàn bộ topology, fan-out, join và conditional route được khai báo trong `engine/agent_engine/workflows/default.yaml`; Jinja2 sandbox chỉ đánh giá các fact boolean do code tất định cung cấp.
- LLM vẫn phân tích/lập kế hoạch/review ngữ nghĩa nhưng không được chọn node tiếp theo, tăng retry budget hoặc đổi điều kiện rẽ nhánh.
- Mỗi node nhận một context envelope theo allowlist `contextRoutes`; history, settings, secret và output không thuộc cạnh hiện tại không được đưa vào prompt.
- Planner Agent tạo task graph; Orchestrator dispatch subtask qua SQLite broker cục bộ.
- Mỗi execution ghi file được cấp một git worktree riêng. Workspace nguồn chỉ nhận thay đổi sau khi Tester, Security Reviewer và Code Reviewer đều đạt.
- Coder chỉ được merge thay đổi trong `allowedFiles`; thay đổi ngoài policy bị rollback trong worktree và báo blocker.
- Worker result luôn mang debug contract: `sandboxDiff`, `policyViolations`, `appliedChanges`, `selectedExecutionRoot`, `events`, `error`.
- Docker/Podman là lớp sandbox ưu tiên, không phải điều kiện bắt buộc. Nếu có container, shell command chạy với network `none`, root filesystem read-only, drop toàn bộ Linux capabilities, `no-new-privileges`, giới hạn PID/RAM/CPU và chỉ mount worktree làm vùng ghi.
- Nếu thiếu Docker/Podman, Coder vẫn chạy trong git worktree với `PolicyFileEditorTool`; shell tool bị tắt, còn Tester chỉ chạy verification command allowlist trên bản sao tạm của worktree.
- Tester chạy trên một bản sao tạm của worktree rồi hủy bản sao đó, nên script build/test không thể làm bẩn nội dung đang chờ merge.
- Worktree loại credential phổ biến và symlink trước khi chạy agent; merge cuối cùng kiểm tra lại `allowedFiles`, `forbiddenPaths`, symlink và xung đột nguồn theo cơ chế all-or-nothing.
- Security Reviewer, Code Reviewer và Release/Deploy Agent là các role độc lập quyết định risk, merge readiness và rollback/deploy notes.
- Nếu task bị đánh dấu `high` risk, pipeline dừng ở human gate cho tới khi người dùng xác nhận.
- Sau đúng `maxReworkAttempts` trong workflow YAML, pipeline dừng ở execution gate. Xác nhận chỉ cấp đúng `approvalGrantAttempts` lượt bổ sung; bộ đếm không bị reset và không có vòng lặp vô hạn.
- Tester không chạy dev server như `npm start`; chỉ chạy các lệnh verification an toàn như `npm run check`, `npm test`, `npm run build`, `pytest`, `go test`.
- Các yêu cầu chỉ đọc như “đọc”, “giải thích”, “tóm tắt”, “trả lời” sẽ không ghi file.

## Container Sandbox

Task ghi file vẫn yêu cầu Git để tạo worktree-per-execution. Docker/Podman là tùy chọn: có thì dùng container sandbox mạnh; thiếu thì pipeline chuyển sang host fallback giới hạn, không cấp shell tự do cho Coder và chỉ chạy verification allowlist trên bản sao tạm. Nếu muốn bắt buộc container trong môi trường doanh nghiệp, đặt:

```powershell
$env:AGENT_REQUIRE_CONTAINER = "1"
```

Image mặc định:

- Node: `node:24-bookworm-slim`
- Python: `python:3.12-slim`
- Go: `golang:1.24-bookworm`
- Rust: `rust:1.87-bookworm`
- Generic: `debian:bookworm-slim`

Mặc định image phải có sẵn cục bộ để tránh tự tải dependency ngoài ý muốn:

```powershell
docker pull node:24-bookworm-slim
docker pull python:3.12-slim
```

Có thể chọn Podman hoặc image nội bộ:

```powershell
$env:AGENT_CONTAINER_RUNTIME = "podman"
$env:AGENT_SANDBOX_IMAGE_NODE = "registry.internal/agent-node@sha256:..."
```

`AGENT_SANDBOX_ALLOW_PULL=true` cho phép runtime tự pull image; chế độ mặc định vẫn là `--pull never`.

## State Authority

Hệ thống dùng hai miền SQLite cục bộ có authority khác nhau để tránh drift sau crash:

- App DB `agent-state.sqlite` là nguồn sự thật cho UI/session: settings không chứa secret thô, danh sách phiên, messages, run summaries đã trả về UI và approval records người dùng thấy.
- Engine DB `agent-broker.sqlite` là nguồn sự thật cho execution runtime: run/subtask status, broker events, crash recovery của worker roles.
- LangGraph checkpoint DB `langgraph-checkpoints.sqlite` là nguồn sự thật cho checkpoint/resume nội bộ của graph.
- Durable supervisor DB `durable-executions.sqlite` quản lý execution ID riêng cho từng task, lease/heartbeat, retry count, kết quả idempotent và checkpoint của từng tool call.
- Mỗi execution dùng `executionId` làm LangGraph `thread_id`; approval tạo thread dẫn xuất từ cùng ID. Retry kỹ thuật gọi graph với input `None` để tiếp tục đúng node đang pending.
- OpenHands lưu conversation và worktree metadata dưới `${AGENT_ENGINE_STATE_DIR}/executions/`; worktree được giữ lại qua lỗi mạng/human gate và chỉ merge sau review đạt.
- Startup app gọi `SessionStore.reconcileStartupState()` để đánh dấu các UI run không-terminal còn sót là `recovered`.
- Startup backend gọi `SQLiteAgentBroker.recover_incomplete_runs()` để chuyển execution run/subtask kẹt ở `running`, `queued`, `needs_rework` sang `recovered`.
- Startup backend cũng chuyển execution có lease dở dang sang `recoverable`; Electron tự kết nối lại một lần với cùng `executionId`.
- Không dùng App DB để phán quyết worker đã merge file hay chưa; xem `appliedChanges`, `policyViolations`, broker events và JSONL debug log của engine.

## Secrets

- `settings.modelConfig` trong SQLite chỉ lưu cấu hình không nhạy cảm như `serverUrl`, `model`, `autoConfirmHumanGate`.
- API key không persist raw trong SQLite. Main process dùng Electron `safeStorage` để mã hóa secret vào `secrets/model-api-key.bin` bằng OS-backed encryption khi khả dụng.
- Nếu OS encryption không khả dụng, API key chỉ giữ trong memory của phiên chạy và người dùng cần nhập lại sau restart.

## Streaming Và OpenHands

- Giao diện chính là Web Dashboard: DAG board hiển thị trạng thái từng node, metric cards, kết quả gần nhất, changed files/blockers, live timeline và backend event log.
- UI không còn render transcript chat cuộn dài; session messages vẫn được lưu nội bộ để engine có context và để xác nhận Human Gate/Execution Gate.
- Backend giữ single-run lock toàn cục. Nếu có run khác đang chạy, request sau sẽ stream stage `queued`, rồi `running` khi lấy được lock.
- Stage `task_intent` cho biết hệ thống đã nhận task thành `read-only`, `modify`, `create_project` hay `command` trước khi để LLM committee suy luận tiếp.
- Stage `codegraph_context` cho biết pipeline có dùng được semantic code context từ CodeGraph hay không.
- Event OpenHands được rút gọn thành các dòng dễ đọc như `terminal: npm run build`, `file_editor: edit src/App.jsx`, `task_tracker: ...`.
- Các stage mới như `planner_agent`, `researcher_agent`, `governance`, `coder_agent`, `tester_agent`, `security_reviewer`, `code_reviewer`, `release_deploy_agent` và `reviewer_decision` cho biết role nào đang sở hữu bước hiện tại.
- Khi pipeline hoàn tất, run payload lưu `progressEvents` để Dashboard mở lại phiên vẫn xem được timeline thay vì mất log sau khi stream trôi qua.
- Coder bật `LLMSummarizingCondenser` để giảm rủi ro tràn context ở các task dài.
- Coder vẫn giữ `tool_concurrency_limit=1` để chỉ có một luồng ghi file trong worktree.
- Bật `Auto xác nhận` trong giao diện để Human Gate tự pass các tác vụ high-risk.
- Nếu Human Gate/Execution Gate dừng tác vụ, gửi `xác nhận` trong cùng phiên sẽ dùng lại chính `executionId`, worktree và durable state; approval dùng một checkpoint thread dẫn xuất mới để không cộng dồn context cũ.
- LangGraph checkpoint dùng SQLite qua `langgraph-checkpoint-sqlite`.

## Observability

Backend Python có instrumentation OpenTelemetry trong `engine/agent_engine/telemetry.py`.

- Mỗi request `/v1/runs` tạo một `correlationId`, trả về header `X-Correlation-Id` và đưa id này vào run result.
- Mỗi task có trace root `agent.task`; mỗi node LangGraph là span `agent.step`.
- Các tool call chính có child span: LLM chat, CodeGraph, file read, verification command, sandbox create/merge/command và OpenHands events/conversation.
- SQLite broker lưu `correlation_id` trên runs, subtasks và events; payload event cũng mang `correlationId`.
- Metrics nền gồm run latency/success status, queue latency, verification pass/fail, rework count, token usage, sandbox failures, approval latency, crash recovery count và broker message count.
- Endpoint nội bộ `GET /v1/observability` trả trạng thái run lock, thư mục log và các debug event gần nhất; Electron Dashboard gọi endpoint này qua IPC `agent:observability`.

Cấu hình export:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
$env:OTEL_CONSOLE_EXPORTER = "true"
pnpm start
```

Nếu không cấu hình exporter, instrumentation vẫn chạy no-op/SDK-local và không làm nhiễu NDJSON stdout của backend.

Debug log dạng JSONL cũng được ghi bền theo ngày để dễ grep khi UI stream đã trôi qua:

- Khi chạy trong app Electron: `${AGENT_ENGINE_STATE_DIR}/logs/agent-debug-YYYYMMDD.jsonl` dưới thư mục user data của app.
- Khi chạy dev/CLI và chưa set `AGENT_ENGINE_STATE_DIR`: `.agent-state/logs/agent-debug-YYYYMMDD.jsonl`.
- Các event hữu ích khi debug flow tạo app: `http.run_received`, `plan.worker_spec_normalized`, `progress`, `broker.message`, `policy.violations`, `run.result`, `run.error`.
- Với lỗi kiểu `Coder agent changes were filtered by allowedFiles policy`, xem `plan.worker_spec_normalized` để kiểm tra `targetProjectDir`, `verificationCwd`, `allowedFiles`; xem tiếp `policy.violations` để biết file nào bị rollback.

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
pnpm run eval:validate
```

Chạy smoke evaluator không cần model server. Lệnh này dùng `solutionFiles` trong benchmark để kiểm thử scorer và ghi kết quả vào SQLite registry:

```powershell
pnpm run eval:smoke
```

Chạy benchmark thật qua pipeline agent hiện tại:

```powershell
node scripts/run-evaluation.js run --mode live --server-url http://localhost:20128/v1 --model gemini/gemini-3.1-flash-lite --prompt-version local-current --policy-version agent-contracts-v1 --keep-artifacts
```

So sánh các experiment đã log:

```powershell
pnpm run eval:compare
```

Kết quả được lưu ở `.agent-state/evaluation-registry.sqlite`, gồm experiment, case result, rubric scores, latency, token usage, changed files, verification output và model/prompt/policy version. Online adaptation mặc định vẫn tắt; `engine/agent_engine/self_improvement.py` chỉ cho phép bật adaptation khi có live evaluation pass đủ các category với điểm trung bình đạt ngưỡng.

## Autonomy L4/L5: ACT-R Memory Và Intrinsic Motivation

Giai đoạn 4 bổ sung control-plane tự trị bậc cao nhưng vẫn fail-safe: hệ thống có thể chủ động quét read-only khi idle, ghi nhớ finding theo mô hình ACT-R activation/decay, lập initiative dài hạn và đề xuất skill/tool mới ở dạng proposal-only. Nó không tự sửa workspace, không tự chạy command và không vượt qua human/evaluation gate.

- Bộ nhớ dài hạn nằm ở `${AGENT_ENGINE_STATE_DIR}/long-term-memory.sqlite` hoặc `.agent-state/long-term-memory.sqlite` khi chạy dev/CLI.
- Memory activation dùng importance + rehearsal count + lexical relevance - decay theo thời gian. Core/architecture memories decay chậm; error/failure memories decay nhanh hơn để lỗi cũ mờ đi nếu không tái xuất hiện.
- Nội dung trước khi lưu memory được redaction các secret/token/password/private key pattern phổ biến.
- Pipeline preflight tự seed trusted root context vào ACT-R memory, retrieve các memory liên quan đến task và chuyển qua explicit context route `longTermMemory` cho Intake/Researcher như dữ liệu tham khảo, không phải instruction.
- Endpoint `GET /v1/autonomy/status` trả memory stats và report gần nhất.
- Endpoint `POST /v1/autonomy/idle-scan` chỉ chạy khi global run lock đang rảnh; nếu pipeline đang chạy sẽ trả `409 run_lock_active`.
- Electron Dashboard có panel **Autonomy L4/L5** hiển thị finding ưu tiên cao, initiative dài hạn, skill proposal L5 và nút “Quét idle”. Dashboard cũng tự lên lịch một scan read-only ngắn khi workspace đang idle và chưa có report khớp workspace.

Report gần nhất nằm ở `${AGENT_ENGINE_STATE_DIR}/autonomy/last-report.json` và có 3 phần chính:

- `findings`: nợ kỹ thuật, security smell, module quá lớn, test coverage gap.
- `longHorizonPlan`: initiative 2-6 tuần với trade-off chiến lược và acceptance criteria.
- `skillProposals`: đề xuất L5 dạng `proposal-only`, ví dụ static analyzer hoặc test-gap mapper, bắt buộc qua evaluation/human gate trước khi trở thành tool thực thi.

## Kiểm Thử Và CI

```powershell
pnpm test
pnpm run check
```

`pnpm test` chạy unit tests Python trong `tests/`. `pnpm run check` chạy JS syntax checks, Python compile, benchmark validation và unit tests. GitHub Actions workflow ở `.github/workflows/ci.yml` chạy cùng gate này trên push/PR.

Coverage hiện có gồm:

- project creation spec normalizer theo stack Python/Node
- read-only intent routing
- hard rollback cho `allowedFiles`
- verification command allowlist/dev-server filter
- benchmark schema validation
- LLM retry/backoff/circuit breaker smoke tests
- SQLite broker correlation/recovery smoke tests
- backend `/health` integration smoke test
- OpenHands worker cleanup đóng `Conversation` trước khi xóa sandbox để tránh `WinError 32` trên Windows
- YAML/Jinja2 deterministic route validation và cấm topology hard-code trong graph
- context envelope allowlist, gồm kiểm tra không rò history/settings qua cạnh reviewer
- worktree dirty-baseline, conflict-safe merge và cleanup
- container hardening flags, network isolation và host fallback khi thiếu runtime
- bounded rework thực tế dừng ở Execution Gate sau đúng retry budget
- approval grant không reset retry budget và chỉ chạy đúng số lượt bổ sung trong YAML
- verifier dùng bản sao tạm; thiếu Docker/Podman thì chạy host allowlist trên bản sao; merge cuối chặn cwd escape, credential, symlink và thay đổi ngoài policy
- approval tiếp tục cùng execution/worktree nhưng dùng checkpoint thread dẫn xuất mới
- Dashboard renderer không còn chat transcript, có DAG board và backend observability log
- backend `/v1/observability` trả debug events gần nhất
- MCP config chỉ load server được trust rõ ràng, command/env/url/cwd được sanitize trước khi truyền vào OpenHands
- ACT-R long-term memory có activation/decay, reinforcement và secret redaction
- Autonomy idle discovery sinh technical-debt findings, long-horizon plan và L5 skill proposals ở chế độ proposal-only
- backend `/v1/autonomy/status` và `/v1/autonomy/idle-scan` có idle lock guard

## Troubleshooting

- Nếu từng gặp `[WinError 32] ... hethongagent-sandbox-*\\workspace`, nguyên nhân thường là OpenHands tool executor còn giữ file/process handle khi Python xóa thư mục sandbox tạm. Worker hiện gọi `Conversation.close()` trước khi cleanup, còn sandbox cleanup có retry và không làm hỏng run nếu Windows vẫn khóa file trong chốc lát.
- Nếu task tạo web/app mới bị báo không có `package.json` ở workspace root, nguyên nhân thường là planner/coder tạo project trong thư mục con như `todo-app` nhưng verification lại chạy ở root. Pipeline hiện chuẩn hóa project creation để `targetProjectDir`, `projectRoot`, `verificationCwd` cùng trỏ về thư mục app và tự thêm `todo-app/**` vào `allowedFiles`.

## CodeGraph Acceleration

Dự án cài `@colbymchenry/codegraph` như dependency local để tăng tốc pha repo context/planning. Pipeline tự tạo project index bằng `codegraph init` khi workspace chưa có `.codegraph/`, nhưng không chạy installer global và không tự sửa config Codex/Claude/Cursor trên máy.

Cách hoạt động:

- Nếu workspace chưa có `.codegraph/`, node `codegraph_context` tự chạy `codegraph init .` một lần.
- Sau đó node `codegraph_context` gọi `codegraph explore` để lấy source liên quan, relationship map và blast radius cho task hiện tại.
- CodeGraph context đi qua explicit handoff: Intake Repo Context → Synthesizer, hoặc Researcher Context → Worker Context; nó luôn là **code data**, không phải repo instruction.
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

Plugin có thể đóng gói skills, hooks, MCP config, agent và commands. Với MCP trực tiếp, tạo `.openhands/mcp.json` hoặc `.mcp.json`. Loader chỉ truyền các server đã được trust rõ ràng vào `Agent(mcp_config=...)`, đồng thời kiểm tra command/url/cwd và bắt buộc secret nằm ở dạng placeholder `${ENV_VAR}`.

Ví dụ MCP an toàn:

```json
{
  "trustedServers": ["internalDocs"],
  "mcpServers": {
    "internalDocs": {
      "command": "node",
      "args": ["./tools/internal-docs-mcp.js"],
      "env": {
        "DOCS_API_TOKEN": "${DOCS_API_TOKEN}"
      }
    },
    "issueTracker": {
      "trusted": true,
      "url": "https://mcp.example.internal/sse"
    }
  }
}
```

Khuyến nghị thực tế:

- Đặt `AGENTS.md` ở root repo để mô tả convention, lệnh test/build, vùng cấm sửa, checklist review.
- Dùng plugin/skill cho tri thức lặp lại theo domain, ví dụ React/Vite, Python packaging, test policy, security checklist.
- Dùng MCP khi cần tool thật như docs nội bộ, issue tracker, database schema read-only, browser automation, hoặc package registry.
- Chỉ thêm server vào `trustedServers` hoặc đặt `"trusted": true` sau khi repo owner đã review command/url/env; server không được trust sẽ bị bỏ qua và ghi event `openhands_mcp`.
- Không bật MCP/plugin nặng mặc định cho mọi repo; chỉ bật theo workspace để giữ tốc độ và giảm nhiễu context.

## Cấu Trúc

- `engine/agent_engine/server.py`: backend HTTP NDJSON nội bộ để Electron gọi task API tách khỏi renderer/main UI.
- `src/renderer/app.js`: Web Dashboard node-state, run metrics, progress timeline và observability log.
- `engine/agent_engine/graph.py`: LangGraph nodes, durable checkpoint, context envelopes, human/execution gates và controlled worktree merge.
- `engine/agent_engine/workflows/default.yaml`: topology, fan-out/join, Jinja2 route rules, context allowlists và hard retry limits.
- `engine/agent_engine/deterministic_workflow.py`: YAML validator, sandboxed Jinja2 evaluator và graph assembler.
- `engine/agent_engine/worktree_manager.py`: worktree-per-execution, dirty-baseline synchronization, conflict-safe merge và cleanup.
- `engine/agent_engine/container_sandbox.py`: Docker/Podman hardening policy và OpenHands `ContainerTerminalTool`.
- `engine/agent_engine/telemetry.py`: OpenTelemetry tracing/metrics helpers, correlation id propagation và exporter config.
- `engine/agent_engine/evaluation.py`: benchmark runner, rubric scorer và SQLite experiment registry cho evaluation-driven improvement.
- `engine/agent_engine/self_improvement.py`: safety gate cho online adaptation dựa trên live evaluation evidence.
- `engine/agent_engine/long_term_memory.py`: SQLite ACT-R memory store với activation/decay, reinforcement và redaction.
- `engine/agent_engine/autonomy.py`: idle technical-debt discovery, long-horizon planning và L5 skill proposal generation.
- `engine/agent_engine/agent_contracts.py`: role contracts cho Planner, Researcher/Context, Coder, Tester, Security Reviewer, Code Reviewer và Release/Deploy Agent.
- `engine/agent_engine/broker.py`: SQLite-backed local broker cho agent runs, subtasks và events.
- `engine/agent_engine/multi_agent.py`: task graph, governance, review aggregation và release/deploy planning helpers.
- `engine/agent_engine/openhands_worker.py`: worktree-isolated Coder adapter dùng `LLM`, `Agent`, `Conversation`, `ContainerTerminalTool`, `FileEditorTool`, `TaskTrackerTool`.
- `engine/agent_engine/llm_client.py`: OpenAI-compatible client cho các committee read-only, có retry/backoff/circuit breaker nhẹ.
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
- `tests/`: unit tests cho policy, graph normalization, evaluator và LLM resilience.
- `.github/workflows/ci.yml`: CI gate chạy install, compile, benchmark validation và unit tests.
