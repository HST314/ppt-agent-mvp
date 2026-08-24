from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from ppt_agent.generation.contracts import DeckSpec, ThemeTokens
from ppt_agent.generation.errors import RenderValidationError
from ppt_agent.rendering.assets import AssetResolver
from ppt_agent.rendering.renderer import DeterministicRenderer
from ppt_agent.rendering.validator import TechnicalValidator

from .support import THEME, asset_record, brief, slide


class RendererDeterminismTests(unittest.TestCase):
    def test_identical_spec_has_identical_bytes_and_dom(self):
        deck = DeckSpec.parse({
            "schema_version": "1.0",
            "slides": [slide("slide-001", "cover", layout="cover"), slide("slide-002", "data", layout="metrics")],
            "theme_tokens": THEME,
            "shared_assets": [],
            "outline_checkpoint_id": "cp-outline",
            "sample_checkpoint_id": "cp-sample",
        })
        renderer = DeterministicRenderer()
        first = renderer.render(deck)
        second = renderer.render(deck)
        self.assertEqual(first.html.encode(), second.html.encode())
        self.assertEqual(first.sha256, second.sha256)
        report = TechnicalValidator().validate(first.html, ["slide-001", "slide-002"])
        self.assertTrue(report.passed)

    def test_asset_paths_are_hash_verified_and_offline(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            record = asset_record(root)
            task_brief = brief(2, [record])
            deck = DeckSpec.parse({
                "schema_version": "1.0",
                "slides": [slide("slide-001", "cover", layout="cover", asset_ref="image-1"), slide("slide-002", "data", layout="metrics")],
                "theme_tokens": THEME,
                "shared_assets": ["image-1"],
                "outline_checkpoint_id": "cp-outline",
                "sample_checkpoint_id": "cp-sample",
            })
            assets = AssetResolver(task_brief.resource_manifest, root).resolve(deck.shared_assets)
            artifact = DeterministicRenderer().render(deck, assets)
            self.assertIn(f"assets/{record['content_hash']}.png", artifact.html)
            self.assertNotIn("http://", artifact.html)
            self.assertTrue(TechnicalValidator().validate(artifact.html, ["slide-001", "slide-002"], assets).passed)

    def test_validator_rejects_executable_or_external_content(self):
        deck = DeckSpec.parse({
            "schema_version": "1.0",
            "slides": [slide("slide-001", "cover", layout="cover"), slide("slide-002", "data", layout="metrics")],
            "theme_tokens": THEME,
            "shared_assets": [],
            "outline_checkpoint_id": "cp-outline",
            "sample_checkpoint_id": "cp-sample",
        })
        artifact = DeterministicRenderer().render(deck)
        unsafe = artifact.html.replace("</body>", '<script src="https://example.com/x.js"></script></body>')
        with self.assertRaises(RenderValidationError) as caught:
            TechnicalValidator().validate(unsafe, ["slide-001", "slide-002"])
        public = caught.exception.public()
        self.assertEqual(public["code"], "render_validation_failed")
        self.assertIn({"code": "unsafe_tag"}, public["details"]["diagnostics"])


if __name__ == "__main__":
    unittest.main()
