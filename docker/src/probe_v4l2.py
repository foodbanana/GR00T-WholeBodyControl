#!/usr/bin/env python3
"""
V4L2(/dev/video*) 경로로 D405 color를 읽을 수 있는지 확인하는 진단.

pyrealsense2(RSUSB 백엔드)가 이 Jetson에서 스트리밍을 못 하는 대신,
커널 uvcvideo가 노출한 /dev/video 노드를 OpenCV(CAP_V4L2)로 직접 읽는
경로가 되는지 검증한다. videohub_pc4가 머리 D435i를 이 방식으로 읽고
있으므로 커널 경로 자체는 정상 — D405 color 노드가 어느 것인지 찾는 게 목적.

컨테이너 안에서 실행 (재빌드 없이 마운트):
    docker run --rm -v /dev:/dev \
      --device-cgroup-rule='c 81:* rmw' --device-cgroup-rule='c 189:* rmw' \
      -v ~/tw_gearsonic/GR00T-WholeBodyControl/docker/src/probe_v4l2.py:/probe_v4l2.py \
      ltw-camera-server:0.9-foxy-3cam python3 /probe_v4l2.py

색이 정상인지 눈으로 보려면 저장 폴더를 마운트:
    ... -v /tmp/v4l2_out:/out ltw-camera-server:0.9-foxy-3cam \
        python3 /probe_v4l2.py --save-dir /out
"""

import argparse
import glob
import os

import cv2


def fourcc_str(v: int) -> str:
    return "".join([chr((int(v) >> (8 * i)) & 0xFF) for i in range(4)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--save-dir", default=None)
    args = ap.parse_args()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    nodes = sorted(
        glob.glob("/dev/video*"),
        key=lambda p: int("".join(c for c in os.path.basename(p) if c.isdigit()) or -1),
    )
    print(f"[v4l2] 노드 {len(nodes)}개 검사\n")

    for path in nodes:
        cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"{path}: open 실패(다른 프로세스 점유 or 비-capture 노드)")
            cap.release()
            continue

        # color를 노려서 YUYV 640x480 요청 (안 먹으면 드라이버 기본값 유지)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fcc = fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))

        frame = None
        for _ in range(10):
            ret, f = cap.read()
            if ret and f is not None:
                frame = f
                break

        if frame is not None:
            tag = ""
            if frame.ndim == 3 and frame.shape[2] == 3:
                tag = " <== COLOR 후보"
            print(f"{path}: OK  set={w}x{h} fourcc={fcc}  frame={frame.shape} {frame.dtype}{tag}")
            if args.save_dir:
                out = os.path.join(args.save_dir, f"{os.path.basename(path)}.png")
                cv2.imwrite(out, frame)
        else:
            print(f"{path}: 프레임 X  (set={w}x{h} fourcc={fcc})")
        cap.release()

    print("\n[v4l2] 완료. 'COLOR 후보'로 뜬 노드가 D405 color 스트림입니다.")
    if args.save_dir:
        print(f"[v4l2] 저장물: {args.save_dir}/videoN.png — 실제 화면/색 확인")


if __name__ == "__main__":
    main()
