import json
from pathlib import Path
import tempfile
import unittest

import agent_trace_vis as atv


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record) + "\n")


class AgentTraceVisTests(unittest.TestCase):
    def test_vendored_mermaid_asset_exists(self):
        path = atv.mermaid_vendor_path()
        self.assertIsNotNone(path)
        self.assertGreater(path.stat().st_size, 100000)

    def test_discover_default_files_uses_codex_and_claude_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex = home / ".codex" / "sessions" / "2026" / "04" / "22" / "codex.jsonl"
            archived = home / ".codex" / "archived_sessions" / "archived.jsonl"
            index = home / ".codex" / "session_index.jsonl"
            claude = home / ".claude" / "projects" / "proj" / "claude.jsonl"
            for path in (codex, archived, index, claude):
                write_jsonl(path, [])

            files = atv.discover_default_files(home)
            self.assertEqual({path.name for path in files}, {"codex.jsonl", "archived.jsonl", "session_index.jsonl", "claude.jsonl"})

    def test_resolve_explicit_file_directory_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = root / "one.jsonl"
            nested = root / "folder" / "nested.jsonl"
            ignored = root / "folder" / "note.txt"
            write_jsonl(one, [])
            write_jsonl(nested, [])
            ignored.parent.mkdir(parents=True, exist_ok=True)
            ignored.write_text("nope", encoding="utf-8")

            files, warnings = atv.resolve_trace_files([str(one), str(root / "folder"), str(ignored), str(root / "missing")])
            self.assertEqual({path.resolve() for path in files}, {one.resolve(), nested.resolve()})
            self.assertTrue(any("non-JSONL" in warning for warning in warnings))
            self.assertTrue(any("does not exist" in warning for warning in warnings))

    def test_codex_normalization_tool_pairing_and_subagent_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".codex" / "sessions" / "2026" / "04" / "22"
            parent = root / "parent.jsonl"
            child = root / "child.jsonl"
            write_jsonl(
                parent,
                [
                    {"timestamp": "2026-04-22T00:00:00Z", "type": "session_meta", "payload": {"id": "parent-thread", "originator": "codex", "cwd": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}}},
                    {"timestamp": "2026-04-22T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "ok", "status": "completed"}},
                    {
                        "timestamp": "2026-04-22T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "collab_agent_spawn_end",
                            "call_id": "spawn-1",
                            "sender_thread_id": "parent-thread",
                            "new_thread_id": "child-thread",
                            "new_agent_nickname": "Verifier",
                            "new_agent_role": "verifier",
                            "prompt": "check it",
                            "model": "gpt-5.4-mini",
                            "reasoning_effort": "high",
                            "status": "completed",
                        },
                    },
                ],
            )
            write_jsonl(
                child,
                [
                    {"timestamp": "2026-04-22T00:00:04Z", "type": "session_meta", "payload": {"id": "child-thread", "originator": "codex", "cwd": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:04Z", "type": "session_meta", "payload": {"id": "parent-thread", "originator": "codex", "cwd": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:05Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "done", "phase": "final"}},
                ],
            )

            store = atv.TraceStore([parent, child], eager_index=True)
            parent_session = next(session for session in store.sessions if Path(session.path).name == "parent.jsonl")
            payload = store.session_payload(parent_session.id)

            self.assertTrue(any(event["kind"] == "tool_call" for event in payload["events"]))
            self.assertTrue(any(event["kind"] == "tool_result" for event in payload["events"]))
            tool_rels = [rel for rel in store.relationships.values() if rel.kind == "tool_call"]
            self.assertEqual(len(tool_rels), 1)
            subagent_rels = [rel for rel in store.relationships.values() if rel.kind == "subagent_spawn"]
            self.assertEqual(len(subagent_rels), 1)
            self.assertIsNotNone(subagent_rels[0].target_session)
            self.assertEqual(subagent_rels[0].metadata["new_thread_id"], "child-thread")
            child_session = store.sessions_by_id[subagent_rels[0].target_session]
            self.assertEqual(child_session.kind, "codex_subagent")
            self.assertEqual(child_session.parent_session, parent_session.id)
            self.assertEqual(child_session.thread_id, "child-thread")
            self.assertEqual(store.thread_to_session["parent-thread"], parent_session.id)

    def test_claude_parent_and_subagent_relationships(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / ".claude" / "projects" / "proj"
            parent = project / "session-1.jsonl"
            sub = project / "session-1" / "subagents" / "agent-a1.jsonl"
            write_jsonl(
                parent,
                [
                    {
                        "type": "assistant",
                        "sessionId": "session-1",
                        "uuid": "assistant-1",
                        "timestamp": "2026-04-22T00:00:00Z",
                        "cwd": "/repo",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "tool_use", "id": "tool-1", "name": "Task", "input": {"prompt": "inspect"}}],
                            "usage": {"input_tokens": 2, "output_tokens": 3},
                        },
                    }
                ],
            )
            write_jsonl(
                sub,
                [
                    {
                        "type": "progress",
                        "sessionId": "session-1",
                        "agentId": "a1",
                        "uuid": "progress-1",
                        "timestamp": "2026-04-22T00:00:01Z",
                        "parentUuid": "assistant-1",
                        "parentToolUseID": "tool-1",
                        "toolUseID": "tool-1",
                        "data": {"status": "running"},
                    },
                    {
                        "type": "assistant",
                        "sessionId": "session-1",
                        "agentId": "a1",
                        "uuid": "assistant-sub",
                        "timestamp": "2026-04-22T00:00:02Z",
                        "parentUuid": "progress-1",
                        "sourceToolAssistantUUID": "assistant-1",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "sub result"}]},
                    },
                ],
            )

            store = atv.TraceStore([parent, sub], eager_index=True)
            parent_session = next(session for session in store.sessions if Path(session.path).name == "session-1.jsonl")
            parent_payload = store.session_payload(parent_session.id)
            self.assertEqual(parent_payload["events"][0]["raw_type"], "assistant.tool_use")
            self.assertEqual(parent_payload["events"][0]["category"], "tool")
            sub_session = next(session for session in store.sessions if session.kind == "claude_subagent")
            self.assertEqual(sub_session.parent_session, parent_session.id)
            self.assertTrue(any(rel.kind == "subagent_spawn" and rel.target_session == sub_session.id for rel in store.relationships.values()))
            self.assertTrue(any(rel.kind == "handoff" and rel.target_session == sub_session.id for rel in store.relationships.values()))
            related = store.related_relationships(sub_session.id)
            self.assertEqual(related[0].kind, "subagent_spawn")

    def test_team_grouping_is_not_subagent_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".codex" / "sessions"
            one = root / "team-one.jsonl"
            two = root / "team-two.jsonl"
            for idx, path in enumerate((one, two), 1):
                write_jsonl(
                    path,
                    [
                        {"timestamp": "2026-04-22T00:00:00Z", "type": "session_meta", "payload": {"id": f"team-{idx}", "originator": "codex", "cwd": "/repo"}},
                        {"timestamp": "2026-04-22T00:00:01Z", "type": "turn_context", "payload": {"type": "task_started", "collaboration_mode_kind": "team", "turn_id": f"turn-{idx}"}},
                    ],
                )

            store = atv.TraceStore([one, two], eager_index=True)
            self.assertTrue(all(session.team_id for session in store.sessions))
            self.assertTrue(any(rel.kind == "team_member" for rel in store.relationships.values()))
            self.assertFalse(any(session.kind.endswith("subagent") for session in store.sessions))
            self.assertFalse(any(rel.kind == "subagent_spawn" for rel in store.relationships.values()))

    def test_subagent_role_worker_does_not_imply_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".codex" / "sessions"
            parent = root / "parent.jsonl"
            child = root / "child.jsonl"
            write_jsonl(
                parent,
                [
                    {"timestamp": "2026-04-22T00:00:00Z", "type": "session_meta", "payload": {"id": "parent", "originator": "codex", "cwd": "/repo"}},
                    {
                        "timestamp": "2026-04-22T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "collab_agent_spawn_end",
                            "call_id": "spawn-worker",
                            "sender_thread_id": "parent",
                            "new_thread_id": "child",
                            "new_agent_nickname": "Worker",
                            "new_agent_role": "worker",
                            "status": "completed",
                        },
                    },
                ],
            )
            write_jsonl(
                child,
                [
                    {"timestamp": "2026-04-22T00:00:02Z", "type": "session_meta", "payload": {"id": "child", "originator": "codex", "cwd": "/repo"}},
                ],
            )

            store = atv.TraceStore([parent, child], eager_index=False)
            parent_session = next(session for session in store.sessions if Path(session.path).name == "parent.jsonl")
            child_session = next(session for session in store.sessions if Path(session.path).name == "child.jsonl")
            self.assertEqual(parent_session.kind, "main")
            self.assertEqual(child_session.kind, "codex_subagent")
            self.assertIsNone(parent_session.team_id)

    def test_event_classification_and_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".codex" / "sessions" / "trace.jsonl"
            write_jsonl(
                path,
                [
                    {"timestamp": "2026-04-22T00:00:00Z", "type": "session_meta", "payload": {"id": "thread", "originator": "codex", "cwd": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:01Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "hello", "phase": "final"}},
                    {"timestamp": "2026-04-22T00:00:02Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "think"}]}},
                    {"timestamp": "2026-04-22T00:00:03Z", "type": "response_item", "payload": {"type": "web_search_call", "call_id": "web-1", "query": "docs"}},
                    {"timestamp": "2026-04-22T00:00:04Z", "type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "ok-1", "exit_code": 0, "command": ["pwd"], "stdout": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:05Z", "type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "bad-1", "exit_code": 2, "command": ["false"], "stderr": "bad"}},
                    {"timestamp": "2026-04-22T00:00:06Z", "type": "event_msg", "payload": {"type": "patch_apply_end", "call_id": "patch-1", "success": True, "changes": []}},
                    {"timestamp": "2026-04-22T00:00:07Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 12}}}},
                ],
            )

            store = atv.TraceStore([path], eager_index=True)
            payload = store.session_payload(store.sessions[0].id)
            by_raw = {event["raw_type"]: event for event in payload["events"]}

            self.assertEqual(by_raw["agent_message"]["category"], "conversation")
            self.assertEqual(by_raw["reasoning"]["category"], "reasoning")
            self.assertEqual(by_raw["web_search_call"]["category"], "search")
            self.assertEqual(by_raw["patch_apply_end"]["category"], "patch")
            self.assertEqual(by_raw["token_count"]["category"], "token")
            shell_events = [event for event in payload["events"] if event["raw_type"] == "exec_command_end"]
            self.assertEqual(shell_events[0]["category"], "shell")
            self.assertEqual(shell_events[1]["category"], "error")
            self.assertTrue(all(event["icon"] and event["category_label"] for event in payload["events"]))
            self.assertEqual(payload["session"]["category_counts"]["error"], 1)

    def test_communication_graph_infers_single_file_subagent_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".codex" / "sessions" / "trace.jsonl"
            write_jsonl(
                path,
                [
                    {"timestamp": "2026-04-22T00:00:00Z", "type": "session_meta", "payload": {"id": "parent", "originator": "codex", "cwd": "/repo"}},
                    {"timestamp": "2026-04-22T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "coordinate agents"}},
                    {
                        "timestamp": "2026-04-22T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "collab_agent_spawn_end",
                            "call_id": "spawn-1",
                            "sender_thread_id": "parent",
                            "new_thread_id": "child",
                            "new_agent_nickname": "Verifier",
                            "new_agent_role": "verifier",
                            "status": "pending_init",
                        },
                    },
                    {
                        "timestamp": "2026-04-22T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "collab_close_end",
                            "call_id": "close-1",
                            "sender_thread_id": "parent",
                            "receiver_thread_id": "child",
                            "receiver_agent_nickname": "Verifier",
                            "receiver_agent_role": "verifier",
                            "status": {"completed": "done"},
                        },
                    },
                ],
            )

            store = atv.TraceStore([path], eager_index=True)
            payload = store.session_payload(store.sessions[0].id)
            graph = payload["communication_graph"]
            mermaid = graph["mermaid"]
            diagrams = {diagram["id"]: diagram for diagram in graph["diagrams"]}

            self.assertIn("sequenceDiagram", mermaid)
            self.assertIn("participant main as Main", mermaid)
            self.assertIn("Verifier", mermaid)
            self.assertIn("verifier", mermaid)
            self.assertIn("completed", mermaid)
            self.assertEqual(diagrams["sequence"]["mermaid"], mermaid)
            self.assertIn("flowchart", diagrams["topology"]["mermaid"])
            self.assertIn("stateDiagram-v2", diagrams["state"]["mermaid"])
            self.assertIn("timeline", diagrams["timeline"]["mermaid"])
            self.assertIn("pie showData", diagrams["pie"]["mermaid"])
            self.assertIn("mindmap", diagrams["mindmap"]["mermaid"])
            self.assertIn("journey", diagrams["journey"]["mermaid"])
            self.assertGreaterEqual(len(graph["review"]), 4)
            self.assertEqual(graph["stats"]["subagent_spawns"], 1)
            self.assertEqual(graph["stats"]["handoffs"], 1)
            self.assertTrue(any(event["kind"] == "handoff" for event in payload["events"]))

    def test_malformed_json_and_preview_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".codex" / "sessions" / "trace.jsonl"
            long_text = "x" * 120
            write_jsonl(
                path,
                [
                    "{bad json",
                    {"timestamp": "2026-04-22T00:00:00Z", "type": "event_msg", "payload": {"type": "agent_message", "message": long_text, "phase": "final"}},
                ],
            )
            store = atv.TraceStore([path], max_preview_chars=20, eager_index=True)
            session = store.sessions[0]
            payload = store.session_payload(session.id)
            self.assertGreaterEqual(session.parse_errors, 1)
            self.assertLessEqual(len(payload["events"][0]["preview"]), 20)
            self.assertTrue(payload["events"][0]["preview"].endswith("..."))

    def test_lazy_session_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude" / "projects" / "proj" / "session.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "sessionId": "session",
                        "timestamp": "2026-04-22T00:00:00Z",
                        "message": {"role": "user", "content": "hello"},
                    }
                ],
            )
            store = atv.TraceStore([path], eager_index=False)
            self.assertFalse(store.sessions[0].parsed)
            sessions_payload = store.sessions_payload()
            self.assertFalse(sessions_payload["sessions"][0]["parsed"])
            payload = store.session_payload(store.sessions[0].id)
            self.assertTrue(payload["session"]["parsed"])
            self.assertEqual(payload["events"][0]["kind"], "message")


if __name__ == "__main__":
    unittest.main()
