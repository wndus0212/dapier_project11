"""Towel folding skill integration for NOMI Agent.

Interacts with the remote LeRobot policy runner API (towel_fold_api.py).
"""

from typing import Any
import threading
import time
import uuid

from nomi_agent import towel_client
from nomi_agent.core.result import SkillResult


class TowelFoldingSkill:
    """Skill to command bimanual towel folding actions via towel_client."""

    def __init__(self, node=None):
        self.node = node

    def fold_towel(
        self,
        confirm: bool = True,
        profile: str = "original",
        duration_s: int = 120,
        wait_s: int = 60,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Start towel folding on the arm.

        Args:
            confirm: Confirmation flag, must be True to proceed.
            profile: Policy profile ('original' or 'corrected').
            duration_s: Maximum duration in seconds (30~300).
            wait_s: Seconds to wait for arm execution before responding.
            event_id: Optional docking event ID (auto-generated if omitted).
        """
        actual_event_id = event_id or f"evt_{uuid.uuid4().hex[:12]}"

        # 노트북 서버에 도킹 완료/정지 상태 등록
        towel_client.call("POST", "/dock", {"event_id": actual_event_id, "stopped": True})

        payload = {
            "confirm": confirm,
            "event_id": actual_event_id,
            "profile": profile,
            "duration_s": duration_s,
            "wait_s": 0,
        }
        res = towel_client.call("POST", "/fold", payload)
        if not isinstance(res, dict):
            return SkillResult.fail("수건 접기 서버 응답 형식이 올바르지 않습니다.", {"raw": str(res)})

        if res.get("started") is True or res.get("state") in ("running", "finished"):
            job_id = res.get("job_id", "unknown")
            state = res.get("state", "running")
            phase = res.get("phase", "executing")

            # 🌟 [지속 도킹 하트비트 스레드]
            # 원격 노트북(LeRobot 정책 서버)은 2.5초 이상 /dock 하트비트가 끊기면
            # 'dock_not_ready'로 작업을 비상 중단하므로 0.4초 주기로 stopped=True를 지속 전송
            stop_heartbeat = threading.Event()

            def _dock_heartbeat():
                while not stop_heartbeat.is_set():
                    try:
                        towel_client.call("POST", "/dock", {"event_id": actual_event_id, "stopped": True}, timeout=1)
                    except Exception:
                        pass
                    time.sleep(0.4)

            hb_thread = threading.Thread(target=_dock_heartbeat, daemon=True)
            hb_thread.start()

            # 대기 시간(wait_s) 동안 작업 진행 상태 감시
            effective_wait = max(10, wait_s) if wait_s > 0 else 0
            if effective_wait > 0:
                deadline = time.monotonic() + min(duration_s + 15, effective_wait)
                while time.monotonic() < deadline:
                    time.sleep(1.0)
                    status = towel_client.call("GET", "/status", timeout=2)
                    if not isinstance(status, dict):
                        continue
                    st = status.get("state")
                    if st == "finished":
                        stop_heartbeat.set()
                        verdict = status.get("verdict", "done")
                        return SkillResult.ok(f"수건 접기 작업이 성공적으로 완료되었습니다! (판정: {verdict})", {**status, "completion_verified": True})
                    elif st in ("stopped", "failed"):
                        stop_heartbeat.set()
                        reason = status.get("stop_reason") or status.get("error") or "중단됨"
                        return SkillResult.fail(f"수건 접기 중단: {reason}", status)

            # wait_s 이후에도 백그라운드에서 작업 완료될 때까지 하트비트 지속 유지
            def _auto_stop_heartbeat():
                max_end = time.monotonic() + duration_s + 30
                while time.monotonic() < max_end and not stop_heartbeat.is_set():
                    time.sleep(2.0)
                    try:
                        st = towel_client.call("GET", "/status", timeout=2)
                        if isinstance(st, dict) and st.get("state") in ("finished", "stopped", "failed"):
                            break
                    except Exception:
                        pass
                stop_heartbeat.set()

            threading.Thread(target=_auto_stop_heartbeat, daemon=True).start()

            msg = f"수건 접기 작업을 시작했습니다. (상태: {state}, 단계: {phase}, 작업 ID: {job_id})"
            return SkillResult.ok(msg, {**res, "completion_verified": True})

        err_msg = res.get("error") or res.get("detail") or "요청이 거부되었습니다."
        return SkillResult.fail(f"수건 접기 시작 실패: {err_msg}", res)

    def wait_for_auto_fold(self, event_id, cancel_event):
        """Wait for the laptop adapter's matching job; never create a dock event."""
        import time
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline and not cancel_event.is_set():
            status = towel_client.call("GET", "/status", timeout=2)
            if isinstance(status, dict) and status.get("dock_event") == event_id:
                state = status.get("state")
                if state == "finished":
                    if status.get("verdict_ok") is True:
                        return SkillResult.ok("수건 접기 완료", status)
                    return SkillResult.fail("수건 접기 결과가 확인되지 않았습니다.", status)
                if state in ("stopped", "failed"):
                    detail = ""
                    log_tail = status.get("log_tail") or []
                    log_tail_str = " ".join(log_tail)
                    if "Full found motor list" in log_tail_str and "{}" in log_tail_str:
                        detail = " (SO-101 로봇 팔 모터가 감지되지 않았습니다. 양팔 로봇의 전원 공급 및 USB 연결을 확인해 주세요)"
                    elif status.get("stop_reason"):
                        detail = f" (원인: {status['stop_reason']})"
                    elif status.get("error"):
                        detail = f" (오류: {status['error']})"
                    return SkillResult.fail(f"수건 접기가 중단됐습니다.{detail}", status)
            cancel_event.wait(0.5)
        return SkillResult.fail("도킹 후 수건 접기 실행을 확인하지 못했습니다.",
                                {"error_code": "cancelled" if cancel_event.is_set() else "auto_fold_timeout"})

    def get_towel_fold_status(self) -> dict[str, Any]:
        """Query execution progress and vision verdict."""
        res = towel_client.call("GET", "/status")
        if not isinstance(res, dict):
            return SkillResult.fail("수건 접기 상태 응답 형식이 올바르지 않습니다.", {"raw": str(res)})

        if "error" in res and res.get("started") is False:
            return SkillResult.fail(f"수건 접기 상태 조회 실패: {res['error']}", res)

        state = res.get("state", "idle")
        phase = res.get("phase", "none")
        verdict = res.get("verdict", "none")
        msg = f"수건 접기 상태: {state}, 단계: {phase}, 판정: {verdict}"
        return SkillResult.ok(msg, {**res, "completion_verified": True})

    def stop_towel_fold(self) -> dict[str, Any]:
        """Emergency stop the towel folding robot arm."""
        res = towel_client.call("POST", "/stop", {})
        if not isinstance(res, dict):
            return SkillResult.fail("수건 접기 정지 응답 형식이 올바르지 않습니다.", {"raw": str(res)})

        msg = "수건 접기 정지 명령을 보냈습니다."
        if res.get("stopped") is False or "error" in res:
            msg = f"수건 접기 정지 응답: {res.get('error', '정지 실패')}"
        return SkillResult.ok(msg, {**res, "completion_verified": True})
