"""Unit and integration tests for towel folding skill and client."""

import unittest
from unittest.mock import MagicMock, patch

from nomi_agent import towel_client
from nomi_agent.safety_guard import classify_no_tool_command, validate_actions
from nomi_agent.skills.towel_folding_skill import TowelFoldingSkill
from nomi_agent.tool_registry import ToolRegistry, ToolValidationError


class TowelClientTests(unittest.TestCase):
    def test_load_tools_returns_three_towel_tools(self):
        tools = towel_client.load_tools()
        names = [t["function"]["name"] for t in tools]
        self.assertIn("fold_towel", names)
        self.assertIn("get_towel_fold_status", names)
        self.assertIn("stop_towel_fold", names)

    @patch("socket.create_connection")
    @patch("urllib.request.urlopen")
    def test_call_dispatches_with_bearer_token(self, mock_urlopen, mock_socket):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"started": true, "state": "running", "job_id": "test_123"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch.dict("os.environ", {"TOWEL_API_URL": "http://192.168.0.23:8100", "TOWEL_API_TOKEN": "secret-key"}):
            res = towel_client.call("POST", "/fold", {"confirm": True, "wait_s": 10})

        self.assertTrue(res.get("started"))
        self.assertEqual(res.get("job_id"), "test_123")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "http://192.168.0.23:8100/fold")
        self.assertEqual(req.headers.get("Authorization"), "Bearer secret-key")


class TowelFoldingSkillTests(unittest.TestCase):
    def setUp(self):
        self.skill = TowelFoldingSkill()

    @patch("nomi_agent.towel_client.call")
    def test_fold_towel_success(self, mock_call):
        mock_call.return_value = {"started": True, "state": "running", "job_id": "job_001", "phase": "executing"}
        result = self.skill.fold_towel(confirm=True, wait_s=60)
        self.assertTrue(result.success)
        self.assertEqual(result.data["job_id"], "job_001")
        self.assertTrue(result.data["completion_verified"])

    @patch("nomi_agent.towel_client.call")
    def test_fold_towel_rejection(self, mock_call):
        mock_call.return_value = {"started": False, "error": "수건 접기 거부: confirm 필요"}
        result = self.skill.fold_towel(confirm=False)
        self.assertFalse(result.success)
        self.assertIn("수건 접기 거부", result.message)

    @patch("nomi_agent.towel_client.call")
    def test_get_towel_fold_status(self, mock_call):
        mock_call.return_value = {
            "state": "finished",
            "phase": "done",
            "job_id": "job_001",
            "verdict": "book_fold",
            "verdict_ok": True,
        }
        result = self.skill.get_towel_fold_status()
        self.assertTrue(result.success)
        self.assertEqual(result.data["verdict"], "book_fold")

    @patch("nomi_agent.towel_client.call")
    def test_stop_towel_fold(self, mock_call):
        mock_call.return_value = {"stopped": True, "message": "정지 완료"}
        result = self.skill.stop_towel_fold()
        self.assertTrue(result.success)
        mock_call.assert_called_with("POST", "/stop", {})


class TowelRegistryAndSafetyTests(unittest.TestCase):
    def setUp(self):
        self.mock_towel = MagicMock()
        self.mock_nav = MagicMock()
        self.skills = {
            "navigation": self.mock_nav,
            "manipulation": MagicMock(),
            "state": MagicMock(),
            "location": MagicMock(),
            "weather": MagicMock(),
            "towel_folding": self.mock_towel,
        }
        self.registry = ToolRegistry(skills=self.skills)

    def test_towel_tools_in_registry_schemas(self):
        schemas = self.registry.get_tool_schemas()
        names = [s["function"]["name"] for s in schemas]
        self.assertIn("fold_towel", names)
        self.assertIn("get_towel_fold_status", names)
        self.assertIn("stop_towel_fold", names)

    def test_emergency_stop_calls_towel_stop(self):
        self.registry.emergency_stop()
        self.mock_nav.stop.assert_called_once()
        self.mock_towel.stop_towel_fold.assert_called_once()

    def test_validate_call_fold_towel_requires_confirm(self):
        with self.assertRaises(ToolValidationError) as ctx:
            self.registry.validate_call("fold_towel", {})
        self.assertEqual(ctx.exception.code, "missing_argument")

    def test_validate_call_fold_towel_valid_args(self):
        spec, args = self.registry.validate_call("fold_towel", {"confirm": True, "wait_s": 60})
        self.assertEqual(spec.name, "fold_towel")
        self.assertEqual(args["confirm"], True)
        self.assertEqual(args["wait_s"], 60)

    def test_safety_guard_negative_phrase(self):
        kind, reply = classify_no_tool_command("수건 접지 마")
        self.assertEqual(kind, "NON_ACTION")
        self.assertIn("수건 접기 동작은 수행하지 않겠습니다", reply)

    def test_safety_guard_validate_actions_unconfirmed(self):
        actions = [{"intent": "fold_towel", "arguments": {"confirm": False}}]
        ok, safe, msg = validate_actions("수건 접어줘", actions, self.registry)
        self.assertFalse(ok)
        self.assertIn("작업대에 수건이 펴져 있고", msg)

    def test_safety_guard_validate_actions_confirmed(self):
        actions = [{"intent": "fold_towel", "arguments": {"confirm": True, "wait_s": 60}}]
        ok, safe, msg = validate_actions("수건 접어줘", actions, self.registry)
        self.assertTrue(ok)
        self.assertEqual(len(safe), 1)
        self.assertEqual(safe[0]["intent"], "fold_towel")


if __name__ == "__main__":
    unittest.main()
