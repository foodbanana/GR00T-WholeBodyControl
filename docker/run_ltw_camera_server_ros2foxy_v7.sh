#!/usr/bin/env bash
# run_ltw_camera_server_ros2foxy_v7.sh
#
# ltw-camera-server:1.0-foxy-3cam 컨테이너를 Orin NX(로봇 온보드)에서 실행한다.
# 머리(ego_view, VideoClient RPC) + 손목 D405 2대(left_wrist, right_wrist,
# V4L2)를 하나의 ZMQ 5555 페이로드로 합쳐 forward 한다. workstation의
# run_data_exporter.py --record-wrist-cameras 와 짝을 이룬다.
#
# v6 run 스크립트와의 차이는 IMAGE_TAG 뿐이다 (0.9-foxy-3cam → 1.0-foxy-3cam).
# 컨테이너 이름이 같으므로 v6/v7는 서로 교체 가능하다(동시 실행은 불가).
#
# 공유 로봇 zero-impact 원칙:
#   - --privileged 는 쓰지 않는다. device-cgroup-rule로 video(major 81) /
#     usb(major 189) 노드 접근만 허용한다.
#   - -v /dev:/dev 는 D405가 리셋 시 USB를 재열거하기 때문에 필요하다
#     (정적 --device 는 재열거 후 핸들이 깨진다). 다른 팀 장치 노드가 보여도
#     이 컨테이너는 우리 노드만 open 하므로 그들 피드를 정지시키지 않는다.
#   - 호스트 서비스(videohub_pc4, master_service 등)는 절대 정지/재시작하지 않는다.
#   - --rm 으로 실행 (종료 시 흔적 zero).
#
# 손목 color 노드를 by-id 경로(재부팅에도 안정적)로 고정한다:
#   LEFT_NODE=/dev/v4l/by-id/usb-...405_..._255323073651-video-index4 \
#   RIGHT_NODE=/dev/v4l/by-id/usb-...405_..._255323071827-video-index4 \
#     ./docker/run_ltw_camera_server_ros2foxy_v7.sh
#
# ★ 새 로봇 주의: D405 시리얼은 그대로지만(카메라를 옮겨 달았으므로)
#   `-video-indexN` 의 N은 커널 uvcvideo 열거 순서에 따라 바뀔 수 있다.
#   먼저 확인할 것:
#     ls /dev/v4l/by-id/
#   또는 컨테이너로 열거(color 자동 탐지):
#     docker run --rm -v /dev:/dev --device-cgroup-rule='c 81:* rmw' \
#       --device-cgroup-rule='c 189:* rmw' ltw-camera-server:1.0-foxy-3cam \
#       python3 /app/camera_forwarder_3cam.py --list-devices
#   미지정 시 by-id에서 D405들을 찾아 시리얼 정렬 순으로 자동배정한다.
#
# Usage:
#   ./docker/run_ltw_camera_server_ros2foxy_v7.sh
#
# 실전 사용 예 (3-cam 데이터 수집, 머리 사전 리사이즈 ON):
#   HEAD_RESIZE=640x480 \
#   LEFT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323073651-video-index4 \
#   RIGHT_NODE=/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_405_Intel_R__RealSense_TM__Depth_Camera_405_255323071827-video-index4 \
#   ./docker/run_ltw_camera_server_ros2foxy_v7.sh

set -euo pipefail

IMAGE_TAG="ltw-camera-server:1.0-foxy-3cam"
CONTAINER_NAME="ltw-camera-server"

# 손목 color 노드(선택). 지정하면 좌우를 그 노드로 고정한다.
LEFT_NODE="${LEFT_NODE:-}"
RIGHT_NODE="${RIGHT_NODE:-}"

# 이전 실행 잔여 컨테이너 정리(v5/v6/v7이 같은 이름을 쓰므로 서로 교체 가능).
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
if [[ -n "${HEAD_RESIZE:-}" ]]; then
    FWD_ARGS+=(--head-resize "$HEAD_RESIZE")
    echo "[ltw-camera-server] HEAD_RESIZE=$HEAD_RESIZE → 머리 사전 리사이즈(Orin 디코드+리사이즈+재인코딩)"
fi

echo "[ltw-camera-server] Starting 3-cam container (v7 standalone)..."
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

# --network host    : VideoClient CycloneDDS discovery + ZMQ 5555 노출.
# -v /dev:/dev      : D405 USB 재열거 대응(정적 --device로는 리셋 후 끊김).
# --device-cgroup-rule 'c 81:* rmw'  : video4linux(major 81) 노드 접근.
# --device-cgroup-rule 'c 189:* rmw' : USB serial/bus(major 189) 노드 접근.
