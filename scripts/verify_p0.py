"""Offline P0 documentation gate."""

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "docs/product-contract.md",
    "docs/development-plan.md",
    "docs/current-state.md",
    "docs/acceptance-matrix.md",
    "docs/adr/README.md",
    "docs/adr/0001-runtime.md",
    "docs/adr/0002-persistence-and-workspace.md",
    "docs/adr/0003-model-gateway.md",
    "docs/adr/0004-html-preview-security.md",
    "docs/adr/0005-versioning.md",
]


def main() -> int:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    matrix_path = ROOT / "docs/acceptance-matrix.md"
    matrix = matrix_path.read_text(encoding="utf-8") if matrix_path.exists() else ""
    ids = set(re.findall(r"AC-(?:0[1-9]|1[0-8])", matrix))
    expected = {f"AC-{number:02d}" for number in range(1, 19)}
    undecided = []
    for path in (ROOT / "docs/adr").glob("[0-9][0-9][0-9][0-9]-*.md"):
        text = path.read_text(encoding="utf-8")
        if "状态：已采纳" not in text:
            undecided.append(str(path.relative_to(ROOT)))
    errors = []
    if missing:
        errors.append("缺少文件: " + ", ".join(missing))
    if ids != expected:
        errors.append("验收项不完整: " + ", ".join(sorted(expected - ids)))
    if undecided:
        errors.append("ADR 未采纳: " + ", ".join(undecided))
    if errors:
        print("P0 校验失败")
        print("\n".join(errors))
        return 1
    print("P0 校验通过：必需文档齐全，AC-01..AC-18 映射完整，核心 ADR 已采纳。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
