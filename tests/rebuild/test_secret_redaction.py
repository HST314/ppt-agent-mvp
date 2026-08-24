from __future__ import annotations

import json
import unittest

from ppt_agent.generation.model_gateway import redact


class SecretRedactionTests(unittest.TestCase):
    def test_nested_credentials_and_bearer_values_are_removed(self):
        sentinel = "secret-sentinel"
        value = redact({"api_key": sentinel, "message": f"Authorization: Bearer {sentinel}", "nested": [{"token": sentinel}], "safe": "keep"}, secret_values=(sentinel,))
        serialized = json.dumps(value)
        self.assertNotIn(sentinel, serialized)
        self.assertIn("keep", serialized)
        self.assertGreaterEqual(serialized.count("[REDACTED]"), 3)


if __name__ == "__main__":
    unittest.main()
