# P2 视觉质量与离线性能门禁

## 视觉质量证据

独立检查在锁定 Chromium 的 1280×720、`reduced-motion` 环境中为每页生成 WebP 截图。截图使用 SHA-256 内容寻址，逐页绑定 deck hash、slide id、媒体类型和字节数；检查报告、终稿、离线包均会复算。截图缺失、篡改或顺序错配会使检查 evidence 失效，并在写入交付事实前失败。

视觉评分为 0–100 分的辅助 QA，不替代 DesignContract、Claim Ledger、overflow 等硬门禁：

- 65% 逐页构图：有效视觉内容覆盖率与视觉重心偏移。
- 20% 布局多样性：按元素角色和空间分桶生成构图签名。
- 15% 主题节奏：整稿可辨识的 light / grey / dark / accent 变化。

`excessive_whitespace`、`visual_imbalance`、`repetitive_layout`、`flat_theme_rhythm` 只产生 warning；现有 blocker 与发布语义不变。

## 离线性能预算

交付包生成 `offline-performance.json`，并在发布前校验：

- Motion runtime 引用不超过 1 次；offline player 引用恰好 1 次。
- offline player JavaScript 不超过 16 KiB（包含后续加入的演示动效、索引视图与低功耗模式）。
- 离线 runtime JavaScript 不超过 80 KiB。
- `index.html + runtime JavaScript` 不超过 256 KiB。
- 翻页仅更新前一页和当前页，状态变更复杂度为 O(1)。
- resize 使用 `requestAnimationFrame` 合并，页面固有尺寸按 slide 缓存。

运行 `scripts/verify_p2_visual_offline.py` 可输出锁定浏览器版本、视觉评分、截图体积、静态预算和 400 次翻页基准的机器可读 JSON。
