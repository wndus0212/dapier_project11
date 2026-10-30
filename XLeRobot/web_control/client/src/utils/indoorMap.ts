import type { StaticObstacle } from './pathPlanner';
import destinationMap from '../../../shared/navigation_destinations.json';

/** SLAM map (my_map.yaml) and AMR waypoints */
export const INDOOR_MAP = {
  source: 'slam_my_map' as const,
  frameId: 'map',
  resolution: 0.05,
  bounds: { minX: -5.285, maxX: 9.815, minY: -13.490, maxY: 1.310 },
  image: '/my_map.png',
  start: {
    x: destinationMap.destinations[0]?.x ?? -0.59,
    y: destinationMap.destinations[0]?.y ?? -0.88,
    yaw: (((destinationMap.destinations[0] as any)?.yaw ?? -17.1) * Math.PI) / 180,
  },
  charging: {
    x: destinationMap.destinations[0]?.x ?? -0.59,
    y: destinationMap.destinations[0]?.y ?? -0.88,
    yaw: (((destinationMap.destinations[0] as any)?.yaw ?? -17.1) * Math.PI) / 180,
  },
  tables: [] as Array<{ name: string; x: number; y: number; halfWidth: number; halfHeight: number; approach: { x: number; y: number } }>,
} as const;

export const INDOOR_STATIC_OBSTACLES: StaticObstacle[] = INDOOR_MAP.tables.map(table => ({
  name: table.name,
  x: table.x,
  y: table.y,
  halfWidth: table.halfWidth,
  halfHeight: table.halfHeight,
}));

export const TABLE_APPROACH_DESTINATIONS = destinationMap.destinations
  .filter(item => item.kind !== 'charging')
  .map(item => ({
    name: item.name,
    x: item.x,
    y: item.y,
    yaw: (item as any).yaw ?? 0,
  }));

export const CHARGING_DESTINATION = {
  name: destinationMap.destinations[0]?.name ?? '홈 스테이션',
  x: destinationMap.destinations[0]?.x ?? -1.41,
  y: destinationMap.destinations[0]?.y ?? -1.39,
  yaw: (destinationMap.destinations[0] as any)?.yaw ?? -15.5,
};

export const NAVIGATION_DESTINATIONS = destinationMap.destinations;
