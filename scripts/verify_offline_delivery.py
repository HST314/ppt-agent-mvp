#!/usr/bin/env python3
import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ppt_agent.offline import validate_zip_members, verify_delivery

parser = argparse.ArgumentParser(description="Verify delivery hashes, HTML and absence of external URLs")
parser.add_argument("path", type=Path, help="delivery directory or offline ZIP")
args = parser.parse_args()
if args.path.is_dir():
    files = verify_delivery(args.path)
else:
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.path) as archive:
            members = validate_zip_members(archive)
            archive.extractall(tmp, members=members)
        files = verify_delivery(Path(tmp))
print(json.dumps({"status": "ok", "files": len(files)}, ensure_ascii=False))
