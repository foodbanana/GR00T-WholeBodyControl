#!/usr/bin/env python3
"""ltw_camera_subscriber: /frontvideostream (ROS 2) -> ZMQ PUB (msgpack).

이 파일이 컨테이너 안의 유일한 파이썬 로직이다. 하는 일은 딱 두 가지뿐:
  1. videohub_pc4가 publish하는 ROS 2 topic /frontvideostream
     (unitree_go/msg/Go2FrontVideoData)을 subscribe해서 raw JPEG bytes를
     꺼낸다 (video720p / video360p / video180p 중 하나, 재인코딩 없음).
  2. {"timestamps": {mount: ts}, "images": {mount: jpeg_bytes}} 형태로
     msgpack pack해서 ZMQ PUB 소켓(tcp://*:5555)으로 내보낸다.

gear_sonic을 쓰지 않는 이유: gear_sonic.camera.drivers.unitree_ros2 등은
워크스테이션(Python 3.10, DGX Spark)에서 개발된 코드라 이 컨테이너의
Python 3.8 (ROS 2 Foxy native)과 문법/의존성이 맞지 않는다. 이 subscriber는
필요한 로직만 빼서 이 컨테이너에 맞게 독립적으로 다시 짠 것이다.

Payload 포맷은 DGX Spark 쪽 gear_sonic.camera.composed_camera의
ComposedCameraClientSensor / run_data_exporter.py가 그대로 소비할 수 있도록
맞췄다 — "ego_view"라는 key는 LeRobot 데이터셋의
observation.images.ego_view 필드와 1:1로 매칭된다.
"""

import argparse
import logging
import sys
import time

import msgpack
import rclpy
import zmq
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from unitree_go.msg import Go2FrontVideoData

# resolution CLI 옵션 -> Go2FrontVideoData 메시지 필드 이름 매핑.
_RESOLUTION_FIELD_MAP = {
    "720p": "video720p",
    "360p": "video360p",
    "180p": "video180p",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", default="/frontvideostream")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument(
        "--resolution",
        choices=sorted(_RESOLUTION_FIELD_MAP),
        default="720p",
    )
    parser.add_argument("--mount-position", default="ego_view")
    parser.add_argument(
        "--log-interval",
        type=float,
        default=5.0,
        help="fps 로그를 몇 초마다 찍을지",
    )
    parser.add_argument("--node-name", default="ltw_camera_subscriber")
    return parser.parse_args()


class CameraForwarder(Node):
    def __init__(self, args: argparse.Namespace, socket: "zmq.Socket") -> None:
        super().__init__(args.node_name)
        self._field_name = _RESOLUTION_FIELD_MAP[args.resolution]
        self._mount = args.mount_position
        self._socket = socket
        self._log_interval = args.log_interval

        self._frame_count = 0
        self._byte_count = 0
        self._last_log_time = time.monotonic()

        # RELIABLE: videohub_pc4 publisher가 RELIABLE로 publish 중이라
        # 여기도 RELIABLE로 맞춰야 discovery/전달이 성립한다. BEST_EFFORT로
        # 두면 QoS mismatch로 subscription은 생성되지만 콜백이 한 번도
        # 불리지 않는, 겉으로는 조용한 실패가 난다 (다른 시도들에서 겪은
        # 패턴과 동일).
        # VOLATILE + KEEP_LAST(depth=1): 실시간 영상 스트림이므로 늦게
        # 붙는 subscriber를 위해 과거 프레임을 들고 있을 필요가 없다.
        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self.create_subscription(
            Go2FrontVideoData, args.topic, self._on_frame, qos
        )
        self.get_logger().info(
            f"Subscribed to {args.topic} (QoS: RELIABLE, field={self._field_name})"
        )
        self.get_logger().info("Waiting for first frame...")

    def _on_frame(self, msg: Go2FrontVideoData) -> None:
        jpeg_bytes = bytes(getattr(msg, self._field_name))
        if not jpeg_bytes:
            # videohub_pc4가 아직 워밍업 중이거나 요청한 해상도를 채우지
            # 않은 프레임 — 빈 이미지를 내보내봤자 exporter만 혼란스러워지므로
            # 그냥 스킵.
            return

        payload = {
            "timestamps": {self._mount: time.time()},
            "images": {self._mount: jpeg_bytes},
        }
        # use_bin_type=True 필수: 이게 없으면 msgpack이 bytes와 str을
        # 구분하지 않고 둘 다 raw로 pack해서, 워크스테이션 exporter가
        # unpack할 때 JPEG bytes를 str로 잘못 디코딩하려다 실패한다.
        packed = msgpack.packb(payload, use_bin_type=True)

        try:
            # NOBLOCK + drop-on-slow-subscriber: 실시간 스트림에서는 밀린
            # 오래된 프레임을 버퍼에 쌓아 나중에 보내는 것보다, 못 받으면
            # 버리고 최신 프레임 우선으로 계속 흘려보내는 쪽이 낫다.
            self._socket.send(packed, zmq.NOBLOCK)
        except zmq.Again:
            pass

        self._frame_count += 1
        self._byte_count += len(jpeg_bytes)
        self._maybe_log_fps()

    def _maybe_log_fps(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_log_time
        if elapsed < self._log_interval:
            return

        fps = self._frame_count / elapsed
        avg_bytes = self._byte_count / self._frame_count if self._frame_count else 0
        self.get_logger().info(
            f"fps: {fps:.1f} (bytes/frame avg: {avg_bytes:.0f})"
        )

        self._frame_count = 0
        self._byte_count = 0
        self._last_log_time = now


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="[%(name)s] %(message)s",
    )

    rclpy.init()

    ctx = zmq.Context()
    socket = ctx.socket(zmq.PUB)
    # SNDHWM=20, LINGER=0: gear_sonic native SensorServer와 동일한 설정으로
    # 맞춰서, 워크스테이션 exporter 쪽 기대 동작(느린 subscriber는 버퍼가
    # 차면 드롭됨, 종료 시 밀린 메시지 붙들고 안 기다림)과 일치시킨다.
    socket.setsockopt(zmq.SNDHWM, 20)
    socket.setsockopt(zmq.LINGER, 0)
    socket.bind(f"tcp://*:{args.port}")
    logging.getLogger(args.node_name).info(
        f"ZMQ PUB bound to tcp://*:{args.port}"
    )

    node = CameraForwarder(args, socket)
    logging.getLogger(args.node_name).info("rclpy initialized")

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        socket.close(0)
        ctx.term()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
