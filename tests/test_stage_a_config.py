import os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ppt_agent.config import load_config
from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.model_clients import OpenAIResponsesClient


ROOT = Path(__file__).resolve().parents[1]
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

    def test_repository_default_config_loads_with_referenced_environment(self):
        # The shipped default is the live Agent runtime and must load cleanly
        # once its referenced secrets exist without exposing them publicly.
        env={"MODEL_API_KEY":"secret","MODEL_BASE_URL":"https://provider.example/v1"}
        with patch.dict(os.environ,env,clear=True), patch("ppt_agent.config.load_dotenv"):
            cfg = load_config(ROOT / "config/ppt-agent.yaml")
        self.assertEqual(cfg.mode, "agent")
        self.assertEqual(cfg.generation.structured_output, "auto")
        self.assertNotIn("secret", str(cfg.public()))

        with patch.dict(os.environ,env,clear=True), patch("ppt_agent.config.load_dotenv"):
            example=load_config(ROOT / "config/ppt-agent.agent.example.yaml")
        self.assertEqual(example.mode,"agent")
        self.assertEqual(example.generation.structured_output,"auto")

    def test_timeout_split_defaults_and_legacy_alias(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1"}
        base="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n%s  inspection: {fallback_to_generation: true}\n"""
        cfg=self.config(base % "",env)
        self.assertEqual(cfg.generation.request_timeout_seconds,60)
        self.assertEqual(cfg.generation.run_timeout_seconds,300)
        self.assertEqual(cfg.generation.job_timeout_seconds,330)
        self.assertEqual(cfg.generation.max_tool_calls,24)
        self.assertEqual(cfg.generation.max_provider_calls,8)
        self.assertEqual(cfg.generation.stage_budgets["sample"].max_provider_calls,6)
        self.assertEqual(cfg.generation.stage_budgets["sample"].reserved_final_calls,2)
        cfg=self.config(base % "    timeout_seconds: 45\n",env)
        self.assertEqual(cfg.generation.request_timeout_seconds,45)
        self.assertEqual(cfg.generation.run_timeout_seconds,300)
        cfg=self.config(base % "    request_timeout_seconds: 30\n    run_timeout_seconds: 900\n",env)
        self.assertEqual(cfg.generation.request_timeout_seconds,30)
        self.assertEqual(cfg.generation.run_timeout_seconds,900)
        self.assertEqual(cfg.generation.job_timeout_seconds,930)
        with self.assertRaises(ValidationError):
            self.config(base % "    timeout_seconds: 30\n    request_timeout_seconds: 30\n",env)
        for invalid in ("    run_timeout_seconds: 5\n","    run_timeout_seconds: 1.5.2\n","    request_timeout_seconds: true\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.config(base % invalid,env)
        for invalid in (
            "    request_timeout_seconds: 300\n    run_timeout_seconds: 300\n",
            "    run_timeout_seconds: 300\n    job_timeout_seconds: 300\n",
            "    max_tool_calls: 0\n",
            "    max_provider_calls: true\n",
            "    stage_budgets: {sample: {max_provider_calls: 2, reserved_final_calls: 2}}\n",
            "    stage_budgets: {sample: {max_exploration_rounds: 9}}\n",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.config(base % invalid,env)

    def test_clarification_block_is_validated_and_carried(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1"}
        base="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n  inspection: {fallback_to_generation: true}\n"""
        cfg=self.config(base,env)
        self.assertEqual((cfg.clarification.max_questions_per_round,cfg.clarification.max_rounds,cfg.clarification.style),(3,3,"comprehensive"))
        cfg=self.config(base+"clarification: {max_questions_per_round: 5, max_rounds: 2, style: minimal}\n",env)
        self.assertEqual((cfg.clarification.max_questions_per_round,cfg.clarification.max_rounds,cfg.clarification.style),(5,2,"minimal"))
        self.assertIn("clarification",cfg.public())
        for invalid in ("clarification: {style: strict}\n","clarification: {max_rounds: 9}\n","clarification: {max_questions_per_round: 1.5}\n","clarification: {unknown: 1}\n","clarification: {max_questions_per_round: 0}\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.config(base+invalid,env)
        fake=self.config("gateway: {mode: fake}\nclarification: {style: minimal, max_rounds: 1}\n",{})
        self.assertEqual((fake.clarification.style,fake.clarification.max_rounds),("minimal",1))

    def test_global_job_and_review_settings_are_validated_and_carried(self):
        cfg=self.config(
            "gateway: {mode: fake}\n"
            "jobs: {generation_timeout_seconds: 91, inspection_timeout_seconds: 92, delivery_timeout_seconds: 93}\n"
            "review: {default_max_rounds: 4}\n",
            {},
        )
        self.assertEqual(cfg.jobs.generation_timeout_seconds,91)
        self.assertEqual(cfg.jobs.inspection_timeout_seconds,92)
        self.assertEqual(cfg.jobs.delivery_timeout_seconds,93)
        self.assertEqual(cfg.review.default_max_rounds,4)
        for invalid in (
            "jobs: {generation_timeout_seconds: 29}\n",
            "jobs: {unknown: 30}\n",
            "review: {default_max_rounds: 11}\n",
            "review: {unknown: 1}\n",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.config("gateway: {mode: fake}\n"+invalid,{})

    def test_inherited_crlf_environment_values_are_normalized(self):
        env={"GEN_KEY":"secret\r","GEN_URL":"https://gen.example/v1\r"}
        cfg=self.config(
            "gateway: {mode: agent}\nmodels:\n"
            "  generation: {provider: openai_responses, model: gen, api_key_env: GEN_KEY, base_url_env: GEN_URL}\n"
            "  inspection: {fallback_to_generation: true}\n",
            env,
        )
        self.assertEqual(cfg.generation.api_key,"secret")
        self.assertEqual(cfg.generation.base_url,"https://gen.example/v1")

    def test_structured_output_mode_is_validated(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1"}
        base="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n    structured_output: %s\n  inspection: {fallback_to_generation: true}\n"""
        for mode, expected in (("prompt", "prompt"), ("json_schema", "json_schema")):
            with self.subTest(mode=mode):
                self.assertEqual(self.config(base % mode, env).generation.structured_output, expected)
        for invalid in ("strict", "true", "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.config(base % invalid, env)

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

    def test_rejects_invalid_inspection_and_strict_scalar_types(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1"}
        base="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n"""
        invalid=(
            base + "  inspection: invalid\n",
            base + "  inspection: {fallback_to_generation: \"false\"}\n",
            base.replace("    base_url_env: GEN_URL\n", "    base_url_env: GEN_URL\n    max_steps: 1.9\n") + "  inspection: {fallback_to_generation: true}\n",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                self.config(text,env)

    def test_rejects_unsafe_base_urls_and_redacts_defensively(self):
        text="""gateway: {mode: agent}\nmodels:\n  generation:\n    provider: openai_responses\n    model: gen\n    api_key_env: GEN_KEY\n    base_url_env: GEN_URL\n  inspection: {fallback_to_generation: true}\n"""
        for url in ("https:///v1", "https://user:password@example.com/v1", "https://example.com/v1?token=secret", "https://example.com/v1#secret"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                self.config(text,{"GEN_KEY":"secret","GEN_URL":url})

        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1","INSPECT_KEY":"i","INSPECT_URL":"https://inspect.example/v1"}
        cfg=self.config(AGENT,env)
        object.__setattr__(cfg.generation,"base_url","https://user:password@example.com/v1?token=secret#fragment")
        snapshot=str(cfg.public())
        for secret in ("user", "password", "token", "secret", "fragment"):
            self.assertNotIn(secret,snapshot)

    def test_responses_request_and_error_mapping(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1","INSPECT_KEY":"i","INSPECT_URL":"https://inspect.example/v1"}
        cfg=self.config(AGENT,env).generation
        sdk=SDK(SimpleNamespace(output_text="ok",id="resp-1")); client=OpenAIResponsesClient(cfg,sdk_client=sdk)
        turn=client.create(input="hello",tools=[{"type":"function"}],response_schema={"name":"answer","schema":{"type":"object"}})
        self.assertEqual(turn.text,"ok"); self.assertEqual(sdk.seen["model"],"gen"); self.assertNotIn("api_key",str(sdk.seen))
        with self.assertRaises(GatewayUnknownResult): OpenAIResponsesClient(cfg,sdk_client=SDK(error=TimeoutError("secret"))).create(input="x")
        with self.assertRaises(GatewayError) as caught: OpenAIResponsesClient(cfg,sdk_client=SDK(result=SimpleNamespace(output_text=""))).create(input="x")
        self.assertNotIn("secret",str(caught.exception.public()))


class EmptyResponseRetryTests(unittest.TestCase):
    class SequenceSDK:
        def __init__(self, results): self.results=list(results); self.responses=self; self.calls=0
        def create(self, **_kwargs):
            self.calls+=1
            return self.results.pop(0)

    def config(self):
        env={"GEN_KEY":"secret","GEN_URL":"https://gen.example/v1","INSPECT_KEY":"i","INSPECT_URL":"https://inspect.example/v1"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=True):
            path = Path(tmp) / "config.yaml"; path.write_text(AGENT)
            return load_config(path).generation

    def test_empty_response_is_retried_within_the_same_call(self):
        cfg=self.config()
        empty=SimpleNamespace(output_text="", id="r-empty", output=[])
        ok=SimpleNamespace(output_text="ok", id="r-ok", output=[])
        sdk=self.SequenceSDK([empty, ok])
        turn=OpenAIResponsesClient(cfg,sdk_client=sdk).create(input="x")
        self.assertEqual(turn.text,"ok"); self.assertEqual(sdk.calls,2)

    def test_persistent_empty_response_fails_after_bounded_retries(self):
        cfg=self.config()
        empty=SimpleNamespace(output_text="", id="r-empty", output=[])
        sdk=self.SequenceSDK([empty, empty, empty, empty])
        with self.assertRaises(GatewayError) as caught:
            OpenAIResponsesClient(cfg,sdk_client=sdk).create(input="x")
        self.assertEqual(sdk.calls,3)  # 1 次原始请求 + 最多 2 次重试
        self.assertEqual(caught.exception.audit_details["category"],"empty_response")

    def test_tool_call_only_response_is_not_retried(self):
        cfg=self.config()
        call_only=SimpleNamespace(output_text="", id="r-call", output=[SimpleNamespace(type="function_call", name="read_skill_file", arguments='{"path":"SKILL.md"}', call_id="c")])
        sdk=self.SequenceSDK([call_only])
        turn=OpenAIResponsesClient(cfg,sdk_client=sdk).create(input="x")
        self.assertEqual(len(turn.tool_calls),1); self.assertEqual(sdk.calls,1)


if __name__ == "__main__": unittest.main()
