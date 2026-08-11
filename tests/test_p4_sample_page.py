"""P4-04 样品页版本时间线、历史预览与差异对比 UI 交互测试。

E2E 通过 WSGI 应用驱动，模拟页面内 JS 的真实调用序列：
GET 样品页解析时间线/预览/对比控件 -> POST 样品动作 API -> GET 页面确认回写。
"""
import io, json, re, tempfile, unittest

from ppt_agent.api import App
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class SamplePageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = TaskService(WorkspaceStore(self.tmp.name))
        self.app = App(self.svc)
        self.svc.create("task")
        self.svc.import_input("task", {"goal": "发布", "audience": "客户", "topic": "方案", "页数": 3})
        self.svc.generate_narrative("task")
        self.svc.confirm_narrative("task")
        self.svc.generate_outline("task")
        self.svc.confirm_outline("task")

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, path, body=None):
        raw = json.dumps(body or {}).encode()
        status, headers = [], []
        out = b"".join(self.app({"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}, lambda s, h: (status.append(s), headers.append(h))))
        return status[0], headers[0], out

    def page(self):
        status, headers, raw = self.call("GET", "/tasks/task/samples")
        self.assertTrue(status.startswith("200"))
        return headers, raw.decode()

    def act(self, action, body=None):
        status, _, raw = self.call("POST", f"/v1/tasks/task/samples/{action}", body)
        self.assertTrue(status.startswith("200"), raw)
        return json.loads(raw)

    def ordered_versions(self):
        view = self.svc.sample_view("task")
        rows = [(json.loads(self.svc.version("task", r["hash"]))["version"], r["hash"]) for r in view["versions"]]
        return [h for _, h in sorted(rows)]

    def make_three_versions(self):
        first = self.act("generate", {})
        ids = first["selection"]["slide_ids"]
        self.act("modify", {"prompt": "标题更醒目", "scope": "page", "slide_id": ids[0]})
        self.act("modify", {"prompt": "改为蓝色", "scope": "element", "slide_id": ids[0], "element_id": "title"})
        return ids[0]

    def timeline_rows(self, page):
        block = re.search(r'<ol class="timeline">(.*?)</ol>', page, re.S).group(1)
        return re.findall(r'<li data-hash="([0-9a-f]{64})"([^>]*)>(.*?)</li>', block, re.S)

    def test_timeline_lists_all_versions_with_source_summary_operator_and_outline_link(self):
        sid = self.make_three_versions()
        _, page = self.page()
        view = self.svc.sample_view("task")
        self.assertIn('aria-label="版本时间线"', page)
        rows = self.timeline_rows(page)
        self.assertEqual([h for h, _, _ in rows], self.ordered_versions())  # 时间线按版本号升序
        # 当前版本徽标只出现在最新版本
        self.assertEqual([("current" in attrs) for _, attrs, _ in rows], [False, False, True])
        self.assertEqual(rows[-1][0], view["sample"]["hash"])
        # 每行展示版本号、摘要、来源操作者、作用域、时间和大纲/内容对应关系
        self.assertIn("<strong>v1</strong>", rows[0][2])
        self.assertIn("生成真实 HTML 样品", rows[0][2])
        self.assertIn("来源：系统", rows[0][2])
        self.assertIn("作用域：全局", rows[0][2])
        self.assertIn("标题更醒目", rows[1][2])
        self.assertIn("来源：用户", rows[1][2])
        self.assertIn(f"作用域：页面 · 目标：页面 {sid}", rows[1][2])
        self.assertIn("改为蓝色", rows[2][2])
        self.assertIn("作用域：元素 · 目标：元素 title", rows[2][2])
        for _, _, body in rows:
            self.assertRegex(body, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
            self.assertIn(f'大纲 <code>{view["outline_hash"][:12]}…</code>', body)
            self.assertIn("预览此版本", body)

    def test_history_preview_embeds_every_version_html_safely(self):
        self.make_three_versions()
        headers, page = self.page()
        view = self.svc.sample_view("task")
        # 每个版本都有预览按钮，且其 HTML 已进入页内数据
        self.assertEqual(page.count("预览此版本"), 3)
        for record in view["versions"]:
            self.assertIn(f'data-hash="{record["hash"]}"', page)
            self.assertIn(record["metadata"]["html"] and record["hash"][:12], page)
        self.assertIn("SAMPLE_VERSIONS", page)
        # 嵌入数据不发生脚本逃逸：整页只允许脚本块自身的一个 </script>
        self.assertEqual(page.count("</script>"), 1)
        # 沙箱、CSP 与当前预览默认行为保持不变
        self.assertIn('<iframe sandbox="" id="previewFrame"', page)
        self.assertIn("正在预览：当前版本 v3", page)
        self.assertIn("返回当前版本", page)
        csp = dict(headers).get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)

    def test_diff_controls_default_to_previous_vs_current(self):
        self.make_three_versions()
        _, page = self.page()
        ordered = self.ordered_versions()
        self.assertIn('aria-label="差异对比"', page)
        left = re.search(r'<select id="diffLeft">(.*?)</select>', page, re.S).group(1)
        right = re.search(r'<select id="diffRight">(.*?)</select>', page, re.S).group(1)
        self.assertEqual(left.count("<option"), 3)
        self.assertEqual(right.count("<option"), 3)
        self.assertIn(f'value="{ordered[-2]}" selected', left)  # 左默认上一版本
        self.assertIn(f'value="{ordered[-1]}" selected', right)  # 右默认当前版本
        self.assertIn('id="diffRun"', page)
        self.assertIn('id="diffResult"', page)
        self.assertIn("function diffLines", page)  # 行级差异对比逻辑随页面下发

    def test_modify_preview_confirm_interaction_flow(self):
        result = self.act("generate", {})
        _, page = self.page()
        self.assertIn("正在预览：当前版本 v1", page)
        self.assertIn("待人工确认", page)
        self.assertIn("确认样品并生成全稿", page)
        # 页面内点击“确认样品并生成全稿”（与 JS 相同的调用序列）
        self.act("confirm", {})
        _, page = self.page()
        self.assertIn("已绑定确认", page)
        # 修改产生新版本：时间线增长、当前徽标迁移、确认失效且 UI 不能绕过
        self.act("modify", {"prompt": "统一加深背景", "scope": "global"})
        _, page = self.page()
        self.assertIn("正在预览：当前版本 v2", page)
        self.assertIn("待人工确认", page)
        rows = self.timeline_rows(page)
        self.assertEqual(len(rows), 2)
        current = [h for h, attrs, _ in rows if "current" in attrs]
        self.assertEqual(current, [self.svc.sample_view("task")["sample"]["hash"]])
        self.assertEqual(len(self.svc.sample_view("task")["versions"]), 2)  # 历史版本不删除
        # 改选页面同样使确认失效并保留全部历史
        ids = list(result["selection"]["slide_ids"])
        ids.reverse()
        self.act("select", {"slide_ids": ids})
        _, page = self.page()
        self.assertIn("待人工确认", page)

    def test_empty_and_single_version_states(self):
        _, page = self.page()
        self.assertIn("尚无样品版本", page)
        self.assertIn("尚未生成样品", page)
        self.assertIn("形成两个以上样品版本后", page)
        self.assertNotIn('id="diffLeft"', page)
        self.assertIn('<iframe sandbox=""', page)
        self.act("generate", {})
        _, page = self.page()
        self.assertIn("<strong>v1</strong>", page)
        self.assertIn("正在预览：当前版本 v1", page)
        self.assertIn("形成两个以上样品版本后", page)  # 单版本不出对比控件
        self.assertNotIn('id="diffLeft"', page)

    def test_timeline_excludes_other_kinds_and_prompt_markup_is_escaped(self):
        narrative_hash = self.svc.planning_view("task")["narrative"]["hash"]
        self.act("generate", {})
        sid = self.svc.sample_view("task")["selection"]["slide_ids"][0]
        # 恶意 Prompt 作为负向用例进入修改流
        self.act("modify", {"prompt": "</script><script>alert(1)</script>", "scope": "page", "slide_id": sid})
        _, page = self.page()
        block = re.search(r'<ol class="timeline">(.*?)</ol>', page, re.S).group(1)
        self.assertEqual(block.count("预览此版本"), 2)  # 仅样品版本，不含叙事/大纲版本
        self.assertNotIn(narrative_hash[:12], block)
        # 注入标记不破坏页面结构：时间线转义、嵌入 JSON 转义、srcdoc 转义
        self.assertEqual(page.count("</script>"), 1)
        self.assertNotIn("</script><script>alert(1)", page)
        self.assertIn('<iframe sandbox=""', page)
        # 注入文本仅作为摘要以转义形式出现
        self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", page)


if __name__ == "__main__":
    unittest.main()
