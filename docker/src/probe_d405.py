#!/usr/bin/env python3
"""
D405 스트리밍 진단 스크립트 (컨테이너 안에서 실행).

"pipeline.start는 성공하는데 wait_for_frames가 타임아웃" 문제의 원인을
한 번에 특정한다:
  1) 각 D405의 USB 연결 타입(2.1 / 3.2 ...) — USB2.0에 물렸는지 확인
  2) 지원하는 color 스트림 프로파일 목록 (640x480@30 bgr8이 유효한지)
  3) 카메라를 '한 대씩' 5초 스트리밍 테스트 (2대 동시 대역폭 문제 배제)
       - 먼저 우리 forwarder와 같은 config(color 640x480 bgr8 30)
       - 실패하면 default config(pipe.start())로 재시도

실행 (Orin NX, 재빌드 없이 스크립트만 마운트):
    docker run --rm -v /dev:/dev \
      --device-cgroup-rule='c 81:* rmw' --device-cgroup-rule='c 189:* rmw' \
      -v ~/tw_gearsonic/GR00T-WholeBodyControl/docker/src/probe_d405.py:/probe_d405.py \
      ltw-camera-server:0.9-foxy-3cam python3 /probe_d405.py
"""

import time

import pyrealsense2 as rs


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


def stream_test(serial, width, height, fmt, fps, label, use_default=False):
    pipe = rs.pipeline()
    cfg = rs.config()
    cfg.enable_device(serial)
    if not use_default:
        cfg.enable_stream(rs.stream.color, width, height, fmt, fps)
    try:
        pipe.start(cfg)
    except Exception as e:
        print(f"    [{label}] start 실패: {e}")
        return
    got = 0
    t0 = time.time()
    try:
        while time.time() - t0 < 5:
            try:
                frames = pipe.wait_for_frames(2000)
            except Exception as e:
                print(f"    [{label}] wait_for_frames 실패: {e}")
                break
            if frames:
                got += 1
    finally:
        try:
            pipe.stop()
        except Exception:
            pass
    print(f"    [{label}] 5초간 프레임 수신: {got}개 "
          f"({'OK' if got > 0 else 'FAIL'})")


def main():
    ctx = rs.context()
    devs = [d for d in ctx.query_devices()
            if "D405" in d.get_info(rs.camera_info.name)]
    print(f"[probe] D405 감지: {len(devs)}대\n")

    for d in devs:
        serial = d.get_info(rs.camera_info.serial_number)
        try:
            usb = d.get_info(rs.camera_info.usb_type_descriptor)
        except Exception:
            usb = "?"
        try:
            fw = d.get_info(rs.camera_info.firmware_version)
        except Exception:
            fw = "?"
        print(f"=== D405 serial={serial}  USB={usb}  FW={fw} ===")
        profs = list_color_profiles(d)
        if profs:
            print("  color 프로파일:")
            for w, h, fps, fmt in profs:
                mark = "  <-- 우리 설정" if (w, h, fps) == (640, 480, 30) else ""
                print(f"    {w}x{h} {fps}fps {fmt}{mark}")
        else:
            print("  color 프로파일 없음(!) — D405 color 미지원 상태일 수 있음")

        print("  스트리밍 테스트 (한 대만):")
        stream_test(serial, 640, 480, rs.format.bgr8, 30, "color 640x480 bgr8 30")
        stream_test(serial, 0, 0, None, 0, "default(pipe.start)", use_default=True)
        print()


if __name__ == "__main__":
    main()
