import os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ppt_agent.config import load_config
from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.model_clients import OpenAIResponsesClient


AGENT = """gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n    timeout_seconds: 30\n    max_steps: 12\n  inspection:\n    provider: openai_responses\n    model: inspect\n    api_key_env: INSPECT_KEY\n    base_url_env: INSPECT_URL\n    timeout_seconds: 20\n    max_steps: 6\n    fallback_to_generation: true\n"""


class SDK:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.seen = result, error, None
        self.responses = self
    def create(self, **kwargs):
        self.seen = kwargs
        if self.error: raise self.error
        return self.result


class StageAConfigTests(unittest.TestCase):
    def config(self, text, env):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            path = Path(tmp) / "config.yaml"; path.write_text(text)
            return load_config(path)

    def test_fake_needs_no_secrets(self):
        cfg = self.config("gateway: {mode: fake}\n", {})
        self.assertEqual(cfg.mode, "fake")

    def test_separate_models_and_redacted_snapshot(self):
        env={"GEN_KEY":"generation-secret","GEN_URL":"https://gen.example/v1","INSPECT_KEY":"inspection-secret","INSPECT_URL":"https://inspect.example/v1"}
        cfg=self.config(AGENT,env)
        self.assertEqual(cfg.generation.model,"gen"); self.assertEqual(cfg.inspection.model,"inspect")
        snapshot=str(cfg.public()); self.assertNotIn("generation-secret",snapshot); self.assertNotIn("inspection-secret",snapshot)

    def test_inspection_falls_back_when_explicitly_enabled(self):
        # An omitted inspection block uses an explicit placeholder with fallback only.
        text="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n  inspection:\n    fallback_to_generation: true\n"""
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1"}
        cfg=self.config(text,env); self.assertIs(cfg.generation,cfg.inspection)

    def test_strict_validation_and_secret_safe_errors(self):
        with self.assertRaises(ValidationError): self.config("gateway: {mode: invalid}\n",{})
        with self.assertRaises(ValidationError): self.config("gateway: {mode: fake}\ncapabilities: {network_tools: true}\n",{})
        with self.assertRaises(ValidationError) as caught: self.config(AGENT,{"GEN_KEY":"secret-value"})
        self.assertNotIn("secret-value",str(caught.exception.public()))

    def test_responses_request_and_error_mapping(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1","INSPECT_KEY":"i","INSPECT_URL":"https://inspect.example/v1"}
        cfg=self.config(AGENT,env).generation
        sdk=SDK(SimpleNamespace(output_text="ok",id="resp-1")); client=OpenAIResponsesClient(cfg,sdk_client=sdk)
        turn=client.create(input="hello",tools=[{"type":"function"}],response_schema={"name":"answer","schema":{"type":"object"}})
        self.assertEqual(turn.text,"ok"); self.assertEqual(sdk.seen["model"],"gen"); self.assertNotIn("api_key",str(sdk.seen))
        with self.assertRaises(GatewayUnknownResult): OpenAIResponsesClient(cfg,sdk_client=SDK(error=TimeoutError("secret"))).create(input="x")
        with self.assertRaises(GatewayError) as caught: OpenAIResponsesClient(cfg,sdk_client=SDK(result=SimpleNamespace(output_text=""))).create(input="x")
        self.assertNotIn("secret",str(caught.exception.public()))


if __name__ == "__main__": unittest.main()
