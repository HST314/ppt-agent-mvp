#!/usr/bin/env python3
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ppt_agent.schema import MODELS
root=Path(__file__).resolve().parents[1]/"schemas"; root.mkdir(exist_ok=True)
for m in MODELS:(root/f"{m.__name__}.schema.json").write_text(json.dumps(m.json_schema(),ensure_ascii=False,indent=2)+"\n")
print(f"exported {len(MODELS)} schemas")
