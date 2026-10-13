#!/usr/bin/env python3
"""chat_upload adapter: screenshot/file shared in chat -> canonical envelope.

Verified working: Michael sends an image in chat, Owen saves it under
workspace/user/files/, then:

  python3 adapters/chat_intake.py --image <path> --intent "rebut this" [--dial 1]

Produces the envelope and leaves the task at INGEST; pipeline.py advance
drives it through the nine stages.
"""
import argparse, os, subprocess, sys

PIPELINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipeline.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True, help="local path to the uploaded image")
    ap.add_argument("--intent", required=True, help="verbatim user intent")
    ap.add_argument("--dial", default=None, help="v8 dial 1-9 or AUTO_TRIAGE")
    ap.add_argument("--task-type", default="screenshot_rebuttal")
    a = ap.parse_args()
    if not os.path.exists(a.image):
        raise SystemExit(f"image not found: {a.image}")
    cmd = [sys.executable, PIPELINE, "ingest", "--adapter", "chat_upload",
           "--image", a.image, "--intent", a.intent, "--task-type", a.task_type]
    if a.dial:
        cmd += ["--dial", a.dial]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
