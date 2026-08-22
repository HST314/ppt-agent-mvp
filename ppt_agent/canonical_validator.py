from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .skill_runtime import SkillRuntime


VALIDATOR_PATH = "scripts/validate-swiss-deck.mjs"
UPSTREAM_COMMIT = "c91369c449d34755d320a8b81d0734000d99d1ab"


def _messages(output: str, heading: str) -> list[str]:
    lines = output.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return []
    messages = []
    for line in lines[start:]:
        if line.startswith("- "):
            messages.append(line[2:].strip())
        elif line.strip():
            break
    return messages


def run_canonical_validator(html_text: str, style_id: str, *, timeout_seconds: float = 45) -> dict:
    """Run the hash-locked upstream Swiss validator, fail-closed.

    The canonical script is preserved byte-for-byte in the built-in Skill and
    invoked without inheriting model credentials.  Chromium geometry remains
    the production Python inspector's job; this validator supplies the Skill's
    own static/layout policy as an independently versioned gate.
    """
    if style_id != "swiss":
        return {
            "applicable": False,
            "passed": True,
            "script": VALIDATOR_PATH,
            "script_hash": None,
            "upstream_commit": UPSTREAM_COMMIT,
            "errors": [],
            "warnings": [],
        }
    skill = SkillRuntime.builtin()
    script_hash = skill.manifest.get(VALIDATOR_PATH)
    try:
        script_path, _ = skill._locked_bytes(VALIDATOR_PATH)  # verified, server-owned path
    except Exception:
        return {
            "applicable": True,
            "passed": False,
            "script": VALIDATOR_PATH,
            "script_hash": script_hash,
            "upstream_commit": UPSTREAM_COMMIT,
            "errors": ["canonical validator 缺失或哈希校验失败"],
            "warnings": [],
        }
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
            handle.write(html_text)
            temporary_path = Path(handle.name)
        completed = subprocess.run(
            ["node", str(script_path), str(temporary_path)],
            cwd=skill.root,
            env={"PATH": os.environ.get("PATH", ""), "NODE_NO_WARNINGS": "1"},
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        combined = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        errors = _messages(combined, "Swiss deck validation failed:")
        warnings = _messages(combined, "Warnings:")
        if completed.returncode and not errors:
            errors = [f"canonical validator 异常退出（code={completed.returncode}）"]
        return {
            "applicable": True,
            "passed": completed.returncode == 0,
            "script": VALIDATOR_PATH,
            "script_hash": script_hash,
            "upstream_commit": UPSTREAM_COMMIT,
            "errors": errors,
            "warnings": warnings,
        }
    except FileNotFoundError:
        return {
            "applicable": True,
            "passed": False,
            "script": VALIDATOR_PATH,
            "script_hash": script_hash,
            "upstream_commit": UPSTREAM_COMMIT,
            "errors": ["Node.js 不可用，canonical validator 未执行"],
            "warnings": [],
        }
    except subprocess.TimeoutExpired:
        return {
            "applicable": True,
            "passed": False,
            "script": VALIDATOR_PATH,
            "script_hash": script_hash,
            "upstream_commit": UPSTREAM_COMMIT,
            "errors": ["canonical validator 执行超时"],
            "warnings": [],
        }
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
