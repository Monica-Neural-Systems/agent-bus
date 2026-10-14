#!/usr/bin/env python3
"""Tests for mesh/handoff.py — agent-to-agent task handoffs."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from handoff import create_handoff, validate_handoff, append_to_chain, TaskContext


def test_create_handoff():
    h = create_handoff("task-123", "owen", "claude",
                       {"text": "write draft"}, note="needs writer")
    assert h["task_id"] == "task-123"
    assert h["handoff_from"] == "owen"
    assert h["handoff_to"] == "claude"
    assert h["destination"] == "claude"
    assert len(h["handoff_chain"]) == 1
    assert h["handoff_chain"][0]["note"] == "needs writer"
    print("PASS test_create_handoff")


def test_validate_handoff():
    h = create_handoff("t1", "owen", "claude", {"x": 1})
    ok, msg = validate_handoff(h)
    assert ok, msg

    bad = dict(h)
    del bad["payload"]
    ok, msg = validate_handoff(bad)
    assert not ok
    print("PASS test_validate_handoff")


def test_self_handoff_rejected():
    h = create_handoff("t1", "owen", "owen", {"x": 1})
    ok, msg = validate_handoff(h)
    assert not ok
    assert "self-handoff" in msg
    print("PASS test_self_handoff_rejected")


def test_chain_extension():
    h = create_handoff("t1", "owen", "claude", {"x": 1})
    h2 = append_to_chain(h, "claude", "argon", "needs final edit")
    assert len(h2["handoff_chain"]) == 2
    assert h2["destination"] == "argon"
    assert h2["handoff_chain"][1]["from"] == "claude"
    print("PASS test_chain_extension")


def test_task_context():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = TaskContext(tmp, "task-abc")
        ctx.append_message("owen", "starting work")
        ctx.add_file("claude", "mesh/tasks/task-abc/draft.md", "first draft")
        ctx.record_decision("argon", "approved", "logic is sound")
        ctx.set_agent_output("claude", "mesh/tasks/task-abc/draft.md")

        loaded = ctx.load()
        assert len(loaded["messages"]) == 1
        assert len(loaded["files"]) == 1
        assert len(loaded["decisions"]) == 1
        assert "claude" in loaded["agent_outputs"]
    print("PASS test_task_context")


if __name__ == "__main__":
    test_create_handoff()
    test_validate_handoff()
    test_self_handoff_rejected()
    test_chain_extension()
    test_task_context()
    print("All handoff tests passed")
