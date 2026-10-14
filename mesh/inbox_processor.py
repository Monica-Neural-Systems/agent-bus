#!/usr/bin/env python3
"""Mesh inbox processor — GitHub Actions consumer for agent-bus.

Scans inbox/*/ for new Telegram envelopes, validates them, tracks processed
IDs to prevent duplicates, and delivers outbox/michael/ results via Telegram.

Safety properties:
- Never deletes or moves inbox files; agents (or Owen's wire checks) own
  their inbox lifecycle. The processor only tracks seen IDs.
- Idempotent: re-running is safe (processed IDs tracked in mesh/processed.json).
- Delivery retried independently of execution; a delivery retry never
  re-executes the task.
- Failed validation is logged; the file stays in place for manual review.

Usage (in GitHub Actions):
  python3 mesh/inbox_processor.py process   # scan inboxes, mark processed
  python3 mesh/inbox_processor.py deliver   # send outbox via Telegram
  python3 mesh/inbox_processor.py route     # route unrouted messages to agents
  python3 mesh/inbox_processor.py handoff   # process agent-to-agent handoffs

Env for deliver:
  TELEGRAM_BOT_TOKEN : bot token (GitHub Secret)
  TELEGRAM_CHAT_ID   : Michael's chat id (GitHub Secret)
"""
import json
import os
import shutil
import sys
import urllib.request

# Import routing and handoff modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from router import parse_command, build_routing_envelope, should_require_approval
from handoff import create_handoff, validate_handoff, TaskContext

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(ROOT, "inbox")
OUTBOX = os.path.join(ROOT, "outbox", "michael")
PROCESSED = os.path.join(ROOT, "mesh", "processed.json")

REQUIRED_ENVELOPE_KEYS = {"envelope_id", "channel", "payload"}


def load_processed():
    if os.path.exists(PROCESSED):
        with open(PROCESSED) as f:
            return json.load(f)
    return {"processed_ids": [], "delivered_ids": []}


def save_processed(state):
    os.makedirs(os.path.dirname(PROCESSED), exist_ok=True)
    with open(PROCESSED, "w") as f:
        json.dump(state, f, indent=1)


def scan_inbox():
    """Yield (agent, path, eid, envelope) for unprocessed inbox JSON files."""
    state = load_processed()
    seen = set(state["processed_ids"])
    if not os.path.isdir(INBOX):
        return
    for agent in sorted(os.listdir(INBOX)):
        adir = os.path.join(INBOX, agent)
        if not os.path.isdir(adir):
            continue
        for name in sorted(os.listdir(adir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(adir, name)
            try:
                with open(path) as f:
                    env = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"SKIP {agent}/{name}: unreadable ({e}) — left in place")
                continue
            eid = env.get("envelope_id") or name.replace(".json", "")
            if eid in seen:
                continue
            yield agent, path, eid, env


def validate_envelope(eid, env):
    missing = REQUIRED_ENVELOPE_KEYS - set(env.keys())
    if missing:
        return False, f"missing keys: {sorted(missing)}"
    return True, "ok"


def cmd_process():
    state = load_processed()
    seen = set(state["processed_ids"])
    processed = 0
    failed = 0
    for agent, path, eid, env in scan_inbox():
        ok, msg = validate_envelope(eid, env)
        if not ok:
            print(f"FAIL {eid}: {msg} — left in inbox for manual review")
            failed += 1
            continue
        # Track as seen. Do NOT move the file: the owning agent (or Owen's
        # wire check) owns inbox lifecycle. Moving another agent's mail
        # would break their polling.
        seen.add(eid)
        processed += 1
        print(f"SEEN {eid} from {agent} ({msg})")
    state["processed_ids"] = sorted(seen)
    save_processed(state)
    print(f"process complete: {processed} seen, {failed} failed")
    return 1 if failed else 0


def telegram_send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("ok", False)


def cmd_deliver():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("SKIP deliver: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return 0
    state = load_processed()
    delivered = set(state["delivered_ids"])
    sent = 0
    if not os.path.isdir(OUTBOX):
        print("SKIP deliver: no outbox dir")
        return 0
    for name in sorted(os.listdir(OUTBOX)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(OUTBOX, name)
        with open(path) as f:
            env = json.load(f)
        eid = env.get("task_id") or name.replace(".json", "")
        if eid in delivered:
            continue
        text = env.get("reply_text") or json.dumps(env, indent=1)[:4000]
        try:
            ok = telegram_send(token, chat_id, f"[{eid}]\n{text}")
        except Exception as e:  # noqa: BLE001 — delivery must not crash the run
            print(f"DELIVERY FAIL {eid}: {e} — left in outbox for retry")
            continue
        if ok:
            delivered.add(eid)
            # Archive the delivered envelope; delivery tracked separately.
            done_dir = os.path.join(OUTBOX, "done")
            os.makedirs(done_dir, exist_ok=True)
            shutil.move(path, os.path.join(done_dir, name))
            sent += 1
            print(f"DELIVERED {eid} via Telegram")
        else:
            print(f"DELIVERY FAIL {eid}: telegram API returned not-ok — retry later")
    state["delivered_ids"] = sorted(delivered)
    save_processed(state)
    print(f"deliver complete: {sent} sent")
    return 0


def cmd_route():
    """Route unrouted Telegram messages to the correct agent inbox.

    Scans inbox/owen/ for messages with explicit /agent commands or @mentions,
    and writes routing envelopes to the target agent's inbox.
    Idempotent: already-routed message IDs are tracked.
    """
    state = load_processed()
    routed = set(state.get("routed_ids", []))
    count = 0

    owen_inbox = os.path.join(INBOX, "owen")
    if not os.path.isdir(owen_inbox):
        print("SKIP route: no owen inbox")
        return 0

    for name in sorted(os.listdir(owen_inbox)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(owen_inbox, name)
        try:
            with open(path) as f:
                env = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        eid = env.get("envelope_id") or name.replace(".json", "")
        if eid in routed:
            continue

        # Only route if not already routed to a specific agent
        if env.get("destination", "owen") != "owen":
            routed.add(eid)
            continue

        text = env.get("payload", {}).get("text", "")
        agent, task_text, is_command, dial = parse_command(text)

        # Only route explicit commands; dials and plain text stay with Owen
        if not is_command or agent == "owen":
            routed.add(eid)
            continue

        # Build routing envelope
        task_id = eid  # preserve original task ID for correlation
        source_msg = {
            "chat_id": env.get("source_metadata", {}).get("chat_id", ""),
            "message_id": env.get("source_metadata", {}).get("message_id"),
            "timestamp": env.get("created_at"),
        }
        photo_ref = env.get("payload", {}).get("photo")

        routing_env = build_routing_envelope(
            task_id, agent, task_text, source_msg, photo_ref
        )
        routing_env["approval_required"] = should_require_approval(task_text, agent)

        # Write to target agent's inbox
        target_dir = os.path.join(INBOX, agent)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, f"{task_id}.json")
        # Don't overwrite if already exists (idempotent)
        if not os.path.exists(target_path):
            with open(target_path, "w") as f:
                json.dump(routing_env, f, indent=1)
            print(f"ROUTED {task_id} → {agent} ({task_text[:50]})")
            count += 1

        routed.add(eid)

    state["routed_ids"] = sorted(routed)
    save_processed(state)
    print(f"route complete: {count} routed")
    return 0


def cmd_handoff():
    """Process agent-to-agent handoff requests.

    Scans for handoff request files in mesh/handoffs/pending/ and writes
    handoff envelopes to target agent inboxes. Tracks processed handoffs.
    """
    state = load_processed()
    done_handoffs = set(state.get("handoff_ids", []))
    count = 0

    pending_dir = os.path.join(ROOT, "mesh", "handoffs", "pending")
    if not os.path.isdir(pending_dir):
        print("SKIP handoff: no pending dir")
        return 0

    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(pending_dir, name)
        try:
            with open(path) as f:
                req = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"SKIP handoff {name}: unreadable ({e})")
            continue

        hid = req.get("handoff_id") or name.replace(".json", "")
        if hid in done_handoffs:
            continue

        # Validate required fields
        for field in ("task_id", "from_agent", "to_agent", "payload"):
            if field not in req:
                print(f"FAIL handoff {hid}: missing {field}")
                continue

        handoff_env = create_handoff(
            req["task_id"],
            req["from_agent"],
            req["to_agent"],
            req["payload"],
            context_ref=req.get("context_ref"),
            note=req.get("note", ""),
        )

        ok, msg = validate_handoff(handoff_env)
        if not ok:
            print(f"FAIL handoff {hid}: {msg}")
            continue

        # Write to target inbox
        target_dir = os.path.join(INBOX, req["to_agent"])
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, f"{handoff_env['envelope_id']}.json")
        if not os.path.exists(target_path):
            with open(target_path, "w") as f:
                json.dump(handoff_env, f, indent=1)
            print(f"HANDOFF {hid}: {req['from_agent']} → {req['to_agent']}")
            count += 1

        # Archive the request
        done_dir = os.path.join(ROOT, "mesh", "handoffs", "done")
        os.makedirs(done_dir, exist_ok=True)
        shutil.move(path, os.path.join(done_dir, name))
        done_handoffs.add(hid)

    state["handoff_ids"] = sorted(done_handoffs)
    save_processed(state)
    print(f"handoff complete: {count} processed")
    return 0


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("process", "deliver", "route", "handoff"):
        print("usage: inbox_processor.py [process|deliver|route|handoff]")
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "process":
        sys.exit(cmd_process())
    elif cmd == "deliver":
        sys.exit(cmd_deliver())
    elif cmd == "route":
        sys.exit(cmd_route())
    elif cmd == "handoff":
        sys.exit(cmd_handoff())


if __name__ == "__main__":
    main()
