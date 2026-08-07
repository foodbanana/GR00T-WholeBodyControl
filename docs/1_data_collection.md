# 1. 데이터 수집 — DGX Spark ↔ Unitree G1

VLA 파인튜닝용 **LeRobot v2.1 데이터셋**을 PICO VR 텔레오퍼레이션으로 수집한다.
로봇 온보드(Orin NX)가 카메라를 발행하고, DGX Spark 가 조종·기록을 담당한다.

> 이 문서는 **실제로 실행한 명령어**를 기준선으로 한다. 설계 배경과 진단 도구까지
> 필요하면 `3cam-data-collection` 브랜치의 `data_collection.md`(635줄)를 참조:
> `git show origin/3cam-data-collection:data_collection.md`

**다음 단계** → [2. 데이터셋 전처리 & merge](2_dataset_preprocess_merge.md)

---

## 0. 구성

![데이터 수집 파이프라인 전체 구조](../media/data_collection_pipeline.svg)

| 항목 | 값 |
|---|---|
| 워크스테이션 | DGX Spark (offboard) — 리포 `~/GR00T-WholeBodyControl` |
| 로봇 온보드 | Unitree G1 + Orin NX — `192.168.123.164` (`unitree` / `123`), 리포 `~/tw_gearsonic/GR00T-WholeBodyControl` |
| 카메라 | 머리 1대(D435/D435i/D455 교체 운용) + 손목 D405 2대 |
| 산출물 | `outputs/<YYYY-MM-DD-HH-MM-SS>/` (LeRobot v2.1) |

> ### ★ v1·v2 학습 데이터는 전부 **head-only** 다 — 손목 카메라가 들어간 적이 없다
>
> 이 문서는 3-cam 구성을 기준선으로 쓰지만, **실제로 파인튜닝에 쓴 데이터셋
> (`raise_arm_banana_01`~`10`, `..._merged`, `..._merged_v2`)은 전부
> `observation.images.ego_view` 한 개뿐**이다 (2026-08-07 확인).
> 손목 D405 2대는 배선·wedge 문제로 수집 시점에 미연결이었다.
>
> 따라서 지금 상태를 그대로 재현하려면 **T1 에 `NO_WRISTS=1`, T4 에서 `--record-wrist-cameras` 생략**이
> 맞는 조합이다. 3-cam 절차(손목 시리얼·autosuspend·안정화 게이트)는 **손목을 다시 붙일 때** 쓴다.
> 손목을 추가하면 모달리티가 바뀌므로 **기존 체크포인트와 호환되지 않는다** — 처음부터 다시 학습해야 한다.

### 포트 맵

| 포트 | 방향 | 발행자 | 내용 |
|---|---|---|---|
| 5555 | Orin → DGX | `camera_forwarder_3cam.py` (Docker) | msgpack `{timestamps, images}`, JPEG 3장 |
| 5556 | DGX 내부 | `pico_manager_thread_server.py` | `pose`(SMPL), `planner`, `manager_state` |
| 5557 | DGX 내부 | C++ deploy `zmq_output_handler` | `g1_debug`(로봇 상태), `robot_config`(~2초 주기) |

`robot_config` 는 데이터셋 `meta/info.json` 의 `script_config` 로 들어간다.
**exporter 는 이 메시지를 받을 때까지 시작하지 않는다**(`--robot-config-timeout 0` = 무한 대기가 기본).

### 주요 파일

| 실행 위치 | 파일 | 역할 |
|---|---|---|
| Orin | [docker/src/camera_forwarder_3cam.py](../docker/src/camera_forwarder_3cam.py) | 카메라 서버 본체. **bind-mount 라 수정해도 이미지 재빌드 불필요** |
| Orin | [docker/run_ltw_camera_server_ros2foxy_v8.sh](../docker/run_ltw_camera_server_ros2foxy_v8.sh) | 위를 컨테이너로 띄우는 런처. 환경변수 해석 |
| Orin | [docker/list_realsense_serials.sh](../docker/list_realsense_serials.sh) | **실행 전 필수** — librealsense 시리얼 열거 |
| Orin | [docker/disable_d405_autosuspend.sh](../docker/disable_d405_autosuspend.sh) | **실행 전 필수** — D405 USB autosuspend 해제 |
| DGX | [gear_sonic/scripts/run_data_exporter.py](../gear_sonic/scripts/run_data_exporter.py) | 5555+5556+5557 구독 → 프레임 조립 → 데이터셋 기록 |
| DGX | [gear_sonic/scripts/pico_manager_thread_server.py](../gear_sonic/scripts/pico_manager_thread_server.py) | PICO teleop 서버. VR 버튼 → 모드 전환 / 녹화 토글 |
| DGX | [gear_sonic/scripts/run_camera_viewer.py](../gear_sonic/scripts/run_camera_viewer.py) | 카메라 피드 실시간 확인(디버깅) |
| DGX | [gear_sonic/scripts/verify_dataset.py](../gear_sonic/scripts/verify_dataset.py) | 수집 후 검증 |

---

## STEP 1 — 사전점검 (Orin, 재부팅 후 매번)

```bash
# DGX 에서 Orin 접속
ping -c 3 192.168.123.164
ssh unitree@192.168.123.164          # 비밀번호는 별도 전달

# --- 이하 Orin ---
# 인터넷이 필요하면 (git pull, 이미지 빌드)
sudo nmcli device wifi connect "delight" password "shy80@kist"

# 코드 최신화
cd ~/tw_gearsonic/GR00T-WholeBodyControl
git pull
git status                            # clean 이어야 함
git log --oneline -1                  # 예: f708a19

# ★ 가장 중요 — docker 이미지 생존 확인 (재빌드하면 40분)
sudo docker images | grep ltw-camera-server
#   1.1-foxy-3cam  ← v8, 실사용
#   1.0-foxy-3cam  ← v7, 롤백용. 지우지 말 것
```

> **이미지가 없으면 여기서 멈춘다.** [부록 A — 이미지 빌드](#부록-a--docker-이미지-빌드) 로 가서
> 먼저 빌드해야 이후 단계를 진행할 수 있다.

### 카메라 시리얼 실측 — ★ 매번 필수

```bash
sudo ./docker/list_realsense_serials.sh
```

출력 예:

```
HEAD_SERIAL=938422073271          # 머리 — D435, 2026-08-07 실기 시점 현행값
LEFT_WRIST_SERIAL=260322270228    # D405
RIGHT_WRIST_SERIAL=260422272337   # D405
```

**머리 카메라는 우리가 직접 교체하는 부품이다.** 교체하면 시리얼이 바뀌고, 바뀐 값을
터미널 1 명령의 `HEAD_SERIAL=` 에 직접 넣어야 한다. 자동 감지에 의존하지 않는다.

| 시점 | 머리 카메라 | 시리얼 |
|---|---|---|
| ~2026-07-26 | D435i | `346122071399` |
| 일시 | D455 | `046322250434` |
| **2026-07-27 ~ 현재** ✅ | **D435** | **`938422073271`** ← 2026-08-07 실기에 쓴 값 |

교체 이력이 이만큼 잦으므로 **문서의 값은 예시로만 보고 매번 실측한다.**

| 종류 | 출처 | 예시 | 용도 |
|---|---|---|---|
| **librealsense 시리얼** | `list_realsense_serials.sh` | `938422073271` | ✅ **명령어에 넣는 값** |
| sysfs(USB 디스크립터) 시리얼 | `disable_d405_autosuspend.sh` 출력 | `255323071827` | ❌ 카메라 서버에 넣지 말 것 |

같은 물리 카메라라도 두 값은 **다르다**(계층이 다르다). `disable_d405_autosuspend.sh` 가
출력하는 값을 그대로 넣는 실수가 흔하다 — 스크립트도 그 자리에서 경고를 찍는다.

> **시리얼을 잘못 넣으면** librealsense 가 "No device connected" 재시도 루프를 돌며
> USB 버스를 흔들어, **머리가 아예 안 뜨고 손목까지 ~3Hz 로 떨어진다.**
> 카메라 하드웨어 고장으로 오진하기 매우 쉽다.

---

## STEP 2 — 실행 (터미널 5개)

> ⚠️ **녹화(T4 exporter)는 맨 마지막에, 카메라 서버가 안정화된 것을 확인한 뒤에만 시작한다.**
> 안정화 전에 녹화하면 앞부분이 손목 정지 프레임으로 오염되어 검증에서 FAIL 난다.

### T1 — 카메라 서버 (Orin NX, SSH)

```bash
cd ~/tw_gearsonic/GR00T-WholeBodyControl

# D405 USB autosuspend 끄기 — 부팅/USB 재연결 때마다 매번
sudo bash ./docker/disable_d405_autosuspend.sh

# 3-cam 서버 기동 (HEAD_SERIAL 은 STEP 1 실측값)
WRIST_BACKEND=realsense HEAD_SERIAL=938422073271 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

**손목 좌/우가 뒤바뀌었으면** 시리얼을 명시해 재실행한다:

```bash
LEFT_WRIST_SERIAL=260422272337 RIGHT_WRIST_SERIAL=260322270228 \
WRIST_BACKEND=realsense HEAD_SERIAL=938422073271 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

<details>
<summary>autosuspend 를 왜 끄는가</summary>

이 Jetson(L4T r35.3.1)에서 D405 를 스트리밍하면 USB3 링크 전력관리(U1/U2 LPM) 때문에
수 초 뒤 스트림이 wedge 된다 (`uvcvideo: Failed to set UVC probe control: -32`).
`power/control` 을 `on` 으로 두면 이 전환이 사라진다. 이 값은 재부팅/USB 재연결 시
기본값(`auto`)으로 돌아가므로 **서버를 띄우기 전 매번** 실행해야 한다.
머리 카메라는 영향 없는, D405 특유의 문제다.
</details>

#### 🚦 안정화 게이트 — 여기서 반드시 대기

서버 시작 직후 손목이 startup wedge 로 잠깐 멈출 수 있다
(`left_wrist wait_for_frames 실패` / `★ WEDGE`). RSUSB 가 스스로 재시작해 복구하므로,
아래 줄이 **연속 3~5회 깨끗하게** 뜰 때까지 기다린다:

```
[3cam] content(new frames): ego_view=29.x  left_wrist=30.0Hz  right_wrist=30.0Hz
```

⛔ `left_wrist=0.0Hz` / `★ WEDGE` / `프레임 오래됨` 이 보이면 아직이다. **녹화 시작 금지.**

### T2 — C++ deploy (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
# → Y → Init done
```

### T3 — PICO teleoperation (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_teleop/bin/activate
export CYCLONEDDS_HOME=$HOME/.local/cyclonedds-c
export LD_LIBRARY_PATH=$CYCLONEDDS_HOME/lib:$LD_LIBRARY_PATH
python gear_sonic/scripts/pico_manager_thread_server.py --manager --vis_vr3pt --vis_smpl
```

### T5 — 카메라 뷰어 (DGX Spark) ※ T4 보다 **먼저** 확인한다

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 --camera-port 5555
```

확인할 것:
- 3개 피드가 **모두 실시간으로 갱신**되는가
- **손목 좌/우가 맞는가** — 왼팔을 움직여 `left_wrist` 타일이 반응하는지

(창 포커스에서 `R` = 녹화 토글, `Q` = 종료. 디버깅용이며 데이터셋과 무관하다.)
**확인이 끝나면 끈다** — 켜둔 채 녹화하면 DGX 코어를 exporter 와 나눠 쓴다.

### T4 — 데이터 exporter (DGX Spark) ★ 맨 마지막

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered \
    --record-wrist-cameras --camera-decode-reduce 1 --dataset-fps 25 \
    2>&1 | tee data_exporter_$(date +%Y%m%d_%H%M%S).log
```

> `--task-prompt` 는 **학습에 그대로 쓰이는 언어 태스크 문구**다. 실기 추론에서도
> 같은 문장을 줘야 하므로 `test_260722_2` 같은 임시값이 아니라 실제 태스크 설명을 넣는다.
> (v2 학습에 쓴 문장: `raise your right arm if you see a banana`)

**head-only 로 수집할 때** — T1 에 `NO_WRISTS=1` 을 주고, 여기서 `--record-wrist-cameras` 를 뺀다.

---

## STEP 3 — 텔레오퍼레이션 & 에피소드 녹화 (PICO VR)

exporter 는 켜져 있어도 바로 저장하지 않는다. **에피소드 단위로 조작자가 토글**한다.

### 모드 제어 (T3, pico_manager)

| 입력 | 동작 |
|---|---|
| **A + B + X + Y** | 정책 시작/정지 토글. `OFF` → `PLANNER`(**시작 시 VR 3점 캘리브레이션**) / 어느 모드에서든 → `OFF` |
| A + X | `POSE` ↔ `PLANNER` |
| B + Y | `POSE` ↔ `PLANNER_FROZEN_UPPER_BODY` |
| 왼쪽 스틱 클릭 | `PLANNER_VR_3PT` 진입/복귀 |
| 왼쪽 메뉴 버튼(홀드) | `POSE_PAUSE` |
| A + B / X + Y | 로코모션 모드 다음/이전 |

> **A+B+X+Y 로 시작할 때 캘리브레이션이 함께 수행된다.** 조작자는 이 순간
> zero-reference 자세로 서 있어야 한다.

### 에피소드 녹화 제어

| 입력 | 동작 |
|---|---|
| **왼쪽 grip + A** | 녹화 토글: `IDLE` → `RECORDING` → `NEED_TO_SAVE`(저장) → `IDLE` |
| **왼쪽 grip + B** | 현재 녹화 중인 에피소드 **폐기**(discard) |

둘 다 rising edge 처리라 누르고 있어도 1회만 발생한다. T4 에
`Started recording <N>` / `Stopping recording, preparing to save` /
`Saved episode and back to idle state` / `Discarded episode` 가 찍히고 TTS 로도 안내된다.

> 한 에피소드가 끝나면 자동으로 `IDLE` 로 돌아가고 같은 폴더에 다음 에피소드가 쌓인다.
> **exporter 를 껐다 켜면 새 데이터셋 폴더가 생긴다** — 한 세션은 한 폴더로 끝내는 게 좋다.

### 종료 절차 — 순서를 지킬 것, 로봇을 먼저 세운다

1. **PICO 에서 A+B+X+Y 를 한 번 더** → 스트림 `OFF`, **로봇 정지**
2. T4 exporter — `Ctrl+C` (녹화 중이면 저장 후 종료)
3. T3 teleop — `Ctrl+C`
4. T2 C++ deploy — `Ctrl+C`
5. T1 카메라 서버 — `Ctrl+C` (**맨 마지막**)

컨테이너는 `--rm` 이라 종료 시 흔적이 남지 않는다.

---

## STEP 4 — 데이터셋 검증 (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/verify_dataset.py outputs/2026-07-22-13-23-08
```

옵션: `--no-load`(LeRobot 로딩 스킵, 빠름), `--no-freeze`(정지 프레임 검사 스킵, 긴 에피소드에서 유용)
종료 코드: 전부 통과 = 0, 하나라도 실패 = 1.

| # | 검사 | 합격 기준 |
|---|---|---|
| 1 | 프레임 수 일치 | parquet 행 수 == 각 mp4 프레임 수 == `info.total_frames` |
| 2 | `frame_index` 연속성 | `0..N-1` |
| 3 | timestamp 시간축 | `frame_index/fps` 균일 그리드 (배속/어긋남 없음) |
| 4 | **정지/중복 프레임** | 손목 정지율 수 % 이내, **연속 정지 1초 미만**. 연속 1초 이상 = D405 wedge → **해당 에피소드 폐기** |
| 5 | 관절/액션 무결성 | NaN 없음, 길이 일치 |
| 6 | LeRobot 실제 로딩 | `ds[0]` 에 3카메라 + state + action, 비디오가 실제로 디코드됨(검정 아님) |

맨 아래 `✅ 전부 통과 — VLA 파인튜닝 사용 가능` 이면 성공.

> **검사 4번이 핵심이다.** 6번의 픽셀 평균 검사는 "얼어붙은 화면"도 통과시키므로,
> wedge 로 오염된 에피소드는 4번에서만 걸러진다.

---

## 산출물 구조

```
outputs/<YYYY-MM-DD-HH-MM-SS>/          # --dataset-name 미지정 시 실행 시각
├── data/chunk-000/episode_*.parquet    # proprio + pose + action
├── videos/chunk-000/
│   ├── observation.images.ego_view/episode_*.mp4
│   ├── observation.images.left_wrist/episode_*.mp4
│   └── observation.images.right_wrist/episode_*.mp4
└── meta/
    ├── info.json                       # fps, total_frames, script_config(=robot_config)
    ├── episodes.jsonl
    └── tasks.jsonl
```

1 프레임 = **{proprio(5557) 스냅샷 + pose(5556) 스냅샷 + 이미지 3장(5555)}**.
proprio/pose 는 카메라 프레임 도착 시점의 최신값으로 스냅샷된다.

---

## CLI 레퍼런스

### `run_data_exporter.py` (DGX)

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--task-prompt` | `demo` | 언어 태스크 문구. **학습·추론에 그대로 쓰인다** |
| `--dataset-name` | 자동(실행 시각) | 데이터셋 폴더명 |
| `--root-output-dir` | `outputs` | 산출물 루트 |
| `--camera-host` / `--camera-port` | `localhost` / `5555` | 카메라 서버(Orin) |
| `--sonic-zmq-port` | `5556` | pico_manager pose |
| `--state-zmq-port` | `5557` | C++ deploy 상태 + robot_config |
| `--record-wrist-cameras` | `False` | 손목 2대까지 기록. **페이로드에 3키가 다 있어야 함** |
| `--camera-triggered` | `False` | 카메라 프레임 도착을 클럭으로 사용 (아래 근거) |
| `--dataset-fps` | `30` | mp4/info.json 에 찍히는 fps. **25 로 쓴다** |
| `--use-nvenc` | `False` | GB10 하드웨어 NVENC. 소프트웨어 libx264 병목 해소 |
| `--camera-decode-reduce` | `2` | JPEG 1/N 해상도 디코드. **1 로 쓴다**(손목 화질 보존) |
| `--cv2-num-threads` | `1` | OpenCV 스레드 상한. 코어 경합 방지 |
| `--robot-config-timeout` | `0` | 0 = 무한 대기 |
| `--no-text-to-speech` | — | TTS 끄기 |

### `run_ltw_camera_server_ros2foxy_v8.sh` (Orin) — 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HEAD_SERIAL` | auto | 머리 librealsense 시리얼. **실측값 명시** |
| `HEAD_BACKEND` | `realsense` | `videohub` = v7 의 VideoClient RPC 경로로 폴백 |
| `HEAD_WIDTH`/`HEAD_HEIGHT`/`HEAD_FPS` | `640`/`480`/`30` | 머리 스트림 요청값 |
| `WRIST_BACKEND` | `v4l2` | **3-cam 에서는 `realsense`** (RSUSB 직결) |
| `WRIST_FPS` | `30` | |
| `LEFT_WRIST_SERIAL`/`RIGHT_WRIST_SERIAL` | auto | 좌/우가 뒤바뀔 때 명시 |
| `NO_WRISTS` | — | `1` 이면 head-only. DGX 에서 `--record-wrist-cameras` 를 뺀다 |
| `HEAD_AE_PRIORITY`/`WRIST_AE_PRIORITY` | 센서 기본 | `0` 이면 fps 고정(어두우면 노출 캡). 30Hz 미달 시 시도 |

---

## 설계 근거 (요약)

### 머리 카메라가 퍼블리시 클럭이다 (`--camera-triggered`)

카메라 서버는 loop 마다 발행하지 않는다. **머리에 새 프레임이 도착할 때만** 손목 최신
JPEG 2장을 묶어 5555 로 발행한다. exporter 도 "카메라 프레임 도착 = 1 데이터 프레임"으로 동작한다.

- 자유 실행 루프에서 생기는 **중복 프레임이 사라진다**
- 저장 레이트 = 카메라 레이트가 되고, timestamp 가 `frame_index / fps` 균일 그리드로 찍혀
  `verify_dataset.py` 의 시간축 검사(3번)를 통과한다

### 버퍼 — 머리 1칸, 손목 5칸 (head-anchored nearest timestamp)

![버퍼 구조와 head-anchored 프레임 매칭](../media/buffer_matching.svg)

카메라 3대는 **독립적으로** 프레임을 낸다(머리 ≈29Hz, 손목 30Hz). "세 대가 정확히
동시에 찍은 프레임" 같은 것은 없다. 머리 프레임 도착 시각을 anchor 로 삼아 각 손목
링버퍼에서 `|Δ|` 최소 프레임을 고른다.

- 손목도 1칸만 두면 평균 **반 프레임(≈16ms)** 어긋난다. 5칸이면 30Hz 에서 ≈166ms 커버
- 손목 버퍼가 비면 **그 회차는 통째로 건너뛴다**(반쪽 페이로드 금지) → `skip(empty buf)=N`
- 시각 비교는 `monotonic` 시계 (NTP 보정에 흔들리지 않게)
- 매칭 Δ 는 5초마다 `[sync]` 통계(mean/p50/p95/max)로 출력. p50/p95 는 **부호 있는** 값이라
  손목이 머리보다 앞서는지 뒤처지는지까지 보인다

> ⚠️ payload 의 `timestamps` 에는 **3대 모두 머리의 시각**이 들어간다(카메라별 실제 캡처
> 시각이 아니다). 기존 DGX 소비자 계약 유지를 위한 선택이고, 실제 per-camera 오프셋은
> T1 의 `[sync]` 로그로만 확인한다.

캡처 스레드는 **읽기만** 하고 인코딩은 별도 스레드가 "최신값 슬롯"에서 가져간다.
한 스레드에서 둘 다 하면 인코딩 중 드라이버 버퍼에 프레임이 쌓여 지연이 누적된다.
ZMQ 도 `SNDHWM=3`(기본 1000)으로 낮춰 **전 구간이 "최신 우선, 밀리면 버림"** 이다.

### `--dataset-fps 25` 인 이유

`dataset_fps` 는 mp4 와 `info.json` 에 **찍히는** fps 다. 실제 발행 레이트가 이보다 낮으면
같은 프레임 수를 더 짧은 시간에 재생하게 되어 **영상이 빠른 배속으로 재생된다.**
머리 실측이 ≈29Hz 이므로 드롭이 나도 안 내려가도록 마진을 두고 25 로 고정했다.

> **올릴 때는** 반드시 T1 의 실측 Hz 를 먼저 확인하고 그보다 확실히 낮은 값으로 잡을 것.

---

## 부록 A — Docker 이미지 빌드

`docker/` 에 Dockerfile 이 여러 개 있지만 **실제로 쓰는 것은 v8 하나뿐이다.**

| Dockerfile | 태그 | 상태 |
|---|---|---|
| [Dockerfile.ltw_camera_server_ros2foxy_v8](../docker/Dockerfile.ltw_camera_server_ros2foxy_v8) | `1.1-foxy-3cam` | ✅ **현재 사용.** 머리·손목 모두 librealsense 직결 |
| [Dockerfile.ltw_camera_server_ros2foxy_v7](../docker/Dockerfile.ltw_camera_server_ros2foxy_v7) | `1.0-foxy-3cam` | 🔙 롤백용(머리를 videohub RPC). **이미지를 지우지 말 것** |

**Orin 에서 직접 빌드한다.** 베이스가 Jetson 전용 L4T 이미지라 같은 aarch64 + L4T
환경에서만 유효하다.

```bash
# 전제: 디스크 5GB 이상, 인터넷 필요 (베이스 이미지 + CycloneDDS + librealsense + unitree_sdk2_python)
df -h / ; ping -c 2 github.com

cd ~/tw_gearsonic/GR00T-WholeBodyControl
sudo docker build -f docker/Dockerfile.ltw_camera_server_ros2foxy_v8 \
    -t ltw-camera-server:1.1-foxy-3cam \
    docker/
```

> ★ **마지막 인자 `docker/` 가 빌드 컨텍스트다.** 리포 루트(`.`)를 주면 Dockerfile 의
> `COPY src/camera_forwarder_3cam.py` 가 경로를 못 찾아 실패한다.
> ★ **태그를 v7 과 다르게 유지할 것** — `1.0-foxy-3cam` 을 덮어쓰면 롤백 자산이 사라진다.

| 상황 | 소요 |
|---|---|
| v7 레이어 캐시 있음 | 20~40분 (librealsense 소스빌드부터 재실행) |
| 새 로봇 / 캐시 없음 | 1~2시간 (CycloneDDS 소스빌드 30~60분 추가) |

| 빌드 실패 | 조치 |
|---|---|
| `c++: fatal error: Killed` | 메모리 부족 → Dockerfile 의 `RUN make -j"$(nproc)"` 를 `-j4` 로 |
| `pyrealsense2 본체 .so 를 찾지 못했다` | 그 위 "빌드 트리에 생성된 Python 모듈" 출력 확인 |
| 디스크 부족 | `sudo docker image prune` 후 재시도 |

빌드 후 확인 — 두 번째 명령이 시리얼을 출력하면 이미지 안의 pyrealsense2 가 정상이다:

```bash
sudo docker images | grep ltw-camera-server
sudo ./docker/list_realsense_serials.sh
```

### 실행 경로 — 컨테이너에서 도는 코드는 항상 호스트 파일이다

```
docker/run_ltw_camera_server_ros2foxy_v8.sh          ← 호스트(Orin)
   │  docker run --rm --name ltw-camera-server ...
   ▼
컨테이너 ltw-camera-server (이미지 ltw-camera-server:1.1-foxy-3cam)
   │  bind-mount: docker/src/camera_forwarder_3cam.py → /app/camera_forwarder_3cam.py (ro)
   ▼
python3 /app/camera_forwarder_3cam.py --port 5555 --head-mount ego_view ...  →  ZMQ PUB :5555
```

Dockerfile 에 `COPY` 가 있어 이미지 안에도 사본이 있지만 **실행 시 호스트 파일이 덮어쓴다.**
로직을 고쳤으면 **컨테이너만 재시작하면 된다.**

| `docker run` 옵션 | 이유 |
|---|---|
| `--network host` | ZMQ 5555 노출 (+ videohub 폴백 시 DDS discovery) |
| `-v /dev:/dev` | D405 가 리셋 시 USB 를 재열거한다. 정적 `--device` 로는 핸들이 깨진다 |
| `--device-cgroup-rule 'c 81:* rmw'` | video4linux(major 81) |
| `--device-cgroup-rule 'c 189:* rmw'` | USB serial/bus(major 189) |
| `--rm` | 공유 로봇이므로 흔적을 남기지 않는다 |
| `--privileged` **미사용** | 공유 로봇 원칙. 필요한 접근만 연다 |

`run_*.sh` 들은 컨테이너 이름 `ltw-camera-server` 를 공유한다. **동시 실행 불가**이며
시작 시 `docker rm -f` 로 이전 컨테이너를 먼저 정리한다.

### `docker/src/` 진단 스크립트

| 파일 | 역할 |
|---|---|
| `camera_forwarder_3cam.py` | ✅ **실제로 실행되는 파일** |
| `probe_realsense_head.py` | 머리 librealsense 직결 실패 시 어느 단계에서 막혔는지 |
| `probe_d405.py` | `pipeline.start` 는 되는데 `wait_for_frames` 가 타임아웃될 때 |
| `probe_v4l2.py` | `/dev/video*` 로 D405 color 를 읽을 수 있는지 |
| `zmq_3cam_check.py` | DGX 쪽에서 5555 페이로드에 어떤 카메라 키가 오는지 |

---

## 부록 B — 자주 겪는 문제

| 증상 | 원인 / 조치 |
|---|---|
| 머리가 안 뜨고 손목이 ~3Hz | `HEAD_SERIAL` 오입력(sysfs 값을 넣었을 가능성). `list_realsense_serials.sh` 로 재확인 |
| `left_wrist=0.0Hz` / `★ WEDGE` 지속 | autosuspend 미해제. T1 의 `disable_d405_autosuspend.sh` 부터 다시 |
| 손목 좌/우 반대 | `LEFT_WRIST_SERIAL`/`RIGHT_WRIST_SERIAL` 명시 후 재기동 |
| exporter 가 시작을 안 함 | `robot_config` 대기 중 → T2 C++ deploy 가 떠 있는지 확인 |
| 검증 4번 FAIL(연속 정지 1초 이상) | D405 wedge 오염 → **해당 에피소드 폐기**([2번 문서](2_dataset_preprocess_merge.md)) |
| 영상이 배속 재생됨 | `--dataset-fps` 가 실측 Hz 보다 높다 |
| `git pull` 실패(Orin) | wifi 미연결 → `sudo nmcli device wifi connect "delight" password "..."` |
