from __future__ import annotations

import html, json, re
from wsgiref.simple_server import make_server

from .errors import DomainError, NotFoundError, ValidationError
from .service import TaskService
from .store import WorkspaceStore

TASK=re.compile(r"^/v1/tasks/([^/]+)$")
ACTION=re.compile(r"^/v1/tasks/([^/]+)/actions$")
EVENTS=re.compile(r"^/v1/tasks/([^/]+)/events$")
VERSIONS=re.compile(r"^/v1/tasks/([^/]+)/versions$")
VERSION=re.compile(r"^/v1/tasks/([^/]+)/versions/([0-9a-f]{64})$")
COMPARE=re.compile(r"^/v1/tasks/([^/]+)/versions/compare$")
PREVIEW=re.compile(r"^/v1/tasks/([^/]+)/preview$")
ISSUES=re.compile(r"^/v1/tasks/([^/]+)/issues/([^/]+)/disposition$")
INPUT=re.compile(r"^/v1/tasks/([^/]+)/input$")
ANSWER=re.compile(r"^/v1/tasks/([^/]+)/clarifications/([^/]+)/answer$")
WORKSPACE=re.compile(r"^/tasks/([^/]+)$")

class App:
    def __init__(self,service): self.service=service
    def __call__(self,environ,start_response):
        try:
            method=environ["REQUEST_METHOD"]; path=environ["PATH_INFO"]
            size=int(environ.get("CONTENT_LENGTH") or 0); body=json.loads(environ["wsgi.input"].read(size) or b"{}")
            if method=="GET" and path=="/healthz": return self.reply(start_response,200,{"status":"ok","stage":"P2","runtime_ready":True})
            if method=="POST" and path=="/v1/tasks":
                self.exact(body,{"task_id","mode"},{"task_id"}); return self.reply(start_response,201,self.service.create(body["task_id"],body.get("mode","manual")))
            m=TASK.match(path)
            if method=="GET" and m:return self.reply(start_response,200,self.service.get(m.group(1)))
            m=INPUT.match(path)
            if method=="POST" and m:
                self.exact(body,{"source","source_format","rebuild"},{"source"})
                return self.reply(start_response,200,self.service.import_input(m.group(1),body["source"],body.get("source_format","json"),body.get("rebuild",False)))
            if method=="GET" and m:return self.reply(start_response,200,self.service.input_view(m.group(1)))
            m=ANSWER.match(path)
            if method=="POST" and m:
                self.exact(body,{"option","other"},{"option"})
                return self.reply(start_response,200,self.service.answer_clarification(m.group(1),m.group(2),body))
            m=WORKSPACE.match(path)
            if method=="GET" and m:return self.page(start_response,m.group(1))
            m=ACTION.match(path)
            if method=="POST" and m:
                self.exact(body,{"command_id","action","actor","payload"},{"command_id","action"})
                return self.reply(start_response,200,self.service.command(m.group(1),body["command_id"],body["action"],body.get("actor","system"),body.get("payload")))
            m=EVENTS.match(path)
            if method=="GET" and m:return self.reply(start_response,200,{"events":self.service.events(m.group(1))})
            m=COMPARE.match(path)
            if method=="POST" and m:
                self.exact(body,{"left","right"},{"left","right"}); return self.reply(start_response,200,self.service.compare(m.group(1),body["left"],body["right"]))
            m=VERSION.match(path)
            if method=="GET" and m:return self.reply(start_response,200,{"hash":m.group(2),"content":self.service.version(m.group(1),m.group(2)).decode(errors="replace")})
            m=VERSIONS.match(path)
            if method=="GET" and m:return self.reply(start_response,200,{"versions":self.service.versions(m.group(1))})
            m=PREVIEW.match(path)
            if method=="POST" and m:return self.reply(start_response,200,self.service.run_fake_pipeline(m.group(1)))
            m=ISSUES.match(path)
            if method=="POST" and m:
                self.exact(body,{"command_id","action","actor"},{"command_id","action","actor"})
                payload={"issue_id":m.group(2),"disposition":body["action"]}; return self.reply(start_response,200,self.service.command(m.group(1),body["command_id"],"resolve_blockers",body["actor"],payload))
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
    def page(self,start,task_id):
        view=self.service.input_view(task_id); state=view["state"]; clarification=view.get("clarification") or {}; questions=clarification.get("questions",[]); answers=clarification.get("answers",{})
        qs="".join(f'<section><h3>{html.escape(q["prompt"])}</h3><p>选项：{html.escape(" / ".join(q["options"]))} / Other</p><p>当前回答：{html.escape(str(answers.get(q["question_id"],"待回答")))}</p></section>' for q in questions if isinstance(q,dict))
        snapshot="已冻结" if view.get("snapshot") else "尚未导入"
        raw=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>任务/资料</title><style>body{{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 24px;color:#172033}}main{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}section,form{{border:1px solid #ccd3df;border-radius:12px;padding:20px}}textarea{{width:100%;min-height:260px}}button,select{{padding:10px;margin-top:10px}}.status{{background:#f1f5fa;padding:12px;border-radius:8px}}@media(max-width:760px){{main{{grid-template-columns:1fr}}}}</style><h1>任务/资料</h1><p class="status">阶段：{html.escape(state["stage"])}　状态：{html.escape(state["status"])}　输入：{snapshot}　等待原因：{html.escape(str(state.get("waiting_reason") or "无"))}</p><main><form id="import"><h2>创建/导入任务卡</h2><label>格式 <select id="fmt"><option value="markdown">Markdown</option><option value="json">JSON</option></select></label><textarea id="source" aria-label="任务卡" placeholder="演示目标：...\n受众：...\n核心主题：..."></textarea><label><input type="checkbox" id="rebuild"> 显式重建快照</label><br><button>导入并扫描授权资源</button><pre id="result" aria-live="polite"></pre></form><div><section><h2>澄清</h2>{qs or '<p>当前没有待展示问题。</p>'}</section><section><h2>主操作</h2><p>{html.escape(state.get("required_action") or "资料已可用于下一阶段")}</p></section></div></main><script>document.querySelector('#import').onsubmit=async(e)=>{{e.preventDefault();let r=await fetch('/v1/tasks/{html.escape(task_id)}/input',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{source:source.value,source_format:fmt.value,rebuild:rebuild.checked}})}});result.textContent=JSON.stringify(await r.json(),null,2);if(r.ok)setTimeout(()=>location.reload(),500)}};</script></html>'''.encode()
        start("200 OK",[("Content-Type","text/html; charset=utf-8"),("Content-Length",str(len(raw)))]); return [raw]

def serve(root=".ppt-agent-data",host="127.0.0.1",port=8000):
    make_server(host,port,App(TaskService(WorkspaceStore(root)))).serve_forever()
