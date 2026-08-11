# 当前状态与能力盘点

更新日期：2026-08-11  
当前边界：P4 收口完成，等待独立复验；尚未进入 P5。

## 已实现

- P0～P1：可启动运行内核、严格 Schema、状态机、原子文件工作区、事件与不可变版本。
- P2：任务卡、澄清、受控资源扫描及冻结输入快照。
- P3：叙事与逐页大纲生成、直接编辑、局部影响域、版本及人工确认。
- P4：样品推荐/改选、真实 HTML、安全沙箱、冻结资源 hash 复核与内嵌、Prompt 作用域识别、精确版本确认门禁。

## 运行与验证

```bash
python3 scripts/export_schemas.py
python3 -m unittest discover -s tests -v
python3 scripts/start.py --data .ppt-agent-data
```

健康检查返回 `stage=P4`、`runtime_ready=true`。机器契约见 `docs/openapi.yaml`。

## 尚未开始

P5 全稿生成、P6 独立检查与修复、P7 人工审核交付、P8 完整 E2E/安全验收仍须按开发清单顺序实施。P4 独立复验通过前不得进入 P5。
