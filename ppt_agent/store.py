from __future__ import annotations

import hashlib, json, os, re, threading, uuid
from pathlib import Path

from .errors import ConflictError, NotFoundError, ValidationError
HASH=re.compile(r"^[0-9a-f]{64}$")


class WorkspaceStore:
    def __init__(self, root, fault=None): self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True); self._locks={}; self._guard=threading.Lock(); self.fault=fault
    def _task(self, task_id):
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in task_id): raise ValidationError("task_id 格式无效")
        p=(self.root/task_id).resolve()
        if self.root not in p.parents: raise ValidationError("任务路径越界")
        return p
    def lock(self, task_id):
        with self._guard: return self._locks.setdefault(task_id,threading.RLock())
    @staticmethod
    def digest(data: bytes): return hashlib.sha256(data).hexdigest()
    def atomic_json(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True); raw=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode(); tmp=path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        with open(tmp,"xb") as f: f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path); return self.digest(raw)
    def create(self,task_id,state):
        with self.lock(task_id):
            p=self._task(task_id)
            if p.exists(): raise ConflictError("任务已存在")
            (p/"artifacts").mkdir(parents=True); (p/"versions").mkdir(); self.atomic_json(p/"checkpoint.json",state); (p/"events.jsonl").touch(); return state
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
        p=self._task(task_id)/"checkpoint.json"
        if not p.exists(): raise NotFoundError("任务不存在")
        return json.loads(p.read_text())
    def commit(self,task_id,state,event):
        with self.lock(task_id):
            p=self._task(task_id); tx=p/"pending-commit.json"
            self.atomic_json(tx,{"state":state,"event":event})
            if self.fault: self.fault("after_prepare")
            self._finish(p,state,event)
            tx.unlink(missing_ok=True)
    def _finish(self,p,state,event):
        existing={e["event_id"] for e in self._read_events(p)}
        if event["event_id"] not in existing:
            with open(p/"events.jsonl","a",encoding="utf-8") as f: f.write(json.dumps(event,ensure_ascii=False,separators=(",",":"))+"\n"); f.flush(); os.fsync(f.fileno())
        if self.fault: self.fault("after_event")
        self.atomic_json(p/"checkpoint.json",state)
    def recover(self,task_id):
        with self.lock(task_id):
            p=self._task(task_id); tx=p/"pending-commit.json"
            if tx.exists():
                data=json.loads(tx.read_text()); saved=self.fault; self.fault=None
                try: self._finish(p,data["state"],data["event"]); tx.unlink()
                finally: self.fault=saved
    def put_version(self,task_id,kind,content:bytes,metadata):
        with self.lock(task_id):
            if not kind or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in kind): raise ValidationError("版本 kind 格式无效")
            digest=self.digest(content); p=self._task(task_id)/"artifacts"/digest
            if not p.exists():
                tmp=p.with_name(f".{digest}.tmp"); tmp.write_bytes(content); os.replace(tmp,p)
            vp=self._task(task_id)/"versions"/kind/f"{digest}.json"
            if vp.exists() and json.loads(vp.read_text()) != metadata: raise ConflictError("历史版本不可覆盖")
            if not vp.exists(): self.atomic_json(vp,metadata)
            return digest
    def versions(self,task_id,kind=None):
        base=self._task(task_id)/"versions"
        if not base.exists(): raise NotFoundError("任务不存在")
        files=(base/kind).glob("*.json") if kind else base.glob("*/*.json")
        return [{"kind":p.parent.name,"hash":p.stem,"metadata":json.loads(p.read_text())} for p in sorted(files)]
    def artifact(self,task_id,digest):
        if not HASH.fullmatch(digest): raise ValidationError("hash 格式无效")
        p=self._task(task_id)/"artifacts"/digest
        if not p.exists(): raise NotFoundError("版本不存在")
        return p.read_bytes()
    @staticmethod
    def _read_events(p): return [json.loads(x) for x in (p/"events.jsonl").read_text().splitlines() if x]
    def events(self,task_id):
        self.recover(task_id)
        p=self._task(task_id)
        if not (p/"events.jsonl").exists(): raise NotFoundError("任务不存在")
        return self._read_events(p)
