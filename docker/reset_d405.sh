#!/usr/bin/env bash
# reset_d405.sh
#
# D405를 물리적으로 만지지 않고 "논리적 replug"으로 리셋한다.
#
# [왜 필요한가]
#   raw V4L2로 D405를 스트리밍하면 가끔 UVC 스트림이 wedge된다
#   (dmesg: "uvcvideo: Failed to set UVC probe control: -32"). 이 wedge는
#   USBDEVFS_RESET(포트 리셋)으로는 안 풀리고 **완전 분리 후 재연결**만 통한다.
#   호스트의 sysfs `authorized`를 0→1로 토글하면 커널이 장치를 버스에서
#   완전히 제거했다가 새로 열거하므로, 물리 뽑았다 꽂기와 동일한 효과가 난다.
#   (컨테이너 안에선 /sys가 read-only라 이 작업을 못 하므로 호스트에서 실행.)
#
# [언제]
#   서버 로그에 "프레임 오래됨"/"프라이밍 실패"가 반복되며 손목이 안 붙을 때.
#   반드시 3-cam 서버(컨테이너)를 먼저 끈 상태에서 실행할 것(장치 점유 충돌 방지).
#
# [공유 로봇 주의]
#   idProduct=0b5b(D405)만 토글한다. 머리 D435i·다른 팀 장치 무영향.
#   authorized 토글은 우리 D405만 잠깐 재연결시킬 뿐 호스트 서비스를 안 건드린다.
#
# [부수효과]
#   재열거되면 autosuspend(power/control)가 기본값(auto)으로 돌아가므로,
#   이 스크립트가 마지막에 다시 off(on) 해준다.
#
# Usage:
#   sudo ./docker/reset_d405.sh
#   (그다음 3-cam 서버 실행)

set -euo pipefail

# 1) D405들만 골라 authorized=0 (버스에서 완전 분리). 경로를 기록해 둔다.
d405_devs=()
for dev in /sys/bus/usb/devices/*/; do
    if [ "$(cat "$dev/idProduct" 2>/dev/null || true)" = "0b5b" ]; then
        serial="$(cat "$dev/serial" 2>/dev/null || echo '(none)')"
        echo "[reset] $(basename "$dev") serial=$serial → authorized 0 (분리)"
        echo 0 | sudo tee "$dev/authorized" >/dev/null
        d405_devs+=("$dev")
    fi
done

if [ "${#d405_devs[@]}" -eq 0 ]; then
    echo "[reset] D405(0b5b)를 못 찾음. USB 연결 확인." >&2
    exit 1
fi

echo "[reset] 1초 대기(완전 분리)..."
sleep 1

# 2) 방금 분리한 D405 경로만 authorized=1 (재열거). authorized=0 이어도 sysfs
#    노드 자체는 남아있어 같은 경로에 다시 쓸 수 있다. 다른 장치는 안 건드림.
for dev in "${d405_devs[@]}"; do
    echo "[reset] $(basename "$dev") → authorized 1 (재연결)"
    echo 1 | sudo tee "$dev/authorized" >/dev/null
done

echo "[reset] 재열거 대기 3초..."
sleep 3

# autosuspend 다시 off (재열거로 auto로 돌아갔으므로)
for dev in /sys/bus/usb/devices/*/; do
    if [ "$(cat "$dev/idProduct" 2>/dev/null || true)" = "0b5b" ]; then
        echo on | sudo tee "$dev/power/control" >/dev/null 2>&1 || true
        echo "[reset] autosuspend off: $(basename "$dev") speed=$(cat "$dev/speed" 2>/dev/null)Mbps"
    fi
done

echo "[reset] 완료. 이제 3-cam 서버를 실행하세요."
