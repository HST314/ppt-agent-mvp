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
PLANNING=re.compile(r"^/v1/tasks/([^/]+)/planning$")
NARRATIVE=re.compile(r"^/v1/tasks/([^/]+)/narrative(?:/(generate|confirm))?$")
OUTLINE=re.compile(r"^/v1/tasks/([^/]+)/outline(?:/(generate|confirm))?$")
ROLLBACK=re.compile(r"^/v1/tasks/([^/]+)/planning/rollback$")
OUTLINE_PAGE=re.compile(r"^/tasks/([^/]+)/outline$")
SAMPLES=re.compile(r"^/v1/tasks/([^/]+)/samples(?:/(select|generate|modify|confirm))?$")
SAMPLE_PAGE=re.compile(r"^/tasks/([^/]+)/samples$")

class App:
    def __init__(self,service): self.service=service
    def __call__(self,environ,start_response):
        try:
            method=environ["REQUEST_METHOD"]; path=environ["PATH_INFO"]
            size=int(environ.get("CONTENT_LENGTH") or 0); body=json.loads(environ["wsgi.input"].read(size) or b"{}")
            if method=="GET" and path=="/healthz": return self.reply(start_response,200,{"status":"ok","stage":"P4","runtime_ready":True})
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
            m=OUTLINE_PAGE.match(path)
            if method=="GET" and m:return self.outline_page(start_response,m.group(1))
            m=SAMPLE_PAGE.match(path)
            if method=="GET" and m:return self.sample_page(start_response,m.group(1))
            m=SAMPLES.match(path)
            if method=="GET" and m and not m.group(2): return self.reply(start_response,200,self.service.sample_view(m.group(1)))
            if method=="POST" and m:
                if m.group(2)=="select": self.exact(body,{"slide_ids","count"},set()); result=self.service.select_samples(m.group(1),body.get("slide_ids"),body.get("count",2))
                elif m.group(2)=="generate": self.exact(body,{"prompt"},set()); result=self.service.generate_sample(m.group(1),body.get("prompt"))
                elif m.group(2)=="modify": self.exact(body,{"prompt","scope","slide_id","element_id"},{"prompt"}); result=self.service.modify_sample(m.group(1),body["prompt"],body.get("scope"),body.get("slide_id"),body.get("element_id"))
                elif m.group(2)=="confirm": self.exact(body,set(),set()); result=self.service.confirm_sample(m.group(1))
                else: raise NotFoundError("接口不存在")
                return self.reply(start_response,200,result)
            m=PLANNING.match(path)
            if method=="GET" and m:return self.reply(start_response,200,self.service.planning_view(m.group(1)))
            m=NARRATIVE.match(path)
            if method=="POST" and m:
                if m.group(2)=="generate": self.exact(body,{"prompt","scope"},set()); result=self.service.generate_narrative(m.group(1),body.get("prompt"),body.get("scope","all"))
                elif m.group(2)=="confirm": self.exact(body,set(),set()); result=self.service.confirm_narrative(m.group(1))
                else: self.exact(body,{"markdown","summary"},{"markdown"}); result=self.service.edit_narrative(m.group(1),body["markdown"],body.get("summary","直接编辑"))
                return self.reply(start_response,200,result)
            m=OUTLINE.match(path)
            if method=="POST" and m:
                if m.group(2)=="generate": self.exact(body,{"prompt","slide_ids"},set()); result=self.service.generate_outline(m.group(1),body.get("prompt"),body.get("slide_ids"))
                elif m.group(2)=="confirm": self.exact(body,set(),set()); result=self.service.confirm_outline(m.group(1))
                else: self.exact(body,{"markdown","summary"},{"markdown"}); result=self.service.edit_outline(m.group(1),body["markdown"],body.get("summary","直接编辑"))
                return self.reply(start_response,200,result)
            m=ROLLBACK.match(path)
            if method=="POST" and m:
                self.exact(body,{"kind","hash"},{"kind","hash"}); return self.reply(start_response,200,self.service.rollback_planning(m.group(1),body["kind"],body["hash"]))
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
    STAGE_LIST=[("created","任务/资料"),("clarification","澄清"),("narrative","叙事结构"),("outline","逐页大纲"),("sample","样品"),("deck","全稿"),("review","检查"),("delivery","交付")]
    STAGE_PRE={"clarification":"前置条件：完成任务创建与资料导入","narrative":"前置条件：完成澄清回答","outline":"前置条件：确认叙事结构","sample":"前置条件：完成逐页大纲","deck":"前置条件：确认样品","review":"前置条件：生成全稿","delivery":"前置条件：通过检查与人工审核"}
    STATUS_LABEL={"ready":"就绪","running":"运行中","waiting_for_user":"等待人工","paused":"已暂停","cancelled":"已取消","failed":"失败","completed":"已完成"}
    WAIT_LABEL={"missing_required_input":"缺少必填信息","manual_gate":"等待人工确认"}
    ACTION_LABEL={"answer_clarifications":"回答澄清问题","approve_narrative":"确认叙事结构","confirm_sample":"确认样品","confirm_delivery":"确认交付"}
    FIELD_LABEL={"goal":"演示目标","audience":"受众","topic":"核心主题"}
    WARNING_LABEL={"missing_sidecar":"缺少配套 Markdown 说明","empty_resource":"空文件，已跳过","duplicate_content":"内容与已有资源重复","invalid_image_content":"图片内容无效或已损坏，未纳入清单"}
    @staticmethod
    def esc(value): return html.escape(str(value),quote=True)
    def page(self,start,task_id):
        view=self.service.input_view(task_id); state=view["state"]; esc=self.esc
        snapshot=view.get("snapshot"); card=view.get("task_card") or {}; manifest=view.get("manifest") or {}
        clarification=view.get("clarification") or {}; questions=[q for q in clarification.get("questions",[]) if isinstance(q,dict)]; answers=clarification.get("answers",{})
        current=state["stage"]; reached=[key for key,_ in self.STAGE_LIST].index(current) if current in dict(self.STAGE_LIST) else 0
        steps=[]
        for index,(key,label) in enumerate(self.STAGE_LIST):
            if index<reached: steps.append(f'<li class="done">{esc(label)}（已完成）</li>')
            elif index==reached: steps.append(f'<li class="current" aria-current="step"><strong>{esc(label)}（当前阶段）</strong></li>')
            else: steps.append(f'<li class="todo">{esc(label)}（未到达：{esc(self.STAGE_PRE.get(key,"前置条件：完成前一阶段"))}）</li>')
        nav=f'<nav aria-label="业务阶段"><ol class="stages">{"".join(steps)}</ol></nav>'
        waiting=self.WAIT_LABEL.get(state.get("waiting_reason"),state.get("waiting_reason")) if state.get("waiting_reason") else "无"
        action=self.ACTION_LABEL.get(state.get("required_action"),state.get("required_action")) if state.get("required_action") else None
        frozen=f'已冻结（快照 {esc(view.get("snapshot_hash","")[:12])}…）' if snapshot else "尚未导入"
        status_bar=f'<p class="status">阶段：{esc(dict(self.STAGE_LIST).get(current,current))}　运行状态：{esc(self.STATUS_LABEL.get(state["status"],state["status"]))}　输入：{frozen}　等待原因：{esc(waiting)}　所需动作：{esc(action or "无")}</p>'
        import_button="重建快照并重新扫描授权资源" if snapshot else "导入并扫描授权资源"
        import_form=f'''<form id="import"><h2>创建/导入任务卡</h2><label>格式 <select id="fmt"><option value="markdown">Markdown</option><option value="json">JSON</option></select></label><textarea id="source" aria-label="任务卡" placeholder="演示目标：...\n受众：...\n核心主题：..."></textarea><label><input type="checkbox" id="rebuild"> 显式重建快照（仅大纲确认前可用）</label><br><button>{import_button}</button><pre id="result" aria-live="polite"></pre></form>'''
        if snapshot:
            def field(key):
                value=card.get(key); return esc(value) if value else '<mark>待澄清（阻断）</mark>'
            constraints="".join(f'<li>{esc(k)}：{esc(v)}</li>' for k,v in (card.get("constraints") or {}).items()) or "<li>无</li>"
            defaults=card.get("defaults") or {}
            default_items="".join(f'<li>{esc(label)}：{esc(defaults.get(key,"-"))}（默认值，可在任务卡中覆盖）</li>' for key,label in (("language","语言"),("aspect_ratio","画布比例"),("sample_count","样品页数")))
            assumptions="".join(f'<li>{esc(a)}</li>' for a in card.get("assumptions") or []) or "<li>无</li>"
            missing="".join(f'<li><span class="badge">阻断</span>{esc(self.FIELD_LABEL.get(key,key))}</li>' for key in card.get("missing") or []) or "<li>无缺失项</li>"
            card_section=f'<section aria-label="任务卡"><h2>任务卡</h2><dl><dt>演示目标</dt><dd>{field("goal")}</dd><dt>受众</dt><dd>{field("audience")}</dd><dt>核心主题</dt><dd>{field("topic")}</dd></dl><h3>约束</h3><ul>{constraints}</ul><h3>默认值</h3><ul>{default_items}</ul><h3>显式假设</h3><ul>{assumptions}</ul><h3>缺失项</h3><ul>{missing}</ul></section>'
        else:
            card_section='<section aria-label="任务卡"><h2>任务卡</h2><p>尚未导入任务卡，请先在左侧提交 Markdown 或 JSON 任务卡。</p></section>'
        resources=manifest.get("resources") or []; warnings=manifest.get("warnings") or []
        if snapshot:
            rows="".join(f'<tr><td>{esc(r["uri"])}</td><td>{esc(r["media_type"])}</td><td><code>{esc(r["content_hash"][:12])}…</code></td><td>{esc(r.get("description") or "无配套说明")}</td></tr>' for r in resources)
            table=f'<table><thead><tr><th>资源</th><th>类型</th><th>内容 hash</th><th>说明</th></tr></thead><tbody>{rows}</tbody></table>' if rows else '<p>尚未发现授权资源；没有图片也可以继续规划，可在大纲确认前补充并重建快照。</p>'
            def warning_text(w):
                label=self.WARNING_LABEL.get(w.get("code"),w.get("code")); extra=f'（{esc(w.get("same_as"))}）' if w.get("same_as") else ""
                return f'<li>{esc(w.get("path"))}：{esc(label)}{extra}</li>'
            warning_list=f'<h3>资源诊断</h3><ul>{"".join(warning_text(w) for w in warnings)}</ul>' if warnings else ""
            resource_section=f'<section aria-label="资源清单"><h2>资源清单（{len(resources)} 项）</h2>{table}{warning_list}</section>'
        else:
            resource_section='<section aria-label="资源清单"><h2>资源清单</h2><p>导入任务卡后自动扫描当前任务授权资源目录。</p></section>'
        if not snapshot:
            qa='<p>导入任务卡后生成澄清问题。</p>'
        elif not questions:
            qa='<p>当前没有待展示问题。</p>'
        else:
            banner='<p class="ok">澄清已确认，所有阻断问题均已回答；仍可修改回答，修改会使相关下游产物标记过期。</p>' if clarification.get("confirmed") else ""
            forms=[]
            for q in questions:
                qid=q["question_id"]; blocking='<span class="badge">阻断</span>' if q.get("blocking") else '<span class="badge plain">非阻断</span>'
                radios="".join(f'<label><input type="radio" name="option" value="{esc(o)}" required> {esc(o)}</label>' for o in q.get("options",[]))
                other=f'<label><input type="radio" name="option" value="Other" required> Other（自定义）</label><label class="other-text">自定义回答 <input type="text" name="other" aria-label="Other 自定义回答"></label>' if q.get("allow_other") else ""
                current_answer=answers.get(qid); answer_line=f'<p>当前回答：<strong>{esc(current_answer)}</strong></p>' if current_answer is not None else '<p>当前回答：待回答</p>'
                button="修改回答" if current_answer is not None else "提交回答"
                forms.append(f'<form class="answer" data-qid="{esc(qid)}"><fieldset><legend>{esc(q["prompt"])} {blocking}</legend>{answer_line}{radios}{other}<br><button type="submit">{button}</button><output class="answer-result" aria-live="polite"></output></fieldset></form>')
            qa=banner+"".join(forms)
        clarification_section=f'<section id="clarification" aria-label="澄清"><h2>澄清</h2>{qa}</section>'
        if action:
            link=' <a href="#clarification">前往回答</a>' if state.get("required_action")=="answer_clarifications" else ""
            main_action=f'<p>{esc(action)}{link}</p>'
        else:
            main_action='<p>资料已可用于下一阶段。</p>' if snapshot else '<p>请先导入任务卡。</p>'
        action_section=f'<section aria-label="主操作"><h2>主操作</h2>{main_action}</section>'
        task_js=json.dumps(task_id)
        script='''<script>
const TASK_ID='''+task_js+''';
document.querySelector('#import').addEventListener('submit',async(e)=>{
e.preventDefault();
const r=await fetch('/v1/tasks/'+TASK_ID+'/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:source.value,source_format:fmt.value,rebuild:rebuild.checked})});
document.querySelector('#result').textContent=JSON.stringify(await r.json(),null,2);
if(r.ok)setTimeout(()=>location.reload(),500);
});
document.querySelectorAll('form.answer').forEach((form)=>{
form.addEventListener('submit',async(e)=>{
e.preventDefault();
const out=form.querySelector('.answer-result');
const picked=form.querySelector('input[name="option"]:checked');
if(!picked){out.textContent='请选择一个选项';return;}
const body={option:picked.value};
if(picked.value==='Other'){
const text=form.querySelector('input[name="other"]').value.trim();
if(!text){out.textContent='选择 Other 时必须填写自定义回答';return;}
body.other=text;
}
const r=await fetch('/v1/tasks/'+TASK_ID+'/clarifications/'+encodeURIComponent(form.dataset.qid)+'/answer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(r.ok){location.reload();}else{const data=await r.json();out.textContent=(data.error&&data.error.message)||'提交失败，请重试';}
});
});
</script>'''
        raw=('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>任务/资料</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:0 24px;color:#172033}'
        'main{display:grid;grid-template-columns:1fr 1fr;gap:24px}section,form{border:1px solid #ccd3df;border-radius:12px;padding:20px}'
        'form.answer{border:none;padding:0;margin:12px 0}fieldset{border:1px solid #ccd3df;border-radius:12px;padding:16px}legend{font-weight:600;padding:0 6px}'
        'textarea{width:100%;min-height:260px}button,select{padding:10px;margin-top:10px}label{display:block;margin:6px 0}label.other-text{margin-left:24px}'
        'table{width:100%;border-collapse:collapse}th,td{border:1px solid #ccd3df;padding:6px 10px;text-align:left;font-size:14px}'
        '.status{background:#f1f5fa;padding:12px;border-radius:8px}.badge{background:#b42318;color:#fff;border-radius:6px;padding:2px 8px;font-size:12px;margin-right:6px}'
        '.badge.plain{background:#475467}.ok{background:#ecfdf3;border:1px solid #abefc6;border-radius:8px;padding:10px}mark{background:#fef0c7;padding:2px 6px;border-radius:4px}'
        '.stages{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0}.stages li{border:1px solid #ccd3df;border-radius:8px;padding:6px 10px;font-size:14px}'
        '.stages li.current{border-color:#1570ef;background:#eff4ff}.stages li.todo{color:#667085}'
        'output{display:block;min-height:20px;color:#b42318;margin-top:8px}@media(max-width:760px){main{grid-template-columns:1fr}}</style>'
        f'<h1>任务/资料</h1>{nav}{status_bar}<main><div>{import_form}{card_section}</div><div>{resource_section}{clarification_section}{action_section}</div></main>{script}</html>').encode()
        start("200 OK",[("Content-Type","text/html; charset=utf-8"),("Content-Length",str(len(raw)))]); return [raw]

    def outline_page(self,start,task_id):
        view=self.service.planning_view(task_id); esc=self.esc
        narrative=view.get("narrative") or {}; outline=view.get("outline") or {}; state=view["state"]
        def timeline(kind):
            rows=[]
            for item in [v for v in view["versions"] if v["kind"]==kind]:
                meta=item["metadata"]; rows.append(f'<li><code>{esc(item["hash"][:12])}</code> {esc(meta.get("summary",meta.get("action","版本")))} <button class="rollback" data-kind="{kind}" data-hash="{item["hash"]}">回退</button></li>')
            return "".join(rows) or "<li>尚无版本</li>"
        raw=(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>大纲工作区</title>
<style>body{{font:16px system-ui;max-width:1280px;margin:24px auto;padding:0 20px;color:#172033}}nav a{{margin-right:16px}}main{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}section{{border:1px solid #ccd3df;border-radius:12px;padding:18px}}textarea{{width:100%;min-height:420px}}button,input{{padding:9px;margin:5px}}.meta{{background:#f1f5fa;padding:10px;border-radius:8px}}@media(max-width:800px){{main{{grid-template-columns:1fr}}}}</style>
<h1>大纲工作区</h1><nav><a href="/tasks/{esc(task_id)}">任务/资料</a><strong>大纲</strong><span>样品（前置条件：完成逐页大纲）</span></nav><p>每次操作形成可见版本，历史版本支持非破坏回退。</p>
<p class="meta">阶段：{esc(state['stage'])}　模式：{esc(state['mode'])}　状态：{esc(state['status'])}。大纲阶段不生成视觉预览。</p>
<main><section aria-label="叙事结构"><h2>整稿叙事结构</h2><form data-kind="narrative"><textarea aria-label="叙事 Markdown">{esc(narrative.get('markdown',''))}</textarea><input name="prompt" aria-label="叙事修改 Prompt" placeholder="输入修改要求"><button name="generate" type="button">生成/整体重生成</button><button name="save">保存直接编辑</button><button name="confirm" type="button">确认叙事</button><output aria-live="polite"></output></form><h3>版本</h3><ul>{timeline('narrative')}</ul></section>
<section aria-label="逐页大纲"><h2>逐页大纲</h2><form data-kind="outline"><textarea aria-label="逐页大纲 Markdown">{esc(outline.get('markdown',''))}</textarea><input name="prompt" aria-label="大纲修改 Prompt" placeholder="输入页/章节修改要求"><button name="generate" type="button">生成/整体重生成</button><button name="save">保存直接编辑</button><button name="confirm" type="button">确认大纲</button><output aria-live="polite"></output></form><p>最近影响范围：{esc(', '.join((outline.get('metadata') or {}).get('affected',[])) or '无')}</p><h3>版本</h3><ul>{timeline('outline')}</ul></section></main>
<script>const ID={json.dumps(task_id)};document.querySelectorAll('form[data-kind]').forEach(f=>{{const k=f.dataset.kind,send=async(url,data)=>{{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(data)}});if(r.ok)location.reload();else f.querySelector('output').textContent=((await r.json()).error||{{}}).message||'操作失败';}};f.onsubmit=e=>{{e.preventDefault();send('/v1/tasks/'+ID+'/'+k,{{markdown:f.querySelector('textarea').value}})}};f.querySelector('[name=generate]').onclick=()=>send('/v1/tasks/'+ID+'/'+k+'/generate',{{prompt:f.querySelector('[name=prompt]').value}});f.querySelector('[name=confirm]').onclick=()=>send('/v1/tasks/'+ID+'/'+k+'/confirm',{{}});}});document.querySelectorAll('.rollback').forEach(b=>b.onclick=()=>fetch('/v1/tasks/'+ID+'/planning/rollback',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{kind:b.dataset.kind,hash:b.dataset.hash}})}}).then(()=>location.reload()));</script></html>''').encode()
        start("200 OK",[("Content-Type","text/html; charset=utf-8"),("Content-Length",str(len(raw)))]); return [raw]

    def sample_page(self,start,task_id):
        view=self.service.sample_view(task_id); esc=self.esc; selection=view.get("selection") or {}; sample=view.get("sample") or {}
        ids=selection.get("slide_ids",[]); checks="".join(f'<label><input type="checkbox" name="slide" value="{esc(x)}" checked> {esc(x)}</label>' for x in ids)
        preview=sample.get("html",""); version=sample.get("version","-"); confirmed=view.get("confirmation")
        raw=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>HTML 样品页</title><style>body{{font:16px system-ui;max-width:1200px;margin:24px auto}}main{{display:grid;grid-template-columns:320px 1fr;gap:20px}}section{{border:1px solid #ccd3df;padding:16px;border-radius:10px}}iframe{{width:100%;height:620px;border:0}}label,button{{display:block;margin:8px 0}}textarea{{width:100%;height:100px}}</style><h1>HTML 样品页</h1><p>样品版本：{esc(version)}；确认状态：{'已绑定确认' if confirmed else '待人工确认'}</p><main><section><h2>样品选择</h2><div id="slides">{checks or '尚未推荐，生成时默认推荐 2 页'}</div><button id="select">保存选择</button><h2>修改</h2><label>作用域 <select id="scope"><option>global</option><option>page</option><option>element</option></select></label><label>页面 ID <input id="slide"></label><label>元素 ID <input id="element"></label><textarea id="prompt" placeholder="输入视觉修改要求"></textarea><button id="generate">生成样品</button><button id="modify">提交修改</button><button id="confirm">确认样品并生成全稿</button><output></output></section><section><h2>安全预览</h2><iframe sandbox="" srcdoc="{esc(preview)}"></iframe></section></main><script>const base='/v1/tasks/{esc(task_id)}/samples/';async function send(a,b){{const r=await fetch(base+a,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});if(r.ok)location.reload();else document.querySelector('output').textContent=((await r.json()).error||{{}}).message}}select.onclick=()=>send('select',{{slide_ids:[...document.querySelectorAll('[name=slide]:checked')].map(x=>x.value)}});generate.onclick=()=>send('generate',{{prompt:prompt.value}});modify.onclick=()=>send('modify',{{prompt:prompt.value,scope:scope.value,slide_id:slide.value||null,element_id:element.value||null}});confirm.onclick=()=>send('confirm',{{}});</script></html>'''.encode()
        start("200 OK",[("Content-Type","text/html; charset=utf-8"),("Content-Security-Policy","default-src 'self'; frame-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'"),("Content-Length",str(len(raw)))]); return [raw]

def serve(root=".ppt-agent-data",host="127.0.0.1",port=8000):
    make_server(host,port,App(TaskService(WorkspaceStore(root)))).serve_forever()
