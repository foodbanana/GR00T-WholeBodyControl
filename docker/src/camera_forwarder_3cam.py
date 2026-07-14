#!/usr/bin/env python3
"""
ltw-camera-server v6 (0.9-foxy-3cam) 용 3-카메라 forwarder — V4L2 손목 버전.

머리(head/ego_view) + 손목 D405 2대(left_wrist, right_wrist)를 하나의 ZMQ
페이로드로 합쳐 publish 한다.

[왜 V4L2인가]
    이 Jetson(L4T r35.3.1)에서는 pip pyrealsense2(RSUSB/libuvc 백엔드)가
    열거는 되지만 스트리밍(wait_for_frames)이 0프레임이다 — 커널 uvcvideo가
    D405 비디오 인터페이스를 점유해 libuvc가 못 뺏기 때문. 반면 커널 V4L2
    경로는 정상(호스트 videohub_pc4가 머리 D435i를 /dev/video4로 잘 읽음).
    우리는 D405의 color만 필요하므로 librealsense를 버리고 OpenCV의
    cv2.VideoCapture(node, CAP_V4L2)로 color 노드를 직접 읽는다. OpenCV가
    UYVY/YUYV를 BGR로 자동 변환해 (H,W,3)로 준다.

아키텍처 (단일 프로세스, 스레드 병합)
--------------------------------------
    [Head 스레드 = 퍼블리시 클럭]
        unitree_sdk2py VideoClient.GetImageSample() RPC (블로킹, ~36fps)
        새 JPEG 프레임 도착 = "지금 저장할 순간"
          -> 손목 최신 스냅샷을 얹어 한 페이로드로 ZMQ send
    [Left/Right wrist 스레드 = 최신값 유지]
        cv2.VideoCapture(color_node, CAP_V4L2) -> BGR -> JPEG 인코딩
          -> LatestFrame 슬롯에 (jpeg_bytes, ts) 저장 (덮어쓰기)

손목 노드 지정 (--left-node / --right-node)
    다음 중 아무거나 받는다:
      * /dev/videoN                              (직접 노드 — 재부팅 시 번호 바뀔 수 있음)
      * /dev/v4l/by-id/...-video-indexN          (재부팅에도 안정적, 권장)
      * udev serial (예: 255323073651)           (by-id에서 해당 serial 노드를
                                                  찾아 color를 자동 탐지)
    미지정 시: /dev/v4l/by-id 에서 D405들을 찾아 color 노드를 자동 탐지하고
    시리얼(이름) 정렬 순으로 [0]->left_wrist, [1]->right_wrist 배정.
    (D405 #B처럼 udev serial이 비어있는 개체는 by-id 경로로 지정해야 확실하다.)

ZMQ payload (기존 exporter ImageMessageSchema 호환)
    {"timestamps": {mount: ts}, "images": {mount: jpeg_bytes}}  msgpack.
    color는 BGR로 인코딩(exporter가 imdecode 후 BGR->RGB로 뒤집음).
"""

import argparse
import fcntl
import glob
import os
import re
import signal
import sys
import threading
import time

import cv2
import msgpack
import numpy as np
import zmq

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.video.video_client import VideoClient


# =============================================================================
# 최신 프레임 슬롯 (스레드 간 공유)
# =============================================================================
class LatestFrame:
    def __init__(self):
        self._lock = threading.Lock()
        self._payload = None  # (jpeg_bytes, ts)

    def set(self, jpeg_bytes: bytes, ts: float) -> None:
        with self._lock:
            self._payload = (jpeg_bytes, ts)

    def get(self):
        with self._lock:
            return self._payload


# 열린 V4L2 cap 레지스트리 — 종료 시 STREAMOFF(release)를 보장해 D405가
# "스트리밍 중 고아" 상태로 남지 않게 한다(안 그러면 다음 실행이 스트림을
# 재시작 못 해 select timeout으로 죽는다).
_CAPS_LOCK = threading.Lock()
_ACTIVE_CAPS: dict = {}


def _register_cap(mount: str, cap) -> None:
    with _CAPS_LOCK:
        _ACTIVE_CAPS[mount] = cap


def _release_all_caps() -> None:
    with _CAPS_LOCK:
        for cap in _ACTIVE_CAPS.values():
            try:
                cap.release()
            except Exception:
                pass
        _ACTIVE_CAPS.clear()


# =============================================================================
# V4L2 노드 해석 / color 탐지
# =============================================================================
def _open_v4l2(node: str, width: int, height: int):
    # NOTE: 이 설정 순서/구성은 sustained read가 되는 probe_v4l2.py와 정확히
    # 일치시켜야 한다. 과거에 FOURCC 미설정 + BUFFERSIZE=1 로 열었더니 첫
    # 버퍼 소진 후 D405 color 스트림이 멈춰(read가 계속 False) 프레임이 안 왔다.
    # D405 RGB(index4) 노드는 native YUYV 이므로 FOURCC를 YUYV로 명시하고,
    # BUFFERSIZE는 건드리지 않는다(드라이버 기본 큐 사용).
    cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    # D405 RGB(index4)를 raw V4L2로 열면 auto-exposure가 꺼진(고정 저노출) 상태라
    # 밝은 실험실에서도 프레임이 매우 어둡게 나온다. UVC의 V4L2_CID_EXPOSURE_AUTO를
    # 3(Aperture Priority = 자동 노출)로 켜서 장면 밝기에 맞춘다. 밝은 장면에선
    # 짧은 노출을 골라 프레임레이트도 유지된다. (1=Manual)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
    return cap


def _node_yields_color(node: str, width: int, height: int) -> bool:
    cap = _open_v4l2(node, width, height)
    if cap is None:
        return False
    ok = False
    try:
        for _ in range(8):
            ret, frame = cap.read()
            if ret and frame is not None and frame.ndim == 3 and frame.shape[2] == 3:
                ok = True
                break
    finally:
        cap.release()
    return ok


def _byid_index(link: str) -> int:
    """by-id 심볼릭 이름 끝의 -video-indexN 에서 N을 뽑는다(없으면 -1)."""
    base = os.path.basename(link)
    tail = base.rsplit("-video-index", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return -1


def _d405_byid_links():
    """by-id에서 D405 비디오 노드 심볼릭을 카메라별로 묶어 반환.

    returns: {cam_key: [(index, byid_link), ...]}. realpath가 아니라 **안정적인
    by-id 심볼릭 경로**를 담는다 — 장치가 재열거되면 /dev/videoN 번호는 바뀌지만
    by-id 심볼릭은 같은 이름을 유지하며 새 노드로 repoint 되므로, 이 링크를
    들고 있다가 매 open 때 realpath로 재해석하면 재열거에도 견딘다."""
    cams: dict[str, list] = {}
    for link in glob.glob("/dev/v4l/by-id/*Depth_Camera_405*-video-index*"):
        cam_key = os.path.basename(link).split("-video-index")[0]
        cams.setdefault(cam_key, []).append((_byid_index(link), link))
    return cams


def _pick_color_link(index_link_pairs, width: int, height: int):
    """한 카메라의 [(index, byid_link), ...] 중 진짜 RGB color 링크를 고른다.

    D405의 RGB color 스트림은 -video-index4 에 있다(index2도 3채널을 내지만
    IR/보정전 스트림이라 RGB가 아님). index 내림차순으로 color를 찾아 index4를
    우선 선택한다. 반환은 realpath가 아닌 **by-id 링크**(재열거 대비)."""
    for _idx, link in sorted(index_link_pairs, key=lambda x: x[0], reverse=True):
        if _node_yields_color(os.path.realpath(link), width, height):
            return link
    return None


def resolve_color_node(spec: str, width: int, height: int):
    """spec(노드경로 / by-id경로 / udev serial)을 **현재** color /dev/videoN으로.

    매 open 때 호출된다 — by-id/serial이면 재열거 후 바뀐 노드로 재해석된다."""
    # 1) 존재하는 경로면 그대로 realpath (by-id 심볼릭이면 현재 노드로 해석).
    if os.path.exists(spec):
        return os.path.realpath(spec)
    # 2) udev serial로 간주 → 해당 serial 노드들 중 RGB color(index4 우선) 선택
    pairs = [(_byid_index(link), link)
             for link in glob.glob(f"/dev/v4l/by-id/*_{spec}-video-index*")]
    link = _pick_color_link(pairs, width, height)
    return os.path.realpath(link) if link else None


def auto_assign_wrist_links(width: int, height: int):
    """by-id에서 D405들을 찾아 각 카메라의 RGB color **by-id 링크**를 탐지.

    returns: list[(cam_key, byid_link)] (color 못 찾은 카메라는 제외).
    링크를 반환하므로 reader가 재열거 후에도 재해석할 수 있다."""
    out = []
    for cam_key, pairs in sorted(_d405_byid_links().items()):
        link = _pick_color_link(pairs, width, height)
        if link:
            out.append((cam_key, link))
    return out


# =============================================================================
# V4L2 reader 스레드
# =============================================================================
# USBDEVFS_RESET = _IO('U', 20) — USB 장치를 버스 레벨에서 리셋(=물리 replug와
# 동일한 재열거). D405가 raw V4L2 스트리밍 중 wedge(probe control -32 폭주)되면
# 소프트 재오픈으론 안 풀리고, 이 리셋으로 재열거해야 복구된다.
_USBDEVFS_RESET = (ord("U") << 8) | 20  # 0x5514


def _extract_serial(spec: str):
    """by-id spec에서 udev serial을 뽑는다. 예: ..._255323073651-video-index4."""
    m = re.search(r"_(\d+)-video-index", spec or "")
    return m.group(1) if m else None


def _usb_reset_for_serial(serial: str, mount: str) -> bool:
    """주어진 udev serial의 D405를 USBDEVFS_RESET으로 재열거한다(물리 replug 대신).

    /sys(도커 기본 ro 마운트)에서 idProduct=0b5b + serial 일치 장치의
    busnum/devnum을 찾아 /dev/bus/usb/BBB/DDD 에 ioctl. 성공 True."""
    if not serial:
        return False
    for dev in glob.glob("/sys/bus/usb/devices/*/"):
        try:
            if open(dev + "idProduct").read().strip() != "0b5b":
                continue
            if open(dev + "serial").read().strip() != serial:
                continue
            busnum = int(open(dev + "busnum").read().strip())
            devnum = int(open(dev + "devnum").read().strip())
        except (OSError, ValueError):
            continue
        path = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
        try:
            fd = os.open(path, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
                print(f"[3cam][{mount}] USBDEVFS_RESET 완료: {path} (serial={serial})",
                      flush=True)
                return True
            finally:
                os.close(fd)
        except OSError as e:
            print(f"[3cam][{mount}] USB 리셋 실패({path}): {e}", flush=True)
            return False
    print(f"[3cam][{mount}] USB 리셋 대상(serial={serial}) 못 찾음 "
          "(/sys 미마운트 or 장치 없음)", flush=True)
    return False


def _open_and_prime(spec: str, width: int, height: int, mount: str, attempts: int = 6):
    """spec을 **매 시도마다 재해석**해 장치를 열고 첫 프레임을 받는다.

    D405가 스트리밍 중 USB에서 빠졌다 재열거되면 /dev/videoN 번호가 바뀌므로,
    고정 노드가 아니라 원본 spec(by-id/serial)을 resolve_color_node로 매번
    다시 풀어 현재 노드를 얻는다. 몇 번 실패가 쌓이면 wedge로 보고 **USB 리셋**을
    한 번 걸어 재열거시킨다(물리 replug 없이 self-heal).
    성공 시 (cap, first_frame, node), 실패 시 (None, None, None)."""
    serial = _extract_serial(spec)
    did_reset = False
    for a in range(attempts):
        node = resolve_color_node(spec, width, height)
        if node and os.path.exists(node):
            cap = _open_v4l2(node, width, height)
            if cap is not None:
                # 스트리밍 정상이면 첫 read가 ~33ms에 성공. 안 오면 각 read가
                # V4L2 select timeout(~10s)만큼 블록되므로 시도 횟수를 작게 둔다.
                for _ in range(2):
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        return cap, frame, node
                cap.release()
        # 2회쯤 실패가 이어지면 wedge로 보고 USB 리셋 1회 → 재열거 대기
        if serial and not did_reset and a >= 1:
            if _usb_reset_for_serial(serial, mount):
                did_reset = True
                time.sleep(4.0)  # 재열거 + by-id 재생성 시간
                continue
        print(f"[3cam][{mount}] 스트림 프라이밍 실패, 재시도 {a + 1}/{attempts} "
              f"(spec={spec}, node={node}, 장치 settle 대기)", flush=True)
        time.sleep(1.0)
    return None, None, None


def v4l2_grabber(
    spec: str,
    mount: str,
    raw_latest: LatestFrame,
    width: int,
    height: int,
    stop_event: threading.Event,
    ready_event: threading.Event,
) -> None:
    """캡처 전용 스레드. grab+retrieve만 하고 인코딩은 안 하므로 루프가 카메라
    프레임레이트를 항상 앞질러 돈다 → V4L2 드라이버 버퍼가 얕게 유지되어(=상시
    드레인) read가 옛 프레임을 꺼내며 지연이 누적되던 문제가 사라진다. 최신 RAW
    프레임만 raw_latest 슬롯에 담아 인코더 스레드로 넘긴다. cap.read()는 다음
    프레임까지 블록하므로 busy-spin이 아니라 카메라 속도로 페이싱된다."""
    cap, first, node = _open_and_prime(spec, width, height, mount)
    if cap is None:
        print(f"[3cam][{mount}] FATAL: V4L2 스트림 시작 실패(프라이밍 불가): {spec}",
              flush=True)
        stop_event.set()
        ready_event.set()
        return
    _register_cap(mount, cap)

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[3cam][{mount}] V4L2 {node} opened {aw}x{ah}", flush=True)
    fail = 0
    consec_fail = 0
    frame = first  # 프라이밍으로 받은 첫 프레임을 즉시 처리

    try:
        while not stop_event.is_set():
            # cap이 없으면(=직전 wedge로 닫힘) 재프라임. _open_and_prime 내부에서
            # USB 리셋까지 escalate 하므로 물리 replug 없이 self-heal 된다.
            if cap is None:
                cap, frame, newnode = _open_and_prime(spec, width, height, mount, attempts=8)
                if cap is None:
                    time.sleep(1.0)
                    continue
                _register_cap(mount, cap)
                if newnode != node:
                    print(f"[3cam][{mount}] 재접속: node {node} → {newnode}", flush=True)
                node = newnode

            if frame is None:
                ret, frame = cap.read()
            if frame is None or (isinstance(frame, np.ndarray) and frame.size == 0):
                fail += 1
                consec_fail += 1
                if fail <= 3 or fail % 100 == 0:
                    print(f"[3cam][{mount}] read 실패 ({fail})", flush=True)
                # 연속 실패 = 스트림 wedge. cap을 닫고 cap=None으로 두면 위에서
                # 재프라임(USB 리셋 포함)으로 복구한다.
                if consec_fail >= 20:
                    print(f"[3cam][{mount}] 연속 실패 → 복구(재오픈/USB리셋) 진입", flush=True)
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    frame = None
                    consec_fail = 0
                    continue
                time.sleep(0.01)
                continue
            consec_fail = 0
            # 요청 해상도와 다르면 맞춰준다(대개는 일치). cap.read()는 매 호출
            # 새 ndarray를 할당하므로 슬롯에 참조를 담아도 인코더와 안전하다.
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            raw_latest.set(frame, time.time())
            if not ready_event.is_set():
                ready_event.set()
            frame = None  # 소비 완료 — 다음 루프에서 새 프레임을 읽도록 비운다
    finally:
        if cap is not None:
            cap.release()
        print(f"[3cam][{mount}] V4L2 stopped", flush=True)


def wrist_encoder(
    mount: str,
    raw_latest: LatestFrame,
    jpeg_latest: LatestFrame,
    jpeg_quality: int,
    stop_event: threading.Event,
) -> None:
    """인코딩 전용 스레드. 항상 '가장 최신' RAW만 JPEG로 인코딩한다. 인코딩이
    카메라 레이트보다 느려도 큐가 아니라 최신 슬롯을 읽으므로 오래된 프레임은
    자연히 건너뛰고(=지연 누적 없음), 발행 루프는 이 JPEG 슬롯을 그대로 소비한다."""
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
    last_ts = None
    frame_count = 0
    last_log = time.time()
    while not stop_event.is_set():
        snap = raw_latest.get()
        if snap is None or snap[1] == last_ts:
            time.sleep(0.002)  # 아직 새 프레임 없음 — 살짝 쉬고 재확인
            continue
        frame, ts = snap
        last_ts = ts
        ok, buf = cv2.imencode(".jpg", frame, encode_params)
        if not ok:
            continue
        jpeg_latest.set(buf.tobytes(), ts)
        frame_count += 1
        now = time.time()
        if now - last_log >= 5.0:
            print(f"[3cam][{mount}] fps: {frame_count / (now - last_log):.1f} "
                  f"({buf.size / 1024:.1f} KB/frame)", flush=True)
            frame_count = 0
            last_log = now


# =============================================================================
# main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--head-mount", default="ego_view")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--fps-log-interval", type=float, default=5.0)

    parser.add_argument("--left-node", default=None,
                        help="left_wrist color 노드(/dev/videoN | by-id 경로 | udev serial)")
    parser.add_argument("--right-node", default=None,
                        help="right_wrist color 노드(위와 동일 형식)")
    parser.add_argument("--wrist-width", type=int, default=640)
    parser.add_argument("--wrist-height", type=int, default=480)
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument("--no-wrists", action="store_true",
                        help="손목 없이 머리만 forward (기존 v5와 동일)")
    parser.add_argument("--mirror-left-to-right", action="store_true",
                        help="left_wrist를 right_wrist로도 복제 발행. D405 1대만 있을 때 "
                             "exporter --record-wrist-cameras(좌우 2키 필수)로 LeRobot "
                             "포맷을 검증하기 위한 임시용(right는 left와 동일 그림).")
    parser.add_argument("--wrist-ready-timeout", type=float, default=60.0)
    parser.add_argument("--list-devices", action="store_true",
                        help="D405 color 노드 자동 탐지 결과 출력 후 종료")
    args = parser.parse_args()

    # ---- list-devices 모드 -------------------------------------------------
    if args.list_devices:
        cams = auto_assign_wrist_links(args.wrist_width, args.wrist_height)
        if not cams:
            print("[3cam] D405 color 노드를 찾지 못했습니다.", flush=True)
        for cam_key, link in cams:
            print(f"[3cam]   {cam_key}\n           color(by-id) -> {link}"
                  f"\n           현재 노드     -> {os.path.realpath(link)}", flush=True)
        print("\n[3cam] 위 by-id 경로를 --left-node / --right-node 로 지정하세요"
              "(재열거에도 안정적).", flush=True)
        sys.exit(0)

    # ---- ZMQ PUB -----------------------------------------------------------
    ctx = zmq.Context()
    socket = ctx.socket(zmq.PUB)
    # SNDHWM을 안 주면 ZMQ 기본값 1000이라, 느린 구독자(viewer/exporter)가 있으면
    # PUB가 최대 1000개(59Hz면 ~17초!) 프레임을 쌓아뒀다 FIFO로 옛것부터 보내
    # 15초짜리 지연이 생긴다. SUB의 CONFLATE는 수신큐만 비우므로 이걸 못 막는다.
    # 카메라는 항상 '최신 프레임'만 중요하므로 송신큐를 얕게(3) 두어 옛 프레임을
    # 드롭시킨다(SUB의 RCVHWM=3과 대칭). LINGER=0로 종료 시 잔여 전송 대기 없음.
    # (head-only가 안 늦던 이유: SensorServer가 SNDHWM=20을 이미 걸어둠. v6 raw
    #  PUB은 그걸 빠뜨려 기본 1000이 됐던 것이 15초 지연의 진짜 원인.)
    socket.setsockopt(zmq.SNDHWM, 3)
    socket.setsockopt(zmq.LINGER, 0)
    socket.bind(f"tcp://*:{args.port}")
    print(f"[3cam] ZMQ PUB bound to tcp://*:{args.port} (SNDHWM=3, latest-only)", flush=True)

    # ---- 손목 노드 결정 ----------------------------------------------------
    stop_event = threading.Event()
    wrist_threads = []
    wrist_latests = {}  # mount -> LatestFrame

    # docker stop(SIGTERM)/Ctrl+C(SIGINT) 시 카메라를 깨끗이 release(STREAMOFF)한다.
    def _shutdown(signum, _frame):
        print(f"[3cam] signal {signum} → 카메라 release 후 종료", flush=True)
        stop_event.set()
        _release_all_caps()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if not args.no_wrists:
        # mount -> spec(원본: by-id 링크/노드경로/serial). reader가 매 open 때
        # 재해석하므로 재열거에 견딘다. 여기선 시작 시 유효성만 검증한다.
        spec_map = {}
        if args.left_node or args.right_node:
            for mount, spec in (("left_wrist", args.left_node),
                                ("right_wrist", args.right_node)):
                if not spec:
                    continue
                if resolve_color_node(spec, args.wrist_width, args.wrist_height) is None:
                    print(f"[3cam] FATAL: {mount} 노드 해석 실패: '{spec}'", flush=True)
                    sys.exit(1)
                spec_map[mount] = spec
        else:
            cams = auto_assign_wrist_links(args.wrist_width, args.wrist_height)
            if not cams:
                print("[3cam] FATAL: D405 color 노드 0개. 연결/패스스루 확인 또는 "
                      "--no-wrists.", flush=True)
                sys.exit(1)
            mounts = ["left_wrist", "right_wrist"]
            for i, (cam_key, link) in enumerate(cams[:2]):
                spec_map[mounts[i]] = link
            print(f"[3cam] 자동배정(좌우 바뀌면 --left-node/--right-node 고정): "
                  f"{spec_map}", flush=True)
            if len(cams) == 1:
                print("[3cam] 주의: D405 1대만 → left_wrist 로만 forward(단일 검증). "
                      "exporter --record-wrist-cameras는 좌우 2키 필수라 이 상태에선 "
                      "zmq_3cam_check.py로 확인.", flush=True)

        ready_events = {}
        for mount, spec in spec_map.items():
            raw_latest = LatestFrame()    # 캡처 스레드 → 인코더 스레드 (RAW BGR)
            jpeg_latest = LatestFrame()   # 인코더 스레드 → 발행 루프 (JPEG)
            ready = threading.Event()
            wrist_latests[mount] = jpeg_latest
            ready_events[mount] = ready
            tg = threading.Thread(
                target=v4l2_grabber,
                args=(spec, mount, raw_latest, args.wrist_width, args.wrist_height,
                      stop_event, ready),
                daemon=True,
            )
            te = threading.Thread(
                target=wrist_encoder,
                args=(mount, raw_latest, jpeg_latest, args.jpeg_quality, stop_event),
                daemon=True,
            )
            tg.start()
            te.start()
            wrist_threads.append(tg)
            wrist_threads.append(te)

        deadline = time.time() + args.wrist_ready_timeout
        for mount, ready in ready_events.items():
            remaining = deadline - time.time()
            if not ready.wait(timeout=max(0.0, remaining)):
                print(f"[3cam] FATAL: {mount} 첫 프레임 대기 타임아웃", flush=True)
                stop_event.set()
                sys.exit(1)
        if stop_event.is_set():
            print("[3cam] FATAL: 손목 초기화 실패", flush=True)
            sys.exit(1)
        print("[3cam] 손목 카메라 준비 완료", flush=True)

    # ---- 머리 VideoClient ---------------------------------------------------
    print(f"[3cam] CycloneDDS init on {args.interface} (domain 0)", flush=True)
    ChannelFactoryInitialize(0, args.interface)
    print(f"[3cam] VideoClient init (timeout={args.timeout}s)", flush=True)
    client = VideoClient()
    client.SetTimeout(args.timeout)
    client.Init()
    print("[3cam] VideoClient ready, forwarding 3-cam payload...", flush=True)

    # ChannelFactoryInitialize/VideoClient.Init()이 자체 SIGINT/SIGTERM 핸들러를
    # 설치해 우리 것을 덮어썼다(그래서 Ctrl+C 시 카메라 release 없이 강제종료됨).
    # 여기서 다시 설치해 우리 핸들러가 이기게 하여 종료 시 STREAMOFF를 보장한다.
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # ---- 머리 루프 = 퍼블리시 클럭 -----------------------------------------
    frame_count = 0
    error_count = 0
    stale_warn = 0
    last_log = time.time()
    stale_thresh = 1.0

    try:
        while True:
            code, data = client.GetImageSample()
            if code != 0 or not data:
                error_count += 1
                if error_count <= 3 or error_count % 30 == 0:
                    print(f"[3cam] GetImageSample failed: code={code} "
                          f"(errors={error_count})", flush=True)
                time.sleep(0.05)
                continue

            ts = time.time()
            images = {args.head_mount: bytes(data)}
            timestamps = {args.head_mount: ts}

            for mount, latest in wrist_latests.items():
                snap = latest.get()
                if snap is None:
                    break
                jpeg_bytes, ts_w = snap
                images[mount] = jpeg_bytes
                timestamps[mount] = ts_w
                if ts - ts_w > stale_thresh:
                    stale_warn += 1
                    if stale_warn <= 3 or stale_warn % 60 == 0:
                        print(f"[3cam] WARNING: {mount} 프레임 오래됨 "
                              f"({ts - ts_w:.2f}s)", flush=True)
            else:
                # 검증용: left_wrist를 right_wrist로 복제(D405 1대로 2-wrist 스키마 검증)
                if (args.mirror_left_to_right and "left_wrist" in images
                        and "right_wrist" not in images):
                    images["right_wrist"] = images["left_wrist"]
                    timestamps["right_wrist"] = timestamps["left_wrist"]
                socket.send(msgpack.packb(
                    {"timestamps": timestamps, "images": images}, use_bin_type=True))
                frame_count += 1

            now = time.time()
            if now - last_log >= args.fps_log_interval:
                fps = frame_count / (now - last_log)
                print(f"[3cam] publish fps: {fps:.1f} | keys={sorted(images.keys())} "
                      f"| head errors={error_count}", flush=True)
                frame_count = 0
                last_log = now
    finally:
        stop_event.set()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[3cam] Shutdown (KeyboardInterrupt)", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"[3cam] Fatal error: {type(e).__name__}: {e}", flush=True)
        raise
