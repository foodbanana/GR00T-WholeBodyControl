"""Smoke-test a running Isaac-GR00T PolicyServer with a synthetic observation.

Isolates the policy hop. No camera, no C++ deploy, no keyboard, no robot:

    ┌──────────────────┐   REQ  synthetic observation   ┌──────────────────┐
    │  PolicyServer    │ ◄───────────────────────────── │  this script     │
    │  (GPU machine)   │ ─────────────────────────────► │                  │
    └──────────────────┘   REP  motion_token + hands    └──────────────────┘

The observation is built through the same code path run_vla_inference.py uses
(same robot model, same prepare_observation_for_eval), so a pass here means the
wire format, the embodiment config and the checkpoint all agree. What it cannot
tell you is whether the policy does the right thing -- only that it answers.

    python gear_sonic/scripts/smoke_test_policy_server.py --port 5551

Exits non-zero if any check fails, so it can gate a deployment script.
"""

from dataclasses import dataclass
import sys
import time

import numpy as np
import tyro

from gear_sonic.data.robot_model.instantiation.g1 import instantiate_g1_robot_model
from gear_sonic.utils.data_collection.transforms import compute_projected_gravity
from gear_sonic.utils.inference.vla_utils import prepare_observation_for_eval

# The client drops any chunk whose motion token exceeds this, so a checkpoint
# that routinely crosses it leaves the robot standing still while the server
# looks perfectly healthy. See run_vla_inference.py.
ACTION_BOUND = 1.25

# Shapes the C++ deploy expects, per the SONIC latent protocol: a 64-dim motion
# token plus 7 joints per hand, over a 40-step horizon.
EXPECTED_HORIZON = 40
EXPECTED_DIMS = {"motion_token": 64, "left_hand_joints": 7, "right_hand_joints": 7}


@dataclass
class SmokeConfig:
    """CLI config for the PolicyServer smoke test."""

    host: str = "localhost"
    """PolicyServer host (use the local end of the SSH tunnel for a remote server)."""

    port: int = 5551
    """PolicyServer port."""

    prompt: str = "raise your right arm if you see a banana"
    """Language prompt. Use the one training used -- a different phrasing is a
    different input, and this is the cheapest place to notice that."""

    image_size: str = "640x480"
    """Synthetic ego_view resolution WxH. Must match what run_vla_inference.py
    sends, since payload size drives the round trip on a remote server."""

    rate: float = 2.5
    """Inference rate (Hz) the deployment will run at; sets the latency budget."""

    n: int = 5
    """Number of requests. The first is a warmup and is excluded from the stats."""


GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"


class Checks:
    """Collects pass/fail results so every check runs before the script exits."""

    def __init__(self):
        self.failures: list[str] = []

    def ok(self, label: str, detail: str = "") -> None:
        print(f"  {GREEN}PASS{RESET}  {label}" + (f"  — {detail}" if detail else ""))

    def fail(self, label: str, detail: str) -> None:
        print(f"  {RED}FAIL{RESET}  {label}  — {detail}")
        self.failures.append(f"{label}: {detail}")

    def warn(self, label: str, detail: str) -> None:
        print(f"  {YELLOW}WARN{RESET}  {label}  — {detail}")

    def expect(self, condition: bool, label: str, detail: str) -> bool:
        (self.ok if condition else self.fail)(label, detail)
        return condition


def build_observation(robot_model, width: int, height: int, prompt: str) -> dict:
    """Build the observation dict run_vla_inference.py sends, with fake sensors.

    The image is noise and the pose is the robot model's zero configuration --
    neither is meant to be realistic. The point is that every key, dtype and
    shape matches, so a rejection here is a real format problem.
    """
    rng = np.random.default_rng(0)
    image = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    qpos = np.zeros(robot_model.num_joints, dtype=np.float32)

    observation = {
        "video": {"ego_view": image[np.newaxis, np.newaxis]},
        "state": {},
        "language": {"annotation.human.task_description": [[prompt]]},
        "q": qpos[np.newaxis, np.newaxis],
        "timestamps": time.time(),
    }
    observation = prepare_observation_for_eval(robot_model, observation)

    # Upright base: identity quaternion -> gravity straight down.
    gravity = compute_projected_gravity(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64))
    observation["state"]["projected_gravity"] = np.asarray(gravity, dtype=np.float32)[
        np.newaxis, np.newaxis
    ]
    return observation


def get_field(action: dict, key: str) -> np.ndarray | None:
    """Read an action field, tolerating the optional ``action.`` prefix."""
    for candidate in (key, f"action.{key}"):
        if candidate in action:
            return np.asarray(action[candidate])
    return None


def main(config: SmokeConfig):
    from gr00t.policy.server_client import MsgSerializer, PolicyClient

    width, height = (int(v) for v in config.image_size.lower().split("x"))
    budget_s = 1.0 / config.rate
    checks = Checks()

    print("=" * 68)
    print("  PolicyServer smoke test")
    print("=" * 68)
    print(f"  target        : {config.host}:{config.port}")
    print(f"  ego_view      : {width}x{height}")
    print(f"  prompt        : {config.prompt!r}")
    print(f"  latency budget: {budget_s * 1000:.0f} ms  ({config.rate} Hz)")
    print()

    robot_model = instantiate_g1_robot_model(waist_location="lower_and_upper_body")
    observation = build_observation(robot_model, width, height, config.prompt)

    payload = MsgSerializer.to_bytes(
        {"endpoint": "get_action", "data": {"observation": observation, "options": None}}
    )
    payload_mb = len(payload) / 1e6
    print(f"  wire payload  : {payload_mb:.2f} MB  "
          f"({payload_mb * config.rate:.2f} MB/s at {config.rate} Hz)")
    print()

    # --- 1. Reachability -----------------------------------------------------
    print("[1] Reachability")
    client = PolicyClient(host=config.host, port=config.port)
    t0 = time.monotonic()
    reachable = client.ping()
    if not checks.expect(reachable, "ping", f"{(time.monotonic() - t0) * 1000:.0f} ms"):
        print(f"\n{RED}Server unreachable — nothing else can be checked.{RESET}")
        print("  Is the PolicyServer up, and does its --port match this one?")
        print("  For a remote server, is the SSH tunnel still open?")
        return 1

    # --- 2. Inference --------------------------------------------------------
    print("\n[2] Inference")
    latencies: list[float] = []
    action: dict | None = None
    for i in range(config.n):
        t0 = time.monotonic()
        try:
            action, _info = client.get_action(observation)
        except Exception as exc:
            checks.fail(f"request {i}", f"{type(exc).__name__}: {exc}")
            print(f"\n{RED}Inference failed — see the PolicyServer log.{RESET}")
            return 1
        dt = time.monotonic() - t0
        latencies.append(dt)
        print(f"        call {i}: {dt * 1000:7.1f} ms" + ("  (warmup, excluded)" if not i else ""))

    # The first call pays for CUDA graph capture and allocator warmup; it is not
    # representative of the steady state the control loop will see.
    steady = latencies[1:] or latencies
    mean_ms, max_ms = np.mean(steady) * 1000, np.max(steady) * 1000
    checks.expect(
        max_ms < budget_s * 1000,
        "latency within budget",
        f"mean {mean_ms:.0f} ms, max {max_ms:.0f} ms vs {budget_s * 1000:.0f} ms budget",
    )

    # --- 3. Action format ----------------------------------------------------
    print("\n[3] Action format")
    assert action is not None
    for key, dim in EXPECTED_DIMS.items():
        value = get_field(action, key)
        if value is None:
            checks.fail(key, f"missing (got keys: {sorted(action.keys())})")
            continue
        # (B, T, D) from the model; the client squeezes the batch dim itself.
        squeezed = value[0] if value.ndim == 3 else value
        checks.expect(
            squeezed.shape == (EXPECTED_HORIZON, dim),
            f"{key} shape",
            f"{value.shape} -> {squeezed.shape}, expected ({EXPECTED_HORIZON}, {dim})",
        )

    # --- 4. Action bound -----------------------------------------------------
    print("\n[4] Action bound")
    token = get_field(action, "motion_token")
    if token is None:
        checks.fail("motion_token bound", "no motion_token to check")
    else:
        peak = float(np.abs(token).max())
        checks.expect(
            peak <= ACTION_BOUND,
            "motion_token within bound",
            f"|max| {peak:.3f} vs bound {ACTION_BOUND}",
        )
        if peak > ACTION_BOUND:
            print("        run_vla_inference.py drops whole chunks past this bound,")
            print("        so the robot would stand still while the server looks fine.")
        elif peak > ACTION_BOUND * 0.8:
            checks.warn(
                "motion_token headroom",
                f"|max| {peak:.3f} is within 20% of the {ACTION_BOUND} bound",
            )
        if not np.all(np.isfinite(np.asarray(token, dtype=np.float64))):
            checks.fail("motion_token finite", "contains NaN or Inf")
        else:
            checks.ok("motion_token finite", "no NaN/Inf")

    # --- Verdict -------------------------------------------------------------
    print("\n" + "=" * 68)
    if checks.failures:
        print(f"{RED}FAILED{RESET} — {len(checks.failures)} check(s):")
        for failure in checks.failures:
            print(f"  - {failure}")
        print("=" * 68)
        return 1

    print(f"{GREEN}PASSED{RESET} — the policy hop is healthy.")
    print(f"  round trip {mean_ms:.0f} ms, motion_token |max| {float(np.abs(token).max()):.3f}")
    print()
    print("  This says the server answers in the right format, not that the policy")
    print("  behaves. Camera, C++ deploy and the robot are still untested.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main(tyro.cli(SmokeConfig)))
