import json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from ppt_agent.errors import ConflictError,GateError,ValidationError
from ppt_agent.fsm import RunStatus,Stage,TaskState,transition
from ppt_agent.gateways import FakeInspectionGateway
from ppt_agent.schema import MODELS,TaskCard
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

class SchemaTests(unittest.TestCase):
 def test_strict_and_export(self):
  card=TaskCard.parse({"task_id":"t","goal":"g","audience":"a","topic":"x","source_format":"json","schema_version":"1.0"}); self.assertEqual(card.topic,"x")
  with self.assertRaises(ValidationError):TaskCard.parse({**card.to_dict(),"secret":"x"})
  self.assertEqual(len(MODELS),11); self.assertTrue(all(m.json_schema()["additionalProperties"] is False for m in MODELS))
class FSMTests(unittest.TestCase):
 def test_human_gates(self):
  s=TaskState("t",stage=Stage.SAMPLE,mode="auto")
  with self.assertRaises(GateError):transition(s,"advance")
  with self.assertRaises(GateError):transition(s,"confirm_sample",actor="system")
  s=transition(s,"confirm_sample",actor="user"); self.assertEqual(s.stage,Stage.DECK)
  self.assertEqual(transition(s,"advance").stage,Stage.REVIEW)
  d=TaskState("t",stage=Stage.DELIVERY,blockers_resolved=True)
  with self.assertRaises(GateError):transition(d,"confirm_delivery",actor="system")
  self.assertEqual(transition(d,"confirm_delivery",actor="user").status,RunStatus.COMPLETED)
 def test_waiting_not_failed_and_mode_future_only(self):
  s=TaskState("t",stage=Stage.OUTLINE,mode="manual"); s=transition(s,"advance"); self.assertEqual(s.status,RunStatus.WAITING_FOR_USER)
  s2=transition(s,"switch_auto"); self.assertEqual(s2.stage,s.stage)
class StoreTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=WorkspaceStore(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_versions_restart_and_isolation(self):
  self.store.create("a",TaskState("a").to_dict()); h=self.store.put_version("a","outline",b"abc",{"v":1}); self.assertEqual(len(h),64)
  self.assertEqual(WorkspaceStore(self.tmp.name).checkpoint("a")["task_id"],"a")
  with self.assertRaises(ValidationError):self.store.checkpoint("../a")
  with self.assertRaises(ConflictError):self.store.put_version("a","outline",b"abc",{"v":2})
 def test_concurrent_atomic_writes_are_complete_json(self):
  self.store.create("a",TaskState("a").to_dict())
  values=[{"task_id":"a","stage":"created","status":"ready","mode":"manual","sample_confirmed":False,"blockers_resolved":False,"delivery_confirmed":False,"revision":i} for i in range(20)]
  threads=[threading.Thread(target=self.store.atomic_json,args=(Path(self.tmp.name)/"a"/"checkpoint.json",v)) for v in values]
  [t.start() for t in threads];[t.join() for t in threads]
  parsed=json.loads((Path(self.tmp.name)/"a"/"checkpoint.json").read_text());self.assertIn(parsed,values)
 def test_persisted_chinese_is_always_read_as_utf8(self):
  self.store.create("cn",TaskState("cn").to_dict()); digest=self.store.put_version("cn","clarification","中文".encode(),{"title":"中文需求"})
  original=Path.read_text
  def windows_read_text(path,*args,**kwargs):
   if not args and kwargs.get("encoding") is None: raise UnicodeDecodeError("gbk",b"\xaa",0,1,"模拟 Windows 默认编码")
   return original(path,*args,**kwargs)
  with patch.object(Path,"read_text",windows_read_text):
   self.assertEqual(self.store.checkpoint("cn")["task_id"],"cn")
   self.assertEqual(self.store.versions("cn","clarification")[0]["metadata"]["title"],"中文需求")
   self.assertEqual(self.store.artifact("cn",digest).decode(),"中文")
class ServiceTests(unittest.TestCase):
 def test_idempotency_and_fake_delivery(self):
  with tempfile.TemporaryDirectory() as p:
   svc=TaskService(WorkspaceStore(p)); svc.create("t","auto")
   first=svc.command("t","1","advance"); self.assertEqual(first,svc.command("t","1","advance")); self.assertEqual(len(svc.events("t")),1)
   for n in range(2,5):svc.command("t",str(n),"advance")
   with self.assertRaises(GateError):svc.command("t","x","advance")
   svc.command("t","5","confirm_sample","user");svc.command("t","6","advance");svc.command("t","7","advance")
   self.assertNotEqual(svc.get("t")["status"],"completed");svc.command("t","9","resolve_blockers","user");svc.command("t","10","confirm_delivery","user");self.assertEqual(svc.get("t")["status"],"completed")
 def test_inspector_has_isolated_input(self):
  calls=[]; result=FakeInspectionGateway(calls=calls).inspect("original","<html/>");self.assertFalse(result["passed"]);self.assertEqual(result["issues"][0]["severity"],"blocker");self.assertEqual(set(calls[0]),{"outline","html"})
if __name__=="__main__":unittest.main()
