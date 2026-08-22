"""Browser regressions for bounded SSE recovery and refresh-safe intents."""

import socket
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import uvicorn
from playwright.sync_api import sync_playwright

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore
from ppt_agent.web import create_app


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BlockingNarrativeService(TaskService):
    def __init__(self, store):
        super().__init__(store)
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate_narrative(self, task_id, prompt=None, scope="all"):
        self.calls += 1
        self.started.set()
        if self.calls == 1:
            self.release.wait(30)
        return super().generate_narrative(task_id, prompt, scope)


class JobRecoveryBrowserGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = BlockingNarrativeService(WorkspaceStore(self.tmp.name))
        self.service.create("recovery")
        self.service.import_input("recovery", {"goal": "发布", "audience": "管理层", "topic": "增长"})
        self.app = create_app(self.service)
        port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.service.release.set()
        self.server.should_exit = True
        self.thread.join(5)
        self.tmp.cleanup()

    def new_page(self, *, fail_first_streams=0, width=1280, height=900):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        script = """
            (() => {
              const failFirst = __FAIL_FIRST__;
              const NativeEventSource = window.EventSource;
              const nativeFetch = window.fetch.bind(window);
              window.__streamUrls = [];
              window.__pollUrls = [];
              const publish = () => {
                document.documentElement?.setAttribute("data-test-stream-count", String(window.__streamUrls.length));
                document.documentElement?.setAttribute("data-test-poll-count", String(window.__pollUrls.length));
              };
              let attempts = 0;
              window.EventSource = function(url, options) {
                attempts += 1;
                window.__streamUrls.push(String(url));
                publish();
                if (attempts <= failFirst) {
                  const fake = {
                    addEventListener() {},
                    close() {},
                    onerror: null,
                    onopen: null,
                  };
                  window.setTimeout(() => fake.onerror?.(new Event("error")), 20);
                  return fake;
                }
                return new NativeEventSource(url, options);
              };
              window.EventSource.prototype = NativeEventSource.prototype;
              window.fetch = (...args) => {
                const target = String(args[0]);
                if (/\/v1\/jobs\/[^/]+$/.test(new URL(target, location.origin).pathname)) {
                  window.__pollUrls.push(target);
                  publish();
                }
                return nativeFetch(...args);
              };
            })();
            """.replace("__FAIL_FIRST__", str(fail_first_streams))
        page.add_init_script(script=script)
        return page

    def new_second_disconnect_page(self):
        page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        page.add_init_script(
            script="""
            (() => {
              const nativeFetch = window.fetch.bind(window);
              window.__streamUrls = [];
              window.__pollUrls = [];
              const publish = () => {
                document.documentElement?.setAttribute("data-test-stream-count", String(window.__streamUrls.length));
                document.documentElement?.setAttribute("data-test-poll-count", String(window.__pollUrls.length));
              };
              window.EventSource = function(url) {
                const attempt = window.__streamUrls.push(String(url));
                publish();
                const fake = {
                  addEventListener() {},
                  close() {},
                  onerror: null,
                  onopen: null,
                };
                if (attempt === 3) {
                  window.setTimeout(() => fake.onopen?.(new Event("open")), 20);
                  window.setTimeout(() => fake.onerror?.(new Event("error")), 80);
                } else {
                  window.setTimeout(() => fake.onerror?.(new Event("error")), 20);
                }
                return fake;
              };
              window.fetch = (...args) => {
                const target = String(args[0]);
                if (/\/v1\/jobs\/[^/]+$/.test(new URL(target, location.origin).pathname)) {
                  window.__pollUrls.push(target);
                  publish();
                }
                return nativeFetch(...args);
              };
            })();
            """
        )
        return page

    def wait_for_session_state(self, page, expected_empty, timeout=6000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            empty = page.locator("html").evaluate(
                "node => !Object.keys(sessionStorage).some(key => key.startsWith('ppt-agent:intent:') || key.startsWith('ppt-agent:job-intent:'))"
            )
            if empty == expected_empty:
                return
            time.sleep(0.05)
        self.fail(f"session intent state did not become empty={expected_empty}")

    def wait_for_job_mapping(self, page, timeout=6000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            present = page.locator("html").evaluate(
                "node => Object.keys(sessionStorage).some(key => key.startsWith('ppt-agent:job-intent:'))"
            )
            if present:
                return
            time.sleep(0.05)
        self.fail("job-to-intent mapping was not persisted")

    def start_first_job(self, page):
        page.goto(self.base + "/tasks/recovery?stage=clarification")
        page.get_by_role("heading", name="澄清", exact=True).wait_for()
        page.get_by_text("授权资源（辅助信息）", exact=True).wait_for()
        page.get_by_role("button", name="生成叙事结构", exact=True).click()
        self.assertTrue(self.service.started.wait(2))

    def wait_for_job_count(self, count, timeout=6000):
        deadline = time.monotonic() + timeout / 1000
        jobs = []
        while time.monotonic() < deadline:
            jobs = self.app.state.job_service.list("recovery")
            if len(jobs) == count and jobs[-1]["status"] in TERMINAL:
                return jobs
            time.sleep(0.05)
        self.fail(f"expected {count} terminal jobs, got {jobs}")

    def test_disconnect_falls_back_to_polling_then_recovers_sse_from_last_seq(self):
        page = self.new_page(fail_first_streams=2)
        self.start_first_job(page)
        page.locator('html[data-test-stream-count="3"][data-test-poll-count]:not([data-test-poll-count="0"])').wait_for(timeout=6000)
        stream_urls = page.evaluate("window.__streamUrls")
        after_values = [int(parse_qs(urlparse(url).query).get("after", ["0"])[0]) for url in stream_urls]
        self.assertGreaterEqual(after_values[-1], 2)
        self.assertEqual(after_values, sorted(after_values))

        self.service.release.set()
        jobs = self.wait_for_job_count(1)
        page.goto(self.base + "/tasks/recovery?stage=narrative")
        page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
        page.close()

    def test_second_disconnect_exhausts_probes_but_polls_until_terminal(self):
        page = self.new_second_disconnect_page()
        self.start_first_job(page)

        # Attempts 1-2 fail, attempt 3 opens and then fails, and attempts 4-5
        # consume the remaining bounded recovery probes.
        page.locator('html[data-test-stream-count="5"]').wait_for(timeout=10000)
        stream_urls = page.evaluate("window.__streamUrls")
        after_values = [int(parse_qs(urlparse(url).query).get("after", ["0"])[0]) for url in stream_urls]
        self.assertEqual(len(stream_urls), 5)
        self.assertEqual(after_values, sorted(after_values))

        polls_before = page.evaluate("window.__pollUrls.length")
        self.assertGreater(polls_before, 0)
        page.wait_for_timeout(1300)
        polls_after = page.evaluate("window.__pollUrls.length")
        self.assertGreater(polls_after, polls_before)
        self.assertEqual(self.app.state.job_service.list("recovery")[-1]["status"], "running")

        # With no SSE source left, the live polling loop must observe terminal
        # state and run the browser completion cleanup.
        self.service.release.set()
        self.wait_for_job_count(1)
        self.wait_for_session_state(page, True)
        page.close()

    def test_job_panel_separates_transport_business_timing_and_cancel_feedback(self):
        page = self.new_page(width=375, height=820)
        page.emulate_media(reduced_motion="reduce", color_scheme="dark")
        page.add_init_script("localStorage.setItem('ppt-agent:theme', 'dark')")
        self.start_first_job(page)
        page.get_by_role("link", name="状态", exact=True).click()
        page.get_by_role("heading", name="运行状态", exact=True).wait_for()
        panel = page.locator(".job-panel")
        panel.wait_for()

        business_step = panel.locator(".job-panel__business-step")
        business_step.wait_for()
        business_before = business_step.inner_text()
        self.assertTrue(business_before)
        self.assertNotIn("进度通道", business_before)
        self.assertTrue(panel.get_by_text("业务进度", exact=True).is_visible())
        panel.get_by_text("进度通道已连接", exact=True).wait_for()
        panel.locator(".job-panel__deadline").filter(has_text="硬截止").wait_for()
        self.assertIn("已用时", panel.inner_text())
        for label in ("Agent 步数", "模型请求", "Skill 工具调用"):
            self.assertIn(label, panel.inner_text())
        panel.locator(".job-execution > summary").click()
        panel.get_by_text("进入队列", exact=True).wait_for()
        panel.get_by_text("开始执行", exact=True).wait_for()

        # Refresh rehydrates the persisted timeline and deduplicates by
        # (job_id, seq), including in dark/reduced-motion mode.
        page.reload()
        panel = page.locator(".job-panel")
        panel.wait_for()
        panel.locator(".job-execution > summary").click()
        panel.get_by_text("进入队列", exact=True).wait_for()
        sequence_labels = panel.locator(".job-timeline__item small").all_inner_texts()
        self.assertEqual(len(sequence_labels), len(set(sequence_labels)))
        self.assertEqual(page.locator("html").get_attribute("data-theme"), "dark")

        elapsed = panel.locator(".job-panel__elapsed")
        before = elapsed.inner_text()
        page.wait_for_timeout(2200)
        self.assertNotEqual(elapsed.inner_text(), before)

        job = self.app.state.job_service.list("recovery")[-1]
        self.app.state.job_service.heartbeat(job["job_id"])
        panel.locator(".job-panel__transport").filter(has_text="进度通道正常").wait_for()
        self.assertEqual(panel.locator(".job-panel__business-step").inner_text(), business_before)

        panel.get_by_role("button", name="取消后台任务", exact=True).click()
        panel.get_by_text("正在取消：等待当前安全停止点", exact=True).first.wait_for()
        panel.get_by_text("取消请求已送达；正在等待当前安全停止点，期间不会提交新的业务结果。", exact=True).wait_for()
        self.assertTrue(panel.get_by_role("button", name="已请求取消", exact=True).is_disabled())
        self.assertTrue(page.locator("body").evaluate("node => node.scrollWidth <= node.clientWidth"))

        page.set_viewport_size({"width": 812, "height": 375})
        self.assertTrue(page.locator("body").evaluate("node => node.scrollWidth <= node.clientWidth"))

        self.service.release.set()
        jobs = self.wait_for_job_count(1)
        self.assertEqual(jobs[-1]["status"], "cancelled")
        page.close()

    def test_refresh_terminal_cleanup_allows_same_intent_to_create_a_new_job(self):
        page = self.new_page()
        self.start_first_job(page)
        self.wait_for_job_mapping(page)
        page.reload()
        page.get_by_role("heading", name="澄清", exact=True).wait_for()

        self.service.release.set()
        self.wait_for_job_count(1)
        self.wait_for_session_state(page, True)
        page.goto(self.base + "/tasks/recovery?stage=narrative")
        page.get_by_role("heading", name="叙事结构", exact=True).wait_for()
        first = self.app.state.job_service.list("recovery")
        self.assertEqual(len(first), 1)

        page.get_by_role("button", name="按要求修改叙事结构", exact=True).click()
        jobs = self.wait_for_job_count(2)
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        self.assertEqual(self.service.calls, 2)
        page.close()


if __name__ == "__main__":
    unittest.main()
