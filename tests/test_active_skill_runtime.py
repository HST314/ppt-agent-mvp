import os
import tempfile
import unittest
from pathlib import Path

from ppt_agent.config import load_config
from ppt_agent.errors import ValidationError
from ppt_agent.gateways import AgentGateway, LockedSkillMetadataLoader
from ppt_agent.model_clients import ModelToolCall, ModelTurn
from ppt_agent.skill_runtime import ActiveSkillResolver, SkillRuntime


def write_skill(root: Path, directory: str, *, name: str, description: str, files=None) -> Path:
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    for relative, content in (files or {}).items():
        path = skill / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return skill


class ActiveSkillResolverTests(unittest.TestCase):
    def test_discovers_standard_directory_without_skill_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill = write_skill(
                root,
                "alpha",
                name="alpha-skill",
                description="Alpha description",
                files={
                    "references/guide.md": "guide",
                    "assets/pixel.bin": b"\x00\x01",
                    "scripts/check.py": "print('ok')",
                },
            )
            (skill / "SKILL_LOCK.json").write_text("not required", encoding="utf-8")
            (skill / "LICENSE").write_text("top-level files are not Agent-readable", encoding="utf-8")

            snapshot = ActiveSkillResolver(root, "alpha").resolve()
            runtime = SkillRuntime(snapshot)

            self.assertEqual(snapshot.name, "alpha-skill")
            self.assertEqual(snapshot.description, "Alpha description")
            self.assertRegex(snapshot.digest, r"^[0-9a-f]{64}$")
            self.assertEqual(
                runtime.list_skill_files()["files"],
                ["SKILL.md", "assets/pixel.bin", "references/guide.md", "scripts/check.py"],
            )
            self.assertNotIn("SKILL_LOCK.json", runtime.manifest)
            self.assertEqual(runtime.read_skill_file("references/guide.md")["content"], "guide")
            self.assertEqual(runtime.get_asset_info("assets/pixel.bin")["bytes"], 2)

    def test_reload_atomically_switches_new_jobs_and_preserves_old_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha", files={"references/a.md": "A"})
            write_skill(root, "beta", name="beta-skill", description="Beta", files={"references/b.md": "B"})
            resolver = ActiveSkillResolver(root, "alpha")
            old_runtime = resolver.runtime()
            old_digest = old_runtime.skill_version

            replacement = resolver.reload("beta")
            new_runtime = resolver.runtime()

            self.assertEqual(old_runtime.skill_name, "alpha-skill")
            self.assertEqual(old_runtime.read_skill_file("references/a.md")["content"], "A")
            self.assertEqual(new_runtime.skill_name, "beta-skill")
            self.assertEqual(new_runtime.read_skill_file("references/b.md")["content"], "B")
            self.assertNotEqual(old_digest, replacement.digest)

    def test_cached_snapshot_excludes_late_files_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill = write_skill(root, "alpha", name="alpha-skill", description="Alpha", files={"references/a.md": "A"})
            resolver = ActiveSkillResolver(root, "alpha")
            runtime = resolver.runtime()
            (skill / "references/new.md").write_text("new", encoding="utf-8")

            with self.assertRaises(ValidationError):
                runtime.read_skill_file("references/new.md")
            self.assertNotIn("references/new.md", resolver.runtime().manifest)

            refreshed = resolver.reload()
            self.assertIn("references/new.md", refreshed.manifest)
            (skill / "references/a.md").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "快照文件校验失败"):
                runtime.read_skill_file("references/a.md")

    def test_active_path_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            for active in ("../alpha", "/alpha", ".", "alpha\\child", "alpha\0child"):
                with self.subTest(active=active), self.assertRaises(ValidationError):
                    ActiveSkillResolver(root, active).resolve()

            if hasattr(os, "symlink"):
                os.symlink(root / "alpha", root / "alias")
                with self.assertRaisesRegex(ValidationError, "软链接"):
                    ActiveSkillResolver(root, "alias").resolve()

    def test_symlink_inside_standard_directory_is_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unsupported")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill = write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            (skill / "references").mkdir()
            target = Path(tmp) / "secret.txt"
            target.write_text("secret", encoding="utf-8")
            os.symlink(target, skill / "references/linked.md")
            with self.assertRaisesRegex(ValidationError, "软链接"):
                ActiveSkillResolver(root, "alpha").resolve()

    def test_frontmatter_and_snapshot_limits_are_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            invalid = root / "invalid"
            invalid.mkdir(parents=True)
            (invalid / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "frontmatter"):
                ActiveSkillResolver(root, "invalid").resolve()

            write_skill(
                root,
                "large",
                name="large-skill",
                description="Large",
                files={"assets/large.bin": b"x" * 1025},
            )
            with self.assertRaisesRegex(ValidationError, "快照上限"):
                ActiveSkillResolver(root, "large", max_snapshot_file_bytes=1024).resolve()

            write_skill(root, "many", name="many-skill", description="Many", files={"references/a.md": "a"})
            with self.assertRaisesRegex(ValidationError, "文件数"):
                ActiveSkillResolver(root, "many", max_files=1).resolve()

    def test_expected_digest_is_optional_and_enforced_when_supplied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            digest = ActiveSkillResolver(root, "alpha").resolve().digest
            self.assertEqual(ActiveSkillResolver(root, "alpha", expected_digest=digest).resolve().digest, digest)
            with self.assertRaisesRegex(ValidationError, "完整性摘要校验失败"):
                ActiveSkillResolver(root, "alpha", expected_digest="0" * 64).resolve()


class GenericSkillRuntimeTests(unittest.TestCase):
    def test_text_asset_and_cumulative_quotas_are_independent_per_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(
                root,
                "alpha",
                name="alpha-skill",
                description="Alpha",
                files={"references/a.md": "1234", "references/b.md": "5678", "assets/x.bin": b"12"},
            )
            snapshot = ActiveSkillResolver(root, "alpha").resolve()
            first = SkillRuntime(snapshot, max_file_bytes=4, max_total_bytes=6)
            second = first.clone()

            first.read_skill_file("references/a.md")
            first.get_asset_info("assets/x.bin")
            with self.assertRaisesRegex(ValidationError, "累计读取"):
                first.read_skill_file("references/b.md")
            self.assertEqual(second.read_skill_file("references/b.md")["content"], "5678")

    def test_binary_text_and_unlisted_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill = write_skill(
                root,
                "alpha",
                name="alpha-skill",
                description="Alpha",
                files={"references/bad.txt": b"\xff", "assets/blob.bin": b"\xff"},
            )
            (skill / "private.txt").write_text("private", encoding="utf-8")
            runtime = ActiveSkillResolver(root, "alpha").runtime()
            with self.assertRaisesRegex(ValidationError, "UTF-8"):
                runtime.read_skill_file("references/bad.txt")
            with self.assertRaises(ValidationError):
                runtime.read_skill_file("assets/blob.bin")
            with self.assertRaises(ValidationError):
                runtime.read_skill_file("private.txt")
            self.assertEqual(runtime.get_asset_info("assets/blob.bin")["media_type"], "application/octet-stream")


class SkillConfigAndInjectionTests(unittest.TestCase):
    def test_config_resolves_root_relative_to_yaml_and_carries_public_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            config_dir = base / "config"
            config_dir.mkdir()
            path = config_dir / "runtime.yaml"
            path.write_text(
                "gateway: {mode: fake}\nskills: {root: ../skills, active: alpha}\n",
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(config.skills.root, root.resolve())
            self.assertEqual(config.skills.active, "alpha")
            self.assertEqual(config.public()["skills"], {"root": str(root.resolve()), "active": "alpha"})

    def test_config_rejects_unknown_missing_and_escaping_skill_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            cases = (
                "skills: {root: skills, active: missing}\n",
                "skills: {root: skills, active: ../alpha}\n",
                "skills: {root: skills, active: alpha, extra: true}\n",
                "skills: {root: missing, active: alpha}\n",
            )
            for index, block in enumerate(cases):
                path = base / f"bad-{index}.yaml"
                path.write_text("gateway: {mode: fake}\n" + block, encoding="utf-8")
                with self.subTest(block=block), self.assertRaises(ValidationError):
                    load_config(path)

    def test_gateway_and_metadata_loader_share_the_resolver_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(root, "alpha", name="alpha-skill", description="Alpha")
            write_skill(root, "beta", name="beta-skill", description="Beta")
            resolver = ActiveSkillResolver(root, "alpha")
            gateway = AgentGateway(object(), skill_resolver=resolver)
            loader = LockedSkillMetadataLoader(skill_resolver=resolver)

            first_runtime = gateway.skill_factory()
            first_metadata = loader.load("narrative")
            resolver.reload("beta")
            second_runtime = gateway.skill_factory()
            second_metadata = loader.load("narrative")

            self.assertEqual(first_runtime.skill_name, "alpha-skill")
            self.assertEqual(second_runtime.skill_name, "beta-skill")
            self.assertEqual(first_metadata["version"], first_runtime.skill_version)
            self.assertEqual(second_metadata["version"], second_runtime.skill_version)
            self.assertNotEqual(first_metadata["version"], second_metadata["version"])

    def test_agent_job_can_read_an_alternate_skill_with_different_file_names(self):
        class Client:
            def __init__(self):
                self.turns = [
                    ModelTurn(None, "tool", (ModelToolCall("read_skill_file", '{"path":"SKILL.md"}', "call"),)),
                    ModelTurn('{"markdown":"alternate skill used"}', "final"),
                ]
                self.inputs = []

            def create(self, **kwargs):
                self.inputs.append(kwargs)
                return self.turns.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            write_skill(
                root,
                "alternate",
                name="alternate-skill",
                description="Alternate",
                files={"references/completely-different-name.md": "different"},
            )
            client = Client()
            gateway = AgentGateway(client, skill_resolver=ActiveSkillResolver(root, "alternate"))

            result = gateway.generate("narrative", {}, skill="ignored compatibility metadata")

            self.assertEqual(result["text"], "alternate skill used")
            self.assertEqual(gateway.runtime.skill.skill_name, "alternate-skill")
            read_tool = next(tool for tool in client.inputs[0]["tools"] if tool["name"] == "read_skill_file")
            self.assertEqual(
                read_tool["parameters"]["properties"]["path"]["enum"],
                ["SKILL.md"],
            )
            second_read_tool = next(tool for tool in client.inputs[1]["tools"] if tool["name"] == "read_skill_file")
            self.assertEqual(
                second_read_tool["parameters"]["properties"]["path"]["enum"],
                ["references/completely-different-name.md"],
            )


if __name__ == "__main__":
    unittest.main()
