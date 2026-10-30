import json
import re
import os
import hashlib

from nomi_agent.llm_provider import LLMProvider, MODEL_PROFILES, initial_config


class LLMClient:

    def __init__(
        self,
        router_model="qwen2.5:7b",
        worker_model="hermes3:8b",
        max_history=12,
        profile=None,
    ):
        self.max_history = int(os.getenv("MAX_HISTORY", str(max_history)))
        if profile is None:
            self.profile, config = initial_config()
            self._apply_config(config, self.profile)
        else:
            self.set_profile(profile)

        # 사용자 ↔ NOMI 대화 기록
        self.conversation_history = []

        # 마지막으로 수행하려고 했던 작업
        # 후속 명령 처리에 사용한다.
        self.last_intent = ""
        self.last_arguments = {}


    def set_profile(self, profile):
        if profile not in MODEL_PROFILES:
            raise ValueError(f"지원하지 않는 모델 프로필: {profile}")
        old_config = MODEL_PROFILES.get(self.profile) if hasattr(self, "profile") else None
        old_profile = getattr(self, "profile", None)
        self._apply_config(MODEL_PROFILES[profile], profile)
        try:
            if self.router_provider is not None:
                self.router_provider.verify_available()
            if self.worker_provider is not None and self.worker_provider is not self.router_provider:
                self.worker_provider.verify_available()
        except Exception:
            if old_config and old_profile:
                self._apply_config(old_config, old_profile)
            raise

    def _apply_config(self, config, profile=None):
        self.profile = profile or self._profile_for_config(config)
        if self.profile == "qwen35_single":
            provider = LLMProvider(config["router_provider"], config["router_model"])
            router = worker = provider
        else:
            router = LLMProvider(config["router_provider"], config["router_model"])
            worker = LLMProvider(config["worker_provider"], config["worker_model"])
        self.router_model = config["router_model"]
        self.worker_model = config["worker_model"]
        self.router_provider = router
        self.worker_provider = worker
        self.is_single_model_profile = self.profile == "qwen35_single"
        self.single_model_provider = router if self.is_single_model_profile else None
        self.last_model_call_count = 0

    @staticmethod
    def _profile_for_config(config):
        for name, candidate in MODEL_PROFILES.items():
            if candidate == config:
                return name
        return "custom"

    # =========================================================
    # Conversation Memory
    # =========================================================

    def add_user_message(self, text: str):
        self.conversation_history.append(
            {
                "role": "user",
                "content": text,
            }
        )

        self._trim_history()

    def add_assistant_message(self, text: str):
        if not text:
            return

        self.conversation_history.append(
            {
                "role": "assistant",
                "content": text,
            }
        )

        self._trim_history()

    def _trim_history(self):
        if len(self.conversation_history) > self.max_history:
            self.conversation_history = (
                self.conversation_history[-self.max_history:]
            )

    def clear_history(self):
        self.conversation_history = []
        self.last_intent = ""
        self.last_arguments = {}

    def get_history(self):
        return list(self.conversation_history)

    # =========================================================
    # Qwen Router
    # =========================================================

    def route_command(self, user_text: str) -> dict:

        self.last_model_call_count = 0

        system_prompt = """
당신은 NOMI 로봇 AI 시스템의 Router이다.

사용자의 발화를 분석해서 CHAT 또는 TASK로 분류한다.

반드시 JSON 하나만 반환한다.

JSON 형식:

{
  "route": "CHAT" 또는 "TASK",
  "type": "chat" 또는 "task",
  "intent": "첫 번째 도구 이름 또는 빈 문자열",
  "arguments": {},
  "actions": [
    {"intent": "도구 이름", "arguments": {}}
  ],
  "reply": "CHAT일 때 답변"
}

==================================================
사용 가능한 TASK
==================================================

navigate_to
open_gripper
close_gripper
pick_object
place_object
get_robot_state
get_user_location
get_weather
fold_towel
get_towel_fold_status
stop_towel_fold

==================================================
TASK 규칙
==================================================

실제 정보를 확인하거나 로봇의 동작이 필요한 요청은
반드시 TASK로 분류한다.

한 문장에 서로 다른 작업이 둘 이상 있으면 모든 작업을
사용자가 말한 순서대로 actions 배열에 넣는다. 최대 5개다.
intent/arguments에는 첫 작업도 함께 넣어 이전 버전과 호환한다.
각 작업의 인자만 적고 실행 결과를 꾸며내지 않는다.
예: "날씨와 로봇 상태를 알려줘"는 아래처럼 둘 다 넣는다.
{
  "route": "TASK", "type": "task",
  "intent": "get_weather", "arguments": {},
  "actions": [
    {"intent": "get_weather", "arguments": {}},
    {"intent": "get_robot_state", "arguments": {}}
  ],
  "reply": ""
}
한 작업만 요청해도 actions 배열에 작업 하나를 넣는다.

날씨, 위치, 로봇 상태, 이동, 그리퍼,
물체 집기, 물체 놓기는 모두 TASK이다.

이동 목적지와 사용자 인터넷 위치를 혼동하지 않는다.
'집으로 가자', '집에 가줘', '집으로 돌아가', '집으로 이동해',
'충전소로 이동해', '저장된 목적지로 가줘'처럼 이동 동사와 장소가
함께 있으면 반드시 navigate_to(location=사용자가 말한 장소 이름)로 분류한다.
이 요청에서 get_user_location을 호출하지 않는다.
get_user_location은 사용자가 자기 위치를 직접 묻거나, 도시가 없는 날씨
요청에만 쓴다. 저장된 장소 여부는 웹 내비게이션이 확인하며, 좌표를
추측하거나 모델이 생성하지 않는다.

==================================================
날씨
==================================================

예:

오늘 날씨 어때?
지금 날씨 알려줘
비 와?
현재 기온 몇 도야?
서울 날씨 알려줘
종로구 날씨 어때?

모두:

intent = get_weather

도시가 있으면:

{
  "route": "TASK",
  "type": "task",
  "intent": "get_weather",
  "arguments": {
    "city": "서울"
  },
  "reply": ""
}

도시가 없으면:

{
  "route": "TASK",
  "type": "task",
  "intent": "get_weather",
  "arguments": {},
  "reply": ""
}

도시가 없다는 이유로 CHAT으로 분류하지 않는다.

==================================================
사용자 위치
==================================================

예:

내 위치 알려줘
내가 어디 있어?
현재 위치 확인해줘
인터넷으로 내 위치 알아봐

intent:

get_user_location

==================================================
로봇 상태
==================================================

예:

로봇 상태 알려줘
로봇 괜찮아?
현재 로봇 상태 확인해줘

intent:

get_robot_state

==================================================
그리퍼
==================================================

그리퍼를 열어:

open_gripper

그리퍼를 닫아:

close_gripper

==================================================
물체 집기
==================================================

예:

컵 집어
수건 집어
분홍색 수건 집어

intent:

pick_object

arguments:

{
  "object_name": "분홍색 수건"
}

==================================================
물체 놓기
==================================================

예:

식탁에 놔
책상에 놓아
저기에 놔

intent:

place_object

arguments:

{
  "location": "식탁"
}

==================================================
이동
==================================================

예:

주방으로 가
거실로 이동해
현관으로 가
집으로 가자
집에 가줘
집으로 돌아가
집으로 이동해
충전소로 이동해

목적지 이동과 사용자 위치 조회를 혼동하지 않는다. 목적지 이동은
navigate_to(location=사용자가 말한 장소 이름)로 호출하고,
get_user_location은 사용자가 자기 위치를 직접 묻거나 도시 없는 날씨 요청에만 쓴다.
"저장된 목적지로 가줘"처럼 이름이 정해지지 않으면 사용자에게 목적지 이름을 묻는다.

intent:

navigate_to

arguments:

{
  "location": "주방"
}

==================================================
수건 접기
==================================================

예:

수건 접어줘
수건 접어봐
수건 개어줘

intent:

fold_towel

arguments:

{
  "confirm": true,
  "wait_s": 60
}

복합 작업 예 ("책상에 가서 수건 접어줘"):

{
  "route": "TASK",
  "type": "task",
  "intent": "navigate_to",
  "arguments": {
    "location": "책상"
  },
  "actions": [
    {"intent": "navigate_to", "arguments": {"location": "책상"}},
    {"intent": "fold_towel", "arguments": {"confirm": true, "wait_s": 60}}
  ],
  "reply": ""
}

수건 접기 상태 조회 ("수건 접는 상태 확인해줘", "수건 다 접었어?"):

intent: get_towel_fold_status

수건 접기 정지 ("수건 접기 멈춰", "수건 접는 거 중단해"):

intent: stop_towel_fold

==================================================
대화 문맥
==================================================

현재 발화만 보지 말고 대화 기록을 참고한다.

예:

사용자:
분홍색 수건 집어

NOMI:
분홍색 수건을 집겠습니다.

사용자:
식탁에 놔

두 번째 발화는 이전 작업의 후속 작업이다.

따라서:

{
  "route": "TASK",
  "type": "task",
  "intent": "place_object",
  "arguments": {
    "location": "식탁"
  },
  "reply": ""
}

또 다른 예:

사용자:
오늘 날씨 어때?

NOMI:
날씨를 확인할게요.

사용자:
그래 확인해줘

두 번째 발화도 날씨 요청의 후속 발화이므로:

{
  "route": "TASK",
  "type": "task",
  "intent": "get_weather",
  "arguments": {},
  "reply": ""
}

==================================================
CHAT
==================================================

실제 도구가 필요 없는 일반 대화만 CHAT이다.

예:

안녕
고마워
너는 누구야?
뭐 하고 있어?
재미있는 이야기 해줘

==================================================
중요
==================================================

다음은 반드시 TASK:

날씨
위치
로봇 상태
이동
그리퍼
물체 집기
물체 놓기

다음과 같은 후속 표현은 이전 대화와 연결한다.

그래
확인해줘
그거 해줘
그럼?
거기로 가
거기에 놔
그것도 해줘

반드시 유효한 JSON만 반환한다.
JSON 이외의 설명은 출력하지 않는다.
"""

        self.add_user_message(user_text)

        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]

        messages.extend(self.conversation_history)

        self.last_model_call_count += 1
        content = self.router_provider.chat(
            messages,
            json_mode=True,
            think=False,
            keep_alive="10m",
            options={"temperature": 0},
        )

        try:
            result = json.loads(content)

        except (json.JSONDecodeError, TypeError):
            return {
                "route": "INVALID",
                "type": "invalid",
                "intent": "",
                "arguments": {},
                "actions": [],
                "reply": "",
                "error": "router_invalid_json",
            }

        if not isinstance(result, dict):
            return {"route": "INVALID", "actions": [], "reply": "", "error": "router_not_object"}

        route = result.get("route")
        intent = result.get("intent", "")
        arguments = result.get("arguments", {})
        actions = result.get("actions", [])
        if not isinstance(route, str) or route not in {"CHAT", "TASK"}:
            return {"route": "INVALID", "actions": [], "reply": "", "error": "router_invalid_route"}
        if not isinstance(actions, list):
            return {"route": "INVALID", "actions": [], "reply": "", "error": "router_actions_not_list"}
        if not isinstance(arguments, dict):
            return {"route": "INVALID", "actions": [], "reply": "", "error": "router_arguments_not_object"}

        # Preserve the full proposed plan. The Harness/Registry must reject bad
        # or unknown calls instead of silently dropping them here.
        if route == "TASK" and not actions and intent:
            actions = [{"intent": intent, "arguments": arguments}]
        if route == "TASK" and actions:
            first = actions[0]
            if isinstance(first, dict):
                self.last_intent = first.get("intent", "")
                self.last_arguments = first.get("arguments", {}) if isinstance(first.get("arguments", {}), dict) else {}

        reply = result.get("reply", "")
        if route == "CHAT" and not isinstance(reply, str):
            return {"route": "INVALID", "actions": [], "reply": "", "error": "router_reply_not_string"}
        return {
            "route": route,
            "type": result.get("type", "task" if route == "TASK" else "chat"),
            "intent": intent,
            "arguments": arguments,
            "actions": actions,
            "reply": reply,
        }

    # =========================================================
    # Hermes 3 Tool Calling
    # =========================================================

    def _build_hermes_tools_prompt(
        self,
        tool_schemas: list[dict],
    ) -> str:

        tools_json = json.dumps(
            tool_schemas,
            ensure_ascii=False,
        )

        return f"""
You are a function calling AI model.

You are provided with function signatures inside
<tools></tools> XML tags.

You may call one or more functions to assist with
the user's request.

Do not ask the user for information that can be obtained
by using an available tool.

Here are the available tools:

<tools>
{tools_json}
</tools>

For every function call, return a JSON object inside
<tool_call></tool_call>.

The JSON format must be:

<tool_call>
{{"name": "function_name", "arguments": {{}}}}
</tool_call>

Important rules:

1. If a tool is needed, call the tool.
2. Do not simply explain that a tool could be used.
3. Do not ask the user to provide information that
   an available tool can obtain.
4. Call one tool at a time.
5. After receiving a <tool_response>, continue the task.
6. When the task is complete, answer the user in Korean.
7. Do not generate fake tool results.
8. Do not stop before the task is completed.
9. A robot destination is not the user's internet location. For phrases such as '집으로 가자', '집에 가줘', '집으로 돌아가', or '충전소로 이동해', call navigate_to with the place name as location. Never call get_user_location for a navigation request.
10. Use get_user_location only when the user asks for their own current location or asks for weather without naming a city. Never invent destination coordinates; the web navigation module resolves the saved place.

==================================================
Weather rule
==================================================

If the user asks for weather and the city is unknown:

First call:

<tool_call>
{{"name": "get_user_location", "arguments": {{}}}}
</tool_call>

After receiving the location result, use its city
to call:

<tool_call>
{{"name": "get_weather", "arguments": {{"city": "도시"}}}}
</tool_call>

Never ask the user for their city when
get_user_location is available.

==================================================
Towel folding rules
==================================================

1. 이동 요청 없이 '수건 접어줘', '수건 개어줘'라고만 하면 이동하지 않고 바로 fold_towel(confirm=true, wait_s=60)을 호출한다.
2. 특정 장소로 이동해서 수건 접기를 요청한 경우(예: '책상에 가서 수건 접어줘')에만 먼저 navigate_to로 이동하고, 도착 결과를 돌려받은 뒤에 fold_towel을 호출한다.
3. fold_towel은 confirm=true, wait_s=60으로 호출한다.
4. 수건 접기가 진행 중(state가 running)일 때는 주행 도구를 호출하지 않는다.
5. 시작 후 get_towel_fold_status를 5~10초마다 확인하고, state가 finished/failed/stopped가 되면 verdict로 결과를 보고한다. verdict가 book_fold가 아니면 '대문접기까지 되지 않은 것으로 판정'이라고 보고하며 성공을 단정하지 않는다.
6. 사용자가 '멈춰'라고 하면 stop_towel_fold를 호출한다.

==================================================
Function call schema
==================================================

Each function call must have:

name
arguments

Both must be valid JSON.

Example:

<tool_call>
{{"name": "get_user_location", "arguments": {{}}}}
</tool_call>
"""

    # =========================================================
    # Hermes XML Tool Call Parser
    # =========================================================

    def _extract_tool_calls(self, text: str):

        if not text:
            return []

        pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"

        matches = re.findall(
            pattern,
            text,
            flags=re.DOTALL,
        )

        tool_calls = []

        for match in matches:

            try:
                data = json.loads(match)

            except json.JSONDecodeError:
                continue

            name = data.get("name")

            arguments = data.get(
                "arguments",
                {},
            )

            if not name:
                continue

            if not isinstance(arguments, dict):
                arguments = {}

            tool_calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                }
            )

        return tool_calls

    # =========================================================
    # Deterministic Weather Workflow
    # =========================================================

    def _run_weather_workflow(
        self,
        city,
        tool_executor,
    ):

        # -----------------------------------------------------
        # 도시가 이미 있으면 바로 날씨 조회
        # -----------------------------------------------------

        if city:

            print("========================================")
            print(
                f"[NOMI] Weather workflow: "
                f"get_weather(city={city})"
            )

            result = tool_executor(
                "get_weather",
                {
                    "city": city,
                },
            )

            return result

        # -----------------------------------------------------
        # 도시가 없으면 위치 조회
        # -----------------------------------------------------

        print("========================================")
        print(
            "[NOMI] Weather workflow: "
            "get_user_location()"
        )

        location_result = tool_executor(
            "get_user_location",
            {},
        )

        if hasattr(location_result, "success"):

            if not location_result.success:

                return location_result

            data = location_result.data or {}

        elif isinstance(location_result, dict):

            data = location_result.get(
                "data",
                {},
            )

        else:

            return location_result

        # -----------------------------------------------------
        # 위치 결과에서 city 찾기
        # -----------------------------------------------------

        # 현재 ToolRegistry 결과는 다음 구조일 수 있다.
        #
        # data = {
        #     "data": {
        #         "country": "KR",
        #         "region": "Seoul",
        #         "city": "Seoul"
        #     }
        # }
        #
        # 또는 정상적으로 평탄화된 경우:
        #
        # data = {
        #     "country": "KR",
        #     "region": "Seoul",
        #     "city": "Seoul"
        # }

        location_data = data.get("data")

        if isinstance(location_data, dict):
            city = location_data.get("city")
        else:
            city = data.get("city")

        if not city:
            city = data.get("region")

        if not city:

            message = getattr(
                location_result,
                "message",
                "",
            )

            print(
                "[NOMI] 위치 결과에 city가 없습니다."
            )

            return {
                "success": False,
                "message": (
                    "사용자 위치에서 도시 정보를 "
                    "확인하지 못했습니다. "
                    f"{message}"
                ),
            }

        print(
            f"[NOMI] 사용자 위치에서 도시 확인: {city}"
        )

        # -----------------------------------------------------
        # 도시 확보 → 날씨 조회
        # -----------------------------------------------------

        print("========================================")
        print(
            f"[NOMI] Weather workflow: "
            f"get_weather(city={city})"
        )

        return tool_executor(
            "get_weather",
            {
                "city": city,
            },
        )

    # =========================================================
    # Hermes Worker
    # =========================================================

    def run_worker_with_tools(
        self,
        user_text: str,
        initial_intent: str,
        initial_arguments: dict,
        tool_schemas: list[dict],
        tool_executor,
        max_rounds: int = 8,
        initial_actions: list[dict] | None = None,
    ) -> str:

        # Multiple explicit tasks are executed in router order. Stop the
        # sequence after a failed action to avoid unsafe dependent follow-ups.
        if initial_actions and len(initial_actions) > 1:
            results = []
            for action in initial_actions[:5]:
                action_intent = action.get("intent", "")
                action_arguments = action.get("arguments", {}) or {}
                if action_intent == "get_weather":
                    result = self._run_weather_workflow(
                        city=action_arguments.get("city"),
                        tool_executor=tool_executor,
                    )
                else:
                    result = tool_executor(action_intent, action_arguments)
                if hasattr(result, "success"):
                    item = {"intent": action_intent, "success": result.success, "message": result.message, "data": result.data}
                elif isinstance(result, dict):
                    item = {"intent": action_intent, **result}
                else:
                    item = {"intent": action_intent, "success": True, "message": str(result)}
                results.append(item)
                if item.get("success") is False:
                    break
            summary = {
                "success": all(item.get("success", True) for item in results),
                "message": "순서대로 요청한 작업 결과입니다.",
                "data": {"actions": results},
            }
            return self._generate_final_response(
                user_text=user_text,
                intent=", ".join(item["intent"] for item in results),
                tool_result=summary,
            )

        # =====================================================
        # 중요한 결정적 처리
        # =====================================================

        # Qwen이 get_weather를 판단했고
        # 도시가 없는 경우에는 Hermes에게 맡기지 않는다.
        #
        # Python이 직접:
        #
        # get_user_location
        #        ↓
        # city
        #        ↓
        # get_weather
        #
        # 를 실행한다.

        if initial_intent == "get_weather":

            city = (
                initial_arguments.get("city")
                if initial_arguments
                else None
            )

            weather_result = self._run_weather_workflow(
                city=city,
                tool_executor=tool_executor,
            )

            return self._generate_final_response(
                user_text=user_text,
                intent=initial_intent,
                tool_result=weather_result,
            )

        # =====================================================
        # Hermes 3 Tool Calling
        # =====================================================

        hermes_system_prompt = self._build_hermes_tools_prompt(
            tool_schemas
        )

        messages = [
            {
                "role": "system",
                "content": hermes_system_prompt,
            }
        ]

        # 이전 대화 기억
        #
        # 현재 user_text도 이미
        # conversation_history에 들어가 있다.
        messages.extend(self.conversation_history)

        # Router 결과를 Hermes에게 알려준다.
        messages.append(
            {
                "role": "user",
                "content": (
                    "Router가 분석한 작업 정보:\n"
                    f"intent={initial_intent}\n"
                    f"arguments="
                    f"{json.dumps(initial_arguments, ensure_ascii=False)}\n\n"
                    f"사용자의 실제 요청:\n"
                    f"{user_text}"
                ),
            }
        )

        for round_index in range(max_rounds):

            print("========================================")
            print(
                f"[Hermes] Worker Round "
                f"{round_index + 1}/{max_rounds}"
            )

            self.last_model_call_count = getattr(self, "last_model_call_count", 0) + 1
            content = self.worker_provider.chat(
                messages,
                tools=tool_schemas if self.worker_provider.provider == "ark" else None,
            )

            print("========================================")
            print("[Hermes] Raw Response:")
            print(content)

            tool_calls = self._extract_tool_calls(
                content
            )

            # =================================================
            # Tool Call 없음
            # =================================================

            if not tool_calls:

                # <tool_call>이 없으면
                # 일반 최종 답변으로 처리한다.
                return content

            # =================================================
            # Assistant Tool Call 메시지 저장
            # =================================================

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                }
            )

            # =================================================
            # Tool 실행
            # =================================================

            for call in tool_calls:

                tool_name = call["name"]
                arguments = call["arguments"]

                print("========================================")
                print(
                    f"[Hermes] Tool Call: "
                    f"{tool_name}"
                )
                print(
                    f"[Hermes] Arguments: "
                    f"{arguments}"
                )

                try:

                    result = tool_executor(
                        tool_name,
                        arguments,
                    )

                    if hasattr(result, "success"):

                        result_data = {
                            "success": result.success,
                            "message": result.message,
                            "data": result.data,
                        }

                    elif isinstance(result, dict):

                        result_data = result

                    else:

                        result_data = {
                            "success": True,
                            "message": str(result),
                        }

                except Exception as exc:

                    result_data = {
                        "success": False,
                        "message": (
                            f"Tool 실행 오류: {exc}"
                        ),
                    }

                print("========================================")
                print(
                    f"[Hermes] Tool Result: "
                    f"{result_data}"
                )

                # Hermes 3 공식 형식에 맞춰
                # tool response를 XML 형태로 전달한다.
                tool_response = (
                    "<tool_response>\n"
                    + json.dumps(
                        {
                            "name": tool_name,
                            "content": result_data,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n</tool_response>"
                )

                messages.append(
                    {
                        "role": "tool",
                        "content": tool_response,
                        "tool_name": tool_name,
                    }
                )

        return (
            "도구 호출이 너무 많이 반복되어 "
            "작업을 종료했습니다."
        )

    def run_single_model_with_tools(
        self,
        user_text: str,
        tool_schemas: list[dict],
        tool_executor,
        *,
        cancel_event=None,
        correlation_id="nomi-task",
        max_tool_calls: int = 5,
        max_rounds: int = 8,
    ) -> str:
        """Interpret a request and call validated tools without a router pass.

        The first model request includes the real ToolRegistry schemas. Further
        model calls happen only when a tool result is needed to choose the next
        action; this is one model profile, not two calls to the same model under
        router/worker labels.
        """
        if not self.is_single_model_profile or self.single_model_provider is None:
            raise RuntimeError("single-model runner requires the qwen35_single profile")

        self.last_model_call_count = 0
        active_model = getattr(self, "router_model", "qwen3.5:9b")
        active_provider = getattr(getattr(self, "router_provider", None), "provider", "ollama")
        system_prompt = (
            f"현재 실행 모델은 {active_model} ({active_provider})다. "
            "사용자가 현재 모델을 물으면 이 값을 그대로 정확히 알려준다. "
            "당신은 NOMI 로봇 도구 선택기다. 사용자 요청을 해석하고 제공된 도구만 호출한다. "
            "도구 이름과 인자는 등록된 스키마를 정확히 따른다. 임의로 실행 결과를 만들지 않는다. "
            "로봇 이동 목적지와 사용자의 인터넷 위치를 혼동하지 않는다. '집으로 가자', '집에 가줘', "
            "'집으로 돌아가', '집으로 이동해', '충전소로 이동해'는 navigate_to(location=장소명)이다. "
            "이동 요청에 get_user_location을 호출하지 않는다. get_user_location은 사용자가 자기 위치를 묻거나 "
            "도시가 없는 날씨 요청에만 쓴다. 목적지 좌표는 추측하지 않고 웹 내비게이션이 저장 목록에서 확인한다. "
            "여러 단계 요청은 앞 단계의 실제 결과를 확인한 다음 다음 도구를 호출한다. "
            "앞 단계가 실패하면 후속 도구를 호출하지 않는다. 모호하거나 필수 정보가 없으면 "
            "사용자가 특정 장소로 가기, 이동 또는 주행을 요청하면 목적지 번호가 있어도 "
            "반드시 navigate_to(location=사용자가 말한 목적지)만 호출한다. "
            "get_navigation_destinations는 이동 동사가 없는 순수한 목적지 위치·목록 조회 질문에만 호출하고, "
            "실제 결과에 있는 좌표로 답한다. 이 조회는 읽기 전용이며 navigate_to를 호출하지 않는다. "
            "사용자가 이동이나 특정 장소로 가기를 요청하지 않고 '수건 접어줘', '수건 개어줘'라고만 하면 이동하지 않고 바로 fold_towel(confirm=true, wait_s=60)을 호출한다. "
            "특정 목적지로 이동하여 수건 접기를 요청한 경우(예: '책상에 가서 수건 접어줘')에만 먼저 navigate_to로 이동한 후 fold_towel을 호출한다. "
            "수건 접기 진행 중에는 get_towel_fold_status로 상태를 확인하고 완료 시 verdict 결과를 보고한다. "
            "이동에 실패했으면 fold_towel을 호출하지 않는다. 사용자가 '멈춰'라고 하면 stop_towel_fold를 호출한다. "
            "도구를 호출하지 말고 짧게 확인 질문을 한다. 도구 호출 없이 응답할 때는 한국어로 답한다."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        called = set()
        seen_call_ids = set()
        executed = 0
        for round_index in range(max_rounds):
            if cancel_event is not None and cancel_event.is_set():
                return "작업이 취소되었습니다."
            self.last_model_call_count += 1
            native_response = self.single_model_provider.chat_native(
                messages,
                tools=tool_schemas,
                call_id_prefix=f"{correlation_id}:round:{round_index + 1}",
                think=False,
                keep_alive="10m",
                options={"temperature": 0},
            )
            if cancel_event is not None and cancel_event.is_set():
                return "작업이 취소되었습니다."
            content = getattr(native_response, "content", None)
            calls = getattr(native_response, "tool_calls", None)
            assistant_message = getattr(native_response, "assistant_message", None)
            if not isinstance(content, str) or not isinstance(calls, list) or not isinstance(assistant_message, dict):
                raise RuntimeError("Ollama Native Tool Calling 응답 형식이 올바르지 않습니다.")
            if not calls:
                return content.strip()

            native_calls = assistant_message.get("tool_calls")
            if not isinstance(native_calls, list) or len(native_calls) != len(calls):
                raise RuntimeError("Native assistant 메시지와 Tool Call 목록이 일치하지 않습니다.")
            messages.append(assistant_message)
            for call in calls:
                if executed >= max_tool_calls:
                    raise RuntimeError(f"최대 도구 호출 횟수({max_tool_calls})를 초과했습니다.")
                name = call.name
                arguments = call.arguments
                call_id = call.call_id
                if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
                    raise RuntimeError("중복되거나 비어 있는 Tool Call ID를 차단했습니다.")
                seen_call_ids.add(call_id)
                if not isinstance(arguments, dict):
                    arguments = {"__invalid_arguments__": True}
                fingerprint = hashlib.sha256(
                    json.dumps([name, arguments], sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
                ).hexdigest()
                if fingerprint in called:
                    raise RuntimeError(f"반복된 도구 호출을 차단했습니다: {name}")
                called.add(fingerprint)
                if cancel_event is not None and cancel_event.is_set():
                    return "작업이 취소되었습니다."
                result = tool_executor(name, arguments)
                executed += 1
                if hasattr(result, "success"):
                    payload = {"success": result.success, "message": result.message, "data": result.data}
                elif isinstance(result, dict):
                    payload = result
                else:
                    payload = {"success": True, "message": str(result)}
                messages.append({
                    "role": "tool",
                    "tool_name": name or "",
                    "content": json.dumps({"tool_call_id": call_id, "tool": name, "result": payload},
                                           ensure_ascii=False, default=str),
                })
                # Do not execute another call returned in the same assistant
                # turn after a failed/cancelled step (e.g. pick then place).
                if payload.get("success") is False or (cancel_event is not None and cancel_event.is_set()):
                    return "작업 단계가 실패하거나 취소되어 후속 작업을 중단했습니다."
        raise RuntimeError(f"최대 모델 호출 횟수({max_rounds})에 도달했습니다.")

    # =========================================================
    # Final Response Generator
    # =========================================================

    def _generate_final_response(
        self,
        user_text: str,
        intent: str,
        tool_result,
    ) -> str:

        if hasattr(tool_result, "success"):

            result_data = {
                "success": tool_result.success,
                "message": tool_result.message,
                "data": tool_result.data,
            }

        elif isinstance(tool_result, dict):

            result_data = tool_result

        else:

            result_data = {
                "success": True,
                "message": str(tool_result),
            }

        prompt = f"""
당신은 NOMI의 최종 응답 생성기다.

사용자 요청:
{user_text}

실행한 작업:
{intent}

도구 실행 결과:
{json.dumps(result_data, ensure_ascii=False, default=str)}

도구 결과를 바탕으로 사용자에게 한국어로
짧고 자연스럽게 답변한다.

도구를 실행하지 않았다고 말하지 않는다.
도구 결과를 추측해서 추가하지 않는다.
실패했다면 실패 이유를 간단하게 설명한다.
"""

        self.last_model_call_count = getattr(self, "last_model_call_count", 0) + 1
        response = self.worker_provider.chat(
            [{"role": "system", "content": prompt}],
        )

        return response or getattr(
            tool_result,
            "message",
            "작업을 완료했습니다.",
        )
