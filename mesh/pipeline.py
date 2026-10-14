#!/usr/bin/env python3
"""Mesh wire pipeline runner. One canonical workflow, nine invariant stages.

Usage:
  pipeline.py ingest --adapter chat_upload --image shot.png --intent "..." [--dial 1] [--task-type screenshot_rebuttal]
  pipeline.py advance <task_id>            # run the next stage for a live task
  pipeline.py collect <task_id> --agent claude --result results/claude.json
  pipeline.py mark-unavailable <task_id> --agent argon --note "quota hit"
  pipeline.py mark-available <task_id> --agent argon
  pipeline.py approve <task_id> --gate argon_editorial --by argon --text approved.txt
  pipeline.py deliver <task_id> --channel buffer
  pipeline.py confirm-delivery <task_id> --outcome DELIVERED --note "buffer id 123"
  pipeline.py status <task_id>
"""
import argparse, hashlib, json, os, shutil, sys, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # agent-bus/
MESH = os.path.join(ROOT, "mesh")
TASKS = os.path.join(MESH, "tasks")
ARCHIVE = os.path.join(MESH, "archive")
REGISTRY = os.path.join(MESH, "registry.json")
INBOX = os.path.join(ROOT, "inbox")

STAGES = ["INGEST", "NORMALIZE", "DISPATCH", "COLLECT", "SYNTHESIZE",
          "VALIDATE", "AUTHORIZED_EXECUTION", "DELIVER", "ARCHIVE"]
# Canonical agent IDs. "argon" replaced "spark" (2026-10-13): the Spark brand was
# retired by Google; the agent continues as Gemini Tasks on Argon. "spark" is
# retained ONLY as a legacy alias for reading old envelopes and inbox files.
# Do not dispatch new work to "spark".
AGENTS = ["openclaw", "claude", "argon", "owen"]
LEGACY_ALIASES = {"spark": "argon"}
GATES = ["argon_editorial", "michael_send", "michael_buffer", "michael_spend"]
# Deprecated gate name, accepted for envelopes created before the argon migration.
# New approvals must use "argon_editorial".
LEGACY_GATES = {"spark_editorial": "argon_editorial"}

AGENT_ROLES = {
    "openclaw": "Windows host daemon: intake adapters, watched folders, Telegram alerts, local file ops.",
    "claude": "First-draft writer. Fast Response v8 drafts, business packages, epistemic challenges. "
              "TEMPORARY fallback final editor ONLY when argon is marked unavailable "
              "(result marked FINAL_EDIT_FALLBACK_CLAUDE; never imply argon reviewed it).",
    "argon": "Preferred final reasoning editor and rebuttal sharpener. Final editorial sign-off "
             "on ALL public text. (Formerly 'spark'; same agent, new canonical ID.)",
    "owen": "Intake, dispatch, orchestration, delivery, retries, audit. Research, source verification, "
            "legwork, evidence collection. Buffer queueing (post-approval only).",
}

TASK_TYPES = ["screenshot_rebuttal", "outreach_draft", "research", "tender_scout",
              "price_watch", "business_package", "epistemic_challenge", "other"]


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha1_text(s):
    return hashlib.sha1(canonical_text(s).encode("utf-8")).hexdigest()


def canonical_text(s):
    """Canonical text form for hashing and comparison.

    - Unicode string (decode bytes as UTF-8 if needed).
    - Line endings normalized: CRLF -> LF, lone CR -> LF.
    - No BOM. No trailing-whitespace stripping: "hello" and "hello\\n"
      are DIFFERENT texts and hash differently. This is intentional:
      byte-for-byte approval matching must not silently ignore newlines.
    """
    if isinstance(s, bytes):
        s = s.decode("utf-8")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if s.startswith("\ufeff"):
        s = s[1:]
    return s


def sha1_file_canonical(path):
    """SHA1 of a file's canonical text form. For approval text matching."""
    with open(path, "rb") as f:
        return sha1_text(f.read())


def set_editorial_state(env, new_state, by, note=""):
    """Auditable editorial state transition. Never silently discards tasks."""
    old = env.get("editorial_state", "ARGON_PENDING")
    valid = {
        "ARGON_PENDING": ["ARGON_UNAVAILABLE", "ARGON_APPROVED"],
        "ARGON_UNAVAILABLE": ["CLAUDE_FINAL_EDIT_FALLBACK", "ARGON_REVIEW_PENDING"],
        "CLAUDE_FINAL_EDIT_FALLBACK": ["ARGON_REVIEW_PENDING"],
        "ARGON_REVIEW_PENDING": ["ARGON_APPROVED", "ARGON_UNAVAILABLE"],
        "ARGON_APPROVED": ["ARGON_REVIEW_PENDING"],  # re-open if candidate changes
    }
    if new_state not in valid.get(old, []):
        raise SystemExit(
            f"invalid editorial transition {old} -> {new_state} "
            f"(allowed: {valid.get(old, [])})"
        )
    env["editorial_state"] = new_state
    env.setdefault("editorial_transitions", []).append(
        {"state": new_state, "from": old, "at": utcnow(), "by": by, "note": note}
    )
    audit(env, f"editorial: {old} -> {new_state} ({note})", by=by)


def load_registry():
    if os.path.exists(REGISTRY):
        with open(REGISTRY) as f:
            return json.load(f)
    return {"processed": []}


def save_registry(reg):
    with open(REGISTRY, "w") as f:
        json.dump(reg, f, indent=1)


def task_dir(task_id):
    return os.path.join(TASKS, task_id)


def load_envelope(task_id):
    for base in (TASKS, ARCHIVE):
        p = os.path.join(base, task_id, "envelope.json")
        if os.path.exists(p):
            with open(p) as f:
                env = json.load(f)
            env["_archived"] = (base == ARCHIVE)
            return env
    raise SystemExit(f"unknown task {task_id}")


def save_envelope(env):
    base = ARCHIVE if env.get("_archived") else TASKS
    p = os.path.join(base, env["task_id"], "envelope.json")
    clean = {k: v for k, v in env.items() if not k.startswith("_")}
    with open(p, "w") as f:
        json.dump(clean, f, indent=1)


def audit(env, line, by="pipeline"):
    env["audit"].append(f"[{utcnow()}] [{by}] {line}")


def set_stage(env, stage, by="pipeline", note=""):
    env["stage"] = stage
    env["stage_history"].append({"stage": stage, "at": utcnow(), "by": by, "note": note})
    audit(env, f"stage -> {stage}" + (f" ({note})" if note else ""), by)


def compute_task_id(adapter, evidence_key):
    minute = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M")
    raw = f"{adapter}|{evidence_key}|{minute}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------- INGEST / NORMALIZE ----------------

def cmd_ingest(args):
    ev_key = None
    image_sha1 = None
    if args.image:
        if not os.path.exists(args.image):
            raise SystemExit(f"image not found: {args.image}")
        image_sha1 = sha1_file(args.image)
        ev_key = f"img:{image_sha1}"
    elif args.text:
        ev_key = f"text:{sha1_text(args.text)}"
    elif args.url:
        ev_key = f"url:{args.url}"
    else:
        raise SystemExit("ingest needs --image, --text, or --url")

    task_id = compute_task_id(args.adapter, ev_key)
    tdir = task_dir(task_id)
    # Idempotent ingest: never overwrite a live task or its evidence.
    # If this task_id already has an envelope, the input is a duplicate —
    # return the existing id without touching evidence or stage history.
    existing = os.path.join(tdir, "envelope.json")
    if os.path.exists(existing):
        print(f"duplicate ingest: task {task_id} already exists, not overwritten")
        return task_id
    os.makedirs(os.path.join(tdir, "evidence"), exist_ok=True)
    os.makedirs(os.path.join(tdir, "results"), exist_ok=True)

    image_rel = None
    if args.image:
        dest = os.path.join(tdir, "evidence", os.path.basename(args.image))
        shutil.copy2(args.image, dest)
        image_rel = os.path.relpath(dest, ROOT)

    env = {
        "envelope_version": "1.0",
        "task_id": task_id,
        "evidence_key": ev_key,
        "created_at": utcnow(),
        "source_adapter": args.adapter,
        "task_type": args.task_type,
        "user_intent": args.intent,
        "dial_command": args.dial,
        "evidence": {"image_path": image_rel, "image_sha1": image_sha1,
                     "text": args.text, "url": args.url},
        "stage": "INGEST",
        "stage_history": [{"stage": "INGEST", "at": utcnow(), "by": args.adapter, "note": "ingested"}],
        "dispatch": {a: {"role": AGENT_ROLES[a], "status": "pending",
                         "result_ref": None, "note": "", "updated_at": None} for a in AGENTS},
        "approvals": {g: {"status": "not_required", "by": None, "at": None, "text_sha1": None,
                          "fallback": False} for g in GATES},
        # Editorial state machine (auditable transitions):
        #   ARGON_PENDING -> ARGON_UNAVAILABLE -> CLAUDE_FINAL_EDIT_FALLBACK
        #       -> ARGON_REVIEW_PENDING -> ARGON_APPROVED
        # A quota/timeout failure must never silently discard the task or falsely
        # mark review complete. Claude fallback is recorded distinctly from argon review.
        # Argon resumption preserves task ID, evidence, candidate text, review history.
        "editorial_state": "ARGON_PENDING",
        "editorial_transitions": [
            {"state": "ARGON_PENDING", "at": utcnow(), "by": args.adapter,
             "note": "argon is the preferred final editor"}
        ],
        "result": {"synthesized": None, "delivery_channel": None, "delivered_at": None},
        "duplicate_of": None,
        "audit": [f"[{utcnow()}] [{args.adapter}] ingested: {args.intent[:120]}"],
    }
    save_envelope(env)
    print(f"ingested task {task_id} (stage INGEST)")
    return task_id


def cmd_normalize(env):
    reg = load_registry()
    ev_key = env.get("evidence_key")
    if env["task_id"] in reg["processed"] or (ev_key and ev_key in reg.get("evidence", [])):
        env["duplicate_of"] = env["task_id"]
        audit(env, "NORMALIZE: duplicate of processed task, archiving immediately")
        set_stage(env, "ARCHIVE", note="duplicate")
        save_envelope(env)
        print(f"duplicate of {env['task_id']} -> archived")
        return
    # task_type-specific gate defaults
    if env["task_type"] == "screenshot_rebuttal":
        env["approvals"]["argon_editorial"]["status"] = "pending"
        audit(env, "NORMALIZE: argon_editorial gate armed (public text task)")
    set_stage(env, "NORMALIZE", note="evidence hashed, gates armed")
    save_envelope(env)
    print(f"normalized {env['task_id']}")


# ---------------- DISPATCH ----------------

def cmd_dispatch(env):
    tdir = task_dir(env["task_id"])
    for agent in AGENTS:
        slot = env["dispatch"][agent]
        body = (f"# Task {env['task_id']}\n\n"
                f"**Type:** {env['task_type']}  \n"
                f"**Intent:** {env['user_intent']}  \n"
                f"**Dial:** {env['dial_command']}  \n"
                f"**Your role:** {slot['role']}  \n\n"
                f"Envelope: `mesh/tasks/{env['task_id']}/envelope.json`  \n"
                f"Evidence: `{env['evidence']['image_path'] or env['evidence']['text'] or env['evidence']['url']}`\n\n"
                f"Write your result to `mesh/tasks/{env['task_id']}/results/{agent}.json` "
                f"as {{\"agent\": \"{agent}\", \"status\": \"done\", \"payload\": {{...}}, \"note\": \"...\"}}.\n"
                f"If you cannot act, do NOT write a fake result — the collector records you unavailable.\n")
        dest = os.path.join(INBOX, agent, f"task-{env['task_id']}.md")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            f.write(body)
        slot["note"] = f"dispatched to inbox/{agent}/"
    set_stage(env, "DISPATCH", note=f"fanned out to {len(AGENTS)} agents")
    save_envelope(env)
    print(f"dispatched {env['task_id']}")


# ---------------- COLLECT ----------------

def cmd_collect(env, agent, result_path):
    # Legacy alias: results filed under "spark" are attributed to argon.
    agent = LEGACY_ALIASES.get(agent, agent)
    if agent not in AGENTS:
        raise SystemExit(f"unknown agent {agent}")
    with open(result_path) as f:
        res = json.load(f)
    tdir = task_dir(env["task_id"])
    dest = os.path.join(tdir, "results", f"{agent}.json")
    shutil.copy2(result_path, dest)
    slot = env["dispatch"][agent]
    slot["status"] = res.get("status", "done")
    slot["result_ref"] = os.path.relpath(dest, ROOT)
    slot["note"] = res.get("note", "")
    slot["updated_at"] = utcnow()
    audit(env, f"COLLECT: {agent} -> {slot['status']}: {slot['note'][:100]}")
    save_envelope(env)
    print(f"collected {agent} -> {slot['status']}")


def cmd_mark_unavailable(env, agent, note):
    agent = LEGACY_ALIASES.get(agent, agent)
    if agent not in AGENTS:
        raise SystemExit(f"unknown agent {agent}")
    slot = env["dispatch"][agent]
    slot["status"] = "unavailable"
    slot["note"] = note
    slot["updated_at"] = utcnow()
    audit(env, f"COLLECT: {agent} UNAVAILABLE ({note}) — recorded, not fabricated")
    # Argon going down drives the editorial state machine. The task is NOT
    # discarded; it waits for Claude fallback or argon resumption.
    if agent == "argon" and env.get("editorial_state") in ("ARGON_PENDING", "ARGON_REVIEW_PENDING"):
        set_editorial_state(env, "ARGON_UNAVAILABLE", by="pipeline",
                            note=f"argon unavailable: {note}")
    save_envelope(env)
    print(f"{agent} marked unavailable")


def cmd_mark_available(env, agent, note=""):
    """Argon (or any agent) is back. For argon, drives resumption:
    ARGON_UNAVAILABLE -> ARGON_REVIEW_PENDING, or
    CLAUDE_FINAL_EDIT_FALLBACK -> ARGON_REVIEW_PENDING.
    Original task ID, evidence, candidate text, and history are preserved."""
    agent = LEGACY_ALIASES.get(agent, agent)
    if agent not in AGENTS:
        raise SystemExit(f"unknown agent {agent}")
    slot = env["dispatch"][agent]
    slot["status"] = "pending"
    slot["note"] = note or "available again"
    slot["updated_at"] = utcnow()
    audit(env, f"{agent} marked available again ({slot['note']})")
    if agent == "argon" and env.get("editorial_state") in ("ARGON_UNAVAILABLE", "CLAUDE_FINAL_EDIT_FALLBACK"):
        set_editorial_state(env, "ARGON_REVIEW_PENDING", by="pipeline",
                            note="argon resumed; awaiting argon review of current candidate")
    save_envelope(env)
    print(f"{agent} marked available")


# ---------------- SYNTHESIZE ----------------

def cmd_synthesize(env):
    tdir = task_dir(env["task_id"])
    payloads = {}
    for agent in AGENTS:
        rp = os.path.join(tdir, "results", f"{agent}.json")
        if os.path.exists(rp):
            with open(rp) as f:
                payloads[agent] = json.load(f)
    synth = {"task_id": env["task_id"], "task_type": env["task_type"],
             "inputs": {a: payloads.get(a, {"status": env["dispatch"][a]["status"]}) for a in AGENTS}}
    # rebuttal: Claude's v8 draft is primary, others annotate
    if env["task_type"] == "screenshot_rebuttal" and "claude" in payloads:
        synth["primary_draft"] = payloads["claude"].get("payload")
        synth["annotations"] = {a: p.get("payload") for a, p in payloads.items() if a != "claude"}
    env["result"]["synthesized"] = synth
    set_stage(env, "SYNTHESIZE", by="owen", note=f"merged {len(payloads)} agent results")
    save_envelope(env)
    print(f"synthesized {env['task_id']}")


# ---------------- VALIDATE ----------------

def check_limits(text, channel):
    n = len(text)
    if channel == "x" and n >= 280:
        return False, f"X text {n} chars >= 280"
    if channel == "linkedin" and n >= 850:
        return False, f"LinkedIn text {n} chars >= 850"
    return True, "ok"


def check_gates(env):
    """Return list of gate problems. Used by VALIDATE and re-checked at EXECUTE."""
    problems = []
    needs_editorial = env["task_type"] in ("screenshot_rebuttal", "outreach_draft", "business_package")
    synth = env["result"]["synthesized"] or {}
    draft = (synth.get("primary_draft") or {}).get("text", "")
    # Canonical editorial gate; legacy spark_editorial accepted for old envelopes.
    ap = env["approvals"].get("argon_editorial") or env["approvals"].get("spark_editorial") or {}
    editorial_state = env.get("editorial_state", "ARGON_PENDING")
    if needs_editorial:
        if ap.get("status") != "approved":
            problems.append("argon_editorial not approved")
        elif draft and ap.get("text_sha1") != sha1_text(draft):
            # Candidate changed after approval -> invalidate, require re-approval
            # of the exact new text. This is enforced, not just warned.
            problems.append("approved text_sha1 does not match draft — candidate changed, re-approval required")
        elif ap.get("fallback") and editorial_state == "CLAUDE_FINAL_EDIT_FALLBACK":
            # Fallback approval is valid for proceeding, but flagged distinctly.
            pass  # no problem; the FINAL_EDIT_FALLBACK_CLAUDE marking travels with the result
    if draft:
        channel = env["result"]["delivery_channel"] or "linkedin"
        ok, msg = check_limits(draft, "x" if channel == "x" else "linkedin")
        if not ok:
            problems.append(f"char limit: {msg}")
    if env["result"]["delivery_channel"] == "buffer":
        if env["approvals"]["michael_buffer"]["status"] != "approved":
            problems.append("michael_buffer not approved")
    return problems


def cmd_validate(env):
    problems = check_gates(env)
    if problems:
        audit(env, "VALIDATE FAILED: " + "; ".join(problems))
        save_envelope(env)
        print("VALIDATE FAILED: " + "; ".join(problems))
        return False
    set_stage(env, "VALIDATE", note="all gates pass")
    save_envelope(env)
    print(f"validated {env['task_id']}")
    return True


# ---------------- APPROVE ----------------

def cmd_approve(env, gate, by, text_path):
    # Resolve legacy gate names (spark_editorial -> argon_editorial).
    original_gate = gate
    gate = LEGACY_GATES.get(gate, gate)
    if gate not in GATES:
        raise SystemExit(f"unknown gate {original_gate}")
    # Resolve legacy agent alias (spark -> argon) for reading old records,
    # but new approvals must use the canonical identity.
    by_canonical = LEGACY_ALIASES.get(by, by)
    if by != by_canonical:
        audit(env, f"APPROVAL: legacy identity '{by}' resolved to '{by_canonical}'", by=by)
    by = by_canonical

    # Editorial gate authorization rules. A bare --by string is NOT proof of
    # authorization; these rules enforce WHO may approve WHAT and WHEN.
    # (Human verification of the approver happens outside the pipeline.)
    editorial_state = env.get("editorial_state", "ARGON_PENDING")
    if gate == "argon_editorial":
        if by == "argon":
            pass  # canonical path
        elif by == "claude":
            # Fallback ONLY when argon is demonstrably unavailable.
            if editorial_state != "ARGON_UNAVAILABLE":
                raise SystemExit(
                    f"claude may approve editorial gate ONLY when argon is unavailable "
                    f"(editorial_state={editorial_state}). Mark argon unavailable first."
                )
        else:
            raise SystemExit(f"argon_editorial must come from argon (or claude as fallback), not {by}")
    if gate.startswith("michael_") and by != "michael":
        raise SystemExit(f"{gate} must come from michael, not {by}")

    text_sha1 = sha1_file_canonical(text_path) if text_path else None
    is_fallback = (gate == "argon_editorial" and by == "claude")
    # Candidate changed since last approval? Invalidate explicitly first so the
    # audit shows the invalidation, then record the new approval.
    prev = env["approvals"].get(gate) or {}
    if (gate == "argon_editorial" and prev.get("status") == "approved"
            and text_sha1 and prev.get("text_sha1") != text_sha1
            and env.get("editorial_state") == "ARGON_APPROVED"):
        set_editorial_state(env, "ARGON_REVIEW_PENDING", by=by,
                            note="candidate changed since last approval; prior approval invalidated")
    env["approvals"][gate] = {
        "status": "approved", "by": by, "at": utcnow(),
        "text_sha1": text_sha1, "fallback": is_fallback,
    }
    if is_fallback:
        set_editorial_state(env, "CLAUDE_FINAL_EDIT_FALLBACK", by="claude",
                            note="FINAL_EDIT_FALLBACK_CLAUDE — argon was unavailable; "
                                 "this is NOT an argon review")
        # Mark the synthesized result distinctly so downstream stages and
        # humans never mistake this for argon approval.
        synth = env["result"].get("synthesized") or {}
        synth["final_edit"] = "FINAL_EDIT_FALLBACK_CLAUDE"
        env["result"]["synthesized"] = synth
    elif gate == "argon_editorial" and by == "argon":
        set_editorial_state(env, "ARGON_APPROVED", by="argon",
                            note="argon approved exact candidate text")
    audit(env, f"APPROVAL: {gate} approved by {by}"
          + (" [FALLBACK — not argon]" if is_fallback else "")
          + (f" (text_sha1 {text_sha1[:12]})" if text_sha1 else ""), by=by)
    save_envelope(env)
    print(f"{gate} approved by {by}" + (" [FALLBACK]" if is_fallback else ""))


# ---------------- EXECUTE / DELIVER / ARCHIVE ----------------

def cmd_execute(env):
    # Re-verify gates: the intended channel may have changed since VALIDATE.
    problems = check_gates(env)
    if problems:
        audit(env, "EXECUTE REFUSED: " + "; ".join(problems))
        save_envelope(env)
        print("EXECUTE REFUSED: " + "; ".join(problems))
        return False
    set_stage(env, "AUTHORIZED_EXECUTION", note="approved actions only")
    save_envelope(env)
    print(f"executed {env['task_id']}")


def cmd_set_channel(env, channel):
    env["result"]["delivery_channel"] = channel
    audit(env, f"intended delivery channel set to {channel}")
    save_envelope(env)
    print(f"channel -> {channel}")


def cmd_deliver(env, channel):
    """Record delivery INTENT. A local delivery.md file is NOT proof of external
    delivery. Status starts as DELIVERY_PENDING; the real external result must be
    recorded via confirm-delivery when an integration exists."""
    env["result"]["delivery_channel"] = channel
    env["result"]["delivery_status"] = "DELIVERY_PENDING"
    env["result"]["delivered_at"] = None
    tdir = task_dir(env["task_id"])
    with open(os.path.join(tdir, "delivery.md"), "w") as f:
        f.write(f"# Delivery {env['task_id']}\n\nchannel: {channel}\nstatus: DELIVERY_PENDING\n"
                f"at: {utcnow()}\n\n"
                f"> This file records delivery INTENT, not completion. "
                f"Use `confirm-delivery` to record the real external result.\n\n"
                f"```json\n{json.dumps(env['result']['synthesized'], indent=1)[:4000]}\n```\n")
    audit(env, f"delivery intent recorded for channel {channel} (status DELIVERY_PENDING)")
    set_stage(env, "DELIVER", note=f"delivery intent via {channel} (pending confirmation)")
    save_envelope(env)
    print(f"delivered {env['task_id']} via {channel} (DELIVERY_PENDING — confirm with confirm-delivery)")


def cmd_confirm_delivery(env, outcome, note=""):
    """Record the REAL external delivery result. outcome: DELIVERED | FAILED | AMBIGUOUS."""
    if outcome not in ("DELIVERED", "FAILED", "AMBIGUOUS"):
        raise SystemExit(f"unknown delivery outcome {outcome}")
    env["result"]["delivery_status"] = outcome
    if outcome == "DELIVERED":
        env["result"]["delivered_at"] = utcnow()
    audit(env, f"delivery outcome: {outcome}" + (f" ({note})" if note else ""))
    if outcome == "AMBIGUOUS":
        audit(env, "AMBIGUOUS delivery: reconcile before retrying to avoid duplicate posts/sends")
    save_envelope(env)
    print(f"delivery outcome recorded: {outcome}")


def cmd_archive(env):
    src = task_dir(env["task_id"])
    dst = os.path.join(ARCHIVE, env["task_id"])
    os.makedirs(ARCHIVE, exist_ok=True)
    if os.path.exists(src):
        if os.path.exists(dst):
            # duplicate re-archive: fold audit lines into the existing record, drop the shell
            try:
                with open(os.path.join(dst, "envelope.json")) as f:
                    olde = json.load(f)
                olde["audit"].extend(env["audit"])
                olde["stage_history"].extend(env["stage_history"])
                with open(os.path.join(dst, "envelope.json"), "w") as f:
                    json.dump(olde, f, indent=1)
            except Exception:
                pass
            shutil.rmtree(src)
        else:
            shutil.move(src, dst)
    reg = load_registry()
    if env["task_id"] not in reg["processed"]:
        reg["processed"].append(env["task_id"])
    ev_key = env.get("evidence_key")
    if ev_key and ev_key not in reg.get("evidence", []):
        reg.setdefault("evidence", []).append(ev_key)
    save_registry(reg)
    env["_archived"] = True
    set_stage(env, "ARCHIVE", note="task archived, id registered")
    save_envelope(env)
    # clean dispatched inbox files (canonical + legacy locations)
    for agent in AGENTS + list(LEGACY_ALIASES.keys()):
        p = os.path.join(INBOX, agent, f"task-{env['task_id']}.md")
        done = os.path.join(INBOX, agent, "done")
        if os.path.exists(p):
            os.makedirs(done, exist_ok=True)
            shutil.move(p, os.path.join(done, f"task-{env['task_id']}.md"))
    print(f"archived {env['task_id']}")


def cmd_advance(task_id):
    env = load_envelope(task_id)
    if env.get("_archived"):
        print(f"{task_id} already archived")
        return
    stage = env["stage"]
    if stage == "ARCHIVE":
        # duplicate short-circuit lands here; move it to the archive
        cmd_archive(env)
        return
    nxt = {"INGEST": cmd_normalize, "NORMALIZE": cmd_dispatch,
           "DISPATCH": None, "COLLECT": cmd_synthesize, "SYNTHESIZE": None,
           "VALIDATE": cmd_execute, "AUTHORIZED_EXECUTION": None,
           "DELIVER": cmd_archive}
    if stage == "DISPATCH":
        print("DISPATCH done — waiting for agent results. Use collect / mark-unavailable, then advance.")
        set_stage(env, "COLLECT", note="awaiting agent results")
        save_envelope(env)
        return
    if stage == "SYNTHESIZE":
        ok = cmd_validate(env)
        if not ok:
            print("fix gates (approve) then advance again")
            return
        return
    if stage == "AUTHORIZED_EXECUTION":
        print("AUTHORIZED_EXECUTION done — set delivery channel: pipeline.py deliver <id> --channel chat|buffer|telegram")
        return
    fn = nxt.get(stage)
    if fn is None:
        print(f"nothing to auto-advance from {stage}")
        return
    fn(env)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("--adapter", required=True)
    p.add_argument("--image"); p.add_argument("--text"); p.add_argument("--url")
    p.add_argument("--intent", required=True)
    p.add_argument("--dial", default=None)
    p.add_argument("--task-type", default="other", choices=TASK_TYPES)

    sub.add_parser("advance").add_argument("task_id")

    p = sub.add_parser("collect")
    p.add_argument("task_id"); p.add_argument("--agent", required=True); p.add_argument("--result", required=True)

    p = sub.add_parser("mark-unavailable")
    p.add_argument("task_id"); p.add_argument("--agent", required=True); p.add_argument("--note", default="no result")

    p = sub.add_parser("mark-available")
    p.add_argument("task_id"); p.add_argument("--agent", required=True); p.add_argument("--note", default="")

    p = sub.add_parser("approve")
    p.add_argument("task_id"); p.add_argument("--gate", required=True,
        choices=GATES + list(LEGACY_GATES.keys()))
    p.add_argument("--by", required=True); p.add_argument("--text", default=None)

    p = sub.add_parser("deliver")
    p.add_argument("task_id"); p.add_argument("--channel", required=True,
        choices=["chat", "buffer", "telegram", "email_draft"])

    p = sub.add_parser("confirm-delivery")
    p.add_argument("task_id"); p.add_argument("--outcome", required=True,
        choices=["DELIVERED", "FAILED", "AMBIGUOUS"]); p.add_argument("--note", default="")

    p = sub.add_parser("set-channel")
    p.add_argument("task_id"); p.add_argument("--channel", required=True,
        choices=["chat", "buffer", "telegram", "email_draft", "x", "linkedin"])

    sub.add_parser("status").add_argument("task_id")

    args = ap.parse_args()
    if args.cmd == "ingest":
        cmd_ingest(args)
    elif args.cmd == "advance":
        cmd_advance(args.task_id)
    elif args.cmd == "collect":
        env = load_envelope(args.task_id); cmd_collect(env, args.agent, args.result)
    elif args.cmd == "mark-unavailable":
        env = load_envelope(args.task_id); cmd_mark_unavailable(env, args.agent, args.note)
    elif args.cmd == "mark-available":
        env = load_envelope(args.task_id); cmd_mark_available(env, args.agent, args.note)
    elif args.cmd == "approve":
        env = load_envelope(args.task_id); cmd_approve(env, args.gate, args.by, args.text)
    elif args.cmd == "deliver":
        env = load_envelope(args.task_id); cmd_deliver(env, args.channel)
    elif args.cmd == "confirm-delivery":
        env = load_envelope(args.task_id); cmd_confirm_delivery(env, args.outcome, args.note)
    elif args.cmd == "set-channel":
        env = load_envelope(args.task_id); cmd_set_channel(env, args.channel)
    elif args.cmd == "status":
        env = load_envelope(args.task_id)
        print(json.dumps({k: env[k] for k in ("task_id", "stage", "task_type", "dispatch", "approvals", "duplicate_of")}, indent=1))


if __name__ == "__main__":
    main()
