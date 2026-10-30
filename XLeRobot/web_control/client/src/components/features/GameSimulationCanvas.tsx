import { useEffect, useMemo, useRef, useState } from 'react';
import { Crosshair, MapPin, Navigation, RotateCcw, Wifi, WifiOff } from 'lucide-react';
import type { RobotSimulationState } from '../../types';
import { STATIC_OBSTACLES, type RoutePoint } from '../../utils/pathPlanner';
import { INDOOR_MAP } from '../../utils/indoorMap';
import type { Destination } from './DestinationNavigator';

interface Pose {
  x: number;
  y: number;
  yaw: number;
}

interface GameSimulationCanvasProps {
  simulationState: RobotSimulationState | null;
  pressedKeys: Set<string>;
  controlDirection: string | null;
  joystick: { x: number; y: number };
  speed: number;
  connected: boolean;
  resetToken: number;
  destination: Destination | null;
  savedDestinations: Destination[];
  plannedRoute: RoutePoint[];
  onReset: () => void;
  onPoseUpdate: (pose: Pose) => void;
  onDestinationChange?: (destination: Destination) => void;
  onSetRobotPose?: (pose: Pose) => void;
  onUpdateWaypointPosition?: (name: string, x: number, y: number, yaw?: number) => void;
  onUpdateWaypointYaw?: (name: string, yaw: number) => void;
}

const MOVEMENT_KEYS: Record<string, string> = {
  w: 'forward', W: 'forward', ArrowUp: 'forward',
  s: 'backward', S: 'backward', ArrowDown: 'backward',
  a: 'left', A: 'left', ArrowLeft: 'left',
  d: 'right', D: 'right', ArrowRight: 'right',
  q: 'rotate_left', Q: 'rotate_left',
  e: 'rotate_right', E: 'rotate_right',
};

function drawIndoorMap(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  pose: Pose,
  destination: Destination | null,
  route: RoutePoint[],
  scan: RoutePoint[],
  savedDestinations: Destination[],
  mapImage: HTMLImageElement | null,
  activeWaypoint: string | null = null,
  posePreview: { start: { x: number; y: number }; current: { x: number; y: number } } | null = null,
  waypointPreview: { start: { x: number; y: number }; current: { x: number; y: number } } | null = null,
  activeHeadingHandle: string | null = null,
) {
  const { minX, maxX, minY, maxY } = INDOOR_MAP.bounds;
  const margin = 24;
  const header = 48;
  const footer = 52;
  const scale = Math.max(1, Math.min((width - margin * 2) / (maxX - minX), (height - header - footer) / (maxY - minY)));
  const roomWidth = (maxX - minX) * scale;
  const roomHeight = (maxY - minY) * scale;
  const left = (width - roomWidth) / 2;
  const top = header + (height - header - footer - roomHeight) / 2;
  const point = (x: number, y: number) => ({ x: left + (x - minX) * scale, y: top + (maxY - y) * scale });

  // Dark slate background
  ctx.fillStyle = '#0f172a';
  ctx.fillRect(0, 0, width, height);

  if (mapImage && mapImage.complete && mapImage.naturalWidth > 0) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, roomWidth, roomHeight);
    ctx.clip();
    ctx.drawImage(mapImage, left, top, roomWidth, roomHeight);
    ctx.restore();

    // Map border
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(left, top, roomWidth, roomHeight);
  } else {
    // Grid fallback while loading image
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(left, top, roomWidth, roomHeight);
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, roomWidth, roomHeight);
    ctx.clip();
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.15)';
    ctx.lineWidth = 1;
    for (let x = Math.ceil(minX); x <= maxX; x += 1) {
      const a = point(x, minY); const b = point(x, maxY);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    for (let y = Math.ceil(minY); y <= maxY; y += 1) {
      const a = point(minX, y); const b = point(maxX, y);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    ctx.restore();
  }

  // MuJoCo simulation walls/tables (only if source is mujoco_static)
  if ((INDOOR_MAP as any).source === 'mujoco_static') {
    ctx.fillStyle = '#475569';
    const wall = Math.max(5, scale * 0.16);
    ctx.fillRect(left - wall, top - wall, roomWidth + wall * 2, wall);
    ctx.fillRect(left - wall, top + roomHeight, roomWidth + wall * 2, wall);
    ctx.fillRect(left - wall, top, wall, roomHeight);
    ctx.fillRect(left + roomWidth, top, wall, roomHeight);

    for (const table of STATIC_OBSTACLES) {
      const center = point(table.x, table.y);
      const w = table.halfWidth * 2 * scale;
      const h = table.halfHeight * 2 * scale;
      ctx.fillStyle = 'rgba(15, 23, 42, 0.14)';
      ctx.fillRect(center.x - w / 2 + 2, center.y - h / 2 + 3, w, h);
      ctx.fillStyle = '#b77945';
      ctx.strokeStyle = '#7c4a2d';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.roundRect(center.x - w / 2, center.y - h / 2, w, h, 5); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#fff7ed';
      ctx.font = 'bold 9px sans-serif';
      ctx.fillText(table.name, center.x, center.y + 3);
    }
  }

  // Fresh range returns (LiDAR)
  ctx.fillStyle = 'rgba(249, 115, 22, 0.85)';
  for (const obstacle of scan) {
    if (obstacle.x < minX || obstacle.x > maxX || obstacle.y < minY || obstacle.y > maxY) continue;
    const p = point(obstacle.x, obstacle.y);
    ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(1.5, scale * 0.035), 0, Math.PI * 2); ctx.fill();
  }

  // Planned Route
  if (route.length > 0) {
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = Math.max(2.5, scale * 0.06);
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    const start = point(pose.x, pose.y); ctx.moveTo(start.x, start.y);
    for (const waypoint of route) { const p = point(waypoint.x, waypoint.y); ctx.lineTo(p.x, p.y); }
    ctx.stroke(); ctx.setLineDash([]);
  }

  // Distinct colors for predefined waypoints
  const WAYPOINT_COLORS: Record<string, string> = {
    '홈 스테이션': '#4ade80',
    '책상': '#38bdf8',
    '소파': '#f59e0b',
  };

  for (const saved of savedDestinations) {
    if (!Number.isFinite(saved.x) || !Number.isFinite(saved.y)
      || saved.x < minX || saved.x > maxX || saved.y < minY || saved.y > maxY) continue;
    const marker = point(saved.x, saved.y);
    const color = WAYPOINT_COLORS[saved.name] || '#60a5fa';
    const isActive = saved.name === activeWaypoint;

    // Draw Waypoint Orientation Arrow (yaw in degrees)
    const yawDeg = typeof saved.yaw === 'number' && Number.isFinite(saved.yaw) ? saved.yaw : 0;
    const yawRad = (yawDeg * Math.PI) / 180;
    const arrowLen = isActive ? Math.max(16, scale * 0.35) : Math.max(12, scale * 0.26);
    const tipX = marker.x + arrowLen * Math.cos(yawRad);
    const tipY = marker.y - arrowLen * Math.sin(yawRad); // Map Y increases up, canvas Y increases down

    ctx.strokeStyle = isActive ? '#ffffff' : color;
    ctx.lineWidth = isActive ? 2.5 : 1.8;
    ctx.beginPath();
    ctx.moveTo(marker.x, marker.y);
    ctx.lineTo(tipX, tipY);
    ctx.stroke();

    const headLen = Math.max(5, scale * 0.1);
    const angle = Math.atan2(tipY - marker.y, tipX - marker.x);
    ctx.fillStyle = isActive ? '#ffffff' : color;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(tipX - headLen * Math.cos(angle - Math.PI / 6), tipY - headLen * Math.sin(angle - Math.PI / 6));
    ctx.lineTo(tipX - headLen * Math.cos(angle + Math.PI / 6), tipY - headLen * Math.sin(angle + Math.PI / 6));
    ctx.closePath();
    ctx.fill();

    // Heading rotation handle at arrow tip
    const isHandleActive = activeHeadingHandle === saved.name;
    const handleRadius = isHandleActive ? Math.max(7, scale * 0.15) : Math.max(5, scale * 0.11);
    ctx.fillStyle = isHandleActive ? '#38bdf8' : (isActive ? '#60a5fa' : 'rgba(255, 255, 255, 0.9)');
    ctx.beginPath();
    ctx.arc(tipX, tipY, handleRadius, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = isHandleActive ? '#ffffff' : color;
    ctx.lineWidth = isHandleActive ? 2 : 1.2;
    ctx.stroke();

    if (isHandleActive) {
      const badgeText = `방향 ${yawDeg.toFixed(0)}° ↻`;
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center';
      const tw = ctx.measureText(badgeText).width;
      ctx.fillStyle = 'rgba(15, 23, 42, 0.95)';
      ctx.fillRect(tipX - tw / 2 - 5, tipY - 24, tw + 10, 16);
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 1;
      ctx.strokeRect(tipX - tw / 2 - 5, tipY - 24, tw + 10, 16);
      ctx.fillStyle = '#38bdf8';
      ctx.fillText(badgeText, tipX, tipY - 12);
    }

    // Outer glow / Ring (larger if dragged/hovered)
    const ringRadius = isActive ? Math.max(10, scale * 0.18) : Math.max(6, scale * 0.12);
    ctx.fillStyle = isActive ? `${color}70` : `${color}40`;
    ctx.beginPath(); ctx.arc(marker.x, marker.y, ringRadius, 0, Math.PI * 2); ctx.fill();
    if (isActive) {
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // Inner dot
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(marker.x, marker.y, Math.max(3.5, scale * 0.07), 0, Math.PI * 2); ctx.fill();

    // Label with badge background
    ctx.font = isActive ? 'bold 10px sans-serif' : 'bold 9px sans-serif';
    ctx.textAlign = 'center';
    const labelText = isActive ? `${saved.name} (${saved.x.toFixed(2)}, ${saved.y.toFixed(2)}, ${yawDeg.toFixed(0)}°)` : `${saved.name} (${yawDeg.toFixed(0)}°)`;
    const textWidth = ctx.measureText(labelText).width;
    ctx.fillStyle = isActive ? 'rgba(15, 23, 42, 0.95)' : 'rgba(15, 23, 42, 0.8)';
    ctx.fillRect(marker.x - textWidth / 2 - 5, marker.y - 20, textWidth + 10, 15);
    if (isActive) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.strokeRect(marker.x - textWidth / 2 - 5, marker.y - 20, textWidth + 10, 15);
    }
    ctx.fillStyle = '#f8fafc';
    ctx.fillText(labelText, marker.x, marker.y - 9);
  }

  // Active Target Destination
  if (destination && Number.isFinite(destination.x) && Number.isFinite(destination.y)) {
    const target = point(destination.x, destination.y);
    ctx.strokeStyle = '#22c55e'; ctx.fillStyle = 'rgba(34, 197, 94, 0.25)'; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.arc(target.x, target.y, Math.max(8, scale * 0.22), 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    const destYawDeg = typeof destination.yaw === 'number' && Number.isFinite(destination.yaw) ? destination.yaw : null;
    if (destYawDeg !== null) {
      const yawRad = (destYawDeg * Math.PI) / 180;
      const arrowLen = Math.max(18, scale * 0.38);
      const tipX = target.x + arrowLen * Math.cos(yawRad);
      const tipY = target.y - arrowLen * Math.sin(yawRad);
      ctx.strokeStyle = '#22c55e';
      ctx.lineWidth = 2.5;
      ctx.beginPath();
      ctx.moveTo(target.x, target.y);
      ctx.lineTo(tipX, tipY);
      ctx.stroke();

      const headLen = Math.max(6, scale * 0.12);
      const angle = Math.atan2(tipY - target.y, tipX - target.x);
      ctx.fillStyle = '#22c55e';
      ctx.beginPath();
      ctx.moveTo(tipX, tipY);
      ctx.lineTo(tipX - headLen * Math.cos(angle - Math.PI / 6), tipY - headLen * Math.sin(angle - Math.PI / 6));
      ctx.lineTo(tipX - headLen * Math.cos(angle + Math.PI / 6), tipY - headLen * Math.sin(angle + Math.PI / 6));
      ctx.closePath();
      ctx.fill();

      // Heading rotation handle at target arrow tip
      const isTargetHandleActive = activeHeadingHandle === destination.name;
      const handleRadius = isTargetHandleActive ? Math.max(7, scale * 0.15) : Math.max(5, scale * 0.11);
      ctx.fillStyle = isTargetHandleActive ? '#4ade80' : 'rgba(74, 222, 128, 0.95)';
      ctx.beginPath();
      ctx.arc(tipX, tipY, handleRadius, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = isTargetHandleActive ? 2 : 1.5;
      ctx.stroke();

      if (isTargetHandleActive) {
        const badgeText = `방향 ${(destYawDeg ?? 0).toFixed(0)}° ↻`;
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        const tw = ctx.measureText(badgeText).width;
        ctx.fillStyle = 'rgba(15, 23, 42, 0.95)';
        ctx.fillRect(tipX - tw / 2 - 5, tipY - 24, tw + 10, 16);
        ctx.strokeStyle = '#22c55e';
        ctx.lineWidth = 1;
        ctx.strokeRect(tipX - tw / 2 - 5, tipY - 24, tw + 10, 16);
        ctx.fillStyle = '#4ade80';
        ctx.fillText(badgeText, tipX, tipY - 12);
      }
    }
    ctx.fillStyle = '#4ade80'; ctx.font = 'bold 10px sans-serif'; ctx.fillText(`★ ${destination.name}${destYawDeg !== null ? ` (${destYawDeg.toFixed(0)}°)` : ''}`, target.x, target.y - 14);
  }

  // Waypoint Drag Direction Preview
  if (waypointPreview) {
    const pStart = point(waypointPreview.start.x, waypointPreview.start.y);
    const pCurrent = point(waypointPreview.current.x, waypointPreview.current.y);
    const dx = waypointPreview.current.x - waypointPreview.start.x;
    const dy = waypointPreview.current.y - waypointPreview.start.y;
    const dist = Math.hypot(dx, dy);
    const angle = Math.atan2(dy, dx);
    const deg = Math.round((((angle * 180 / Math.PI + 180) % 360 + 360) % 360 - 180));

    // Base point ring (Blue/Cyan for waypoint)
    ctx.fillStyle = 'rgba(59, 130, 246, 0.4)';
    ctx.beginPath(); ctx.arc(pStart.x, pStart.y, 14, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#3b82f6';
    ctx.beginPath(); ctx.arc(pStart.x, pStart.y, 5, 0, Math.PI * 2); ctx.fill();

    if (dist >= 0.05) {
      // Arrow line
      ctx.strokeStyle = '#60a5fa';
      ctx.lineWidth = 3.5;
      ctx.beginPath();
      ctx.moveTo(pStart.x, pStart.y);
      ctx.lineTo(pCurrent.x, pCurrent.y);
      ctx.stroke();

      // Arrow head
      const headLength = 14;
      const canvasAngle = Math.atan2(pCurrent.y - pStart.y, pCurrent.x - pStart.x);
      ctx.fillStyle = '#60a5fa';
      ctx.beginPath();
      ctx.moveTo(pCurrent.x, pCurrent.y);
      ctx.lineTo(
        pCurrent.x - headLength * Math.cos(canvasAngle - Math.PI / 6),
        pCurrent.y - headLength * Math.sin(canvasAngle - Math.PI / 6),
      );
      ctx.lineTo(
        pCurrent.x - headLength * Math.cos(canvasAngle + Math.PI / 6),
        pCurrent.y - headLength * Math.sin(canvasAngle + Math.PI / 6),
      );
      ctx.closePath();
      ctx.fill();

      // Heading degree badge
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center';
      const badgeText = `웨이포인트 방향 ${deg}°`;
      const textWidth = ctx.measureText(badgeText).width;
      ctx.fillStyle = 'rgba(15, 23, 42, 0.95)';
      ctx.fillRect(pCurrent.x - textWidth / 2 - 6, pCurrent.y - 25, textWidth + 12, 16);
      ctx.strokeStyle = '#3b82f6';
      ctx.lineWidth = 1;
      ctx.strokeRect(pCurrent.x - textWidth / 2 - 6, pCurrent.y - 25, textWidth + 12, 16);
      ctx.fillStyle = '#93c5fd';
      ctx.fillText(badgeText, pCurrent.x, pCurrent.y - 13);
    }
  }

  // 2D Pose Estimate Arrow Preview (Drag Direction)
  if (posePreview) {
    const pStart = point(posePreview.start.x, posePreview.start.y);
    const pCurrent = point(posePreview.current.x, posePreview.current.y);
    const dx = posePreview.current.x - posePreview.start.x;
    const dy = posePreview.current.y - posePreview.start.y;
    const dist = Math.hypot(dx, dy);
    const angle = Math.atan2(dy, dx);
    const deg = Math.round(((angle * 180 / Math.PI + 360) % 360));

    // Base point ring
    ctx.fillStyle = 'rgba(16, 185, 129, 0.4)';
    ctx.beginPath(); ctx.arc(pStart.x, pStart.y, 14, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#10b981';
    ctx.beginPath(); ctx.arc(pStart.x, pStart.y, 5, 0, Math.PI * 2); ctx.fill();

    if (dist >= 0.05) {
      // Arrow line
      ctx.strokeStyle = '#34d399';
      ctx.lineWidth = 3.5;
      ctx.beginPath();
      ctx.moveTo(pStart.x, pStart.y);
      ctx.lineTo(pCurrent.x, pCurrent.y);
      ctx.stroke();

      // Arrow head
      const headLength = 14;
      const canvasAngle = Math.atan2(pCurrent.y - pStart.y, pCurrent.x - pStart.x);
      ctx.fillStyle = '#34d399';
      ctx.beginPath();
      ctx.moveTo(pCurrent.x, pCurrent.y);
      ctx.lineTo(
        pCurrent.x - headLength * Math.cos(canvasAngle - Math.PI / 6),
        pCurrent.y - headLength * Math.sin(canvasAngle - Math.PI / 6),
      );
      ctx.lineTo(
        pCurrent.x - headLength * Math.cos(canvasAngle + Math.PI / 6),
        pCurrent.y - headLength * Math.sin(canvasAngle + Math.PI / 6),
      );
      ctx.closePath();
      ctx.fill();

      // Heading degree badge
      ctx.font = 'bold 11px sans-serif';
      ctx.textAlign = 'center';
      const badgeText = `${deg}° 방향`;
      const textWidth = ctx.measureText(badgeText).width;
      ctx.fillStyle = 'rgba(15, 23, 42, 0.9)';
      ctx.fillRect(pCurrent.x - textWidth / 2 - 6, pCurrent.y - 25, textWidth + 12, 16);
      ctx.strokeStyle = '#10b981';
      ctx.lineWidth = 1;
      ctx.strokeRect(pCurrent.x - textWidth / 2 - 6, pCurrent.y - 25, textWidth + 12, 16);
      ctx.fillStyle = '#34d399';
      ctx.fillText(badgeText, pCurrent.x, pCurrent.y - 13);
    }
  }

  // Robot Pose Marker
  const robot = point(pose.x, pose.y);
  ctx.save();
  ctx.translate(robot.x, robot.y);
  ctx.rotate(-pose.yaw);
  ctx.fillStyle = '#06b6d4'; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(scale * 0.26, 0); ctx.lineTo(-scale * 0.15, -scale * 0.15);
  ctx.lineTo(-scale * 0.09, 0); ctx.lineTo(-scale * 0.15, scale * 0.15); ctx.closePath(); ctx.fill(); ctx.stroke();
  ctx.restore();
}

export function GameSimulationCanvas({
  simulationState,
  pressedKeys,
  controlDirection,
  joystick,
  speed,
  connected,
  resetToken,
  destination,
  savedDestinations,
  plannedRoute,
  onReset,
  onPoseUpdate,
  onDestinationChange,
  onSetRobotPose,
  onUpdateWaypointPosition,
  onUpdateWaypointYaw,
}: GameSimulationCanvasProps) {
  const initialHomePose = useMemo((): Pose => {
    try {
      const saved = localStorage.getItem('xlerobot.lastRobotPose');
      if (saved) {
        const p = JSON.parse(saved);
        if (Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.yaw)) {
          return { x: p.x, y: p.y, yaw: p.yaw };
        }
      }
    } catch {}
    const home = savedDestinations.find(d => (d as any).kind === 'charging' || d.name === '홈 스테이션' || d.name === '집')
      || { x: INDOOR_MAP.start.x, y: INDOOR_MAP.start.y, yaw: -15.5 };
    const homeYaw = (home as any).yaw ?? -15.5;
    return { x: home.x, y: home.y, yaw: (homeYaw * Math.PI) / 180 };
  }, [savedDestinations]);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const poseRef = useRef<Pose>(initialHomePose);
  const remotePoseRef = useRef<Pose | null>(null);
  const lastRemotePoseRef = useRef<Pose | null>(null);
  const remoteOffsetRef = useRef<Pose>({ x: 0, y: 0, yaw: 0 });
  const drivingRef = useRef(false);
  const inputsRef = useRef({ keys: pressedKeys, direction: controlDirection, joystick, speed, simulationState, destination, savedDestinations, plannedRoute });
  const fpsRef = useRef({ last: 0, value: 0 });
  const lastUiUpdateRef = useRef(0);
  const poseCallbackRef = useRef(onPoseUpdate);
  const [displayPose, setDisplayPose] = useState<Pose>(initialHomePose);
  const [fps, setFps] = useState(0);
  const [clickMode, setClickMode] = useState<'destination' | 'robot_pose'>('destination');
  const userRelocatedPoseRef = useRef<{ pose: Pose; time: number } | null>(null);
  const [mouseMapCoord, setMouseMapCoord] = useState<{ x: number; y: number } | null>(null);
  const [hoveredWaypoint, setHoveredWaypoint] = useState<string | null>(null);
  const [draggingWaypoint, setDraggingWaypoint] = useState<string | null>(null);
  const [hoveredHeadingHandle, setHoveredHeadingHandle] = useState<string | null>(null);
  const [draggingHeadingHandle, setDraggingHeadingHandle] = useState<string | null>(null);
  const [posePreview, setPosePreview] = useState<{ start: { x: number; y: number }; current: { x: number; y: number } } | null>(null);
  const [waypointPreview, setWaypointPreview] = useState<{ start: { x: number; y: number }; current: { x: number; y: number } } | null>(null);
  const isDraggingRef = useRef(false);
  const isSettingPoseRef = useRef(false);
  const isSettingWaypointRef = useRef(false);
  const poseStartCoordRef = useRef<{ x: number; y: number } | null>(null);
  const waypointStartCoordRef = useRef<{ x: number; y: number } | null>(null);
  const dragStartCoordRef = useRef<{ clientX: number; clientY: number } | null>(null);
  const draggingHeadingHandleRef = useRef<string | null>(null);
  const mapImageRef = useRef<HTMLImageElement | null>(null);
  const activeWaypointRef = useRef<string | null>(null);
  const posePreviewRef = useRef<{ start: { x: number; y: number }; current: { x: number; y: number } } | null>(null);
  const waypointPreviewRef = useRef<{ start: { x: number; y: number }; current: { x: number; y: number } } | null>(null);
  const activeHeadingHandleRef = useRef<string | null>(null);

  useEffect(() => {
    activeWaypointRef.current = draggingWaypoint || hoveredWaypoint;
  }, [draggingWaypoint, hoveredWaypoint]);

  useEffect(() => {
    activeHeadingHandleRef.current = draggingHeadingHandle || hoveredHeadingHandle;
  }, [draggingHeadingHandle, hoveredHeadingHandle]);

  useEffect(() => {
    posePreviewRef.current = posePreview;
  }, [posePreview]);

  useEffect(() => {
    waypointPreviewRef.current = waypointPreview;
  }, [waypointPreview]);

  const getMapCoordinates = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const clientX = e.clientX - rect.left;
    const clientY = e.clientY - rect.top;

    const scaleFactorX = canvas.width / rect.width;
    const scaleFactorY = canvas.height / rect.height;
    const canvasX = clientX * scaleFactorX;
    const canvasY = clientY * scaleFactorY;

    const { minX, maxX, minY, maxY } = INDOOR_MAP.bounds;
    const margin = 24;
    const header = 48;
    const footer = 52;
    const scale = Math.max(1, Math.min((canvas.width - margin * 2) / (maxX - minX), (canvas.height - header - footer) / (maxY - minY)));
    const roomWidth = (maxX - minX) * scale;
    const roomHeight = (maxY - minY) * scale;
    const left = (canvas.width - roomWidth) / 2;
    const top = header + (canvas.height - header - footer - roomHeight) / 2;

    const mapX = minX + (canvasX - left) / scale;
    const mapY = maxY - (canvasY - top) / scale;

    if (mapX < minX || mapX > maxX || mapY < minY || mapY > maxY) {
      return null;
    }
    return {
      x: Number(mapX.toFixed(2)),
      y: Number(mapY.toFixed(2)),
    };
  };

  const findWaypointNear = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const scaleFactorX = canvas.width / rect.width;
    const scaleFactorY = canvas.height / rect.height;
    const canvasX = (e.clientX - rect.left) * scaleFactorX;
    const canvasY = (e.clientY - rect.top) * scaleFactorY;

    const { minX, maxX, minY, maxY } = INDOOR_MAP.bounds;
    const margin = 24;
    const header = 48;
    const footer = 52;
    const scale = Math.max(1, Math.min((canvas.width - margin * 2) / (maxX - minX), (canvas.height - header - footer) / (maxY - minY)));
    const roomWidth = (maxX - minX) * scale;
    const roomHeight = (maxY - minY) * scale;
    const left = (canvas.width - roomWidth) / 2;
    const top = header + (canvas.height - header - footer - roomHeight) / 2;
    const point = (x: number, y: number) => ({ x: left + (x - minX) * scale, y: top + (maxY - y) * scale });

    for (const saved of savedDestinations) {
      const p = point(saved.x, saved.y);
      const dist = Math.hypot(canvasX - p.x, canvasY - p.y);
      if (dist <= Math.max(20, scale * 0.28)) {
        return saved;
      }
    }
    return null;
  };

  const findHeadingHandleNear = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const scaleFactorX = canvas.width / rect.width;
    const scaleFactorY = canvas.height / rect.height;
    const canvasX = (e.clientX - rect.left) * scaleFactorX;
    const canvasY = (e.clientY - rect.top) * scaleFactorY;

    const { minX, maxX, minY, maxY } = INDOOR_MAP.bounds;
    const margin = 24;
    const header = 48;
    const footer = 52;
    const scale = Math.max(1, Math.min((canvas.width - margin * 2) / (maxX - minX), (canvas.height - header - footer) / (maxY - minY)));
    const roomWidth = (maxX - minX) * scale;
    const roomHeight = (maxY - minY) * scale;
    const left = (canvas.width - roomWidth) / 2;
    const top = header + (canvas.height - header - footer - roomHeight) / 2;
    const point = (x: number, y: number) => ({ x: left + (x - minX) * scale, y: top + (maxY - y) * scale });

    for (const saved of savedDestinations) {
      const marker = point(saved.x, saved.y);
      const isActive = saved.name === destination?.name;
      const yawDeg = typeof saved.yaw === 'number' && Number.isFinite(saved.yaw) ? saved.yaw : 0;
      const yawRad = (yawDeg * Math.PI) / 180;
      const arrowLen = isActive ? Math.max(16, scale * 0.35) : Math.max(12, scale * 0.26);
      const tipX = marker.x + arrowLen * Math.cos(yawRad);
      const tipY = marker.y - arrowLen * Math.sin(yawRad);

      const dist = Math.hypot(canvasX - tipX, canvasY - tipY);
      if (dist <= 18) {
        return saved;
      }
    }

    if (destination && Number.isFinite(destination.x) && Number.isFinite(destination.y)) {
      const target = point(destination.x, destination.y);
      const destYawDeg = typeof destination.yaw === 'number' && Number.isFinite(destination.yaw) ? destination.yaw : 0;
      const yawRad = (destYawDeg * Math.PI) / 180;
      const arrowLen = Math.max(18, scale * 0.38);
      const tipX = target.x + arrowLen * Math.cos(yawRad);
      const tipY = target.y - arrowLen * Math.sin(yawRad);

      const dist = Math.hypot(canvasX - tipX, canvasY - tipY);
      if (dist <= 18) {
        return destination;
      }
    }

    return null;
  };

  const rotateRobotYaw = (degDelta: number) => {
    const cur = poseRef.current;
    const newYaw = cur.yaw + (degDelta * Math.PI) / 180;
    const newPose = { ...cur, yaw: newYaw };
    userRelocatedPoseRef.current = { pose: newPose, time: Date.now() };
    poseRef.current = newPose;
    if (lastRemotePoseRef.current) {
      remoteOffsetRef.current = {
        ...remoteOffsetRef.current,
        yaw: remoteOffsetRef.current.yaw + (degDelta * Math.PI) / 180,
      };
    }
    setDisplayPose(newPose);
    poseCallbackRef.current(newPose);
    onSetRobotPose?.(newPose);
  };

  const setExactRobotHeading = (targetDeg: number) => {
    const cur = poseRef.current;
    const newYaw = (targetDeg * Math.PI) / 180;
    const newPose = { ...cur, yaw: newYaw };
    userRelocatedPoseRef.current = { pose: newPose, time: Date.now() };
    poseRef.current = newPose;
    if (lastRemotePoseRef.current) {
      remoteOffsetRef.current = {
        ...remoteOffsetRef.current,
        yaw: newYaw - lastRemotePoseRef.current.yaw,
      };
    }
    setDisplayPose(newPose);
    poseCallbackRef.current(newPose);
    onSetRobotPose?.(newPose);
  };

  const handleMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const coord = getMapCoordinates(e);
    if (!coord) return;

    if (clickMode === 'robot_pose') {
      isSettingPoseRef.current = true;
      poseStartCoordRef.current = coord;
      setPosePreview({ start: coord, current: coord });
      return;
    }

    // 1. Waypoint heading rotation handle drag
    const headingWp = findHeadingHandleNear(e);
    if (headingWp) {
      draggingHeadingHandleRef.current = headingWp.name;
      setDraggingHeadingHandle(headingWp.name);
      onDestinationChange?.(headingWp);
      return;
    }

    // 2. Waypoint position move drag
    const near = findWaypointNear(e);
    if (near) {
      setDraggingWaypoint(near.name);
      dragStartCoordRef.current = { clientX: e.clientX, clientY: e.clientY };
      isDraggingRef.current = false;
      return;
    }

    // 3. Click & drag to place/orient waypoint (RViz-style)
    isSettingWaypointRef.current = true;
    waypointStartCoordRef.current = coord;
    dragStartCoordRef.current = { clientX: e.clientX, clientY: e.clientY };
    setWaypointPreview({ start: coord, current: coord });
  };

  const handleMouseMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const coord = getMapCoordinates(e);
    setMouseMapCoord(coord);

    if (isSettingPoseRef.current && poseStartCoordRef.current && coord) {
      setPosePreview({ start: poseStartCoordRef.current, current: coord });
      return;
    }

    if (draggingHeadingHandleRef.current && coord) {
      const wpName = draggingHeadingHandleRef.current;
      const targetWp = savedDestinations.find(s => s.name === wpName)
        ?? (destination?.name === wpName ? destination : null);
      if (targetWp) {
        const dx = coord.x - targetWp.x;
        const dy = coord.y - targetWp.y;
        if (Math.hypot(dx, dy) >= 0.04) {
          const yawRad = Math.atan2(dy, dx);
          const yawDeg = Number((((yawRad * 180 / Math.PI + 180) % 360 + 360) % 360 - 180).toFixed(1));
          onUpdateWaypointYaw?.(wpName, yawDeg);
          if (destination?.name === wpName) {
            onDestinationChange?.({ ...destination, yaw: yawDeg });
          }
        }
      }
      return;
    }

    if (draggingWaypoint && coord) {
      if (dragStartCoordRef.current && Math.hypot(e.clientX - dragStartCoordRef.current.clientX, e.clientY - dragStartCoordRef.current.clientY) > 4) {
        isDraggingRef.current = true;
      }
      const existingWp = savedDestinations.find(s => s.name === draggingWaypoint);
      onUpdateWaypointPosition?.(draggingWaypoint, coord.x, coord.y, existingWp?.yaw);
      return;
    }

    if (isSettingWaypointRef.current && waypointStartCoordRef.current && coord) {
      setWaypointPreview({ start: waypointStartCoordRef.current, current: coord });
      return;
    }

    if (clickMode === 'destination') {
      const headingWp = findHeadingHandleNear(e);
      setHoveredHeadingHandle(headingWp ? headingWp.name : null);
      if (!headingWp) {
        const near = findWaypointNear(e);
        setHoveredWaypoint(near ? near.name : null);
      } else {
        setHoveredWaypoint(null);
      }
    } else {
      setHoveredHeadingHandle(null);
      setHoveredWaypoint(null);
    }
  };

  const handleMouseUp = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (isSettingPoseRef.current && poseStartCoordRef.current) {
      const start = poseStartCoordRef.current;
      const cur = getMapCoordinates(e) || start;
      const dx = cur.x - start.x;
      const dy = cur.y - start.y;
      let finalYaw = poseRef.current.yaw;
      if (Math.hypot(dx, dy) >= 0.08) {
        finalYaw = Math.atan2(dy, dx);
      }
      const newPose = { x: start.x, y: start.y, yaw: finalYaw };
      userRelocatedPoseRef.current = { pose: newPose, time: Date.now() };
      poseRef.current = newPose;
      if (lastRemotePoseRef.current) {
        remoteOffsetRef.current = {
          x: newPose.x - lastRemotePoseRef.current.x,
          y: newPose.y - lastRemotePoseRef.current.y,
          yaw: newPose.yaw - lastRemotePoseRef.current.yaw,
        };
      } else {
        remoteOffsetRef.current = { x: 0, y: 0, yaw: 0 };
      }
      setDisplayPose(newPose);
      poseCallbackRef.current(newPose);
      onSetRobotPose?.(newPose);

      isSettingPoseRef.current = false;
      poseStartCoordRef.current = null;
      setPosePreview(null);
      return;
    }

    if (draggingHeadingHandleRef.current) {
      draggingHeadingHandleRef.current = null;
      setDraggingHeadingHandle(null);
      return;
    }

    if (isSettingWaypointRef.current && waypointStartCoordRef.current) {
      const start = waypointStartCoordRef.current;
      const cur = getMapCoordinates(e) || start;
      const dx = cur.x - start.x;
      const dy = cur.y - start.y;
      const dist = Math.hypot(dx, dy);

      isSettingWaypointRef.current = false;
      waypointStartCoordRef.current = null;
      setWaypointPreview(null);

      if (dist >= 0.08) {
        isDraggingRef.current = true;
        const yawRad = Math.atan2(dy, dx);
        const yawDeg = Number((((yawRad * 180 / Math.PI + 180) % 360 + 360) % 360 - 180).toFixed(1));

        const currentSelected = destination?.name
          ? savedDestinations.find(s => s.name === destination.name)
          : null;

        if (currentSelected && onUpdateWaypointPosition) {
          onUpdateWaypointPosition(currentSelected.name, start.x, start.y, yawDeg);
          onUpdateWaypointYaw?.(currentSelected.name, yawDeg);
          onDestinationChange?.({ ...currentSelected, x: start.x, y: start.y, yaw: yawDeg });
        } else {
          onDestinationChange?.({
            name: `지점 (${start.x.toFixed(2)}, ${start.y.toFixed(2)})`,
            x: start.x,
            y: start.y,
            yaw: yawDeg,
          });
        }
        return;
      }
    }

    setDraggingWaypoint(null);
    dragStartCoordRef.current = null;
  };

  const handleCanvasClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (clickMode === 'robot_pose') {
      return; // handled in handleMouseUp
    }

    if (isDraggingRef.current) {
      isDraggingRef.current = false;
      return;
    }

    const coord = getMapCoordinates(e);
    if (!coord) return;

    const clickedWp = findWaypointNear(e);
    if (clickedWp) {
      onDestinationChange?.(clickedWp);
      return;
    }

    // If an existing saved waypoint is currently selected, update its position directly!
    const currentSelected = destination?.name
      ? savedDestinations.find(s => s.name === destination.name)
      : null;

    if (currentSelected && onUpdateWaypointPosition) {
      onUpdateWaypointPosition(currentSelected.name, coord.x, coord.y, currentSelected.yaw);
    } else {
      const curRobotYawDeg = Number(((poseRef.current.yaw * 180 / Math.PI + 180) % 360 - 180).toFixed(1));
      onDestinationChange?.({
        name: `지점 (${coord.x.toFixed(2)}, ${coord.y.toFixed(2)})`,
        x: coord.x,
        y: coord.y,
        yaw: curRobotYawDeg,
      });
    }
  };

  useEffect(() => {
    const img = new Image();
    img.src = '/my_map.png';
    img.onload = () => {
      mapImageRef.current = img;
    };
  }, []);

  useEffect(() => {
    inputsRef.current = { keys: pressedKeys, direction: controlDirection, joystick, speed, simulationState, destination, savedDestinations, plannedRoute };
  }, [pressedKeys, controlDirection, joystick, speed, simulationState, destination, savedDestinations, plannedRoute]);

  useEffect(() => {
    poseCallbackRef.current = onPoseUpdate;
  }, [onPoseUpdate]);

  useEffect(() => {
    const isDriving = pressedKeys.size > 0 || controlDirection !== null || Math.hypot(joystick.x, joystick.y) >= 0.1;
    if (isDriving) {
      drivingRef.current = true;
      remotePoseRef.current = null;
      return;
    }

    if (drivingRef.current) {
      if (simulationState?.host_connected === true) {
        remoteOffsetRef.current = { x: 0, y: 0, yaw: 0 };
      } else {
        const lastRemote = lastRemotePoseRef.current;
        if (lastRemote) {
          remoteOffsetRef.current = {
            x: poseRef.current.x - lastRemote.x,
            y: poseRef.current.y - lastRemote.y,
            yaw: poseRef.current.yaw - lastRemote.yaw,
          };
        }
      }
      drivingRef.current = false;
    }

    const lastRemote = lastRemotePoseRef.current;
    if (lastRemote) {
      const isRealHost = simulationState?.host_connected === true;
      remotePoseRef.current = {
        x: lastRemote.x + (isRealHost ? 0 : remoteOffsetRef.current.x),
        y: lastRemote.y + (isRealHost ? 0 : remoteOffsetRef.current.y),
        yaw: lastRemote.yaw + (isRealHost ? 0 : remoteOffsetRef.current.yaw),
      };
    }
  }, [pressedKeys, controlDirection, joystick, simulationState]);

  useEffect(() => {
    if (simulationState?.host_connected !== true) return;
    const position = simulationState?.position;
    const yaw = simulationState?.rotation?.yaw;
    if (position && Number.isFinite(position.x) && Number.isFinite(position.y)) {
      lastRemotePoseRef.current = {
        x: position.x ?? poseRef.current.x,
        y: position.y ?? poseRef.current.y,
        yaw: Number.isFinite(yaw) ? (yaw as number) : poseRef.current.yaw,
      };
      if (!drivingRef.current) {
        remotePoseRef.current = {
          x: lastRemotePoseRef.current.x,
          y: lastRemotePoseRef.current.y,
          yaw: lastRemotePoseRef.current.yaw,
        };
      }
    }
  }, [simulationState]);

  useEffect(() => {
    poseRef.current = initialHomePose;
    remotePoseRef.current = initialHomePose;
    setDisplayPose(initialHomePose);
    lastRemotePoseRef.current = null;
    remoteOffsetRef.current = { x: 0, y: 0, yaw: 0 };
    drivingRef.current = false;
    poseCallbackRef.current(initialHomePose);
  }, [resetToken, initialHomePose]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    let animationFrame = 0;
    let previousTime = 0;

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();

    const draw = (time: number) => {
      const rect = canvas.getBoundingClientRect();
      const width = rect.width;
      const height = rect.height;
      const delta = previousTime ? Math.min((time - previousTime) / 1000, 0.05) : 0;
      previousTime = time;
      const input = inputsRef.current;
      const directions = new Set<string>();
      input.keys.forEach(key => {
        const direction = MOVEMENT_KEYS[key];
        if (direction) directions.add(direction);
      });
      if (input.direction) directions.add(input.direction);
      const keyForward = (directions.has('forward') ? 1 : 0) - (directions.has('backward') ? 1 : 0);
      const keyStrafe = (directions.has('right') ? 1 : 0) - (directions.has('left') ? 1 : 0);
      const stickMagnitude = Math.hypot(input.joystick.x, input.joystick.y);
      const forward = stickMagnitude >= 0.1 ? -input.joystick.y : keyForward;
      const keyTurn = (directions.has('rotate_left') ? 1 : 0) - (directions.has('rotate_right') ? 1 : 0);
      const stickTurn = stickMagnitude >= 0.1 ? -input.joystick.x : 0;
      const turn = stickMagnitude >= 0.1 && Math.abs(input.joystick.x) >= 0.1 ? stickTurn : keyTurn;
      const strafe = stickMagnitude >= 0.1 ? 0 : keyStrafe;
      const current = poseRef.current;
      const isDriving = directions.size > 0 || stickMagnitude >= 0.1;
      const remotePose = remotePoseRef.current;
      const livePosition = input.simulationState?.position;
      const liveYaw = input.simulationState?.rotation?.yaw;
      const hasLivePose = input.simulationState?.host_connected === true
        && livePosition
        && Number.isFinite(livePosition.x)
        && Number.isFinite(livePosition.y)
        && Number.isFinite(liveYaw);
      if (hasLivePose) {
        // MuJoCo/ROS is authoritative while connected.
        const userReloc = userRelocatedPoseRef.current;
        const isRecentUserReloc = userReloc && (Date.now() - userReloc.time < 1200);
        if (isRecentUserReloc) {
          const distToUser = Math.hypot(
            (livePosition.x as number) - userReloc.pose.x,
            (livePosition.y as number) - userReloc.pose.y
          );
          if (distToUser > 0.35) {
            // Keep user's placed pose until server converges
            current.x = userReloc.pose.x;
            current.y = userReloc.pose.y;
            current.yaw = userReloc.pose.yaw;
          } else {
            userRelocatedPoseRef.current = null;
            current.x = livePosition.x as number;
            current.y = livePosition.y as number;
            current.yaw = liveYaw as number;
          }
        } else {
          current.x = livePosition.x as number;
          current.y = livePosition.y as number;
          current.yaw = liveYaw as number;
        }
        remotePoseRef.current = null;
        drivingRef.current = false;
      } else {
        if (isDriving) {
          remotePoseRef.current = null;
        } else if (remotePose) {
          const blend = 1 - Math.exp(-8 * delta);
          current.x += (remotePose.x - current.x) * blend;
          current.y += (remotePose.y - current.y) * blend;
          const yawDifference = Math.atan2(Math.sin(remotePose.yaw - current.yaw), Math.cos(remotePose.yaw - current.yaw));
          current.yaw += yawDifference * blend;
          if (Math.abs(remotePose.x - current.x) + Math.abs(remotePose.y - current.y) + Math.abs(yawDifference) < 0.005) {
            current.x = remotePose.x;
            current.y = remotePose.y;
            current.yaw = remotePose.yaw;
            remotePoseRef.current = null;
          }
        }
        const speedUnits = 1.25 * input.speed;
        // Match the MuJoCo chassis frame: +yaw turns left and slide axes remain fixed.
        const yawCosine = Math.cos(current.yaw);
        const yawSine = Math.sin(current.yaw);
        current.x += (forward * yawCosine + strafe * yawSine) * speedUnits * delta;
        current.y += (forward * yawSine - strafe * yawCosine) * speedUnits * delta;
        current.yaw += turn * 1.4 * delta;
      }

      ctx.clearRect(0, 0, width, height);
      const status = input.simulationState?.obstacle_scan_status;
      const scan = status?.healthy && Number.isFinite(status.age_ms) && (status.age_ms as number) <= 350
        ? input.simulationState?.obstacle_scan ?? []
        : [];
      drawIndoorMap(ctx, width, height, current, input.destination, input.plannedRoute,
        scan.map(({ x, y }) => ({ x, y })), input.savedDestinations, mapImageRef.current, activeWaypointRef.current, posePreviewRef.current, waypointPreviewRef.current, activeHeadingHandleRef.current);
      fpsRef.current.value = previousTime ? Math.round(1000 / Math.max(1, time - fpsRef.current.last)) : 0;
      fpsRef.current.last = time;
      if (time - lastUiUpdateRef.current > 200) {
        lastUiUpdateRef.current = time;
        setDisplayPose({ ...current });
        setFps(fpsRef.current.value);
        poseCallbackRef.current({ ...current });
      }
      animationFrame = window.requestAnimationFrame(draw);
    };

    animationFrame = window.requestAnimationFrame(draw);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      observer.disconnect();
    };
  }, []);

  const pose = displayPose;
  const sourceLabel = simulationState ? 'MUJOCO SYNC' : connected ? 'SIM SERVER' : 'LOCAL SIM';
  const sourceColor = simulationState ? 'text-emerald-300' : connected ? 'text-sky-300' : 'text-amber-300';
  const scanStatus = simulationState?.obstacle_scan_status;
  const scanFresh = scanStatus?.healthy === true
    && Number.isFinite(scanStatus.age_ms) && (scanStatus.age_ms as number) <= 350;

  const cursorClass = clickMode === 'robot_pose'
    ? 'cursor-crosshair'
    : draggingHeadingHandle
      ? 'cursor-ew-resize'
      : hoveredHeadingHandle
        ? 'cursor-pointer'
        : draggingWaypoint
          ? 'cursor-grabbing'
          : hoveredWaypoint
            ? 'cursor-grab'
            : 'cursor-crosshair';

  return (
    <div className="game-simulation-viewport relative h-[min(43svh,400px)] min-h-[250px] overflow-hidden rounded-2xl border border-slate-600/70 bg-slate-950 shadow-[0_24px_70px_-34px_rgba(0,0,0,0.9)] sm:h-[420px] sm:min-h-[420px] lg:h-[calc(100vh-250px)] lg:max-h-[760px]">
      <canvas
        ref={canvasRef}
        onClick={handleCanvasClick}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => {
          setMouseMapCoord(null);
          setDraggingWaypoint(null);
          setHoveredWaypoint(null);
          setDraggingHeadingHandle(null);
          draggingHeadingHandleRef.current = null;
          setHoveredHeadingHandle(null);
          if (isSettingWaypointRef.current) {
            isSettingWaypointRef.current = false;
            waypointStartCoordRef.current = null;
            setWaypointPreview(null);
          }
        }}
        className={`absolute inset-0 h-full w-full ${cursorClass}`}
        aria-label="XLeRobot game-style simulation viewport"
      />
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(180deg,rgba(3,9,16,0.55),transparent_24%,transparent_69%,rgba(3,9,16,0.55))]" />

      {/* Mode Switcher */}
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex items-center gap-1.5 rounded-xl border border-white/20 bg-slate-950/85 p-1 backdrop-blur-md shadow-xl sm:top-4">
        <button
          type="button"
          onClick={() => setClickMode('destination')}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-bold transition sm:px-3 sm:py-1.5 ${
            clickMode === 'destination'
              ? 'bg-blue-600 text-white shadow-md'
              : 'text-slate-400 hover:bg-white/10 hover:text-white'
          }`}
        >
          <Navigation size={13} />
          <span>목적지/웨이포인트 지정</span>
          <span className="hidden text-[10px] font-normal opacity-75 sm:inline">· 클릭 후 드래그로 방향 설정</span>
        </button>
        <button
          type="button"
          onClick={() => setClickMode('robot_pose')}
          className={`flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs font-bold transition sm:px-3 sm:py-1.5 ${
            clickMode === 'robot_pose'
              ? 'bg-emerald-600 text-white shadow-md ring-2 ring-emerald-400/40'
              : 'text-slate-400 hover:bg-white/10 hover:text-white'
          }`}
        >
          <MapPin size={13} />
          <span>로봇 위치·방향 재배치 (2D Pose)</span>
          <span className="hidden text-[10px] font-normal opacity-75 sm:inline">· 클릭 후 드래그</span>
        </button>
      </div>

      <div className="absolute left-4 top-14 flex flex-wrap items-center gap-2 sm:left-6 sm:top-6 sm:max-w-[calc(50%-180px)]">
        <div className="rounded-lg border border-cyan-200/20 bg-slate-950/75 px-3 py-2 backdrop-blur-md">
          <div className="text-[10px] font-bold tracking-[0.24em] text-cyan-200/70">AMR // SLAM MAP</div>
          <div className="mt-1 flex items-center gap-2 text-sm font-semibold text-white">
            <span className={`h-2 w-2 rounded-full ${simulationState ? 'bg-emerald-400' : connected ? 'bg-sky-400' : 'bg-amber-400'} shadow-[0_0_12px_currentColor]`} />
            {sourceLabel}
          </div>
        </div>
        <div className="rounded-lg border border-white/10 bg-slate-950/65 px-3 py-2 text-xs text-slate-200 backdrop-blur-md">
          <span className="text-slate-500">MAP</span> <span className="ml-1 font-mono">MY_MAP</span>
        </div>
        {simulationState && (
          <div title={`${scanStatus?.beam_count ?? 0} beams × ${scanStatus?.layer_heights_m?.length ?? 0} heights · ${scanStatus?.range_m ?? 0} m max · ${scanStatus?.scan_duration_ms ?? 0} ms scan · ${scanStatus?.error ?? ''}`} className={`rounded-lg border px-3 py-2 text-xs backdrop-blur-md ${scanFresh ? 'border-emerald-300/20 bg-slate-950/70 text-emerald-100' : 'border-amber-300/20 bg-slate-950/70 text-amber-100'}`}>
            <span className={`mr-1.5 inline-block h-2 w-2 rounded-full ${scanFresh ? 'bg-emerald-400' : 'bg-amber-400'}`} />
            {scanFresh
          ? `RANGE ${scanStatus?.hit_count ?? simulationState.obstacle_scan?.length ?? 0} · ${scanStatus?.range_m ?? 0}m · ${scanStatus?.age_ms ?? 0}ms old`
              : `RANGE ${scanStatus?.status?.toUpperCase() ?? 'STALE'}`}
          </div>
        )}
      </div>

      <div className="absolute right-4 top-4 flex items-start gap-2 sm:right-6 sm:top-6">
        <div className="rounded-lg border border-white/10 bg-slate-950/70 px-3 py-2 text-right backdrop-blur-md">
          <div className={`flex items-center justify-end gap-1.5 text-[10px] font-bold tracking-[0.18em] ${sourceColor}`}>
            {connected ? <Wifi size={12} /> : <WifiOff size={12} />}{sourceLabel}
          </div>
          <div className="mt-1 font-mono text-[11px] text-slate-300">{pose.x.toFixed(2)}, {pose.y.toFixed(2)} m</div>
          <div className="mt-0.5 font-mono text-[9px] text-slate-500">{fps} FPS · SIM VIEW</div>
        </div>
        <button onClick={onReset} title="Reset simulation" className="pointer-events-auto rounded-lg border border-white/10 bg-slate-950/75 p-2.5 text-slate-200 transition hover:border-cyan-300/60 hover:text-cyan-200">
          <RotateCcw size={16} />
        </button>
      </div>

      <div className="absolute bottom-4 left-4 right-4 flex items-end justify-between gap-3 sm:bottom-6 sm:left-6 sm:right-6">
        <div className="flex flex-wrap items-center gap-2">
          <div className="rounded-lg border border-white/10 bg-slate-950/75 px-3 py-2 backdrop-blur-md">
            <div className="flex items-center justify-between gap-3 text-[10px] font-bold tracking-[0.2em] text-emerald-300">
              <span className="flex items-center gap-1.5"><MapPin size={12} /> 로봇 위치·방향</span>
              <button
                type="button"
                onClick={() => {
                  const home = savedDestinations.find(d => (d as any).kind === 'charging' || d.name === '홈 스테이션' || d.name === '집') || INDOOR_MAP.start;
                  const homeYaw = (home as any).yaw ?? -15.5;
                  const newPose = { x: home.x, y: home.y, yaw: (homeYaw * Math.PI) / 180 };
                  userRelocatedPoseRef.current = { pose: newPose, time: Date.now() };
                  poseRef.current = newPose;
                  setDisplayPose(newPose);
                  poseCallbackRef.current(newPose);
                  onSetRobotPose?.(newPose);
                }}
                title="홈 스테이션 위치(-1.41, -1.39) 및 방향(-15.5°)으로 즉시 정렬"
                className="rounded bg-emerald-500/20 px-2 py-0.5 text-[9px] font-bold text-emerald-300 transition hover:bg-emerald-500/35 hover:text-white"
              >
                🏠 홈 위치로 정렬
              </button>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-xs text-slate-300">
              <span>HEADING {((pose.yaw * 180 / Math.PI + 360) % 360).toFixed(0)}°</span>
              <div className="flex items-center gap-1 border-l border-white/15 pl-2">
                <button
                  type="button"
                  onClick={() => rotateRobotYaw(-45)}
                  title="왼쪽으로 45도 회전"
                  className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] font-bold text-slate-200 transition hover:bg-white/20 hover:text-white"
                >
                  ↺ -45°
                </button>
                <button
                  type="button"
                  onClick={() => rotateRobotYaw(45)}
                  title="오른쪽으로 45도 회전"
                  className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] font-bold text-slate-200 transition hover:bg-white/20 hover:text-white"
                >
                  ↻ +45°
                </button>
                <button
                  type="button"
                  onClick={() => setExactRobotHeading(0)}
                  title="0° 정렬 (동쪽/+X)"
                  className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 transition hover:bg-white/15 hover:text-slate-200"
                >
                  0°
                </button>
                <button
                  type="button"
                  onClick={() => setExactRobotHeading(90)}
                  title="90° 정렬 (북쪽/+Y)"
                  className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 transition hover:bg-white/15 hover:text-slate-200"
                >
                  90°
                </button>
                <button
                  type="button"
                  onClick={() => setExactRobotHeading(180)}
                  title="180° 정렬 (서쪽/-X)"
                  className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 transition hover:bg-white/15 hover:text-slate-200"
                >
                  180°
                </button>
                <button
                  type="button"
                  onClick={() => setExactRobotHeading(270)}
                  title="270° 정렬 (남쪽/-Y)"
                  className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 transition hover:bg-white/15 hover:text-slate-200"
                >
                  270°
                </button>
              </div>
            </div>
          </div>
          {destination && (
            <div className="rounded-lg border border-blue-400/30 bg-slate-950/80 px-3 py-2 backdrop-blur-md">
              <div className="flex items-center gap-2 text-[10px] font-bold tracking-[0.2em] text-blue-300">
                <Navigation size={12} /> {destination.name.length > 10 ? `${destination.name.slice(0, 10)}...` : destination.name} 방향
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-xs text-slate-300">
                <span className="font-bold text-blue-300">{(destination.yaw ?? 0).toFixed(0)}°</span>
                <div className="flex items-center gap-1 border-l border-white/15 pl-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      const cur = destination.yaw ?? 0;
                      const next = ((cur - 45 + 180) % 360 + 360) % 360 - 180;
                      onDestinationChange?.({ ...destination, yaw: next });
                      onUpdateWaypointYaw?.(destination.name, next);
                    }}
                    title="웨이포인트 방향 왼쪽 45도 회전"
                    className="rounded bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-bold text-blue-200 transition hover:bg-blue-500/40"
                  >
                    ↺ -45°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const cur = destination.yaw ?? 0;
                      const next = ((cur + 45 + 180) % 360 + 360) % 360 - 180;
                      onDestinationChange?.({ ...destination, yaw: next });
                      onUpdateWaypointYaw?.(destination.name, next);
                    }}
                    title="웨이포인트 방향 오른쪽 45도 회전"
                    className="rounded bg-blue-500/20 px-1.5 py-0.5 text-[10px] font-bold text-blue-200 transition hover:bg-blue-500/40"
                  >
                    ↻ +45°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      onDestinationChange?.({ ...destination, yaw: 0 });
                      onUpdateWaypointYaw?.(destination.name, 0);
                    }}
                    className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 hover:text-white"
                  >
                    0°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      onDestinationChange?.({ ...destination, yaw: 90 });
                      onUpdateWaypointYaw?.(destination.name, 90);
                    }}
                    className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 hover:text-white"
                  >
                    90°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      onDestinationChange?.({ ...destination, yaw: 180 });
                      onUpdateWaypointYaw?.(destination.name, 180);
                    }}
                    className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 hover:text-white"
                  >
                    180°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      onDestinationChange?.({ ...destination, yaw: -90 });
                      onUpdateWaypointYaw?.(destination.name, -90);
                    }}
                    className="rounded bg-white/5 px-1 py-0.5 text-[9px] text-slate-400 hover:text-white"
                  >
                    -90°
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      const robotYawDeg = Number(((pose.yaw * 180 / Math.PI + 180) % 360 - 180).toFixed(1));
                      onDestinationChange?.({ ...destination, yaw: robotYawDeg });
                      onUpdateWaypointYaw?.(destination.name, robotYawDeg);
                    }}
                    title="로봇의 현재 각도를 이 웨이포인트 방향으로 복사"
                    className="rounded bg-purple-500/30 px-1.5 py-0.5 text-[9px] font-bold text-purple-200 hover:bg-purple-500/50"
                  >
                    로봇방향 복사
                  </button>
                </div>
              </div>
            </div>
          )}
          {mouseMapCoord && (
            <div className="hidden rounded-lg border border-cyan-400/30 bg-slate-950/85 px-3 py-2 text-xs font-mono text-cyan-200 backdrop-blur-md md:block">
              <span className="text-slate-400">커서: </span>
              <span className="font-bold text-white">X {mouseMapCoord.x.toFixed(2)}, Y {mouseMapCoord.y.toFixed(2)}</span>
            </div>
          )}
        </div>
        <div className="hidden items-center gap-2 rounded-lg border border-white/10 bg-slate-950/70 px-3 py-2 text-[10px] font-mono text-slate-300 backdrop-blur-md sm:flex">
          <Crosshair size={13} className="text-cyan-300" /> WASD MOVE <span className="text-slate-600">/</span> Q E TURN
        </div>
        <div className="rounded-lg border border-cyan-200/15 bg-slate-950/75 px-3 py-2 text-right backdrop-blur-md">
          <div className="text-[10px] font-bold tracking-[0.2em] text-slate-500">DRIVE</div>
          <div className="mt-1 font-mono text-sm font-bold text-cyan-200">{speed.toFixed(2)}×</div>
        </div>
      </div>
    </div>
  );
}
