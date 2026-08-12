import tempfile
import unittest

from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class PassingInspector:
    def inspect(self, outline, html): return {"passed": True, "issues": [], "model": "fixture"}


@unittest.skipUnless(sync_playwright, "playwright is required")
class OfflineDeliveryBrowserGate(unittest.TestCase):
    def test_package_runs_without_network_and_pages_with_controls_and_keyboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=WorkspaceStore(tmp); svc=TaskService(store,inspector=PassingInspector()); svc.create("offline","manual")
            svc.import_input("offline",{"goal":"发布","audience":"客户","topic":"离线演示","页数":3})
            svc.generate_narrative("offline"); svc.confirm_narrative("offline"); svc.generate_outline("offline"); svc.confirm_outline("offline")
            svc.generate_sample("offline"); svc.confirm_sample("offline"); svc.generate_deck("offline"); svc.run_inspection("offline",0)
            deck=svc.deck_view("offline")["deck"]; delivery=svc.confirm_delivery("offline",deck["hash"])["delivery"]
            root=store.delivery_root("offline",delivery["delivery_id"]); network=[]
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True); page=browser.new_page()
                page.route("http://**/*",lambda route:(network.append(route.request.url),route.abort())[1])
                page.route("https://**/*",lambda route:(network.append(route.request.url),route.abort())[1])
                page.goto((root/"index.html").as_uri()); page.wait_for_selector('.slide[aria-hidden="false"]')
                self.assertEqual(page.locator("#offline-page").text_content(),"1 / 3")
                page.click("#offline-next"); self.assertEqual(page.locator("#offline-page").text_content(),"2 / 3")
                page.keyboard.press("ArrowRight"); self.assertEqual(page.locator("#offline-page").text_content(),"3 / 3")
                self.assertTrue(page.locator("#offline-next").is_disabled())
                page.keyboard.press("Home"); self.assertEqual(page.locator("#offline-page").text_content(),"1 / 3")
                page.keyboard.press("End"); page.keyboard.press("PageUp"); self.assertEqual(page.locator("#offline-page").text_content(),"2 / 3")
                self.assertEqual(page.locator('.slide[aria-hidden="false"]').count(),1)
                self.assertEqual(network,[]); browser.close()


if __name__ == "__main__": unittest.main()
