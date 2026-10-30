import threading
import time
import unittest
from dataclasses import replace

from nomi_agent.core.execution_harness import ExecutionHarness, TaskStatus
from nomi_agent.core.result import SkillResult
from nomi_agent.tool_registry import ToolRegistry, ToolValidationError


class FakeNavigation:
    def __init__(self, result=None, started=None, release=None):
        self.result = result or {"success": True, "message": "이동 명령을 보냈습니다.", "data": {"completion_verified": True}}
        self.started = started
        self.release = release
        self.calls = []
        self.stops = 0

    def navigate_to(self, location=None):
        self.calls.append(location)
        if self.started:
            self.started.set()
        if self.release:
            self.release.wait(2)
        return self.result

    def stop(self):
        self.stops += 1
        return True


class FakeManipulation:
    def __init__(self):
        self.calls = []

    def open_gripper(self):
        self.calls.append("open")
        return {"success": True, "message": "열기 명령 전달", "data": {}}

    def close_gripper(self):
        self.calls.append("close")
        return {"success": True, "message": "닫기 명령 전달", "data": {}}

    def pick_object(self, object_name=None):
        self.calls.append(("pick", object_name))
        return {"success": False, "message": "미지원", "data": {"error_code": "unsupported"}}

    def place_object(self, location=None):
        self.calls.append(("place", location))
        return {"success": False, "message": "미지원", "data": {"error_code": "unsupported"}}


class FakeState:
    def __init__(self):
        self.calls = 0
        self.block = None

    def get_robot_state(self):
        self.calls += 1
        if self.block:
            self.block.wait(2)
        return SkillResult.ok("모의 상태", {"source": "test"})


class FakeLocation:
    def get_user_location(self):
        return {"success": True, "message": "테스트 위치", "data": {"city": "서울"}}


class FakeWeather:
    def get_weather(self, city="서울"):
        return {"success": True, "message": city, "data": {"city": city}}


class FakeLLM:
    profile = "test"

    def __init__(self, route):
        self.route = route
        self.tool_calls = []
        self.history = []

    def route_command(self, _text):
        if isinstance(self.route, BaseException):
            raise self.route
        return self.route

    def add_assistant_message(self, text):
        self.history.append(text)

    def run_worker_with_tools(self, *, initial_actions, tool_executor, **_kwargs):
        for action in initial_actions:
            result = tool_executor(action["intent"], action["arguments"])
            self.tool_calls.append(action["intent"])
            if not result.success:
                break
        return "완료했습니다."


def make_registry(**overrides):
    skills = {
        "navigation": overrides.get("navigation", FakeNavigation()),
        "manipulation": overrides.get("manipulation", FakeManipulation()),
        "state": overrides.get("state", FakeState()),
        "location": FakeLocation(),
        "weather": FakeWeather(),
    }
    reg = ToolRegistry(skills=skills)
    reg.update_destinations([
        {"name": "테이블 1", "aliases": ["1번 테이블", "1번테이블"], "x": -2.45, "y": -0.4, "kind": "table_approach"},
        {"name": "테이블 2", "aliases": ["2번 테이블", "2번테이블"], "x": 0.0, "y": 1.5, "kind": "table_approach"},
        {"name": "테이블 3", "aliases": ["3번 테이블", "3번테이블"], "x": 2.45, "y": -0.4, "kind": "table_approach"},
        {"name": "책상", "aliases": ["A", "지점 A"], "x": -1.07, "y": -3.15, "kind": "station"},
        {"name": "소파", "aliases": ["B", "지점 B"], "x": 2.20, "y": -1.75, "kind": "station"},
    ])
    return reg, skills


class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.skills = make_registry()

    def test_schemas_are_generated_from_registered_tools_and_strict(self):
        schemas = self.registry.get_tool_schemas()
        self.assertEqual({s["function"]["name"] for s in schemas}, set(self.registry.get_tool_names()))
        self.assertTrue(all(s["function"]["parameters"]["additionalProperties"] is False for s in schemas))

    def test_normal_tool_dispatch_preserves_skill_interface(self):
        result = self.registry.execute("navigate_to", {"location": "앞으로"})
        self.assertTrue(result.success)
        self.assertEqual(self.skills["navigation"].calls, ["앞으로"])

    def test_unknown_tool_rejected_without_execution(self):
        result = self.registry.execute("run_shell", {"command": "echo hi"})
        self.assertFalse(result.success)
        self.assertEqual(result.data["error_code"], "unknown_tool")

    def test_extra_missing_wrong_type_arguments_are_rejected(self):
        cases = [
            ("get_robot_state", {"verbose": True}, "unexpected_argument"),
            ("navigate_to", {}, "missing_argument"),
            ("navigate_to", {"location": 3}, "invalid_type"),
        ]
        for name, args, code in cases:
            with self.subTest(name=name, args=args):
                result = self.registry.execute(name, args)
                self.assertFalse(result.success)
                self.assertEqual(result.data["error_code"], code)

    def test_named_destination_is_accepted_by_registry(self):
        spec, args = self.registry.validate_call("navigate_to", {"location": "3번 테이블"})
        self.assertEqual(spec.name, "navigate_to")
        self.assertEqual(args, {"location": "테이블 3"})

    def test_unknown_or_ambiguous_destination_is_rejected(self):
        for location, code in (("테이블", "ambiguous_destination"), ("저쪽", "ambiguous_destination"),
                               ("비등록 장소", "unknown_destination")):
            with self.subTest(location=location):
                with self.assertRaises(ToolValidationError) as context:
                    self.registry.validate_call("navigate_to", {"location": location})
                self.assertEqual(context.exception.code, code)
                self.assertTrue(context.exception.needs_clarification)

    def test_navigation_destination_query_returns_shared_coordinates(self):
        result = self.registry.execute("get_navigation_destinations", {})
        self.assertTrue(result.success)
        table = next(row for row in result.data["destinations"] if row["name"] == "테이블 3")
        self.assertEqual((table["x"], table["y"]), (2.45, -0.4))

    def test_legacy_argument_alias_is_normalized(self):
        spec, args = self.registry.validate_call("navigate_to", {"destination": "forward"})
        self.assertEqual(spec.name, "navigate_to")
        self.assertEqual(args, {"location": "forward"})

    def test_unverified_motion_or_gripper_is_not_reported_successful(self):
        unverified_navigation = FakeNavigation({"success": True, "message": "전달됨", "data": {}})
        registry, _ = make_registry(navigation=unverified_navigation)
        for name, args in (("navigate_to", {"location": "forward"}), ("open_gripper", {})):
            result = registry.execute(name, args)
            self.assertFalse(result.success)
            self.assertEqual(result.data["error_code"], "completion_unverified")
            self.assertTrue(result.data["command_dispatched"])

    def test_invalid_skill_result_shape_is_rejected(self):
        class BadState:
            def get_robot_state(self):
                return {"success": "yes", "message": "bad", "data": []}
        registry, _ = make_registry(state=BadState())
        result = registry.execute("get_robot_state", {})
        self.assertFalse(result.success)
        self.assertEqual(result.data["error_code"], "invalid_result")

    def test_mock_robot_state_is_not_presented_as_live_state(self):
        class MockState:
            def get_robot_state(self):
                return {"success": True, "message": "battery 85", "data": {"connection": "mock", "battery": 85}}
        registry, _ = make_registry(state=MockState())
        result = registry.execute("get_robot_state", {})
        self.assertFalse(result.success)
        self.assertEqual(result.data["error_code"], "state_unverified")

    def test_tool_timeout_is_distinct_and_requests_stop_for_robot_motion(self):
        release = threading.Event()
        navigation = FakeNavigation(release=release)
        registry, _ = make_registry(navigation=navigation)
        registry._specs["navigate_to"] = replace(registry._specs["navigate_to"], timeout_seconds=0.05)
        result = registry.execute("navigate_to", {"location": "forward"})
        self.assertFalse(result.success)
        self.assertEqual(result.data["error_code"], "timeout")
        self.assertEqual(result.data["execution_outcome"], "unknown_after_timeout")
        self.assertEqual(navigation.stops, 1)
        release.set()


class ExecutionHarnessTests(unittest.TestCase):
    def wait_terminal(self, harness, task_id, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = harness.get_task(task_id)
            if result and result["status"] in {status.value for status in (TaskStatus.NEEDS_CLARIFICATION, TaskStatus.VALIDATED, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED)}:
                return result
            time.sleep(0.01)
        self.fail(f"task did not finish: {harness.get_task(task_id)}")

    def build_harness(self, route, registry=None):
        registry = registry or make_registry()[0]
        llm = FakeLLM(route)
        replies, statuses = [], []
        harness = ExecutionHarness(llm, registry, replies.append, statuses.append)
        return harness, llm, registry, replies, statuses

    def test_valid_plan_is_executed_and_reported(self):
        route = {"route": "TASK", "actions": [{"intent": "get_robot_state", "arguments": {}}]}
        harness, llm, registry, replies, statuses = self.build_harness(route)
        state_skill = registry.skills["state"]
        outcome, task_id = harness.submit("상태 확인", "req-1")
        self.assertEqual(outcome, "started")
        result = self.wait_terminal(harness, task_id)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(state_skill.calls, 1)
        self.assertEqual(llm.tool_calls, ["get_robot_state"])
        self.assertTrue(replies)
        seen = [item["status"] for item in statuses]
        self.assertEqual(seen[0], "PENDING")
        self.assertIn("VALIDATING", seen)
        self.assertIn("RUNNING", seen)
        self.assertEqual(seen[-1], "SUCCEEDED")

    def test_validate_only_checks_destination_without_executing_skill(self):
        route = {"route": "TASK", "actions": [{"intent": "navigate_to", "arguments": {"location": "3번 테이블"}}]}
        harness, _llm, registry, _replies, _ = self.build_harness(route)
        _, task_id = harness.submit("3번 테이블로 이동해 줘", validate_only=True)
        result = self.wait_terminal(harness, task_id)
        self.assertEqual(result["status"], TaskStatus.VALIDATED.value)
        self.assertEqual(registry.skills["navigation"].calls, [])
        self.assertTrue(result["steps"][0]["data"]["validation_only"])
        self.assertFalse(result["steps"][0]["data"]["execution_performed"])

    def test_bad_route_and_invalid_json_are_not_executed(self):
        for route in ({"route": "INVALID", "error": "router_invalid_json"}, None):
            with self.subTest(route=route):
                harness, _llm, registry, _replies, _ = self.build_harness(route)
                result, task_id = harness.submit("앞으로 가")
                self.assertEqual(result, "started")
                self.assertEqual(self.wait_terminal(harness, task_id)["status"], "FAILED")
                self.assertEqual(registry.skills["navigation"].calls, [])

    def test_unknown_tool_invalid_arguments_and_missing_required_field_are_rejected(self):
        plans = [
            ("작업", [{"intent": "not_registered", "arguments": {}}], "FAILED"),
            ("날씨", [{"intent": "get_weather", "arguments": {"city": 4}}], "FAILED"),
            ("앞으로 가", [{"intent": "navigate_to", "arguments": {}}], "NEEDS_CLARIFICATION"),
            ("거기로 가", [{"intent": "navigate_to", "arguments": {"location": "거기"}}], "NEEDS_CLARIFICATION"),
        ]
        for text, actions, expected in plans:
            with self.subTest(text=text):
                route = {"route": "TASK", "actions": actions}
                harness, _llm, registry, _replies, _ = self.build_harness(route)
                _, task_id = harness.submit(text)
                self.assertEqual(self.wait_terminal(harness, task_id)["status"], expected)
                self.assertEqual(registry.skills["navigation"].calls, [])

    def test_tool_failure_stops_dependent_follow_up_steps(self):
        route = {"route": "TASK", "actions": [
            {"intent": "open_gripper", "arguments": {}},
            {"intent": "get_robot_state", "arguments": {}},
        ]}
        harness, llm, registry, replies, _ = self.build_harness(route)
        _, task_id = harness.submit("그리퍼를 열고 상태 확인")
        result = self.wait_terminal(harness, task_id)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(llm.tool_calls, ["open_gripper"])
        self.assertEqual(registry.skills["state"].calls, 0)
        self.assertIn("완료를 확인하지 못했습니다", replies[-1])

    def test_duplicate_id_and_concurrent_task_are_not_started_twice(self):
        route = {"route": "TASK", "actions": [{"intent": "get_robot_state", "arguments": {}}]}
        harness, llm, _registry, _replies, _ = self.build_harness(route)
        first, task_id = harness.submit("상태 확인", "same-id")
        second, duplicate_id = harness.submit("다른 요청", "same-id")
        self.assertEqual(first, "started")
        self.assertEqual(second, "duplicate")
        self.assertEqual(task_id, duplicate_id)
        self.wait_terminal(harness, task_id)
        self.assertEqual(llm.tool_calls.count("get_robot_state"), 1)

    def test_cancel_stops_active_task_and_publishes_stop(self):
        started, release = threading.Event(), threading.Event()
        nav = FakeNavigation(started=started, release=release)
        registry, _ = make_registry(navigation=nav)
        route = {"route": "TASK", "actions": [{"intent": "navigate_to", "arguments": {"location": "forward"}}]}
        harness, _llm, _registry, _replies, _ = self.build_harness(route, registry)
        _, task_id = harness.submit("앞으로 가")
        self.assertTrue(started.wait(1))
        cancelled, _ = harness.cancel_active()
        self.assertTrue(cancelled)
        self.assertEqual(harness.get_task(task_id)["status"], "CANCELLED")
        self.assertEqual(nav.stops, 1)
        release.set()

    def test_connection_and_llm_timeout_failures_have_distinct_codes(self):
        for error, expected in ((ConnectionError("offline"), "llm_connection_error"), (TimeoutError("timed out"), "llm_timeout")):
            with self.subTest(error=error):
                harness, _llm, _registry, _replies, _ = self.build_harness(error)
                _, task_id = harness.submit("상태 확인")
                result = self.wait_terminal(harness, task_id)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["error"]["code"], expected)

    def test_cancelled_task_allows_later_recovery_request(self):
        started, release = threading.Event(), threading.Event()
        nav = FakeNavigation(started=started, release=release)
        registry, _ = make_registry(navigation=nav)
        route = {"route": "TASK", "actions": [{"intent": "navigate_to", "arguments": {"location": "forward"}}]}
        harness, _llm, _registry, _replies, _ = self.build_harness(route, registry)
        _, first_id = harness.submit("앞으로 가", "first")
        self.assertTrue(started.wait(1))
        harness.cancel_active()
        release.set()
        outcome, second_id = harness.submit("앞으로 가", "second")
        self.assertEqual(outcome, "started")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.wait_terminal(harness, second_id)["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
