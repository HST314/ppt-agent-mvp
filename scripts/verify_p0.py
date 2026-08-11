"""Offline P0 documentation gate."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "scripts/start_p0.py",
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
    plan = (ROOT / "docs/development-plan.md").read_text(encoding="utf-8")
    task_ids = set(re.findall(r"^### (P[0-8]-\d{2})\b", plan, re.MULTILINE))
    matrix_rows = re.findall(r"^\| (AC-\d{2}) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", matrix, re.MULTILINE)
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
    if len(matrix_rows) != 18:
        errors.append(f"验收矩阵字段不完整或行数错误: 解析到 {len(matrix_rows)} 行")
    for ac_id, behavior, owners, surface, evidence in matrix_rows:
        refs = set(re.findall(r"P[0-8]-\d{2}", owners))
        invalid = refs - task_ids
        if not refs or invalid:
            errors.append(f"{ac_id} 责任任务无效: {', '.join(sorted(invalid)) or '未填写'}")
        if not behavior.strip() or not surface.strip() or not evidence.strip():
            errors.append(f"{ac_id} 存在空映射字段")
    state = (ROOT / "docs/current-state.md").read_text(encoding="utf-8")
    evidence_markers = ["768471c3efa5aee5032c41468d2438a16d43c8dd", "agent_core/models.py", "storage/project_store.py", "model_router/gateway.py", "frontend/index.html"]
    missing_evidence = [item for item in evidence_markers if item not in state]
    if missing_evidence:
        errors.append("P0-01 证据缺失: " + ", ".join(missing_evidence))
    startup = subprocess.run(
        [sys.executable, str(ROOT / "scripts/start_p0.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if startup.returncode or "自检通过" not in startup.stdout:
        errors.append("最小启动入口自检失败: " + (startup.stderr.strip() or startup.stdout.strip()))
    if undecided:
        errors.append("ADR 未采纳: " + ", ".join(undecided))
    if errors:
        print("P0 校验失败")
        print("\n".join(errors))
        return 1
    print("P0 校验通过")
    print("- 必需文件齐全，核心 ADR 已采纳")
    print("- AC-01..AC-18 字段完整，责任任务均存在")
    print("- P0-01 Image Agent revision 与关键源码证据齐全")
    print("- 最小启动入口自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
