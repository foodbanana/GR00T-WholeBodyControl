#!/usr/bin/env bash
# disable_d405_autosuspend.sh
#
# 연결된 모든 Intel RealSense D405(idProduct=0b5b)의 USB autosuspend를 끈다.
#
# [왜 필요한가]
#   이 Jetson(L4T r35.3.1)에서 D405를 raw V4L2로 스트리밍하면 USB3 링크
#   전력관리(U1/U2 LPM) 때문에 스트림이 수 초 뒤 wedge된다
#   (dmesg: "uvcvideo: Failed to set UVC probe control: -32",
#           "usb ...: Disable of device-initiated U1/U2 failed").
#   power/control 을 'on'으로 두면(=런타임 suspend 비활성) 이 전환이 사라져
#   스트림이 안정적으로 지속된다. 머리 D435i는 영향 없음(D405 특유).
#
# [언제 실행하나]
#   power/control 값은 재부팅/USB 재연결 시 기본값(auto)으로 돌아가므로,
#   **3-cam 서버(run_ltw_camera_server_ros2foxy_v6.sh)를 띄우기 전에 매번** 실행.
#   포트 경로가 바뀌어도 되도록 idProduct 매칭으로 모든 D405에 적용한다.
#
# [공유 로봇 주의]
#   우리 카메라(D405)만 건드리고 다른 팀 장치/서비스는 손대지 않는다.
#   되돌리려면 각 dev의 power/control 에 'auto'를 쓰면 된다.
#
# Usage:
#   sudo ./docker/disable_d405_autosuspend.sh
#   (또는 sudo 없이 실행하면 내부에서 sudo tee 사용)

set -euo pipefail

found=0
for dev in /sys/bus/usb/devices/*/; do
    if [ "$(cat "$dev/idProduct" 2>/dev/null || true)" = "0b5b" ]; then
        serial="$(cat "$dev/serial" 2>/dev/null || echo '(none)')"
        speed="$(cat "$dev/speed" 2>/dev/null || echo '?')"
        if echo on | sudo tee "$dev/power/control" >/dev/null 2>&1; then
            echo "[autosuspend off] $(basename "$dev")  serial=$serial  speed=${speed}Mbps  -> $(cat "$dev/power/control")"
            found=$((found + 1))
        else
            echo "[warn] $(basename "$dev") power/control 쓰기 실패 (권한?)" >&2
        fi
    fi
done

if [ "$found" -eq 0 ]; then
    echo "[autosuspend] D405(idProduct=0b5b)를 찾지 못했습니다. USB 연결을 확인하세요." >&2
    exit 1
fi
echo "[autosuspend] D405 ${found}대 처리 완료. 이제 3-cam 서버를 실행하세요."
