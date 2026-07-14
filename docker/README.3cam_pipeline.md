# 3-cam VLA 데이터 수집 파이프라인 — 병목 해결 여정 & known-good 기준점

작성: 2026-07-14 (세션 c5739959)
목적: **머리(ego_view) + 손목 D405 2대(left/right_wrist)** 3-카메라 LeRobot v2.1 데이터셋(VLA 파인튜닝용).

> ⚠️ 이 문서는 **"forwarder 사전 리사이즈(17.6Hz→30Hz)" 시도 전의 known-good 상태**를 박제한 것이다.
> 그 시도가 실패/성능저하 시 **여기 적힌 상태로 되돌리면** 동작하는 15fps 파이프라인이 복구된다.

---

## 0. 지금 동작하는 상태 (KNOWN-GOOD, 롤백 목표)

- **성능**: 3-cam achieved **~15.7~17.6Hz**, `--dataset-fps 15` 시 **speedup 0.98x(정상 재생)**.
- **검증**: LeRobot 로더(`LeRobotDataset`)로 실제 로딩 성공 — 3카메라 (3,480,640) 디코드 + state(43) + action.wbc(43) 정렬 확인. VLA 파인튜닝 소비 가능.
- **저장물**: `outputs/<날짜>/` — 3카메라 640×480 mp4 + parquet + meta(info/modality/episodes/tasks/stats). frame_index 0..N 연속, timestamp 균일 1/fps 그리드, NaN 없음.

### 확정 실행 명령 (본격 수집용)
```bash
# --- Orin NX (온보드) ---
sudo bash ./docker/disable_d405_autosuspend.sh    # D405 2대 5000Mbps 확인 (매 부팅)
LEFT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323073651-video-index4 \
RIGHT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323071827-video-index4 \
./docker/run_ltw_camera_server_ros2foxy_v6.sh
# 로그: "ZMQ PUB bound ... (SNDHWM=3, latest-only)" + 손목 각 fps~30

# --- DGX Spark ---
python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "..." --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered \
    --record-wrist-cameras --camera-decode-reduce 2 --dataset-fps 15
```

---

## 1. 아키텍처

```
[Orin NX 온보드 · Docker: ltw-camera-server:0.9-foxy-3cam]
  D435i 머리 → 호스트 videohub_pc4 → CycloneDDS → VideoClient RPC ─┐ (JPEG relay, 퍼블리시 클럭 ~54Hz)
  D405 ×2   → 커널 uvcvideo → V4L2 cv2.VideoCapture ───────────────┤ (grabber→encoder, JPEG)
                                    camera_forwarder_3cam.py        │
                                    ZMQ PUB 5555 (msgpack: {ts, images:{ego,left,right}=JPEG})
                                              │  Gigabit 192.168.123.x
[DGX Spark]                                   ▼
  run_data_exporter.py  SUB 5555(카메라)+5556(teleop pose)+5557(로봇상태)
    camera_triggered(머리 ts=저장클럭) → frame_index 정렬 → NVENC h264 → LeRobot v2.1
```

---

## 2. 병목 해결 여정 (문제 → 원인 → 해결) — **핵심 참고**

| # | 문제 | 원인 | 해결 | 파일 |
|---|---|---|---|---|
| 1 | wrist 검은 화면 | D405 raw V4L2 고정 저노출 | `cap.set(CAP_PROP_AUTO_EXPOSURE, 3)` | camera_forwarder_3cam.py `_open_v4l2` |
| 2 | 2대 동시 실패 (REQBUFS errno=19) | 한 대가 USB2.0(480Mbps) | 2대 다 **USB3.0 허브**에 연결 (둘 다 5000Mbps + 고유 시리얼) | HW |
| 3 | **15초 지연** | forwarder PUB **SNDHWM 미설정 → 기본 1000** → 느린 구독자 있으면 최대 ~17초 백로그, FIFO로 옛것부터 발송. SUB의 CONFLATE는 수신큐만 비워 못 막음 | `socket.setsockopt(zmq.SNDHWM,3)` + `LINGER,0` | camera_forwarder_3cam.py PUB 생성부 |
| 4 | **resize 20ms 병목** | `mat[..., ::-1]`(BGR→RGB)가 음수 stride 비연속 뷰 → cv2.resize가 ~2배 느려짐 | `cv2.cvtColor(mat, COLOR_BGR2RGB)`(연속배열) → rsz_ego 20→9ms | sensor_server.py `deserialize` |
| 5 | 손목 화질 블러 | 전역 reduce=2가 손목 640×480→320×240 반토막→업스케일 | **per-camera reduce**: deserialize가 `reduce_factor`를 dict로도 받음. exporter가 `{"ego_view":2}` 전달 → ego만 reduce, 손목 원본(1) | sensor_server.py + run_data_exporter.py |
| 6 | 배속 재생 | achieved(15~17Hz) < stamped fps | `--dataset-fps 15` (achieved 밑) → speedup 0.98x | CLI |

### 진단으로 배제/확정한 것 (같은 실수 반복 방지)
- **대역폭 아님**: 링크 Gigabit(3-cam ~13MB/s ≪ 125MB/s). 15초는 대역폭이 아니라 PUB 백로그였음.
- **수신 버퍼 아님**: SUB CONFLATE+RCVHWM=3, 수학적 최대 ~0.5초. 문제는 **송신(PUB)** 측.
- **큐 키우기 무효**: NVENC 인코더가 병목이 아님(큐 안 참). 병목은 **메인스레드 디코드+리사이즈**.
- **CPU 인코딩(libx264) 더 나쁨**: 메인스레드와 CPU 경쟁 → 더 느려짐. NVENC 유지가 정답.
- **과거 head-only 30Hz는 진짜였음**: reduce=2로 loop 여유(47Hz) 확보 → camera_triggered로 30Hz. 코드 동일(git: flip은 base 커밋 daf3899). 20ms는 strided 퇴행이었고 cvtColor로 복원(9ms=과거 af_img 9.5ms 일치).
- **A/B 측정**: head-only(NO_WRISTS)도 rsz_ego 20ms였음 → 손목 아니라 ego resize가 병목. 박사님 지적("손목만으론 그렇게 안 느려짐")이 정확.

### 왜 3-cam이 head-only(30Hz)보다 느린가 (17.6Hz)
발행(54Hz)·네트워크는 문제 없음. **DGX 메인스레드의 프레임당 이미지 처리**가 병목:
- head-only: 디코드 1장 + 리사이즈 1장
- 3-cam: 디코드 3장(ego 960×540 + 손목 640×480 ×2) + 리사이즈 3장
- → 프레임당 ~50ms → ~17Hz. NVENC(GPU 3프로세스 병렬)는 놀면서 프레임 기다림(병목 아님).

---

## 3. 변경된 파일 (known-good diff 요약)

- **docker/src/camera_forwarder_3cam.py**
  - `v4l2_grabber`(캡처 전용, cap.read만 → 드라이버 버퍼 상시 드레인) + `wrist_encoder`(최신 RAW만 JPEG) **2스레드 분리**
  - `_open_v4l2`: `CAP_PROP_AUTO_EXPOSURE=3`
  - PUB 소켓: `SNDHWM=3` + `LINGER=0` (bind 전) ← **15초 지연 해결의 핵심**
- **gear_sonic/camera/sensor_server.py**
  - `deserialize`: `mat[...,::-1]` → `cv2.cvtColor(...,COLOR_BGR2RGB)` (연속배열, resize 정상화)
  - `deserialize`: `reduce_factor`가 int 또는 **dict{key:factor}** (per-camera reduce)
  - `LAST_DECODE_MS` 전역(카메라별 디코드 계측)
- **gear_sonic/scripts/run_data_exporter.py**
  - `decode_reduce_factor={"ego_view": config.camera_decode_reduce}` (손목은 기본 1)
  - `dec_<key>`/`rsz_<key>` telemetry + `[shape]` 1회 로그(입력해상도+C_contig+cv2_threads)
- **docker/run_ltw_camera_server_ros2foxy_v6.sh**
  - forwarder bind-mount(이미지 리빌드 없이 반영), `NO_WRISTS=1`(head-only baseline), `MIRROR_WRIST=1`
- **주의**: 이 파일들은 대부분 **미커밋 WIP** (git엔 base 커밋만). 롤백하려면 **지금 상태를 git 커밋/태그**해 두는 게 안전(아래 4번).

---

## 4. ▶ 다음 시도: forwarder 사전 리사이즈 (17.6→30Hz) + 롤백 방법

### 목표
DGX 메인스레드의 ego_view 디코드+리사이즈(~15ms)를 없애기 위해, **Orin의 forwarder가 ego_view를 미리 640×480으로 줄여 전송**. 그러면 DGX는 작은 이미지만 받아 처리 → achieved 30Hz 가능. 부수효과: 대역폭↓.

### 후보 방식 (택1)
1. **raw 전송**: forwarder가 imdecode→resize(640×480)→**재인코딩 없이 raw ndarray**로 send. DGX는 디코드·리사이즈 0. 대역폭 640×480×3×30 ≈ 27MB/s(Gigabit OK). Orin은 디코드+리사이즈만(~15ms/frame).
2. **JPEG 재인코딩 전송**: forwarder가 imdecode→resize→**imencode(640×480 JPEG)**→send. 대역폭 최소지만 Orin에 재인코딩 부하 추가.
3. **videohub 저해상도 요청**(최선, 가능하면): VideoClient RPC/videohub가 720p/VGA 필드를 주면 Orin 재인코딩 0. → 가능 여부 조사 필요(`camera_forwarder_3cam.py`의 `GetImageSample` 해상도 옵션).

### ⚠️ 리스크 (되돌려야 할 수 있는 이유)
- **Orin은 공유 로봇** — 재인코딩/디코드 부하가 로봇 제어·타 팀에 영향 가능. `htop`으로 CPU 여유 먼저 확인.
- "Hz 넉넉"은 RPC 제한이지 **CPU 여유가 아님**. 현재 forwarder는 ego를 relay만 해 CPU 거의 안 씀 → 사전 리사이즈는 실제 부하 추가.
- Orin이 30fps 디코드+리사이즈를 못 버티면 오히려 발행 Hz가 떨어져 **전체가 더 느려질 수** 있음.

### 롤백 방법 (실패 시)
1. **forwarder 원복**: `camera_forwarder_3cam.py`의 머리 루프를 "GetImageSample JPEG를 그대로 relay"(사전 리사이즈 추가분 제거)로 되돌림.
2. **exporter 원복**: `--camera-decode-reduce 2`(ego 960×540 디코드+리사이즈) 그대로 사용.
3. **git 태그로 즉시 복귀**(권장): 아래처럼 지금 상태를 태그해두면 `git checkout <tag> -- <files>`로 파일 단위 복구 가능.

```bash
# 지금(known-good)을 스냅샷 (강력 권장 — MD보다 확실한 롤백)
git add -A && git commit -m "3cam known-good: 15fps pipeline (SNDHWM+cvtColor+per-cam reduce)"
git tag 3cam-known-good-15fps
# 이후 사전 리사이즈 실험이 실패하면:
#   git checkout 3cam-known-good-15fps -- docker/src/camera_forwarder_3cam.py gear_sonic/camera/sensor_server.py gear_sonic/scripts/run_data_exporter.py
```

---

## 5. 남은 백로그 (급하지 않음)
1. **D405 wedge** — 간헐 발생(~30초~수분), **물리 replug만 복구**(authorized 토글 무효 확인됨). 프로덕션 안정화 = RSUSB+uvcvideo 언바인드 or librealsense 소스빌드.
2. **3-cam 30fps** — 이 문서 4번(사전 리사이즈). 지금은 15fps로 충분.
3. **발행 중복 검증** — head 54Hz 발행이 실제 새 프레임인지(videohub fps). 효율/대역폭.
4. **deploy 종료 크래시** — A+B+X+Y 종료 시 CycloneDDS `EntityDelegate` assertion core dump. 카메라 무관, 데이터 손상 없음.
5. **로그 노이즈** — `Time delta exception`(teleop pose 100ms 초과, raise 안 함), `DataExporter Missed`(폴링 20ms 초과). 둘 다 무해.

---

## 6. 데이터 검증 방법 (에피소드 정렬 확인)
```bash
# mp4 프레임수/fps
ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames,avg_frame_rate <mp4>
# parquet: 행수 == mp4프레임 == info.total_frames, timestamp==frame_index/fps 균일, NaN 없음
# LeRobot 실제 로딩: LeRobotDataset(repo_id="x", root="outputs/<날짜>") → ds[0]에 3카메라+state+action
```
검증 통과 기준: 3소스 프레임수 일치 + timestamp 균일 그리드 + LeRobot 로딩 성공.
