"""
LiDAR Scan-to-Map Matching Localizer
벽면 거리 필드(Distance Transform Field)를 활용하여 실시간 라이다 스캔과 SLAM 지도를 대조,
초기 위치 추정 오차를 밀리초 단위로 자동 보정하는 모듈.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml


def _nelder_mead_3d(cost_func, x0, step=(0.08, 0.08, 0.05), max_iter=45):
    """순수 NumPy 기반 3차원(x, y, yaw) 넬더-미드 심플렉스 최적화기 (scipy 의존성 제거)"""
    n = len(x0)
    sim = [np.array(x0, dtype=float)]
    for i in range(n):
        y = np.array(x0, dtype=float)
        y[i] += step[i]
        sim.append(y)

    alpha, gamma, rho, sigma = 1.0, 2.0, 0.5, 0.5
    f_sim = [cost_func(p) for p in sim]

    for _ in range(max_iter):
        idx = np.argsort(f_sim)
        sim = [sim[i] for i in idx]
        f_sim = [f_sim[i] for i in idx]

        x_bar = np.mean(sim[:-1], axis=0)
        x_r = x_bar + alpha * (x_bar - sim[-1])
        f_r = cost_func(x_r)

        if f_sim[0] <= f_r < f_sim[-2]:
            sim[-1], f_sim[-1] = x_r, f_r
            continue

        if f_r < f_sim[0]:
            x_e = x_bar + gamma * (x_r - x_bar)
            f_e = cost_func(x_e)
            sim[-1], f_sim[-1] = (x_e, f_e) if f_e < f_r else (x_r, f_r)
            continue

        x_c = x_bar + rho * (sim[-1] - x_bar)
        f_c = cost_func(x_c)
        if f_c < f_sim[-1]:
            sim[-1], f_sim[-1] = x_c, f_c
            continue

        for i in range(1, len(sim)):
            sim[i] = sim[0] + sigma * (sim[i] - sim[0])
            f_sim[i] = cost_func(sim[i])

    return sim[0], f_sim[0]


class ScanMatcher:
    def __init__(self, map_yaml_path: str | Path | None = None):
        self.available = False
        self.map_cfg: dict[str, Any] = {}
        self.dist_map: np.ndarray | None = None
        self.resolution: float = 0.05
        self.origin_x: float = -5.285
        self.origin_y: float = -13.490
        self.map_w: int = 0
        self.map_h: int = 0
        self.map_path: Path | None = None

        self._load_map(map_yaml_path)

    def _load_map(self, path: str | Path | None = None) -> bool:
        candidate_paths = [
            Path(path) if path else None,
            Path("/home/juyeon/my_map.yaml"),
            Path("/home/juyeon/ros2_amr_ws/maps/my_map.yaml"),
            Path.home() / "my_map.yaml",
            Path(__file__).parent.parent.parent / "maps" / "my_map.yaml",
        ]

        valid_yaml = None
        for p in candidate_paths:
            if p and p.is_file():
                valid_yaml = p
                break

        if not valid_yaml:
            print("[SCAN_MATCHER] ⚠️ my_map.yaml 파일을 찾을 수 없어 스캔 매칭을 비활성화합니다.")
            return False

        try:
            with open(valid_yaml, "r", encoding="utf-8") as f:
                self.map_cfg = yaml.safe_load(f)

            img_rel = self.map_cfg.get("image", "my_map.pgm")
            img_path = valid_yaml.parent / img_rel
            if not img_path.is_file():
                # Fallback to absolute or sibling
                img_path = Path("/home/juyeon") / Path(img_rel).name

            img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                print(f"[SCAN_MATCHER] ⚠️ 맵 이미지 로드 실패: {img_path}")
                return False

            self.map_h, self.map_w = img.shape
            self.resolution = float(self.map_cfg.get("resolution", 0.05))
            origin = self.map_cfg.get("origin", [-5.285, -13.490, 0.0])
            self.origin_x = float(origin[0])
            self.origin_y = float(origin[1])

            # PGM map convention: 0 is occupied (black wall), 254 is free space
            walls = (img < 50).astype(np.uint8)
            # Compute Euclidean distance transform in meters
            self.dist_map = cv2.distanceTransform(
                cv2.bitwise_not(walls * 255), cv2.DIST_L2, 5
            ).astype(np.float32) * self.resolution

            self.map_path = valid_yaml
            self.available = True
            print(
                f"[SCAN_MATCHER] ✅ SLAM 맵 벽면 거리장 로드 완료: {valid_yaml} "
                f"(크기: {self.map_w}x{self.map_h}, 해상도: {self.resolution}m, 벽면 픽셀: {int(np.sum(walls))})"
            )
            return True
        except Exception as exc:
            print(f"[SCAN_MATCHER] ❌ 맵 초기화 오류: {exc}")
            self.available = False
            return False

    def _load_waypoints(self) -> list[dict[str, Any]]:
        candidate_json = [
            Path("/home/juyeon/dapier_project11/XLeRobot/web_control/shared/navigation_destinations.json"),
            Path("/home/juyeon/XLeRobot/web_control/shared/navigation_destinations.json"),
            Path(__file__).parent.parent.parent / "shared" / "navigation_destinations.json",
        ]
        for p in candidate_json:
            if p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        return data.get("destinations", [])
                except Exception:
                    pass
        return []

    def extract_points_from_scan(self, scan_data: Any) -> np.ndarray:
        """ROS 2 LaserScan 메시지 또는 raw 딕셔너리로부터 로봇 기준 유효 (x, y) 포인트 추출"""
        if scan_data is None:
            return np.empty((0, 2), dtype=np.float32)

        try:
            ranges = getattr(scan_data, "ranges", None)
            angle_min = float(getattr(scan_data, "angle_min", -math.pi))
            angle_inc = float(getattr(scan_data, "angle_increment", 0.01745))

            if ranges is None and isinstance(scan_data, dict):
                ranges = scan_data.get("ranges", [])
                angle_min = float(scan_data.get("angle_min", -math.pi))
                angle_inc = float(scan_data.get("angle_increment", 0.01745))

            if not ranges:
                return np.empty((0, 2), dtype=np.float32)

            ranges_arr = np.array(ranges, dtype=np.float32)
            num_points = len(ranges_arr)
            angles = angle_min + np.arange(num_points, dtype=np.float32) * angle_inc

            # 유효 거리 필터 (로봇 몸체 필터 0.25m ~ 최대 8.0m)
            valid_mask = (ranges_arr >= 0.25) & (ranges_arr <= 8.0) & np.isfinite(ranges_arr)
            valid_r = ranges_arr[valid_mask]
            valid_a = angles[valid_mask]

            if len(valid_r) == 0:
                return np.empty((0, 2), dtype=np.float32)

            # 계산 속도를 위해 최대 180포인트로 균일 다운샘플링
            if len(valid_r) > 180:
                step = len(valid_r) // 180
                valid_r = valid_r[::step][:180]
                valid_a = valid_a[::step][:180]

            # Laser frame mount offset (+0.095m forward from robot center per amr.urdf)
            laser_offset = 0.095
            xs = valid_r * np.cos(valid_a) + laser_offset
            ys = valid_r * np.sin(valid_a)
            return np.column_stack([xs, ys]).astype(np.float32)
        except Exception as exc:
            print(f"[SCAN_MATCHER] 포인트 변환 오류: {exc}")
            return np.empty((0, 2), dtype=np.float32)

    def align_scan(
        self,
        scan_data: Any,
        init_x: float | None = None,
        init_y: float | None = None,
        init_yaw: float | None = None,
        search_radius: float = 1.3,
        search_yaw_deg: float = 180.0,
        hint_name: str | None = None,
    ) -> dict[str, Any]:
        """
        초기 추정 위치 (init_x, init_y, init_yaw) 및 등록된 모든 웨이포인트(지점 A, B, C, 홈)를
        동시에 대조하여 로봇이 실제 위치한 구역의 벽면에 정밀 정합 수행
        """
        fallback_x = float(init_x) if init_x is not None else -0.59
        fallback_y = float(init_y) if init_y is not None else -0.88
        fallback_yaw = float(init_yaw) if init_yaw is not None else math.radians(-17.1)

        if not self.available or self.dist_map is None:
            return {
                "success": False,
                "message": "스캔 매처가 준비되지 않았습니다 (맵 미로드)",
                "x": fallback_x,
                "y": fallback_y,
                "yaw": fallback_yaw,
                "yaw_deg": round(math.degrees(fallback_yaw), 1),
                "residual_cm": 999.0,
            }

        scan_pts = (
            scan_data
            if isinstance(scan_data, np.ndarray)
            else self.extract_points_from_scan(scan_data)
        )

        if len(scan_pts) < 15:
            return {
                "success": False,
                "message": f"유효한 라이다 벽면 포인트가 부족합니다 (감지: {len(scan_pts)}개)",
                "x": fallback_x,
                "y": fallback_y,
                "yaw": fallback_yaw,
                "yaw_deg": round(math.degrees(fallback_yaw), 1),
                "residual_cm": 999.0,
            }

        h, w = self.map_h, self.map_w
        res = self.resolution
        minX, minY = self.origin_x, self.origin_y
        dist_field = self.dist_map

        # 후보 시드 수집:
        # 1) 호출자가 지정한 힌트/추정 위치 (있다면 최우선 후보)
        # 2) 등록된 모든 주요 웨이포인트(홈, 지점 A, 지점 B, 지점 C)
        # -> 로봇이 실제 어디에 있든(A 또는 홈 등) 모든 구역을 전역 탐색하여 오차가 가장 적은 실제 위치로 정합
        seeds: list[tuple[str, float, float, float]] = []
        if init_x is not None and init_y is not None:
            iyaw = float(init_yaw) if init_yaw is not None else 0.0
            seeds.append((hint_name or "지정 위치", float(init_x), float(init_y), iyaw))

        seen_coords = set()
        for s_name, sx, sy, _ in seeds:
            seen_coords.add((round(sx, 1), round(sy, 1)))

        for wp in self._load_waypoints():
            wp_name = wp.get("name", "웨이포인트")
            wp_x = float(wp.get("x", 0.0))
            wp_y = float(wp.get("y", 0.0))
            wp_yaw = math.radians(float(wp.get("yaw", 0.0)))
            key = (round(wp_x, 1), round(wp_y, 1))
            if key not in seen_coords:
                seen_coords.add(key)
                seeds.append((wp_name, wp_x, wp_y, wp_yaw))

        if not seeds:
            seeds = [("홈 기본 위치", -0.59, -0.88, math.radians(-17.1))]

        def cost_func(p: list[float] | np.ndarray) -> float:
            c, s = math.cos(p[2]), math.sin(p[2])
            wx = p[0] + scan_pts[:, 0] * c - scan_pts[:, 1] * s
            wy = p[1] + scan_pts[:, 0] * s + scan_pts[:, 1] * c
            px = np.clip(np.round((wx - minX) / res).astype(int), 0, w - 1)
            py = np.clip(np.round((h - 1) - (wy - minY) / res).astype(int), 0, h - 1)
            return float(np.mean(np.minimum(dist_field[py, px], 0.35) ** 2))

        # 1. 조대 탐색 (Coarse Grid Search): 시드 주변 ±1.2m 격자 및 16개 방향(360도 전방위)
        best_seed_name = seeds[0][0]
        best_coarse = [seeds[0][1], seeds[0][2], seeds[0][3]]
        best_score = float("inf")

        for s_name, sx, sy, syaw in seeds:
            rad = max(0.6, min(1.3, float(search_radius)))
            grid_xs = np.linspace(sx - rad, sx + rad, 8)
            grid_ys = np.linspace(sy - rad, sy + rad, 8)
            grid_yaws = np.linspace(-math.pi, math.pi, 16, endpoint=False)

            for gx in grid_xs:
                for gy in grid_ys:
                    for gyaw in grid_yaws:
                        sc = cost_func([gx, gy, gyaw])
                        if sc < best_score:
                            best_score = sc
                            best_coarse = [gx, gy, gyaw]
                            best_seed_name = s_name

        # 2. 미세 최적화 (Fine Simplex Optimization)
        try:
            opt_pose, min_fun = _nelder_mead_3d(
                cost_func,
                best_coarse,
                step=(0.06, 0.06, 0.04),
                max_iter=45,
            )
            opt_x, opt_y, opt_yaw = opt_pose
            residual_cm = math.sqrt(max(0.0, float(min_fun))) * 100.0

            # 각도 정규화 (-pi ~ pi)
            opt_yaw = math.atan2(math.sin(opt_yaw), math.cos(opt_yaw))

            # 실제 환경의 가구/의자 등 비지도 물체를 감안하여 잔차 26cm 이내면 정합 성공
            success = residual_cm <= 26.0

            return {
                "success": success,
                "x": round(float(opt_x), 3),
                "y": round(float(opt_y), 3),
                "yaw": round(float(opt_yaw), 4),
                "yaw_deg": round(math.degrees(opt_yaw), 1),
                "residual_cm": round(float(residual_cm), 1),
                "num_points": int(len(scan_pts)),
                "matched_area": best_seed_name,
                "message": (
                    f"벽면 스캔 매칭 성공: {best_seed_name} 부근 정합 (오차: {residual_cm:.1f}cm)"
                    if success
                    else f"벽면 스캔 매칭 신뢰도 낮음 (오차: {residual_cm:.1f}cm)"
                ),
            }
        except Exception as exc:
            return {
                "success": False,
                "message": f"최적화 계산 오류: {exc}",
                "x": fallback_x,
                "y": fallback_y,
                "yaw": fallback_yaw,
                "yaw_deg": round(math.degrees(fallback_yaw), 1),
                "residual_cm": 999.0,
            }

    def refine_local_pose(
        self,
        scan_data: Any,
        current_x: float,
        current_y: float,
        current_yaw: float,
        max_dist_drift: float = 0.20,
        max_residual_cm: float = 24.0,
    ) -> tuple[float, float, float, float] | None:
        """
        1.5ms 초고속 국소 단체법(Nelder-Mead simplex) 보정.
        주행 중 또는 정지 시 라이다 점이 벽면과 어긋나지 않도록 실시간 미세 트래킹.
        반환: (refined_x, refined_y, refined_yaw, residual_cm) 또는 None
        """
        if not self.available or self.dist_map is None:
            return None
        scan_pts = (
            scan_data
            if isinstance(scan_data, np.ndarray)
            else self.extract_points_from_scan(scan_data)
        )
        if len(scan_pts) < 15:
            return None

        h, w = self.map_h, self.map_w
        res = self.resolution
        minX, minY = self.origin_x, self.origin_y
        dist_field = self.dist_map

        def cost_func(p: list[float] | np.ndarray) -> float:
            c, s = math.cos(p[2]), math.sin(p[2])
            wx = p[0] + scan_pts[:, 0] * c - scan_pts[:, 1] * s
            wy = p[1] + scan_pts[:, 0] * s + scan_pts[:, 1] * c
            px = np.clip(np.round((wx - minX) / res).astype(int), 0, w - 1)
            py = np.clip(np.round((h - 1) - (wy - minY) / res).astype(int), 0, h - 1)
            return float(np.mean(np.minimum(dist_field[py, px], 0.35) ** 2))

        try:
            init_cost = cost_func([current_x, current_y, current_yaw])
            opt, min_fun = _nelder_mead_3d(
                cost_func,
                [current_x, current_y, current_yaw],
                step=(0.04, 0.04, 0.03),
                max_iter=25,
            )
            resid_cm = math.sqrt(max(0.0, float(min_fun))) * 100.0

            dx = float(opt[0]) - current_x
            dy = float(opt[1]) - current_y
            dist_drift = math.hypot(dx, dy)

            # 오차가 개선되었고, 급격한 튀김(점프)이 없으며 허용 잔차 이내인 경우만 수용
            if min_fun < init_cost and dist_drift <= max_dist_drift and resid_cm <= max_residual_cm:
                norm_yaw = math.atan2(math.sin(float(opt[2])), math.cos(float(opt[2])))
                return float(opt[0]), float(opt[1]), norm_yaw, resid_cm
        except Exception:
            pass
        return None
