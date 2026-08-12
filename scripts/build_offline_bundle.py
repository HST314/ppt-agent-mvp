#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ppt_agent.offline import build_zip

parser = argparse.ArgumentParser(description="Verify a delivery and create a deterministic offline ZIP")
parser.add_argument("delivery", type=Path, help="immutable delivery directory containing manifest.json")
parser.add_argument("--output", type=Path, help="ZIP path (default: <delivery>.zip)")
args = parser.parse_args()
output = args.output or args.delivery.with_suffix(".zip")
digest = build_zip(args.delivery, output)
print(json.dumps({"zip": str(output.resolve()), "sha256": digest}, ensure_ascii=False))
