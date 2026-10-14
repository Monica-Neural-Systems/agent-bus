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

Env for deliver:
  TELEGRAM_BOT_TOKEN : bot token (GitHub Secret)
  TELEGRAM_CHAT_ID   : Michael's chat id (GitHub Secret)
"""
import json
import os
import shutil
import sys
import urllib.request

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


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("process", "deliver"):
        print("usage: inbox_processor.py [process|deliver]")
        sys.exit(2)
    sys.exit(cmd_process() if sys.argv[1] == "process" else cmd_deliver())


if __name__ == "__main__":
    main()
