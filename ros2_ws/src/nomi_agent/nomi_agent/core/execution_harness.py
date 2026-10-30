"""Task lifecycle and guarded execution for NOMI tool calls."""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from nomi_agent.safety_guard import classify_no_tool_command, validate_actions
from nomi_agent.tool_registry import ToolRegistry, ToolValidationError


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    VALIDATED = "VALIDATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {TaskStatus.NEEDS_CLARIFICATION, TaskStatus.VALIDATED, TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


@dataclass
class TaskRecord:
    task_id: str
    request_key: str
    user_text: str
    validate_only: bool = False
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    actions: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    response_text: str | None = None
    model_calls: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    halted: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "validate_only": self.validate_only,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "actions": self.actions,
            "steps": self.steps,
            "error": self.error,
            "response_text": self.response_text,
            "model_calls": self.model_calls,
        }


class ExecutionHarness:
    """Owns one active task, validates every model-selected tool, and records outcomes."""

    def __init__(
        self,
        llm_client,
        tool_registry: ToolRegistry,
        publish_response: Callable[[str], None],
        publish_status: Callable[[dict[str, Any]], None] | None = None,
        logger=None,
        audit_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.publish_response = publish_response
        self.publish_status = publish_status or (lambda _status: None)
        self.logger = logger
        self.audit_callback = audit_callback
        self._lock = threading.RLock()
        self._active: TaskRecord | None = None
        self._tasks: dict[str, TaskRecord] = {}
        self._request_ids: dict[str, str] = {}
        self._max_history = 100

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(" ".join(text.lower().split()).encode("utf-8")).hexdigest()

    def submit(self, user_text: str, request_id: str | None = None, *, validate_only: bool = False) -> tuple[str, str]:
        text = user_text.strip()
        if not text:
            return "invalid", "명령을 입력해 주세요."
        if request_id is not None:
            request_id = str(request_id).strip()
            if not request_id or len(request_id) > 128:
                return "invalid", "요청 ID 형식이 올바르지 않습니다."

        key = self._key(text)
        with self._lock:
            if request_id and request_id in self._request_ids:
                task_id = self._request_ids[request_id]
                return "duplicate", task_id
            if self._active and self._active.status not in TERMINAL:
                if self._active.request_key == key:
                    return "duplicate", self._active.task_id
                return "busy", self._active.task_id
            task_id = request_id or str(uuid.uuid4())
            record = TaskRecord(task_id=task_id, request_key=key, user_text=text, validate_only=validate_only)
            self._tasks[task_id] = record
            self._active = record
            if request_id:
                self._request_ids[request_id] = task_id
            while len(self._tasks) > self._max_history:
                oldest = next(iter(self._tasks))
                if self._tasks[oldest] is self._active:
                    break
                old = self._tasks.pop(oldest)
                if old.task_id in self._request_ids.values():
                    self._request_ids = {k: v for k, v in self._request_ids.items() if v != old.task_id}

        self._set_status(record, TaskStatus.PENDING)
        self._audit("task_submitted", record, input=text)
        threading.Thread(target=self._run_task, args=(record,), name=f"nomi-task-{task_id[:8]}", daemon=True).start()
        return "started", task_id

    def _audit(self, event_type: str, record: TaskRecord, **fields) -> None:
        """Emit structured, best-effort audit data without affecting execution."""
        if self.audit_callback is None:
            return
        event = {
            "timestamp": time.time(),
            "event": event_type,
            "task_id": record.task_id,
            "input": record.user_text,
            **fields,
        }
        try:
            self.audit_callback(event)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"NOMI audit 기록 실패: {exc}")

    def cancel_active(self) -> tuple[bool, str | None]:
        with self._lock:
            record = self._active
            if record is None or record.status in TERMINAL:
                return False, None
            record.cancel_event.set()
            record.halted = True
        self.tool_registry.emergency_stop()
        self._set_status(record, TaskStatus.CANCELLED, {"code": "cancelled", "message": "사용자 중단 요청"})
        return True, record.task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tasks.get(task_id)
            return record.public_dict() if record else None

    def get_active(self) -> dict[str, Any] | None:
        with self._lock:
            record = self._active
            return record.public_dict() if record else None

    def _set_status(self, record: TaskRecord, status: TaskStatus, error: dict[str, Any] | None = None) -> None:
        with self._lock:
            if record.status == TaskStatus.CANCELLED and status != TaskStatus.CANCELLED:
                return
            record.status = status
            record.updated_at = time.time()
            if error is not None:
                record.error = error
            payload = record.public_dict()
        self._audit("task_status", record, status=status.value,
                    total_duration_seconds=round(time.time() - record.created_at, 3),
                    error=error)
        if self.logger:
            self.logger.info(f"NOMI task {record.task_id}: {status.value}")
        try:
            self.publish_status(payload)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"작업 상태 발행 실패: {exc}")

    def _publish_text(self, record: TaskRecord, text: str) -> None:
        if record.cancel_event.is_set():
            return
        self.llm_client.add_assistant_message(text)
        with self._lock:
            record.response_text = str(text)
            record.updated_at = time.time()
            payload = record.public_dict()
        self._audit("task_response", record, response_text=str(text))
        try:
            self.publish_status(payload)
        except Exception as exc:
            if self.logger:
                self.logger.warning(f"응답 상태 발행 실패: {exc}")
        self.publish_response(text)

    def _run_task(self, record: TaskRecord) -> None:
        try:
            self._set_status(record, TaskStatus.VALIDATING)
            command_kind, no_action_reply = classify_no_tool_command(record.user_text)
            if command_kind == "NON_ACTION":
                self._set_status(record, TaskStatus.SUCCEEDED)
                self._publish_text(record, no_action_reply)
                return
            if self._try_auto_towel_mission(record):
                return
            if getattr(self.llm_client, "is_single_model_profile", False):
                # Single model mode skips the separate router request; every
                # incremental proposal is still checked by Registry/SafetyGuard.
                self._audit("single_model_started", record, profile=self.llm_client.profile)
                self._set_status(record, TaskStatus.RUNNING)
                response = self.llm_client.run_single_model_with_tools(
                    user_text=record.user_text,
                    tool_schemas=self.tool_registry.get_tool_schemas(),
                    tool_executor=lambda name, args: self.execute_single_model_tool(record, name, args),
                    cancel_event=record.cancel_event,
                    correlation_id=record.task_id,
                )
                record.model_calls = int(getattr(self.llm_client, "last_model_call_count", 0))
                self._audit("single_model_finished", record, model_calls=record.model_calls,
                            tool_calls=len(record.steps))
                if record.cancel_event.is_set():
                    return
                failed_steps = [step for step in record.steps if not step.get("success")]
                if failed_steps:
                    first_failure = failed_steps[0]
                    failure_status = (TaskStatus.NEEDS_CLARIFICATION
                                      if first_failure.get("data", {}).get("needs_clarification")
                                      else TaskStatus.FAILED)
                    self._set_status(record, failure_status, {
                        "code": first_failure.get("error_code", "tool_failed"),
                        "tool": first_failure.get("tool"),
                        "message": first_failure.get("message", "도구 실행이 실패했습니다."),
                    })
                    failure_message = first_failure.get("message", "도구 실행이 실패했습니다.")
                    if failure_status == TaskStatus.NEEDS_CLARIFICATION:
                        self._publish_text(record, failure_message)
                    else:
                        self._publish_text(record, f"요청한 작업의 완료를 확인하지 못했습니다. {failure_message}".strip())
                    return
                if not record.steps:
                    if not isinstance(response, str) or not response.strip():
                        raise ToolValidationError("empty_model_response", "모델이 도구 호출이나 확인 응답을 반환하지 않았습니다.")
                    response_kind, clarification = classify_no_tool_command(record.user_text)
                    if response_kind == "ROBOT_TASK":
                        self._set_status(record, TaskStatus.NEEDS_CLARIFICATION, {
                            "code": "tool_call_missing", "message": clarification,
                        })
                        self._publish_text(record, clarification)
                        return
                    self._set_status(record, TaskStatus.SUCCEEDED)
                    self._publish_text(record, response)
                    return
                if record.validate_only:
                    self._set_status(record, TaskStatus.VALIDATED)
                    self._publish_text(record, "목적지와 Tool 인자 검증을 완료했습니다. 검증 전용 요청이라 실제 도구는 실행하지 않았습니다.")
                    return
                if not isinstance(response, str) or not response.strip():
                    response = "요청한 도구 실행을 확인했습니다."
                self._set_status(record, TaskStatus.SUCCEEDED)
                self._publish_text(record, response)
                return

            route_result = self.llm_client.route_command(record.user_text)
            if record.cancel_event.is_set():
                return
            if not isinstance(route_result, dict):
                raise ToolValidationError("invalid_route", "명령 해석 결과 형식이 올바르지 않습니다.")

            route = route_result.get("route")
            self._audit("route_selected", record, route=route,
                        actions=route_result.get("actions", []),
                        intent=route_result.get("intent"))
            if route == "CHAT":
                reply = route_result.get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    raise ToolValidationError("invalid_reply", "응답 형식이 올바르지 않습니다.")
                response_kind, clarification = classify_no_tool_command(record.user_text)
                if response_kind == "ROBOT_TASK":
                    self._set_status(record, TaskStatus.NEEDS_CLARIFICATION, {
                        "code": "task_misclassified_as_chat", "message": clarification,
                    })
                    self._publish_text(record, clarification)
                    return
                self._set_status(record, TaskStatus.SUCCEEDED)
                self._publish_text(record, reply)
                return
            if route != "TASK":
                raise ToolValidationError("invalid_route", "명령을 검증할 수 없어 실행을 보류했습니다.")

            actions = route_result.get("actions", [])
            if not isinstance(actions, list):
                raise ToolValidationError("invalid_plan", "작업 계획 형식이 올바르지 않습니다.")
            if not actions and route_result.get("intent"):
                actions = [{
                    "intent": route_result.get("intent"),
                    "arguments": route_result.get("arguments", {}),
                }]

            try:
                validated_actions = self.tool_registry.validate_plan(actions)
            except ToolValidationError as exc:
                self._audit("plan_validation", record, accepted=False, error_code=exc.code, error=str(exc))
                raise
            self._audit("plan_validation", record, accepted=True, actions=validated_actions)
            safe, semantic_actions, message = validate_actions(record.user_text, validated_actions)
            if not safe:
                clarification = any(token in message for token in ("어디", "어떤 물체", "구체적으로", "더 알려", "몇 번"))
                status = TaskStatus.NEEDS_CLARIFICATION if clarification else TaskStatus.FAILED
                self._set_status(record, status, {"code": "input_rejected", "message": message})
                self._publish_text(record, message)
                return
            validated_actions = semantic_actions
            record.actions = validated_actions
            self._set_status(record, TaskStatus.RUNNING)

            first = validated_actions[0]
            response = self.llm_client.run_worker_with_tools(
                user_text=record.user_text,
                initial_intent=first["intent"],
                initial_arguments=first["arguments"],
                tool_schemas=self.tool_registry.get_tool_schemas(),
                tool_executor=lambda name, args: self.execute_single_model_tool(record, name, args),
                initial_actions=validated_actions,
            )
            record.model_calls = int(getattr(self.llm_client, "last_model_call_count", 0))
            if record.cancel_event.is_set():
                return

            failed_steps = [step for step in record.steps if not step.get("success")]
            if failed_steps:
                first_failure = failed_steps[0]
                self._set_status(record, TaskStatus.FAILED, {
                    "code": first_failure.get("error_code", "tool_failed"),
                    "tool": first_failure.get("tool"),
                    "message": first_failure.get("message", "도구 실행이 실패했습니다."),
                })
                # Do not let a generated narrative override the recorded tool outcome.
                self._publish_text(record, f"요청한 작업의 완료를 확인하지 못했습니다. {first_failure.get('message', '')}".strip())
                return
            if not record.steps:
                message = "작업 도구가 호출되지 않아 실행 여부를 확인할 수 없습니다."
                self._set_status(record, TaskStatus.FAILED, {"code": "no_tool_execution", "message": message})
                self._publish_text(record, message)
                return
            if record.validate_only:
                self._set_status(record, TaskStatus.VALIDATED)
                self._publish_text(record, "Tool 인자 검증을 완료했습니다. 검증 전용 요청이라 실제 도구는 실행하지 않았습니다.")
                return
            if not isinstance(response, str) or not response.strip():
                response = "도구 실행은 확인했지만 응답을 생성하지 못했습니다."
            self._set_status(record, TaskStatus.SUCCEEDED)
            self._publish_text(record, response)
        except ToolValidationError as exc:
            record.model_calls = int(getattr(self.llm_client, "last_model_call_count", record.model_calls))
            self._audit("task_error", record, error_code=exc.code, error=str(exc))
            status = TaskStatus.NEEDS_CLARIFICATION if exc.needs_clarification else TaskStatus.FAILED
            self._set_status(record, status, {"code": exc.code, "message": str(exc)})
            self._publish_text(record, str(exc))
        except Exception as exc:
            record.model_calls = int(getattr(self.llm_client, "last_model_call_count", record.model_calls))
            if record.cancel_event.is_set():
                return
            exception_name = type(exc).__name__.lower()
            message_lower = str(exc).lower()
            if isinstance(exc, TimeoutError) or "timeout" in exception_name or "timed out" in message_lower:
                error_code = "llm_timeout"
            elif "500" in message_lower or "eof" in message_lower or "responseerror" in exception_name:
                error_code = "llm_provider_error"
            elif "connection" in exception_name or "connection" in message_lower:
                error_code = "llm_connection_error"
            else:
                error_code = "execution_error"
            self._audit("task_error", record, error_code=error_code, error=str(exc), exception_type=type(exc).__name__)
            self._set_status(record, TaskStatus.FAILED, {"code": error_code, "type": type(exc).__name__, "message": str(exc)})
            self._publish_text(record, f"명령 처리 중 오류가 발생했습니다: {exc}")

    def _try_auto_towel_mission(self, record: TaskRecord) -> bool:
        text = "".join(record.user_text.split())
        if not re.search(r"수건.{0,20}(?:접어|접기|개어|개기|개줘|접으)", text):
            return False
        if classify_no_tool_command(record.user_text)[0] == "NON_ACTION":
            return False
        destinations = self.tool_registry.get_navigation_destinations()["data"]["destinations"]
        matches = [row["name"] for row in destinations if re.search(
            re.escape("".join(row["name"].split())) + r"(?:으로|로|에)(?:가|이동|향해)", text)]
        if len(matches) != 1:
            return False
        actions = [("navigate_to", {"location": matches[0]}), ("fold_towel", {"confirm": True})]
        record.actions = [{"intent": name, "arguments": args} for name, args in actions]
        validated = self.tool_registry.validate_plan(record.actions)
        safe, checked, message = validate_actions(record.user_text, validated, tool_registry=self.tool_registry)
        if not safe:
            self._set_status(record, TaskStatus.FAILED, {"code": "semantic_safety_rejected", "message": message})
            self._publish_text(record, message)
            return True
        record.actions = checked
        if record.validate_only:
            self._set_status(record, TaskStatus.VALIDATED)
            self._publish_text(record, "이동과 수건 접기 순서를 확인했습니다. 실제 동작은 실행하지 않았습니다.")
            return True
        towel = self.tool_registry.skills.get("towel_folding")
        if towel is None:
            self._set_status(record, TaskStatus.FAILED, {"code": "towel_adapter_unavailable", "message": "수건 접기 기능이 준비되지 않았습니다."})
            self._publish_text(record, "수건 접기 기능이 준비되지 않아 이동을 시작하지 않았습니다.")
            return True
        self._set_status(record, TaskStatus.RUNNING)
        for action in checked:
            if record.cancel_event.is_set() or record.halted:
                return True
            result = self.execute_tool(record, action["intent"], action.get("arguments", {}))
            if record.cancel_event.is_set():
                return True
            if not result.success:
                self._set_status(record, TaskStatus.FAILED, {"code": result.data.get("error_code", "tool_failed"),
                                                            "tool": action["intent"], "message": result.message})
                self._publish_text(record, f"요청한 작업을 완료하지 못했습니다. {result.message}")
                return True
        self._set_status(record, TaskStatus.SUCCEEDED)
        self._publish_text(record, "도착 후 수건 접기 판정을 완료했습니다.")
        return True

    def execute_tool(self, record: TaskRecord, name: str, arguments: Any):
        if record.cancel_event.is_set() or record.halted:
            from nomi_agent.core.result import SkillResult
            return SkillResult.fail("이전 단계가 중단되어 도구를 실행하지 않았습니다.", {"error_code": "cancelled"})
        try:
            spec, normalized = self.tool_registry.validate_call(name, arguments)
        except ToolValidationError as exc:
            self._audit("tool_validation", record, tool=name, accepted=False, error_code=exc.code, error=str(exc))
            record.halted = True
            result = self._record_step(record, name, {}, False, str(exc), exc.code)
            return result

        safe, checked_actions, message = validate_actions(
            record.user_text,
            [{"intent": spec.name, "arguments": normalized}],
            tool_registry=self.tool_registry,
            held_object=any(step.get("tool") == "pick_object" and step.get("success") for step in record.steps),
        )
        if not safe:
            self._audit(
                "tool_validation",
                record,
                tool=spec.name,
                accepted=False,
                error_code="semantic_safety_rejected",
                error=message,
            )
            record.halted = True
            return self._record_step(
                record,
                spec.name,
                normalized,
                False,
                message,
                "semantic_safety_rejected",
            )
        # SafetyGuard may correct an explicit destination that the model
        # confused with the user's IP location. Execute the corrected action
        # through the same Registry instead of silently using the original.
        if checked_actions:
            checked = checked_actions[0]
            if checked["intent"] != spec.name or checked["arguments"] != normalized:
                spec, normalized = self.tool_registry.validate_call(
                    checked["intent"], checked["arguments"]
                )

        self._audit("tool_validation", record, tool=spec.name, arguments=normalized, accepted=True)
        if record.validate_only:
            message = "인자 검증 완료 (실행하지 않음)"
            self._audit("tool_validation_only", record, tool=spec.name, arguments=normalized, executed=False)
            return self._record_step(record, spec.name, normalized, True, message,
                                     data={"validation_only": True, "execution_performed": False})
        started = time.monotonic()
        self._audit("tool_started", record, tool=spec.name, arguments=normalized)
        compact = "".join(record.user_text.split())
        compound_towel = ("수건" in compact and any(w in compact for w in ("접어", "접기", "개어", "개줘", "접으"))
                          and any(w in compact for w in ("이동", "가서", "가줘", "가자", "가고", "가면서", "도착")))
        if spec.name == "fold_towel" and compound_towel:
            nav_done = any(step.get("tool") == "navigate_to" and step.get("success") is True for step in record.steps)
            towel = self.tool_registry.skills.get("towel_folding")
            if nav_done and towel is not None:
                result = towel.fold_towel(confirm=True)
            else:
                from nomi_agent.core.result import SkillResult
                result = SkillResult.fail("먼저 도착 완료를 확인해야 합니다.", {"error_code": "arrival_not_verified"})
        else:
            result = self.tool_registry.execute(spec.name, normalized, cancel_event=record.cancel_event)
        elapsed = round(time.monotonic() - started, 3)
        self._audit("tool_finished", record, tool=spec.name, arguments=normalized,
                    duration_seconds=elapsed, success=result.success,
                    result=result.message, data=result.data,
                    error_code=result.data.get("error_code"))
        self._record_step(
            record, spec.name, normalized, result.success, result.message,
            result.data.get("error_code"), elapsed, result.data,
        )
        if not result.success:
            record.halted = True
        return result

    def execute_single_model_tool(self, record: TaskRecord, name: str, arguments: Any):
        """Explicit Registry + SafetyGuard gate for incremental model proposals."""
        if record.cancel_event.is_set() or record.halted:
            from nomi_agent.core.result import SkillResult
            return SkillResult.fail("이전 단계가 중단되어 도구를 실행하지 않았습니다.", {"error_code": "cancelled"})
        try:
            spec, normalized = self.tool_registry.validate_call(name, arguments)
            safe, _, message = validate_actions(
                record.user_text,
                [{"intent": spec.name, "arguments": normalized}],
                tool_registry=self.tool_registry,
                held_object=any(step.get("tool") == "pick_object" and step.get("success") for step in record.steps),
            )
        except ToolValidationError as exc:
            self._audit("tool_validation", record, tool=name, accepted=False, error_code=exc.code, error=str(exc))
            record.halted = True
            return self._record_step(record, name, {}, False, str(exc), exc.code,
                                     data={"needs_clarification": exc.needs_clarification})
        if not safe:
            record.halted = True
            needs_clarification = any(token in message for token in ("무엇을", "어떤 물체", "어디", "구체적으로", "몇 번"))
            return self._record_step(record, spec.name, normalized, False, message, "semantic_safety_rejected",
                                     data={"needs_clarification": needs_clarification})
        # execute_tool repeats the gate immediately before dispatch, preserving
        # the same boundary as existing qwen_hermes calls.
        return self.execute_tool(record, spec.name, normalized)

    def _record_step(self, record, tool, arguments, success, message, error_code=None, elapsed=None, data=None):
        from nomi_agent.core.result import SkillResult
        step = {
            "index": len(record.steps) + 1,
            "tool": tool,
            "arguments": arguments,
            "success": bool(success),
            "message": str(message),
            "error_code": error_code,
            "duration_seconds": elapsed,
            "data": data or {},
        }
        record.steps.append(step)
        self._set_status(record, TaskStatus.RUNNING, None if success else {"code": error_code or "tool_failed", "tool": tool, "message": str(message)})
        return SkillResult.ok(str(message), data or {}) if success else SkillResult.fail(str(message), data or {"error_code": error_code or "tool_failed"})
