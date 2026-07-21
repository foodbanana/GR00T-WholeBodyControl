#!/usr/bin/env bash
# prep_d435i_head.sh
#
# 머리 D435i(idProduct=0b3a)를 librealsense로 직접 읽기 전에 준비시킨다.
# 손목용 disable_d405_autosuspend.sh + reset_d405.sh 를 머리 카메라에 맞춰
# 하나로 합친 것이다. 로직은 그 둘과 동일하고 대상 idProduct만 다르다.
#
# [왜 필요한가]
#   2026-07-21 실측: videohub을 억제해 EBUSY를 없앤 뒤에도 librealsense가
#     [3] pipeline.start() OK
#     [4] wait_for_frames -> "Frame didn't arrive within 2000" (0프레임)
#   상태였다. 손목 D405에서 똑같은 증상을 겪었고, 그때 원인은 두 가지였다:
#
#   (1) USB autosuspend — power/control 이 'auto' 면 USB3 링크 전력관리
#       (U1/U2 LPM) 때문에 스트림이 시작돼도 프레임이 안 흐른다.
#       ★ 머리 D435i에는 이 처치를 한 번도 한 적이 없다. 지금까지 머리는
#         호스트 videohub이 읽었기 때문에 우리 관심 밖이었다.
#   (2) UVC 스트림 wedge — 스트리밍 중인 장치를 거칠게 끊으면
#       (dmesg: "uvcvideo: Failed to set UVC probe control: -32") 소프트
#       재오픈으로 안 풀린다. USBDEVFS_RESET(포트 리셋)으로도 부족하고
#       **버스에서 완전 분리 후 재연결**만 통한다. sysfs `authorized` 를
#       0->1 토글하면 물리적으로 뽑았다 꽂는 것과 동일한 효과가 난다.
#       ★ 우리는 videohub_pc4 를 0.5초마다 SIGTERM으로 죽이고 있으므로,
#         스트리밍 중인 D435i가 반복적으로 거칠게 끊긴 상태다. 정확히 이
#         wedge 조건에 해당한다.
#
# [실행 순서 — 중요]
#   반드시 **videohub 억제가 걸린 상태에서** 실행하고, 끝나면 바로
#   librealsense 쪽(진단 또는 카메라 서버)을 붙일 것. 억제가 풀려 있으면
#   재열거 직후 videohub이 먼저 장치를 잡아가 원점으로 돌아간다.
#
#       sudo touch /tmp/suppress_videohub
#       sudo bash -c 'while [ -f /tmp/suppress_videohub ]; do \
#                       pkill -x videohub_pc4; sleep 0.5; done' &
#       sudo ./docker/prep_d435i_head.sh
#       (이어서 probe_realsense_head.py 또는 v8 카메라 서버 실행)
#
# [공유 로봇 주의]
#   idProduct=0b3a(D435i)만 건드린다. 손목 D405(0b5b)와 다른 팀 장치는
#   무영향이다. authorized 토글은 우리 카메라만 잠깐 재연결시킬 뿐 호스트
#   서비스를 정지/변경하지 않는다. autosuspend/authorized 값은 모두
#   재부팅하면 기본값으로 돌아가므로 영구 변경이 아니다.
#
# Usage:
#   sudo ./docker/prep_d435i_head.sh              # 재열거 + autosuspend off
#   sudo ./docker/prep_d435i_head.sh --no-replug  # autosuspend off 만
#
# 컨테이너 안에서는 /sys 가 read-only 라 실행 불가 — 반드시 **호스트**에서.

set -euo pipefail

ID_PRODUCT="0b3a"   # Intel RealSense D435i (손목 D405는 0b5b)
DO_REPLUG=1

if [ "${1:-}" = "--no-replug" ]; then
    DO_REPLUG=0
fi

find_devs() {
    local out=()
    for dev in /sys/bus/usb/devices/*/; do
        if [ "$(cat "$dev/idProduct" 2>/dev/null || true)" = "$ID_PRODUCT" ]; then
            out+=("$dev")
        fi
    done
    printf '%s\n' "${out[@]:-}"
}

mapfile -t devs < <(find_devs)
devs=("${devs[@]:-}")
if [ -z "${devs[0]:-}" ]; then
    echo "[prep] D435i(idProduct=$ID_PRODUCT)를 못 찾음. USB 연결 확인." >&2
    exit 1
fi

for dev in "${devs[@]}"; do
    serial="$(cat "$dev/serial" 2>/dev/null || echo '(none)')"
    speed="$(cat "$dev/speed" 2>/dev/null || echo '?')"
    echo "[prep] 대상: $(basename "$dev")  serial=$serial  speed=${speed}Mbps"
done

# -----------------------------------------------------------------------------
# 1) 논리적 replug (authorized 0 -> 1) — UVC wedge 해소
# -----------------------------------------------------------------------------
if [ "$DO_REPLUG" = "1" ]; then
    for dev in "${devs[@]}"; do
        echo "[prep] $(basename "$dev") → authorized 0 (버스에서 분리)"
        echo 0 | tee "$dev/authorized" >/dev/null
    done

    echo "[prep] 1초 대기(완전 분리)..."
    sleep 1

    # authorized=0 이어도 sysfs 노드는 남아있어 같은 경로에 다시 쓸 수 있다.
    for dev in "${devs[@]}"; do
        echo "[prep] $(basename "$dev") → authorized 1 (재연결)"
        echo 1 | tee "$dev/authorized" >/dev/null
    done

    echo "[prep] 재열거 대기 3초..."
    sleep 3
fi

# -----------------------------------------------------------------------------
# 2) autosuspend off — 재열거하면 auto로 돌아가므로 replug '뒤에' 해야 한다
# -----------------------------------------------------------------------------
found=0
for dev in /sys/bus/usb/devices/*/; do
    if [ "$(cat "$dev/idProduct" 2>/dev/null || true)" = "$ID_PRODUCT" ]; then
        serial="$(cat "$dev/serial" 2>/dev/null || echo '(none)')"
        speed="$(cat "$dev/speed" 2>/dev/null || echo '?')"
        if echo on | tee "$dev/power/control" >/dev/null 2>&1; then
            echo "[prep] autosuspend off: $(basename "$dev")  serial=$serial" \
                 " speed=${speed}Mbps  -> $(cat "$dev/power/control")"
            found=$((found + 1))
        else
            echo "[warn] $(basename "$dev") power/control 쓰기 실패 (권한?)" >&2
        fi
    fi
done

if [ "$found" -eq 0 ]; then
    echo "[prep] 재열거 후 D435i를 다시 못 찾음(!). dmesg 확인 필요." >&2
    exit 1
fi

echo "[prep] 완료 — D435i ${found}대. videohub 억제가 유지된 상태에서"
echo "[prep] 바로 probe_realsense_head.py 또는 카메라 서버를 실행하세요."
