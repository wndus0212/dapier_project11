import { INDOOR_MAP, INDOOR_STATIC_OBSTACLES } from './indoorMap';

export interface RoutePoint {
  x: number;
  y: number;
}

export interface StaticObstacle {
  name: string;
  x: number;
  y: number;
  halfWidth: number;
  halfHeight: number;
}

export interface ScannedObstacle extends RoutePoint {
  distance?: number;
  source?: 'static' | 'dynamic';
}

// These footprints match the collidable table boxes in scene.xml.
export const STATIC_OBSTACLES: StaticObstacle[] = INDOOR_STATIC_OBSTACLES;

const WORLD = { ...INDOOR_MAP.bounds, resolution: INDOOR_MAP.resolution };
// Physical AMR clearance (~0.15m robot radius + safety margin).
const ROBOT_CLEARANCE = 0.20;
// Dynamic obstacle margin for real LiDAR scans.
export const DYNAMIC_OBSTACLE_CLEARANCE = 0.25;
const SQRT2 = Math.SQRT2;
// 8-directional chassis movements (orthogonal + diagonal) for natural, direct navigation.
const DIRECTIONS = [
  [-1, 0, 1],
  [1, 0, 1],
  [0, -1, 1],
  [0, 1, 1],
  [-1, -1, SQRT2],
  [-1, 1, SQRT2],
  [1, -1, SQRT2],
  [1, 1, SQRT2],
] as const;

class MinHeap {
  private values: Array<{ index: number; score: number }> = [];

  get size() { return this.values.length; }

  push(value: { index: number; score: number }) {
    this.values.push(value);
    let child = this.values.length - 1;
    while (child > 0) {
      const parent = Math.floor((child - 1) / 2);
      if (this.values[parent].score <= value.score) break;
      this.values[child] = this.values[parent];
      child = parent;
    }
    this.values[child] = value;
  }

  pop() {
    const first = this.values[0];
    const last = this.values.pop();
    if (this.values.length > 0 && last) {
      let parent = 0;
      while (true) {
        const left = parent * 2 + 1;
        const right = left + 1;
        if (left >= this.values.length) break;
        const child = right < this.values.length && this.values[right].score < this.values[left].score ? right : left;
        if (this.values[child].score >= last.score) break;
        this.values[parent] = this.values[child];
        parent = child;
      }
      this.values[parent] = last;
    }
    return first;
  }
}

/** Plan around the static scene and the latest live MuJoCo range scan. */
export function findRoute(
  start: RoutePoint,
  goal: RoutePoint,
  detectedObstacles: ScannedObstacle[] = [],
): RoutePoint[] | null {
  const columns = Math.round((WORLD.maxX - WORLD.minX) / WORLD.resolution) + 1;
  const rows = Math.round((WORLD.maxY - WORLD.minY) / WORLD.resolution) + 1;
  const total = columns * rows;
  const dirCount = DIRECTIONS.length;
  const stateCount = total * (dirCount + 1);
  const startDirection = dirCount;
  const turnPenalty = 0.20;
  const toCell = ({ x, y }: RoutePoint) => ({
    x: Math.round((x - WORLD.minX) / WORLD.resolution),
    y: Math.round((y - WORLD.minY) / WORLD.resolution),
  });
  const toPoint = (cell: { x: number; y: number }): RoutePoint => ({
    x: WORLD.minX + cell.x * WORLD.resolution,
    y: WORLD.minY + cell.y * WORLD.resolution,
  });
  const indexOf = (x: number, y: number) => y * columns + x;
  const inBounds = (x: number, y: number) => x >= 0 && x < columns && y >= 0 && y < rows;

  if (![start.x, start.y, goal.x, goal.y].every(Number.isFinite)) return null;
  if (start.x < WORLD.minX || start.x > WORLD.maxX || start.y < WORLD.minY || start.y > WORLD.maxY
    || goal.x < WORLD.minX || goal.x > WORLD.maxX || goal.y < WORLD.minY || goal.y > WORLD.maxY) return null;

  const startCell = toCell(start);
  const goalCell = toCell(goal);

  const planAStar = (useDynamic: boolean): RoutePoint[] | null => {
    const dynamicBlocked = new Uint8Array(total);
    if (useDynamic) {
      const dynamicCellRadius = Math.ceil(DYNAMIC_OBSTACLE_CLEARANCE / WORLD.resolution);
      for (const obstacle of detectedObstacles.filter(item => item.source !== 'static')) {
        // Ignore obstacles inside robot start or goal footprints
        if (Math.hypot(obstacle.x - start.x, obstacle.y - start.y) <= 0.20
          || Math.hypot(obstacle.x - goal.x, obstacle.y - goal.y) <= 0.20) continue;

        const center = toCell(obstacle);
        for (let dy = -dynamicCellRadius; dy <= dynamicCellRadius; dy += 1) {
          for (let dx = -dynamicCellRadius; dx <= dynamicCellRadius; dx += 1) {
            const x = center.x + dx;
            const y = center.y + dy;
            if (!inBounds(x, y)) continue;
            const point = toPoint({ x, y });
            if (Math.hypot(point.x - obstacle.x, point.y - obstacle.y) <= DYNAMIC_OBSTACLE_CLEARANCE) {
              dynamicBlocked[indexOf(x, y)] = 1;
            }
          }
        }
      }

      // Unblock start cell footprint so the robot is not blocked by near-field sensor reflections
      const robotFootprintRadius = Math.ceil(0.18 / WORLD.resolution);
      for (let dy = -robotFootprintRadius; dy <= robotFootprintRadius; dy++) {
        for (let dx = -robotFootprintRadius; dx <= robotFootprintRadius; dx++) {
          const nx = startCell.x + dx;
          const ny = startCell.y + dy;
          if (inBounds(nx, ny)) dynamicBlocked[indexOf(nx, ny)] = 0;
        }
      }

      // Unblock goal cell footprint (docking station target)
      const goalClearRadius = Math.ceil(0.15 / WORLD.resolution);
      for (let dy = -goalClearRadius; dy <= goalClearRadius; dy++) {
        for (let dx = -goalClearRadius; dx <= goalClearRadius; dx++) {
          const gx = goalCell.x + dx;
          const gy = goalCell.y + dy;
          if (inBounds(gx, gy)) dynamicBlocked[indexOf(gx, gy)] = 0;
        }
      }
    }

    const isPointBlocked = (px: number, py: number): boolean => {
      if (px < WORLD.minX + ROBOT_CLEARANCE || px > WORLD.maxX - ROBOT_CLEARANCE
        || py < WORLD.minY + ROBOT_CLEARANCE || py > WORLD.maxY - ROBOT_CLEARANCE) return true;
      if (STATIC_OBSTACLES.some(obstacle =>
        Math.abs(px - obstacle.x) <= obstacle.halfWidth + ROBOT_CLEARANCE
        && Math.abs(py - obstacle.y) <= obstacle.halfHeight + ROBOT_CLEARANCE)) return true;
      if (useDynamic) {
        const cx = Math.round((px - WORLD.minX) / WORLD.resolution);
        const cy = Math.round((py - WORLD.minY) / WORLD.resolution);
        if (inBounds(cx, cy) && dynamicBlocked[indexOf(cx, cy)] === 1) return true;
      }
      return false;
    };

    const isLineClear = (p1: RoutePoint, p2: RoutePoint): boolean => {
      const dist = Math.hypot(p2.x - p1.x, p2.y - p1.y);
      const steps = Math.max(2, Math.ceil(dist / (WORLD.resolution * 0.5)));
      for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        const x = p1.x + t * (p2.x - p1.x);
        const y = p1.y + t * (p2.y - p1.y);
        if (isPointBlocked(x, y)) return false;
      }
      return true;
    };

    // 0. Quick Direct Line-of-Sight Check:
    // If the straight path between start and goal is completely clear, use it directly (shortest possible path)
    if (isLineClear(start, goal)) {
      return [{ ...goal }];
    }

    const blocked = (x: number, y: number) => {
      if (!inBounds(x, y)) return true;
      const point = toPoint({ x, y });
      return isPointBlocked(point.x, point.y);
    };

    const startIndex = indexOf(startCell.x, startCell.y);
    const goalIndex = indexOf(goalCell.x, goalCell.y);
    const multiplier = dirCount + 1;
    const costs = new Float64Array(stateCount).fill(Number.POSITIVE_INFINITY);
    const parents = new Int32Array(stateCount).fill(-1);
    const closed = new Uint8Array(stateCount);
    const queue = new MinHeap();
    const heuristic = (x: number, y: number) => Math.hypot(goalCell.x - x, goalCell.y - y);
    const startState = startIndex * multiplier + startDirection;
    let goalState = startState;
    costs[startState] = 0;
    queue.push({ index: startState, score: heuristic(startCell.x, startCell.y) });

    while (queue.size > 0) {
      const current = queue.pop();
      if (!current || closed[current.index]) continue;
      const cellIndex = Math.floor(current.index / multiplier);
      const previousDirection = current.index % multiplier;
      if (cellIndex === goalIndex) {
        goalState = current.index;
        break;
      }
      closed[current.index] = 1;
      const x = cellIndex % columns;
      const y = Math.floor(cellIndex / columns);

      for (const [directionIndex, [dx, dy, stepCost]] of DIRECTIONS.entries()) {
        const nx = x + dx;
        const ny = y + dy;
        if (blocked(nx, ny)) continue;
        const nextState = indexOf(nx, ny) * multiplier + directionIndex;
        const turnCost = previousDirection === startDirection || previousDirection === directionIndex ? 0 : turnPenalty;
        const nextCost = costs[current.index] + stepCost + turnCost;
        if (nextCost >= costs[nextState]) continue;
        costs[nextState] = nextCost;
        parents[nextState] = current.index;
        queue.push({ index: nextState, score: nextCost + heuristic(nx, ny) });
      }
    }

    if (startIndex !== goalIndex && goalState === startState) return null;
    const reversedCells: Array<{ x: number; y: number }> = [];
    for (let cursor = goalState; cursor !== startState; cursor = parents[cursor]) {
      if (cursor < 0) return null;
      const cellIndex = Math.floor(cursor / multiplier);
      reversedCells.push({ x: cellIndex % columns, y: Math.floor(cellIndex / columns) });
    }
    reversedCells.reverse();
    if (reversedCells.length === 0) return [{ ...goal }];

    // Raw dense waypoints from start to goal
    const rawPath: RoutePoint[] = [{ x: start.x, y: start.y }, ...reversedCells.map(c => toPoint(c)), { x: goal.x, y: goal.y }];

    // String-Pulling (Line-of-Sight) Path Shortcutter:
    // Prunes all unnecessary grid staircase corners and connects distant visible waypoints directly
    const smoothed: RoutePoint[] = [rawPath[0]];
    let currentIdx = 0;
    while (currentIdx < rawPath.length - 1) {
      let furthestIdx = currentIdx + 1;
      for (let testIdx = rawPath.length - 1; testIdx > currentIdx + 1; testIdx--) {
        if (isLineClear(rawPath[currentIdx], rawPath[testIdx])) {
          furthestIdx = testIdx;
          break;
        }
      }
      smoothed.push(rawPath[furthestIdx]);
      currentIdx = furthestIdx;
    }

    // Exclude the initial start point since it is the robot's current pose
    const finalWaypoints = smoothed.slice(1);
    if (finalWaypoints.length === 0) return [{ ...goal }];
    return finalWaypoints;
  };

  // 1. Try dynamic A* route with current obstacles
  // 2. Fall back to static map A* if dynamic clutter blocks narrow corridors
  // 3. Fall back to straight line waypoint
  return planAStar(true) ?? planAStar(false) ?? [{ x: goal.x, y: goal.y }];
}

/** Only mobile returns belong in live re-planning; static geometry is already in the map. */
export function getDynamicObstaclePoints(scan: ScannedObstacle[]): RoutePoint[] {
  return scan
    .filter(point => point.source !== 'static' && Number.isFinite(point.x) && Number.isFinite(point.y))
    .map(({ x, y }) => ({ x, y }));
}

/** Check the remaining route against dynamic scan points using A*'s exact margin. */
export function isRouteBlockedByDynamicObstacle(
  pose: RoutePoint,
  route: RoutePoint[],
  waypointIndex: number,
  obstacles: RoutePoint[],
): boolean {
  const points = [pose, ...route.slice(waypointIndex)];
  return obstacles.some(obstacle => points.slice(1).some((end, index) => {
    const start = points[index];
    const vx = end.x - start.x;
    const vy = end.y - start.y;
    const lengthSquared = vx * vx + vy * vy;
    const t = lengthSquared > 0
      ? Math.max(0, Math.min(1, ((obstacle.x - start.x) * vx + (obstacle.y - start.y) * vy) / lengthSquared))
      : 0;
    return Math.hypot(obstacle.x - (start.x + t * vx), obstacle.y - (start.y + t * vy)) < DYNAMIC_OBSTACLE_CLEARANCE;
  }));
}
