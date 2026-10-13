#!/usr/bin/env python3
"""mms adapter: MMS/RCS attachment from the paired phone -> canonical envelope.

Flow (run by Owen): pull the attachment with the device message.attachment.get
command, save it locally, then:

  python3 adapters/mms_intake.py --image <path> --sender "+1555..." --intent "..."

Untested end-to-end; the device command exists and the adapter is ready.
"""
import argparse, os, subprocess, sys

PIPELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--sender", default="unknown")
    ap.add_argument("--intent", required=True)
    ap.add_argument("--dial", default=None)
    ap.add_argument("--task-type", default="screenshot_rebuttal")
    a = ap.parse_args()
    if not os.path.exists(a.image):
        raise SystemExit(f"image not found: {a.image}")
    intent = f"[mms from {a.sender}] {a.intent}"
    cmd = [sys.executable, PIPELINE, "ingest", "--adapter", "mms",
           "--image", a.image, "--intent", intent, "--task-type", a.task_type]
    if a.dial:
        cmd += ["--dial", a.dial]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
