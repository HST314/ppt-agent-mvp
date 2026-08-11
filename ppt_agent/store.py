from __future__ import annotations

import hashlib, json, os, threading, uuid
from pathlib import Path

from .errors import ConflictError, NotFoundError, ValidationError


class WorkspaceStore:
    def __init__(self, root): self.root=Path(root).resolve(); self.root.mkdir(parents=True,exist_ok=True); self._locks={}; self._guard=threading.Lock()
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
    def checkpoint(self,task_id):
        p=self._task(task_id)/"checkpoint.json"
        if not p.exists(): raise NotFoundError("任务不存在")
        return json.loads(p.read_text())
    def commit(self,task_id,state,event):
        with self.lock(task_id):
            p=self._task(task_id); self.atomic_json(p/"checkpoint.json",state)
            with open(p/"events.jsonl","a",encoding="utf-8") as f: f.write(json.dumps(event,ensure_ascii=False,separators=(",",":"))+"\n"); f.flush(); os.fsync(f.fileno())
    def put_version(self,task_id,kind,content:bytes,metadata):
        with self.lock(task_id):
            digest=self.digest(content); p=self._task(task_id)/"artifacts"/digest
            if not p.exists():
                tmp=p.with_name(f".{digest}.tmp"); tmp.write_bytes(content); os.replace(tmp,p)
            vp=self._task(task_id)/"versions"/kind/f"{digest}.json"
            if vp.exists() and json.loads(vp.read_text()) != metadata: raise ConflictError("历史版本不可覆盖")
            if not vp.exists(): self.atomic_json(vp,metadata)
            return digest
    def events(self,task_id):
        p=self._task(task_id)/"events.jsonl"
        if not p.exists(): raise NotFoundError("任务不存在")
        return [json.loads(x) for x in p.read_text().splitlines() if x]
