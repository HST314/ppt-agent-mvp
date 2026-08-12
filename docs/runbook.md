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

真实 API 使用 `config/ppt-agent.yaml` 的 `gateway.mode: agent`。生成与检查分别配置 `provider: openai_responses`、模型、超时、最大步数，以及保存环境变量名称的 `api_key_env`/`base_url_env`；秘密值只放 `.env`，不要写进 YAML 或日志。检查模型未配置时只有显式 `fallback_to_generation: true` 才允许回退。Skill 根目录为 `ppt_agent/builtin_skills/guizang-ppt/`，以 `SKILL_LOCK.json` 校验并渐进读取。

## 使用流程

1. 创建任务并导入 JSON/Markdown 资料，处理阻断澄清项。
2. 生成并人工确认叙事；生成并确认逐页大纲。
3. 生成样品、按 Prompt 修改，并由用户确认当前样品版本。
4. 生成全稿；在 manual/auto 模式下完成独立检查和问题处置。
5. 当前检查未过期且无未处置 blocker 时，由用户绑定当前 `deck_hash` 确认交付。
6. 对交付目录执行离线打包与校验；如需修改，从历史交付派生新候选并重新检查。

## 测试与离线验收

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_browser_gate.py
python3 scripts/build_offline_bundle.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>
python3 scripts/verify_offline_delivery.py .ppt-agent-data/tasks/<task-id>/deliveries/<delivery-id>.zip
```

最后两条命令会拒绝 hash 不匹配、缺失/多余文件、无效 HTML、外部 HTTP(S)/协议相对 URL 和危险 ZIP 路径。把 ZIP 复制到断网临时目录，重新校验并解压，直接打开 `deck.html`，人工确认翻页/滚动与文字、清单内图片均正常。不要通过本地 HTTP 服务掩盖跨目录引用问题。

## 故障排查

- 启动时报配置错误：确认 YAML 字段白名单、`gateway.mode`，以及 YAML 引用的环境变量均已在 `.env` 配置。
- `gateway_error`：根据诊断 ID 检查端点、证书和超时；修正后再由用户重试。
- `gateway_unknown_result`：不得自动重试，先查供应商请求记录和任务摘要，避免产生重复候选。
- Skill 校验失败：不要直接修改内置文件；恢复经过评审的 Skill 与 `SKILL_LOCK.json` 配套版本。
- 离线校验失败：按错误中的 missing/extra/changed 或 URL 文件修复源交付并重新确认，禁止手改已发布目录。
- 浏览器门禁 skipped：安装锁定 Playwright/Chromium 及系统共享库后重跑，不能把跳过当成功。

模型调用超时返回 `gateway_error`；连接在请求发出后中断返回 `gateway_unknown_result`。两者都带诊断 ID 且不暴露密钥或供应商原始异常。未知结果不得自动重试：操作人员先查供应商请求记录，再通过任务摘要确认最后成功版本，最后由用户选择重试、修改输入或人工处理。

备份应复制整个数据目录并保留权限；恢复时先停止服务，将备份复制到新目录后以 `--data` 指向该目录启动。内核会重放 `pending-commit.json` 完成原子提交；交付目录和历史版本不可覆盖。恢复后运行测试，并抽查 `/summary`、版本列表、事件列表与交付 manifest hash。

日志和工单只记录诊断 ID、动作、耗时和结果类别；不得记录 API key、完整 Prompt、供应商原始响应或内部推理。
