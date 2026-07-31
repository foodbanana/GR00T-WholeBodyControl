#!/usr/bin/env python3
"""
ltw-camera-server v5 (0.8-foxy-videoclient) 용 카메라 forwarder.

Unitree G1의 front camera JPEG stream을 unitree_sdk2py.VideoClient RPC로
받아 ZMQ PUB로 forward한다.

접근 방식:
    v1~v4는 /frontvideostream ROS 2 topic을 rclpy로 subscribe 했으나
    이 topic은 raw H.264 stream chunks라 파싱 불가능했다.

    v5는 unitree_sdk2py.go2.video.VideoClient.GetImageSample() RPC로
    /api/videohub/request 채널을 통해 완전한 JPEG bytes를 받는다.
    이건 Unitree 공식 API이고, Go2용으로 개발됐지만 G1에도
    /api/videohub/* endpoint가 있어 동일하게 동작한다.

ZMQ payload 형식 (기존 workstation 코드와 호환):
    {
        "timestamps": {mount_position: unix_timestamp},
        "images":     {mount_position: jpeg_bytes},
    }
    msgpack encoded, use_bin_type=True
"""

import argparse
import sys
import time

import cv2
import msgpack
import numpy as np
import zmq

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.video.video_client import VideoClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--interface",
        default="eth0",
        help="Network interface for CycloneDDS (default: eth0, "
             "matches host videohub_pc4)")
    parser.add_argument(
        "--port",
        type=int,
        default=5555,
        help="ZMQ PUB port (default: 5555)")
    parser.add_argument(
        "--mount-position",
        default="ego_view",
        help="Camera mount position key in payload (default: ego_view, "
             "matches workstation run_data_exporter.py convention)")
    parser.add_argument(
        "--fps-log-interval",
        type=float,
        default=1.0,
        help="How often to log fps in seconds (default: 1.0)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="VideoClient RPC timeout in seconds (default: 3.0)")
    args = parser.parse_args()

    # ZMQ PUB 초기화
    ctx = zmq.Context()
    socket = ctx.socket(zmq.PUB)
    socket.bind(f"tcp://*:{args.port}")
    print(f"[ltw_camera_forwarder] ZMQ PUB bound to tcp://*:{args.port}",
          flush=True)

    # CycloneDDS 채널 초기화 (호스트 videohub_pc4와 동일 domain 0,
    # 동일 interface eth0)
    print(f"[ltw_camera_forwarder] Initializing CycloneDDS on "
          f"{args.interface} (domain 0)", flush=True)
    ChannelFactoryInitialize(0, args.interface)

    # VideoClient 초기화
    # 내부적으로 /api/videohub/request 로 RPC 요청 채널 열고,
    # /api/videohub/response 로 응답 수신 대기
    print(f"[ltw_camera_forwarder] Creating VideoClient "
          f"(timeout={args.timeout}s)", flush=True)
    client = VideoClient()
    client.SetTimeout(args.timeout)
    client.Init()
    print("[ltw_camera_forwarder] VideoClient initialized, "
          "requesting frames...", flush=True)

    # Forward 루프
    frame_count = 0
    error_count = 0
    invalid_jpeg_count = 0
    last_log_time = time.time()

    while True:
        # RPC 호출: /api/videohub/request 로 요청 보내고 응답 대기
        # code=0 이면 성공, data는 JPEG bytes (list of uint8)
        code, data = client.GetImageSample()

        if code != 0 or not data:
            error_count += 1
            # 처음 3번은 항상 로그, 이후엔 30번마다 (스팸 방지)
            if error_count <= 3 or error_count % 30 == 0:
                print(f"[ltw_camera_forwarder] GetImageSample failed: "
                      f"code={code} (error count={error_count})", flush=True)
            time.sleep(0.05)
            continue

        # data는 list of uint8, bytes로 변환
        jpeg_bytes = bytes(data)
        ts = time.time()

        # [진단용, 임시] GetImageSample()이 code=0 + non-empty data를 줬는데도
        # 실제로는 불완전/손상된 JPEG를 반환하는 경우가 있는지 확인하기 위한
        # 검증. 워크스테이션 쪽 cv2.imdecode()가 None을 반환하며 크래시하는
        # 문제(TypeError: 'NoneType' object is not subscriptable)의 원인이
        # RPC 응답 자체에 있는지, 그 이후 전송 경로에 있는지 구분하기 위함.
        # 전송은 그대로 진행 — 여기서 걸러내면 재현이 안 돼서 원인 특정이 안 됨.
        test_decode = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if test_decode is None:
            invalid_jpeg_count += 1
            head = jpeg_bytes[:16].hex()
            tail = jpeg_bytes[-16:].hex()
            print(
                f"[ltw_camera_forwarder] [DIAG] INVALID JPEG from GetImageSample(): "
                f"size={len(jpeg_bytes)} bytes, head={head}, tail={tail}, "
                f"ts={ts:.6f} (invalid count so far: {invalid_jpeg_count})",
                flush=True,
            )

        payload = {
            "timestamps": {args.mount_position: ts},
            "images":     {args.mount_position: jpeg_bytes},
        }

        socket.send(msgpack.packb(payload, use_bin_type=True))

        # FPS 로그
        frame_count += 1
        now = time.time()
        if now - last_log_time >= args.fps_log_interval:
            fps = frame_count / (now - last_log_time)
            avg_kb = len(jpeg_bytes) / 1024
            print(f"[ltw_camera_forwarder] fps: {fps:.1f} "
                  f"(latest frame: {avg_kb:.1f} KB, "
                  f"total errors: {error_count}, "
                  f"invalid jpeg: {invalid_jpeg_count})", flush=True)
            frame_count = 0
            last_log_time = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ltw_camera_forwarder] Shutdown (KeyboardInterrupt)",
              flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"[ltw_camera_forwarder] Fatal error: {type(e).__name__}: {e}",
              flush=True)
        raise
