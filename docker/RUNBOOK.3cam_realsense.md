# 3-cam RealSense(RSUSB) 데이터 수집 Runbook

D435i(머리) + D405×2(손목) 세 대를 **모두 librealsense RSUSB로 직결**해 VLA 파인튜닝 데이터를 수집한다.
(이미지 `ltw-camera-server:1.1-foxy-3cam` = v8)

> ## ⚠️ 단 하나의 핵심 규칙
> **녹화(exporter)는 맨 마지막에, 카메라 서버가 안정화된 것을 확인한 뒤에만 시작한다.**
> 터미널1 로그에서 `left_wrist=30.0Hz`가 `★ WEDGE` 없이 **연속 3~5회** 뜬 걸 확인하고 녹화를 건다.
> (2026-07-22: 안정화 전 녹화 → 앞 8초 손목 정지로 FAIL. 안정화 후 녹화 → ALL PASS. **차이는 이 타이밍 하나뿐이었다.**)

---

## 전체 파이프라인

```
        WORKSTATION (DGX Spark, offboard)                              ROBOT (Unitree G1 Orin NX, onboard)
┌───────────────────────┐  ┌────────────────────────┐    ┌──────────────────────────────────────────────────────────┐
│  C++ deploy            │  │  pico_manager          │    │  물리 카메라                                                 │
│  (zmq_output_handler)  │  │  _thread_server.py     │    │   ┌─────────┐   ┌─────────┐   ┌─────────┐                    │
│  port 5557             │  │  port 5556             │    │   │ D435i   │   │ D405 #L │   │ D405 #R │                    │
│  topics: g1_debug,     │  │  topic: pose           │    │   │ (머리)  │   │ (왼손목)│   │(오른손목)│                    │
│          robot_config  │  │  (SMPL body params)    │    │   └────┬────┘   └────┬────┘   └────┬────┘                    │
└───────────┬────────────┘  └───────────┬────────────┘    │        │ libusb      │ libusb      │ libusb                 │
            │                           │                 │        │ (RSUSB) ★커널 uvcvideo 우회 → XU 컨트롤 통과      │
            │                           │                 │        ▼             ▼             ▼                        │
            │                           │                 │  ┌────────────────────────────────────────────────────┐    │
            │                           │                 │  │  videohub_pc4 는 정지시키지 않음 — RSUSB로 공존.       │   │
            │                           │                 │  │  단 불안정(3대 RSUSB면 수 분 뒤 밀려날 수 있음). 밀려도  │   │
            │                           │                 │  │  파이프라인 정상, 복구는 재부팅. ★videohub 수동기동 금지 │   │
            │                           │                 │  └────────────────────────────────────────────────────┘    │
            │                           │                 │  ┌────────────────────────────────────────────────────┐    │
            │                           │                 │  │  ltw-camera-server:1.1-foxy-3cam  (Docker, v8)       │   │
            │                           │                 │  │  camera_forwarder_3cam.py (bind-mount, 재빌드 불필요)│    │
            │                           │                 │  │  pyrealsense2 = librealsense v2.55.1 소스빌드         │   │
            │                           │                 │  │       (-DFORCE_RSUSB_BACKEND=true)                  │    │
            │                           │                 │  │   ┌──────────────┐  ┌───────────┐  ┌───────────┐    │   │
            │                           │                 │  │   │ Head 스레드  │  │ Left 스레드│  │Right 스레드│    │  │
            │                           │                 │  │   │ rs.pipeline  │  │rs.pipeline│  │rs.pipeline│    │   │
            │                           │                 │  │   │ 346122071399 │  │260322270228│ │260422272337│    │  │
            │                           │                 │  │   │ 640x480 bgr8 │  │ bgr8       │  │ bgr8      │    │   │
            │                           │                 │  │   │ ≈29Hz        │  │ 30Hz       │  │ 30Hz      │    │   │
            │                           │                 │  │   │ =퍼블리시클럭│  │→JPEG(최신) │  │→JPEG(최신)│    │   │
            │                           │                 │  │   │  ego_view    │  │ left_wrist │  │right_wrist│    │   │
            │                           │                 │  │   └──────┬───────┘  └─────┬─────┘  └─────┬─────┘    │   │
            │                           │                 │  │          └─ 머리 새 프레임 도착 시 병합·발행 ─┘         │  │
            │                           │                 │  │                      ▼                              │   │
            │                           │                 │  │        ZMQ PUB  port 5555                           │   │
            │                           │                 │  │        msgpack {timestamps, images:                 │   │
            │                           │                 │  │          {ego_view, left_wrist, right_wrist} = JPEG}│   │
            │                           │                 │  └──────────────────────┬──────────────────────────────┘  │
            │                           │                 └─────────────────────────┼─────────────────────────────────┘
            │                           │        (192.168.123.x  gigabit)           │
            └───────────────┬───────────┴───────────────────────────────────────────┘
                            │  구독: 5557(proprio) + 5556(pose) + 5555(3-cam)
                   ┌────────▼──────────────────────────────┐
                   │  run_data_exporter.py  (DGX Spark)     │
                   │   --camera-triggered  (머리≈29Hz 클럭)  │
                   │   --record-wrist-cameras  (3키 요구)     │
                   │   --use-nvenc  --camera-decode-reduce 1 │
                   │   --dataset-fps 25                       │
                   │   LeRobot v2.1: data/*.parquet +        │
                   │     videos/observation.images.{ego_view,│
                   │       left_wrist, right_wrist}/          │
                   │   → verify_dataset.py ✅ ALL PASS        │
                   └─────────────────────────────────────────┘
```

**시리얼 (카메라 서버에 넣는 값 = librealsense 값):**

| 카메라 | librealsense 시리얼 | 플래그 |
|---|---|---|
| 머리 D435i | `346122071399` | `HEAD_SERIAL` |
| 왼손목 D405 | `260322270228` | (자동배정 = left) |
| 오른손목 D405 | `260422272337` | (자동배정 = right) |

> ※ `disable_d405_autosuspend.sh`가 찍는 `2553...`은 **sysfs 값**이라 다르다 — 카메라 서버에 **넣지 않는다.**

---

## Step 1. 사전점검 (재부팅 후)

```bash
# (DGX에서 Orin 접속 / 필요시 Orin wifi)
ping -c 3 192.168.123.164
ssh unitree@192.168.123.164            # 비밀번호는 별도 전달
# sudo nmcli device wifi connect "delight" password "shy80@kist"   # 네트워크 바꿀 때만(공유주의)

# --- 이하 Orin에서 ---
cd ~/tw_gearsonic/GR00T-WholeBodyControl
git pull
git log --oneline -1                   # 기대: f708a19 (또는 그 이후 최신)

# 1) videohub 자동 기동
pgrep -a videohub_pc4                  # → 2557 .../videohub_pc4 /dev/video4

# 2) 파일시스템 이상 (전원차단 흔적)
sudo dmesg | grep -iE "ext4|EXT4|recovery|corrupt" | head
#    → "ordered data mode"/"re-mounted"만 정상. corrupt/error 뜨면 중단.

# 3) docker 이미지 (★가장 중요 — 없으면 40분 재빌드)
sudo docker images | grep ltw-camera-server
#    → 1.1-foxy-3cam (v8, 사용) + 1.0-foxy-3cam (v7, 롤백용) 둘 다 있어야 함

# 4) repo 정상
git status                             # → clean

# 5) pyrealsense 시리얼 확인
sudo ./docker/list_realsense_serials.sh
#    → D435I 346122071399 / D405 260322270228 / D405 260422272337
```

---

## Step 2. 실행 (순서 중요 — 녹화는 맨 마지막)

### 터미널 1 — 카메라 서버 (Orin NX)

```bash
cd ~/tw_gearsonic/GR00T-WholeBodyControl
sudo bash ./docker/disable_d405_autosuspend.sh     # 부팅마다 필요 (D405 autosuspend off)

WRIST_BACKEND=realsense HEAD_SERIAL=346122071399 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh

# (손목 좌/우가 반대면 시리얼 명시)
# LEFT_WRIST_SERIAL=260422272337 RIGHT_WRIST_SERIAL=260322270228 \
# WRIST_BACKEND=realsense HEAD_SERIAL=346122071399 \
#   sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

> ### 🚦 여기서 안정화 게이트 — 반드시 대기
> 서버 시작 직후 손목이 **startup wedge**로 잠깐 멈출 수 있다(`left_wrist wait_for_frames 실패` / `★ WEDGE`).
> RSUSB가 스스로 재시작해 복구하니, 아래 줄이 **연속 3~5회 깨끗하게** 뜰 때까지 기다린다:
> ```
> [3cam] content(new frames): ego_view=29.x  left_wrist=30.0Hz  right_wrist=30.0Hz
> ```
> ⛔ `left_wrist=0.0Hz` / `★ WEDGE` / `프레임 오래됨`이 보이면 아직이다. **녹화 시작 금지.**

### 터미널 5 — 뷰어 (DGX) ※ 녹화 전에 먼저 눈으로 확인

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 --camera-port 5555
```
- 3피드 모두 실시간 갱신되는지, 손목 **좌/우가 맞는지**(왼팔 움직여 `left_wrist` 확인) 확인.

### 터미널 2 — C++ deploy (DGX)

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
# → Y → Init done
```

### 터미널 3 — PICO teleop (DGX)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_teleop/bin/activate
export CYCLONEDDS_HOME=$HOME/.local/cyclonedds-c
export LD_LIBRARY_PATH=$CYCLONEDDS_HOME/lib:$LD_LIBRARY_PATH
python gear_sonic/scripts/pico_manager_thread_server.py --manager --vis_vr3pt --vis_smpl
```

### 터미널 4 — exporter (DGX) ★맨 마지막, 안정화 확인 후

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "test_260722_3" --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered \
    --record-wrist-cameras --camera-decode-reduce 1 --dataset-fps 25 \
    2>&1 | tee data_exporter_$(date +%Y%m%d_%H%M%S).log
```

**종료 순서(역순):** exporter(Ctrl+C, 에피소드 저장) → teleop → deploy → **카메라 서버(마지막)**.

---

## Step 3. 데이터셋 검사 (DGX)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/verify_dataset.py outputs/<이번_날짜폴더>
# ex) python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-11-09-30
```

**합격 기준:**
- `[4] 정지프레임`: 손목 정지율 **낮고(수 %)** 연속정지 **1초 미만** → ✅
  - 연속 1초+ 정지 = wedge → **FAIL. 그 에피소드 폐기.** (주로 안정화 전 녹화가 원인)
- `[1] 프레임 수 일치 / [2] 연속 / [3] dt 균일(0.05) / [5] NaN 없음 / [6] LeRobot 로딩 3카메라` 모두 ✅
- 맨 아래 `✅ 전부 통과 — VLA 파인튜닝 사용 가능` 이면 성공.

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| 시작 시 `left_wrist ★ WEDGE` | startup wedge. **RSUSB가 자동 복구**하니 30Hz 될 때까지 대기 후 녹화. |
| verify `[4]` 손목 FAIL | 안정화 전 녹화. 에피소드 폐기 후 게이트 지켜 재수집. |
| 손목 좌/우 뒤바뀜 | 터미널1을 `LEFT_WRIST_SERIAL`/`RIGHT_WRIST_SERIAL` 명시로 재실행. |
| videohub가 사라짐 | RSUSB 공존 불안정. 파이프라인엔 무해. 복구는 **재부팅**. videohub 수동 기동 금지. |
| docker 이미지 없음 | v8 재빌드 필요(~40분): `docker build -f docker/Dockerfile.ltw_camera_server_ros2foxy_v8 -t ltw-camera-server:1.1-foxy-3cam docker/` |
| 문제 발생 → 롤백 | v7 이미지(`1.0-foxy-3cam`) 사용: `./docker/run_ltw_camera_server_ros2foxy_v7.sh` (머리 videohub RPC 경로) |
