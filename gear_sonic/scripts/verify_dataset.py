#!/usr/bin/env python3
"""LeRobot v2.1 데이터셋 검증 — 비디오↔parquet 정렬 + VLA 로딩 가능성.

수집한 에피소드가 VLA 파인튜닝(GR00T)에 바로 쓸 수 있는지 자동 확인한다.
검사 항목:
  1) 프레임 수 일치      : parquet 행 == 각 mp4 프레임 == info.total_frames
  2) frame_index 연속성  : 0..N-1
  3) timestamp 시간축    : frame_index/fps 균일 그리드 (배속/어긋남 없음)
  4) fps 3층 일관성      : info.json == mp4 avg_frame_rate == parquet dt
  5) 관절/액션 무결성    : NaN 없음, 길이 일치
  6) LeRobot 실제 로딩   : LeRobotDataset(root=...) → ds[0]에 3카메라+state+action,
                          비디오가 실제 디코드(검정 아님)되는지

사용:
  source .venv_data_collection/bin/activate
  python gear_sonic/scripts/verify_dataset.py outputs/2026-07-14-21-14-36
  python gear_sonic/scripts/verify_dataset.py outputs/<날짜> --no-load   # LeRobot 로딩 스킵(빠름)

종료코드: 모두 통과=0, 하나라도 실패=1.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

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


def verify(root: Path, do_load: bool) -> bool:
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
    args = ap.parse_args()
    root = Path(args.dataset)
    if not (root / "meta" / "info.json").exists():
        print(f"{NO} {root}/meta/info.json 없음 — 데이터셋 경로 확인")
        sys.exit(2)
    sys.exit(0 if verify(root, not args.no_load) else 1)


if __name__ == "__main__":
    main()
