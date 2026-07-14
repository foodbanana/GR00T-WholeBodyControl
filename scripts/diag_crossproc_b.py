"""Cross-process DDS test, side B (mimics the role of g1_deploy_onnx_ref / Terminal 2):
 - subscribes to rt/lowstate (LowState_) and logs received ticks
 - publishes rt/lowcmd (LowCmd_) with an incrementing tick

Run alongside diag_crossproc_a.py (side A).
"""
import time

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelPublisher,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_, unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_, LowCmd_

ChannelFactoryInitialize(0, "lo")

received = []
suber = ChannelSubscriber("rt/lowstate", LowState_)
suber.Init(handler=lambda msg: received.append(msg.tick))

puber = ChannelPublisher("rt/lowcmd", LowCmd_)
puber.Init()

time.sleep(0.5)  # let publication_matched fire

for i in range(50):
    msg = unitree_hg_msg_dds__LowCmd_()
    msg.crc = i
    ok = puber.Write(msg)
    time.sleep(0.1)

time.sleep(1.0)
print(f"[B] sent ticks 0..49 on rt/lowcmd")
print(f"[B] received {len(received)} messages on rt/lowstate, ticks={received[:10]}{'...' if len(received) > 10 else ''}")
print("[B] CROSS-PROC RECV " + ("OK" if received else "EMPTY"))
