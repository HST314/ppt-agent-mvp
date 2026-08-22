# Guizang PPT Generation Contract v1

本文件是 `sample` 与 `deck` 阶段唯一、锁定且必须读取的生成契约。它把
`SKILL.md`、`references/themes.md`、`references/layouts.md`、
`references/components.md`、`assets/template.html` 与动效规则中生成单页所需的
约束合并为一个最小但完整的 bundle。公共 HTML 外壳由服务端组装；模型只生成
本次请求指定的 `section.slide`。

规则优先级从高到低为：事实边界与安全约束、输出契约、用户已确认的设计 brief、
本文件的设计系统、单页美化偏好。低优先级规则不得覆盖高优先级规则。

## 1. 不可违反的事实边界

- 大纲与冻结任务资料是唯一事实来源。不得补造数字、日期、实体、部门、预算、
  SLA、KPI、倍数、效果、承诺、截止时间或已完成事项。
- 不得把计划误写为已发生事实。例如“未来 12 周计划”不能改写成“已完成 12 周
  试点”。不得自行生成 `SLA 99.5%`、`3×`、`100 万+`、`3 个工作日内` 等值。
- 缺失的事实直接删去；确需展示缺口时只能写“数据待确认”或“日期待确认”，并
  降低视觉权重。不得输出 `XX`、`XXX`、`TBD`、`TODO`、`[必填]`、
  `{{变量}}`、`X 月 X 日`、默认日期、默认部门或空白下划线。
- 只能使用输入中已授权的文字和 `resources://` 资源。不得生成新的资源 URI，
  不得引用远程 URL，不得虚构来源或引用。
- 当大纲中的数字和冻结资料冲突时，保留冻结资料中的值；无法判断时删除该数字，
  不要选择看起来更合理的值。

提交前逐一检查所有可见文本：每个数字、日期、专名与承诺是否能在输入中逐字找到
依据；每个模板字段是否都已替换。任一答案为否，必须先修正再返回 JSON。

## 2. 固定输出契约

- 只生成请求中列出的 `slide_id`，顺序必须完全一致，不扩展到全稿。
- 每项 `html` 必须且只能包含一个完整的 `<section>`；不得输出 Markdown 围栏、
  `html`、`head`、`body`、`style`、`script`、iframe、事件属性或说明文字。
- 根节点固定写为：
  `<section class="slide light|dark [hero]" id="slide-N" data-slide-id="slide-N" data-layout="允许的布局 ID" data-animate="允许的动效 recipe">`。
- `id` 与 `data-slide-id` 必须和对应 JSON 项的 `slide_id` 完全相同。
- 页面固定为 1280×720。公共外壳已注入锁定 `assets/template.html` 的唯一 style
  块与服务端画布覆盖，不得再造公共主题或依赖模板脚本。
- 标题使用 `h1`/`h2` 且带 `data-element-id="title"`；主要内容容器带
  `data-element-id="body"`。同一页所有 `data-element-id` 唯一。

## 3. 每批次先冻结微型 DesignContract

在脑中先完成下列决策，再写任何 HTML。不要把决策过程或额外字段输出到 JSON：

1. 从冻结 DesignContract 读取每页的主题 class、明暗节奏、正式程度和信息密度；
   用户颜色偏好不得越过服务端已冻结的主题决定。
2. 整批只消费锁定模板 palette。不得在根 section 或任何后代的 inline style 中
   声明、重定义或覆盖主题 CSS 变量；只能引用模板已有的 `var(--*)`。
3. 为每页选择一个 `data-layout`，相邻页面避免重复；封面和信息密度最高页必须
   使用不同 archetype。
4. 为每页选择一个 `data-animate` recipe，并给 2–8 个叶子内容节点添加
   `data-anim`；只有用户明确要求静态时才允许 `data-animate="none"` 且不加标记。
5. 样稿必须代表整稿：优先覆盖封面/hero 与信息密度最高的数据、流程或决策页，
   不能只做两个普通卡片页。

允许的主题写法：

```html
<!-- 只使用 DesignContract 指定的 light/dark/accent/grey 等主题 class；不声明主题变量 -->
<section class="slide dark" ...>
```

颜色约束：

- 以下锁定主题变量在所有 inline style 中均禁止声明：`--accent`、
  `--accent-rgb`、`--accent-on`、`--accent-bright`、`--ink`、`--ink-rgb`、
  `--ink-tint`、`--paper`、`--paper-rgb`、`--paper-tint`、`--grey-1`、
  `--grey-2`、`--grey-3`、`--text-primary`、`--text-secondary`、
  `--text-helper`、`--text-placeholder`、`--text-on-color`、`--border-subtle`、
  `--border-strong`、`--sans`、`--sans-zh`、`--serif-en`、`--serif-body-en`、
  `--serif-zh`、`--mono`。布局局部变量（例如 `--cols`）仍可使用。
- 根 section 之外禁止出现裸 hex、`rgb()` 或 `hsl()`；内容强调只能引用
  `var(--ink)`、`var(--paper)`、`var(--accent)`、`currentColor` 或带透明度的
  `rgba(var(--ink-rgb),A)` / `rgba(var(--paper-rgb),A)`。
- 同一批次不得改写 token。封面、章节页、结论页可用 `dark`，正文页用
  `light`；除非用户明确要求全浅色，否则 5 页以上整稿至少有一页深色 hero。
- 颜色不能作为唯一状态信号；风险、完成、待定还要有文字标签或形状差异。

## 4. 锁定模板 API

只使用以下已经存在于服务端锁定模板中的类；不得假设其他公共类存在。

### 排版

- 大标题：`h-hero`（封面短标题）、`h-xl`（页面主标题）、`h-sub`、`h-md`。
- 引导文：`lead`。小标签：`kicker`、`meta`、`meta-row`。正文可用
  `body-zh`、`body-serif`。数字可用 `big-num`、`mid-num`。
- 中文大标题优先衬线类，正文优先非衬线类；同页最多两种字重、三层字号。
- 标题建议 40–72px 等效范围，正文 20–30px，辅助文字不得低于 16px。
  不得为塞入内容而把正文压到 16px 以下。

### 容器与网格

- 主容器：`frame`、`col`、`row`、`fill`、`center`。
- 两栏：`split`、`split-55`、`grid-2-7-5`、`grid-2-6-6`、
  `grid-2-8-4`。
- 网格：`grid-3`、`grid-4`、`grid-6`、`grid-9`、`grid-3-3`。
- 分隔与页脚：`rule`、`chrome`、`foot`、`tag`。
- 只在模板类无法表达局部尺寸时使用 inline style；它只能包含布局尺寸、间距、
  对齐、透明度或 `var(...)` 色值。禁止为每个节点重复写字体、颜色、圆角和阴影。

### 组件

- 数据卡固定结构：`stat-card > stat-label + stat-nb + stat-note`，单位放在
  `stat-unit`。只展示冻结资料中存在的指标。
- 流程固定结构：`pipeline-section > pipeline > step`，步骤内使用 `step-nb`、
  `step-title`、`step-desc`。`pipeline` 的 `data-cols` 只能是 3、4、5 或 6。
- 对比/清单可使用 `rowline`，内部使用 `k`、`v`、`m`；不要用大量无边界卡片。
- 观点/来源可使用 `callout`、`q-big`、`callout-src`。没有真实来源时不得生成
  `callout-src` 或伪造引号。
- 图片必须来自本次 `assets` 中已授权的 data URI，放在 `frame-img` 内，填写
  非空 `alt`。照片用 `r-16x10` / `r-4x3`；截图和信息图加 `fit-contain`；
  同组图片用 `h-16` / `h-18` / `h-22` / `h-26` / `h-28` 固定同高。
- 没有授权图片时，使用 CSS 线条、数字、流程或原生 SVG；不要输出虚线图片占位框，
  不要用 emoji 代替图标。

## 5. 允许的布局 archetype

每页 `data-layout` 必须从下表选择。布局名称是交付审计的一部分，不得省略或自造。

| layout ID | 用途 | DOM 骨架 | 密度上限 |
| --- | --- | --- | --- |
| `cover-hero` | 封面、开场 | hero + kicker + h-hero + lead + meta-row | 标题 1、说明 1、元信息 3 |
| `section-hero` | 章节、核心判断 | center/bottom-left + h-xl/h-hero + lead | 3 个文本块 |
| `metrics-grid` | KPI、预算、结果 | h-xl + grid-3/grid-4 + stat-card | 3–4 个指标 |
| `split-evidence` | 图文、证据、方案 | h-xl + grid-2-* / split-55 | 每栏 3–4 要点 |
| `comparison` | 前后、方案对比 | h-xl + split / rowline | 2 列或 4 行 |
| `pipeline` | 流程、时间线 | h-xl + pipeline | 3–6 步 |
| `decision` | 风险、申请、下一步 | h-xl + grid-3 / rowline + callout | 3–5 项 |
| `quote` | 金句、单一结论 | callout/q-big 或大标题 | 1 个主观点 |

选择规则：

- 封面必须用 `cover-hero`，不能用通用卡片墙。
- 只有真实的 3–4 个数值指标才可用 `metrics-grid`；没有数字时改用
  `split-evidence` 或 `decision`，不得制造数字补齐卡片。
- 时间顺序或阶段关系使用 `pipeline`；不要用 5 个互不关联的卡片伪装流程。
- 连续两页不得同时使用同一个 layout；整稿 5 页以上至少使用 3 种 layout。
- 内容放不下时先缩短文案或减少卡片，不得增加滚动、隐藏溢出内容或缩小到不可读。

## 6. 动效 contract

允许的 `data-animate`：`cascade`、`hero`、`quote`、`directional`、`pipeline`、
`none`。

- `cascade`：默认页面；2–8 个叶子节点写 `data-anim`。
- `hero`：封面/章节页；kicker、标题、lead、meta 分别写 `data-anim`。
- `quote`：金句页；每行可写 `data-anim="line"`。
- `directional`：左右对比；左列写 `data-anim="left"`，右列写
  `data-anim="right"`，分隔线写普通 `data-anim`。
- `pipeline`：根容器同时写 `data-animate="pipeline"`；每个 `.step` 写
  `data-anim="step"`。
- `data-anim` 应标在标题、卡片、步骤、图形等叶子内容节点，不要只标整个大容器，
  也不要给同一节点嵌套多层重复标记。
- 动效失败时内容仍须可见且版面完整；不得依靠 transform 把静态内容移出画布。

## 7. 合格骨架示例

示例只演示结构，方括号内容必须换成大纲中的真实文本，不能原样保留。

```html
<section class="slide dark hero" id="slide-1" data-slide-id="slide-1" data-layout="cover-hero" data-animate="hero">
  <div class="kicker" data-anim>[真实场景标签]</div>
  <div class="frame" style="justify-content:center;max-width:72vw">
    <h1 class="h-hero" data-element-id="title" data-anim>[真实标题]</h1>
    <p class="lead" data-element-id="body" data-anim>[真实副标题]</p>
  </div>
  <div class="meta-row" data-anim><span>[真实受众或范围]</span></div>
</section>
```

```html
<section class="slide light" id="slide-2" data-slide-id="slide-2" data-layout="metrics-grid" data-animate="cascade">
  <div class="kicker" data-anim>RESULTS</div>
  <h2 class="h-xl" data-element-id="title" data-anim>[由真实结论组成的标题]</h2>
  <div class="frame grid-3" data-element-id="body">
    <div class="stat-card" data-anim><div class="stat-label">[指标名]</div><div class="stat-nb">[已确认数值]</div><div class="stat-note">[依据输入的解释]</div></div>
    <div class="stat-card" data-anim><div class="stat-label">[指标名]</div><div class="stat-nb">[已确认数值]</div><div class="stat-note">[依据输入的解释]</div></div>
    <div class="stat-card" data-anim><div class="stat-label">[指标名]</div><div class="stat-nb">[已确认数值]</div><div class="stat-note">[依据输入的解释]</div></div>
  </div>
</section>
```

## 8. 返回前的确定性自检

必须逐页检查，任一项失败就先修正：

1. 页面数量、顺序、`slide_id`、根 section 与请求完全一致。
2. 每页都有合法 `data-layout`；相邻页不重复，5 页以上至少 3 种 layout。
3. 每页有合法 `data-animate`；除显式静态页外有 2–8 个叶子 `data-anim`。
4. 所有数字、日期、实体和承诺都来自冻结输入；没有占位符、默认字段或虚构来源。
5. 未声明任何锁定主题变量；内容节点无裸颜色；明暗节奏符合 DesignContract。
6. 标题、正文、卡片、图形均位于 1280×720 内；没有裁切、重叠或滚动。
7. 正文不低于 16px，长文已缩写为短句，每页只有一个视觉焦点。
8. 图片只来自授权 assets，`alt` 非空；没有图片时没有空占位框。
9. 未输出公共外壳、style/script、远程 URL、额外页面或解释文字。

全部通过后，直接提交符合阶段 JSON Schema 的最终结果，不再请求其他文件。
