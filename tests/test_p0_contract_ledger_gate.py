import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ppt_agent.claim_ledger import assert_claims_bound, audit_claims, audit_html_claims, build_claim_ledger
from ppt_agent.design_contract import build_presentation_technical_contract
from ppt_agent.errors import ConflictError, ValidationError
from ppt_agent.overflow_autofit import MAX_CASCADE_ROUNDS
from ppt_agent.p2 import canonical, digest, now
from ppt_agent.p4 import assemble_presentation, render
from ppt_agent.render_gate import canonical_post_render_evidence, post_render_evidence_hash
from ppt_agent.service import TaskService
from ppt_agent.store import WorkspaceStore


class PassingInspector:
    requires_browser_evidence = True

    def inspect(self, _outline, _html, *, browser_evidence=None):
        return {"passed": True, "issues": [], "model": "passing", "browser_received": browser_evidence is not None}


class PassingGenerationBrowser:
    enforce_on_generation = True

    def inspect(self, _html, expected_slide_ids):
        return {
            "available": True,
            "passed": True,
            "engine": "chromium",
            "engine_version": "contract-test",
            "viewport": {"width": 1280, "height": 720},
            "issues": [],
            "slides": [{"slide_id": slide_id} for slide_id in expected_slide_ids],
        }


class OverflowGenerationBrowser(PassingGenerationBrowser):
    def inspect(self, _html, expected_slide_ids):
        result = super().inspect(_html, expected_slide_ids)
        result.update({
            "passed": False,
            "issues": [{
                "issue_id": "overflow",
                "severity": "blocker",
                "level": "element",
                "code": "content_out_of_bounds",
                "message": "overflow",
                "slide_id": expected_slide_ids[0],
                "element_id": "body",
                "evidence": "35px",
                "suggestion": "fit",
            }],
        })
        return result


class AutofitGenerationBrowser(OverflowGenerationBrowser):
    def __init__(self):
        self.calls = 0

    def inspect(self, _html, expected_slide_ids):
        self.calls += 1
        if self.calls == 1:
            return super().inspect(_html, expected_slide_ids)
        return PassingGenerationBrowser.inspect(self, _html, expected_slide_ids)


class ClaimDroppingBuilder:
    def build(self, _outline, **context):
        sections = [
            f'<section class="slide" id="{slide_id}" data-slide-id="{slide_id}">'
            f'<h2 data-element-id="title">{slide_id}</h2><p data-element-id="body">无数字摘要</p></section>'
            for slide_id in context["slide_ids"]
        ]
        return "<!doctype html><html><head><meta charset=\"utf-8\"></head><body>" + "".join(sections) + "</body></html>"


class RegisteredClaimDroppingBuilder:
    def __init__(self):
        self.calls = []

    def build(self, outline, **context):
        self.calls.append(dict(context))
        html = render(
            outline,
            context["slide_ids"],
            context.get("rules"),
            context.get("exceptions"),
            context.get("assets"),
            context.get("design_contract"),
            context.get("design_contract_hash"),
        )
        for claim in context.get("required_claims_verbatim", []):
            html = html.replace(claim["value"], "冻结事实待服务端绑定")
        return html


class ContractLedgerGateTests(unittest.TestCase):
    def test_temporal_ranges_ordinals_dates_and_navigation_are_typed(self):
        source = (
            "2025年7月1日，经第122次会议讨论。"
            "2025年10月1日，经第133次会议决定。4人才培养"
        )
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding=source,
            created_at="2026-01-01T00:00:00+00:00",
        )
        values = {(item["kind"], item["value"]) for item in ledger["claims"]}
        self.assertIn(("ordinal", "第122次"), values)
        self.assertIn(("ordinal", "第133次"), values)
        self.assertFalse(any(item[1] in {"2025年", "7月", "10月", "122次", "133次", "4人"} for item in values))

        temporal = audit_claims("项目从7月到10月推进。", ledger)
        self.assertTrue(temporal["passed"], temporal)
        self.assertEqual(temporal["bindings"][0]["formula"], "derived_temporal_range")

        ordinal_growth = audit_claims("会议次数从122次增长到133次。", ledger)
        self.assertFalse(ordinal_growth["passed"])
        self.assertEqual(ordinal_growth["unbound"][0]["kind"], "metric_transition")
    def _service(self, root, browser=None):
        return TaskService(
            WorkspaceStore(root),
            inspector=PassingInspector(),
            browser_inspector=browser or PassingGenerationBrowser(),
        )

    def _to_outline(self, service, task_id="task"):
        service.create(task_id, "manual")
        service.import_input(task_id, {
            "goal": "批准扩容预算",
            "audience": "CEO 与 CFO",
            "topic": "AI 客服试点复盘",
            "页数": 4,
            "风格": "严格使用风格 B · 瑞士国际主义",
            "known_facts": {"budget": "80 万元", "saving": "季度节省 36 万元"},
        })
        service.generate_narrative(task_id)
        service.confirm_narrative(task_id)
        service.generate_outline(task_id)
        service.confirm_outline(task_id)

    def _to_deck(self, service, task_id="task"):
        self._to_outline(service, task_id)
        service.select_samples(task_id, ["slide-1", "slide-3"])
        service.generate_sample(task_id)
        service.confirm_sample(task_id)
        return service.generate_deck(task_id)["deck"]

    def test_presentation_technical_contract_and_assembler_are_design_agnostic(self):
        contract=build_presentation_technical_contract(
            task_id="technical",input_snapshot_hash="a"*64,
            outline_hash="b"*64,slide_ids=["slide-1"],created_at=now(),
        )
        contract_hash=digest(canonical(contract))
        fragment='<section class="slide radial-story" id="slide-1" data-slide-id="slide-1"><h1>任意视觉语言</h1></section>'
        html=assemble_presentation(
            [fragment],technical_contract=contract,contract_hash=contract_hash,
            shared_assets={"css":".radial-story{display:grid;background:conic-gradient(#fff,#ddd)}"},
        )

        self.assertEqual(contract["contract_type"],"presentation_technical_contract")
        self.assertNotIn("layout",json.dumps(contract,ensure_ascii=False).lower())
        self.assertIn("radial-story",html)
        self.assertIn('name="presentation-technical-contract"',html)

    def test_claim_ledger_binds_source_and_accepts_only_auditable_derivation(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["软件 12 万元", "服务 8 万元"]},
            created_at=now(),
        )
        bound = audit_claims("预算为 12 万元，服务为 8 万元，合计 12万元+8万元=20万元。", ledger)

        self.assertTrue(bound["passed"])
        self.assertTrue(any(item["status"] == "derived" and item["value"].replace(" ", "") == "20万元" for item in bound["bindings"]))
        with self.assertRaises(ValidationError):
            assert_claims_bound("预计 7 个月回本。", ledger, "叙事")

    def test_currency_scale_equivalence_does_not_merge_other_dimensions(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["软件预算 24万", "覆盖客户 24万人"]},
            created_at=now(),
        )

        result = audit_claims("软件预算 24 万元，覆盖客户 24万人。", ledger)

        self.assertTrue(result["passed"])
        self.assertEqual(result["unbound_count"], 0)
        with self.assertRaisesRegex(ValidationError, "未绑定事实"):
            assert_claims_bound("覆盖客户 24万元。", build_claim_ledger(
                task_id="people",
                input_snapshot_hash="b" * 64,
                source_binding={"known_facts": ["覆盖客户 24万人"]},
                created_at=now(),
            ), "大纲")

    def test_markdown_emphasis_does_not_hide_equivalent_transition_claim(self):
        ledger = build_claim_ledger(
            task_id="transition",
            input_snapshot_hash="c" * 64,
            source_binding={"客户满意度": "4.2→4.6"},
            created_at=now(),
        )
        claim_id=ledger["claims"][0]["claim_id"]

        result = audit_claims(
            "客户满意度由 **4.2** 提升至 **4.6**。",
            ledger,
            required_claim_ids=[claim_id],
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["covered_required_claim_ids"],[claim_id])
        self.assertEqual(result["bindings"][0]["value"],"4.2 提升至 4.6")

    def test_markdown_transition_with_compound_unit_is_one_directional_claim(self):
        ledger = build_claim_ledger(
            task_id="compound-transition",
            input_snapshot_hash="c" * 64,
            source_binding="拣选效率由 **110 单/小时** 提升至 **145 单/小时**。",
            created_at="2026-08-22T00:00:00+00:00",
        )

        relation = [claim for claim in ledger["claims"] if claim["kind"] == "metric_transition"]
        self.assertEqual(len(relation), 1)
        self.assertEqual(relation[0]["normalized_value"], "110单/小时→145单/小时")
        rendered = audit_claims("拣选效率从 110 单/小时 → 145 单/小时", ledger)
        self.assertTrue(rendered["passed"], rendered)
        self.assertIn(relation[0]["claim_id"], {
            claim_id
            for binding in rendered["bindings"]
            for claim_id in binding["source_claim_ids"]
        })

    def test_transition_unit_mismatch_is_not_collapsed_into_relationship(self):
        ledger = build_claim_ledger(
            task_id="mismatched-transition",
            input_snapshot_hash="d" * 64,
            source_binding="吞吐从 110 单/小时 提升至 145 件/日。",
            created_at="2026-08-22T00:00:00+00:00",
        )

        self.assertFalse(any(claim["kind"] == "metric_transition" for claim in ledger["claims"]))

    def test_generated_narrative_cannot_disclose_invented_metrics_as_assumptions(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["项目总周期 12 周"]},
            created_at=now(),
        )

        disclosed = "建议第 4 周完成启动，并配置 45% 资源（数据待确认）。"

        self.assertTrue(assert_claims_bound(disclosed, ledger, "人工叙事"))
        with self.assertRaisesRegex(ValidationError, "4 周.*45%"):
            assert_claims_bound(disclosed, ledger, "模型叙事", allow_disclosed_assumptions=False)

    def test_required_claim_coverage_is_bidirectional_and_reports_omissions(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["响应时间下降 42%", "满意度 4.2 → 4.6"]},
            created_at=now(),
        )
        required = [claim["claim_id"] for claim in ledger["claims"]]

        missing = audit_claims("响应时间下降 42%。", ledger, required_claim_ids=required)

        self.assertFalse(missing["passed"])
        self.assertEqual(missing["unbound_count"], 0)
        self.assertEqual(missing["missing_required_count"], 1)
        self.assertEqual(missing["missing_required"][0]["normalized_value"], "4.2→4.6")
        complete = audit_claims("响应时间下降 42%，满意度 4.2→4.6。", ledger, required_claim_ids=required)
        self.assertTrue(complete["passed"])
        self.assertEqual(complete["covered_required_count"], 2)

    def test_transition_binding_normalizes_natural_language_and_arrow_across_stages(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["满意度从 4.2 提升至 4.6"]},
            created_at=now(),
        )

        arrow = audit_claims("核心结果：满意度 4.2 → 4.6。", ledger)
        wording = audit_claims("核心结果：满意度 4.2 提高到 4.6。", ledger)
        invented = audit_claims("核心结果：满意度 4.2 → 4.7。", ledger)

        self.assertTrue(arrow["passed"], arrow)
        self.assertTrue(wording["passed"], wording)
        self.assertFalse(invented["passed"])
        self.assertEqual(invented["unbound"][0]["normalized_value"], "4.2→4.7")
        self.assertEqual(ledger["claims"][0]["normalized_value"], "4.2→4.6")

    def test_html_claim_extraction_never_joins_number_and_unit_across_dom_nodes(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["满意度 4.2 → 4.6"]},
            created_at=now(),
        )
        html = """<!doctype html><html><body><section class="slide">
        <p>系数 0.4</p><p>人员配置保持稳定</p>
        <p>满意度 <strong>4.2 → 4.6</strong></p>
        </section></body></html>"""

        result = audit_html_claims(html, ledger, required_claim_ids=[ledger["claims"][0]["claim_id"]])
        real_metric = audit_html_claims(
            '<!doctype html><html><body><section class="slide"><p>新增配置 0.4 人</p></section></body></html>',
            ledger,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["unbound_count"], 0)
        self.assertEqual(result["covered_required_count"], 1)
        self.assertFalse(real_metric["passed"])
        self.assertEqual(real_metric["unbound"][0]["normalized_value"], "0.4人")

    def test_standard_head_meta_and_body_void_tags_do_not_hide_claim_text(self):
        ledger = build_claim_ledger(
            task_id="task",
            input_snapshot_hash="a" * 64,
            source_binding={"known_facts": ["软件 36 万元", "服务 24 万元", "实施 12 万元", "培训 8 万元"]},
            created_at=now(),
        )
        html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>预算方案</title>
  <style>.slide { color: black; }</style>
</head>
<body>
  <section class="slide" data-slide-id="slide-1">
    <p>软件 36 万元<br>服务 24 万元</p>
    <p>实施 12 万元，培训 8 万元，另报 80 万元</p>
  </section>
</body>
</html>"""

        result = audit_html_claims(html, ledger)

        self.assertFalse(result["passed"])
        self.assertEqual(result["binding_count"], 4)
        self.assertEqual(result["unbound_count"], 1)
        self.assertEqual(result["unbound"][0]["normalized_value"], "80万")

    def test_contract_and_ledger_hashes_survive_every_delivery_stage(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            self._to_outline(service)
            service.select_samples("task", ["slide-1", "slide-3"])
            sample = service.generate_sample("task")["sample"]
            contract = service.design_contract_view("task")
            ledger = service.claim_ledger_view("task")

            self.assertEqual(contract["contract_type"], "presentation_technical_contract")
            self.assertEqual(contract["slide_ids"], ["slide-1", "slide-2", "slide-3", "slide-4"])
            self.assertEqual(sample["metadata"]["presentation_technical_contract_hash"], contract["hash"])
            self.assertEqual(sample["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(sample["metadata"]["claim_ledger_hash"], ledger["hash"])
            service.confirm_sample("task")
            deck = service.generate_deck("task")["deck"]
            gate = deck["metadata"]["post_render_gate"]
            self.assertEqual(gate["blocker_count"], 0)
            self.assertEqual(gate["geometry"]["overflow_count"], 0)
            self.assertEqual(gate["layout"]["technical_binding_percent"], 100)
            self.assertEqual(gate["claims"]["unbound_count"], 0)
            self.assertEqual(canonical_post_render_evidence(gate), service.version("task", gate["evidence_hash"]))
            self.assertIn(gate["evidence_hash"], {item["hash"] for item in service.versions("task", "post-render-gate-evidence")})

            inspection = service.run_inspection("task", 0)["report"]
            self.assertEqual(inspection["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(inspection["metadata"]["claim_ledger_hash"], ledger["hash"])
            finalization = service.finalize_deck("task", deck["hash"], "review")["finalization"]
            self.assertEqual(finalization["design_contract_hash"], contract["hash"])
            self.assertEqual(finalization["claim_ledger_hash"], ledger["hash"])
            delivery = service.publish_delivery("task")["delivery"]
            delivery_record = service.versions("task", "delivery")[-1]
            self.assertEqual(delivery_record["metadata"]["design_contract_hash"], contract["hash"])
            self.assertEqual(delivery_record["metadata"]["claim_ledger_hash"], ledger["hash"])
            self.assertIn("presentation-technical-contract.json", delivery["files"])
            self.assertIn("claim-ledger.json", delivery["files"])
            self.assertIn("post-render-gate-evidence.json", delivery["files"])
            delivery_root = service.store.delivery_root("task", delivery["delivery_id"])
            packaged_evidence = (delivery_root / "post-render-gate-evidence.json").read_bytes()
            self.assertEqual(packaged_evidence, canonical_post_render_evidence(gate))
            self.assertEqual(digest(packaged_evidence), gate["evidence_hash"])

    def test_terminal_green_geometry_reconciles_autofit_convergence_in_hashed_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            browser = AutofitGenerationBrowser()
            service = self._service(root, browser)
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])
            fitted = {
                "available": True,
                "html": None,
                "rules": [{"slide_id": "slide-1", "element_id": "body", "font_size": 18}],
                "rounds": MAX_CASCADE_ROUNDS,
                "converged": False,
                "remaining": [],
            }

            def fit(html_text, max_rounds):
                self.assertEqual(max_rounds, MAX_CASCADE_ROUNDS)
                return {**fitted, "html": html_text}

            with patch("ppt_agent.service.fit_deck_html", side_effect=fit):
                sample = service.generate_sample("task")["sample"]

            gate = sample["metadata"]["post_render_gate"]
            self.assertTrue(gate["geometry"]["passed"])
            self.assertEqual(gate["geometry"]["overflow_count"], 0)
            self.assertEqual(gate["overflow_autofit"]["remaining"], [])
            self.assertTrue(gate["overflow_autofit"]["converged"])
            self.assertEqual(gate["overflow_autofit"]["rules"], fitted["rules"])
            self.assertEqual(gate["overflow_autofit"]["rounds"], MAX_CASCADE_ROUNDS)
            raw = canonical_post_render_evidence(gate)
            self.assertEqual(digest(raw), gate["evidence_hash"])
            self.assertEqual(service.version("task", gate["evidence_hash"]), raw)

    def test_terminal_overflow_keeps_autofit_fail_closed_for_regeneration(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root, OverflowGenerationBrowser())
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])
            remaining = [{
                "slide_id": "slide-1",
                "kind": "oob",
                "selector": '.slide[data-slide-id="slide-1"] [data-element-id="body"]',
            }]

            def fit(html_text, max_rounds):
                self.assertEqual(max_rounds, MAX_CASCADE_ROUNDS)
                return {
                    "available": True,
                    "html": html_text,
                    "rules": {remaining[0]["selector"]: "height: 564.00px !important;"},
                    "rounds": MAX_CASCADE_ROUNDS,
                    "converged": False,
                    "remaining": remaining,
                }

            with patch("ppt_agent.service.fit_deck_html", side_effect=fit):
                with self.assertRaisesRegex(ValidationError, "渲染后硬门禁未通过"):
                    service.generate_sample("task")

            records = service.versions("task", "post-render-gate-evidence")
            self.assertEqual(len(records), 1)
            evidence = json.loads(service.version("task", records[0]["hash"]))
            self.assertFalse(evidence["geometry"]["passed"])
            self.assertEqual(evidence["geometry"]["overflow_count"], 1)
            self.assertFalse(evidence["overflow_autofit"]["converged"])
            self.assertEqual(evidence["overflow_autofit"]["remaining"], remaining)
            self.assertFalse(service.versions("task", "sample"))

    def test_finalize_rejects_tampered_gate_evidence_without_saving_a_fact(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            deck = self._to_deck(service)
            service.run_inspection("task", 0)
            evidence_hash = deck["metadata"]["post_render_gate"]["evidence_hash"]
            evidence_path = Path(root) / "task" / "artifacts" / evidence_hash
            tampered = json.loads(evidence_path.read_bytes())
            tampered["blocker_count"] = 1
            evidence_path.write_bytes(canonical(tampered))

            with self.assertRaisesRegex(ConflictError, "evidence 哈希重算不一致"):
                service.finalize_deck("task", deck["hash"], "review")

            self.assertFalse(service.versions("task", "final-deck"))

    def test_delivery_rejects_missing_gate_evidence_without_writing_a_package(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            deck = self._to_deck(service)
            service.run_inspection("task", 0)
            service.finalize_deck("task", deck["hash"], "review")
            evidence_hash = deck["metadata"]["post_render_gate"]["evidence_hash"]
            (Path(root) / "task" / "artifacts" / evidence_hash).unlink()

            with self.assertRaisesRegex(ConflictError, "evidence 工件缺失"):
                service.publish_delivery("task")

            self.assertFalse(service.versions("task", "delivery"))

    def test_unbound_narrative_is_rejected_before_a_new_artifact_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            service.create("task", "manual")
            service.import_input("task", {"goal": "汇报", "audience": "管理层", "topic": "试点"})
            service.generate_narrative("task")
            before = len(service.versions("task", "narrative"))

            with self.assertRaisesRegex(ValidationError, "未绑定事实"):
                service.edit_narrative("task", "# 叙事\n\n预计 7 个月回本。")

            self.assertEqual(len(service.versions("task", "narrative")), before)

    def test_generation_overflow_is_rejected_before_sample_artifact_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root, OverflowGenerationBrowser())
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])

            with self.assertRaisesRegex(ValidationError, "渲染后硬门禁未通过"):
                service.generate_sample("task")

            self.assertFalse(service.versions("task", "sample"))

    def test_missing_facts_are_materialized_without_adding_a_style_gate(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            self._to_outline(service)
            service.select_samples("task", ["slide-1", "slide-2", "slide-3", "slide-4"])
            builder = RegisteredClaimDroppingBuilder()
            service.builder = builder

            sample = service.generate_sample("task")["sample"]

            generation = sample["metadata"]["generation"]
            self.assertEqual(len(builder.calls), 2)
            self.assertGreater(generation["server_claim_materialization"]["materialized_count"], 0)
            self.assertEqual(sample["metadata"]["post_render_gate"]["claims"]["missing_required_count"], 0)
            self.assertNotIn("canonical_validator", sample["metadata"]["post_render_gate"])
            self.assertEqual(sample["metadata"]["post_render_gate"]["layout"]["technical_binding_percent"], 100)

    def test_stale_automatic_sample_selection_is_refreshed_on_generation(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root)
            self._to_outline(service)
            first = service.select_samples("task")["selection"]
            outline = service.planning_view("task")["outline"]["markdown"]
            service.edit_outline("task", outline + "\n<!-- clarify copy -->\n")
            service.confirm_outline("task")

            generated = service.generate_sample("task")

            self.assertNotEqual(generated["selection"]["hash"], first["hash"])
            self.assertEqual(generated["selection"]["outline_hash"], generated["outline_hash"])
            self.assertEqual(generated["selection"]["metadata"]["strategy"], "representative-diversity-v1")

    def test_failed_gate_persists_evidence_with_blocker_diagnostics(self):
        with tempfile.TemporaryDirectory() as root:
            service = self._service(root, OverflowGenerationBrowser())
            self._to_outline(service)
            service.select_samples("task", ["slide-1"])

            with self.assertRaisesRegex(ValidationError, "渲染后硬门禁未通过（evidence [0-9a-f]{12}）"):
                service.generate_sample("task")

            self.assertFalse(service.versions("task", "sample"))
            records = service.versions("task", "post-render-gate-evidence")
            self.assertEqual(len(records), 1)
            self.assertIs(records[0]["metadata"]["passed"], False)
            self.assertEqual(records[0]["metadata"]["immutable"], True)
            evidence = json.loads(service.version("task", records[0]["hash"]))
            self.assertFalse(evidence["passed"])
            self.assertEqual(evidence["geometry"]["overflow_count"], 1)
            blocker = next(item for item in evidence["blockers"] if item["code"] == "content_out_of_bounds")
            self.assertEqual(blocker["slide_id"], "slide-1")
            self.assertEqual(blocker["element_id"], "body")
            self.assertEqual(post_render_evidence_hash(evidence), records[0]["hash"])


if __name__ == "__main__":
    unittest.main()
