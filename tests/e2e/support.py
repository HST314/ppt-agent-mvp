import io
import json
import tempfile
import unittest

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"


class SampleJourney(unittest.TestCase):
    """Drive the same WSGI API used by the desktop sample page."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(self.tmp.name)
        self.app = App(TaskService(self.store))
        status, _ = self.call("POST", "/v1/tasks", {"task_id": "journey", "mode": "manual"})
        self.assertTrue(status.startswith("201"))
        self.store.put_resource("journey", "hero.png", PNG)
        self.ok("/v1/tasks/journey/input", {"source": {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3}})
        self.ok("/v1/tasks/journey/narrative/generate", {})
        self.ok("/v1/tasks/journey/narrative/confirm", {})
        outline = self.ok("/v1/tasks/journey/outline/generate", {})
        markdown = outline["outline"]["markdown"] + "\n![主视觉](resources://hero.png)\n"
        self.ok("/v1/tasks/journey/outline", {"markdown": markdown, "summary": "绑定真实样品资源"})
        self.ok("/v1/tasks/journey/outline/confirm", {})

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        seen = []
        result = b"".join(self.app({"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}, lambda status, headers: seen.append(status)))
        return seen[0], result

    def ok(self, path, body=None):
        status, raw = self.call("POST", path, body)
        self.assertTrue(status.startswith("200"), raw.decode())
        return json.loads(raw)

    def get_json(self, path):
        status, raw = self.call("GET", path)
        self.assertTrue(status.startswith("200"), raw.decode())
        return json.loads(raw)
