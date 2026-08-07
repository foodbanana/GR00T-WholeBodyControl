# 5. 실기 배포 — VLA 구동

파인튜닝된 VLA 를 **GPU 서버의 PolicyServer** 로 띄우고, DGX Spark 가 카메라·로봇과
중계해 **Unitree G1** 을 움직인다. 터미널 6개를 순서대로 띄우고 키 3개를 누르면 된다.

**이전** ← [4. 평가](4_evaluation.md)

> **확정 설정 (2026-08-07 첫 실기 구동)**
> `checkpoint-18000` + `--chunk-blend-frames 2` + `--action-publish-rate 25`

원본: [vla_run_procedure.md](vla_run_procedure.md) (실행 순서) /
[v2_validation_and_deploy_runbook.md](v2_validation_and_deploy_runbook.md) STEP 8 (배경과 근거)

---

## 0. 구조

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
└────┬───────────┬────┘              └──────────────────────┘
     │ PUB :5556 │ SUB :5557
     ▼           ▼
┌─────────────────────┐              ┌──────────────────────┐
│  C++ Deploy         │ ◄─ SUB :5580 │  키보드 publisher     │
│  T4                 │              │  T5                  │
└─────────┬───────────┘              └──────────────────────┘
          │ DDS (unitree SDK)          (T6 도 5580 을 구독)
          ▼
     G1 로봇 (29 DoF + Dex3)
```

| 구간 | 프로토콜 | 포트 | 내용 |
|---|---|---|---|
| T3 → T6 | ZMQ | 5555 | `ego_view` 카메라 이미지 |
| T6 ↔ T1 | ZMQ REQ/REP | **5551 → 5550** | 관측 → **motion_token 40개 chunk** |
| T6 → T4 | ZMQ PUB | 5556 | motion_token 1개씩 (**25Hz**) |
| T4 → T6 | ZMQ SUB | 5557 | 로봇 상태 (`g1_debug`) |
| T5 → T6 | ZMQ PUB | 5580 | 키 입력 `k`/`i`/`p`/`t` |
| T4 → 로봇 | DDS | — | 관절 목표 (50Hz) |

### 데이터 수집 때와 무엇이 다른가

```
[데이터 수집]  사람 → PICO VR → pico_manager_thread_server.py ─┐
                                                              ├─► C++ Deploy → 로봇
[VLA 추론]     카메라 → T6 VLA Inference ↔ T1 PolicyServer ────┘
```

**T6 가 `pico_manager_thread_server.py` 자리를 대체**하고 **PolicyServer 가 새로 추가**된다.
사람이 조종하던 것을 모델이 대신하므로 **둘을 동시에 띄우면 안 된다.**

---

## 1. 사전 조건

로봇 없이 미리 끝낼 수 있는 것들이다. 로봇 전원이 올라오면 남는 건 카메라 서버뿐이다.

| 항목 | 확인 방법 |
|---|---|
| C++ deploy 바이너리 | `ls gear_sonic_deploy/target/release/g1_deploy_onnx_ref` |
| `.venv_inference` | `.venv_inference/bin/python -c "import gr00t, torch; print(torch.cuda.is_available())"` |
| SSH 터널 | `ssh kist-5090 hostname` ([3번 문서 §0-1](3_finetune_groot_n17.md#0-1-접속--2단-점프)) |
| 체크포인트 | `ssh kist-5090 'ls -d ~/groot_output/rab-v2b-20260806/checkpoint-* \| sed "s#.*checkpoint-##" \| sort -n'` |
| 로봇 네트워크 | `ping -c 3 192.168.123.164` |

### PolicyServer 를 로컬이 아니라 **서버에서** 돌리는 이유

| 안 | 장점 | 문제 |
|---|---|---|
| A. DGX Spark(로컬) | 네트워크 지연 없음 | 체크포인트마다 **6.5GB** 를 내려받아야 하고 GB10 한 장을 추론이 점유한다 |
| **B. KIST 서버(원격)** ✅ | 체크포인트가 이미 서버에 다 있어 **CLI 로 즉시 교체**, GPU 4장 | 네트워크 지연 — **해상도 조정으로 해결됨(아래)** |

### 원격 지연 — 전송량이 곧 지연이다

관측치는 msgpack 으로 **압축 없이 raw uint8** 로 넘어간다. 즉 **이미지 해상도가 왕복 시간을
그대로 결정한다.** 터널 너머 실측 (2026-08-07):

| ego_view 해상도 | 전송량/회 | **왕복 시간** |
|---|---|---|
| 1920x1080 | 6.22 MB | **647 ms** ❌ |
| 960x540 | 1.56 MB | 243 ms |
| **640x480** ✅ | 0.92 MB | **192 ms** |

2.5Hz 예산이 **400ms** 인데 원본 해상도는 그것만으로 예산을 넘긴다.
현재 head 카메라는 **640x480 네이티브**라 `--camera-image-size 640x480`(기본값)이 그대로 맞는다.

> 속도만의 문제가 아니다. **학습 때와 같은 전처리 경로여야 한다** —
> v2 수집 로그상 ego_view 가 `(480, 640)` 으로 리사이즈 없이 기록됐다.
> 원본을 그대로 보내면 학습 때와 다른 리샘플링을 거친 이미지를 모델에 준다.

---

## 2. 터미널 6개

### T1 — PolicyServer (GPU 서버)

```bash
# 서버 접속 (2단)
ssh ltw1203@161.122.21.93 -p 4648
ssh 192.168.135.101 -p 4648
# 또는 SSH config 를 등록했다면:  ssh kist-5090

tmux new -s policy                      # SSH 를 닫아도 살아 있게

source ~/groot_env.sh                   # ★ 없으면 401 로 죽는다
cd ~/Isaac-GR00T
uv run python gr00t/eval/run_gr00t_server.py \
    --model-path ~/groot_output/rab-v2b-20260806/checkpoint-18000 \
    --embodiment-tag UNITREE_G1_SONIC \
    --device cuda:0 \
    --port 5550
```

`Loading checkpoint shards: 100%` 까지 약 1분. `Ctrl+b d` 로 detach.

> **★ `source ~/groot_env.sh` 필수.** GR00T 체크포인트는 **VLM 백본을 항상 별도로 로드**한다.
> `--model-path` 가 로컬 경로여도 백본(`nvidia/Cosmos-Reason2-2B`, gated)만은 HF 를 타므로,
> `HF_HOME` 이 없으면 **401 로 죽는다**. 상세는 [3번 문서 §0-3](3_finetune_groot_n17.md#0-3--groot_envsh--모든-명령-앞에-붙는다).

> **★ `--port 5550` 필수.** `run_gr00t_server.py` 의 기본 포트는 **5555 로 카메라 서버와 같다.**
> 서버만 기본값으로 띄우면 클라이언트(`run_vla_inference.py`, 기본 5550)가
> **에러 없이 조용히** 못 붙는다.

**체크포인트 교체**는 `Ctrl+C` 로 끄고 `--model-path` 의 숫자만 바꿔 다시 띄운다
(같은 포트를 두 번 bind 할 수 없다). **이때 T6 도 반드시 재시작한다** — [§4](#-2번에서-팔이-올라가면-그-실행은-버린다) 참조.

### T2 — SSH 터널 (DGX Spark)

GPU 서버는 게이트웨이 뒤에 있고 열린 포트는 SSH(4648)뿐이다. ZMQ 트래픽을 SSH 안에 실어 나른다.
`-L` 의 `localhost` 는 **서버 입장**에서 해석된다.

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 \
    -L 5551:localhost:5550 kist-5090
```

출력 없이 멈춰 있는 것이 정상. 다른 터미널에서 확인:

```bash
ss -ltn | grep 5551
# LISTEN 0  128  127.0.0.1:5551  0.0.0.0:*
```

로컬 5550 은 다른 서버가 쓸 수 있어 **5551 로 뺀다.**
`ss` 는 **입구가 열렸다**는 것만 보여준다 — 반대편까지 닿는지는 [§3 스모크 테스트](#3-선택-기동-확인--스모크-테스트)로 확인한다.

### T3 — 카메라 서버 (로봇 온보드, head-only)

```bash
ping -c 3 192.168.123.164
ssh unitree@192.168.123.164        # pw: 123

cd ~/tw_gearsonic/GR00T-WholeBodyControl
# autosuspend 는 D405(손목)용이라 head-only 면 생략 가능

NO_WRISTS=1 HEAD_SERIAL=938422073271 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

ego_view 를 **640x480 @ 29fps** 로 발행한다. `HEAD_SERIAL` 은 **실측값**을 넣는다
([1번 문서 STEP 1](1_data_collection.md#카메라-시리얼-실측---매번-필수)).

> **v2 모델은 head-only 로 학습됐다** — 손목 카메라 없이 `ego_view` 하나다.
> 그래서 `NO_WRISTS=1` 이 학습 조건과 맞는 설정이다.

**(선택) 카메라 확인** — 녹화할 때는 끌 것:

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 --camera-port 5555
```

### T4 — C++ deploy (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
# → Y → Init done
```

데이터 수집 때와 **완전히 동일**하다.

> **`--cp` / `--obs-config` 를 주지 않는다.** SONIC 컨트롤러는 기본값(`policy/release/`)을 쓴다.
> **VLA 가 이 컨트롤러의 latent 공간으로 학습됐기 때문**이다. 다른 SONIC 체크포인트로 바꾸면
> 같은 토큰이 다른 자세로 디코드된다. ([4번 문서](4_evaluation.md) 의 오프라인 평가도 같은
> `policy/release/model_decoder.onnx` 를 쓴다.)

이 시점에 5557 로 나오는 것은 `robot_config` 뿐이다. `g1_debug` 는 제어루프가 tick 할 때만
나오므로 **정상**이다(아래 `k` 참조).

### T5 — 키보드 publisher (DGX Spark)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_inference/bin/activate
python gear_sonic/scripts/keyboard_publisher.py
```

**띄우기만 하고, 키 입력은 T6 까지 다 뜬 뒤에 한다.**

> tmux 런처는 이걸 pane 안에서 자동으로 띄우지만, **터미널을 직접 여는 경우 빠뜨리기 쉽다.**
> 없으면 `k`/`i`/`p` 가 아무 데도 가지 않는다.

### T6 — VLA Inference (DGX Spark)

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

| 인자 | 기본값 | **우리 값** | 이유 |
|---|---|---|---|
| `--port` | 5550 | **5551** | T2 터널의 로컬 입구 |
| `--action-publish-rate` | **50** | **25** | 모델이 **25fps** 데이터로 학습됐다. `delta_indices=range(40)` 이 데이터셋 프레임 40개 **연속**이라 리샘플링이 없어, 50이면 **동작이 2배 속도로 재생**된다 |
| `--chunk-blend-frames` | **0** | **2** | 2026-08-07 실기 확정값. `0`=팔 내릴 때 튐 / `1`=무효(alpha 1.0) / `3`=ck2000 에서 **과제를 죽였다** |
| `--prompt` | `demo` | 학습과 **동일 문장** | 다르면 모델이 이미지 대신 프롬프트로 판별할 여지가 생긴다 |
| `--camera-image-size` | `640x480` | 기본값 | 현재 head 카메라가 640x480 네이티브 |
| `--camera-decode-reduce` | `1` | 기본값 | 640x480 네이티브라 줄이면 화질만 버린다 |
| `--initial-pose-blend-duration` | `1.0` | 기본값 | `0` 은 초기 자세로 순간 이동해 **위험하다** |

`waiting for state msg` 에서 대기하는 것이 정상이다 — 카메라·프롬프트·상태 구독이 모두 붙었고
로봇 상태만 없는 상태다.

---

## 3. (선택) 기동 확인 — 스모크 테스트

로봇·카메라·deploy 없이 **정책 홉만** 검증한다.

```bash
cd ~/GR00T-WholeBodyControl
.venv_inference/bin/python gear_sonic/scripts/smoke_test_policy_server.py --port 5551
```

`PASSED` 면 왕복 **190ms 내외**, `motion_token |max|` 가 **1.25 미만**이다.
T2 의 `ss` 가 통과해도 여기서 막히면 서버 쪽 문제다.

---

## 4. 키보드 조작 (T5 에서 입력)

**T1~T6 이 모두 뜬 뒤에** 입력한다.

> **T6 이 없으면 키가 그냥 버려진다.** C++ deploy 는 `--input-type zmq_manager` 라 5580 을
> 구독하지 않고(C++ 소스에 5580 이 등장하지 않는다, 2026-08-07 확인), `run_vla_inference.py`
> 만 받아서 5556 의 start 명령으로 번역해 보낸다. ZMQ PUB 는 저장하지 않으므로 구독자가 없는
> 동안 누른 키는 사라진다 — **안전하지만 고장난 줄 알기 쉽다.**

시작 전에: **로봇을 붙잡을 수 있는 상태로, T5 창에 손을 올려둔 채로.**

| 순서 | 키 | 일어나는 일 | 확인 |
|---|---|---|---|
| 1 | `k` | 제어루프 시작 (PLANNER 모드) | T6 의 `waiting for state msg` 가 멎는다 |
| 2 | `i` | 초기 자세로 블렌딩 → POSE 모드 | **팔이 내려간 자세인지 눈으로 확인** |
| 3 | `p` | 추론 시작 | `New action chunk (... latency: 0.19s)` |
| — | `p` | 일시정지 | 로봇은 현재 자세 유지 |
| — | `k` | 제어루프 정지 | |

기타: `[` `]` 손 열기/닫기 토글, `t <문장>` **프롬프트 실행 중 교체**.

**중단 순서:** `p`(추론 정지) → `k`(제어루프 정지) → 리모컨 비상정지

### ★ 2번에서 팔이 올라가면 그 실행은 버린다

`i` 는 [`initial_poses.py`](../gear_sonic/utils/inference/initial_poses.py) 의 **하드코딩된 상수
토큰**(`LATENT_INITIAL_MOTION_TOKEN`)을 보낸다. 모델과 무관하다. 그런데 디코더 입력 994차원 중
**930차원이 로봇의 최근 10프레임 상태**라, 같은 토큰이라도 직전 자세에 따라 다르게 디코드된다.

```
offset  0   token_state                     64   ← i 가 보내는 상수
       64   his_base_angular_velocity       30  ┐
       94   his_body_joint_positions       290  │
      384   his_body_joint_velocities      290  ├─ 930 = 로봇의 현재/최근 상태
      674   his_last_actions               290  │
      964   his_gravity_dir                 30  ┘
```

**체크포인트를 바꿀 때는 T6 도 반드시 재시작한다.** 서버만 바꾸면 `last_sent_motion_token` 에
이전 세션의 토큰이 남아 `blend_to_initial_pose()` 경로를 타고 시작 조건이 달라진다.
T6 를 새로 띄우면 그 값이 `None` 이라 `publish_initial_pose()` 로 초기 토큰을 바로 발행한다.

```
T6 Ctrl+C → 재실행 → k → i (자세 확인) → p
```

> 2026-08-07 에 실제로 이 함정을 밟았다. ck18000 만 T6 를 유지한 채 서버를 바꿔서 `i` 때 팔이
> 올라갔고, 전체 재시작 후 재측정하니 정상이었다.

---

## 5. 안전

**MuJoCo sim2sim(폐루프) 검증을 생략했으므로 이 절이 유일한 안전망이다.** 시뮬이 걸러주던
"넘어지는가"를 사람이 직접 받아내야 한다.

- 첫 구동은 **사람이 붙잡을 수 있는 상태**로, **T5 에 손을 올려둔 채로** 시작
  - 이상하면 `p`(추론 정지) → 그래도 안 되면 `k`(제어루프 정지)
- **첫 시도는 음성 조건(바나나 없음)으로 한다.** 정답이 "가만히 있기"라 모델이 크게 움직이면
  그 자체가 이상 신호다. 양성 조건은 그다음
- `--initial-pose-blend-duration 1.0`(기본) 유지. `0` 은 초기 자세로 순간 이동해 위험하다
- `--chunk-blend-frames` 는 **`2` 를 쓴다.** `3` 은 ck2000 에서 과제를 죽였으므로(팔이 안 올라감)
  올릴 때는 반드시 양성 조건까지 확인할 것

### 초기 자세 토큰이 데모 시작 자세와 다르다 (알고 가는 것)

v2 데이터셋 79개 에피소드의 frame-0 토큰과 비교하면 (2026-08-07 측정):

| 항목 | 값 |
|---|---|
| `‖INITIAL_TOKEN‖` | 1.118 |
| `‖평균 frame-0 토큰‖` | 1.282 |
| 에피소드 간 자체 편차 | 평균 0.482 / 최대 1.080 |
| **dist(frame-0, INITIAL)** | 최소 0.988 / **평균 1.210** / 최대 1.831 |
| cosine(평균 frame-0, INITIAL) | **0.579** |

**데모 시작 분포에서 에피소드 자체 편차의 약 2.3배 떨어져 있고 방향도 다르다.**
정책이 한 번도 시작점으로 본 적 없는 자세에서 추론이 시작되므로 **첫 action chunk 가 튈 수 있다.**

**현재는 값을 유지한다** — 알려진 안전 standing 자세라는 점이 더 중요하다는 판단.
대신 첫 chunk 를 주의해서 보고, 튀면 `--initial-pose-blend-duration` 을 2.0 으로 올린다.
그래도 안 되면 데모 시작 토큰으로 교체한다 — medoid 는 `episode_000044` 의 frame-0
(평균과의 거리 0.222 로 79개 중 가장 대표적).

---

## 6. 환경 — 학습 조건과 맞출 것

| 항목 | 학습 데이터 | 실기에서 |
|---|---|---|
| 카메라 | head 단독 (`ego_view`), 손목 없음 | `NO_WRISTS=1` |
| fps | 25 | `--action-publish-rate 25` |
| **바닥 흰 줄** | **음성 24개에만 있음** | **음성 조건에서 흰 줄이 있어야 학습 조건과 일치** |
| 프롬프트 | `raise your right arm if you see a banana` | 동일 |

> 음성 에피소드 27개 중 24개(ep55~78)가 **바닥에 흰 줄이 있는 상태**로 수집됐다. 모델이
> 바나나가 아니라 흰 줄을 보고 판별할 가능성이 있어 **학습과 같은 환경에서 실행**해야
> 결과가 재현된다.
>
> 다만 오프라인 평가에서는 **흰 줄 없는 대조군 ep49 도 나머지 음성과 같은 수준(1.4~4.3°)** 이라
> 그 우려는 상당 부분 해소됐다 ([4번 문서](4_evaluation.md)).

---

## 7. 실기에서 측정할 것

**① 추론 지연** — chunk 도착마다 자동 출력된다.

```
New action chunk (prompt: "...", latency: 0.132s)
```

`지연 × 25Hz` 가 chunk 시작 인덱스가 된다.

| 값 | 조치 |
|---|---|
| 1~2 프레임 이내 | 무시. 현재 오프라인 평가가 그대로 유효 |
| **4 프레임 이상** | 오프라인 평가에 지연 보상을 반영해 재평가 |

**② 동작 크기** — open-loop 평가에서 모델의 어깨 pitch 진폭은 GT 명령의 **95~99%** 였다
(ck18000). 다만 **로봇은 명령의 82%까지만 도달한다**(PD 정상상태 오차, 어깨 pitch 평균 −14.9°)
— 데이터 수집 때와 같은 현상이므로 **정상**이다.

**③ 조건부 판별** — 바나나 있음/없음에서 실제로 갈리는지. **이것만은 실기에서만 확인 가능하다.**

---

## 8. 이상 징후

| 로그 / 증상 | 의미 | 조치 |
|---|---|---|
| `latency:` 가 0.4s 초과 | 원격 경로가 밀림 | 터널·서버 확인 |
| `action['motion_token'] max (...) > 1.25 ... skipping` | chunk 가 통째로 버려짐 | **로봇이 멈춘 것처럼 보인다.** 체크포인트의 토큰 크기 확인 |
| 로그가 아예 멎음 | 터널/서버 사망 | 로봇은 **마지막 동작을 붙들고 굳는다** → `p` → `k` |
| `i` 후 팔이 올라감 | 이전 세션 상태 이월 | **T6 재시작** 후 다시 ([§4](#-2번에서-팔이-올라가면-그-실행은-버린다)) |
| `401 ... Cosmos-Reason2-2B` | T1 에서 `source ~/groot_env.sh` 누락 | 다시 |
| 클라이언트가 조용히 안 붙음 | T1 의 `--port` 누락(기본 5555) | 다시 |
| 키를 눌러도 무반응 | T6 가 아직 안 떴다 / T5 를 안 띄웠다 | T6 기동 후 다시 입력 |
| 동작이 2배 빠름 | `--action-publish-rate` 기본값(50) | 25 로 |

---

## 2026-08-07 실기 결과

`--action-publish-rate 25`, 프롬프트 고정, 음성(바나나 없음) → 양성 순서.
매 실행 **T6 재시작**으로 시작 조건을 맞췄다.

| 체크포인트 | blend | 과제 | 관찰 |
|---|---|---|---|
| ck2000 | 0 | O | 정지 시 떨림, 몸 흔들림 |
| ck2000 | **3** | **X** | 떨림은 약간 줄었으나 **팔이 올라가지 않음** |
| ck8000 | 0 | O | 동작 중 팍 튐 |
| ck18000 | 0 | O | 팔 **내릴 때** 가끔 빠름 |
| **ck18000** | **2** | **O** | **부드러움 — 채택** |

`--chunk-blend-frames` 는 코드에 "하드웨어 미검증"으로 적혀 있던 옵션이다. 이제 실측이 있다.

- **`1` 은 무효다** — `alpha = 1/1 = 1.0` 이라 새 토큰이 그대로 나간다. 의미 있는 최소값은 **`2`**
- **`3` 은 ck2000 에서 과제를 죽였다** — latent 선형 보간은 **자세의 보간이 아니다.**
  chunk 당 10프레임 중 앞 3프레임이 매번 직전 자세로 끌려가면, 여러 chunk 에 걸친 상승 동작이
  누적 상쇄된다
- **`2` 는 ck18000 에서 과제를 유지하며 부드러워졌다**

세 체크포인트가 **모두 과제를 수행**했고 open-loop 관절공간 평가도 2° 안에 붙어 있다
(ck2000 23.18° / ck8000 24.86° / ck18000 23.02°). 실기에서 뚜렷한 우열이 없어
**사전 지표 최저인 ck18000** 으로 정했다.

### 튐과 떨림은 별개 증상이다

| 증상 | 언제 | 원인 |
|---|---|---|
| **튐** | 동작 중, 특히 팔 내릴 때 | chunk 경계의 **판단 전환** |
| **떨림** | 정지 시 | **미상** |

튐의 기전은 **인덱싱 버그가 아니다.** 타이밍은 정확히 맞는다:

```
지연 보상   index = round(0.19s × 25Hz) ≈ 5
chunk N     index 5→15 을 0.4초에 재생  = t_obs+0.2s → t_obs+0.6s
chunk N+1   0.4초 뒤 관측, index 5 ↔ t_obs+0.4+0.2 = t_obs+0.6s
                                                    ─────────────
                                                    시각이 일치
```

즉 **0.4초 간격의 서로 다른 이미지로 돌린 두 forward pass 가 같은 시각에 대해 다른 답을
내는 것**이다. 바나나를 치우면 정책이 "든다"→"쉰다"로 판단을 뒤집고, 그 전환이 경계에 걸리면
최대 크기의 점프가 된다(실측 최대 204°, 일반 프레임은 4.8°). 팔을 **올릴 때** 덜 튀는 것은
상승이 여러 chunk 에 걸친 연속 동작이라 이웃 chunk 끼리 크게 다르지 않기 때문이다.

> **`--rate` 를 낮추는 것은 이 증상에 듣지 않는다.** 경계 수는 줄지만 판단이 뒤집히는 그 한 번의
> 경계는 그대로이고, 바나나 반응이 최대 0.8초까지 늦어진다.

떨림은 blend 로 거의 줄지 않았다 — 즉 **chunk 경계가 주범이 아니다.**

---

## 9. 미해결

1. **blend 경계** — ck18000 에서 `--chunk-blend-frames 3` 을 보면, ck2000 의 실패가
   **개입 강도 때문인지 체크포인트 때문인지** 갈린다
2. **정지 시 떨림** — 후보는 제어루프 50Hz(`control_dt_ = 0.02`) vs 토큰 발행 25Hz 의 계단 입력.
   **미검증.** 발행은 50Hz 로 하되 연속 토큰 사이를 보간해 타이밍과 매끄러움을 동시에 얻는
   방향이 있으나, 현재 코드는 **발행 주기와 토큰 소비 주기가 묶여 있다**
3. **초기 자세 토큰** — `LATENT_INITIAL_MOTION_TOKEN` 이 데모 시작 분포와 cosine 0.579 /
   거리 1.210 (자체 편차 0.482 의 2.3배) 떨어져 있다. 교체 후보는 medoid `episode_000044` 의 frame-0
4. **추론 지연 보상** — 오프라인 평가에 미반영. 실기 `latency` 실측 후 반영 여부 판단
5. **ep36·ep47 추종 실패** — 오프라인에서 37~39°, 원인 미상 ([4번 문서 §8-③](4_evaluation.md#8-v2-결과--읽고-넘어가야-할-세-가지))

---

## (선택) 실기 실행 녹화

실기 실행을 녹화해두면 나중에 open-loop 으로 재분석할 수 있다.

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "raise your right arm if you see a banana" \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered --camera-decode-reduce 1 --dataset-fps 25 \
    2>&1 | tee vla_run_$(date +%Y%m%d_%H%M%S).log
```

head-only 이므로 `--record-wrist-cameras` 를 **넣지 않는다.**
