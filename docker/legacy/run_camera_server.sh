#!/usr/bin/env bash
# run_camera_server.sh
# D435i 카메라 서버를 Docker 컨테이너로 실행 (Orin NX에서 실행)
#
# Usage:
#   bash docker/run_camera_server.sh            # 기본 실행 (포트 5555)
#   bash docker/run_camera_server.sh --port 5556 # 포트 변경
#   bash docker/run_camera_server.sh --help      # 옵션 확인

set -euo pipefail

IMAGE_NAME="sonic-camera-server:latest"
PORT="${SONIC_CAMERA_PORT:-5555}"

# 인자를 그대로 CMD에 전달 (포트 변경 등 override 가능)
EXTRA_ARGS=("$@")

echo "[camera-server] Starting sonic-camera-server container..."
echo "[camera-server]   image : $IMAGE_NAME"
echo "[camera-server]   port  : $PORT"
echo "[camera-server]   device: D435i (RealSense, --privileged)"
echo ""

docker run --rm \
    --privileged \
    --network host \
    --name sonic-camera-server \
    "$IMAGE_NAME" \
    python -m gear_sonic.camera.composed_camera \
        --ego-view-camera realsense \
        --port "$PORT" \
        "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
