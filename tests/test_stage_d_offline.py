import hashlib
import json
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ppt_agent.errors import ValidationError
from ppt_agent.offline import build_zip, download_remote_image, external_urls, localize_delivery_html, validate_zip_members, verify_delivery
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    def inspect(self, outline, html):
        return {"passed": True, "issues": [], "model": "fixture"}


class OfflineDeliveryTests(unittest.TestCase):
    def test_remote_download_pins_public_dns_result_and_rejects_mixed_private_answers(self):
        public=(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",80))
        private=(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",80))
        with patch("ppt_agent.offline.socket.getaddrinfo",return_value=[public,private]), self.assertRaisesRegex(ValidationError,"内网"):
            download_remote_image("http://images.example/hero.png")

        png=b"\x89PNG\r\n\x1a\nremote"
        observed={}
        class Response:
            status=200
            class Headers(dict):
                def get_content_type(self): return "image/png"
            headers=Headers({"Content-Length":str(len(png))})
            def read(self,_limit): return png
        class Connection:
            def __init__(self,address,port,timeout): observed.update(address=address,port=port,timeout=timeout)
            def request(self,method,target,headers): observed.update(method=method,target=target,headers=headers)
            def getresponse(self): return Response()
            def close(self): observed["closed"]=True
        with patch("ppt_agent.offline.socket.getaddrinfo",return_value=[public]), patch("ppt_agent.offline.http.client.HTTPConnection",Connection):
            self.assertEqual(download_remote_image("http://images.example/hero.png?size=2"),(png,"image/png"))
        self.assertEqual(observed["address"],"93.184.216.34")
        self.assertEqual(observed["headers"]["Host"],"images.example")
        self.assertEqual(observed["target"],"/hero.png?size=2")

    def test_remote_and_relative_images_are_localized_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/"hero.png").write_bytes(b"local-image")
            manifest={"resources":[{"uri":"resources://hero.png","content_hash":hashlib.sha256(b"local-image").hexdigest(),"media_type":"image/png"}]}
            remote=b"\x89PNG\r\n\x1a\nremote"
            source='<!doctype html><html><head><style>.hero{background:url("https://cdn.example/bg.png")}</style></head><body><img src="hero.png"><section data-slide-id="slide-1"></section></body></html>'
            localized,files,records=localize_delivery_html(source,manifest,root,lambda _url:(remote,"image/png"))
            self.assertNotIn("https://",localized)
            self.assertIn('src="resources/hero.png"',localized)
            self.assertEqual(files["resources/hero.png"],b"local-image")
            self.assertEqual({item["source"] for item in records},{"remote","relative"})

    def actual_delivery(self, workspace: Path) -> Path:
        store = WorkspaceStore(workspace)
        service = TaskService(store, inspector=PassingInspector())
        service.create("offline", "manual")
        service.import_input("offline", {"goal": "发布", "audience": "客户", "topic": "离线演示", "页数": 3})
        service.generate_narrative("offline"); service.confirm_narrative("offline")
        service.generate_outline("offline"); service.confirm_outline("offline")
        service.generate_sample("offline"); service.confirm_sample("offline")
        service.generate_deck("offline"); service.run_inspection("offline", 0)
        deck = service.deck_view("offline")["deck"]
        delivery = service.confirm_delivery("offline", deck["hash"])["delivery"]
        return store.delivery_root("offline", delivery["delivery_id"])

    def fixture(self, root: Path):
        files = {"deck.html": b"<!doctype html><html><body><section>offline</section></body></html>", "outline.md": b"# outline"}
        for name, content in files.items():
            (root / name).write_bytes(content)
        manifest = {"files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def refresh_manifest(self, root: Path):
        files = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"
        }
        (root / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")

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

    def test_runtime_scan_ignores_comments_and_notices_but_blocks_executable_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vendor.js").write_text("/* https://license.example */\n//# sourceMappingURL=https://cdn.example/a.map\nconst local = 1", encoding="utf-8")
            (root / "THIRD_PARTY_NOTICES.txt").write_text("License: https://license.example", encoding="utf-8")
            self.assertEqual(external_urls(root), {})
            (root / "app.js").write_text("fetch('https://api.example/data')", encoding="utf-8")
            (root / "index.html").write_text('<script src="https://cdn.example/app.js"></script>', encoding="utf-8")
            findings = external_urls(root)
            self.assertIn("app.js", findings)
            self.assertIn("index.html", findings)

    def test_embedded_html_runtime_urls_block_all_delivery_entrypoints(self):
        cases = {
            "style element": '<style>.hero{background:url("https://style.example/hero.png")}</style>',
            "style attribute": '<main style="background:url(https://attribute.example/hero.png)">x</main>',
            "iframe srcdoc": '<iframe srcdoc="&lt;img src=&quot;https://srcdoc.example/hero.png&quot;&gt;"></iframe>',
        }
        for label, embedded in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "delivery"; root.mkdir(); self.fixture(root)
                (root / "deck.html").write_text(f"<!doctype html><html><body>{embedded}</body></html>", encoding="utf-8")
                self.refresh_manifest(root)
                findings = external_urls(root)
                self.assertIn("deck.html", findings)
                self.assertEqual(len(findings["deck.html"]), 1)
                with self.assertRaisesRegex(ValueError, "external URLs"):
                    verify_delivery(root)
                with self.assertRaisesRegex(ValueError, "external URLs"):
                    build_zip(root, Path(tmp) / "blocked.zip")
                self.assertFalse((Path(tmp) / "blocked.zip").exists())

    def test_actual_delivery_verifier_builder_and_zip_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.actual_delivery(base / "workspace")
            repo = Path(__file__).resolve().parents[1]
            verified = subprocess.run(
                [sys.executable, "scripts/verify_offline_delivery.py", str(root)],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            first, second = base / "actual-one.zip", base / "actual-two.zip"
            for output in (first, second):
                built = subprocess.run(
                    [sys.executable, "scripts/build_offline_bundle.py", str(root), "--output", str(output)],
                    cwd=repo, capture_output=True, text=True,
                )
                self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            zip_verified = subprocess.run(
                [sys.executable, "scripts/verify_offline_delivery.py", str(first)],
                cwd=repo, capture_output=True, text=True,
            )
            self.assertEqual(zip_verified.returncode, 0, zip_verified.stderr)

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
