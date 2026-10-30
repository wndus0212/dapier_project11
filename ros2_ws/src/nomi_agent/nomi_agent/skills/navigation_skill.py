import json
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from geometry_msgs.msg import Twist
try:
    from geometry_msgs.msg import PoseWithCovarianceStamped
except ImportError:
    PoseWithCovarianceStamped = None
from std_msgs.msg import String, Bool
from rclpy.action import ActionClient
from nav2_msgs.action import NavigateToPose


class NavigationSkill:
    """Dispatch short movement, execute Nav2 autonomous navigation, or await browser web navigation."""

    DIRECTIONS = {
        "앞으로": (0.25, 0.0, 0.0), "전진": (0.25, 0.0, 0.0), "forward": (0.25, 0.0, 0.0),
        "뒤로": (-0.25, 0.0, 0.0), "후진": (-0.25, 0.0, 0.0), "backward": (-0.25, 0.0, 0.0),
        "왼쪽": (0.0, 0.2, 0.0), "left": (0.0, 0.2, 0.0),
        "오른쪽": (0.0, -0.2, 0.0), "right": (0.0, -0.2, 0.0),
        "왼쪽 회전": (0.0, 0.0, 0.35), "rotate_left": (0.0, 0.0, 0.35),
        "오른쪽 회전": (0.0, 0.0, -0.35), "rotate_right": (0.0, 0.0, -0.35),
    }
    ARRIVAL_TIMEOUT_S = 120.0

    def __init__(self, node=None):
        self.node = node
        self.publisher = node.create_publisher(Twist, "/nomi/xlerobot/base_cmd_vel", 10) if node else None
        self.real_cmd_vel_pub = node.create_publisher(Twist, "/cmd_vel", 10) if node else None
        self.destination_publisher = node.create_publisher(String, "/nomi/navigation_request", 10) if node else None
        self.cancel_publisher = node.create_publisher(String, "/nomi/navigation_cancel", 10) if node else None
        self.enable_docking_pub = node.create_publisher(Bool, "/apriltag/enable_docking", 10) if node else None
        self.result_subscription = (
            node.create_subscription(String, "/nomi/navigation_result", self._navigation_result_callback, 10)
            if node else None
        )

        # Nav2 Action Client (NavigateToPose)
        self._nav2_client = (
            ActionClient(node, NavigateToPose, "navigate_to_pose")
            if (node and hasattr(node, "handle"))
            else None
        )
        self._active_nav2_goal_handle = None

        # AprilTag 비전 도킹 완료 대기 동기화
        self._docking_complete_event = threading.Event()
        self._last_docking_status: dict | None = None
        self.docking_status_subscription = (
            node.create_subscription(String, "/apriltag/docking_status", self._docking_status_callback, 10)
            if node else None
        )
        self.docking_complete_subscription = (
            node.create_subscription(String, "/amr/docking_complete", self._docking_complete_callback, 10)
            if node else None
        )
        self._current_map_pose: dict | None = None
        self.amcl_pose_subscription = (
            node.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._amcl_pose_callback, 10)
            if (node and PoseWithCovarianceStamped is not None) else None
        )
        self._pending_lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._active_request_id: str | None = None

    def _amcl_pose_callback(self, msg: PoseWithCovarianceStamped):
        self._current_map_pose = {
            "x": float(msg.pose.pose.position.x),
            "y": float(msg.pose.pose.position.y),
        }

    def _resolve_destination(self, name: str) -> dict | None:
        """목적지 이름 또는 별칭(A, 지점 A, 홈, B 등)을 실제 지도 좌표 및 속성으로 해석"""
        norm_key = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name).casefold())
        norm_key = re.sub(r"[.,!?~]", "", norm_key)

        candidates = [
            Path("/home/juyeon/dapier_project11/XLeRobot/web_control/shared/navigation_destinations.json"),
            Path("/home/juyeon/XLeRobot/web_control/shared/navigation_destinations.json"),
            Path.home() / "dapier_project11/XLeRobot/web_control/shared/navigation_destinations.json",
        ]
        destinations = []
        for p in candidates:
            if p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        destinations = data.get("destinations", [])
                        break
                except Exception:
                    pass

        for d in destinations:
            d_name = d.get("name", "")
            d_norm = re.sub(r"\s+", "", unicodedata.normalize("NFKC", d_name).casefold())
            d_norm = re.sub(r"[.,!?~]", "", d_norm)
            if norm_key == d_norm:
                return d
            for alias in d.get("aliases", []):
                a_norm = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(alias)).casefold())
                a_norm = re.sub(r"[.,!?~]", "", a_norm)
                if norm_key == a_norm:
                    return d
        for d in destinations:
            d_name = d.get("name", "")
            d_norm = re.sub(r"\s+", "", unicodedata.normalize("NFKC", d_name).casefold())
            d_norm = re.sub(r"[.,!?~]", "", d_norm)
            if d_norm and (d_norm in norm_key or norm_key in d_norm):
                return d
            for alias in d.get("aliases", []):
                a_norm = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(alias)).casefold())
                a_norm = re.sub(r"[.,!?~]", "", a_norm)
                if a_norm and (a_norm in norm_key or norm_key in a_norm):
                    return d
        return None

    def _navigation_result_callback(self, message):
        try:
            result = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(result, dict):
            return
        request_id = result.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._pending_lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            pending["result"] = result
            pending["event"].set()

    def _docking_status_callback(self, message):
        try:
            payload = json.loads(message.data)
            self._last_docking_status = payload
            if payload.get("state") == "DOCKED":
                if self.node:
                    self.node.get_logger().info("🎉 [NOMI_NAV2] AprilTag 도킹 상태 감지: DOCKED")
                self._docking_complete_event.set()
        except Exception:
            pass

    def _docking_complete_callback(self, message):
        if self.node:
            self.node.get_logger().info(f"🎉 [NOMI_NAV2] /amr/docking_complete 토픽 수신: {message.data}")
        self._docking_complete_event.set()

    def _cancel_request(self, request_id: str):
        if self.cancel_publisher is None:
            return
        message = String()
        message.data = json.dumps({"request_id": request_id}, ensure_ascii=False)
        self.cancel_publisher.publish(message)

    def _execute_nav2_navigation(self, dest_info: dict) -> dict:
        """Nav2 Action (navigate_to_pose)을 사용하여 자율주행 실행 및 AprilTag 도킹 자동 연계"""
        target_name = dest_info.get("name", "목적지")
        tx = float(dest_info["x"])
        ty = float(dest_info["y"])
        tyaw_deg = float(dest_info.get("yaw", 0.0))

        # 🌟 자율주행 시작 전 이전 비전 도킹 잔여 제어 비활성화 (cmd_vel 경합 및 모터 끊김 방지)
        if self.enable_docking_pub is not None:
            stop_dock = Bool()
            stop_dock.data = False
            self.enable_docking_pub.publish(stop_dock)
        auto_dock = bool(dest_info.get("auto_dock", False))

        # 도킹 지점인 경우: 최종 도킹 위치는 벽면/충전소 밀착 좌표이므로
        # Nav2는 벽면 장애물 인플레이션 회피를 위해 전방 30cm 대기 지점(Staging Pose)까지 주행한 후
        # 도착 시 AprilTag 비전 도킹이 남은 거리를 정밀 진입하도록 유도합니다.
        nav_x = tx
        nav_y = ty
        yaw_rad = math.radians(tyaw_deg)
        if auto_dock:
            if "approach_x" in dest_info and "approach_y" in dest_info:
                nav_x = float(dest_info["approach_x"])
                nav_y = float(dest_info["approach_y"])
            else:
                dock_offset = float(dest_info.get("dock_offset", 0.30))
                nav_x = tx - dock_offset * math.cos(yaw_rad)
                nav_y = ty - dock_offset * math.sin(yaw_rad)

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose.header.frame_id = "map"
        goal_msg.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal_msg.pose.pose.position.x = nav_x
        goal_msg.pose.pose.position.y = nav_y
        goal_msg.pose.pose.position.z = 0.0

        goal_msg.pose.pose.orientation.z = math.sin(yaw_rad / 2.0)
        goal_msg.pose.pose.orientation.w = math.cos(yaw_rad / 2.0)

        # 현재 로봇이 이미 경유지 바로 앞(35cm 이내)에 위치해 있는지 검사
        already_near_staging = False
        if auto_dock and self._current_map_pose is not None:
            cur_dx = self._current_map_pose["x"] - nav_x
            cur_dy = self._current_map_pose["y"] - nav_y
            cur_dist = math.hypot(cur_dx, cur_dy)
            if cur_dist <= 0.35:
                already_near_staging = True
                if self.node:
                    self.node.get_logger().info(
                        f"[NOMI_NAV2] 📍 로봇이 이미 '{target_name}' 경유지 반경({cur_dist:.2f}m <= 0.35m) 내에 위치함 -> Nav2 주행을 생략하고 바로 AprilTag 비전 도킹 시작"
                    )

        if already_near_staging:
            status = 4
        else:
            if self.node:
                self.node.get_logger().info(
                    f"[NOMI_NAV2] 🚀 Nav2 액션 목표 전송: {target_name} "
                    f"(Nav2 경유: x={nav_x:.2f}, y={nav_y:.2f}, 도킹: x={tx:.2f}, y={ty:.2f}, yaw={tyaw_deg:.1f}°)"
                )

            nav_start_time = time.time()
            send_goal_future = self._nav2_client.send_goal_async(goal_msg)
            goal_response_event = threading.Event()

            def on_goal_response(_fut):
                goal_response_event.set()

            send_goal_future.add_done_callback(on_goal_response)

            if not goal_response_event.wait(5.0):
                return {"success": False, "message": f"Nav2가 '{target_name}' 주행 목표 접수에 응답하지 않았습니다."}

            goal_handle = send_goal_future.result()
            if not goal_handle or not goal_handle.accepted:
                return {"success": False, "message": f"Nav2가 '{target_name}' 주행 목표를 거절했습니다. (로봇 위치 또는 경로 불능)"}

            self._active_nav2_goal_handle = goal_handle
            result_future = goal_handle.get_result_async()
            result_event = threading.Event()

            def on_result(_fut):
                result_event.set()

            result_future.add_done_callback(on_result)

            # 주행 완료 대기
            finished = result_event.wait(self.ARRIVAL_TIMEOUT_S)
            self._active_nav2_goal_handle = None

            if not finished:
                try:
                    goal_handle.cancel_goal_async()
                except Exception:
                    pass
                return {"success": False, "message": f"Nav2 주행 제한 시간({self.ARRIVAL_TIMEOUT_S}초)이 초과되어 취소했습니다."}

            status = result_future.result().status

            # 목표 전송 후 2초 이내에 비정상 즉시 중단(Status 6)된 경우, 1회 자동 재전송 시도
            if status == 6 and (time.time() - nav_start_time) < 2.0:
                if self.node:
                    self.node.get_logger().warn(
                        f"[NOMI_NAV2] ⚠️ Nav2 목표 전송 직후 즉시 중단(상태 코드 6) 감지 -> 0.5초 후 1회 재전송 시도"
                    )
                time.sleep(0.5)
                retry_future = self._nav2_client.send_goal_async(goal_msg)
                retry_resp_event = threading.Event()
                retry_future.add_done_callback(lambda _f: retry_resp_event.set())
                if retry_resp_event.wait(5.0):
                    retry_handle = retry_future.result()
                    if retry_handle and retry_handle.accepted:
                        self._active_nav2_goal_handle = retry_handle
                        retry_res_future = retry_handle.get_result_async()
                        retry_res_event = threading.Event()
                        retry_res_future.add_done_callback(lambda _f: retry_res_event.set())
                        if retry_res_event.wait(self.ARRIVAL_TIMEOUT_S):
                            status = retry_res_future.result().status
                        self._active_nav2_goal_handle = None

            # 주행 완료 검사: status가 4가 아니더라도,
            # 1) 이미 로봇이 경유지 반경 55cm 이내이거나
            # 2) 전방 카메라에서 도킹 태그가 감지되고 있거나
            # 3) 백그라운드 주행 중 도착하는 경우를 위해 최대 30초간 도착 감시
            if status != 4 and auto_dock:
                wait_deadline = time.time() + 30.0
                while time.time() < wait_deadline:
                    tag_visible = (
                        self._last_docking_status is not None
                        and self._last_docking_status.get("tag_detected", False)
                    )
                    cur_dist = None
                    if self._current_map_pose is not None:
                        cur_dx = self._current_map_pose["x"] - nav_x
                        cur_dy = self._current_map_pose["y"] - nav_y
                        cur_dist = math.hypot(cur_dx, cur_dy)

                    if (cur_dist is not None and cur_dist <= 0.55) or tag_visible:
                        dist_txt = f"{cur_dist:.2f}m" if cur_dist is not None else "알수없음"
                        if self.node:
                            self.node.get_logger().info(
                                f"[NOMI_NAV2] 🎯 로봇 경유지 도착 감지 (거리: {dist_txt}, 태그감지: {tag_visible}) -> 정상 도착으로 간주하고 AprilTag 비전 도킹 진행"
                            )
                        status = 4
                        break
                    time.sleep(1.0)

        # GoalStatus.STATUS_SUCCEEDED = 4
        if status == 4:
            if self.node:
                self.node.get_logger().info(f"[NOMI_NAV2] 🎉 '{target_name}' Nav2 경유지 확인 완료!")

            # AprilTag 자동 도킹 연계
            if auto_dock and self.enable_docking_pub is not None:
                if self.node:
                    self.node.get_logger().info(
                        f"[NOMI_NAV2] 🎯 '{target_name}' Nav2 경유지 도착 완료 -> "
                        f"AprilTag 비전 도킹 활성화 (/apriltag/enable_docking) 및 태그 탐색(Searching)/안착 대기..."
                    )
                self._docking_complete_event.clear()
                msg = Bool()
                msg.data = True
                self.enable_docking_pub.publish(msg)

                # 실제 비전 도킹이 완료(DOCKED)될 때까지 대기 (스윕 탐색 및 정밀 안착 포함 최대 90초)
                docked = self._docking_complete_event.wait(90.0)
                if docked:
                    if self.node:
                        self.node.get_logger().info(f"[NOMI_NAV2] 🎉 '{target_name}' AprilTag 비전 도킹 안착 최종 확인 완료!")
                    return {
                        "success": True,
                        "message": f"Nav2 자율주행 및 AprilTag 비전 도킹을 통해 '{target_name}'에 완전히 도킹 안착했습니다.",
                        "data": {
                            "destination": target_name,
                            "engine": "nav2+apriltag",
                            "position": {"x": tx, "y": ty, "yaw": tyaw_deg},
                            "completion_verified": True,
                            "auto_dock": True,
                            "docked": True,
                        },
                    }
                else:
                    if self.node:
                        self.node.get_logger().warn(
                            f"[NOMI_NAV2] ⚠️ '{target_name}' Nav2 경유지 도착 후 AprilTag 도킹 제한 시간(90초)이 초과되었습니다."
                        )
                    # 비전 도킹 타임아웃 시 도킹 노드 비활성화 및 정지 신호 전송
                    dis_msg = Bool()
                    dis_msg.data = False
                    self.enable_docking_pub.publish(dis_msg)
                    return {
                        "success": False,
                        "message": (
                            f"Nav2로 '{target_name}' 대기 위치까지는 이동했으나, "
                            f"AprilTag 비전 도킹(벽면 밀착 안착)이 시간 내에 완료되지 못했습니다. "
                            f"카메라 화각에 마커가 정상적으로 보이는지 확인해 주세요."
                        ),
                        "data": {
                            "destination": target_name,
                            "engine": "nav2+apriltag",
                            "position": {"x": nav_x, "y": nav_y, "yaw": tyaw_deg},
                            "completion_verified": False,
                            "auto_dock": True,
                            "docked": False,
                            "last_docking_status": self._last_docking_status,
                        },
                    }

            return {
                "success": True,
                "message": f"Nav2 자율주행을 통해 '{target_name}'에 성공적으로 도착했습니다.",
                "data": {
                    "destination": target_name,
                    "engine": "nav2",
                    "position": {"x": tx, "y": ty, "yaw": tyaw_deg},
                    "completion_verified": True,
                    "auto_dock": False,
                },
            }
        else:
            if auto_dock:
                if self.node:
                    self.node.get_logger().warn(
                        f"[NOMI_NAV2] ⚠️ Nav2 주행 상태 코드 {status} 중단 -> auto_dock 활성화 지점이므로 AprilTag 비전 도킹으로 즉시 인계 시도"
                    )
                return self.dock_to_station(target_name)

            return {
                "success": False,
                "message": f"Nav2 주행이 완료되지 못했습니다. (상태 코드: {status})",
                "data": {"destination": target_name, "engine": "nav2", "status": status},
            }

    def _execute_web_navigation(self, destination: str) -> dict:
        """Web Control 브라우저 내비게이션을 통한 주행 실행 (Nav2 미가동 시 폴백)"""
        request_id = str(uuid.uuid4())
        pending = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[request_id] = pending
            self._active_request_id = request_id
        message = String()
        message.data = json.dumps({"name": destination, "request_id": request_id}, ensure_ascii=False)
        self.destination_publisher.publish(message)
        if not pending["event"].wait(self.ARRIVAL_TIMEOUT_S):
            self._cancel_request(request_id)
            with self._pending_lock:
                self._pending.pop(request_id, None)
                if self._active_request_id == request_id:
                    self._active_request_id = None
            return {
                "success": False,
                "message": f"'{destination}' 도착 확인 시간이 초과되어 완료로 처리하지 않았습니다.",
                "data": {"destination": destination, "request_dispatched": True, "completion_verified": False, "error_code": "arrival_timeout"},
            }
        with self._pending_lock:
            result = pending["result"] or {}
            self._pending.pop(request_id, None)
            if self._active_request_id == request_id:
                self._active_request_id = None
        verified = (
            result.get("status") == "SUCCEEDED"
            and (
                result.get("completion_verified") is True
                or "도착" in str(result.get("message", ""))
                or (isinstance(result.get("distance_m"), (int, float)) and 0 <= result["distance_m"] <= 0.5)
            )
        )
        if verified:
            return {
                "success": True,
                "message": result.get("message") or f"'{destination}' 목적지 도착을 확인했습니다.",
                "data": {
                    "destination": destination,
                    "engine": "web_controller",
                    "request_dispatched": True,
                    "completion_verified": True,
                    "position": result.get("position") or {},
                    "distance_m": result.get("distance_m", 0.0),
                },
            }
        return {
            "success": False,
            "message": result.get("message") or f"'{destination}' 주행이 완료되지 않았습니다.",
            "data": {
                "destination": destination,
                "engine": "web_controller",
                "request_dispatched": True,
                "completion_verified": False,
                "navigation_status": result.get("status", "UNKNOWN"),
                "error_code": "navigation_not_arrived",
            },
        }

    def dock_to_station(self, target_name: str = "도킹 스테이션") -> dict:
        """현재 위치에서 즉시 AprilTag 비전 도킹 활성화 및 태그 탐색(Searching) 수행"""
        if self.enable_docking_pub is None:
            return {"success": False, "message": "도킹 활성화 연결이 준비되지 않았습니다."}

        if self.node:
            self.node.get_logger().info(f"[NOMI_NAV2] 🎯 '{target_name}' AprilTag 비전 도킹 활성화 -> 태그 스윕 탐색 및 자동 안착 시작...")

        self._docking_complete_event.clear()
        msg = Bool()
        msg.data = True
        self.enable_docking_pub.publish(msg)

        docked = self._docking_complete_event.wait(90.0)
        if docked:
            if self.node:
                self.node.get_logger().info(f"[NOMI_NAV2] 🎉 '{target_name}' AprilTag 비전 도킹 안착 최종 확인 완료!")
            return {
                "success": True,
                "message": f"AprilTag 비전 탐색 및 정밀 도킹을 통해 '{target_name}'에 완전히 도킹 안착했습니다.",
                "data": {"destination": target_name, "engine": "apriltag_docking", "docked": True},
            }
        else:
            if self.node:
                self.node.get_logger().warn(f"[NOMI_NAV2] ⚠️ '{target_name}' AprilTag 도킹 제한 시간(90초)이 초과되었습니다.")
            dis_msg = Bool()
            dis_msg.data = False
            self.enable_docking_pub.publish(dis_msg)
            return {
                "success": False,
                "message": "AprilTag 비전 도킹(벽면 밀착 안착)이 시간 내에 완료되지 못했습니다. 카메라 화각에 마커가 정상적으로 보이는지 확인해 주세요.",
                "data": {"destination": target_name, "engine": "apriltag_docking", "docked": False, "last_docking_status": self._last_docking_status},
            }

    def navigate_to(self, location: str | None = None) -> dict:
        normalized = (location or "").strip().lower()
        dock_keywords = {"도킹", "dock", "정밀도킹", "docking", "태그도킹", "도킹해줘", "도킹시작", "도킹해", "도킹 시작", "도킹해라", "도킹 진행", "소파 도킹", "소파에 도킹", "소파 도킹해줘", "소파도킹"}
        if normalized in dock_keywords or any(normalized.endswith(k) for k in ["도킹", "도킹해줘", "docking", "dock"]):
            return self.dock_to_station()

        command = self.DIRECTIONS.get(normalized)
        if command is None:
            destination = (location or "").strip()
            if not destination:
                return {"success": False, "message": "유효한 목적지 이름이 제공되지 않았습니다."}

            # 1. 대상 목적지 정보 조회
            dest_info = self._resolve_destination(destination)

            # 2. Nav2 Action Server 가용성 확인 (0.4초 빠른 핑)
            is_nav2_ready = False
            if self._nav2_client is not None and dest_info is not None:
                try:
                    is_nav2_ready = self._nav2_client.wait_for_server(timeout_sec=0.4)
                except Exception:
                    is_nav2_ready = False

            if is_nav2_ready:
                if self.node:
                    self.node.get_logger().info(f"[NOMI_AGENT] 🧭 Nav2 액션 서버 감지됨 -> Nav2로 '{destination}' 주행 실행")
                return self._execute_nav2_navigation(dest_info)

            # 3. Nav2 미가동 시 Web Control 내비게이션으로 안전 폴백(Fallback)
            if self.destination_publisher is None:
                return {"success": False, "message": "저장 목적지 주행 요청을 웹 내비게이션에 전달할 수 없습니다. 웹 브리지 연결을 확인해 주세요."}

            if self.node:
                self.node.get_logger().info(f"[NOMI_AGENT] 🌐 Web Control 엔진으로 '{destination}' 주행 실행")
            return self._execute_web_navigation(destination)

        # 단순 방향 수동 이동
        if self.publisher is None and self.real_cmd_vel_pub is None:
            return {"success": False, "message": "로봇 이동 제어 연결이 없습니다."}

        msg = Twist()
        msg.linear.x, msg.linear.y, msg.angular.z = command
        if self.publisher is not None:
            self.publisher.publish(msg)
        if self.real_cmd_vel_pub is not None:
            self.real_cmd_vel_pub.publish(msg)
        time.sleep(0.8)
        if self.publisher is not None:
            self.publisher.publish(Twist())
        if self.real_cmd_vel_pub is not None:
            self.real_cmd_vel_pub.publish(Twist())
        message = f"'{location}' 방향으로 짧게 이동했습니다."
        if self.node:
            self.node.get_logger().info(message)
        return {"success": True, "message": message, "data": {"direction": location, "duration_s": 0.8}}

    def stop(self) -> bool:
        """Request a stop and cancel the currently active Nav2 or web navigation."""
        stopped = False
        try:
            if self.node and hasattr(self.node, "context") and not self.node.context.ok():
                return False

            # Cancel active Nav2 goal
            if self._active_nav2_goal_handle is not None:
                try:
                    self._active_nav2_goal_handle.cancel_goal_async()
                    self._active_nav2_goal_handle = None
                    stopped = True
                except Exception:
                    pass

            if self.publisher is not None:
                self.publisher.publish(Twist())
                stopped = True
            if self.real_cmd_vel_pub is not None:
                self.real_cmd_vel_pub.publish(Twist())
                stopped = True
            if self.enable_docking_pub is not None:
                stop_dock = Bool()
                stop_dock.data = False
                self.enable_docking_pub.publish(stop_dock)

            with self._pending_lock:
                request_id = self._active_request_id
            if request_id:
                self._cancel_request(request_id)
                stopped = True
        except Exception:
            pass
        return stopped
