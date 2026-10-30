"""Optional bridge between the Web Control server and the NOMI ROS graph."""

from __future__ import annotations

import threading
import json
import math
import time
from pathlib import Path
from typing import Callable

from .scan_matcher import ScanMatcher


class NomiBridge:
    """Publish NOMI commands and forward ROS responses to the asyncio server."""

    def __init__(
        self,
        on_response: Callable[[str], None] | None,
        on_model_status: Callable[[str], None],
        on_task_status: Callable[[dict], None] | None = None,
        on_connection_status: Callable[[bool, str | None], None] | None = None,
        on_navigation_request: Callable[[dict], None] | None = None,
        on_navigation_cancel: Callable[[str], None] | None = None,
        on_docking_status: Callable[[dict], None] | None = None,
        on_arm_task_complete: Callable[[dict], None] | None = None,
    ):
        self._on_response = on_response
        self._on_model_status = on_model_status
        self._on_task_status = on_task_status or (lambda _payload: None)
        self._on_connection_status = on_connection_status or (lambda _connected, _error: None)
        self._on_navigation_request = on_navigation_request or (lambda _payload: None)
        self._on_navigation_cancel = on_navigation_cancel or (lambda _request_id: None)
        self._on_docking_status = on_docking_status or (lambda _payload: None)
        self._on_arm_task_complete = on_arm_task_complete or (lambda _payload: None)
        self._node = None
        self._thread: threading.Thread | None = None
        self._executor = None
        self._rclpy = None
        self._string_type = None
        self._owns_rclpy_context = False
        self.available = False
        self.agent_connected = False
        self.error: str | None = None

        # Real AMR odometry tracking
        self.has_odom = False
        self.docking_status = None
        self._pose_file = Path(__file__).resolve().parent.parent.parent / "shared" / "last_robot_pose.json"
        self._last_pose_save_time = 0.0
        saved_pose = self._load_saved_pose()
        init_x = float(saved_pose.get('x', -0.59))
        init_y = float(saved_pose.get('y', -0.88))
        init_yaw = float(saved_pose.get('yaw', math.radians(-17.1)))

        self.base_map_pose = {'x': init_x, 'y': init_y, 'yaw': init_yaw}
        self.base_odom_pose = None
        self.robot_x = init_x
        self.robot_y = init_y
        self.robot_yaw = init_yaw
        self.last_odom_time = 0.0
        self.last_tf_time = 0.0
        self.laser_offset_x = 0.095  # Laser frame offset from base_footprint (meters)

        # AMR 오도메트리 물리 캘리브레이션 계수
        # STS3215 서보는 차체 하중 및 지면 마찰로 인해 명령 속도 대비 실속도가 약 70~75% 수준임
        # 4륜 스키드 조향 시 지면 횡마찰(scrubbing)로 인해 회전 각속도 역시 이론값 대비 약 78% 수준으로 회전함
        self.odom_linear_scale = 0.72
        self.odom_angular_scale = 0.78
        self.last_scan_refine_time = 0.0

        # Real LiDAR scan tracking
        self.obstacle_scan = []
        self.obstacle_scan_status = {
            "healthy": False,
            "timestamp": 0.0,
            "age_ms": 0.0,
            "status": "waiting",
            "range_m": 12.0,
            "hit_count": 0,
            "beam_count": 0,
        }
        self.last_scan_time = 0.0
        self.latest_scan_msg = None
        self.has_auto_aligned = True  # 시작 시 임의 벽면 오정합 방지: 기본 True
        self.last_amcl_time = 0.0
        self.first_scan_time = 0.0
        self.last_wall_align_result = None
        self.scan_matcher = ScanMatcher()

    def _load_saved_pose(self) -> dict:
        try:
            if hasattr(self, '_pose_file') and self._pose_file.is_file():
                with open(self._pose_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict) and 'x' in data and 'y' in data and 'yaw' in data:
                        return data
        except Exception:
            pass
        return {}

    def _save_saved_pose(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - getattr(self, '_last_pose_save_time', 0.0) < 1.0:
            return
        self._last_pose_save_time = now
        try:
            if hasattr(self, '_pose_file'):
                self._pose_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._pose_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        'x': round(float(self.robot_x), 4),
                        'y': round(float(self.robot_y), 4),
                        'yaw': round(float(self.robot_yaw), 4),
                        'timestamp': now
                    }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return self.available
        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
            from std_msgs.msg import String, Bool
            from geometry_msgs.msg import Twist, PoseWithCovarianceStamped
            from nav_msgs.msg import Odometry
            from sensor_msgs.msg import LaserScan
            import tf2_ros

            self._rclpy = rclpy
            self._string_type = String
            self._twist_type = Twist
            self._owns_rclpy_context = not rclpy.ok()
            if not rclpy.ok():
                rclpy.init(args=None)

            bridge = self

            class BridgeNode(Node):
                def __init__(self):
                    super().__init__("xlerobot_web_nomi_bridge")
                    self.cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
                    self.cmd_vel_nav_pub = self.create_publisher(Twist, "/cmd_vel_nav", 10)
                    self.command_pub = self.create_publisher(String, "/nomi/command", 10)
                    self.cancel_pub = self.create_publisher(String, "/nomi/cancel", 10)
                    self.nav_cancel_pub = self.create_publisher(String, "/nomi/navigation_cancel", 10)
                    self.model_pub = self.create_publisher(String, "/nomi/model_switch", 10)
                    self.navigation_result_pub = self.create_publisher(String, "/nomi/navigation_result", 10)
                    initial_pose_qos = QoSProfile(
                        depth=1,
                        durability=DurabilityPolicy.TRANSIENT_LOCAL,
                        reliability=ReliabilityPolicy.RELIABLE,
                    )
                    self.initial_pose_pub = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", initial_pose_qos)
                    self.enable_docking_pub = self.create_publisher(Bool, "/apriltag/enable_docking", 10)
                    self.docking_complete_pub = self.create_publisher(String, "/amr/docking_complete", 10)
                    self.arm_task_complete_pub = self.create_publisher(String, "/arm/task_complete", 10)
                    destinations_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                                                  durability=DurabilityPolicy.TRANSIENT_LOCAL)
                    self.destinations_pub = self.create_publisher(
                        String, "/nomi/navigation_destinations", destinations_qos
                    )
                    if bridge._on_response is not None:
                        self.create_subscription(String, "/nomi/response", lambda msg: bridge._on_response(msg.data), 10)
                    model_status_qos = QoSProfile(
                        depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
                    )
                    self.create_subscription(String, "/nomi/model_status", lambda msg: bridge._on_model_status(msg.data), model_status_qos)
                    self.create_subscription(String, "/nomi/task_status", bridge._task_status_callback, 10)
                    self.create_subscription(String, "/nomi/navigation_request", bridge._navigation_request_callback, 10)
                    self.create_subscription(String, "/nomi/navigation_cancel", bridge._navigation_cancel_callback, 10)

                    # Real AMR odometry, pose, and lidar subscriptions (SensorDataQoS for best compatibility)
                    self.create_subscription(Odometry, "/odom", bridge._odom_callback, qos_profile_sensor_data)
                    self.create_subscription(LaserScan, "/scan", bridge._scan_callback, qos_profile_sensor_data)
                    self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", bridge._amcl_pose_callback, 10)
                    self.create_subscription(PoseWithCovarianceStamped, "/initialpose", bridge._initial_pose_callback, 10)

                    # TF2 Listener for direct, live map -> base_footprint tracking (matches RViz 100%)
                    self.tf_buffer = tf2_ros.Buffer()
                    self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)
                    self.create_timer(0.05, self._tf_pose_update)

                    # Precision visual docking status subscription
                    self.create_subscription(String, "/apriltag/docking_status", bridge._docking_status_callback, 10)

                    # External robot arm (secondary laptop) handoff subscription
                    self.create_subscription(String, "/arm/task_complete", bridge._arm_task_complete_callback, 10)
                    self.create_subscription(String, "/amr/arm_task_complete", bridge._arm_task_complete_callback, 10)

                    self._consecutive_offline_ticks = 0
                    self.create_timer(0.5, self._refresh_agent_status)

                def _tf_pose_update(self):
                    try:
                        target_frame = 'base_footprint'
                        if not self.tf_buffer.can_transform('map', target_frame, rclpy.time.Time()):
                            target_frame = 'base_link'
                            if not self.tf_buffer.can_transform('map', target_frame, rclpy.time.Time()):
                                return
                        t = self.tf_buffer.lookup_transform('map', target_frame, rclpy.time.Time())
                        trans = t.transform.translation
                        rot = t.transform.rotation
                        yaw = 2.0 * math.atan2(float(rot.z), float(rot.w))

                        # TF 타임스탬프 유효성 검사 (오래된 과거 캐시 TF로 인한 위치 강제 리셋 방지)
                        tf_time = t.header.stamp.sec + t.header.stamp.nanosec * 1e-9
                        current_ros_time = self.get_clock().now().nanoseconds * 1e-9
                        if abs(current_ros_time - tf_time) > 2.5:
                            return  # AMCL이 멈췄거나 TF가 단절된 경우 오래된 과거 위치로 덮어쓰기 방지

                        now = time.time()
                        # 최근 사용자 시작 위치 재배치(< 3.0초) 보호: AMCL TF가 아직 예전 위치를 가리키고 있다면 덮어쓰기 무시
                        if now - getattr(bridge, 'last_initial_pose_time', 0.0) < 3.0:
                            target = getattr(bridge, 'target_initial_pose', None)
                            if target is not None:
                                dist = math.hypot(trans.x - target['x'], trans.y - target['y'])
                                if dist > 0.35:
                                    return

                        bridge.robot_x = float(trans.x)
                        bridge.robot_y = float(trans.y)
                        bridge.robot_yaw = float(yaw)
                        bridge.base_map_pose = {'x': bridge.robot_x, 'y': bridge.robot_y, 'yaw': bridge.robot_yaw}
                        bridge.base_odom_pose = None
                        bridge.has_odom = True
                        bridge.last_odom_time = time.time()
                        bridge.last_tf_time = time.time()
                        bridge._save_saved_pose()
                    except Exception:
                        pass

                def _refresh_agent_status(self):
                    cmd_subs = self.count_subscribers("/nomi/command")
                    task_pubs = self.count_publishers("/nomi/task_status")
                    raw_connected = (cmd_subs > 0 and task_pubs > 0)
                    if not raw_connected and self._consecutive_offline_ticks % 10 == 0:
                        print(f"[NOMI_BRIDGE] status check: cmd_subs={cmd_subs}, task_pubs={task_pubs}, bridge_connected={bridge.agent_connected}")
                    elif raw_connected and not bridge.agent_connected:
                        print(f"[NOMI_BRIDGE] connected! cmd_subs={cmd_subs}, task_pubs={task_pubs}")
                    if raw_connected:
                        self._consecutive_offline_ticks = 0
                        connected = True
                    else:
                        self._consecutive_offline_ticks += 1
                        connected = False if self._consecutive_offline_ticks >= 3 else bridge.agent_connected

                    if connected != bridge.agent_connected:
                        bridge.agent_connected = connected
                        bridge._on_connection_status(connected, None)

                def publish(self, topic: str, value: str):
                    msg = String()
                    msg.data = value
                    publishers = {
                        "command": self.command_pub,
                        "cancel": self.cancel_pub,
                        "model": self.model_pub,
                    }
                    publishers[topic].publish(msg)

            self._node = BridgeNode()
            self._executor = SingleThreadedExecutor()
            self._executor.add_node(self._node)
            self._thread = threading.Thread(target=self._executor.spin, name="nomi-ros-bridge", daemon=True)
            self._thread.start()
            self.available = True
            self.error = None
            self.agent_connected = False
        except Exception as exc:  # ROS 2 is optional for standalone Web Control.
            self.available = False
            self.error = str(exc)
            self.agent_connected = False
            self._on_connection_status(False, self.error)
        return self.available

    def _task_status_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self._on_task_status(payload)

    def _navigation_request_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        name = payload.get("name")
        request_id = payload.get("request_id")
        if (isinstance(name, str) and name.strip() and len(name.strip()) <= 120
                and isinstance(request_id, str) and request_id.strip() and len(request_id) <= 120):
            self._on_navigation_request({"name": name.strip(), "request_id": request_id.strip()})

    def _navigation_cancel_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        if isinstance(request_id, str) and request_id.strip() and len(request_id) <= 120:
            self._on_navigation_cancel(request_id.strip())

    def _docking_status_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self.docking_status = payload
            self._on_docking_status(payload)

    def _arm_task_complete_callback(self, message) -> None:
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            payload = {"status": "SUCCEEDED", "raw": getattr(message, "data", "")}
        if isinstance(payload, dict):
            self._on_arm_task_complete(payload)

    def publish_docking_complete(self, payload: dict) -> bool:
        if not self.available or self._node is None or not hasattr(self._node, 'docking_complete_pub'):
            return False
        try:
            msg = self._string_type()
            msg.data = json.dumps(payload, ensure_ascii=False)
            self._node.docking_complete_pub.publish(msg)
            return True
        except Exception:
            return False

    def publish_arm_task_complete(self, payload: dict) -> bool:
        if not self.available or self._node is None or not hasattr(self._node, 'arm_task_complete_pub'):
            return False
        try:
            msg = self._string_type()
            msg.data = json.dumps(payload, ensure_ascii=False)
            self._node.arm_task_complete_pub.publish(msg)
            self._on_arm_task_complete(payload)
            return True
        except Exception as e:
            print(f"[NOMI_BRIDGE] publish_arm_task_complete error: {e}")
            return False

    def set_docking_enabled(self, enabled: bool) -> bool:
        if not self.available or self._node is None or not hasattr(self._node, 'enable_docking_pub'):
            return False
        try:
            from std_msgs.msg import Bool
            msg = Bool()
            msg.data = bool(enabled)
            self._node.enable_docking_pub.publish(msg)
            return True
        except Exception:
            return False

    def publish_navigation_result(self, payload: dict) -> bool:
        if not self.available or not self.agent_connected or self._node is None:
            return False
        message = self._string_type()
        message.data = json.dumps(payload, ensure_ascii=False)
        self._node.navigation_result_pub.publish(message)
        return True

    def send_command(self, text: str, request_id: str, validate_only: bool = False) -> bool:
        if not self.available or not self.agent_connected or self._node is None:
            return False
        self._node.publish("command", json.dumps({
            "request_id": request_id, "text": text, "validate_only": bool(validate_only),
        }, ensure_ascii=False))
        return True

    def sync_destinations(self, destinations: list[dict]) -> bool:
        if not self.available or not self.agent_connected or self._node is None:
            return False
        message = self._string_type()
        message.data = json.dumps({"destinations": destinations}, ensure_ascii=False)
        self._node.destinations_pub.publish(message)
        return True

    def cancel_task(self, task_id: str) -> bool:
        if not self.available or not self.agent_connected or self._node is None:
            return False
        self._node.publish("cancel", json.dumps({"task_id": task_id}, ensure_ascii=False))
        return True

    def switch_model(self, profile: str) -> bool:
        if not self.available or not self.agent_connected or self._node is None:
            return False
        self._node.publish("model", profile)
        return True

    def send_cmd_vel(self, linear_x: float = 0.0, angular_z: float = 0.0) -> bool:
        if not self.available or self._node is None:
            return False
        msg = self._twist_type()
        msg.linear.x = float(linear_x)
        msg.angular.z = float(angular_z)
        self._node.cmd_vel_pub.publish(msg)
        if abs(linear_x) > 0.001 or abs(angular_z) > 0.001:
            print(f"[NOMI_BRIDGE] 🚀 /cmd_vel 발행: linear={linear_x:+.2f} m/s, angular={angular_z:+.2f} rad/s")
        return True

    def send_direction(self, direction: str, speed: float = 1.0, angular: float = 0.0, linear_x: float = 0.0, linear_y: float = 0.0) -> bool:
        if not self.available or self._node is None:
            return False
        max_lin = 0.35
        max_ang = 0.75
        speed_clamped = max(0.0, min(1.0, float(speed)))

        lx = 0.0
        az = 0.0
        if direction == 'forward':
            lx = max_lin * speed_clamped
            if angular != 0.0:
                az = max(-max_ang, min(max_ang, float(angular) * max_ang if abs(angular) <= 1.0 else float(angular)))
        elif direction == 'backward':
            lx = -max_lin * speed_clamped
            if angular != 0.0:
                az = max(-max_ang, min(max_ang, float(angular) * max_ang if abs(angular) <= 1.0 else float(angular)))
        elif direction == 'rotate_left':
            az = max_ang * speed_clamped
        elif direction == 'rotate_right':
            az = -max_ang * speed_clamped
        elif direction == 'left':
            lx = max_lin * 0.7 * speed_clamped
            az = max_ang * 0.7 * speed_clamped
        elif direction == 'right':
            lx = max_lin * 0.7 * speed_clamped
            az = -max_ang * 0.7 * speed_clamped
        elif direction == 'drive_vector':
            import math
            lin_mag = math.hypot(linear_x, linear_y)
            if lin_mag > 0.005:
                # Use lin_mag directly without squaring by speed_clamped
                lx = max(-max_lin, min(max_lin, lin_mag))
            else:
                lx = 0.0
            az = max(-max_ang, min(max_ang, float(angular)))
        elif direction == 'stop':
            lx = 0.0
            az = 0.0

        return self.send_cmd_vel(lx, az)

    def cancel_all_navigation(self) -> None:
        """모든 자율주행, Nav2 목표, 도킹, NOMI 에이전트 작업을 즉시 중단하고 모터를 정지"""
        if self._node is None:
            return
        try:
            # 1. /nomi/cancel 발행 (NOMI Agent 작업 및 Nav2 Action 목표 취소)
            msg_cancel = self._string_type()
            msg_cancel.data = json.dumps({"command": "stop", "reason": "emergency_stop"}, ensure_ascii=False)
            if hasattr(self._node, 'cancel_pub'):
                self._node.cancel_pub.publish(msg_cancel)

            # 2. /apriltag/enable_docking -> False (AprilTag 비전 도킹 즉각 해제)
            if hasattr(self._node, 'enable_docking_pub'):
                from std_msgs.msg import Bool
                dock_msg = Bool()
                dock_msg.data = False
                self._node.enable_docking_pub.publish(dock_msg)

            # 3. /nomi/navigation_cancel 발행
            if hasattr(self._node, 'nav_cancel_pub'):
                nav_c = self._string_type()
                nav_c.data = json.dumps({"request_id": "all"}, ensure_ascii=False)
                self._node.nav_cancel_pub.publish(nav_c)

            # 4. /cmd_vel 및 /cmd_vel_nav에 정지 명령(0) 송출
            stop_twist = self._twist_type()
            stop_twist.linear.x = 0.0
            stop_twist.linear.y = 0.0
            stop_twist.angular.z = 0.0
            for _ in range(3):
                self._node.cmd_vel_pub.publish(stop_twist)
                if hasattr(self._node, 'cmd_vel_nav_pub'):
                    self._node.cmd_vel_nav_pub.publish(stop_twist)
            print("[NOMI_BRIDGE] 🛑 긴급 정지 완료: 모든 자율주행/도킹/에이전트 작업 취소 및 모터 즉시 정지")
        except Exception as e:
            print(f"[NOMI_BRIDGE] ⚠️ 긴급 정지 처리 중 오류: {e}")

    def _odom_callback(self, msg) -> None:
        try:
            # If TF map->base_footprint is active (from AMCL/SLAM), prioritize TF scan-matching
            if time.time() - self.last_tf_time < 1.0:
                return

            # 사용자 위치 재배치 직후에는 오도메트리 델타 누적 일시 중지
            if time.time() - getattr(self, 'last_initial_pose_time', 0.0) < 1.5:
                return

            ox = float(msg.pose.pose.position.x)
            oy = float(msg.pose.pose.position.y)
            qz = float(msg.pose.pose.orientation.z)
            qw = float(msg.pose.pose.orientation.w)
            oyaw = 2.0 * math.atan2(qz, qw)

            if self.base_odom_pose is None:
                self.base_odom_pose = {'x': ox, 'y': oy, 'yaw': oyaw}

            # Delta in odom local frame
            dx_odom = ox - self.base_odom_pose['x']
            dy_odom = oy - self.base_odom_pose['y']
            dyaw = oyaw - self.base_odom_pose['yaw']

            # Transform delta from odom frame into map frame:
            # odom 프레임과 map 프레임 간의 회전 각도는 (base_map_yaw - base_odom_yaw)입니다.
            # 실기체 마찰/하중을 보정한 odom_linear_scale을 적용하여 실제 로봇과 1:1 동기화
            rot_odom_to_map = self.base_map_pose['yaw'] - self.base_odom_pose['yaw']
            cos_rot = math.cos(rot_odom_to_map)
            sin_rot = math.sin(rot_odom_to_map)

            scale_lin = getattr(self, 'odom_linear_scale', 0.72)
            scale_ang = getattr(self, 'odom_angular_scale', 1.0)

            dx_map = (dx_odom * cos_rot - dy_odom * sin_rot) * scale_lin
            dy_map = (dx_odom * sin_rot + dy_odom * cos_rot) * scale_lin

            self.robot_x = self.base_map_pose['x'] + dx_map
            self.robot_y = self.base_map_pose['y'] + dy_map
            self.robot_yaw = math.atan2(
                math.sin(self.base_map_pose['yaw'] + dyaw * scale_ang),
                math.cos(self.base_map_pose['yaw'] + dyaw * scale_ang)
            )
            self.has_odom = True
            self.last_odom_time = time.time()
            self._save_saved_pose()
        except Exception:
            pass

    def _amcl_pose_callback(self, msg) -> None:
        try:
            px = float(msg.pose.pose.position.x)
            py = float(msg.pose.pose.position.y)
            qz = float(msg.pose.pose.orientation.z)
            qw = float(msg.pose.pose.orientation.w)
            pyaw = 2.0 * math.atan2(qz, qw)

            now = time.time()
            if now - getattr(self, 'last_initial_pose_time', 0.0) < 1.5:
                target = getattr(self, 'target_initial_pose', None)
                if target is not None:
                    dist = math.hypot(px - target['x'], py - target['y'])
                    if dist > 0.40:
                        return

            self.robot_x = px
            self.robot_y = py
            self.robot_yaw = pyaw
            self.base_map_pose = {'x': px, 'y': py, 'yaw': pyaw}
            self.base_odom_pose = None  # Re-anchor on next /odom tick
            self.has_odom = True
            self.last_odom_time = time.time()
            self.last_tf_time = time.time()
            self.last_amcl_time = time.time()
            self._save_saved_pose()
            print(f"[NOMI_BRIDGE] 📍 RViz/AMCL 위치 동기화 완료: ({px:.3f}, {py:.3f}), yaw={math.degrees(pyaw):.1f}°")
        except Exception as exc:
            pass

    def _initial_pose_callback(self, msg) -> None:
        self._amcl_pose_callback(msg)

    def set_initial_pose(self, x: float, y: float, yaw: float) -> bool:
        """웹 UI 또는 스캔 매칭에서 로봇 초기 위치(/initialpose)를 강제 설정"""
        try:
            self.base_map_pose = {'x': float(x), 'y': float(y), 'yaw': float(yaw)}
            self.base_odom_pose = None  # Re-anchor on next /odom tick
            self.robot_x = float(x)
            self.robot_y = float(y)
            self.robot_yaw = float(yaw)
            self.has_odom = True
            self.last_initial_pose_time = time.time()
            self.target_initial_pose = {'x': float(x), 'y': float(y), 'yaw': float(yaw)}
            self._save_saved_pose(force=True)

            if self._node is not None and hasattr(self._node, 'initial_pose_pub'):
                from geometry_msgs.msg import PoseWithCovarianceStamped

                def _burst_publish():
                    for delay in [0.0, 0.08, 0.22]:
                        if delay > 0:
                            time.sleep(delay)
                        msg = PoseWithCovarianceStamped()
                        msg.header.frame_id = 'map'
                        msg.header.stamp = self._node.get_clock().now().to_msg()
                        msg.pose.pose.position.x = float(x)
                        msg.pose.pose.position.y = float(y)
                        msg.pose.pose.position.z = 0.0
                        msg.pose.pose.orientation.z = math.sin(yaw / 2.0)
                        msg.pose.pose.orientation.w = math.cos(yaw / 2.0)
                        cov = [0.0] * 36
                        cov[0] = 0.15      # X variance (0.38m)^2
                        cov[7] = 0.15      # Y variance (0.38m)^2
                        cov[35] = 0.05     # Yaw variance (12.8 deg)^2
                        msg.pose.covariance = cov
                        self._node.initial_pose_pub.publish(msg)
                    print(f"[NOMI_BRIDGE] 📡 AMCL /initialpose 전송 완료 (3회 버스트): ({x:.3f}, {y:.3f}, yaw={math.degrees(yaw):.1f}°)")

                threading.Thread(target=_burst_publish, daemon=True).start()
            return True
        except Exception as exc:
            print(f"[NOMI_BRIDGE] ⚠️ set_initial_pose 실패: {exc}")
            return False

    def _scan_callback(self, msg) -> None:
        try:
            now = time.time()
            if now - self.last_scan_time < 0.15:  # ~6.7 Hz throttle
                return
            self.last_scan_time = now

            self.latest_scan_msg = msg

            # 실시간 연속 라이다 벽면 미세 트래킹 (AMCL 비활성 시에만 작동)
            # Nav2 AMCL/TF (map -> base_footprint)가 활성화되어 있는 동안에는
            # AMCL 전역 추정과 상충하여 라이다 점이 뒤틀렸다가 돌아오는(hunting/jitter) 현상을 완전히 방지함
            is_amcl_active = (now - getattr(self, 'last_amcl_time', 0.0) < 2.0) or (now - self.last_tf_time < 2.0)
            if not is_amcl_active and self.has_odom and self.scan_matcher.available:
                if now - self.last_scan_refine_time >= 0.25:
                    self.last_scan_refine_time = now
                    try:
                        refined = self.scan_matcher.refine_local_pose(
                            msg, self.robot_x, self.robot_y, self.robot_yaw
                        )
                        if refined is not None:
                            opt_x, opt_y, opt_yaw, resid_cm = refined
                            self.robot_x = opt_x
                            self.robot_y = opt_y
                            self.robot_yaw = opt_yaw
                            # Re-anchor base_map_pose so future odom ticks accumulate from this refined pose
                            self.base_map_pose = {'x': opt_x, 'y': opt_y, 'yaw': opt_yaw}
                            self.base_odom_pose = None
                    except Exception:
                        pass

            ranges = msg.ranges
            angle_min = float(msg.angle_min)
            angle_inc = float(msg.angle_increment)
            r_min = max(0.15, float(msg.range_min))
            r_max = min(12.0, float(msg.range_max))

            rx = self.robot_x
            ry = self.robot_y
            ryaw = self.robot_yaw
            cos_yaw = math.cos(ryaw)
            sin_yaw = math.sin(ryaw)

            hits = []
            laser_offset = getattr(self, 'laser_offset_x', 0.095)
            step = max(1, len(ranges) // 180)
            for i in range(0, len(ranges), step):
                r = float(ranges[i])
                if not math.isfinite(r) or r < r_min or r > r_max:
                    continue
                theta = angle_min + i * angle_inc
                # Laser beam position in local laser frame
                px_l = r * math.cos(theta)
                py_l = r * math.sin(theta)
                # Offset by physical laser mount (0.095m forward in base frame)
                px_b = px_l + laser_offset
                py_b = py_l
                px_m = rx + px_b * cos_yaw - py_b * sin_yaw
                py_m = ry + px_b * sin_yaw + py_b * cos_yaw
                hits.append({'x': round(px_m, 3), 'y': round(py_m, 3)})

            self.obstacle_scan = hits
            self.obstacle_scan_status = {
                "healthy": True,
                "timestamp": now,
                "age_ms": 0.0,
                "status": "ok",
                "range_m": r_max,
                "hit_count": len(hits),
                "beam_count": len(ranges),
            }
            if not self.has_odom:
                self.has_odom = True
                self.last_odom_time = now
        except Exception as exc:
            print(f"[NOMI_BRIDGE] ⚠️ _scan_callback error: {exc}")

    def align_with_walls(
        self,
        init_x: float | None = None,
        init_y: float | None = None,
        init_yaw: float | None = None,
        search_radius: float = 1.5,
        search_yaw_deg: float = 180.0,
        hint_name: str | None = None,
    ) -> dict[str, Any]:
        """실시간 수신된 라이다 스캔과 SLAM 맵 벽면을 대조하여 로봇의 위치(x, y, yaw)를 밀리초 단위로 정밀 보정"""
        if self.latest_scan_msg is None:
            return {
                "success": False,
                "message": "수신된 실시간 라이다(/scan) 데이터가 없습니다. 라이다 노드가 실행 중인지 확인하세요.",
                "x": self.robot_x,
                "y": self.robot_y,
                "yaw": self.robot_yaw,
                "yaw_deg": round(math.degrees(self.robot_yaw), 1),
                "residual_cm": 999.0,
            }

        ix = float(init_x) if init_x is not None else None
        iy = float(init_y) if init_y is not None else None
        if init_yaw is not None:
            iyaw = float(init_yaw)
            # 프론트엔드에서 도 단위(degree)로 넘어온 경우 라디안 변환
            if abs(iyaw) > math.pi * 2:
                iyaw = math.radians(iyaw)
        else:
            iyaw = None

        result = self.scan_matcher.align_scan(
            self.latest_scan_msg,
            init_x=ix,
            init_y=iy,
            init_yaw=iyaw,
            search_radius=search_radius,
            search_yaw_deg=search_yaw_deg,
            hint_name=hint_name,
        )

        self.last_wall_align_result = result

        if result.get("success"):
            new_x = float(result["x"])
            new_y = float(result["y"])
            new_yaw = float(result["yaw"])
            # 로봇 위치 갱신 및 /initialpose 발행
            self.set_initial_pose(new_x, new_y, new_yaw)
            print(
                f"[NOMI_BRIDGE] 🎯 벽면 스캔 매칭 성공: ({new_x:.3f}, {new_y:.3f}), "
                f"yaw={math.degrees(new_yaw):.1f}°, 잔차오차={result['residual_cm']:.1f}cm"
            )
        else:
            print(f"[NOMI_BRIDGE] ⚠️ 벽면 스캔 매칭 결과: {result.get('message')}")

        return result

    def stop(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(timeout_sec=1.0)
            self._executor = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self._node is not None:
            self._node.destroy_node()
            self._node = None
        if self._owns_rclpy_context and self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()
        self._owns_rclpy_context = False
        self.available = False
        self.agent_connected = False
