#!/usr/bin/env python3
"""LeRobot v2.1 데이터셋 검증 — 비디오↔parquet 정렬 + VLA 로딩 가능성.

수집한 에피소드가 VLA 파인튜닝(GR00T)에 바로 쓸 수 있는지 자동 확인한다.
검사 항목:
  1) 프레임 수 일치      : parquet 행 == 각 mp4 프레임 == info.total_frames
  2) frame_index 연속성  : 0..N-1
  3) timestamp 시간축    : frame_index/fps 균일 그리드 (배속/어긋남 없음)
  4) 정지/중복 프레임    : 프레임 간 변화량으로 D405 wedge(스트림 정지)를 검출.
                          [6]의 픽셀평균은 "얼어붙은 화면"도 정상으로 통과시키므로,
                          오염된 에피소드를 걸러내려면 이 검사가 필요하다.
  5) 관절/액션 무결성    : NaN 없음, 길이 일치
  6) LeRobot 실제 로딩   : LeRobotDataset(root=...) → ds[0]에 3카메라+state+action,
                          비디오가 실제 디코드(검정 아님)되는지.
                          단 process_dataset.py 정제본은 meta/episodes_stats.jsonl이 없어
                          native 로더가 Hub로 폴백(404)하는데, 실제 소비자 Isaac-GR00T는
                          이 파일이 불필요하므로 native 로딩을 스킵하고 VLA-facing 키만 확인한다.

사용:
  source .venv_data_collection/bin/activate
  python gear_sonic/scripts/verify_dataset.py outputs/2026-07-14-21-14-36
  python gear_sonic/scripts/verify_dataset.py outputs/<날짜> --no-load     # LeRobot 로딩 스킵(빠름)
  python gear_sonic/scripts/verify_dataset.py outputs/<날짜> --no-freeze   # 정지 프레임 검사 스킵(긴 에피소드에서 느림)

종료코드: 모두 통과=0, 하나라도 실패=1.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

OK = "\033[92m✅\033[0m"
NO = "\033[91m❌\033[0m"
WARN = "\033[93m⚠️\033[0m"


def _p(mark, msg):
    print(f"  {mark} {msg}")


def ffprobe_video(path: Path):
    """mp4의 (nb_frames, fps, w, h, duration)를 반환."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height,duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    ).stdout.split()
    # 순서: width height avg_frame_rate duration nb_read_frames (ffprobe 정렬)
    # 안전하게 key=value로 다시 조회
    out2 = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height,duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
    ).stdout
    d = {}
    for line in out2.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    fr = d.get("avg_frame_rate", "0/1")
    num, den = (fr.split("/") + ["1"])[:2]
    fps = float(num) / float(den) if float(den) else 0.0
    return {
        "nb_frames": int(d.get("nb_read_frames", 0)),
        "fps": fps,
        "w": int(d.get("width", 0)),
        "h": int(d.get("height", 0)),
        "duration": float(d.get("duration", 0.0)),
    }


# 프레임 간 grayscale 평균절대차가 이 값 미만이면 "직전과 동일한 화면"으로 본다.
# 실측 분포는 0.0~0.1(완전 중복)과 0.5+(실제 움직임)로 뚜렷이 갈려 임계값에 둔감하다.
_DUP_THRESH = 0.5
# 이 시간(초) 이상 연속으로 화면이 멈춰 있으면 wedge로 판정해 FAIL.
_WEDGE_SEC = 1.0


def frame_diffs(path: Path) -> np.ndarray:
    """mp4를 디코드해 프레임 간 grayscale 평균절대차 배열을 반환."""
    cap = cv2.VideoCapture(str(path))
    diffs, prev = [], None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(gray - prev).mean()))
        prev = gray
    cap.release()
    return np.asarray(diffs)


def longest_run(mask: np.ndarray) -> int:
    """mask에서 True가 연속으로 이어지는 최대 길이."""
    run = best = 0
    for x in mask:
        run = run + 1 if x else 0
        best = max(best, run)
    return best


def check_frozen(mp4s, fps: float) -> bool:
    """카메라별 정지/중복 프레임 검사.

    두 가지를 구분한다:
      - 긴 연속 정지  → wedge. 그 구간 데이터는 못 씀 → FAIL
      - 흩어진 중복    → 소스 프레임률이 dataset fps보다 낮을 뿐 → WARN (사용 가능)
        (예: 머리 videohub가 ~14Hz인데 20fps로 저장하면 약 28%가 중복)
    """
    ok_all = True
    for f in mp4s:
        cam = f.parent.name.replace("observation.images.", "")
        d = frame_diffs(f)
        if len(d) == 0:
            _p(NO, f"{cam}: 프레임을 읽지 못함")
            ok_all = False
            continue
        dup = d < _DUP_THRESH
        run = longest_run(dup)
        run_sec = run / fps
        uniq_hz = fps * (1.0 - dup.mean())

        if run_sec >= _WEDGE_SEC:
            _p(NO, f"{cam}: 정지 {100*dup.mean():.1f}% — 최장 {run}프레임({run_sec:.2f}s) 연속 정지 → wedge 의심!")
            ok_all = False
        elif dup.mean() > 0.05:
            _p(WARN, f"{cam}: 중복 {100*dup.mean():.1f}% (최장 {run}프레임 연속) — "
                     f"소스 신규프레임률 ≈ {uniq_hz:.1f}Hz < {fps:g}fps. wedge 아님, 사용 가능")
        else:
            _p(OK, f"{cam}: 정지 프레임 {dup.sum()}/{len(d)} ({100*dup.mean():.1f}%) — 스트림 정상")
    return ok_all


def verify(root: Path, do_load: bool, do_freeze: bool) -> bool:
    ok_all = True
    print(f"\n=== 데이터셋 검증: {root} ===")

    # --- info.json ---
    info = json.load(open(root / "meta" / "info.json"))
    fps = info["fps"]
    total = info["total_frames"]
    video_keys = [k for k, v in info["features"].items() if v.get("dtype") == "video"]
    print(f"[info.json] fps={fps} total_frames={total} video_keys={[k.split('.')[-1] for k in video_keys]} "
          f"codebase={info.get('codebase_version')}")

    # --- mp4들 ---
    print("\n[1] 프레임 수 일치 & fps")
    mp4s = sorted((root / "videos").rglob("*.mp4"))
    frame_counts = {}
    for f in mp4s:
        cam = f.parent.name.replace("observation.images.", "")
        v = ffprobe_video(f)
        frame_counts[cam] = v["nb_frames"]
        fps_ok = abs(v["fps"] - fps) < 1e-3
        _p(OK if fps_ok else NO,
           f"{cam}: {v['nb_frames']}프레임 {v['w']}x{v['h']} {v['fps']:.3f}fps {v['duration']:.2f}s")
        ok_all &= fps_ok

    # --- parquet ---
    import pyarrow.parquet as pq
    pfile = next((root / "data").rglob("*.parquet"))
    df = pq.read_table(pfile).to_pandas()
    n = len(df)

    # 프레임 수 교차검증
    counts = {"parquet": n, "info.total_frames": total, **frame_counts}
    all_equal = len(set(counts.values())) == 1
    _p(OK if all_equal else NO, f"프레임 수 교차검증: {counts} → {'모두 일치' if all_equal else '불일치!'}")
    ok_all &= all_equal

    # --- frame_index 연속성 ---
    print("\n[2] frame_index 연속성")
    fi = np.asarray(df["frame_index"].tolist()).ravel()
    cont = np.array_equal(fi, np.arange(n))
    _p(OK if cont else NO, f"0..{n-1} 연속: {cont} (min={fi.min()}, max={fi.max()})")
    ok_all &= cont

    # --- timestamp 균일 그리드 ---
    print("\n[3] timestamp 시간축 (균일 그리드)")
    ts = np.asarray(df["timestamp"].tolist()).astype(float).ravel()
    dt = np.diff(ts)
    uniform = np.allclose(dt, 1.0 / fps, atol=1e-4)
    _p(OK if uniform else NO,
       f"dt 평균={dt.mean():.6f}s 기대={1/fps:.6f}s std={dt.std():.2e} → 균일: {uniform}")
    _p(OK, f"처음 5개 ts: {np.round(ts[:5], 5).tolist()}")
    ok_all &= uniform

    # --- 정지/중복 프레임 (wedge 검출) ---
    if do_freeze:
        print("\n[4] 정지/중복 프레임 (D405 wedge 검출)")
        ok_all &= check_frozen(mp4s, fps)
    else:
        print("\n[4] 정지 프레임 검사 스킵 (--no-freeze)")

    # --- 관절/액션 무결성 ---
    print("\n[5] 관절/액션 무결성 (NaN)")
    for col in ("observation.state", "action.wbc", "observation.eef_state"):
        if col in df.columns:
            arr = np.stack(df[col].tolist())
            has_nan = bool(np.isnan(arr).any())
            _p(NO if has_nan else OK,
               f"{col}: shape={arr.shape} NaN={'있음!' if has_nan else '없음'} "
               f"범위=[{arr.min():.3f},{arr.max():.3f}]")
            ok_all &= not has_nan

    # --- LeRobot 실제 로딩 ---
    if do_load:
        print("\n[6] LeRobot 실제 로딩 (VLA 소비 가능성)")
        stats_path = root / "meta" / "episodes_stats.jsonl"
        if not stats_path.exists():
            # process_dataset.py로 정제한 데이터셋은 meta/episodes_stats.jsonl을 남기지 않는다.
            # native LeRobotDataset(v2.1)은 이 파일이 없으면 로컬 로딩에 실패하고 HF Hub로 폴백해
            # 엉뚱한 404(RepositoryNotFoundError)를 던진다. 하지만 실제 파인튜닝 소비자인
            # Isaac-GR00T 로더는 이 파일을 요구하지 않는다 — NVIDIA 공식 경로가
            # collect → process_dataset.py(→ 이 파일 제거) → launch_finetune 이기 때문.
            # 즉 native 로딩 실패는 '잘못된 로더' 경보이므로 실패로 치지 않고,
            # VLA가 실제로 쓰는 키(3카메라 비디오 + state + action)의 로컬 존재만 확인한다.
            _p(WARN, "meta/episodes_stats.jsonl 없음 → process_dataset.py 정제본. "
                     "native LeRobotDataset 로딩 스킵 (Isaac-GR00T 파인튜닝엔 불필요).")
            cam_ok = all(any(c in k for k in video_keys)
                         for c in ("ego_view", "left_wrist", "right_wrist"))
            cols_ok = all(c in df.columns for c in ("observation.state", "action.wbc"))
            _p(OK if (cam_ok and cols_ok) else NO,
               f"VLA-facing 키 로컬 확인: 3카메라={cam_ok} state/action={cols_ok}")
            ok_all &= cam_ok and cols_ok
        else:
            try:
                from lerobot.common.datasets.lerobot_dataset import LeRobotDataset
                ds = LeRobotDataset(repo_id="verify/tmp", root=str(root))
                it = ds[0]
                _p(OK, f"LeRobotDataset 로딩 성공: {len(ds)}프레임 {ds.num_episodes}에피소드")
                needed = [f"observation.images.{c}" for c in ("ego_view", "left_wrist", "right_wrist")]
                needed += ["observation.state", "action.wbc"]
                missing = [k for k in needed if k not in it]
                if missing:
                    _p(NO, f"샘플에 누락 키: {missing}")
                    ok_all = False
                else:
                    _p(OK, "ds[0]에 3카메라 + state + action 모두 존재")
                # 비디오 디코드 확인(검정 아님)
                for c in ("ego_view", "left_wrist", "right_wrist"):
                    k = f"observation.images.{c}"
                    if k in it:
                        v = it[k]
                        mean = float(v.float().mean())
                        black = mean < 0.01
                        _p(WARN if black else OK,
                           f"{c}: shape={tuple(v.shape)} 픽셀평균={mean:.3f}"
                           f"{' (거의 검정?!)' if black else ''}")
                        ok_all &= not black
            except Exception as e:
                import traceback
                traceback.print_exc()
                _p(NO, f"LeRobot 로딩 실패: {type(e).__name__}: {e}")
                ok_all = False
    else:
        print("\n[6] LeRobot 로딩 스킵 (--no-load)")

    # --- 종합 ---
    print("\n" + "=" * 50)
    print(f"  결과: {OK + ' 전부 통과 — VLA 파인튜닝 사용 가능' if ok_all else NO + ' 일부 실패 — 위 항목 확인'}")
    print("=" * 50)
    return ok_all


def main():
    ap = argparse.ArgumentParser(description="LeRobot 데이터셋 정렬/로딩 검증")
    ap.add_argument("dataset", help="데이터셋 폴더 (outputs/<날짜>)")
    ap.add_argument("--no-load", action="store_true", help="LeRobot 실제 로딩 검사 스킵(빠름)")
    ap.add_argument("--no-freeze", action="store_true", help="정지 프레임 검사 스킵(긴 에피소드에서 느림)")
    args = ap.parse_args()
    root = Path(args.dataset)
    if not (root / "meta" / "info.json").exists():
        print(f"{NO} {root}/meta/info.json 없음 — 데이터셋 경로 확인")
        sys.exit(2)
    sys.exit(0 if verify(root, not args.no_load, not args.no_freeze) else 1)


if __name__ == "__main__":
    main()
