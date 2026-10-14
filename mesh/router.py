#!/usr/bin/env python3
"""Telegram command router — parses Michael's messages and routes to agents.

Supported formats:
  /owen <task>          → route to Owen's inbox
  /claude <task>        → route to Claude's inbox
  /argon <task>         → route to Argon's inbox
  /gpt <task>           → route to GPT's inbox
  @owen <task>          → same (mention style)
  <dial 1-9>            → rebuttal workflow (existing, unchanged)
  <plain text>          → defaults to Owen (existing behavior)

Task IDs: deterministic from chat_id + message_id (matches Worker v2.1.0).
Thread context: reply_to_message_id links follow-ups to parent task.

This module is pure logic (no I/O) for testability. The processor calls it.
"""
import re

VALID_AGENTS = {"owen", "claude", "argon", "gpt", "spark", "openclaw"}

# Map legacy/alias names to canonical inbox
AGENT_ALIASES = {
    "spark": "argon",  # spark retired 2026-10-13, argon is canonical
}

COMMAND_RE = re.compile(r"^/(owen|claude|argon|gpt|spark|openclaw)\s*(.*)$", re.IGNORECASE | re.DOTALL)
MENTION_RE = re.compile(r"^@(owen|claude|argon|gpt|spark|openclaw)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
DIAL_RE = re.compile(r"^([1-9])$")


def canonical_agent(name):
    """Resolve alias to canonical inbox name."""
    name = name.lower().strip()
    return AGENT_ALIASES.get(name, name)


def parse_command(text):
    """Parse a Telegram message into (agent, task_text, is_command).

    Returns:
        (agent: str, task_text: str, is_command: bool, dial: str|None)
    """
    text = (text or "").strip()
    if not text:
        return "owen", "", False, None

    # Explicit /agent command
    m = COMMAND_RE.match(text)
    if m:
        agent = canonical_agent(m.group(1))
        task = m.group(2).strip()
        return agent, task, True, None

    # @mention style
    m = MENTION_RE.match(text)
    if m:
        agent = canonical_agent(m.group(1))
        task = m.group(2).strip()
        return agent, task, True, None

    # Dial number (rebuttal workflow — existing behavior preserved)
    m = DIAL_RE.match(text)
    if m:
        return "owen", text, False, m.group(1)

    # Default: Owen handles it
    return "owen", text, False, None


def build_routing_envelope(task_id, agent, task_text, source_msg, photo_ref=None):
    """Build a routing envelope for cross-agent delivery.

    source_msg: dict with chat_id, message_id, timestamp from Telegram
    photo_ref: dict with file info if message had photo (from Worker hydration)
    """
    return {
        "envelope_id": task_id,
        "task_id": task_id,
        "channel": "telegram",
        "from": "michael",
        "destination": agent,
        "routed_by": "mesh-router",
        "is_command": True,
        "created_at": source_msg.get("timestamp"),
        "source_metadata": {
            "chat_id": str(source_msg.get("chat_id", "")),
            "message_id": source_msg.get("message_id"),
            "reply_to_message_id": source_msg.get("reply_to_message_id"),
        },
        "payload": {
            "text": task_text,
            "photo": photo_ref,
        },
        "handoff_chain": [],
        "approval_required": False,
        "status": "ROUTED",
    }


def should_require_approval(task_text, agent):
    """Determine if a task needs Michael's exact-text approval before external action.

    Approval required for: sending emails, posting to social, spending money,
    deleting data, or any irreversible external action.
    """
    text_lower = (task_text or "").lower()
    triggers = [
        "send email", "send an email", "email ",
        "post to", "post on", "publish",
        "spend", "buy ", "purchase", "pay ",
        "delete", "remove ",
    ]
    return any(t in text_lower for t in triggers)
