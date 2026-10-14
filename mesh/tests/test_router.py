#!/usr/bin/env python3
"""Tests for mesh/router.py — Telegram command parsing and agent routing."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from router import parse_command, canonical_agent, should_require_approval, build_routing_envelope


def test_explicit_commands():
    agent, task, is_cmd, dial = parse_command("/claude write a draft")
    assert agent == "claude", f"got {agent}"
    assert task == "write a draft"
    assert is_cmd is True
    assert dial is None
    print("PASS test_explicit_commands")


def test_mention_style():
    agent, task, is_cmd, dial = parse_command("@argon audit this")
    assert agent == "argon"
    assert task == "audit this"
    assert is_cmd is True
    print("PASS test_mention_style")


def test_dial_number():
    agent, task, is_cmd, dial = parse_command("3")
    assert dial == "3"
    assert is_cmd is False
    print("PASS test_dial_number")


def test_plain_text_defaults_owen():
    agent, task, is_cmd, dial = parse_command("hello world")
    assert agent == "owen"
    assert task == "hello world"
    assert is_cmd is False
    print("PASS test_plain_text_defaults_owen")


def test_spark_alias():
    assert canonical_agent("spark") == "argon"
    assert canonical_agent("claude") == "claude"
    print("PASS test_spark_alias")


def test_approval_triggers():
    assert should_require_approval("send email to john", "owen") is True
    assert should_require_approval("post to linkedin", "claude") is True
    assert should_require_approval("write a draft", "owen") is False
    print("PASS test_approval_triggers")


def test_routing_envelope():
    env = build_routing_envelope(
        "tg-123-456", "claude", "write draft",
        {"chat_id": "123", "message_id": 456, "timestamp": "2026-10-14T00:00:00Z"},
    )
    assert env["envelope_id"] == "tg-123-456"
    assert env["destination"] == "claude"
    assert env["payload"]["text"] == "write draft"
    print("PASS test_routing_envelope")


if __name__ == "__main__":
    test_explicit_commands()
    test_mention_style()
    test_dial_number()
    test_plain_text_defaults_owen()
    test_spark_alias()
    test_approval_triggers()
    test_routing_envelope()
    print("All router tests passed")
