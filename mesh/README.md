# Mesh Wire Spec v1.0

One canonical workflow. Every input adapter produces the same task envelope.
Every agent reads the same envelope. No side channels.

## The 9 stages (invariant order)

```
INGEST → NORMALIZE → DISPATCH → COLLECT → SYNTHESIZE → VALIDATE → AUTHORIZED_EXECUTION → DELIVER → ARCHIVE
```

| Stage | What happens | Who runs it |
|---|---|---|
| INGEST | Adapter captures raw input, creates envelope, stores evidence | whichever adapter fired |
| NORMALIZE | Fill evidence hashes, set task_type/dial, dedup against registry | ingesting agent |
| DISPATCH | Write `inbox/<agent>/task-<id>.md` per agent with role + envelope ref | pipeline |
| COLLECT | Agents write `mesh/tasks/<id>/results/<agent>.json`; timeout (15 min default) → `unavailable`, never fabricated | pipeline |
| SYNTHESIZE | Merge results per task_type (rebuttal: Claude's v8 draft is primary, others annotate) | Owen (execution lead) |
| VALIDATE | Enforce gates: editorial sign-off, char limits, approvals | pipeline |
| AUTHORIZED_EXECUTION | Only approved actions run; each logged to audit | pipeline |
| DELIVER | Write result to the configured channel | pipeline |
| ARCHIVE | Move task dir to `mesh/archive/<id>/`, register id, commit | pipeline |

## Agent lanes (fixed — the task never changes, only the role)

- **openclaw** — Windows host daemon. Intake adapters (watched folder, Telegram optional), local file ops, Telegram alerts to Michael's phone. Infrastructure role, not a reasoning agent.
- **owen** — Execution lead. Intake, dispatch, orchestration, delivery, retries, checkpointing, audit. Also: research, source verification, legwork, evidence collection. Buffer queueing only after editorial sign-off + Michael's buffer approval.
- **claude** — First-draft writer. Fast Response v8 drafts (dial 1–9 rules, `<850` LinkedIn / `<280` X, ` ```text ` copy blocks, 3-line context breakdown), business packages, epistemic challenges.
- **argon** — Preferred final reasoning editor and rebuttal sharpener. Final editorial sign-off on ALL public text. Nothing publishes without `approvals.argon_editorial = approved`. (Canonical ID since 2026-10-13; formerly "spark". The `inbox/spark/` directory is preserved for historical records — new work dispatches to `inbox/argon/`.)

## Editorial fallback and resumption (auditable)

Argon is preferred, but quota/timeout failures must never silently discard a task
or falsely mark review complete. The envelope tracks `editorial_state`:

```
ARGON_PENDING → ARGON_UNAVAILABLE → CLAUDE_FINAL_EDIT_FALLBACK
    → ARGON_REVIEW_PENDING → ARGON_APPROVED
```

- `mark-unavailable --agent argon` → `ARGON_UNAVAILABLE`. Task waits; nothing is fabricated.
- `approve --gate argon_editorial --by claude` is allowed ONLY in `ARGON_UNAVAILABLE`.
  The approval is recorded with `by: claude, fallback: true` and the result is marked
  `FINAL_EDIT_FALLBACK_CLAUDE`. It is never presented as argon review.
- `mark-available --agent argon` → `ARGON_REVIEW_PENDING`. Task ID, evidence,
  candidate text, and review history are preserved.
- `approve --gate argon_editorial --by argon` → `ARGON_APPROVED`.
- If the candidate text changes after any approval, the old approval is invalidated
  (hash mismatch) and the exact new text must be re-approved.

## Text hashing (canonical form)

Approval matching is byte-exact on the canonical form:

- Unicode text, UTF-8 encoding.
- Line endings normalized: CRLF → LF, lone CR → LF. Leading BOM stripped.
- NO trailing-newline stripping: `"hello"` and `"hello\n"` are different texts
  with different hashes. When in doubt, re-approve the exact bytes being delivered.

## Delivery honesty

`deliver` records delivery INTENT (`DELIVERY_PENDING`) and writes `delivery.md`.
A local file is not proof of external delivery. Record the real outcome with
`confirm-delivery --outcome DELIVERED|FAILED|AMBIGUOUS`. An `AMBIGUOUS` outcome
must be reconciled before retrying to avoid duplicate posts or sends.

## Input adapters (all produce the same envelope)

| Adapter | Source | Status |
|---|---|---|
| `chat_upload` | Screenshot/file sent in chat with Michael | VERIFIED WORKING |
| `mms` | MMS/RCS attachment pulled from the paired phone | implemented, untested |
| `watched_folder` | Screenshot dropped in `AI-Workspace/drive-wire/00_intake/` | Windows side (OpenClaw) |
| `telegram` | Photo sent to @Crumly_bot | OPTIONAL — never required |
| `browser_capture` | URL/text captured from browser | implemented |
| `manual` | Hand-built envelope for tests/ops | implemented |

Telegram is a convenience, not a prerequisite. If it is down, expired, or
absent from the share sheet, the other adapters cover intake.

## Approval gates (VALIDATE enforces, never bypassed)

- Public text (LinkedIn/X/comment): `approvals.argon_editorial` must be `approved`,
  and the approved `text_sha1` must match the canonical form of the text being
  delivered byte-for-byte. (`spark_editorial` accepted as a legacy alias for old envelopes.)
- Buffer queueing: `approvals.michael_buffer` must be `approved`.
- Sending email as Michael: `approvals.michael_send` must be `approved`.
- Spending / destructive: `approvals.michael_spend` must be `approved`.
- Char limits: X `<280`, LinkedIn `<850` — checked at VALIDATE, violations fail the task.

The pipeline cannot approve anything itself. Approvals arrive as recorded writes
to the envelope from argon (editorial), claude (editorial fallback ONLY when argon
is unavailable, marked `FINAL_EDIT_FALLBACK_CLAUDE`), or Michael (everything else),
each appended to the audit trail. A `--by` identity string alone is not proof of
authorization — the pipeline enforces who may approve what and when.

## Idempotency

`task_id = sha1(source_adapter | evidence_key | created_utc_minute)[0:16]`.
Same input within the same minute = same id. NORMALIZE checks
`mesh/registry.json`; a hit marks the envelope `duplicate_of` and archives it
immediately. Retries are safe: re-running any stage is a no-op once recorded
in `stage_history`.

## Unavailable agents

An agent that does not write its result file before the collect timeout is
recorded `unavailable` with a note (timeout, limit hit, integration down).
The task proceeds with the agents that responded. The system NEVER fabricates
an agent's execution or approval.

## Fast Response v8 preservation

Dial rules, platform limits, copy-block formatting, and the 3-line context
breakdown live in the `linkedin-fast-response-v8` skill — the canonical source.
The envelope carries `dial_command` through every stage. Claude drafts per v8;
Spark signs off; Owen queues. Nothing in this wire weakens or reinterprets v8.

## Layout

```
mesh/
  README.md                # this spec
  envelope.schema.json     # the envelope contract
  pipeline.py              # stage runner (python3, runs anywhere)
  registry.json            # processed task_ids (dedup)
  adapters/
    chat_intake.py         # chat_upload adapter (Owen side)
    mms_intake.py          # phone MMS adapter (Owen side)
  tasks/<task_id>/         # live tasks: envelope.json, evidence/, results/
  archive/<task_id>/       # finished tasks
  windows/
    mesh-runner.ps1        # Windows runner for OpenClaw (same envelope spec)
    Install-Mesh.ps1       # one-time Windows setup
```

## Running it

```bash
# Ingest a screenshot from chat
python3 mesh/pipeline.py ingest --adapter chat_upload --image path/to/shot.png \
    --intent "rebut this post" --dial 1 --task-type screenshot_rebuttal

# Advance one stage (dispatch, collect, synthesize, validate, execute, deliver, archive)
python3 mesh/pipeline.py advance <task_id>

# Record an agent result
python3 mesh/pipeline.py collect <task_id> --agent claude --result path/to/claude.json

# Argon unavailable -> claude fallback -> argon resume
python3 mesh/pipeline.py mark-unavailable <task_id> --agent argon --note "quota hit"
python3 mesh/pipeline.py approve <task_id> --gate argon_editorial --by claude --text path/to/claude-edit.txt
python3 mesh/pipeline.py mark-available <task_id> --agent argon --note "quota reset"
python3 mesh/pipeline.py approve <task_id> --gate argon_editorial --by argon --text path/to/argon-edit.txt

# Record an approval (argon or michael; claude only as editorial fallback)
python3 mesh/pipeline.py approve <task_id> --gate argon_editorial --by argon --text path/to/approved.txt

# Record the real external delivery outcome (delivery.md alone is not proof)
python3 mesh/pipeline.py confirm-delivery <task_id> --outcome DELIVERED --note "buffer id 123"

# Declare the intended delivery channel BEFORE validation (gates depend on it)
python3 mesh/pipeline.py set-channel <task_id> --channel buffer
```
