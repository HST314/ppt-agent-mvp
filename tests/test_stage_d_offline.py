import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from ppt_agent.offline import build_zip, external_urls, validate_zip_members, verify_delivery


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

    def test_output_inside_delivery_is_rejected_without_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "delivery"; root.mkdir(); self.fixture(root)
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            output = root / "nested" / "inside.zip"
            with self.assertRaisesRegex(ValueError, "outside the delivery"):
                build_zip(root, output)
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(output.parent.exists())

            result = subprocess.run(
                [sys.executable, "scripts/build_offline_bundle.py", str(root), "--output", str(root / "inside.zip")],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})

    def test_cross_platform_unsafe_and_duplicate_zip_members_are_rejected(self):
        unsafe_names = ["../escape.txt", "..\\escape.txt", "/absolute.txt", "\\absolute.txt", "C:\\escape.txt", "a//b.txt"]
        for name in unsafe_names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / "bad.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(name, b"bad")
                with zipfile.ZipFile(archive_path) as archive, self.assertRaisesRegex(ValueError, "unsafe path"):
                    validate_zip_members(archive)

        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "duplicate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("asset.txt", b"one")
                archive.writestr("asset.txt", b"two")
            with zipfile.ZipFile(archive_path) as archive, self.assertRaisesRegex(ValueError, "duplicate member"):
                validate_zip_members(archive)

    def test_special_zip_file_types_are_rejected(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o644, stat.S_IFDIR | 0o755):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                archive_path = Path(tmp) / "special.zip"
                info = zipfile.ZipInfo("special")
                info.create_system = 3
                info.external_attr = mode << 16
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(info, b"target")
                with zipfile.ZipFile(archive_path) as archive, self.assertRaisesRegex(ValueError, "non-regular member"):
                    validate_zip_members(archive)


if __name__ == "__main__": unittest.main()
