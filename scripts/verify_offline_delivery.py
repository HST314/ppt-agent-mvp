#!/usr/bin/env python3
import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ppt_agent.offline import verify_delivery

parser = argparse.ArgumentParser(description="Verify delivery hashes, HTML and absence of external URLs")
parser.add_argument("path", type=Path, help="delivery directory or offline ZIP")
args = parser.parse_args()
if args.path.is_dir():
    files = verify_delivery(args.path)
else:
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(args.path) as archive:
            for member in archive.infolist():
                target = (Path(tmp) / member.filename).resolve()
                if Path(tmp).resolve() not in target.parents:
                    raise SystemExit("ZIP contains an unsafe path")
            archive.extractall(tmp)
        files = verify_delivery(Path(tmp))
print(json.dumps({"status": "ok", "files": len(files)}, ensure_ascii=False))
