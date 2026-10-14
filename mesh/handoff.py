#!/usr/bin/env python3
"""Agent-to-agent task handoffs via GitHub inbox infrastructure.

An agent hands off work by writing a handoff envelope to the target agent's
inbox. The handoff chain is preserved for audit. Task context accumulates
in mesh/tasks/{task_id}/.

Handoff envelope structure:
{
    "envelope_id": "handoff-{source_task_id}-{timestamp}",
    "task_id": "{original_task_id}",  # preserved for correlation
    "handoff_from": "{source_agent}",
    "handoff_to": "{target_agent}",
    "handoff_chain": [...],  # full chain for audit
    "payload": {...},  # task data
    "task_context_ref": "mesh/tasks/{task_id}/context.json",
    "status": "HANDOFF_PENDING"
}

This module is pure logic for testability.
"""
import json
import os
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def create_handoff(source_task_id, from_agent, to_agent, payload,
                   context_ref=None, note=""):
    """Create a handoff envelope from one agent to another."""
    timestamp = utc_now()
    # Deterministic enough for dedup, unique enough for ordering
    envelope_id = f"handoff-{source_task_id}-{from_agent}-to-{to_agent}"

    return {
        "envelope_id": envelope_id,
        "task_id": source_task_id,  # original task ID preserved
        "channel": "agent-handoff",
        "from": from_agent,
        "destination": to_agent,
        "handoff_from": from_agent,
        "handoff_to": to_agent,
        "handoff_chain": [
            {
                "from": from_agent,
                "to": to_agent,
                "at": timestamp,
                "note": note,
            }
        ],
        "created_at": timestamp,
        "payload": payload,
        "task_context_ref": context_ref or f"mesh/tasks/{source_task_id}/context.json",
        "approval_required": False,
        "status": "HANDOFF_PENDING",
    }


def append_to_chain(existing_handoff, from_agent, to_agent, note=""):
    """Extend an existing handoff chain (multi-hop)."""
    new_entry = {
        "from": from_agent,
        "to": to_agent,
        "at": utc_now(),
        "note": note,
    }
    chain = existing_handoff.get("handoff_chain", []) + [new_entry]
    existing_handoff["handoff_chain"] = chain
    existing_handoff["handoff_from"] = from_agent
    existing_handoff["handoff_to"] = to_agent
    existing_handoff["destination"] = to_agent
    return existing_handoff


def validate_handoff(envelope):
    """Validate a handoff envelope has required fields."""
    required = {"envelope_id", "task_id", "handoff_from", "handoff_to", "payload"}
    missing = required - set(envelope.keys())
    if missing:
        return False, f"missing: {sorted(missing)}"
    if envelope.get("handoff_from") == envelope.get("handoff_to"):
        return False, "handoff_from == handoff_to (self-handoff not allowed)"
    return True, "ok"


class TaskContext:
    """Accumulating task context in mesh/tasks/{task_id}/.

    Stores: messages, files, decisions, agent outputs.
    Each agent appends; nothing is overwritten.
    """

    def __init__(self, root, task_id):
        self.task_dir = os.path.join(root, "mesh", "tasks", task_id)
        self.context_path = os.path.join(self.task_dir, "context.json")

    def load(self):
        if os.path.exists(self.context_path):
            with open(self.context_path) as f:
                return json.load(f)
        return {
            "task_id": os.path.basename(self.task_dir),
            "created_at": utc_now(),
            "messages": [],
            "files": [],
            "decisions": [],
            "agent_outputs": {},
        }

    def save(self, context):
        os.makedirs(self.task_dir, exist_ok=True)
        with open(self.context_path, "w") as f:
            json.dump(context, f, indent=1)

    def append_message(self, agent, text, msg_type="note"):
        ctx = self.load()
        ctx["messages"].append({
            "agent": agent,
            "text": text,
            "type": msg_type,
            "at": utc_now(),
        })
        self.save(ctx)
        return ctx

    def add_file(self, agent, file_path, description=""):
        ctx = self.load()
        ctx["files"].append({
            "agent": agent,
            "path": file_path,
            "description": description,
            "at": utc_now(),
        })
        self.save(ctx)
        return ctx

    def record_decision(self, agent, decision, rationale=""):
        ctx = self.load()
        ctx["decisions"].append({
            "agent": agent,
            "decision": decision,
            "rationale": rationale,
            "at": utc_now(),
        })
        self.save(ctx)
        return ctx

    def set_agent_output(self, agent, output_ref):
        ctx = self.load()
        ctx["agent_outputs"][agent] = {
            "ref": output_ref,
            "at": utc_now(),
        }
        self.save(ctx)
        return ctx
