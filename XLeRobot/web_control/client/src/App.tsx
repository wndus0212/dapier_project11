import { useEffect, useRef, useState } from 'react';
import { Moon, Sun, Radio, Activity, Bot, Compass, Cpu, Gauge, Gamepad2, MessageCircle, SlidersHorizontal } from 'lucide-react';
import { useSocket } from './hooks/useSocket';
import { GameSimulationCanvas } from './components/features/GameSimulationCanvas';
import { GameControlDeck } from './components/features/GameControlDeck';
import { DestinationNavigator, type Destination } from './components/features/DestinationNavigator';
import { NomiPanel } from './components/features/NomiPanel';
import { TowelPlacementPanel } from './components/features/TowelPlacementPanel';
import type { RobotSimulationState } from './types';
import { findRoute, getDynamicObstaclePoints, isRouteBlockedByDynamicObstacle, type RoutePoint } from './utils/pathPlanner';
import { INDOOR_MAP, NAVIGATION_DESTINATIONS } from './utils/indoorMap';
import { computeWaypointCommand, NavigationMotionGuard, shouldStopForDriveFeedback } from './utils/navigationController';

type StickVector = { x: number; y: number };

function readSavedHome(): Destination | null {
  try {
    const saved = localStorage.getItem('xlerobot.homeWaypoint');
    if (saved) {
      const value = JSON.parse(saved) as Destination;
      if (Number.isFinite(value.x) && Number.isFinite(value.y)
        && value.x >= INDOOR_MAP.bounds.minX && value.x <= INDOOR_MAP.bounds.maxX
        && value.y >= INDOOR_MAP.bounds.minY && value.y <= INDOOR_MAP.bounds.maxY) {
        return {
          name: value.name || '홈 스테이션',
          x: value.x,
          y: value.y,
          yaw: typeof value.yaw === 'number' && Number.isFinite(value.yaw) ? value.yaw : -17.1,
          tag_id: typeof value.tag_id === 'number' ? value.tag_id : 1,
          auto_dock: typeof value.auto_dock === 'boolean' ? value.auto_dock : true,
        };
      }
    }
  } catch {
    // Use the built-in home waypoint when browser storage is unavailable.
  }
  return { name: '홈 스테이션', x: INDOOR_MAP.start.x, y: INDOOR_MAP.start.y, yaw: -17.1, tag_id: 1, auto_dock: true };
}

const LAST_POSE_STORAGE_KEY = 'xlerobot.lastRobotPose';

function readLastRobotPose(): { x: number; y: number; yaw: number } | null {
  try {
    const saved = localStorage.getItem(LAST_POSE_STORAGE_KEY);
    if (saved) {
      const p = JSON.parse(saved);
      if (Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.yaw)) {
        return { x: p.x, y: p.y, yaw: p.yaw };
      }
    }
  } catch {}
  return null;
}

function saveLastRobotPose(p: { x: number; y: number; yaw: number }) {
  try {
    localStorage.setItem(LAST_POSE_STORAGE_KEY, JSON.stringify({
      x: Number(p.x.toFixed(4)),
      y: Number(p.y.toFixed(4)),
      yaw: Number(p.yaw.toFixed(4)),
    }));
  } catch {}
}

function readSavedDestinations(): Destination[] {
  const defaults: Destination[] = NAVIGATION_DESTINATIONS.map(d => ({
    name: d.name,
    x: d.x,
    y: d.y,
    yaw: typeof (d as any).yaw === 'number' ? (d as any).yaw : 0,
    tag_id: typeof (d as any).tag_id === 'number' ? (d as any).tag_id : undefined,
    auto_dock: typeof (d as any).auto_dock === 'boolean' ? (d as any).auto_dock : undefined,
  }));
  try {
    const saved = localStorage.getItem('xlerobot.savedDestinations');
    if (saved) {
      const values = JSON.parse(saved) as unknown;
      if (Array.isArray(values) && values.length > 0) {
        const parsed = values.filter((value): value is Destination => value && typeof value.name === 'string'
          && value.name.trim().length > 0 && Number.isFinite(value.x) && Number.isFinite(value.y)
          && value.x >= INDOOR_MAP.bounds.minX && value.x <= INDOOR_MAP.bounds.maxX
          && value.y >= INDOOR_MAP.bounds.minY && value.y <= INDOOR_MAP.bounds.maxY)
          .map(value => ({
            name: value.name.trim(),
            x: value.x,
            y: value.y,
            yaw: typeof value.yaw === 'number' && Number.isFinite(value.yaw) ? value.yaw : 0,
            tag_id: typeof value.tag_id === 'number' ? value.tag_id : undefined,
            auto_dock: typeof value.auto_dock === 'boolean' ? value.auto_dock : undefined,
          }));
        if (parsed.length > 0) {
          const names = new Set(parsed.map(p => p.name));
          const missingDefaults = defaults.filter(d => !names.has(d.name));
          return [...missingDefaults, ...parsed];
        }
      }
    }
  } catch {
    // fallback to navigation destinations
  }
  return defaults;
}

function numberText(value: number | undefined) {
  return Number.isFinite(value) ? (value as number).toFixed(2) : '0.00';
}

function RobotTelemetry({
  state,
  localPose,
  speed,
  moving,
  socketStatus,
}: {
  state: RobotSimulationState | null;
  localPose: { x: number; y: number; yaw: number };
  speed: number;
  moving: boolean;
  socketStatus: string;
}) {
  const x = state?.position?.x ?? localPose.x;
  const y = state?.position?.y ?? localPose.y;
  const yaw = state?.rotation?.yaw ?? localPose.yaw;
  const left = state?.arm_joints?.left ?? [];
  const right = state?.arm_joints?.right ?? [];
  const source = state?.host_connected ? 'MUJOCO LIVE' : socketStatus === 'connected' ? 'HOST WAITING' : 'LOCAL SIM';

  return (
    <section className="surface-card rounded-2xl border p-4 sm:p-5">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[10px] font-bold tracking-[0.24em] text-slate-500">로봇 상태</div>
          <h2 className="mt-1 text-base font-semibold">시뮬레이션 상태</h2>
        </div>
        <span className="rounded-full bg-blue-50 px-2.5 py-1 font-mono text-[9px] tracking-widest text-blue-700">{source}</span>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2">
        <div className="surface-subcard rounded-xl border p-3">
          <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest text-slate-500"><Compass size={12} /> X 위치</div>
          <div className="mt-2 font-mono text-xl text-[var(--ui-fg)]">{numberText(x)}<span className="ml-1 text-xs text-slate-500">m</span></div>
        </div>
        <div className="surface-subcard rounded-xl border p-3">
          <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest text-slate-500"><Compass size={12} /> Y 위치</div>
          <div className="mt-2 font-mono text-xl text-[var(--ui-fg)]">{numberText(y)}<span className="ml-1 text-xs text-slate-500">m</span></div>
        </div>
        <div className="surface-subcard rounded-xl border p-3">
          <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest text-slate-500"><Gauge size={12} /> 주행 속도</div>
          <div className="mt-2 flex items-center gap-2 font-mono text-xl text-blue-600">{speed.toFixed(2)}<span className="text-xs text-slate-500">×</span><span className={`ml-auto h-2 w-2 rounded-full ${moving ? 'animate-pulse bg-emerald-400' : 'bg-slate-700'}`} /></div>
        </div>
        <div className="surface-subcard rounded-xl border p-3">
          <div className="flex items-center gap-2 text-[10px] font-bold tracking-widest text-slate-500"><Activity size={12} /> 방향</div>
          <div className="mt-2 font-mono text-xl text-[var(--ui-fg)]">{((yaw * 180 / Math.PI + 360) % 360).toFixed(0)}<span className="ml-1 text-xs text-slate-500">°</span></div>
        </div>
      </div>

      <div className="mt-4 surface-subcard rounded-xl border p-3">
        <div className="mb-2 flex items-center gap-2 text-[10px] font-bold tracking-widest text-slate-500"><Bot size={12} /> 팔 관절 <span className="ml-auto text-slate-600">RAD</span></div>
        <div className="grid grid-cols-2 gap-3">
          {[['왼팔', left], ['오른팔', right]].map(([label, values]) => (
            <div key={label as string}>
              <div className="mb-1 text-[9px] font-bold tracking-widest text-blue-600/80">{label as string}</div>
              <div className="space-y-1">
                {(values as number[]).slice(0, 6).map((angle, index) => (
                  <div key={index} className="flex items-center justify-between font-mono text-[10px] text-[var(--ui-muted)]"><span>J{index + 1}</span><span>{numberText(angle)}</span></div>
                ))}
                {(values as number[]).length === 0 && <div className="font-mono text-[10px] text-[var(--ui-muted)]">데이터 대기 중</div>}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

export default function App() {
  const [speed, setSpeed] = useState(1.0);
  const {
    socket,
    status,
    sendPing,
    sendMoveCommand,
    sendDriveCommand,
    sendEnableDocking,
    sendDockingComplete,
    triggerMockArmTaskComplete,
    armTaskComplete,
    simulationState,
    dockingStatus,
    commandFeedback,
    nomiNavigationRequest,
    nomiNavigationCancel,
    pressedKeys,
    stopContinuousMovement,
    sendNomiNavigationResult,
    requestWallAlignment,
    wallAlignResult,
  } = useSocket(speed);

  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [mobilePanel, setMobilePanel] = useState<'drive' | 'nomi' | 'status'>('drive');
  const [joystick, setJoystick] = useState<StickVector>({ x: 0, y: 0 });
  const [controlDirection, setControlDirection] = useState<string | null>(null);
  const [resetToken, setResetToken] = useState(0);
  const [localPose, setLocalPose] = useState(() => {
    const last = readLastRobotPose();
    if (last) return last;
    const home = readSavedHome();
    return {
      x: home?.x ?? INDOOR_MAP.start.x,
      y: home?.y ?? INDOOR_MAP.start.y,
      yaw: (((home?.yaw ?? -15.5) * Math.PI) / 180),
    };
  });
  const [homeWaypoint, setHomeWaypoint] = useState<Destination | null>(readSavedHome);
  const [savedDestinations, setSavedDestinations] = useState<Destination[]>(readSavedDestinations);
  const [destination, setDestination] = useState<Destination>(() => readSavedHome()
    ?? readSavedDestinations().find(place => place.name.normalize('NFKC').trim().toLocaleLowerCase() === '집')
    ?? { name: '', x: Number.NaN, y: Number.NaN });
  const [navigationStatus, setNavigationStatus] = useState<'idle' | 'backing_up' | 'navigating' | 'docking' | 'docked' | 'arrived' | 'stopped' | 'blocked'>('idle');
  const [preBackupEnabled, setPreBackupEnabled] = useState(() => {
    try {
      const saved = localStorage.getItem('xlerobot.preBackupEnabled');
      return saved !== null ? saved === 'true' : true;
    } catch {
      return true;
    }
  });
  const backupTimeoutRef = useRef<number | null>(null);
  const backupIntervalRef = useRef<number | null>(null);
  const clearBackupTimeout = () => {
    if (backupTimeoutRef.current !== null) {
      window.clearTimeout(backupTimeoutRef.current);
      backupTimeoutRef.current = null;
    }
    if (backupIntervalRef.current !== null) {
      window.clearInterval(backupIntervalRef.current);
      backupIntervalRef.current = null;
    }
  };
  const handleTogglePreBackup = (enabled: boolean) => {
    setPreBackupEnabled(enabled);
    try {
      localStorage.setItem('xlerobot.preBackupEnabled', String(enabled));
    } catch {
      // ignore
    }
  };
  const [plannedRoute, setPlannedRoute] = useState<RoutePoint[]>([]);
  const [routeOrigin, setRouteOrigin] = useState<RoutePoint | null>(null);
  const [waypointIndex, setWaypointIndex] = useState(0);
  const [navigationNotice, setNavigationNotice] = useState<string | null>(null);
  const turnDirectionRef = useRef<number | null>(null);
  const motionGuardRef = useRef(new NavigationMotionGuard());
  const lastControllerPhaseRef = useRef<string | null>(null);
  const lastHandledDriveFeedbackSequenceRef = useRef(0);
  const activeNomiNavigationRef = useRef<string | null>(null);
  const logNavigation = (event: string, details: Record<string, unknown> = {}) => {
    console.info('[navigation]', JSON.stringify({
      event,
      host_connected: simulationState?.host_connected ?? false,
      state_age: simulationState?.state_age ?? null,
      scan_healthy: simulationState?.obstacle_scan_status?.healthy ?? false,
      scan_age_ms: simulationState?.obstacle_scan_status?.age_ms ?? null,
      navigation_available: navigationAvailable,
      navigation_unavailable_reason: navigationUnavailableReason,
      ...details,
    }));
  };
  const clearPlannedRoute = (reason: string, details: Record<string, unknown> = {}) => {
    if (plannedRoute.length > 0) logNavigation('route_cleared', { reason, waypoint_count: plannedRoute.length, ...details });
    setPlannedRoute([]);
  };
  const reportNomiNavigationResult = (
    status: 'SUCCEEDED' | 'FAILED' | 'CANCELLED',
    message: string,
    pose?: RoutePoint,
    distance?: number,
  ) => {
    const requestId = activeNomiNavigationRef.current;
    if (!requestId) return;
    const verifiedArrival = status === 'SUCCEEDED' && pose && Number.isFinite(distance) && distance! <= 0.35;
    const sent = sendNomiNavigationResult({
      request_id: requestId,
      status,
      message,
      ...(verifiedArrival ? { completion_verified: true, position: pose, distance_m: distance } : {}),
    });
    logNavigation('nomi_navigation_result', {
      request_id: requestId,
      status: verifiedArrival ? 'SUCCEEDED' : status === 'SUCCEEDED' ? 'FAILED' : status,
      completion_verified: Boolean(verifiedArrival && sent),
      distance_m: distance ?? null,
      delivered: sent,
      message,
    });
    activeNomiNavigationRef.current = null;
  };
  const navigationEffectsRef = useRef({ clearPlannedRoute, reportNomiNavigationResult });
  navigationEffectsRef.current = { clearPlannedRoute, reportNomiNavigationResult };
  const handledNomiRequestRef = useRef<string | number | null>(null);
  const lastJoystickCommandRef = useRef(0);
  const hostPosition = simulationState?.position;
  const hostYaw = simulationState?.rotation?.yaw;
  const stateAge = simulationState?.state_age;
  const explicitMujocoMode = simulationState?.robot_type === 'mujoco'
    && simulationState?.navigation_execution_enabled === true
    && simulationState?.nomi_mock_mode !== true;
  const hostStateAvailable = status.socket === 'connected'
    && simulationState?.host_connected === true
    && (stateAge == null || (Number.isFinite(stateAge) && (stateAge as number) <= 5.0))
    && Number.isFinite(hostPosition?.x) && Number.isFinite(hostPosition?.y)
    && Number.isFinite(hostYaw);
  const hostPositionFresh = status.socket === 'connected'
    && simulationState?.host_connected === true
    && (stateAge == null || (Number.isFinite(stateAge) && (stateAge as number) <= 5.0))
    && Number.isFinite(hostPosition?.x) && Number.isFinite(hostPosition?.y);
  const scanStatus = simulationState?.obstacle_scan_status;
  const rangeSensorAvailable = scanStatus?.healthy === true
    && Number.isFinite(scanStatus.age_ms) && (scanStatus.age_ms as number) <= 1200
    && Array.isArray(simulationState?.obstacle_scan);

  // 실기체 AMR 연결 시 즉시 주행 허용, 미연결 시 로컬 시뮬레이션 지원
  const isRealAMR = hostStateAvailable && (simulationState?.host_connected === true);
  const isLocalSim = !hostStateAvailable;
  const navigationAvailable = isRealAMR
    ? (status.socket === 'connected')
    : isLocalSim
      ? (status.socket === 'connected')
      : (explicitMujocoMode && rangeSensorAvailable);

  let navigationUnavailableReason: string | null = null;
  if (status.socket !== 'connected') {
    navigationUnavailableReason = '웹 서버와 연결되지 않았습니다.';
  } else if (!isRealAMR && !isLocalSim) {
    if (simulationState?.nomi_mock_mode === true) {
      navigationUnavailableReason = 'Mock Agent 모드는 실제 로봇 주행을 차단합니다.';
    } else if (simulationState?.robot_type !== 'mujoco' && simulationState?.robot_type !== 'amr') {
      navigationUnavailableReason = '목적지 주행은 AMR 또는 시뮬레이션 모드가 확인된 경우에만 허용됩니다.';
    } else if (simulationState?.navigation_execution_enabled !== true) {
      navigationUnavailableReason = '목적지 주행이 비활성화되어 있습니다.';
    } else if (!scanStatus || scanStatus.healthy !== true) {
      navigationUnavailableReason = `거리 센서 상태가 비정상입니다${scanStatus?.error ? `: ${scanStatus.error}` : ''}.`;
    } else if (!Number.isFinite(scanStatus.age_ms) || (scanStatus.age_ms as number) > 1200) {
      navigationUnavailableReason = '거리 센서 데이터가 오래됐습니다.';
    }
  }
  const navigationPose = hostStateAvailable
    ? { x: hostPosition!.x!, y: hostPosition!.y!, yaw: hostYaw! }
    : localPose;
  const navigationPoseRef = useRef(navigationPose);
  navigationPoseRef.current = navigationPose;
  const simulationStateRef = useRef(simulationState);
  simulationStateRef.current = simulationState;
  const obstacleScanRef = useRef<RoutePoint[]>([]);
  const lastDynamicReplanRef = useRef(0);
  useEffect(() => {
    obstacleScanRef.current = getDynamicObstaclePoints(simulationState?.obstacle_scan ?? []);
  }, [simulationState]);
  const isMoving = pressedKeys.size > 0 || controlDirection !== null || Math.hypot(joystick.x, joystick.y) >= 0.1;

  useEffect(() => {
    if (!socket || status.socket !== 'connected') return;
    const baseNames = new Set(NAVIGATION_DESTINATIONS.map(d => d.name));
    const extraSaved = savedDestinations
      .filter(s => !baseNames.has(s.name))
      .map(({ name, x, y, yaw }) => ({ name, aliases: [], x, y, yaw: yaw ?? 0, kind: 'saved' }));
    const homeEntry = homeWaypoint
      ? [{
          name: homeWaypoint.name,
          aliases: ['홈', '홈스테이션', '집', '원점', '충전소', '충전 위치', 'home'],
          x: homeWaypoint.x,
          y: homeWaypoint.y,
          yaw: homeWaypoint.yaw ?? -15.5,
          kind: 'home',
        }]
      : [];
    const destinations = [
      ...NAVIGATION_DESTINATIONS,
      ...extraSaved,
      ...homeEntry,
    ];
    socket.emit('nomi_destinations_sync', { destinations });
  }, [socket, status.socket, savedDestinations, homeWaypoint]);

  const editDestination = (target: Destination) => {
    setDestination(target);
    setNavigationNotice(null);
    clearBackupTimeout();
    if (navigationStatus === 'navigating' || navigationStatus === 'backing_up' || navigationStatus === 'docking') {
      sendEnableDocking(false);
      reportNomiNavigationResult('CANCELLED', '목적지가 바뀌어 이전 자동 주행을 취소했습니다.');
      setNavigationStatus('stopped');
      setControlDirection(null);
      clearPlannedRoute('destination_changed');
      sendMoveCommand('stop', 0);
    }
  };

  const beginNavigation = (target: Destination) => {
    logNavigation('begin_requested', { target: { name: target.name, x: target.x, y: target.y } });
    clearBackupTimeout();
    sendEnableDocking(false);
    if (!navigationAvailable) {
      setNavigationNotice(navigationUnavailableReason ?? '주행에 필요한 상태를 확인할 수 없습니다.');
      setNavigationStatus('stopped');
      sendMoveCommand('stop', 0);
      reportNomiNavigationResult('FAILED', navigationUnavailableReason ?? '주행에 필요한 상태를 확인할 수 없습니다.');
      return;
    }
    const initialDistance = Math.hypot(target.x - navigationPose.x, target.y - navigationPose.y);
    const normName = (target.name || '').normalize('NFKC').trim().toLocaleLowerCase();
    const shouldDock = (
      Boolean((target as any).kind === 'charging' || (target as any).tag_id) ||
      ['집', 'home', '홈', '홈스테이션', '충전소', '충전위치', '충전도크', '충전장소', '도킹스테이션', '충전기', 'dock', 'chargingstation'].includes(normName)
    );

    if (initialDistance <= 0.35) {
      if (shouldDock) {
        setDestination(target);
        setNavigationStatus('docking');
        sendEnableDocking(true);
        setNavigationNotice(`🎯 ${target.name || '목적지'} 부근입니다: AprilTag 정밀 도킹을 가동합니다...`);
        return;
      }
      setNavigationStatus('arrived');
      setNavigationNotice(`이미 목적지 '${target.name}' 근처(${initialDistance.toFixed(2)}m)에 위치해 있습니다.`);
      reportNomiNavigationResult('SUCCEEDED', `이미 목적지 '${target.name}'에 위치해 있습니다.`, navigationPose, initialDistance);
      return;
    }
    const route = findRoute(navigationPose, target, obstacleScanRef.current);
    if (!route) {
      clearPlannedRoute('planner_no_route', { target: target.name, live_scan_points: obstacleScanRef.current.length });
      setNavigationNotice('장애물과 로봇 여유 공간을 고려해도 통과 가능한 경로를 찾지 못했습니다.');
      setNavigationStatus('blocked');
      sendMoveCommand('stop', 0);
      reportNomiNavigationResult('FAILED', '장애물과 로봇 여유 공간을 고려한 경로를 찾지 못했습니다.');
      return;
    }
    stopContinuousMovement();
    setJoystick({ x: 0, y: 0 });
    setControlDirection(null);
    setDestination(target);
    setPlannedRoute(route);
    setRouteOrigin({ x: navigationPose.x, y: navigationPose.y });
    setWaypointIndex(0);
    turnDirectionRef.current = null;
    motionGuardRef.current.reset();
    lastControllerPhaseRef.current = null;

    // 출발 전 안전 후진(언도킹)은 로봇이 충전소나 정거장 도크(40cm 이내)에 실제로 있을 때만 수행
    // 이미 통로/중간 위치(-0.03, -2.09 등)에 있는 경우에는 후진 없이 목적지로 즉시 최단거리 전진
    const isAtDock = savedDestinations.some(d => {
      const dist = Math.hypot(d.x - navigationPose.x, d.y - navigationPose.y);
      return dist <= 0.40;
    }) || (homeWaypoint ? Math.hypot(homeWaypoint.x - navigationPose.x, homeWaypoint.y - navigationPose.y) <= 0.40 : false);

    const shouldPreBackup = preBackupEnabled && isAtDock;

    if (shouldPreBackup) {
      setNavigationStatus('backing_up');
      setNavigationNotice('🔙 [출발 전 안전 후진] 언도킹 안전 공간을 확보하기 위해 후진 중...');
      setControlDirection('backward');

      const backupSpeed = 0.7; // ~0.175 m/s (adequate speed to overcome robot friction)
      sendMoveCommand('backward', backupSpeed);

      if (backupIntervalRef.current !== null) {
        window.clearInterval(backupIntervalRef.current);
      }
      // Stream backward velocity every 100ms so robot hardware watchdog does not stop motors
      backupIntervalRef.current = window.setInterval(() => {
        sendMoveCommand('backward', backupSpeed);
      }, 100);

      backupTimeoutRef.current = window.setTimeout(() => {
        if (backupIntervalRef.current !== null) {
          window.clearInterval(backupIntervalRef.current);
          backupIntervalRef.current = null;
        }
        sendMoveCommand('stop', 0);
        setControlDirection(null);
        backupTimeoutRef.current = window.setTimeout(() => {
          const currentPostBackupPose = navigationPoseRef.current;
          const updatedRoute = findRoute(currentPostBackupPose, target, obstacleScanRef.current) ?? route;
          setPlannedRoute(updatedRoute);
          setRouteOrigin({ x: currentPostBackupPose.x, y: currentPostBackupPose.y });
          setWaypointIndex(0);
          setNavigationNotice(null);
          setNavigationStatus('navigating');
          logNavigation('route_planned', { target: target.name, waypoint_count: updatedRoute.length, route: updatedRoute });
        }, 400);
      }, 1500);
    } else {
      setNavigationNotice(null);
      setNavigationStatus('navigating');
      logNavigation('route_planned', { target: target.name, waypoint_count: route.length, route });
    }
  };
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    document.body.classList.toggle('dark', theme === 'dark');
  }, [theme]);

  useEffect(() => {
    if (!nomiNavigationRequest) return;
    if (handledNomiRequestRef.current === nomiNavigationRequest.request_id) return;
    handledNomiRequestRef.current = nomiNavigationRequest.request_id;
    activeNomiNavigationRef.current = String(nomiNavigationRequest.request_id);
    const normalizePlaceName = (name: string) => name.normalize('NFKC').replace(/[\s_-]+/g, '').toLocaleLowerCase();
    const requestedName = normalizePlaceName(nomiNavigationRequest.name || nomiNavigationRequest.destination);
    const genericSavedName = ['저장된목적지', '저장된장소', '목적지', 'saved', 'saveddestination'].includes(requestedName);
    const homeNames = new Set(['집', 'home', '홈', '홈스테이션', '충전소', '충전위치', '충전도크', '원점', 'chargingstation', 'dock']);
    const homeRequested = nomiNavigationRequest.destination === 'home' || homeNames.has(requestedName);
    const chargingNames = new Set(['충전위치', '충전도크', '충전소', '충전장소', '도킹스테이션', '충전기', 'dock', 'chargingstation']);

    let target: Destination | undefined;
    if (homeRequested || homeNames.has(requestedName)) {
      target = homeWaypoint
        ?? savedDestinations.find(p => homeNames.has(normalizePlaceName(p.name)))
        ?? NAVIGATION_DESTINATIONS.find(p => homeNames.has(normalizePlaceName(p.name)));
    } else if (genericSavedName) {
      target = savedDestinations.length === 1 ? savedDestinations[0] : undefined;
    } else {
      target = savedDestinations.find(place => normalizePlaceName(place.name) === requestedName || (chargingNames.has(requestedName) && chargingNames.has(normalizePlaceName(place.name))))
        ?? NAVIGATION_DESTINATIONS.find(place => normalizePlaceName(place.name) === requestedName || (chargingNames.has(requestedName) && chargingNames.has(normalizePlaceName(place.name))));
    }
    if (!target) {
      setNavigationStatus('stopped');
      reportNomiNavigationResult('FAILED', '저장된 목적지를 찾지 못해 주행을 시작하지 않았습니다.');
      setNavigationNotice(genericSavedName
        ? savedDestinations.length === 0
          ? '저장된 목적지가 없습니다. 웹앱에서 목적지 이름과 좌표를 저장한 뒤 다시 요청해 주세요.'
          : '저장된 목적지가 여러 개입니다. 이동할 목적지 이름을 말씀해 주세요.'
        : `저장된 목적지 '${nomiNavigationRequest.name}'을(를) 찾지 못했습니다. 웹앱에서 목적지를 저장한 뒤 다시 요청해 주세요.`);
      return;
    }
    if (!navigationAvailable) {
      setNavigationNotice(`NOMI 목적지 주행을 시작하지 않았습니다. ${navigationUnavailableReason ?? '주행 상태를 확인할 수 없습니다.'}`);
      setNavigationStatus('stopped');
      sendMoveCommand('stop', 0);
      reportNomiNavigationResult('FAILED', navigationUnavailableReason ?? '주행 상태를 확인할 수 없습니다.');
      return;
    }
    if (pressedKeys.size > 0) stopContinuousMovement();
    beginNavigation(target);
  }, [nomiNavigationRequest, homeWaypoint, savedDestinations, hostStateAvailable, navigationAvailable, navigationUnavailableReason, navigationPose, pressedKeys.size, sendMoveCommand, stopContinuousMovement]);

  useEffect(() => {
    if (!nomiNavigationCancel || activeNomiNavigationRef.current !== nomiNavigationCancel.request_id) return;
    sendEnableDocking(false);
    reportNomiNavigationResult('CANCELLED', 'NOMI에서 자동 주행 취소를 요청했습니다.');
    setNavigationStatus('stopped');
    setControlDirection(null);
    clearPlannedRoute('nomi_task_cancelled');
    sendMoveCommand('stop', 0);
  }, [nomiNavigationCancel, sendEnableDocking, sendMoveCommand]);

  useEffect(() => {
    if (pressedKeys.size > 0 && (plannedRoute.length > 0 || navigationStatus === 'docking')) {
      sendEnableDocking(false);
      reportNomiNavigationResult('CANCELLED', '수동 키 입력으로 자동 주행을 취소했습니다.');
      clearPlannedRoute('manual_keyboard_input');
      setNavigationStatus('stopped');
    }
  }, [pressedKeys.size, navigationStatus, sendEnableDocking]);

  useEffect(() => {
    if (navigationStatus !== 'navigating') return;
    if (!navigationAvailable) {
      setControlDirection(null);
      setNavigationNotice(`자동 주행을 중지했습니다. ${navigationUnavailableReason ?? '주행 상태를 확인할 수 없습니다.'}`);
      clearPlannedRoute('navigation_unavailable');
      setNavigationStatus('stopped');
      sendMoveCommand('stop', 0);
      reportNomiNavigationResult('FAILED', navigationUnavailableReason ?? '주행 상태가 유효하지 않습니다.');
      return;
    }
    if (pressedKeys.size > 0) {
      reportNomiNavigationResult('CANCELLED', '수동 조작으로 자동 주행을 취소했습니다.');
      setControlDirection(null);
      clearPlannedRoute('manual_keyboard_cancel');
      setNavigationStatus('stopped');
      return;
    }
    if (speed <= 0) {
      setControlDirection(null);
      setNavigationNotice('주행 속도가 0이라 목적지 주행을 시작하지 않았습니다. 속도를 입력한 뒤 다시 시도해 주세요.');
      clearPlannedRoute('nonpositive_speed');
      setNavigationStatus('stopped');
      sendMoveCommand('stop', 0);
      reportNomiNavigationResult('FAILED', '주행 속도가 0이어서 자동 주행을 시작하지 않았습니다.');
      return;
    }

    let bestDistance = Number.POSITIVE_INFINITY;
    let bestHeadingError = Number.POSITIVE_INFINITY;
    let bestBrakingRate = Number.POSITIVE_INFINITY;
    let lastProgressAt = performance.now();
    logNavigation('route_follower_started', { waypoint_index: waypointIndex, waypoint_count: plannedRoute.length });
    const timer = window.setInterval(() => {
      const pose = navigationPoseRef.current;
      const waypoint = plannedRoute[waypointIndex] ?? destination;
      const dx = waypoint.x - pose.x;
      const dy = waypoint.y - pose.y;
      const distance = Math.hypot(dx, dy);
      const now = performance.now();
      const liveState = simulationStateRef.current;
      const liveScanStatus = liveState?.obstacle_scan_status;
      if (hostStateAvailable) {
        const liveStateValid = liveState?.host_connected === true
          && Number.isFinite(liveState.state_age) && (liveState.state_age as number) <= 2.5
          && Number.isFinite(liveState.position?.x) && Number.isFinite(liveState.position?.y)
          && Number.isFinite(liveState.rotation?.yaw)
          && liveScanStatus?.healthy === true
          && Number.isFinite(liveScanStatus.age_ms) && (liveScanStatus.age_ms as number) <= 1200
          && Array.isArray(liveState.obstacle_scan);
        if (!liveStateValid) {
          setControlDirection(null);
          setNavigationStatus('stopped');
          setNavigationNotice('MuJoCo 위치 또는 센서 데이터가 오래되었거나 유효하지 않아 자동 주행을 안전 정지했습니다.');
          clearPlannedRoute('live_pose_or_sensor_stale');
          sendMoveCommand('stop', 0);
          reportNomiNavigationResult('FAILED', '최신 위치·방향 또는 센서 상태를 확인하지 못해 안전 정지했습니다.');
          return;
        }
        if (!rangeSensorAvailable) {
          setControlDirection(null);
          setNavigationStatus('stopped');
          setNavigationNotice('MuJoCo 거리 센서 갱신이 중단되어 자동 주행을 안전 정지했습니다.');
          clearPlannedRoute('sensor_unhealthy_or_stale');
          sendMoveCommand('stop', 0);
          reportNomiNavigationResult('FAILED', 'MuJoCo 거리 센서 갱신이 끊겨 안전 정지했습니다.');
          return;
        }
      }
      const desiredYaw = Math.atan2(dy, dx);
      const headingError = Math.abs(Math.atan2(
        Math.sin(desiredYaw - pose.yaw),
        Math.cos(desiredYaw - pose.yaw),
      ));
      const measuredYawRate = simulationStateRef.current?.velocity?.angular?.z ?? 0;
      const actualYawRate = Math.abs(measuredYawRate);
      const liveObstacles = (hostStateAvailable && rangeSensorAvailable) ? obstacleScanRef.current : [];
      const routeBlocked = isRouteBlockedByDynamicObstacle(pose, plannedRoute, waypointIndex, liveObstacles);
      if (routeBlocked && now - lastDynamicReplanRef.current > 900) {
        lastDynamicReplanRef.current = now;
        const reroute = findRoute(pose, destination, liveObstacles);
        if (!reroute) {
          setControlDirection(null);
          setNavigationStatus('blocked');
          setNavigationNotice('실시간 MuJoCo 거리 센서가 경로의 장애물을 감지해 안전 정지했습니다. 장애물이 사라지면 목적지 주행을 다시 시작해 주세요.');
          clearPlannedRoute('dynamic_obstacle_no_safe_reroute', { dynamic_obstacle_count: liveObstacles.length });
          sendMoveCommand('stop', 0);
          reportNomiNavigationResult('FAILED', '실시간 장애물을 피할 안전한 경로를 찾지 못해 정지했습니다.');
          return;
        }
        setPlannedRoute(reroute);
        logNavigation('dynamic_obstacle_reroute', { old_waypoint_count: plannedRoute.length, new_waypoint_count: reroute.length, dynamic_obstacle_count: liveObstacles.length });
        setRouteOrigin({ x: pose.x, y: pose.y });
        setWaypointIndex(0);
        turnDirectionRef.current = null;
        motionGuardRef.current.reset();
        lastControllerPhaseRef.current = null;
        setNavigationNotice('주행 경로에서 장애물을 감지해 우회 경로를 다시 계산했습니다.');
        setControlDirection(null);
        sendMoveCommand('stop', 0);
        return;
      }
      if (distance < bestDistance - 0.01) {
        bestDistance = Math.min(bestDistance, distance);
        lastProgressAt = now;
      }
      if (headingError < bestHeadingError - 0.01) {
        bestHeadingError = headingError;
        lastProgressAt = now;
      }
      if (headingError <= 0.3 && actualYawRate < bestBrakingRate - 0.02) {
        bestBrakingRate = actualYawRate;
        lastProgressAt = now;
      }
      if (now - lastProgressAt > 15000) {
        setControlDirection(null);
        setNavigationStatus('blocked');
        setNavigationNotice('15초 동안 위치 또는 방향 정렬이 진행되지 않아 정지했습니다. 장애물이나 주행 상태를 확인해 주세요.');
        clearPlannedRoute('navigation_progress_timeout', {
          waypoint_index: waypointIndex,
          distance_m: Number(distance.toFixed(3)),
          heading_error_rad: Number(headingError.toFixed(3)),
          yaw_rate_rad_s: Number(actualYawRate.toFixed(3)),
        });
        sendMoveCommand('stop', 0);
        reportNomiNavigationResult('FAILED', '15초 동안 위치 또는 방향 정렬이 진행되지 않아 자동 주행을 정지했습니다.');
        return;
      }
      const segmentStart = waypointIndex === 0
        ? (routeOrigin ?? pose)
        : plannedRoute[waypointIndex - 1];
      const segmentX = waypoint.x - segmentStart.x;
      const segmentY = waypoint.y - segmentStart.y;
      const segmentLengthSquared = segmentX * segmentX + segmentY * segmentY;
      const segmentLength = Math.sqrt(segmentLengthSquared);
      const alongSegment = segmentLengthSquared > 0
        ? ((pose.x - segmentStart.x) * segmentX + (pose.y - segmentStart.y) * segmentY) / segmentLengthSquared
        : 0;
      const crossTrack = segmentLength > 0
        ? Math.abs(segmentX * (pose.y - segmentStart.y) - segmentY * (pose.x - segmentStart.x)) / segmentLength
        : Number.POSITIVE_INFINITY;
      const isFinalWaypoint = waypointIndex >= plannedRoute.length - 1;
      const passCaptureDistance = isFinalWaypoint ? 0.3 : 0.45;
      const passedWaypoint = alongSegment >= 1
        && distance <= passCaptureDistance
        && crossTrack <= 0.35;
      const waypointPositionReached = distance <= 0.3 || (!isFinalWaypoint && passedWaypoint);
      if (waypointPositionReached) {
        if (!isFinalWaypoint) {
          // Seamlessly advance to the next route waypoint without stopping or jerking
          turnDirectionRef.current = null;
          lastControllerPhaseRef.current = null;
          motionGuardRef.current.reset();
          setWaypointIndex(index => index + 1);
          return;
        }

        // Only at the final destination do we settle and align heading
        if (actualYawRate <= 0.08) {
          // Final waypoint reached: check final heading alignment if destination.yaw is specified
          if (typeof destination.yaw === 'number' && Number.isFinite(destination.yaw)) {
            const targetYawRad = (destination.yaw * Math.PI) / 180;
            const headingErr = Math.atan2(Math.sin(targetYawRad - pose.yaw), Math.cos(targetYawRad - pose.yaw));
            if (Math.abs(headingErr) > 0.15) {
              const rotDir = headingErr > 0 ? 'rotate_left' : 'rotate_right';
              setControlDirection(rotDir);
              const rotSpeed = Math.max(0.18, Math.min(0.35, Math.abs(headingErr) * 0.7));
              sendMoveCommand(rotDir, rotSpeed);
              return;
            }
          }

          setControlDirection(null);
          sendMoveCommand('stop', 0);
          const actualPose = navigationPoseRef.current;
          const arrivalDistance = Math.hypot(destination.x - actualPose.x, destination.y - actualPose.y);
          if (navigationAvailable && arrivalDistance <= 0.35) {
            const normName = (destination.name || '').replace(/[\s_-]+/g, '').toLowerCase();
            const shouldDock = Boolean(
              destination.auto_dock ||
              (destination.tag_id !== undefined && destination.tag_id !== null) ||
              ['집', 'home', '홈', '홈스테이션', '충전소', '충전위치', '충전도크', '충전장소', '도킹스테이션', '충전기', 'dock', 'chargingstation'].includes(normName)
            );

            if (shouldDock) {
              setNavigationStatus('docking');
              sendEnableDocking(true);
              setNavigationNotice(`🎯 ${destination.name || '목적지'} 정렬 완료: AprilTag 정밀 도킹을 시작합니다...`);
              return;
            }

            setNavigationStatus('arrived');
            reportNomiNavigationResult('SUCCEEDED', `${destination.name || '목적지'}에 도착했습니다. (방향 정렬 완료)`, actualPose, arrivalDistance);
          } else {
            setNavigationStatus('blocked');
            setNavigationNotice('목적지 도착을 위치 데이터로 확인하지 못해 완료 처리하지 않았습니다.');
            reportNomiNavigationResult('FAILED', '현재 위치가 목적지 0.3m 이내라는 도착 확인이 없습니다.', actualPose, arrivalDistance);
          }
          return;
        }
      }

      const command = computeWaypointCommand(
        pose,
        waypoint,
        speed,
        turnDirectionRef.current,
        measuredYawRate,
        isFinalWaypoint,
      );
      turnDirectionRef.current = command.turnDirection;
      if (command.phase !== lastControllerPhaseRef.current) {
        logNavigation('controller_phase', {
          phase: command.phase,
          waypoint_index: waypointIndex,
          distance_m: Number(command.distance.toFixed(3)),
          desired_yaw_rad: Number(command.desiredYaw.toFixed(3)),
          heading_error_rad: Number(command.headingError.toFixed(3)),
          angular_command_rad_s: Number(command.angular.toFixed(3)),
        });
        lastControllerPhaseRef.current = command.phase;
      }
      const actualVelocity = simulationStateRef.current?.velocity?.linear;
      const actualAngular = simulationStateRef.current?.velocity?.angular?.z ?? 0;
      const motionFault = motionGuardRef.current.observe({
        nowMs: now,
        distance: command.distance,
        commandedX: command.linearX,
        commandedY: command.linearY,
        actualX: actualVelocity?.x ?? 0,
        actualY: actualVelocity?.y ?? 0,
        commandedAngular: command.angular,
        actualAngular,
        damping: command.damping,
      });
      if (hostStateAvailable && motionFault) {
        logNavigation('motion_guard_fault', {
          reason: motionFault,
          phase: command.phase,
          commanded_angular_rad_s: Number(command.angular.toFixed(3)),
          actual_angular_rad_s: Number(actualAngular.toFixed(3)),
          heading_error_rad: Number(command.headingError.toFixed(3)),
        });
        setControlDirection(null);
        setNavigationStatus('blocked');
        setNavigationNotice(motionFault);
        clearPlannedRoute('motion_feedback_mismatch', { reason: motionFault });
        sendMoveCommand('stop', 0);
        reportNomiNavigationResult('FAILED', motionFault);
        return;
      }
      setControlDirection(command.phase === 'aligning' || command.phase === 'braking'
        ? command.angular >= 0 ? 'rotate_left' : 'rotate_right'
        : command.phase === 'driving' ? 'forward' : null);
      sendDriveCommand(command.linearX, command.linearY, command.angular);
    }, 100);
    return () => {
      window.clearInterval(timer);
      logNavigation('route_follower_stopped', { waypoint_index: waypointIndex, waypoint_count: plannedRoute.length });
    };
  }, [destination, hostStateAvailable, navigationAvailable, navigationUnavailableReason, navigationStatus, plannedRoute, pressedKeys.size, rangeSensorAvailable, routeOrigin, sendDriveCommand, sendEnableDocking, sendMoveCommand, speed, waypointIndex]);

  // Monitor precision docking completion / timeout
  useEffect(() => {
    if (navigationStatus !== 'docking') return;

    if (dockingStatus?.state === 'DOCKED') {
      sendEnableDocking(false);
      setNavigationStatus('docked');
      setNavigationNotice('🎉 AprilTag 정밀 도킹 완료: 로봇암 작업 대기 중 (/amr/docking_complete 발행)...');
      sendDockingComplete({ destination: destination.name, tag_id: destination.tag_id, status: 'DOCKED' });
      const actualPose = navigationPoseRef.current;
      const arrivalDistance = Math.hypot(destination.x - actualPose.x, destination.y - actualPose.y);
      reportNomiNavigationResult('SUCCEEDED', `${destination.name || '목적지'} AprilTag 정밀 도킹에 성공했습니다.`, actualPose, arrivalDistance);
      return;
    }

    // Safety timeout for visual docking (35 seconds)
    const timeout = setTimeout(() => {
      if (navigationStatus === 'docking') {
        sendEnableDocking(false);
        setNavigationStatus('blocked');
        setNavigationNotice('⚠️ AprilTag 도킹 제한 시간 초과: 마커 안착이 완료되지 못했습니다.');
        const actualPose = navigationPoseRef.current;
        const arrivalDistance = Math.hypot(destination.x - actualPose.x, destination.y - actualPose.y);
        reportNomiNavigationResult('FAILED', `${destination.name || '목적지'} AprilTag 비전 도킹 시간 초과 (미안착)`, actualPose, arrivalDistance);
      }
    }, 35000);

    return () => clearTimeout(timeout);
  }, [navigationStatus, dockingStatus?.state, sendEnableDocking, sendDockingComplete, destination, reportNomiNavigationResult]);

  useEffect(() => {
    if (wallAlignResult?.success) {
      setLocalPose({ x: wallAlignResult.x, y: wallAlignResult.y, yaw: wallAlignResult.yaw });
      setNavigationNotice(`🎯 벽면 스캔 매칭 완료: 오차 ${wallAlignResult.residual_cm.toFixed(1)}cm (${wallAlignResult.x.toFixed(2)}, ${wallAlignResult.y.toFixed(2)})`);
    } else if (wallAlignResult && !wallAlignResult.success) {
      setNavigationNotice(`⚠️ 벽면 스캔 매칭: ${wallAlignResult.message}`);
    }
  }, [wallAlignResult]);

  // Monitor external robot arm (secondary laptop) task completion -> auto undock & return home
  useEffect(() => {
    if (!armTaskComplete || navigationStatus !== 'docked') return;
    setNavigationNotice('🤖 [로봇암 작업 완료 수신] 수건 개기가 완료되었습니다! 안전 후진(언도킹) 후 홈 스테이션으로 복귀합니다...');
    if (homeWaypoint && destination.name !== homeWaypoint.name) {
      const timer = window.setTimeout(() => {
        beginNavigation(homeWaypoint);
      }, 1200);
      return () => window.clearTimeout(timer);
    }
  }, [armTaskComplete, navigationStatus, homeWaypoint, destination.name]);

  useEffect(() => {
    if (commandFeedback?.direction !== 'drive_vector') return;
    if (isLocalSim) return;
    if (navigationStatus !== 'navigating') {
      lastHandledDriveFeedbackSequenceRef.current = Math.max(
        lastHandledDriveFeedbackSequenceRef.current,
        commandFeedback.sequence,
      );
      return;
    }
    if (commandFeedback.status === 'pending'
      || commandFeedback.sequence <= lastHandledDriveFeedbackSequenceRef.current) return;

    lastHandledDriveFeedbackSequenceRef.current = commandFeedback.sequence;
    if (!shouldStopForDriveFeedback(commandFeedback.status)) return;

    const reason = commandFeedback.message || '자동 주행 명령이 서버 안전 검사에서 거부되었습니다.';
    setControlDirection(null);
    setNavigationStatus('blocked');
    setNavigationNotice(`자동 주행 명령을 거부해 정지했습니다. ${reason}`);
    navigationEffectsRef.current.clearPlannedRoute('drive_command_rejected', {
      sequence: commandFeedback.sequence,
      reason,
    });
    sendMoveCommand('stop', 0);
    navigationEffectsRef.current.reportNomiNavigationResult('FAILED', reason);
  }, [commandFeedback, navigationStatus, sendMoveCommand]);

  const handleRotate = (direction: string) => {
    reportNomiNavigationResult('CANCELLED', '수동 회전 명령으로 자동 주행을 취소했습니다.');
    setNavigationStatus('stopped');
    setControlDirection(direction);
    clearPlannedRoute('manual_rotate');
    sendMoveCommand(direction, speed);
  };

  const handleMoveStop = () => {
    reportNomiNavigationResult('CANCELLED', '수동 정지 명령으로 자동 주행을 취소했습니다.');
    setNavigationStatus('stopped');
    setControlDirection(null);
    setJoystick({ x: 0, y: 0 });
    clearPlannedRoute('manual_stop');
    sendMoveCommand('stop', 0);
  };

  const handleJoystickMove = (vector: StickVector) => {
    reportNomiNavigationResult('CANCELLED', '조이스틱 입력으로 자동 주행을 취소했습니다.');
    setNavigationStatus('stopped');
    clearPlannedRoute('joystick_input');
    setJoystick(vector);
    const magnitude = Math.min(1, Math.hypot(vector.x, vector.y));
    if (magnitude < 0.1) {
      setControlDirection(null);
      sendMoveCommand('stop', 0);
      return;
    }

    // Joystick coordinate:
    // vector.x: -1 (left) ~ +1 (right)
    // vector.y: -1 (up / 전진) ~ +1 (down / 후진)
    const linY = -vector.y; // > 0: forward, < 0: backward
    const angX = -vector.x; // > 0: left (CCW), < 0: right (CW)

    let direction: string;
    let driveSpeed: number;
    let angularSpeed = 0;

    if (linY > 0.1) {
      direction = 'forward';
      driveSpeed = Math.min(1, Math.abs(linY)) * speed;
      angularSpeed = Math.max(-0.7, Math.min(0.7, angX * 0.7));
      setControlDirection('forward');
    } else if (linY < -0.1) {
      direction = 'backward';
      driveSpeed = Math.min(1, Math.abs(linY)) * speed;
      angularSpeed = Math.max(-0.7, Math.min(0.7, angX * 0.7));
      setControlDirection('backward');
    } else {
      direction = angX > 0 ? 'rotate_left' : 'rotate_right';
      driveSpeed = Math.min(1, Math.abs(angX)) * speed;
      angularSpeed = angX > 0 ? 0.7 : -0.7;
      setControlDirection(direction);
    }

    const now = performance.now();
    if (now - lastJoystickCommandRef.current >= 60) {
      lastJoystickCommandRef.current = now;
      sendMoveCommand(direction, driveSpeed, angularSpeed);
    }
  };

  const handleEmergencyStop = () => {
    clearBackupTimeout();
    sendEnableDocking(false);
    reportNomiNavigationResult('CANCELLED', '긴급 정지로 자동 주행을 취소했습니다.');
    setNavigationStatus('stopped');
    stopContinuousMovement();
    setControlDirection(null);
    clearPlannedRoute('emergency_stop');
    sendMoveCommand('stop', 0);
  };

  const handlePoseUpdate = (pose: { x: number; y: number; yaw: number }) => {
    setLocalPose(pose);
    saveLastRobotPose(pose);
  };

  const handleSetRobotPose = (pose: { x: number; y: number; yaw: number }) => {
    setLocalPose(pose);
    saveLastRobotPose(pose);
    socket?.emit('set_robot_pose', pose);
  };

  const handleReset = () => {
    clearBackupTimeout();
    reportNomiNavigationResult('CANCELLED', '시뮬레이션 초기화로 자동 주행을 취소했습니다.');
    setNavigationStatus('idle');
    setControlDirection(null);
    clearPlannedRoute('simulation_reset');
    setNavigationNotice(null);
    const home = readSavedHome();
    const resetPose = {
      x: home?.x ?? INDOOR_MAP.start.x,
      y: home?.y ?? INDOOR_MAP.start.y,
      yaw: ((home?.yaw ?? -15.5) * Math.PI) / 180,
    };
    setLocalPose(resetPose);
    saveLastRobotPose(resetPose);
    setResetToken(token => token + 1);
    socket?.emit('set_robot_pose', resetPose);
    socket?.emit('reset_robot');
  };

  const startNavigation = () => {
    if (!destination.name.trim() || !Number.isFinite(destination.x) || !Number.isFinite(destination.y)
      || destination.x < INDOOR_MAP.bounds.minX || destination.x > INDOOR_MAP.bounds.maxX
      || destination.y < INDOOR_MAP.bounds.minY || destination.y > INDOOR_MAP.bounds.maxY) return;
    beginNavigation(destination);
  };

  const stopNavigation = () => {
    clearBackupTimeout();
    sendEnableDocking(false);
    reportNomiNavigationResult('CANCELLED', '사용자가 목적지 주행을 취소했습니다.');
    setNavigationStatus('stopped');
    setControlDirection(null);
    clearPlannedRoute('navigation_stopped_by_user');
    sendMoveCommand('stop', 0);
  };

  const handleStartDockingTest = () => {
    clearBackupTimeout();
    clearPlannedRoute('docking_test_started');
    setNavigationStatus('docking');
    setNavigationNotice('🎯 [단독 테스트] AprilTag 정밀 비전 도킹을 시작합니다...');
    sendEnableDocking(true);
  };

  const handleStopDockingTest = () => {
    sendEnableDocking(false);
    setNavigationStatus('stopped');
    setNavigationNotice('🛑 도킹이 사용자에 의해 정지되었습니다.');
    sendMoveCommand('stop', 0);
  };

  const handleUndockTest = () => {
    sendEnableDocking(false);
    setNavigationStatus('backing_up');
    setNavigationNotice('🔙 [테스트] 도킹 재시험을 위해 40cm 안전 후진 중...');
    sendDriveCommand(-0.10, 0, 0);
    setTimeout(() => {
      sendMoveCommand('stop', 0);
      setNavigationStatus('idle');
      setNavigationNotice('✅ 후진 완료: 이제 다시 도킹 테스트를 시작할 수 있습니다.');
    }, 4000);
  };

  const persistDestination = (place: Destination) => {
    if (!place.name || !Number.isFinite(place.x) || !Number.isFinite(place.y)
      || place.x < INDOOR_MAP.bounds.minX || place.x > INDOOR_MAP.bounds.maxX
      || place.y < INDOOR_MAP.bounds.minY || place.y > INDOOR_MAP.bounds.maxY) return;
    const cleanPlace: Destination = {
      name: place.name.trim(),
      x: place.x,
      y: place.y,
      yaw: typeof place.yaw === 'number' && Number.isFinite(place.yaw) ? place.yaw : 0,
      tag_id: typeof place.tag_id === 'number' ? place.tag_id : undefined,
      auto_dock: typeof place.auto_dock === 'boolean' ? place.auto_dock : undefined,
    };
    const updated = [...savedDestinations.filter(item => item.name !== cleanPlace.name), cleanPlace].slice(-30);
    setSavedDestinations(updated);
    try {
      localStorage.setItem('xlerobot.savedDestinations', JSON.stringify(updated));
    } catch {
      // The current session still keeps saved places if browser storage is blocked.
    }
    if (cleanPlace.name.normalize('NFKC').trim() === '집' || cleanPlace.name === '홈 스테이션') {
      setHomeWaypoint(cleanPlace);
      try {
        localStorage.setItem('xlerobot.homeWaypoint', JSON.stringify(cleanPlace));
      } catch {
        // The current session still uses this home waypoint.
      }
    }
  };

  const saveDestination = () => {
    persistDestination({
      name: destination.name.trim(),
      x: destination.x,
      y: destination.y,
      yaw: destination.yaw ?? 0,
      tag_id: destination.tag_id,
      auto_dock: destination.auto_dock,
    });
  };

  const deleteDestination = (name: string) => {
    const updated = savedDestinations.filter(item => item.name !== name);
    setSavedDestinations(updated);
    try {
      localStorage.setItem('xlerobot.savedDestinations', JSON.stringify(updated));
    } catch {
      // The current session still keeps saved places if browser storage is blocked.
    }
  };

  const updateWaypointPosition = (name: string, x: number, y: number, yaw?: number) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)
      || x < INDOOR_MAP.bounds.minX || x > INDOOR_MAP.bounds.maxX
      || y < INDOOR_MAP.bounds.minY || y > INDOOR_MAP.bounds.maxY) return;
    const roundedX = Number(x.toFixed(2));
    const roundedY = Number(y.toFixed(2));
    const existing = savedDestinations.find(item => item.name === name);
    const resolvedYaw = typeof yaw === 'number' && Number.isFinite(yaw)
      ? yaw
      : (existing?.yaw ?? destination.yaw ?? 0);
    const updated = existing
      ? savedDestinations.map(item => item.name === name ? { ...item, x: roundedX, y: roundedY, yaw: resolvedYaw } : item)
      : [...savedDestinations, { name, x: roundedX, y: roundedY, yaw: resolvedYaw }];

    setSavedDestinations(updated);
    try {
      localStorage.setItem('xlerobot.savedDestinations', JSON.stringify(updated));
    } catch {
      // ignore
    }
    if (destination.name === name) {
      setDestination({ ...destination, x: roundedX, y: roundedY, yaw: resolvedYaw });
    }
    if (name.normalize('NFKC').trim() === '집' || name === '홈 스테이션') {
      const homePlace: Destination = { name, x: roundedX, y: roundedY, yaw: resolvedYaw };
      setHomeWaypoint(homePlace);
      try {
        localStorage.setItem('xlerobot.homeWaypoint', JSON.stringify(homePlace));
      } catch {
        // ignore
      }
    }
  };

  const updateWaypointYaw = (name: string, yaw: number) => {
    if (!Number.isFinite(yaw)) return;
    const normalizedYaw = Number((((yaw + 180) % 360 + 360) % 360 - 180).toFixed(1));
    const existing = savedDestinations.find(item => item.name === name);
    if (!existing) {
      if (destination.name === name) {
        setDestination({ ...destination, yaw: normalizedYaw });
      }
      return;
    }
    const updated = savedDestinations.map(item => item.name === name ? { ...item, yaw: normalizedYaw } : item);
    setSavedDestinations(updated);
    try {
      localStorage.setItem('xlerobot.savedDestinations', JSON.stringify(updated));
    } catch {
      // ignore
    }
    if (destination.name === name) {
      setDestination({ ...destination, yaw: normalizedYaw });
    }
    if (name.normalize('NFKC').trim() === '집' || name === '홈 스테이션') {
      const homePlace: Destination = { ...existing, yaw: normalizedYaw };
      setHomeWaypoint(homePlace);
      try {
        localStorage.setItem('xlerobot.homeWaypoint', JSON.stringify(homePlace));
      } catch {
        // ignore
      }
    }
  };

  const currentPosition = hostPositionFresh
    ? { x: hostPosition!.x!, y: hostPosition!.y! }
    : null;
  const currentPositionUnavailableReason = status.socket !== 'connected'
    ? '현재 위치를 저장할 수 없습니다. 웹 서버 연결이 없습니다.'
    : simulationState?.host_connected !== true
      ? '현재 위치를 저장할 수 없습니다. 위치 데이터가 없습니다.'
      : !Number.isFinite(stateAge)
        ? '현재 위치를 저장할 수 없습니다. 위치 데이터 대기 중입니다.'
        : (stateAge as number) > 2.5
          ? `현재 위치를 저장할 수 없습니다. 위치 데이터가 오래됐습니다 (${(stateAge as number).toFixed(1)}초).`
          : '현재 위치 좌표가 올바르지 않습니다.';
  const saveCurrentPosition = () => {
    if (!currentPosition || !destination.name.trim()) return;
    const curYawDeg = Number(((navigationPose.yaw * 180 / Math.PI + 180) % 360 - 180).toFixed(1));
    const place: Destination = {
      name: destination.name.trim(),
      x: currentPosition.x,
      y: currentPosition.y,
      yaw: curYawDeg,
      tag_id: destination.tag_id,
      auto_dock: destination.auto_dock,
    };
    setDestination(place);
    persistDestination(place);
  };

  const destinationNavigator = <DestinationNavigator
    destination={destination}
    homeWaypoint={homeWaypoint}
    savedDestinations={savedDestinations}
    plannedRoute={plannedRoute}
    navigationNotice={navigationNotice}
    currentPose={navigationPose}
    currentPosition={currentPosition}
    currentPositionUnavailableReason={currentPositionUnavailableReason}
    navigationAvailable={navigationAvailable}
    navigationUnavailableReason={navigationUnavailableReason}
    status={navigationStatus}
    onDestinationChange={editDestination}
    onSelectDestination={editDestination}
    onStart={startNavigation}
    onStop={stopNavigation}
    onSaveDestination={saveDestination}
    onSaveCurrentPosition={saveCurrentPosition}
    onDeleteDestination={deleteDestination}
    preBackupEnabled={preBackupEnabled}
    onTogglePreBackup={handleTogglePreBackup}
    dockingStatus={dockingStatus}
    onEnableDocking={sendEnableDocking}
    onMockArmComplete={() => triggerMockArmTaskComplete('fold_towel')}
    onPublishDockingComplete={() => sendDockingComplete({ destination: destination.name || '테스트 목적지', tag_id: destination.tag_id ?? 1, status: 'DOCKED' })}
    armTaskComplete={armTaskComplete}
    onRequestWallAlignment={(target) => requestWallAlignment(target || navigationPose)}
    wallAlignResult={wallAlignResult}
    onStartDockingTest={handleStartDockingTest}
    onStopDockingTest={handleStopDockingTest}
    onUndockTest={handleUndockTest}
  />;

  const socketConnected = status.socket === 'connected';
  const hostConnected = hostStateAvailable;

  return (
    <main className="app-shell min-h-[100svh] selection:bg-blue-200">
      <div className="mx-auto max-w-[1720px] px-3 pb-[calc(5.25rem+env(safe-area-inset-bottom))] pt-3 sm:px-6 sm:pb-8 sm:pt-4 lg:px-8">
        <header className="app-header mb-3 flex flex-wrap items-center justify-between gap-3 border-b pb-3 sm:mb-5 sm:gap-4 sm:pb-4">
          <div className="flex items-center gap-3">
            <div className="grid h-10 w-10 place-items-center rounded-xl border border-blue-100 bg-blue-50 text-blue-600"><Bot size={22} /></div>
            <div>
              <div className="text-[10px] font-bold tracking-[0.18em] text-blue-600">NOMI × XLEROBOT</div>
              <h1 className="text-base font-bold tracking-tight text-[var(--ui-fg)] sm:text-xl">SIM ARENA <span className="ml-1 hidden font-medium text-[var(--ui-muted)] sm:inline">/ PILOT CONSOLE</span></h1>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <div className="status-pill flex items-center gap-2 rounded-xl border px-3 py-2 text-xs">
              <span className={`h-2 w-2 rounded-full ${socketConnected ? 'bg-emerald-400' : 'bg-amber-400'}`} />
              <span className="text-[var(--ui-muted)] sm:hidden">{socketConnected ? (hostConnected ? 'HOST LIVE' : 'SERVER') : 'LOCAL'}</span>
              <span className="hidden text-[var(--ui-muted)] sm:inline">{socketConnected ? (hostConnected ? 'SERVER + MUJOCO LIVE' : 'SERVER CONNECTED · HOST WAITING') : 'LOCAL SIMULATION'}</span>
            </div>
            {commandFeedback && <div className="status-pill hidden rounded-xl border px-3 py-2 text-[10px] text-[var(--ui-muted)] md:block" title={commandFeedback.message ?? ''}>
              CMD #{commandFeedback.sequence} · {commandFeedback.status.toUpperCase()}{commandFeedback.latencyMs !== null ? ` · ${commandFeedback.latencyMs} ms` : ''}
            </div>}
            <button onClick={sendPing} title="Ping simulation server" className="toolbar-button rounded-xl border p-2.5 transition"><Radio size={16} /></button>
            <button onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')} title="Toggle theme" className="toolbar-button rounded-xl border p-2.5 transition">
              {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
            </button>
          </div>
        </header>

        <div className="mobile-dashboard-grid grid grid-cols-1 items-start gap-5 xl:grid-cols-[minmax(0,1.85fr)_minmax(360px,0.9fr)]">
          <section className="min-w-0">
            <div className="mb-2 flex items-end justify-between gap-3 sm:mb-3">
              <div>
                <div className="text-[10px] font-semibold tracking-[0.12em] text-[var(--ui-muted)]">시뮬레이션 · 자유 주행</div>
                <h2 className="mt-1 text-sm font-semibold text-[var(--ui-fg)]">XLeRobot 시뮬레이션 구역</h2>
              </div>
              <div className="hidden items-center gap-2 text-[10px] font-medium tracking-wide text-[var(--ui-muted)] sm:flex"><Cpu size={13} className="text-blue-600" /> SIMULATION VIEW</div>
            </div>
            <GameSimulationCanvas
              simulationState={hostConnected ? simulationState : null}
              pressedKeys={pressedKeys}
              controlDirection={controlDirection}
              joystick={joystick}
              speed={speed}
              connected={socketConnected}
              resetToken={resetToken}
              onReset={handleReset}
              onPoseUpdate={handlePoseUpdate}
              onDestinationChange={editDestination}
              onSetRobotPose={handleSetRobotPose}
              onUpdateWaypointPosition={updateWaypointPosition}
              onUpdateWaypointYaw={updateWaypointYaw}
              destination={destination}
              savedDestinations={savedDestinations}
              plannedRoute={plannedRoute}
            />
            <div className="mt-3 hidden xl:block xl:mt-4">
              <GameControlDeck
                speed={speed}
                onSpeedChange={setSpeed}
                onJoystickMove={handleJoystickMove}
                onRotate={handleRotate}
                onMoveStop={handleMoveStop}
                onEmergencyStop={handleEmergencyStop}
                onReset={handleReset}
                connected={hostConnected}
              />
              <div className="mt-3">{destinationNavigator}</div>
            </div>
          </section>

          <aside className="hidden space-y-4 xl:block">
            <RobotTelemetry
              state={hostConnected ? simulationState : null}
              localPose={localPose}
              speed={speed}
              moving={isMoving}
              socketStatus={status.socket}
            />
            <NomiPanel socket={socket} theme={theme} />
            <TowelPlacementPanel />
          </aside>
        </div>

        <section className="mobile-control-panel mt-3 xl:hidden" aria-label="Mobile control panel">
          {mobilePanel === 'drive' && (
            <>
              <GameControlDeck
                speed={speed}
                onSpeedChange={setSpeed}
                onJoystickMove={handleJoystickMove}
                onRotate={handleRotate}
                onMoveStop={handleMoveStop}
                onEmergencyStop={handleEmergencyStop}
                onReset={handleReset}
                connected={hostConnected}
              />
              <div className="mt-3">{destinationNavigator}</div>
            </>
          )}
          {mobilePanel === 'nomi' && <><NomiPanel socket={socket} theme={theme} /><TowelPlacementPanel /></>}
          {mobilePanel === 'status' && (
            <RobotTelemetry state={hostConnected ? simulationState : null} localPose={localPose} speed={speed} moving={isMoving} socketStatus={status.socket} />
          )}
        </section>

        <footer className="mt-5 hidden flex-wrap items-center justify-between gap-2 border-t border-[var(--ui-card-border)] pt-3 text-[10px] font-medium tracking-wide text-[var(--ui-muted)] xl:flex">
          <span>W A S D / ARROWS · MOVE</span>
          <span>Q / E · 회전</span>
          <span>시뮬레이션 데이터 · 카메라 미사용</span>
        </footer>
      </div>
      <nav className="mobile-nav fixed inset-x-0 bottom-0 z-40 grid grid-cols-3 border-t px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))] backdrop-blur-xl xl:hidden" aria-label="Mobile navigation">
        {([
          ['drive', Gamepad2, '조종'],
          ['nomi', MessageCircle, 'NOMI'],
          ['status', SlidersHorizontal, '상태'],
        ] as const).map(([panel, Icon, label]) => (
          <button key={panel} onClick={() => setMobilePanel(panel)} aria-current={mobilePanel === panel ? 'page' : undefined} className={`mobile-nav-button flex min-h-12 flex-col items-center justify-center gap-1 rounded-xl text-[11px] font-semibold transition ${mobilePanel === panel ? 'is-active' : ''}`}>
            <Icon size={19} strokeWidth={2.2} />{label}
          </button>
        ))}
      </nav>
    </main>
  );
}
