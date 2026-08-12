import json, os, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from ppt_agent.errors import GatewayError, GatewayUnknownResult, ValidationError
from ppt_agent.gateways import DirectorySkillLoader, JsonHttpModelGateway, gateways_from_env

class Handler(BaseHTTPRequestHandler):
    response={"text":"generated"}; seen=[]
    def do_POST(self):
        length=int(self.headers["Content-Length"]); type(self).seen.append((dict(self.headers),json.loads(self.rfile.read(length))))
        raw=json.dumps(type(self).response).encode(); self.send_response(200); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self,*args): pass

class P8GatewayTests(unittest.TestCase):
    def setUp(self):
        Handler.seen=[]; Handler.response={"text":"generated"}; self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.endpoint=f"http://127.0.0.1:{self.server.server_port}/model"
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join()
    def test_generation_and_independent_inspection_contracts(self):
        gateway=JsonHttpModelGateway(self.endpoint,"fixture","secret",1)
        result=gateway.generate("outline",{"task_id":"t"},skill="rules")
        self.assertEqual(result["text"],"generated"); self.assertEqual(Handler.seen[0][1]["purpose"],"generation")
        Handler.response={"passed":True,"issues":[]}
        inspector=JsonHttpModelGateway(self.endpoint,"checker","secret",1,"independent_inspection")
        self.assertTrue(inspector.inspect("outline","<html>")["passed"])
        sent=Handler.seen[-1][1]; self.assertEqual(set(sent)-{"model","purpose"},{"original_outline","html"}); self.assertNotIn("generation_context",sent)
    def test_invalid_response_and_unknown_result_are_publicly_sanitized(self):
        Handler.response={"unexpected":True}
        with self.assertRaises(GatewayError) as caught: JsonHttpModelGateway(self.endpoint,"m","top-secret",1).generate("x",{},skill="s")
        self.assertNotIn("top-secret",json.dumps(caught.exception.public()))
        with self.assertRaises(GatewayUnknownResult): JsonHttpModelGateway("http://127.0.0.1:1/model","m","top-secret",.05).generate("x",{},skill="s")
    def test_endpoint_and_skill_boundaries(self):
        with self.assertRaises(ValidationError): JsonHttpModelGateway("http://example.test/model","m")
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp,"outline.md"),"w") as handle: handle.write("outline rules")
            loader=DirectorySkillLoader(tmp); self.assertEqual(loader.load("outline")["content"],"outline rules")
            with self.assertRaises(ValidationError): loader.load("../secret")
    def test_fake_is_default_and_http_config_is_explicit(self):
        with patch.dict(os.environ,{},clear=True): self.assertEqual(gateways_from_env(),{})
        with patch.dict(os.environ,{"PPT_AGENT_GATEWAY_MODE":"http"},clear=True):
            with self.assertRaises(ValidationError): gateways_from_env()

if __name__=="__main__": unittest.main()
