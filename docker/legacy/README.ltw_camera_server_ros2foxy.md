# LTW Camera Server (ROS 2 Foxy)

## What This Container Does

Unitree G1의 Orin NX(PC2)에서 실행되는 컨테이너로, 호스트의
`videohub_pc4`가 ROS 2 topic `/frontvideostream`으로 publish 중인 정면
카메라(D435i) JPEG 프레임을 rclpy로 subscribe해서, 재인코딩 없이 그대로
msgpack으로 감싸 ZMQ PUB 5555 포트로 내보낸다. 그 이상의 역할은 없다.

```
PC1 (D435i) ─ROS2─▶ videohub_pc4 (PC2 = Orin NX, root, systemd)
                         │ publish
                         ▼
               ROS2 topic: /frontvideostream
               Type: unitree_go/msg/Go2FrontVideoData
               Domain: 0, QoS: RELIABLE + VOLATILE
                         │ subscribe
                         ▼
          ltw-camera-server:0.4-foxy (PC2, --network host, 이 컨테이너)
                         │ msg.video720p (raw JPEG bytes 그대로)
                         │ msgpack pack: {"timestamps":{"ego_view":ts},
                         │                "images":{"ego_view":jpeg_bytes}}
                         ▼
               ZMQ PUB tcp://*:5555
                         │ ethernet
                         ▼
               DGX Spark: run_data_exporter.py
               ComposedCameraClientSensor(server_ip, port)
                         │
                         ▼
               LeRobot dataset (parquet + mp4)
```

## Architecture

- **Physical**: PC1(D435i) → PC2(Orin NX, videohub_pc4) → 이 컨테이너(PC2 위,
  Docker) → 이더넷 → DGX Spark(워크스테이션).
- **Container internal**: rclpy subscriber 1개 노드(`camera_subscriber.py`)가
  전부다. ROS 2 Foxy + apt CycloneDDS 0.7.x(호스트 매칭) + unitree_go
  메시지 패키지(컨테이너 안에서 colcon build)로 구성.
- **Files**: 아래 File Structure 참조.

## Prerequisites

- Unitree G1 로봇이 켜져 있고 `videohub_pc4`가 실행 중 (systemd, 자동 시작)
- Orin NX에 SSH 접근 가능 (`unitree@192.168.123.164`)
- Orin NX에 Docker 설치됨 (v20+)
- DGX Spark ↔ Orin NX가 같은 서브넷 (`192.168.123.0/24`)

## File Structure

```
docker/
├── Dockerfile.ltw_camera_server_ros2foxy   # 이미지 정의
├── run_ltw_camera_server_ros2foxy.sh       # 실행 스크립트
├── config/
│   └── cyclonedds.xml                      # CycloneDDS 설정 (호스트 복사본)
└── src/
    └── camera_subscriber.py                # 유일한 파이썬 로직
```

## Setup Steps

### Step 1: Files 전송 (DGX Spark → Orin NX)

```bash
# 준비
ssh unitree@192.168.123.164 \
  "mkdir -p ~/tw_gearsonic/GR00T-WholeBodyControl/docker/config \
            ~/tw_gearsonic/GR00T-WholeBodyControl/docker/src"

# Dockerfile
scp /home/edgexpert00/GR00T-WholeBodyControl/docker/Dockerfile.ltw_camera_server_ros2foxy \
    unitree@192.168.123.164:~/tw_gearsonic/GR00T-WholeBodyControl/docker/

# run script
scp /home/edgexpert00/GR00T-WholeBodyControl/docker/run_ltw_camera_server_ros2foxy.sh \
    unitree@192.168.123.164:~/tw_gearsonic/GR00T-WholeBodyControl/docker/

# CycloneDDS XML
scp /home/edgexpert00/GR00T-WholeBodyControl/docker/config/cyclonedds.xml \
    unitree@192.168.123.164:~/tw_gearsonic/GR00T-WholeBodyControl/docker/config/

# subscriber
scp /home/edgexpert00/GR00T-WholeBodyControl/docker/src/camera_subscriber.py \
    unitree@192.168.123.164:~/tw_gearsonic/GR00T-WholeBodyControl/docker/src/

# 실행 권한
ssh unitree@192.168.123.164 \
  "chmod +x ~/tw_gearsonic/GR00T-WholeBodyControl/docker/run_ltw_camera_server_ros2foxy.sh"
```

### Step 2: Sanity Check (Orin NX)

```bash
ssh unitree@192.168.123.164
cd ~/tw_gearsonic/GR00T-WholeBodyControl

# 파일 확인
ls -la docker/config/cyclonedds.xml
ls -la docker/src/camera_subscriber.py
wc -l docker/Dockerfile.ltw_camera_server_ros2foxy

# base image pull 가능 확인
docker pull nvcr.io/nvidia/l4t-jetpack:r35.3.1

# topic 살아있는지 재확인
source /opt/ros/foxy/setup.bash
ros2 topic info /frontvideostream
```

### Step 3: Build

```bash
cd ~/tw_gearsonic/GR00T-WholeBodyControl
docker build \
  -f docker/Dockerfile.ltw_camera_server_ros2foxy \
  -t ltw-camera-server:0.4-foxy \
  docker/
```

주의: build context가 `docker/` 디렉토리다 (`src/`, `config/`에 접근하기
위해). Dockerfile의 `COPY`는 `src/camera_subscriber.py`,
`config/cyclonedds.xml` 상대 경로를 쓴다.

예상 빌드 시간: 5~10분 (인터넷 속도 의존, 첫 빌드는 layer 캐시 없음).

### Step 4: Run

```bash
~/tw_gearsonic/GR00T-WholeBodyControl/docker/run_ltw_camera_server_ros2foxy.sh
```

기대 출력:

```
[ltw-camera-server] Starting container...
[ltw-camera-server]   image     : ltw-camera-server:0.4-foxy
[ltw-camera-server]   container : ltw-camera-server
[ltw-camera-server]   port      : 5555 (ZMQ PUB)

[ltw_camera_subscriber] ZMQ PUB bound to tcp://*:5555
[ltw_camera_subscriber] rclpy initialized
[ltw_camera_subscriber] Subscribed to /frontvideostream (QoS: RELIABLE, field=video720p)
[ltw_camera_subscriber] Waiting for first frame...
[ltw_camera_subscriber] fps: 30.1 (bytes/frame avg: 152340)
[ltw_camera_subscriber] fps: 30.0 (bytes/frame avg: 148772)
...
```

fps 로그가 안 나오면 → Troubleshooting 섹션 참조.

### Step 5: 검증 (Workstation → 우리 컨테이너)

**5a. Camera viewer로 1차 검증** (가장 빠름):

DGX Spark에서:

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 \
    --camera-port 5555
```

OpenCV 창에 로봇 정면 카메라 이미지가 실시간으로 뜨면 성공.

**5b. 전체 데이터 수집 파이프라인**:

```bash
python gear_sonic/scripts/launch_data_collection.py \
    --camera-host 192.168.123.164 \
    --task-prompt "pick up the cup"
```

## Troubleshooting

### 증상 1: fps 로그가 안 나옴

원인 후보:
- rclpy discovery 실패
- QoS mismatch
- videohub_pc4 죽음

진단:

```bash
# 다른 SSH 세션에서
docker exec -it ltw-camera-server bash
source /opt/ros/foxy/setup.bash
source /opt/unitree_ros2_ws/install/setup.bash
ros2 topic list                           # /frontvideostream 보이는가?
ros2 topic info /frontvideostream         # Publisher count > 0인가?
ros2 topic echo /frontvideostream --once  # 프레임 오는가?
```

### 증상 2: workstation에서 "no frames" 또는 connection refused

```bash
# Orin NX에서 5555 실제 listen 중인지
ss -tlnp | grep 5555

# workstation에서 connectivity 확인
nc -zv 192.168.123.164 5555

# firewall 확인 (Orin NX, 조회만)
sudo iptables -L | head
```

### 증상 3: workstation에서 이미지가 깨져서 뜸

payload format 문제. 컨테이너 로그의 bytes/frame 값 확인 — 정상이면
100k~300k bytes 범위. `camera_subscriber.py`에서 `msgpack.packb(...,
use_bin_type=True)`가 유지되고 있는지 확인.

## Design Decisions Log

- Base image: `l4t-base` r35.3.1 → 없음, `l4t-jetpack` r35.3.1 채택 (Ubuntu
  20.04/focal 매칭 + Jetson 런타임 레이어 포함).
- CycloneDDS: 소스빌드(0.3-ros2에서 시도) → apt(`ros-foxy-rmw-cyclonedds-cpp`,
  0.7.x)로 대체, 호스트와 정확히 매칭.
- unitree_sdk2py: raw DDS(0.2-dds에서 시도, 콜백 자체가 안 불림) → rclpy로
  전환, ROS 2 RMW encapsulation을 그대로 이해하는 쪽을 선택.
- gear_sonic: 재사용 시도(0.3-ros2) → 직접 구현(`camera_subscriber.py`).
  Python 3.10 문법이 이 컨테이너의 Python 3.8(Foxy native)과 안 맞음.
- Payload: raw TCP → ZMQ msgpack, 워크스테이션 `run_data_exporter.py`의
  `ComposedCameraClientSensor`가 기대하는 포맷에 맞춤.

## Zero-Impact Guarantee

이 컨테이너가 호스트에 남기는 흔적:
- Docker 이미지 하나 (`ltw-camera-server:0.4-foxy`, 약 3~4GB)

그 외에는 아무것도 없다. 컨테이너는 `--rm`으로 실행되어 종료 시 즉시
삭제되고, 호스트 파일 시스템/시스템 서비스/네트워크 설정에 어떤 수정도
가하지 않는다. `videohub_pc4`, `master_service.service` 등 호스트 서비스는
정지/재시작을 시도하지 않으며, `--privileged`/`--device`/`/dev` 마운트도
전혀 쓰지 않는다.

## Future Work

- Wrist cameras(D405) 추가: `--record-wrist-cameras` 지원
  - USB device passthrough 필요 (`--device`, 아마 `--privileged`도 필요)
  - subscriber에 `left_wrist`, `right_wrist` mount position 추가
- Multi-resolution 동시 publish: `video720p`, `video360p` 함께
- Health check endpoint
- (v5, 0.8-foxy-videoclient) `Dockerfile.ltw_camera_server_ros2foxy_v5`
  섹션 4의 `setuptools==59.6.0` force-reinstall은 v5에서 죽은 코드로
  확인됨 — 뒤따르는 cyclonedds(4.5), unitree_sdk2_python(4.6) 빌드가
  둘 다 `pyproject.toml` 기반 PEP 517 격리 빌드라 이 pin의 영향을
  받지 않음(GitHub에서 두 pyproject.toml 실측 확인). 빌드를 깨뜨리진
  않으니 급하지 않음 — 다음에 이 Dockerfile 건드릴 때 정리.
