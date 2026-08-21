from __future__ import annotations

import errno, hashlib, json, os, re, shutil, threading, time, uuid, weakref
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

import portalocker

from .errors import ConflictError, NotFoundError, ValidationError
HASH=re.compile(r"^[0-9a-f]{64}$")
BRANCH=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class _TaskLock:
    """A thread-reentrant mutex backed by an inter-process file lock."""
    def __init__(self,path):
        self.path=path; self.thread_lock=threading.RLock(); self.owner=None; self.depth=0; self.file_lock=None
    def __enter__(self):
        self.thread_lock.acquire(); owner=threading.get_ident()
        try:
            if self.owner==owner:
                self.depth+=1
                return self
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.file_lock=portalocker.Lock(str(self.path),mode="a+b",timeout=60,check_interval=.05)
            self.file_lock.acquire(); self.owner=owner; self.depth=1
            return self
        except BaseException:
            self.file_lock=None; self.thread_lock.release(); raise
    def __exit__(self,exc_type,exc,tb):
        self.depth-=1
        if self.depth==0:
            self.owner=None
            try: self.file_lock.release()
            finally: self.file_lock=None
        self.thread_lock.release()


class WorkspaceStore:
    _shared_locks=weakref.WeakValueDictionary(); _shared_guard=threading.Lock()
    def __init__(self, root, fault=None): self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True); self._guard=threading.Lock(); self.fault=fault
    def _task(self, task_id):
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in task_id): raise ValidationError("task_id 格式无效")
        p=(self.root/task_id).resolve()
        if self.root not in p.parents: raise ValidationError("任务路径越界")
        return p
    def lock(self, task_id):
        self._task(task_id)
        path=self.root/".task-locks"/f"{task_id}.lock"
        key=str(path)
        with self._shared_guard: return self._shared_locks.setdefault(key,_TaskLock(path))
    @contextmanager
    def transaction(self, task_id):
        """Rollback every task-local write when a multi-artifact action fails."""
        with self.lock(task_id):
            task=self._task(task_id)
            if not task.is_dir(): raise NotFoundError("任务不存在")
            backup=task.with_name(f".{task.name}.{uuid.uuid4().hex}.transaction")
            shutil.copytree(task,backup)
            try:
                yield
            except BaseException:
                failed=task.with_name(f".{task.name}.{uuid.uuid4().hex}.failed")
                os.replace(task,failed); os.replace(backup,task)
                shutil.rmtree(failed,ignore_errors=True)
                raise
            else:
                shutil.rmtree(backup,ignore_errors=True)
    @staticmethod
    def digest(data: bytes): return hashlib.sha256(data).hexdigest()
    def atomic_json(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True); raw=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp,"xb") as f: f.write(raw); f.flush(); os.fsync(f.fileno())
            for attempt in range(5):
                try: os.replace(tmp,path); return self.digest(raw)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES,errno.EBUSY,errno.EPERM} or attempt == 4: raise
                    time.sleep(.01*(2**attempt))
        finally: tmp.unlink(missing_ok=True)
    def atomic_bytes(self,path,data:bytes):
        path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp,"xb") as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path); return self.digest(data)
        finally: tmp.unlink(missing_ok=True)
    def create(self,task_id,state):
        with self.lock(task_id):
            p=self._task(task_id)
            if p.exists(): raise ConflictError("任务已存在")
            (p/"artifacts").mkdir(parents=True); (p/"versions").mkdir(); self.atomic_json(p/"checkpoint.json",state); (p/"events.jsonl").touch()
            branch=p/"branches"/"main"; branch.mkdir(parents=True)
            self.atomic_json(branch/"checkpoint.json",state); (branch/"events.jsonl").touch()
            created=datetime.now(timezone.utc).isoformat()
            self.atomic_json(p/"branches.json",{"active":"main","branches":{"main":{"branch_id":"main","parent":None,"source_branch":None,"source_revision":state.get("revision",0),"base_revision":state.get("revision",0),"base_state":state,"head_revision":state.get("revision",0),"stage":state.get("stage"),"created_at":created,"updated_at":created}}})
            return state
    def _ensure_branches_locked(self,task_id):
        task=self._task(task_id); path=task/"branches.json"
        if path.exists(): return json.loads(path.read_text(encoding="utf-8"))
        checkpoint=task/"checkpoint.json"; events=task/"events.jsonl"
        if not checkpoint.exists(): raise NotFoundError("任务不存在")
        state=json.loads(checkpoint.read_text(encoding="utf-8")); branch=task/"branches"/"main"; branch.mkdir(parents=True,exist_ok=True)
        self.atomic_json(branch/"checkpoint.json",state)
        self.atomic_bytes(branch/"events.jsonl",events.read_bytes() if events.exists() else b"")
        created=datetime.now(timezone.utc).isoformat()
        manifest={"active":"main","branches":{"main":{"branch_id":"main","parent":None,"source_branch":None,"source_revision":state.get("revision",0),"base_revision":0,"base_state":{"task_id":task_id,"stage":"created","status":"ready","mode":state.get("mode","manual"),"sample_confirmed":False,"blockers_resolved":False,"delivery_confirmed":False,"revision":0,"waiting_reason":None,"required_action":None,"target_slide_count":state.get("target_slide_count")},"head_revision":state.get("revision",0),"stage":state.get("stage"),"created_at":created,"updated_at":created}}}
        self.atomic_json(path,manifest); return manifest
    def _branch_root_locked(self,task_id,branch_id=None):
        manifest=self._ensure_branches_locked(task_id); branch_id=branch_id or manifest["active"]
        if not BRANCH.fullmatch(branch_id) or branch_id not in manifest["branches"]: raise NotFoundError("分支不存在")
        return self._task(task_id)/"branches"/branch_id,manifest
    def branch_context(self,task_id):
        with self.lock(task_id):
            root,manifest=self._branch_root_locked(task_id); item=manifest["branches"][manifest["active"]]
            state=json.loads((root/"checkpoint.json").read_text(encoding="utf-8"))
            return {key:value for key,value in item.items() if key!="base_state"} | {"active":True,"head_revision":state.get("revision",item.get("head_revision",0)),"stage":state.get("stage")}
    def branches(self,task_id):
        with self.lock(task_id):
            manifest=self._ensure_branches_locked(task_id)
            return {"active":manifest["active"],"branches":[{key:value for key,value in item.items() if key!="base_state"} | {"active":branch_id==manifest["active"]} for branch_id,item in sorted(manifest["branches"].items(),key=lambda pair:(pair[1].get("created_at",""),pair[0]))]}
    def branch_from(self,task_id,branch_id,source_branch=None,source_revision=None,switch=True):
        if not isinstance(branch_id,str) or not BRANCH.fullmatch(branch_id): raise ValidationError("分支名称须为 1-64 位字母、数字、点、下划线或连字符")
        if not isinstance(switch,bool): raise ValidationError("switch 必须为 boolean")
        with self.lock(task_id):
            manifest=self._ensure_branches_locked(task_id)
            if branch_id in manifest["branches"]: raise ConflictError("分支已存在")
            source_branch=source_branch or manifest["active"]
            source_root,_=self._branch_root_locked(task_id,source_branch); source_meta=manifest["branches"][source_branch]
            current=json.loads((source_root/"checkpoint.json").read_text(encoding="utf-8")); events=self._read_events(source_root)
            if source_revision is None: source_revision=current.get("revision",0)
            if isinstance(source_revision,bool) or not isinstance(source_revision,int) or source_revision<0 or source_revision>current.get("revision",0): raise ValidationError("来源修订号无效")
            event=next((item for item in reversed(events) if item.get("to",{}).get("revision")==source_revision),None)
            origin=next((item.get("from") for item in events if item.get("from",{}).get("revision")==source_revision),None)
            if event: state=event["to"]
            elif source_revision==source_meta.get("base_revision",0): state=source_meta.get("base_state",current)
            elif origin: state=origin
            else: raise ValidationError("来源修订不存在")
            selected=[item for item in events if item.get("to",{}).get("revision",-1)<=source_revision]
            target=self._task(task_id)/"branches"/branch_id; target.mkdir(parents=True)
            self.atomic_json(target/"checkpoint.json",state)
            raw="".join(json.dumps(item,ensure_ascii=False,separators=(",",":"))+"\n" for item in selected).encode()
            self.atomic_bytes(target/"events.jsonl",raw)
            created=datetime.now(timezone.utc).isoformat()
            manifest["branches"][branch_id]={"branch_id":branch_id,"parent":source_branch,"source_branch":source_branch,"source_revision":source_revision,"base_revision":source_revision,"base_state":state,"head_revision":source_revision,"stage":state.get("stage"),"created_at":created,"updated_at":created}
            if switch: manifest["active"]=branch_id
            self.atomic_json(self._task(task_id)/"branches.json",manifest)
            if switch: self._mirror_active_locked(task_id,target,state)
            return self.branches(task_id)
    def switch_branch(self,task_id,branch_id):
        with self.lock(task_id):
            root,manifest=self._branch_root_locked(task_id,branch_id); state=json.loads((root/"checkpoint.json").read_text(encoding="utf-8"))
            manifest["active"]=branch_id; self.atomic_json(self._task(task_id)/"branches.json",manifest); self._mirror_active_locked(task_id,root,state)
            return self.branches(task_id)
    def _mirror_active_locked(self,task_id,branch_root,state):
        task=self._task(task_id); self.atomic_json(task/"checkpoint.json",state); self.atomic_bytes(task/"events.jsonl",(branch_root/"events.jsonl").read_bytes())
    def resource_root(self,task_id):
        p=self._task(task_id)
        if not p.exists(): raise NotFoundError("任务不存在")
        root=p/"resources"; root.mkdir(exist_ok=True); return root
    def put_resource(self,task_id,name,content:bytes):
        if not name or Path(name).name != name or name in {".",".."}: raise ValidationError("资源文件名无效")
        root=self.resource_root(task_id); target=(root/name).resolve()
        if root.resolve() not in target.parents: raise ValidationError("资源路径越权")
        if target.exists() and target.read_bytes()!=content: raise ConflictError("同名资源不可静默覆盖")
        if not target.exists():
            tmp=target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp"); tmp.write_bytes(content); os.replace(tmp,target)
        return self.digest(content)
    def checkpoint(self,task_id):
        self.recover(task_id)
        with self.lock(task_id):
            p=self._branch_root_locked(task_id)[0]/"checkpoint.json"
            if not p.exists(): raise NotFoundError("任务不存在")
            return json.loads(p.read_text(encoding="utf-8"))
    def commit(self,task_id,state,event):
        from .execution import checkpoint, progress
        progress("saving_result", "保存业务结果")
        with self.lock(task_id):
            # Cancellation and publication share this task lock. A cancel that
            # wins the lock makes publication fail; a completed commit is
            # ordered before the cancellation request and cannot be half-seen.
            checkpoint()
            p,_=self._branch_root_locked(task_id); tx=p/"pending-commit.json"
            current=json.loads((p/"checkpoint.json").read_text(encoding="utf-8"))
            from_revision=event.get("from",{}).get("revision")
            if from_revision is not None and from_revision!=current.get("revision"):
                raise ConflictError("分支头已变化，拒绝写入过期结果")
            self.atomic_json(tx,{"state":state,"event":event})
            if self.fault: self.fault("after_prepare")
            self._finish(task_id,p,state,event)
            from .execution import publication_committed
            publication_committed(state)
            tx.unlink(missing_ok=True)
    def _finish(self,task_id,p,state,event):
        existing={e["event_id"] for e in self._read_events(p)}
        if event["event_id"] not in existing:
            with open(p/"events.jsonl","a",encoding="utf-8") as f: f.write(json.dumps(event,ensure_ascii=False,separators=(",",":"))+"\n"); f.flush(); os.fsync(f.fileno())
        if self.fault: self.fault("after_event")
        self.atomic_json(p/"checkpoint.json",state)
        self._mirror_active_locked(task_id,p,state)
        manifest=self._ensure_branches_locked(task_id); active=manifest["active"]; item=manifest["branches"][active]
        item.update({"head_revision":state.get("revision",item.get("head_revision",0)),"stage":state.get("stage"),"updated_at":datetime.now(timezone.utc).isoformat(),"head_event_id":event.get("event_id")})
        self.atomic_json(self._task(task_id)/"branches.json",manifest)
    def recover(self,task_id):
        with self.lock(task_id):
            p,_=self._branch_root_locked(task_id); tx=p/"pending-commit.json"
            if tx.exists():
                data=json.loads(tx.read_text(encoding="utf-8")); saved=self.fault; self.fault=None
                try: self._finish(task_id,p,data["state"],data["event"]); tx.unlink()
                finally: self.fault=saved
    def put_version(self,task_id,kind,content:bytes,metadata):
        from .execution import checkpoint
        with self.lock(task_id):
            checkpoint()
            if not kind or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in kind): raise ValidationError("版本 kind 格式无效")
            digest=self.digest(content); p=self._task(task_id)/"artifacts"/digest
            if not p.exists():
                tmp=p.with_name(f".{digest}.tmp"); tmp.write_bytes(content); os.replace(tmp,p)
            vp=self._task(task_id)/"versions"/kind/f"{digest}.json"
            if vp.exists() and json.loads(vp.read_text(encoding="utf-8")) != metadata: raise ConflictError("历史版本不可覆盖")
            if not vp.exists(): self.atomic_json(vp,metadata)
            return digest
    def versions(self,task_id,kind=None):
        base=self._task(task_id)/"versions"
        if not base.exists(): raise NotFoundError("任务不存在")
        files=(base/kind).glob("*.json") if kind else base.glob("*/*.json")
        records=[{"kind":p.parent.name,"hash":p.stem,"metadata":json.loads(p.read_text(encoding="utf-8"))} for p in files]
        def order(record):
            try: artifact=json.loads(self.artifact(task_id,record["hash"]))
            except (NotFoundError,json.JSONDecodeError,UnicodeDecodeError): artifact={}
            return (artifact.get("version",record["metadata"].get("v",0)),artifact.get("created_at",artifact.get("confirmed_at","")),record["hash"])
        return sorted(records,key=order)
    def artifact(self,task_id,digest):
        if not HASH.fullmatch(digest): raise ValidationError("hash 格式无效")
        p=self._task(task_id)/"artifacts"/digest
        if not p.exists(): raise NotFoundError("版本不存在")
        return p.read_bytes()
    def publish_delivery(self,task_id,delivery_id,files,verifier=None):
        """Verify a complete staging tree before atomically publishing it."""
        if not delivery_id or Path(delivery_id).name != delivery_id: raise ValidationError("delivery_id 格式无效")
        base=self._task(task_id)/"deliveries"; base.mkdir(exist_ok=True)
        target=base/delivery_id
        with self.lock(task_id):
            if target.exists():
                existing={str(p.relative_to(target)):p.read_bytes() for p in target.rglob("*") if p.is_file()}
                if existing != files: raise ConflictError("交付版本不可覆盖")
                return target
            staging=base/f".{delivery_id}.{uuid.uuid4().hex}.tmp"; staging.mkdir()
            try:
                for name,content in files.items():
                    relative=Path(name)
                    if relative.is_absolute() or ".." in relative.parts: raise ValidationError("交付文件路径越界")
                    path=(staging/relative).resolve()
                    if staging.resolve() not in path.parents: raise ValidationError("交付文件路径越界")
                    path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(content)
                if verifier is not None: verifier(staging)
                if self.fault: self.fault("before_delivery_publish")
                os.replace(staging,target)
                if self.fault: self.fault("after_delivery_publish")
            except Exception:
                shutil.rmtree(staging,ignore_errors=True); raise
        return target
    def delivery_intent(self,task_id,delivery_id,seed):
        """Persist the values which must remain stable while a delivery is retried."""
        with self.lock(task_id):
            path=self._task(task_id)/"delivery-intents"/f"{delivery_id}.json"
            if path.exists():
                value=json.loads(path.read_text(encoding="utf-8"))
                if value.get("seed") != seed: raise ConflictError("交付事务请求冲突")
                return value
            value={"seed":seed,"confirmed_at":datetime.now(timezone.utc).isoformat()}
            self.atomic_json(path,value)
            return value
    def clear_delivery_intent(self,task_id,delivery_id):
        with self.lock(task_id): (self._task(task_id)/"delivery-intents"/f"{delivery_id}.json").unlink(missing_ok=True)
    def delivery_root(self,task_id,delivery_id):
        path=(self._task(task_id)/"deliveries"/delivery_id).resolve()
        if not path.is_dir(): raise NotFoundError("交付不存在")
        return path
    @staticmethod
    def _read_events(p): return [json.loads(x) for x in (p/"events.jsonl").read_text(encoding="utf-8").splitlines() if x]
    def events(self,task_id):
        self.recover(task_id)
        with self.lock(task_id):
            p=self._branch_root_locked(task_id)[0]
            if not (p/"events.jsonl").exists(): raise NotFoundError("任务不存在")
            return self._read_events(p)
    def append_agent_audit(self, record):
        """Persist a secret-free Agent run globally and in its task export tree."""
        paths=[self.root/"agent-audit.jsonl"]
        task_id=record.get("task_id")
        if isinstance(task_id,str):
            task=self._task(task_id)
            if task.is_dir(): paths.append(task/"agent-audit.jsonl")
        line=json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n"
        with self._guard:
            for path in paths:
                with open(path,"a",encoding="utf-8") as f:
                    f.write(line); f.flush(); os.fsync(f.fileno())

    def append_runtime_probe(self, record):
        """Persist a global, secret-free readiness probe independently of tasks."""
        path=self.root/"runtime-probes.jsonl"
        with self._guard:
            with open(path,"a",encoding="utf-8") as f:
                f.write(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n")
                f.flush(); os.fsync(f.fileno())

    def runtime_probes(self,limit=20):
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 100: raise ValidationError("探测记录 limit 必须是 1 到 100")
        path=self.root/"runtime-probes.jsonl"
        records=[] if not path.exists() else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        return records[-limit:][::-1]
    def agent_audits(self,task_id=None,job_id=None):
        task_path=self._task(task_id)/"agent-audit.jsonl" if task_id is not None else None
        path=task_path if task_path is not None and task_path.exists() else self.root/"agent-audit.jsonl"
        records=[] if not path.exists() else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        if task_id is not None: records=[record for record in records if record.get("task_id")==task_id]
        if job_id is not None: records=[record for record in records if record.get("job_id")==job_id]
        return records
