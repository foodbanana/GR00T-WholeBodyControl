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

[머리 백엔드 2종 — --head-backend]
    videohub  (기본, 기존 known-good):
        unitree_sdk2py VideoClient.GetImageSample() RPC로 호스트 videohub_pc4가
        중계하는 D435i JPEG를 받는다. 1080p로만 오므로 --head-resize로 Orin에서
        디코드→리사이즈→재인코딩해야 하고, 실제 새 프레임은 ~15Hz다(RPC는 새
        프레임이 없으면 직전 바이트를 재반환한다).
    realsense (2026-07-21 실측으로 전제가 바뀜 — 아래 필독):
        librealsense(pyrealsense2)로 D435i를 직접 연다. 640x480@30을 bgr8로
        **센서에 직접 요청**하므로 1080p 디코드·리사이즈·재인코딩이 통째로
        사라진다(--head-resize 불필요). 손목과 완전히 같은 파이프라인이 되고,
        wedge 시 rs.device.hardware_reset()이라는 복구 수단이 생긴다.
        이 경로는 CycloneDDS/VideoClient를 아예 초기화하지 않는다.

        ★★ pip wheel 로는 안 된다 — 2026-07-21 실측으로 확정 ★★
          당초 "pip pyrealsense2 wheel은 RSUSB(libuvc/libusb) 백엔드라 커널
          uvcvideo를 우회하므로 videohub이 STREAMON 독점(EBUSY)해도 공존한다"
          고 적었으나, **틀렸다.** probe_realsense_head.py 실행 결과:

              xioctl(VIDIOC_S_FMT) failed, errno=16 Device or resource busy

          VIDIOC_S_FMT는 V4L2 ioctl이다. RSUSB였다면 libusb 계열 에러
          (Failed to claim interface / LIBUSB_ERROR_BUSY)가 떠야 한다. 즉
          **이 wheel은 V4L2 백엔드로 빌드돼 있고**, cv2.VideoCapture와 똑같이
          videohub의 EBUSY에 막힌다. 위 "[왜 V4L2인가]" 블록의 D405 0프레임
          기록과도 정확히 일치한다(그 결론이 옳았다).

          ★ 그 뒤 videohub을 정지해 EBUSY를 없앤 뒤에도 [4]가 0프레임이었고,
            커널이 "uvcvideo: Failed to query (GET_CUR) UVC control 1 on
            unit 3: -32" 를 찍었다. -32=EPIPE, unit 3=UVC XU. 즉 L4T 순정
            uvcvideo가 librealsense의 XU 컨트롤을 통과시키지 못하는 것이
            진짜 원인이다(librealsense #5302). USB 논리적 replug로도 동일 —
            장치 wedge가 아니라 커널 드라이버의 구조적 한계다.
          ★ 해결: v8 이미지(1.1-foxy-3cam)는 librealsense를 소스에서
            -DFORCE_RSUSB_BACKEND=true 로 빌드해 V4L2/uvcvideo를 통째로
            우회한다. 커널 패치(공유 로봇에 영향)를 피하는 유일한 길이다.
            **이 forwarder를 --head-backend realsense 로 쓰려면 v8 이미지가
            필요하다.** v7 이미지(pip wheel)로는 0프레임이다.

        ★★ v8 실측 결과 (2026-07-21) — 목표 달성 ★★
          content(new frames): ego_view=29.2Hz  left_wrist=30.0Hz  right_wrist=30.0Hz
          publish fps 29.2, head errors 0, 머리 JPEG 13.3KB/frame.
          videohub RPC 경로(~15Hz) 대비 약 2배이고, 1080p 디코드->리사이즈->
          재인코딩이 사라져 Orin CPU도 크게 줄었다.

        ★ videohub 과의 관계 — "공존"이 아니라 "밀어내기"다
          RSUSB 가 USB 인터페이스를 claim 하면 커널 uvcvideo 가 detach 되고,
          videohub_pc4 의 V4L2 스트림이 끊겨 프로세스가 사라진다. 그리고
          **자동으로 되살아나지 않는다**(master_service 가 재시작을 포기함).
          이유: 이 과정에서 D435i 가 여러 번 재열거되어 /dev/videoN 번호가
          밀리는데, master_service 는 videohub 을 `/dev/video4` 로 하드코딩해
          띄우기 때문이다. 실제로 실행 후 /dev/video4 는 **손목 D405** 를
          가리키게 됐다 — 이 상태에서 videohub 을 수동 기동하면 머리가 아니라
          손목을 점유하므로 **절대 하지 말 것.**
          => videohub 복구는 **재부팅**이 유일하고 확실한 방법이다.
             (2026-07-21 팀 승인 라이프사이클의 "G1 전원 재투입 -> 원상태"에
              해당한다.)

        ★ 손목 노드도 같은 이유로 밀린다 — 그러나 문제되지 않는다.
          실측에서 머리 시작 직후 손목이 REQBUFS errno=19 로 실패했지만,
          by-id 재해석 로직이 새 노드를 찾아 자동 복구했다
          (video15->video16, video10->video11). 그 뒤 계속 30.0Hz.
          **손목은 반드시 by-id 경로로 지정할 것**(/dev/videoN 직접 지정 금지).

아키텍처 (단일 프로세스, 스레드 병합)
--------------------------------------
    [발행 클럭 = 머리 새 프레임 도착]
        videohub  : GetImageSample() RPC 블로킹 반환이 곧 클럭
        realsense : rs grabber -> encoder -> head_jpeg 슬롯의 ts 변화가 클럭
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


def _unregister_cap(mount: str) -> None:
    with _CAPS_LOCK:
        _ACTIVE_CAPS.pop(mount, None)


def _release_all_caps() -> None:
    """최후 수단 — 소유 스레드가 제때 못 놓았을 때만 쓴다.
    ★ 소유 grabber 가 cap.read() 안에 있는 동안 다른 스레드에서 release() 를
      부르면 OpenCV(스레드 안전하지 않음)에서 멈출 수 있다. 평시에는 각
      grabber 가 자기 finally 에서 놓게 하고, 이 함수는 그게 실패했을 때만 쓴다."""
    with _CAPS_LOCK:
        for cap in _ACTIVE_CAPS.values():
            try:
                cap.release()
            except Exception:
                pass
        _ACTIVE_CAPS.clear()


# 열린 librealsense pipeline 레지스트리 — V4L2 cap과 같은 이유로 필요하다.
# grabber 스레드의 finally에 pipeline.stop()이 있지만 **그것만으로는 부족하다**:
# 스레드가 daemon이라 SIGTERM 처리 중 sys.exit()가 나면 finally가 실행되기 전에
# 프로세스가 죽는다. 그러면 D435i가 스트리밍 중 고아로 남아 다음 실행이 장치를
# 못 연다(그때는 USB 재열거나 물리 replug가 필요하다).
_RS_LOCK = threading.Lock()
_ACTIVE_PIPELINES: dict = {}


def _register_pipeline(mount: str, pipeline) -> None:
    with _RS_LOCK:
        _ACTIVE_PIPELINES[mount] = pipeline


def _unregister_pipeline(mount: str) -> None:
    with _RS_LOCK:
        _ACTIVE_PIPELINES.pop(mount, None)


def _stop_all_pipelines() -> None:
    with _RS_LOCK:
        for pipeline in _ACTIVE_PIPELINES.values():
            try:
                pipeline.stop()
            except Exception:
                pass
        _ACTIVE_PIPELINES.clear()


def _release_all_devices() -> None:
    """종료 시 모든 카메라를 깨끗이 놓는다(V4L2 STREAMOFF + rs pipeline stop)."""
    _release_all_caps()
    _stop_all_pipelines()


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
        _unregister_cap(mount)
        print(f"[3cam][{mount}] V4L2 stopped", flush=True)


# =============================================================================
# librealsense (RSUSB) reader 스레드 — 머리 D435i 직결
# =============================================================================
def _rs_pick_head_serial(rs, want_serial: str):
    """연결된 RealSense 중 머리로 쓸 장치의 시리얼을 고른다.

    want_serial이 주어지면 그것이 실재하는지 확인만 하고 그대로 쓴다.
    미지정 시 D405가 **아닌** 첫 장치를 고른다 — D405 2대는 손목이고 V4L2로
    이미 열려 있으므로 librealsense가 절대 건드리면 안 된다."""
    ctx = rs.context()
    found = []
    for dev in ctx.query_devices():
        try:
            name = dev.get_info(rs.camera_info.name)
            serial = dev.get_info(rs.camera_info.serial_number)
        except Exception:
            continue
        found.append((name, serial))
    print(f"[3cam][head] librealsense 장치 목록: {found}", flush=True)
    if want_serial:
        if any(s == want_serial for _n, s in found):
            return want_serial
        print(f"[3cam][head] 경고: 지정 시리얼 {want_serial} 미발견 — 그대로 시도",
              flush=True)
        return want_serial
    for name, serial in found:
        if "405" not in name:          # D405 = 손목(V4L2 점유 중), 제외
            return serial
    return None


def realsense_grabber(
    serial: str,
    mount: str,
    raw_latest: LatestFrame,
    width: int,
    height: int,
    fps: int,
    stop_event: threading.Event,
    ready_event: threading.Event,
) -> None:
    """머리 D435i를 librealsense로 직접 읽는 캡처 전용 스레드.

    videohub이 커널 V4L2 노드를 STREAMON 독점(EBUSY)하고 있어도, RSUSB 백엔드는
    libusb로 USB 인터페이스를 직접 claim 하므로 공존한다. 따라서 이 스레드는
    호스트 서비스를 정지시키지 않는다.

    v4l2_grabber와 동일하게 "캡처만" 하고 인코딩은 encoder 스레드에 넘긴다.
    wait_for_frames가 다음 프레임까지 블록하므로 카메라 속도로 페이싱된다.
    스트림이 죽으면 pipeline 재시작 → 그래도 안 되면 hardware_reset()으로
    에스컬레이션한다(D405 wedge에 물리 replug밖에 없던 것과 달리, 여기엔
    펌웨어 리셋 경로가 있다)."""
    import pyrealsense2 as rs

    pipeline = None
    did_reset = False
    consec_fail = 0
    try:
        while not stop_event.is_set():
            # ---- (재)시작 ---------------------------------------------------
            if pipeline is None:
                try:
                    if serial is None:
                        serial = _rs_pick_head_serial(rs, None)
                        if serial is None:
                            raise RuntimeError("머리로 쓸 RealSense 장치를 못 찾음")
                    cfg = rs.config()
                    cfg.enable_device(serial)
                    # bgr8을 직접 요청한다 — librealsense가 D435i의 native YUYV를
                    # 변환해 주므로 우리가 cvtColor를 할 필요가 없고, exporter가
                    # 기대하는 BGR과도 그대로 맞는다.
                    cfg.enable_stream(rs.stream.color, width, height,
                                      rs.format.bgr8, fps)
                    pipeline = rs.pipeline()
                    pipeline.start(cfg)
                    _register_pipeline(mount, pipeline)
                    print(f"[3cam][{mount}] librealsense started "
                          f"serial={serial} {width}x{height}@{fps} bgr8", flush=True)
                    consec_fail = 0
                except Exception as e:
                    pipeline = None
                    print(f"[3cam][{mount}] librealsense start 실패: "
                          f"{type(e).__name__}: {e}", flush=True)
                    # 첫 시작부터 계속 실패하면 한 번은 펌웨어 리셋을 걸어본다.
                    consec_fail += 1
                    if consec_fail >= 3 and not did_reset:
                        did_reset = _rs_hardware_reset(rs, serial, mount)
                        if did_reset:
                            time.sleep(5.0)   # 재열거 대기
                            continue
                    time.sleep(2.0)
                    continue

            # ---- 프레임 취득 -------------------------------------------------
            try:
                # 위치인자로 넘긴다 — pybind11 바인딩의 키워드 인자명(timeout_ms)이
                # 버전에 따라 다를 수 있어 TypeError로 죽는 것을 피한다.
                # 타임아웃을 1초로 둔 이유: 이 호출이 블록되는 동안에는 stop_event를
                # 못 보므로, 길면 종료가 그만큼 늦어진다. 30fps 스트림에서 1초는
                # 이미 충분히 관대한 값이다(정상이면 33ms에 돌아온다).
                frames = pipeline.wait_for_frames(1000)
                color = frames.get_color_frame()
                if not color:
                    consec_fail += 1
                    continue
                # get_data()는 pipeline 소유 버퍼를 가리키므로 반드시 복사한다.
                # (복사 안 하면 다음 프레임이 같은 메모리를 덮어써서 encoder가
                #  찢어진 프레임을 인코딩한다.)
                frame = np.array(np.asanyarray(color.get_data()), copy=True)
            except Exception as e:
                consec_fail += 1
                if consec_fail <= 3 or consec_fail % 20 == 0:
                    print(f"[3cam][{mount}] wait_for_frames 실패({consec_fail}): "
                          f"{type(e).__name__}: {e}", flush=True)
                # 연속 실패 = 스트림 wedge. 재시작 → 그래도 안 되면 HW 리셋.
                if consec_fail >= 10:
                    try:
                        pipeline.stop()
                    except Exception:
                        pass
                    _unregister_pipeline(mount)
                    pipeline = None
                    if consec_fail >= 30 and not did_reset:
                        did_reset = _rs_hardware_reset(rs, serial, mount)
                        if did_reset:
                            time.sleep(5.0)
                    consec_fail = 0
                continue

            consec_fail = 0
            did_reset = False   # 정상 프레임이 왔으면 리셋 예산을 되돌린다
            raw_latest.set(frame, time.time())
            if not ready_event.is_set():
                ready_event.set()
    finally:
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                pass
        _unregister_pipeline(mount)
        print(f"[3cam][{mount}] librealsense stopped", flush=True)


def _rs_hardware_reset(rs, serial: str, mount: str) -> bool:
    """해당 시리얼의 RealSense에 펌웨어 리셋을 건다(USB 재열거 유발)."""
    try:
        for dev in rs.context().query_devices():
            if dev.get_info(rs.camera_info.serial_number) == serial:
                dev.hardware_reset()
                print(f"[3cam][{mount}] hardware_reset() 발행 (serial={serial})",
                      flush=True)
                return True
    except Exception as e:
        print(f"[3cam][{mount}] hardware_reset 실패: {type(e).__name__}: {e}",
              flush=True)
    return False


def wrist_encoder(
    mount: str,
    raw_latest: LatestFrame,
    jpeg_latest: LatestFrame,
    jpeg_quality: int,
    stop_event: threading.Event,
    content_hz: dict = None,
) -> None:
    """인코딩 전용 스레드. 항상 '가장 최신' RAW만 JPEG로 인코딩한다. 인코딩이
    카메라 레이트보다 느려도 큐가 아니라 최신 슬롯을 읽으므로 오래된 프레임은
    자연히 건너뛰고(=지연 누적 없음), 발행 루프는 이 JPEG 슬롯을 그대로 소비한다.

    content_hz(옵션): 진단용 공유 딕셔너리. JPEG 바이트를 해싱해 '내용이 실제로
    바뀐' 프레임만 세어 mount별 콘텐츠 Hz를 기록한다. wedge(같은 옛 프레임 반복)
    시 fps는 30이어도 content_hz는 0 근처로 떨어져 실시간 감지된다."""
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
    last_ts = None
    frame_count = 0
    content_count = 0      # 내용이 바뀐(=새로운) 프레임 수
    last_hash = None
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
        jpeg_bytes = buf.tobytes()
        jpeg_latest.set(jpeg_bytes, ts)
        frame_count += 1
        h = hash(jpeg_bytes)          # 내용 변화 감지(wedge면 동일 바이트 반복)
        if h != last_hash:
            content_count += 1
            last_hash = h
        now = time.time()
        if now - last_log >= 5.0:
            dt = now - last_log
            if content_hz is not None:
                content_hz[mount] = content_count / dt
            print(f"[3cam][{mount}] fps: {frame_count / dt:.1f} "
                  f"| content: {content_count / dt:.1f}Hz "
                  f"({buf.size / 1024:.1f} KB/frame)", flush=True)
            frame_count = 0
            content_count = 0
            last_log = now


# =============================================================================
# main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="eth0")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--head-mount", default="ego_view")
    parser.add_argument("--head-resize", default=None,
                        help="머리(ego_view)를 이 크기로 Orin에서 사전 리사이즈해 발행 "
                             "(예: 640x480). 미지정 시 원본(1080p) JPEG 그대로 relay. 지정 시 "
                             "RPC JPEG를 디코드→리사이즈→재인코딩 → DGX 디코드/리사이즈 부담↓ + "
                             "대역폭↓. Orin CPU 여유 있을 때 사용(3-cam 30fps 목적). exporter는 "
                             "이때 --camera-decode-reduce 1 로(이미 640x480이라 reduce 불필요).")
    parser.add_argument("--head-backend", default="videohub",
                        choices=["videohub", "realsense"],
                        help="머리 영상 취득 경로. videohub=VideoClient RPC(기본, "
                             "기존 known-good). realsense=librealsense로 D435i 직결 "
                             "— videohub을 정지시키지 않고 공존하며, 원하는 해상도를 "
                             "직접 요청하므로 1080p 디코드/리사이즈/재인코딩이 사라진다.")
    parser.add_argument("--head-serial", default=None,
                        help="realsense 백엔드에서 쓸 D435i 시리얼(예: 253843061423). "
                             "미지정 시 D405가 아닌 첫 장치를 자동 선택.")
    parser.add_argument("--head-width", type=int, default=640,
                        help="realsense 백엔드의 머리 color 요청 폭")
    parser.add_argument("--head-height", type=int, default=480,
                        help="realsense 백엔드의 머리 color 요청 높이")
    parser.add_argument("--head-fps", type=int, default=30,
                        help="realsense 백엔드의 머리 color 요청 fps")
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
        # librealsense가 보는 장치도 함께 출력한다 — --head-serial 값을 여기서
        # 그대로 복사하면 된다. videohub이 떠 있어도 열거는 된다.
        try:
            import pyrealsense2 as rs
            print("\n[3cam] librealsense 장치(머리 --head-serial 용):", flush=True)
            for dev in rs.context().query_devices():
                print(f"[3cam]   {dev.get_info(rs.camera_info.name)}  "
                      f"serial={dev.get_info(rs.camera_info.serial_number)}",
                      flush=True)
        except Exception as e:
            print(f"[3cam] librealsense 열거 실패: {type(e).__name__}: {e}", flush=True)
        sys.exit(0)

    # ---- 머리 사전 리사이즈 파싱 (예: "640x480") ----------------------------
    use_rs_head = (args.head_backend == "realsense")
    head_resize = None
    if use_rs_head and args.head_resize:
        # realsense는 원하는 해상도를 센서에 직접 요청하므로 사후 리사이즈는
        # 순수 낭비다(디코드→리사이즈→재인코딩 사이클이 다시 생긴다).
        print(f"[3cam] --head-resize는 realsense 백엔드에서 무시된다 "
              f"(--head-width/--head-height {args.head_width}x{args.head_height}로 "
              f"직접 요청).", flush=True)
        args.head_resize = None
    if args.head_resize:
        _hw, _hh = args.head_resize.lower().split("x")
        head_resize = (int(_hw), int(_hh))
        print(f"[3cam] 머리 사전 리사이즈 ON: {head_resize[0]}x{head_resize[1]} "
              f"(RPC 1080p JPEG → 디코드→리사이즈→재인코딩). exporter는 "
              f"--camera-decode-reduce 1 권장.", flush=True)

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
    content_hz = {}     # 진단용: mount -> 실제 콘텐츠 Hz (손목 encoder가 갱신)

    # docker stop(SIGTERM)/Ctrl+C(SIGINT) 시 카메라를 깨끗이 release 한다.
    #
    # ★ 재진입 방지가 필수다 (2026-07-21 실측으로 확인)
    #   파이썬 시그널 핸들러는 **메인 스레드에서** 실행된다. 핸들러가 락을 잡고
    #   있는 동안 다음 SIGINT 가 들어오면 같은 스레드에서 핸들러가 **중첩 실행**
    #   되고, threading.Lock 은 재진입 불가라 자기 자신이 이미 잡은 락에 영원히
    #   막힌다. 바깥 프레임은 재개될 수 없으므로 메인 스레드가 통째로 굳는다.
    #   증상: ^C 를 누를수록 "signal 2 → ..." 만 반복 출력되고 안 죽는다.
    #   -> 핸들러 진입 즉시 이후 시그널을 "무조건 즉시 종료"로 바꿔 중첩을 막는다.
    _shutting_down = {"v": False}

    def _force_exit(signum, _frame):
        os._exit(130)

    def _shutdown(signum, _frame):
        if _shutting_down["v"]:
            os._exit(130)
        _shutting_down["v"] = True
        # 이 시점 이후의 SIGINT/SIGTERM 은 락을 건드리지 않고 즉시 죽인다.
        signal.signal(signal.SIGINT, _force_exit)
        signal.signal(signal.SIGTERM, _force_exit)

        # ★ librealsense pipeline 은 **여기서 stop() 하지 않는다.**
        #   grabber 스레드가 wait_for_frames 안에 블록돼 있을 때 다른 스레드에서
        #   같은 pipeline 에 stop() 을 걸면 교착된다. 대신 stop_event 만 세우고,
        #   pipeline.stop() 은 그 pipeline 을 소유한 grabber 스레드가 자기
        #   finally 에서 하게 둔다.
        print(f"[3cam] signal {signum} → 카메라 release 후 종료", flush=True)
        stop_event.set()

        # ★ librealsense pipeline 정리를 **기다리지 않는다** (2026-07-21 결론)
        #   처음엔 grabber 가 pipeline.stop() 을 끝낼 때까지 3초 기다렸는데,
        #   pipeline.stop() 이 그보다 오래 걸려서 사용자 체감으로는 "^C 를 눌러도
        #   안 죽는다"가 됐다. 그리고 그 대기는 **불필요하다**:
        #     `docker rm -f` 로 컨테이너를 강제 종료(SIGKILL)한 뒤에도 다음 실행이
        #     정상적으로 `librealsense started` 했다. RSUSB 는 프로세스가 죽으면
        #     libusb 가 인터페이스를 놓고 커널이 재바인딩하기 때문이다.
        #   V4L2 에서 겪은 "고아 스트림" 문제(그래서 _ACTIVE_CAPS 가 있다)를
        #   RSUSB 에 그대로 옮긴 것이 과했다. V4L2 정리만 확실히 하고 즉시 죽는다.
        #
        #   짧게(0.3초)만 기다리는 이유: 그 사이에 grabber 가 끝나면 깨끗한 stop 이
        #   덤으로 얻어진다. 안 끝나도 손해가 없으므로 기다림을 늘리지 않는다.
        #   각 스레드는 자기 장치를 자기 finally 에서 놓고 레지스트리에서
        #   스스로 빠진다. 여기서는 그게 끝나기를 잠깐 기다리기만 한다.
        #   (V4L2 도 마찬가지다 — 핸들러가 cap.release() 를 직접 부르면 소유
        #    grabber 가 cap.read() 안에 있을 때 OpenCV 에서 멈출 수 있다.
        #    실측상 grabber 는 33ms 안에 read 에서 돌아오므로 금방 끝난다.)
        deadline = time.time() + 1.0
        while time.time() < deadline and (_ACTIVE_CAPS or _ACTIVE_PIPELINES):
            time.sleep(0.02)

        # 최후 수단: 소유 스레드가 못 놓은 V4L2 만 여기서 정리한다. D405 는
        # 스트리밍 중 고아로 남으면 다음 실행이 select timeout 으로 죽는 전례가
        # 있어서(그래서 _ACTIVE_CAPS 가 존재한다) 이건 포기하지 않는다.
        if _ACTIVE_CAPS:
            print(f"[3cam] 미정리 V4L2 {sorted(_ACTIVE_CAPS)} → 강제 release",
                  flush=True)
            _release_all_caps()

        # os._exit: 인터프리터 종료 절차를 건너뛰고 즉시 죽는다. sys.exit()는
        # SystemExit 예외라서, 데몬 스레드가 C 확장 안에 갇혀 있으면 finalize
        # 단계에서 다시 멈출 수 있다.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

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
                args=(mount, raw_latest, jpeg_latest, args.jpeg_quality, stop_event,
                      content_hz),
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

    # ---- 머리 백엔드 기동 ---------------------------------------------------
    client = None
    head_jpeg_latest = None
    if use_rs_head:
        # librealsense 직결. CycloneDDS/VideoClient를 아예 초기화하지 않는다
        # — 머리가 DDS에 의존하지 않게 되는 것이 이 경로의 부수 이득이다.
        head_raw = LatestFrame()
        head_jpeg_latest = LatestFrame()
        head_ready = threading.Event()
        tg = threading.Thread(
            target=realsense_grabber,
            args=(args.head_serial, args.head_mount, head_raw,
                  args.head_width, args.head_height, args.head_fps,
                  stop_event, head_ready),
            daemon=True,
        )
        te = threading.Thread(
            target=wrist_encoder,
            args=(args.head_mount, head_raw, head_jpeg_latest, args.jpeg_quality,
                  stop_event, content_hz),
            daemon=True,
        )
        tg.start()
        te.start()
        print("[3cam] 머리 백엔드=realsense (librealsense 직결, videohub 정지 불필요). "
              "첫 프레임 대기...", flush=True)
        if not head_ready.wait(timeout=args.wrist_ready_timeout):
            print("[3cam] FATAL: 머리(realsense) 첫 프레임 대기 타임아웃", flush=True)
            stop_event.set()
            _release_all_devices()
            sys.exit(1)
        print("[3cam] 머리 카메라 준비 완료, forwarding 3-cam payload...", flush=True)
    else:
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
    head_content_count = 0   # 머리 내용이 실제로 바뀐 프레임 수(RPC가 새 JPEG 반환)
    head_last_hash = None
    last_log = time.time()
    stale_thresh = 1.0

    head_last_pub_ts = None

    try:
        while True:
            if use_rs_head:
                # 발행 클럭 = 머리 JPEG 슬롯의 ts 변화. encoder가 항상 최신 RAW만
                # 인코딩하므로 여기서 옛 프레임을 볼 일이 없다.
                snap = None
                while not stop_event.is_set():
                    snap = head_jpeg_latest.get()
                    if snap is not None and snap[1] != head_last_pub_ts:
                        break
                    snap = None
                    time.sleep(0.002)
                if snap is None:
                    break
                head_bytes, ts = snap
                head_last_pub_ts = ts
                # 콘텐츠 Hz는 encoder가 content_hz[head_mount]에 이미 기록한다.
            else:
                code, data = client.GetImageSample()
                if code != 0 or not data:
                    error_count += 1
                    if error_count <= 3 or error_count % 30 == 0:
                        print(f"[3cam] GetImageSample failed: code={code} "
                              f"(errors={error_count})", flush=True)
                    time.sleep(0.05)
                    continue

                ts = time.time()
                head_bytes = bytes(data)
                # 진단: RPC가 준 raw 바이트(리사이즈 전 = 진짜 소스 콘텐츠)를 해싱.
                # GetImageSample은 새 프레임이 없으면 직전 바이트를 재반환하므로,
                # 바이트가 바뀔 때만 세면 videohub의 실제 콘텐츠 Hz가 나온다.
                _hh = hash(head_bytes)
                if _hh != head_last_hash:
                    head_content_count += 1
                    head_last_hash = _hh
            # 사전 리사이즈: RPC 1080p JPEG를 Orin에서 640x480으로 줄여 재인코딩.
            # DGX는 작은 640x480만 디코드(리사이즈 불필요) → 3-cam 30fps 목적.
            if head_resize is not None:
                _m = cv2.imdecode(np.frombuffer(head_bytes, np.uint8), cv2.IMREAD_COLOR)
                if _m is not None:
                    _m = cv2.resize(_m, head_resize, interpolation=cv2.INTER_AREA)
                    _ok, _b = cv2.imencode(".jpg", _m,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), args.jpeg_quality])
                    if _ok:
                        head_bytes = _b.tobytes()
            images = {args.head_mount: head_bytes}
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
                dt = now - last_log
                fps = frame_count / dt
                # 통합 콘텐츠 Hz 줄: 각 카메라 영상이 '실제로 새로 바뀌는' 속도.
                # (publish fps와 다름 — publish는 루프 회전마다 나가고, 머리는
                #  RPC 재탕분이 섞여 콘텐츠 Hz < publish Hz 인 게 정상.)
                if use_rs_head:
                    parts = [f"{args.head_mount}="
                             f"{content_hz.get(args.head_mount, 0.0):.1f}Hz"]
                else:
                    parts = [f"{args.head_mount}={head_content_count / dt:.1f}Hz"]
                for m in sorted(content_hz.keys()):
                    if m == args.head_mount:
                        continue          # 머리는 위에서 이미 넣었다
                    parts.append(f"{m}={content_hz[m]:.1f}Hz")
                print(f"[3cam] content(new frames): {'  '.join(parts)}", flush=True)
                print(f"[3cam] publish fps: {fps:.1f} | keys={sorted(images.keys())} "
                      f"| head errors={error_count}", flush=True)
                frame_count = 0
                head_content_count = 0
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
