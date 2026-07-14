"""Unitree G1 on-robot DDS video relay driver.

The D435i is physically wired to PC1 (locked down, no access). PC1 captures
and JPEG-encodes frames and forwards them to PC2 (Orin NX) over Ethernet,
where the ``videohub_pc4`` system service republishes them as a CycloneDDS
topic. This driver only subscribes to that topic — it never opens the
camera device or touches ``/dev/video*``, so it does not conflict with
``videohub_pc4`` holding the device open.

Frames arrive already JPEG-encoded (``video720p``/``video360p``/``video180p``)
so they are passed straight through to :class:`ImageMessageSchema` as raw
bytes — no re-encoding.

Requires ``unitree_sdk2py`` with ``cyclonedds==0.10.2`` pinned exactly to
match the wire protocol of the robot's own DDS stack (see
``docker/Dockerfile.ltw_camera_server_dds``) — a newer cyclonedds changes
the wire protocol and silently breaks discovery with ``videohub_pc4``.
"""

import threading
import time
from typing import Any

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_go.msg.dds_ import Go2FrontVideoData_

from gear_sonic.camera.sensor import Sensor
from gear_sonic.camera.sensor_server import CameraMountPosition, ImageMessageSchema

DDS_TOPIC = "rt/frontvideostream"

# ChannelFactoryInitialize sets up a process-wide DDS domain participant.
# Guard it so multiple UnitreeDDSSensor instances (or re-inits after a
# reconnect) don't call it twice in the same process.
_factory_lock = threading.Lock()
_factory_initialized = False


class UnitreeDDSConfig:
    """Configuration for the Unitree on-robot DDS video relay."""

    dds_interface: str = "eth0"
    dds_domain: int = 0
    mount_position: str = CameraMountPosition.EGO_VIEW.value


class UnitreeDDSSensor(Sensor):
    """Relays JPEG frames already published on the robot's own DDS bus.

    Does not own or configure any camera hardware — PC1 captures, PC2's
    ``videohub_pc4`` republishes over DDS, this class only subscribes.
    """

    def __init__(
        self,
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
        dds_interface: str = "eth0",
        dds_domain: int = 0,
    ):
        self.mount_position = mount_position
        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._latest_time_frame: int | None = None
        self._frame_count = 0

        global _factory_initialized
        with _factory_lock:
            if not _factory_initialized:
                ChannelFactoryInitialize(dds_domain, dds_interface)
                _factory_initialized = True

        self._subscriber = ChannelSubscriber(DDS_TOPIC, Go2FrontVideoData_)
        self._subscriber.Init(self._on_message, queueLen=1)
        print(
            f"[UnitreeDDSSensor] Subscribed to '{DDS_TOPIC}' "
            f"on {dds_interface} (domain {dds_domain})"
        )

    def _on_message(self, msg: Go2FrontVideoData_) -> None:
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
        self._subscriber.Close()
        print(f"[UnitreeDDSSensor] Closed subscriber for '{DDS_TOPIC}'")
