"""
Unified Remote Control Core.

This replaces all the individual controllers (ManiSkillController, MuJoCoController,
RealRobotController) with a single, simplified remote control interface that communicates
with robot hosts via ZeroMQ.
"""

import asyncio
import logging
import math
import time
from typing import Any, Dict, List, Optional

import zmq
import zmq.asyncio

from .config import ServerConfig
from .protocol import RobotProtocol, CommandType, ResponseType


class RemoteCore:
    """Unified remote control core for all robot types."""

    def __init__(self, config: ServerConfig):
        """Initialize the remote control core.

        Args:
            config: Server configuration with robot and connection settings
        """
        self.config = config
        self.config.validate()

        # ZeroMQ setup
        self.context = zmq.asyncio.Context()
        self.cmd_socket: Optional[zmq.asyncio.Socket] = None
        self.data_socket: Optional[zmq.asyncio.Socket] = None

        # Connection state
        self.connected = False
        self.last_ping_time = 0
        self.ping_interval = 5.0  # Ping every 5 seconds
        self.last_state_received = 0.0
        self._pong_event = asyncio.Event()
        self._receiver_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None

        # Robot state cache (Default to Home Station: -0.59, -0.88, -17.1 deg)
        home_yaw = math.radians(-17.1)
        self.robot_state = {
            'status': 'disconnected',
            'position': {'x': -0.59, 'y': -0.88, 'z': 0.0},
            'rotation': {'roll': 0.0, 'pitch': 0.0, 'yaw': home_yaw},
            'arm_joints': {'left': [0.0] * 6, 'right': [0.0] * 6},
            'base_joints': [-0.59, -0.88, home_yaw],
            'velocity': {
                'linear': {'x': 0.0, 'y': 0.0, 'z': 0.0},
                'angular': {'x': 0.0, 'y': 0.0, 'z': 0.0}
            },
            'obstacle_scan': [],
            'obstacle_scan_status': None
        }

        # Setup logging
        self.logger = logging.getLogger(f"RemoteCore-{config.robot_type}")
        self.logger.setLevel(logging.INFO)

        print(f"RemoteCore initialized for {config.robot_type}")
        print(f"Will connect to: {config.get_robot_cmd_address()}")

    async def connect(self) -> bool:
        """Connect to the robot host.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            self.logger.info(f"Connecting to {self.config.robot_type} host...")
            self.last_state_received = 0.0
            self._pong_event.clear()
            self.robot_state['status'] = 'disconnected'

            # Create ZeroMQ sockets
            self.cmd_socket = self.context.socket(zmq.PUSH)  # Send commands
            self.data_socket = self.context.socket(zmq.PULL)  # Receive data

            # Set socket options
            self.cmd_socket.setsockopt(zmq.LINGER, 1000)
            self.data_socket.setsockopt(zmq.LINGER, 1000)

            # Connect to robot host
            cmd_addr = self.config.get_robot_cmd_address()
            data_addr = self.config.get_robot_data_address()

            self.cmd_socket.connect(cmd_addr)
            self.data_socket.connect(data_addr)

            self.logger.info(f"ZeroMQ sockets connected")

            # Keep a single owner for data_socket.recv(); the receiver dispatches
            # PONG messages to this event while also collecting telemetry.
            self.connected = True
            self._receiver_task = asyncio.create_task(self._data_receiver_loop())
            ping_success = await self._ping_robot()
            if ping_success:
                self.robot_state['status'] = 'connected'
                self.logger.info(f"Successfully connected to {self.config.robot_type} host")

                self._ping_task = asyncio.create_task(self._ping_loop())

                return True
            else:
                self.logger.error("Failed to ping robot host")
                await self.disconnect()
                return False

        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            await self.disconnect()
            return False

    async def disconnect(self) -> bool:
        """Disconnect from robot host.

        Returns:
            True if disconnection successful
        """
        try:
            self.connected = False
            self.robot_state['status'] = 'disconnected'

            current_task = asyncio.current_task()
            background_tasks = [self._receiver_task, self._ping_task]
            self._receiver_task = None
            self._ping_task = None
            for task in background_tasks:
                if task is not None and task is not current_task and not task.done():
                    task.cancel()
            pending = [task for task in background_tasks if task is not None and task is not current_task]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if self.cmd_socket:
                self.cmd_socket.close()
                self.cmd_socket = None

            if self.data_socket:
                self.data_socket.close()
                self.data_socket = None

            self.logger.info("Disconnected from robot host")
            return True

        except Exception as e:
            self.logger.error(f"Disconnection error: {e}")
            return False

    async def move(self, direction: str, speed: float = 1.0, angular: float = 0.0, linear_x: float = 0.0, linear_y: float = 0.0, sequence: Optional[int] = None) -> Dict[str, Any]:
        """Send movement command to robot.

        Args:
            direction: Movement direction (forward, backward, left, right, etc.)
            speed: Movement speed (0.0 to 1.0)

        Returns:
            Response dictionary with status and details
        """
        if not self.connected:
            return {'status': 'error', 'message': 'Not connected to robot host'}

        try:
            # Validate inputs
            speed = max(0.0, min(1.0, speed))  # Clamp speed to [0, 1]

            # Send move command
            angular = max(-1.0, min(1.0, float(angular)))
            linear_x = max(-1.0, min(1.0, float(linear_x)))
            linear_y = max(-1.0, min(1.0, float(linear_y)))
            cmd_data = RobotProtocol.encode_move_command(direction, speed, angular, linear_x, linear_y, sequence)
            enqueue_started = time.perf_counter()
            await self.cmd_socket.send(cmd_data)
            enqueue_ms = (time.perf_counter() - enqueue_started) * 1000

            print(
                f"[navigation-trace] zmq_queued sequence={sequence} direction={direction} "
                f"map=({linear_x:+.3f},{linear_y:+.3f}) angular={angular:+.3f} "
                f"send_wait_ms={enqueue_ms:.2f}",
                flush=True,
            )

            return {
                'status': 'success',
                'direction': direction,
                'speed': speed,
                'angular': angular,
                'linear_x': linear_x,
                'linear_y': linear_y,
                'robot_type': self.config.robot_type,
                'sequence': sequence,
                'transport_status': 'queued_to_zmq',
            }

        except Exception as e:
            self.logger.error(f"Move command failed: {e}")
            return {'status': 'error', 'message': str(e)}

    async def stop(self, sequence: Optional[int] = None) -> Dict[str, Any]:
        """Stop robot movement.

        Returns:
            Response dictionary with status
        """
        return await self.move('stop', 0.0, sequence=sequence)

    async def get_state(self) -> Dict[str, Any]:
        """Get current robot state.

        Returns:
            Current robot state dictionary
        """
        state = self.robot_state.copy()
        now = time.time()
        state_age = now - self.last_state_received if self.last_state_received else None
        host_connected = bool(self.connected and state_age is not None and state_age <= 2.5)
        state['robot_type'] = self.config.robot_type
        state['connected'] = self.connected
        state['host_connected'] = host_connected
        state['state_age'] = state_age
        state['timestamp'] = now
        return state

    async def set_arm_joint(self, arm: str, joint_index: int, angle: float) -> Dict[str, Any]:
        """Set arm joint angle.

        Args:
            arm: Arm identifier ("left" or "right")
            joint_index: Joint index (0-based)
            angle: Target angle in radians

        Returns:
            Response dictionary with status
        """
        if not self.connected:
            return {'status': 'error', 'message': 'Not connected to robot host'}

        try:
            cmd_data = RobotProtocol.encode_arm_joint_command(arm, joint_index, angle)
            await self.cmd_socket.send(cmd_data)

            self.logger.debug(f"Sent arm joint command: {arm}[{joint_index}] = {angle}")

            return {
                'status': 'success',
                'arm': arm,
                'joint_index': joint_index,
                'angle': angle
            }

        except Exception as e:
            self.logger.error(f"Arm joint command failed: {e}")
            return {'status': 'error', 'message': str(e)}

    async def reset(self) -> Dict[str, Any]:
        """Reset robot to initial state.

        Returns:
            Response dictionary with status
        """
        if not self.connected:
            return {'status': 'error', 'message': 'Not connected to robot host'}

        try:
            cmd_data = RobotProtocol.encode_command(CommandType.RESET)
            await self.cmd_socket.send(cmd_data)

            self.logger.info("Sent reset command")

            return {'status': 'success', 'message': 'Reset command sent'}

        except Exception as e:
            self.logger.error(f"Reset command failed: {e}")
            return {'status': 'error', 'message': str(e)}

    async def get_camera_frame(self) -> Optional[bytes]:
        """Get latest camera frame bytes.

        Returns:
            Raw JPEG bytes or None if unavailable
        """
        return getattr(self, '_last_frame_bytes', None)

    async def get_camera_frame_base64(self) -> Optional[str]:
        """Get latest camera frame as base64 string.

        Returns:
            Base64 encoded JPEG frame or None if unavailable
        """
        return getattr(self, '_last_frame_b64', None)

    async def set_camera_position(self, position: List[float], target: Optional[List[float]] = None) -> Dict[str, Any]:
        """Set camera position and target.

        Args:
            position: Camera position [x, y, z]
            target: Optional camera target [x, y, z]

        Returns:
            Response dictionary with status
        """
        if not self.connected:
            return {'status': 'error', 'message': 'Not connected to robot host'}

        try:
            cmd_data = RobotProtocol.encode_camera_command(position, target)
            await self.cmd_socket.send(cmd_data)

            self.logger.debug(f"Sent camera command: pos={position}, target={target}")

            return {
                'status': 'success',
                'position': position,
                'target': target
            }

        except Exception as e:
            self.logger.error(f"Camera command failed: {e}")
            return {'status': 'error', 'message': str(e)}

    async def reset_camera(self) -> Dict[str, Any]:
        """Reset camera to default position.

        Returns:
            Response dictionary with status
        """
        if not self.connected:
            return {'status': 'error', 'message': 'Not connected to robot host'}

        try:
            cmd_data = RobotProtocol.encode_command(CommandType.RESET_CAMERA)
            await self.cmd_socket.send(cmd_data)

            self.logger.debug("Sent camera reset command")

            return {'status': 'success', 'message': 'Camera reset command sent'}

        except Exception as e:
            self.logger.error(f"Camera reset failed: {e}")
            return {'status': 'error', 'message': str(e)}

    def get_capabilities(self) -> Dict[str, bool]:
        """Get robot capabilities.

        Returns:
            Dictionary of capability flags
        """
        return {
            'movement': True,
            'arm_control': True,
            'camera_control': True,
            'video_streaming': True,
            'state_feedback': True,
            'remote_control': True,
            'robot_type': self.config.robot_type,
            'connected': self.connected
        }

    async def _ping_robot(self) -> bool:
        """Ping the robot host to test connectivity.

        Returns:
            True if ping successful
        """
        try:
            if not self.cmd_socket or not self.data_socket:
                return False
            self._pong_event.clear()
            cmd_data = RobotProtocol.encode_command(CommandType.PING)
            await self.cmd_socket.send(cmd_data)
            await asyncio.wait_for(self._pong_event.wait(), timeout=self.config.connect_timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

        except Exception as e:
            self.logger.error(f"Ping failed: {e}")
            return False

    async def _ping_loop(self):
        """Background task to periodically ping the robot host."""
        while self.connected:
            try:
                current_time = time.time()
                if current_time - self.last_ping_time >= self.ping_interval:
                    ping_success = await self._ping_robot()
                    if not ping_success:
                        if time.time() - self.last_state_received > 1.0:
                            self.logger.error("Robot host heartbeat and state stream are both stale")
                            self.connected = False
                            self.robot_state['status'] = 'disconnected'
                            break
                        self.logger.warning("Robot host ping timed out, but state telemetry is still live")
                    self.last_ping_time = current_time

                await asyncio.sleep(1.0)  # Check every second

            except Exception as e:
                self.logger.error(f"Ping loop error: {e}")
                break

    async def _data_receiver_loop(self):
        """Background task to receive data from robot host."""
        while self.connected:
            try:
                # Receive data with timeout
                data = await asyncio.wait_for(
                    self.data_socket.recv(),
                    timeout=self.config.polling_timeout_ms / 1000.0
                )

                # Decode response
                response = RobotProtocol.decode_response(data)

                # Process different response types
                response_type = response.get('response')

                response_data = response.get('data') or {}
                if response_type == ResponseType.SUCCESS.value and response_data.get('sequence') is not None:
                    print(
                        f"[navigation-trace] host_ack sequence={response_data.get('sequence')} "
                        f"host_status={response_data.get('host_status', 'received')}",
                        flush=True,
                    )

                if response_type == ResponseType.PONG.value:
                    self._pong_event.set()

                elif response_type == ResponseType.STATE.value:
                    self._update_robot_state(response.get('data', {}))
                    self.last_state_received = time.time()

                elif response_type == ResponseType.VIDEO.value:
                    self._update_video_frame(response.get('data', {}))

                elif response_type == ResponseType.ERROR.value:
                    self.logger.warning(f"Robot host error: {response.get('data', {}).get('message', 'Unknown error')}")

            except (asyncio.TimeoutError, zmq.Again):
                # Normal timeout - continue loop
                continue

            except Exception as e:
                self.logger.error(f"Data receiver error: {e}")
                # Brief pause before retrying
                await asyncio.sleep(0.1)

    def _update_robot_state(self, state_data: Dict[str, Any]):
        """Update cached robot state from received data."""
        try:
            # Update state fields that are present in the received data
            for key, value in state_data.items():
                if key in self.robot_state:
                    self.robot_state[key] = value

            self.robot_state['status'] = 'connected'

        except Exception as e:
            self.logger.error(f"Failed to update robot state: {e}")

    def _update_video_frame(self, video_data: Dict[str, Any]):
        """Update cached video frame from received data."""
        try:
            # Cache base64 frame
            frame_b64 = video_data.get('frame')
            if frame_b64:
                self._last_frame_b64 = frame_b64

                frame_bytes = RobotProtocol.decode_video_frame({'response': ResponseType.VIDEO.value, 'data': video_data})
                if frame_bytes:
                    self._last_frame_bytes = frame_bytes

        except Exception as e:
            self.logger.error(f"Failed to update video frame: {e}")

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.disconnect()
