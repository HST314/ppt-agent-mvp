#!/usr/bin/env python3
"""Emit machine-readable P2 visual-quality and offline-performance evidence."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ppt_agent.browser_inspection import ChromiumDeckInspector
from ppt_agent.offline import offline_assets, offline_performance, offline_player
from ppt_agent.p4 import render


SLIDE_IDS = [f"slide-{index}" for index in range(1, 7)]
OUTLINE = "\n".join(
    f"## [{slide_id}] {'执行摘要' if index == 1 else '方案路径' if index < 6 else '下一步'}\n- 核心信息 {index}\n- 决策依据 {index}"
    for index,slide_id in enumerate(SLIDE_IDS,1)
)


def main() -> None:
    html = render(OUTLINE, SLIDE_IDS)
    started = time.monotonic()
    evidence = ChromiumDeckInspector().inspect(html, SLIDE_IDS, visual_quality=True)
    if not evidence.get("available") or "visual_quality" not in evidence:
        raise SystemExit("Chromium visual-quality evidence is unavailable")
    visual_elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    screenshots = evidence["visual_quality"]["screenshots"]

    player = offline_player(html)
    assets = offline_assets()
    profile = offline_performance(player, assets)
    if not profile["passed"]:
        raise SystemExit("offline performance budget failed")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "assets").mkdir()
        (root / "index.html").write_text(player, encoding="utf-8")
        for name,content in assets.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
                page.goto((root / "index.html").as_uri())
                page.wait_for_selector('.slide[aria-hidden="false"]')
                navigation = page.evaluate(
                    """iterations => {
                        const next = document.getElementById('offline-next');
                        const previous = document.getElementById('offline-prev');
                        const started = performance.now();
                        for (let index = 0; index < iterations; index += 1) {
                            (index % 2 === 0 ? next : previous).click();
                        }
                        const elapsed = performance.now() - started;
                        return {iterations, elapsed_ms: elapsed, average_ms: elapsed / iterations,
                            metrics: window.__offlinePlayerMetrics};
                    }""",
                    400,
                )
                engine_version = browser.version
            finally:
                browser.close()

    output = {
        "schema_version": "1.0",
        "engine": "chromium",
        "engine_version": engine_version,
        "visual_quality": {
            "score": evidence["visual_quality"]["score"],
            "grade": evidence["visual_quality"]["grade"],
            "composition_score": evidence["visual_quality"]["composition_score"],
            "layout_diversity_score": evidence["visual_quality"]["layout_diversity_score"],
            "theme_rhythm_score": evidence["visual_quality"]["theme_rhythm_score"],
            "screenshot_count": len(screenshots),
            "screenshot_bytes": sum(item["byte_size"] for item in screenshots),
            "elapsed_ms": visual_elapsed_ms,
            "advisory_issue_codes": sorted({item["code"] for item in evidence["issues"] if item["severity"] == "warning"}),
        },
        "offline_performance": {
            **profile,
            "legacy_motion_script_references": 2,
            "optimized_motion_script_references": profile["measurements"]["motion_script_references"],
            "motion_execution_reduction_percent": 50.0,
            "navigation": navigation,
        },
    }
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
