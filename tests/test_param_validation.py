"""参数校验：缺哪个、越哪条线、哪个参数不认识，都要指名道姓。

覆盖控制台（HTTP）与 CLI 两个入口，以及 ParamSpec 本身的聚合行为。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

from flashsmelter.console import ConsoleApp, ConsoleServer
from flashsmelter.errors import ValidationError
from flashsmelter.params import Field, ParamSpec, Params

from .helpers import make_app, make_root


class ParamSpecTest(unittest.TestCase):
    def test_missing_reports_name_and_range(self) -> None:
        spec = ParamSpec(
            "waste.start",
            [Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位")],
        )
        with self.assertRaises(ValidationError) as caught:
            spec.extract(Params({}))
        message = caught.exception.message
        self.assertIn("drum_level", message)
        self.assertIn("汽包液位", message)
        self.assertIn("0.0 ~ 1.0", message)
        self.assertEqual("drum_level", caught.exception.details["param"])
        self.assertEqual("missing", caught.exception.details["issues"][0]["problem"])

    def test_out_of_range_reports_value_and_allowed_range(self) -> None:
        spec = ParamSpec(
            "waste.start",
            [Field("drum_level", "number", minimum=0.0, maximum=1.0, unit="比例", label="汽包液位")],
        )
        with self.assertRaises(ValidationError) as caught:
            spec.extract(Params({"drum_level": 1.5}))
        message = caught.exception.message
        self.assertIn("drum_level", message)
        self.assertIn("1.5", message)
        self.assertIn("0.0 ~ 1.0", message)
        issue = caught.exception.details["issues"][0]
        self.assertEqual("above-maximum", issue["problem"])
        self.assertEqual(1.5, issue["value"])
        self.assertEqual(1.0, issue["maximum"])

    def test_unknown_param_is_named(self) -> None:
        spec = ParamSpec("waste.start", [Field("drum_level", "number")])
        with self.assertRaises(ValidationError) as caught:
            spec.extract(Params({"drum_level": 0.5, "drum_lvl": 0.5}))
        message = caught.exception.message
        self.assertIn("drum_lvl", message)
        self.assertIn("drum_level", message)  # 提示里列出可接受的参数名
        self.assertEqual("unknown", caught.exception.details["issues"][0]["problem"])

    def test_all_issues_are_aggregated(self) -> None:
        spec = ParamSpec(
            "demo.act",
            [
                Field("level", "number", minimum=0.0, maximum=1.0),
                Field("rate", "number", minimum=0.0),
                Field("note", "text"),
            ],
        )
        with self.assertRaises(ValidationError) as caught:
            spec.extract(Params({"level": 2.0, "levl": 0.5}))
        message = caught.exception.message
        self.assertIn("共 4 处问题", message)  # level 越界 + rate/note 缺失 + levl 不认识
        problems = [issue["problem"] for issue in caught.exception.details["issues"]]
        self.assertEqual(
            ["above-maximum", "missing", "missing", "unknown"],
            problems,
        )

    def test_non_numeric_and_non_boolean_are_named(self) -> None:
        spec = ParamSpec(
            "demo.act",
            [Field("rate", "number", unit="t/h", label="给料速率"), Field("flag", "boolean")],
        )
        with self.assertRaises(ValidationError) as caught:
            spec.extract(Params({"rate": "150t/h", "flag": "maybe"}))
        message = caught.exception.message
        self.assertIn("rate", message)
        self.assertIn("150t/h", message)
        self.assertIn("flag", message)

    def test_extract_applies_defaults(self) -> None:
        spec = ParamSpec(
            "demo.act",
            [
                Field("actor", "text", required=False, default="control-room"),
                Field("hold", "number", required=False),
                Field("flag", "boolean", required=False, default=True),
            ],
        )
        values = spec.extract(Params({}))
        self.assertEqual({"actor": "control-room", "hold": None, "flag": True}, values)


class ConsoleParamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = make_app()
        cls.console = ConsoleApp(cls.app)
        cls.server = ConsoleServer(cls.console, host="127.0.0.1", port=0)
        cls.host, cls.port = cls.server.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def _post(self, path: str, body: dict):
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_missing_param_points_at_field(self) -> None:
        status, payload = self._post("/api/waste/start", {"actor": "ops"})
        self.assertEqual(400, status)
        self.assertIn("drum_level", payload["message"])
        self.assertIn("0.0 ~ 1.0", payload["message"])
        self.assertEqual("drum_level", payload["details"]["param"])

    def test_out_of_range_gives_allowed_range(self) -> None:
        status, payload = self._post("/api/waste/start", {"drum_level": 1.5})
        self.assertEqual(400, status)
        self.assertIn("1.5", payload["message"])
        self.assertIn("0.0 ~ 1.0", payload["message"])

    def test_unknown_param_is_rejected_before_execution(self) -> None:
        before = dict(self.app.waste.status())
        status, payload = self._post("/api/waste/start", {"drum_level": 0.5, "drum_lvl": 0.5})
        self.assertEqual(400, status)
        self.assertIn("drum_lvl", payload["message"])
        self.assertEqual("unknown", payload["details"]["issues"][0]["problem"])
        self.assertEqual(before, dict(self.app.waste.status()))  # 动作没有执行

    def test_typo_shows_missing_and_unknown_together(self) -> None:
        status, payload = self._post("/api/waste/start", {"drum_lvl": 0.5})
        self.assertEqual(400, status)
        problems = {issue["problem"] for issue in payload["details"]["issues"]}
        self.assertEqual({"missing", "unknown"}, problems)
        self.assertIn("共 2 处问题", payload["message"])

    def test_actions_listing_exposes_param_schema(self) -> None:
        request = urllib.request.Request(f"http://{self.host}:{self.port}/api/actions", method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            listing = json.loads(response.read().decode("utf-8"))
        entry = next(item for item in listing["actions"] if item["action"] == "waste.start")
        drum = next(param for param in entry["params"] if param["name"] == "drum_level")
        self.assertTrue(drum["required"])
        self.assertEqual(0.0, drum["minimum"])
        self.assertEqual(1.0, drum["maximum"])
        self.assertEqual("比例", drum["unit"])

    def test_generation_must_be_integer(self) -> None:
        status, payload = self._post("/api/waste/start", {"drum_level": 0.5, "expected_generation": 2.5})
        self.assertEqual(400, status)
        self.assertIn("expected_generation", payload["message"])
        self.assertIn("整数", payload["message"])


class CliParamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root("flashsmelter-params-")

    def _call(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "flashsmelter", "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )

    def test_missing_param_names_field(self) -> None:
        completed = self._call("call", "waste.start")
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertIn("drum_level", payload["message"])
        self.assertIn("0.0 ~ 1.0", payload["message"])

    def test_unknown_param_names_field(self) -> None:
        completed = self._call(
            "call", "waste.start", "--param", "drum_level=0.5", "--param", "drumlevel=0.5"
        )
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertIn("drumlevel", payload["message"])

    def test_actions_command_lists_schema(self) -> None:
        completed = self._call("actions")
        self.assertEqual(0, completed.returncode)
        listing = json.loads(completed.stdout)
        entry = next(item for item in listing["actions"] if item["action"] == "furnace.start")
        names = {param["name"] for param in entry["params"]}
        self.assertIn("drum_level", names)
        self.assertIn("oxygen_flow_nm3h", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
