"""参数聚合校验：缺参、越限、未知参数、单位疑似填错，一次报全。"""

from __future__ import annotations

import unittest

from flashsmelter.errors import ValidationError
from flashsmelter.specs import build_action_specs
from flashsmelter.validation import format_range, validate_action

from .helpers import make_app


class ValidationAggregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app()
        self.specs = self.app._action_specs

    def _invoke(self, action: str, params):
        return self.app.invoke(action, params, source="test")

    def _issues(self, action: str, params) -> list[dict]:
        try:
            self._invoke(action, params)
        except ValidationError as exc:
            return exc.details["issues"]
        self.fail("应当抛出 ValidationError")

    # ------------------------------------------------------------------ 缺参
    def test_all_missing_params_reported_at_once(self) -> None:
        issues = self._issues("furnace.feed", {"actor": "ops"})
        names = {item["param"] for item in issues}
        self.assertEqual({"heat_id", "rate_tph", "tons"}, names)
        self.assertTrue(all(item["code"] == "missing" for item in issues))
        rate = next(item for item in issues if item["param"] == "rate_tph")
        self.assertEqual("t/h", rate["unit"])

    def test_empty_string_counts_as_missing(self) -> None:
        issues = self._issues("furnace.feed", {"heat_id": "  ", "rate_tph": 10, "tons": 1})
        self.assertEqual(["heat_id"], [item["param"] for item in issues])

    # ------------------------------------------------------------------ 越限
    def test_out_of_range_shows_value_and_allowed_range(self) -> None:
        issues = self._issues(
            "burner.ignite", {"fuel_pressure_kpa": 500, "air_flow_nm3h": 5200}
        )
        self.assertEqual(1, len(issues))
        issue = issues[0]
        self.assertEqual("out-of-range", issue["code"])
        self.assertEqual(500, issue["value"])
        self.assertEqual(120, issue["minimum"])
        self.assertEqual(320, issue["maximum"])
        self.assertIn("120~320 kPa", issue["message"])

    def test_exclusive_minimum_rendered_as_strict_greater(self) -> None:
        issues = self._issues("conv.blow", {"seconds": 0})
        self.assertEqual("out-of-range", issues[0]["code"])
        self.assertIn("大于 0 s", issues[0]["message"])

    def test_enum_choice_error_lists_options(self) -> None:
        issues = self._issues("settler.begin_tap", {"kind": "copper"})
        self.assertEqual("invalid-choice", issues[0]["code"])
        self.assertEqual(["slag", "matte"], issues[0]["choices"])

    def test_bad_type_and_not_integer(self) -> None:
        issues = self._issues("furnace.feed", {"heat_id": "H1", "rate_tph": "fast", "tons": True})
        codes = {item["param"]: item["code"] for item in issues}
        # bool 不应被当成数值
        self.assertEqual("invalid-type", codes["rate_tph"])
        self.assertIn(codes["tons"], ("invalid-type", "out-of-range"))

    # ------------------------------------------------------------- 未知参数
    def test_unknown_param_is_rejected_with_suggestion(self) -> None:
        issues = self._issues("furnace.feed", {"heaat_id": "H1", "rate_tph": 10, "tons": 1})
        issue = issues[0]
        self.assertEqual("unknown-param", issue["code"])
        self.assertEqual("heaat_id", issue["param"])
        self.assertEqual("heat_id", issue["suggestion"])

    def test_unknown_param_without_close_match_lists_allowed(self) -> None:
        issues = self._issues("conv.blow", {"seconds": 10, "zzz": 1})
        issue = next(item for item in issues if item["code"] == "unknown-param")
        self.assertIn("seconds", issue["message"])

    # ------------------------------------------------------------- 单位填错
    def test_pa_instead_of_kpa_is_flagged_with_conversion(self) -> None:
        issues = self._issues("burner.ignite", {"fuel_pressure_kpa": 200000, "air_flow_nm3h": 5200})
        issue = issues[0]
        self.assertEqual("out-of-range", issue["code"])
        self.assertIn("Pa", issue["message"])
        self.assertIn("200", issue["suggestion"])

    def test_percent_instead_of_ratio_is_flagged(self) -> None:
        issues = self._issues(
            "furnace.start",
            {
                "drum_level": 0.6,
                "fuel_pressure_kpa": 200,
                "air_flow_nm3h": 5200,
                "oxygen_baseline": 62,
                "oxygen_baseline_source": "a",
                "oxygen_target": 0.62,
                "oxygen_flow_nm3h": 9000,
            },
        )
        issue = next(item for item in issues if item["param"] == "oxygen_baseline")
        self.assertIn("0.62", issue["suggestion"])
        self.assertIn("比例", issue["message"])

    def test_kgph_instead_of_tph_is_flagged(self) -> None:
        issues = self._issues("furnace.feed", {"heat_id": "H1", "rate_tph": 150000, "tons": 1})
        issue = next(item for item in issues if item["param"] == "rate_tph")
        self.assertIn("kg/h", issue["message"])

    def test_genuinely_out_of_range_value_has_no_unit_hint(self) -> None:
        # 600 kPa 换算成任何常见单位都进不了 120~320，不能乱给单位提示。
        issues = self._issues("burner.ignite", {"fuel_pressure_kpa": 600, "air_flow_nm3h": 5200})
        self.assertIsNone(issues[0].get("suggestion"))

    # ------------------------------------------------------------------ 汇总
    def test_single_and_multiple_issue_summaries(self) -> None:
        try:
            self._invoke("conv.blow", {"seconds": 0})
        except ValidationError as exc:
            self.assertNotIn("个参数问题", exc.message)
        issues = self._issues("furnace.feed", {})
        try:
            self._invoke("furnace.feed", {})
        except ValidationError as exc:
            self.assertIn("3 个参数问题", exc.message)
            self.assertEqual(3, exc.details["issue_count"])
            # 兼容旧调用方的 details.param 指向第一个问题
            self.assertEqual(issues[0]["param"], exc.details["param"])

    def test_backward_compat_param_detail_present(self) -> None:
        try:
            self._invoke("waste.start", {"actor": "ops"})
        except ValidationError as exc:
            self.assertEqual("drum_level", exc.details["param"])

    def test_specs_cover_every_action_and_format_range(self) -> None:
        self.assertEqual(set(self.app.actions), set(self.specs))
        spec = self.specs["burner.ignite"]
        pressure = spec.get("fuel_pressure_kpa")
        self.assertIn("120~320 kPa", format_range(pressure))
        only_max = self.specs["furnace.feed"].get("tons")
        self.assertIn("大于 0 t", format_range(only_max))

    def test_describe_actions_carries_param_specs(self) -> None:
        described = {item["action"]: item for item in self.app.describe_actions()}
        feed = described["furnace.feed"]
        self.assertEqual({"heat_id", "rate_tph", "tons"}, set(feed["required"]))
        rate = next(p for p in feed["params"] if p["name"] == "rate_tph")
        self.assertEqual("t/h", rate["unit"])
        self.assertEqual(180.0, rate["maximum"])

    def test_unknown_action_remains_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.invoke("nope.nope", {})

    def test_build_specs_uses_settings_ranges(self) -> None:
        from flashsmelter.config import Settings

        specs = build_action_specs(Settings(root="/tmp/x", burner_fuel_pressure_max_kpa=300.0))
        pressure = specs["burner.ignite"].get("fuel_pressure_kpa")
        self.assertEqual(300.0, pressure.maximum)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
