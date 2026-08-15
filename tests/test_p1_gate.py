import io,json,tempfile,unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.contracts import validate_instance
from ppt_agent.errors import ConflictError,GateError,ValidationError
from ppt_agent.fsm import Stage,TaskState,transition
from ppt_agent.schema import MODELS,ClarificationSet,DeckArtifact,DeliveryManifest,InspectionReport,IssueDisposition,NarrativeDocument,ResourceManifest,SampleSelection,SlideOutline,TaskCard,TaskInputSnapshot
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
 def test_json_arrays_and_field_semantics(self):
  common={"task_id":"task","schema_version":"1.0"}
  resource=ResourceManifest.parse({**common,"manifest_id":"manifest","resources":[],"content_hash":H,"created_at":NOW})
  clarification=ClarificationSet.parse({**common,"clarification_id":"clarify","questions":[],"assumptions":[],"confirmed":False})
  self.assertEqual(resource.resources,()); self.assertEqual(clarification.questions,())
  with self.assertRaises(ValidationError): NarrativeDocument.parse({**common,"document_id":"doc","version":1,"markdown":" ","content_hash":H,"created_at":NOW})
  with self.assertRaises(ValidationError): TaskInputSnapshot.parse({"snapshot_id":"snap","task_id":"task","task_card_hash":H,"resource_manifest_hash":H,"created_at":"2026-08-11T00:00:00","schema_version":"1.0"})
  with self.assertRaises(ValidationError): SlideOutline.parse({**common,"outline_id":"outline","version":1,"markdown":"# ok","slide_ids":["bad/id"],"content_hash":H,"created_at":NOW})
  with self.assertRaises(ValidationError): DeckArtifact.parse({**common,"artifact_id":"artifact","version":1,"kind":"zip","outline_hash":H,"content_hash":H,"created_at":NOW})
 def test_exported_schema_and_runtime_are_bidirectionally_equivalent(self):
  common={"task_id":"task","schema_version":"1.0"}
  fixtures={
   TaskCard:{**common,"goal":"Build a deck","audience":"reviewers","topic":"P1","source_format":"json"},
   TaskInputSnapshot:{**common,"snapshot_id":"snapshot","task_card_hash":H,"resource_manifest_hash":H,"created_at":NOW},
   ResourceManifest:{**common,"manifest_id":"manifest","resources":[{"resource_id":"source-1","uri":"asset://source-1","media_type":"text/markdown","content_hash":H}],"content_hash":H,"created_at":NOW},
   ClarificationSet:{**common,"clarification_id":"clarify","questions":["Which audience?"],"assumptions":["Internal review"],"confirmed":False},
   NarrativeDocument:{**common,"document_id":"doc","version":1,"markdown":"# valid","content_hash":H,"created_at":NOW},
   SlideOutline:{**common,"outline_id":"outline","version":1,"markdown":"# outline","slide_ids":["slide-1"],"content_hash":H,"created_at":NOW},
   SampleSelection:{**common,"selection_id":"selection","outline_hash":H,"slide_ids":["slide-1"],"confirmed":True},
   DeckArtifact:{**common,"artifact_id":"artifact","version":1,"kind":"deck","outline_hash":H,"content_hash":H,"created_at":NOW},
   InspectionReport:{**common,"report_id":"report","deck_hash":H,"issues":[{"issue_id":"overflow-1","severity":"blocker","code":"text_overflow","message":"overflow","slide_id":"slide-1"}],"passed":False,"created_at":NOW},
   IssueDisposition:{**common,"disposition_id":"disposition","issue_id":"overflow-1","action":"resolve","actor":"user","created_at":NOW},
   DeliveryManifest:{**common,"delivery_id":"delivery","deck_hash":H,"files":["deck.html"],"confirmed_by":"user","confirmed_at":NOW}}
  # Every published model must accept the same legal JSON instance at both
  # boundaries and preserve that decision after a runtime round-trip.
  self.assertEqual(set(fixtures),set(MODELS))
  for model,fixture in fixtures.items():
   validate_instance(fixture,model.json_schema())
   parsed=model.parse(fixture)
   validate_instance(parsed.to_dict(),model.json_schema())
   model.parse(parsed.to_dict())
  # One semantic counterexample per model. This includes all three audit
  # reproductions: blank markdown, blank delivery file and blank question.
  invalid={
   TaskCard:{**fixtures[TaskCard],"goal":" \t"},
   TaskInputSnapshot:{**fixtures[TaskInputSnapshot],"snapshot_id":" "},
   ResourceManifest:{**fixtures[ResourceManifest],"resources":[{**fixtures[ResourceManifest]["resources"][0],"uri":"\n "}]},
   ClarificationSet:{**fixtures[ClarificationSet],"questions":[""]},
   NarrativeDocument:{**fixtures[NarrativeDocument],"markdown":" "},
   SlideOutline:{**fixtures[SlideOutline],"markdown":"\n\t"},
   SampleSelection:{**fixtures[SampleSelection],"slide_ids":[""]},
   DeckArtifact:{**fixtures[DeckArtifact],"kind":" "},
   InspectionReport:{**fixtures[InspectionReport],"issues":[{**fixtures[InspectionReport]["issues"][0],"message":" "}]},
   IssueDisposition:{**fixtures[IssueDisposition],"action":" "},
   DeliveryManifest:{**fixtures[DeliveryManifest],"files":[""]}}
  self.assertEqual(set(invalid),set(MODELS))
  for model,value in invalid.items():
   schema_accepts=runtime_accepts=True
   try: validate_instance(value,model.json_schema())
   except ValidationError: schema_accepts=False
   try: model.parse(value)
   except ValidationError: runtime_accepts=False
   self.assertEqual(schema_accepts,runtime_accepts,model.__name__)
   self.assertFalse(schema_accepts,model.__name__)
  for bad in (0,-1):
   value={**fixtures[NarrativeDocument],"version":bad}
   with self.assertRaises(ValidationError): validate_instance(value,NarrativeDocument.json_schema())
   with self.assertRaises(ValidationError): NarrativeDocument.parse(value)
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
 def test_openapi_and_preview_bypass_is_absent(self):
  import yaml
  text=Path("docs/openapi.yaml").read_text(); spec=yaml.safe_load(text)
  for token in ("requestBody:","Error:","/versions:","/versions/compare:","/issues/{issue_id}/disposition:"): self.assertIn(token,text)
  self.assertNotIn("/preview:",text)
  for path,item in spec["paths"].items():
   for operation in item.values():
    responses=operation["responses"]; success=next(v for k,v in responses.items() if str(k).startswith("2"))
    media=next(iter(success["content"].values())); self.assertIn("schema",media,path); self.assertIn("example",media,path)
    validate_instance(media["example"],media["schema"],spec)
    if path not in {"/healthz","/livez","/readyz","/v1/runtime/recheck"}: self.assertTrue(any(v.get("$ref")=="#/components/responses/Error" for k,v in responses.items() if not str(k).startswith("2")),path)
  with tempfile.TemporaryDirectory() as p:
   app=App(TaskService(WorkspaceStore(p))); self.assertEqual(self.call(app,"POST","/v1/tasks",{"task_id":"t","mode":"auto"})[0],201)
   code,result=self.call(app,"POST","/v1/tasks/t/preview"); self.assertEqual(code,404)
   self.assertEqual(app.service.get("t")["status"],"ready")
   self.assertEqual(app.service.versions("t"),[])

if __name__=="__main__": unittest.main()
