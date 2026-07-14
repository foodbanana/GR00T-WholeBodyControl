"""Cross-process DDS test, side A (mimics the role of run_sim_loop.py / Terminal 1):
 - publishes rt/lowstate (LowState_) with an incrementing tick
 - subscribes to rt/lowcmd (LowCmd_) and logs received ticks

Run alongside diag_crossproc_b.py (side B) to check whether two separate
processes on domain 0 / "lo" using the patched cyclonedds 0.10.2 can
actually exchange data.
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
suber = ChannelSubscriber("rt/lowcmd", LowCmd_)
suber.Init(handler=lambda msg: received.append(msg.crc))

puber = ChannelPublisher("rt/lowstate", LowState_)
puber.Init()

time.sleep(0.5)  # let publication_matched fire

for i in range(50):
    msg = unitree_hg_msg_dds__LowState_()
    msg.tick = i
    ok = puber.Write(msg)
    time.sleep(0.1)

time.sleep(1.0)
print(f"[A] sent ticks 0..49 on rt/lowstate")
print(f"[A] received {len(received)} messages on rt/lowcmd, crc={received[:10]}{'...' if len(received) > 10 else ''}")
print("[A] CROSS-PROC RECV " + ("OK" if received else "EMPTY"))
