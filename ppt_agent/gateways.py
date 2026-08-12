from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

class GenerationGateway(Protocol):
    def generate(self, action:str, payload:dict, *, skill:str)->dict: ...
class InspectionGateway(Protocol):
    def inspect(self, original_outline:str, html:str)->dict: ...
class SkillLoader(Protocol):
    def load(self, action:str)->dict: ...
class HtmlBuilder(Protocol):
    def build(self, outline:str)->str: ...

@dataclass
class FakeSkillLoader:
    version:str="fake-skill-1"
    def load(self,action): return {"action":action,"version":self.version,"content":f"rules:{action}"}
@dataclass
class FakeGenerationGateway:
    model:str="fake-generator-1"
    def generate(self,action,payload,*,skill): return {"text":f"{action}:{payload.get('task_id','task')}","model":self.model,"skill":skill}
@dataclass
class FakeInspectionGateway:
    model:str="fake-inspector-1"
    calls:list|None=None
    def inspect(self,original_outline,html):
        if self.calls is not None:self.calls.append({"outline":original_outline,"html":html})
        return {"issues":[{"issue_id":"fake-overflow","severity":"blocker","level":"element","code":"text_overflow","message":"标题文本溢出","slide_id":"slide-1","element_id":"title","evidence":"标题超出安全框","suggestion":"缩短标题或减小字号"}],"passed":False,"model":self.model}
@dataclass
class FakeHtmlBuilder:
    version:str="fake-builder-1"
    def build(self,outline): return f"<!doctype html><html><body>{outline}</body></html>"
