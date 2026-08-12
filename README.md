# PPT Agent MVP

PPT Agent MVP 的需求、实现决策与验收追踪仓库。

P8 后端发布边界已实现：全写动作生命周期保护、版本 hash 审批、真实 Builder/分阶段 Skill、生成后自动独立检查、有界修复、请求与资源大小限制及结构化错误日志。产品行为以 `docs/product-contract.md` 为准。

## 快速开始（默认 fake、可离线）

环境要求：Python 3.10+。首次运行测试前安装声明依赖：`python3 -m pip install -r requirements.txt`。

```bash
python3 scripts/start.py --data .ppt-agent-data --host 127.0.0.1 --port 8000
```

另一个终端访问 `http://127.0.0.1:8000/healthz`，应得到含 `"stage": "P8"`、`"status": "ok"` 和 `"runtime_ready": true` 的 JSON。

默认配置 `config/ppt-agent.yaml` 使用 deterministic fake：不需要 `.env`、网络或密钥。启动后从任务页依次完成资料导入、澄清、叙事确认、大纲确认、样品确认、全稿、独立检查和最终交付确认。内置 Skill 位于 `ppt_agent/builtin_skills/guizang-ppt/`，运行时只按需读取 `SKILL.md` 与 lock 清单中的 references/assets。

真实 Responses API 模式请复制配置文件，把 `gateway.mode` 改为 `agent`，并按 YAML 中 `api_key_env`、`base_url_env` 指定的变量名在 `.env` 中提供生成与检查配置；推荐两者使用独立模型配置。然后运行：

```bash
PPT_AGENT_CONFIG=config/ppt-agent.yaml python3 scripts/start.py --data .ppt-agent-data
```

Base URL 必须为 HTTPS（回环调试除外），请求不携带图片或联网工具。连接结果未知时不会自动重试；先核对供应商请求记录，再由用户决定下一步。完整配置、操作和故障恢复见 `docs/runbook.md`。

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

样品页脚本对全部控件使用显式 `getElementById` 绑定，不依赖 window 隐式命名属性（`prompt`/`confirm` 会与浏览器原生冲突）。`tests/test_p4_sample_page_browser.py` 在真实 headless Chromium 中执行页面 JavaScript，覆盖自动识别提交、理解依据刷新展示、歧义提示与确认门禁四条交互；该模块需要额外依赖，缺失时自动跳过、不影响全量套件：

```bash
pip install playwright
python3 -m playwright install chromium
python3 -m unittest discover -s tests
```

## P8 固定 Chromium 门禁

浏览器证据固定使用 Playwright 1.54.0（Chromium 139.0.7258.5 / build v1181）。门禁脚本会执行现有 P4、P6 浏览器回归及 AC-18 桌面完整旅程，任一用例跳过即失败：

```bash
python3 -m pip install -r requirements-browser.txt
python3 -m playwright install chromium
python3 scripts/verify_browser_gate.py
```

## P6 独立检查与审核

`POST /v1/tasks/{task_id}/inspection/run` 执行首次全检或指定页面增量检查；检查 Gateway 仅接收最初大纲和待审 HTML。`POST /inspection/mode` 切换 manual/auto，切换只影响下一动作；auto 在 `max_rounds` 内修复并复检，达到上限进入等待人工且不会虚假完成。`POST /issues/{issue_id}/disposition` 保存 Agent 修复、手工处理、豁免或暂缓的操作者、依据和目标 HTML 版本。`POST /inspection/delivery-gate` 在当前报告过期或存在未处置阻断问题时拒绝交付。`GET /tasks/{task_id}/inspection` 提供分组问题、联动定位、轮次和整稿人工浏览界面。

## P7 交付与恢复

`POST /v1/tasks/{task_id}/delivery/confirm` 必须由用户提交当前候选 `deck_hash`，检查报告有效且阻断问题全部解决或豁免后才会完成任务。交付目录包含冻结的 `deck.html`、可直接打开的离线播放器 `index.html`、包内 Skill 动效资产与翻页逻辑、两类 Markdown、冻结资源清单、授权资源、结果摘要和逐文件 hash manifest，并以目录级原子发布避免半交付。离线播放器支持按钮与 Arrow/PageUp/PageDown/Space/Home/End 键导航，不请求 CDN。`POST /delivery/derive` 从历史交付派生新候选，旧交付保持不可变，新候选必须重新检查和确认。`GET /summary` 输出不含对话或推理的编排摘要；通用 actions 接口提供 pause/resume/cancel/fail/retry，非活动状态不会启动新的生成或检查动作。

## 阶段 D 离线交付

最终确认后，交付目录位于 `<data>/tasks/<task-id>/deliveries/<delivery-id>/`。下列命令先校验 manifest、HTML 和外部 URL，再生成内容与时间戳均确定的 ZIP；相同交付重复打包应得到相同 SHA-256：

```bash
python3 scripts/build_offline_bundle.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>
python3 scripts/verify_offline_delivery.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>.zip
```

将 ZIP 搬到断网机器后再次执行校验，随后直接用浏览器打开解压后的 `deck.html`。ZIP 的 SHA-256 会由打包命令输出，适合作为传输校验值；ZIP 内 `manifest.json` 覆盖全部实际交付内容文件。
`--output` 必须位于交付目录之外；打包器会在写入前拒绝污染交付目录的输出位置。ZIP 校验同时拒绝 POSIX/Windows 路径穿越、绝对路径、盘符、空路径段、重复条目及非普通文件类型。
