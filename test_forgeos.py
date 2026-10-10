import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forgeos import CompileRequest, Component, Dependency, Design, ForgeError, ForgeOS, SearchBudget, Store, canonical_hash


class ForgeOSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = ForgeOS(Store(Path(self.tmp.name) / "test.db"))
        self.design = Design(name="Sensor fleet", summary="A deterministic test system for field sensing.", unit_name="kit", components=[
            Component(id="controller", name="Controller", function="Process sensor readings", search_query="ESP32 board", quantity_per_unit=1, must_include=["ESP32"], interface="I2C"),
            Component(id="sensor", name="Sensor", function="Measure temperature", search_query="SHT30 sensor", quantity_per_unit=1, must_include=["SHT30"], interface="I2C"),
            Component(id="power", name="Power supply", function="Provide regulated power", search_query="5V power supply", quantity_per_unit=1, must_include=["5V"], interface="DC"),
            Component(id="enclosure", name="Enclosure", function="Protect the electronics", search_query="electronics enclosure", quantity_per_unit=1, must_include=[], interface="physical"),
        ], dependencies=[
            Dependency(source="controller", target="sensor", rule="I2C at 3.3V"),
            Dependency(source="controller", target="power", rule="5V regulated input"),
            Dependency(source="controller", target="enclosure", rule="board must fit enclosure"),
        ])
        self.req = CompileRequest(intent="Build field sensing kits for classroom deployment", quantity=10, budget=10000, location="Goa, India")
        self.market = {
            "controller": {"candidates": [
                {"id":"c1","component_id":"controller","title":"ESP32 board","supplier":"Alpha","price":300,"pack_size":1,"rating":4.5,"reviews":100,"delivery":"Free","link":None,"thumbnail":None,"product_id":"1","compatibility":"verified","gates":[],"evidence_risk":.08},
                {"id":"c2","component_id":"controller","title":"ESP32 5 pack","supplier":"Beta","price":1300,"pack_size":5,"rating":4.4,"reviews":40,"delivery":"3 days","link":None,"thumbnail":None,"product_id":"2","compatibility":"verified","gates":[],"evidence_risk":.04}]},
            "sensor": {"candidates": [
                {"id":"s1","component_id":"sensor","title":"SHT30 module","supplier":"Alpha","price":180,"pack_size":1,"rating":4.1,"reviews":25,"delivery":"Free","link":None,"thumbnail":None,"product_id":"3","compatibility":"verified","gates":[],"evidence_risk":.12},
                {"id":"s2","component_id":"sensor","title":"SHT30 pair","supplier":"Gamma","price":330,"pack_size":2,"rating":4.6,"reviews":80,"delivery":"2 days","link":None,"thumbnail":None,"product_id":"4","compatibility":"verified","gates":[],"evidence_risk":.03}]},
            "power": {"candidates": [
                {"id":"p1","component_id":"power","title":"5V supply","supplier":"Alpha","price":120,"pack_size":1,"rating":4.2,"reviews":30,"delivery":"Free","link":None,"thumbnail":None,"product_id":"5","compatibility":"verified","gates":[],"evidence_risk":.10},
                {"id":"p2","component_id":"power","title":"5V supply pack of 5","supplier":"Delta","price":500,"pack_size":5,"rating":4.7,"reviews":90,"delivery":"2 days","link":None,"thumbnail":None,"product_id":"6","compatibility":"verified","gates":[],"evidence_risk":.02}]},
            "enclosure": {"candidates": [
                {"id":"e1","component_id":"enclosure","title":"Electronics enclosure","supplier":"Alpha","price":100,"pack_size":1,"rating":4.0,"reviews":20,"delivery":"Free","link":None,"thumbnail":None,"product_id":"7","compatibility":"verified","gates":[],"evidence_risk":.11},
                {"id":"e2","component_id":"enclosure","title":"Electronics enclosure pack of 5","supplier":"Epsilon","price":450,"pack_size":5,"rating":4.8,"reviews":120,"delivery":"2 days","link":None,"thumbnail":None,"product_id":"8","compatibility":"verified","gates":[],"evidence_risk":.02}]},
        }

    def tearDown(self): self.tmp.cleanup()

    def test_under_decomposed_qwen_design_is_completed(self):
        raw = {
            "name": "Classroom monitor", "summary": "A classroom environmental monitoring device.", "unit_name": "station",
            "components": [
                {"id": "sensor", "name": "Air sensor", "function": "Measure air quality", "search_query": "CO2 air quality sensor", "quantity_per_unit": 1, "must_include": ["CO2"], "interface": "I2C", "optional": True, "alternatives": []},
                {"id": "display", "name": "Display", "function": "Show readings", "search_query": "OLED display module", "quantity_per_unit": 1, "must_include": ["OLED"], "interface": "I2C", "optional": True, "alternatives": []},
            ], "dependencies": []
        }
        with patch.object(self.engine, "_ollama", return_value=raw):
            completed = self.engine.design(self.req)
        self.assertGreaterEqual(len(completed.components), 4)
        self.assertTrue(all(not component.optional for component in completed.components))
        self.assertIn("power_system", {component.id for component in completed.components})

    def test_optimizer_covers_quantities_and_budget(self):
        plan = self.engine.optimize(self.design, self.market, self.req, "balanced")
        self.assertIn(plan["status"], {"optimal", "feasible"})
        self.assertLessEqual(plan["total_cost"], self.req.budget)
        self.assertTrue(all(x["supplied_units"] >= x["required_units"] for x in plan["selections"]))

    def test_supplier_failure_recompiles(self):
        plan = self.engine.optimize(self.design, self.market, self.req, "balanced", {"Alpha"})
        self.assertIn(plan["status"], {"optimal", "feasible"})
        self.assertNotIn("Alpha", plan["supplier_mix"])

    def test_chaos_is_seeded_and_reproducible(self):
        plan = self.engine.optimize(self.design, self.market, self.req, "resilient")
        first = self.engine.chaos(plan, 21, 1000)
        second = self.engine.chaos(plan, 21, 1000)
        self.assertEqual(canonical_hash(first), canonical_hash(second))
        self.assertGreaterEqual(first["cvar95"], first["p95_cost"])

    def test_economics_uses_user_selling_price(self):
        plan = self.engine.optimize(self.design, self.market, self.req, "balanced")
        priced_request = self.req.model_copy(update={"selling_price_per_unit": 1000.0})
        economics = self.engine.economics(plan, priced_request)
        self.assertEqual(economics["projected_revenue"], 10000.0)
        self.assertEqual(economics["projected_gross_profit"], round(10000.0 - plan["total_cost"], 2))
        self.assertTrue(economics["profitable_on_material_cost"])

    def test_commercial_viability_uses_market_benchmark(self):
        plan = self.engine.optimize(self.design, self.market, self.req, "balanced")
        priced_request = self.req.model_copy(update={"selling_price_per_unit": 1000.0})
        response = {"shopping_results": [
            {"title": "Comparable A", "source": "Seller A", "extracted_price": 800},
            {"title": "Comparable B", "source": "Seller B", "extracted_price": 1000},
            {"title": "Comparable C", "source": "Seller C", "extracted_price": 1200},
            {"title": "Comparable D", "source": "Seller D", "extracted_price": 1400},
        ], "search_metadata": {"id": "market-1"}}
        with patch.object(self.engine, "_serp", return_value=response) as search:
            result = self.engine.commercial_viability(self.design, priced_request, plan, SearchBudget(10))
        self.assertTrue(result["available"])
        self.assertEqual(result["market_price_median"], 1100.0)
        self.assertEqual(result["selling_price"], 1000.0)
        self.assertEqual(search.call_count, 1)

    def test_dependency_graph_criticality(self):
        graph = self.engine.graph(self.design)
        nodes = {x["id"]: x for x in graph["nodes"]}
        self.assertGreater(nodes["controller"]["criticality"], nodes["sensor"]["criticality"])
        self.assertEqual(graph["cycles"], [])

    def test_serpapi_monthly_guard_is_atomic(self):
        # Limit 14, 12 previously used and one reserved means exactly one new search is allowed.
        self.engine.store.reserve_serp_search("first", 14, 12, 1)
        status = self.engine.store.serp_usage(14, 12, 1)
        self.assertEqual(status["forgeos_live_searches"], 1)
        self.assertEqual(status["usable_remaining"], 0)
        with self.assertRaises(ForgeError) as caught:
            self.engine.store.reserve_serp_search("second", 14, 12, 1)
        self.assertEqual(caught.exception.status, 429)

    def test_per_operation_search_budget_is_hard(self):
        budget = SearchBudget(3)
        for _ in range(3):
            budget.reserve()
        self.assertEqual(budget.used, 3)
        with self.assertRaises(ForgeError) as caught:
            budget.reserve()
        self.assertEqual(caught.exception.status, 429)

    def test_ollama_falls_back_from_chat_to_generate(self):
        class Response:
            def __init__(self, ok, status, data=None, text=""):
                self.ok, self.status_code, self._data, self.text = ok, status, data, text
            def json(self):
                return self._data

        responses = [
            Response(False, 500, text="chat runner unavailable"),
            Response(True, 200, {"response": "```json\n{\"status\": \"READY\"}\n```"}),
        ]
        schema = {"type": "object"}
        with patch("forgeos.requests.post", side_effect=responses) as post:
            result = self.engine._ollama("Return READY", schema)
        self.assertEqual(result, {"status": "READY"})
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].args[0].endswith("/api/chat"))
        self.assertTrue(post.call_args_list[1].args[0].endswith("/api/generate"))
        self.assertNotIn("format", post.call_args_list[0].kwargs["json"])
        self.assertFalse(post.call_args_list[0].kwargs["json"]["think"])


if __name__ == "__main__": unittest.main()
