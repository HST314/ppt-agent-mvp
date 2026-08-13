# PPT Agent MVP 前端重构任务书与实施契约

> 文档状态：实施基线（已确认）
> 适用仓库：`ppt-agent-mvp`
> 基线版本：PPT Agent `09141afcbfb2fd0309eb8fc7085dff349b903063`
> 视觉参照：Image Agent `61d6e7504f0649e0e47557ac03c7ee8d3daa22a4`
> 文档日期：2026-08-13
> 目标读者：产品负责人、前端工程师、后端工程师、测试/验收人员
> 优先级：本契约约束本次前端与 FastAPI 改造；若与既有产品流程契约冲突，以 `docs/product-contract.md` 的业务门禁、产物和安全边界为准。

## 1. 任务概述

当前 PPT Agent 已具备从任务创建、资料导入、澄清、叙事、大纲、样品、全稿、独立检查到交付的 P0～P8 业务闭环，但界面仍由 `ppt_agent/api.py` 直接拼接多份 HTML/CSS/JS，页面彼此分散，缺少统一应用壳、组件规范、静态资源目录和适合长任务的恢复机制。

本任务在不重写业务内核的前提下，将 Web 层改造成与 Image Agent 设计语言一致、适合 PPT 高信息密度工作流的 FastAPI 应用：

- 后端采用 FastAPI 薄适配层，复用现有 `TaskService`、`WorkspaceStore`、状态机、Gateway、Skill、版本与交付实现；
- 前端采用独立 `frontend/` 目录、原生 HTML/CSS、模块化 ES modules，不引入 Node 构建链；
- 使用统一应用壳承载最近任务、8 阶段导航、阶段工作台、状态与设置；
- 继承 Image Agent 的品牌色、排版气质、组件规则和反馈语言，但为 PPT 的大纲编辑、固定比例画布、版本对比、检查与交付设计专属布局；
- 所有生成类长任务通过持久化后台 Job 执行，并通过 SSE 汇报真实进度、支持断线续传与轮询降级；
- 分两步交付：先建立 FastAPI 壳、设计系统、导航与通用状态，再迁移全部阶段交互。

## 2. 已确认决策

| 决策项 | 已确认方案 | 约束 |
| --- | --- | --- |
| 前端技术栈 | FastAPI + 原生 HTML/CSS + ES modules | 不新增 React/Vue/Vite/Tailwind 等运行或构建依赖 |
| FastAPI 迁移 | 保留现有业务内核和 `/v1` 契约，新增薄适配层；验收后替代旧 WSGI 入口 | Web 框架不得承载业务规则，不允许复制一份 `TaskService` 逻辑 |
| 视觉一致性 | 继承 Image Agent 的设计语言、导航骨架、组件和反馈规范 | 不像素级复制图片画廊；PPT 工作区采用专属高密度布局 |
| 信息架构 | 单一应用壳：左侧最近任务与 8 阶段，中央阶段工作台，顶部工作区/状态/设置 | 阶段状态由后端决定，前端不得伪造推进结果 |
| 交付节奏 | 两步交付 | 第一步完成基础设施与壳；第二步完成全部业务交互迁移 |
| 长任务 | 后台 Job + SSE + 恢复机制 | SSE 断线后按序号续传；不可用时轮询；不得因刷新重复执行 |

以上决策视为实现输入，不在开发过程中重新开放技术选型。确需变更时按第 18 节执行。

## 3. 目标与成功标准

### 3.1 产品目标

1. 用户从创建任务到确认交付始终处于同一个可预测的工作区中。
2. 用户可以立即理解当前任务处于哪个阶段、为何等待、下一步是什么、长任务是否仍在运行。
3. 刷新、临时断网、SSE 断开或服务重启后，用户可以恢复到后端真实状态，不产生重复生成。
4. 已有 P0～P8 业务能力、人工门禁、版本绑定、检查隔离和离线交付不因界面重构而退化。
5. 页面视觉与 Image Agent 属于同一产品家族，同时满足 PPT 编辑工作区对信息密度和预览空间的要求。

### 3.2 可量化成功标准

- 既有 Python 单元/E2E 测试全部通过；固定 Chromium 门禁不得跳过；
- 旧 `/v1` API 的成功状态码、错误包络和业务语义保持兼容；
- AC-18 桌面完整旅程改为统一应用壳后仍可从创建任务走到交付派生；
- 长任务页面刷新后能在 2 秒内恢复活动 Job 的状态展示；
- SSE 重连不丢事件、不重复应用事件，终态回调在前端恰好一次；
- 375、768、1024、1440 px 四个视口无页面级意外横向滚动；演示画布自身缩放不计入页面横向滚动；
- 所有核心操作可用键盘完成，焦点顺序与视觉顺序一致；
- 正文和控件文字对比度达到 WCAG 2.1 AA 的 4.5:1，焦点指示清晰可见；
- 所有动态状态均有可访问文本，不能只用颜色、动画或图标表达；
- 普通切换反馈在 100 ms 内出现；预计超过 1 秒的动作显示进行状态，超过 10 秒显示阶段进度和恢复说明。

## 4. 范围

### 4.1 本期范围

- FastAPI 应用工厂、依赖注入、错误映射、静态资源和 SPA/应用壳入口；
- 当前 `/healthz`、`/v1/tasks/**` 全部接口的 FastAPI 适配；
- 新增长任务 Job、SSE 和轮询恢复接口；
- 首页、任务创建、最近任务、统一任务工作区、8 阶段导航和设置入口；
- 任务/资料、澄清、叙事结构、逐页大纲、样品、全稿、检查、交付全部现有交互；
- 版本历史、版本对比、回退、问题处置、模式切换、暂停/恢复/取消；
- 空、加载、进行、等待人工、成功、失败、过期、无权限/不存在、断线等通用状态；
- 深浅色、响应式、键盘、屏幕阅读器基本语义、减少动态效果；
- API、前端模块、集成、浏览器、恢复和无障碍自动化测试；
- README、OpenAPI、运行手册和验收矩阵同步更新。

### 4.2 非目标

- 不重写 `TaskService`、`WorkspaceStore`、FSM、Gateway、Skill 或离线交付器；
- 不改变任务卡、资源冻结、版本 hash、样品确认、检查隔离、阻断问题和最终交付门禁；
- 不新增 React/Vue、前端包管理器或 bundler；
- 不实现 PDF/PPTX 导出、多人协同、云端账号、权限系统或统一主 Agent 看板；
- 不要求移动端具备与桌面端完全相同的高密度编辑效率，但窄屏必须可查看、可导航、可完成关键确认且无页面破版；
- 不对生成内容的 HTML 视觉质量算法做本期外的重构；
- 不像素级复制 Image Agent 的图片画廊、圈画和图像特有功能。

## 5. 不可破坏的业务契约

本次改造必须保持以下事实，任何 UI 便利性不得绕过它们：

1. 阶段枚举固定为 `created → clarification → narrative → outline → sample → deck → review → delivery`。
2. 运行状态来自后端：`ready`、`running`、`waiting_for_user`、`paused`、`cancelled`、`failed`、`completed`。
3. manual 模式的叙事确认、样品确认和最终交付必须由用户完成；auto 模式也不能跳过样品确认、阻断问题豁免和最终交付。
4. 当前版本改变后，所有绑定旧 hash 的确认、检查报告或交付资格必须按现有服务规则失效。
5. 输入首次导入即冻结；只有大纲前可显式重建，目录变化不得静默进入当前 manifest。
6. 用户直接编辑的叙事或大纲是新的权威版本，前端不得以本地草稿覆盖服务端新版本。
7. 样品和全稿预览继续使用安全沙箱；未经授权的外链、data URL、跨任务资源和主动内容继续被拒绝。
8. 检查模型与生成模型继续隔离；前端不得把生成对话或自述补入检查输入。
9. 只有用户提交当前候选 `deck_hash` 且通过交付门禁后，任务才可进入 `completed`。
10. 已交付版本不可变；从历史交付派生必须创建新候选并重新检查、确认。
11. 所有写操作仍应遵守当前的原子提交和失败不发布原则；Job 只改变调度方式，不改变事务语义。
12. UI 中的进度、阶段、版本、问题数量和完成状态必须来自后端记录，不允许用定时器制造假进度或提前乐观推进。

## 6. 目标架构

### 6.1 分层

```text
Browser
  ├─ App Shell / ES modules / CSS tokens
  ├─ REST client
  └─ Job tracker (SSE → polling fallback)
          │
FastAPI Web Adapter
  ├─ page/static routes
  ├─ existing /v1 compatibility routes
  ├─ job routes and SSE stream
  ├─ request/response/error mapping
  └─ dependency injection
          │
Existing Domain Layer (unchanged authority)
  ├─ TaskService
  ├─ FSM and gates
  ├─ WorkspaceStore / versions / events
  ├─ generation + inspection gateways
  └─ delivery builder / verifier
```

### 6.2 代码组织契约

允许在实施时微调文件名，但职责边界不得混合：

```text
ppt_agent/
  web/
    app.py                 # create_app，FastAPI 生命周期和依赖装配
    dependencies.py        # TaskService、JobService 等依赖
    errors.py              # DomainError → HTTP 响应映射
    routes/
      pages.py             # 应用壳和兼容页面入口
      tasks.py             # 既有 /v1/tasks 契约适配
      jobs.py              # Job REST + SSE
    jobs.py                # Job 状态、持久化、执行与恢复
frontend/
  index.html               # 唯一应用壳
  static/
    css/
      tokens.css
      base.css
      layout.css
      components.css
      stages.css
    js/
      app.js
      api.js
      router.js
      store.js
      job-tracker.js
      shell.js
      components/
      stages/
tests/
  web/
  frontend/
  browser/
```

- `routes/**` 只负责协议校验、调用服务、状态码和序列化；不得包含阶段推进规则。
- `frontend/static/js/api.js` 是唯一 HTTP 客户端；错误中文化、超时、JSON 解析和 Job 接口集中处理。
- 阶段模块只渲染传入状态并发起明确动作；不得直接推断或写入全局任务状态。
- DOM 更新必须经过单一当前任务/当前视图世代守卫，迟到响应不得覆盖用户已切换后的页面。
- 不使用隐式 `window` 命名属性；所有元素通过显式查询和事件绑定。

### 6.3 FastAPI 运行契约

- 提供 `create_app(...)`，测试可注入临时 `WorkspaceStore` 和 fake gateways；模块导入不得启动服务或读取密钥。
- 启动参数继续支持数据目录、host、port 和现有配置文件；默认 fake、离线可运行。
- FastAPI 成为验收后的默认入口，旧 WSGI `App` 在兼容期可保留，但不得长期形成两套业务实现。
- `/healthz` 继续返回至少 `status=ok`、`stage=P8`、`runtime_ready=true`；允许增量添加 `web_runtime=fastapi`。
- JSON 请求体上限继续为 2 MiB；任务 ID 继续遵守 OpenAPI 既有格式约束。
- FastAPI/Pydantic 的默认 422 不得直接暴露为另一种公开错误格式，必须映射到第 9 节统一包络。

## 7. 信息架构与路由契约

### 7.1 应用壳

统一应用壳由三部分组成：

1. 顶部栏：产品标识、当前任务名称/ID、manual/auto、运行状态、连接状态、主题和设置入口；
2. 左侧栏：新建任务、最近任务、8 阶段进度；当前阶段标记 `aria-current="step"`，锁定阶段展示前置条件；
3. 中央工作台：按阶段加载业务视图；宽屏允许右侧出现上下文抽屉，但不得形成第二套主导航。

桌面端建议尺寸：顶部栏 56～64 px；左栏 264～296 px；中央区域占剩余宽度。大纲/叙事使用编辑双栏，全稿/样品优先保证固定比例画布，检查阶段使用“问题列表 + 预览联动”。

### 7.2 Canonical URL

| URL | 行为 |
| --- | --- |
| `/` | 应用首页；展示创建任务与最近任务 |
| `/tasks/{task_id}` | 统一任务工作区；默认显示后端当前阶段 |
| `/tasks/{task_id}?stage={stage}` | 深链到已到达或可查看阶段；不可执行的未来阶段显示锁定原因 |
| `/tasks/{task_id}/outline` 等旧页面 URL | 返回同一应用壳并规范化到相应 `stage`，或使用 307/308 重定向；不得返回 404 |

- 浏览器前进/后退必须恢复阶段、选中版本和可编码的筛选状态。
- 未保存草稿离开时必须提示；已提交服务端的版本不重复提示。
- 切换任务时终止当前视图的 fetch/SSE 订阅，但不能取消后端 Job；新任务数据加载完成前不得展示旧任务内容。

### 7.3 阶段工作台

| 后端阶段 | 页面名称 | 核心内容 | 主操作 |
| --- | --- | --- | --- |
| `created` | 任务/资料 | 任务卡录入、模式、格式、资源说明 | 创建或导入任务卡 |
| `clarification` | 澄清 | 阻断/非阻断问题、当前答案、Other | 提交或修改答案 |
| `narrative` | 叙事结构 | Markdown 编辑、生成、版本历史 | 生成/编辑/确认叙事 |
| `outline` | 逐页大纲 | Markdown 编辑、页列表、资源引用、版本 | 生成/编辑/确认大纲 |
| `sample` | 样品 | 推荐页、固定比例预览、Prompt、版本 | 生成/修改/确认样品 |
| `deck` | 全稿 | 页面导航、全稿预览、修改范围、版本对比 | 生成/修改/回退 |
| `review` | 检查 | 问题分组、页面定位、轮次、处置 | 检查/修复/豁免/通过门禁 |
| `delivery` | 交付 | 候选 hash、检查摘要、交付历史 | 确认交付/派生 |

页面显示名称不改变后端枚举。前端不得另造与后端并行的阶段状态机。

## 8. 视觉与交互设计契约

### 8.1 设计语言

采用与 Image Agent 同源的“AI 紫 + 靛蓝 + 生成粉”产品语言，并将密度调高到适合生产力工作台的 8/10：

| Token | 基准值 | 用途 |
| --- | --- | --- |
| `--color-primary` | `#7C3AED` | 品牌、选中、焦点 |
| `--color-on-primary` | `#FFFFFF` | 主色上的文字 |
| `--color-secondary` | `#6366F1` | 次要强调、阶段连接 |
| `--color-accent` | `#EC4899` | 唯一主 CTA 或生成动作 |
| `--color-background` | `#FAF5FF` | 浅色背景 |
| `--color-foreground` | `#0F172A` | 主文字 |
| `--color-muted` | `#F7F3FD` | 次级表面 |
| `--color-border` | `#EFE7FC` | 边界 |
| `--color-destructive` | `#DC2626` | 失败、破坏性操作 |
| `--color-ring` | `#7C3AED` | 键盘焦点 |

- 标题字体延续 Image Agent 的 Space Grotesk 气质，正文字体延续 DM Sans；必须提供中文系统字体和 `system-ui` 回退。
- 字体资源应本地托管或可无网络降级，界面运行不能依赖 Google Fonts 可访问。
- 所有颜色必须通过语义 token 使用，组件中不得散落无含义的原始色值。
- 深色主题使用独立语义 token，不得对浅色主题简单反相；正文和状态色仍满足对比度。
- 图标统一使用同一套 SVG 线性图标，不用 emoji 充当功能图标；纯图标按钮提供 `aria-label` 和 tooltip。

### 8.2 组件规范

- 按钮：主操作每个视图最多一个强色 CTA；危险动作与普通动作分离；禁用态必须同时说明原因。
- 卡片：12 px 圆角、1 px 边框、克制阴影；状态卡不使用会引发布局移动的 hover scale。
- 表单：可见 label、辅助文本和字段就近错误；placeholder 不得代替 label；触控目标至少 44×44 px。
- 状态徽标：图标/文字/颜色三者至少使用两种编码；`running`、`waiting_for_user`、`failed` 不得只靠颜色区分。
- 弹窗：仅用于不可逆确认或需要阻断当前流程的选择；普通详情使用抽屉/展开区。
- Toast：只报告补充性结果；关键错误、门禁和恢复动作必须留在页面内。
- Markdown 编辑器：编辑区与预览/结构区清晰分栏；保存中、已保存、本地修改、版本冲突状态可见。
- 预览 iframe：固定比例、自适应缩放、明确边框和加载骨架；沙箱属性不可由前端状态动态放宽。

### 8.3 动效与反馈

- 常规 hover/focus/展开使用 150～300 ms；长任务进度变化不使用弹跳或装饰性循环动画。
- 仅动画 `transform` 和 `opacity`，避免用 width/height 做连续动画。
- `prefers-reduced-motion: reduce` 时关闭非必要动画和自动滚动。
- 提交后立即锁定重复操作入口并显示真实“正在提交”；收到 Job 后切换到后端状态。
- 不显示超过 10 秒的孤立 spinner；必须显示操作名、当前步骤、已用时间/最新事件和“可安全离开，稍后恢复”的说明。

### 8.4 响应式

- `>= 1024 px`：顶部栏 + 常驻左栏 + 工作台；大纲、检查可双/三栏。
- `768～1023 px`：左栏可折叠；编辑与预览按任务切换；上下文抽屉覆盖显示。
- `< 768 px`：单栏；阶段导航为可展开面板；表格转卡片或局部可滚动；主操作固定但不得遮挡内容。
- 任何视口都不得禁止缩放；固定顶部/底部区域需处理安全区和内容内边距。

## 9. REST 兼容与错误契约

### 9.1 既有 API

- `docs/openapi.yaml` 中现有 `/v1/tasks/**` 路径、请求字段、成功状态码和响应语义保持兼容。
- 同步接口在兼容期继续可用，既有自动化测试和外部调用方无需改造即可运行。
- 新 UI 对会触发模型/Builder/检查器的长操作使用 Job API；读取、确认、问题处置、模式切换等短操作仍可直接使用既有接口。
- Job worker 必须调用与同步路由相同的 `TaskService` 方法，禁止维护第二套生成流程。

### 9.2 统一错误

所有 REST 错误保持：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求字段无效",
    "diagnostic_id": "..."
  }
}
```

- DomainError 状态码保持既有映射：校验/领域错误 400、未找到 404、门禁/冲突 409、Gateway 502、未知结果 503。
- FastAPI 请求校验、Job 校验和内部异常也映射为同一包络，不把 Pydantic 内部结构或 traceback 直接显示给用户。
- 前端显示中文可操作说明，同时在可展开详情中保留 `diagnostic_id` 供排障；不得把英文错误码作为唯一用户文案。
- 失败页面必须给出与状态匹配的恢复动作：修改输入、重新连接、查看任务状态、以新尝试重试或联系维护者。

## 10. 后台 Job 与 SSE 契约

### 10.1 适用操作

至少以下操作必须通过后台 Job 执行：

- `narrative.generate`
- `outline.generate`
- `samples.generate`
- `samples.modify`
- `deck.generate`
- `deck.modify`
- `inspection.run`
- 任何会触发真实模型、HTML Builder、检查器或有界自动修复循环的新增操作

纯读取、人工确认、模式切换、问题处置、版本选择/对比等不强制走 Job。

### 10.2 创建 Job

`POST /v1/tasks/{task_id}/jobs`

```json
{
  "operation": "deck.modify",
  "payload": {
    "prompt": "统一增加留白",
    "change_type": "visual",
    "scope": "global"
  },
  "idempotency_key": "client-generated-opaque-key"
}
```

成功返回 `202 Accepted`：

```json
{
  "job_id": "job_...",
  "task_id": "demo",
  "operation": "deck.modify",
  "status": "queued",
  "progress": 0,
  "current_step": "queued",
  "last_seq": 1,
  "created_at": "2026-08-13T00:00:00Z",
  "started_at": null,
  "finished_at": null,
  "result": null,
  "error": null
}
```

契约要求：

- `idempotency_key` 对同一 `task_id + operation + payload fingerprint` 唯一；相同请求重试返回原 Job，不重复执行。
- 同一 key 携带不同 operation 或 payload 返回 409 `conflict`。
- 创建前校验任务存在、当前状态可执行、操作字段合法；不合法时不创建 Job。
- 每个任务同一时间最多执行一个会写业务状态的 Job；并发写请求返回 409，并返回当前活动 `job_id` 的可恢复信息。
- UI 生成 idempotency key 并按“任务 + 用户意图 + payload fingerprint”保存在 `sessionStorage` 或等效持久位置；只有收到权威成功终态或权威失败/取消终态后才清理。

### 10.3 查询、取消与发现活动 Job

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/jobs/{job_id}` | 获取 Job 权威快照 |
| `GET /v1/tasks/{task_id}/jobs?status=active` | 页面进入/刷新时发现活动 Job |
| `POST /v1/jobs/{job_id}/cancel` | 请求取消尚未进入不可中断提交点的 Job |
| `GET /v1/jobs/{job_id}/events?after={seq}` | SSE 事件流/有限快照流 |

- 取消是请求，不保证底层外部调用可立即终止；服务端必须报告 `cancellation_requested`，并在确认停止后进入 `cancelled`。
- 若业务提交已经原子完成，取消不得回滚已发布版本；Job 应返回 `succeeded` 及真实结果。
- `GET .../jobs?status=active` 至少返回 `queued`、`running`、`cancellation_requested`。

### 10.4 状态机

```text
queued → running → succeeded
                 → failed
                 → cancelled
                 → interrupted
queued ─────────→ cancelled
```

- 终态：`succeeded`、`failed`、`cancelled`、`interrupted`。
- Job 状态与任务 FSM 状态是两套不同但相关的数据：Job 描述一次执行，任务 FSM 描述产品流程；前端不得把 Job `succeeded` 等同于任务 `completed`。
- 服务重启时：已持久化的 `queued` 可安全重新调度；无法证明结果的 `running` 标记为 `interrupted`，不得自动重复产生外部副作用。
- Gateway 返回“结果未知”时 Job 为 `failed` 或 `interrupted`，错误码保持 `gateway_unknown_result`；只有用户明确新尝试才生成新 idempotency key。

### 10.5 SSE

响应类型为 `text/event-stream`。事件至少包含：

```text
id: 7
event: progress
data: {"seq":7,"job_id":"job_...","type":"progress","progress":45,"step":"rendering_slides","message":"正在生成页面 5/12","at":"..."}
```

- `seq` 在单个 Job 内严格递增；客户端按 `after` 或 `Last-Event-ID` 续传。
- 类型至少包括 `queued`、`started`、`progress`、`checkpoint`、`succeeded`、`failed`、`cancelled`、`heartbeat`。
- 进度必须源于真实检查点；无法量化时允许 `progress=null`，但必须给出当前步骤，不得伪造百分比。
- 终态事件后，客户端必须再 `GET /v1/jobs/{job_id}` 获取权威记录和结果，然后只执行一次完成回调。
- SSE 不可用、断线或代理缓冲时，前端每 1.5～3 秒轮询 Job；恢复 SSE 后从最后 `seq` 继续。
- 事件消息不得包含密钥、完整 Prompt、原始客户资料、模型推理或未净化异常。

### 10.6 持久化与保留

- Job 元数据和事件采用当前工作区可原子写入的持久存储，进程内内存只能作为缓存。
- Job 结果引用业务版本/hash，不复制另一份可变业务真相。
- 活动 Job 永不被清理；终态保留期和清理策略写入运行手册，MVP 默认至少保留 7 天。
- 写 Job 事件、写业务版本和发布任务状态的先后顺序必须可恢复；失败不得留下“Job 成功但业务版本不存在”的状态。

## 11. 前端状态与数据一致性契约

### 11.1 单一真相来源

- 任务阶段、状态、等待原因、必需动作：`GET /v1/tasks/{task_id}` 或阶段 view 响应；
- 业务产物与版本：对应 `/input`、`/planning`、`/samples`、`/deck`、`/inspection`、`/delivery`；
- 长操作进度：Job 记录和 Job events；
- 前端 store 只缓存，不成为持久业务真相。

### 11.2 必须覆盖的视图状态

每个阶段模块至少显式处理：

1. `loading`：首屏骨架和可访问状态文本；
2. `empty`：说明缺少什么以及唯一推荐下一步；
3. `ready`：展示可执行主操作；
4. `submitting`：防重复提交但不伪造 Job；
5. `running`：显示 Job 真实步骤、连接方式和离开后可恢复说明；
6. `waiting_for_user`：突出 required action 与门禁原因；
7. `stale`：说明哪个上游版本变化、哪些确认/报告已失效；
8. `failed`：保留最后成功版本，显示恢复动作和 diagnostic id；
9. `paused/cancelled/completed`：只开放状态允许的动作；
10. `not_found/offline`：不残留上一任务内容。

### 11.3 并发和迟到响应

- 每次任务/阶段切换建立新的 AbortController 或世代标识；旧请求返回后必须静默丢弃。
- Job 完成后的刷新必须先验证当前仍是同一任务、同一 Job、同一意图，不能终止或覆盖更新的操作。
- 提交 120 秒超时只表示客户端未获得响应，不代表后端未执行；客户端先用原 idempotency key 对账，不得直接生成新 Job。
- 当任务 `revision` 高于编辑器载入版本时，保存前必须重新读取并提示冲突，不做 last-write-wins 静默覆盖。

## 12. 安全、隐私与性能契约

### 12.1 安全

- 延续现有任务路径、资源 hash、2 MiB 请求体、Skill lock、外部 URL 和交付验证限制。
- 静态资源只从受控目录提供，阻止 `..`、符号链接逃逸和任意文件读取。
- 预览 iframe 使用最小权限 `sandbox`；演示内容不能访问父页面 DOM、同源凭据或导航顶层窗口。
- 用户生成 HTML、Markdown、Prompt、文件名和错误消息进入 DOM 前必须按上下文转义；禁止直接插入未经净化的 `innerHTML`。
- 建议设置 CSP、`X-Content-Type-Options: nosniff`、合理的 `Referrer-Policy` 和 frame 限制；应用自身不得加载不必要的第三方脚本。
- 日志、Job event、URL 和前端存储不得包含 API key、完整客户原文或模型隐藏推理。

### 12.2 性能

- 首屏只加载应用壳和当前阶段模块；其他阶段动态 import。
- 大型版本列表和问题列表采用分页/增量加载，必要时虚拟化；不一次拉取全部 artifact 内容。
- 预览 iframe 延迟加载并预留比例空间，避免 CLS；切换页面时释放无用事件监听和 object URL。
- 目标：应用壳静态资源压缩后初始传输不超过 250 KiB（不含字体和预览内容）；普通任务切换不触发整页 reload。
- 不因 hover、进度更新或轮询产生布局抖动；相同状态不重复渲染整个工作台。

## 13. 分阶段任务书

### 13.1 第一步：FastAPI 壳、设计系统、导航与通用状态

#### F1. FastAPI 基础设施

- 建立应用工厂、依赖注入、生命周期和测试启动方式；
- 适配 `/healthz` 与全部既有 `/v1/tasks/**`；
- 建立 DomainError/请求校验/未知异常统一映射；
- 提供静态资源、应用壳和旧页面 URL 兼容；
- 更新启动脚本和依赖声明。

验收：现有 API/E2E 套件在 FastAPI 入口运行通过；错误包络不变；默认 fake 离线启动成功。

#### F2. 设计系统和组件基础

- 建立浅/深色 token、排版、间距、圆角、阴影和状态色；
- 实现 Button、IconButton、Field、Select、Textarea、Badge、Card、Dialog、Drawer、Toast、InlineError、Skeleton、EmptyState、Progress；
- 实现 focus-visible、reduced-motion 和响应式断点；
- 组件具备可访问名称、禁用原因和键盘行为。

验收：组件演示/测试页覆盖所有状态；无 emoji 功能图标；对比度、键盘和四档视口检查通过。

#### F3. 统一应用壳

- 首页、创建任务、最近任务；
- 顶部状态栏、任务切换、连接状态、模式与主题；
- 左侧 8 阶段、完成/当前/锁定/失效状态；
- URL/History 深链和旧页面兼容；
- 通用 loading/empty/error/offline/not-found 页面。

验收：可创建和切换任务；刷新/前进/后退稳定；阶段与锁定原因来自后端；窄屏无页面级横向滚动。

#### F4. Job/SSE 基础能力

- 实现 Job 持久化、互斥、幂等、恢复、查询、取消和事件；
- 实现 SSE `after`/`Last-Event-ID` 续传、heartbeat 和轮询降级；
- 实现前端 job-tracker、活动 Job 自动发现和终态恰好一次；
- 为第二步阶段模块提供统一调用接口。

验收：重复提交、刷新、断线、SSE 不可用、服务重启、取消竞争和未知结果测试通过。

第一步交付点：用户可在新应用壳中创建/打开任务、观察真实阶段和运行状态；旧业务页面仍可兼容访问。除已迁移视图外，不删除旧交互实现。

### 13.2 第二步：全部阶段交互迁移

#### F5. 任务/资料与澄清

- 迁移 Markdown/JSON 任务卡、资源 manifest、默认值、假设、诊断和重建快照；
- 迁移选择题、Other、自定义回答、改答和下游失效提示；
- 保持输入冻结与大纲后禁止重建。

#### F6. 叙事与大纲

- 双编辑器/分阶段编辑视图、直接编辑、Prompt 生成/修改、确认；
- 版本历史、来源、操作者、摘要、对比、回退；
- 生成走 Job，编辑/确认保持现有门禁；
- 未保存草稿、版本冲突和失效范围可见。

#### F7. 样品

- 样品页推荐与选择、固定比例沙箱预览、Prompt 修改和作用范围理解；
- 当前选择、理解依据、版本历史和对比；
- 生成/修改走 Job；确认绑定当前 outline/selection/sample hash。

#### F8. 全稿

- 页面导航、整稿播放、全局/页面/元素修改、内容/视觉修改；
- 版本时间线、逐页差异、回退和受影响页面提示；
- 生成/修改走 Job；最后成功版本在失败时保持可用。

#### F9. 检查与人工审核

- manual/auto、最大轮数、全检/增量检查；
- 元素/页面/整稿问题分组、严重度、预览联动定位；
- 单项与同代码批量处置、依据填写、过期报告提示；
- 检查和自动修复循环走 Job，达到上限必须等待人工。

#### F10. 交付与派生

- 当前候选 hash、门禁摘要、阻断问题入口、交付确认；
- 不可变交付历史、文件摘要、派生新候选；
- 最终确认继续要求用户动作；完成状态不再展示待办动作。

#### F11. 旧 UI 下线与发布门禁

- 所有阶段达到功能和浏览器测试等价后，删除 `ppt_agent/api.py` 中内联页面实现；
- 旧页面 URL 保留兼容，不保留第二套页面代码；
- 更新 OpenAPI、README、runbook、acceptance matrix；
- 执行全量单元、E2E、固定 Chromium、离线交付和安全回归。

第二步交付点：统一应用壳覆盖创建至交付派生完整旅程，旧 WSGI 不再是默认启动入口，现有业务契约无退化。

## 14. 测试与验收契约

### 14.1 自动化层次

1. 单元测试：路由映射、错误包络、Job 状态机、幂等、事件序号、恢复策略、前端纯函数；
2. API 契约测试：既有 OpenAPI 正反例、同步兼容、新 Job API、SSE 格式；
3. 集成测试：FastAPI + 临时 WorkspaceStore + fake gateways 的全部阶段；
4. 浏览器测试：真实 Chromium 中的统一应用壳、键盘、History、沙箱预览、版本对比、断线恢复；
5. 发布门禁：现有 `scripts/verify_browser_gate.py`、离线 bundle 校验、安全负例和全套 unittest。

### 14.2 必测场景

- 创建 manual/auto 任务，导入完整/缺失任务卡，回答与修改澄清；
- 叙事和大纲直接编辑、生成、确认、回退、修改上游导致下游失效；
- 样品推荐、生成、局部修改、歧义错误、确认后再修改导致确认失效；
- 全稿生成、视觉/内容修改、逐页对比、回退、失败不发布；
- manual 检查、auto 有界修复、轮数上限、报告过期、单项/批量处置；
- 交付门禁、当前 hash 确认、离线包、历史交付派生；
- Job 重复 POST、请求超时后对账、SSE 断线续传、乱序/重复事件忽略、轮询降级；
- 服务在 queued/running/业务提交后分别重启；不能证明结果时不重复外部调用；
- 用户在 Job 运行时切任务、切阶段、刷新、关闭后重开；
- paused/cancelled/failed/completed 不允许启动不合法新动作；
- 任务 ID、Prompt、Markdown、文件名、错误消息含特殊字符时无 XSS；
- 375/768/1024/1440 视口、200% 缩放、深浅色、reduced motion；
- 只用键盘完成创建、澄清、叙事确认、样品确认、问题处置和交付确认。

### 14.3 关键验收编号

| ID | 验收条件 |
| --- | --- |
| FE-AC-01 | FastAPI 默认启动，`/healthz` 和既有 `/v1` 测试通过 |
| FE-AC-02 | 旧 `/tasks/{id}/...` 深链进入统一壳且定位正确阶段 |
| FE-AC-03 | 阶段、等待原因、主操作与 `TaskState` 一致，无前端伪状态 |
| FE-AC-04 | 创建至交付派生完整 Chromium 旅程通过 |
| FE-AC-05 | 相同 idempotency key 的重复长任务只执行一次 |
| FE-AC-06 | SSE 断线后从 seq 续传；轮询降级后结果一致 |
| FE-AC-07 | 刷新或切换视图不会丢失 Job，也不会让迟到响应污染当前视图 |
| FE-AC-08 | 服务重启不自动重放结果未知的外部副作用 |
| FE-AC-09 | 所有 hash 绑定确认和失效规则与改造前一致 |
| FE-AC-10 | iframe 沙箱、资源授权和 XSS 负例全部通过 |
| FE-AC-11 | 键盘、焦点、对比度、状态文本和 reduced-motion 达标 |
| FE-AC-12 | 四档视口可用，无应用级意外横向滚动 |
| FE-AC-13 | 现有单元/E2E/浏览器/离线门禁全部通过且无跳过 |
| FE-AC-14 | 文档、OpenAPI、运行手册与实际实现一致 |

## 15. 交付物

第一步：

- FastAPI 薄适配层与默认启动入口；
- 独立 `frontend/` 和设计 token/基础组件；
- 首页、最近任务、统一应用壳、8 阶段导航和通用状态；
- Job/SSE 后端与前端 tracker；
- 对应测试和文档。

第二步：

- 8 阶段完整交互与所有版本/检查/交付能力；
- 兼容路由和旧内联 UI 下线；
- 更新后的 `docs/openapi.yaml`、README、runbook、acceptance matrix；
- 全量测试报告和固定 Chromium 验收证据。

## 16. Definition of Done

只有同时满足以下条件，任务才能标记完成：

- 本文第 2 节所有确认决策均已实现；
- 第 5 节业务契约无回归；
- 第 14 节 FE-AC-01～14 全部通过；
- 同步 API 兼容期策略、Job 清理策略和故障恢复方法已写入运行手册；
- 代码中不存在两套阶段规则或两套产物写入逻辑；
- 旧内联 UI 已下线，但旧深链仍可访问统一应用壳；
- 默认 fake 模式无需密钥和网络即可运行；
- 用户可在真实 Chromium 中从创建任务走到确认交付并从交付派生；
- 无未解释的测试跳过、浏览器控制台错误、严重无障碍问题或安全高风险项；
- 产品负责人按本文和更新后的验收矩阵完成验收。

## 17. 风险与缓解

| 风险 | 缓解措施 |
| --- | --- |
| FastAPI 适配时改变错误/状态码 | 契约测试固定既有 OpenAPI 正反例，统一异常映射 |
| 同步与 Job 两条路径产生业务分叉 | 两者只调用同一 `TaskService`；不复制领域逻辑 |
| 请求超时或刷新导致重复付费调用 | 强制幂等 key、活动 Job 发现、先对账后重试 |
| 进程重启时长任务结果未知 | 持久化事件；running 标记 interrupted；禁止自动重放未知副作用 |
| 单应用壳状态复杂、迟到响应污染 | AbortController + 世代守卫 + task/revision/job 三重校验 |
| 高密度界面牺牲可访问性 | 语义组件、44 px 目标、键盘旅程、对比度与四视口门禁 |
| 视觉追求与 PPT 预览空间冲突 | 品牌语言复用，布局按阶段专用；预览阶段优先画布 |
| 新 UI 与旧测试耦合 | 先保持 URL/API 兼容，再逐阶段迁移测试，最后统一下线旧实现 |

## 18. 变更控制

下列变更必须先更新本文并获得产品负责人确认，不能由实现者自行决定：

- 引入 Node 构建链或 SPA 框架；
- 改变现有 `/v1` 请求/响应或状态码；
- 改变 8 阶段、manual/auto 或人工门禁；
- 改变输入冻结、版本 hash、检查隔离或交付不可变规则；
- 取消 Job 持久化、SSE 续传、幂等或轮询降级；
- 将旧 WSGI 与 FastAPI 长期并行维护；
- 扩大到 PDF/PPTX、多人协同、权限或统一主 Agent 等非目标。

普通视觉微调、模块文件拆分和不改变公开语义的内部实现，可在满足本文验收条件的前提下由工程团队决定。

## 19. 实施启动条件

本文经确认后即可进入第一步开发，不再需要补充技术选型。实施者开始编码前只需：

1. 将当前基线测试跑通并保存结果；
2. 建立本文 FE-AC 与现有 AC 的追踪矩阵；
3. 先提交 FastAPI 适配和兼容测试，再提交应用壳与 Job/SSE；
4. 每迁移一个阶段即迁移对应浏览器测试，避免第二步末尾一次性重写测试；
5. 在全部阶段验收前保留可回退的旧交互入口，在 F11 一次性收口为统一应用壳。
