from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .errors import GateError


class Stage(str, Enum):
    CREATED="created"; CLARIFICATION="clarification"; NARRATIVE="narrative"; OUTLINE="outline"; SAMPLE="sample"; DECK="deck"; REVIEW="review"; DELIVERY="delivery"
class RunStatus(str, Enum):
    READY="ready"; RUNNING="running"; WAITING_FOR_USER="waiting_for_user"; PAUSED="paused"; CANCELLED="cancelled"; FAILED="failed"; COMPLETED="completed"

@dataclass(frozen=True)
class TaskState:
    task_id: str; stage: Stage = Stage.CREATED; status: RunStatus = RunStatus.READY; mode: str = "manual"
    sample_confirmed: bool = False; blockers_resolved: bool = False; delivery_confirmed: bool = False; revision: int = 0

    def to_dict(self):
        d=self.__dict__.copy(); d["stage"]=self.stage.value; d["status"]=self.status.value; return d

    @classmethod
    def parse(cls, d):
        return cls(**{**d, "stage":Stage(d["stage"]), "status":RunStatus(d["status"])})


NEXT = {Stage.CREATED:Stage.CLARIFICATION, Stage.CLARIFICATION:Stage.NARRATIVE, Stage.NARRATIVE:Stage.OUTLINE,
        Stage.OUTLINE:Stage.SAMPLE, Stage.SAMPLE:Stage.DECK, Stage.DECK:Stage.REVIEW, Stage.REVIEW:Stage.DELIVERY}

def transition(s: TaskState, action: str, *, actor: str="system") -> TaskState:
    if s.status in {RunStatus.CANCELLED, RunStatus.COMPLETED}: raise GateError("任务已结束，不能执行该动作")
    if action == "pause": return replace(s,status=RunStatus.PAUSED,revision=s.revision+1)
    if action == "resume":
        if s.status != RunStatus.PAUSED: raise GateError("只有暂停任务可以恢复")
        return replace(s,status=RunStatus.READY,revision=s.revision+1)
    if action == "cancel": return replace(s,status=RunStatus.CANCELLED,revision=s.revision+1)
    if action == "switch_manual": return replace(s,mode="manual",revision=s.revision+1)
    if action == "switch_auto": return replace(s,mode="auto",revision=s.revision+1)
    if action == "confirm_sample":
        if s.stage != Stage.SAMPLE or actor == "system": raise GateError("样品必须由用户在样品阶段确认")
        return replace(s,sample_confirmed=True,status=RunStatus.READY,revision=s.revision+1)
    if action == "resolve_blockers": return replace(s,blockers_resolved=True,revision=s.revision+1)
    if action == "confirm_delivery":
        if s.stage != Stage.DELIVERY or actor == "system" or not s.blockers_resolved: raise GateError("须由用户解决或豁免阻断问题后确认交付")
        return replace(s,delivery_confirmed=True,status=RunStatus.COMPLETED,revision=s.revision+1)
    if action == "advance":
        if s.status == RunStatus.PAUSED: raise GateError("任务已暂停")
        if s.stage == Stage.SAMPLE and not s.sample_confirmed: raise GateError("请先确认样品")
        if s.stage == Stage.DELIVERY: raise GateError("生成或检查不能自动完成任务")
        ns=NEXT.get(s.stage)
        if not ns: raise GateError("当前阶段不能继续推进")
        waiting = (s.mode == "manual" and ns in {Stage.NARRATIVE,Stage.SAMPLE,Stage.DELIVERY}) or ns == Stage.DELIVERY
        return replace(s,stage=ns,status=RunStatus.WAITING_FOR_USER if waiting else RunStatus.READY,revision=s.revision+1)
    raise GateError("未知动作")
