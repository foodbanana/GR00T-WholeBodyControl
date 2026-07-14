#!/usr/bin/env python3
"""
3-cam forwarder 검증용 ZMQ subscriber (DGX Spark 등 consumer 쪽에서 실행).

camera_forwarder_3cam.py 가 5555로 쏘는 msgpack 페이로드를 구독해서:
  - 어떤 카메라 키(ego_view/left_wrist/right_wrist)가 오는지
  - 각 JPEG가 정상 디코드되는지 + 해상도
  - 수신 fps
를 출력한다. exporter를 켜지 않고도(특히 D405 1대만 물린 단일 검증 시)
서버가 제대로 publish 하는지 확인하는 용도.

의존성: pyzmq, msgpack, opencv-python(or headless), numpy — .venv_data_collection
에 이미 다 있음.

사용:
    python docker/src/zmq_3cam_check.py --host 192.168.123.164 --port 5555
    # 각 키 첫 프레임을 파일로도 저장하려면:
    python docker/src/zmq_3cam_check.py --host 192.168.123.164 --save-dir /tmp/cam_check
"""

import argparse
import os
import time

import cv2
import msgpack
import numpy as np
import zmq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.123.164")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="이 시간(s) 뒤 자동 종료(0=무한, Ctrl+C로 종료)")
    ap.add_argument("--save-dir", default=None,
                    help="각 카메라 키의 첫 프레임을 이 폴더에 저장")
    args = ap.parse_args()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt_string(zmq.SUBSCRIBE, "")
    sock.setsockopt(zmq.CONFLATE, True)
    sock.setsockopt(zmq.RCVHWM, 3)
    sock.connect(f"tcp://{args.host}:{args.port}")
    print(f"[check] SUB connected to tcp://{args.host}:{args.port}", flush=True)

    saved = set()
    count = 0
    t0 = time.time()
    last_log = t0

    try:
        while True:
            if sock.poll(1000) == 0:
                print("[check] 1초간 메시지 없음 — 서버가 publish 중인지 확인", flush=True)
                continue
            packed = sock.recv()
            msg = msgpack.unpackb(packed, raw=False)
            images = msg.get("images", {})
            timestamps = msg.get("timestamps", {})
            count += 1

            now = time.time()
            if now - last_log >= 1.0:
                lines = []
                for key in sorted(images.keys()):
                    val = images[key]
                    mat = cv2.imdecode(np.frombuffer(val, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if mat is None:
                        lines.append(f"{key}=DECODE_FAIL({len(val)}B)")
                        continue
                    h, w = mat.shape[:2]
                    age = now - float(timestamps.get(key, now))
                    lines.append(f"{key}={w}x{h} {len(val)/1024:.0f}KB age={age*1e3:.0f}ms")
                    if args.save_dir and key not in saved:
                        path = os.path.join(args.save_dir, f"{key}.jpg")
                        cv2.imwrite(path, mat)
                        saved.add(key)
                        print(f"[check] saved {path}", flush=True)
                fps = count / (now - t0)
                print(f"[check] recv fps~{fps:.1f} | " + " | ".join(lines), flush=True)
                last_log = now

            if args.seconds > 0 and (now - t0) >= args.seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        ctx.term()
        print(f"[check] done. total messages: {count}", flush=True)


if __name__ == "__main__":
    main()
