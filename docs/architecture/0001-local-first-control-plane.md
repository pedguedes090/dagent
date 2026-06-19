# ADR 0001: Keep a Local-First Control Plane

- Status: Accepted
- Date: 2026-06-18

## Context

The desktop application currently runs one local Python backend, LangGraph, OpenHands workers, and SQLite persistence. It had four independently interpreted semantic stores:

- Electron UI/session state
- broker run/subtask/event state
- LangGraph checkpoints
- durable execution/step state

It also serialized every pipeline and autonomy scan behind one process-wide lock, including tasks that cannot write to the workspace.

Temporal solves durable workflow execution, retries, event history, worker scaling, signals, and versioning. It also requires a Temporal Service plus worker lifecycle management. Temporal's local development server is a separate service process, while production requires Temporal Cloud or a production-ready self-hosted service. That is a poor default dependency for the current single-user desktop deployment.

LangGraph checkpointers already provide thread-scoped graph checkpoints for resume, human-in-the-loop, and fault tolerance. They should remain execution-machine state rather than becoming the business authority for run lifecycle.

## Decision

Keep the product local-first for the current deployment model. Do not add more general-purpose scheduler, retry, timer, or distributed lease features to the custom control plane.

Use one local SQLite control-plane database, `agent-state.sqlite`, with these ownership rules:

| State | Authority |
| --- | --- |
| execution lifecycle, lease, terminal result | `durable_executions` |
| idempotent tool/node attempts | `durable_steps` |
| role/subtask status and role events | `agent_runs`, `agent_subtasks`, `agent_events` |
| graph-machine resume state | LangGraph `checkpoints`, `writes` |
| sessions, messages, approvals, rendered run summaries | Electron tables as a rebuildable UI projection |

`execution_id` is the canonical identity. New broker runs use it as `agent_runs.id`; the UI run ID and base LangGraph `thread_id` use the same value. Approval branches use a derived LangGraph thread ID but retain the same execution ID.

The previous `agent-broker.sqlite`, `durable-executions.sqlite`, and `langgraph-checkpoints.sqlite` files are imported once into `agent-state.sqlite`. Long-term memory, evaluation data, logs, reports, and execution artifacts remain separate because they are not execution lifecycle state.

Split admission into two lanes:

- `write`: conservatively classified tasks; one process-wide write lock protects source-workspace mutation and final worktree merge.
- `read_only`: deterministically classified tasks; no write lock and no git worktree creation.

Autonomy scans use a separate scan lock and may run while a write task works in its isolated worktree. Ambiguous tasks fail closed into the write lane.

## Temporal Migration Triggers

Revisit this ADR and prefer Temporal over extending the custom control plane when any of these becomes true:

1. Workers must run on more than one host or survive the desktop process being offline.
2. Product SLOs require remote durable completion across machine or service failure.
3. Workflow signals, schedules, versioned worker deployments, or multi-service coordination become product requirements.
4. More than one write-capable worker must execute concurrently against independently owned workspaces.
5. SQLite writer contention or custom recovery code becomes a measured operational bottleneck.

At that point, map `execution_id` to Temporal Workflow ID, role/tool work to Activities, approvals to Signals/Updates, and keep the Electron tables as projections. Temporal's official Python integration for LangGraph is the preferred spike path.

## Consequences

- Local installation stays self-contained.
- Recovery and debugging start from one execution ID and one control-plane database.
- Read-only throughput can increase without weakening write safety.
- SQLite still permits only one writer at a time; transactions must remain short.
- The write lock remains coarse. Per-workspace write locks are a future optimization, not part of this decision.
- The team must resist adding Temporal-like platform features locally once a migration trigger is reached.

## References

- [Temporal production deployments](https://docs.temporal.io/production-deployment)
- [Temporal local development server](https://docs.temporal.io/develop/run-a-development-server)
- [Temporal LangGraph integration](https://docs.temporal.io/develop/python/integrations/langgraph)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [SQLite write-ahead logging](https://sqlite.org/wal.html)
- [SQLite isolation](https://sqlite.org/isolation.html)
