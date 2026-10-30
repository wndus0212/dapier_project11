import type { RoutePoint } from './pathPlanner';

export interface NavigationPose extends RoutePoint {
  yaw: number;
}

export interface WaypointCommand {
  distance: number;
  desiredYaw: number;
  headingError: number;
  steeringError: number;
  angular: number;
  linearX: number;
  linearY: number;
  turnDirection: number | null;
  phase: 'arrived' | 'aligning' | 'braking' | 'driving';
  damping: boolean;
}

const ARRIVAL_RADIUS_M = 0.3;
const DRIVE_ALIGNMENT_RAD = 0.2;
const MAX_ANGULAR_SPEED_RAD_S = 0.55;
const ANGULAR_GAIN = 1.25;
const ANGULAR_DAMPING_GAIN = 0.8;
const ARRIVAL_YAW_RATE_RAD_S = 0.05;

export function shouldStopForDriveFeedback(status: string): boolean {
  return ['rejected', 'error', 'throttled', 'rate_limited', 'stale'].includes(status);
}

export function wrapAngle(angle: number): number {
  return Math.atan2(Math.sin(angle), Math.cos(angle));
}

/** Keep the chosen turn direction stable at the +/-pi wrap boundary. */
export function updateTurnDirection(error: number, previous: number | null): number | null {
  const magnitude = Math.abs(error);
  if (magnitude > 2.65) return previous ?? (error >= 0 ? 1 : -1);
  if (magnitude < 2.2) return null;
  return previous;
}

/**
 * Holonomic MuJoCo waypoint controller. World/map translation follows the
 * checked A* segment; heading is aligned first so the chassis faces travel.
 * Angles are radians, distances metres, and rates m/s or rad/s.
 */
export function computeWaypointCommand(
  pose: NavigationPose,
  target: RoutePoint,
  maxSpeed: number,
  previousTurnDirection: number | null = null,
  actualYawRate = 0,
  isFinalWaypoint = false,
): WaypointCommand {
  const dx = target.x - pose.x;
  const dy = target.y - pose.y;
  const distance = Math.hypot(dx, dy);
  const desiredYaw = Math.atan2(dy, dx);
  const headingError = wrapAngle(desiredYaw - pose.yaw);
  const turnDirection = updateTurnDirection(headingError, previousTurnDirection);
  const steeringError = turnDirection === null
    ? headingError
    : turnDirection * Math.abs(headingError);
  const yawRate = Number.isFinite(actualYawRate) ? actualYawRate : 0;
  const settled = Math.abs(yawRate) <= ARRIVAL_YAW_RATE_RAD_S;
  const rawAngular = ANGULAR_GAIN * steeringError - ANGULAR_DAMPING_GAIN * yawRate;
  const pdAngular = Math.max(-MAX_ANGULAR_SPEED_RAD_S, Math.min(MAX_ANGULAR_SPEED_RAD_S, rawAngular));
  const absHeading = Math.abs(headingError);
  const aligned = absHeading <= DRIVE_ALIGNMENT_RAD;
  const angular = aligned && settled ? 0 : pdAngular;
  const damping = angular * yawRate < 0;

  if (isFinalWaypoint) {
    if (distance <= ARRIVAL_RADIUS_M && settled) {
      return { distance, desiredYaw, headingError, steeringError, angular: 0, linearX: 0, linearY: 0, turnDirection, phase: 'arrived', damping: false };
    }
    if (distance <= ARRIVAL_RADIUS_M || (aligned && !settled)) {
      return { distance, desiredYaw, headingError, steeringError, angular, linearX: 0, linearY: 0, turnDirection, phase: 'braking', damping };
    }
  }

  // Only stop linear drive to rotate in place if heading error is large (> 0.75 rad ~ 43 deg)
  if (absHeading > 0.75 || maxSpeed <= 0) {
    return { distance, desiredYaw, headingError, steeringError, angular: pdAngular, linearX: 0, linearY: 0, turnDirection, phase: 'aligning', damping };
  }

  // Smooth unicycle forward drive: scale smoothly with cos(headingError) when slightly off-heading
  const forwardScale = Math.max(0.4, Math.cos(headingError));
  const rawSpeed = isFinalWaypoint
    ? Math.min(maxSpeed, Math.max(0.25, Math.sqrt(1.5 * Math.max(0, distance - ARRIVAL_RADIUS_M))))
    : maxSpeed;
  const driveSpeed = rawSpeed * forwardScale;

  return {
    distance,
    desiredYaw,
    headingError,
    steeringError,
    angular,
    linearX: driveSpeed * dx / distance,
    linearY: driveSpeed * dy / distance,
    turnDirection,
    phase: 'driving',
    damping: false,
  };
}

export interface MotionSafetySample {
  nowMs: number;
  distance: number;
  commandedX: number;
  commandedY: number;
  actualX: number;
  actualY: number;
  commandedAngular: number;
  actualAngular: number;
  damping?: boolean;
}

/** Stateful guards for persistent wrong-way motion and goal divergence. */
export class NavigationMotionGuard {
  private wrongWaySince: number | null = null;
  private wrongTurnSince: number | null = null;
  private brakingSince: number | null = null;
  private bestBrakingRate = Number.POSITIVE_INFINITY;
  private uncommandedSpinSince: number | null = null;
  private divergenceSince: number | null = null;
  private previousDistance: number | null = null;
  private minAchievedDistance: number = Number.POSITIVE_INFINITY;

  reset() {
    this.wrongWaySince = null;
    this.wrongTurnSince = null;
    this.brakingSince = null;
    this.bestBrakingRate = Number.POSITIVE_INFINITY;
    this.uncommandedSpinSince = null;
    this.divergenceSince = null;
    this.previousDistance = null;
    this.minAchievedDistance = Number.POSITIVE_INFINITY;
  }

  observe(sample: MotionSafetySample): string | null {
    const commandMagnitude = Math.hypot(sample.commandedX, sample.commandedY);
    const actualMagnitude = Math.hypot(sample.actualX, sample.actualY);
    const normalizedDot = commandMagnitude > 0 && actualMagnitude > 0
      ? (sample.commandedX * sample.actualX + sample.commandedY * sample.actualY)
        / (commandMagnitude * actualMagnitude)
      : 0;

    if (commandMagnitude > 0.08 && actualMagnitude > 0.04 && normalizedDot < -0.35) {
      this.wrongWaySince ??= sample.nowMs;
      if (sample.nowMs - this.wrongWaySince >= 900) return '명령 방향과 MuJoCo 이동 방향이 지속해서 반대입니다.';
    } else {
      this.wrongWaySince = null;
    }

    const oppositeTurn = Math.abs(sample.commandedAngular) > 0.08
      && Math.abs(sample.actualAngular) > 0.04
      && sample.commandedAngular * sample.actualAngular < 0;
    if (oppositeTurn && sample.damping === true) {
      this.wrongTurnSince = null;
      const currentRate = Math.abs(sample.actualAngular);
      this.brakingSince ??= sample.nowMs;
      if (currentRate < this.bestBrakingRate - 0.015) {
        this.bestBrakingRate = currentRate;
        this.brakingSince = sample.nowMs;
      }
      if (sample.nowMs - this.brakingSince >= 900) return '회전 감속 명령에도 실제 yaw 속도가 줄지 않아 정지했습니다.';
    } else {
      this.brakingSince = null;
      this.bestBrakingRate = Number.POSITIVE_INFINITY;
      if (oppositeTurn) {
        this.wrongTurnSince ??= sample.nowMs;
        if (sample.nowMs - this.wrongTurnSince >= 900) return '회전 명령과 MuJoCo yaw 변화 방향이 지속해서 반대입니다.';
      } else {
        this.wrongTurnSince = null;
      }
    }

    if (Math.abs(sample.commandedAngular) < 0.05 && Math.abs(sample.actualAngular) > 0.12) {
      this.uncommandedSpinSince ??= sample.nowMs;
      if (sample.nowMs - this.uncommandedSpinSince >= 700) return '회전 명령이 없는데 MuJoCo 로봇이 계속 회전해 정지했습니다.';
    } else {
      this.uncommandedSpinSince = null;
    }

    // 목표 이탈(Divergence) 안전 검사:
    // 전진 주행 명령이 내려진 상태에서 거리가 최근 최단거리보다 25cm 이상 계속 멀어지는 경우만 감지 (3.5초 유예)
    const isDrivingForward = commandMagnitude > 0.05;
    if (sample.distance < this.minAchievedDistance) {
      this.minAchievedDistance = sample.distance;
      this.divergenceSince = null;
    } else if (isDrivingForward && sample.distance > this.minAchievedDistance + 0.25) {
      this.divergenceSince ??= sample.nowMs;
      if (sample.nowMs - this.divergenceSince >= 3500) {
        return '목적지까지의 거리가 지속해서 증가해 주행을 정지했습니다.';
      }
    } else if (sample.distance <= (this.previousDistance ?? sample.distance) + 0.015) {
      this.divergenceSince = null;
    }

    this.previousDistance = sample.distance;
    return null;
  }
}
