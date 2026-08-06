# Open-loop eval — decoded joint space (29 DoF)

작성 2026-08-06. 대상: `raise-arm-banana-val-20260801` 체크포인트들.

## 1. 목표

기존 eval은 `motion_token`(64-dim latent)의 MSE를 봤다. 스케일 기준이 없어
"MSE 0.006이 좋은 건지 나쁜 건지" 판단이 불가능했다.

바꾼다 → **예측 motion_token을 실제 WBC decoder에 통과시켜 29-DoF 관절값으로
디코드하고, 데이터셋의 실제 관절값과 같은 축에 겹쳐 그린다.** 지표도 관절공간
MSE(rad²)와 RMSE(도)로 낸다. 기존 Isaac-GR00T open-loop eval과 같은 형식.

우선순위 관절은 **오른팔 7개**(task = "바나나를 보면 오른팔을 올린다").
손(Dex3)은 이번 범위에서 제외.

> 폐기된 대안: 데이터셋에서 `token→joint` 매핑을 학습하는 proxy decoder.
> 정확도가 낮아(오른팔 RMSE 8° 이상) 채택하지 않는다. 아래는 실제 ONNX
> decoder를 그대로 쓰는 방법이다.

## 2. 검증된 규격

### 2.1 decoder ONNX

`gear_sonic_deploy/policy/release/model_decoder.onnx`

```
IN   obs_dict [1, 994] float32
OUT  action   [1, 29]  float32
```

994 = 64 + 30 + 290 + 290 + 290 + 30. `observation_config.yaml` 상단 주석의
`436` 은 stale(4frame 시절 값)이므로 무시할 것. 실제 enable된 것은 10frame 계열.

### 2.2 obs 994 레이아웃과 데이터셋 소스

`observation_config.yaml` 의 `observations:` 나열 순서대로 이어붙인다.

| offset | 블록 | dim | 데이터셋 소스 |
|---|---|---|---|
| 0 | `token_state` | 64 | `action.motion_token` (GT 또는 모델 예측) |
| 64 | `his_base_angular_velocity_10frame_step1` | 30 = 3×10 | `observation.root_orientation` 쿼터니언 차분 |
| 94 | `his_body_joint_positions_10frame_step1` | 290 = 29×10 | `observation.state` |
| 384 | `his_body_joint_velocities_10frame_step1` | 290 | `observation.state` 차분 |
| 674 | `his_last_actions_10frame_step1` | 290 | `action.wbc` |
| 964 | `his_gravity_dir_10frame_step1` | 30 = 3×10 | `observation.root_orientation` 에서 계산 |

- history는 **oldest-first** (`newest_first = false` 기본값)
- 프레임 간격 `sample_dt = control_dt × step_size`, `step_size = 1`
- `gravity_dir = quat_rotate(quat_conjugate(base_quat), [0, 0, -1])`
- 각 블록 내부는 `[frame][joint]` 순서 (frame-major)

근거: `gear_sonic_deploy/src/g1/g1_deploy_onnx_ref/src/g1_deploy_onnx_ref.cpp`
의 `GatherHis*` 함수들(1461~1641행)과 등록 테이블(1793~1802행).

### 2.3 43 → 29 관절 추출

데이터셋의 43-dim 레이아웃(`meta/modality.json`):

```
left_leg 6 | right_leg 6 | waist 3 | left_arm 7 | left_hand 7 | right_arm 7 | right_hand 7
0        6 12          15        22          29           36            43
```

손 14개를 빼면 29 DoF → **인덱스 `[0:22] + [29:36]`**.
오른팔은 이 29-vector에서 `[22:29]`.

### 2.4 관절 순서와 단위 — 가장 헷갈리는 부분

C++ **내부** 상태와 **ZMQ로 내보낸** 값(=데이터셋에 기록된 값)이 다르다.

내부(`g1_deploy_onnx_ref.cpp:2839`):

```cpp
body_q[i] = unitree_joint_state[mujoco_to_isaaclab[i]].q() - default_angles[mujoco_to_isaaclab[i]];
```

→ **isaaclab 순서 + default_angles 차감**. (`// URDF order` 주석은 오해를 부른다.)

발행(`zmq_output_handler.hpp:315-334`):

```cpp
body_q_mujoco[i]      = state.body_q[isaaclab_to_mujoco[i]] + default_angles[i];
body_dq_mujoco[i]     = state.body_dq[isaaclab_to_mujoco[i]];
last_action_mujoco[i] = state.last_action[isaaclab_to_mujoco[i]] * g1_action_scale[i] + default_angles[i];
```

→ 되돌려서 내보낸다. 따라서 **데이터셋에 있는 값은**:

| 컬럼 | 의미 |
|---|---|
| `observation.state`[body29] | **절대 관절각 (rad), mujoco 순서** |
| `action.wbc`[body29] | **관절목표 (rad), mujoco 순서** |

`last_action_mujoco` 계산식은 실제 모터 명령을 만드는 식
(`g1_deploy_onnx_ref.cpp:3135`)과 **완전히 동일**하다. 즉 `action.wbc` 는 raw
네트워크 출력이 아니라 이미 스케일·오프셋이 적용된 관절목표다. 덕분에
`observation.state` 와 **같은 단위·같은 순서**라 바로 겹쳐 그릴 수 있다.

### 2.5 필요한 변환

**데이터셋 → decoder 입력** (mujoco/절대 → isaaclab/상대):

```
body_q_il[ISAACLAB_TO_MUJOCO[i]]      = state_mj[i] - default[i]
body_dq_il[ISAACLAB_TO_MUJOCO[i]]     = dq_mj[i]                       # state 차분
last_action_il[ISAACLAB_TO_MUJOCO[i]] = (wbc_mj[i] - default[i]) / scale[i]
```

**decoder 출력 → 비교/플롯** (isaaclab/raw → mujoco/rad):

```
q_target_mj[i] = default[i] + action_il[ISAACLAB_TO_MUJOCO[i]] * scale[i]
```

상수(`g1_action_scale`, `default_angles`, `isaaclab_to_mujoco`)는
`policy_parameters.hpp` 에 있고, 구현은 `gear_sonic/scripts/wbc_decoder.py` 에 옮겨두었다.

## 2.6 참고 — 학습 중 val/loss (정답지)

`train_banana_val2.log` 에 `step N  val/loss = X` 형식으로 100회 기록돼 있다.
(로그 키는 `val/loss` 다. `eval_loss` 로 grep하면 0건이라 없다고 오판하기 쉽다.)

| step | 2000 | 4000 | 6000 | 8000 | 10000 | 12000 | 14000 | 16000 | 18000 | 20000 |
|---|---|---|---|---|---|---|---|---|---|---|
| val/loss | **0.1632** | 0.1716 | 0.1676 | 0.1874 | 0.2060 | 0.2282 | 0.2735 | 0.2910 | 0.3346 | 0.3408 |

최저 0.1482 @ step 2800 이후 단조 증가 → **과적합 확정, 최적은 ck2000, ck20000이 최악.**

기존 latent MSE는 ck2000 0.00652 → ck20000 0.00621 로 이 변화를 전혀 잡지 못했다.
**따라서 이번 decoded joint 지표의 1차 검증 포인트는 "ck2000 < ck20000 을 제대로
구분해내는가" 이다.** 구분해내면 지표가 유효하고, 여기서도 평평하면 지표 설계를
다시 봐야 한다.

## 3. 평가 대상 데이터

- held-out: `~/dataset/raise_arm_banana_val5` = merged 에피소드 **49, 50, 51, 52, 53**
- 길이: **218, 172, 150, 164, 174** (합 878)
- 예측 토큰: `~/eval_heldout/preds/ck{2000,4000,6000,8000,10000,20000}/traj_{0..4}.npz`
  - 각 npz에 `gt (200,64) float64`, `pred (200,64) float32`
  - **주의**: `--steps 200` 으로 덤프했으나 traj 1~4는 에피소드가 200프레임보다
    짧다. 기존 latent MSE 수치에는 패딩 구간이 섞여 있다.
    **새 eval에서는 에피소드 실제 길이로 잘라서 계산할 것.**

GPU 재추론은 불필요하다 — 예측 토큰이 이미 덤프되어 있다.

### 3.1 ★양성/음성 에피소드가 섞여 있다★

task가 "바나나를 **보면** 오른팔을 올린다"는 **조건부**라, 팔을 올리지 않는 음성
에피소드가 존재한다. 오른쪽 어깨 pitch 가동범위로 구분하면:

| | 전체 | 양성(팔 올림) | 음성(안 올림) |
|---|---|---|---|
| merged 55 | 55 | 52 | **3** (ep 4, 9, 49) |
| train50 | 50 | 48 | 2 (4%) |
| val5 | 5 | 4 | 1 (20%, ep49) |

- 양성: 어깨 pitch 110~167°, 팔꿈치 95~127° 움직임
- 음성: 오른팔 가동범위 1~4° (사실상 정지)

**지표를 낼 때 양성/음성을 반드시 분리한다.** 기존 latent MSE에서 traj_0(=ep49)이
가장 낮은 MSE(0.0018)를 기록했는데, 잘 맞춰서가 아니라 **아무것도 안 움직여서**다.
섞어서 평균내면 이런 착시가 생긴다.

별개로, 음성 예제가 학습 데이터의 4%뿐이라 "조건부" 자체가 학습됐는지 의심스럽다.
항상 팔을 올려도 train 96% / val 80%를 맞힌다. 데이터 구성 판단은 별도 사안.

## 4. 절차

### Phase 0 — 검증 게이트 (선행 필수)

> **GT token + GT history 를 decoder에 넣은 출력이 기록된 `action.wbc` 를 재현하는가?**

재현되면 오프라인 harness가 정확하다는 뜻이고, 이후 token만 교체하면 된다.
안 되면 어느 근사가 깨뜨렸는지 이 지점에서 드러난다. 비용은 CPU 몇 초.

두 변형을 모두 돌려 비교한다:

- **(a) 25Hz 그대로** — 데이터셋 프레임을 그대로 decoder에 먹인다. 단순하지만
  history 10프레임이 200ms가 아니라 400ms를 덮으므로 decoder 입장에선 분포가 다르다.
- **(b) 50Hz 보간 후 실행 → 출력 25Hz 다운샘플** — 관절/쿼터니언을 50Hz로 보간해
  0.02s 간격 history를 만들어 decoder를 원래 분포에 맞춘다. 실제 시스템에서도 VLA는
  느리게 토큰을 내고 50Hz decoder가 그 토큰을 반복 소비하므로 실제에 더 가깝다.

**통과 기준**: 재현 RMSE가 아래 Phase 2에서 볼 모델 오차보다 확실히 작을 것.
(a)/(b) 중 더 낮은 쪽을 채택한다. 둘 다 크면 Phase 1로 넘어가지 않고 원인부터 잡는다.

#### ✅ Phase 0 실측 결과 (2026-08-06, val5 5개 에피소드)

실행: `gear_sonic/scripts/eval_decoded_gate.py` (앞 10프레임은 history 미충전이라 제외)

| 변형 | 전체 RMSE | 오른팔 RMSE | MSE |
|---|---|---|---|
| (a) 25Hz | 1.219° | 1.970° | 0.000453 rad² |
| **(b) 50Hz 보간** | **0.910°** | **1.529°** | **0.000252 rad²** |

**→ (b) 50Hz 채택.** 예상대로 decoder 원래 분포에 맞춘 쪽이 낫다.

에피소드별 (50Hz): ep49 0.27° / ep50 0.98° / ep51 1.00° / ep52 1.14° / ep53 1.02°
(ep49는 팔을 안 움직이는 음성 에피소드라 오차가 작다.)

오른팔 관절별 RMSE(°): R_shoulder_pitch 2.56, R_elbow 2.12, R_shoulder_yaw 1.60,
R_shoulder_roll 1.42, R_wrist_roll 0.78, R_wrist_yaw 0.36, R_wrist_pitch 0.15

**해석**: 오른팔이 실제로 110~167° 움직이는 신호에 대해 재현 바닥값이 1.5°다
(신호 대비 약 1%). 모델 오차를 재기에 충분한 해상도다. 잔차의 원인은 joint
velocity·base angular velocity를 실측이 아니라 차분으로 유도한 것, 그리고
25→50Hz 보간이다.

### Phase 1 — 예측 토큰 디코드

Phase 0에서 채택한 방식으로, history는 **데이터셋 GT를 그대로 사용**(teacher forcing)
하고 `token_state` 만 `pred` 로 교체해 decoder를 돌린다.

history까지 자기 출력으로 갈아끼우면 오차가 누적돼 발산하므로, 토큰 품질만
분리해서 보려면 teacher forcing이 맞다. 체크포인트 6개 × traj 5개.

### Phase 2 — 플롯 & 지표

관절 하나당 3개 곡선을 겹쳐 그린다 (x축 = 시간(초)):

1. **실제 관절값** — `observation.state`[해당 관절]
2. **GT 명령** — `action.wbc` → 관절각 변환 (2.5절)
3. **모델 예측** — `decoder(pred_token)` → 관절각 변환

지표:

- **decoded joint MSE** (rad²) 및 **RMSE(도)** — 전체 29, 그리고 **오른팔 7개** 별도
- 관절별 RMSE 표
- 체크포인트별 추이 (2000 → 20000에서 실제로 좋아지는가)

## 5. 산출물

```
~/eval_decoded/
  gate.md                     Phase 0 결과 (a/b 비교, 채택 근거)
  metrics.csv                 ckpt × traj × joint RMSE
  summary.md                  체크포인트별 오른팔 RMSE 추이
  plots_ck{N}/traj{K}_rightarm.png    오른팔 7관절 (우선)
  plots_ck{N}/traj{K}_all29.png       전체 29관절
```

## 6. 리스크

| 항목 | 내용 | 대응 |
|---|---|---|
| joint velocity 미기록 | `observation.state` 25Hz 차분으로 유도 → 노이즈 | Phase 0가 판정 |
| base angular velocity 미기록 | `observation.root_orientation` 차분으로 유도 | Phase 0가 판정 |
| 25Hz vs 50Hz | history 시간창 불일치 | (a)/(b) 비교 후 채택 |
| 관절 순서 | isaaclab ↔ mujoco 변환 실수 가능 | Phase 0가 판정 |
| 기존 preds 패딩 | traj 1~4가 200프레임 미만 | 에피소드 길이로 절단 |

## 7. 실행 위치

decoder ONNX와 `raise_arm_banana_merged` 가 모두 DGX Spark 로컬에 있으므로
**Phase 0은 로컬에서 CPU만으로 수행**한다. 예측 토큰 npz(개당 ~30KB)만 서버에서
받아오면 Phase 1~2도 로컬에서 끝난다. GPU 재추론 없음, 서버 무변경.

공용 서버 작업 시 수칙은 세션 메모리 `feedback_kist_server_conduct` 참조.
