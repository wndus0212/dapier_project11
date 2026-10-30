export interface SocketStatus {
  socket: 'connected' | 'connecting' | 'disconnected' | 'error';
  video: 'connected' | 'connecting' | 'disconnected' | 'error' | 'streaming';
}

export interface SystemMessage {
  id: string;
  content: string;
  timestamp: number;
  type: 'info' | 'success' | 'warning' | 'error';
}

export type MapSource = 'mujoco_static' | 'slam_occupancy_grid';

/** Normalized ROS sensor_msgs/LaserScan contract for a future live scan adapter. */
export interface LaserScanData {
  frameId: string;
  stamp: { sec: number; nanosec: number };
  angleMin: number;
  angleMax: number;
  angleIncrement: number;
  rangeMin: number;
  rangeMax: number;
  ranges: number[];
}

/** Normalized map contract; ROS nav_msgs/OccupancyGrid maps to this shape. */
export interface OccupancyGridMap {
  source: 'slam_occupancy_grid';
  frameId: string;
  stamp: { sec: number; nanosec: number };
  resolution: number;
  width: number;
  height: number;
  origin: { x: number; y: number; yaw: number };
  /** Row-major ROS occupancy values: -1 unknown, 0 free, 100 occupied. */
  data: number[];
}

export interface MapMetadata {
  source: MapSource;
  frameId: string;
  resolution: number;
  bounds: { minX: number; maxX: number; minY: number; maxY: number };
}

export interface RobotSimulationState {
  robot_type?: string;
  navigation_execution_enabled?: boolean;
  nomi_mock_mode?: boolean;
  status?: string;
  connected?: boolean;
  host_connected?: boolean;
  state_age?: number | null;
  timestamp?: number;
  position?: { x?: number; y?: number; z?: number };
  rotation?: { roll?: number; pitch?: number; yaw?: number };
  velocity?: { linear?: { x?: number; y?: number }; angular?: { z?: number } };
  arm_joints?: { left?: number[]; right?: number[] };
  obstacle_scan?: Array<{ x: number; y: number; distance: number; source?: 'static' | 'dynamic' }>;
  obstacle_scan_status?: {
    healthy?: boolean;
    timestamp?: number;
    age_ms?: number;
    beam_count?: number;
    layer_heights_m?: number[];
    ray_count?: number;
    range_m?: number;
    hit_count?: number;
    scan_duration_ms?: number;
    status?: 'ok' | 'slow' | 'error';
    error?: string | null;
  };
  docking_status?: DockingStatus | null;
}

export interface DockingStatus {
  state: 'IDLE' | 'SEARCHING' | 'ALIGNING' | 'APPROACHING' | 'STRAIGHT_IN' | 'STRAIGHT_LOCK' | 'DOCKED' | string;
  tag_detected: boolean;
  tag_id?: number | null;
  tag_mode?: string;
  dist_m?: number | null;
  dist_err_m?: number | null;
  lat_err_m?: number | null;
  yaw_deg?: number | null;
  heading_deg?: number | null;
  handover_active?: boolean;
  cmd_vx?: number;
  cmd_wz?: number;
  motion_code?: string;
  motion_kr?: string;
  motion_en?: string;
  backup_count?: number;
  docking_time_s?: number;
  flip_180?: boolean;
  timestamp?: number;
}

export interface CommandFeedback {
  sequence: number;
  direction: string;
  speed: number;
  status: 'pending' | 'success' | 'accepted' | 'queued_to_zmq' | 'error' | 'rejected' | 'throttled' | 'rate_limited' | 'stale';
  latencyMs: number | null;
  message?: string;
}
