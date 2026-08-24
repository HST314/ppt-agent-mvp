from __future__ import annotations

import unittest

from ppt_agent.generation.contracts import NarrativeSpec
from ppt_agent.generation.errors import ModelResultUnknown, ModelTransportError
from ppt_agent.generation.model_gateway import ModelGateway, ProviderResponse

from .support import ContractProvider, TransportFailure


class ModelGatewayTests(unittest.TestCase):
    def setUp(self):
        self.provider = ContractProvider()
        self.audits = []
        self.gateway = ModelGateway(self.provider, model="test-model", audit_sink=self.audits.append, secret_values=("secret-sentinel",))

    def test_json_schema_and_local_contract_are_both_enforced(self):
        result = self.gateway.generate(NarrativeSpec, input=[{"role": "system", "content": "x"}, {"role": "user", "content": '{"input":{}}'}], idempotency_key="narrative-1", stage="narrative")
        self.assertEqual(result.contract.schema_version, "1.0")
        schema = self.provider.calls[0]["response_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["name"], "narrative_spec_v1")

    def test_pre_dispatch_transient_failure_has_one_bounded_retry(self):
        self.provider.failure = TransportFailure(request_sent=False)
        result = self.gateway.generate(NarrativeSpec, input=[{"role": "system", "content": "x"}, {"role": "user", "content": '{"input":{}}'}], idempotency_key="narrative-2", stage="narrative")
        self.assertEqual(result.provider_calls, 2)

    def test_known_invalid_contract_gets_one_bounded_schema_correction(self):
        original_create = self.provider.create
        calls = 0

        def invalid_then_valid(**request):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.provider.calls.append(request)
                return ProviderResponse("resp-invalid", {"schema_version": "1.0"})
            return original_create(**request)

        self.provider.create = invalid_then_valid
        result = self.gateway.generate(NarrativeSpec, input=[{"role": "system", "content": "x"}, {"role": "user", "content": '{"input":{}}'}], idempotency_key="narrative-correction", stage="narrative")
        self.assertEqual(result.provider_calls, 2)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(self.audits[-2]["status"], "schema_correction")

    def test_response_id_is_retrieved_before_any_replay(self):
        response = ProviderResponse("resp-stored", {
            "schema_version": "1.0", "thesis": "结论", "audience_takeaway": "行动",
            "story_arc": [{"beat_id": "a", "purpose": "背景", "message": "信息"}, {"beat_id": "b", "purpose": "决策", "message": "行动"}],
            "evidence_refs": [], "tone": "清晰",
        })
        self.provider.responses[response.response_id] = response
        self.provider.failure = TransportFailure(request_sent=True, response_id=response.response_id)
        result = self.gateway.generate(NarrativeSpec, input=[], idempotency_key="narrative-3", stage="narrative")
        self.assertEqual(result.recovery_count, 1)
        self.assertEqual(len(self.provider.calls), 1)

    def test_unknown_dispatched_result_never_replays(self):
        self.provider.failure = TransportFailure(request_sent=True)
        with self.assertRaises(ModelResultUnknown):
            self.gateway.generate(NarrativeSpec, input=[], idempotency_key="narrative-4", stage="narrative")
        self.assertEqual(len(self.provider.calls), 1)

    def test_explicit_provider_rejection_is_known_and_never_replayed(self):
        error = RuntimeError("rejected")
        error.status = 400
        error.audit_details = {"http_status": 400, "result_certainty": "known"}
        self.provider.failure = error
        with self.assertRaises(ModelTransportError):
            self.gateway.generate(NarrativeSpec, input=[], idempotency_key="narrative-400", stage="narrative")
        self.assertEqual(len(self.provider.calls), 1)

    def test_same_idempotency_key_returns_one_authoritative_result(self):
        first = self.gateway.generate(NarrativeSpec, input=[{"role": "system", "content": "x"}, {"role": "user", "content": '{"input":{}}'}], idempotency_key="same", stage="narrative")
        second = self.gateway.generate(NarrativeSpec, input=[], idempotency_key="same", stage="narrative")
        self.assertIs(first, second)
        self.assertEqual(len(self.provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
