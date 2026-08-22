import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ppt_agent.config import FeatureFlagsConfig
from ppt_agent.errors import RuntimeUnavailableError, ValidationError
from ppt_agent.gateways import AgentGateway, LockedSkillMetadataLoader, agent_gateways_from_config
from ppt_agent.skill_runtime import ActiveSkillResolver, SkillRuntime


class P8GatewayTests(unittest.TestCase):
    def test_fake_mode_has_no_model_adapters(self):
        self.assertEqual(agent_gateways_from_config(SimpleNamespace(mode="fake")), {})

    def test_agent_mode_injects_one_resolver_into_every_current_port(self):
        resolver = ActiveSkillResolver.builtin()
        config = SimpleNamespace(
            mode="agent",
            skills=SimpleNamespace(root=resolver.root, active=resolver.active),
            feature_flags=FeatureFlagsConfig(),
            generation=SimpleNamespace(
                max_steps=12,
                max_tool_calls=24,
                max_provider_calls=8,
                run_timeout_seconds=300,
                job_timeout_seconds=330,
                model="generation",
                stage_budgets={},
            ),
            inspection=SimpleNamespace(
                max_steps=12,
                max_tool_calls=24,
                max_provider_calls=8,
                run_timeout_seconds=300,
                job_timeout_seconds=330,
                model="inspection",
            ),
        )
        clients = {"generation": object(), "inspection": object()}
        with patch("ppt_agent.model_clients.model_clients_from_config", return_value=clients):
            ports = agent_gateways_from_config(config)

        self.assertIs(ports["generator"], ports["builder"])
        self.assertIs(ports["generator"], ports["clarifier"])
        self.assertIs(ports["generator"].skill_resolver, ports["inspector"].skill_resolver)
        metadata = ports["skills"].load("outline")
        self.assertEqual(metadata["protocol"], "skill_runtime_v2")
        self.assertEqual(metadata["version"], ports["generator"].skill_factory().skill_version)

    def test_explicit_injection_is_required_after_legacy_loader_removal(self):
        with self.assertRaisesRegex(ValidationError, "显式注入"):
            AgentGateway(object())
        with self.assertRaisesRegex(ValidationError, "显式注入"):
            LockedSkillMetadataLoader()

    def test_disabled_skill_rollout_fails_closed_without_legacy_fallback(self):
        skill = SkillRuntime.builtin()
        gateway = AgentGateway(object(), skill=skill, skill_runtime_v2_enabled=False)
        loader = LockedSkillMetadataLoader(skill=skill, skill_runtime_v2_enabled=False)
        with self.assertRaises(RuntimeUnavailableError) as gateway_error:
            gateway._run("narrative", {})
        with self.assertRaises(RuntimeUnavailableError) as loader_error:
            loader.load("narrative")
        self.assertEqual(gateway_error.exception.failed_check, "skill_runtime_v2")
        self.assertEqual(loader_error.exception.failed_check, "skill_runtime_v2")


if __name__ == "__main__":
    unittest.main()
