# PPT Agent MVP

PPT Agent MVP 的需求、实现决策与验收追踪仓库。

P4 HTML 样品页闭环已实现，包括样品推荐与改选、真实 HTML 安全预览、元素/页面/全局修改、版本追踪，以及绑定大纲与样品 hash 的人工确认门禁。产品行为以 `docs/product-contract.md` 为准，实施顺序以 `docs/development-plan.md` 为准。

## 本地启动

环境要求：Python 3.10+。无需安装第三方依赖。

```bash
python3 scripts/start.py --data .ppt-agent-data --host 127.0.0.1 --port 8000
```

另一个终端访问 `http://127.0.0.1:8000/healthz`，应得到含 `"stage": "P4"`、`"status": "ok"` 和 `"runtime_ready": true` 的 JSON。任务、资料、叙事、大纲和 HTML 样品 API 均由该入口提供。

## P0 校验

环境要求：Git 与 Python 3.10+。

```bash
python3 scripts/verify_p0.py
```

该命令离线检查 P0 必需文件、18 条验收映射字段、责任任务有效性、P0-01 证据和最小启动入口；不安装依赖、不调用模型。

## 文档入口

- `docs/current-state.md`：仓库及现有能力盘点
- `docs/acceptance-matrix.md`：需求—实现—测试追踪矩阵
- `docs/adr/README.md`：架构决策索引与 MVP 非目标
- `docs/product-contract.md`：已确认产品需求与流程契约
- `docs/development-plan.md`：分阶段开发任务清单

## P1 运行与测试

```bash
python3 scripts/export_schemas.py
python3 -m unittest discover -s tests -v
python3 scripts/start.py --data .ppt-agent-data
```

API 契约见 `docs/openapi.yaml`，内核契约见 `docs/p1-contract.md`。

## P2 使用

创建任务后，向 `POST /v1/tasks/{task_id}/input` 提交 `source`、`source_format`（`json`/`markdown`）及可选 `rebuild`。服务只扫描任务工作区的 `resources/`；图片可使用同名 `.md` 描述，内容损坏或格式不符的图片会被诊断为 `invalid_image_content` 且不纳入清单。`GET /tasks/{task_id}` 提供桌面端“任务/资料”页面：展示任务卡摘要、可见默认值、阻断缺失项、资源清单与诊断、当前主操作，并可在页面内直接以选择题或 `Other` 自定义完成澄清回答与改答。`GET /v1/tasks/{task_id}/input` 返回当前冻结快照、任务卡、资源清单和澄清状态，`POST /v1/tasks/{task_id}/clarifications/{question_id}/answer` 保存选项或 `Other` 回答。

输入首次导入即冻结；目录后续变化不会静默进入当前 manifest，只有在大纲前显式 `rebuild: true` 才会生成新快照。

## P4 样品闭环

`GET /tasks/{task_id}/samples` 打开安全沙箱预览。样品只内嵌当前冻结 manifest 中、读取时 hash 仍匹配的图片；外链、跨任务路径、未授权 data URL 与主动内容会被拒绝。`POST /v1/tasks/{task_id}/samples/modify` 可只提交 Prompt，并结合当前 `slide_id`/`element_id` 自动判断全局、页面或元素范围；语义冲突或明显歧义返回可理解的校验错误。确认事实原子绑定当前大纲、选择与样品内容版本。
