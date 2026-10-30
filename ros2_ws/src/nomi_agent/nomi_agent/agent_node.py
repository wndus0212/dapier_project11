"""ROS 2 entry point for NOMI's guarded task Harness."""

import json
import re

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from nomi_agent.core.execution_harness import ExecutionHarness
from nomi_agent.llm_client import LLMClient
from nomi_agent.tool_registry import ToolRegistry


class NomiAgentNode(Node):
    CANCEL_PHRASES = {"취소", "작업 취소", "중지", "정지", "멈춰", "그만", "stop", "cancel"}

    def __init__(self):
        super().__init__("nomi_agent")
        self.get_logger().info("NOMI Agent: validated Function/Tool Harness 시작")
        self.llm_client = LLMClient(max_history=12)
        self.get_logger().info(f"활성 모델 프로필: {self.llm_client.profile}")
        self.tool_registry = ToolRegistry(self)

        self.response_publisher = self.create_publisher(String, "/nomi/response", 10)
        self.task_status_publisher = self.create_publisher(String, "/nomi/task_status", 10)
        model_status_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self.model_status_publisher = self.create_publisher(String, "/nomi/model_status", model_status_qos)
        # Publish initial active profile latched so bridges/UIs receive it immediately
        init_model_msg = String()
        init_model_msg.data = self.llm_client.profile
        self.model_status_publisher.publish(init_model_msg)
        self.harness = ExecutionHarness(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            publish_response=self.publish_response,
            publish_status=self.publish_task_status,
            logger=self.get_logger(),
            audit_callback=self.audit_event,
        )

        self.command_subscriber = self.create_subscription(String, "/nomi/command", self.command_callback, 10)
        self.cancel_subscriber = self.create_subscription(String, "/nomi/cancel", self.cancel_callback, 10)
        self.model_switch_subscriber = self.create_subscription(String, "/nomi/model_switch", self.model_switch_callback, 10)
        destinations_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                                      durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.destinations_subscriber = self.create_subscription(
            String, "/nomi/navigation_destinations", self.destinations_callback, destinations_qos
        )
        self.get_logger().info("명령 토픽: /nomi/command · 취소 토픽: /nomi/cancel · 상태 토픽: /nomi/task_status")

    def get_tool_schemas(self):
        """Compatibility method; schemas come from the registry only."""
        return self.tool_registry.get_tool_schemas()

    def command_callback(self, message):
        text = message.data.strip()
        request_id = None
        validate_only = False
        if not text:
            return

        # Backward compatible: plain std_msgs/String remains accepted. Clients
        # may opt into deduplication by publishing {"request_id", "text"}.
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "text" in payload:
                text = str(payload.get("text", "")).strip()
                request_id = payload.get("request_id")
                validate_only = payload.get("validate_only") is True

        normalized = re.sub(r"\s+", " ", text.strip().lower())
        if normalized in self.CANCEL_PHRASES or any(token in normalized for token in ("작업 취소", "취소해", "멈춰", "그만해", "정지해")):
            self.cancel_callback(message)
            return

        result, task_id = self.harness.submit(text, request_id=request_id, validate_only=validate_only)
        if result == "started":
            self.publish_response("명령을 접수했습니다. 실행 결과를 확인하고 있습니다.")
        elif result == "duplicate":
            self.publish_response(f"중복 요청으로 판단해 다시 실행하지 않았습니다. 작업 ID: {task_id}")
        elif result == "busy":
            self.publish_response(f"다른 작업이 진행 중입니다. 완료 또는 취소 후 다시 요청해 주세요. 작업 ID: {task_id}")
        else:
            self.publish_response(task_id)

    def destinations_callback(self, message):
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and self.tool_registry.update_destinations(payload.get("destinations")):
            self.get_logger().info("웹앱 저장 목적지 동기화 완료")

    def cancel_callback(self, _message):
        cancelled, task_id = self.harness.cancel_active()
        if cancelled:
            self.publish_response(f"작업 중단을 요청했습니다. 정지 명령을 보냈습니다. 작업 ID: {task_id}")
        else:
            self.publish_response("현재 진행 중인 작업이 없습니다.")

    def model_switch_callback(self, message):
        requested = message.data.strip()
        status = String()
        active = self.harness.get_active()
        if active and active.get("status") not in {"NEEDS_CLARIFICATION", "SUCCEEDED", "FAILED", "CANCELLED"}:
            status.data = f"ERROR: 작업 실행 중에는 모델을 변경할 수 없습니다. 작업 ID: {active.get('task_id')}"
            self.model_status_publisher.publish(status)
            return
        try:
            self.llm_client.set_profile(requested)
            status.data = requested
            self.get_logger().info(f"모델 프로필 변경 완료: {requested}")
        except Exception as exc:
            status.data = f"ERROR: {exc}"
            self.get_logger().error(f"모델 프로필 변경 실패: {exc}")
        self.model_status_publisher.publish(status)

    def publish_response(self, text):
        message = String()
        message.data = str(text)
        self.response_publisher.publish(message)
        self.get_logger().info(f"NOMI 응답: {text}")

    def publish_task_status(self, payload):
        message = String()
        message.data = json.dumps(payload, ensure_ascii=False, default=str)
        self.task_status_publisher.publish(message)

    def audit_event(self, event):
        """Write one JSON object per line for task/tool diagnostics."""
        self.get_logger().info("NOMI_AUDIT " + json.dumps(event, ensure_ascii=False, default=str))


def main(args=None):
    rclpy.init(args=args)
    node = NomiAgentNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        try:
            node.harness.cancel_active()
            if rclpy.ok():
                node.tool_registry.emergency_stop()
        except Exception:
            pass
        finally:
            try:
                node.destroy_node()
            except Exception:
                pass
            if rclpy.ok():
                try:
                    rclpy.shutdown()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
