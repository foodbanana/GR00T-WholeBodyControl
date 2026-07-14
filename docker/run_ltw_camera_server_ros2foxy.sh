#!/usr/bin/env bash
# run_ltw_camera_server_ros2foxy.sh
#
# ltw-camera-server:0.8-foxy-videoclient 컨테이너를 Orin NX(로봇 온보드)에서 실행한다.
# 이 컨테이너는 unitree_sdk2py VideoClient RPC(/api/videohub/request,
# /response)로 완전한 JPEG 프레임을 받아 ZMQ PUB 5555 포트로 forward하는
# 것 외에 아무 일도 하지 않는다. (v1~v4는 /frontvideostream ROS 2 topic을
# rclpy로 subscribe하는 방식이었으나, 이 topic이 raw H.264 stream chunks라
# 파싱 불가능해 v5에서 VideoClient RPC 방식으로 전면 교체했다.)
#
# 이 로봇은 여러 팀이 공유하므로 zero-impact 원칙을 지킨다:
#   - --privileged 안 씀 (권한 상승 없음)
#   - --device 안 씀, /dev 마운트 안 함 (USB/카메라 장치 접근 없음 —
#     이 컨테이너는 카메라를 직접 열지 않고 VideoClient RPC로만 프레임을 받는다)
#   - 호스트 파일/서비스를 건드리는 로직 없음 (videohub_pc4,
#     master_service.service 등은 절대 정지/재시작 시도하지 않는다)
#   - --rm으로 실행 (종료 시 컨테이너 흔적 zero)
#
# 이전 이름(run_ltw_camera_server.sh, 이미지 0.1-rgb, D435i USB 직결
# 방식)은 다른 접근이라 그대로 보존하고, 이 스크립트는 별도 파일로 둔다.
#
# Usage:
#   ./docker/run_ltw_camera_server_ros2foxy.sh

set -euo pipefail

IMAGE_TAG="ltw-camera-server:0.8-foxy-videoclient"
CONTAINER_NAME="ltw-camera-server"

# 이전 실행이 비정상 종료(--rm이 미처 정리 못한 경우 등)로 이름이 남아있을
# 수 있으므로, 새로 띄우기 전에 동일 이름 컨테이너를 정리한다.
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "[ltw-camera-server] Starting container..."
echo "[ltw-camera-server]   image     : $IMAGE_TAG"
echo "[ltw-camera-server]   container : $CONTAINER_NAME"
echo "[ltw-camera-server]   port      : 5555 (ZMQ PUB)"
echo ""

docker run --rm \
    --name "$CONTAINER_NAME" \
    --network host \
    -e ROS_DOMAIN_ID=0 \
    -e CYCLONEDDS_URI=file:///etc/cyclonedds.xml \
    "$IMAGE_TAG"

# --network host: CycloneDDS(VideoClient RPC) discovery와 ZMQ 5555 포트
#   노출 둘 다에 필요하다. 별도 -p 포트 매핑이 없는 이유이기도 하다.
# -e ROS_DOMAIN_ID / CYCLONEDDS_URI: 이미지 안에도 동일한 ENV가 이미
#   박혀 있지만(Dockerfile.ltw_camera_server_ros2foxy_v5 섹션 6), 여기서
#   다시 명시하는 이유는 이 스크립트만 보고도 videohub_pc4 publisher와
#   정확히 어떤 값으로 매칭시켜야 하는지 바로 알 수 있게 하기 위함이다
#   (이미지를 열어보지 않아도 됨). RMW_IMPLEMENTATION은 넘기지 않는다 —
#   v5는 rclpy/rmw 레이어를 쓰지 않아(VideoClient가 CycloneDDS를 직접
#   호출) 이 값을 읽는 코드가 없다.
