# Documentation

이 디렉터리에는 두 종류의 문서가 있다.

1. **인수인계 문서** (`1_` ~ `5_`) — 이 포크에서 수행한 **Unitree G1 VLA 파인튜닝 파이프라인**의
   운영 문서. 아래 [인수인계 문서](#인수인계-문서) 절부터 읽는다.
2. **NVIDIA 상류 문서** (`source/`) — Sphinx 문서 사이트 소스. 빌드 방법은 맨 아래
   [Building the NVIDIA docs site](#building-the-nvidia-docs-site) 참조.

---

# 인수인계 문서

**목표**: 사람이 PICO VR 로 시연 → 그 데이터로 GR00T N1.7 을 파인튜닝 → 실제 G1 로봇이
`"raise your right arm if you see a banana"` 를 수행한다.

**대상 독자**: GR00T / Unitree 를 이미 아는 사람. 이 셋업 **고유의 것만** 적었다.

> ### 접속 정보는 자리표시자로 되어 있다
> 공개 저장소라 서버 주소·계정·비밀번호를 문서에서 뺐다. 아래 자리표시자는
> **실제 값으로 바꿔서** 쓴다. 값은 담당자에게 별도로 받는다.
>
> | 자리표시자 | 무엇인가 |
> |---|---|
> | `<GATEWAY_IP>` | KIST 게이트웨이 (SSH 포트 4648) |
> | `<GPU_SERVER_IP>` | 그 뒤 사설망의 GPU 서버 (RTX 5090 × 4) |
> | `<NFS_IP>` | `/home` 을 제공하는 NFS 마스터 |
> | `<USER>` | GPU 서버 계정명. 문서의 `/home/<USER>/...` 경로에도 쓰인다 |
> | `# 비밀번호는 별도 전달` | 로봇 온보드(`unitree@192.168.123.164`) 비밀번호 |
>
> `~/.ssh/config` 에 `kist-gw` / `kist-5090` 별칭을 등록해두면 문서의 명령을
> 그대로 쓸 수 있다. 등록 방법은
> [3_finetune_groot_n17.md §0-1](3_finetune_groot_n17.md#0-1-접속--2단-점프) 참조.
>
> 로봇 IP `192.168.123.164` 는 Unitree G1 의 **공개된 기본값**이라 그대로 두었다.

## 시스템 구성

```
   [KIST GPU 서버]              [DGX Spark]                 [Unitree G1]
   cluster101                   워크스테이션                 로봇 + Orin NX
   RTX 5090 × 4                 (offboard)                  192.168.123.164
        │                            │                            │
        │  학습 / PolicyServer       │  수집·전처리·평가·중계      │  카메라 / 구동
        └────── SSH 터널 ────────────┴──────── ZMQ / DDS ─────────┘
```

## 순서대로 읽는다

| # | 문서 | 무엇을 하는가 | 어디서 |
|---|---|---|---|
| 1 | [1_data_collection.md](1_data_collection.md) | PICO VR 텔레오퍼레이션으로 LeRobot v2.1 데이터셋 수집 | Orin + DGX |
| 2 | [2_dataset_preprocess_merge.md](2_dataset_preprocess_merge.md) | 정제 → 병합 → 검증 → train/val 분할 | DGX |
| 3 | [3_finetune_groot_n17.md](3_finetune_groot_n17.md) | GR00T N1.7-3B 파인튜닝 | GPU 서버 |
| 4 | [4_evaluation.md](4_evaluation.md) | 관절공간 open-loop 평가 + **3곡선 그래프**, 체크포인트 선정 | GPU 서버 + DGX |
| 5 | [5_deploy.md](5_deploy.md) | 실기 구동 (터미널 6개) | 전부 |

## 급할 때

| 상황 | 바로 갈 곳 |
|---|---|
| **로봇을 지금 돌려야 한다** | [5_deploy.md](5_deploy.md) — 확정 설정은 `ck18000` + `--chunk-blend-frames 2` + `--action-publish-rate 25` |
| 데이터를 새로 찍는다 | [1_data_collection.md](1_data_collection.md) |
| 모델이 이상하게 움직인다 | [5_deploy.md §8 이상 징후](5_deploy.md#8-이상-징후) |
| 어느 체크포인트를 쓸지 모르겠다 | [4_evaluation.md §8](4_evaluation.md#8-v2-결과--읽고-넘어가야-할-세-가지) — **val/loss 로 고르면 안 된다** |
| 그래프를 다시 뽑아야 한다 | [4_evaluation.md §5](4_evaluation.md#5-step-4--3곡선-플롯-) — 먼저 [§1-1 onnxenv](4_evaluation.md#1-1-로컬-venv-onnxenv) 재생성 |
| 무엇이 검증됐고 무엇이 안 됐나 | [5_deploy.md §9 알려진 한계](5_deploy.md#9-알려진-한계--여기까지가-검증된-범위다) |
| **다른 머신에 올린다 / 백업한다** | [§백업](#백업--다른-머신에-올릴-때) — 레포는 **두 개**이고, 꼭 지켜야 할 것은 데이터셋 169MB 다 |

## 인계받는 사람이 먼저 알아야 할 일곱 가지

1. **`--action-publish-rate 25`** — 기본값 50 이면 동작이 **2배 속도로 재생**된다.
   모델이 25fps 데이터로 학습됐고 `delta_indices` 가 연속 프레임이라 리샘플링이 없다.
2. **`source ~/groot_env.sh`** — 서버에서 이걸 빼면 gated VLM 백본을 받으러 가다 **401 로 죽는다.**
   `--model-path` 가 로컬이어도 백본만은 항상 HF 를 탄다.
3. **`--port 5550`** — PolicyServer 기본 포트는 5555 로 **카메라 서버와 같다.**
   빠뜨리면 클라이언트가 **에러 없이 조용히** 못 붙는다.
4. **val/loss 로 체크포인트를 고르면 안 된다** — 관절공간 성능과 상관이 **−0.605 로 방향이 반대**다.
   [4번 문서](4_evaluation.md)의 관절공간 평가로 고른다.
5. **체크포인트를 바꿀 때는 VLA 추론 터미널도 재시작한다** — 안 하면 이전 세션 토큰이 남아
   초기 자세가 달라진다.
6. **학습 데이터는 전부 head-only 다** — `ego_view` 1대뿐이고 손목 카메라가 들어간 적이 없다.
   3-cam 절차는 손목을 다시 붙일 때 쓰는 것이고, 그때는 **모델을 처음부터 다시 학습해야 한다.**
7. **Isaac-GR00T 는 이 포크(`foodbanana/Isaac-GR00T`)를 쓴다** — NVIDIA 원본에는
   `PolicyClient` 소켓 누수 수정(`a9c944a`)이 없다. 없으면 **PolicyServer 가 죽었을 때
   추론 프로세스를 `Ctrl+C` 로 죽일 수 없다**(로봇이 마지막 자세로 굳은 채로).
   [5_deploy.md §1](5_deploy.md#-isaac-gr00t-에-소켓-수정이-들어-있어야-한다)

## 현재 상태 (2026-08-07)

**파이프라인 1회전 완료.** 수집 → 병합(79 ep) → 파인튜닝(24000 step) → 관절공간 평가(19 ckpt)
→ 실기 구동까지 끝났고, 실기에서 ck2000·ck8000·ck18000 **셋 다 과제를 수행**했다.
교차조건 테스트에서도 이미지만 바꿨을 때 토큰이 **기대 방향으로 100% 갈렸다**
([4_evaluation.md §7](4_evaluation.md#-v2-결과--돌았고-v1-의-실패가-뒤집혔다)) — v1 에서
방향이 반대로 나왔던 문제가 해결됐다.

검증된 범위의 경계는 [5_deploy.md §9](5_deploy.md#9-알려진-한계--여기까지가-검증된-범위다) 에 있다.
로봇은 현재 전원 off 이고, 그 외 실기 자산(바이너리·venv·터널·체크포인트 48개)은 전부 살아 있다.

## 백업 / 다른 머신에 올릴 때

### 레포는 두 개다 — `Isaac-GR00T` 를 빠뜨리기 쉽다

| 레포 | 브랜치 | 없으면 |
|---|---|---|
| `foodbanana/GR00T-WholeBodyControl` | **`3cam-pipeline`** | 수집·평가·추론 스크립트 전부 없음 |
| **`foodbanana/Isaac-GR00T`** | `main` | **`run_vla_inference.py` 가 import 부터 실패** |

`gear_sonic[inference]` 가 `gr00t` 를 **editable 로** 물고 있어, 추론 클라이언트가 쓰는
`gr00t` 는 별도 폴더에서 온다:

```bash
$ .venv_inference/bin/python -c "import gr00t, gear_sonic; print(gr00t.__file__); print(gear_sonic.__file__)"
/home/edgexpert00/Isaac-GR00T/gr00t/__init__.py
/home/edgexpert00/GR00T-WholeBodyControl/gear_sonic/__init__.py
```

```bash
git clone -b 3cam-pipeline https://github.com/foodbanana/GR00T-WholeBodyControl.git
git clone https://github.com/foodbanana/Isaac-GR00T.git
```

**NVIDIA 원본이 아니라 이 포크여야 한다** — 원본에는 `PolicyClient` 소켓 누수 수정
(`a9c944a`)이 없다([5_deploy.md §1](5_deploy.md#-isaac-gr00t-에-소켓-수정이-들어-있어야-한다)).

### 역할별로 필요한 것

| 역할 | WholeBodyControl | Isaac-GR00T | 그 외 |
|---|---|---|---|
| PolicyServer 만 (GPU 서버) | 불필요 | **필요** | 체크포인트 |
| VLA 추론 + C++ deploy (DGX) | 필요 | **필요** | ONNX 다운로드 + C++ 빌드 + venv |
| 카메라 서버 (로봇 온보드) | 필요 | 불필요 | `.venv_camera` |
| **코드 백업만** | 필요 | 필요 | 아래 "git 밖" 표 참조 |

### venv 는 복사하면 안 된다 — 새로 만든다

`.pth` 에 **절대경로가 박혀 있어** 사용자명·경로가 다르면 깨진다. `.venv_inference` 만
**13GB** 다.

```bash
$ cat .venv_inference/lib/python3.12/site-packages/__editable__.gear_sonic-0.1.0.pth
/home/edgexpert00/GR00T-WholeBodyControl
```

```bash
bash install_scripts/install_inference.sh
```

> ⚠️ 이 스크립트는 **멱등하지 않다.** `rm -rf .venv_inference` 로 시작하므로
> **이미 venv 가 있는 머신에서는 절대 돌리지 말 것.**

### 클론해도 안 따라오는 것 — 실기 전에 채워야 한다

```bash
★ 없음   gear_sonic_deploy/policy/release/model_decoder.onnx     SONIC 디코더
★ 없음   gear_sonic_deploy/policy/release/model_encoder.onnx
★ 없음   gear_sonic_deploy/planner/target_vel/V2/planner_sonic.onnx
★ 없음   gear_sonic_deploy/target/release/g1_deploy_onnx_ref     C++ 바이너리
```

`observation_config.yaml` 은 추적되는데 **짝이 되는 ONNX 만 없다** — 설정만 있고 모델이
없는 상태가 된다. 둘 다 복구 가능하다:

```bash
python download_from_hf.py            # policy/release/*.onnx + planner (HF stock)
cd gear_sonic_deploy && just build    # C++ 바이너리
```

문서 이미지 40장은 **전부 git 에 있다.**

### 백업 서버라면 — git 밖에 있는 것만 옮기면 된다

코드는 GitHub 에 있으므로 **파일로 옮길 것은 "다시 만들 수 없는 것" 뿐이다.**

| 대상 | 크기 | 복구 가능? | 백업 |
|---|---|---|---|
| **`outputs/raise_arm_banana_merged_v2`** | **169MB** | ❌ **불가** — 실기 텔레오퍼레이션 녹화다 | **필수** |
| `outputs/` 전체 (원본 세션 포함) | 1.8GB | ❌ 불가 | 권장 |
| 서버 체크포인트 48개 | 6.5GB/개 | △ 재학습 11시간 × GPU 4장, **데이터셋이 살아 있을 때만** | 선별 (ck18000) |
| `eval_results/` | 105MB (추적 9 / 전체 217) | ⭕ 체크포인트+데이터셋에서 재생성 | 선택 |
| `policy/release/*.onnx` | 174MB | ⭕ `download_from_hf.py` | 불필요 |
| C++ 바이너리 | 5.6MB | ⭕ `just build` | 불필요 |
| `.venv_*` | 19GB | ⭕ install 스크립트 | **하지 말 것** |

**가장 중요한 것은 데이터셋 169MB 다.** 로봇 앞에서 사람이 VR 로 79 에피소드를 다시 찍는
것 말고는 복구 방법이 없다. 체크포인트가 날아가도 데이터셋만 있으면 다시 학습하면 되지만,
반대는 성립하지 않는다.

```bash
# 최소 백업 — 이것만은 반드시
rsync -ahP outputs/raise_arm_banana_merged_v2 <백업서버>:~/backup/

# 확정 체크포인트 한 개 (추론에 필요한 6.5GB 만; 옵티마이저 상태 27GB 는 제외)
rsync -ahP --include='config.json' --include='embodiment_id.json' \
  --include='experiment_cfg/***' --include='model-*.safetensors' \
  --include='model.safetensors.index.json' --include='processor_config.json' \
  --include='statistics.json' --exclude='*' \
  kist-5090:/home/<USER>/groot_output/rab-v2b-20260806/checkpoint-18000/ \
  <백업서버>:~/backup/rab-v2b-ck18000/
```

> `statistics.json` 과 `experiment_cfg/` 는 정규화 통계·모달리티 설정이라 **빠지면 서버가
> 뜨긴 하지만 출력이 망가진다.** 위 목록을 줄이지 말 것.

## 저장소 구조가 두 브랜치로 갈려 있다

| 브랜치 | 들어 있는 것 |
|---|---|
| **`3cam-pipeline`** (이 브랜치) | 인수인계 문서 5개, 평가/추론 스크립트(`wbc_decoder.py`, `eval_decoded_*.py`, `plot_*.py`, `split_dataset.py`, `dump_open_loop_predictions.py`, `cross_condition_test.py`, `smoke_test_policy_server.py`, `keyboard_publisher.py`) |
| `3cam-data-collection` | 데이터 수집 원본 문서 `data_collection.md` (635줄) |

수집 문서 원본을 보려면:

```bash
git show origin/3cam-data-collection:data_collection.md
```

핵심 내용은 [1_data_collection.md](1_data_collection.md) 에 옮겨 담았으므로 보통은 그것만으로 충분하다.

## 배경 문서 (원본)

인수인계 문서 5개는 아래 문서들을 정리한 것이다. **더 깊은 근거가 필요할 때** 본다.

| 문서 | 내용 |
|---|---|
| [v2_validation_and_deploy_runbook.md](v2_validation_and_deploy_runbook.md) | v2 검증 STEP 1~8 전체와 판단 근거 (696줄) |
| [vla_run_procedure.md](vla_run_procedure.md) | 2026-08-07 첫 실기 구동 절차·실측 (300줄) |
| [open_loop_eval_decoded_joint.md](open_loop_eval_decoded_joint.md) | 관절공간 평가 방법론·규격 (259줄) |
| [server_finetune_runbook.md](server_finetune_runbook.md) | 서버 파인튜닝 원본 런북 (481줄) |
| [server_finetune_checklist.md](server_finetune_checklist.md) | 파인튜닝 체크리스트 (413줄) |
| [../eval_results/](../eval_results/) | 평가 결과 README + `metrics.csv` (**그림은 gitignore 대상**) |

> ⚠️ 배경 문서에는 **현재 코드와 어긋나는 서술**이 남아 있다. 인수인계 문서 쪽이 최신이며,
> 어긋나는 지점은 인수인계 문서 안에 📌 로 표시해 두었다.

---

# Building the NVIDIA docs site

아래는 상류 NVIDIA 문서(`source/`)에 대한 원문이다.

This directory contains the source code for the GR00T-WholeBodyControl documentation website.

## Building Locally

### Prerequisites

Install the required Python packages:

```bash
pip install sphinx sphinx-book-theme sphinx-design sphinxemoji \
            autodocsumm sphinxcontrib-bibtex myst-parser \
            sphinx-copybutton
```

### Build the Documentation

```bash
cd docs
make html
```

The built documentation will be in `build/html/`. Open `build/html/index.html` in your browser.

### Live Preview

Start a local web server to preview:

```bash
cd build/html
python -m http.server 8000
```

Then open http://localhost:8000

### Clean Build

To remove all built files and rebuild from scratch:

```bash
make clean
make html
```

## Deployment

The documentation is automatically built and deployed to GitHub Pages when changes are pushed to the `main` branch via the GitHub Actions workflow at `.github/workflows/docs.yml`.

The live documentation will be available at:
**https://nvlabs.github.io/GR00T-WholeBodyControl/**

## Documentation Structure

- `source/` - All documentation source files
  - `conf.py` - Sphinx configuration
  - `index.rst` - Main landing page
  - `_static/` - Static assets (CSS, images, logos)
  - `tutorials/` - Tutorial pages
  - `getting_started/` - Getting started guides
  - `user_guide/` - User guide
  - `api/` - API reference
  - `resources/` - Additional resources

## Writing Documentation

- Use Markdown (`.md`) or reStructuredText (`.rst`) files
- Markdown is recommended for simplicity
- Place new files in the appropriate subdirectory
- Update `index.rst` to add new sections to the navigation

## Theme

The documentation uses the `sphinx_book_theme` with NVIDIA branding, matching the Isaac Lab documentation style.
