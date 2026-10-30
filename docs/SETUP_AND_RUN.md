# Ubuntu 24.04 설치 및 실행

새 Ubuntu 24.04 컴퓨터에 저장소를 Clone하는 절차입니다. 명령은 현재 저장소와 package.xml/requirements에 맞췄습니다. 새 컴퓨터에서 패키지 다운로드와 전체 실행을 이 작업이 검증한 것은 아닙니다.

## 1. Git Clone 및 런타임 경로

GitHub에서 저장소를 만든 뒤 주소를 사용합니다.

~~~
cd ~
git clone <YOUR_REPOSITORY_URL> NOMI_GITHUB_READY
~~~

현재 코드가 ~/XLeRobot과 ~/ros2_ws 경로를 기대하므로 새 계정에서만 링크를 만듭니다. 해당 경로가 이미 있으면 덮어쓰지 말고 먼저 비교하세요.

~~~
test ! -e ~/XLeRobot && ln -s ~/NOMI_GITHUB_READY/XLeRobot ~/XLeRobot
test ! -e ~/ros2_ws && ln -s ~/NOMI_GITHUB_READY/ros2_ws ~/ros2_ws
~~~

## 2. Ubuntu와 ROS 2 Jazzy

ROS 2 Jazzy를 설치한 뒤 환경을 확인합니다.

~~~
source /opt/ros/jazzy/setup.bash
printenv ROS_DISTRO
~~~

출력이 jazzy인지 확인합니다. 기본 도구는 colcon, rosdep, Python venv와 build-essential입니다. ROS가 이미 설치되어 있으면 재설치하지 않습니다.

rosdep이 처음 설정되는 컴퓨터에서만 init을 실행합니다.

~~~
sudo apt update
sudo apt install python3-venv python3-pip python3-colcon-common-extensions python3-rosdep build-essential
sudo rosdep init
rosdep update
source /opt/ros/jazzy/setup.bash
cd ~/ros2_ws
rosdep install --from-paths src --ignore-src --rosdistro jazzy -r -y
~~~

rosdep init이 이미 설정된 시스템에서는 해당 명령을 건너뜁니다.

## 3. Python 3.12 가상환경

ROS의 rclpy를 사용할 수 있도록 시스템 site packages를 포함한 가상환경을 만듭니다.

~~~
python3 --version
python3 -m venv --system-site-packages ~/venvs/xlerobot_mujoco
source ~/venvs/xlerobot_mujoco/bin/activate
python -m pip install --upgrade pip
python -m pip install -r ~/XLeRobot/simulation/mujoco/requirements.txt
python -m pip install -r ~/XLeRobot/web_control/server/requirements.txt
python -m pip install -e ~/ros2_ws/src/nomi_agent
~~~

두 requirements 파일 사이의 resolver 결과가 다른 컴퓨터에서도 같은지 검증하지 않았습니다. 설치 중 충돌이 나면 로그를 보존하고 ROS Python 패키지를 무작정 재설치하지 마세요.

~~~
source /opt/ros/jazzy/setup.bash
source ~/venvs/xlerobot_mujoco/bin/activate
python -c "import rclpy, mujoco, zmq; print('imports OK')"
~~~

## 4. ROS 패키지 빌드

~~~
source /opt/ros/jazzy/setup.bash
source ~/venvs/xlerobot_mujoco/bin/activate
cd ~/ros2_ws
colcon build --packages-select nomi_agent nomi_xlerobot_sim nomi_rviz_bridge --symlink-install
source ~/ros2_ws/install/setup.bash
ros2 pkg list | grep -E 'nomi_agent|nomi_xlerobot_sim|nomi_rviz_bridge'
~~~

## 5. Ollama 및 Qwen3.5 9B

Ollama 서비스와 모델은 별도 설치·다운로드해야 합니다.

~~~
ollama list
ollama show qwen3.5:9b
~~~

모델이 없으면 다운로드합니다.

~~~
ollama pull qwen3.5:9b
~~~

환경 예시를 사용자 전용 파일로 복사하고 셸에 로드합니다.

~~~
cd ~/NOMI_GITHUB_READY
cp .env.example .env
set -a
source .env
set +a
~~~

NOMI_MODEL_PROFILE=qwen35_single, OLLAMA_HOST=http://127.0.0.1:11434, ROS_DOMAIN_ID=30을 확인합니다. .env는 Git에서 제외됩니다. Qwen3.5 Ollama 프로필에는 API 키가 필요하지 않습니다.

## 6. 웹 프런트엔드

Vite 7은 Node 20.19 이상 또는 22.12 이상이 필요합니다. Node 22와 pnpm을 준비합니다.

~~~
node --version
corepack enable
corepack prepare pnpm@12.5.1 --activate
cd ~/XLeRobot/web_control/client
pnpm install --frozen-lockfile
pnpm run build
~~~

클라이언트에는 npm과 pnpm lockfile이 함께 있습니다. 개발 서버 실행은 pnpm을 사용합니다. 새 컴퓨터에서 package registry 다운로드가 필요합니다.

## 7. 실행 순서

각 항목을 별도 터미널에서 실행합니다. ROS_DOMAIN_ID=30을 모든 ROS 터미널에서 동일하게 사용합니다.

### 터미널 1 — Ollama LLM 서버 및 시뮬레이터 / 로봇

~~~
# Ollama 백그라운드 서비스 기동 (미기동 시)
ollama serve

# 시뮬레이터 (MuJoCo 또는 AprilTag 비전 시뮬레이터 또는 실제 로봇)
# A) 실제 AMR 로봇 구동 시:
# export ROS_DOMAIN_ID=30 && ros2 launch amr_bringup autonav.launch.py
# B) AprilTag 비전 인터랙티브 시뮬레이터 구동 시:
# export ROS_DOMAIN_ID=30 && python3 ~/ros2_amr_ws/tools/sim_docking_interactive.py --port 8090
~~~

### 터미널 2 — NOMI Agent (LLM 자연어 에이전트)

~~~
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
source ~/venvs/xlerobot_mujoco/bin/activate
export ROS_DOMAIN_ID=30
export NOMI_MODEL_PROFILE=qwen35_single
export OLLAMA_HOST=http://127.0.0.1:11434
python3 ~/ros2_ws/install/nomi_agent/lib/nomi_agent/agent
~~~

### 터미널 3 — 웹 백엔드 (FastAPI + Socket.IO + ROS 2 Bridge)

~~~
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
source ~/venvs/xlerobot_mujoco/bin/activate
cd ~/XLeRobot/web_control/server
export ROS_DOMAIN_ID=30
export UI_HOST=0.0.0.0
export UI_PORT=8000
python main.py
~~~

NOMI_MUJOCO_NAV_ENABLED=1은 MuJoCo 테스트용입니다. 실물 로봇 연결에 이 설정을 그대로 쓰지 마세요.

### 터미널 4 — Vite 프런트엔드

~~~
cd ~/XLeRobot/web_control/client
export VITE_SERVER_HOST=127.0.0.1
export VITE_SERVER_PORT=8000
export VITE_SERVER_PROTOCOL=http
pnpm run dev
~~~

브라우저 주소는 http://localhost:5173 입니다.

### 선택 — RViz

Host 실행 중, 같은 ROS domain에서 별도 실행합니다.

~~~
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=222
ros2 launch nomi_xlerobot_sim xlerobot_state.launch.py
~~~

이 launch는 RViz/상태 브리지용이며 MuJoCo Host를 시작하지 않습니다.

### 비이동 Mock 개발 모드

비이동 채팅 평가에는 MockRobot을 사용하는 스크립트를 실행할 수 있습니다.

~~~
bash ~/XLeRobot/web_control/scripts/start_nomi_mock_dev.sh
~~~

Mock과 full simulation 모드는 같은 포트/ROS domain에서 동시에 실행하지 마세요. 스크립트는 기존 프로세스를 발견하면 종료하지 않고 중단합니다.

## 8. 연결 확인

~~~
export ROS_DOMAIN_ID=222
ros2 node list
ros2 topic list | grep -E '^/nomi/'
ros2 topic echo /nomi/xlerobot/state
~~~

웹 UI에서 NOMI 브리지와 모델 프로필 확인 응답을 확인합니다. 서버는 8000, 프런트엔드는 5173, MuJoCo ZMQ 기본 포트는 5555/5556입니다.

## 9. 문제 해결

- rclpy import 실패: ROS Jazzy source 순서와 system-site-packages 가상환경 여부를 확인합니다.
- Agent 중복: ros2 node list에서 /nomi_agent를 확인합니다. 기존 프로세스를 무단 종료하지 마세요.
- 모델 연결 실패: Ollama, OLLAMA_HOST, qwen3.5:9b 설치 상태를 확인합니다.
- 웹 연결 실패: 8000/5173 포트와 VITE_SERVER_HOST를 확인합니다.
- 이동 버튼 비활성화: Host 상태, fresh 센서/위치, 목적지 및 안전 조건을 확인합니다. 안전 검사를 우회하지 마세요.
- 빌드 실패: ROS domain, package.xml 의존성, colcon 로그를 확인합니다.

실물 로봇, 암, 그리퍼 동작은 이 개발 절차의 검증 범위가 아닙니다.
