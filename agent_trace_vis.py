#!/usr/bin/env python3
"""Local Codex and Claude Code JSONL trace visualizer."""

import argparse
import dataclasses
import datetime as _dt
import hashlib
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
import webbrowser


INDEX_SAMPLE_LINES = 200
DEFAULT_MAX_PREVIEW_CHARS = 320
MERMAID_VENDOR_RELATIVE_PATH = Path("vendor") / "mermaid.min.js"


def mermaid_vendor_path() -> Optional[Path]:
    candidates = [
        Path(__file__).resolve().parent / MERMAID_VENDOR_RELATIVE_PATH,
        Path.cwd() / MERMAID_VENDOR_RELATIVE_PATH,
        Path(sys.prefix) / "share" / "agent-trace-vis" / MERMAID_VENDOR_RELATIVE_PATH,
        Path(sys.base_prefix) / "share" / "agent-trace-vis" / MERMAID_VENDOR_RELATIVE_PATH,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def iso_from_mtime(mtime: float) -> str:
    return _dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "replace")).hexdigest()[:16]


def compact_json(value: Any, limit: Optional[int] = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            text = repr(value)
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 1)] + "..."
    return text


def preview_text(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) > max_chars:
        if max_chars <= 3:
            return "." * max_chars
        return text[: max(0, max_chars - 3)] + "..."
    return text


def extract_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "input", "output"):
                    value = item.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
                else:
                    parts.append(compact_json(item, 1200))
            else:
                parts.append(compact_json(item, 1200))
        return "\n".join(part for part in parts if part)
    return compact_json(content)


def duration_to_ms(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        secs = value.get("secs", 0)
        nanos = value.get("nanos", 0)
        try:
            return int(secs * 1000 + nanos / 1000000)
        except TypeError:
            return None
    return None


def token_usage_from_codex(info: Any) -> Dict[str, int]:
    if not isinstance(info, dict):
        return {}
    usage = info.get("total_token_usage") or info.get("last_token_usage") or {}
    if not isinstance(usage, dict):
        return {}
    out: Dict[str, int] = {}
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            out[key] = value
    return out


def token_usage_from_claude(message: Any) -> Dict[str, int]:
    if not isinstance(message, dict):
        return {}
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    out: Dict[str, int] = {}
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, int):
            out[key] = value
    if out:
        out["total_tokens"] = sum(out.values())
    return out


def status_state(status: Any) -> str:
    if isinstance(status, dict):
        if "completed" in status:
            return "completed"
        if "failed" in status:
            return "failed"
        if "error" in status:
            return "error"
        if status:
            return next(iter(status.keys()))
    if isinstance(status, str) and status:
        return status
    return "linked"


def summarize_agent_statuses(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    statuses: List[Dict[str, Any]] = []
    for item in payload.get("agent_statuses") or []:
        if not isinstance(item, dict):
            continue
        statuses.append(
            {
                "thread_id": item.get("thread_id"),
                "nickname": item.get("agent_nickname"),
                "role": item.get("agent_role"),
                "state": status_state(item.get("status")),
            }
        )
    if not statuses and payload.get("receiver_thread_id"):
        statuses.append(
            {
                "thread_id": payload.get("receiver_thread_id"),
                "nickname": payload.get("receiver_agent_nickname"),
                "role": payload.get("receiver_agent_role"),
                "state": status_state(payload.get("status")),
            }
        )
    return statuses


def has_team_hint(*values: Any) -> bool:
    needles = ("team", "swarm", "coordinator", "team-plan", "team-exec", "team-verify")
    for value in values:
        if value is None:
            continue
        text = str(value).lower()
        if any(needle in text for needle in needles):
            return True
    return False


EVENT_CATEGORY_DEFS = {
    "conversation": {"label": "Conversation", "icon": "✉"},
    "reasoning": {"label": "Reasoning", "icon": "∴"},
    "tool": {"label": "Tool IO", "icon": "⚙"},
    "shell": {"label": "Shell", "icon": "$"},
    "patch": {"label": "Patch", "icon": "±"},
    "search": {"label": "Search", "icon": "⌕"},
    "agent": {"label": "Agent", "icon": "↳"},
    "team": {"label": "Team", "icon": "⊕"},
    "lifecycle": {"label": "Lifecycle", "icon": "◷"},
    "metadata": {"label": "Metadata", "icon": "i"},
    "token": {"label": "Tokens", "icon": "#"},
    "attachment": {"label": "Attachment", "icon": "▣"},
    "error": {"label": "Error", "icon": "!"},
    "record": {"label": "Record", "icon": "·"},
}


def classify_event(kind: str, raw_type: Optional[str], tool_name: Optional[str], status: Optional[str]) -> Dict[str, str]:
    raw = (raw_type or "").lower()
    tool = (tool_name or "").lower()
    state = (status or "").lower()
    if state and ("error" in state or "failed" in state or (state.startswith("exit ") and state != "exit 0")):
        category = "error"
    elif kind == "message" or raw in ("last-prompt",):
        category = "conversation"
    elif kind == "reasoning" or raw == "thinking":
        category = "reasoning"
    elif kind in ("tool_call", "tool_result") or "tool" in raw or "tool" in tool:
        category = "search" if "search" in raw or "search" in tool or "web" in raw else "tool"
    elif kind == "shell":
        category = "shell"
    elif kind == "patch":
        category = "patch"
    elif kind in ("subagent_spawn", "handoff") or "agent" in raw or "collab" in raw:
        category = "agent"
    elif kind == "team":
        category = "team"
    elif kind in ("task", "progress") or raw in ("queue-operation", "task_started", "task_complete", "progress"):
        category = "lifecycle"
    elif kind == "metadata" or raw in ("session_meta", "turn_context", "ai-title", "system", "file-history-snapshot"):
        category = "metadata"
    elif kind == "token" or raw == "token_count":
        category = "token"
    elif kind == "attachment" or raw == "attachment":
        category = "attachment"
    else:
        category = "record"
    info = EVENT_CATEGORY_DEFS[category]
    return {"category": category, "category_label": info["label"], "icon": info["icon"]}


RELATIONSHIP_CATEGORY_DEFS = {
    "tool_call": {"category": "tool", "category_label": "Tool IO", "icon": "⚙"},
    "subagent_spawn": {"category": "agent", "category_label": "Agent", "icon": "↳"},
    "handoff": {"category": "agent", "category_label": "Agent", "icon": "↳"},
    "team_member": {"category": "team", "category_label": "Team", "icon": "⊕"},
}


def classify_relationship(kind: str) -> Dict[str, str]:
    return RELATIONSHIP_CATEGORY_DEFS.get(
        kind,
        {"category": "record", "category_label": EVENT_CATEGORY_DEFS["record"]["label"], "icon": EVENT_CATEGORY_DEFS["record"]["icon"]},
    )


def mermaid_id(value: str) -> str:
    digest = stable_id(value)
    return f"n_{digest}"


def mermaid_label(value: Any, max_chars: int = 80) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "..."
    return (
        text.replace('"', "'")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("|", "/")
    )


@dataclasses.dataclass
class TraceEvent:
    seq: int
    line: int
    timestamp: Optional[str]
    provider: str
    kind: str
    role: Optional[str] = None
    summary: str = ""
    text: str = ""
    preview: str = ""
    tool_name: Optional[str] = None
    call_id: Optional[str] = None
    status: Optional[str] = None
    duration_ms: Optional[int] = None
    token_usage: Optional[Dict[str, int]] = None
    tags: Optional[List[str]] = None
    raw_type: Optional[str] = None
    category: str = "record"
    category_label: str = "Record"
    icon: str = "·"
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Relationship:
    id: str
    kind: str
    source_session: str
    source_seq: Optional[int] = None
    target_session: Optional[str] = None
    target_seq: Optional[int] = None
    label: str = ""
    status: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    category: str = "record"
    category_label: str = "Record"
    icon: str = "·"

    def to_dict(self) -> Dict[str, Any]:
        data = dataclasses.asdict(self)
        data.update(classify_relationship(self.kind))
        return data


@dataclasses.dataclass
class TraceSession:
    id: str
    provider: str
    path: str
    display_path: str
    title: str
    size: int
    mtime: str
    cwd: Optional[str] = None
    thread_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    kind: str = "main"
    agent_id: Optional[str] = None
    role: Optional[str] = None
    parent_session: Optional[str] = None
    team_id: Optional[str] = None
    parsed: bool = False
    event_count: Optional[int] = None
    parse_errors: int = 0
    event_counts: Optional[Dict[str, int]] = None
    category_counts: Optional[Dict[str, int]] = None
    token_totals: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class TraceStore:
    def __init__(
        self,
        files: Iterable[Path],
        provider_filter: str = "auto",
        max_preview_chars: int = DEFAULT_MAX_PREVIEW_CHARS,
        eager_index: bool = False,
    ) -> None:
        self.provider_filter = provider_filter
        self.max_preview_chars = max_preview_chars
        self.eager_index = eager_index
        self.sessions: List[TraceSession] = []
        self.sessions_by_id: Dict[str, TraceSession] = {}
        self.path_to_id: Dict[str, str] = {}
        self.thread_to_session: Dict[str, str] = {}
        self.claude_session_to_ids: Dict[str, List[str]] = {}
        self.relationships: Dict[str, Relationship] = {}
        self.pending_codex_spawns: List[Tuple[str, Dict[str, Any]]] = []
        self.handoffs_seen: set = set()
        self.events_by_session: Dict[str, List[TraceEvent]] = {}
        self.raw_by_session_seq: Dict[Tuple[str, int], Any] = {}
        self.warnings: List[str] = []

        for path in files:
            self._index_file(path)
        self._link_claude_subagents()
        self._link_codex_spawns()
        self._build_team_groups()

        if eager_index:
            for session in list(self.sessions):
                self.parse_session(session.id)

    def _index_file(self, path: Path) -> None:
        provider = detect_provider(path, self.provider_filter)
        if provider is None:
            self.warnings.append(f"Skipped unknown JSONL provider: {path}")
            return

        try:
            stat = path.stat()
        except OSError as exc:
            self.warnings.append(f"Cannot stat {path}: {exc}")
            return

        session_id = stable_id(str(path.resolve()))
        display_path = display_path_for(path)
        session = TraceSession(
            id=session_id,
            provider=provider,
            path=str(path.resolve()),
            display_path=display_path,
            title=path.stem,
            size=stat.st_size,
            mtime=iso_from_mtime(stat.st_mtime),
            kind="main",
            event_counts={},
            token_totals={},
        )

        if provider == "claude" and "subagents" in path.parts:
            session.kind = "claude_subagent"
            session.role = "subagent"
            session.agent_id = path.stem.replace("agent-", "", 1)

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    if line_no > INDEX_SAMPLE_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        session.parse_errors += 1
                        continue
                    self._apply_index_record(session, record)
        except OSError as exc:
            self.warnings.append(f"Cannot read {path}: {exc}")
            return

        if session.team_id is None and session.kind in ("team_coordinator", "team_member"):
            session.team_id = self._team_id_for(session)

        self.sessions.append(session)
        self.sessions_by_id[session.id] = session
        self.path_to_id[session.path] = session.id

        if session.provider == "claude" and session.agent_id is None:
            # The first sampled records usually carry this value.
            pass

    def _apply_index_record(self, session: TraceSession, record: Dict[str, Any]) -> None:
        if session.start_time is None:
            session.start_time = record.get("timestamp") or record.get("updated_at")
        session.end_time = record.get("timestamp") or record.get("updated_at") or session.end_time

        if session.provider == "codex":
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            payload_type = payload.get("type") or record.get("type")
            if payload.get("id") and payload.get("originator"):
                # Codex subagent traces can include inherited parent session_meta
                # records. The first session_meta is the file's primary thread.
                if session.thread_id is None:
                    session.thread_id = str(payload["id"])
                    self.thread_to_session[session.thread_id] = session.id
                    session.title = payload.get("thread_name") or payload.get("id") or session.title
            if record.get("id") and record.get("thread_name"):
                session.title = record.get("thread_name") or session.title
            if payload.get("cwd"):
                session.cwd = payload.get("cwd")
            if payload.get("model"):
                session.role = session.role or payload.get("model")
            if has_team_hint(
                payload_type,
                payload.get("phase"),
                payload.get("role"),
                payload.get("collaboration_mode_kind"),
            ):
                session.kind = "team_member"
                session.team_id = self._team_id_for(session)
            if payload_type == "collab_agent_spawn_end":
                self.pending_codex_spawns.append(
                    (
                        session.id,
                        {
                            "call_id": payload.get("call_id"),
                            "sender_thread_id": payload.get("sender_thread_id"),
                            "new_thread_id": payload.get("new_thread_id"),
                            "nickname": payload.get("new_agent_nickname"),
                            "role": payload.get("new_agent_role"),
                            "model": payload.get("model"),
                            "reasoning_effort": payload.get("reasoning_effort"),
                            "status": payload.get("status"),
                        },
                    )
                )
        elif session.provider == "claude":
            sid = record.get("sessionId")
            if isinstance(sid, str):
                self.claude_session_to_ids.setdefault(sid, []).append(session.id)
            if record.get("cwd"):
                session.cwd = record.get("cwd")
            if record.get("agentId"):
                session.agent_id = record.get("agentId")
                if session.kind == "main":
                    session.kind = "claude_subagent"
                    session.role = "subagent"
            if record.get("type") == "ai-title" and record.get("aiTitle"):
                session.title = record["aiTitle"]
            if has_team_hint(record.get("entrypoint"), record.get("userType"), record.get("slug")):
                if session.kind == "main":
                    session.kind = "team_member"
                    session.team_id = self._team_id_for(session)
        elif session.provider == "omx":
            if record.get("session_id"):
                session.title = record.get("session_id") or session.title
            if has_team_hint(record.get("event"), record.get("mode"), record.get("role"), record.get("phase")):
                session.kind = "team_member"
                session.team_id = self._team_id_for(session)

    def _link_claude_subagents(self) -> None:
        for session in self.sessions:
            if session.provider != "claude" or session.kind != "claude_subagent":
                continue
            path = Path(session.path)
            if "subagents" not in path.parts:
                continue
            try:
                parent_session_dir = path.parent.parent
                parent_file = parent_session_dir.parent / f"{parent_session_dir.name}.jsonl"
            except IndexError:
                continue
            parent_id = self.path_to_id.get(str(parent_file.resolve()))
            if parent_id:
                session.parent_session = parent_id
                relationship = Relationship(
                    id=f"claude-subagent:{parent_id}:{session.id}",
                    kind="subagent_spawn",
                    source_session=parent_id,
                    target_session=session.id,
                    label=f"Claude subagent {session.agent_id or path.stem}",
                    metadata={"agent_id": session.agent_id, "path_inferred": True},
                )
                self.relationships[relationship.id] = relationship

    def _link_codex_spawns(self) -> None:
        for source_session, metadata in self.pending_codex_spawns:
            self._upsert_codex_subagent_relationship(source_session, None, metadata)

    def _upsert_codex_subagent_relationship(
        self, source_session: str, source_seq: Optional[int], metadata: Dict[str, Any]
    ) -> None:
        new_thread_id = metadata.get("new_thread_id")
        target_session = self.thread_to_session.get(str(new_thread_id)) if new_thread_id else None
        rel_id = f"codex-subagent:{source_session}:{new_thread_id or metadata.get('call_id') or 'unknown'}"
        label = f"Spawn {metadata.get('nickname') or 'agent'} ({metadata.get('role') or 'role unknown'})"

        existing = self.relationships.get(rel_id)
        rel = Relationship(
            id=rel_id,
            kind="subagent_spawn",
            source_session=source_session,
            source_seq=source_seq if source_seq is not None else (existing.source_seq if existing else None),
            target_session=target_session,
            label=label,
            status=metadata.get("status"),
            metadata=metadata,
        )
        self.relationships[rel.id] = rel

        if target_session and target_session in self.sessions_by_id:
            target = self.sessions_by_id[target_session]
            if target.kind == "main":
                target.kind = "codex_subagent"
            target.parent_session = source_session
            target.role = metadata.get("role") or target.role
            target.agent_id = metadata.get("nickname") or metadata.get("new_thread_id") or target.agent_id

    def _team_id_for(self, session: TraceSession) -> str:
        cwd = session.cwd or str(Path(session.path).parent)
        day = (session.start_time or session.mtime or "")[:10]
        return f"{session.provider}:{stable_id(cwd)}:{day}"

    def _build_team_groups(self) -> None:
        groups: Dict[str, List[TraceSession]] = {}
        for session in self.sessions:
            if session.team_id:
                groups.setdefault(session.team_id, []).append(session)
        for team_id, members in groups.items():
            if not members:
                continue
            coordinator = members[0]
            coordinator.kind = "team_coordinator"
            for member in members:
                relationship = Relationship(
                    id=f"team-member:{team_id}:{member.id}",
                    kind="team_member",
                    source_session=coordinator.id,
                    target_session=member.id,
                    label="Team member" if member.id != coordinator.id else "Team coordinator",
                    metadata={"team_id": team_id, "member_kind": member.kind},
                )
                self.relationships[relationship.id] = relationship

    def parse_session(self, session_id: str) -> Tuple[TraceSession, List[TraceEvent]]:
        session = self.sessions_by_id.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.parsed:
            return session, self.events_by_session.get(session_id, [])

        events: List[TraceEvent] = []
        raw_by_seq: Dict[int, Any] = {}
        event_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        token_totals: Dict[str, int] = {}
        parse_errors = 0

        path = Path(session.path)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_no, line in enumerate(handle, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue

                    normalized = self._normalize_record(session, record, line_no)
                    for fields in normalized:
                        seq = len(events)
                        text = fields.get("text") or ""
                        classification = classify_event(
                            fields.get("kind") or "record",
                            fields.get("raw_type"),
                            fields.get("tool_name"),
                            fields.get("status"),
                        )
                        event = TraceEvent(
                            seq=seq,
                            line=line_no,
                            timestamp=fields.get("timestamp"),
                            provider=session.provider,
                            kind=fields.get("kind") or "record",
                            role=fields.get("role"),
                            summary=fields.get("summary") or "",
                            text=text,
                            preview=preview_text(text or fields.get("summary") or "", self.max_preview_chars),
                            tool_name=fields.get("tool_name"),
                            call_id=fields.get("call_id"),
                            status=fields.get("status"),
                            duration_ms=fields.get("duration_ms"),
                            token_usage=fields.get("token_usage") or {},
                            tags=fields.get("tags") or [],
                            raw_type=fields.get("raw_type"),
                            category=classification["category"],
                            category_label=classification["category_label"],
                            icon=classification["icon"],
                            metadata=fields.get("metadata") or {},
                        )
                        events.append(event)
                        raw_by_seq[seq] = record
                        event_counts[event.kind] = event_counts.get(event.kind, 0) + 1
                        category_counts[event.category] = category_counts.get(event.category, 0) + 1
                        if event.token_usage:
                            for key, value in event.token_usage.items():
                                if isinstance(value, int):
                                    token_totals[key] = token_totals.get(key, 0) + value

                        self._capture_event_relationships(session, event, fields)
        except OSError as exc:
            self.warnings.append(f"Cannot parse {path}: {exc}")

        self._pair_tool_calls(session, events)
        session.parsed = True
        session.event_count = len(events)
        session.parse_errors += parse_errors
        session.event_counts = event_counts
        session.category_counts = category_counts
        session.token_totals = token_totals
        if events:
            session.start_time = session.start_time or events[0].timestamp
            session.end_time = events[-1].timestamp or session.end_time

        self.events_by_session[session_id] = events
        for seq, raw in raw_by_seq.items():
            self.raw_by_session_seq[(session_id, seq)] = raw
        return session, events

    def _normalize_record(self, session: TraceSession, record: Dict[str, Any], line_no: int) -> List[Dict[str, Any]]:
        if session.provider == "codex":
            return self._normalize_codex(record)
        if session.provider == "claude":
            return self._normalize_claude(record)
        return self._normalize_generic(record)

    def _base_fields(self, timestamp: Optional[str], kind: str, raw_type: Optional[str]) -> Dict[str, Any]:
        return {"timestamp": timestamp, "kind": kind, "raw_type": raw_type}

    def _normalize_codex(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        timestamp = record.get("timestamp") or payload.get("timestamp") or record.get("updated_at")
        raw_type = payload.get("type") or record.get("type") or "record"

        if not payload and record.get("thread_name"):
            fields = self._base_fields(timestamp, "metadata", raw_type)
            fields.update(
                summary=f"Session index: {record.get('thread_name')}",
                text=compact_json(record),
                role="system",
            )
            return [fields]

        fields = self._base_fields(timestamp, "record", raw_type)
        fields["summary"] = raw_type
        fields["text"] = compact_json(payload or record)

        if raw_type in ("session_meta", "turn_context"):
            fields["kind"] = "metadata"
            fields["summary"] = f"Metadata: {payload.get('id') or raw_type}"
            fields["role"] = "system"
        elif raw_type in ("task_started", "task_complete"):
            fields["kind"] = "task"
            fields["role"] = "system"
            fields["summary"] = raw_type.replace("_", " ")
            if raw_type == "task_complete":
                fields["duration_ms"] = payload.get("duration_ms")
                fields["text"] = payload.get("last_agent_message") or fields["text"]
        elif raw_type in ("message", "agent_message", "user_message"):
            fields["kind"] = "message"
            fields["role"] = payload.get("role")
            if raw_type == "agent_message":
                fields["role"] = "assistant"
                fields["text"] = payload.get("message") or ""
            elif raw_type == "user_message":
                fields["role"] = "user"
                fields["text"] = payload.get("message") or extract_content_text(payload.get("content"))
            else:
                fields["text"] = extract_content_text(payload.get("content"))
            fields["summary"] = f"{fields.get('role') or 'message'} message"
        elif raw_type in ("reasoning", "agent_reasoning"):
            fields["kind"] = "reasoning"
            fields["role"] = "assistant"
            fields["summary"] = "Reasoning"
            fields["text"] = payload.get("text") or extract_content_text(payload.get("summary"))
            if not fields["text"] and payload.get("encrypted_content"):
                fields["text"] = "[encrypted reasoning]"
        elif raw_type in (
            "function_call",
            "custom_tool_call",
            "web_search_call",
            "view_image_tool_call",
            "collab_agent_spawn_call",
        ):
            fields["kind"] = "tool_call"
            fields["role"] = "tool"
            fields["tool_name"] = payload.get("name") or payload.get("tool_name") or raw_type
            fields["call_id"] = payload.get("call_id")
            fields["summary"] = f"Call {fields['tool_name']}"
            fields["text"] = compact_json(payload.get("arguments") or payload.get("input") or payload.get("query") or payload)
        elif raw_type in ("function_call_output", "custom_tool_call_output", "web_search_end"):
            fields["kind"] = "tool_result"
            fields["role"] = "tool"
            fields["tool_name"] = payload.get("name") or raw_type
            fields["call_id"] = payload.get("call_id")
            fields["status"] = payload.get("status")
            fields["summary"] = f"Result {fields['tool_name']}"
            fields["text"] = compact_json(payload.get("output") or payload.get("result") or payload)
        elif raw_type == "exec_command_end":
            fields["kind"] = "shell"
            fields["role"] = "tool"
            fields["tool_name"] = "exec_command"
            fields["call_id"] = payload.get("call_id")
            exit_code = payload.get("exit_code")
            fields["status"] = "ok" if exit_code == 0 else f"exit {exit_code}"
            command = payload.get("command")
            fields["summary"] = compact_json(command, 160) if command else "Shell command"
            fields["duration_ms"] = duration_to_ms(payload.get("duration"))
            fields["text"] = payload.get("formatted_output") or payload.get("aggregated_output") or "\n".join(
                part for part in (payload.get("stdout"), payload.get("stderr")) if part
            )
        elif raw_type == "patch_apply_end":
            fields["kind"] = "patch"
            fields["role"] = "tool"
            fields["tool_name"] = "apply_patch"
            fields["call_id"] = payload.get("call_id")
            fields["status"] = "ok" if payload.get("success") else "failed"
            fields["summary"] = "Patch applied" if payload.get("success") else "Patch failed"
            fields["text"] = compact_json(payload.get("changes") or payload)
        elif raw_type == "token_count":
            fields["kind"] = "token"
            fields["role"] = "system"
            fields["token_usage"] = token_usage_from_codex(payload.get("info"))
            total = fields["token_usage"].get("total_tokens") if fields["token_usage"] else None
            fields["summary"] = f"Token count: {total}" if total is not None else "Token count"
            fields["text"] = compact_json(payload.get("info"))
        elif raw_type == "collab_agent_spawn_end":
            fields["kind"] = "subagent_spawn"
            fields["role"] = "system"
            fields["call_id"] = payload.get("call_id")
            fields["tool_name"] = "spawn_agent"
            fields["status"] = payload.get("status")
            role = payload.get("new_agent_role")
            nickname = payload.get("new_agent_nickname")
            fields["summary"] = f"Spawn {nickname or 'agent'} ({role or 'role unknown'})"
            fields["text"] = payload.get("prompt") or ""
            fields["relationship"] = {
                "sender_thread_id": payload.get("sender_thread_id"),
                "new_thread_id": payload.get("new_thread_id"),
                "nickname": nickname,
                "role": role,
                "model": payload.get("model"),
                "reasoning_effort": payload.get("reasoning_effort"),
            }
            fields["metadata"] = fields["relationship"]
        elif raw_type in ("collab_waiting_end", "collab_close_end"):
            statuses = summarize_agent_statuses(payload)
            fields["kind"] = "handoff"
            fields["role"] = "system"
            fields["tool_name"] = "wait_agent" if raw_type == "collab_waiting_end" else "close_agent"
            fields["call_id"] = payload.get("call_id")
            completed = sum(1 for item in statuses if item.get("state") == "completed")
            failed = sum(1 for item in statuses if item.get("state") in ("failed", "error"))
            if raw_type == "collab_waiting_end":
                fields["summary"] = f"Agent status update: {len(statuses)} agent(s)"
            else:
                receiver = statuses[0].get("nickname") if statuses else "agent"
                fields["summary"] = f"Close {receiver}"
            fields["status"] = "failed" if failed else "completed" if completed else None
            fields["text"] = compact_json({"statuses": statuses}, 1600)
            fields["metadata"] = {
                "sender_thread_id": payload.get("sender_thread_id"),
                "agent_statuses": statuses,
            }
        return [fields]

    def _normalize_claude(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        record_type = record.get("type") or "record"
        timestamp = record.get("timestamp")
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        role = message.get("role") or record_type
        events: List[Dict[str, Any]] = []

        def add(kind: str, summary: str, text: str = "", raw_override: Optional[str] = None, **extra: Any) -> None:
            fields = self._base_fields(timestamp, kind, raw_override or record_type)
            fields.update(summary=summary, text=text, role=role, **extra)
            events.append(fields)

        if record_type in ("assistant", "user"):
            content = message.get("content")
            usage = token_usage_from_claude(message)
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        add("message", f"{role} message", compact_json(item), token_usage=usage)
                        continue
                    item_type = item.get("type") or "content"
                    item_raw_type = f"{record_type}.{item_type}"
                    if item_type == "text":
                        add("message", f"{role} message", item.get("text") or "", raw_override=item_raw_type, token_usage=usage)
                    elif item_type == "thinking":
                        add("reasoning", "Thinking", item.get("thinking") or item.get("text") or "", raw_override=item_raw_type, token_usage=usage)
                    elif item_type == "tool_use":
                        name = item.get("name") or "tool_use"
                        add(
                            "tool_call",
                            f"Call {name}",
                            compact_json(item.get("input") or item),
                            raw_override=item_raw_type,
                            tool_name=name,
                            call_id=item.get("id"),
                            token_usage=usage,
                        )
                    elif item_type == "tool_result":
                        add(
                            "tool_result",
                            "Tool result",
                            extract_content_text(item.get("content")),
                            raw_override=item_raw_type,
                            tool_name="tool_result",
                            call_id=item.get("tool_use_id"),
                            status="error" if item.get("is_error") else "ok",
                            token_usage=usage,
                        )
                    else:
                        add("message", f"{role} {item_type}", compact_json(item), raw_override=item_raw_type, token_usage=usage)
            else:
                add("message", f"{role} message", extract_content_text(content), token_usage=usage)
        elif record_type == "progress":
            add("progress", "Subagent progress", compact_json(record.get("data") or record))
        elif record_type == "system":
            add("metadata", record.get("subtype") or "System", compact_json(record))
        elif record_type == "attachment":
            add("attachment", "Attachment", compact_json(record.get("attachment") or record))
        elif record_type == "queue-operation":
            add("task", record.get("operation") or "Queue operation", compact_json(record))
        elif record_type == "last-prompt":
            add("message", "Last prompt", record.get("lastPrompt") or record.get("content") or "")
        elif record_type == "ai-title":
            add("metadata", "AI title", record.get("aiTitle") or "")
        else:
            add("record", record_type, compact_json(record))

        tool_result = record.get("toolUseResult")
        if tool_result is not None and not any(event.get("kind") == "tool_result" for event in events):
            add(
                "tool_result",
                "Tool result detail",
                compact_json(tool_result),
                raw_override=f"{record_type}.toolUseResult",
                tool_name="tool_result",
                call_id=record.get("sourceToolAssistantUUID") or record.get("toolUseID"),
                status="error" if isinstance(tool_result, dict) and tool_result.get("is_error") else None,
            )

        for event in events:
            if record.get("agentId"):
                event.setdefault("tags", []).append("subagent")
            if record.get("parentToolUseID") or record.get("sourceToolAssistantUUID"):
                event["handoff"] = {
                    "parent_tool_use_id": record.get("parentToolUseID"),
                    "source_tool_assistant_uuid": record.get("sourceToolAssistantUUID"),
                    "tool_use_id": record.get("toolUseID"),
                    "agent_id": record.get("agentId"),
                }
        return events

    def _normalize_generic(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_type = record.get("event") or record.get("type") or "record"
        timestamp = record.get("timestamp") or record.get("_ts")
        kind = "metadata"
        if has_team_hint(raw_type, record.get("mode"), record.get("role"), record.get("phase")):
            kind = "team"
        fields = self._base_fields(timestamp, kind, raw_type)
        fields.update(summary=str(raw_type), text=compact_json(record), role=record.get("role") or "system")
        return [fields]

    def _capture_event_relationships(self, session: TraceSession, event: TraceEvent, fields: Dict[str, Any]) -> None:
        relationship = fields.get("relationship")
        if event.kind == "subagent_spawn" and isinstance(relationship, dict):
            relationship = dict(relationship)
            relationship.setdefault("status", event.status)
            self._upsert_codex_subagent_relationship(session.id, event.seq, relationship)

        handoff = fields.get("handoff")
        if isinstance(handoff, dict) and (session.parent_session or event.tags):
            handoff_key_value = handoff.get("parent_tool_use_id") or handoff.get("agent_id") or handoff.get(
                "source_tool_assistant_uuid"
            )
            handoff_key = (session.parent_session or session.id, session.id, handoff_key_value)
            if handoff_key in self.handoffs_seen:
                return
            self.handoffs_seen.add(handoff_key)
            rel = Relationship(
                id=f"handoff:{session.id}:{event.seq}",
                kind="handoff",
                source_session=session.parent_session or session.id,
                target_session=session.id,
                target_seq=event.seq,
                label="Subagent handoff",
                metadata=handoff,
            )
            self.relationships[rel.id] = rel

    def _pair_tool_calls(self, session: TraceSession, events: List[TraceEvent]) -> None:
        calls: Dict[str, TraceEvent] = {}
        for event in events:
            if not event.call_id:
                continue
            if event.kind == "tool_call":
                calls.setdefault(event.call_id, event)
            elif event.kind in ("tool_result", "shell", "patch"):
                call = calls.get(event.call_id)
                if call:
                    rel = Relationship(
                        id=f"tool:{session.id}:{event.call_id}:{call.seq}:{event.seq}",
                        kind="tool_call",
                        source_session=session.id,
                        source_seq=call.seq,
                        target_session=session.id,
                        target_seq=event.seq,
                        label=call.tool_name or event.tool_name or "tool",
                        status=event.status,
                        metadata={"call_id": event.call_id},
                    )
                    self.relationships[rel.id] = rel

    def related_relationships(self, session_id: str) -> List[Relationship]:
        out = [
            rel
            for rel in self.relationships.values()
            if rel.source_session == session_id or rel.target_session == session_id
        ]
        priority = {"subagent_spawn": 0, "team_member": 1, "handoff": 2, "tool_call": 3}
        out.sort(key=lambda rel: (priority.get(rel.kind, 9), rel.id))
        return out

    def communication_graph(
        self, session: TraceSession, events: List[TraceEvent], relationships: List[Relationship]
    ) -> Dict[str, Any]:
        nodes: Dict[str, Dict[str, str]] = {}
        edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        def add_node(node_id: str, label: str, kind: str = "node") -> None:
            nodes[node_id] = {"id": node_id, "label": label, "kind": kind}

        def add_edge(source: str, target: str, label: str, kind: str = "flow", count: int = 1) -> None:
            key = (source, target, label)
            if key not in edges:
                edges[key] = {"source": source, "target": target, "label": label, "kind": kind, "count": 0}
            edges[key]["count"] += count

        main_label = session.title or Path(session.path).stem
        add_node("main", f"Main\\n{mermaid_label(main_label, 42)}", "main")

        user_messages = sum(1 for event in events if event.kind == "message" and event.role == "user")
        assistant_messages = sum(1 for event in events if event.kind == "message" and event.role == "assistant")
        developer_messages = sum(1 for event in events if event.kind == "message" and event.role == "developer")
        if user_messages:
            add_node("user", "User", "actor")
            add_edge("user", "main", f"{user_messages} message(s)", "message", user_messages)
        if assistant_messages:
            add_node("assistant", "Assistant", "actor")
            add_edge("main", "assistant", f"{assistant_messages} reply/update(s)", "message", assistant_messages)
        if developer_messages:
            add_node("developer", "Developer/System", "actor")
            add_edge("developer", "main", f"{developer_messages} instruction(s)", "message", developer_messages)

        category_targets = {
            "reasoning": ("reasoning", "Reasoning"),
            "tool": ("tools", "Tools"),
            "shell": ("shell", "Shell"),
            "search": ("search", "Search"),
            "patch": ("patch", "Patch"),
            "token": ("tokens", "Token Meter"),
            "error": ("errors", "Errors"),
        }
        for category, (node_id, label) in category_targets.items():
            count = sum(1 for event in events if event.category == category)
            if count:
                add_node(node_id, label, category)
                add_edge("main", node_id, f"{count} {label.lower()}", category, count)
                if category in ("tool", "shell", "search", "patch", "error"):
                    result_count = sum(
                        1
                        for event in events
                        if event.category == category and event.kind in ("tool_result", "shell", "patch")
                    )
                    if result_count:
                        add_edge(node_id, "main", f"{result_count} result(s)", category, result_count)

        agent_threads: Dict[str, str] = {}
        for rel in relationships:
            if rel.kind != "subagent_spawn":
                continue
            metadata = rel.metadata or {}
            thread_id = metadata.get("new_thread_id") or rel.target_session or rel.id
            nickname = metadata.get("nickname") or metadata.get("agent_id") or rel.label or "agent"
            role = metadata.get("role") or ""
            node_id = mermaid_id(f"agent:{thread_id}")
            agent_threads[str(thread_id)] = node_id
            add_node(node_id, f"{mermaid_label(nickname, 28)}\\n{mermaid_label(role, 24)}", "agent")
            label = role or "spawn"
            if rel.source_seq is not None:
                label = f"{label} #{rel.source_seq}"
            add_edge("main", node_id, label, "agent")

        for event in events:
            if event.kind != "handoff":
                continue
            for item in (event.metadata or {}).get("agent_statuses") or []:
                thread_id = item.get("thread_id") or item.get("nickname") or f"handoff:{event.seq}"
                node_id = agent_threads.get(str(thread_id)) or mermaid_id(f"agent:{thread_id}")
                if node_id not in nodes:
                    add_node(
                        node_id,
                        f"{mermaid_label(item.get('nickname') or thread_id, 28)}\\n{mermaid_label(item.get('role') or 'agent', 24)}",
                        "agent",
                    )
                state = item.get("state") or "status"
                add_edge(node_id, "main", f"{state} #{event.seq}", "agent")

        if len(nodes) == 1:
            add_node("events", "Trace Events", "record")
            add_edge("main", "events", f"{len(events)} event(s)", "record", len(events))

        participant_labels = {
            "user": "User",
            "main": f"Main / {mermaid_label(main_label, 36)}",
            "assistant": "Assistant",
            "developer": "Developer/System",
            "reasoning": "Reasoning",
            "tools": "Tools",
            "shell": "Shell",
            "search": "Search",
            "patch": "Patch",
            "tokens": "Token Meter",
            "errors": "Errors",
        }
        use_agent_pool = len(agent_threads) > 16
        if use_agent_pool:
            participant_labels["agents"] = f"Subagents ({len(agent_threads)})"
        for node_id, node in nodes.items():
            if node["kind"] == "agent" and not use_agent_pool:
                participant_labels[node_id] = node["label"].replace("\\n", " / ")

        participants: List[str] = []
        messages: List[Tuple[int, str, str, str, str]] = []
        notes: List[Tuple[str, str, str]] = []

        def ensure_participant(node_id: str) -> None:
            if node_id not in participant_labels:
                participant_labels[node_id] = node_id
            if node_id not in participants:
                participants.append(node_id)

        def add_message(seq: int, source: str, target: str, label: str, arrow: str = "->>") -> None:
            ensure_participant(source)
            ensure_participant(target)
            messages.append((seq, source, target, label, arrow))

        def add_note(source: str, target: str, label: str) -> None:
            ensure_participant(source)
            ensure_participant(target)
            notes.append((source, target, label))

        conversation_events = [event for event in events if event.kind == "message"]
        if len(conversation_events) > 30:
            keep_conversation = {event.seq for event in conversation_events[:12]}
            keep_conversation.update(event.seq for event in conversation_events[-8:])
        else:
            keep_conversation = {event.seq for event in conversation_events}

        category_summaries = {
            "reasoning": ("main", "reasoning", "reasoning step(s)"),
            "tool": ("main", "tools", "tool event(s)"),
            "search": ("main", "search", "search event(s)"),
            "shell": ("main", "shell", "shell result(s)"),
            "patch": ("main", "patch", "patch event(s)"),
            "token": ("main", "tokens", "token update(s)"),
            "error": ("main", "errors", "error event(s)"),
        }
        for category, (source, target, label) in category_summaries.items():
            count = sum(1 for event in events if event.category == category)
            if count:
                add_note(source, target, f"{count} {label}")

        for event in events:
            label = f"#{event.seq} {event.summary or event.raw_type or event.kind}"
            if event.kind == "message":
                if event.seq not in keep_conversation:
                    continue
                if event.role == "user":
                    add_message(event.seq, "user", "main", label)
                elif event.role == "assistant":
                    add_message(event.seq, "main", "assistant", label)
                elif event.role == "developer":
                    add_message(event.seq, "developer", "main", label)
            elif event.category == "error":
                add_message(event.seq, "main", "errors", label)
            elif event.kind == "subagent_spawn":
                thread_id = (event.metadata or {}).get("new_thread_id") or event.call_id or f"agent:{event.seq}"
                agent_id = "agents" if use_agent_pool else agent_threads.get(str(thread_id)) or mermaid_id(f"agent:{thread_id}")
                nickname = (event.metadata or {}).get("nickname") or "Agent"
                role = (event.metadata or {}).get("role") or "subagent"
                if agent_id not in participant_labels:
                    participant_labels[agent_id] = f"{nickname} / {role}"
                if use_agent_pool:
                    label = f"#{event.seq} spawn {nickname} / {role}"
                add_message(event.seq, "main", agent_id, label)
            elif event.kind == "handoff":
                for item in (event.metadata or {}).get("agent_statuses") or []:
                    thread_id = item.get("thread_id") or item.get("nickname") or f"handoff:{event.seq}"
                    agent_id = "agents" if use_agent_pool else agent_threads.get(str(thread_id)) or mermaid_id(f"agent:{thread_id}")
                    if agent_id not in participant_labels:
                        participant_labels[agent_id] = f"{item.get('nickname') or thread_id} / {item.get('role') or 'agent'}"
                    state = item.get("state") or "status"
                    name = item.get("nickname") or thread_id
                    label = f"#{event.seq} {name} {state}" if use_agent_pool else f"#{event.seq} {state}"
                    add_message(event.seq, agent_id, "main", label, "-->>")

        if not messages:
            add_message(0, "main", "tools", f"{len(events)} event(s)")

        sequence_cap = 80
        messages.sort(key=lambda item: item[0])
        omitted = max(0, len(messages) - sequence_cap)
        if omitted:
            head_count = max(1, sequence_cap // 2)
            tail_count = max(1, sequence_cap - head_count - 1)
            head = messages[:head_count]
            tail = messages[-tail_count:]
            messages = head + [(head[-1][0] + 1, "main", "main", f"... {omitted} earlier/later messages omitted ...", "-->>")] + tail

        lines = ["sequenceDiagram", "  autonumber"]
        for node_id in participants:
            lines.append(f'  participant {node_id} as {mermaid_label(participant_labels[node_id], 60)}')
        for source, target, label in notes:
            lines.append(f'  Note over {source},{target}: {mermaid_label(label, 88)}')
        for _, source, target, label, arrow in messages:
            lines.append(f'  {source}{arrow}{target}: {mermaid_label(label, 88)}')

        sequence_mermaid = "\n".join(lines)

        category_counts: Dict[str, int] = {}
        category_labels: Dict[str, str] = {}
        tool_counts: Dict[str, int] = {}
        agent_roles: Dict[str, int] = {}
        for event in events:
            category_counts[event.category] = category_counts.get(event.category, 0) + 1
            category_labels[event.category] = event.category_label or event.category
            if event.tool_name:
                tool_counts[event.tool_name] = tool_counts.get(event.tool_name, 0) + 1
        for rel in relationships:
            if rel.kind == "subagent_spawn":
                role = ((rel.metadata or {}).get("role") or "subagent").strip() or "subagent"
                agent_roles[role] = agent_roles.get(role, 0) + 1

        def sorted_items(data: Dict[str, int], limit: int = 12) -> List[Tuple[str, int]]:
            return sorted(data.items(), key=lambda item: (-item[1], item[0]))[:limit]

        topology_lines = [
            "flowchart LR",
            f'  main["Main<br/>{mermaid_label(main_label, 42)}"]',
            '  user["User"]',
            '  assistant["Assistant"]',
            '  tools["Tools"]',
            '  agents["Subagents"]',
            '  search["Search"]',
            '  shell["Shell"]',
            '  patches["Patch"]',
            '  tokens["Tokens"]',
            '  errors["Errors"]',
            f'  user -->|"{user_messages} messages"| main',
            f'  main -->|"{assistant_messages} updates"| assistant',
            f'  main -->|"{category_counts.get("tool", 0)} tool events"| tools',
            f'  main -->|"{sum(agent_roles.values())} spawned"| agents',
            f'  agents -->|"{sum(1 for event in events if event.kind == "handoff")} handoffs"| main',
            f'  main -->|"{category_counts.get("search", 0)} searches"| search',
            f'  main -->|"{category_counts.get("shell", 0)} shell"| shell',
            f'  main -->|"{category_counts.get("patch", 0)} patches"| patches',
            f'  main -->|"{category_counts.get("token", 0)} updates"| tokens',
            f'  main -->|"{category_counts.get("error", 0)} errors"| errors',
            "  classDef hot fill:#f7dfc9,stroke:#2c2a25,stroke-width:2px;",
            "  classDef cool fill:#d6f0ee,stroke:#2c2a25,stroke-width:2px;",
            "  classDef neutral fill:#fffdf7,stroke:#2c2a25,stroke-width:2px;",
            "  class agents,errors hot;",
            "  class user,assistant cool;",
            "  class main,tools,search,shell,patches,tokens neutral;",
        ]

        state_names = {
            "conversation": "Conversation",
            "reasoning": "Reasoning",
            "tool": "ToolIO",
            "search": "Search",
            "shell": "Shell",
            "patch": "Patch",
            "agent": "Agent",
            "token": "Token",
            "error": "Error",
            "metadata": "Metadata",
            "lifecycle": "Lifecycle",
            "record": "Record",
            "attachment": "Attachment",
            "team": "Team",
        }
        transitions: Dict[Tuple[str, str], int] = {}
        previous = "Start"
        for event in events:
            current = state_names.get(event.category, "Record")
            transitions[(previous, current)] = transitions.get((previous, current), 0) + 1
            previous = current
        transitions[(previous, "End")] = transitions.get((previous, "End"), 0) + 1
        state_lines = ["stateDiagram-v2", "  [*] --> Start"]
        for transition_key, count in sorted_items({"||".join(pair): value for pair, value in transitions.items()}, 24):
            source_name, target_name = transition_key.split("||", 1)
            state_lines.append(f"  {source_name} --> {target_name}: {count}")
        state_lines.append("  End --> [*]")

        pie_lines = ["pie showData", f"  title Event Category Share: {mermaid_label(main_label, 40)}"]
        for category, count in sorted_items(category_counts, 14):
            pie_lines.append(f'  "{mermaid_label(category_labels.get(category, category), 40)}" : {count}')

        timeline_lines = ["timeline", f"  title Trace Phases: {mermaid_label(main_label, 46)}"]
        if events:
            chunk_count = min(8, max(1, len(events)))
            chunk_size = max(1, (len(events) + chunk_count - 1) // chunk_count)
            for index in range(0, len(events), chunk_size):
                chunk = events[index : index + chunk_size]
                counts: Dict[str, int] = {}
                for event in chunk:
                    counts[event.category_label or event.category] = counts.get(event.category_label or event.category, 0) + 1
                label = f"#{chunk[0].seq}-#{chunk[-1].seq}"
                summary = " / ".join(f"{mermaid_label(name, 18)} {count}" for name, count in sorted_items(counts, 4))
                timeline_lines.append(f"  {label} : {summary}")

        mindmap_lines = [
            "mindmap",
            f"  root(({mermaid_label(main_label, 34)}))",
            "    Event Categories",
        ]
        for category, count in sorted_items(category_counts, 10):
            mindmap_lines.append(f"      {mermaid_label(category_labels.get(category, category), 28)}: {count}")
        mindmap_lines.append("    Agents")
        if agent_roles:
            for role, count in sorted_items(agent_roles, 10):
                mindmap_lines.append(f"      {mermaid_label(role, 28)}: {count}")
        else:
            mindmap_lines.append("      none")
        mindmap_lines.append("    Top Tools")
        if tool_counts:
            for tool, count in sorted_items(tool_counts, 10):
                mindmap_lines.append(f"      {mermaid_label(tool, 28)}: {count}")
        else:
            mindmap_lines.append("      none")

        journey_lines = [
            "journey",
            f"  title Trace Work Journey: {mermaid_label(main_label, 42)}",
            "  section Intake",
            f"    User messages: 5: User",
            f"    Developer instructions: 3: Developer",
            "  section Execution",
            f"    Reasoning steps: 4: Assistant",
            f"    Tool and shell events: 3: Tools",
            "  section Delegation",
            f"    Subagent spawns: 5: Agents",
            f"    Handoffs returned: 4: Agents",
            "  section Risk",
            f"    Errors observed: {1 if category_counts.get('error', 0) else 5}: Tools",
        ]

        diagrams = [
            {
                "id": "sequence",
                "title": "Sequence: Communication Over Time",
                "type": "sequenceDiagram",
                "purpose": "Shows the ordered conversation and agent handoff spine. High-volume tool/search/token events are summarized as notes.",
                "mermaid": sequence_mermaid,
            },
            {
                "id": "topology",
                "title": "Flowchart: Communication Topology",
                "type": "flowchart",
                "purpose": "Shows who talks to which subsystem and where traffic concentrates.",
                "mermaid": "\n".join(topology_lines),
            },
            {
                "id": "state",
                "title": "State: Event Category Transitions",
                "type": "stateDiagram",
                "purpose": "Shows how the trace moves between conversation, reasoning, tools, agent work, and errors.",
                "mermaid": "\n".join(state_lines),
            },
            {
                "id": "timeline",
                "title": "Timeline: Trace Phases",
                "type": "timeline",
                "purpose": "Splits the JSONL into sequential chunks and summarizes dominant event types per phase.",
                "mermaid": "\n".join(timeline_lines),
            },
            {
                "id": "pie",
                "title": "Pie: Event Category Mix",
                "type": "pie",
                "purpose": "Quantifies what the trace mostly contains.",
                "mermaid": "\n".join(pie_lines),
            },
            {
                "id": "mindmap",
                "title": "Mindmap: Trace Inventory",
                "type": "mindmap",
                "purpose": "Gives a compact inventory of categories, agent roles, and tools.",
                "mermaid": "\n".join(mindmap_lines),
            },
            {
                "id": "journey",
                "title": "Journey: Work Shape",
                "type": "journey",
                "purpose": "Summarizes the trace as a work process from intake to execution, delegation, and risk.",
                "mermaid": "\n".join(journey_lines),
            },
        ]

        review = [
            "First principle: a trace is an ordered log of actors, actions, tools, and state changes; no single diagram preserves all four dimensions.",
            "Sequence preserves time and handoffs, but compresses high-frequency tool noise so the browser remains usable.",
            "Topology preserves communication structure, but intentionally loses ordering.",
            "State and timeline views reveal workflow phases and category transitions, not exact message content.",
            "Pie and mindmap are inventory views; they answer what dominates the trace before reading individual events.",
            "When a trace lacks explicit Agent Team metadata, subagent spawns and wait/close notifications are treated as communication evidence.",
        ]

        return {
            "title": "Communication Graph",
            "mermaid": sequence_mermaid,
            "diagrams": diagrams,
            "review": review,
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "events": len(events),
                "relationships": len(relationships),
                "subagent_spawns": sum(1 for rel in relationships if rel.kind == "subagent_spawn"),
                "handoffs": sum(1 for event in events if event.kind == "handoff"),
            },
        }

    def session_payload(self, session_id: str) -> Dict[str, Any]:
        session, events = self.parse_session(session_id)
        relationships = self.related_relationships(session_id)
        return {
            "session": session.to_dict(),
            "events": [event.to_dict() for event in events],
            "relationships": [rel.to_dict() for rel in relationships],
            "communication_graph": self.communication_graph(session, events, relationships),
            "related_sessions": {
                sid: self.sessions_by_id[sid].to_dict()
                for rel in relationships
                for sid in (rel.source_session, rel.target_session)
                if sid and sid in self.sessions_by_id
            },
        }

    def raw_event(self, session_id: str, seq: int) -> Any:
        self.parse_session(session_id)
        return self.raw_by_session_seq.get((session_id, seq))

    def sessions_payload(self) -> Dict[str, Any]:
        providers: Dict[str, int] = {}
        kinds: Dict[str, int] = {}
        total_bytes = 0
        for session in self.sessions:
            providers[session.provider] = providers.get(session.provider, 0) + 1
            kinds[session.kind] = kinds.get(session.kind, 0) + 1
            total_bytes += session.size
        return {
            "summary": {
                "session_count": len(self.sessions),
                "total_bytes": total_bytes,
                "providers": providers,
                "kinds": kinds,
                "warnings": self.warnings,
                "eager_index": self.eager_index,
            },
            "sessions": [session.to_dict() for session in self.sessions],
            "relationships": [rel.to_dict() for rel in self.relationships.values()],
        }


def display_path_for(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except ValueError:
        return str(path)


def detect_provider(path: Path, provider_filter: str = "auto") -> Optional[str]:
    if provider_filter in ("codex", "claude"):
        return provider_filter

    parts = set(path.parts)
    if ".codex" in parts or path.name == "session_index.jsonl":
        return "codex"
    if ".claude" in parts:
        return "claude"
    if ".omx" in parts:
        return "omx"

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if isinstance(record.get("payload"), dict) and "type" in record:
                    return "codex"
                if "sessionId" in record or "message" in record:
                    return "claude"
                if "native_session_id" in record or "session_id" in record or "event" in record:
                    return "omx"
                return None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def discover_default_files(home: Optional[Path] = None) -> List[Path]:
    home = home or Path.home()
    candidates: List[Path] = []
    codex_sessions = home / ".codex" / "sessions"
    codex_archived = home / ".codex" / "archived_sessions"
    codex_index = home / ".codex" / "session_index.jsonl"
    claude_projects = home / ".claude" / "projects"

    if codex_sessions.exists():
        candidates.extend(codex_sessions.rglob("*.jsonl"))
    if codex_archived.exists():
        candidates.extend(codex_archived.glob("*.jsonl"))
    if codex_index.exists():
        candidates.append(codex_index)
    if claude_projects.exists():
        candidates.extend(claude_projects.rglob("*.jsonl"))
    return dedupe_paths(candidates)


def dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    out: List[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    out.sort(key=lambda item: (str(item.parent), item.name))
    return out


def resolve_trace_files(paths: List[str], provider_filter: str = "auto") -> Tuple[List[Path], List[str]]:
    warnings: List[str] = []
    if not paths:
        files = discover_default_files()
        if not files:
            warnings.append("No default JSONL traces found under ~/.codex or ~/.claude.")
        return files, warnings

    candidates: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            warnings.append(f"Path does not exist: {raw}")
            continue
        if path.is_file():
            if path.suffix != ".jsonl":
                warnings.append(f"Skipped non-JSONL file: {path}")
                continue
            candidates.append(path)
        elif path.is_dir():
            found = list(path.rglob("*.jsonl"))
            if not found:
                warnings.append(f"No JSONL files found in directory: {path}")
            candidates.extend(found)
        else:
            warnings.append(f"Skipped unsupported path: {path}")

    files = dedupe_paths(candidates)
    if provider_filter in ("codex", "claude"):
        filtered: List[Path] = []
        for path in files:
            detected = detect_provider(path, "auto")
            if detected in (provider_filter, None):
                filtered.append(path)
            else:
                warnings.append(f"Skipped {detected} file while provider={provider_filter}: {path}")
        files = filtered
    return files, warnings


class TraceRequestHandler(BaseHTTPRequestHandler):
    store: TraceStore

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(INDEX_HTML)
            elif parsed.path == "/favicon.ico":
                self._send_empty(204)
            elif parsed.path == "/vendor/mermaid.min.js":
                self._send_vendor_mermaid()
            elif parsed.path == "/api/sessions":
                self._send_json(self.store.sessions_payload())
            elif parsed.path == "/api/session":
                query = parse_qs(parsed.query)
                session_id = (query.get("id") or [""])[0]
                if not session_id:
                    self._send_json({"error": "missing id"}, status=400)
                    return
                self._send_json(self.store.session_payload(session_id))
            elif parsed.path == "/api/raw":
                query = parse_qs(parsed.query)
                session_id = (query.get("id") or [""])[0]
                try:
                    seq = int((query.get("seq") or [""])[0])
                except ValueError:
                    self._send_json({"error": "invalid seq"}, status=400)
                    return
                raw = self.store.raw_event(session_id, seq)
                if raw is None:
                    self._send_json({"error": "raw event not found"}, status=404)
                    return
                self._send_json({"session_id": session_id, "seq": seq, "raw": raw})
            else:
                self._send_json({"error": "not found"}, status=404)
        except KeyError as exc:
            self._send_json({"error": f"unknown session: {exc}"}, status=404)
        except Exception as exc:  # Keep local debugging visible.
            self._send_json({"error": str(exc)}, status=500)

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, body: Any, status: int = 200) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_empty(self, status: int = 204) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_vendor_mermaid(self) -> None:
        path = mermaid_vendor_path()
        if path is None:
            self._send_json({"error": "vendored Mermaid file not found"}, status=404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_handler(store: TraceStore):
    class Handler(TraceRequestHandler):
        pass

    Handler.store = store
    return Handler


def serve(store: TraceStore, host: str, port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(store))
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"
    print(f"Agent Trace Visualizer: {url}")
    print(f"Sessions indexed: {len(store.sessions)}")
    for warning in store.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize Codex and Claude Code JSONL traces in a temporary local website."
    )
    parser.add_argument("paths", nargs="*", help="JSONL file(s) or directory/directories to scan recursively.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="Port to bind. Default: 0 (auto)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    parser.add_argument("--provider", choices=("auto", "codex", "claude"), default="auto")
    parser.add_argument("--max-preview-chars", type=int, default=DEFAULT_MAX_PREVIEW_CHARS)
    parser.add_argument("--eager-index", action="store_true", help="Parse all selected sessions at startup.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    files, warnings = resolve_trace_files(args.paths, args.provider)
    if not files:
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        print("No readable JSONL trace files found.", file=sys.stderr)
        return 2
    store = TraceStore(
        files,
        provider_filter=args.provider,
        max_preview_chars=max(40, args.max_preview_chars),
        eager_index=args.eager_index,
    )
    store.warnings[:0] = warnings
    serve(store, args.host, args.port, not args.no_open)
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Trace Visualizer</title>
  <style>
    :root {
      --ink: #181817;
      --paper: #f4f0e8;
      --panel: #fffaf0;
      --line: #2c2a25;
      --muted: #716b61;
      --cyan: #006d77;
      --rust: #b44522;
      --green: #2d6a4f;
      --gold: #b68100;
      --blue: #2f5f98;
      --violet: #76549a;
      --danger: #a92828;
      --shadow: rgba(24, 24, 23, .12);
      --mono: "SFMono-Regular", "Cascadia Mono", "Liberation Mono", Menlo, monospace;
      --sans: Avenir Next, Optima, Trebuchet MS, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(24,24,23,.035) 1px, transparent 1px),
        linear-gradient(180deg, rgba(24,24,23,.03) 1px, transparent 1px),
        var(--paper);
      background-size: 28px 28px;
      font: 14px/1.45 var(--sans);
    }
    button, input, select {
      font: inherit;
      color: inherit;
    }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-rows: 56px 1fr;
    }
    header {
      display: grid;
      grid-template-columns: 280px 1fr auto;
      align-items: center;
      border-bottom: 2px solid var(--line);
      background: rgba(255,250,240,.92);
      backdrop-filter: blur(6px);
    }
    .brand {
      height: 100%;
      display: flex;
      align-items: center;
      padding: 0 18px;
      border-right: 2px solid var(--line);
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .metrics {
      display: flex;
      gap: 18px;
      align-items: center;
      padding: 0 18px;
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
    }
    .metric span {
      display: block;
      font: 11px/1 var(--mono);
      color: var(--muted);
      text-transform: uppercase;
    }
    .metric strong {
      font: 15px/1.25 var(--mono);
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 0 12px;
    }
    .icon-button {
      width: 34px;
      height: 34px;
      border: 2px solid var(--line);
      background: var(--panel);
      cursor: pointer;
      box-shadow: 2px 2px 0 var(--line);
    }
    .icon-button:hover { transform: translate(1px, 1px); box-shadow: 1px 1px 0 var(--line); }
    main {
      min-height: 0;
      display: grid;
      grid-template-columns: 360px minmax(440px, 1fr) 400px;
    }
    aside, section.detail {
      min-height: 0;
      border-right: 2px solid var(--line);
      background: rgba(255,250,240,.82);
    }
    section.detail {
      border-right: 0;
      border-left: 2px solid var(--line);
    }
    .filters {
      display: grid;
      gap: 8px;
      padding: 12px;
      border-bottom: 2px solid var(--line);
    }
    input, select {
      width: 100%;
      border: 2px solid var(--line);
      background: #fffdf7;
      padding: 8px 10px;
      min-height: 36px;
      outline: none;
    }
    .filter-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .event-tools {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
      padding: 8px;
      border: 2px solid var(--line);
      background: #fffdf7;
    }
    .event-tools select { max-width: 260px; }
    .legend {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .sessions {
      height: calc(100vh - 151px);
      overflow: auto;
    }
    .session {
      border-bottom: 1px solid rgba(44,42,37,.25);
      padding: 11px 12px;
      cursor: pointer;
      background: transparent;
    }
    .session:hover, .session.active {
      background: #fff4d2;
    }
    .session.active {
      box-shadow: inset 4px 0 0 var(--rust);
    }
    .session-title {
      font-weight: 800;
      overflow-wrap: anywhere;
    }
    .session-path {
      margin-top: 4px;
      color: var(--muted);
      font: 11px/1.35 var(--mono);
      overflow-wrap: anywhere;
    }
    .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 7px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 2px 6px;
      border: 1px solid var(--line);
      font: 10px/1 var(--mono);
      text-transform: uppercase;
      background: #fffdf7;
    }
    .badge.codex { background: #d6f0ee; }
    .badge.claude { background: #f7dfc9; }
    .badge.omx { background: #e9dfef; }
    .badge.subagent { background: #dff1d8; }
    .badge.team { background: #ffe9a8; }
    .badge.category-conversation { background: #d6f0ee; }
    .badge.category-reasoning { background: #e9dfef; }
    .badge.category-tool { background: #dbe8f7; }
    .badge.category-shell { background: #e0ead7; }
    .badge.category-patch { background: #dff1d8; }
    .badge.category-search { background: #d8edf2; }
    .badge.category-agent { background: #f7dfc9; }
    .badge.category-team { background: #ffe9a8; }
    .badge.category-lifecycle { background: #ece4d2; }
    .badge.category-metadata { background: #ebe7dd; }
    .badge.category-token { background: #f1e6b5; }
    .badge.category-attachment { background: #e3e1f0; }
    .badge.category-error { background: #f4c6c0; }
    .workspace {
      min-width: 0;
      min-height: 0;
      display: grid;
      grid-template-rows: auto auto 1fr;
    }
    .session-head {
      padding: 14px 18px;
      border-bottom: 2px solid var(--line);
      background: rgba(244,240,232,.9);
    }
    .session-head h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }
    .session-head p {
      margin: 5px 0 0;
      color: var(--muted);
      font: 12px/1.35 var(--mono);
      overflow-wrap: anywhere;
    }
    .tabs {
      display: flex;
      border-bottom: 2px solid var(--line);
      background: #fff7e6;
    }
    .tab {
      border: 0;
      border-right: 2px solid var(--line);
      background: transparent;
      padding: 10px 14px;
      cursor: pointer;
      font-weight: 800;
    }
    .tab.active { background: var(--line); color: var(--paper); }
    .view {
      min-height: 0;
      overflow: auto;
      padding: 14px 18px 28px;
    }
    .event {
      display: grid;
      grid-template-columns: 78px 34px 1fr auto;
      gap: 10px;
      align-items: start;
      padding: 10px 0;
      border-bottom: 1px dashed rgba(44,42,37,.28);
      cursor: pointer;
    }
    .event:hover { background: rgba(255,244,210,.55); }
    .event-time {
      color: var(--muted);
      font: 11px/1.3 var(--mono);
      overflow-wrap: anywhere;
    }
    .event-icon, .rel-icon {
      width: 30px;
      height: 30px;
      min-width: 30px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 2px solid var(--line);
      background: #fffdf7;
      font: 14px/1 var(--mono);
      font-weight: 900;
      box-shadow: 2px 2px 0 var(--line);
    }
    .category-conversation .event-icon, .event-icon.category-conversation, .category-conversation .rel-icon { background: #d6f0ee; }
    .category-reasoning .event-icon, .event-icon.category-reasoning, .category-reasoning .rel-icon { background: #e9dfef; }
    .category-tool .event-icon, .event-icon.category-tool, .category-tool .rel-icon { background: #dbe8f7; }
    .category-shell .event-icon, .event-icon.category-shell, .category-shell .rel-icon { background: #e0ead7; }
    .category-patch .event-icon, .event-icon.category-patch, .category-patch .rel-icon { background: #dff1d8; }
    .category-search .event-icon, .event-icon.category-search, .category-search .rel-icon { background: #d8edf2; }
    .category-agent .event-icon, .event-icon.category-agent, .category-agent .rel-icon { background: #f7dfc9; }
    .category-team .event-icon, .event-icon.category-team, .category-team .rel-icon { background: #ffe9a8; }
    .category-lifecycle .event-icon, .event-icon.category-lifecycle, .category-lifecycle .rel-icon { background: #ece4d2; }
    .category-metadata .event-icon, .event-icon.category-metadata, .category-metadata .rel-icon { background: #ebe7dd; }
    .category-token .event-icon, .event-icon.category-token, .category-token .rel-icon { background: #f1e6b5; }
    .category-attachment .event-icon, .event-icon.category-attachment, .category-attachment .rel-icon { background: #e3e1f0; }
    .category-error .event-icon, .event-icon.category-error, .category-error .rel-icon { background: #f4c6c0; }
    .event-main { min-width: 0; }
    .event-summary { font-weight: 800; overflow-wrap: anywhere; }
    .event-preview {
      margin-top: 4px;
      color: #464037;
      font: 12px/1.45 var(--mono);
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .event-meta {
      display: flex;
      gap: 6px;
      justify-content: flex-end;
      flex-wrap: wrap;
    }
    .rel-list, .team-board, .graph-panel {
      display: grid;
      gap: 10px;
    }
    .graph-header {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      border: 2px solid var(--line);
      background: #fffdf7;
      padding: 10px;
      box-shadow: 3px 3px 0 var(--line);
    }
    .graph-title {
      font-weight: 900;
      font-size: 16px;
    }
    .mermaid-box {
      min-height: 360px;
      overflow: auto;
      border: 2px solid var(--line);
      background: #fffdf7;
      padding: 12px;
      box-shadow: 3px 3px 0 var(--line);
    }
    .mermaid-box svg {
      max-width: none;
      min-width: 720px;
    }
    .mermaid-source pre {
      max-height: 360px;
      overflow: auto;
    }
    .rel {
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 10px;
      align-items: start;
      border: 2px solid var(--line);
      background: #fffdf7;
      padding: 10px;
      box-shadow: 3px 3px 0 var(--line);
    }
    .rel-title { font-weight: 800; }
    .rel-meta {
      margin-top: 5px;
      font: 11px/1.4 var(--mono);
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .lane {
      border-left: 8px solid var(--gold);
      background: #fffdf7;
      padding: 10px 12px;
      border-top: 2px solid var(--line);
      border-right: 2px solid var(--line);
      border-bottom: 2px solid var(--line);
    }
    .detail-inner {
      height: calc(100vh - 56px);
      overflow: auto;
      padding: 14px;
    }
    .detail h2 {
      margin: 0 0 8px;
      font-size: 17px;
      overflow-wrap: anywhere;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .kv {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 6px 10px;
      font: 12px/1.4 var(--mono);
      margin: 10px 0 14px;
    }
    .kv div:nth-child(odd) { color: var(--muted); }
    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #1f1d1a;
      color: #fff4d2;
      padding: 12px;
      border: 2px solid var(--line);
      font: 12px/1.45 var(--mono);
    }
    details {
      border: 2px solid var(--line);
      background: #fffdf7;
      margin: 10px 0;
    }
    summary {
      cursor: pointer;
      padding: 9px 10px;
      font-weight: 800;
      border-bottom: 2px solid var(--line);
    }
    details:not([open]) summary { border-bottom: 0; }
    .detail-text {
      padding: 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px/1.5 var(--mono);
    }
    .empty {
      padding: 20px;
      color: var(--muted);
      font: 13px/1.5 var(--mono);
    }
    @media (max-width: 1100px) {
      header { grid-template-columns: 1fr; }
      .brand { border-right: 0; border-bottom: 2px solid var(--line); min-height: 48px; }
      .metrics { border-bottom: 2px solid var(--line); padding: 10px 14px; overflow: auto; }
      .toolbar { padding: 10px 14px; }
      main { grid-template-columns: 1fr; }
      aside, section.detail { border: 0; border-bottom: 2px solid var(--line); }
      .sessions, .detail-inner { height: auto; max-height: 42vh; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">Agent Trace Visualizer</div>
      <div class="metrics" id="metrics"></div>
      <div class="toolbar">
        <button class="icon-button" title="Reload sessions" id="reloadBtn">R</button>
      </div>
    </header>
    <main>
      <aside>
        <div class="filters">
          <input id="search" type="search" placeholder="Search sessions">
          <div class="filter-row">
            <select id="providerFilter"><option value="">All providers</option></select>
            <select id="kindFilter"><option value="">All kinds</option></select>
          </div>
        </div>
        <div class="sessions" id="sessions"></div>
      </aside>
      <div class="workspace">
        <div class="session-head" id="sessionHead">
          <h1>No session selected</h1>
          <p>Select a trace file from the left.</p>
        </div>
        <div class="tabs">
          <button class="tab active" data-tab="timeline">Timeline</button>
          <button class="tab" data-tab="relationships">Relations</button>
          <button class="tab" data-tab="team">Graph</button>
        </div>
        <div class="view" id="view"></div>
      </div>
      <section class="detail">
        <div class="detail-inner" id="detail"><div class="empty">Event details appear here.</div></div>
      </section>
    </main>
  </div>
  <script>
    const state = {
      sessions: [],
      relationships: [],
      summary: null,
      activeSession: null,
      activePayload: null,
      tab: "timeline",
      eventCategory: "",
      activeDiagram: "sequence"
    };

    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
    const className = (value) => String(value ?? "").replace(/[^a-z0-9_-]/gi, "-");
    let mermaidLoadPromise = null;
    const fmtBytes = (n) => {
      if (!n) return "0 B";
      const units = ["B", "KiB", "MiB", "GiB"];
      let value = n, idx = 0;
      while (value >= 1024 && idx < units.length - 1) { value /= 1024; idx++; }
      return `${value.toFixed(idx ? 1 : 0)} ${units[idx]}`;
    };
    const shortTime = (value) => value ? String(value).replace("T", " ").slice(0, 19) : "";

    async function loadSessions() {
      const res = await fetch("/api/sessions");
      const data = await res.json();
      state.sessions = data.sessions || [];
      state.relationships = data.relationships || [];
      state.summary = data.summary || {};
      renderMetrics();
      renderFilters();
      renderSessions();
      if (!state.activeSession && state.sessions.length) {
        selectSession(state.sessions[0].id);
      }
    }

    function renderMetrics() {
      const summary = state.summary || {};
      const providers = Object.entries(summary.providers || {}).map(([k, v]) => `${k}:${v}`).join(" ");
      const warnings = (summary.warnings || []).length;
      $("metrics").innerHTML = `
        <div class="metric"><span>Sessions</span><strong>${summary.session_count || 0}</strong></div>
        <div class="metric"><span>Bytes</span><strong>${fmtBytes(summary.total_bytes || 0)}</strong></div>
        <div class="metric"><span>Providers</span><strong>${escapeHtml(providers || "-")}</strong></div>
        <div class="metric"><span>Warnings</span><strong>${warnings}</strong></div>
      `;
    }

    function renderFilters() {
      const providers = [...new Set(state.sessions.map(s => s.provider))].sort();
      const kinds = [...new Set(state.sessions.map(s => s.kind))].sort();
      const providerValue = $("providerFilter").value;
      const kindValue = $("kindFilter").value;
      $("providerFilter").innerHTML = `<option value="">All providers</option>` + providers.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
      $("kindFilter").innerHTML = `<option value="">All kinds</option>` + kinds.map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`).join("");
      $("providerFilter").value = providerValue;
      $("kindFilter").value = kindValue;
    }

    function sessionMatches(session) {
      const q = $("search").value.trim().toLowerCase();
      const provider = $("providerFilter").value;
      const kind = $("kindFilter").value;
      if (provider && session.provider !== provider) return false;
      if (kind && session.kind !== kind) return false;
      if (!q) return true;
      return [session.title, session.display_path, session.cwd, session.agent_id, session.kind, session.provider]
        .filter(Boolean).join(" ").toLowerCase().includes(q);
    }

    function filteredSessions() {
      return state.sessions.filter(sessionMatches);
    }

    function applySessionFilters() {
      renderSessions();
      const list = filteredSessions();
      if (list.length && state.activeSession && !list.some(session => session.id === state.activeSession)) {
        selectSession(list[0].id);
      }
      if (!list.length) {
        $("view").innerHTML = `<div class="empty">No sessions match the filters.</div>`;
      }
    }

    function badges(session) {
      const provider = `<span class="badge ${escapeHtml(session.provider)}">${escapeHtml(session.provider)}</span>`;
      const kindClass = session.kind && session.kind.includes("subagent") ? "subagent" : session.kind && session.kind.includes("team") ? "team" : "";
      const kind = `<span class="badge ${kindClass}">${escapeHtml(session.kind || "main")}</span>`;
      const parsed = session.parsed ? `<span class="badge">parsed ${session.event_count || 0}</span>` : `<span class="badge">lazy</span>`;
      return `<div class="badges">${provider}${kind}${parsed}</div>`;
    }

    function renderSessions() {
      const list = filteredSessions();
      $("sessions").innerHTML = list.map(session => `
        <div class="session ${state.activeSession === session.id ? "active" : ""}" data-id="${escapeHtml(session.id)}">
          <div class="session-title">${escapeHtml(session.title || session.display_path)}</div>
          <div class="session-path">${escapeHtml(session.display_path)}</div>
          ${badges(session)}
        </div>
      `).join("") || `<div class="empty">No sessions match the filters.</div>`;
      document.querySelectorAll(".session").forEach(node => {
        node.addEventListener("click", () => selectSession(node.dataset.id));
      });
    }

    async function selectSession(id) {
      state.activeSession = id;
      state.eventCategory = "";
      state.activeDiagram = "sequence";
      renderSessions();
      $("view").innerHTML = `<div class="empty">Loading session...</div>`;
      const res = await fetch(`/api/session?id=${encodeURIComponent(id)}`);
      const data = await res.json();
      state.activePayload = data;
      renderSessionHead(data.session);
      renderActiveTab();
      $("detail").innerHTML = `<div class="empty">Select an event.</div>`;
    }

    function renderSessionHead(session) {
      $("sessionHead").innerHTML = `
        <h1>${escapeHtml(session.title || session.display_path)}</h1>
        <p>${escapeHtml(session.display_path)} | ${escapeHtml(session.cwd || "cwd unknown")} | ${fmtBytes(session.size)} | ${escapeHtml(shortTime(session.start_time || session.mtime))}</p>
      `;
    }

    function renderActiveTab() {
      document.querySelectorAll(".tab").forEach(tab => tab.classList.toggle("active", tab.dataset.tab === state.tab));
      if (!state.activePayload) return;
      if (state.tab === "timeline") renderTimeline();
      if (state.tab === "relationships") renderRelationships();
      if (state.tab === "team") renderTeam();
    }

    function renderTimeline() {
      const events = state.activePayload.events || [];
      const categoryStats = [...events.reduce((map, event) => {
        const key = event.category || "record";
        const existing = map.get(key) || { category: key, label: event.category_label || key, icon: event.icon || "·", count: 0 };
        existing.count += 1;
        map.set(key, existing);
        return map;
      }, new Map()).values()].sort((a, b) => a.label.localeCompare(b.label));
      if (state.eventCategory && !categoryStats.some(item => item.category === state.eventCategory)) {
        state.eventCategory = "";
      }
      const filteredEvents = state.eventCategory ? events.filter(event => event.category === state.eventCategory) : events;
      const categoryOptions = [`<option value="">All event categories (${events.length})</option>`]
        .concat(categoryStats.map(item => `<option value="${escapeHtml(item.category)}" ${state.eventCategory === item.category ? "selected" : ""}>${escapeHtml(item.icon)} ${escapeHtml(item.label)} (${item.count})</option>`))
        .join("");
      const legend = categoryStats.map(item => `<span class="badge category-${className(item.category)}">${escapeHtml(item.icon)} ${escapeHtml(item.label)} ${item.count}</span>`).join("");
      $("view").innerHTML = `
        <div class="event-tools">
          <select id="eventCategoryFilter">${categoryOptions}</select>
          <div class="legend">${legend}</div>
        </div>
      ` + (filteredEvents.map(event => `
        <div class="event ${escapeHtml(event.kind)} category-${className(event.category)}" data-seq="${event.seq}">
          <div class="event-time">${escapeHtml(shortTime(event.timestamp) || ("#" + event.seq))}</div>
          <div class="event-icon" title="${escapeHtml(event.category_label || event.category)}">${escapeHtml(event.icon || "·")}</div>
          <div class="event-main">
            <div class="event-summary">${escapeHtml(event.summary || event.raw_type || event.kind)}</div>
            <div class="event-preview">${escapeHtml(event.preview || "")}</div>
          </div>
          <div class="event-meta">
            <span class="badge category-${className(event.category)}">${escapeHtml(event.category_label || event.category)}</span>
            <span class="badge">${escapeHtml(event.kind)}</span>
            ${event.role ? `<span class="badge">${escapeHtml(event.role)}</span>` : ""}
            ${event.tool_name ? `<span class="badge">${escapeHtml(event.tool_name)}</span>` : ""}
            ${event.status ? `<span class="badge">${escapeHtml(event.status)}</span>` : ""}
          </div>
        </div>
      `).join("") || `<div class="empty">No events match this category.</div>`);
      const categoryFilter = $("eventCategoryFilter");
      if (categoryFilter) {
        categoryFilter.addEventListener("change", () => {
          state.eventCategory = categoryFilter.value;
          renderTimeline();
        });
      }
      document.querySelectorAll(".event").forEach(node => {
        node.addEventListener("click", () => {
          const event = events.find(item => item.seq === Number(node.dataset.seq));
          renderDetail(event);
        });
      });
    }

    function renderRelationships() {
      const sessions = state.activePayload.related_sessions || {};
      const relationships = state.activePayload.relationships || [];
      $("view").innerHTML = `<div class="rel-list">` + relationships.map(rel => {
        const src = sessions[rel.source_session] || {};
        const dst = sessions[rel.target_session] || {};
        return `
          <div class="rel category-${className(rel.category)}">
            <div class="rel-icon" title="${escapeHtml(rel.category_label || rel.category)}">${escapeHtml(rel.icon || "·")}</div>
            <div>
              <div class="rel-title">${escapeHtml(rel.kind)}: ${escapeHtml(rel.label || "")}</div>
              <div class="rel-meta">from ${escapeHtml(src.title || rel.source_session)}${rel.source_seq !== null ? " #" + rel.source_seq : ""}</div>
              <div class="rel-meta">to ${escapeHtml(dst.title || rel.target_session || "unresolved")}${rel.target_seq !== null ? " #" + rel.target_seq : ""}</div>
              <div class="badges">
                <span class="badge category-${className(rel.category)}">${escapeHtml(rel.category_label || rel.category)}</span>
                <span class="badge">${escapeHtml(rel.status || "linked")}</span>
              </div>
            </div>
          </div>
        `;
      }).join("") + `</div>`;
      if (!relationships.length) $("view").innerHTML = `<div class="empty">No relationships for this session yet.</div>`;
    }

    function renderTeam() {
      const current = state.activePayload.session;
      const graph = state.activePayload.communication_graph || {};
      const diagrams = (graph.diagrams && graph.diagrams.length) ? graph.diagrams : [{
        id: "sequence",
        title: graph.title || "Communication Graph",
        type: "mermaid",
        purpose: "Default communication graph.",
        mermaid: graph.mermaid || ""
      }];
      if (!diagrams.some(diagram => diagram.id === state.activeDiagram)) {
        state.activeDiagram = diagrams[0]?.id || "sequence";
      }
      const selected = diagrams.find(diagram => diagram.id === state.activeDiagram) || diagrams[0] || {};
      const teamId = current.team_id;
      const members = state.sessions.filter(session => session.team_id === teamId);
      const stats = graph.stats || {};
      const teamBoard = teamId ? `<div class="team-board">` + members.map(member => `
          <div class="lane">
            <div class="session-title">${escapeHtml(member.title)}</div>
            <div class="session-path">${escapeHtml(member.display_path)}</div>
            ${badges(member)}
          </div>
        `).join("") + `</div>` : "";
      $("view").innerHTML = `
        <div class="graph-panel">
          <div class="graph-header">
            <div>
              <div class="graph-title">Communication Graph</div>
              <div class="rel-meta">${teamId ? "Agent Team grouping plus trace communication edges." : "No explicit Agent Team metadata; graph is inferred from messages, tools, subagents, and handoffs."}</div>
            </div>
            <div class="badges">
              <span class="badge category-agent">↳ agents ${stats.subagent_spawns || 0}</span>
              <span class="badge category-tool">⚙ rels ${stats.relationships || 0}</span>
              <span class="badge">nodes ${stats.nodes || 0}</span>
              <span class="badge">edges ${stats.edges || 0}</span>
            </div>
          </div>
          ${teamBoard}
          <div class="event-tools">
            <select id="diagramSelector">
              ${diagrams.map(diagram => `<option value="${escapeHtml(diagram.id)}" ${diagram.id === selected.id ? "selected" : ""}>${escapeHtml(diagram.title)} (${escapeHtml(diagram.type || "mermaid")})</option>`).join("")}
            </select>
            <div class="legend"><span class="badge">${escapeHtml(selected.purpose || "")}</span></div>
          </div>
          <div class="rel">
            <div class="rel-icon category-reasoning">∴</div>
            <div>
              <div class="rel-title">First-principles review</div>
              ${(graph.review || []).map(item => `<div class="rel-meta">${escapeHtml(item)}</div>`).join("")}
            </div>
          </div>
          <div class="mermaid-box" id="mermaidGraph">Rendering Mermaid graph...</div>
          <details class="mermaid-source">
            <summary>Mermaid source</summary>
            <pre id="mermaidSource">${escapeHtml(selected.mermaid || "")}</pre>
          </details>
        </div>
      `;
      const selector = $("diagramSelector");
      if (selector) {
        selector.addEventListener("change", () => {
          state.activeDiagram = selector.value;
          renderTeam();
        });
      }
      renderMermaidGraph(selected.mermaid || "");
    }

    async function renderMermaidGraph(source) {
      const target = $("mermaidGraph");
      if (!target) return;
      if (!source) {
        target.innerHTML = `<div class="empty">No graph data available for this session.</div>`;
        return;
      }
      try {
        if (!mermaidLoadPromise) {
          mermaidLoadPromise = loadMermaid()
            .then(mermaid => {
              mermaid.initialize({
                startOnLoad: false,
                securityLevel: "strict",
                theme: "base",
                themeVariables: {
                  fontFamily: "SFMono-Regular, Menlo, monospace",
                  primaryColor: "#fffdf7",
                  primaryTextColor: "#181817",
                  primaryBorderColor: "#2c2a25",
                  lineColor: "#2c2a25",
                  secondaryColor: "#f4f0e8",
                  tertiaryColor: "#fff4d2"
                }
              });
              return mermaid;
            });
        }
        const mermaid = await mermaidLoadPromise;
        const id = `trace-graph-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const result = await mermaid.render(id, source);
        target.innerHTML = result.svg;
      } catch (error) {
        target.innerHTML = `
          <div class="empty">Mermaid rendering failed or the local Mermaid asset is unavailable. The sequence source below is still usable.</div>
          <pre>${escapeHtml(source)}</pre>
        `;
      }
    }

    function loadMermaid() {
      if (window.mermaid) return Promise.resolve(window.mermaid);
      return new Promise((resolve, reject) => {
        const existing = document.querySelector('script[data-mermaid-vendor="true"]');
        const script = existing || document.createElement("script");
        let settled = false;
        const timeout = setTimeout(() => {
          if (!settled) {
            settled = true;
            reject(new Error("Timed out loading local Mermaid asset"));
          }
        }, 5000);
        script.onload = () => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          window.mermaid ? resolve(window.mermaid) : reject(new Error("Mermaid global was not registered"));
        };
        script.onerror = () => {
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          reject(new Error("Failed to load local Mermaid asset"));
        };
        if (!existing) {
          script.dataset.mermaidVendor = "true";
          script.src = "/vendor/mermaid.min.js";
          document.head.appendChild(script);
        }
      });
    }

    async function renderDetail(event) {
      if (!event) return;
      $("detail").innerHTML = `
        <h2><span class="event-icon category-${className(event.category)}" title="${escapeHtml(event.category_label || event.category)}">${escapeHtml(event.icon || "·")}</span>${escapeHtml(event.summary || event.kind)}</h2>
        <div class="kv">
          <div>seq</div><div>${event.seq}</div>
          <div>line</div><div>${event.line}</div>
          <div>category</div><div>${escapeHtml(event.category_label || event.category || "-")}</div>
          <div>kind</div><div>${escapeHtml(event.kind)}</div>
          <div>raw</div><div>${escapeHtml(event.raw_type || "-")}</div>
          <div>role</div><div>${escapeHtml(event.role || "-")}</div>
          <div>tool</div><div>${escapeHtml(event.tool_name || "-")}</div>
          <div>call</div><div>${escapeHtml(event.call_id || "-")}</div>
          <div>status</div><div>${escapeHtml(event.status || "-")}</div>
          <div>time</div><div>${escapeHtml(shortTime(event.timestamp) || "-")}</div>
        </div>
        <details>
          <summary>Content</summary>
          <div class="detail-text">${escapeHtml(event.text || event.preview || "")}</div>
        </details>
        <details>
          <summary>Token usage</summary>
          <pre>${escapeHtml(JSON.stringify(event.token_usage || {}, null, 2))}</pre>
        </details>
        <button class="icon-button" title="Load raw JSON" id="rawBtn">{}</button>
        <div id="rawBox"></div>
      `;
      $("rawBtn").addEventListener("click", async () => {
        const res = await fetch(`/api/raw?id=${encodeURIComponent(state.activeSession)}&seq=${event.seq}`);
        const data = await res.json();
        $("rawBox").innerHTML = `<pre>${escapeHtml(JSON.stringify(data.raw ?? data, null, 2))}</pre>`;
      });
    }

    document.querySelectorAll(".tab").forEach(tab => {
      tab.addEventListener("click", () => {
        state.tab = tab.dataset.tab;
        renderActiveTab();
      });
    });
    $("search").addEventListener("input", applySessionFilters);
    $("providerFilter").addEventListener("change", applySessionFilters);
    $("kindFilter").addEventListener("change", applySessionFilters);
    $("reloadBtn").addEventListener("click", loadSessions);
    loadSessions().catch(err => {
      $("view").innerHTML = `<div class="empty">${escapeHtml(err.message || err)}</div>`;
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
