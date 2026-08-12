import io, json, logging, tempfile, unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.p2 import scan_resources
from ppt_agent.p4 import controlled_assets
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class P8ReleaseTests(unittest.TestCase):
    def request(self, app, path, body=b"{}", declared=None):
        response={}
        def start(status, headers): response["status"]=int(status.split()[0])
        env={"REQUEST_METHOD":"POST","PATH_INFO":path,"CONTENT_LENGTH":str(len(body) if declared is None else declared),"wsgi.input":io.BytesIO(body)}
        raw=b"".join(app(env,start)); return response["status"],json.loads(raw)

    def test_request_body_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            status,payload=self.request(App(TaskService(WorkspaceStore(tmp))),"/v1/tasks",declared=2*1024*1024+1)
            self.assertEqual(status,400); self.assertIn("2 MiB",payload["error"]["message"])

    def test_nested_resource_contract_and_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); nested=root/"brand"; nested.mkdir()
            png=b"\x89PNG\r\n\x1a\n"+b"\0\0\0\rIHDR"+b"\0\0\0\1\0\0\0\1"+b"\x08\x06\0\0\0"+b"payload"
            (nested/"hero.png").write_bytes(png)
            resources,_=scan_resources(root)
            self.assertEqual(resources[0]["uri"],"resources://brand/hero.png")
            self.assertIn(resources[0]["uri"],controlled_assets({"resources":resources},root))
            (nested/"huge.png").write_bytes(b"x"*(16*1024*1024+1))
            _,warnings=scan_resources(root)
            self.assertIn("resource_too_large",[w["code"] for w in warnings])

    def test_action_metric_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App(TaskService(WorkspaceStore(tmp))); stream=io.StringIO(); handler=logging.StreamHandler(stream); root=logging.getLogger(); old=root.level
            root.addHandler(handler); root.setLevel(logging.INFO)
            try: self.request(app,"/v1/tasks",json.dumps({"task_id":"metric"}).encode())
            finally: root.removeHandler(handler); root.setLevel(old)
            records=[json.loads(line) for line in stream.getvalue().splitlines() if '"event": "action_metric"' in line]
            self.assertEqual(len(records),1); self.assertEqual(records[0]["action"],"POST /v1/tasks")
            self.assertEqual(records[0]["status"],201); self.assertFalse(records[0]["failed"])
            self.assertIn("duration_ms",records[0]); self.assertIn("diagnostic_id",records[0])


if __name__ == "__main__": unittest.main()
