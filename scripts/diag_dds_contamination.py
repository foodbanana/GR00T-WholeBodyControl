"""Bisect where cyclonedds state gets contaminated during run_sim_loop.py's import flow.

Reports /proc/self/maps entries containing 'ddsc' and ctypes.util.find_library('ddsc')
result after each major import/instantiation step, then attempts the real
init_channel() call that fails in run_sim_loop.py.
"""
import sys
import ctypes.util


def report_maps(label):
    print(f"=== {label} ===")
    with open("/proc/self/maps") as f:
        for line in f:
            if "ddsc" in line.lower():
                print("   ", line.strip())
    print("    find_library('ddsc') ->", ctypes.util.find_library("ddsc"))
    sys.stdout.flush()


report_maps("0. start")

from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # noqa: E402
from unitree_sdk2py.core.channel_config import ChannelConfigHasInterface  # noqa: E402

report_maps("1. after unitree_sdk2py.core.channel import")

from gear_sonic.utils.mujoco_sim.simulator_factory import SimulatorFactory, init_channel  # noqa: E402

report_maps("2. after simulator_factory import (mujoco/scipy/base_sim chain)")

from gear_sonic.data.robot_model.instantiation.g1 import instantiate_g1_robot_model  # noqa: E402

report_maps("3. after g1 instantiation module import (pinocchio etc.)")

robot_model = instantiate_g1_robot_model()

report_maps("4. after instantiate_g1_robot_model() call")

try:
    init_channel({"DOMAIN_ID": 0, "INTERFACE": "lo"})
    print(">>> init_channel SUCCESS")
except Exception as e:
    print(">>> init_channel FAILED:", repr(e))

report_maps("5. after init_channel attempt")
