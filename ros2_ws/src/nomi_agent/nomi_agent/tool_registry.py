"""Validated registry for all NOMI callable tools.

The registry is the single source of truth for model schemas, argument
validation, execution timeouts, and normalized results.
"""

from __future__ import annotations

import queue
import re
import threading
import json
import math
import os
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from nomi_agent.core.result import SkillResult


class ToolValidationError(ValueError):
    def __init__(self, code: str, message: str, *, needs_clarification: bool = False):
        super().__init__(message)
        self.code = code
        self.needs_clarification = needs_clarification


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    description: str
    kind: str = "string"
    required: bool = True
    max_length: int = 160
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.kind, "description": self.description}
        if self.max_length and self.kind == "string":
            result["maxLength"] = self.max_length
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.choices:
            result["enum"] = list(self.choices)
        return result


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: tuple[ArgumentSpec, ...]
    executor: Callable[[dict[str, Any]], Any]
    timeout_seconds: float = 5.0
    category: str = "read_only"
    requires_completion_evidence: bool = False

    @property
    def required(self) -> list[str]:
        return [arg.name for arg in self.arguments if arg.required]

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {arg.name: arg.schema() for arg in self.arguments},
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


class ToolRegistry:
    """A strict, injectable registry that preserves the existing Skill APIs."""

    ALIASES = {
        "robot.navigate": "navigate_to",
        "robot.gripper.open": "open_gripper",
        "robot.gripper.close": "close_gripper",
        "robot.pick": "pick_object",
        "robot.place": "place_object",
        "robot.state": "get_robot_state",
        "location.query": "get_user_location",
        "weather.query": "get_weather",
        "towel.fold": "fold_towel",
        "towel.status": "get_towel_fold_status",
        "towel.stop": "stop_towel_fold",
    }
    ARGUMENT_ALIASES = {
        "navigate_to": {"destination": "location"},
        "pick_object": {"object": "object_name"},
        "place_object": {"destination": "location"},
    }

    def __init__(self, node=None, skills: dict[str, Any] | None = None):
        self.node = node
        self.skills = skills if skills is not None else self._create_skills(node)
        self._destination_lock = threading.RLock()
        self._destinations = self._load_navigation_destinations()
        self._destinations_subscription = None
        if node is not None and hasattr(node, "create_subscription"):
            from std_msgs.msg import String
            from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
            qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL)
            self._destinations_subscription = node.create_subscription(
                String, "/nomi/navigation_destinations", self._destinations_callback, qos
            )
        self._specs = self._build_specs()
        self.intent_aliases = self.ALIASES

    @staticmethod
    def _create_skills(node):
        # Import ROS-dependent skills only when constructing a production registry.
        from nomi_agent.skills.navigation_skill import NavigationSkill
        from nomi_agent.skills.manipulation_skill import ManipulationSkill
        from nomi_agent.skills.state_skill import StateSkill
        from nomi_agent.skills.location_skill import LocationSkill
        from nomi_agent.skills.weather_skill import WeatherSkill
        from nomi_agent.skills.towel_folding_skill import TowelFoldingSkill

        return {
            "navigation": NavigationSkill(node),
            "manipulation": ManipulationSkill(node),
            "state": StateSkill(node),
            "location": LocationSkill(node),
            "weather": WeatherSkill(node),
            "towel_folding": TowelFoldingSkill(node),
        }

    @staticmethod
    def _load_navigation_destinations() -> dict[str, dict[str, Any]]:
        configured = os.getenv("NOMI_NAVIGATION_DESTINATIONS_FILE")
        path = Path(configured).expanduser() if configured else Path.home() / "XLeRobot/web_control/shared/navigation_destinations.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        destinations = payload.get("destinations", []) if isinstance(payload, dict) else []
        return ToolRegistry._validated_destinations(destinations)

    @staticmethod
    def _validated_destinations(items) -> dict[str, dict[str, Any]]:
        rows = {}
        if not isinstance(items, list):
            return rows
        for item in items[:100]:
            if not isinstance(item, dict):
                continue
            name, x, y = item.get("name"), item.get("x"), item.get("y")
            if (not isinstance(name, str) or not name.strip() or len(name) > 120
                    or isinstance(x, bool) or not isinstance(x, (int, float))
                    or isinstance(y, bool) or not isinstance(y, (int, float))
                    or not math.isfinite(x) or not math.isfinite(y)
                    or not -20 <= x <= 20 or not -20 <= y <= 24):
                continue
            aliases = item.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            clean_name = name.strip()
            new_aliases = [a.strip() for a in aliases[:10] if isinstance(a, str) and a.strip()]
            if clean_name in rows:
                existing_aliases = rows[clean_name].get("aliases", [])
                merged = list(dict.fromkeys(existing_aliases + new_aliases))
                rows[clean_name]["aliases"] = merged
                rows[clean_name]["x"] = float(x)
                rows[clean_name]["y"] = float(y)
                if item.get("kind"):
                    rows[clean_name]["kind"] = str(item.get("kind"))
            else:
                rows[clean_name] = {"name": clean_name, "x": float(x), "y": float(y),
                                    "aliases": new_aliases,
                                    "kind": str(item.get("kind", "saved"))}
        return rows

    def _destinations_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        rows = self._validated_destinations(payload.get("destinations") if isinstance(payload, dict) else None)
        if rows:
            with self._destination_lock:
                self._destinations = rows

    @staticmethod
    def _normalize_destination_text(value: str) -> str:
        text = unicodedata.normalize("NFKC", value).casefold().strip()
        text = re.sub(r"\s+", "", text)
        table = re.fullmatch(r"(\d+)번?테이블", text)
        if table:
            return f"테이블{table.group(1)}"
        return re.sub(r"[.,!?~]", "", text)

    def _resolve_destination(self, requested: str) -> dict[str, Any] | None:
        key = self._normalize_destination_text(requested)
        with self._destination_lock:
            rows = list(self._destinations.values())
        for row in rows:
            if key == self._normalize_destination_text(row["name"]):
                return row
            if any(key == self._normalize_destination_text(alias) for alias in row["aliases"]):
                return row
        return None

    def update_destinations(self, destinations) -> bool:
        rows = self._validated_destinations(destinations)
        if not rows:
            return False
        with self._destination_lock:
            self._destinations = rows
        return True

    def get_navigation_destinations(self) -> dict[str, Any]:
        with self._destination_lock:
            rows = [dict(row) for row in self._destinations.values()]
        return {"success": True, "message": f"등록된 목적지 {len(rows)}개를 조회했습니다. 이동은 실행하지 않았습니다.",
                "data": {"source": "web_navigation_destinations", "frame_id": "xlerobot_map",
                         "destinations": [{k: v for k, v in row.items() if k != "aliases"} for row in rows]}}

    def _build_specs(self) -> dict[str, ToolSpec]:
        s = self.skills

        def call(skill: str, method: str, **fixed):
            inst = s.get(skill)
            if inst is None or not hasattr(inst, method):
                return lambda args: SkillResult.fail(f"{skill} 스킬이 설정되지 않았습니다.", {"error_code": "skill_not_found"})
            fn = getattr(inst, method)
            return lambda args: fn(**({**fixed, **args}))

        specs = [
            ToolSpec("navigate_to", "사용자가 명시한 이동 방향 또는 현재 웹앱에서 동기화된 저장 목적지로 이동한다. 미등록 장소나 모호한 지시는 요청하지 않는다. 사용자의 인터넷 위치를 조회하는 도구가 아니다.",
                     (ArgumentSpec("location", "등록된 목적지 이름 또는 지원되는 이동 방향", max_length=120),),
                     call("navigation", "navigate_to"), 125.0, "robot_motion", True),
            ToolSpec("get_navigation_destinations", "웹앱과 동기화된 저장 목적지 이름과 실제 지도 좌표를 읽기 전용으로 반환한다. 이동을 실행하지 않는다.",
                     (), lambda _args: self.get_navigation_destinations(), 2.0),
            ToolSpec("open_gripper", "왼쪽 그리퍼 열기 명령을 보낸다.", (), call("manipulation", "open_gripper"), 2.0, "robot_actuation", True),
            ToolSpec("close_gripper", "왼쪽 그리퍼 닫기 명령을 보낸다.", (), call("manipulation", "close_gripper"), 2.0, "robot_actuation", True),
            ToolSpec("pick_object", "물체 인식 및 접근이 연결된 경우 지정한 물체를 집는다.",
                     (ArgumentSpec("object_name", "집을 물체의 구체적인 이름", max_length=120),),
                     call("manipulation", "pick_object"), 5.0, "robot_actuation", True),
            ToolSpec("place_object", "현재 들고 있는 물체를 지정한 장소에 놓는다.",
                     (ArgumentSpec("location", "물체를 놓을 구체적인 장소", max_length=120),),
                     call("manipulation", "place_object"), 5.0, "robot_actuation", True),
            ToolSpec("get_robot_state", "ROS /nomi/xlerobot/state 토픽에서 수신한 최신 실제 위치, 방향, 속도 데이터를 반환한다. 배터리 값은 메시지에 있을 때만 표시한다.",
                     (), call("state", "get_robot_state"), 2.0),
            ToolSpec("get_user_location", "사용자가 현재 사용자 위치 자체를 묻거나, 도시가 지정되지 않은 날씨 질문에만 외부 IP 기반 대략 위치를 조회한다. 로봇 이동 목적지나 집으로 가자 같은 주행 요청에는 절대 사용하지 않는다.",
                     (), call("location", "get_user_location"), 7.0),
            ToolSpec("get_weather", "지정한 도시의 현재 날씨를 조회한다. 도시가 없으면 위치 조회 후 날씨를 확인한다.",
                     (ArgumentSpec("city", "날씨를 확인할 도시 이름", required=False, max_length=100),),
                     lambda args: s["weather"].get_weather(args["city"]) if "weather" in s and args.get("city") else SkillResult.fail("날씨를 확인할 도시가 필요합니다.", {"error_code": "missing_argument", "needs_clarification": True}), 12.0),
            ToolSpec("fold_towel", "학습된 정책으로 SO-101 양팔 로봇을 사용하여 수건을 반접기 및 대문접기한다. 자율주행 도착 후 실행하거나 승인 확인 후 호출한다.",
                     (ArgumentSpec("confirm", "실행 승인 여부 (필수: true)", kind="boolean", required=True),
                      ArgumentSpec("event_id", "도착 heartbeat 고유 event_id", kind="string", required=False),
                      ArgumentSpec("profile", "정책 프로필 (original 또는 corrected)", kind="string", required=False, choices=("original", "corrected")),
                      ArgumentSpec("duration_s", "최대 실행 시간(초, 기본 120)", kind="integer", required=False, minimum=30, maximum=300),
                      ArgumentSpec("wait_s", "응답 대기 시간(초, 주행 후 호출 시 60 권장)", kind="integer", required=False, minimum=0, maximum=120)),
                     call("towel_folding", "fold_towel"), 75.0, "robot_actuation", False),
            ToolSpec("get_towel_fold_status", "수건 접기 작업의 진행 상태와 결과(verdict)를 조회한다.",
                     (), call("towel_folding", "get_towel_fold_status"), 15.0, "read_only", False),
            ToolSpec("stop_towel_fold", "수건 접기 작업을 긴급 중단한다.",
                     (), call("towel_folding", "stop_towel_fold"), 15.0, "robot_actuation", False),
        ]
        return {spec.name: spec for spec in specs}

    def get_tool_names(self) -> list[str]:
        return list(self._specs)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [spec.model_schema() for spec in self._specs.values()]

    @staticmethod
    def _normalize_text(value: Any, name: str, max_length: int) -> str:
        if not isinstance(value, str):
            raise ToolValidationError("invalid_type", f"'{name}'은(는) 문자열이어야 합니다.")
        value = value.strip()
        if not value:
            raise ToolValidationError("missing_argument", f"'{name}' 값이 필요합니다.", needs_clarification=True)
        if len(value) > max_length:
            raise ToolValidationError("out_of_range", f"'{name}'은(는) {max_length}자 이내여야 합니다.")
        if re.search(r"[\x00-\x1f\x7f]", value):
            raise ToolValidationError("invalid_value", f"'{name}'에 제어 문자를 사용할 수 없습니다.")
        return value

    def _validate_navigation_location(self, value: str) -> str:
        directions = {
            "앞으로", "전진", "forward", "뒤로", "후진", "backward",
            "왼쪽", "left", "오른쪽", "right", "왼쪽 회전", "rotate_left",
            "오른쪽 회전", "rotate_right",
        }
        if value in directions:
            return value
        normalized = self._resolve_destination(value)
        if normalized:
            return normalized["name"]
        compact = self._normalize_destination_text(value)
        ambiguous = compact in {"테이블", "저쪽", "어딘가", "어디", "저기", "거기", "이쪽", "이거", "그거"}
        question = ("몇 번 테이블로 이동할까요?" if compact == "테이블" else
                    "어디로 이동할까요? 저장된 목적지 이름을 구체적으로 알려주세요.")
        raise ToolValidationError("ambiguous_destination" if ambiguous else "unknown_destination", question,
                                  needs_clarification=True)

    def validate_call(self, name: str, arguments: Any) -> tuple[ToolSpec, dict[str, Any]]:
        if not isinstance(name, str):
            raise ToolValidationError("unknown_tool", "도구 이름은 문자열이어야 합니다.")
        canonical_name = self.ALIASES.get(name, name)
        spec = self._specs.get(canonical_name)
        if spec is None:
            raise ToolValidationError("unknown_tool", f"등록되지 않은 도구입니다: {name}")
        if not isinstance(arguments, dict):
            raise ToolValidationError("invalid_arguments", "도구 인자는 JSON 객체여야 합니다.")
        if any(not isinstance(key, str) for key in arguments):
            raise ToolValidationError("invalid_arguments", "도구 인자 이름은 문자열이어야 합니다.")

        normalized = dict(arguments)
        aliases = self.ARGUMENT_ALIASES.get(canonical_name, {})
        for old, new in aliases.items():
            if old in normalized:
                if new in normalized:
                    raise ToolValidationError("duplicate_argument", f"'{old}'와 '{new}'를 함께 지정할 수 없습니다.")
                normalized[new] = normalized.pop(old)

        allowed = {arg.name for arg in spec.arguments}
        extras = sorted(set(normalized) - allowed)
        if extras:
            raise ToolValidationError("unexpected_argument", f"허용되지 않은 인자입니다: {', '.join(extras)}")

        output: dict[str, Any] = {}
        for arg in spec.arguments:
            if arg.name not in normalized:
                if arg.required:
                    raise ToolValidationError("missing_argument", f"'{arg.name}' 값이 필요합니다.", needs_clarification=True)
                continue
            value = normalized[arg.name]
            if arg.kind == "string":
                value = self._normalize_text(value, arg.name, arg.max_length)
                if canonical_name == "navigate_to" and arg.name == "location":
                    value = self._validate_navigation_location(value)
                elif arg.choices and value not in arg.choices:
                    raise ToolValidationError("invalid_value", f"허용 값: {', '.join(arg.choices)}")
            elif arg.kind in ("number", "integer"):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ToolValidationError("invalid_type", f"'{arg.name}'은(는) 숫자여야 합니다.")
                if arg.kind == "integer" and not isinstance(value, int):
                    raise ToolValidationError("invalid_type", f"'{arg.name}'은(는) 정수여야 합니다.")
                if arg.minimum is not None and value < arg.minimum or arg.maximum is not None and value > arg.maximum:
                    raise ToolValidationError("out_of_range", f"'{arg.name}' 값이 허용 범위를 벗어났습니다.")
            elif arg.kind == "boolean" and not isinstance(value, bool):
                raise ToolValidationError("invalid_type", f"'{arg.name}'은(는) 참/거짓 값이어야 합니다.")
            output[arg.name] = value
        return spec, output

    def validate_plan(self, actions: Any) -> list[dict[str, Any]]:
        if not isinstance(actions, list) or not actions:
            raise ToolValidationError("empty_plan", "실행할 작업을 찾지 못했습니다.", needs_clarification=True)
        if len(actions) > 5:
            raise ToolValidationError("plan_too_long", "한 번에 실행할 수 있는 작업은 최대 5단계입니다.")
        validated = []
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict) or set(action) - {"intent", "arguments"}:
                raise ToolValidationError("invalid_action", f"{index}단계 작업 형식이 올바르지 않습니다.")
            name = action.get("intent")
            spec, args = self.validate_call(name, action.get("arguments", {}))
            validated.append({"intent": spec.name, "arguments": args})
        return validated

    def execute(self, intent: str, arguments: dict | None = None, *, cancel_event: threading.Event | None = None) -> SkillResult:
        try:
            spec, normalized = self.validate_call(intent, arguments if arguments is not None else {})
        except ToolValidationError as exc:
            return SkillResult.fail(str(exc), {"error_code": exc.code, "needs_clarification": exc.needs_clarification})

        if cancel_event is not None and cancel_event.is_set():
            return SkillResult.fail("작업이 취소되어 도구를 실행하지 않았습니다.", {"error_code": "cancelled"})

        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def invoke():
            try:
                result_queue.put((True, spec.executor(normalized)))
            except BaseException as exc:
                result_queue.put((False, exc))

        thread = threading.Thread(target=invoke, name=f"nomi-tool-{spec.name}", daemon=True)
        thread.start()
        try:
            ok, raw_result = result_queue.get(timeout=spec.timeout_seconds)
        except queue.Empty:
            if spec.category.startswith("robot_"):
                self.emergency_stop()
            return SkillResult.fail(
                f"도구 실행 시간 초과({spec.timeout_seconds:g}초): {spec.name}",
                {"error_code": "timeout", "execution_outcome": "unknown_after_timeout", "tool": spec.name},
            )

        if not ok:
            return SkillResult.fail(
                f"도구 실행 중 예외가 발생했습니다: {raw_result}",
                {"error_code": "tool_exception", "exception_type": type(raw_result).__name__, "tool": spec.name},
            )

        result = self._normalize_result(raw_result, spec)
        if cancel_event is not None and cancel_event.is_set():
            return SkillResult.fail("도구 실행 중 작업이 취소되었습니다.", {"error_code": "cancelled", "tool": spec.name})
        return result

    def _normalize_result(self, raw_result: Any, spec: ToolSpec) -> SkillResult:
        if isinstance(raw_result, SkillResult):
            result = raw_result
        elif isinstance(raw_result, dict):
            if not isinstance(raw_result.get("success"), bool) or not isinstance(raw_result.get("message", ""), str):
                return SkillResult.fail("도구 결과 형식이 올바르지 않습니다.", {"error_code": "invalid_result", "tool": spec.name})
            data = raw_result.get("data", {})
            if not isinstance(data, dict):
                return SkillResult.fail("도구 결과의 data는 JSON 객체여야 합니다.", {"error_code": "invalid_result", "tool": spec.name})
            result = SkillResult(raw_result["success"], raw_result.get("message", ""), data)
        else:
            return SkillResult.fail("도구가 표준 결과 형식이 아닌 값을 반환했습니다.", {"error_code": "invalid_result", "tool": spec.name})

        if spec.name == "get_robot_state" and result.success and result.data.get("connection") == "mock":
            return SkillResult.fail(
                "현재 로봇 상태 연결이 없어 모의 상태값만 제공됩니다. 실제 상태 조회는 확인되지 않았습니다.",
                {**result.data, "error_code": "state_unverified", "source": "mock"},
            )

        if result.success and spec.requires_completion_evidence and result.data.get("completion_verified") is not True:
            return SkillResult.fail(
                f"{result.message} 완료 상태를 확인하지 못해 성공으로 처리하지 않았습니다.",
                {**result.data, "command_dispatched": True, "completion_verified": False, "error_code": "completion_unverified", "tool": spec.name},
            )
        return result

    def emergency_stop(self) -> None:
        navigation = self.skills.get("navigation")
        stop = getattr(navigation, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                if self.node:
                    try:
                        self.node.get_logger().error(f"비상 정지 요청 실패: {exc}")
                    except Exception:
                        pass
        towel = self.skills.get("towel_folding")
        stop_towel = getattr(towel, "stop_towel_fold", None)
        if callable(stop_towel):
            try:
                stop_towel()
            except Exception as exc:
                if self.node:
                    try:
                        self.node.get_logger().error(f"수건 접기 비상 정지 요청 실패: {exc}")
                    except Exception:
                        pass
