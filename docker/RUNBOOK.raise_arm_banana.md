# RUNBOOK — "바나나 보면 오른팔 들기" 데이터 수집 (head-only, 80 에피소드)

**목표**: Isaac-GR00T 파인튜닝용 LeRobot 데이터셋 80 에피소드 수집
**구성**: head 카메라 1대(D455)만 사용, 손목 D405 미연결
**작성 기준**: 2026-07-29

---

## 0. 시작 전 1분 점검

### 0-1. head 카메라 시리얼 실측 (Orin에서)

머리 유닛은 지금까지 두 번 교체됐다(D435i `346122071399` → D435 `938422073271` → 현재 D455 `046322250434`).
**아래 값을 믿지 말고 매번 실측할 것.**

```bash
ssh unitree@192.168.123.164        # 비밀번호는 별도 전달
cd ~/tw_gearsonic/GR00T-WholeBodyControl
./docker/list_realsense_serials.sh
```

출력의 `librealsense 장치` 항목이 명령어에 넣는 값이다(by-id USB 시리얼 아님).

> 시리얼을 잘못 넣으면 librealsense가 `No device connected` 재시도 루프를 돌며
> USB 버스를 흔든다. 머리가 아예 안 뜨고, 손목이 붙어 있다면 ~3Hz로 떨어진다.
> **카메라 고장으로 오진하기 쉬우니 이 증상이 보이면 시리얼부터 의심할 것.**

### 0-2. 준비물

- 바나나 1~2개
- **디스트랙터 물체 2~3종** (사과, 컵, 인형 등 — 바나나가 아닌 것)
- 물체를 놓을 책상/받침

---

## 1. 터미널 실행 순서

반드시 **1 → 2 → 3 → 4** 순서. 터미널1이 건강해진 걸 확인한 뒤 4를 띄운다.

### 터미널1 — 카메라 서버 (★ 로봇 온보드 Orin에서 실행, DGX 아님)

```bash
ssh unitree@192.168.123.164        # 비밀번호는 별도 전달
cd ~/tw_gearsonic/GR00T-WholeBodyControl

# autosuspend 스크립트는 D405(손목)용 → head-only면 생략 가능 (돌려도 무해)

NO_WRISTS=1 HEAD_SERIAL=046322250434 \
  sudo -E ./docker/run_ltw_camera_server_ros2foxy_v8.sh
```

- `WRIST_BACKEND`은 넣지 않는다 (손목 안 씀).
- 정상이면 `NO_WRISTS=1 → head-only(ego_view)만 발행` 로그가 뜬다.
- **`[sync]` 로그는 안 나온다** — 맞출 손목이 없으니 정상.
- **건강 신호**: `content(new frames): ego_view=29Hz` + `publish fps`.
  이게 연속으로 안정되기 전에는 터미널4를 띄우지 않는다.

### 터미널2 — C++ deploy (DGX)

```bash
cd ~/GR00T-WholeBodyControl/gear_sonic_deploy
source scripts/setup_env.sh
./deploy.sh --input-type zmq_manager real
# → Y → Init done
```

### 터미널3 — PICO 텔레오퍼레이션 (DGX)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_teleop/bin/activate
export CYCLONEDDS_HOME=$HOME/.local/cyclonedds-c
export LD_LIBRARY_PATH=$CYCLONEDDS_HOME/lib:$LD_LIBRARY_PATH
python gear_sonic/scripts/pico_manager_thread_server.py --manager --vis_vr3pt --vis_smpl
```

> `--analog_grip`은 이번 태스크에서 **사용하지 않는다**(그립이 필요 없음).
> 이 플래그는 `script_config`에 기록되지 않아 **병합기가 불일치를 못 잡는다.**
> 8세션 내내 뺀 상태로 고정할 것.

### 터미널4 — exporter (DGX)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate

python gear_sonic/scripts/run_data_exporter.py \
    --task-prompt "raise your right arm if you see a banana" \
    --dataset-name raise_arm_banana_01 \
    --camera-host 192.168.123.164 --camera-port 5555 \
    --use-nvenc --camera-triggered \
    --camera-decode-reduce 1 --dataset-fps 25 \
    2>&1 | tee data_exporter_$(date +%Y%m%d_%H%M%S).log
```

**세션마다 `--dataset-name`의 숫자만** `01` → `08`로 올린다.
**`--task-prompt`는 글자 하나도 바꾸지 않는다** (이유는 6-1 참조).

`--record-wrist-cameras`는 넣지 않는다 (기본 False → `ego_view`만 스키마에 들어감).

### 터미널5 — 카메라 뷰어 (선택, ★ 녹화 중에는 끌 것)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate
python gear_sonic/scripts/run_camera_viewer.py \
    --camera-host 192.168.123.164 --camera-port 5555
```

---

## 2. 에피소드 구성 — 세션당 10개를 이렇게 섞는다

지시문이 80개 전부 동일하므로, **"들어올린다 / 안 들어올린다"를 가르는 신호는 이미지밖에 없다.**
전부 포지티브면 모델은 이미지를 무시하고 항상 팔을 드는 쪽으로 수렴한다.

| 유형 | 장면 | 행동 | 세션당 | 총계 |
|---|---|---|---|---|
| 포지티브 | 바나나 있음 | 오른팔 들어올림 | 6 | **48** |
| 네거티브-디스트랙터 | 바나나 아닌 물체 | 가만히 | 3 | **24** |
| 네거티브-빈 화면 | 아무것도 없음 | 가만히 | 1 | **8** |

**디스트랙터가 왜 필요한가**: 네거티브가 "빈 화면"뿐이면 모델은 *"뭐라도 있으면 든다"* 를 배운다.
바나나를 특정하려면 **다른 물체를 놓고 안 드는** 사례가 있어야 한다.

### ⚠️ 세션 단위로 몰아 찍지 말 것

"1~6세션 포지티브, 7~8세션 네거티브" 식은 **금지**.
세션마다 조명·자동노출·캘리브레이션이 미세하게 달라지는데, 라벨이 세션과 일치하면
그 차이가 라벨과 완벽히 상관되어 모델이 바나나 대신 **화면 톤**을 학습한다.

→ **10개를 세션 안에서 섞고, 순서도 매번 다르게** 한다 (예: `P N P P D P N D P D`).
   규칙적으로 번갈아 가는 것도 패턴이 되니 피할 것.

### 포지티브 6개 = 위치 6칸을 한 번씩

```
          가까이      멀리
좌         P1         P4
중앙       P2         P5
우         P3         P6
```

8세션 후 각 칸에 정확히 8개가 쌓인다.
세션마다 추가로 바꿀 것: **바나나 방향**(가로/세로/비스듬), **조명**, **배경 잡동사니**, **개수**(1~2개).

디스트랙터도 **같은 6칸에 골고루** 놓는다.
포지티브는 항상 가운데 / 네거티브는 항상 구석 → **위치가 곧 라벨**이 되어버린다.

---

## 3. 에피소드 하나 찍는 방법

1. **장면을 먼저 세팅하고 나서 녹화 시작.**
   바나나를 손으로 내려놓는 장면이 찍히면 모델이 바나나가 아니라 **사람 손**을 트리거로 학습한다.
2. **시작 자세는 매번 팔 내린 같은 자세.**
3. **앞에 1~2초 여유**를 두고 시작 → 그다음 팔 올리기.
   시작하자마자 올리면 "보고 나서 든다"는 전이가 데이터에 안 들어간다.
4. **팔 올린 상태를 1~2초 유지**하고 정지.
5. **네거티브도 똑같이 5~10초.** 짧게 대충 넘기면 **길이 자체가 라벨**이 된다.
6. 동작 속도를 완전히 똑같게 맞추려 애쓸 필요 없다. 약간의 변동은 오히려 도움이 된다.
7. 애매하거나 실패한 건 **그 자리에서 폐기**. 애매한 데이터는 없는 것보다 나쁘다.

**목표 길이: 5~10초 = 25fps 기준 125~250프레임**

---

## 4. 버튼 조작

| 조작 | 동작 |
|---|---|
| **left grip + A** (1번째) | 녹화 시작 → `Started recording N` |
| **left grip + A** (2번째) | 녹화 정지 → 자동 저장 → `Finished saving episode` |
| **left grip + B** | 현재 에피소드 폐기 (**녹화 중에만 동작**) |

- **A 2번 = 에피소드 1개.** 세 번째 누를 필요 없다(저장 후 자동으로 IDLE 복귀).
- **exporter를 껐다 켤 필요 없다.** 다음 A가 바로 `episode_000001`을 시작한다.
- ⚠️ **2번째 A 이후 `Finished saving episode`를 확인하고 다음 에피소드를 시작할 것.**
  저장은 동기 호출이라 그동안 메인 루프가 멈추고, **저장 중에 누른 A는 씹힐 수 있다.**
  안 눌린 줄 모르고 동작하면 그 에피소드가 통째로 날아간다. TTS 소리로 확인하면 편하다.
- ⚠️ **B는 녹화 중에만 먹는다.** A를 2번 눌러 저장이 시작된 뒤에는 취소할 수 없다.
  어그러졌다 싶으면 **정지시키기 전에** B를 누른다.

### 매 에피소드 확인할 로그

```
[DataExporter] episode: 200 frames in 8.0s = 25.0 Hz actual save rate
               (dataset fps stamped: 25). speedup vs real = 1.00x
```

- **`speedup vs real`이 1.00 근처면 정상.**
- 0.8x 같은 값이 나오기 시작하면 DGX가 밀리는 중 → 저장 영상이 빨리 재생된다. 세션을 멈추고 점검.
- 프레임 수로 길이 확인: **125~250프레임**이 목표.

---

## 5. 세션 체크리스트

세션 시작:
- [ ] 터미널1에서 `content(new frames): ego_view=29Hz` 안정 확인
- [ ] 터미널5 뷰어 종료 (녹화 중에는 끈다)
- [ ] 터미널4의 `--dataset-name` 숫자 올렸는지 확인
- [ ] `--task-prompt` / 나머지 옵션은 **손대지 않았는지** 확인

세션 중:
- [ ] 포지티브 6 + 디스트랙터 3 + 빈 화면 1, 순서 섞어서
- [ ] 에피소드마다 `speedup vs real ≈ 1.00`, 프레임 수 125~250 확인
- [ ] 실패는 즉시 B로 폐기

세션 종료 후 (Ctrl+C로 exporter 종료 → 같은 터미널):
```bash
python gear_sonic/scripts/verify_dataset.py outputs/raise_arm_banana_01
```
- [ ] `결과: ✅ 전부 통과`, `EXIT=0` 확인 후 다음 세션으로

> 8세션 다 돌고 나서 한꺼번에 검증하지 말 것. 카메라가 죽은 걸 늦게 발견할수록 손해가 크다.

---

## 6. 병합 (8세션 완료 후)

```bash
cd ~/GR00T-WholeBodyControl
source .venv_data_collection/bin/activate

python gear_sonic/scripts/process_dataset.py \
  --dataset-path outputs/raise_arm_banana_0{1,2,3,4,5,6,7,8} \
  --output-path  outputs/raise_arm_banana_merged \
  --no-remove-stale-smpl

python gear_sonic/scripts/verify_dataset.py outputs/raise_arm_banana_merged
```

### 6-1. ⚠️ `--task-prompt`를 8세션 내내 동일하게 유지해야 하는 이유

병합기의 task 중복 제거는 **문자열이 아니라 `task_index` 기준**이다
([process_dataset.py:520-524](process_dataset.py)의 `seen_task_ids`).
8개 세션이 각자 안에서는 전부 `task_index=0`이라, 프롬프트가 조금이라도 다르면
**첫 세션 문자열만 남고 나머지 70개 에피소드가 조용히 그 라벨로 덮어씌워진다.**
에러도 경고도 나지 않는다.

이 문자열은 `modality.json`의 `annotation.human.task_description`을 통해
**GR00T에 들어가는 언어 지시문 그 자체**다.

### 6-2. ⚠️ `--no-remove-stale-smpl`은 선택이 아니라 **필수**

실측(2026-07-29, `outputs/2026-07-29-11-16-52`):

```
teleop.smpl_pose     shape=(3695,63)  all-zero = 3695 (100.0%)
teleop.smpl_joints   shape=(3695,72)  all-zero = 3695 (100.0%)
teleop.stream_mode   = 5 (전 프레임 동일)
teleop.vr_3pt_position  all-zero = 0/3695   ← 실제 텔레옵 신호는 여기
```

VR 3-point 모드(`stream_mode=5`)에서는 전신 SMPL 리타게팅이 돌지 않아
`teleop.smpl_pose`가 **원래 전부 0**이다(고장 아님).
그런데 정제기는 이 컬럼의 all-zero를 "드롭된 프레임"으로 해석한다
(`SMPL_POSE_COLUMN`, `build_stale_mask`).

→ **플래그 없이 돌리면 `Episode 0: removing 3695/3695 frames (100.0%)`,
   즉 전 에피소드가 삭제된다.**

### 6-3. 병합기가 잡아주는 것 vs 조용히 통과시키는 것

| 항목 | 바뀌면 |
|---|---|
| `--dataset-fps 25`, `--camera-decode-reduce 1`, `--record-wrist-cameras` 미사용 | ✅ 병합 시 **하드 에러**로 중단 (`script_config mismatch`) |
| `--task-prompt` 문자열 | ⚠️ **조용히** 첫 세션 라벨로 덮어씀 |
| 터미널3 `--analog_grip`, deploy `--freeze-thumb` | ⚠️ `script_config`에 없어서 **병합이 못 잡음**. 손 관절 분포만 조용히 달라짐 |

---

## 7. ★ 20 에피소드 시점에서 한 번 끊을 것

**2세션(20개)을 모은 시점에 짧은 파인튜닝을 한 번 돌려볼 것.**
성능을 보려는 게 아니라, 데이터가 학습 파이프라인에 **들어가긴 하는지**만 확인하는 리허설이다.

확인 항목:
- [ ] data config가 **단일 뷰**를 받는가
      (이 데이터셋의 `modality.json`은 `video: {ego_view}` 하나뿐. 3-view를 기대하면 로딩에서 깨짐)
- [ ] action 키로 **`action.wbc`** 를 쓰는가
      (`smpl_pose`/`smpl_joints`는 값이 전부 0이라 상수를 학습하게 됨)
- [ ] `episodes_stats.jsonl` 이슈 없는가
      (`process_dataset.py`가 이 파일을 안 남긴다. Isaac-GR00T 로더는 요구하지 않지만
       훈련 머신에서 `grep -rn "episodes_stats" gr00t/data/` 로 30초면 확인 가능)

80개 다 모은 뒤에 config 문제를 발견하면 재수집은 아니어도 시간이 크게 든다.

---

## 8. 자주 겪는 증상

| 증상 | 원인 / 조치 |
|---|---|
| 머리가 안 뜨고 `No device connected` 반복 | HEAD_SERIAL 오지정. `list_realsense_serials.sh`로 재확인 |
| `[sync]` 로그가 안 보임 | head-only에서는 정상. `content(new frames)`를 보라 |
| `speedup vs real`이 1.0보다 낮아짐 | DGX 부하. 뷰어(터미널5) 켜져 있는지 확인 |
| A를 눌렀는데 반응 없음 | 직전 에피소드 저장 중. `Finished saving episode` 대기 |
| verify `[4]`에서 정지 프레임 FAIL | 카메라 스트림 문제. 해당 세션 재수집 |
| 병합 시 `script_config mismatch` | 세션 간 exporter/deploy 옵션이 달랐음. 6-3 표 확인 |

---

## 관련 문서

- `docker/RUNBOOK.3cam_realsense.md` — 3-cam(손목 포함) 구성
- `docker/README.3cam_pipeline.md` — 병목 해결 이력, 롤백 방법
- `docker/run_ltw_camera_server_ros2foxy_v8.sh` — 카메라 서버 헤더 주석에 시리얼 이력
