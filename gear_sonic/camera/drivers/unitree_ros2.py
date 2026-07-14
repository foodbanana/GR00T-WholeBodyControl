"""Unitree G1 on-robot ROS 2 video relay driver.

The D435i is physically wired to PC1 (locked down, no access). PC1 captures
and JPEG-encodes frames and forwards them to PC2 (Orin NX) over Ethernet,
where the ``videohub_pc4`` system service republishes them — as a *ROS 2*
topic (rclpy/rclcpp), not raw CycloneDDS. The earlier attempt
(:mod:`gear_sonic.camera.drivers.unitree_dds`, raw ``ChannelSubscriber``)
never received a single callback: multicast packets reached the container
(confirmed via tcpdump) but CycloneDDS silently dropped them, because a raw
DDS reader does not speak the ROS 2 RMW encapsulation/QoS profile the
publisher uses. This driver subscribes the way the publisher actually
expects — via rclpy — instead of reimplementing ROS 2's wire framing.

Frames arrive already JPEG-encoded (``video720p``/``video360p``/``video180p``)
so they are passed straight through to :class:`ImageMessageSchema` as raw
bytes — no re-encoding.

Requires ROS 2 Foxy with ``rmw_cyclonedds_cpp`` (must match the host's RMW,
not the Foxy default ``rmw_fastrtps_cpp``) and the colcon-built
``unitree_go`` message package (see
``docker/Dockerfile.ltw_camera_server_ros2``).
"""

import threading
import time
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from unitree_go.msg import Go2FrontVideoData

from gear_sonic.camera.sensor import Sensor
from gear_sonic.camera.sensor_server import CameraMountPosition, ImageMessageSchema

ROS2_TOPIC = "/frontvideostream"

# rclpy.init() sets up a process-wide ROS 2 context. Guard it the same way
# unitree_dds.py guards ChannelFactoryInitialize, so re-instantiating this
# driver (composed_camera.py's auto-reconnect logic re-runs
# _instantiate_camera() -> __init__ on failure) doesn't call rclpy.init()
# twice in one process, which raises.
_rclpy_lock = threading.Lock()
_rclpy_initialized = False


class UnitreeROS2Sensor(Sensor):
    """Relays JPEG frames published on the robot's own ROS 2 video topic.

    Does not own or configure any camera hardware — PC1 captures, PC2's
    ``videohub_pc4`` republishes as a ROS 2 topic, this class only
    subscribes.
    """

    def __init__(
        self,
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
        dds_interface: str = "eth0",
    ):
        self.mount_position = mount_position
        # NOTE: unlike raw CycloneDDS's ChannelFactoryInitialize(domain, iface),
        # rclpy has no per-call network-interface argument — the RMW layer
        # picks its interface from a CycloneDDS XML config (CYCLONEDDS_URI)
        # or auto-detects at process start. Kept as a constructor arg only
        # so this factory call matches unitree_dds.py's signature; it is
        # not used here to bind eth0. If discovery ever needs to be pinned
        # to a specific interface, that has to be done via a CYCLONEDDS_URI
        # config file, not from here.
        self._dds_interface = dds_interface

        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_time_frame: int | None = None
        self._frame_count = 0

        global _rclpy_initialized
        with _rclpy_lock:
            if not _rclpy_initialized:
                rclpy.init(args=None)
                _rclpy_initialized = True

        self._node = Node("ltw_camera_bridge")

        # Standard "sensor data" QoS: best-effort/volatile so a slow
        # subscriber never makes the publisher block, depth=1 so only the
        # latest frame is ever queued. If frames still never arrive, check
        # the publisher's actual QoS with `ros2 topic info /frontvideostream
        # --verbose` — a reliability/durability mismatch silently drops
        # matching, just like the raw-DDS attempt did.
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._subscription = self._node.create_subscription(
            Go2FrontVideoData, ROS2_TOPIC, self._on_message, qos
        )

        # rclpy.spin() blocks, so it needs its own thread. It exits on its
        # own once the node is destroyed in close().
        self._spin_thread = threading.Thread(
            target=rclpy.spin, args=(self._node,), daemon=True
        )
        self._spin_thread.start()

        print(f"[UnitreeROS2Sensor] Subscribed to '{ROS2_TOPIC}' via rclpy")

    def _on_message(self, msg: Go2FrontVideoData) -> None:
        jpeg_bytes = bytes(msg.video720p)
        if not jpeg_bytes:
            return
        with self._lock:
            self._latest_jpeg = jpeg_bytes
            self._latest_time_frame = msg.time_frame
            self._frame_count += 1

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            jpeg_bytes = self._latest_jpeg
        if jpeg_bytes is None:
            return None

        current_time = time.time()
        return {
            "timestamps": {self.mount_position: current_time},
            "images": {self.mount_position: jpeg_bytes},
        }

    def serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        serialized_msg = ImageMessageSchema(timestamps=data["timestamps"], images=data["images"])
        return serialized_msg.serialize()

    def observation_space(self):
        return None

    def close(self) -> None:
        # Intentionally does NOT call rclpy.shutdown() — that would tear
        # down the process-wide context and break any later reconnect
        # attempt by composed_camera.py's _camera_worker_wrapper, which
        # re-runs _instantiate_camera() (and thus __init__) in the same
        # process rather than starting a new one.
        self._node.destroy_subscription(self._subscription)
        self._node.destroy_node()
        print(f"[UnitreeROS2Sensor] Closed subscription for '{ROS2_TOPIC}'")
