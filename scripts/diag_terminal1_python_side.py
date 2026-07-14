"""Minimal stand-in for Terminal 1 (run_sim_loop.py's DDS role), to test
wire compatibility with the real Terminal 2 C++ binary (g1_deploy_onnx_ref),
which links its own prebuilt cyclonedds 0.10.2 (thirdparty/unitree_sdk2).

 - publishes rt/lowstate (LowState_) at ~50Hz with identity IMU quaternion
 - subscribes to rt/lowcmd (LowCmd_) and logs a sample of received motor_cmd.q
"""
import time

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelPublisher,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_, LowCmd_

DURATION_S = 25
RATE_HZ = 50

ChannelFactoryInitialize(0, "lo")

received = []


def on_lowcmd(msg):
    received.append((msg.crc, [m.q for m in msg.motor_cmd[:3]]))


suber = ChannelSubscriber("rt/lowcmd", LowCmd_)
suber.Init(handler=on_lowcmd)

puber = ChannelPublisher("rt/lowstate", LowState_)
puber.Init()

time.sleep(0.5)

n = int(DURATION_S * RATE_HZ)
for i in range(n):
    msg = unitree_hg_msg_dds__LowState_()
    msg.tick = i
    msg.imu_state.quaternion = [1.0, 0.0, 0.0, 0.0]
    puber.Write(msg)
    time.sleep(1.0 / RATE_HZ)

time.sleep(1.0)
print(f"[PY] sent {n} rt/lowstate messages")
print(f"[PY] received {len(received)} rt/lowcmd messages")
if received:
    print(f"[PY] first sample (crc, motor_cmd[:3].q): {received[0]}")
    print(f"[PY] last sample  (crc, motor_cmd[:3].q): {received[-1]}")
print("[PY] CROSS-PROC RECV " + ("OK" if received else "EMPTY"))
