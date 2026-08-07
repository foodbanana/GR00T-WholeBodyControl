# VLA 실기 구동 절차

터미널 6개를 띄우고 로봇을 돌리기까지의 순서. 2026-08-07 첫 실기 구동에서
확정된 설정을 반영한다. 배경과 근거는
[`v2_validation_and_deploy_runbook.md`](v2_validation_and_deploy_runbook.md) 참조.

**확정 설정** — `checkpoint-18000` + `--chunk-blend-frames 2` + `--action-publish-rate 25`

```
┌──────────────────────┐
│  Isaac-GR00T         │   T1  KIST 서버 GPU0 :5550
│  PolicyServer        │
└──────┬───────────────┘
       │ ZMQ REQ/REP  (T2 SSH 터널 localhost:5551 → 5550)
       ▼
┌─────────────────────┐    ZMQ TCP    ┌──────────────────────┐
│  VLA Inference      │ ◄─────────── │  Camera Server       │
│  T6  :5556 bind     │   :5555      │  T3  로봇 온보드      │
│                     │              └──────────────────────┘
│                     │   SUB :5580   ┌──────────────────────┐
│                     │ ◄─────────── │  키보드 publisher     │
└────┬───────────┬────┘              │  T5                  │
     │ PUB :5556 │ SUB :5557         └──────────────────────┘
     ▼           ▼
┌─────────────────────┐
│  C++ Deploy         │   ★ 5580 을 구독하지 않는다.
│  T4                 │     k/i/p 는 T6 가 받아 5556 으로 번역한다
└─────────────────────┘
```

---

## Terminal 1 — PolicyServer (GPU 서버)

```bash
# 서버 접속 (2단)
ssh <USER>@<GATEWAY_IP> -p 4648
ssh <GPU_SERVER_IP> -p 4648

# ★ HF_HOME 을 설정한다. 없으면 VLM 백본(Cosmos-Reason2-2B, gated repo)을
#   받으려다 401 로 죽는다. --model-path 가 로컬이어도 백본은 항상 HF 를 탄다.
source ~/groot_env.sh
cd ~/Isaac-GR00T

uv run python gr00t/eval/run_gr00t_server.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-18000 \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 \
    --port 5550
```

`Loading checkpoint shards: 100%` 까지 약 1분.

> **`--port 5550` 필수.** 기본값은 5555 로 카메라 서버와 같은 번호다. 서버만
> 기본값으로 띄우면 클라이언트가 **에러 없이 조용히** 못 붙는다.

체크포인트 목록:

```bash
ls -d ~/groot_output/rab-v2b-20260806/checkpoint-* | sed 's#.*checkpoint-##' | sort -n
```

## Terminal 2 — SSH 터널 (DGX Spark)

GPU 서버는 게이트웨이 뒤에 있고 열린 포트는 SSH(4648)뿐이다. ZMQ 트래픽을
SSH 안에 실어 나른다. `-L` 의 `localhost` 는 **서버 입장**에서 해석된다.

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -L 5551:localhost:5550 kist-5090
```

출력 없이 멈춰 있는 것이 정상. 다른 터미널에서 확인:

```bash
ss -ltn | grep 5551
# LISTEN 0  128  127.0.0.1:5551  0.0.0.0:*
# LISTEN 0  128      [::1]:5551     [::]:*
```

`ss` 는 **입구가 열렸다**는 것만 보여준다. 반대편까지 닿는지는 아래 스모크
테스트로 확인한다.

## Terminal 3 — 카메라 서버 (head-only, 로봇 온보드)

```bash
ping -c 3 192.168.123.164
ssh unitree@192.168.123.164        # 비밀번호는 별도 전달

cd ~/tw_gearsonic/GR00T-WholeBodyControl
# autosuspend 는 D405(손목)용이라 head-only 면 생략 가능

NO_WRISTS=1 HEAD_SERIAL=938422073271 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

ego_view 를 **640x480 @ 29fps** 로 발행한다(head-only, 손목 없음).

**(선택) 카메라 확인** — 녹화할 때는 끌 것:

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 --camera-port 5555
```

## Terminal 4 — C++ deploy (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
# → Y → Init done
```

SONIC 컨트롤러는 기본값(`policy/release/`)을 쓴다. **`--cp` / `--obs-config` 를
주지 않는다** — VLA 가 이 컨트롤러의 latent 공간으로 학습됐다. 다른 SONIC
체크포인트로 바꾸면 같은 토큰이 다른 자세로 디코드된다.

이 시점에 5557 로 나오는 것은 `robot_config` 뿐이다. `g1_debug` 는 제어루프가
tick 할 때만 나오므로 **정상**이다(아래 `k` 참조).

## Terminal 5 — 키보드 publisher (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_inference/bin/activate
python gear_sonic/scripts/keyboard_publisher.py
```

띄우기만 하고, **키 입력은 T6 까지 다 뜬 뒤**에 한다(맨 아래 절 참조).

## Terminal 6 — VLA Inference (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_inference/bin/activate
python gear_sonic/scripts/run_vla_inference.py \
    --host localhost --port 5551 \
    --embodiment-tag unitree_g1_sonic \
    --prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --action-publish-rate 25 \
    --chunk-blend-frames 2
```

| 인자 | 이유 |
|---|---|
| `--port 5551` | T2 터널의 로컬 입구 |
| `--action-publish-rate 25` | 모델이 **25fps** 로 학습됐다. `delta_indices=range(40)` 이 연속 프레임이라 기본값 50 이면 **동작이 2배 속도로 재생** |
| `--chunk-blend-frames 2` | 2026-08-07 실측 최적. 아래 표 참조 |
| `--prompt` | 학습과 **동일 문장**. 다르면 모델이 이미지 대신 프롬프트로 판별할 여지가 생긴다 |
| 카메라 인자 | `--camera-decode-reduce 1` / `--camera-image-size 640x480` 은 이미 기본값이라 생략 가능 |

`waiting for state msg` 에서 대기하는 것이 정상. 카메라·프롬프트·상태 구독이
모두 붙었고 로봇 상태만 없는 상태다.

## (선택) 기동 확인

```bash
cd ~/GR00T-WholeBodyControl
.venv_inference/bin/python gear_sonic/scripts/smoke_test_policy_server.py --port 5551
```

로봇·카메라·deploy 없이 정책 홉만 검증한다. `PASSED` 면 왕복 190ms 내외,
`motion_token |max|` 가 1.25 미만이다.

---

# 키보드 조작 (T5 에서 입력)

**T1~T6 이 모두 뜬 뒤에** 입력한다. T6 이 없으면 키가 그냥 버려진다 — C++ deploy
는 `--input-type zmq_manager` 라 5580 을 구독하지 않고, `run_vla_inference.py`
만 받아서 5556 으로 번역해 보낸다. ZMQ PUB 는 저장하지 않으므로 구독자가 없는
동안 누른 키는 사라진다(안전하지만, 고장난 줄 알기 쉽다).

시작 전에: **로봇을 붙잡을 수 있는 상태로, T5 창에 손을 올려둔 채로.**

| 순서 | 키 | 일어나는 일 | 확인 |
|---|---|---|---|
| 1 | `k` | 제어루프 시작 (PLANNER 모드) | T6 의 `waiting for state msg` 가 멎는다 |
| 2 | `i` | 초기 자세로 블렌딩 → POSE 모드 | **팔이 내려간 자세인지 눈으로 확인** |
| 3 | `p` | 추론 시작 | `New action chunk (... latency: 0.19s)` |
| — | `p` | 일시정지 | 로봇은 현재 자세 유지 |
| — | `k` | 제어루프 정지 | |

기타: `[` `]` 손 열기/닫기 토글, `t <문장>` 프롬프트 실행 중 교체.

**중단 순서:** `p`(추론 정지) → `k`(제어루프 정지) → 리모컨 비상정지

## ★ 2번에서 팔이 올라가면 그 실행은 버린다

`i` 는 `initial_poses.py` 의 **하드코딩된 상수 토큰**을 보낸다. 모델과 무관하다.
그런데 디코더 입력 994차원 중 **930차원이 로봇의 최근 10프레임 상태**라, 같은
토큰이라도 직전 자세에 따라 다르게 디코드된다.

```
offset  0   token_state                     64   ← i 가 보내는 상수
       64   his_base_angular_velocity       30  ┐
       94   his_body_joint_positions       290  │
      384   his_body_joint_velocities      290  ├─ 930 = 로봇의 현재/최근 상태
      674   his_last_actions               290  │
      964   his_gravity_dir                 30  ┘
```

**체크포인트를 바꿀 때는 T6 도 반드시 재시작한다.** 서버만 바꾸면
`last_sent_motion_token` 에 이전 세션의 토큰이 남아 `blend_to_initial_pose()`
경로를 타고, 시작 조건이 달라진다. T6 를 새로 띄우면 그 값이 `None` 이라
`publish_initial_pose()` 로 초기 토큰을 바로 발행한다.

```
T6 Ctrl+C → 재실행 → k → i (자세 확인) → p
```

2026-08-07 에 실제로 이 함정을 밟았다. ck18000 만 T6 를 유지한 채 서버를 바꿔서
`i` 때 팔이 올라갔고, 전체 재시작 후 재측정하니 정상이었다.

---

# 이상 징후

| 로그 / 증상 | 의미 | 조치 |
|---|---|---|
| `latency:` 가 0.4s 초과 | 원격 경로가 밀림 | 터널·서버 확인 |
| `action['motion_token'] max (...) > 1.25 ... skipping` | chunk 가 통째로 버려짐 | 로봇이 멈춘 것처럼 보인다. 체크포인트의 토큰 크기 확인 |
| 로그가 아예 멎음 | 터널/서버 사망 | 로봇은 **마지막 동작을 붙들고 굳는다** → `p` → `k` |
| `i` 후 팔이 올라감 | 이전 세션 상태 이월 | T6 재시작 후 다시 |
| `401 ... Cosmos-Reason2-2B` | T1 에서 `source ~/groot_env.sh` 누락 | 다시 |
| 클라이언트가 조용히 안 붙음 | T1 의 `--port` 누락(기본 5555) | 다시 |

---

# 2026-08-07 실기 결과

`--action-publish-rate 25`, 프롬프트 고정, 음성(바나나 없음) → 양성 순서.
매 실행 T6 재시작으로 시작 조건을 맞췄다.

| 체크포인트 | blend | 과제 | 관찰 |
|---|---|---|---|
| ck2000 | 0 | O | 정지 시 떨림, 몸 흔들림 |
| ck2000 | **3** | **X** | 떨림은 약간 줄었으나 **팔이 올라가지 않음** |
| ck8000 | 0 | O | 동작 중 팍 튐 |
| ck18000 | 0 | O | 팔 **내릴 때** 가끔 빠름 |
| **ck18000** | **2** | **O** | **부드러움 — 채택** |

`--chunk-blend-frames` 는 코드에 "하드웨어 미검증"으로 적혀 있던 옵션이다.
이제 실측이 있다.

- **`1` 은 무효다** — `alpha = 1/1 = 1.0` 이라 새 토큰이 그대로 나간다.
  의미 있는 최소값은 `2`
- **`3` 은 ck2000 에서 과제를 죽였다** — latent 선형 보간은 자세의 보간이
  아니다. chunk 당 10프레임 중 앞 3프레임이 매번 직전 자세로 끌려가면, 여러
  chunk 에 걸친 상승 동작이 누적 상쇄된다
- **`2` 는 ck18000 에서 과제를 유지하며 부드러워졌다**

세 체크포인트가 모두 과제를 수행했고, open-loop 관절공간 평가도 2° 안에
붙어 있다(ck2000 23.18° / ck8000 24.86° / ck18000 23.02°). 실기에서 뚜렷한
우열이 없어 **사전 지표 최저인 ck18000** 으로 정했다.

## 튐과 떨림은 별개 증상이다

| 증상 | 언제 | 원인 |
|---|---|---|
| **튐** | 동작 중, 특히 팔 내릴 때 | chunk 경계의 **판단 전환** |
| **떨림** | 정지 시 | 미상 |

튐의 기전은 인덱싱 버그가 아니다. 타이밍은 정확히 맞는다:

```
지연 보상   index = round(0.19s × 25Hz) ≈ 5
chunk N     index 5→15 을 0.4초에 재생  = t_obs+0.2s → t_obs+0.6s
chunk N+1   0.4초 뒤 관측, index 5 ↔ t_obs+0.4+0.2 = t_obs+0.6s
                                                    ─────────────
                                                    시각이 일치
```

즉 **0.4초 간격의 서로 다른 이미지로 돌린 두 forward pass 가 같은 시각에 대해
다른 답을 내는 것**이다. 바나나를 치우면 정책이 "든다"→"쉰다"로 판단을 뒤집고,
그 전환이 경계에 걸리면 최대 크기의 점프가 된다(런북 실측 최대 204°, 일반
프레임은 4.8°). 팔을 올릴 때 덜 튀는 것은 상승이 여러 chunk 에 걸친 연속
동작이라 이웃 chunk 끼리 크게 다르지 않기 때문이다.

**`--rate` 를 낮추는 것은 이 증상에 듣지 않는다.** 경계 수는 줄지만 판단이
뒤집히는 그 한 번의 경계는 그대로이고, 바나나 반응이 최대 0.8초까지 늦어진다.

떨림은 blend 로 거의 줄지 않았다. 즉 chunk 경계가 주범이 아니다.

---

# 미해결

1. **blend 경계** — ck18000 에서 `--chunk-blend-frames 3` 을 보면, ck2000 의
   실패가 개입 강도 때문인지 체크포인트 때문인지 갈린다
2. **정지 시 떨림** — 후보는 제어루프 50Hz(`control_dt_ = 0.02`) vs 토큰 발행
   25Hz 의 계단 입력. **미검증.** 발행은 50Hz 로 하되 연속 토큰 사이를 보간해
   원래 타이밍과 매끄러움을 동시에 얻는 방향이 있으나, 현재 코드는 발행 주기와
   토큰 소비 주기가 묶여 있다
3. **초기 자세 토큰** — `LATENT_INITIAL_MOTION_TOKEN` 이 데모 시작 분포와
   cosine 0.579 / 거리 1.210 (에피소드 자체 편차 0.482 의 2.3배) 떨어져 있다.
   교체 후보는 medoid `episode_000044` 의 frame-0 (평균과의 거리 0.222 로 79개
   중 가장 대표적)
