#!/usr/bin/env bash
# list_realsense_serials.sh
#
# 카메라 서버(HEAD_SERIAL / LEFT_WRIST_SERIAL / RIGHT_WRIST_SERIAL)에 넣을
# **pyrealsense(librealsense) 시리얼**을 출력하고 바로 종료한다.
#
# [왜 래퍼가 필요한가]
#   pyrealsense2 는 소스빌드된 v8 이미지(ltw-camera-server:1.1-foxy-3cam)
#   **안에만** 있고 호스트에는 없다. 그래서 열거도 컨테이너 안에서 해야 하는데,
#   그 docker run 한 줄이 device-cgroup-rule 까지 붙어 길다. 이 스크립트가 그
#   원샷 실행을 대신한다. 실제 열거 로직은 camera_forwarder_3cam.py 의
#   `--list-serials` / `--list-devices` 모드(= _rs_devices) 를 그대로 재사용한다.
#
# [출력]
#   기본(간결)  : 명령어에 그대로 넣는 librealsense 시리얼만. 붙여넣을 수 있는
#                 실행 명령 한 줄도 함께 나온다.
#   -v(자세히)  : 위에 더해 D405 by-id color 노드(--left-node/--right-node 용)까지.
#   ★ 명령어에 "직접 넣는" 값은 언제나 librealsense 시리얼이다. by-id 의 USB
#     시리얼과는 같은 물리 카메라라도 **다른 값**이다(계층이 다름).
#     실측: by-id 255323073651/255323071827  <->  librealsense 260322270228/260422272337.
#
# [videohub 이 떠 있어도 됨]
#   열거(query_devices)는 스트림을 시작하지 않으므로 videohub 이 /dev/video4 를
#   물고 있어도 문제없이 목록이 나온다. 서버를 띄우기 "전에" 값만 확인하는 용도.
#
# Usage:
#   ./docker/list_realsense_serials.sh          # 간결 (기본)
#   ./docker/list_realsense_serials.sh -v       # by-id 노드까지 전부
#
# forwarder 소스는 호스트 최신본을 bind-mount 하므로 이미지 리빌드 불필요.

set -euo pipefail

IMAGE_TAG="ltw-camera-server:1.1-foxy-3cam"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FWD_SRC="$SCRIPT_DIR/src/camera_forwarder_3cam.py"

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    echo "[list] 이미지 $IMAGE_TAG 가 없습니다. v8 빌드를 먼저 하세요." >&2
    exit 1
fi

# 기본은 간결 모드(--list-serials): 명령어에 넣을 librealsense 시리얼만 출력.
# -v / --verbose 를 주면 by-id 노드까지 전부 찍는 기존 --list-devices 로 전환.
MODE="--list-serials"
case "${1:-}" in
    -v|--verbose)
        MODE="--list-devices"
        echo "[list] librealsense 장치 열거 (컨테이너 $IMAGE_TAG 안에서)..." >&2
        echo "" >&2
        ;;
    -h|--help)
        echo "사용법: $(basename "$0") [-v|--verbose]"
        echo "  (기본)      명령어에 넣을 librealsense 시리얼만 출력"
        echo "  -v          by-id color 노드까지 전부 출력"
        exit 0
        ;;
esac

docker run --rm \
    --name ltw-camera-server-list \
    -v /dev:/dev \
    -v "$FWD_SRC":/app/camera_forwarder_3cam.py:ro \
    --device-cgroup-rule='c 81:* rmw' \
    --device-cgroup-rule='c 189:* rmw' \
    "$IMAGE_TAG" \
    python3 /app/camera_forwarder_3cam.py "$MODE"
