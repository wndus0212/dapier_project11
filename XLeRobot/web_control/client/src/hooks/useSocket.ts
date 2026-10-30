import { useEffect, useState, useCallback, useRef } from 'react';
import { io, Socket } from 'socket.io-client';
import type { CommandFeedback, DockingStatus, RobotSimulationState, SocketStatus, SystemMessage } from '../types';
import { ENV } from '../config/environment';
import { KEY_TO_DIRECTION, MESSAGE_TYPES } from '../config/constants';

interface RobotTelemetry {
  battery: string | null;
  temp: string | null;
  speed: string | null;
  voltage: string | null;
}

interface NetworkMetrics {
  latency: number | null;
  fps: number | null;
}

interface ArmPositions {
  angles: number[] | null;
}

interface NomiNavigationRequest {
  destination: 'home' | 'charging_dock' | 'saved';
  name: string;
  request_id: string | number;
}

interface NomiNavigationCancel {
  request_id: string;
}

export const useSocket = (controlSpeed = ENV.DEFAULT_SPEED) => {
  const [socket, setSocket] = useState<Socket | null>(null);
  const [status, setStatus] = useState<SocketStatus>({
    socket: 'disconnected',
    video: 'disconnected'
  });
  const [messages, setMessages] = useState<SystemMessage[]>([]);

  const [telemetry, setTelemetry] = useState<RobotTelemetry>({
    battery: null,
    temp: null,
    speed: null,
    voltage: null
  });
  
  const [networkMetrics, setNetworkMetrics] = useState<NetworkMetrics>({
    latency: null,
    fps: null
  });
  
  const [armPositions, setArmPositions] = useState<ArmPositions>({
    angles: null
  });
  const [simulationState, setSimulationState] = useState<RobotSimulationState | null>(null);
  const [dockingStatus, setDockingStatus] = useState<DockingStatus | null>(null);
  const [armTaskComplete, setArmTaskComplete] = useState<{ status: string; task?: string } | null>(null);
  const [wallAlignResult, setWallAlignResult] = useState<{
    success: boolean;
    x: number;
    y: number;
    yaw: number;
    yaw_deg: number;
    residual_cm: number;
    message: string;
  } | null>(null);
  const [commandFeedback, setCommandFeedback] = useState<CommandFeedback | null>(null);
  const [nomiNavigationRequest, setNomiNavigationRequest] = useState<NomiNavigationRequest | null>(null);
  const [nomiNavigationCancel, setNomiNavigationCancel] = useState<NomiNavigationCancel | null>(null);

  const [pressedKeys, setPressedKeys] = useState<Set<string>>(new Set());
  const continuousIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pressedKeysRef = useRef<Set<string>>(new Set());
  const controlSpeedRef = useRef(controlSpeed);
  const commandSequenceRef = useRef(0);
  const commandSentAtRef = useRef(new Map<number, number>());

  useEffect(() => {
    controlSpeedRef.current = controlSpeed;
  }, [controlSpeed]);


  const addMessage = useCallback((content: string, type: SystemMessage['type'] = MESSAGE_TYPES.INFO) => {
    const message: SystemMessage = {
      id: Date.now().toString(),
      content,
      timestamp: Date.now(),
      type
    };
    setMessages(prev => [...prev.slice(-(ENV.LOG_MAX_ENTRIES - 1)), message]);
  }, []);

  const emitMoveCommand = useCallback((direction: string, speed = 1.0, angular = 0, linearX = 0, linearY = 0) => {
    if (!socket || status.socket !== 'connected') return;
    const sequence = ++commandSequenceRef.current;
    const sentAt = performance.now();
    const boundedSpeed = Number.isFinite(speed) ? Math.min(1, Math.max(0, speed)) : 0;
    commandSentAtRef.current.set(sequence, sentAt);
    // Keep only a small window in case a response is lost.
    if (commandSentAtRef.current.size > 64) {
      const oldest = commandSentAtRef.current.keys().next().value;
      if (oldest !== undefined) commandSentAtRef.current.delete(oldest);
    }
    setCommandFeedback({ sequence, direction, speed: boundedSpeed, status: 'pending', latencyMs: null });
    const boundedAngular = Number.isFinite(angular) ? Math.min(1, Math.max(-1, angular)) : 0;
    const boundedLinearX = Number.isFinite(linearX) ? Math.min(1, Math.max(-1, linearX)) : 0;
    const boundedLinearY = Number.isFinite(linearY) ? Math.min(1, Math.max(-1, linearY)) : 0;
    socket.emit('move_command', { direction, speed: boundedSpeed, angular: boundedAngular, linear_x: boundedLinearX, linear_y: boundedLinearY, sequence, timestamp: Date.now() });
  }, [socket, status.socket]);

  useEffect(() => {
    const newSocket = io(ENV.SERVER_URL, {
      transports: ['websocket'],
      reconnection: ENV.SOCKET_RECONNECTION,
      reconnectionDelay: ENV.SOCKET_RECONNECTION_DELAY,
      // The UI may be opened before the Python backend after a reboot.
      // Keep retrying so starting the backend later restores telemetry and controls.
      reconnectionAttempts: Number.POSITIVE_INFINITY,
    });

    newSocket.on('connect', () => {
      setStatus(prev => ({ ...prev, socket: 'connected' }));
      addMessage(`Connected to server (ID: ${newSocket.id})`, MESSAGE_TYPES.SUCCESS);
      newSocket.emit('get_robot_state');
    });

    newSocket.on('disconnect', () => {
      setStatus(prev => ({ ...prev, socket: 'disconnected' }));
      addMessage('Disconnected from server', MESSAGE_TYPES.WARNING);
    });

    newSocket.on('connect_error', (error) => {
      setStatus(prev => ({ ...prev, socket: 'error' }));
      addMessage(`Connection error: ${error.message}`, MESSAGE_TYPES.ERROR);
    });

    newSocket.io.on('reconnect_attempt', () => {
      setStatus(prev => ({ ...prev, socket: 'connecting' }));
    });

    newSocket.on('connection_established', (data) => {
      addMessage(data.message, MESSAGE_TYPES.SUCCESS);
    });

    newSocket.on('nomi_navigation_request', (data: NomiNavigationRequest) => {
      if (data && ['home', 'charging_dock', 'saved'].includes(data.destination)
        && typeof data.name === 'string' && data.name.trim().length > 0
        && ((typeof data.request_id === 'number' && Number.isFinite(data.request_id))
          || (typeof data.request_id === 'string' && data.request_id.trim().length > 0))) {
        setNomiNavigationRequest(data);
      }
    });

    newSocket.on('nomi_navigation_cancel', (data: NomiNavigationCancel) => {
      if (data && typeof data.request_id === 'string' && data.request_id.trim()) {
        setNomiNavigationCancel(data);
      }
    });



    newSocket.on('stream_status', (data) => {
      if (data.status === 'streaming_started') {
        setStatus(prev => ({ ...prev, video: 'streaming' }));
        addMessage('Video stream started successfully', MESSAGE_TYPES.SUCCESS);
      } else if (data.status === 'streaming_stopped') {
        setStatus(prev => ({ ...prev, video: 'disconnected' }));
        addMessage('Video stream stopped', MESSAGE_TYPES.INFO);
      }
    });

    newSocket.on('video_stream_error', (error) => {
      setStatus(prev => ({ ...prev, video: 'error' }));
      addMessage(`Video stream error: ${error.message || error}`, MESSAGE_TYPES.ERROR);
    });


    newSocket.on('telemetry_update', (data) => {
      setTelemetry({
        battery: data.battery || null,
        temp: data.temperature || null,
        speed: data.speed || null,
        voltage: data.voltage || null
      });
    });

    newSocket.on('network_metrics', (data) => {
      setNetworkMetrics({
        latency: data.latency || null,
        fps: data.fps || null
      });
    });

    newSocket.on('arm_position_update', (data) => {
      setArmPositions({
        angles: data.angles || null
      });
    });

    newSocket.on('robot_state_snapshot', (data: { state?: RobotSimulationState }) => {
      if (data?.state) {
        setSimulationState(data.state);
        if (data.state.docking_status) {
          setDockingStatus(data.state.docking_status);
        }
      }
    });

    newSocket.on('docking_status', (data: DockingStatus) => {
      if (data) {
        setDockingStatus(data);
      }
    });

    newSocket.on('arm_task_complete', (data: { status: string; task?: string }) => {
      if (data) {
        setArmTaskComplete(data);
      }
    });

    newSocket.on('wall_alignment_result', (data: any) => {
      if (data) {
        setWallAlignResult(data);
        if (data.message) {
          addMessage(data.message, data.success ? MESSAGE_TYPES.SUCCESS : MESSAGE_TYPES.WARNING);
        }
      }
    });

    newSocket.on('command_received', (data: { robot_state?: RobotSimulationState; sequence?: number; direction?: string; speed?: number; status?: string; transport_status?: string; message?: string }) => {
      if (data?.robot_state) setSimulationState(data.robot_state);
      if (typeof data?.sequence === 'number') {
        const sentAt = commandSentAtRef.current.get(data.sequence);
        if (sentAt !== undefined) commandSentAtRef.current.delete(data.sequence);
        setCommandFeedback(previous => {
          if (previous && data.sequence! < previous.sequence) return previous;
          const status = data.direction === 'drive_vector'
            ? data.status === 'queued_to_zmq' ? 'queued_to_zmq'
              : data.status === 'success' || data.status === 'executed' ? 'accepted'
              : data.status === 'throttled' || data.status === 'rate_limited' ? data.status
                : data.status === 'stale' || data.status === 'error' || data.status === 'rejected' ? 'rejected'
                  : previous?.status ?? 'pending'
            : data.status === 'success' || data.status === 'executed' ? 'success'
              : data.status === 'throttled' || data.status === 'rate_limited' || data.status === 'stale' ? data.status
                : data.status === 'error' ? 'error' : previous?.status ?? 'pending';
          if (data.direction === 'drive_vector') {
            console.info('[navigation] drive_vector result', {
              sequence: data.sequence,
              status: status === 'accepted' ? 'accepted' : status,
              backend_status: data.status,
              transport_status: data.transport_status,
              message: data.message,
              speed: data.speed,
              latency_ms: sentAt === undefined ? null : Math.round(performance.now() - sentAt),
            });
          }
          return {
            sequence: data.sequence!,
            direction: data.direction ?? previous?.direction ?? 'stop',
            speed: typeof data.speed === 'number' ? data.speed : previous?.speed ?? 0,
            status,
            latencyMs: sentAt === undefined ? null : Math.round(performance.now() - sentAt),
            message: data.message,
          };
        });
      }
    });

    newSocket.on('camera_action_result', (data) => {
      if (data.action === 'reset') {
        const msg = data.status === 'success'
          ? 'Camera reset successful'
          : `Camera reset failed: ${data.message || 'Unknown error'}`;
        addMessage(msg, data.status === 'success' ? MESSAGE_TYPES.SUCCESS : MESSAGE_TYPES.ERROR);
      }
    });

    setSocket(newSocket);

    return () => {
      newSocket.close();
    };
  }, [addMessage]);

  const startContinuousMovement = useCallback(() => {
    if (continuousIntervalRef.current) {
      clearInterval(continuousIntervalRef.current);
    }

    continuousIntervalRef.current = setInterval(() => {
      const currentKeys = pressedKeysRef.current;
      if (currentKeys.size === 0) return;

      const keysArray = Array.from(currentKeys);
      const primaryKey = keysArray[keysArray.length - 1];
      const direction = KEY_TO_DIRECTION[primaryKey];

      if (direction) emitMoveCommand(direction, controlSpeedRef.current);
    }, ENV.MOVEMENT_INTERVAL_MS);
  }, [emitMoveCommand]);

  const stopContinuousMovement = useCallback(() => {
    pressedKeysRef.current.clear();
    setPressedKeys(new Set());
    if (continuousIntervalRef.current) {
      clearInterval(continuousIntervalRef.current);
      continuousIntervalRef.current = null;
    }

    emitMoveCommand('stop', 0);
  }, [emitMoveCommand]);

  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    const key = event.code === 'KeyQ' ? 'q' : event.code === 'KeyE' ? 'e' : event.key;

    if (!KEY_TO_DIRECTION[key]) return;

    event.preventDefault();

    if (pressedKeysRef.current.has(key)) return;

    pressedKeysRef.current.add(key);
    setPressedKeys(new Set(pressedKeysRef.current));

    if (pressedKeysRef.current.size === 1) {
      startContinuousMovement();
    }
  }, [startContinuousMovement]);

  const handleKeyUp = useCallback((event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    if (target?.closest('input, textarea, select, [contenteditable="true"]')) return;
    const key = event.code === 'KeyQ' ? 'q' : event.code === 'KeyE' ? 'e' : event.key;

    if (!KEY_TO_DIRECTION[key]) return;

    event.preventDefault();

    pressedKeysRef.current.delete(key);
    setPressedKeys(new Set(pressedKeysRef.current));

    if (pressedKeysRef.current.size === 0) {
      stopContinuousMovement();
    }
  }, [stopContinuousMovement]);

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    window.addEventListener('keyup', handleKeyUp);

    const releaseKeys = () => {
      if (pressedKeysRef.current.size > 0) {
        pressedKeysRef.current.clear();
        setPressedKeys(new Set());
        stopContinuousMovement();
      }
    };
    window.addEventListener('blur', releaseKeys);

    return () => {
      window.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('keyup', handleKeyUp);
      window.removeEventListener('blur', releaseKeys);
      if (continuousIntervalRef.current) {
        clearInterval(continuousIntervalRef.current);
        continuousIntervalRef.current = null;
      }
      pressedKeysRef.current.clear();
      setPressedKeys(new Set());
    };
  }, [handleKeyDown, handleKeyUp, stopContinuousMovement]);

  const sendPing = useCallback(() => {
    if (socket && status.socket === 'connected') {
      const timestamp = Date.now();
      socket.emit('ping', { timestamp, message: 'ping from client' });
      addMessage(`Sent Ping (timestamp: ${timestamp})`, MESSAGE_TYPES.INFO);
    }
  }, [socket, status.socket]);

  const sendMoveCommand = useCallback((direction: string, speed = 1.0, angular = 0) => {
    emitMoveCommand(direction, speed, angular);
  }, [emitMoveCommand]);

  const sendDriveCommand = useCallback((linearX: number, linearY: number, angular: number) => {
    emitMoveCommand('drive_vector', Math.hypot(linearX, linearY), angular, linearX, linearY);
  }, [emitMoveCommand]);

  const sendNomiNavigationResult = useCallback((result: {
    request_id: string;
    status: 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
    message: string;
    completion_verified?: boolean;
    position?: { x: number; y: number };
    distance_m?: number;
  }) => {
    if (!socket || status.socket !== 'connected') return false;
    socket.emit('nomi_navigation_result', result);
    return true;
  }, [socket, status.socket]);

  const startVideoStream = useCallback(() => {
    if (socket && status.socket === 'connected') {
      socket.emit('start_video_stream');
      setStatus(prev => ({ ...prev, video: 'connecting' }));
      addMessage('Starting video stream', MESSAGE_TYPES.INFO);
    }
  }, [socket, status.socket]);

  const stopVideoStream = useCallback(() => {
    if (socket && status.socket === 'connected') {
      socket.emit('stop_video_stream');
      setStatus(prev => ({ ...prev, video: 'disconnected' }));
      addMessage('Stopping video stream', MESSAGE_TYPES.INFO);
    }
  }, [socket, status.socket]);

  const sendEnableDocking = useCallback((enabled: boolean) => {
    if (!socket || status.socket !== 'connected') return;
    socket.emit('enable_docking', { enabled: Boolean(enabled) });
  }, [socket, status.socket]);

  const sendDockingComplete = useCallback((payload?: Record<string, unknown>) => {
    if (!socket || status.socket !== 'connected') return;
    socket.emit('report_docking_complete', payload || { status: 'DOCKED' });
  }, [socket, status.socket]);

  const triggerMockArmTaskComplete = useCallback((taskName?: string) => {
    if (!socket || status.socket !== 'connected') return;
    socket.emit('mock_arm_task_complete', { status: 'SUCCEEDED', task: taskName || 'fold_towel' });
  }, [socket, status.socket]);

  const requestWallAlignment = useCallback((pose?: { x: number; y: number; yaw?: number; name?: string }) => {
    if (!socket || status.socket !== 'connected') return;
    socket.emit('request_wall_alignment', pose || {});
  }, [socket, status.socket]);

  return {
    socket,
    status,
    messages,
    sendPing,
    sendMoveCommand,
    sendDriveCommand,
    sendEnableDocking,
    startVideoStream,
    stopVideoStream,
    telemetry,
    networkMetrics,
    armPositions,
    simulationState,
    dockingStatus,
    armTaskComplete,
    sendDockingComplete,
    triggerMockArmTaskComplete,
    requestWallAlignment,
    wallAlignResult,
    commandFeedback,
    nomiNavigationRequest,
    nomiNavigationCancel,
    pressedKeys,
    stopContinuousMovement,
    sendNomiNavigationResult,
  };
};
