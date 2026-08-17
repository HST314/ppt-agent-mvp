# P8 本地运行与恢复手册

## 环境前置

- Python 3 与 `venv` 模块必须可用；Debian/Ubuntu 环境如缺失 `venv`，先安装与当前 Python 版本匹配的 `python3-venv` 系统包。
- 在隔离环境安装测试依赖：`python3 -m venv .venv`，激活后执行 `python3 -m pip install -r requirements.txt pytest`。`PyYAML` 已在 `requirements.txt` 锁定，供 OpenAPI 契约门禁使用；标准回归仍以仓库内置 `unittest` 命令为准，pytest 用于审计/验收执行与结果汇总。
- AC-18 浏览器门禁必须安装锁定依赖 `python3 -m pip install -r requirements-browser.txt`，再执行 `python3 -m playwright install chromium`。若 Chromium 报缺少共享库，由环境管理员按 Playwright 输出补齐系统浏览器依赖；不得以跳过浏览器用例作为替代。
- 固定验收组合为 Playwright 1.54.0 与 Chromium 139.0.7258.5（build v1181）。运行 `python3 scripts/verify_browser_gate.py`，仅 0 failed、0 skipped 可视为门禁通过。

## 启动与配置

默认离线/fake 启动无需 `.env`：

```bash
python3 scripts/start.py --data .ppt-agent-data --host 127.0.0.1 --port 8000
```

该命令启动 FastAPI/Uvicorn Web 适配层。`/livez` 只检查 Web 进程存活并始终以 200 表示存活；`/readyz` 检查真实模型运行契约，未就绪时返回 503。兼容端点 `/healthz` 与 readiness 使用相同的 200/503 语义；浏览器轮询 `/v1/runtime/status`，该端点固定返回 200，并通过 `runtime_ready` 表达模型状态，避免预期的未就绪状态污染浏览器控制台。真实模型模式必须满足 `runtime_ready=true` 且 `model_capabilities.status=ready`。启动按顺序验证基础文本响应、严格 JSON Schema、强制函数调用与结果回传；任一检查失败即停止，状态中保留 `probe_id`、`failed_check` 与精确业务错误码，依赖模型的 Job 在入队和执行前都会关闭失败。脱敏且可跨重启读取的探测记录由 `GET /v1/runtime/probes` 导出，只保存检查状态、HTTP/SDK 类别、request-id 哈希和响应 ID 哈希。修复配置或等待供应商恢复后，在工作台“显示与连接”执行显式重新检测。`/`、`/tasks/{task_id}` 和 `/components` 使用独立 `frontend/` 静态资源。8 阶段交互全部位于统一应用壳；旧 `/tasks/{task_id}/outline|samples|deck|inspection|delivery` 深链会规范化到对应阶段，`/legacy/**` 已下线。

样品、全稿和检查预览由 `/v1/tasks/{task_id}/previews/{hash}` 提供。端点只接受当前任务内 `sample`/`deck` 版本，返回 `no-store`、`SAMEORIGIN` 与禁止脚本的独立 CSP；应用壳 CSP 不允许内联脚本或样式。预览异常时先核对 hash 是否属于当前任务及对应版本，不要绕过端点直接读取工作区文件。

### Job 与 SSE 恢复

- 浏览器刷新后会查询 `/v1/tasks/{task_id}/jobs?status=active`，并从持久化的 `job_id ↔ intent storage key` 映射恢复清理责任；终态会同时清理 intent 与映射，下一次同参操作会生成新幂等键。
- 工作台通过 `/v1/jobs/{job_id}/event-history` 恢复最多 500 条持久化事件，并按 `(job_id, seq)` 去重；实时 SSE 与轮询都从最后已接收序号续传。执行详情显示 Agent 步数、provider 请求、只读工具调用、阶段剩余时间与脱敏 Agent 审计；终态 Job 的用时固定在 `finished_at`。
- SSE 断开后先执行有界指数退避重连；连续失败才降级轮询。轮询期间继续进行有限 SSE 恢复探测，成功后以 `after=last_seq` 续传并停止轮询；恢复连接再次断开时立即重启轮询，再消耗剩余探测次数，达到探测上限后保持轮询到终态。
- 每个任务同时只允许一个写业务状态的活动 Job。相同 `idempotency_key` 与相同请求返回原 Job；同 key 不同请求返回 409。
- 排队 Job 在服务重启后可重新调度；运行中或已请求取消但结果未知的 Job 会标记为 `interrupted`，必须由用户发起新的明确尝试。
- 活动 Job 不清理。MVP 终态 Job 至少保留 7 天；当前版本由运维在备份后按 `finished_at` 清理任务目录内的终态 `jobs/*.json` 及同名事件文件，不得清理活动状态。
- SSE 事件与 Job 错误只包含步骤、诊断 ID、`agent_audit_id` 和业务版本引用，不能写入完整 Prompt、客户资料、密钥或模型推理。按任务导出脱敏审计使用 `GET /v1/tasks/{task_id}/agent-audits`，可用 `job_id` 查询参数收窄；按 Job 导出使用 `GET /v1/jobs/{job_id}/agent-audits`。关联审计同时镜像到任务目录的 `agent-audit.jsonl`，因此任务目录归档会自带该任务审计。工具错误码区分 `invalid_arguments`、`path_not_in_lock`、`quota_exceeded`、`unauthorized_tool` 与其余校验错误。

仓库默认 `config/ppt-agent.yaml` 为 Agent 模式示例。真实 API 应复制 `config/ppt-agent.agent.example.yaml` 为本地配置，并配置 `provider: openai_responses`、模型与保存环境变量名称的 `api_key_env`/`base_url_env`；秘密值只放 `.env`，不要写进 YAML 或日志。`request_timeout_seconds` 必须小于 `run_timeout_seconds`，`job_timeout_seconds` 又必须严格大于 Agent 运行预算。规划阶段使用全局 30 步、40 次只读工具和 8 次 provider 请求；样品独立使用 8 步/4 工具/6 provider、最多 2 轮探索并预留最后 2 次请求；全稿独立使用 12 步/8 工具/10 provider、最多 3 轮探索并预留最后 2 次请求。收敛点按 Agent 步数与 provider 请求上限的较小值计算，确保在请求耗尽前可达。样品/全稿只读取版本化 `references/design-pack-v1.md`，已读路径不会重复注入正文。检查模型未配置时只有显式 `fallback_to_generation: true` 才允许回退。所有阶段仍不具备联网、Shell、写文件或状态机能力。

创建任务时可选择 `quick` 快速生成模式，并必须提交 `target_slide_count`（1–200）。该页数会写入冻结任务卡成为强约束；快速模式固定使用一轮 minimal 澄清，只保留阻断题，澄清完成后自动保存叙事、大纲并推进到样品门禁。

## 使用流程

1. 创建任务并导入 JSON/Markdown 资料，处理阻断澄清项。
2. 生成并人工确认叙事；生成并确认逐页大纲。
3. 生成样品、按 Prompt 修改，并由用户确认当前样品版本。
4. 生成全稿；在 manual/auto 模式下完成独立检查和问题处置。
5. 当前检查未过期且无未处置 blocker 时，由用户绑定当前 `deck_hash` 确认交付。
6. 对交付目录执行离线打包与校验；如需修改，从历史交付派生新候选并重新检查。

所有生成、修改样品、修改全稿和检查操作从界面创建持久化 Job。短操作（回答、直接编辑、确认、回退、问题处置、交付）直接调用现有 `/v1` 接口；页面刷新后仍以服务端任务、版本和 Job 为权威状态。

## 测试与离线验收

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_browser_gate.py
python3 scripts/update_frontend_build.py --check
python3 scripts/build_offline_bundle.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>
python3 scripts/verify_offline_delivery.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>.zip
```

最后两条命令会拒绝 hash 不匹配、缺失/多余文件、无效 HTML、外部 HTTP(S)/协议相对 URL，以及按 POSIX/Windows 任一语义危险、重复或非普通文件类型的 ZIP 条目。ZIP 输出必须位于交付目录外，非法输出位置会在任何写入前被拒绝。把 ZIP 复制到断网临时目录，重新校验并解压，直接打开 `deck.html`，人工确认翻页/滚动与文字、清单内图片均正常。不要通过本地 HTTP 服务掩盖跨目录引用问题。

## 故障排查

- 启动时报配置错误：确认 YAML 字段白名单、`gateway.mode`，以及 YAML 引用的环境变量均已在 `.env` 配置。
- `model_authentication_failed` / `model_permission_denied` / `model_not_found` / `model_request_invalid`：属于确定性配置故障，修复凭据、权限、模型名或 Responses/Schema 兼容性后重新探测，不要连续重试。
- `model_rate_limited`：遵守响应中的 `retry_after_seconds`（如有），等待后重新探测。
- `model_upstream_unavailable`：等待供应商恢复后重新探测；不要以连续提交代替健康检查。
- `model_timeout` / `model_connection_error`：结果可能未知，不得自动重试；先用 `agent_audit_id`、诊断 ID 和供应商请求记录核对结果。
- `gateway_error`：无法进一步分类的 SDK/HTTP 故障；根据诊断 ID 检查运行日志，确认原因后再操作。
- `probe_invalid_output` / `probe_tool_call_missing` / `probe_tool_round_failed` / `probe_tool_final_invalid_output` / `probe_step_limit`：分别表示严格 Schema 失败、未执行强制工具调用、工具结果回传失败、工具调用后最终 Schema 输出失败或步数边界未满足。结合 `failed_check`、`probe_phase`、`terminal_reason`、`tool_calls`、`underlying_code` 与 `probe_id` 查询 `/v1/runtime/probes`，确认模型能力与端点配置后再重新检测。
- Skill 校验失败：不要直接修改内置文件；恢复经过评审的 Skill 与 `SKILL_LOCK.json` 配套版本。
- 离线校验失败：按错误中的 missing/extra/changed 或 URL 文件修复源交付并重新确认，禁止手改已发布目录。
- 浏览器门禁 skipped：安装锁定 Playwright/Chromium 及系统共享库后重跑，不能把跳过当成功。

所有模型故障都保留稳定业务码与诊断 ID；Agent 路径还会返回可关联的 `agent_audit_id`。脱敏审计只保存 HTTP 状态、SDK 异常类型、可重试标志和供应商 request-id 哈希，不保存密钥、完整 Prompt、原始响应或内部推理。超时与连接中断属于结果可能未知，操作人员必须先查供应商记录和任务摘要，最后由用户选择重试、修改输入或人工处理。

备份应复制整个数据目录并保留权限；恢复时先停止服务，将备份复制到新目录后以 `--data` 指向该目录启动。内核会重放 `pending-commit.json` 完成原子提交；交付目录和历史版本不可覆盖。恢复后运行测试，并抽查 `/summary`、版本列表、事件列表与交付 manifest hash。

日志和工单只记录诊断 ID、动作、耗时和结果类别；不得记录 API key、完整 Prompt、供应商原始响应或内部推理。
