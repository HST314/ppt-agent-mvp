import io,json,tempfile,unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.errors import ConflictError,GateError,ValidationError
from ppt_agent.fsm import Stage,TaskState,transition
from ppt_agent.schema import MODELS,TaskInputSnapshot
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

H="0"*64; NOW="2026-08-11T00:00:00+00:00"

class NegativeContractTests(unittest.TestCase):
 def test_actor_and_mode_cannot_bypass(self):
  with self.assertRaises(GateError): transition(TaskState("t",stage=Stage.SAMPLE),"confirm_sample",actor="robot")
  with self.assertRaises(GateError): transition(TaskState("t"),"resolve_blockers",actor="system")
  with tempfile.TemporaryDirectory() as p:
   with self.assertRaises(ValidationError): TaskService(WorkspaceStore(p)).create("t","turbo")
 def test_all_schemas_are_typed_required_and_reject_wrong_types(self):
  for model in MODELS:
   schema=model.json_schema(); self.assertTrue(schema["required"]); self.assertTrue(all("type" in x for x in schema["properties"].values()))
  good={"snapshot_id":"snap","task_id":"task","task_card_hash":H,"resource_manifest_hash":H,"created_at":NOW,"schema_version":"1.0"}
  TaskInputSnapshot.parse(good)
  for key,bad in (("snapshot_id",1),("task_id",[]),("task_card_hash",{}),("created_at","yesterday")):
   with self.assertRaises(ValidationError): TaskInputSnapshot.parse({**good,key:bad})
 def test_kind_escape_rejected(self):
  with tempfile.TemporaryDirectory() as p:
   s=WorkspaceStore(p); s.create("t",TaskState("t").to_dict())
   with self.assertRaises(ValidationError): s.put_version("t","../../escaped",b"x",{})

class RecoveryAndIdempotencyTests(unittest.TestCase):
 def test_fault_after_prepare_recovers_without_partial_state(self):
  with tempfile.TemporaryDirectory() as p:
   def fault(stage):
    if stage=="after_prepare": raise RuntimeError("power loss")
   svc=TaskService(WorkspaceStore(p,fault)); svc.create("t")
   with self.assertRaises(RuntimeError): svc.command("t","c","advance")
   recovered=TaskService(WorkspaceStore(p)); self.assertEqual(recovered.get("t")["stage"],"clarification"); self.assertEqual(len(recovered.events("t")),1)
 def test_fault_after_event_recovers_exactly_once(self):
  with tempfile.TemporaryDirectory() as p:
   def fault(stage):
    if stage=="after_event": raise RuntimeError("power loss")
   s=WorkspaceStore(p,fault); svc=TaskService(s); svc.create("t")
   with self.assertRaises(RuntimeError): svc.command("t","c","advance")
   recovered=TaskService(WorkspaceStore(p)); self.assertEqual(recovered.get("t")["stage"],"clarification"); self.assertEqual(len(recovered.events("t")),1)
 def test_replay_is_original_and_request_drift_conflicts(self):
  with tempfile.TemporaryDirectory() as p:
   svc=TaskService(WorkspaceStore(p)); svc.create("t","auto"); first=svc.command("t","one","advance"); svc.command("t","two","advance")
   self.assertEqual(svc.command("t","one","advance"),first)
   with self.assertRaises(ConflictError): svc.command("t","one","advance","user")

class ApiIntegrationTests(unittest.TestCase):
 def call(self,app,method,path,body=None):
  raw=json.dumps(body or {}).encode(); status=[]
  out=b"".join(app({"REQUEST_METHOD":method,"PATH_INFO":path,"CONTENT_LENGTH":str(len(raw)),"wsgi.input":io.BytesIO(raw)},lambda s,h:status.append(s)))
  return int(status[0][:3]),json.loads(out)
 def test_openapi_and_fake_artifact_path(self):
  text=Path("docs/openapi.yaml").read_text()
  for token in ("requestBody:","Error:","/versions:","/versions/compare:","/preview:","/issues/{issue_id}/disposition:"): self.assertIn(token,text)
  with tempfile.TemporaryDirectory() as p:
   app=App(TaskService(WorkspaceStore(p))); self.assertEqual(self.call(app,"POST","/v1/tasks",{"task_id":"t","mode":"auto"})[0],201)
   code,result=self.call(app,"POST","/v1/tasks/t/preview"); self.assertEqual(code,200); self.assertTrue(result["passed"])
   code,versions=self.call(app,"GET","/v1/tasks/t/versions"); self.assertEqual(code,200); self.assertEqual(len(versions["versions"]),3)

if __name__=="__main__": unittest.main()
