# 📋 [작업 공유] AMR 네비게이션 개편 및 SO-101 양팔 로봇 수건 접기 스킬 연동

## 1. 시스템 아키텍처 및 개요 (방안 A: 분산 구조)

> **💡 하드웨어 분산 아키텍처 (안정성 & 성능 최적화)**
> - **라즈베리파이 / AMR (Edge)**: 하드웨어 센서 제어, 모터 구동 및 Nav2 자율주행 전담 (CPU/메모리 부하 최소화)
> - **GPU 노트북 (Server)**: 고부하 연산 전담 (로컬 LLM Ollama 추론 + SO-101 양팔 로봇 정책 모델 추론)
> - **통신**: AMR 에이전트가 Wi-Fi(사설 네트워크)를 통해 노트북의 Ollama(포트 11434) 및 수건 접기 API(포트 8100)를 HTTP 비동기 호출

- **작업 목적**:
  1. 직관적인 사용자 명령을 위한 자율주행 목적지 명칭 개편 (기존 A/B/C → 책상/소파).
  2. SO-101 양팔 로봇의 학습된 정책 기반 **수건 접기 스킬(Towel Folding Skill)**을 NOMI Agent에 완전 연동하여 복합 자율 미션("책상에 가서 수건 접어줘") 구현.

---

## 2. 주요 작업 및 구현 내용

### ① 자율주행 목적지 개편 및 동기화
- **내용**: 기존 알파벳 목적지(`지점 A`, `지점 B`, `지점 C`)를 실제 환경 기준 명칭으로 변경하고 미사용 지점 정리.
  - `지점 A` → **`책상`** (`x: -1.07`, `y: -3.15`, `yaw: -106.4°`)
  - `지점 B` → **`소파`** (`x: 2.20`, `y: -1.75`, `yaw: 70.0°`)
  - `지점 C` → **완전 삭제**
- **반영 파일**:
  - Web UI / Simulator: `XLeRobot/web_control/shared/navigation_destinations.json`, `XLeRobot/web_control/client/src/components/features/GameSimulationCanvas.tsx`
  - AMR Nav2 설정: `ros2_amr_ws/config/waypoints.xml`, `ros2_amr_ws/.agents/skills/navigate-amr/SKILL.md`

---

### ② SO-101 양팔 로봇 수건 접기 스킬 연동 (노션 사양 준수)
- **HTTP 클라이언트 (`ros2_ws/src/nomi_agent/nomi_agent/towel_client.py`)**:
  - `TOWEL_API_URL`, `TOWEL_API_TOKEN` 환경변수를 통해 GPU 노트북(LeRobot 정책 서버)과 `Bearer` 토큰 인증 기반 통신.
  - `POST /fold`, `GET /status`, `POST /stop`, `GET /tools` 엔드포인트 지원.
  - `python3 -c "import towel_client as c; print([t['function']['name'] for t in c.load_tools()])"` 검증 통과 (`fold_towel`, `get_towel_fold_status`, `stop_towel_fold`).
- **NOMI 스킬 클래스 (`ros2_ws/src/nomi_agent/nomi_agent/skills/towel_folding_skill.py`)**:
  - `fold_towel(confirm=True, wait_s=60)`: 로봇 팔 정책 추론 시작 요청.
  - `get_towel_fold_status()`: 5~10초 주기 상태(`state`, `phase`) 및 비전 검증 판정(`verdict`) 폴링.
  - `stop_towel_fold()`: 비상 중단 처리.
- **도구 레지스트리 (`ros2_ws/src/nomi_agent/nomi_agent/tool_registry.py`)**:
  - 함수 호출 스키마에 수건 접기 도구 3종 신규 등록.
  - 사용자가 "멈춰"를 외칠 때 AMR 주행 정지뿐 아니라 **양팔 로봇도 즉시 정지**되도록 `emergency_stop()`에 `stop_towel_fold()` 연동.

---

### ③ SafetyGuard 및 LLM 복합 시나리오 프롬프트 반영
- **안전 검증 (`ros2_ws/src/nomi_agent/nomi_agent/safety_guard.py`)**:
  - 사용자 사전 승인 없이 실행 요청 시 "로봇 팔이 움직입니다. 작업대에 수건이 펴져 있고 팔 주변이 비어 있나요? 시작할까요?" 안전 확인 질문 자동 반환.
  - "수건 접지 마", "수건 개지 마" 등의 부정 표현 자동 감지 및 안전 취소(`NON_ACTION`) 처리.
- **자율 복합 작업 (`ros2_ws/src/nomi_agent/nomi_agent/llm_client.py`)**:
  - 사용자가 `"책상에 가서 수건 접어줘"`라고 지시하면 다음과 같이 순차 플랜 자동 생성 및 실행:
    주행 도구 호출(navigate_to(책상)) → 도착 확인 → 수건 접기(fold_towel) → 상태 확인 및 최종 보고
  - 주행 실패 시 수건 접기를 시도하지 않도록 방어 로직 반영.

---

## 3. 테스트 및 검증 결과

1. **단위 테스트 (100% 통과)**
   - `ros2_ws/src/nomi_agent/tests/`: **총 89개 테스트 전원 통과**
   - 수건 접기 전용 테스트 슈트(`test_towel_folding.py`) 13개 케이스 통과 (토큰 인증, 스킬 호출, 에러 핸들링, 비상 정지 연동, SafetyGuard 규칙).
2. **패키지 빌드**
   - ROS 2 Jazzy 환경에서 `colcon build --packages-select nomi_agent --symlink-install` 성공.
3. **실제 토픽 테스트**
   - `/nomi/command`로 `"수건 접지 마"` 발행 시 SafetyGuard에서 정확히 인지하여 `알겠습니다. 수건 접기 동작은 수행하지 않겠습니다.` 정상 응답 확인.

---

## 4. 팀원 테스트 및 실행 가이드 (방안 A 기준)

### 1) [노트북] 서비스 실행 (GPU 호스트)
노트북에서 Ollama 외부 접속 허용 및 수건 접기 API 서버를 구동합니다.
```bash
# 1. Ollama 외부 접속 허용 실행 (기본 포트: 11434)
OLLAMA_HOST=0.0.0.0 ollama serve

# 2. 수건 접기 API 서버 실행 (포트: 8100)
export TOWEL_API_TOKEN="노트북에서_발급한_시크릿키"
uv run --project ~/lerobot python ~/so101-bimanual-teleop/towel_fold_api.py --host 0.0.0.0
```

### 2) [AMR / 라즈베리파이] 환경변수 설정 및 실행
AMR 쪽에서는 노트북의 IP를 바라보도록 환경변수를 설정하고 가벼운 에이전트 노드와 주행 프로그램을 켭니다.
```bash
# 환경변수 지정 (노트북 IP 지정)
export OLLAMA_HOST="http://<노트북_IP>:11434"
export TOWEL_API_URL="http://<노트북_IP>:8100"
export TOWEL_API_TOKEN="노트북에서_발급한_시크릿키"

# 1. 웹 제어 및 지도 서버 실행
source /opt/ros/jazzy/setup.bash
cd ~/dapier_project11/XLeRobot/web_control/server
python3 main.py

# 2. NOMI 에이전트 노드 실행
source /opt/ros/jazzy/setup.bash
source ~/dapier_project11/ros2_ws/install/setup.bash
ros2 run nomi_agent agent
```

### 3) 지원되는 주요 발화 예시
- **단일 작업**: `"수건 접어줘"`, `"수건 개어줘"`, `"수건 접는 상태 확인해줘"`, `"수건 접기 멈춰"`
- **복합 작업**: `"책상으로 가서 수건 접어줘"`, `"소파로 이동해서 수건 개어줘"`
- **비상 정지**: `"멈춰"`, `"작업 취소"`, `"그만"` (주행과 팔 동작 동시 정지)
