"""NOMI action safety checks. This module does not execute robot commands."""

import re


REQUIRED_FIELDS = {
    "navigate_to": "location",
    "pick_object": "object_name",
    "place_object": "location",
    "fold_towel": "confirm",
}

QUESTIONS = {
    ("navigate_to", "location"):
        "어디로 이동할까요? 이동할 장소를 구체적으로 알려주세요.",
    ("pick_object", "object_name"):
        "어떤 물체를 집을까요? 물체 이름을 알려주세요.",
    ("place_object", "location"):
        "물체를 어디에 놓을까요? 장소를 구체적으로 알려주세요.",
    ("place_object", "object_name"):
        "테이블에 무엇을 놓을까요? 놓을 물체를 지정하거나 먼저 집을 물체를 알려주세요.",
    ("fold_towel", "confirm"):
        "로봇 팔이 움직입니다. 작업대에 수건이 펴져 있고 팔 주변이 비어 있나요? 시작할까요?",
}

# 단독으로는 대상을 특정할 수 없는 표현들
AMBIGUOUS_VALUES = {
    "그거", "그것", "저거", "이거", "저것", "이것",
    "거기", "저기", "여기", "그쪽", "저쪽", "이쪽",
    "아무거나", "어딘가", "어디", "모르겠어",
    "there", "here", "that", "this", "it", "somewhere",
    "unknown", "none", "null",
    "물체", "물건", "대상", "것", "테이블",
}

PLACE_OBJECT_PATTERN = re.compile(
    r"(?P<object>[가-힣A-Za-z0-9]+)(?:을|를)\s*.*?(?:놓|놔|올려|배치)", re.IGNORECASE
)
ROBOT_COMMAND_PATTERN = re.compile(
    r"이동|움직|전진|후진|회전|돌아|집어|잡아|놓아|놓아\s*줘|놔\s*줘|올려|내려|옮겨|"
    r"그리퍼.*(?:열|닫)|(?:열|닫)아\s*(?:줘|라)|(?:앞으로|뒤로)\s*가|"
    r"수건.*(?:접|개)|(?:수건.*)?(?:접어|개어)(?:\s*줘|\s*봐)?|"
    r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 _-]{0,40}?(?:으로|로|에)\s*(?:가자|가줘|가서|가고|돌아가|이동(?:해|해줘|하세요|해서)?|가(?:[\s.!?]|$))|"
    r"날씨.*알려|위치.*알려|로봇\s*상태.*알려|배터리.*알려",
    re.IGNORECASE,
)

# 명령 대상 동작에 대한 명시적 부정 표현.
# 자연어 전체를 완벽히 이해하는 규칙은 아니므로 별도 테스트가 필요함.
NEGATIVE_PATTERNS = {
    "open_gripper": [
        r"열지\s*마",
        r"열면\s*안",
        r"열지\s*말",
        r"열지\s*않",
        r"열면\s*안\s*돼",
    ],
    "close_gripper": [
        r"닫지\s*마",
        r"닫으면\s*안",
        r"닫지\s*말",
        r"닫지\s*않",
        r"닫으면\s*안\s*돼",
    ],
    "fold_towel": [
        r"접지\s*마",
        r"접으면\s*안",
        r"접지\s*말",
        r"개지\s*마",
        r"개면\s*안",
    ],
}


def _is_ambiguous(value):
    if not isinstance(value, str):
        return True

    normalized = value.strip().lower()
    if not normalized:
        return True

    # 조사만 붙은 단순 지시어도 차단: "그거를", "저기에" 등
    normalized = re.sub(r"[\s.,!?~]+$", "", normalized)
    normalized = re.sub(r"(을|를|은|는|이|가|에|에서|으로|로)$", "", normalized)

    return normalized in AMBIGUOUS_VALUES


def classify_no_tool_command(user_text):
    """Classify a response that produced no tool call.

    Returns (kind, response). Explicit gripper negation is a handled no-op;
    imperative robot/data requests must be clarified instead of marked done.
    """
    text = user_text if isinstance(user_text, str) else ""
    for intent, patterns in NEGATIVE_PATTERNS.items():
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            if intent == "open_gripper":
                action = "그리퍼 열기"
            elif intent == "close_gripper":
                action = "그리퍼 닫기"
            elif intent == "fold_towel":
                action = "수건 접기"
            else:
                action = intent
            return "NON_ACTION", f"알겠습니다. {action} 동작은 수행하지 않겠습니다."
    if ROBOT_COMMAND_PATTERN.search(text):
        return "ROBOT_TASK", "요청한 동작을 선택할 도구 호출이 확인되지 않았습니다. 대상과 목적지를 구체적으로 알려주세요."
    return "CHAT", ""


def extract_navigation_destination(user_text):
    """Extract only an explicit place from a Korean named-navigation request."""
    text = user_text if isinstance(user_text, str) else ""
    if not re.search(r"(?:가자|가줘|가요|가세요|가서|가고|돌아가|이동(?:해|해줘|하세요|해서)?|향해|찾아가|가(?:[\s.!?]|$)|\bgo\b|\bhead\b)", text, re.IGNORECASE):
        return None
    if re.search(r"(?:집|home)", text, re.IGNORECASE):
        return "집"
    charging = re.search(r"충전\s*(?:도크|소)|도킹\s*스테이션|충전기|\bdock\b|\bcharging\s+station\b", text, re.IGNORECASE)
    if charging:
        return charging.group(0)
    if re.search(r"(?:저장된\s*)?(?:목적지|장소)(?:로|으로)?\s*(?:가자|가줘|가요|가세요|가서|가고|가|이동(?:해|해줘|하세요|해서)?)", text):
        return "저장된 목적지"
    match = re.search(
        r"(?P<name>[가-힣A-Za-z0-9][가-힣A-Za-z0-9 _-]{0,78}?)(?:으로|로|에)\s*(?:돌아가|가자|가줘|가요|가세요|가서|가고|가(?:[\s.!?]|$)|이동(?:해|해줘|하세요|해서)?|향해|찾아가)",
        text,
        re.IGNORECASE,
    )
    return match.group("name").strip() if match else None


def validate_actions(user_text, actions, tool_registry=None, *, held_object=False):
    """Return (ok, safe_actions, message).

    This function only validates action data. It never executes tools.
    If any action is invalid, the entire action sequence is rejected.
    """
    if not isinstance(user_text, str) or not isinstance(actions, list):
        return False, [], "명령을 안전하게 확인하지 못했습니다. 다시 말씀해 주세요."

    safe_actions = []

    # Correct a known model confusion deterministically: a named robot destination
    # is not the user's geolocation. Keep it inside the same Registry + Harness path.
    destination = extract_navigation_destination(user_text)
    if destination and isinstance(actions, list) and any(
        isinstance(action, dict) and action.get("intent") == "get_user_location"
        for action in actions
    ) and not any(
        isinstance(action, dict) and action.get("intent") == "get_weather"
        for action in actions
    ):
        actions = [{"intent": "navigate_to", "arguments": {"location": destination}}]

    for action in actions:
        if not isinstance(action, dict):
            return False, [], "작업 형식이 올바르지 않아 실행을 보류했습니다."
        if set(action) - {"intent", "arguments"}:
            return False, [], "작업에 허용되지 않은 필드가 있어 실행을 보류했습니다."

        intent = action.get("intent")
        arguments = action.get("arguments", {})

        if not isinstance(intent, str) or not isinstance(arguments, dict):
            return False, [], "작업 인자를 확인하지 못해 실행을 보류했습니다."

        if tool_registry is not None:
            try:
                spec, normalized_arguments = tool_registry.validate_call(intent, arguments)
            except Exception as exc:
                if getattr(exc, "needs_clarification", False):
                    return False, [], str(exc)
                return False, [], f"작업을 검증하지 못해 실행을 보류했습니다: {exc}"
            intent = spec.name
            arguments = normalized_arguments

        # 부정된 그리퍼 동작이 있으면 해당 동작뿐 아니라 전체 작업을 보류한다.
        patterns = NEGATIVE_PATTERNS.get(intent, [])
        if any(re.search(pattern, user_text, re.IGNORECASE) for pattern in patterns):
            return False, [], "금지한 동작이 포함되어 있어 작업을 실행하지 않았습니다."

        if intent == "fold_towel":
            if arguments.get("confirm") is not True:
                return False, [], QUESTIONS[("fold_towel", "confirm")]

        field = REQUIRED_FIELDS.get(intent)
        if field and intent != "fold_towel":
            value = arguments.get(field)

            if intent == "navigate_to":
                direct_directions = {
                    "앞으로", "전진", "forward", "뒤로", "후진", "backward",
                    "왼쪽", "left", "오른쪽", "right", "왼쪽 회전", "rotate_left",
                    "오른쪽 회전", "rotate_right",
                }
                if value not in direct_directions:
                    requested_destination = extract_navigation_destination(user_text)
                    if requested_destination is None or _is_ambiguous(requested_destination):
                        return False, [], "몇 번 테이블 또는 어떤 저장 목적지로 이동할까요?"
                    compact_requested = re.sub(r"\s+", "", requested_destination).casefold()
                    compact_value = re.sub(r"\s+", "", str(value)).casefold()
                    table_alias = re.fullmatch(r"(\d+)번?테이블", compact_requested)
                    if table_alias:
                        compact_requested = f"테이블{table_alias.group(1)}"
                    matched = (compact_requested == compact_value)
                    if not matched and tool_registry is not None and hasattr(tool_registry, "_resolve_destination"):
                        res_req = tool_registry._resolve_destination(requested_destination)
                        res_val = tool_registry._resolve_destination(str(value))
                        if res_req and res_val and res_req.get("name") == res_val.get("name"):
                            matched = True
                        elif res_req and res_req.get("name") == str(value):
                            matched = True
                        elif res_val and compact_requested in [re.sub(r"\s+", "", a).casefold() for a in res_val.get("aliases", [])]:
                            matched = True
                    if not matched:
                        return False, [], "말씀하신 목적지를 확인할 수 없습니다. 등록된 목적지 이름을 그대로 알려주세요."

            if _is_ambiguous(value):
                question = QUESTIONS.get(
                    (intent, field),
                    "작업에 필요한 정보를 더 구체적으로 알려주세요.",
                )
                return False, [], question

        if intent == "place_object":
            object_match = PLACE_OBJECT_PATTERN.search(user_text)
            object_is_named = bool(object_match and not _is_ambiguous(object_match.group("object")))
            picked_in_plan = any(previous["intent"] == "pick_object" for previous in safe_actions)
            if not (object_is_named or picked_in_plan or held_object):
                return False, [], QUESTIONS[("place_object", "object_name")]

        safe_actions.append({
            "intent": intent,
            "arguments": dict(arguments),
        })

    return True, safe_actions, ""
