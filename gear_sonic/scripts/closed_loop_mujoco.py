#!/usr/bin/env python3
"""VLA 예측 토큰을 MuJoCo에서 **폐루프**로 실행해 안전성을 확인한다.

open-loop eval은 매 프레임 이력을 데이터셋 GT로 재고정하므로 "모델의 토큰을 실제로
실행하면 로봇이 넘어지는가"를 볼 수 없다. 이 스크립트는 그 질문만 본다.

C++ deploy 바이너리(`gear_sonic_deploy`)가 빌드되어 있지 않아도 되도록, C++ 제어
루프가 하는 일을 Python에서 그대로 재현한다:

    50Hz 마다  상태 → 994-dim 관측 → decoder ONNX → raw action(29)
               → q_target = default + action[il2mj] * scale
               → tau = kp*(q_target - q) - kd*dq   (500Hz 물리 10 substep)

이미지도 정책 서버도 필요 없다 — **이미 덤프해둔 예측 토큰을 그대로 흘려보낸다.**
따라서 MuJoCo 렌더링과 실사의 시각 격차 문제를 우회한다. 대신 "바나나 판별"은
이 방법으로 검증할 수 없다(그건 실기에서만 가능).

## 사용법

`$SC` 는 onnxruntime + mujoco 가 설치된 venv 가 있는 scratchpad 경로.

    SC=/tmp/claude-1000/-home-edgexpert00-GR00T-WholeBodyControl/<세션>/scratchpad

### 1) 창을 띄워 눈으로 확인 — 음성·양성 둘 다

    $SC/onnxenv/bin/python gear_sonic/scripts/closed_loop_mujoco.py \\
        --preds-dir  $SC/preds_h10 \\          # 덤프된 예측 토큰이 있는 폴더
        --dataset    outputs/raise_arm_banana_val5 \\   # 라벨(ep번호·POS/NEG) 출처
        --checkpoints 4000 \\                  # 볼 체크포인트 (덤프돼 있어야 함)
        --trajs 0 1 \\                         # traj0=ep49 음성, traj1=ep50 양성
        --source pred \\                       # 모델 예측 토큰 (gt=정답, 대조군)
        --viewer                              # MuJoCo 창 띄우고 실시간 재생

`--trajs` 는 **데이터셋 안에서의 순번**이다(에피소드 번호가 아니다).
`--dataset` 을 주면 터미널에 `ep49 NEG` 처럼 실제 번호가 찍히므로 확인하고 고르면 된다.

### 2) 정답과 비교 — 같은 명령에서 `--source` 만 바꾼다

    ... --source gt --viewer      # 시연 그대로. 팔을 얼마나 드는지의 기준선

모델이 팔을 덜 드는 정도가 이 둘의 차이다(v1 기준 GT 163° vs 예측 103~133°).

### 3) 영상으로 남기기 (창 없이, 헤드리스 가능)

    ... --video --video-size 960 720
    → eval_results/closed_loop_mujoco/video/{source}_ck{N}_traj{K}.mp4

`--viewer` 와 동시에 써도 된다.

### 4) 실기 블렌딩을 반영해서 보기

    ... --chunk-blend-frames 3

`run_vla_inference.py --chunk-blend-frames` 와 같은 값을 주면 실기와 같은 조건이
된다. chunk 경계 간격은 덤프의 `meta.json`(execution_horizon)에서 자동으로 읽는다.

### 5) 수치만 (창·영상 없이, 여러 체크포인트 일괄)

    ... --checkpoints 2000 4000 20000 --trajs 0 1 3

    → 넘어짐 여부 / 최저높이 / 최대기울기 / 관절한계위반 / 토크포화 / 오른팔가동범위
    → eval_results/closed_loop_mujoco/summary_{source}.json

## 주의

- `--viewer` 는 DISPLAY 가 필요하다. 이 장비는 EGL/OSMesa 가 실패하고 glfw 만
  동작해서 `MUJOCO_GL=glfw` 를 자동 설정한다.
- 뷰어에서 마우스로 시점 회전·확대, 스페이스바로 일시정지가 된다.
- `--preds-dir` 에 없는 체크포인트는 쓸 수 없다. 먼저
  `scripts/eval/dump_open_loop_predictions.py` 로 덤프해야 한다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 헤드리스 환경에서 오프스크린 렌더링용. 이 장비에서는 egl/osmesa 가 실패하고
# glfw 만 동작했다. 이미 지정돼 있으면 존중한다.
os.environ.setdefault("MUJOCO_GL", "glfw")

import mujoco  # noqa: E402
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wbc_decoder import (  # noqa: E402
    ACTION_SCALE, DEFAULT_ANGLES, ISAACLAB_TO_MUJOCO, JOINT_LIMITS_DEG,
    KD, KP, N_HIST, OBS_DIM, RIGHT_ARM, episode_kind, gravity_dir_from_quat,
)

DEFAULT_SCENE = "gear_sonic_deploy/g1/scene_29dof.xml"
DEFAULT_DECODER = "gear_sonic_deploy/policy/release/model_decoder.onnx"
CONTROL_HZ = 50.0
TOKEN_HZ = 25.0          # VLA 발행률 (--action-publish-rate 25)
FALL_HEIGHT = 0.5        # 골반 높이가 이보다 낮으면 넘어진 것으로 본다
TILT_LIMIT_DEG = 50.0    # 몸통 기울기 한계

def episode_labels(dataset: Path | None) -> dict[int, str]:
    """traj_id → "ep49 NEG" 형태의 표시용 라벨.

    데이터셋 경로를 주면 `episodes.jsonl` 에서 읽는다. 하드코딩하면 val5(5개)와
    v2_val(14개)처럼 구성이 바뀔 때 엉뚱한 라벨이 붙는다.
    """
    if dataset is None:
        return {}
    meta = dataset / "meta" / "episodes.jsonl"
    if not meta.exists():
        return {}
    eps = [json.loads(x)["episode_index"] for x in meta.read_text().splitlines() if x.strip()]
    return {k: f"ep{e} {episode_kind(e)}" for k, e in enumerate(eps)}


def apply_chunk_blend(tok: np.ndarray, horizon: int, blend_frames: int) -> np.ndarray:
    """새 chunk 로 넘어가는 지점에서 토큰을 교차 페이드한다.

    `run_vla_inference.py` 의 `--chunk-blend-frames` 와 같은 동작을 덤프된 토큰
    시퀀스에 재현한다. 덤프는 `--execution-horizon H` 로 만들어졌으므로 H 배수
    인덱스가 chunk 경계다.
    """
    if blend_frames <= 0 or horizon <= 0:
        return tok
    out = tok.copy()
    for b in range(horizon, len(tok), horizon):
        prev = out[b - 1]                      # 직전에 발행된(=블렌딩 반영된) 토큰
        for j in range(min(blend_frames, len(tok) - b)):
            a = (j + 1) / blend_frames
            out[b + j] = (1.0 - a) * prev + a * tok[b + j]
    return out


def mj_to_il(v: np.ndarray) -> np.ndarray:
    """mujoco 순서 → isaaclab 순서."""
    out = np.empty_like(v)
    out[ISAACLAB_TO_MUJOCO] = v
    return out


class History:
    """decoder가 요구하는 10프레임 이력 버퍼 (oldest-first)."""

    def __init__(self):
        self.q, self.dq, self.act, self.w, self.g = ([] for _ in range(5))

    def push(self, body_q_il, body_dq_il, last_action_il, base_ang_vel, grav_dir):
        for buf, v in ((self.q, body_q_il), (self.dq, body_dq_il), (self.act, last_action_il),
                       (self.w, base_ang_vel), (self.g, grav_dir)):
            buf.append(np.asarray(v, dtype=np.float64))
            if len(buf) > N_HIST:
                buf.pop(0)

    def _stack(self, buf):
        """부족하면 가장 오래된 값으로 앞을 채운다(C++ 의 clamp 와 동일)."""
        pad = [buf[0]] * (N_HIST - len(buf))
        return np.concatenate(pad + buf)

    def obs(self, token: np.ndarray) -> np.ndarray:
        o = np.empty(OBS_DIM)
        o[0:64] = token
        o[64:94] = self._stack(self.w)
        o[94:384] = self._stack(self.q)
        o[384:674] = self._stack(self.dq)
        o[674:964] = self._stack(self.act)
        o[964:994] = self._stack(self.g)
        return o


class VideoWriter:
    """MuJoCo 오프스크린 렌더 → ffmpeg 파이프. 추가 파이썬 의존성 없음."""

    def __init__(self, model, path: Path, width=640, height=480, fps=25):
        self.renderer = mujoco.Renderer(model, height, width)
        self.cam = mujoco.MjvCamera()
        self.cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
        self.cam.trackbodyid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "pelvis")
        self.cam.distance, self.cam.azimuth, self.cam.elevation = 3.0, 135.0, -15.0
        self.proc = subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            stdin=subprocess.PIPE,
        )

    def grab(self, data):
        self.renderer.update_scene(data, camera=self.cam)
        self.proc.stdin.write(self.renderer.render().tobytes())

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()
        self.renderer.close()


def rollout(model, data, sess, tokens_50hz, settle_s=1.0, video: VideoWriter | None = None,
            video_every: int = 2, viewer=None, realtime: bool = False):
    """토큰 시퀀스를 폐루프로 실행하고 궤적·안전 지표를 반환."""
    in_name = sess.get_inputs()[0].name
    substeps = int(round((1.0 / CONTROL_HZ) / model.opt.timestep))
    hist = History()
    last_action_il = np.zeros(29)
    q_target = DEFAULT_ANGLES.copy()

    rec = {k: [] for k in ("t", "height", "tilt", "q", "q_target", "tau_sat")}

    def step_physics():
        for _ in range(substeps):
            q = data.qpos[7:]
            dq = data.qvel[6:]
            tau = KP * (q_target - q) - KD * dq
            lim = model.actuator_ctrlrange[:, 1]
            rec["tau_sat"].append(float((np.abs(tau) > lim).mean()))
            data.ctrl[:] = np.clip(tau, -lim, lim)
            mujoco.mj_step(model, data)

    dt_ctrl = 1.0 / CONTROL_HZ
    t_wall = time.perf_counter()

    def sync():
        """뷰어 갱신 + 실시간 속도 맞추기."""
        nonlocal t_wall
        if viewer is not None:
            viewer.sync()
        if realtime:
            t_wall += dt_ctrl
            lag = t_wall - time.perf_counter()
            if lag > 0:
                time.sleep(lag)
            else:                      # 밀렸으면 따라잡기를 포기하고 기준을 재설정
                t_wall = time.perf_counter()

    # 기본 자세로 안정화 (토큰 없이 PD 만)
    for _ in range(int(settle_s * CONTROL_HZ)):
        step_physics()
        sync()

    for i, token in enumerate(tokens_50hz):
        q = data.qpos[7:].copy()
        dq = data.qvel[6:].copy()
        base_quat = data.qpos[3:7].copy()          # mujoco 도 (w,x,y,z)
        base_ang_vel = data.qvel[3:6].copy()       # free joint 각속도는 body frame

        hist.push(mj_to_il(q - DEFAULT_ANGLES), mj_to_il(dq), last_action_il,
                  base_ang_vel, gravity_dir_from_quat(base_quat))

        action_il = sess.run(None, {in_name: hist.obs(token).astype(np.float32)[None]})[0][0]
        last_action_il = action_il.astype(np.float64)
        q_target = DEFAULT_ANGLES + action_il[ISAACLAB_TO_MUJOCO] * ACTION_SCALE

        step_physics()
        sync()
        if video is not None and i % video_every == 0:
            video.grab(data)

        up = gravity_dir_from_quat(data.qpos[3:7])      # 몸통 기준 중력 방향
        rec["t"].append(i / CONTROL_HZ)
        rec["height"].append(float(data.qpos[2]))
        rec["tilt"].append(float(np.degrees(np.arccos(np.clip(-up[2], -1, 1)))))
        rec["q"].append(data.qpos[7:].copy())
        rec["q_target"].append(q_target.copy())

    for k in ("q", "q_target"):
        rec[k] = np.array(rec[k])
    for k in ("t", "height", "tilt"):
        rec[k] = np.array(rec[k])
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds-dir", type=Path, required=True)
    ap.add_argument("--checkpoints", nargs="+", type=int, default=[4000, 20000])
    ap.add_argument("--trajs", nargs="+", type=int, default=[0, 1, 3])
    ap.add_argument("--scene", type=Path, default=Path(DEFAULT_SCENE))
    ap.add_argument("--decoder", type=Path, default=Path(DEFAULT_DECODER))
    ap.add_argument("--out", type=Path, default=Path("eval_results/closed_loop_mujoco"))
    ap.add_argument("--source", choices=["pred", "gt"], default="pred",
                    help="pred=모델 예측 토큰, gt=정답 토큰(대조군)")
    ap.add_argument("--video", action="store_true", help="롤아웃마다 mp4 렌더링")
    ap.add_argument("--video-size", nargs=2, type=int, default=[640, 480], metavar=("W", "H"))
    ap.add_argument("--viewer", action="store_true",
                    help="MuJoCo 창을 띄워 실시간으로 본다 (DISPLAY 필요)")
    ap.add_argument("--pause", type=float, default=1.5,
                    help="--viewer 일 때 롤아웃 사이 대기 초")
    ap.add_argument("--dataset", type=Path, default=None,
                    help="에피소드 라벨(ep번호·POS/NEG)을 읽을 데이터셋. 생략하면 traj 번호만 표시")
    ap.add_argument("--chunk-blend-frames", type=int, default=0,
                    help="새 chunk 로 넘어갈 때 토큰을 N 프레임 교차 페이드 "
                         "(실기 run_vla_inference 의 --chunk-blend-frames 와 같은 값을 주면 된다)")
    cfg = ap.parse_args()

    model = mujoco.MjModel.from_xml_path(str(cfg.scene))
    sess = ort.InferenceSession(str(cfg.decoder), providers=["CPUExecutionProvider"])
    cfg.out.mkdir(parents=True, exist_ok=True)
    rep = int(round(CONTROL_HZ / TOKEN_HZ))   # 25Hz 토큰을 50Hz 로 ZOH
    EP_LABEL = episode_labels(cfg.dataset)

    # chunk 경계 위치는 덤프 당시의 execution_horizon 이 정한다.
    meta_p = cfg.preds_dir / f"ck{cfg.checkpoints[0]}" / "meta.json"
    horizon = json.loads(meta_p.read_text())["execution_horizon"] if meta_p.exists() else 0
    if cfg.chunk_blend_frames > 0:
        print(f"chunk 블렌딩: {cfg.chunk_blend_frames} 프레임 "
              f"(경계 간격 = execution_horizon {horizon})")

    print(f"scene={cfg.scene}  물리 {1/model.opt.timestep:.0f}Hz  제어 {CONTROL_HZ:.0f}Hz  "
          f"토큰 {TOKEN_HZ:.0f}Hz(ZOH x{rep})  source={cfg.source}\n")
    # 뷰어를 쓸 때는 MjData 를 하나만 만들어 재사용한다 — launch_passive 가 특정
    # MjData 에 묶이므로 롤아웃마다 새로 만들면 창이 갱신되지 않는다.
    data = mujoco.MjData(model)

    def reset():
        mujoco.mj_resetData(model, data)
        data.qpos[:3] = [0.0, 0.0, 0.793]
        data.qpos[3:7] = [1.0, 0.0, 0.0, 0.0]
        data.qpos[7:] = DEFAULT_ANGLES
        mujoco.mj_forward(model, data)

    if cfg.viewer:
        # `import mujoco.viewer` 는 이 스코프의 `mujoco` 를 지역변수로 만들어
        # 모듈 수준 임포트를 가린다. 별칭으로만 바인딩할 것.
        import mujoco.viewer as mj_viewer
        viewer_ctx = mj_viewer.launch_passive(model, data,
                                              show_left_ui=False, show_right_ui=False)
    else:
        viewer_ctx = contextlib.nullcontext()

    rows, store = [], {}
    with viewer_ctx as v:
        if v is not None:
            v.cam.type = mujoco.mjtCamera.mjCAMERA_TRACKING
            v.cam.trackbodyid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, "pelvis")
            v.cam.distance, v.cam.azimuth, v.cam.elevation = 3.0, 135.0, -15.0
        for ck in cfg.checkpoints:
            for k in cfg.trajs:
                tok25 = np.load(cfg.preds_dir / f"ck{ck}" / f"traj_{k}.npz")[cfg.source]
                tok25 = apply_chunk_blend(tok25.astype(np.float64), horizon,
                                          cfg.chunk_blend_frames)
                tok50 = np.repeat(tok25, rep, axis=0).astype(np.float64)

                reset()
                if v is not None:
                    ep = EP_LABEL.get(k, f"traj{k}")
                    print(f"\n▶ 재생: ck{ck} {ep}  ({len(tok25)/TOKEN_HZ:.1f}초)", flush=True)
                    v.sync()
                    time.sleep(cfg.pause)

                vid = None
                if cfg.video:
                    vdir = cfg.out / "video"
                    vdir.mkdir(parents=True, exist_ok=True)
                    vid = VideoWriter(model, vdir / f"{cfg.source}_ck{ck:05d}_traj{k}.mp4",
                                      *cfg.video_size)
                r = rollout(model, data, sess, tok50, video=vid,
                            viewer=v, realtime=cfg.viewer)
                if vid is not None:
                    vid.close()
                store[f"ck{ck}_traj{k}"] = r["q"]
                fell = bool((r["height"] < FALL_HEIGHT).any()
                            or (r["tilt"] > TILT_LIMIT_DEG).any())
                over = np.degrees(r["q"]) < JOINT_LIMITS_DEG[:, 0] - 1e-6
                over |= np.degrees(r["q"]) > JOINT_LIMITS_DEG[:, 1] + 1e-6
                rows.append(dict(checkpoint=ck, traj=k, episode=EP_LABEL.get(k, f"traj{k}"),
                                 fell=fell, min_height=float(r["height"].min()),
                                 max_tilt=float(r["tilt"].max()),
                                 limit_hits=int(over.sum()),
                                 torque_sat=float(np.mean(r["tau_sat"])),
                                 arm_range=float(np.degrees(
                                     r["q"][:, RIGHT_ARM].max(0)
                                     - r["q"][:, RIGHT_ARM].min(0)).max())))
                s = rows[-1]
                print(f"  ck{ck:<6} {s['episode']:<12} {'넘어짐 ✗' if fell else '유지 ✓'}   "
                      f"최저높이 {s['min_height']:.3f}m  최대기울기 {s['max_tilt']:5.1f}°  "
                      f"관절한계위반 {s['limit_hits']:>4}  토크포화 {100*s['torque_sat']:.1f}%  "
                      f"오른팔가동 {s['arm_range']:5.1f}°", flush=True)

    (cfg.out / f"summary_{cfg.source}.json").write_text(json.dumps(rows, indent=1))
    np.savez_compressed(cfg.out / f"traj_{cfg.source}.npz", **store)
    print(f"\n저장: {cfg.out}")


if __name__ == "__main__":
    main()
