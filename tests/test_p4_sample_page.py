"""P4 sample-stage regressions after migration to the FastAPI single shell."""

import io
import json
from pathlib import Path
import tempfile
import unittest

from ppt_agent.api import App
from ppt_agent.errors import ValidationError
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


ROOT = Path(__file__).resolve().parents[1]


class SamplePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = TaskService(WorkspaceStore(self.tmp.name))
        self.app = App(self.svc)
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task")
        self.svc.confirm_narrative("task")
        self.svc.generate_outline("task")
        self.svc.confirm_outline("task")

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        status, headers = [], []
        result = b"".join(self.app({
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }, lambda value, response_headers: (status.append(value), headers.append(dict(response_headers)))))
        parsed = json.loads(result) if result.startswith((b"{", b"[")) else result.decode()
        return int(status[0].split()[0]), headers[0], parsed

    def act(self, action, body=None):
        status, _headers, result = self.call("POST", f"/v1/tasks/task/samples/{action}", body)
        self.assertEqual(status, 200, result)
        return result

    def make_three_versions(self):
        first = self.act("generate", {})
        slide_id = first["selection"]["slide_ids"][0]
        self.act("modify", {"prompt": "标题更醒目", "scope": "page", "slide_id": slide_id})
        self.act("modify", {"prompt": "改为蓝色", "scope": "element", "slide_id": slide_id, "element_id": "title"})
        return slide_id

    def test_timeline_metadata_and_history_are_authoritative_api_data(self):
        slide_id = self.make_three_versions()
        view = self.svc.sample_view("task")
        self.assertEqual(len(view["versions"]), 3)
        summaries = [item["metadata"]["summary"] for item in view["versions"]]
        self.assertIn("标题更醒目", summaries)
        self.assertIn("改为蓝色", summaries)
        self.assertEqual(view["versions"][-1]["metadata"]["element_id"], "title")
        self.assertIn(slide_id, view["sample"]["html"])

    def test_every_sample_version_has_a_sandbox_preview_endpoint(self):
        self.make_three_versions()
        for record in self.svc.sample_view("task")["versions"]:
            status, headers, body = self.call("GET", f'/v1/tasks/task/previews/{record["hash"]}')
            self.assertEqual(status, 200)
            self.assertIn("script-src 'none'", headers["content-security-policy"])
            self.assertIn("<!doctype html>", body)

    def test_compare_contract_and_single_shell_module_controls(self):
        self.make_three_versions()
        versions = self.svc.sample_view("task")["versions"]
        comparison = self.svc.compare("task", versions[-2]["hash"], versions[-1]["hash"])
        self.assertNotEqual(comparison["left"], comparison["right"])
        self.assertFalse(comparison["equal"])
        module = (ROOT / "frontend/static/js/stages/sample.js").read_text()
        for token in ("sample-compare-left", "sample-compare-right", "对比样品版本", "previewUrl"):
            self.assertIn(token, module)

    def test_modify_confirm_and_selection_changes_preserve_history(self):
        generated = self.act("generate", {})
        self.act("confirm", {})
        self.assertTrue(self.svc.sample_view("task")["confirmation"])
        self.act("modify", {"prompt": "统一加深背景", "scope": "global"})
        self.assertFalse(self.svc.sample_view("task")["confirmation"])
        ids = list(reversed(generated["selection"]["slide_ids"]))
        self.act("select", {"slide_ids": ids})
        self.assertGreaterEqual(len(self.svc.sample_view("task")["versions"]), 2)

    def test_empty_and_single_version_states_are_expressed_in_module(self):
        module = (ROOT / "frontend/static/js/stages/sample.js").read_text()
        self.assertIn("至少生成两个样品版本后可并排对比", module)
        self.assertIsNone(self.svc.sample_view("task")["sample"])
        self.act("generate", {})
        self.assertEqual(len(self.svc.sample_view("task")["versions"]), 1)

    def test_prompt_markup_remains_data_and_never_enters_shell_html(self):
        generated = self.act("generate", {})
        slide_id = generated["selection"]["slide_ids"][0]
        prompt = "</script><script>alert(1)</script>"
        self.act("modify", {"prompt": prompt, "scope": "page", "slide_id": slide_id})
        status, _headers, shell = self.call("GET", "/tasks/task/samples")
        self.assertEqual(status, 200)
        self.assertNotIn(prompt, shell)
        self.assertEqual(shell.count("</script>"), 1)

    def test_auto_scope_and_ambiguity_are_domain_validated(self):
        generated = self.act("generate", {})
        slide_id = generated["selection"]["slide_ids"][0]
        modified = self.act("modify", {"prompt": "当前页标题更醒目", "slide_id": slide_id})
        self.assertEqual(modified["sample"]["metadata"]["scope_understanding"]["scope"], "page")
        with self.assertRaises(ValidationError):
            self.svc.modify_sample("task", "统一所有页，但只改当前页", slide_id=slide_id)

    def test_stage_module_uses_explicit_dom_ids_and_no_inline_html_sink(self):
        module = (ROOT / "frontend/static/js/stages/sample.js").read_text()
        all_js = "\n".join(path.read_text() for path in (ROOT / "frontend/static/js").rglob("*.js"))
        for control in ("sample-prompt", "sample-scope", "sample-slide", "sample-element", "sample-preview"):
            self.assertIn(control, module)
        self.assertNotIn("innerHTML", all_js)
        self.assertIn("确认当前样品并进入全稿", module)


if __name__ == "__main__":
    unittest.main()
