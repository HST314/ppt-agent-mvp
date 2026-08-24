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


class BlockingClarifier:
    model = "authority-reconcile-clarifier"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def clarify(self, _payload):
        self.started.set()
        if not self.release.wait(30):
            raise RuntimeError("clarifier test timed out")
        return {
            "model": self.model,
            "questions": [{
                "question_id": "approval-mode",
                "field_path": "approval_mode",
                "prompt": "本次汇报需要申请预算批准，还是仅同步项目进展？",
                "helper_text": "用于决定材料的论证深度。",
                "options": [
                    {"value": "approve", "label": "申请批准", "description": "突出预算与回报"},
                    {"value": "update", "label": "同步进展", "description": "突出里程碑与风险"},
                ],
                "allow_other": True,
                "blocking": True,
            }],
        }


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

    def new_page(self, *, fail_first_streams=0, stale_authority_reads=0, delay_job_reads=0, width=1280, height=900):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        script = """
            (() => {
              const failFirst = __FAIL_FIRST__;
              const staleAuthorityLimit = __STALE_AUTHORITY__;
              const jobReadDelay = __JOB_READ_DELAY__;
              const NativeEventSource = window.EventSource;
              const nativeFetch = window.fetch.bind(window);
              window.__streamUrls = [];
              window.__pollUrls = [];
              window.__authorityUrls = [];
              window.__staleAuthorityReplies = 0;
              window.__trackerAbortCount = 0;
              window.__trackerSignalCount = 0;
              window.__trackerReadActive = new WeakMap();
              window.__trackerReadMax = 0;
              const publish = () => {
                document.documentElement?.setAttribute("data-test-stream-count", String(window.__streamUrls.length));
                document.documentElement?.setAttribute("data-test-poll-count", String(window.__pollUrls.length));
                document.documentElement?.setAttribute("data-test-authority-count", String(window.__authorityUrls.length));
                document.documentElement?.setAttribute("data-test-stale-authority-count", String(window.__staleAuthorityReplies));
                document.documentElement?.setAttribute("data-test-tracker-abort-count", String(window.__trackerAbortCount));
                document.documentElement?.setAttribute("data-test-tracker-signal-count", String(window.__trackerSignalCount));
                document.documentElement?.setAttribute("data-test-tracker-read-max", String(window.__trackerReadMax));
              };
              let attempts = 0;
              let staleShell = null;
              let staleInput = null;
              let staleShellReplies = 0;
              let staleInputReplies = 0;
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
              const staleResponse = (response, payload) => new Response(JSON.stringify(payload), {
                status: response.status,
                statusText: response.statusText,
                headers: response.headers,
              });
              window.fetch = async (...args) => {
                const target = String(args[0]);
                const path = new URL(target, location.origin).pathname;
                if (/\/v1\/jobs\/[^/]+(?:\/event-history)?$/.test(path) && args[1]?.signal) {
                  window.__trackerSignalCount += 1;
                  const signal = args[1].signal;
                  const signalReads = (window.__trackerReadActive.get(signal) || 0) + 1;
                  window.__trackerReadActive.set(signal, signalReads);
                  window.__trackerReadMax = Math.max(window.__trackerReadMax, signalReads);
                  args[1].signal.addEventListener("abort", () => {
                    window.__trackerAbortCount += 1;
                    publish();
                  }, { once: true });
                  publish();
                  if (jobReadDelay) await new Promise((resolve) => window.setTimeout(resolve, jobReadDelay));
                  window.__trackerReadActive.set(signal, signalReads - 1);
                  publish();
                }
                if (/\/v1\/jobs\/[^/]+$/.test(path)) {
                  window.__pollUrls.push(target);
                  publish();
                }
                const response = await nativeFetch(...args);
                if (/\/v1\/tasks\/[^/]+\/shell$/.test(path)) {
                  window.__authorityUrls.push(target);
                  const payload = await response.clone().json().catch(() => null);
                  if (!staleShell && payload?.active_jobs?.length) staleShell = payload;
                  if (staleShell && payload?.task?.revision > staleShell.task.revision && staleShellReplies < staleAuthorityLimit) {
                    staleShellReplies += 1;
                    window.__staleAuthorityReplies += 1;
                    publish();
                    return staleResponse(response, staleShell);
                  }
                  publish();
                }
                if (/\/v1\/tasks\/[^/]+\/input$/.test(path)) {
                  window.__authorityUrls.push(target);
                  const payload = await response.clone().json().catch(() => null);
                  if (!staleInput && payload?.clarification?.status === "generating") staleInput = payload;
                  if (staleInput && payload?.state?.revision > staleInput.state.revision && staleInputReplies < staleAuthorityLimit) {
                    staleInputReplies += 1;
                    window.__staleAuthorityReplies += 1;
                    publish();
                    return staleResponse(response, staleInput);
                  }
                  publish();
                }
                return response;
              };
            })();
            """.replace("__FAIL_FIRST__", str(fail_first_streams)).replace("__STALE_AUTHORITY__", str(stale_authority_reads)).replace("__JOB_READ_DELAY__", str(delay_job_reads))
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

    def wait_for_job_count(self, count, timeout=6000, task_id="recovery"):
        deadline = time.monotonic() + timeout / 1000
        jobs = []
        while time.monotonic() < deadline:
            jobs = self.app.state.job_service.list(task_id)
            if len(jobs) == count and jobs[-1]["status"] in TERMINAL:
                return jobs
            time.sleep(0.05)
        self.fail(f"expected {count} terminal jobs, got {jobs}")

    def test_terminal_waits_for_authoritative_revision_and_shows_questions_without_reload(self):
        clarifier = BlockingClarifier()
        self.service.clarifier = clarifier
        self.service.create("authority")
        page = self.new_page(fail_first_streams=5, stale_authority_reads=2, width=375, height=820)
        page.goto(self.base + "/tasks/authority?stage=created")
        page.get_by_label("任务卡内容").fill("核心主题：新品发布")
        page.get_by_role("button", name="导入并冻结资料", exact=True).click()

        page.get_by_role("heading", name="模型正在生成澄清问题", exact=True).wait_for()
        self.assertTrue(clarifier.started.wait(2))
        self.assertEqual(page.locator("fieldset.question-card").count(), 0)
        clarifier.release.set()

        jobs = self.wait_for_job_count(1, timeout=8000, task_id="authority")
        self.assertEqual(jobs[-1]["status"], "succeeded")
        self.assertEqual(jobs[-1]["result"]["revision"], self.service.get("authority")["revision"])
        page.locator('html[data-test-poll-count]:not([data-test-poll-count="0"])').wait_for(timeout=10000)
        page.locator('html[data-test-stale-authority-count="4"]').wait_for(timeout=10000)
        page.get_by_role("heading", name="需求澄清", exact=True).wait_for(timeout=10000)
        self.assertEqual(page.locator("fieldset.question-card").count(), 1)
        self.assertTrue(page.get_by_text("本次汇报需要申请预算批准，还是仅同步项目进展？", exact=True).is_visible())
        self.assertEqual(page.evaluate("performance.getEntriesByType('navigation').length"), 1)
        self.assertGreaterEqual(page.evaluate("window.__authorityUrls.length"), 6)
        page.close()

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

    def test_spa_navigation_aborts_queued_tracker_reads(self):
        page = self.new_page(delay_job_reads=500)
        self.start_first_job(page)
        page.locator('html[data-test-tracker-signal-count]:not([data-test-tracker-signal-count="0"])').wait_for()

        page.get_by_label("返回任务首页").click()
        page.get_by_role("heading", name="从任务资料到可交付演示稿，都在一个工作台。").wait_for()
        page.locator('html[data-test-tracker-abort-count]:not([data-test-tracker-abort-count="0"])').wait_for()

        self.service.release.set()
        page.close()

    def test_checkpoint_burst_uses_single_reconcile_and_terminal_stream_does_not_reconnect(self):
        page = self.new_page(delay_job_reads=150)
        errors = []
        http_errors = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("response", lambda response: http_errors.append((response.status, response.url)) if response.status >= 400 else None)
        self.start_first_job(page)
        page.locator('html[data-test-stream-count="1"]').wait_for(timeout=10000)

        coordinator = self.app.state.job_service
        job = coordinator.list("recovery")[-1]
        with coordinator._guard:
            record = coordinator._read("recovery", job["job_id"])
            for index in range(3):
                coordinator._publish_event(record, "checkpoint", message=f"burst-{index}")

        self.service.release.set()
        self.wait_for_job_count(1)
        self.wait_for_session_state(page, True, timeout=10000)
        page.wait_for_timeout(300)

        self.assertEqual(page.evaluate("window.__trackerReadMax"), 1)
        self.assertEqual(page.evaluate("window.__streamUrls.length"), 1)
        self.assertEqual(http_errors, [])
        self.assertEqual(errors, [])
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
