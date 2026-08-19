import tempfile
import threading
import time
import unittest
from unittest import mock

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.errors import ConflictError
from ppt_agent.web.jobs import JobService


class DeferredExecutor:
    def __init__(self): self.calls=[]
    def submit(self,function,*args): self.calls.append((function,args))
    def shutdown(self,**_kwargs): pass


class BlockingGenerationGateway:
    model="blocking-test"
    def __init__(self): self.entered=threading.Event(); self.release=threading.Event()
    def generate(self,action,payload,skill=None):
        self.entered.set()
        if not self.release.wait(3): raise TimeoutError("test gateway was not released")
        return {"text":"# 叙事结构\n\n## 开场\n方案目标与受众。\n","model":self.model}


class BranchingAndSettingsTests(unittest.TestCase):
    def test_historical_branch_has_independent_head_and_shared_versions(self):
        with tempfile.TemporaryDirectory() as root:
            store=WorkspaceStore(root); service=TaskService(store); service.create("task")
            service.command("task","step-1","advance")
            shared=store.put_version("task","note",b"shared",{"v":1})
            service.command("task","step-2","advance")
            main=store.branch_context("task")

            created=store.branch_from("task","alternate",source_revision=1,switch=True)
            self.assertEqual(created["active"],"alternate")
            self.assertEqual(service.get("task")["stage"],"clarification")
            self.assertEqual(service.version("task",shared),b"shared")
            service.command("task","alternate-step","advance")
            self.assertEqual(store.branch_context("task")["head_revision"],2)
            inherited=store.branch_from("task","from-created",source_branch="alternate",source_revision=0,switch=False)
            from_created=next(item for item in inherited["branches"] if item["branch_id"]=="from-created")
            self.assertEqual(from_created["stage"],"created")

            store.switch_branch("task","main")
            self.assertEqual(service.get("task")["stage"],"narrative")
            self.assertEqual(store.branch_context("task")["head_revision"],main["head_revision"])
            self.assertEqual(len(store.branches("task")["branches"]),3)

    def test_settings_persist_and_new_jobs_read_updated_timeouts(self):
        with tempfile.TemporaryDirectory() as root:
            service=TaskService(WorkspaceStore(root)); service.create("task"); service.command("task","to-clarification","advance"); service.command("task","to-narrative","advance")
            saved=service.update_settings({"workflow":{"max_rounds":4},"jobs":{"generation_timeout_seconds":91},"review":{"default_max_rounds":3}})
            self.assertEqual(saved["values"]["workflow"]["max_rounds"],4)
            recovered=TaskService(WorkspaceStore(root))
            self.assertEqual(recovered.settings_view()["values"]["review"]["default_max_rounds"],3)
            jobs=JobService(recovered,executor=DeferredExecutor())
            created,_=jobs.create("task","narrative.generate",{},"settings-timeout")
            self.assertEqual(created["deadline_seconds"],91)
            self.assertEqual(created["branch_id"],"main")
            self.assertEqual(created["head_revision"],2)
            with mock.patch.object(recovered,"run_inspection",return_value={}) as inspection:
                jobs._invoke("inspection.run","task",{})
                inspection.assert_called_once_with("task",3,None)
            jobs.close()

    def test_running_job_rejects_result_when_same_branch_head_moves(self):
        with tempfile.TemporaryDirectory() as root:
            gateway=BlockingGenerationGateway(); service=TaskService(WorkspaceStore(root),generator=gateway)
            service.create("task")
            service.import_input("task",{"goal":"发布","audience":"客户","topic":"方案","页数":3})
            service.command("task","to-narrative","advance")
            jobs=JobService(service)
            created,_=jobs.create("task","narrative.generate",{},"stale-head")
            self.assertTrue(gateway.entered.wait(2))

            service.command("task","concurrent-mode-change","switch_auto")
            gateway.release.set()
            deadline=time.monotonic()+3
            while jobs.get(created["job_id"])["status"] not in {"succeeded","failed","cancelled","interrupted"} and time.monotonic()<deadline:
                time.sleep(.01)
            finished=jobs.get(created["job_id"])
            self.assertEqual(finished["status"],"failed")
            self.assertEqual(finished["error"]["code"],"conflict")
            self.assertEqual(service.versions("task","narrative"),[])
            jobs.close()

    def test_branch_switch_is_atomic_with_job_admission(self):
        with tempfile.TemporaryDirectory() as root:
            service=TaskService(WorkspaceStore(root)); service.create("task")
            service.command("task","to-clarification","advance")
            service.command("task","to-narrative","advance")
            jobs=JobService(service,executor=DeferredExecutor())
            jobs.create_branch("task","alternate",switch=False)
            created,_=jobs.create("task","narrative.generate",{},"active-job")
            self.assertEqual(created["status"],"queued")
            with self.assertRaises(ConflictError): jobs.switch_branch("task","alternate")
            with self.assertRaises(ConflictError): jobs.create_branch("task","another",switch=True)
            self.assertEqual(service.store.branch_context("task")["branch_id"],"main")
            jobs.close()


if __name__=="__main__": unittest.main()
