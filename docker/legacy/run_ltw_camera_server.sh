#!/usr/bin/env bash
# run_ltw_camera_server.sh
# D435i(head/ego-view) 카메라 서버를 Docker 컨테이너로 실행 (Orin NX에서 실행)
#
# Usage:
#   bash docker/run_ltw_camera_server.sh              # 기본 실행 (포트 5555)
#   LTW_CAMERA_PORT=5556 bash docker/run_ltw_camera_server.sh   # 포트 변경

set -euo pipefail

IMAGE_NAME="ltw-camera-server:0.1-rgb"
CONTAINER_NAME="ltw-camera-server"
PORT="${LTW_CAMERA_PORT:-5555}"

echo "[ltw-camera-server] Starting container..."
echo "[ltw-camera-server]   image     : $IMAGE_NAME"
echo "[ltw-camera-server]   container : $CONTAINER_NAME"
echo "[ltw-camera-server]   port      : $PORT"
echo "[ltw-camera-server]   device    : D435i (RealSense, /dev/bus/usb only — not --privileged)"
echo ""

docker run --rm \
    --name "$CONTAINER_NAME" \
    --network host \
    --device=/dev/bus/usb:/dev/bus/usb \
    -e CAMERA_PORT="$PORT" \
    "$IMAGE_NAME"
