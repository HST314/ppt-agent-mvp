import io,json,tempfile,unittest
from pathlib import Path

from ppt_agent.api import App
from ppt_agent.errors import ConflictError,ValidationError
from ppt_agent.p2 import parse_task_card,scan_resources
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

PNG=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
JPEG=b"\xff\xd8\xff\xe0JFIF\x00\xff\xd9"

class P2Tests(unittest.TestCase):
 def test_short_natural_language_retains_explicit_topic_and_goal(self):
  card=parse_task_card("设计一个用于北工大集成电路学院介绍的ppt","markdown")
  self.assertEqual(card["topic"],"北工大集成电路学院介绍")
  self.assertEqual(card["goal"],"介绍北工大集成电路学院")
  self.assertEqual(card["missing"],["audience"])
  self.assertIn("未推断受众",card["assumptions"][0])
 def test_nested_known_facts_and_batch_answers_update_task_card(self):
  self.svc.create("nested"); self.svc.create("batch")
  result=self.svc.import_input("nested",{"goal":"促成审批","topic":"新品","known_facts":{"audience":"管理层"}})
  self.assertEqual(result["task_card"]["audience"],"管理层"); self.assertTrue(result["clarification"]["confirmed"])
  result=self.svc.import_input("batch",{"topic":"新品"}); questions=result["clarification"]["details"]
  submitted={q["question_id"]:{"option":"Other","other":"促成审批" if q["field"]=="goal" else "管理层"} for q in questions}
  done=self.svc.answer_clarifications("batch",submitted); self.assertTrue(done["confirmed"])
  view=self.svc.input_view("batch"); self.assertEqual(view["task_card"]["goal"],"促成审批"); self.assertEqual(view["task_card"]["audience"],"管理层")
 def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.store=WorkspaceStore(self.tmp.name); self.svc=TaskService(self.store); self.svc.create("task")
 def tearDown(self): self.tmp.cleanup()
 def test_json_markdown_normalize_and_block(self):
  j=parse_task_card({"goal":"销售","audience":"客户","topic":"新品"},"json")
  m=parse_task_card("演示目标：销售\n受众：客户\n核心主题：新品","markdown")
  for key in ("goal","audience","topic","defaults","missing"): self.assertEqual(j[key],m[key])
  result=self.svc.import_input("task",{"goal":"销售"},"json")
  self.assertEqual(result["state"]["status"],"waiting_for_user"); self.assertEqual(len(result["clarification"]["details"]),2)
  with self.assertRaises(ConflictError): self.svc.import_input("task",{"goal":"x","audience":"a","topic":"t"})
 def test_auto_format_detection_rejects_mismatch_and_exposes_source(self):
  card=parse_task_card('{"goal":"销售","audience":"客户","topic":"新品"}',"auto")
  self.assertEqual(card["format_detection"],{"requested":"auto","detected":"json","confidence":"high"})
  with self.assertRaises(ValidationError): parse_task_card('{"goal":"销售"}',"markdown")
  result=self.svc.import_input("task",{"topic":"新品"})
  view=self.svc.input_view("task")
  self.assertEqual(view["source"],{"topic":"新品"})
  self.assertEqual(view["source_format"],"json")
  clarification=result["clarification"]
  self.assertEqual(clarification["question_source"],"fallback")
  self.assertTrue(clarification["diagnostic_id"].startswith("clarification-"))
  question=clarification["details"][0]
  self.assertEqual(set(question),{"question_id","field_path","field","prompt","helper_text","options","allow_other","blocking"})
  self.assertEqual(set(question["options"][0]),{"value","label","description"})
 def test_batch_requires_complete_round_without_partial_write(self):
  result=self.svc.import_input("task",{"topic":"新品"}); questions=result["clarification"]["details"]
  first=questions[0]
  with self.assertRaises(ValidationError):
   self.svc.answer_clarifications("task",{first["question_id"]:{"option":first["options"][0]["value"]}},require_complete=True)
  self.assertEqual(self.svc.input_view("task")["clarification"]["answers"],{})
 def test_resource_pairing_hash_freeze_and_explicit_rebuild(self):
  self.store.put_resource("task","hero.png",PNG); self.store.put_resource("task","hero.md","主视觉说明".encode())
  first=self.svc.import_input("task",{"goal":"g","audience":"a","topic":"t"})
  self.assertEqual(first["manifest"]["resources"][0]["description"],"主视觉说明")
  self.store.put_resource("task","later.jpg",JPEG)
  frozen=self.svc.input_view("task"); self.assertEqual(frozen["snapshot_hash"],first["snapshot_hash"])
  second=self.svc.import_input("task",{"goal":"g","audience":"a","topic":"t"},rebuild=True)
  self.assertNotEqual(second["snapshot_hash"],first["snapshot_hash"]); self.assertEqual(len(second["manifest"]["resources"]),2)
 def test_input_view_reads_source_from_current_snapshot_raw_source_hash(self):
  self.svc.import_input("task","第一版原始资料","markdown")
  current=self.svc.input_view("task")
  orphan_hash=self.store.put_version("task","input-source","不属于当前快照".encode(),{"content_type":"text/plain"})
  self.assertNotEqual(orphan_hash,next(v for v in self.svc.versions("task","input-snapshot") if v["hash"]==current["snapshot_hash"])["metadata"]["raw_source_hash"])
  view=self.svc.input_view("task")
  self.assertEqual(view["source"],"第一版原始资料")
  self.assertEqual(view["source_format"],"markdown")
 def test_no_images_and_path_guards(self):
  resources,warnings=scan_resources(self.store.resource_root("task")); self.assertEqual(resources,[])
  with self.assertRaises(ValidationError): self.store.put_resource("task","../escape.png",b"x")
 def test_corrupt_image_is_diagnosed_and_excluded(self):
  self.store.put_resource("task","broken.png",b"not-an-image")
  self.store.put_resource("task","valid.png",PNG)
  resources,warnings=scan_resources(self.store.resource_root("task"))
  self.assertEqual([item["uri"] for item in resources],["resources://valid.png"])
  self.assertIn({"code":"invalid_image_content","path":"broken.png"},warnings)
 def test_other_answer_and_change_invalidation(self):
  result=self.svc.import_input("task",{"goal":"g","topic":"t"}); q=result["clarification"]["details"][0]
  with self.assertRaises(ValidationError): self.svc.answer_clarification("task",q["question_id"],{"option":"Other"})
  done=self.svc.answer_clarification("task",q["question_id"],{"option":"Other","other":"管理层"}); self.assertTrue(done["confirmed"])
  changed=self.svc.answer_clarification("task",q["question_id"],{"option":"Other","other":"客户"}); self.assertIn("outline",changed["invalidated"])
 def test_api_and_desktop_page(self):
  app=App(self.svc)
  def call(method,path,body=None):
   raw=json.dumps(body or {}).encode(); status=[]; out=b"".join(app({"REQUEST_METHOD":method,"PATH_INFO":path,"CONTENT_LENGTH":str(len(raw)),"wsgi.input":io.BytesIO(raw)},lambda s,h:status.append(s)))
   return status[0],out
  status,_=call("POST","/v1/tasks/task/input",{"source":{"goal":"g","audience":"a","topic":"t"}}); self.assertTrue(status.startswith("200"))
  status,page=call("GET","/tasks/task"); self.assertTrue(status.startswith("200")); self.assertIn(b'type="module"',page); self.assertIn(b"aria-live",page)
  module=Path("frontend/static/js/stages/input.js").read_text(); self.assertIn("任务卡内容",module); self.assertIn("授权资源清单",module)

if __name__=="__main__": unittest.main()
