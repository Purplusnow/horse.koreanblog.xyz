"""데모용 과거 예측 시딩 (개발/CI 전용).

⚠️ **운영 DB 에 절대 실행하지 마십시오.** 공개 예측은 경주 전에 생성되어야만
적중률이 의미를 갖습니다. 이 스크립트는 사이트의 적중률·지난결과 페이지가 제대로
렌더링되는지 확인하기 위한 합성 DB 전용 도구입니다.

부풀린 숫자를 넣지 않기 위해, 여기서 쓰는 예측값은 워크포워드 검증에서 나온
**out-of-sample 예측**이다. 즉 각 예측은 그 경주 이전 데이터로만 학습한 모델이
낸 값이라, 실제 운영에서 기대할 수 있는 수준과 같은 성격을 갖는다.

    python tools/seed_demo_predictions.py --db data/synth.sqlite
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from horseai.features import build_training_frame  # noqa: E402
from horseai.kra.store import session, upsert  # noqa: E402
from horseai.model import walk_forward
from horseai.predict import build_rows  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/synth.sqlite")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--min-train-races", type=int, default=1000)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if "synth" not in Path(args.db).name and "demo" not in Path(args.db).name:
        print(f"✗ 안전장치: 파일명에 'synth' 또는 'demo' 가 없는 DB({args.db})에는 "
              f"실행하지 않습니다.", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        df = build_training_frame(conn)
    finally:
        conn.close()
    if df.empty:
        print("학습 데이터가 없습니다.", file=sys.stderr)
        return 1

    _, preds = walk_forward(df, n_folds=args.folds, min_train_races=args.min_train_races)
    if preds.empty:
        print("워크포워드 예측을 생성하지 못했습니다.", file=sys.stderr)
        return 1

    # 예측 저장 형식은 운영 경로와 동일해야 화면이 어긋나지 않는다
    rows = build_rows(preds)

    with session(args.db) as conn:
        n = upsert(conn, "predictions", rows, ["race_key", "hr_no", "model_version"])

    print(f"데모 예측 {n:,}건 시딩 완료 ({preds['race_key'].nunique():,}경주, out-of-sample)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
