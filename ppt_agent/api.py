from __future__ import annotations

import json, re
from wsgiref.simple_server import make_server

from .errors import DomainError, NotFoundError, ValidationError
from .service import TaskService
from .store import WorkspaceStore

TASK=re.compile(r"^/v1/tasks/([^/]+)$")
ACTION=re.compile(r"^/v1/tasks/([^/]+)/actions$")
EVENTS=re.compile(r"^/v1/tasks/([^/]+)/events$")

class App:
    def __init__(self,service): self.service=service
    def __call__(self,environ,start_response):
        try:
            method=environ["REQUEST_METHOD"]; path=environ["PATH_INFO"]
            size=int(environ.get("CONTENT_LENGTH") or 0); body=json.loads(environ["wsgi.input"].read(size) or b"{}")
            if method=="GET" and path=="/healthz": return self.reply(start_response,200,{"status":"ok","stage":"P1","runtime_ready":True})
            if method=="POST" and path=="/v1/tasks":
                self.exact(body,{"task_id","mode"},{"task_id"}); return self.reply(start_response,201,self.service.create(body["task_id"],body.get("mode","manual")))
            m=TASK.match(path)
            if method=="GET" and m:return self.reply(start_response,200,self.service.get(m.group(1)))
            m=ACTION.match(path)
            if method=="POST" and m:
                self.exact(body,{"command_id","action","actor"},{"command_id","action"})
                return self.reply(start_response,200,self.service.command(m.group(1),body["command_id"],body["action"],body.get("actor","system")))
            m=EVENTS.match(path)
            if method=="GET" and m:return self.reply(start_response,200,{"events":self.service.events(m.group(1))})
            raise NotFoundError("接口不存在")
        except DomainError as exc:return self.reply(start_response,exc.status,exc.public())
        except Exception:
            exc=DomainError("请求处理失败"); exc.status=500; exc.code="internal_error"; return self.reply(start_response,500,exc.public())
    @staticmethod
    def exact(body,allowed,required):
        if not isinstance(body,dict) or set(body)-allowed or required-set(body):raise ValidationError("请求字段无效")
    @staticmethod
    def reply(start,status,data):
        raw=json.dumps(data,ensure_ascii=False).encode(); start(f"{status} OK",[("Content-Type","application/json; charset=utf-8"),("Content-Length",str(len(raw)))]); return [raw]

def serve(root=".ppt-agent-data",host="127.0.0.1",port=8000):
    make_server(host,port,App(TaskService(WorkspaceStore(root)))).serve_forever()
