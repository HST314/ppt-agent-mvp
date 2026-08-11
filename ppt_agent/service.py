from __future__ import annotations

import hashlib, json
from datetime import datetime, timezone

from .errors import ConflictError
from .fsm import TaskState, transition

def utcnow(): return datetime.now(timezone.utc).isoformat()

class TaskService:
    def __init__(self,store): self.store=store
    def create(self,task_id,mode="manual"):
        s=TaskState(task_id=task_id,mode=mode); self.store.create(task_id,s.to_dict()); return s.to_dict()
    def get(self,task_id): return self.store.checkpoint(task_id)
    def command(self,task_id,command_id,action,actor="system"):
        with self.store.lock(task_id):
            prior=[e for e in self.store.events(task_id) if e["command_id"]==command_id]
            if prior:
                if prior[0]["action"] != action: raise ConflictError("command_id 已用于其他动作")
                return self.get(task_id)
            old=TaskState.parse(self.get(task_id)); new=transition(old,action,actor=actor)
            event={"event_id":hashlib.sha256(f"{task_id}:{command_id}".encode()).hexdigest()[:24],"command_id":command_id,"action":action,"actor":actor,"at":utcnow(),"from":old.to_dict(),"to":new.to_dict()}
            self.store.commit(task_id,new.to_dict(),event); return new.to_dict()
    def versions(self,task_id): return []
    def events(self,task_id): return self.store.events(task_id)
