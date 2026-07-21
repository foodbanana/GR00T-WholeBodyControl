#!/usr/bin/env python3
"""
머리(D435i) librealsense 직결 진단 — videohub_pc4가 떠 있는 상태에서 실행한다.

camera_forwarder_3cam.py --head-backend realsense 가 실패했을 때, 어느 단계에서
막혔는지 한 번에 특정하는 것이 목적이다. 순서 자체가 진단이다:

    [1] 장치가 librealsense에 보이는가        -> 안 보이면 udev/권한/USB 문제
    [2] color 프로파일에 640x480@30 bgr8이 있는가
    [3] pipeline.start()가 되는가             -> 여기서 죽으면 인터페이스 claim 실패
                                                  (= videohub과 진짜로 충돌)
    [4] wait_for_frames가 프레임을 주는가      -> start는 되는데 0프레임이면
                                                  D405 때와 동일 증상 (아래 참조)

[왜 이 스크립트가 필요한가 — 배경]
    2026-07-20 실측: videohub_pc4가 D435i의 /dev/videoN을 STREAMON 배타독점
    (EBUSY)한다. 커널 V4L2로는 머리를 못 읽는다는 뜻이다.
    2026-07-21 채택: librealsense의 RSUSB 백엔드는 libusb로 USB 인터페이스를
    직접 claim 해 커널 uvcvideo를 우회하므로 videohub과 공존할 것으로 기대한다
    (다른 연구원이 videohub 비활성화 없이 성공했다는 증언이 근거).
    ★ 그러나 반대 증거가 있다: 같은 Jetson에서 pip pyrealsense2로 **D405**를
      열었을 때 열거는 됐지만 wait_for_frames가 0프레임이었다(uvcvideo 점유가
      당시 결론). 그게 맞다면 D435i에도 같은 일이 일어날 수 있다.
    이 스크립트는 그 두 가설 중 어느 쪽이 맞는지를 30초 안에 가른다.

[해석 가이드 — 결과별 다음 수순]
    [1]에서 0대           : udev 룰(99-realsense-libusb.rules)이 호스트에 필요한
                            경우일 수 있다(librealsense issue #12022). 또는 USB
                            패스스루 누락. -v /dev:/dev 와 device-cgroup-rule을
                            확인할 것.
    [3]에서 실패(errno=16): **2026-07-21 실측에서 실제로 여기서 막혔다.**
                            에러가 xioctl(VIDIOC_S_FMT) = V4L2 ioctl 이라는 점이
                            결정적이다 — 이 pip wheel은 RSUSB가 아니라 **V4L2
                            백엔드**로 빌드돼 있고, 그래서 cv2.VideoCapture와
                            똑같이 videohub의 STREAMON 독점에 막힌다.
                            -> 해결: 수집 중 videohub 정지(팀 승인됨). 또는
                               librealsense를 -DFORCE_RSUSB_BACKEND=true 로
                               소스빌드(pip wheel로는 공존 불가).
    [4]에서 0프레임       : D405 때와 같은 증상. start까지는 되는데 스트림이
                            안 흐르는 상태다. 이 경우 --head-fps를 낮추거나
                            (30 -> 15) 해상도를 낮춰 대역폭을 줄여보고, 그래도
                            안 되면 videohub 공존이 불가하다는 결론.
    전부 OK               : forwarder를 --head-backend realsense 로 돌리면 된다.

[중요] 이 스크립트는 **손목 D405를 건드리지 않는다.** 이름에 "405"가 들어간
    장치는 전부 건너뛴다 — 손목은 V4L2(cv2.VideoCapture)로 열려 있고, 여기서
    librealsense가 같은 장치를 claim 하면 진행 중인 수집을 깨뜨린다.

실행 (Orin NX, 재빌드 없이 스크립트만 마운트):
    docker run --rm -v /dev:/dev \
      --device-cgroup-rule='c 81:* rmw' --device-cgroup-rule='c 189:* rmw' \
      -v ~/tw_gearsonic/GR00T-WholeBodyControl/docker/src/probe_realsense_head.py:/probe_realsense_head.py \
      ltw-camera-server:1.1-foxy-3cam python3 /probe_realsense_head.py

    (v7 이미지 1.0-foxy-3cam 으로도 그대로 동작한다 — pyrealsense2는 v7에도
     들어 있다. 즉 v8을 빌드하기 전에 먼저 이걸로 가능 여부를 확인해도 된다.)
"""

import sys
import time

import pyrealsense2 as rs

WANT = (640, 480, 30)


def list_color_profiles(dev):
    seen = set()
    out = []
    for s in dev.query_sensors():
        for p in s.get_stream_profiles():
            if p.stream_type() != rs.stream.color:
                continue
            vp = p.as_video_stream_profile()
            key = (vp.width(), vp.height(), p.fps(), str(p.format()))
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
    out.sort()
    return out


def stream_test(serial, label, width=0, height=0, fmt=None, fps=0,
                use_default=False, seconds=5.0):
    """[3] start + [4] wait_for_frames 를 한 번에 본다. (started, frames) 반환."""
    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_device(serial)
    if not use_default:
        cfg.enable_stream(rs.stream.color, width, height, fmt, fps)
    try:
        pipe.start(cfg)
    except Exception as e:
        print(f"    [{label}] [3] start 실패: {type(e).__name__}: {e}")
        return False, 0
    print(f"    [{label}] [3] start OK")

    got = 0
    first_latency = None
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            try:
                frames = pipe.wait_for_frames(2000)
            except Exception as e:
                print(f"    [{label}] [4] wait_for_frames 실패: "
                      f"{type(e).__name__}: {e}")
                break
            if frames:
                if got == 0:
                    first_latency = time.time() - t0
                got += 1
    finally:
        try:
            pipe.stop()
        except Exception:
            pass

    hz = got / seconds
    verdict = "OK" if got > 0 else "FAIL(0프레임)"
    extra = f", 첫 프레임까지 {first_latency:.2f}s" if first_latency else ""
    print(f"    [{label}] [4] {seconds:.0f}초간 {got}개 = {hz:.1f}Hz "
          f"({verdict}{extra})")
    return True, got


def main():
    print("=" * 70)
    print("머리(D435i) librealsense 직결 진단 — videohub 공존 여부 판정")
    print("=" * 70)

    # ---- [1] 장치 열거 ----------------------------------------------------
    try:
        ctx = rs.context()
        all_devs = list(ctx.query_devices())
    except Exception as e:
        print(f"[1] rs.context() 자체가 실패: {type(e).__name__}: {e}")
        print("    -> librealsense가 USB에 접근조차 못 하는 상태. USB 패스스루"
              "(-v /dev:/dev, device-cgroup-rule 189) 확인.")
        sys.exit(1)

    print(f"\n[1] librealsense가 보는 장치: {len(all_devs)}대")
    heads = []
    for d in all_devs:
        try:
            name = d.get_info(rs.camera_info.name)
            serial = d.get_info(rs.camera_info.serial_number)
        except Exception as e:
            print(f"    (정보 조회 실패한 장치 하나: {e})")
            continue
        try:
            usb = d.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb = "?"
        try:
            fw = d.get_info(rs.camera_info.firmware_version)
        except Exception:
            fw = "?"
        is_wrist = "405" in name
        tag = "손목(D405) — 건너뜀" if is_wrist else "머리 후보"
        print(f"    - {name}  serial={serial}  USB={usb}  FW={fw}   [{tag}]")
        if not is_wrist:
            heads.append((name, serial, d))

    if not all_devs:
        print("\n    -> 장치 0대. 다음을 의심할 것:")
        print("       (a) 호스트에 99-realsense-libusb.rules 가 없다"
              " (librealsense issue #12022)")
        print("       (b) USB 패스스루 누락 (-v /dev:/dev, device-cgroup-rule)")
        print("       (c) D435i가 물리적으로 안 꽂혀 있거나 다른 포트에 있다")
        sys.exit(2)

    if not heads:
        print("\n    -> D405만 보이고 머리 후보(D435i 등)가 없다.")
        print("       videohub이 D435i를 USB 레벨에서까지 가리고 있을 가능성,")
        print("       또는 D435i 미연결. 호스트에서 `lsusb | grep 8086` 확인.")
        sys.exit(3)

    # ---- [2]~[4] 머리 후보마다 프로파일 + 스트리밍 ------------------------
    ok_any = False
    for name, serial, dev in heads:
        print(f"\n=== {name}  serial={serial} ===")

        print("[2] color 프로파일:")
        profs = list_color_profiles(dev)
        if not profs:
            print("    없음(!) — 이 장치의 color 센서를 못 여는 상태")
        has_want = False
        for w, h, fps, fmt in profs:
            mark = ""
            if (w, h, fps) == WANT and "bgr8" in fmt:
                mark = "   <-- forwarder 기본 설정"
                has_want = True
            print(f"    {w}x{h} {fps}fps {fmt}{mark}")
        if not has_want:
            print(f"    ※ {WANT[0]}x{WANT[1]}@{WANT[2]} bgr8 이 목록에 없다 —"
                  " --head-width/--head-height/--head-fps 를 위 목록 중"
                  " 하나로 맞춰야 한다.")

        print("[3][4] 스트리밍 테스트:")
        started, got = stream_test(serial, "color 640x480 bgr8 30",
                                   640, 480, rs.format.bgr8, 30)
        if got == 0:
            # 대역폭/프로파일 문제 배제: 더 가벼운 설정과 default로 재시도
            print("    -> 0프레임. 더 가벼운 설정으로 재시도:")
            _s, g2 = stream_test(serial, "color 640x480 bgr8 15",
                                 640, 480, rs.format.bgr8, 15)
            _s, g3 = stream_test(serial, "default(pipe.start)",
                                 use_default=True)
            got = max(got, g2, g3)
        if got > 0:
            ok_any = True

    # ---- 판정 -------------------------------------------------------------
    print("\n" + "=" * 70)
    if ok_any:
        print("판정: ✅ videohub이 떠 있는 상태에서 머리 스트리밍 성공.")
        print("      --head-backend realsense 로 forwarder를 돌리면 된다.")
        print("      (위에서 실제로 프레임이 나온 해상도/fps를 --head-width/")
        print("       --head-height/--head-fps 에 그대로 넣을 것)")
    else:
        print("판정: ❌ 머리 스트리밍 실패.")
        print("      에러에 xioctl(VIDIOC_S_FMT) / errno=16 이 보이면 원인 확정이다:")
        print("        이 pyrealsense2 wheel은 V4L2 백엔드라 videohub의 STREAMON")
        print("        독점(EBUSY)을 우회하지 못한다. RSUSB 우회는 pip wheel로 불가.")
        print("      -> 다음 수순: 수집 중 videohub 정지(2026-07-21 팀 승인).")
        print("         정지 후 이 스크립트를 다시 돌려 [3][4]가 OK면 확정.")
        print("         그래도 안 되면 그때는 udev/권한 쪽을 본다.")
        print("      -> 정지 없이 공존이 꼭 필요하면 librealsense를 소스에서")
        print("         -DFORCE_RSUSB_BACKEND=true 로 빌드해야 한다.")
    print("=" * 70)


if __name__ == "__main__":
    main()
