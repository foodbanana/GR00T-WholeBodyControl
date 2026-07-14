"""Replicate run_sim_loop.py's main() flow up to (and including) init_channel(),
using the real wbc_config loaded from YAML, to see whether the failure
reproduces outside of the full run_sim_loop.py script.
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

from gear_sonic.utils.mujoco_sim.simulator_factory import SimulatorFactory, init_channel  # noqa: E402
from gear_sonic.utils.mujoco_sim.configs import SimLoopConfig  # noqa: E402
from gear_sonic.data.robot_model.instantiation.g1 import instantiate_g1_robot_model  # noqa: E402
from gear_sonic.data.robot_model.robot_model import RobotModel  # noqa: E402

report_maps("1. after all top-level imports")

config = SimLoopConfig()
print("config.interface (resolved) =", config.interface)
print("config.env_type =", config.env_type)

wbc_config = config.load_wbc_yaml()
wbc_config["ENV_NAME"] = config.env_name
print("wbc_config DOMAIN_ID =", wbc_config.get("DOMAIN_ID"))
print("wbc_config INTERFACE =", wbc_config.get("INTERFACE"))

report_maps("2. after load_wbc_yaml()")

robot_model = instantiate_g1_robot_model()

report_maps("3. after instantiate_g1_robot_model()")

try:
    init_channel(config=wbc_config)
    print(">>> init_channel SUCCESS")
except Exception as e:
    print(">>> init_channel FAILED:", repr(e))

report_maps("4. after init_channel attempt")
