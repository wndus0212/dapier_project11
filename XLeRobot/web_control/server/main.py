import time
import asyncio
import math
import json
import uuid
import os
from pathlib import Path

import socketio
import uvicorn
from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
import httpx
from api.streaming import video_manager
from api.towel_viewer import router as towel_viewer_router
from core.config import ServerConfig
from core.remote_core import RemoteCore
from core.nomi_bridge import NomiBridge
from core.model_profiles import MODEL_PROFILES

# Load simplified configuration
config = ServerConfig.from_env('.env')
config.validate()

print(f"Server Configuration:")
print(config)

# Create unified remote control core
remote_core = RemoteCore(config)
video_manager.attach_remote_core(remote_core)
main_event_loop = None
nomi_bridge = None
nomi_task_owners: dict[str, str] = {}
nomi_task_payloads: dict[str, dict] = {}
nomi_navigation_owners: dict[str, str] = {}
nomi_active_task_id: str | None = None
nomi_recent_task_id: str | None = None
nomi_pending_model_switch: dict | None = None
nomi_active_model: str = 'qwen35_single'
state_broadcast_task: asyncio.Task | None = None
NOMI_MOCK_DEV = os.getenv('NOMI_MOCK_DEV', '').strip() == '1'
NOMI_MUJOCO_NAV_ENABLED = (
    os.getenv('NOMI_MUJOCO_NAV_ENABLED', '0').strip() == '1'
    and not NOMI_MOCK_DEV
    and str(config.robot_type).strip().lower() == 'mujoco'
)
MUJOCO_MAP_BOUNDS = {'min_x': -5.285, 'max_x': 9.815, 'min_y': -13.490, 'max_y': 1.310}
MUJOCO_NAV_CLEARANCE_M = 0.8
MUJOCO_HOST_WATCHDOG_S = 0.5


def _navigation_execution_enabled() -> bool:
    return (
        NOMI_MUJOCO_NAV_ENABLED
        and not NOMI_MOCK_DEV
        and str(config.robot_type).strip().lower() == 'mujoco'
    )


def _decorate_robot_state(state: dict) -> dict:
    decorated = dict(state)
    decorated['navigation_execution_enabled'] = _navigation_execution_enabled()
    decorated['nomi_mock_mode'] = NOMI_MOCK_DEV
    if nomi_bridge is not None and (getattr(nomi_bridge, 'has_odom', False) or getattr(nomi_bridge, 'obstacle_scan', None)):
        decorated['robot_type'] = 'amr'
        decorated['navigation_execution_enabled'] = True
        decorated['position'] = {'x': nomi_bridge.robot_x, 'y': nomi_bridge.robot_y, 'z': 0.0}
        decorated['rotation'] = {'yaw': nomi_bridge.robot_yaw, 'roll': 0.0, 'pitch': 0.0}
        decorated['base_joints'] = [nomi_bridge.robot_x, nomi_bridge.robot_y, nomi_bridge.robot_yaw]
        decorated['host_connected'] = True
        decorated['connected'] = True
        decorated['status'] = 'connected'
        last_activity = max(getattr(nomi_bridge, 'last_odom_time', 0.0), getattr(nomi_bridge, 'last_scan_time', 0.0))
        decorated['state_age'] = max(0.0, time.time() - last_activity) if last_activity > 0 else 0.0
        if nomi_bridge.obstacle_scan:
            scan_time = getattr(nomi_bridge, 'last_scan_time', 0.0)
            age_ms = round(max(0.0, (time.time() - scan_time) * 1000.0), 1) if scan_time > 0 else 9999.0
            scan_status = dict(getattr(nomi_bridge, 'obstacle_scan_status', {}))
            scan_status['age_ms'] = age_ms
            scan_status['healthy'] = (age_ms <= 1200.0)
            decorated['obstacle_scan'] = nomi_bridge.obstacle_scan
            decorated['obstacle_scan_status'] = scan_status
    if nomi_bridge is not None and getattr(nomi_bridge, 'docking_status', None):
        decorated['docking_status'] = nomi_bridge.docking_status
    return decorated


def _init_client_state() -> dict:
    now = time.time()
    return {
        'last_command_time': 0.0,
        'command_count': 0,
        'window_start': now,
        'throttle_violations': 0,
        'last_throttle_time': 0.0,
        'last_sequence': 0,
    }


client_states: dict[str, dict] = {}

RATE_LIMIT_CONFIG = {
    'max_commands_per_second': 20,
    'min_command_interval': 0.05,
    'rate_limit_window': 1.0,
    'throttle_penalty_duration': 2.0,
    'max_throttle_violations': 3,
}

async def startup_event():
    global main_event_loop, nomi_bridge, state_broadcast_task
    main_event_loop = asyncio.get_running_loop()
    nomi_bridge = NomiBridge(
        on_response=None,
        on_model_status=_forward_nomi_model_status,
        on_task_status=_forward_nomi_task_status,
        on_connection_status=_forward_nomi_bridge_status,
        on_navigation_request=_forward_nomi_navigation_request,
        on_navigation_cancel=_forward_nomi_navigation_cancel,
        on_docking_status=_forward_docking_status,
        on_arm_task_complete=_forward_arm_task_complete,
    )
    if nomi_bridge.start():
        print("NOMI ROS 2 bridge started")
    else:
        print(f"NOMI ROS 2 bridge unavailable: {nomi_bridge.error}")
    if NOMI_MOCK_DEV:
        # Safe development mode keeps manual motion and simulator paths offline.
        remote_core.connected = False
        print("NOMI_MOCK_DEV: RemoteCore.connect blocked; manual robot control stays offline")
    else:
        print("Starting remote core connection monitor...")
        state_broadcast_task = asyncio.create_task(_maintain_remote_connection())

async def shutdown_event():
    if state_broadcast_task is not None:
        state_broadcast_task.cancel()
        try:
            await state_broadcast_task
        except asyncio.CancelledError:
            pass
    if nomi_bridge is not None:
        nomi_bridge.stop()
    if not NOMI_MOCK_DEV:
        # Safe mock mode never initialized or connected the remote control core.
        print("Shutting down remote control core...")
        await remote_core.disconnect()
        print("Remote core shut down")


async def _maintain_remote_connection():
    """Maintain connection to robot host or stream ROS odometry telemetry at 20 Hz."""
    connected_at: float | None = None
    last_reconnect_attempt = 0.0
    while True:
        now = time.monotonic()
        if not remote_core.connected and not (nomi_bridge and getattr(nomi_bridge, 'has_odom', False)) and (now - last_reconnect_attempt >= 10.0):
            last_reconnect_attempt = now
            if await remote_core.connect():
                connected_at = time.monotonic()
                print(f"Remote core connected to {config.robot_type} host successfully")

        state = _decorate_robot_state(await remote_core.get_state())
        await sio.emit('robot_state_snapshot', {'state': state})

        if remote_core.connected:
            last_state_received = remote_core.last_state_received
            telemetry_stale = last_state_received > 0 and time.time() - last_state_received > 2.5
            telemetry_missing = (
                last_state_received <= 0
                and connected_at is not None
                and time.monotonic() - connected_at > 3.0
            )
            if telemetry_stale or telemetry_missing:
                print("MuJoCo telemetry became stale; reconnecting remote core")
                await remote_core.disconnect()
                connected_at = None

        await asyncio.sleep(0.05)

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)

app = FastAPI(title="XLeRobot Web Control API", version="0.1.0")
app.include_router(towel_viewer_router)

app.add_event_handler("startup", startup_event)
app.add_event_handler("shutdown", shutdown_event)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

socket_app = socketio.ASGIApp(sio, app)


def _forward_nomi_event(event: str, payload: dict, recipient: str | None = None) -> None:
    """Forward callbacks from the ROS thread through the server event loop."""
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, sio.emit(event, payload, to=recipient))


async def _publish_nomi_task_status(payload: dict) -> None:
    global nomi_active_task_id
    task_id = str(payload.get('task_id', ''))
    if task_id == nomi_active_task_id and payload.get('status') in {'NEEDS_CLARIFICATION', 'VALIDATED', 'SUCCEEDED', 'FAILED', 'CANCELLED'}:
        nomi_active_task_id = None
    recipient = nomi_task_owners.get(task_id)
    if not task_id:
        return
    nomi_task_payloads[task_id] = payload
    if len(nomi_task_payloads) > 200:
        oldest = next(iter(nomi_task_payloads))
        nomi_task_payloads.pop(oldest, None)
        nomi_task_owners.pop(oldest, None)
    if recipient:
        await sio.emit('nomi_task_status', payload, to=recipient)


def _forward_nomi_task_status(payload: dict) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_nomi_task_status(payload))


async def _publish_nomi_navigation_request(payload: dict) -> None:
    task_id = nomi_active_task_id or nomi_recent_task_id
    recipient = nomi_task_owners.get(task_id or "")
    name = str(payload.get('name', '')).strip()
    request_id = str(payload.get('request_id', '')).strip()
    if not name or not request_id:
        return

    # Fallback to connected client if recipient sid is stale or disconnected
    if not recipient or recipient not in client_states:
        connected_sids = list(client_states.keys())
        recipient = connected_sids[0] if connected_sids else None

    normalized = ''.join(name.casefold().split()).replace('_', '').replace('-', '')
    destination = 'home' if normalized in {'집', 'home', '홈', '홈스테이션', '충전소', '충전위치', '원점'} else 'saved'
    if recipient:
        nomi_navigation_owners[request_id] = recipient
        if len(nomi_navigation_owners) > 200:
            nomi_navigation_owners.pop(next(iter(nomi_navigation_owners)), None)

    await sio.emit('nomi_navigation_request', {
        'destination': destination,
        'name': name,
        'request_id': request_id,
    }, to=recipient)


def _forward_nomi_navigation_request(payload: dict) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_nomi_navigation_request(payload))


async def _publish_nomi_navigation_cancel(request_id: str) -> None:
    recipient = nomi_navigation_owners.get(request_id)
    if recipient:
        await sio.emit('nomi_navigation_cancel', {'request_id': request_id}, to=recipient)


def _forward_nomi_navigation_cancel(request_id: str) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_nomi_navigation_cancel(request_id))


async def _publish_docking_status(payload: dict) -> None:
    await sio.emit('docking_status', payload)


def _forward_docking_status(payload: dict) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_docking_status(payload))


async def _publish_arm_task_complete(payload: dict) -> None:
    await sio.emit('arm_task_complete', payload)


def _forward_arm_task_complete(payload: dict) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_arm_task_complete(payload))


async def _publish_nomi_model_status(value: str) -> None:
    global nomi_pending_model_switch, nomi_active_model
    pending = nomi_pending_model_switch
    if value.startswith('ERROR: '):
        recipient = pending.get('sid') if pending else None
        payload = {
            'status': 'error',
            'profile': pending['profile'] if pending else '',
            'request_id': pending['request_id'] if pending else '',
            'message': value[7:],
        }
        nomi_pending_model_switch = None
        if recipient:
            await sio.emit('nomi_model_status', payload, to=recipient)
        else:
            await sio.emit('nomi_model_status', payload)
        return

    if value in MODEL_PROFILES:
        nomi_active_model = value
        recipient = pending.get('sid') if pending else None
        payload = {
            'status': 'success',
            'profile': value,
            'request_id': pending['request_id'] if pending else '',
            'label': MODEL_PROFILES[value]['label'],
        }
        nomi_pending_model_switch = None
        if recipient:
            await sio.emit('nomi_model_status', payload, to=recipient)
            await sio.emit('nomi_model_status', payload, skip_sid=recipient)
        else:
            await sio.emit('nomi_model_status', payload)
    else:
        return


def _forward_nomi_model_status(value: str) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_nomi_model_status(value))


async def _publish_nomi_bridge_status(connected: bool, error: str | None) -> None:
    global nomi_pending_model_switch
    await sio.emit('nomi_bridge_status', {
        'connected': connected,
        'message': 'NOMI Agent 연결됨' if connected else (error or 'NOMI Agent 연결 안 됨'),
        'active_model': nomi_active_model,
    })
    if not connected and nomi_pending_model_switch is not None:
        pending = nomi_pending_model_switch
        nomi_pending_model_switch = None
        await sio.emit('nomi_model_status', {
            'status': 'error', 'profile': pending['profile'],
            'request_id': pending['request_id'],
            'message': error or '모델 변경 중 NOMI Agent 연결이 끊겼습니다.',
        }, to=pending['sid'])


def _forward_nomi_bridge_status(connected: bool, error: str | None) -> None:
    loop = main_event_loop
    if loop is not None and not loop.is_closed():
        loop.call_soon_threadsafe(asyncio.create_task, _publish_nomi_bridge_status(connected, error))


@sio.event
async def get_robot_state(sid):
    state = _decorate_robot_state(await remote_core.get_state())
    await sio.emit('robot_state_snapshot', {'state': state}, to=sid)


@sio.event
async def reset_robot(sid):
    result = await remote_core.reset()
    if nomi_bridge is not None:
        nomi_bridge.set_initial_pose(-0.59, -0.88, math.radians(-17.1))
    await sio.emit('robot_reset_result', result, to=sid)


@sio.event
async def set_robot_pose(sid, data):
    """Calibrate / relocate robot initial pose on map"""
    data = data if isinstance(data, dict) else {}
    try:
        x = float(data.get('x', -0.59))
        y = float(data.get('y', -0.88))
        yaw = float(data.get('yaw', math.radians(-17.1)))
    except (TypeError, ValueError):
        return
    if nomi_bridge is not None:
        nomi_bridge.set_initial_pose(x, y, yaw)
    state = _decorate_robot_state(await remote_core.get_state())
    await sio.emit('robot_state_snapshot', {'state': state})


@sio.event
async def request_wall_alignment(sid, data=None):
    """Align robot pose with map walls using real-time LiDAR scan matching"""
    data = data if isinstance(data, dict) else {}
    if nomi_bridge is None:
        await sio.emit('wall_alignment_result', {'success': False, 'message': 'ROS 브리지가 연결되지 않았습니다.'}, to=sid)
        return

    init_x = data.get('x')
    init_y = data.get('y')
    init_yaw = data.get('yaw')
    hint_name = str(data.get('name', '')).strip() or None

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: nomi_bridge.align_with_walls(
            init_x=float(init_x) if init_x is not None else None,
            init_y=float(init_y) if init_y is not None else None,
            init_yaw=float(init_yaw) if init_yaw is not None else None,
            hint_name=hint_name,
        )
    )

    if result.get('success'):
        state = _decorate_robot_state(await remote_core.get_state())
        await sio.emit('robot_state_snapshot', {'state': state})

    await sio.emit('wall_alignment_result', result)


@sio.event
async def enable_docking(sid, data):
    """Enable or disable visual AprilTag precision docking node"""
    data = data if isinstance(data, dict) else {}
    enabled = bool(data.get('enabled', True))
    success = False
    if nomi_bridge is not None:
        success = nomi_bridge.set_docking_enabled(enabled)
    await sio.emit('docking_enabled_result', {'enabled': enabled, 'success': success}, to=sid)


@sio.event
async def report_docking_complete(sid, data):
    """Publish AMR docking completion to /amr/docking_complete for external robot arm laptop"""
    payload = data if isinstance(data, dict) else {'status': 'DOCKED'}
    success = False
    if nomi_bridge is not None:
        success = nomi_bridge.publish_docking_complete(payload)
    await sio.emit('docking_complete_broadcast', {'payload': payload, 'success': success})


@sio.event
async def mock_arm_task_complete(sid, data=None):
    """Placeholder/Test helper: simulate robot arm completion without second laptop"""
    payload = data if isinstance(data, dict) else {'status': 'COMPLETED', 'task': 'fold_towel'}
    success = False
    if nomi_bridge is not None:
        success = nomi_bridge.publish_arm_task_complete(payload)
    await _publish_arm_task_complete(payload)
    await sio.emit('arm_task_complete_broadcast', {'payload': payload, 'success': success})


@sio.event
async def nomi_command(sid, data):
    global nomi_active_task_id, nomi_recent_task_id
    data = data if isinstance(data, dict) else {}
    text = str(data.get('text', '')).strip()
    validate_only = data.get('validate_only', False) is True
    if not text:
        await sio.emit('nomi_response', {'status': 'error', 'request_id': (data or {}).get('request_id'), 'message': '명령을 입력해 주세요.'}, to=sid)
        return
    request_id = str((data or {}).get('request_id') or uuid.uuid4()).strip()
    if not request_id or len(request_id) > 128 or len(text) > 4000:
        await sio.emit('nomi_response', {'status': 'error', 'request_id': request_id, 'message': '명령 또는 요청 ID 형식이 올바르지 않습니다.'}, to=sid)
        return
    existing_owner = nomi_task_owners.get(request_id)
    if existing_owner:
        if existing_owner == sid:
            prior = nomi_task_payloads.get(request_id)
            await sio.emit('nomi_command_received', {
                'status': 'duplicate', 'request_id': request_id,
            }, to=sid)
            if prior:
                await sio.emit('nomi_task_status', prior, to=sid)
        else:
            await sio.emit('nomi_response', {'status': 'error', 'request_id': request_id, 'message': '이미 사용 중인 요청 ID입니다.'}, to=sid)
        return
    if nomi_active_task_id is not None:
        active_status = nomi_task_payloads.get(nomi_active_task_id, {}).get('status')
        active_time = nomi_task_payloads.get(nomi_active_task_id, {}).get('_created_at', 0)
        import time as _t
        if _t.time() - active_time > 25.0:
            nomi_active_task_id = None
        elif active_status not in {'NEEDS_CLARIFICATION', 'VALIDATED', 'SUCCEEDED', 'FAILED', 'CANCELLED'}:
            await sio.emit('nomi_response', {
                'status': 'error', 'request_id': request_id,
                'message': f'다른 사용자의 NOMI 작업이 진행 중입니다. 작업 ID: {nomi_active_task_id}',
            }, to=sid)
            return
        else:
            nomi_active_task_id = None
    if nomi_bridge is None or not nomi_bridge.available or not nomi_bridge.agent_connected:
        await sio.emit('nomi_response', {
            'status': 'error',
            'request_id': request_id,
            'message': 'NOMI ROS 2 브리지에 연결되지 않아 Harness에 명령을 전달하지 못했습니다.',
        }, to=sid)
        return
    import time as _t
    nomi_task_owners[request_id] = sid
    nomi_task_payloads[request_id] = {'task_id': request_id, 'status': 'PENDING', '_created_at': _t.time()}
    nomi_active_task_id = request_id
    nomi_recent_task_id = request_id
    if not nomi_bridge.send_command(text, request_id, validate_only=validate_only):
        nomi_task_owners.pop(request_id, None)
        nomi_task_payloads.pop(request_id, None)
        nomi_active_task_id = None
        await sio.emit('nomi_response', {
            'status': 'error',
            'request_id': request_id,
            'message': 'NOMI ROS 2 브리지에 연결되지 않아 Harness에 명령을 전달하지 못했습니다.',
        }, to=sid)
        return
    await sio.emit('nomi_command_received', {
        'status': 'accepted', 'text': text, 'request_id': request_id,
        'validate_only': validate_only,
    }, to=sid)


@sio.event
async def nomi_destinations_sync(sid, data):
    """Forward the browser's validated navigation destinations to the Agent."""
    destinations = (data or {}).get('destinations') if isinstance(data, dict) else None
    if not isinstance(destinations, list) or len(destinations) > 100:
        return
    checked = []
    for item in destinations:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        x, y = item.get('x'), item.get('y')
        aliases = item.get('aliases', [])
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                or isinstance(y, bool) or not isinstance(y, (int, float)) or not math.isfinite(y)
                or not -18 <= x <= 18 or not -4 <= y <= 24):
            continue
        if not isinstance(aliases, list):
            aliases = []
        yaw = item.get('yaw', 0.0)
        checked.append({
            'name': name.strip(), 'x': float(x), 'y': float(y),
            'yaw': float(yaw) if isinstance(yaw, (int, float)) and math.isfinite(yaw) else 0.0,
            'aliases': [alias.strip() for alias in aliases[:10]
                        if isinstance(alias, str) and alias.strip() and len(alias) <= 120],
            'kind': str(item.get('kind', 'saved'))[:40],
        })
    if nomi_bridge is not None and checked:
        nomi_bridge.sync_destinations(checked)


@sio.event
async def nomi_task_cancel(sid, data):
    task_id = str((data or {}).get('task_id', '')).strip()
    if nomi_bridge is not None and hasattr(nomi_bridge, 'cancel_all_navigation'):
        nomi_bridge.cancel_all_navigation()
    if not task_id or task_id == 'all' or nomi_task_owners.get(task_id) != sid or nomi_active_task_id != task_id:
        if nomi_bridge is not None and nomi_bridge.available and nomi_bridge.agent_connected:
            nomi_bridge.cancel_task(task_id or 'all')
        await sio.emit('nomi_cancel_result', {'status': 'accepted', 'task_id': task_id or 'all'}, to=sid)
        return
    status = nomi_task_payloads.get(task_id, {}).get('status')
    if status in {'NEEDS_CLARIFICATION', 'VALIDATED', 'SUCCEEDED', 'FAILED', 'CANCELLED'}:
        await sio.emit('nomi_cancel_result', {
            'status': 'error', 'task_id': task_id,
            'message': '이미 종료된 작업입니다.',
        }, to=sid)
        return
    if nomi_bridge is None or not nomi_bridge.available or not nomi_bridge.agent_connected or not nomi_bridge.cancel_task(task_id):
        await sio.emit('nomi_cancel_result', {
            'status': 'error', 'task_id': task_id,
            'message': 'NOMI ROS 2 브리지에 연결되지 않아 취소를 전달하지 못했습니다.',
        }, to=sid)
        return
    await sio.emit('nomi_cancel_result', {'status': 'accepted', 'task_id': task_id}, to=sid)


@sio.event
async def nomi_navigation_result(sid, data):
    payload = data if isinstance(data, dict) else {}
    request_id = str(payload.get('request_id', '')).strip()
    status = payload.get('status')
    owner = nomi_navigation_owners.get(request_id)
    if not request_id or (owner and owner != sid and len(client_states) > 1) or status not in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
        await sio.emit('nomi_navigation_result_ack', {
            'status': 'error', 'request_id': request_id,
            'message': '유효한 내비게이션 요청과 결과 상태를 확인하지 못했습니다.',
        }, to=sid)
        return
    result = {
        'request_id': request_id,
        'status': status,
        'message': str(payload.get('message', ''))[:500],
    }
    if status == 'SUCCEEDED':
        position = payload.get('position')
        distance = payload.get('distance_m')
        valid_arrival = (
            payload.get('completion_verified') is True
            and isinstance(position, dict)
            and all(isinstance(position.get(axis), (int, float)) and math.isfinite(position[axis]) for axis in ('x', 'y'))
            and isinstance(distance, (int, float)) and math.isfinite(distance) and 0 <= distance <= 0.35
        )
        if not valid_arrival:
            await sio.emit('nomi_navigation_result_ack', {
                'status': 'error', 'request_id': request_id,
                'message': '현재 위치가 목적지 0.3m 이내라는 도착 확인이 없어 완료 결과를 거부했습니다.',
            }, to=sid)
            return
        result.update({'completion_verified': True, 'position': position, 'distance_m': distance})
    if nomi_bridge is None or not nomi_bridge.publish_navigation_result(result):
        await sio.emit('nomi_navigation_result_ack', {
            'status': 'error', 'request_id': request_id,
            'message': 'NOMI ROS 2 브리지로 실제 도착 결과를 전달하지 못했습니다.',
        }, to=sid)
        return
    nomi_navigation_owners.pop(request_id, None)
    await sio.emit('nomi_navigation_result_ack', {'status': 'forwarded', 'request_id': request_id}, to=sid)


@sio.event
async def nomi_model_switch(sid, data):
    global nomi_pending_model_switch
    profile = str((data or {}).get('profile', '')).strip()
    if profile not in MODEL_PROFILES:
        await sio.emit('nomi_model_status', {'status': 'error', 'message': '지원하지 않는 모델 프로필입니다.'}, to=sid)
        return
    now = time.time()
    if nomi_pending_model_switch is not None:
        if now - float(nomi_pending_model_switch.get('timestamp', 0)) > 5.0:
            nomi_pending_model_switch = None
        else:
            await sio.emit('nomi_model_status', {
                'status': 'error', 'message': '다른 모델 변경 결과를 확인 중입니다.',
            }, to=sid)
            return
    request_id = str((data or {}).get('request_id') or uuid.uuid4()).strip()
    if not request_id or len(request_id) > 128:
        await sio.emit('nomi_model_status', {'status': 'error', 'message': '요청 ID 형식이 올바르지 않습니다.'}, to=sid)
        return
    if nomi_bridge is None or not nomi_bridge.available or not nomi_bridge.agent_connected:
        await sio.emit('nomi_model_status', {'status': 'error', 'message': 'NOMI ROS 2 브리지에 연결되지 않았습니다.'}, to=sid)
        return
    nomi_pending_model_switch = {'sid': sid, 'profile': profile, 'request_id': request_id, 'timestamp': now}
    if not nomi_bridge.switch_model(profile):
        nomi_pending_model_switch = None
        await sio.emit('nomi_model_status', {'status': 'error', 'message': 'NOMI ROS 2 브리지에 연결되지 않았습니다.'}, to=sid)
        return
    await sio.emit('nomi_model_switch_sent', {'profile': profile, 'request_id': request_id}, to=sid)


@sio.event
async def nomi_get_status(sid):
    connected = bool(nomi_bridge and nomi_bridge.available and nomi_bridge.agent_connected)
    await sio.emit('nomi_bridge_status', {
        'connected': connected,
        'message': 'NOMI Agent 연결됨' if connected else 'NOMI Agent 연결 안 됨',
        'active_model': nomi_active_model,
    }, to=sid)
    active_id = nomi_active_task_id or nomi_recent_task_id
    if active_id and active_id in nomi_task_payloads:
        nomi_task_owners[active_id] = sid
        payload = nomi_task_payloads[active_id]
        await sio.emit('nomi_task_status', payload, to=sid)

CLIENT_DIST_DIR = Path(__file__).resolve().parent.parent / "client" / "dist"
if (CLIENT_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=CLIENT_DIST_DIR / "assets"), name="assets")

@app.get("/")
async def root():
    """Root endpoint - serve production built client UI"""
    index_file = CLIENT_DIST_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "XLeRobot Web Control Server is running"}

@app.get("/my_map.png")
async def get_map_image():
    map_file = CLIENT_DIST_DIR / "my_map.png"
    if map_file.exists():
        return FileResponse(map_file)
    return {"error": "map image not found"}

@app.get("/vite.svg")
async def get_vite_svg():
    vite_file = CLIENT_DIST_DIR / "vite.svg"
    if vite_file.exists():
        return FileResponse(vite_file)
    return {"error": "vite svg not found"}

@app.get("/api/docking_stream")
async def docking_camera_stream():
    """Proxy live MJPEG camera feed directly from SBC stream with minimal buffering"""
    client = httpx.AsyncClient(timeout=10.0)
    try:
        req = client.build_request("GET", "http://192.168.0.64:8088/stream.mjpg")
        r = await client.send(req, stream=True)
        async def stream_generator():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
                await client.aclose()
        return StreamingResponse(
            stream_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Failed to connect to docking camera: {e}")

@app.get("/api/docking_snapshot")
async def docking_snapshot():
    """Fetch on-demand snapshot from SBC"""
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            r = await client.get("http://192.168.0.64:8088/snapshot.jpg")
            if r.status_code == 200:
                return Response(
                    content=r.content,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
                )
        except Exception:
            pass
    raise HTTPException(status_code=502, detail="Failed to fetch snapshot")

_latest_webcam_frame_bytes = None
_latest_webcam_frame_time = 0.0
_midas_net = None

def get_midas_net():
    global _midas_net
    if _midas_net is None:
        model_p = '/home/juyeon/ros2_amr_ws/models/model-small.onnx'
        if os.path.exists(model_p):
            import cv2
            _midas_net = cv2.dnn.readNet(model_p)
            _midas_net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            _midas_net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    return _midas_net

@app.post("/api/webcam_frame")
async def receive_webcam_frame(request: Request):
    global _latest_webcam_frame_bytes, _latest_webcam_frame_time
    _latest_webcam_frame_bytes = await request.body()
    _latest_webcam_frame_time = time.time()
    return {"status": "ok", "size": len(_latest_webcam_frame_bytes)}

@app.get("/api/webcam_snapshot")
async def get_webcam_snapshot():
    global _latest_webcam_frame_bytes
    if _latest_webcam_frame_bytes:
        return Response(
            content=_latest_webcam_frame_bytes,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
        )
    raise HTTPException(status_code=404, detail="No webcam frame available yet")

@app.post("/api/depth_pointcloud_web")
async def depth_pointcloud_web(request: Request):
    """Processes a 2D webcam image with MiDaS ONNX and returns binary Float32 3D points [x, y, z, r, g, b]"""
    global _latest_webcam_frame_bytes, _latest_webcam_frame_time
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty image frame")

    _latest_webcam_frame_bytes = body
    _latest_webcam_frame_time = time.time()

    import cv2
    import numpy as np

    nparr = np.frombuffer(body, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="Decode failed")

    net = get_midas_net()
    if net is None:
        raise HTTPException(status_code=500, detail="MiDaS model not available")

    target_w, target_h = 120, 90
    blob = cv2.dnn.blobFromImage(img, 1.0 / 255.0, (256, 256), (123.675, 116.28, 103.53), swapRB=True, crop=False)
    net.setInput(blob)
    disp = net.forward()[0, :, :]
    disp = cv2.resize(disp, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
    img_small = cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

    norm = (disp - disp.min()) / (disp.max() - disp.min() + 1e-6)
    z = 0.35 / (norm * 0.12 + (1.0 - norm) * 0.25)
    z = np.clip(z, 0.25, 3.5)

    u, v = np.meshgrid(np.arange(target_w), np.arange(target_h))
    fx = fy = 95.0
    x = (u - target_w / 2.0) * z / fx
    y = -(v - target_h / 2.0) * z / fy
    z_3d = -z

    colors = (img_small[:, :, ::-1] / 255.0).astype(np.float32)

    pts = np.stack([
        x.flatten(),
        y.flatten(),
        z_3d.flatten(),
        colors[:, :, 0].flatten(),
        colors[:, :, 1].flatten(),
        colors[:, :, 2].flatten()
    ], axis=1).astype(np.float32)

    return Response(content=pts.tobytes(), media_type="application/octet-stream")

@app.get("/webcam")
async def webcam_page():
    template_path = "/home/juyeon/dapier_project11/XLeRobot/web_control/server/templates/webcam_3d.html"
    return FileResponse(template_path)

@app.get("/api/time")
async def get_server_time():
    return {"t": time.time()}

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    controller_info = {
        'type': config.robot_type,
        'connected': remote_core.connected,
        'capabilities': remote_core.get_capabilities()
    }

    bridge_info = {}
    if nomi_bridge is not None:
        bridge_info = {
            'available': nomi_bridge.available,
            'thread_alive': nomi_bridge._thread.is_alive() if nomi_bridge._thread else False,
            'agent_connected': nomi_bridge.agent_connected,
            'has_odom': nomi_bridge.has_odom,
            'last_scan_time': getattr(nomi_bridge, 'last_scan_time', 0.0),
            'last_odom_time': getattr(nomi_bridge, 'last_odom_time', 0.0),
            'last_tf_time': getattr(nomi_bridge, 'last_tf_time', 0.0),
            'obstacle_scan_len': len(getattr(nomi_bridge, 'obstacle_scan', [])),
            'error': nomi_bridge.error,
        }

    return {
        "status": "healthy",
        "service": "XLeRobot Web Control",
        "controller": controller_info,
        "bridge": bridge_info
    }

@app.get("/robot/info")
async def robot_info():
    """Robot controller info endpoint"""
    return {
        'controller_type': config.robot_type,
        'connected': remote_core.connected,
        'capabilities': remote_core.get_capabilities(),
        'state': _decorate_robot_state(await remote_core.get_state())
    }

@app.get("/robot/controllers")
async def available_controllers():
    """Get available controllers"""
    return {
        'available': ['maniskill', 'mujoco', 'xlerobot'],
        'current': config.robot_type,
        'description': 'Unified remote control supports all robot types via host programs'
    }

@app.post("/robot/camera/reset")
async def reset_camera():
    """Reset camera to default position"""
    try:
        result = await remote_core.reset_camera()
        return result
    except Exception as e:
        return {"error": f"Camera reset failed: {str(e)}"}

@app.post("/robot/camera/position")
async def set_camera_position(request: dict):
    """Set camera position"""
    position = request.get('position')
    target = request.get('target')

    if not position:
        return {"error": "Position is required"}

    try:
        result = await remote_core.set_camera_position(position, target)
        return result
    except Exception as e:
        return {"error": f"Set camera position failed: {str(e)}"}

@app.get("/robot/camera/info")
async def get_camera_info():
    """Get camera info"""
    try:
        # For simplified architecture, return basic camera info
        return {
            'camera_id': 0,
            'position': [2.0, 2.0, 2.0],
            'target': [0.0, 0.0, 0.0],
            'frame_size': {'width': config.video_width, 'height': config.video_height},
            'fps': config.video_fps,
            'quality': config.video_quality
        }
    except Exception as e:
        return {"error": f"Get camera info failed: {str(e)}"}

# Rate limiting and throttling functions
def check_rate_limit(sid: str) -> bool:
    """Check if client is within rate limits"""
    current_time = time.time()
    client_state = client_states.get(sid)

    if not client_state:
        return True

    # Reset command count if window has passed
    if current_time - client_state['window_start'] >= RATE_LIMIT_CONFIG['rate_limit_window']:
        client_state['command_count'] = 0
        client_state['window_start'] = current_time

    return client_state['command_count'] < RATE_LIMIT_CONFIG['max_commands_per_second']

def check_throttle(sid: str) -> bool:
    """Check if client is throttling commands too fast"""
    current_time = time.time()
    client_state = client_states.get(sid)

    if not client_state:
        return True

    # Check if client is in penalty period
    if (client_state['throttle_violations'] >= RATE_LIMIT_CONFIG['max_throttle_violations'] and
        current_time - client_state['last_throttle_time'] < RATE_LIMIT_CONFIG['throttle_penalty_duration']):
        return False

    # Reset violations if penalty period has passed
    if current_time - client_state['last_throttle_time'] >= RATE_LIMIT_CONFIG['throttle_penalty_duration']:
        client_state['throttle_violations'] = 0

    # Check minimum interval between commands
    if current_time - client_state['last_command_time'] < RATE_LIMIT_CONFIG['min_command_interval']:
        client_state['throttle_violations'] += 1
        client_state['last_throttle_time'] = current_time
        return False

    return True

def update_client_state(sid: str, *, timestamp: float) -> None:
    """Update client state after successful command"""
    client_state = client_states.setdefault(sid, _init_client_state())
    client_state['last_command_time'] = timestamp
    client_state['command_count'] += 1

@sio.event
async def connect(sid, environ, auth):
    """Client connection event"""
    print(f"Client connected: {sid}")
    # Initialize client state
    client_states[sid] = _init_client_state()
    await sio.emit('connection_established', {'message': 'Connected to XLeRobot control server'}, to=sid)
    await nomi_get_status(sid)

@sio.event
async def disconnect(sid):
    """Client disconnect event"""
    print(f"Client disconnected: {sid}")

    if sid in video_manager.stream_tasks:
        task = video_manager.stream_tasks[sid]
        if not task.cancelled():
            task.cancel()
            print(f"Cancelled video stream task for disconnected client {sid}")
        del video_manager.stream_tasks[sid]

    if client_states.pop(sid, None) is not None:
        print(f"Cleaned up state for client {sid}")
    for request_id, owner in list(nomi_navigation_owners.items()):
        if owner == sid:
            nomi_navigation_owners.pop(request_id, None)

@sio.event
async def ping(sid, data):
    """Ping-pong test event"""
    print(f"Received ping from {sid}: {data}")
    await sio.emit('pong', {'message': 'pong', 'timestamp': data.get('timestamp')}, to=sid)

@sio.event
async def move_command(sid, data):
    """Movement command handler with rate limiting and throttling"""
    data = data if isinstance(data, dict) else {}
    direction = data.get('direction')
    sequence = data.get('sequence')
    timestamp = data.get('timestamp', time.time() * 1000)
    try:
        speed = float(data.get('speed', 1.0))
        angular = float(data.get('angular', 0.0))
        linear_x = float(data.get('linear_x', 0.0))
        linear_y = float(data.get('linear_y', 0.0))
        client_timestamp = float(timestamp)
    except (TypeError, ValueError):
        speed, angular, linear_x, linear_y, client_timestamp = float('nan'), float('nan'), float('nan'), float('nan'), 0.0
    if not math.isfinite(client_timestamp):
        client_timestamp = 0.0

    current_time = time.time()
    client_state = client_states.setdefault(sid, _init_client_state())
    if sequence is not None:
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            sequence = -1
        if sequence <= client_state['last_sequence']:
            await sio.emit('command_received', {
                'type': 'move', 'status': 'stale', 'sequence': sequence,
                'server_timestamp': current_time * 1000,
            }, to=sid)
            return
        client_state['last_sequence'] = sequence

    allowed_directions = {'forward', 'backward', 'left', 'right', 'rotate_left', 'rotate_right', 'drive_vector', 'stop'}
    if direction not in allowed_directions or not all(math.isfinite(value) for value in (speed, angular, linear_x, linear_y)):
        await sio.emit('command_received', {
            'type': 'move', 'status': 'error', 'message': 'Invalid movement direction or speed',
            'sequence': sequence, 'server_timestamp': current_time * 1000,
        }, to=sid)
        return
    requested_speed = speed
    speed = min(1.0, max(0.0, speed))
    angular = min(1.0, max(-1.0, angular))
    linear_x = min(1.0, max(-1.0, linear_x))
    linear_y = min(1.0, max(-1.0, linear_y))

    def emit_command_status(status, **extra):
        if direction == 'drive_vector':
            diagnostic_status = 'accepted' if status in {'success', 'executed', 'accepted'} else (
                'rejected' if status == 'error' else status
            )
            print(
                f"[navigation-command] {diagnostic_status} sequence={sequence} "
                f"requested=({linear_x:+.2f},{linear_y:+.2f}) angular={angular:+.2f} "
                f"reason={extra.get('message', '')}"
            )
        return sio.emit('command_received', {
            'type': 'move', 'direction': direction, 'speed': speed,
            'status': status, 'sequence': sequence,
            'client_timestamp': client_timestamp,
            'server_timestamp': time.time() * 1000,
            'speed_limited': speed != requested_speed,
            **extra,
        }, to=sid)

    # Stops are safety commands: never reject them because of movement throttling.
    if direction == 'stop':
        if nomi_bridge is not None and hasattr(nomi_bridge, 'send_direction'):
            nomi_bridge.send_direction('stop')
        if not remote_core.connected:
            update_client_state(sid, timestamp=current_time)
            await emit_command_status(
                'success',
                robot_state=_decorate_robot_state(await remote_core.get_state()),
                metrics={'latency': max(0.0, time.time() * 1000 - client_timestamp) if client_timestamp else None},
            )
            return
        result = await remote_core.stop(sequence=sequence)
        update_client_state(sid, timestamp=current_time)
        await emit_command_status(
            result.get('status', 'success'),
            robot_state=_decorate_robot_state(await remote_core.get_state()),
            metrics={'latency': max(0.0, time.time() * 1000 - client_timestamp) if client_timestamp else None},
        )
        return

    # The browser's route follower uses drive_vector. Keep a server-side gate
    # as well so stale sensors, non-MuJoCo profiles, and Mock-only mode cannot
    # issue autonomous navigation commands through a hand-crafted socket call.
    if direction == 'drive_vector':
        if not remote_core.connected:
            if nomi_bridge is not None and hasattr(nomi_bridge, 'send_direction'):
                nomi_bridge.send_direction('drive_vector', speed, angular, linear_x, linear_y)
            update_client_state(sid, timestamp=current_time)
            await emit_command_status(
                'success',
                robot_state=_decorate_robot_state(await remote_core.get_state()),
                metrics={'latency': max(0.0, time.time() * 1000 - client_timestamp) if client_timestamp else None},
            )
            return

        if (not NOMI_MUJOCO_NAV_ENABLED or NOMI_MOCK_DEV
                or str(config.robot_type).strip().lower() != 'mujoco'):
            await emit_command_status(
                'error', message='MuJoCo 자동 주행이 명시적으로 활성화되지 않아 이동 명령을 차단했습니다.',
            )
            return
        nav_state = await remote_core.get_state()
        scan_status = nav_state.get('obstacle_scan_status') or {}
        state_age = nav_state.get('state_age')
        scan_age = scan_status.get('age_ms')
        state_valid = (
            remote_core.connected
            and nav_state.get('host_connected') is True
            and isinstance(state_age, (int, float)) and math.isfinite(state_age) and state_age <= 2.5
            and nav_state.get('robot_type') == 'mujoco'
        )
        scan_valid = (
            scan_status.get('healthy') is True
            and isinstance(scan_age, (int, float)) and math.isfinite(scan_age) and scan_age <= 1200
            and isinstance(nav_state.get('obstacle_scan'), list)
        )
        if not state_valid or not scan_valid:
            await emit_command_status(
                'error', message='최신 MuJoCo 위치 또는 거리 센서 데이터가 없어 자동 주행 명령을 차단했습니다.',
            )
            return
        position = nav_state.get('position') or {}
        map_x, map_y = position.get('x'), position.get('y')
        if (not isinstance(map_x, (int, float)) or isinstance(map_x, bool)
                or not isinstance(map_y, (int, float)) or isinstance(map_y, bool)
                or not math.isfinite(map_x) or not math.isfinite(map_y)):
            await emit_command_status(
                'error', message='MuJoCo 지도 좌표를 확인할 수 없어 자동 주행 명령을 차단했습니다.',
            )
            return
        safe_bounds = {
            'min_x': MUJOCO_MAP_BOUNDS['min_x'] + MUJOCO_NAV_CLEARANCE_M,
            'max_x': MUJOCO_MAP_BOUNDS['max_x'] - MUJOCO_NAV_CLEARANCE_M,
            'min_y': MUJOCO_MAP_BOUNDS['min_y'] + MUJOCO_NAV_CLEARANCE_M,
            'max_y': MUJOCO_MAP_BOUNDS['max_y'] - MUJOCO_NAV_CLEARANCE_M,
        }
        predicted_x = map_x + linear_x * MUJOCO_HOST_WATCHDOG_S
        predicted_y = map_y + linear_y * MUJOCO_HOST_WATCHDOG_S
        if not (
            safe_bounds['min_x'] <= map_x <= safe_bounds['max_x']
            and safe_bounds['min_y'] <= map_y <= safe_bounds['max_y']
            and safe_bounds['min_x'] <= predicted_x <= safe_bounds['max_x']
            and safe_bounds['min_y'] <= predicted_y <= safe_bounds['max_y']
        ):
            await emit_command_status(
                'error', message='로봇 여유 공간을 포함한 MuJoCo 지도 경계를 벗어날 수 있어 자동 주행 명령을 차단했습니다.',
            )
            return

    # 정지 및 긴급 정지 명령은 rate limit이나 throttle 검사를 완전히 우회하여 0ms 즉각 처리
    if direction == 'stop':
        if nomi_bridge is not None and hasattr(nomi_bridge, 'send_direction'):
            nomi_bridge.send_direction('stop')
        update_client_state(sid, timestamp=current_time)
        await emit_command_status(
            'success',
            robot_state=_decorate_robot_state(await remote_core.get_state()),
            metrics={'latency': max(0.0, time.time() * 1000 - client_timestamp) if client_timestamp else None},
        )
        return

    # Check rate limiting
    if not check_rate_limit(sid):
        await emit_command_status(
            'rate_limited',
            message=f'Rate limit exceeded: maximum {RATE_LIMIT_CONFIG["max_commands_per_second"]} commands per second',
            max_rate=RATE_LIMIT_CONFIG['max_commands_per_second'],
        )
        return

    # Check throttling
    if not check_throttle(sid):
        client_state = client_states.setdefault(sid, _init_client_state())
        await emit_command_status(
            'throttled',
            message=f'Command throttled: minimum {RATE_LIMIT_CONFIG["min_command_interval"]*1000:.0f}ms interval required',
            min_interval=RATE_LIMIT_CONFIG['min_command_interval'] * 1000,
            violations=client_state['throttle_violations'],
            penalty_remaining=max(0, RATE_LIMIT_CONFIG['throttle_penalty_duration'] - (current_time - client_state['last_throttle_time'])) if client_state['throttle_violations'] >= RATE_LIMIT_CONFIG['max_throttle_violations'] else 0,
        )
        return

    if not remote_core.connected:
        if nomi_bridge is not None and hasattr(nomi_bridge, 'send_direction'):
            nomi_bridge.send_direction(direction, speed, angular, linear_x, linear_y)
        update_client_state(sid, timestamp=current_time)
        await emit_command_status(
            'success',
            robot_state=_decorate_robot_state(await remote_core.get_state()),
            metrics={'latency': max(0.0, time.time() * 1000 - client_timestamp) if client_timestamp else None},
        )
        return

    # Log command with rate limiting info
    client_state = client_states.setdefault(sid, _init_client_state())
    next_count = client_state['command_count'] + 1
    if next_count == 1 or next_count % 10 == 0:
        if direction == 'drive_vector':
            print(f"[{sid[:8]}] Move command: map=({linear_x:+.2f},{linear_y:+.2f}) angular={angular:+.2f} - count={next_count}/{RATE_LIMIT_CONFIG['max_commands_per_second']}")
        else:
            print(f"[{sid[:8]}] Move command: {direction} (speed={speed:.2f}) - count={next_count}/{RATE_LIMIT_CONFIG['max_commands_per_second']}")

    # Execute robot command
    try:
        if nomi_bridge is not None and hasattr(nomi_bridge, 'send_direction'):
            nomi_bridge.send_direction(direction, speed, angular, linear_x, linear_y)
        result = await remote_core.move(
            direction, speed, angular, linear_x, linear_y, sequence=sequence,
        )

        # Update client state after successful command
        update_client_state(sid, timestamp=current_time)

        # Get current robot state
        robot_state = _decorate_robot_state(await remote_core.get_state())

        # Calculate latency
        latency = (current_time * 1000) - client_timestamp if client_timestamp else 0

        # Send success response with detailed metrics
        response_status = result.get('status', 'executed')
        transport_status = result.get('transport_status')
        if direction == 'drive_vector' and response_status in {'success', 'executed', 'accepted'}:
            # RemoteCore confirms only that ZeroMQ accepted the bytes. Host receipt
            # and actuator application are traced asynchronously by sequence.
            response_status = 'queued_to_zmq'
            transport_status = transport_status or 'queued_to_zmq'
        await sio.emit('command_received', {
            'type': 'move', 'direction': direction, 'speed': speed,
            'status': response_status, 'transport_status': transport_status, 'sequence': sequence,
            'speed_limited': speed != requested_speed,
            'robot_state': robot_state,
            'metrics': {
                'latency': latency,
                'commands_in_window': client_state['command_count'],
                'max_commands': RATE_LIMIT_CONFIG['max_commands_per_second'],
                'throttle_violations': client_state['throttle_violations']
            },
            'client_timestamp': client_timestamp,
            'server_timestamp': current_time * 1000
        }, to=sid)
        if direction == 'drive_vector':
            print(
                f"[navigation-command] queued_to_zmq sequence={sequence} "
                f"requested=({linear_x:+.2f},{linear_y:+.2f}) angular={angular:+.2f} "
                f"state_age={nav_state.get('state_age')} scan_healthy={((nav_state.get('obstacle_scan_status') or {}).get('healthy'))} "
                f"scan_age_ms={((nav_state.get('obstacle_scan_status') or {}).get('age_ms'))}"
            )

    except Exception as e:
        await sio.emit('command_received', {
            'type': 'move',
            'status': 'error',
            'sequence': sequence,
            'message': f'Command execution failed: {str(e)}',
            'client_timestamp': timestamp,
            'server_timestamp': current_time * 1000
        }, to=sid)
        if direction == 'drive_vector':
            print(f"[navigation-command] rejected sequence={sequence} reason={e}")

@sio.event
async def start_video_stream(sid):
    """Start video streaming"""
    print(f"Client {sid} requested start video stream")
    
    if sid in video_manager.stream_tasks:
        task = video_manager.stream_tasks[sid]
        if not task.cancelled():
            task.cancel()
        del video_manager.stream_tasks[sid]
    
    result = await video_manager.start_stream()
    await sio.emit('stream_status', result, to=sid)
    
    task = asyncio.create_task(video_manager.stream_frames(sio, sid))
    video_manager.stream_tasks[sid] = task

@sio.event
async def stop_video_stream(sid):
    """Stop video streaming"""
    print(f"Client {sid} requested stop video stream")
    result = await video_manager.stop_stream(sid)
    await sio.emit('stream_status', result, to=sid)


@sio.event
async def reset_camera(sid):
    """Reset camera event handler"""
    print(f"Client {sid} requested camera reset")

    try:
        result = await remote_core.reset_camera()
        await sio.emit('camera_action_result', {
            'action': 'reset',
            **result
        }, to=sid)
        print(f"Camera reset result: {result['status']}")
    except Exception as e:
        await sio.emit('camera_action_result', {
            'action': 'reset',
            'status': 'error',
            'message': f'Camera reset failed: {str(e)}'
        }, to=sid)

@sio.event
async def set_camera_position(sid, data):
    """Set camera position event handler"""
    print(f"Client {sid} requested set camera position")

    position = data.get('position')
    target = data.get('target')

    if not position:
        await sio.emit('camera_action_result', {
            'action': 'set_position',
            'status': 'error',
            'message': 'Position is required'
        }, to=sid)
        return

    try:
        result = await remote_core.set_camera_position(position, target)
        await sio.emit('camera_action_result', {
            'action': 'set_position',
            **result
        }, to=sid)
        print(f"Camera position set result: {result['status']}")
    except Exception as e:
        await sio.emit('camera_action_result', {
            'action': 'set_position',
            'status': 'error',
            'message': f'Set camera position failed: {str(e)}'
        }, to=sid)

@sio.event
async def get_camera_info(sid):
    """Get camera info event handler"""
    print(f"Client {sid} requested camera info")

    try:
        # Return basic camera info for simplified architecture
        camera_info = {
            'camera_id': 0,
            'position': [2.0, 2.0, 2.0],
            'target': [0.0, 0.0, 0.0],
            'frame_size': {'width': config.video_width, 'height': config.video_height},
            'fps': config.video_fps,
            'quality': config.video_quality
        }
        await sio.emit('camera_info_result', {
            'status': 'success',
            'camera_info': camera_info
        }, to=sid)
    except Exception as e:
        await sio.emit('camera_info_result', {
            'status': 'error',
            'message': f'Get camera info failed: {str(e)}'
        }, to=sid)

if __name__ == "__main__":
    uvicorn.run(
        socket_app,
        host=config.ui_host,
        port=config.ui_port,
        reload=False,  # Disable reload for simplified architecture
        log_level="info"
    )
