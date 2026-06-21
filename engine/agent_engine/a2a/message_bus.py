"""A2A Message Bus — in-process pub/sub for typed A2A Messages.

Replaces the current pattern of agents reading raw PipelineState dict keys
with structured message passing. Each agent subscribes to its input contract
topics; publishes output messages to the bus instead of mutating shared state.

Design:
  - Single bus per backend process (singleton via module-level _BUS).
  - Topics are dot-delimited strings: "agent.<role>.input", "task.<id>.output".
  - `publish(msg)` → fans out to all matching subscriptions.
  - `request(agent_id, msg)` → synchronous request/response with timeout.
  - All messages are typed A2A Message objects (from .types).
  - Optional: signature verification via agent auth module (Phase 8).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .types import Message, TaskState
from ..debug_log import write_debug_event


@dataclass
class Subscription:
    id: str
    topic_filter: str  # prefix match, may contain '*' wildcard
    callback: Callable[[Message], Any]
    agent_id: str | None = None

    def matches(self, topic: str) -> bool:
        filt = self.topic_filter
        if filt == topic:
            return True
        if filt.endswith(".*"):
            return topic.startswith(filt[:-1])
        return False


@dataclass
class BusMessage:
    message: Message
    topic: str
    sender_agent_id: str | None
    timestamp: float
    correlation_id: str
    message_id: str


@dataclass
class RequestTicket:
    message: Message
    result_event: threading.Event
    response: Message | None = None
    error: str | None = None
    timeout: float = 30.0


class A2AMessageBus:
    """In-process message bus with topic-based pub/sub and request/response."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[Subscription]] = defaultdict(list)
        self._pending_requests: dict[str, RequestTicket] = {}
        self._history: list[BusMessage] = []
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._message_count: int = 0

    # ── Pub/Sub ──────────────────────────────────────────────────────────

    def publish(
        self,
        message: Message,
        *,
        topic: str = "",
        sender_agent_id: str | None = None,
        correlation_id: str | None = None,
    ) -> list[BusMessage]:
        """Publish a message to all matching subscribers. Returns delivered messages."""
        cid = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        bm = BusMessage(
            message=message,
            topic=topic,
            sender_agent_id=sender_agent_id,
            timestamp=time.time(),
            correlation_id=cid,
            message_id=message.messageId,
        )
        delivered: list[BusMessage] = []
        with self._lock:
            self._message_count += 1
            subs = self._matching(topic)
            for sub in subs:
                try:
                    sub.callback(message)
                    delivered.append(bm)
                except Exception as exc:
                    write_debug_event("a2a.bus.delivery_error", {
                        "topic": topic, "sub_id": sub.id, "error": str(exc),
                    })
            if len(self._history) >= 2000:
                self._history = self._history[-1000:]
            self._history.append(bm)
        return delivered

    def subscribe(
        self,
        topic_filter: str,
        callback: Callable[[Message], Any],
        *,
        agent_id: str | None = None,
    ) -> str:
        """Register a subscription. Returns subscription ID for unsubscribe."""
        sub_id = f"sub-{uuid.uuid4().hex[:10]}"
        sub = Subscription(id=sub_id, topic_filter=topic_filter, callback=callback, agent_id=agent_id)
        with self._lock:
            self._subscriptions[topic_filter].append(sub)
        write_debug_event("a2a.bus.subscribe", {"id": sub_id, "topic": topic_filter, "agent": agent_id})
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            for topic_filter, subs in list(self._subscriptions.items()):
                for sub in list(subs):
                    if sub.id == sub_id:
                        subs.remove(sub)
                        if not subs:
                            del self._subscriptions[topic_filter]
                        return True
        return False

    # ── Request/Response ─────────────────────────────────────────────────

    def request(
        self,
        agent_id: str,
        message: Message,
        *,
        timeout: float = 30.0,
        correlation_id: str | None = None,
    ) -> tuple[Message | None, str | None]:
        """Synchronous request to a specific agent. Returns (response, error)."""
        cid = correlation_id or f"corr-{uuid.uuid4().hex[:12]}"
        topic = f"agent.{agent_id}.input"
        ticket = RequestTicket(message=message, result_event=threading.Event(), timeout=timeout)
        with self._lock:
            self._pending_requests[cid] = ticket

        self.publish(message, topic=topic, sender_agent_id="orchestrator", correlation_id=cid)

        if not ticket.result_event.wait(timeout=timeout):
            with self._lock:
                self._pending_requests.pop(cid, None)
            return None, f"request timeout after {timeout}s for agent {agent_id}"

        with self._lock:
            self._pending_requests.pop(cid, None)
        return ticket.response, ticket.error

    def respond(self, correlation_id: str, response: Message, *, error: str | None = None) -> bool:
        """Deliver a response to a pending request."""
        with self._lock:
            ticket = self._pending_requests.get(correlation_id)
            if not ticket:
                return False
            ticket.response = response
            ticket.error = error
            ticket.result_event.set()
        return True

    # ── Helpers ──────────────────────────────────────────────────────────

    def _matching(self, topic: str) -> list[Subscription]:
        matches: list[Subscription] = []
        for subs in self._subscriptions.values():
            for sub in subs:
                if sub.matches(topic):
                    matches.append(sub)
        return matches

    def history(self, limit: int = 100, topic: str | None = None) -> list[BusMessage]:
        with self._lock:
            if topic:
                return [bm for bm in self._history if bm.topic == topic][-limit:]
            return self._history[-limit:]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "subscriptionCount": sum(len(subs) for subs in self._subscriptions.values()),
                "topicCount": len(self._subscriptions),
                "messageCount": self._message_count,
                "pendingRequests": len(self._pending_requests),
                "uptimeSeconds": round(time.time() - self._started_at, 1),
                "historySize": len(self._history),
            }

    def clear(self) -> None:
        with self._lock:
            self._subscriptions.clear()
            self._pending_requests.clear()
            self._history.clear()


# ── Module-level singleton ────────────────────────────────────────────────────

_BUS: A2AMessageBus | None = None
_BUS_LOCK = threading.Lock()


def get_message_bus() -> A2AMessageBus:
    global _BUS
    if _BUS is None:
        with _BUS_LOCK:
            if _BUS is None:
                _BUS = A2AMessageBus()
    return _BUS


def reset_message_bus() -> None:
    global _BUS
    with _BUS_LOCK:
        if _BUS is not None:
            _BUS.clear()
        _BUS = None
