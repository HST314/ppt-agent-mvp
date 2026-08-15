# PPT Agent MVP

> 一个本地优先、人工可控的 AI 演示文稿工作台：从任务资料、叙事结构和逐页大纲，一路完成样品、全稿、独立检查与离线交付。

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Offline first](https://img.shields.io/badge/runtime-local%20%26%20offline-7C3AED)](#两种运行模式)

PPT Agent MVP 把演示稿生成拆成 8 个可追踪阶段。每次生成、编辑、确认和检查都会保存为不可静默覆盖的版本；长任务可以刷新恢复；最终结果可打包成不依赖 CDN 的离线文件。

当前仓库是可运行的 MVP，默认使用确定性的 fake 模式演示完整流程，不需要 API Key，也不会访问外部模型。

## 目录

- [它能做什么](#它能做什么)
- [5 分钟启动](#5-分钟启动)
- [第一次创建 PPT](#第一次创建-ppt)
- [如何准备图片资源](#如何准备图片资源)
- [两种运行模式](#两种运行模式)
- [工作流与架构](#工作流与架构)
- [测试](#测试)
- [常见问题](#常见问题)
- [更多文档](#更多文档)

## 它能做什么

- 统一工作台：任务/资料、澄清、叙事、大纲、样品、全稿、检查、交付均在同一个 Web 界面完成。
- 人工门禁：叙事、大纲、样品和最终交付都需要显式确认，不会在后台静默越过关键决策。
- 可恢复长任务：生成与检查使用持久化 Job；刷新页面或 SSE 短暂断线后仍能恢复进度。
- 版本化编辑：直接编辑、Prompt 修改、回退和对比都会保留历史版本。
- 资源白名单：只有冻结清单内且 hash 匹配的图片能进入样品和全稿。
- 独立检查：检查结果与原始大纲绑定，阻断问题必须修复、人工处置或豁免后才能交付。
- 离线交付：生成带完整性 manifest 的本地包，不依赖外部脚本、字体或 CDN。
- 安全默认值：严格 CSP、沙箱预览、请求大小限制、路径越权校验和不泄露密钥的错误封装。

## 5 分钟启动

### 1. 环境要求

- Python 3.10 或更高版本
- Git
- 推荐使用 Chromium、Chrome 或 Edge 的当前稳定版

### 2. 下载并安装

```bash
git clone https://github.com/HST314/ppt-agent-mvp.git
cd ppt-agent-mvp

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

Windows PowerShell 激活虚拟环境：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果 Debian/Ubuntu 提示缺少 `venv`，先安装与当前 Python 版本匹配的 `python3-venv` 系统包。

### 3. 启动本地服务

```bash
python scripts/start.py --data .ppt-agent-data --host 127.0.0.1 --port 8000
```

浏览器打开：

- 工作台：<http://127.0.0.1:8000/>
- 健康检查：<http://127.0.0.1:8000/healthz>
- 组件与交互状态：<http://127.0.0.1:8000/components>

看到健康检查中的 `"status": "ok"` 和 `"runtime_ready": true`，说明服务已就绪。

## 第一次创建 PPT

下面这条路径不需要图片、不需要模型密钥，适合先确认环境是否正常。

1. 在首页输入任务 ID，例如 `demo`。
2. 选择“人工模式（推荐）”，点击“创建任务并进入工作台”。
3. 任务卡格式选择 `Markdown`。
4. 粘贴下面的任务卡。字段名和中文冒号/英文冒号都可以，但每项必须单独成行。

```markdown
演示目标：向管理层汇报 2026 年上半年增长情况
受众：公司管理层
核心主题：增长来源、风险与下半年行动计划
页数：8
风格：简洁、专业、数据优先
```

5. 点击“导入并冻结资料”。
6. 如果没有准备图片，系统会弹窗说明“资源为空不是错误”。选择“继续无图片”即可完成纯文本流程。
7. 如果任务卡缺少目标、受众或主题，在“澄清”阶段逐项回答；资料完整时可以直接生成叙事结构。
8. 依次完成：叙事确认 → 大纲确认 → 样品确认 → 全稿 → 检查 → 交付。

默认 fake 模式生成的是用于验证工作流、版本和门禁的确定性内容，不代表真实模型的设计质量。要生成真实内容，请参见[真实 Agent 模式](#真实-agent-模式)。

## 如何准备图片资源

图片资源是可选项。没有图片不会导致流程失败；系统会提醒后允许继续。如果你希望演示稿使用 Logo、产品图、数据截图或品牌素材，需要在导入任务卡前放入任务资源目录。

默认数据目录下，任务 `demo` 的资源路径是：

```text
.ppt-agent-data/demo/resources/
```

如果启动时使用了 `--data /path/to/data`，对应路径就是：

```text
/path/to/data/demo/resources/
```

推荐结构：

```text
.ppt-agent-data/
└── demo/
    └── resources/
        ├── logo.png
        ├── logo.md
        ├── product-hero.webp
        ├── product-hero.md
        └── growth-chart.svg
```

资源规则：

- 支持 `.png`、`.jpg`、`.jpeg`、`.webp`、`.gif` 和安全 SVG。
- 单个资源最大 16 MiB。
- 建议为每张图片增加同名 `.md` 说明，帮助 Agent 理解用途、语义和使用限制。
- 同名说明示例：`product-hero.webp` 对应 `product-hero.md`。
- 资源只在导入任务卡或显式重建快照时扫描；之后直接往目录加文件不会自动进入当前任务。
- 已导入但需要补资源时：返回“任务/资料” → 勾选“显式重建快照并重新扫描授权资源” → 再次提交任务卡。

一个实用的图片说明文件可以这样写：

```markdown
# 产品主视觉

- 用途：首页或产品介绍页主图
- 必须保持完整比例，不裁切 Logo
- 图片右侧留白可放标题
- 不得用于风险或问题页面
```

当前 MVP 不提供浏览器上传控件，资源需要通过本地文件系统放入上述目录。

## 两种运行模式

### 默认 fake 模式

仓库默认配置为：

```yaml
gateway:
  mode: fake
```

特点：

- 无需 `.env` 或 API Key
- 不访问网络
- 输出确定、便于测试和复现
- 适合体验产品流程、开发前端和运行回归测试

### 真实 Agent 模式

先复制一份本地配置，避免直接修改仓库默认值：

```bash
cp config/ppt-agent.yaml config/ppt-agent.local.yaml
```

将 `config/ppt-agent.local.yaml` 中的 `gateway.mode` 改为 `agent`，并检查生成/检查模型的 `model`、`api_key_env`、`base_url_env`、超时和最大步骤数。

然后在仓库根目录创建 `.env`。变量名必须与 YAML 中的 `api_key_env` 和 `base_url_env` 一致，例如：

```dotenv
ARK_API_KEY=your-api-key
ARK_BASE_URL=https://your-provider.example/v1
```

`.env` 和 `config/*.local.yaml` 已被 Git 忽略。不要把密钥写入 YAML、Issue、日志或提交记录。

启动：

```bash
PPT_AGENT_CONFIG=config/ppt-agent.local.yaml \
python3 scripts/start.py --data .ppt-agent-data --host 127.0.0.1 --port 8000
```

真实模式要求兼容 Responses API 的模型端点。Base URL 必须使用 HTTPS，本机回环调试地址除外。生成与检查推荐使用独立模型配置；如果检查模型回退到生成模型，必须在配置中显式开启。

## 工作流与架构

```text
任务/资料 → 澄清 → 叙事结构 → 逐页大纲 → 样品 → 全稿 → 独立检查 → 交付
   冻结       回答       确认          确认       确认     生成      处置问题      锁定版本
```

核心边界：

```text
Browser UI
   │  REST + SSE
   ▼
FastAPI Web Adapter
   ├── TaskService：阶段、门禁、版本与交付规则
   ├── JobService：长任务、幂等、取消与恢复
   ├── WorkspaceStore：本地原子提交与不可变版本
   └── Agent / Fake Gateways：生成、HTML Builder、独立检查
```

关键目录：

```text
frontend/                 浏览器应用壳、阶段页面和设计系统
ppt_agent/                领域服务、存储、Agent Runtime 与 Web API
ppt_agent/builtin_skills/ 内置 PPT Skill 与锁定资产
config/                   运行配置
schemas/                  持久化与交付 JSON Schema
scripts/                  启动、门禁、导出和离线打包脚本
tests/                    单元、集成、浏览器与端到端测试
docs/                     产品契约、架构决策、Runbook 与验收矩阵
```

任务运行数据默认保存在 `.ppt-agent-data/<task-id>/`。这个目录包含资源、版本、事件、Job 和交付物，不应提交到 Git。

## 测试

运行标准回归：

```bash
python3 -m unittest discover -s tests -v
```

运行固定 Chromium 门禁：

```bash
python3 -m pip install -r requirements-browser.txt
python3 -m playwright install chromium
python3 scripts/verify_browser_gate.py
```

浏览器门禁锁定 Playwright 1.54.0 与 Chromium build v1181。验收时不应把 skipped 当作通过。

其他常用检查：

```bash
python3 scripts/verify_p0.py
python3 scripts/export_schemas.py
```

## 离线交付

最终确认后，交付目录位于：

```text
<data-dir>/<task-id>/deliveries/<delivery-id>/
```

构建并验证离线 ZIP：

```bash
python3 scripts/build_offline_bundle.py \
  .ppt-agent-data/<task-id>/deliveries/<delivery-id>

python3 scripts/verify_offline_delivery.py \
  .ppt-agent-data/<task-id>/deliveries/<delivery-id>.zip
```

把 ZIP 复制到目标机器后建议再次校验，再解压并打开 `index.html`。构建器会拒绝 hash 不匹配、路径穿越、外部 URL 和被篡改的文件。

## 常见问题

### 导入后出现“澄清”，是不是缺少图片？

不一定。进入“澄清”通常表示任务卡缺少以下必填项之一：演示目标、主要受众、核心主题。图片资源是可选的，空资源会弹窗提醒，但不会阻止流程。

### 我放了图片，为什么资源清单还是空的？

依次检查：

1. 路径是否为 `<data-dir>/<task-id>/resources/`，而不是仓库根目录的 `resources/`。
2. 后缀是否受支持，文件是否真实可读且未超过 16 MiB。
3. 图片是否在任务卡导入之后才添加。若是，请显式重建资料快照。
4. 页面资源诊断是否提示图片损坏、空文件或重复内容。

### 导入 Markdown 后三个必填项都被判定缺失

解析器读取“字段名：内容”形式。请直接使用 README 中的任务卡示例，确保目标、受众和主题分别独占一行。普通段落不会自动映射为这三个字段。

### 页面显示诊断 ID 或“请求处理失败”

先保留诊断 ID，不要反复提交同一个生成动作。随后：

1. 确认服务终端仍在运行，访问 `/healthz`。
2. 对浏览器执行一次强制刷新，避免旧静态资源缓存。
3. 查看启动终端中相同诊断 ID 对应的异常。
4. 如果是 `gateway_unknown_result`，先核对模型供应商记录，再决定是否重试，避免重复生成。

### 刷新页面会丢失任务吗？

不会。任务状态、版本和 Job 都保存在数据目录中。浏览器刷新后会从服务端恢复权威状态；运行中 Job 会继续显示进度。

### 可以直接修改交付目录吗？

不建议。交付物与 manifest/hash 绑定，已发布版本应保持不可变。需要修改时，从历史交付派生新候选，再重新检查和确认。

## 更多文档

- [产品行为契约](docs/product-contract.md)
- [本地运行与恢复手册](docs/runbook.md)
- [验收追踪矩阵](docs/acceptance-matrix.md)
- [OpenAPI 契约](docs/openapi.yaml)
- [架构决策记录](docs/adr/README.md)
- [开发计划](docs/development-plan.md)
- [当前能力盘点](docs/current-state.md)

## 项目状态

这是一个面向本地运行和工程验证的 MVP。它已经覆盖完整工作流、版本化、人工门禁、恢复、检查和离线交付，但仍应在真实业务部署前补齐组织级身份认证、权限管理、集中式可观测性、备份策略和容量规划。

欢迎通过 Issue 提交可复现问题。建议附上运行方式、Python/浏览器版本、诊断 ID、最小任务卡以及脱敏后的任务目录结构；请勿上传密钥或未脱敏客户资料。
