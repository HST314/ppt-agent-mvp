import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from ppt_agent.offline import build_zip, external_urls, verify_delivery


class OfflineDeliveryTests(unittest.TestCase):
    def fixture(self, root: Path):
        files = {"deck.html": b"<!doctype html><html><body><section>offline</section></body></html>", "outline.md": b"# outline"}
        for name, content in files.items():
            (root / name).write_bytes(content)
        manifest = {"files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_zip_is_deterministic_and_verifiable_after_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "delivery"; root.mkdir(); self.fixture(root)
            first, second = Path(tmp) / "one.zip", Path(tmp) / "two.zip"
            self.assertEqual(build_zip(root, first), build_zip(root, second))
            with zipfile.ZipFile(first) as archive:
                moved = Path(tmp) / "moved"; archive.extractall(moved)
            self.assertEqual(set(verify_delivery(moved)), {"deck.html", "outline.md"})

    def test_tamper_and_external_url_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            (root / "deck.html").write_text('<html><script src="https://cdn.example/app.js"></script></html>')
            self.assertIn("deck.html", external_urls(root))
            with self.assertRaisesRegex(ValueError, "hash mismatch"): verify_delivery(root)


if __name__ == "__main__": unittest.main()
