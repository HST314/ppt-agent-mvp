import json
import unittest

from ppt_agent.diagnostics import (
    log_exception_chain,
    redact_diagnostic_payload,
    redact_diagnostic_text,
)


class DiagnosticRedactionTests(unittest.TestCase):
    SECRET_FIELD_NAMES = (
        "authorization",
        "proxy_authorization",
        "proxy-authorization",
        "api_key",
        "api-key",
        "access_token",
        "access-token",
        "refresh_token",
        "refresh-token",
        "auth_token",
        "auth-token",
        "bearer",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "client-secret",
        "private_key",
        "private-key",
        "credential",
        "credentials",
        "token",
    )

    def test_free_text_and_structured_secret_names_stay_aligned(self):
        for index, field_name in enumerate(self.SECRET_FIELD_NAMES):
            with self.subTest(field_name=field_name):
                sentinel = f"sentinel-{index}-must-not-leak"
                forms = (
                    f"{field_name}={sentinel}",
                    f"{field_name}: '{sentinel}'",
                    f'{{"{field_name}": "{sentinel}"}}',
                )
                for form in forms:
                    redacted = redact_diagnostic_text(form)
                    self.assertNotIn(sentinel, redacted)
                    self.assertIn("[REDACTED]", redacted)

                structured = redact_diagnostic_payload({field_name: sentinel})
                self.assertEqual(structured[field_name], "[REDACTED]")

    def test_log_payload_leaks_no_named_secret_sentinel_in_any_text_form(self):
        sentinels = []
        details = []
        for field_index, field_name in enumerate(self.SECRET_FIELD_NAMES):
            for form_index, template in enumerate(("{}={}", "{}: '{}'", '{{"{}": "{}"}}')):
                sentinel = f"log-sentinel-{field_index}-{form_index}-must-not-leak"
                sentinels.append(sentinel)
                details.append(template.format(field_name, sentinel))

        with self.assertLogs("ppt_agent.runtime", level="ERROR") as captured:
            log_exception_chain(
                RuntimeError("provider message is private"),
                diagnostic_id="diagnostic-test",
                probe_id="probe-test",
                context={"details": details},
            )

        serialized = captured.records[0].getMessage()
        payload = json.loads(serialized)
        self.assertEqual(len(payload["details"]), len(details))
        self.assertTrue(all("[REDACTED]" in item for item in payload["details"]))
        for sentinel in sentinels:
            self.assertNotIn(sentinel, serialized)

    def test_named_secret_matching_does_not_redact_a_field_name_suffix(self):
        value = "not-token=ordinary-value"
        self.assertEqual(redact_diagnostic_text(value), value)


if __name__ == "__main__":
    unittest.main()
