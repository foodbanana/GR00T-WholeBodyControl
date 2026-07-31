# 3-cam 데이터 수집 파이프라인 — 설계 배경 (v8 기준)

머리(ego_view) + 손목 D405 2대(left/right_wrist) 3-카메라로 LeRobot v2.1 데이터셋을
수집하는 파이프라인이 **왜 지금 형태가 되었는지**를 기록한 문서다.

> 운영 절차(실행 명령·순서·검증)는 리포 루트의 [data_collection.md](../data_collection.md) 를 볼 것.
> 이 문서는 코드에 남아 있는 결정들의 근거를 설명한다.

---

## 1. 현재 구조 (v8 = `ltw-camera-server:1.1-foxy-3cam`)

```
[Orin NX 온보드 · Docker: ltw-camera-server:1.1-foxy-3cam]
  머리   → librealsense(RSUSB) 직결 → rs.pipeline 640x480 bgr8 ─┐  ≈29Hz  (퍼블리시 클럭)
  D405 ×2 → librealsense(RSUSB) 직결 → rs.pipeline 640x480 bgr8 ┤  30Hz   (grabber→encoder, 최신 JPEG)
                                       camera_forwarder_3cam.py  │
                                       ZMQ PUB 5555 (msgpack: {ts, images:{ego,left,right}=JPEG})
                                                 │  Gigabit 192.168.123.x
[DGX Spark]                                      ▼
  run_data_exporter.py  SUB 5555(카메라) + 5556(teleop pose) + 5557(로봇상태)
    --camera-triggered (머리 새 프레임 = 저장 클럭) → NVENC h264 → LeRobot v2.1
    --camera-decode-reduce 1  --dataset-fps 25
```

머리와 손목이 **완전히 같은 경로**(librealsense RSUSB)를 타는 것이 v8의 핵심이다.
센서에 640×480을 직접 요청하므로 Orin에서의 디코드→리사이즈→재인코딩 사이클이 아예 없다.

---

## 2. 머리 취득 경로의 변천 — v8이 존재하는 이유

| 단계 | 방식 | 한계 |
|---|---|---|
| v6/v7 초기 | 호스트 `videohub_pc4` → CycloneDDS → VideoClient RPC로 JPEG relay | 1080p로만 오므로 DGX에서 디코드+리사이즈 ~15ms → 3-cam 17.6Hz |
| v7 + `HEAD_RESIZE` | Orin이 1080p JPEG를 디코드→640×480 리사이즈→재인코딩해 발행 | 25Hz까지 개선됐으나 **공유 로봇인 Orin에 재인코딩 부하 추가** |
| **v8 (현재)** | librealsense로 D435i/D455 직결, 센서에 640×480@30 직접 요청 | 리사이즈·재인코딩 자체가 소멸. `--head-resize`는 realsense 백엔드에서 무시된다 |

`videohub` 저해상도 토픽은 채택하지 않았다 — `video360p`(640×360)은 업스케일 화질 손실,
`video720p`는 DGX 디코드 비용이 줄지 않았다.

### librealsense를 소스빌드하는 이유 (`-DFORCE_RSUSB_BACKEND=true`)

순정 커널 `uvcvideo`는 RealSense의 XU(eXtension Unit) 컨트롤을 통과시키지 못한다.
장치는 열리는데 스트리밍만 안 되는 증상(librealsense 이슈 #5302과 동일)이 나온다.
RSUSB 백엔드는 커널 V4L2를 우회하고 libusb로 직접 제어하므로 이 문제가 없고,
`hardware_reset()` 이라는 실질적 복구 수단도 생긴다.

> RSUSB는 Intel 문서상 multi-cam에 최적화된 구성이 아니다. 3대를 동시에 RSUSB로 돌리는 것은
> 실측으로 확인하며 쓰는 영역이다. 문제가 생기면 v7(`1.0-foxy-3cam`, 머리 = videohub RPC 경로)로
> 롤백할 수 있도록 이미지 태그를 분리해 두었다.

---

## 3. 병목 해결 여정 (문제 → 원인 → 해결)

코드에 남아 있는 비직관적인 처리들의 근거다. **건드리기 전에 읽을 것.**

| # | 문제 | 원인 | 해결 | 위치 |
|---|---|---|---|---|
| 1 | 손목 검은 화면 | D405 raw V4L2가 고정 저노출로 열림 | `cap.set(CAP_PROP_AUTO_EXPOSURE, 3)` | `camera_forwarder_3cam.py` `_open_v4l2` |
| 2 | D405 2대 동시 실패 (`REQBUFS errno=19`) | 한 대가 USB2.0(480Mbps)에 물림 | 2대 모두 **USB3.0 허브**에 연결 (둘 다 5000Mbps 확인) | 하드웨어 |
| 3 | **15초 지연** | PUB 소켓의 `SNDHWM` 미설정 → 기본값 1000. 느린 구독자가 있으면 최대 ~17초 백로그가 쌓이고 FIFO라 옛 프레임부터 나간다. SUB의 `CONFLATE`는 수신큐만 비우므로 못 막는다 | `SNDHWM=3` + `LINGER=0` (bind 전에 설정) | `camera_forwarder_3cam.py` PUB 생성부 |
| 4 | resize 20ms 병목 | `mat[..., ::-1]`(BGR→RGB)이 음수 stride 비연속 뷰를 만들어 `cv2.resize`가 ~2배 느려짐 | `cv2.cvtColor(mat, COLOR_BGR2RGB)`로 연속 배열 생성 → 20ms → 9ms | `gear_sonic/camera/sensor_server.py` `deserialize` |
| 5 | 손목 화질 블러 | 전역 `reduce=2`가 손목 640×480을 320×240으로 반토막 낸 뒤 업스케일 | **카메라별 reduce**: `deserialize`가 `reduce_factor`를 dict로도 받는다. exporter가 `{"ego_view": N}`만 전달 → 손목은 원본(1) | `sensor_server.py` + `run_data_exporter.py` |
| 6 | 배속 재생 | 실제 achieved Hz < mp4에 찍힌 fps | `--dataset-fps`를 achieved 아래로 설정 (현재 ≈29Hz에 대해 **25**) | CLI |

### 손목 캡처를 2스레드로 나눈 이유

`v4l2_grabber` / `rs` 캡처 스레드는 **읽기만** 하고(드라이버 버퍼를 상시 드레인),
`wrist_encoder` 스레드가 **최신 RAW 한 장만** JPEG로 인코딩한다.
한 스레드에서 캡처+인코딩을 같이 하면 인코딩 동안 드라이버 버퍼가 밀려 지연이 누적된다.

### 진단으로 배제한 것 (같은 실수 반복 방지)

- **대역폭 문제 아님** — 링크가 Gigabit이고 3-cam이 ~13MB/s(≪125MB/s). 15초 지연은 PUB 백로그였다.
- **수신 버퍼 문제 아님** — SUB는 `CONFLATE` + `RCVHWM=3`이라 수학적 최대 ~0.5초. 문제는 **송신(PUB)** 측이었다.
- **NVENC 병목 아님** — 큐가 차지 않는다. 병목은 DGX 메인스레드의 디코드+리사이즈였다.
- **CPU 인코딩(libx264)은 더 나쁨** — 메인스레드와 CPU를 경쟁해 오히려 느려진다. `--use-nvenc` 유지가 정답.
- **손목이 원인이 아님** — head-only(`NO_WRISTS=1`)에서도 `rsz_ego`가 20ms였다. ego 리사이즈가 병목이었다.

---

## 4. D405 USB autosuspend

이 Jetson(L4T r35.3.1)에서 D405를 스트리밍하면 USB3 링크 전력관리(U1/U2 LPM) 때문에
수 초 뒤 스트림이 wedge된다:

```
uvcvideo: Failed to set UVC probe control: -32
usb ...: Disable of device-initiated U1/U2 failed
```

`power/control`을 `on`으로 두면(런타임 suspend 비활성) 이 전환이 사라진다.
이 값은 **재부팅/USB 재연결 시 기본값(`auto`)으로 돌아가므로** 서버를 띄우기 전 매번
[disable_d405_autosuspend.sh](disable_d405_autosuspend.sh)를 실행해야 한다.
머리 카메라는 영향받지 않는 D405 특유의 문제다.

RSUSB 전환 이후에도 서버 시작 직후 손목이 잠깐 wedge되는 경우가 있으나(startup wedge),
RSUSB가 스스로 재시작해 복구한다. 그래서 운영 절차상 **손목 30Hz가 안정된 뒤에 녹화를 시작**한다.

---

## 5. `videohub_pc4` 와의 공존

- 우리 쪽에서 **정지시키지 않는다.** RSUSB는 커널 V4L2를 우회하므로 공존이 가능하다.
- 다만 3대를 모두 RSUSB로 돌리면 videohub가 수 분 뒤 밀려나는 경우가 관측됐다.
  밀려나도 이 파이프라인은 정상 동작하며, 복구는 재부팅이다(부팅 시 자동 기동).
- ★ **videohub를 수동으로 기동하지 말 것** — `/dev/video4`가 손목 D405를 가리키게 되면
  우리 손목 카메라를 점유한다.

## 6. 공유 로봇 원칙

- `--privileged` 를 쓰지 않는다. `--device-cgroup-rule` 로 video(major 81) / usb(major 189)
  노드 접근만 연다.
- `-v /dev:/dev` 는 필요하다 — D405가 리셋 시 USB를 재열거하므로 정적 `--device` 는 재열거 후
  핸들이 깨진다.
- `--rm` 으로 실행해 종료 시 흔적을 남기지 않는다.
- 호스트 서비스(`videohub_pc4`, `master_service` 등)를 정지·재시작하지 않는다.
