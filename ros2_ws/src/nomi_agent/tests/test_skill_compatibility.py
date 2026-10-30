"""Regression checks against the existing Skill method signatures and payloads.

ROS message classes and publishers are replaced in-process; no ROS graph or robot
hardware is contacted.
"""

import importlib
import json
import sys
import types
import unittest
from unittest.mock import patch


class Publisher:
    def __init__(self, node, topic):
        self.node = node
        self.topic = topic
        self.messages = []

    def publish(self, message):
        self.messages.append(message)
        if self.topic == "/nomi/navigation_request" and self.node.auto_navigation_result:
            request = json.loads(message.data)
            result = self.node.auto_navigation_result(request)
            callback = self.node.subscriptions.get("/nomi/navigation_result")
            if callback:
                callback(types.SimpleNamespace(data=json.dumps(result)))


class MockCallbackGroup:
    def add_entity(self, entity):
        pass


class MockNode:
    def __init__(self):
        self.publishers = {}
        self.subscriptions = {}
        self.auto_navigation_result = None
        self.default_callback_group = MockCallbackGroup()

    def create_publisher(self, message_type, topic, _depth):
        publisher = Publisher(self, topic)
        self.publishers[topic] = (message_type, publisher)
        return publisher

    def create_subscription(self, _message_type, topic, callback, _depth):
        self.subscriptions[topic] = callback
        return callback

    def get_logger(self):
        return types.SimpleNamespace(info=lambda _message: None)


def ros_message_stubs():
    geometry = types.ModuleType("geometry_msgs")
    geometry_msg = types.ModuleType("geometry_msgs.msg")

    class Twist:
        def __init__(self):
            self.linear = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
            self.angular = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)

    geometry_msg.Twist = Twist
    geometry.msg = geometry_msg
    std = types.ModuleType("std_msgs")
    std_msg = types.ModuleType("std_msgs.msg")

    class String:
        def __init__(self):
            self.data = ""

    class Bool:
        def __init__(self):
            self.data = False

    std_msg.String = String
    std_msg.Bool = Bool
    std.msg = std_msg
    return {"geometry_msgs": geometry, "geometry_msgs.msg": geometry_msg, "std_msgs": std, "std_msgs.msg": std_msg}


class ExistingSkillCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.previous_modules = {}

    def load_skill_module(self, name):
        module_name = f"nomi_agent.skills.{name}"
        previous = sys.modules.pop(module_name, None)
        self.previous_modules[module_name] = previous
        try:
            with patch.dict(sys.modules, ros_message_stubs()):
                module = importlib.import_module(module_name)
            return module, previous
        except Exception:
            if previous is not None:
                sys.modules[module_name] = previous
            raise

    def tearDown(self):
        import nomi_agent.skills as skills_package
        for module_name, previous in self.previous_modules.items():
            sys.modules.pop(module_name, None)
            attribute = module_name.rsplit(".", 1)[1]
            if previous is not None:
                sys.modules[module_name] = previous
                setattr(skills_package, attribute, previous)
            elif hasattr(skills_package, attribute):
                delattr(skills_package, attribute)

    def test_navigation_signature_publishes_velocity_then_zero_and_supports_stop(self):
        module, _previous = self.load_skill_module("navigation_skill")
        node = MockNode()
        skill = module.NavigationSkill(node)
        with patch.object(module.time, "sleep"):
            result = skill.navigate_to(location="forward")
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["duration_s"], 0.8)
        publisher = node.publishers["/nomi/xlerobot/base_cmd_vel"][1]
        self.assertEqual(len(publisher.messages), 2)
        self.assertEqual(publisher.messages[0].linear.x, 0.25)
        self.assertEqual(publisher.messages[1].linear.x, 0.0)
        self.assertTrue(skill.stop())
        self.assertEqual(len(publisher.messages), 3)

    def test_named_destination_is_forwarded_to_web_navigation_without_velocity(self):
        module, _previous = self.load_skill_module("navigation_skill")
        node = MockNode()
        node.auto_navigation_result = lambda request: {
            "request_id": request["request_id"], "status": "SUCCEEDED",
            "completion_verified": True, "position": {"x": 1.0, "y": 2.0},
            "distance_m": 0.2, "message": "도착 확인",
        }
        skill = module.NavigationSkill(node)
        result = skill.navigate_to(location="집")
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["request_dispatched"])
        self.assertTrue(result["data"]["completion_verified"])
        velocity_publisher = node.publishers["/nomi/xlerobot/base_cmd_vel"][1]
        self.assertEqual(velocity_publisher.messages, [])
        publisher = node.publishers["/nomi/navigation_request"][1]
        request = json.loads(publisher.messages[0].data)
        self.assertEqual(request["name"], "집")
        self.assertTrue(request["request_id"])

    def test_named_destination_failure_is_never_reported_as_arrival(self):
        module, _previous = self.load_skill_module("navigation_skill")
        node = MockNode()
        node.auto_navigation_result = lambda request: {
            "request_id": request["request_id"], "status": "FAILED",
            "completion_verified": False, "message": "경로를 찾지 못했습니다.",
        }
        result = module.NavigationSkill(node).navigate_to(location="테이블 3")
        self.assertFalse(result["success"])
        self.assertFalse(result["data"]["completion_verified"])
        self.assertEqual(result["data"]["navigation_status"], "FAILED")

    def test_gripper_signatures_and_legacy_json_command_payload_remain_stable(self):
        module, _previous = self.load_skill_module("manipulation_skill")
        node = MockNode()
        skill = module.ManipulationSkill(node)
        self.assertTrue(skill.open_gripper()["success"])
        self.assertTrue(skill.close_gripper()["success"])
        publisher = node.publishers["/nomi/xlerobot/gripper_command"][1]
        self.assertEqual([json.loads(msg.data) for msg in publisher.messages], [
            {"arm": "left", "angle": 0.7},
            {"arm": "left", "angle": -0.7},
        ])
        self.assertFalse(skill.pick_object(object_name="책")["success"])
        self.assertFalse(skill.place_object(location="책상")["success"])

    def test_state_skill_reports_missing_ros_state_instead_of_mock_values(self):
        module = importlib.import_module("nomi_agent.skills.state_skill")
        result = module.StateSkill().get_robot_state()
        self.assertEqual(set(result), {"success", "message", "data"})
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["error_code"], "state_unavailable")

    def test_state_skill_returns_only_fresh_ros_payload_fields(self):
        module = importlib.import_module("nomi_agent.skills.state_skill")
        node = MockNode()
        with patch.dict(sys.modules, ros_message_stubs()):
            skill = module.StateSkill(node)
        payload = {"status": "connected", "position": {"x": 1.25, "y": -0.5},
                   "rotation": {"yaw": 0.3},
                   "velocity": {"linear": {"x": 0.1, "y": 0.0}, "angular": {"z": 0.02}},
                   "obstacle_scan_status": {"healthy": True}}
        node.subscriptions["/nomi/xlerobot/state"](types.SimpleNamespace(data=json.dumps(payload)))
        result = skill.get_robot_state()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["position"], {"x": 1.25, "y": -0.5})
        self.assertNotIn("battery", result["data"])
        self.assertEqual(result["data"]["yaw"], 0.3)

    def test_location_and_weather_skills_keep_interfaces_without_network_access(self):
        from nomi_agent.skills.location_skill import LocationSkill
        from nomi_agent.skills.weather_skill import WeatherSkill

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        def fake_get(url, **_kwargs):
            if "ipinfo" in url:
                return Response({"country": "KR", "region": "서울", "city": "종로구"})
            if "geocoding" in url:
                return Response({"results": [{"name": "서울", "latitude": 37.5, "longitude": 127.0}]})
            return Response({"current": {
                "temperature_2m": 20,
                "apparent_temperature": 19,
                "relative_humidity_2m": 50,
                "wind_speed_10m": 3,
                "weather_code": 1,
            }})

        with patch("requests.get", side_effect=fake_get) as get:
            location = LocationSkill().get_user_location()
            weather = WeatherSkill().get_weather(city="서울")
        self.assertTrue(location["success"])
        self.assertEqual(location["data"]["city"], "종로구")
        self.assertTrue(weather["success"])
        self.assertEqual(weather["data"]["temperature"], 20)
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
