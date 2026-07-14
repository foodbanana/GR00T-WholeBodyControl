"""Test whether a second (redundant) ChannelFactoryInitialize() call - as happens
in BaseSimulator.__init__ (base_sim.py:564) right after SimWrapper already called
init_channel() - breaks pub/sub functionality on the ChannelFactory singleton,
even though the exception it raises is caught and only printed.
"""
import sys
import time

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelPublisher,
    ChannelSubscriber,
    ChannelFactory,
)
from unitree_sdk2py.idl.unitree_go.msg.dds_ import WirelessController_

DOUBLE_INIT = "--double" in sys.argv

print(f">>> Mode: {'DOUBLE init (reproduces real flow)' if DOUBLE_INIT else 'SINGLE init (baseline)'}")

# Call 1 (== init_channel() in SimWrapper.__init__)
ChannelFactoryInitialize(0, "lo")
print(">>> call 1 done")

if DOUBLE_INIT:
    # Call 2 (== ChannelFactoryInitialize inside BaseSimulator.__init__, base_sim.py:564)
    try:
        ChannelFactoryInitialize(0, "lo")
        print(">>> call 2 SUCCESS (unexpected)")
    except Exception as e:
        print(f">>> call 2 FAILED (caught, as in base_sim.py): {e!r}")

factory = ChannelFactory()
print(">>> factory participant:", factory._ChannelFactory__participant)
print(">>> factory domain:", factory._ChannelFactory__domain)

# Now try real pub/sub through the (possibly damaged) singleton
received = []

suber = ChannelSubscriber("rt/wirelesscontroller", WirelessController_)
suber.Init(handler=lambda msg: received.append(msg))

puber = ChannelPublisher("rt/wirelesscontroller", WirelessController_)
puber.Init()

time.sleep(0.5)  # let publication_matched fire

msg = WirelessController_(lx=1.0, ly=2.0, rx=3.0, ry=4.0, keys=0)
ok = puber.Write(msg, timeout=1.0)
print(">>> publish Write() returned:", ok)

time.sleep(0.5)
print(">>> received messages:", received)
print(">>> PUB/SUB WORKS" if received else ">>> PUB/SUB BROKEN (no message received)")
