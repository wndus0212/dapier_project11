"""Towel folding skill client for remote LeRobot policy API server.

Matches the API specification from the Notion integration guide:
- POST /fold : start policy execution
- GET /status : poll execution status and vision verdict
- POST /stop : emergency stop
- GET /tools : fetch OpenAI function calling schemas
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fold_towel",
            "description": "학습된 정책으로 SO-101 양팔 로봇을 사용하여 수건을 반접기 및 대문접기한다. 자율주행 도착 후 실행하거나 승인 확인 후 호출한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "confirm": {
                        "type": "boolean",
                        "description": "실행 승인 여부 (필수: true)",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["original", "corrected"],
                        "description": "정책 프로필 (기본: original)",
                    },
                    "duration_s": {
                        "type": "integer",
                        "minimum": 30,
                        "maximum": 300,
                        "description": "최대 실행 시간(초, 기본 120)",
                    },
                    "wait_s": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 120,
                        "description": "응답 대기 시간(초, 주행 후 호출 시 60 권장)",
                    },
                },
                "required": ["confirm"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_towel_fold_status",
            "description": "수건 접기 작업의 진행 상태와 결과(verdict)를 조회한다.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_towel_fold",
            "description": "수건 접기 작업을 긴급 중단한다.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


def get_base_url() -> str:
    url = os.environ.get("TOWEL_API_URL")
    if url:
        return url.rstrip("/")
    return "http://127.0.0.1:8100"


def get_api_key() -> str:
    return os.environ.get("TOWEL_API_TOKEN", "")


def call(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    if os.environ.get("TOWEL_MOCK", "").lower() in ("1", "true", "yes"):
        if path == "/fold":
            return {"started": True, "state": "running", "job_id": "mock_job_demo", "phase": "executing"}
        elif path == "/status":
            return {"state": "finished", "phase": "done", "job_id": "mock_job_demo", "verdict": "book_fold", "verdict_ok": True, "elapsed_s": 3.0}
        elif path == "/stop":
            return {"stopped": True, "message": "모의 수건 접기 정지 완료"}
        elif path == "/tools":
            return {"tools": DEFAULT_TOOLS}

    base = get_base_url()
    key = get_api_key()

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        method=method,
        data=data,
        headers=headers,
    )
    if timeout is None:
        timeout = 15 + ((body or {}).get("wait_s", 0) if isinstance(body, dict) else 0)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {"started": True}
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {exc.code}: {exc.reason}", "started": False}
    except (OSError, TimeoutError) as exc:
        return {"error": f"수건 접기 서버({base}) 통신 오류: {exc}", "started": False}


TOOL_IMPL = {
    "fold_towel": lambda a: call("POST", "/fold", a if isinstance(a, dict) else {}),
    "get_towel_fold_status": lambda _a=None: call("GET", "/status"),
    "stop_towel_fold": lambda _a=None: call("POST", "/stop", {}),
}


def load_tools() -> list[dict[str, Any]]:
    """Return OpenAI function calling schemas for towel folding tools."""
    try:
        res = call("GET", "/tools", timeout=2.0)
        if isinstance(res, dict) and "tools" in res and isinstance(res["tools"], list):
            return res["tools"]
    except Exception:
        pass
    return DEFAULT_TOOLS
