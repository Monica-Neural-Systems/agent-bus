#!/usr/bin/env python3
"""Mesh wire pipeline runner. One canonical workflow, nine invariant stages.

Usage:
  pipeline.py ingest --adapter chat_upload --image shot.png --intent "..." [--dial 1] [--task-type screenshot_rebuttal]
  pipeline.py advance <task_id>            # run the next stage for a live task
  pipeline.py collect <task_id> --agent claude --result results/claude.json
  pipeline.py mark-unavailable <task_id> --agent spark --note "limit hit"
  pipeline.py approve <task_id> --gate spark_editorial --by spark --text approved.txt
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
AGENTS = ["openclaw", "claude", "spark", "owen"]
GATES = ["spark_editorial", "michael_send", "michael_buffer", "michael_spend"]

AGENT_ROLES = {
    "openclaw": "Windows host daemon: intake adapters, watched folders, Telegram alerts, local file ops.",
    "claude": "Lead Reasoning: Fast Response v8 drafts (dial rules, <850/<280, ```text blocks, 3-line breakdown), business packages, epistemic challenges.",
    "spark": "Editor-in-Chief: final editorial sign-off on ALL public text.",
    "owen": "Web Scout + Distribution: research, verification, Buffer queueing (post-approval), execution, audit.",
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
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


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
        "approvals": {g: {"status": "not_required", "by": None, "at": None, "text_sha1": None} for g in GATES},
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
        env["approvals"]["spark_editorial"]["status"] = "pending"
        audit(env, "NORMALIZE: spark_editorial gate armed (public text task)")
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
    slot = env["dispatch"][agent]
    slot["status"] = "unavailable"
    slot["note"] = note
    slot["updated_at"] = utcnow()
    audit(env, f"COLLECT: {agent} UNAVAILABLE ({note}) — recorded, not fabricated")
    save_envelope(env)
    print(f"{agent} marked unavailable")


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
    if needs_editorial:
        ap = env["approvals"]["spark_editorial"]
        if ap["status"] != "approved":
            problems.append("spark_editorial not approved")
        elif draft and ap["text_sha1"] != sha1_text(draft):
            problems.append("approved text_sha1 does not match draft — re-approval required")
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
    if gate not in GATES:
        raise SystemExit(f"unknown gate {gate}")
    if by not in ("spark", "michael"):
        raise SystemExit("approvals only from spark or michael")
    if gate == "spark_editorial" and by != "spark":
        raise SystemExit("spark_editorial must come from spark")
    if gate.startswith("michael_") and by != "michael":
        raise SystemExit(f"{gate} must come from michael")
    text_sha1 = sha1_file(text_path) if text_path else None
    env["approvals"][gate] = {"status": "approved", "by": by, "at": utcnow(), "text_sha1": text_sha1}
    audit(env, f"APPROVAL: {gate} approved by {by}" + (f" (text_sha1 {text_sha1[:12]})" if text_sha1 else ""), by=by)
    save_envelope(env)
    print(f"{gate} approved by {by}")


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
    env["result"]["delivery_channel"] = channel
    env["result"]["delivered_at"] = utcnow()
    tdir = task_dir(env["task_id"])
    with open(os.path.join(tdir, "delivery.md"), "w") as f:
        f.write(f"# Delivery {env['task_id']}\n\nchannel: {channel}\nat: {env['result']['delivered_at']}\n\n"
                f"```json\n{json.dumps(env['result']['synthesized'], indent=1)[:4000]}\n```\n")
    set_stage(env, "DELIVER", note=f"delivered via {channel}")
    save_envelope(env)
    print(f"delivered {env['task_id']} via {channel}")


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
    # clean dispatched inbox files
    for agent in AGENTS:
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

    p = sub.add_parser("approve")
    p.add_argument("task_id"); p.add_argument("--gate", required=True, choices=GATES)
    p.add_argument("--by", required=True); p.add_argument("--text", default=None)

    p = sub.add_parser("deliver")
    p.add_argument("task_id"); p.add_argument("--channel", required=True,
        choices=["chat", "buffer", "telegram", "email_draft"])

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
    elif args.cmd == "approve":
        env = load_envelope(args.task_id); cmd_approve(env, args.gate, args.by, args.text)
    elif args.cmd == "deliver":
        env = load_envelope(args.task_id); cmd_deliver(env, args.channel)
    elif args.cmd == "set-channel":
        env = load_envelope(args.task_id); cmd_set_channel(env, args.channel)
    elif args.cmd == "status":
        env = load_envelope(args.task_id)
        print(json.dumps({k: env[k] for k in ("task_id", "stage", "task_type", "dispatch", "approvals", "duplicate_of")}, indent=1))


if __name__ == "__main__":
    main()
