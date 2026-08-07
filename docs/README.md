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

## 인계받는 사람이 먼저 알아야 할 다섯 가지

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

## 현재 상태 (2026-08-07)

**파이프라인 1회전 완료.** 수집 → 병합(79 ep) → 파인튜닝(24000 step) → 관절공간 평가(19 ckpt)
→ 실기 구동까지 끝났고, 실기에서 ck2000·ck8000·ck18000 **셋 다 과제를 수행**했다.
교차조건 테스트에서도 이미지만 바꿨을 때 토큰이 **기대 방향으로 100% 갈렸다**
([4_evaluation.md §7](4_evaluation.md#-v2-결과--돌았고-v1-의-실패가-뒤집혔다)) — v1 에서
방향이 반대로 나왔던 문제가 해결됐다.

검증된 범위의 경계는 [5_deploy.md §9](5_deploy.md#9-알려진-한계--여기까지가-검증된-범위다) 에 있다.
로봇은 현재 전원 off 이고, 그 외 실기 자산(바이너리·venv·터널·체크포인트 48개)은 전부 살아 있다.

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
