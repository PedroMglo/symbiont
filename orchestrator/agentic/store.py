"""SQLite-backed task ledger for the agentic runtime."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from config.storage_paths import symbiont_data_path
from orchestrator.agentic.models import AgenticTask, ApprovalStatus, TaskStatus

_SQL_DIR = Path(__file__).resolve().parent / "sql"
_SQL_CACHE = {}


def _sql(name: str) -> str:
    text = _SQL_CACHE.get(name)
    if text is None:
        text = (_SQL_DIR / name).read_text(encoding="utf-8").strip()
        _SQL_CACHE[name] = text
    return text


log = logging.getLogger(__name__)

_SCHEMA = _sql("schema.sql")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


_REDACTED_VALUE = "[REDACTED]"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "passwd",
    "secret",
    "token",
}
_SENSITIVE_KEY_PARTS = (
    "access_token",
    "auth_token",
    "bearer_token",
    "client_secret",
    "private_key",
    "refresh_token",
    "secret_key",
    "x_api_key",
)
_AI_EVENT_STRING_LIMIT = 2000


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_ai_local_payload(value: Any, *, sensitive: bool = False) -> Any:
    if sensitive:
        return _REDACTED_VALUE
    if isinstance(value, dict):
        return {
            str(key): _redact_ai_local_payload(item, sensitive=_is_sensitive_key(str(key)))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_ai_local_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith(("bearer ", "basic ")):
            return _REDACTED_VALUE
        if len(value) > _AI_EVENT_STRING_LIMIT:
            return f"{value[:_AI_EVENT_STRING_LIMIT]}...[truncated]"
    return value


def _preview(value: Any, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = _json_dumps(value)
    text = " ".join(text.split())
    return text[:limit]


_COMMAND_OUTPUT_STATUSES = {"completed", "failed", "timeout"}


def _text_size_bytes(value: str | None) -> int:
    return len((value or "").encode("utf-8"))


def _text_sha256(value: str | None) -> str:
    import hashlib

    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _command_output_metadata(
    *,
    run_id: str,
    status: str,
    stdout: str | None,
    stderr: str | None,
    output_truncated: bool,
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    result = dict(metadata or {})
    has_output = stdout is not None or stderr is not None or status in _COMMAND_OUTPUT_STATUSES
    if not has_output:
        return result

    stdout_ref = result.get("stdout_ref") or result.get("workspace_execution_stdout_ref")
    stderr_ref = result.get("stderr_ref") or result.get("workspace_execution_stderr_ref")
    result["stdout_ref"] = str(stdout_ref or f"agentic-command-run:{run_id}:stdout")
    result["stderr_ref"] = str(stderr_ref or f"agentic-command-run:{run_id}:stderr")

    diff_ref = result.get("diff_ref") or result.get("workspace_execution_diff_ref")
    if diff_ref:
        result["diff_ref"] = str(diff_ref)
    artifacts = result.get("artifacts") or result.get("workspace_execution_artifacts")
    if artifacts is not None:
        result["artifacts"] = artifacts

    result.setdefault("stdout_sha256", _text_sha256(stdout))
    result.setdefault("stderr_sha256", _text_sha256(stderr))
    result.setdefault("stdout_size_bytes", _text_size_bytes(stdout))
    result.setdefault("stderr_size_bytes", _text_size_bytes(stderr))
    result.setdefault("output_truncated", bool(output_truncated))
    result.setdefault("redaction_status", "redacted")
    result.setdefault("output_preview_policy", "ledger_preview_with_refs")
    result.setdefault("raw_output_payload_persisted", False)
    return result


def _memory_tokens(value: Any) -> set[str]:
    text = value if isinstance(value, str) else _json_dumps(value)
    normalized = "".join(char.lower() if char.isalnum() else " " for char in text)
    return {part for part in normalized.split() if len(part) >= 3}


def _memory_metadata_matches(memory_metadata: dict[str, Any], metadata_filter: dict[str, Any]) -> bool:
    for key, expected in metadata_filter.items():
        actual = memory_metadata.get(key)
        if isinstance(expected, list):
            actual_values = actual if isinstance(actual, list) else [actual]
            if not set(map(str, expected)).intersection(set(map(str, actual_values))):
                return False
            continue
        if str(actual) != str(expected):
            return False
    return True


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _payload_hash(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_strings(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else (value,)
        for item in items:
            text = _string_or_none(item)
            if text and text not in seen:
                result.append(text)
                seen.add(text)
    return result


def _first_string(*values: Any) -> str | None:
    refs = _unique_strings(*values)
    return refs[0] if refs else None


def _metadata_from_action(value: Any) -> dict[str, Any]:
    action = _dict_value(value)
    return _dict_value(action.get("metadata"))


def _metadata_from_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_metadata_from_action(item) for item in value if isinstance(item, dict)]


def _event_payload_with_context(
    payload: dict[str, Any] | None,
    *,
    event_id: str,
    event_type: str,
    task_id: str | None,
    trace_id: str | None,
    task_metadata: dict[str, Any],
) -> dict[str, Any]:
    event_payload = dict(payload or {})
    existing_context = _dict_value(event_payload.get("event_context"))
    metadata = _dict_value(event_payload.get("metadata"))
    decision = _dict_value(event_payload.get("decision"))
    decision_metadata = _dict_value(decision.get("metadata"))
    action = _dict_value(event_payload.get("action"))
    action_metadata = _metadata_from_action(action)
    action_context = _dict_value(event_payload.get("action_context"))
    action_result = _dict_value(event_payload.get("action_result"))
    capability_metadata = _dict_value(event_payload.get("capability_metadata"))
    lease = _dict_value(event_payload.get("lease"))
    lease_decision = _dict_value(event_payload.get("lease_decision"))
    task_lease_decision = _dict_value(task_metadata.get("lease_decision"))
    action_metadatas = _metadata_from_actions(decision.get("proposed_actions"))

    capability_ids = _unique_strings(
        existing_context.get("capability_id"),
        existing_context.get("capability_ids"),
        event_payload.get("capability_id"),
        capability_metadata.get("capability_id"),
        action_metadata.get("capability_id"),
        action_context.get("capability_id"),
        *(item.get("capability_id") for item in action_metadatas),
    )
    policy_actions = _unique_strings(
        existing_context.get("policy_action"),
        existing_context.get("policy_actions"),
        event_payload.get("policy_action"),
        capability_metadata.get("policy_action"),
        action_metadata.get("policy_action"),
        action_context.get("policy_action"),
        *(item.get("policy_action") for item in action_metadatas),
    )
    evidence_refs = _unique_strings(
        existing_context.get("evidence_refs"),
        event_payload.get("evidence_refs"),
        metadata.get("evidence_refs"),
        decision.get("evidence_refs"),
        decision_metadata.get("evidence_refs"),
        action_metadata.get("evidence_refs"),
        action_context.get("evidence_refs"),
        action_result.get("evidence_refs"),
        *(item.get("evidence_refs") for item in action_metadatas),
    )

    context = dict(existing_context)
    context.update(
        {
            "event_id": event_id,
            "event_type": event_type,
        }
    )
    if task_id:
        context["task_id"] = str(task_id)
    if trace_id:
        context["trace_id"] = str(trace_id)
    request_id = _first_string(
        context.get("request_id"),
        event_payload.get("request_id"),
        metadata.get("request_id"),
        decision_metadata.get("request_id"),
        action_metadata.get("request_id"),
        action_context.get("request_id"),
        task_metadata.get("request_id"),
    )
    if request_id:
        context["request_id"] = request_id
    resource_lease_id = _first_string(
        context.get("resource_lease_id"),
        event_payload.get("resource_lease_id"),
        event_payload.get("lease_id"),
        lease.get("lease_id"),
        lease_decision.get("lease_id"),
        task_lease_decision.get("lease_id"),
    )
    if resource_lease_id:
        context["resource_lease_id"] = resource_lease_id
    resource_lease_decision = _first_string(
        context.get("resource_lease_decision"),
        lease.get("decision"),
        lease_decision.get("decision"),
        task_lease_decision.get("decision"),
    )
    if resource_lease_decision:
        context["resource_lease_decision"] = resource_lease_decision
    action_id = _first_string(
        context.get("action_id"),
        event_payload.get("action_id"),
        action.get("action_id"),
        action_result.get("action_id"),
    )
    if action_id:
        context["action_id"] = action_id
    decision_id = _first_string(context.get("decision_id"), event_payload.get("decision_id"))
    if decision_id:
        context["decision_id"] = decision_id
    state_hash_value = _first_string(context.get("state_hash"), event_payload.get("state_hash"))
    if state_hash_value:
        context["state_hash"] = state_hash_value
    input_state_hash = _first_string(
        context.get("input_state_hash"),
        event_payload.get("input_state_hash"),
        decision.get("input_state_hash"),
    )
    if input_state_hash:
        context["input_state_hash"] = input_state_hash
    if capability_ids:
        context["capability_id"] = capability_ids[0]
        if len(capability_ids) > 1:
            context["capability_ids"] = capability_ids
    if policy_actions:
        context["policy_action"] = policy_actions[0]
        if len(policy_actions) > 1:
            context["policy_actions"] = policy_actions
    if evidence_refs:
        context["evidence_refs"] = evidence_refs
    event_payload["event_context"] = context
    return event_payload


def _fingerprint(*parts: Any) -> str:
    import hashlib

    return hashlib.sha256(_json_dumps(parts).encode("utf-8")).hexdigest()


def _normalize_action(action: str) -> str:
    return " ".join((action or "").strip().lower().split())


def _resolve_db_path(raw_path: str) -> Path:
    raw = os.path.expandvars(os.path.expanduser(raw_path))
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except PermissionError:
        if str(path).startswith("/app/"):
            fallback = symbiont_data_path("symbiont", "agentic.db")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            log.warning("AgenticStore: cannot write %s; using %s", path, fallback)
            return fallback
        raise


class AgenticStore:
    """Persistent operational ledger for agentic tasks."""

    def __init__(self, db_path: str) -> None:
        self.path = _resolve_db_path(db_path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        self._db.execute(_sql("execute_554.sql"))
        self._db.execute(_sql("execute_555.sql"))
        self._db.executescript(_SCHEMA)
        self._db.commit()
        log.info("AgenticStore opened at %s", self.path)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_task(
        self,
        *,
        goal: str,
        mode: str,
        source: str,
        session_id: str | None = None,
        user_id_hash: str | None = None,
        trace_id: str | None = None,
        priority: str = "normal",
        budget: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = TaskStatus.RUNNING.value,
    ) -> AgenticTask:
        now = time.time()
        task_id = _new_id("task")
        trace = trace_id or _new_id("trace")
        with self._lock:
            self._db.execute(
                _sql("execute_583.sql"),
                (
                    task_id,
                    goal,
                    mode,
                    status,
                    priority,
                    session_id,
                    user_id_hash,
                    trace,
                    source,
                    now,
                    now,
                    _json_dumps(budget or {}),
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="task.created",
            actor="symbiont",
            payload={"goal_preview": _preview(goal, 500), "mode": mode, "source": source},
            trace_id=trace,
        )
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> AgenticTask | None:
        row = self._one(_sql("one_617.sql"), (task_id,))
        return self._task_from_row(row) if row else None

    def count_tasks(self, *, status: str | None = None) -> int:
        if status:
            row = self._one(_sql("one_622.sql"), (status,))
        else:
            row = self._one(_sql("one_624.sql"), ())
        return int(row["c"]) if row else 0

    def task_status_counts(self) -> dict[str, int]:
        rows = self._all(_sql("all_628.sql"))
        return {str(row["status"]): int(row["c"]) for row in rows}

    def active_task_ids(self) -> list[str]:
        rows = self._all(
            _sql("all_633.sql"),
            (TaskStatus.PLANNING.value, TaskStatus.RUNNING.value),
        )
        return [str(row["id"]) for row in rows]

    def list_tasks(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        if status:
            rows = self._all(
                _sql("all_649.sql"),
                (status, limit, offset),
            )
        else:
            rows = self._all(_sql("all_653.sql"), (limit, offset))
        return [self._task_from_row(row).to_dict() for row in rows]

    def claim_next_task(self, *, worker_id: str, include_proposals: bool = False) -> AgenticTask | None:
        """Atomically claim the next queued/recovering task for the runner."""

        now = time.time()
        with self._lock:
            rows = self._db.execute(
                _sql("execute_662.sql"),
                (TaskStatus.QUEUED.value, TaskStatus.RECOVERING.value),
            ).fetchall()
            tasks = [self._task_from_row(row) for row in rows]
            tasks.sort(key=self._claim_sort_key)
            claimed: AgenticTask | None = None
            previous_status = ""
            for task in tasks:
                if not include_proposals and self._is_proposal_only(task):
                    continue
                defer_until = float(task.metadata.get("defer_until") or 0)
                if defer_until > now:
                    continue
                metadata = dict(task.metadata)
                metadata.update({
                    "runner_worker_id": worker_id,
                    "claimed_at": now,
                    "previous_status": task.status,
                })
                cur = self._db.execute(
                    _sql("execute_694.sql"),
                    (
                        TaskStatus.PLANNING.value,
                        now,
                        _json_dumps(metadata),
                        task.id,
                        task.status,
                    ),
                )
                if cur.rowcount:
                    self._db.commit()
                    claimed = self.get_task(task.id)
                    previous_status = task.status
                    break
            if claimed is None:
                return None

        self.record_event(
            task_id=claimed.id,
            event_type="task.claimed",
            actor="agentic.runner",
            payload={"worker_id": worker_id, "previous_status": previous_status},
            trace_id=claimed.trace_id,
        )
        return claimed

    @staticmethod
    def _task_lane(task: AgenticTask) -> str:
        metadata = task.metadata or {}
        lane = str(metadata.get("lane") or "").strip().lower()
        if lane:
            return lane
        if task.source == "agentic.event_loop" or metadata.get("proposal") or metadata.get("proposal_only"):
            return "maintenance"
        if metadata.get("background") or metadata.get("heavy"):
            return "background"
        return "user"

    @classmethod
    def _is_proposal_only(cls, task: AgenticTask) -> bool:
        metadata = task.metadata or {}
        return bool(metadata.get("proposal_only") or (task.source == "agentic.event_loop" and metadata.get("proposal")))

    @classmethod
    def _claim_sort_key(cls, task: AgenticTask) -> tuple[int, int, float]:
        lane_rank = {
            "interactive": 0,
            "user": 1,
            "default": 1,
            "background": 2,
            "maintenance": 3,
        }.get(cls._task_lane(task), 2)
        priority_rank = {
            "high": 0,
            "normal": 1,
            "low": 2,
        }.get(str(task.priority).lower(), 3)
        return (lane_rank, priority_rank, float(task.created_at))

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        merged_metadata = dict(task.metadata)
        if metadata:
            merged_metadata.update(metadata)
        next_status = status or task.status
        with self._lock:
            self._db.execute(
                _sql("execute_775.sql"),
                (
                    next_status,
                    time.time(),
                    _json_dumps(result) if result is not None else _json_dumps(task.result) if task.result else None,
                    _json_dumps(error) if error is not None else _json_dumps(task.error) if task.error else None,
                    _json_dumps(merged_metadata),
                    task_id,
                ),
            )
            self._db.commit()
        if status and status != task.status:
            self.record_event(
                task_id=task_id,
                event_type=f"task.{status}",
                actor="symbiont",
                payload={"previous_status": task.status, "status": status},
                trace_id=task.trace_id,
            )

    def start_run(
        self,
        *,
        task_id: str,
        trace_id: str,
        graph_run_id: str | None,
        entrypoint: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = _new_id("run")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_812.sql"),
                (run_id, task_id, trace_id, graph_run_id, entrypoint, "running", now, _json_dumps(metadata or {})),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="run.started",
            actor="symbiont",
            payload={"run_id": run_id, "graph_run_id": graph_run_id, "entrypoint": entrypoint},
            trace_id=trace_id,
        )
        return run_id

    def finish_run(self, run_id: str, *, status: str, metadata: dict[str, Any] | None = None) -> None:
        row = self._one(_sql("one_831.sql"), (run_id,))
        if not row:
            return
        current = _json_loads(row["metadata_json"]) or {}
        if metadata:
            current.update(metadata)
        with self._lock:
            self._db.execute(
                _sql("execute_839.sql"),
                (status, time.time(), _json_dumps(current), run_id),
            )
            self._db.commit()
        self.record_event(
            task_id=row["task_id"],
            event_type=f"run.{status}",
            actor="symbiont",
            payload={"run_id": run_id, "graph_run_id": row["graph_run_id"]},
            trace_id=row["trace_id"],
        )

    def record_step(
        self,
        *,
        task_id: str,
        step_name: str,
        step_type: str,
        status: str,
        run_id: str | None = None,
        started_at: float | None = None,
        duration_ms: float | None = None,
        input_preview: str | None = None,
        output_preview: str | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        step_id = _new_id("step")
        started = started_at or time.time()
        finished = started + duration_ms / 1000.0 if duration_ms is not None else None
        with self._lock:
            self._db.execute(
                _sql("execute_871.sql"),
                (
                    step_id,
                    task_id,
                    run_id,
                    step_name,
                    step_type,
                    status,
                    started,
                    finished,
                    duration_ms,
                    input_preview,
                    output_preview,
                    _json_dumps(error) if error else None,
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        return step_id

    def record_event(
        self,
        *,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        event_id = _new_id("event")
        task_metadata: dict[str, Any] = {}
        if task_id:
            task = self.get_task(str(task_id))
            if task is not None:
                task_metadata = dict(task.metadata or {})
        event_payload = _event_payload_with_context(
            payload,
            event_id=event_id,
            event_type=event_type,
            task_id=task_id,
            trace_id=trace_id,
            task_metadata=task_metadata,
        )
        with self._lock:
            self._db.execute(
                _sql("execute_908.sql"),
                (event_id, task_id, event_type, time.time(), actor, _json_dumps(event_payload), trace_id),
            )
            self._db.commit()
        return event_id

    def initialize_agent_state(self, task_id: str) -> dict[str, Any] | None:
        """Create the first deterministic AgentState snapshot for a task."""

        if self.latest_agent_state_snapshot(task_id) is not None:
            return self.latest_agent_state_snapshot(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        from orchestrator.agentic.reducer import initial_state_from_task, state_hash

        state = initial_state_from_task(task)
        digest = state_hash(state)
        event_id = self.record_event(
            task_id=task.id,
            trace_id=task.trace_id,
            event_type="agent.state.initialized",
            actor="agentic.reducer",
            payload={"state": state.model_dump(mode="json"), "state_hash": digest},
        )
        return self.record_agent_state_snapshot(
            state,
            previous_state_hash=None,
            source_event_id=event_id,
        )

    def record_agent_state_snapshot(
        self,
        state: Any,
        *,
        previous_state_hash: str | None = None,
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        from orchestrator.agentic.contracts import AgentState
        from orchestrator.agentic.reducer import state_hash

        validated = AgentState.model_validate(state)
        digest = state_hash(validated)
        snapshot_id = _new_id("state")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_958.sql"),
                (
                    snapshot_id,
                    validated.task_id,
                    validated.trace_id,
                    digest,
                    previous_state_hash,
                    _json_dumps(validated.model_dump(mode="json")),
                    source_event_id,
                    now,
                ),
            )
            self._db.commit()
        row = self._one(
            _sql("one_977.sql"),
            (validated.task_id, digest),
        )
        return self._row_to_dict(row) if row else {}

    def latest_agent_state_snapshot(self, task_id: str) -> dict[str, Any] | None:
        row = self._one(
            _sql("one_984.sql"),
            (task_id,),
        )
        return self._row_to_dict(row) if row else None

    def list_agent_state_snapshots(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._all(
            _sql("all_992.sql"),
            (task_id, limit),
        )
        return [self._row_to_dict(row) for row in rows]

    def current_agent_state(self, task_id: str) -> dict[str, Any] | None:
        snapshot = self.latest_agent_state_snapshot(task_id)
        if snapshot is not None:
            return snapshot
        return self.initialize_agent_state(task_id)

    def replay_agent_state(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        from orchestrator.agentic.reducer import initial_state_from_task, replay_events, state_hash

        events = [
            self._row_to_dict(row)
            for row in self._all(_sql("all_1011.sql"), (task_id,))
        ]
        state = replay_events(initial_state_from_task(task), events)
        return {
            "task_id": task_id,
            "trace_id": task.trace_id,
            "state_hash": state_hash(state),
            "state": state.model_dump(mode="json"),
            "events_replayed": len(events),
        }

    def record_agent_raw_output(
        self,
        *,
        agent: str,
        output: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        artifact_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import hashlib

        from orchestrator.agentic.contracts import RawOutputRef
        from orchestrator.observability.redaction import get_redactor
        from orchestrator.security.secrets import SecretsScanner

        raw = output or ""
        redacted_text = get_redactor().redact_string(SecretsScanner().redact(raw))
        digest = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
        ref_id = _new_id("raw")
        now = time.time()
        preview = _preview(redacted_text, 4000)
        with self._lock:
            self._db.execute(
                _sql("execute_1046.sql"),
                (
                    ref_id,
                    task_id,
                    trace_id,
                    agent,
                    digest,
                    preview,
                    1,
                    artifact_ref,
                    len(redacted_text.encode("utf-8")),
                    now,
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        ref = RawOutputRef(
            ref_id=ref_id,
            sha256=digest,
            preview=preview,
            redacted=True,
            artifact_ref=artifact_ref,
            size_bytes=len(redacted_text.encode("utf-8")),
        )
        self.record_event(
            task_id=task_id,
            trace_id=trace_id,
            event_type="agent.raw_output.recorded",
            actor="agentic.store",
            payload={"agent": agent, "raw_output_ref": ref.model_dump(mode="json")},
        )
        row = self.get_agent_raw_output(ref_id) or {}
        return {**row, "ref": ref.model_dump(mode="json")}

    def get_agent_raw_output(self, raw_output_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1086.sql"), (raw_output_id,))
        return self._row_to_dict(row) if row else None

    def list_agent_raw_outputs(self, *, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._all(
            _sql("all_1092.sql"),
            (task_id, limit),
        )
        return [self._row_to_dict(row) for row in rows]

    def record_agent_decision(self, decision: Any, *, actor: str = "agentic.runner") -> dict[str, Any]:
        from orchestrator.agentic.contracts import AgentDecision
        from orchestrator.agentic.reducer import reduce_state

        validated = AgentDecision.model_validate(decision)
        snapshot = self.current_agent_state(validated.task_id)
        if snapshot is None:
            raise KeyError("task_not_found")
        current_state = snapshot["state"]
        current_hash = str(snapshot["state_hash"])
        if validated.input_state_hash != current_hash:
            error = {
                "type": "StaleStateHash",
                "message": "AgentDecision input_state_hash does not match current AgentState",
                "expected": current_hash,
                "actual": validated.input_state_hash,
            }
            row = self._insert_agent_decision(validated, valid=False, error=error)
            self.record_event(
                task_id=validated.task_id,
                trace_id=validated.trace_id,
                event_type="agent.decision.rejected",
                actor=actor,
                payload={"decision": validated.model_dump(mode="json"), "error": error},
            )
            return {"valid": False, "error": error, "decision": row, "state_snapshot": snapshot}

        decision_payload = validated.model_dump(mode="json")
        event_id = self.record_event(
            task_id=validated.task_id,
            trace_id=validated.trace_id,
            event_type="agent.decision.recorded",
            actor=actor,
            payload={"decision": decision_payload, "input_state_hash": current_hash},
        )
        row = self._insert_agent_decision(validated, valid=True, error=None)
        next_state = reduce_state(current_state, {"event_type": "agent.decision.recorded", "payload": {"decision": decision_payload}})
        next_snapshot = self.record_agent_state_snapshot(
            next_state,
            previous_state_hash=current_hash,
            source_event_id=event_id,
        )
        for action in validated.proposed_actions:
            self.record_event(
                task_id=validated.task_id,
                trace_id=validated.trace_id,
                event_type="agent.action.proposed",
                actor=actor,
                payload={
                    "action": action.model_dump(mode="json"),
                    "decision_id": row.get("id"),
                    "state_hash": next_snapshot.get("state_hash"),
                },
            )
        return {"valid": True, "decision": row, "state_snapshot": next_snapshot}

    def record_agent_decision_rejected(
        self,
        *,
        task_id: str,
        trace_id: str,
        input_state_hash: str,
        raw_decision: Any,
        error: dict[str, Any],
        actor: str = "agentic.runner",
    ) -> dict[str, Any]:
        decision_id = _new_id("decision")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1167.sql"),
                (
                    decision_id,
                    task_id,
                    trace_id,
                    input_state_hash,
                    "invalid",
                    0.0,
                    _json_dumps(raw_decision),
                    _json_dumps(error),
                    now,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            trace_id=trace_id,
            event_type="agent.decision.rejected",
            actor=actor,
            payload={"raw_decision_preview": _preview(raw_decision), "error": error, "input_state_hash": input_state_hash},
        )
        return self.get_agent_decision(decision_id) or {}

    def record_agent_action_result(self, action_result: Any, *, task_id: str, trace_id: str, actor: str = "agentic.runner") -> dict[str, Any]:
        from orchestrator.agentic.contracts import ActionResult
        from orchestrator.agentic.reducer import reduce_state

        validated = ActionResult.model_validate(action_result)
        snapshot = self.current_agent_state(task_id)
        if snapshot is None:
            raise KeyError("task_not_found")
        current_hash = str(snapshot["state_hash"])
        action_context: dict[str, Any] = {}
        state = snapshot.get("state") if isinstance(snapshot, dict) else {}
        pending_actions = state.get("pending_actions") if isinstance(state, dict) else []
        if isinstance(pending_actions, list):
            for action in pending_actions:
                if isinstance(action, dict) and action.get("action_id") == validated.action_id:
                    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
                    action_context = {
                        "action_id": validated.action_id,
                        "capability_id": metadata.get("capability_id"),
                        "policy_action": metadata.get("policy_action"),
                        "evidence_refs": metadata.get("evidence_refs") or [],
                    }
                    break
        payload = {"action_result": validated.model_dump(mode="json"), "input_state_hash": current_hash}
        if action_context:
            payload["action_context"] = action_context
        event_id = self.record_event(
            task_id=task_id,
            trace_id=trace_id,
            event_type="agent.action.result_recorded",
            actor=actor,
            payload=payload,
        )
        next_state = reduce_state(snapshot["state"], {"event_type": "agent.action.result_recorded", "payload": payload})
        next_snapshot = self.record_agent_state_snapshot(
            next_state,
            previous_state_hash=current_hash,
            source_event_id=event_id,
        )
        return {"action_result": validated.model_dump(mode="json"), "state_snapshot": next_snapshot}

    def _insert_agent_decision(self, decision: Any, *, valid: bool, error: dict[str, Any] | None) -> dict[str, Any]:
        raw_ref = decision.raw_output_ref.model_dump(mode="json") if decision.raw_output_ref is not None else None
        decision_id = _new_id("decision")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1227.sql"),
                (
                    decision_id,
                    decision.task_id,
                    decision.trace_id,
                    decision.input_state_hash,
                    decision.status,
                    decision.confidence,
                    _json_dumps(decision.model_dump(mode="json")),
                    _json_dumps(raw_ref) if raw_ref is not None else None,
                    1 if valid else 0,
                    _json_dumps(error) if error else None,
                    now,
                ),
            )
            self._db.commit()
        return self.get_agent_decision(decision_id) or {}

    def get_agent_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1252.sql"), (decision_id,))
        return self._row_to_dict(row) if row else None

    def list_agent_decisions(self, *, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._all(
            _sql("all_1258.sql"),
            (task_id, limit),
        )
        return [self._row_to_dict(row) for row in rows]

    def record_parallel_round(self, round_data: Any, *, plan: Any = None, actor: str = "agentic.runner") -> dict[str, Any]:
        from orchestrator.agentic.contracts import AgenticParallelPlan, AgenticParallelRound

        validated = AgenticParallelRound.model_validate(round_data)
        plan_payload = (
            AgenticParallelPlan.model_validate(plan).model_dump(mode="json")
            if plan is not None
            else {
                "schema_version": validated.schema_version,
                "plan_id": validated.plan_id,
                "task_id": validated.task_id,
                "trace_id": validated.trace_id,
                "participants": [participant.model_dump(mode="json") for participant in validated.participants],
            }
        )
        round_payload = validated.model_dump(mode="json")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1282.sql"),
                (
                    validated.round_id,
                    validated.task_id,
                    validated.trace_id,
                    validated.plan_id,
                    validated.status,
                    _json_dumps(plan_payload),
                    _json_dumps(round_payload),
                    now,
                    now,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=validated.task_id,
            trace_id=validated.trace_id,
            event_type="agent.parallel_round.recorded",
            actor=actor,
            payload={
                "round_id": validated.round_id,
                "plan_id": validated.plan_id,
                "status": validated.status,
                "degraded": validated.degraded,
                "participants": [participant.agent_name for participant in validated.participants],
            },
        )
        return self.get_parallel_round(validated.round_id) or {}

    def get_parallel_round(self, round_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1322.sql"), (round_id,))
        return self._row_to_dict(row) if row else None

    def list_parallel_rounds(self, *, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._all(
            _sql("all_1328.sql"),
            (task_id, limit),
        )
        return [self._row_to_dict(row) for row in rows]

    def list_recent_parallel_rounds(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        rows = self._all(
            _sql("all_1336.sql"),
            (limit,),
        )
        return [self._row_to_dict(row) for row in rows]

    def record_agent_message(self, message: Any, *, actor: str = "agentic.blackboard") -> dict[str, Any]:
        validated = self._coerce_agent_message(message)
        payload = validated.model_dump(mode="json")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1347.sql"),
                (
                    validated.message_id,
                    validated.task_id,
                    validated.trace_id,
                    validated.round_id,
                    validated.kind,
                    validated.sender,
                    validated.recipient,
                    _json_dumps(payload),
                    now,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=validated.task_id,
            trace_id=validated.trace_id,
            event_type="agent.message.recorded",
            actor=actor,
            payload={
                "message_id": validated.message_id,
                "kind": validated.kind,
                "sender": validated.sender,
                "recipient": validated.recipient,
                "round_id": validated.round_id,
            },
        )
        return self.get_agent_message(validated.message_id) or {}

    def get_agent_message(self, message_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1384.sql"), (message_id,))
        return self._row_to_dict(row) if row else None

    def list_agent_messages(self, *, task_id: str, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        if kind:
            rows = self._all(
                _sql("all_1391.sql"),
                (task_id, kind, limit),
            )
        else:
            rows = self._all(
                _sql("all_1396.sql"),
                (task_id, limit),
            )
        return [self._row_to_dict(row) for row in rows]

    def list_recent_agent_messages(self, *, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        if kind:
            rows = self._all(
                _sql("all_1405.sql"),
                (kind, limit),
            )
        else:
            rows = self._all(
                _sql("all_1410.sql"),
                (limit,),
            )
        return [self._row_to_dict(row) for row in rows]

    def record_ai_local_event(self, event: Any, *, actor: str = "agentic.event_bus") -> dict[str, Any]:
        from orchestrator.agentic.contracts import AiLocalEvent

        validated = AiLocalEvent.model_validate(event)
        validated = validated.model_copy(
            update={
                "payload": _redact_ai_local_payload(validated.payload),
                "metadata": _redact_ai_local_payload(validated.metadata),
            }
        )
        payload = validated.model_dump(mode="json")
        with self._lock:
            self._db.execute(
                _sql("execute_1428.sql"),
                (
                    validated.event_id,
                    validated.task_id,
                    validated.trace_id,
                    validated.producer,
                    validated.type,
                    validated.severity,
                    _json_dumps(payload),
                    validated.created_at,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=validated.task_id,
            trace_id=validated.trace_id,
            event_type=f"ai_local.{validated.type}",
            actor=actor,
            payload={
                "event_id": validated.event_id,
                "producer": validated.producer,
                "severity": validated.severity,
                "payload": validated.payload,
                "evidence_ref": validated.evidence_ref,
            },
        )
        return self.get_ai_local_event(validated.event_id) or {}

    def get_ai_local_event(self, event_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1464.sql"), (event_id,))
        return self._row_to_dict(row) if row else None

    def list_ai_local_events(
        self,
        *,
        task_id: str | None = None,
        event_type: str | None = None,
        producer: str | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if producer:
            clauses.append("producer = ?")
            params.append(producer)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            _sql("fstring_1072.sql").format(where),
            tuple([*params, limit]),
        )
        return [self._row_to_dict(row) for row in rows]

    def count_ai_local_events(self, *, event_type: str, since: float | None = None) -> int:
        if since is None:
            row = self._one(_sql("one_1500.sql"), (event_type,))
        else:
            row = self._one(
                _sql("one_1503.sql"),
                (event_type, since),
            )
        return int(row["c"]) if row else 0

    def record_agent_memory(self, memory: Any, *, actor: str = "agentic.memory") -> dict[str, Any]:
        from orchestrator.agentic.contracts import AgenticMemory
        from orchestrator.observability.redaction import get_redactor
        from orchestrator.security.secrets import SecretsScanner

        validated = AgenticMemory.model_validate(memory)
        metadata = dict(validated.metadata or {})
        sensitivity = validated.sensitivity
        if sensitivity == "normal" and metadata.get("sensitive") is True:
            sensitivity = "sensitive"
        has_sensitive_opt_in = bool(metadata.get("sensitive_memory_opt_in") or metadata.get("approval_id"))
        redacted_content = get_redactor().redact_string(SecretsScanner().redact(validated.content))
        redaction_status = validated.redaction_status
        if sensitivity in {"sensitive", "secret"} and not has_sensitive_opt_in:
            redacted_content = _REDACTED_VALUE
            redaction_status = "redacted_only"
            metadata["sensitive_memory_policy"] = "redacted_only_without_opt_in"
        elif redacted_content != validated.content or redaction_status != "not_required":
            redaction_status = "redacted"

        storage_artifact_ref = validated.storage_artifact_ref or metadata.get("storage_artifact_ref")
        storage_owner = str(metadata.get("storage_owner") or metadata.get("persistence_owner") or "").strip()
        if storage_artifact_ref:
            if storage_owner != "storage_guardian":
                metadata["persistence_status"] = "blocked_non_storage_guardian_owner"
                metadata["blocked_storage_artifact_ref"] = storage_artifact_ref
                storage_artifact_ref = None
            else:
                metadata["persistence_status"] = "storage_guardian_reference"
                metadata["storage_owner"] = "storage_guardian"

        semantic_ref = dict(validated.semantic_ref or {})
        if validated.kind == "semantic_ref":
            semantic_ref.setdefault("owner", "rag/research")
            semantic_ref.setdefault("retrieval_owner", "rag/research")

        metadata.update(
            {
                "owner": validated.owner,
                "sensitivity": sensitivity,
                "redaction_status": redaction_status,
                "durable_storage_required": bool(storage_artifact_ref),
            }
        )
        payload = validated.model_copy(
            update={
                "content": redacted_content,
                "sensitivity": sensitivity,
                "redaction_status": redaction_status,
                "storage_artifact_ref": storage_artifact_ref,
                "semantic_ref": semantic_ref,
                "metadata": metadata,
            }
        ).model_dump(mode="json")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1565.sql"),
                (
                    validated.memory_id,
                    validated.task_id,
                    validated.trace_id,
                    validated.kind,
                    validated.source,
                    _preview(redacted_content, 1000),
                    _json_dumps(payload),
                    now,
                    validated.expires_at,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=validated.task_id,
            trace_id=validated.trace_id,
            event_type="agent.memory.recorded",
            actor=actor,
            payload={
                "memory_id": validated.memory_id,
                "kind": validated.kind,
                "source": validated.source,
                "evidence_refs": validated.evidence_refs,
                "redaction_status": redaction_status,
                "storage_artifact_ref": storage_artifact_ref,
                "semantic_owner": semantic_ref.get("owner") if semantic_ref else None,
            },
        )
        if storage_artifact_ref:
            self.record_event(
                task_id=validated.task_id,
                trace_id=validated.trace_id,
                event_type="agent.memory.persistence_ref_recorded",
                actor=actor,
                payload={
                    "memory_id": validated.memory_id,
                    "storage_owner": "storage_guardian",
                    "storage_artifact_ref": storage_artifact_ref,
                },
            )
        return self.get_agent_memory(validated.memory_id) or {}

    def get_agent_memory(self, memory_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1618.sql"), (memory_id,))
        return self._row_to_dict(row) if row else None

    def list_agent_memory(
        self,
        *,
        task_id: str | None = None,
        kind: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if kind:
            clauses.append("memory_type = ?")
            params.append(kind)
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(time.time())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            _sql("fstring_1213_2.sql").format(where),
            tuple([*params, limit]),
        )
        return [self._row_to_dict(row) for row in rows]

    def retrieve_agent_memory(self, query: Any, *, actor: str = "agentic.memory") -> dict[str, Any]:
        from orchestrator.agentic.contracts import AgenticMemoryQuery, RetrievedAgenticMemory

        if isinstance(query, str):
            query_payload = {"query_id": _new_id("memq"), "query": query}
        else:
            query_payload = query
        memory_query = AgenticMemoryQuery.model_validate(query_payload)
        candidates = self.list_agent_memory(include_expired=True, limit=500)
        now = time.time()
        selected: list[dict[str, Any]] = []
        ignored: list[dict[str, Any]] = []
        expired: list[str] = []
        query_tokens = _memory_tokens(memory_query.query)
        requested_kinds = set(memory_query.kinds)
        requested_sources = set(memory_query.sources)
        evidence_filter = {str(ref) for ref in memory_query.evidence_refs}
        allowed_shared_kinds = {"episodic", "semantic_ref", "procedural_ref", "preference_ref"}

        for row in candidates:
            memory = row.get("memory") if isinstance(row, dict) else None
            if not isinstance(memory, dict):
                continue
            memory_id = str(memory.get("memory_id") or row.get("id") or "")
            memory_kind = str(memory.get("kind") or "")
            memory_task_id = memory.get("task_id")
            memory_source = str(memory.get("source") or "")
            memory_metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
            memory_expired = bool(row.get("expires_at") and float(row["expires_at"]) < now)
            if requested_kinds and memory_kind not in requested_kinds:
                continue
            if requested_sources and memory_source not in requested_sources:
                continue
            if memory_query.task_id and memory_task_id not in {None, memory_query.task_id} and memory_kind not in allowed_shared_kinds:
                continue
            if not _memory_metadata_matches(memory_metadata, memory_query.metadata_filter):
                continue

            score = 0.0
            reasons: list[str] = []
            if memory_query.task_id and memory_task_id == memory_query.task_id:
                score += 1.0
                reasons.append("same_task")
            elif memory_task_id is None and memory_kind in allowed_shared_kinds:
                score += 0.25
                reasons.append("shared_memory")
            if memory_kind == "episodic":
                score += 0.3
                reasons.append("episodic")
            if memory_kind == "semantic_ref":
                score += 0.2
                reasons.append("semantic_ref_owner:rag/research")
            memory_refs = {str(ref) for ref in memory.get("evidence_refs") or []}
            evidence_overlap = sorted(evidence_filter.intersection(memory_refs))
            if evidence_overlap:
                score += min(1.0, 0.35 * len(evidence_overlap))
                reasons.append("evidence_refs")
            target_tokens = _memory_tokens(
                {
                    "content": memory.get("content"),
                    "metadata": memory_metadata,
                    "evidence_refs": list(memory_refs),
                    "semantic_ref": memory.get("semantic_ref") or {},
                }
            )
            token_overlap = sorted(query_tokens.intersection(target_tokens))
            if token_overlap:
                score += min(2.0, len(token_overlap) / max(1, min(len(query_tokens), 8)) * 2.0)
                reasons.append("query_overlap")
            if memory_query.metadata_filter:
                score += 0.4
                reasons.append("metadata_filter")
            if not query_tokens and not evidence_filter and not memory_query.metadata_filter and score > 0:
                reasons.append("recent_shared")
            if memory_expired and not memory_query.include_expired:
                expired.append(memory_id)
                continue
            if score < memory_query.min_score:
                ignored.append({"memory_id": memory_id, "score": round(score, 4), "reasons": reasons})
                continue
            retrieved = RetrievedAgenticMemory.model_validate(
                {
                    "memory": memory,
                    "score": round(score, 4),
                    "reasons": reasons,
                    "expired": memory_expired,
                }
            )
            payload = retrieved.model_dump(mode="json")
            payload["created_at"] = row.get("created_at")
            selected.append(payload)

        selected.sort(key=lambda item: (float(item.get("score") or 0.0), float(item.get("created_at") or 0.0)), reverse=True)
        selected = selected[: memory_query.limit]
        if memory_query.task_id or memory_query.trace_id:
            if expired:
                self.record_event(
                    task_id=memory_query.task_id,
                    trace_id=memory_query.trace_id,
                    event_type="agent.memory.expired",
                    actor=actor,
                    payload={"query_id": memory_query.query_id, "memory_ids": expired[:50]},
                )
            self.record_event(
                task_id=memory_query.task_id,
                trace_id=memory_query.trace_id,
                event_type="agent.memory.retrieved",
                actor=actor,
                payload={
                    "query_id": memory_query.query_id,
                    "selected_memory_ids": [item["memory"]["memory_id"] for item in selected],
                    "ignored_memory_ids": [item["memory_id"] for item in ignored[:50]],
                    "expired_memory_ids": expired[:50],
                    "count": len(selected),
                    "kinds": sorted({item["memory"]["kind"] for item in selected}),
                    "rag_semantic_owner_preserved": all(
                        item["memory"]["kind"] != "semantic_ref"
                        or item["memory"].get("semantic_ref", {}).get("owner") == "rag/research"
                        for item in selected
                    ),
                },
            )
        return {
            "query": memory_query.model_dump(mode="json"),
            "memories": selected,
            "ignored": ignored[: memory_query.limit],
            "expired_memory_ids": expired[:50],
        }

    def record_tool_call(
        self,
        *,
        tool_name: str,
        risk_level: str,
        status: str,
        task_id: str | None = None,
        step_id: str | None = None,
        input_payload: Any = None,
        output_payload: Any = None,
        requires_approval: bool = False,
        approval_id: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        call_id = _new_id("tool")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_1796.sql"),
                (
                    call_id,
                    task_id,
                    step_id,
                    tool_name,
                    risk_level,
                    status,
                    _preview(input_payload),
                    _preview(output_payload),
                    now,
                    now,
                    1 if requires_approval else 0,
                    approval_id,
                    error,
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        return call_id

    def create_approval(
        self,
        *,
        action: str,
        risk_level: str,
        payload: Any,
        ttl_seconds: int,
        task_id: str | None = None,
        dry_run_result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval_id = _new_id("approval")
        now = time.time()
        payload_preview = _preview(payload)
        payload_hash = _payload_hash(payload)
        row = (
            approval_id,
            task_id,
            action,
            risk_level,
            payload_preview,
            payload_hash,
            _preview(dry_run_result),
            ApprovalStatus.PENDING.value,
            now,
            now + ttl_seconds,
            None,
            None,
            None,
            _json_dumps(metadata or {}),
        )
        with self._lock:
            self._db.execute(
                _sql("execute_1856.sql"),
                row,
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="approval.created",
            actor="policy",
            payload={"approval_id": approval_id, "action": action, "risk_level": risk_level},
        )
        return self.get_approval(approval_id) or {}

    def find_approval_for_payload(
        self,
        *,
        action: str,
        payload: Any,
        statuses: tuple[str, ...],
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        payload_hash = _payload_hash(payload)
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [action, payload_hash, *statuses]
        task_clause = ""
        if task_id is not None:
            task_clause = "AND task_id = ?"
            params.append(task_id)
        row = self._one(
            _sql("fstring_1448_3.sql").format(placeholders, task_clause),
            tuple(params),
        )
        return self._row_to_dict(row) if row else None

    def approvals_for_task(self, task_id: str, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._all(
                _sql("all_1903.sql"),
                (task_id, status),
            )
        else:
            rows = self._all(
                _sql("all_1908.sql"),
                (task_id,),
            )
        return [self._row_to_dict(row) for row in rows]

    def expire_pending_approvals(self) -> int:
        now = time.time()
        rows = self._all(
            _sql("all_1916.sql"),
            (ApprovalStatus.PENDING.value, now),
        )
        changed = 0
        for row in rows:
            approval = self._row_to_dict(row)
            self._set_approval_status(str(approval["id"]), ApprovalStatus.EXPIRED.value)
            changed += 1
            self.record_event(
                task_id=approval.get("task_id"),
                event_type="approval.expired",
                actor="policy",
                payload={"approval_id": approval.get("id"), "action": approval.get("action")},
            )
        return changed

    def list_approvals(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self._all(
                _sql("all_1936.sql"),
                (status, limit),
            )
        else:
            rows = self._all(_sql("all_1940.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_1944.sql"), (approval_id,))
        return self._row_to_dict(row) if row else None

    def approve(self, approval_id: str, *, approved_by: str = "user") -> dict[str, Any] | None:
        approval = self.get_approval(approval_id)
        if approval is None:
            return None
        now = time.time()
        if approval["status"] != ApprovalStatus.PENDING.value:
            return approval
        if now > float(approval["expires_at"]):
            self._set_approval_status(approval_id, ApprovalStatus.EXPIRED.value)
            return self.get_approval(approval_id)
        with self._lock:
            self._db.execute(
                _sql("execute_1959.sql"),
                (ApprovalStatus.APPROVED.value, approved_by, now, approval_id),
            )
            self._db.commit()
        self.record_event(
            task_id=approval.get("task_id"),
            event_type="approval.approved",
            actor=approved_by,
            payload={"approval_id": approval_id, "action": approval.get("action")},
        )
        return self.get_approval(approval_id)

    def reject(self, approval_id: str, *, reason: str = "") -> dict[str, Any] | None:
        approval = self.get_approval(approval_id)
        if approval is None:
            return None
        if approval["status"] != ApprovalStatus.PENDING.value:
            return approval
        with self._lock:
            self._db.execute(
                _sql("execute_1979.sql"),
                (ApprovalStatus.REJECTED.value, reason, approval_id),
            )
            self._db.commit()
        self.record_event(
            task_id=approval.get("task_id"),
            event_type="approval.rejected",
            actor="user",
            payload={"approval_id": approval_id, "action": approval.get("action"), "reason": reason},
        )
        return self.get_approval(approval_id)

    def create_preapproval_window(
        self,
        *,
        action: str,
        scope: dict[str, Any] | None = None,
        ttl_seconds: int = 300,
        max_uses: int = 1,
        reason: str = "",
        created_by: str = "user",
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        window_id = _new_id("preapproval")
        now = time.time()
        normalized = _normalize_action(action)
        ttl = max(1, int(ttl_seconds))
        uses = max(1, int(max_uses))
        with self._lock:
            self._db.execute(
                _sql("execute_2010.sql"),
                (
                    window_id,
                    task_id,
                    normalized,
                    _json_dumps(scope or {}),
                    "active",
                    reason,
                    created_by,
                    now,
                    now + ttl,
                    uses,
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="preapproval.window_created",
            actor="agentic.preapproval",
            payload={
                "window_id": window_id,
                "action": normalized,
                "scope": scope or {},
                "ttl_seconds": ttl,
                "max_uses": uses,
                "reason": reason,
                "created_by": created_by,
            },
        )
        return self.get_preapproval_window(window_id) or {}

    def get_preapproval_window(self, window_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_2049.sql"), (window_id,))
        return self._row_to_dict(row) if row else None

    def list_preapproval_windows(
        self,
        *,
        status: str | None = None,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if not include_expired:
            self.expire_preapproval_windows()
        if status:
            rows = self._all(
                _sql("all_2064.sql"),
                (status, limit),
            )
        else:
            rows = self._all(_sql("all_2068.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def revoke_preapproval_window(self, window_id: str, *, reason: str = "") -> dict[str, Any] | None:
        window = self.get_preapproval_window(window_id)
        if window is None:
            return None
        if window["status"] != "active":
            return window
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_2080.sql"),
                ("revoked", now, reason, window_id),
            )
            self._db.commit()
        self.record_event(
            task_id=window.get("task_id"),
            event_type="preapproval.window_revoked",
            actor="agentic.preapproval",
            payload={"window_id": window_id, "action": window.get("action"), "reason": reason},
        )
        return self.get_preapproval_window(window_id)

    def find_preapproval_window(
        self,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized = _normalize_action(action)
        self.expire_preapproval_windows()
        rows = self._all(
            _sql("all_2106.sql"),
            (normalized, "active", time.time()),
        )
        payload = payload or {}
        for row in rows:
            window = self._row_to_dict(row)
            if self._preapproval_scope_matches(window, payload=payload, task_id=task_id):
                return window
        return None

    def expire_preapproval_windows(self) -> int:
        now = time.time()
        rows = self._all(
            _sql("all_2123.sql"),
            ("active", now),
        )
        changed = 0
        for row in rows:
            window = self._row_to_dict(row)
            with self._lock:
                self._db.execute(
                    _sql("execute_2131.sql"),
                    ("expired", window["id"]),
                )
                self._db.commit()
            changed += 1
            self.record_event(
                task_id=window.get("task_id"),
                event_type="preapproval.window_expired",
                actor="agentic.preapproval",
                payload={"window_id": window.get("id"), "action": window.get("action")},
            )
        return changed

    def consume_preapproval_window(
        self,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        actor: str = "agentic.runtime",
    ) -> dict[str, Any] | None:
        normalized = _normalize_action(action)
        self.expire_preapproval_windows()
        rows = self._all(
            _sql("all_2155.sql"),
            (normalized, "active", time.time()),
        )
        payload = payload or {}
        for row in rows:
            window = self._row_to_dict(row)
            if not self._preapproval_scope_matches(window, payload=payload, task_id=task_id):
                continue
            used_count = int(window.get("used_count") or 0) + 1
            status = "consumed" if used_count >= int(window.get("max_uses") or 1) else "active"
            with self._lock:
                self._db.execute(
                    _sql("execute_2171.sql"),
                    (used_count, status, window["id"], "active"),
                )
                self._db.commit()
            self.record_event(
                task_id=task_id or window.get("task_id"),
                event_type="preapproval.window_consumed",
                actor=actor,
                payload={
                    "window_id": window.get("id"),
                    "action": normalized,
                    "used_count": used_count,
                    "max_uses": window.get("max_uses"),
                    "status": status,
                    "payload_preview": _preview(payload),
                },
            )
            return self.get_preapproval_window(str(window["id"]))
        return None

    @staticmethod
    def _preapproval_scope_matches(
        window: dict[str, Any],
        *,
        payload: dict[str, Any],
        task_id: str | None,
    ) -> bool:
        scope = window.get("scope") or {}
        window_task = scope.get("task_id") or window.get("task_id")
        if window_task and task_id and str(window_task) != str(task_id):
            return False
        if window_task and not task_id:
            return False
        proposal_id = scope.get("proposal_id")
        if proposal_id and str(proposal_id) != str(payload.get("proposal_id") or payload.get("id") or ""):
            return False
        operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else payload
        key = str(operation.get("key") or payload.get("runtime_flag_key") or "")
        allowed_keys = set(scope.get("runtime_flag_keys") or scope.get("keys") or [])
        if allowed_keys and key not in allowed_keys:
            return False
        allowed_prefixes = tuple(str(prefix) for prefix in (scope.get("runtime_flag_prefixes") or scope.get("key_prefixes") or []))
        if allowed_prefixes and not key.startswith(allowed_prefixes):
            return False
        expected_safe_action = scope.get("safe_action")
        value = operation.get("value") if isinstance(operation.get("value"), dict) else {}
        if expected_safe_action and value.get("safe_action") != expected_safe_action:
            return False
        max_ttl = scope.get("max_ttl_seconds")
        if max_ttl is not None and int(operation.get("ttl_seconds") or 0) > int(max_ttl):
            return False
        return True

    def record_resource_lease(
        self,
        *,
        capability: str,
        decision: str,
        status: str,
        task_id: str | None = None,
        lease_id: str | None = None,
        payload: dict[str, Any] | None = None,
        expires_at: float | None = None,
    ) -> str:
        row_id = _new_id("lease")
        with self._lock:
            self._db.execute(
                _sql("execute_2242.sql"),
                (row_id, task_id, lease_id, capability, decision, status, time.time(), expires_at, _json_dumps(payload or {})),
            )
            self._db.commit()
        return row_id

    def renew_resource_lease(self, lease_id: str, *, expires_at: float | None = None) -> bool:
        with self._lock:
            cur = self._db.execute(
                _sql("execute_2256.sql"),
                ("active", time.time(), expires_at, lease_id),
            )
            changed = cur.rowcount > 0
            self._db.commit()
        return changed

    def release_resource_lease(self, lease_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                _sql("execute_2270.sql"),
                ("released", time.time(), lease_id),
            )
            changed = cur.rowcount > 0
            self._db.commit()
        return changed

    def expire_resource_leases(self) -> int:
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                _sql("execute_2285.sql"),
                ("expired", now, "expired"),
            )
            changed = cur.rowcount
            self._db.commit()
        return changed

    def list_resource_leases(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self._all(
                _sql("all_2300.sql"),
                (status, limit),
            )
        else:
            rows = self._all(
                _sql("all_2305.sql"),
                (limit,),
            )
        return [self._row_to_dict(row) for row in rows]

    def resume_task(self, task_id: str, *, reason: str = "") -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        if task.status not in {TaskStatus.WAITING_APPROVAL.value, TaskStatus.RECOVERING.value, TaskStatus.QUEUED.value}:
            return False
        self.update_task(
            task_id,
            status=TaskStatus.QUEUED.value,
            metadata={"resume_reason": reason, "resumed_at": time.time(), "defer_until": 0},
        )
        self.record_event(
            task_id=task_id,
            event_type="task.resumed",
            actor="symbiont",
            payload={"reason": reason},
            trace_id=task.trace_id,
        )
        return True

    def defer_task(self, task_id: str, *, reason: str, retry_after_seconds: float | None = None, metadata: dict[str, Any] | None = None) -> bool:
        task = self.get_task(task_id)
        if task is None:
            return False
        defer_until = time.time() + max(0.0, float(retry_after_seconds or 0))
        payload = {"defer_reason": reason, "defer_until": defer_until}
        if metadata:
            payload.update(metadata)
        self.update_task(task_id, status=TaskStatus.QUEUED.value, metadata=payload)
        self.record_event(
            task_id=task_id,
            event_type="task.deferred",
            actor="agentic.runner",
            payload={"reason": reason, "retry_after_seconds": retry_after_seconds},
            trace_id=task.trace_id,
        )
        return True

    def mark_tasks_recovering(self, task_ids: list[str], *, reason: str) -> int:
        if not task_ids:
            return 0
        now = time.time()
        changed = 0
        for task_id in task_ids:
            task = self.get_task(task_id)
            if task is None or task.status not in {TaskStatus.PLANNING.value, TaskStatus.RUNNING.value}:
                continue
            self.update_task(task_id, status=TaskStatus.RECOVERING.value, metadata={"recovery_reason": reason, "recovered_at": now})
            changed += 1
        return changed

    def tool_calls_for_task(
        self,
        task_id: str,
        *,
        statuses: tuple[str, ...] | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [task_id]
        clauses = ["task_id = ?"]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        rows = self._all(
            _sql("fstring_1897_4.sql").format(' AND '.join(clauses)),
            tuple(params),
        )
        return [self._row_to_dict(row) for row in rows]

    def trace(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        return {
            "task": task.to_dict(),
            "runs": [self._row_to_dict(row) for row in self._all(_sql("all_2389.sql"), (task_id,))],
            "steps": [self._row_to_dict(row) for row in self._all(_sql("all_2390.sql"), (task_id,))],
            "events": [self._row_to_dict(row) for row in self._all(_sql("all_2391.sql"), (task_id,))],
            "state_snapshots": [
                self._row_to_dict(row)
                for row in self._all(
                    _sql("all_2395.sql"),
                    (task_id,),
                )
            ],
            "decisions": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2401.sql"), (task_id,))
            ],
            "raw_outputs": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2405.sql"), (task_id,))
            ],
            "tool_calls": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2409.sql"), (task_id,))
            ],
            "approvals": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2413.sql"), (task_id,))
            ],
            "preapproval_windows": [
                self._row_to_dict(row)
                for row in self._all(
                    _sql("all_2418.sql"),
                    (task_id,),
                )
            ],
            "resource_leases": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2424.sql"), (task_id,))
            ],
            "improvement_proposals": [
                self._row_to_dict(row)
                for row in self._all(
                    _sql("all_2429.sql"),
                    (task_id,),
                )
            ],
            "actuations": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2435.sql"), (task_id,))
            ],
            "parallel_rounds": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2439.sql"), (task_id,))
            ],
            "messages": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2443.sql"), (task_id,))
            ],
            "ai_events": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2447.sql"), (task_id,))
            ],
            "memories": [
                self._row_to_dict(row)
                for row in self._all(_sql("all_2451.sql"), (task_id,))
            ],
        }

    def explain(self, task_id: str) -> dict[str, Any] | None:
        trace = self.trace(task_id)
        if trace is None:
            return None
        task = trace["task"]
        events = trace["events"]
        pending = [a for a in trace["approvals"] if a["status"] == ApprovalStatus.PENDING.value]
        policy_events = [e for e in events if str(e["event_type"]).startswith("policy.")]
        run = trace["runs"][-1] if trace["runs"] else {}
        state_snapshot = trace["state_snapshots"][-1] if trace["state_snapshots"] else None
        agent_state = state_snapshot.get("state") if isinstance(state_snapshot, dict) else None
        summary = {
            "task_id": task["id"],
            "status": task["status"],
            "mode": task["mode"],
            "goal_preview": _preview(task["goal"], 500),
            "trace_id": task["trace_id"],
            "graph_run_id": run.get("graph_run_id"),
            "agent_state_hash": state_snapshot.get("state_hash") if state_snapshot else None,
            "agent_state_status": agent_state.get("status") if isinstance(agent_state, dict) else None,
            "steps_count": len(trace["steps"]),
            "tool_calls_count": len(trace["tool_calls"]),
            "decisions_count": len(trace["decisions"]),
            "raw_outputs_count": len(trace["raw_outputs"]),
            "parallel_rounds_count": len(trace["parallel_rounds"]),
            "messages_count": len(trace["messages"]),
            "ai_events_count": len(trace["ai_events"]),
            "memories_count": len(trace["memories"]),
            "pending_approvals": len(pending),
        }
        return {
            "summary": summary,
            "why": {
                "created_from": task["source"],
                "mode": task["mode"],
                "policy_mode": (policy_events[-1]["payload"].get("policy_mode") if policy_events else None),
                "resume_reason": task.get("metadata", {}).get("resume_reason"),
                "terminal_reason": task.get("error") or task.get("result"),
            },
            "model_and_routing": task.get("result") or {},
            "approval_state": {
                "pending": len(pending),
                "approved": len([a for a in trace["approvals"] if a["status"] == ApprovalStatus.APPROVED.value]),
                "rejected": len([a for a in trace["approvals"] if a["status"] == ApprovalStatus.REJECTED.value]),
                "expired": len([a for a in trace["approvals"] if a["status"] == ApprovalStatus.EXPIRED.value]),
            },
            "preapproval_state": {
                "active": len([w for w in trace["preapproval_windows"] if w["status"] == "active"]),
                "consumed": len([w for w in trace["preapproval_windows"] if w["status"] == "consumed"]),
                "revoked": len([w for w in trace["preapproval_windows"] if w["status"] == "revoked"]),
                "expired": len([w for w in trace["preapproval_windows"] if w["status"] == "expired"]),
            },
            "improvement_state": {
                "proposed": len([p for p in trace["improvement_proposals"] if p["status"] == "proposed"]),
                "waiting_approval": len([p for p in trace["improvement_proposals"] if p["status"] == "waiting_approval"]),
                "applied": len([p for p in trace["improvement_proposals"] if p["status"] == "applied"]),
                "rejected": len([p for p in trace["improvement_proposals"] if p["status"] == "rejected"]),
            },
            "actuation_state": {
                "active": len([a for a in trace["actuations"] if a["status"] == "active"]),
                "applied": len([a for a in trace["actuations"] if a["status"] == "applied"]),
                "rolled_back": len([a for a in trace["actuations"] if a["status"] == "rolled_back"]),
                "expired": len([a for a in trace["actuations"] if a["status"] == "expired"]),
                "failed": len([a for a in trace["actuations"] if a["status"] == "failed"]),
            },
            "lease_decision": (trace["resource_leases"][-1] if trace["resource_leases"] else None),
            "policy_decisions": policy_events,
            "agent_state": agent_state,
            "agent_state_snapshots": trace["state_snapshots"],
            "decisions": trace["decisions"],
            "raw_outputs": trace["raw_outputs"],
            "tools": trace["tool_calls"],
            "approvals": trace["approvals"],
            "preapproval_windows": trace["preapproval_windows"],
            "resource_leases": trace["resource_leases"],
            "improvement_proposals": trace["improvement_proposals"],
            "actuations": trace["actuations"],
            "parallel_rounds": trace["parallel_rounds"],
            "messages": trace["messages"],
            "consensus": [m for m in trace["messages"] if m.get("message_type") == "consensus"],
            "ai_events": trace["ai_events"],
            "memories": trace["memories"],
            "timeline": events,
        }

    def list_events(self, *, limit: int = 100, event_type: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        if event_type:
            rows = self._all(
                _sql("all_2544.sql"),
                (event_type, limit),
            )
        else:
            rows = self._all(_sql("all_2548.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def replay_actuation_lifecycle(self, actuation_id: str, *, limit: int = 1000) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        proposal_id = str(actuation.get("proposal_id") or "")
        proposal = self.get_improvement_proposal(proposal_id) if proposal_id else None
        events = []
        for event in reversed(self.list_events(limit=limit)):
            payload = event.get("payload") or {}
            if payload.get("actuation_id") == actuation_id or (proposal_id and payload.get("proposal_id") == proposal_id):
                events.append(event)

        impact_events: list[dict[str, Any]] = []
        closed_loop_decisions: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []
        escalation_routes: list[dict[str, Any]] = []
        rollback: dict[str, Any] | None = None
        renewals: list[dict[str, Any]] = []
        for event in events:
            payload = event.get("payload") or {}
            event_type = str(event.get("event_type") or "")
            if event_type == "actuation.impact_measured":
                impact_events.append(dict(payload.get("impact") or {}))
            elif event_type == "actuation.closed_loop_decision":
                closed_loop_decisions.append(dict(payload.get("decision") or {}))
            elif event_type == "actuation.escalated":
                escalations.append(dict(payload.get("escalation") or {}))
            elif event_type == "escalation.route_planned":
                escalation_routes.append(dict(payload.get("route") or {}))
            elif event_type == "actuation.rolled_back":
                rollback = dict(payload)
            elif event_type == "actuation.renewed":
                renewals.append(dict(payload))

        return {
            "actuation_id": actuation_id,
            "proposal_id": proposal_id or None,
            "status": actuation.get("status"),
            "proposal": proposal,
            "actuation": actuation,
            "events_replayed": len(events),
            "event_types": [event.get("event_type") for event in events],
            "impact_events": impact_events,
            "current_impact": actuation.get("impact") or {},
            "closed_loop_decisions": closed_loop_decisions,
            "escalations": escalations,
            "escalation_routes": escalation_routes,
            "renewals": renewals,
            "rollback": rollback,
        }

    def count_events(self, *, event_type: str, since: float | None = None) -> int:
        if since is None:
            row = self._one(_sql("one_2604.sql"), (event_type,))
        else:
            row = self._one(
                _sql("one_2607.sql"),
                (event_type, since),
            )
        return int(row["c"]) if row else 0

    def set_runtime_flag(
        self,
        key: str,
        value: dict[str, Any],
        *,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        safe_value = _redact_ai_local_payload(value if isinstance(value, dict) else {})
        with self._lock:
            self._db.execute(
                _sql("execute_2624.sql"),
                (key, _json_dumps(safe_value), now, expires_at),
            )
            self._db.commit()
        self.record_event(
            event_type="runtime.flag_set",
            actor="agentic.runtime",
            payload={"key": key, "value": safe_value, "expires_at": expires_at},
        )
        return {"key": key, "value": safe_value, "updated_at": now, "expires_at": expires_at}

    def get_runtime_flag(self, key: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_2643.sql"), (key,))
        if row is None:
            return None
        result = self._row_to_dict(row)
        expires_at = result.get("expires_at")
        if expires_at is not None and float(expires_at) < time.time():
            self.clear_runtime_flag(key, reason="expired")
            return None
        return result

    def list_runtime_flags(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        rows = self._all(_sql("all_2654.sql"))
        flags: list[dict[str, Any]] = []
        now = time.time()
        for row in rows:
            flag = self._row_to_dict(row)
            expires_at = flag.get("expires_at")
            if expires_at is not None and float(expires_at) < now:
                if include_expired:
                    flag["expired"] = True
                    flags.append(flag)
                else:
                    self.clear_runtime_flag(str(flag["key"]), reason="expired")
                continue
            flags.append(flag)
        return flags

    def clear_runtime_flag(self, key: str, *, reason: str = "") -> bool:
        with self._lock:
            cur = self._db.execute(_sql("execute_2672.sql"), (key,))
            changed = cur.rowcount > 0
            self._db.commit()
        if changed:
            self.record_event(
                event_type="runtime.flag_cleared",
                actor="agentic.runtime",
                payload={"key": key, "reason": reason},
            )
        return changed

    def create_improvement_proposal(
        self,
        *,
        kind: str,
        title: str,
        risk_level: str,
        payload: dict[str, Any],
        evidence: dict[str, Any],
        task_id: str | None = None,
        confidence: float = 0.0,
        score: float = 0.0,
        ttl_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        fp = fingerprint or _fingerprint(kind, payload)
        existing = self._one(
            _sql("one_2701.sql"),
            (fp, "proposed", "waiting_approval", "approved"),
        )
        if existing:
            proposal = self._row_to_dict(existing)
            self.record_event(
                task_id=task_id or proposal.get("task_id"),
                event_type="improvement.proposal_deduped",
                actor="agentic.improvement",
                payload={"proposal_id": proposal.get("id"), "kind": kind, "fingerprint": fp},
            )
            return proposal

        proposal_id = _new_id("impr")
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        with self._lock:
            self._db.execute(
                _sql("execute_2723.sql"),
                (
                    proposal_id,
                    task_id,
                    kind,
                    title,
                    "proposed",
                    risk_level,
                    float(confidence),
                    float(score),
                    fp,
                    _json_dumps(payload),
                    _json_dumps(evidence),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                    expires_at,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="improvement.proposed",
            actor="agentic.improvement",
            payload={
                "proposal_id": proposal_id,
                "kind": kind,
                "title": title,
                "risk_level": risk_level,
                "confidence": float(confidence),
                "score": float(score),
                "fingerprint": fp,
            },
        )
        return self.get_improvement_proposal(proposal_id) or {}

    def get_improvement_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_2767.sql"), (proposal_id,))
        return self._row_to_dict(row) if row else None

    def list_improvement_proposals(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self._all(
                _sql("all_2774.sql"),
                (status, limit),
            )
        else:
            rows = self._all(_sql("all_2778.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def set_improvement_approval(self, proposal_id: str, *, approval_id: str) -> dict[str, Any] | None:
        proposal = self.get_improvement_proposal(proposal_id)
        if proposal is None:
            return None
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_2788.sql"),
                ("waiting_approval", approval_id, now, proposal_id),
            )
            self._db.commit()
        self.record_event(
            task_id=proposal.get("task_id"),
            event_type="improvement.approval_requested",
            actor="agentic.improvement",
            payload={"proposal_id": proposal_id, "approval_id": approval_id},
        )
        return self.get_improvement_proposal(proposal_id)

    def mark_improvement_applied(self, proposal_id: str, *, result: dict[str, Any]) -> dict[str, Any] | None:
        proposal = self.get_improvement_proposal(proposal_id)
        if proposal is None:
            return None
        metadata = dict(proposal.get("metadata") or {})
        metadata["apply_result"] = result
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_2813.sql"),
                ("applied", now, now, _json_dumps(metadata), proposal_id),
            )
            self._db.commit()
        self.record_event(
            task_id=proposal.get("task_id"),
            event_type="improvement.applied",
            actor="agentic.improvement",
            payload={"proposal_id": proposal_id, "result": result},
        )
        return self.get_improvement_proposal(proposal_id)

    def reject_improvement_proposal(self, proposal_id: str, *, reason: str = "") -> dict[str, Any] | None:
        proposal = self.get_improvement_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.get("status") in {"applied", "rejected"}:
            return proposal
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_2838.sql"),
                ("rejected", reason, now, proposal_id),
            )
            self._db.commit()
        self.record_event(
            task_id=proposal.get("task_id"),
            event_type="improvement.rejected",
            actor="agentic.improvement",
            payload={"proposal_id": proposal_id, "reason": reason},
        )
        return self.get_improvement_proposal(proposal_id)

    def create_actuation(
        self,
        *,
        action: str,
        mode: str,
        operation: dict[str, Any],
        before: dict[str, Any] | None = None,
        proposal_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        actuation_id = _new_id("act")
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_2870.sql"),
                (
                    actuation_id,
                    proposal_id,
                    task_id,
                    action,
                    mode,
                    "active",
                    _json_dumps(before or {}),
                    _json_dumps(operation),
                    _json_dumps({}),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                    expires_at,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            event_type="actuation.created",
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation_id,
                "proposal_id": proposal_id,
                "action": action,
                "mode": mode,
                "expires_at": expires_at,
            },
        )
        return self.get_actuation(actuation_id) or {}

    def finish_actuation(
        self,
        actuation_id: str,
        *,
        status: str,
        after: dict[str, Any] | None = None,
        impact: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        with self._lock:
            self._db.execute(
                _sql("execute_2923.sql"),
                (
                    status,
                    _json_dumps(after) if after is not None else _json_dumps(actuation.get("after") or {}),
                    _json_dumps(impact) if impact is not None else _json_dumps(actuation.get("impact") or {}),
                    _json_dumps(error) if error is not None else None,
                    time.time(),
                    actuation_id,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=actuation.get("task_id"),
            event_type=f"actuation.{status}",
            actor="agentic.actuator",
            payload={"actuation_id": actuation_id, "proposal_id": actuation.get("proposal_id"), "error": error or {}},
        )
        return self.get_actuation(actuation_id)

    def get_actuation(self, actuation_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_2947.sql"), (actuation_id,))
        return self._row_to_dict(row) if row else None

    def list_actuations(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self._all(
                _sql("all_2954.sql"),
                (status, limit),
            )
        else:
            rows = self._all(_sql("all_2958.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def update_actuation_impact(
        self,
        actuation_id: str,
        *,
        impact: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        merged_impact = dict(actuation.get("impact") or {})
        merged_impact.update(impact)
        merged_metadata = dict(actuation.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        with self._lock:
            self._db.execute(
                _sql("execute_2978.sql"),
                (_json_dumps(merged_impact), _json_dumps(merged_metadata), time.time(), actuation_id),
            )
            self._db.commit()
        self.record_event(
            task_id=actuation.get("task_id"),
            event_type="actuation.impact_measured",
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation_id,
                "proposal_id": actuation.get("proposal_id"),
                "impact": impact,
            },
        )
        return self.get_actuation(actuation_id)

    def update_actuation_metadata(
        self,
        actuation_id: str,
        *,
        metadata: dict[str, Any],
        event_type: str = "actuation.metadata_updated",
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        merged_metadata = dict(actuation.get("metadata") or {})
        merged_metadata.update(metadata)
        with self._lock:
            self._db.execute(
                _sql("execute_3013.sql"),
                (_json_dumps(merged_metadata), time.time(), actuation_id),
            )
            self._db.commit()
        self.record_event(
            task_id=actuation.get("task_id"),
            event_type=event_type,
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation_id,
                "proposal_id": actuation.get("proposal_id"),
                **(event_payload or {}),
            },
        )
        return self.get_actuation(actuation_id)

    def renew_actuation(
        self,
        actuation_id: str,
        *,
        expires_at: float,
        after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        merged_metadata = dict(actuation.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        with self._lock:
            self._db.execute(
                _sql("execute_3049.sql"),
                (
                    expires_at,
                    _json_dumps(after) if after is not None else _json_dumps(actuation.get("after") or {}),
                    _json_dumps(merged_metadata),
                    time.time(),
                    actuation_id,
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=actuation.get("task_id"),
            event_type="actuation.renewed",
            actor="agentic.actuator",
            payload={
                "actuation_id": actuation_id,
                "proposal_id": actuation.get("proposal_id"),
                "expires_at": expires_at,
                "metadata": metadata or {},
            },
        )
        return self.get_actuation(actuation_id)

    def mark_actuation_rolled_back(
        self,
        actuation_id: str,
        *,
        reason: str,
        after: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        actuation = self.get_actuation(actuation_id)
        if actuation is None:
            return None
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_3089.sql"),
                ("rolled_back", _json_dumps(after or {}), now, now, reason, actuation_id),
            )
            self._db.commit()
        self.record_event(
            task_id=actuation.get("task_id"),
            event_type="actuation.rolled_back",
            actor="agentic.actuator",
            payload={"actuation_id": actuation_id, "proposal_id": actuation.get("proposal_id"), "reason": reason},
        )
        return self.get_actuation(actuation_id)

    def expire_actuations(self) -> int:
        now = time.time()
        rows = self._all(
            _sql("all_3108.sql"),
            ("active", "applied", now),
        )
        changed = 0
        for row in rows:
            actuation = self._row_to_dict(row)
            with self._lock:
                self._db.execute(
                    _sql("execute_3116.sql"),
                    ("expired", now, actuation["id"]),
                )
                self._db.commit()
            changed += 1
            self.record_event(
                task_id=actuation.get("task_id"),
                event_type="actuation.expired",
                actor="agentic.actuator",
                payload={"actuation_id": actuation.get("id"), "proposal_id": actuation.get("proposal_id")},
            )
        return changed

    def create_command_session(
        self,
        *,
        context_profile: str,
        cwd: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        ttl_seconds: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = _new_id("cmdsess")
        now = time.time()
        expires_at = now + ttl_seconds if ttl_seconds is not None else None
        with self._lock:
            self._db.execute(
                _sql("execute_3144.sql"),
                (
                    session_id,
                    task_id,
                    trace_id,
                    context_profile,
                    cwd,
                    "open",
                    now,
                    now,
                    expires_at,
                    _json_dumps(metadata or {}),
                ),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            trace_id=trace_id,
            event_type="command.session_created",
            actor="agentic.command",
            payload={
                "session_id": session_id,
                "context_profile": context_profile,
                "cwd": cwd,
                "expires_at": expires_at,
            },
        )
        return self.get_command_session(session_id) or {}

    def get_command_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_3179.sql"), (session_id,))
        return self._row_to_dict(row) if row else None

    def list_command_sessions(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if status:
            rows = self._all(
                _sql("all_3191.sql"),
                (status, limit),
            )
        else:
            rows = self._all(_sql("all_3195.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def close_command_session(self, session_id: str, *, reason: str = "manual_close") -> dict[str, Any] | None:
        session = self.get_command_session(session_id)
        if session is None:
            return None
        now = time.time()
        with self._lock:
            self._db.execute(
                _sql("execute_3205.sql"),
                ("closed", now, now, session_id),
            )
            self._db.commit()
        self.record_event(
            task_id=session.get("task_id"),
            trace_id=session.get("trace_id"),
            event_type="command.session_closed",
            actor="agentic.command",
            payload={"session_id": session_id, "reason": reason},
        )
        return self.get_command_session(session_id)

    def record_command_run(
        self,
        *,
        session_id: str,
        command: str,
        cwd: str,
        context_profile: str,
        action: str,
        risk_level: str,
        policy_decision: str,
        status: str,
        task_id: str | None = None,
        trace_id: str | None = None,
        exit_code: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        output_truncated: bool = False,
        started_at: float | None = None,
        finished_at: float | None = None,
        approval_id: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = _new_id("cmdrun")
        started = started_at or time.time()
        duration_ms = (finished_at - started) * 1000.0 if finished_at is not None else None
        output_metadata = _command_output_metadata(
            run_id=run_id,
            status=status,
            stdout=stdout,
            stderr=stderr,
            output_truncated=output_truncated,
            metadata=metadata,
        )
        with self._lock:
            self._db.execute(
                _sql("execute_3258.sql"),
                (
                    run_id,
                    session_id,
                    task_id,
                    trace_id,
                    command,
                    cwd,
                    context_profile,
                    action,
                    risk_level,
                    policy_decision,
                    status,
                    exit_code,
                    stdout,
                    stderr,
                    1 if output_truncated else 0,
                    started,
                    finished_at,
                    duration_ms,
                    approval_id,
                    error,
                    _json_dumps(output_metadata),
                ),
            )
            self._db.execute(
                _sql("execute_3291.sql"),
                (time.time(), session_id),
            )
            self._db.commit()
        self.record_event(
            task_id=task_id,
            trace_id=trace_id,
            event_type=f"command.{status}",
            actor="agentic.command",
            payload={
                "run_id": run_id,
                "session_id": session_id,
                "action": action,
                "risk_level": risk_level,
                "policy_decision": policy_decision,
                "exit_code": exit_code,
                "approval_id": approval_id,
                "output_truncated": output_truncated,
                "stdout_ref": output_metadata.get("stdout_ref"),
                "stderr_ref": output_metadata.get("stderr_ref"),
                "diff_ref": output_metadata.get("diff_ref"),
                "artifacts": output_metadata.get("artifacts"),
                "stdout_sha256": output_metadata.get("stdout_sha256"),
                "stderr_sha256": output_metadata.get("stderr_sha256"),
                "stdout_size_bytes": output_metadata.get("stdout_size_bytes"),
                "stderr_size_bytes": output_metadata.get("stderr_size_bytes"),
                "redaction_status": output_metadata.get("redaction_status"),
            },
        )
        return self.get_command_run(run_id) or {}

    def get_command_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one(_sql("one_3323.sql"), (run_id,))
        return self._row_to_dict(row) if row else None

    def list_command_runs(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        if session_id:
            rows = self._all(
                _sql("all_3336.sql"),
                (session_id, limit),
            )
        elif task_id:
            rows = self._all(
                _sql("all_3341.sql"),
                (task_id, limit),
            )
        else:
            rows = self._all(_sql("all_3345.sql"), (limit,))
        return [self._row_to_dict(row) for row in rows]

    def recover_non_terminal(self) -> int:
        recoverable = (TaskStatus.PLANNING.value, TaskStatus.RUNNING.value)
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                _sql("execute_3353.sql"),
                (TaskStatus.RECOVERING.value, now, *recoverable),
            )
            changed = cur.rowcount
            self._db.commit()
        if changed:
            self.record_event(
                event_type="runtime.recovered_non_terminal",
                actor="symbiont",
                payload={"tasks_marked_recovering": changed},
            )
        return changed

    @staticmethod
    def _coerce_agent_message(message: Any) -> Any:
        from orchestrator.agentic.contracts import (
            AgentAnswer,
            AgentMessage,
            AgentQuestion,
            ConsensusDecision,
            CritiqueDecision,
            ValidationVote,
        )

        if isinstance(message, AgentMessage):
            return message
        if isinstance(message, dict) and "kind" in message and "message_id" in message:
            return AgentMessage.model_validate(message)
        payload = message if isinstance(message, dict) else getattr(message, "model_dump", lambda **_: message)(mode="json")
        if not isinstance(payload, dict):
            return AgentMessage.model_validate(payload)

        if "answer_id" in payload:
            answer = AgentAnswer.model_validate(payload)
            return AgentMessage(
                message_id=answer.answer_id,
                task_id=answer.task_id,
                trace_id=answer.trace_id,
                kind="answer",
                sender=answer.from_agent,
                recipient=str(answer.metadata.get("to_agent") or "") or None,
                content=answer.answer,
                evidence_refs=answer.evidence_refs,
                metadata={"source_contract": "AgentAnswer", "payload": answer.model_dump(mode="json")},
            )
        if "question_id" in payload:
            question = AgentQuestion.model_validate(payload)
            return AgentMessage(
                message_id=question.question_id,
                task_id=question.task_id,
                trace_id=question.trace_id,
                kind="question",
                sender=question.from_agent,
                recipient=question.to_agent,
                content=question.question,
                round_id=question.round_id,
                evidence_refs=question.evidence_refs,
                metadata={"source_contract": "AgentQuestion", "payload": question.model_dump(mode="json")},
            )
        if "vote_id" in payload:
            vote = ValidationVote.model_validate(payload)
            return AgentMessage(
                message_id=vote.vote_id,
                task_id=vote.task_id,
                trace_id=vote.trace_id,
                kind="validation",
                sender=vote.voter,
                recipient=vote.target_ref,
                content=f"{vote.vote}: {vote.rationale}".strip(),
                evidence_refs=vote.evidence_refs,
                metadata={"source_contract": "ValidationVote", "payload": vote.model_dump(mode="json")},
            )
        if "critique_id" in payload:
            critique = CritiqueDecision.model_validate(payload)
            content = "\n".join([*critique.findings, *critique.required_revisions]) or "critique recorded"
            return AgentMessage(
                message_id=critique.critique_id,
                task_id=critique.task_id,
                trace_id=critique.trace_id,
                kind="critique",
                sender=critique.critic,
                recipient=critique.target_ref,
                content=content,
                metadata={"source_contract": "CritiqueDecision", "payload": critique.model_dump(mode="json")},
            )
        if "consensus_id" in payload:
            consensus = ConsensusDecision.model_validate(payload)
            return AgentMessage(
                message_id=consensus.consensus_id,
                task_id=consensus.task_id,
                trace_id=consensus.trace_id,
                kind="consensus",
                sender=str(consensus.metadata.get("decider") or "agentic.consensus"),
                content=consensus.summary,
                round_id=consensus.round_id,
                metadata={"source_contract": "ConsensusDecision", "payload": consensus.model_dump(mode="json")},
            )
        return AgentMessage.model_validate(payload)

    def _set_approval_status(self, approval_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(_sql("execute_3458.sql"), (status, approval_id))
            self._db.commit()

    def _one(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, params).fetchall())

    def _task_from_row(self, row: sqlite3.Row) -> AgenticTask:
        return AgenticTask(
            id=row["id"],
            goal=row["goal"],
            mode=row["mode"],
            status=row["status"],
            priority=row["priority"],
            session_id=row["session_id"],
            user_id_hash=row["user_id_hash"],
            trace_id=row["trace_id"],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            budget=_json_loads(row["budget_json"]) or {},
            result=_json_loads(row["result_json"]),
            error=_json_loads(row["error_json"]),
            metadata=_json_loads(row["metadata_json"]) or {},
        )

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                new_key = key[:-5]
                result[new_key] = _json_loads(result.pop(key)) or {}
        return result


_STORE: AgenticStore | None = None


def get_agentic_store() -> AgenticStore:
    global _STORE
    if _STORE is None:
        from orchestrator.config import get_settings

        _STORE = AgenticStore(get_settings().agentic_runtime.db_path)
    return _STORE


def reset_agentic_store() -> None:
    global _STORE
    if _STORE is not None:
        try:
            _STORE.close()
        except Exception:
            pass
    _STORE = None
