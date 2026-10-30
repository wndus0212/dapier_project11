import { useState } from 'react';
import { Camera, ChevronDown, ChevronUp, Crosshair, Home, MapPin, Navigation, Radio, RefreshCw, Scan, Square, Zap } from 'lucide-react';
import type { RoutePoint } from '../../utils/pathPlanner';
import { CHARGING_DESTINATION, TABLE_APPROACH_DESTINATIONS, INDOOR_MAP } from '../../utils/indoorMap';
import type { DockingStatus } from '../../types';

export interface Destination {
  name: string;
  x: number;
  y: number;
  yaw?: number;
  tag_id?: number | null;
  auto_dock?: boolean;
}

interface DestinationNavigatorProps {
  destination: Destination;
  homeWaypoint: Destination | null;
  savedDestinations: Destination[];
  plannedRoute: RoutePoint[];
  navigationNotice: string | null;
  currentPose: { x: number; y: number; yaw: number };
  currentPosition: { x: number; y: number } | null;
  currentPositionUnavailableReason: string | null;
  navigationAvailable: boolean;
  navigationUnavailableReason: string | null;
  status: 'idle' | 'backing_up' | 'navigating' | 'docking' | 'docked' | 'arrived' | 'stopped' | 'blocked';
  onDestinationChange: (destination: Destination) => void;
  onSelectDestination: (destination: Destination) => void;
  onStart: () => void;
  onStop: () => void;
  onSaveDestination: () => void;
  onSaveCurrentPosition: () => void;
  onDeleteDestination?: (name: string) => void;
  preBackupEnabled?: boolean;
  onTogglePreBackup?: (enabled: boolean) => void;
  dockingStatus?: DockingStatus | null;
  onEnableDocking?: (enabled: boolean) => void;
  onMockArmComplete?: () => void;
  onPublishDockingComplete?: () => void;
  armTaskComplete?: { status: string; task?: string } | null;
  onStartDockingTest?: () => void;
  onStopDockingTest?: () => void;
  onUndockTest?: () => void;
  onRequestWallAlignment?: (target?: { x: number; y: number; yaw?: number; name?: string }) => void;
  wallAlignResult?: { success: boolean; message?: string; residual_cm?: number; matched_area?: string } | null;
}

export function DestinationNavigator({
  destination,
  homeWaypoint,
  savedDestinations,
  plannedRoute,
  navigationNotice,
  currentPose,
  currentPosition,
  currentPositionUnavailableReason,
  navigationAvailable,
  navigationUnavailableReason,
  status,
  onDestinationChange,
  onSelectDestination,
  onStart,
  onStop,
  onSaveDestination,
  onSaveCurrentPosition,
  onDeleteDestination,
  preBackupEnabled = true,
  onTogglePreBackup,
  dockingStatus,
  onEnableDocking,
  onMockArmComplete,
  onPublishDockingComplete,
  armTaskComplete,
  onStartDockingTest,
  onStopDockingTest,
  onUndockTest,
  onRequestWallAlignment,
  wallAlignResult,
}: DestinationNavigatorProps) {
  const [showCamera, setShowCamera] = useState(true);
  const [streamKey, setStreamKey] = useState(0);
  const [lastTopicMessage, setLastTopicMessage] = useState<string | null>(null);

  const setPreset = (name: string, x: number, y: number, yaw?: number, auto_dock?: boolean, tag_id?: number) => {
    const isDock = auto_dock ?? (name.includes('집') || name.includes('충전') || name.includes('홈'));
    onDestinationChange({
      name,
      x,
      y,
      yaw: yaw ?? 0,
      auto_dock: isDock,
      tag_id: tag_id ?? (isDock ? 1 : undefined),
    });
  };
  const destinationNameValid = destination.name.trim().length > 0;
  const destinationCoordinatesValid = Number.isFinite(destination.x) && Number.isFinite(destination.y)
    && destination.x >= INDOOR_MAP.bounds.minX && destination.x <= INDOOR_MAP.bounds.maxX
    && destination.y >= INDOOR_MAP.bounds.minY && destination.y <= INDOOR_MAP.bounds.maxY;
  const movementUnavailableReason = !navigationAvailable
    ? navigationUnavailableReason ?? '주행 상태를 확인할 수 없습니다.'
    : !destinationNameValid
      ? '목적지를 선택하거나 이름을 입력하세요.'
      : !destinationCoordinatesValid
        ? '목적지 좌표가 비어 있거나 허용된 지도 범위를 벗어났습니다.'
        : null;

  const isDockingActive = status === 'docking' || (dockingStatus && dockingStatus.state !== 'IDLE' && dockingStatus.state !== 'DOCKED');

  return (
    <section className="surface-card rounded-2xl border p-4 sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[10px] font-bold tracking-[0.2em] text-slate-500"><Navigation size={13} /> 목적지 주행</div>
          <div className="mt-1 text-sm font-semibold text-[var(--ui-fg)]">위치와 방향을 지정하면 자동으로 이동 후 정밀 도킹합니다</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {onRequestWallAlignment && (
            <button
              type="button"
              onClick={() => onRequestWallAlignment({
                x: Number.isFinite(destination.x) ? destination.x : currentPose.x,
                y: Number.isFinite(destination.y) ? destination.y : currentPose.y,
                yaw: Number.isFinite(destination.yaw) ? destination.yaw : currentPose.yaw,
                name: destination.name || '현재 위치',
              })}
              className="flex items-center gap-1.5 rounded-lg border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-xs font-bold text-indigo-700 hover:bg-indigo-100 dark:border-indigo-800 dark:bg-indigo-950 dark:text-indigo-300 transition shadow-sm"
              title={`${destination.name || '선택 위치'} 주변 벽면과 실시간 라이다를 대조하여 로봇 위치를 자동 보정합니다`}
            >
              <Scan size={13} className="text-indigo-600 dark:text-indigo-400" />
              <span>
                {destination.name && destination.name !== '홈 스테이션'
                  ? `🎯 ${destination.name.split(' ')[0]} 기준 벽면 맞춤`
                  : '🎯 벽면 스캔 위치 맞춤'}
              </span>
              {wallAlignResult && typeof wallAlignResult.residual_cm === 'number' && wallAlignResult.residual_cm < 100 && (
                <span className="ml-0.5 rounded bg-indigo-200/60 px-1 py-0.2 text-[9px] font-mono text-indigo-900 dark:bg-indigo-900 dark:text-indigo-200">
                  {wallAlignResult.residual_cm.toFixed(1)}cm
                </span>
              )}
            </button>
          )}
          <span className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${
          status === 'docking'
            ? 'bg-purple-100 text-purple-700 animate-pulse border border-purple-300'
            : status === 'docked'
              ? 'bg-emerald-100 text-emerald-700 border border-emerald-300 font-extrabold'
              : status === 'navigating'
                ? 'bg-blue-100 text-blue-700'
                : status === 'arrived'
                  ? 'bg-emerald-100 text-emerald-700'
                  : status === 'blocked'
                    ? 'bg-rose-100 text-rose-700'
                    : 'bg-slate-100 text-slate-600'
        }`}>
          {status === 'docking'
            ? '🎯 마커 정밀 도킹 중...'
            : status === 'docked'
              ? '🎉 도킹 완료'
              : status === 'navigating'
                ? '이동 중'
                : status === 'arrived'
                  ? '도착 (정렬 완료)'
                  : status === 'blocked'
                    ? '진전 없음 · 정지'
                    : status === 'stopped'
                      ? '정지됨'
                      : '대기'}
        </span>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" disabled={!homeWaypoint} onClick={() => homeWaypoint && setPreset(homeWaypoint.name, homeWaypoint.x, homeWaypoint.y, homeWaypoint.yaw, true, 1)} title={!homeWaypoint ? '목적지 이름을 집으로 저장하면 등록됩니다.' : undefined} className="secondary-button flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-50"><Home size={14} /> 집 (Tag 1 도킹)</button>
        {TABLE_APPROACH_DESTINATIONS.map(place => <button key={place.name} type="button" onClick={() => setPreset(place.name, place.x, place.y, (place as any).yaw)} className="secondary-button flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold">{place.name}</button>)}
        <button type="button" onClick={() => setPreset(CHARGING_DESTINATION.name, CHARGING_DESTINATION.x, CHARGING_DESTINATION.y, (CHARGING_DESTINATION as any).yaw, true, 1)} className="secondary-button flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold"><MapPin size={14} /> 충전 위치 (Tag 1 도킹)</button>
        {savedDestinations.find(place => ['충전위치', '충전도크', '충전소', '충전장소', '도킹스테이션', '충전기', 'dock', 'chargingstation'].includes(place.name.replace(/[\s_-]+/g, '').toLowerCase())) && (() => {
          const charging = savedDestinations.find(place => ['충전위치', '충전도크', '충전소', '충전장소', '도킹스테이션', '충전기', 'dock', 'chargingstation'].includes(place.name.replace(/[\s_-]+/g, '').toLowerCase()))!;
          return <button type="button" onClick={() => setPreset(charging.name, charging.x, charging.y, charging.yaw, true, charging.tag_id ?? 1)} className="secondary-button flex items-center gap-2 rounded-lg border px-3 py-2 text-xs font-semibold"><MapPin size={14} /> 충전 도크</button>;
        })()}
      </div>

      {savedDestinations.length > 0 && <div className="mt-3">
        <div className="mb-1.5 flex items-center justify-between text-[9px] font-bold tracking-widest text-slate-500">
          <span>저장된 장소</span>
          <span className="text-[10px] text-slate-400 font-normal">클릭하여 선택/수정 · ✕ 삭제</span>
        </div>
        <div className="flex flex-wrap gap-2">
          {savedDestinations.map(place => (
            <div key={place.name} className={`flex items-center rounded-lg border text-xs transition ${destination.name === place.name ? 'border-blue-400 bg-blue-50 text-blue-700 font-semibold' : 'border-slate-200 bg-slate-50 text-slate-700 hover:bg-slate-100'}`}>
              <button type="button" onClick={() => onSelectDestination(place)} className="px-2.5 py-1.5 text-left">
                {place.name} <span className="ml-1 font-mono opacity-60 text-[10px]">({place.x.toFixed(2)}, {place.y.toFixed(2)}{typeof place.yaw === 'number' ? `, ${place.yaw.toFixed(0)}°` : ''})</span>
              </button>
              {onDeleteDestination && (
                <button
                  type="button"
                  title={`${place.name} 삭제`}
                  onClick={(e) => { e.stopPropagation(); onDeleteDestination(place.name); }}
                  className="px-2 py-1.5 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-r-lg"
                >
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      </div>}

      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-[minmax(0,1.2fr)_85px_85px_95px_auto] sm:items-end">
        <label className="col-span-2 text-[10px] font-semibold text-slate-500 sm:col-span-1">목적지 이름
          <input value={destination.name} onChange={event => onDestinationChange({ ...destination, name: event.target.value })} className="speed-input mt-1 w-full rounded-lg border px-3 py-2 text-sm" placeholder="예: 집" />
        </label>
        <label className="text-[10px] font-semibold text-slate-500">X 좌표 (m)
          <input type="number" min={INDOOR_MAP.bounds.minX} max={INDOOR_MAP.bounds.maxX} step="0.01" value={Number.isFinite(destination.x) ? destination.x : ''} onChange={event => onDestinationChange({ ...destination, x: event.target.value === '' ? Number.NaN : Number(event.target.value) })} className="speed-input mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm" />
        </label>
        <label className="text-[10px] font-semibold text-slate-500">Y 좌표 (m)
          <input type="number" min={INDOOR_MAP.bounds.minY} max={INDOOR_MAP.bounds.maxY} step="0.01" value={Number.isFinite(destination.y) ? destination.y : ''} onChange={event => onDestinationChange({ ...destination, y: event.target.value === '' ? Number.NaN : Number(event.target.value) })} className="speed-input mt-1 w-full rounded-lg border px-3 py-2 font-mono text-sm" />
        </label>
        <label className="text-[10px] font-semibold text-slate-500">방향 (Yaw °)
          <div className="relative mt-1">
            <input type="number" min={-180} max={180} step="1" value={Number.isFinite(destination.yaw) ? destination.yaw : 0} onChange={event => onDestinationChange({ ...destination, yaw: event.target.value === '' ? 0 : Number(event.target.value) })} className="speed-input w-full rounded-lg border px-3 py-2 font-mono text-sm pr-6" />
            <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-slate-400 font-bold">°</span>
          </div>
        </label>
        <div className="col-span-2 flex gap-2 sm:col-span-1">
          <button type="button" onClick={onSaveDestination} disabled={!destinationNameValid || !destinationCoordinatesValid} className="secondary-button flex-1 rounded-lg border px-3 py-2.5 text-xs font-bold disabled:cursor-not-allowed disabled:opacity-50">저장</button>
          {status === 'navigating' || status === 'backing_up' || status === 'docking' ? (
            <button type="button" onClick={() => { onEnableDocking?.(false); onStop(); }} className="pilot-emergency flex flex-1 items-center justify-center gap-2 rounded-lg px-4 py-2.5 text-xs font-bold text-white"><Square size={13} /> {status === 'docking' ? '도킹 중지' : '주행 중지'}</button>
          ) : (
          <button type="button" onClick={onStart} disabled={movementUnavailableReason !== null} title={movementUnavailableReason ?? undefined} className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-blue-600 px-4 py-2.5 text-xs font-bold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"><Navigation size={14} /> 이동</button>
          )}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
        <span className="text-[10px] font-semibold text-slate-400 mr-1">방향 프리셋:</span>
        {[
          { label: '동(0°)', deg: 0 },
          { label: '북(90°)', deg: 90 },
          { label: '서(180°)', deg: 180 },
          { label: '남(-90°)', deg: -90 },
        ].map(p => (
          <button
            key={p.deg}
            type="button"
            onClick={() => onDestinationChange({ ...destination, yaw: p.deg })}
            className={`rounded px-2 py-0.5 text-[10px] font-bold border transition ${
              (destination.yaw ?? 0) === p.deg
                ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300'
                : 'border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400'
            }`}
          >
            {p.label}
          </button>
        ))}
        <button
          type="button"
          onClick={() => {
            const curYawDeg = Number(((currentPose.yaw * 180 / Math.PI + 180) % 360 - 180).toFixed(1));
            onDestinationChange({ ...destination, yaw: curYawDeg });
          }}
          className="rounded px-2 py-0.5 text-[10px] font-bold border border-purple-200 bg-purple-50 text-purple-700 hover:bg-purple-100 dark:border-purple-800 dark:bg-purple-950 dark:text-purple-300 transition"
          title="현재 로봇이 바라보는 각도를 이 목적지의 방향으로 복사합니다"
        >
          로봇 현재 방향 복사 ({((currentPose.yaw * 180 / Math.PI + 360) % 360).toFixed(0)}°)
        </button>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 border-t pt-2.5">
        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 cursor-pointer select-none text-xs font-semibold text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              checked={preBackupEnabled}
              onChange={(e) => onTogglePreBackup?.(e.target.checked)}
              className="h-4 w-4 rounded border-slate-300 text-purple-600 focus:ring-purple-500"
            />
            <span className="flex items-center gap-1.5">
              <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-bold text-purple-700 dark:bg-purple-950 dark:text-purple-300">안전</span>
              출발 전 후진
            </span>
          </label>

          <label className="flex items-center gap-2 cursor-pointer select-none text-xs font-semibold text-slate-700 dark:text-slate-300">
            <input
              type="checkbox"
              checked={Boolean(destination.auto_dock ?? (destination.name.includes('집') || destination.name.includes('충전') || destination.name.includes('홈')))}
              onChange={(e) => onDestinationChange({ ...destination, auto_dock: e.target.checked })}
              className="h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            />
            <span className="flex items-center gap-1.5">
              <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-bold text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">AprilTag</span>
              도착 시 정밀 도킹
            </span>
          </label>
        </div>

        <div className="flex items-center gap-1.5 text-xs">
          <span className="text-[10px] font-semibold text-slate-400">마커 ID:</span>
          <input
            type="number"
            min={0}
            max={586}
            placeholder="자동 (1)"
            value={destination.tag_id !== undefined && destination.tag_id !== null ? destination.tag_id : ''}
            onChange={(e) => onDestinationChange({
              ...destination,
              tag_id: e.target.value === '' ? null : Number(e.target.value),
            })}
            className="w-16 rounded border px-2 py-0.5 font-mono text-xs text-slate-700 dark:text-slate-300"
          />
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-[var(--ui-muted)]">
        <button type="button" onClick={onSaveCurrentPosition} disabled={!currentPosition || !destination.name.trim()} className="rounded-lg border px-3 py-2 font-semibold text-[var(--ui-fg)] disabled:cursor-not-allowed disabled:opacity-50">현재 로봇 위치 및 방향 저장</button>
        <span>{currentPosition ? destination.name.trim()
          ? `실제 위치 (${currentPosition.x.toFixed(2)}, ${currentPosition.y.toFixed(2)} m, ${((currentPose.yaw * 180 / Math.PI + 360) % 360).toFixed(0)}°)`
          : '현재 위치를 저장하려면 목적지 이름을 입력하세요.'
          : currentPositionUnavailableReason ?? '현재 위치 데이터 대기 중'}</span>
      </div>
      <div className="mt-3 text-[10px] text-[var(--ui-muted)]">{movementUnavailableReason ? `이동 버튼 비활성화: ${movementUnavailableReason}` : '센서 및 위치 정상 · 목적지 준비 완료'} · 현재 위치 ({currentPose.x.toFixed(2)}, {currentPose.y.toFixed(2)} m, {((currentPose.yaw * 180 / Math.PI + 360) % 360).toFixed(0)}°) · A* 경로 계획</div>
      
      {/* AprilTag Visual Precision Docking Live Monitor */}
      {(status === 'docking' || status === 'docked' || (dockingStatus && dockingStatus.state !== 'IDLE')) && (
        <div className={`mt-3 rounded-xl border p-3 text-xs transition-all ${
          status === 'docked' || dockingStatus?.state === 'DOCKED'
            ? 'border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200'
            : 'border-indigo-300 bg-indigo-50/70 text-indigo-950 dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-200'
        }`}>
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-indigo-200/60 pb-2 dark:border-indigo-800/60">
            <div className="flex items-center gap-2">
              <Crosshair size={16} className={status === 'docking' ? 'text-indigo-600 animate-spin' : 'text-emerald-600'} />
              <span className="font-bold tracking-wide">
                {status === 'docked' || dockingStatus?.state === 'DOCKED'
                  ? '🎉 AprilTag 도킹 완료 (완전 밀착 및 고정)'
                  : '🎯 AprilTag 비주얼 서보 정밀 도킹 시스템'}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono font-bold ${
                dockingStatus?.tag_detected ? 'bg-emerald-200 text-emerald-800' : 'bg-amber-200 text-amber-800'
              }`}>
                {dockingStatus?.tag_detected ? `Tag #${dockingStatus.tag_id ?? 1} 감지됨` : '마커 탐색 중'}
              </span>
              <span className="rounded-md bg-indigo-200 px-2 py-0.5 font-mono text-[10px] font-bold text-indigo-800 dark:bg-indigo-900 dark:text-indigo-300">
                {dockingStatus?.state ?? (status === 'docking' ? 'SEARCHING' : 'IDLE')}
              </span>
            </div>
          </div>

          <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-3 font-mono text-[11px]">
              <span>거리: <strong>{typeof dockingStatus?.dist_m === 'number' ? `${dockingStatus.dist_m.toFixed(2)}m` : '--'}</strong></span>
              <span>횡오차: <strong>{typeof dockingStatus?.lat_err_m === 'number' ? `${dockingStatus.lat_err_m.toFixed(3)}m` : '--'}</strong></span>
              <span>각도: <strong>{typeof dockingStatus?.yaw_deg === 'number' ? `${dockingStatus.yaw_deg.toFixed(1)}°` : '--'}</strong></span>
              {dockingStatus?.docking_time_s ? (
                <span className="text-slate-500">진행: {dockingStatus.docking_time_s.toFixed(1)}s</span>
              ) : null}
            </div>

            <div className="flex items-center gap-2">
              {status === 'docked' && onMockArmComplete ? (
                <button
                  type="button"
                  onClick={onMockArmComplete}
                  className="rounded-lg bg-emerald-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-emerald-700 shadow-sm animate-pulse"
                >
                  🤖 [테스트] 로봇암 수건개기 완료 신호
                </button>
              ) : isDockingActive ? (
                <button
                  type="button"
                  onClick={() => { onEnableDocking?.(false); onStop(); }}
                  className="rounded-lg bg-rose-600 px-2.5 py-1 text-[11px] font-bold text-white hover:bg-rose-700 shadow-sm"
                >
                  🛑 도킹 중지
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => onEnableDocking?.(true)}
                  className="rounded-lg border border-indigo-400 bg-white px-2.5 py-1 text-[11px] font-bold text-indigo-700 hover:bg-indigo-100 shadow-sm dark:bg-slate-900 dark:text-indigo-300"
                >
                  🎯 즉시 정밀 도킹
                </button>
              )}
            </div>
          </div>

          {dockingStatus?.motion_kr && (
            <div className="mt-2 rounded-lg bg-white/70 px-2.5 py-1 text-[11px] font-medium text-slate-800 dark:bg-slate-900/70 dark:text-slate-200">
              {dockingStatus.motion_kr}
            </div>
          )}
        </div>
      )}

      {/* 📷 AprilTag 비전 도킹 실시간 카메라 뷰 (Live HUD) */}
      <div className="mt-3 rounded-xl border border-slate-700 bg-slate-950 p-3 text-slate-100 shadow-md">
        <div className="flex items-center justify-between border-b border-slate-800 pb-2">
          <div className="flex items-center gap-2">
            <Camera size={16} className="text-sky-400" />
            <span className="text-xs font-bold tracking-wide text-sky-200">
              AprilTag 도킹 카메라 뷰
            </span>
            <span className="flex items-center gap-1.5 rounded-full bg-emerald-500/20 px-2 py-0.5 text-[10px] font-semibold text-emerald-300">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
              Live 8088
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setStreamKey(k => k + 1)}
              title="브라우저 버퍼 비우기 및 즉시 실시간 동기화"
              className="flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-[11px] font-semibold text-sky-300 hover:bg-slate-700 active:scale-95 transition"
            >
              <RefreshCw size={12} />
              지연 초기화
            </button>
            <button
              type="button"
              onClick={() => setShowCamera(!showCamera)}
              className="flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-[11px] font-semibold text-slate-300 hover:bg-slate-700 transition"
            >
              {showCamera ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
              {showCamera ? '화면 접기' : '화면 펼치기'}
            </button>
          </div>
        </div>

        {showCamera && (
          <div className="mt-2.5 space-y-2.5">
            {/* 실시간 비디오 뷰어 (초저지연 직접 스트림 우선 연결) */}
            <div className="relative aspect-[4/3] w-full max-w-[440px] mx-auto overflow-hidden rounded-lg border border-slate-700 bg-black shadow-inner">
              <img
                key={streamKey}
                src={`http://192.168.0.64:8088/stream.mjpg?t=${streamKey}`}
                alt="AprilTag Docking Camera Stream"
                className="h-full w-full object-contain"
                decoding="sync"
                loading="eager"
                onError={(e) => {
                  if (!e.currentTarget.src.includes('/api/docking_stream')) {
                    e.currentTarget.src = `/api/docking_stream?t=${Date.now()}`;
                  }
                }}
              />
              <div className="absolute bottom-1.5 inset-x-2 rounded bg-black/60 px-2 py-1 text-center font-mono text-[10px] text-slate-300 backdrop-blur-sm">
                중심 십자선 ✛ 에이프릴태그 중심 일치 시 도킹 진입
              </div>
            </div>

            {/* 실시간 텔레메트리 & 중심 편차 분석 */}
            <div className="grid grid-cols-2 gap-2 text-[11px] font-mono sm:grid-cols-4">
              <div className="rounded-lg bg-slate-900/90 border border-slate-800 p-2">
                <div className="text-[10px] text-slate-400">인식 모드</div>
                <div className="font-bold text-amber-300 truncate">{dockingStatus?.tag_mode || 'NONE'}</div>
              </div>
              <div className="rounded-lg bg-slate-900/90 border border-slate-800 p-2">
                <div className="text-[10px] text-slate-400">전방 거리</div>
                <div className="font-bold text-sky-300">
                  {typeof dockingStatus?.dist_m === 'number' ? `${(dockingStatus.dist_m * 100).toFixed(1)} cm` : '--'}
                </div>
              </div>
              <div className="rounded-lg bg-slate-900/90 border border-slate-800 p-2">
                <div className="text-[10px] text-slate-400">중심 편차 (횡오차)</div>
                <div className={`font-bold ${
                  typeof dockingStatus?.lat_err_m === 'number'
                    ? Math.abs(dockingStatus.lat_err_m) > 0.05
                      ? 'text-rose-400 animate-pulse'
                      : 'text-emerald-400'
                    : 'text-slate-300'
                }`}>
                  {typeof dockingStatus?.lat_err_m === 'number'
                    ? `${dockingStatus.lat_err_m > 0 ? '우측 +' : '좌측 '}${(dockingStatus.lat_err_m * 100).toFixed(1)} cm`
                    : '--'}
                </div>
              </div>
              <div className="rounded-lg bg-slate-900/90 border border-slate-800 p-2">
                <div className="text-[10px] text-slate-400">각도 편차</div>
                <div className="font-bold text-purple-300">
                  {typeof dockingStatus?.yaw_deg === 'number' ? `${dockingStatus.yaw_deg > 0 ? '+' : ''}${dockingStatus.yaw_deg.toFixed(1)}°` : '--'}
                </div>
              </div>
            </div>

            {typeof dockingStatus?.lat_err_m === 'number' && Math.abs(dockingStatus.lat_err_m) > 0.04 && (
              <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-300 flex items-center gap-1.5">
                <span>⚠️</span>
                <span>
                  <strong>도킹 중심 벗어남 감지:</strong> 횡방향 편차 {(Math.abs(dockingStatus.lat_err_m) * 100).toFixed(1)}cm 발생.
                  {dockingStatus.lat_err_m > 0 ? ' (로봇이 우측으로 쏠림)' : ' (로봇이 좌측으로 쏠림)'}
                </span>
              </div>
            )}

            {/* 🎯 단독 도킹 테스트 제어 버튼 */}
            <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-800">
              {status === 'docking' || (dockingStatus && dockingStatus.state !== 'IDLE' && dockingStatus.state !== 'DOCKED') ? (
                <button
                  type="button"
                  onClick={onStopDockingTest || (() => onEnableDocking?.(false))}
                  className="flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-[11px] font-bold text-white shadow hover:bg-rose-700 active:scale-95 transition"
                >
                  <Square size={13} fill="currentColor" />
                  🛑 도킹 정지
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onStartDockingTest || (() => onEnableDocking?.(true))}
                  className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3.5 py-1.5 text-[11px] font-bold text-white shadow hover:bg-indigo-700 active:scale-95 transition"
                >
                  <Crosshair size={13} />
                  🎯 [단독 테스트] AprilTag 정밀 도킹 시작
                </button>
              )}

              <button
                type="button"
                onClick={onUndockTest}
                className="flex items-center gap-1.5 rounded-lg border border-slate-600 bg-slate-800 px-3 py-1.5 text-[11px] font-semibold text-slate-200 hover:bg-slate-700 active:scale-95 transition"
              >
                🔙 40cm 후진 (재시험용)
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 🤖 로봇암(Robot Arm) 협업 테스트 토픽 제어 패널 */}
      <div className="mt-3 rounded-xl border border-indigo-200 bg-indigo-50/60 p-3 text-xs dark:border-indigo-900/60 dark:bg-indigo-950/20">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-indigo-200/60 pb-2 dark:border-indigo-800/60">
          <div className="flex items-center gap-2">
            <Radio size={15} className="text-indigo-600 dark:text-indigo-400" />
            <span className="font-bold tracking-wide text-indigo-950 dark:text-indigo-200">
              🤖 로봇암(Robot Arm) 연동 테스트 토픽
            </span>
          </div>
          <span className="rounded bg-indigo-100 px-2 py-0.5 text-[10px] font-mono font-semibold text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300">
            DDS #30
          </span>
        </div>

        <div className="mt-2.5 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => {
              onPublishDockingComplete?.();
              setLastTopicMessage(`발행: /amr/docking_complete (${new Date().toLocaleTimeString()})`);
            }}
            className="flex items-center gap-1.5 rounded-lg border border-emerald-500 bg-emerald-600 px-3 py-1.5 text-[11px] font-bold text-white shadow-sm hover:bg-emerald-700 active:scale-95 transition"
          >
            <Radio size={13} />
            📡 [테스트] AMR 도킹 완료 발행 (/amr/docking_complete)
          </button>

          <button
            type="button"
            onClick={() => {
              onMockArmComplete?.();
              setLastTopicMessage(`발행: /arm/task_complete [fold_towel] (${new Date().toLocaleTimeString()})`);
            }}
            className="flex items-center gap-1.5 rounded-lg border border-indigo-500 bg-indigo-600 px-3 py-1.5 text-[11px] font-bold text-white shadow-sm hover:bg-indigo-700 active:scale-95 transition"
          >
            <Zap size={13} />
            🦾 [테스트] 로봇암 작업 완료 발행 (/arm/task_complete)
          </button>
        </div>

        {/* 상태 표시줄 */}
        <div className="mt-2.5 rounded-lg bg-white/90 p-2 text-[11px] font-mono text-slate-700 border border-slate-200/60 dark:bg-slate-900/90 dark:border-slate-800 dark:text-slate-300">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>최근 토픽: <strong>{lastTopicMessage || '대기 중'}</strong></span>
            <span className={armTaskComplete ? 'text-emerald-600 font-bold dark:text-emerald-400' : 'text-slate-500'}>
              {armTaskComplete ? `✅ 로봇암 완료 수신 (${armTaskComplete.task || 'fold_towel'})` : '로봇암 작업 대기 중'}
            </span>
          </div>
        </div>
      </div>
      {status === 'backing_up' && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-purple-300 bg-purple-50 px-3 py-2 text-xs font-bold text-purple-900 animate-pulse dark:bg-purple-950/60 dark:border-purple-800 dark:text-purple-200">
          <span className="h-2 w-2 rounded-full bg-purple-600 animate-ping" />
          🔙 [안전 언도킹] 출발 전 안전 공간 확보를 위해 후진 중...
        </div>
      )}
      {status === 'navigating' && <div className="mt-2 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-800"><span className="font-bold">현재 목표</span> · {destination.name} · X {destination.x.toFixed(2)} m, Y {destination.y.toFixed(2)} m{typeof destination.yaw === 'number' ? `, 목표 방향 ${destination.yaw.toFixed(0)}°` : ''}</div>}
      {status === 'navigating' && plannedRoute.length > 0 && <div className="mt-1 text-[10px] font-semibold text-blue-700">장애물 회피 경로 · {plannedRoute.length}개 경유점</div>}
      {navigationNotice && <div role="status" className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">{navigationNotice}</div>}
    </section>
  );
}
