#!/usr/bin/env python3
"""WBC decoder(`model_decoder.onnx`)를 오프라인에서 재현하기 위한 공용 모듈.

데이터셋(LeRobot v2.1, 25fps)에 기록된 상태로 994-dim 관측을 만들어 decoder를
돌린다. open-loop eval에서 motion_token을 관절공간으로 디코드하는 데 쓴다.

## 좌표/순서 규약 (코드에서 확인한 사실)

`gear_sonic_deploy/src/g1/g1_deploy_onnx_ref` 기준.

- **hardware(=mujoco) 순서**: 사지별로 묶임.
  `left_leg 6 | right_leg 6 | waist 3 | left_arm 7 | right_arm 7`
- **isaaclab 순서**: 좌우가 교차됨. 정책 네트워크 내부가 이 순서.
- 두 순서는 `ISAACLAB_TO_MUJOCO` / `MUJOCO_TO_ISAACLAB` 로 상호 변환(서로 역치환).

C++ 내부 상태(`state.body_q` 등)는 **isaaclab 순서 + default_angles 차감** 이지만,
ZMQ로 내보낼 때 mujoco 순서 + default 복원으로 되돌린다
(`zmq_output_handler.hpp`). 데이터셋은 그 ZMQ 값을 그대로 기록하므로:

- `observation.state`[body29] = **절대 관절각(rad), mujoco 순서**
- `action.wbc`[body29] = **관절목표(rad), mujoco 순서**
  = `last_action_il[ISAACLAB_TO_MUJOCO[i]] * action_scale[i] + default_angles[i]`
  → decoder 출력에 적용하는 식(`g1_deploy_onnx_ref.cpp:3135`)과 동일하므로,
    decoder 출력을 같은 식으로 변환하면 `action.wbc` 와 직접 1:1 비교된다.

## 994-dim 관측 레이아웃

`policy/release/observation_config.yaml` 의 `observations:` 순서대로 이어붙인다.
각 블록은 frame-major(`[frame][joint]`)이고 history는 **oldest-first**.

| offset | 블록 | dim |
|---|---|---|
| 0   | token_state                | 64  |
| 64  | his_base_angular_velocity  | 30  = 3×10 |
| 94  | his_body_joint_positions   | 290 = 29×10 |
| 384 | his_body_joint_velocities  | 290 |
| 674 | his_last_actions           | 290 |
| 964 | his_gravity_dir            | 30  |
"""

from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------
# 상수 — policy_parameters.hpp 에서 그대로 옮김
# --------------------------------------------------------------------------

N_JOINT = 29
N_HIST = 10
OBS_DIM = 994
CONTROL_DT = 0.02  # 50 Hz

ISAACLAB_TO_MUJOCO = np.array(
    [0, 3, 6, 9, 13, 17, 1, 4, 7, 10, 14, 18, 2, 5, 8,
     11, 15, 19, 21, 23, 25, 27, 12, 16, 20, 22, 24, 26, 28]
)
MUJOCO_TO_ISAACLAB = np.array(
    [0, 6, 12, 1, 7, 13, 2, 8, 14, 3, 9, 15, 22, 4, 10,
     16, 23, 5, 11, 17, 24, 18, 25, 19, 26, 20, 27, 21, 28]
)

_NATURAL_FREQ = 10 * 2.0 * 3.1415926535
_ARMATURE = {"5020": 0.003609725, "7520_14": 0.010177520, "7520_22": 0.025101925, "4010": 0.00425}
_EFFORT = {"5020": 25.0, "7520_14": 88.0, "7520_22": 139.0, "4010": 5.0}
_MOTOR_TYPE_MUJOCO = (
    ["7520_22", "7520_22", "7520_14", "7520_22", "5020", "5020"]      # left leg
    + ["7520_22", "7520_22", "7520_14", "7520_22", "5020", "5020"]    # right leg
    + ["7520_14", "5020", "5020"]                                     # waist
    + ["5020"] * 5 + ["4010", "4010"]                                 # left arm
    + ["5020"] * 5 + ["4010", "4010"]                                 # right arm
)
ACTION_SCALE = np.array(
    [0.25 * _EFFORT[m] / (_ARMATURE[m] * _NATURAL_FREQ**2) for m in _MOTOR_TYPE_MUJOCO]
)

# PD 게인 (mujoco 순서). policy_parameters.hpp 의 kps/kds 배열을 그대로 옮긴 것이며,
# 발목·허리roll/pitch 만 2배가 붙는다.
_STIFF = {k: v * _NATURAL_FREQ**2 for k, v in _ARMATURE.items()}
_DAMP = {k: 2.0 * 2.0 * v * _NATURAL_FREQ for k, v in _ARMATURE.items()}  # damping_ratio = 2
_GAIN_X2 = np.array([False] * 4 + [True] * 2 + [False] * 4 + [True] * 2
                    + [False] + [True] * 2 + [False] * 14)
KP = np.array([_STIFF[m] for m in _MOTOR_TYPE_MUJOCO]) * np.where(_GAIN_X2, 2.0, 1.0)
KD = np.array([_DAMP[m] for m in _MOTOR_TYPE_MUJOCO]) * np.where(_GAIN_X2, 2.0, 1.0)

DEFAULT_ANGLES = np.array(
    [-0.312, 0.0, 0.0, 0.669, -0.363, 0.0,
     -0.312, 0.0, 0.0, 0.669, -0.363, 0.0,
     0.0, 0.0, 0.0,
     0.2, 0.2, 0.0, 0.6, 0.0, 0.0, 0.0,
     0.2, -0.2, 0.0, 0.6, 0.0, 0.0, 0.0]
)

# 43-dim(손 포함) → 29-dim(body) 추출 인덱스
BODY29_IN_43 = np.array(list(range(0, 22)) + list(range(29, 36)))

JOINT_NAMES = [
    "L_hip_pitch", "L_hip_roll", "L_hip_yaw", "L_knee", "L_ankle_pitch", "L_ankle_roll",
    "R_hip_pitch", "R_hip_roll", "R_hip_yaw", "R_knee", "R_ankle_pitch", "R_ankle_roll",
    "waist_yaw", "waist_roll", "waist_pitch",
    "L_shoulder_pitch", "L_shoulder_roll", "L_shoulder_yaw", "L_elbow",
    "L_wrist_roll", "L_wrist_pitch", "L_wrist_yaw",
    "R_shoulder_pitch", "R_shoulder_roll", "R_shoulder_yaw", "R_elbow",
    "R_wrist_roll", "R_wrist_pitch", "R_wrist_yaw",
]
RIGHT_ARM = np.arange(22, 29)  # mujoco 29-vector 안에서의 오른팔

# 관절 물리 가동한계 (도). `gear_sonic_deploy/g1/g1_29dof.xml` 의 <joint range> 를
# 옮긴 것이며 XML 의 관절 순서가 위 mujoco 순서와 그대로 일치한다.
JOINT_LIMITS_DEG = np.array([
    [-145.0, 165.0], [-30.0, 170.0], [-158.0, 158.0],      # left hip pitch/roll/yaw
    [-5.0, 165.0], [-50.0, 30.0], [-15.0, 15.0],           # left knee, ankle pitch/roll
    [-145.0, 165.0], [-170.0, 30.0], [-158.0, 158.0],      # right hip pitch/roll/yaw
    [-5.0, 165.0], [-50.0, 30.0], [-15.0, 15.0],           # right knee, ankle pitch/roll
    [-150.0, 150.0], [-29.8, 29.8], [-29.8, 29.8],         # waist yaw/roll/pitch
    [-177.0, 153.0], [-91.0, 129.0], [-150.0, 150.0],      # left shoulder pitch/roll/yaw
    [-60.0, 120.0], [-113.0, 113.0], [-92.5, 92.5], [-92.5, 92.5],   # left elbow, wrist
    [-177.0, 153.0], [-129.0, 91.0], [-150.0, 150.0],      # right shoulder pitch/roll/yaw
    [-60.0, 120.0], [-113.0, 113.0], [-92.5, 92.5], [-92.5, 92.5],   # right elbow, wrist
])

# --------------------------------------------------------------------------
# 데이터셋 — 음성(negative) 에피소드 목록
# --------------------------------------------------------------------------
# task "바나나를 보면 오른팔을 올린다" 는 조건부라, 바나나가 없어서 **팔을 올리지
# 않는 것이 정답**인 에피소드가 있다. 음성은 아무것도 안 움직이므로 RMSE가 낮게
# 나오는 게 당연하다 — 성능 판단에서 반드시 분리해야 한다.
#
# 자동 판정(가동범위 임계값)은 임계값 근처 에피소드에서 흔들릴 수 있으므로 쓰지
# 않는다. 아래는 `raise_arm_banana_merged` 기준 **원본 episode_index** 목록이다.
#
# 인덱스는 `raise_arm_banana_merged_v2` 기준이며, 0~54 는 v1(`raise_arm_banana_merged`)
# 과 동일하므로 두 버전 모두에 그대로 쓸 수 있다.
#
#   4, 9, 49   v1 수집분. 2026-08-06 사용자 확인 완료.
#   55~67      2026-08-06 20:55:58 세션 (13개) — 빈 공간을 보면 정지
#   68~78      2026-08-06 21:05:31 세션 (11개) — 빈 공간을 보면 정지
#
# 신규 24개는 오른팔 가동범위 0.4~5.1° 로 전부 정지 확인됨(양성은 110~167°).
NEGATIVE_EPISODES = frozenset({4, 9, 49} | set(range(55, 79)))


def episode_kind(episode_index: int) -> str:
    """원본 episode_index 로 양성/음성을 판정한다 (목록 조회, 계산 없음)."""
    return "NEG" if episode_index in NEGATIVE_EPISODES else "POS"


# --------------------------------------------------------------------------
# 쿼터니언 유틸 (모두 qw, qx, qy, qz 순서)
# --------------------------------------------------------------------------


def quat_conjugate(q: np.ndarray) -> np.ndarray:
    out = q.copy()
    out[..., 1:] *= -1.0
    return out


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    bw, bx, by, bz = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ],
        axis=-1,
    )


def quat_rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """q 로 v 를 회전. `quat_rotate_d` (math_utils.hpp) 와 같은 규약."""
    qw = q[..., :1]
    qv = q[..., 1:]
    t = 2.0 * np.cross(qv, np.broadcast_to(v, qv.shape))
    return v + qw * t + np.cross(qv, t)


def gravity_dir_from_quat(base_quat: np.ndarray) -> np.ndarray:
    """`GatherHisGravityDir`: quat_rotate(conj(base_quat), [0,0,-1])."""
    return quat_rotate(quat_conjugate(base_quat), np.array([0.0, 0.0, -1.0]))


def base_ang_vel_from_quat(base_quat: np.ndarray, dt: float) -> np.ndarray:
    """쿼터니언 시계열에서 body-frame 각속도를 유도한다.

    실기에서는 IMU 자이로 실측값이지만 데이터셋에 기록되지 않았다.
    ω_body = 2 * vec(conj(q_t) ⊗ (q_{t+1} - q_t)/dt) 로 근사한다.
    """
    n = len(base_quat)
    w = np.zeros((n, 3))
    if n < 2:
        return w
    dq = np.diff(base_quat, axis=0) / dt
    w[:-1] = 2.0 * quat_mul(quat_conjugate(base_quat[:-1]), dq)[:, 1:]
    w[-1] = w[-2]
    return w


def slerp_series(q: np.ndarray, t_src: np.ndarray, t_dst: np.ndarray) -> np.ndarray:
    """쿼터니언 시계열을 t_dst 시각으로 slerp 보간."""
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    idx = np.clip(np.searchsorted(t_src, t_dst, side="right") - 1, 0, len(t_src) - 2)
    t0, t1 = t_src[idx], t_src[idx + 1]
    u = np.clip((t_dst - t0) / np.maximum(t1 - t0, 1e-12), 0.0, 1.0)[:, None]
    q0, q1 = q[idx], q[idx + 1]
    dot = np.sum(q0 * q1, axis=1, keepdims=True)
    q1 = np.where(dot < 0, -q1, q1)
    dot = np.abs(dot).clip(-1.0, 1.0)
    theta = np.arccos(dot)
    small = theta[:, 0] < 1e-6
    out = np.empty_like(q0)
    st = np.sin(theta)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (np.sin((1 - u) * theta) * q0 + np.sin(u * theta) * q1) / np.where(st == 0, 1.0, st)
    out[small] = ((1 - u) * q0 + u * q1)[small]
    return out / np.linalg.norm(out, axis=1, keepdims=True)


# --------------------------------------------------------------------------
# 데이터셋 ↔ decoder 표현 변환
# --------------------------------------------------------------------------


def mujoco_q_to_isaaclab_body_q(q_mujoco: np.ndarray) -> np.ndarray:
    """절대 관절각(mujoco) → C++ `state.body_q` (isaaclab 순서, default 차감)."""
    rel = q_mujoco - DEFAULT_ANGLES
    out = np.empty_like(rel)
    out[..., ISAACLAB_TO_MUJOCO] = rel
    return out


def mujoco_dq_to_isaaclab(dq_mujoco: np.ndarray) -> np.ndarray:
    out = np.empty_like(dq_mujoco)
    out[..., ISAACLAB_TO_MUJOCO] = dq_mujoco
    return out


def wbc_target_to_isaaclab_action(q_target_mujoco: np.ndarray) -> np.ndarray:
    """`action.wbc`(관절목표, mujoco) → C++ `state.last_action` (isaaclab, raw)."""
    raw = (q_target_mujoco - DEFAULT_ANGLES) / ACTION_SCALE
    out = np.empty_like(raw)
    out[..., ISAACLAB_TO_MUJOCO] = raw
    return out


def isaaclab_action_to_mujoco_target(action_il: np.ndarray) -> np.ndarray:
    """decoder 출력(isaaclab, raw) → 관절목표(rad, mujoco).

    `g1_deploy_onnx_ref.cpp:3135` 와 동일:
        target[i] = default[i] + action[ISAACLAB_TO_MUJOCO[i]] * scale[i]
    """
    return DEFAULT_ANGLES + action_il[..., ISAACLAB_TO_MUJOCO] * ACTION_SCALE


# --------------------------------------------------------------------------
# 관측 조립
# --------------------------------------------------------------------------


def _history_index(t: int, n_hist: int, stride: int) -> np.ndarray:
    """oldest-first history 인덱스. 시작부는 가장 오래된 프레임으로 clamp."""
    return np.clip(t - np.arange(n_hist - 1, -1, -1) * stride, 0, None)


def build_obs(
    token: np.ndarray,
    body_q_il: np.ndarray,
    body_dq_il: np.ndarray,
    last_action_il: np.ndarray,
    base_ang_vel: np.ndarray,
    grav_dir: np.ndarray,
    t: int,
    stride: int = 1,
) -> np.ndarray:
    """시각 t 의 994-dim 관측을 만든다. 입력은 모두 시계열 배열."""
    h = _history_index(t, N_HIST, stride)
    obs = np.empty(OBS_DIM, dtype=np.float64)
    obs[0:64] = token
    obs[64:94] = base_ang_vel[h].reshape(-1)
    obs[94:384] = body_q_il[h].reshape(-1)
    obs[384:674] = body_dq_il[h].reshape(-1)
    obs[674:964] = last_action_il[h].reshape(-1)
    obs[964:994] = grav_dir[h].reshape(-1)
    return obs


class EpisodeStates:
    """한 에피소드의 decoder 입력 시계열 묶음."""

    def __init__(self, q_mujoco, wbc_mujoco, base_quat, token, dt):
        self.dt = dt
        self.n = len(q_mujoco)
        self.q_mujoco = q_mujoco
        self.wbc_mujoco = wbc_mujoco
        self.token = token

        dq_mujoco = np.gradient(q_mujoco, dt, axis=0)
        self.body_q_il = mujoco_q_to_isaaclab_body_q(q_mujoco)
        self.body_dq_il = mujoco_dq_to_isaaclab(dq_mujoco)
        self.last_action_il = wbc_target_to_isaaclab_action(wbc_mujoco)
        self.base_ang_vel = base_ang_vel_from_quat(base_quat, dt)
        self.grav_dir = gravity_dir_from_quat(base_quat)

    def obs_at(self, t: int, token: np.ndarray | None = None, stride: int = 1) -> np.ndarray:
        return build_obs(
            self.token[t] if token is None else token,
            self.body_q_il, self.body_dq_il, self.last_action_il,
            self.base_ang_vel, self.grav_dir, t, stride,
        )


def upsample_states(
    q_mujoco: np.ndarray, wbc_mujoco: np.ndarray, base_quat: np.ndarray,
    token: np.ndarray, dt_src: float, factor: int,
) -> tuple[EpisodeStates, np.ndarray]:
    """25Hz 시계열을 factor배로 보간해 50Hz decoder 분포에 맞춘다.

    반환: (보간된 EpisodeStates, 원본 프레임에 대응하는 보간 인덱스)
    """
    n = len(q_mujoco)
    t_src = np.arange(n) * dt_src
    t_dst = np.arange((n - 1) * factor + 1) * (dt_src / factor)

    def lerp(a):
        return np.stack([np.interp(t_dst, t_src, a[:, j]) for j in range(a.shape[1])], axis=1)

    # token 은 VLA가 저속으로 갱신하고 50Hz decoder가 반복 소비하므로 zero-order hold.
    tok_idx = np.clip(np.searchsorted(t_src, t_dst, side="right") - 1, 0, n - 1)
    st = EpisodeStates(
        lerp(q_mujoco), lerp(wbc_mujoco), slerp_series(base_quat, t_src, t_dst),
        token[tok_idx], dt_src / factor,
    )
    return st, np.arange(n) * factor
