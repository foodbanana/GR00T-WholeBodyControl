#!/usr/bin/env bash
# run_ltw_camera_server_ros2foxy_v8.sh
#
# ltw-camera-server:1.1-foxy-3cam 컨테이너를 Orin NX(로봇 온보드)에서 실행한다.
# v7과의 유일한 차이는 **머리(ego_view) 취득 경로**다:
#   HEAD_BACKEND=realsense (v8 기본) : librealsense로 D435i 직결, 640x480@30
#   HEAD_BACKEND=videohub            : VideoClient RPC (= v7과 완전히 동일)
# 손목 D405 2대(left/right_wrist, V4L2)는 v7과 같다. 하나의 ZMQ 5555 페이로드로
# 합쳐 forward 하며, workstation의 run_data_exporter.py --record-wrist-cameras
# 와 짝을 이룬다.
#
# [왜 v8인가 — v7을 안 건드리는 이유]
#   librealsense 직결은 2026-07-21 현재 **이 로봇에서 실측 미검증**이다.
#   검증 안 된 가설이 known-good(v7 = 1.0-foxy-3cam, 25Hz 채택본)을 덮어쓰면
#   문제 발생 시 돌아갈 곳이 없어진다. 그래서 이미지 태그부터 분리했다.
#   ★ 문제가 생기면: 이 스크립트를 멈추고 run_..._v7.sh 를 그냥 실행하면 된다.
#     v7 이미지(1.0-foxy-3cam)는 그대로 남아 있다.
#
# [videohub 과의 공존 — 불안정 (2026-07-21 관측)]
#   관측된 사실만:
#     머리만 RSUSB  -> videohub 최소 20초 생존, ego 29Hz 정상
#     3대 전부 RSUSB + 수 분 -> videohub 사라짐
#   어느 시점에 죽는지는 모른다. **언젠가 밀려난다고 가정하고 운용할 것.**
#   - 우리가 명시적으로 정지시키지는 않는다(그럴 필요 없음).
#   - 밀려나도 우리 파이프라인은 정상(실측 ego 29Hz / 손목 30Hz).
#   - 복구는 재부팅. 부팅 시 videohub 자동 기동 + /dev/video4=D435i 원복 확인됨.
#   - ★ videohub 수동 기동 금지 — /dev/video4 가 손목 D405 를 가리키게 되면
#     우리 손목을 점유한다.
#
# 공유 로봇 원칙:
#   - --privileged 는 쓰지 않는다. device-cgroup-rule로 video(major 81) /
#     usb(major 189) 노드 접근만 허용한다.
#   - -v /dev:/dev 는 D405가 리셋 시 USB를 재열거하기 때문에 필요하다
#     (정적 --device 는 재열거 후 핸들이 깨진다).
#   - 호스트 서비스(videohub_pc4, master_service 등)를 **정지/재시작하지 않는다.**
#   - --rm 으로 실행 (종료 시 흔적 zero).
#
# ★ 머리 시리얼: HEAD_SERIAL 미지정 시 forwarder가 "이름에 405가 없는" 장치를
#   자동 선택한다 — V4L2로 이미 열려 있는 손목 D405 2대를 librealsense가
#   건드리면 안 되기 때문이다.
#   ※ 머리 유닛은 지금까지 두 번 교체됐다(D435i 346122071399 → D435 938422073271
#     → 2026-07-29 현재 D455 046322250434). **여기 적힌 값을 믿지 말고 매번
#     아래 스크립트로 실측할 것.** 시리얼을 잘못 넣으면 librealsense가
#     "No device connected" 재시도 루프를 돌며 USB 버스를 흔들어 머리가 아예
#     안 뜨고 손목까지 ~3Hz로 떨어진다(카메라 고장으로 오진하기 쉬움).
#   장치 목록 확인 (래퍼 사용 권장):
#     ./docker/list_realsense_serials.sh
#   (D405 by-id 노드 + librealsense 장치 시리얼을 함께 출력한다.
#    ★ 명령어에 넣는 값은 언제나 후자 = librealsense 시리얼)
#
# Usage:
#   ./docker/run_ltw_camera_server_ros2foxy_v8.sh
#
# 실전 사용 예 (head-only 데이터 수집 — 손목 D405 미연결, 2026-07-29 현재 구성):
#   NO_WRISTS=1 HEAD_SERIAL=046322250434 \
#     sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
#   # head-only에선 [sync] 로그가 안 나온다(맞출 손목이 없으니 정상).
#   # 건강 신호는 content(new frames): ego_view=29Hz + publish fps.
#   # DGX exporter에선 --record-wrist-cameras 를 뺀다(ego_view만 스키마에 들어감).
#
# 실전 사용 예 (3-cam 데이터 수집, 머리 librealsense 직결):
#   HEAD_SERIAL=<list_realsense_serials.sh로 실측> \
#   LEFT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323073651-video-index4 \
#   RIGHT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323071827-video-index4 \
#   ./docker/run_ltw_camera_server_ros2foxy_v8.sh
#   # DGX: run_data_exporter.py --camera-decode-reduce 1 --dataset-fps 25
#
# v7 동작을 이 이미지로 재현(A/B 비교용):
#   HEAD_BACKEND=videohub HEAD_RESIZE=640x480 LEFT_NODE=... RIGHT_NODE=... \
#     ./docker/run_ltw_camera_server_ros2foxy_v8.sh

set -euo pipefail

IMAGE_TAG="ltw-camera-server:1.1-foxy-3cam"
CONTAINER_NAME="ltw-camera-server"

# 손목 color 노드(선택). 지정하면 좌우를 그 노드로 고정한다.
LEFT_NODE="${LEFT_NODE:-}"
RIGHT_NODE="${RIGHT_NODE:-}"

# 이전 실행 잔여 컨테이너 정리(v5~v8이 같은 이름을 쓰므로 서로 교체 가능,
# 동시 실행은 불가 — 같은 카메라를 두 프로세스가 잡는 사고를 막는다).
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# forwarder 인자 조립
FWD_ARGS=(python3 /app/camera_forwarder_3cam.py
          --port 5555 --head-mount ego_view
          --wrist-width 640 --wrist-height 480)
if [[ -n "$LEFT_NODE" ]]; then
    FWD_ARGS+=(--left-node "$LEFT_NODE")
    echo "[ltw-camera-server] left_wrist  node: $LEFT_NODE"
fi
if [[ -n "$RIGHT_NODE" ]]; then
    FWD_ARGS+=(--right-node "$RIGHT_NODE")
    echo "[ltw-camera-server] right_wrist node: $RIGHT_NODE"
fi
if [[ -z "$LEFT_NODE" && -z "$RIGHT_NODE" ]]; then
    echo "[ltw-camera-server] 손목 노드 미지정 → by-id 자동배정"
fi
# 검증용: D405 1대(left)만 있을 때 right로 복제 발행 (MIRROR_WRIST=1)
if [[ "${MIRROR_WRIST:-}" == "1" ]]; then
    FWD_ARGS+=(--mirror-left-to-right)
    echo "[ltw-camera-server] MIRROR_WRIST=1 → left_wrist를 right_wrist로 복제(검증용)"
fi
# 측정 baseline: 머리(ego_view) 1대만 발행 (손목 스레드 안 띄움). A/B 실험용.
if [[ "${NO_WRISTS:-}" == "1" ]]; then
    FWD_ARGS+=(--no-wrists)
    echo "[ltw-camera-server] NO_WRISTS=1 → head-only(ego_view)만 발행 (1-cam baseline)"
fi
# 머리를 Orin에서 640x480으로 사전 리사이즈해 발행 (17.6 → 25Hz).
# 사용: HEAD_RESIZE=640x480 ./docker/run_...v7.sh  (exporter는 --camera-decode-reduce 1)
# 머리 백엔드 선택. v8은 realsense가 **기본**이다(이 이미지의 존재 이유).
# HEAD_BACKEND=videohub 로 주면 v7과 동일한 VideoClient RPC 경로로 폴백한다 —
# 이미지를 바꾸지 않고 A/B 비교가 가능하다.
# realsense 경로는 원하는 해상도를 센서에 직접 요청하므로 HEAD_RESIZE가
# 불필요하며(1080p 디코드→리사이즈→재인코딩 사이클 소멸), 지정해도 무시된다.
if [[ "${HEAD_BACKEND:-realsense}" == "realsense" ]]; then
    FWD_ARGS+=(--head-backend realsense
               --head-width "${HEAD_WIDTH:-640}"
               --head-height "${HEAD_HEIGHT:-480}"
               --head-fps "${HEAD_FPS:-30}")
    if [[ -n "${HEAD_SERIAL:-}" ]]; then
        FWD_ARGS+=(--head-serial "$HEAD_SERIAL")
    fi
    # HEAD_AE_PRIORITY=0 → 머리 프레임레이트 고정(30fps 유지, 어두우면 노출 캡).
    # 미지정이면 센서 기본(=노출 우선, 어두우면 fps 저하). 30Hz 안 뜨면 0으로 시도.
    if [[ -n "${HEAD_AE_PRIORITY:-}" ]]; then
        FWD_ARGS+=(--head-ae-priority "$HEAD_AE_PRIORITY")
        echo "[ltw-camera-server] HEAD_AE_PRIORITY=$HEAD_AE_PRIORITY → 머리 auto_exposure_priority 설정"
    fi
    echo "[ltw-camera-server] HEAD_BACKEND=realsense → librealsense 직결 " \
         "(${HEAD_WIDTH:-640}x${HEAD_HEIGHT:-480}@${HEAD_FPS:-30}, serial=${HEAD_SERIAL:-auto})"
    echo "[ltw-camera-server]   videohub_pc4는 정지시키지 않는다 (RSUSB로 공존)"
else
    echo "[ltw-camera-server] HEAD_BACKEND=videohub → VideoClient RPC 폴백 (v7과 동일 동작)"
fi
# 손목 백엔드 선택. WRIST_BACKEND=realsense 이면 손목 D405 도 librealsense(RSUSB)
# 직결로 읽는다. 기본은 v4l2(기존 known-good).
#   목적: (1) 커널 uvcvideo 를 우회해 D405 wedge 에서 벗어날 여지
#         (2) hardware_reset() 이라는 진짜 복구 수단 확보 — USBDEVFS_RESET 은
#             2026-07-21 실측에서 wedge 복구에 실패했고, 유효한 sysfs authorized
#             토글은 컨테이너에서 /sys 가 ro 라 쓸 수 없다.
#   ⚠️ Intel 문서: RSUSB 는 multi-cam 에 최적화돼 있지 않다. 머리까지 합쳐 3대를
#      RSUSB 로 돌리는 것은 미검증 영역이므로 실측으로 확인할 것.
#   ★ 시리얼 주의: LEFT/RIGHT_WRIST_SERIAL 은 by-id 의 USB 시리얼이 아니라
#     librealsense 가 보고하는 값이다(--list-devices 로 확인).
if [[ "${WRIST_BACKEND:-v4l2}" == "realsense" ]]; then
    FWD_ARGS+=(--wrist-backend realsense --wrist-fps "${WRIST_FPS:-30}")
    if [[ -n "${LEFT_WRIST_SERIAL:-}" ]]; then
        FWD_ARGS+=(--left-wrist-serial "$LEFT_WRIST_SERIAL")
    fi
    if [[ -n "${RIGHT_WRIST_SERIAL:-}" ]]; then
        FWD_ARGS+=(--right-wrist-serial "$RIGHT_WRIST_SERIAL")
    fi
    if [[ -n "${WRIST_AE_PRIORITY:-}" ]]; then
        FWD_ARGS+=(--wrist-ae-priority "$WRIST_AE_PRIORITY")
        echo "[ltw-camera-server] WRIST_AE_PRIORITY=$WRIST_AE_PRIORITY → 손목 auto_exposure_priority 설정"
    fi
    echo "[ltw-camera-server] WRIST_BACKEND=realsense → 손목도 librealsense 직결" \
         "(${WRIST_FPS:-30}fps, serial=${LEFT_WRIST_SERIAL:-auto}/${RIGHT_WRIST_SERIAL:-auto})"
    echo "[ltw-camera-server]   ⚠️ 3대 전부 RSUSB = 미검증 영역. Hz/wedge 실측 확인할 것"
else
    echo "[ltw-camera-server] WRIST_BACKEND=v4l2 (기본, cv2.VideoCapture)"
fi
if [[ -n "${HEAD_RESIZE:-}" ]]; then
    FWD_ARGS+=(--head-resize "$HEAD_RESIZE")
    echo "[ltw-camera-server] HEAD_RESIZE=$HEAD_RESIZE → 머리 사전 리사이즈(Orin 디코드+리사이즈+재인코딩)"
fi

echo "[ltw-camera-server] Starting 3-cam container (v8: librealsense head)..."
echo "[ltw-camera-server]   image     : $IMAGE_TAG"
echo "[ltw-camera-server]   container : $CONTAINER_NAME"
echo "[ltw-camera-server]   port      : 5555 (ZMQ PUB, ego_view+left_wrist+right_wrist)"
echo ""

# forwarder 소스를 호스트에서 bind-mount 하여 이미지 리빌드 없이 수정 반영.
# (이미지에 COPY된 /app/camera_forwarder_3cam.py 를 호스트 최신본으로 덮어씀)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FWD_SRC="$SCRIPT_DIR/src/camera_forwarder_3cam.py"

docker run --rm \
    --name "$CONTAINER_NAME" \
    --network host \
    -v /dev:/dev \
    -v "$FWD_SRC":/app/camera_forwarder_3cam.py:ro \
    --device-cgroup-rule='c 81:* rmw' \
    --device-cgroup-rule='c 189:* rmw' \
    -e ROS_DOMAIN_ID=0 \
    -e CYCLONEDDS_URI=file:///etc/cyclonedds.xml \
    "$IMAGE_TAG" \
    "${FWD_ARGS[@]}"

# --network host    : ZMQ 5555 노출 (+ videohub 폴백 시 CycloneDDS discovery).
# -v /dev:/dev      : D405 USB 재열거 대응(정적 --device로는 리셋 후 끊김).
# --device-cgroup-rule 'c 81:* rmw'  : video4linux(major 81) 노드 접근.
# --device-cgroup-rule 'c 189:* rmw' : USB serial/bus(major 189) 노드 접근.
