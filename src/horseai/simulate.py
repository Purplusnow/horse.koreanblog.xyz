"""경주 시뮬레이션 — 과거 기록으로 미래 경주의 전개를 재현한다.

지금까지의 모델은 말을 한 마리씩 독립적으로 채점한 뒤 확률을 정규화했다.
그러나 실제 경주는 **서로 영향을 주고받는 하나의 사건**이다. 선행마가 몰리면
초반이 과열되고, 과열된 앞은 막판에 무너지며, 그 틈을 추입마가 파고든다.
독립 채점 방식은 이 상호작용을 구조적으로 담을 수 없다.

이 모듈은 경주를 200m 구간으로 쪼개 **실제로 달려 보는** 방식을 쓴다.

  1. 각 마필의 과거 구간기록에서 능력 프로파일을 뽑는다
     — 초반 속도, 지구력, 막판 600m 가속, 최후 200m 뒷심, 기복
  2. 각질에 따라 에너지를 구간에 배분한다 (선행마는 앞, 추입마는 뒤)
  3. 매 구간 선두권 경쟁의 대가를 물린다 (앞에서 다투면 뒤가 무너진다)
  4. 당일 컨디션과 구간 편차를 난수로 흔들어 수백 번 반복한다

반복 결과의 착순 분포가 곧 승률이고, 그 중 중앙값에 가까운 한 판이
**'미리 보는 경주'**의 대본이 된다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import math

import numpy as np
import pandas as pd

SEG_M = 200.0          # 구간 길이(m). 구간기록이 200m(1F) 단위로 제공된다.
N_SIMS = 2000          # 반복 횟수. 12두 경주에서 승률 표준오차가 약 1%p 수준.

# 각질별 에너지 배분 곡선의 기울기.
# 양수면 초반에 힘을 쓰고 후반에 떨어진다(선행), 음수면 그 반대(추입).
# ── 주로 기하 ──────────────────────────────────────────────────────
# 코너에서 바깥으로 돌면 실제로 더 뛴다. 반경 r 만큼 밖으로 돌면 코너 하나(90°)당
# r×π/2 만큼 거리가 늘어난다. 데이터에도 이 효과가 그대로 보인다 —
# 서울·부경 10~12두 경주에서 마번 1~4 승률 10.3% vs 6~12번 8.3%.
#
# 코너 수는 추측하지 않고 **코너 통과기록이 몇 개 찍히는지**로 정한다
# (corner_counts). 서울 1200m 은 2코너, 1700m 은 4코너다.
LANE_WIDTH = 1.6              # 한 두 폭(m). 바깥 레인 하나당 이만큼 더 돈다.

# 경마장별 주로 제원. 서울 내주로는 일주 1,600m 에 직선 450m·곡선 350m 다
# (한국마사회 경주로 구조). 화면에 타원을 그리고 '어디가 코너인지'를 맞추는 데
# 쓴다. 세 곳 모두 반시계 방향으로 시행한다.
# straight = **결승 직선주로**(마지막 코너를 돈 뒤 결승선까지). 결승선은 고정이고
# 거리에 따라 출발점이 뒤로 물러난다.
#
# 이 값은 추정이 아니라 데이터에서 역산했다. 결승선에서 거꾸로 D 만큼 거슬러
# 올라갈 때 지나는 코너 수는 직선주로 길이에 따라 달라지므로, 경주성적의 코너
# 통과기록(실제 코너 수)과 가장 잘 맞는 값을 찾았다.
#
#   서울      500m → 9/9 거리에서 코너 수 일치
#   부산경남  500m → 6/7   (1800m 만 어긋난다 — 별도 출발로로 보인다)
#   제주      410m → 9/10  (1400m 만 어긋난다)
# lap 은 직선·곡선에서 계산한다 — 따로 적으면 반드시 어긋난다.
#
# **이 값들은 실측 제원이 아니라 추정값이다.** 코너 수는 맞지만, 같은 코너 수를
# 내는 조합은 무수히 많으므로 직선·곡선 각각의 길이가 실제와 같다는 보장이
# 없다. 그 결과 한 바퀴가 서울 1700m 로 떨어져 1700m 경주는 출발점과 결승선이
# 겹쳐 보인다 — 화면상의 출발 위치는 신뢰할 수 없다는 뜻이다.
#
# 코너를 몇 개 도는가(예측·시뮬레이션에 쓰는 값)는 자료와 맞으므로 그대로 쓴다.
# 어디서 출발하는가(화면 표시)만 부정확하다. 실측 제원을 확보하면 여기만
# 바꾸면 되고, 그때 거리별 코너 수가 여전히 맞는지 다시 확인해야 한다.
TRACK_SPEC = {
    "서울":     {"straight": 500, "curve": 350},
    "부산경남": {"straight": 500, "curve": 350},
    "제주":     {"straight": 410, "curve": 306},   # 곡률반경 97.5m
}
for _v in TRACK_SPEC.values():
    _v["lap"] = _v["straight"] * 2 + _v["curve"] * 2
CORNER_RAD = math.pi / 2      # 코너 하나 = 90°

# 각질에 따라 자리를 잡는 정도. 선행마는 일찍 안쪽을 차지하고, 추입마는 바깥으로
# 돌아 나가는 대가를 치른다. 값은 '레인 수' 단위다.
STYLE_LANE = {"front": -0.6, "stalk": 0.0, "close": 0.7, "unknown": 0.2}

STYLE_SLOPE = {"front": 0.55, "stalk": 0.05, "close": -0.50, "unknown": 0.0}


@dataclass
class Runner:
    """시뮬레이션에 들어가는 한 마리의 능력 프로파일 (전부 과거 기록 기반)."""

    hr_no: str
    chul_no: int
    hr_name: str
    style: str = "unknown"
    base: float = 0.0        # 전체 속도 수준 (조건 보정 z)
    early: float = 0.0       # 초반 200m 속도 z
    late: float = 0.0        # 막판 600m 속도 z
    finish: float = 0.0      # 최후 200m 속도 z
    spread: float = 0.6      # 기복 (당일 편차)
    experience: int = 0      # 과거 출주 수 — 신뢰도 계산에 쓴다


@dataclass
class RaceSim:
    """시뮬레이션 산출물."""

    win_prob: np.ndarray             # 마리별 우승 확률
    place_prob: np.ndarray           # 마리별 3착 이내 확률
    mean_rank: np.ndarray            # 평균 착순
    positions: np.ndarray            # 대표 시나리오의 구간별 누적거리 (마리 × 구간)
    seg_times: np.ndarray            # 대표 시나리오의 구간 소요시간
    runners: List[Runner] = field(default_factory=list)
    n_sims: int = 0


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if np.isnan(x) else x


def build_runners(rows: Sequence[dict]) -> List[Runner]:
    """예측 프레임의 행들을 시뮬레이션 입력으로 변환."""
    out = []
    for r in rows:
        out.append(Runner(
            hr_no=str(r.get("hr_no")),
            chul_no=int(_f(r.get("chul_no"), 0)),
            hr_name=str(r.get("hr_name") or ""),
            style=str(r.get("style_code") or "unknown"),
            base=_f(r.get("abs_speed_avg3")),
            early=_f(r.get("early_speed_avg3")),
            late=_f(r.get("late_speed_avg3")),
            finish=_f(r.get("finish_speed_avg3")),
            # 기복이 클수록 당일 편차를 크게 준다. 이력이 없으면 크게 흔든다.
            spread=float(np.clip(_f(r.get("speed_sd5"), 0.9), 0.35, 1.6)),
            experience=int(_f(r.get("starts_prior"), 0)),
        ))
    return out


def anchor_to(runners: Sequence[Runner], target_prob: Sequence[float],
              strength: float = 1.0) -> None:
    """총 능력치를 예측 승률에 맞춰 고정한다 (제자리 수정).

    시뮬레이션은 예측을 **대체**하지 않고 **연출**한다. 화면에서 ◎ 축마가
    아닌 말이 우승해 버리면 같은 페이지 안에서 자기모순이 생긴다.

    그래서 '누가 이기는가'는 예측 모델이 정한 대로 두고, 시뮬레이션은
    '어떤 전개로 그렇게 되는가'만 만든다. 승률의 로그를 능력치로 환산해
    base 에 심으면, 반복 시행의 착순 분포가 예측 승률에 수렴한다.
    """
    p = np.asarray([max(float(x), 1e-4) for x in target_prob], dtype=float)
    p = p / p.sum()
    # 로그오즈를 능력 z 로. 계수는 시뮬레이션의 속도-능력 민감도에 맞춰 잡는다.
    z = np.log(p)
    z = (z - z.mean()) / (z.std() or 1.0)
    for r, v in zip(runners, z):
        r.base = float(v * strength)


def fit_noise(runners: Sequence[Runner], distance: float,
              target_prob: Sequence[float], iters: int = 9, corners: int = 0) -> float:
    """'결과가 갈리는 정도'를 경주마다 보정한다.

    능력 격차를 줄여서 맞추려 하면, 격차가 작아진 만큼 각질·구간 특성이 능력을
    압도해 순위 자체가 뒤집힌다(실제로 그런 경주가 나왔다). 그래서 능력 순서는
    예측 모델이 정한 대로 고정하고, **불확실성의 크기만** 조절한다.

    짧은 시뮬레이션을 반복하며 축마의 시뮬 승률이 예측 승률과 맞는
    변동폭 배율을 이분법으로 찾는다. 변동폭이 작을수록 예측대로 굳고,
    클수록 이변의 여지가 커진다.
    """
    anchor_to(runners, target_prob)
    target = float(np.max(np.asarray(target_prob, dtype=float)))
    lo, hi, best = 0.2, 6.0, 1.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        got = float(np.max(simulate(runners, distance, n_sims=400,
                                    noise_scale=mid, corners=corners).win_prob))
        best = mid
        if got > target:
            lo = mid          # 너무 결정적 → 변동폭을 키운다
        else:
            hi = mid
        if abs(got - target) < 0.015:
            break
    return best


def simulate(
    runners: Sequence[Runner],
    distance: float,
    n_sims: int = N_SIMS,
    seed: int = 20260808,
    noise_scale: float = 1.0,
    scenario_winner: Optional[int] = None,
    corners: int = 0,
) -> RaceSim:
    """경주를 n_sims 번 달려 본다.

    ``noise_scale`` 은 '결과가 얼마나 갈리는가'를 조절한다. 능력 순서는 예측
    모델이 정하고, 이 값이 그 순서가 뒤집힐 여지를 정한다.

    ``scenario_winner`` 는 화면에 보여줄 대표 한 판을 고르는 기준이다. 그냥
    중앙값 한 판을 뽑으면, 축마 승률이 30%인 경주에서는 열에 일곱은 다른 말이
    이기는 그림이 나온다. 같은 페이지에서 ◎ 를 달아 놓고 다른 말이 들어오는
    영상을 트는 셈이다. 그래서 **축마가 이긴 판들 중 가장 전형적인 한 판**을
    고른다. 화면이 답하는 질문은 '누가 이기나'가 아니라
    '우리 예상대로 흘러가면 어떤 그림인가'다.
    """
    n = len(runners)
    if n == 0:
        empty = np.zeros(0)
        return RaceSim(empty, empty, empty, np.zeros((0, 0)), np.zeros((0, 0)))

    rng = np.random.default_rng(seed)
    n_seg = max(3, int(round(distance / SEG_M)))
    # 마필별 추가 주행거리(m). 안쪽을 도는 말은 0 에 가깝다.
    lane_extra = lane_offsets(runners, len(runners)) * LANE_WIDTH * CORNER_RAD * max(0, corners)
    # 구간 진행도 0(출발) ~ 1(결승)
    prog = (np.arange(n_seg) + 0.5) / n_seg

    base = np.array([r.base for r in runners])
    early = np.array([r.early for r in runners])
    late = np.array([r.late for r in runners])
    finish = np.array([r.finish for r in runners])
    spread = np.array([r.spread for r in runners])
    slope = np.array([STYLE_SLOPE.get(r.style, 0.0) for r in runners])
    exp = np.array([r.experience for r in runners])

    # 이력이 부족한 말은 구간 특성 추정을 평균 쪽으로 끌어당긴다.
    # 3전 미만이면 '어떻게 달리는 말인지' 사실상 모른다고 보는 편이 정직하다.
    # base(총 능력)는 예측 모델이 고정하므로 여기서 건드리지 않는다.
    shrink = np.clip(exp / 5.0, 0.0, 1.0)
    early, late, finish = (v * shrink for v in (early, late, finish))

    # 구간별 능력 가중치: 앞구간은 early, 뒷구간은 late/finish 가 지배한다
    w_early = np.clip(1.0 - prog * 2.2, 0.0, 1.0)          # 초반에만
    w_late = np.clip((prog - 0.45) / 0.55, 0.0, 1.0)        # 막판 600m 쪽
    w_finish = np.clip((prog - 0.80) / 0.20, 0.0, 1.0)      # 최후 200m
    # 각질 배분: 선행은 앞에 +, 뒤에 −
    shape = (0.5 - prog) * 2.0                              # +1(출발) ~ -1(결승)

    # (sims, n, seg) 를 한 번에 만들면 메모리가 크므로 sims 를 나눠 처리한다
    chunk = max(1, min(n_sims, int(4e6 / max(1, n * n_seg))))
    ranks_sum = np.zeros(n)
    wins = np.zeros(n)
    places = np.zeros(n)
    kept: Optional[np.ndarray] = None
    kept_total: Optional[np.ndarray] = None
    done = 0

    while done < n_sims:
        m = min(chunk, n_sims - done)
        # 당일 컨디션 — 경주 전체에 걸린다
        day = rng.normal(0.0, spread[None, :, None] * 0.55 * noise_scale, size=(m, n, 1))
        # 구간 편차 — 매 구간 흔들린다
        noise = rng.normal(0.0, 0.30 * noise_scale, size=(m, n, n_seg))

        ability = (
            base[None, :, None]
            + early[None, :, None] * w_early[None, None, :]
            + late[None, :, None] * w_late[None, None, :]
            + finish[None, :, None] * w_finish[None, None, :]
            + slope[None, :, None] * shape[None, None, :] * 0.45
            + day + noise
        )

        # 페이스 대가: 각 구간에서 선두권에 있으면 이후 구간에 피로가 쌓인다.
        # 선행마가 몰릴수록 서로를 갉아먹고, 그 손실이 뒤쪽에 기회가 된다.
        lead_press = np.clip(ability - np.quantile(ability, 0.75, axis=1, keepdims=True), 0, None)
        fatigue = np.cumsum(lead_press, axis=2) / n_seg
        ability = ability - fatigue * 0.9

        # 능력 z → 구간 소요시간. 200m 를 대략 12초로 두고 z 1당 약 2% 변동.
        seg_time = (SEG_M / 16.7) * (1.0 - 0.02 * ability)
        seg_time = np.clip(seg_time, 6.0, 30.0)

        # 코너에서 바깥으로 도는 대가. 레인 하나당 코너마다 LANE_WIDTH×π/2 만큼
        # 더 뛴다. 그 거리를 구간에 고르게 나눠 시간으로 환산한다.
        if corners and lane_extra.any():
            per_seg = lane_extra[None, :, None] / max(1, n_seg)
            seg_time = seg_time * (1.0 + per_seg / SEG_M)
        total = seg_time.sum(axis=2)                       # (m, n)

        order = np.argsort(np.argsort(total, axis=1), axis=1)  # 0=1착
        wins += (order == 0).sum(axis=0)
        places += (order <= 2).sum(axis=0)
        ranks_sum += (order + 1).sum(axis=0)

        if kept is None or kept_total is None:
            cand = np.arange(m)
            if scenario_winner is not None:
                won = order[:, scenario_winner] == 0
                if won.any():
                    cand = np.flatnonzero(won)
            if len(cand):
                # 후보 중 **예상 순서와 가장 잘 맞는 판**을 고른다.
                #
                # 총소요시간이 중앙값인 판을 뽑던 방식은 1착만 맞을 뿐 나머지
                # 순서가 난수였다. 그래서 화면에서 ◎ 2순위가 6착으로 들어오고
                # 7순위가 2착을 하는 그림이 나왔다 — 같은 페이지 안에서 추천과
                # 미리보기가 서로를 부정한다.
                #
                # runners 는 예상 순위대로 들어오므로 인덱스가 곧 예상 착순이다.
                # 예상 착순과 시뮬 착순의 차이를 상위권에 가중해 더한 값이 작을수록
                # '우리 예상대로 흘러간 판'이다. 다만 최솟값을 고르면 1-2-3-4-5 로
                # 줄줄이 들어오는 비현실적인 그림이 되므로, 하위 15% 지점을 쓴다.
                w = 1.0 / (np.arange(n) + 1.0)
                cost = (np.abs(order[cand] - np.arange(n)[None, :]) * w).sum(axis=1)
                pick = int(np.clip(round(len(cand) * 0.15), 0, len(cand) - 1))
                idx = int(cand[np.argsort(cost)[pick]])
                kept = seg_time[idx]
                kept_total = total[idx]
        done += m

    win_prob = wins / n_sims
    place_prob = places / n_sims
    mean_rank = ranks_sum / n_sims

    # 대표 시나리오를 '누적 통과 시각'으로 바꿔 애니메이션에 쓴다
    cum_time = np.cumsum(kept, axis=1)                     # (n, seg)
    return RaceSim(
        win_prob=win_prob,
        place_prob=place_prob,
        mean_rank=mean_rank,
        positions=cum_time,
        seg_times=kept,
        runners=list(runners),
        n_sims=n_sims,
    )


# ---------------------------------------------------------------------------
# 신뢰도 지표
# ---------------------------------------------------------------------------

# 임계값은 과거 경주의 실제 점수 분포에서 백분위로 잡는다(상위 20% / 55%).
# 손으로 고른 상수를 쓰면 운영 분포와 어긋나 한쪽 등급이 비어 버린다 —
# 실제로 처음 쓰던 72/52 에서는 어떤 경주도 강승부가 되지 못했다.
# 재산출: python tools/calibrate_confidence.py
CONFIDENCE_LABELS = [
    (51, "강승부", "축마의 우위가 뚜렷하고 출주마 이력도 충분합니다"),
    (41, "중승부", "상위권 판단은 서지만 변수가 남아 있습니다"),
    (0, "약승부", "우열이 가려지지 않거나 이력이 부족해 예측이 흔들립니다"),
]


def confidence(sim: RaceSim) -> Dict:
    """시뮬레이션 결과에서 예측 신뢰도를 0~100 으로 지표화한다.

    세 가지를 본다.
      * **우위** — 1순위 승률이 얼마나 높은가
      * **격차** — 1순위와 2순위의 차이가 뚜렷한가
      * **근거** — 출주마들이 판단할 만한 이력을 갖고 있는가

    셋이 모두 갖춰졌을 때만 '강승부'가 된다. 못 맞히는 경주를 미리 말하는 것이
    맞히는 경주를 말하는 것만큼 중요하다.
    """
    if sim.n_sims == 0 or len(sim.win_prob) == 0:
        return {"score": 0, "label": "약승부", "desc": CONFIDENCE_LABELS[-1][2]}

    p = np.sort(sim.win_prob)[::-1]
    n = len(p)
    top = float(p[0])
    second = float(p[1]) if n > 1 else 0.0
    # 두수가 많을수록 같은 승률이라도 더 어려운 경주다
    even = 1.0 / n

    edge = np.clip((top - even) / max(1e-6, 1 - even), 0, 1)          # 평균 대비 우위
    gap = np.clip((top - second) / max(top, 1e-6), 0, 1)              # 1·2순위 격차
    exp = np.array([r.experience for r in sim.runners])
    grounded = float(np.clip(np.mean(np.minimum(exp, 6) / 6.0), 0, 1))  # 이력 충실도

    score = 100 * (0.45 * edge + 0.30 * gap + 0.25 * grounded)
    score = float(np.clip(score, 0, 100))
    for cut, label, desc in CONFIDENCE_LABELS:
        if score >= cut:
            break
    return {
        "score": round(score),
        "label": label,
        "desc": desc,
        "edge": round(float(top) * 100, 1),
        "gap": round(float(top - second) * 100, 1),
        "grounded": round(grounded * 100),
    }


def par_times(conn) -> Dict[tuple, float]:
    """(경마장, 거리)별 우승 기록의 중앙값.

    시뮬레이션의 절대 시간을 여기에 맞춘다. 기준 속도를 상수로 두면 경마장을
    구분하지 못한다 — 제주는 한라마라 11.9m/s 로 달리고 서울·부산경남은
    15~16m/s 다. 같은 1200m 인데 27초가 차이 난다.
    """
    rows = conn.execute(
        "SELECT r.meet, r.distance, res.record_sec FROM results res "
        "JOIN races r ON r.race_key = res.race_key "
        "WHERE res.ord = 1 AND res.record_sec BETWEEN 40 AND 300 "
        "  AND r.distance BETWEEN 700 AND 3000").fetchall()
    buckets: Dict[tuple, list] = {}
    speeds: Dict[str, list] = {}
    for meet, dist, sec in rows:
        if not (meet and dist and sec):
            continue
        buckets.setdefault((meet, int(dist)), []).append(float(sec))
        speeds.setdefault(meet, []).append(float(dist) / float(sec))
    out = {k: float(np.median(v)) for k, v in buckets.items() if len(v) >= 10}
    # 표본이 없는 (경마장, 거리) 조합은 그 경마장의 평균 속도로 메운다
    for meet, sp in speeds.items():
        out[(meet, 0)] = float(np.median(sp))
    return out


def corner_counts(conn) -> Dict[tuple, int]:
    """(경마장, 거리) → 그 경주가 도는 코너 수.

    주로 제원을 외부에서 가져오지 않는다. 경주성적에 코너 통과기록이 몇 개
    찍히는지가 곧 코너 수다 — 서울 1200m 은 c3·c4 만, 1700m 은 c1~c4 가 있다.
    """
    rows = conn.execute(
        "SELECT r.meet, r.distance, "
        "  SUM(res.c1_rank IS NOT NULL), SUM(res.c2_rank IS NOT NULL), "
        "  SUM(res.c3_rank IS NOT NULL), SUM(res.c4_rank IS NOT NULL), COUNT(*) "
        "FROM results res JOIN races r ON r.race_key = res.race_key "
        "WHERE r.distance BETWEEN 500 AND 3000 GROUP BY 1, 2").fetchall()
    out: Dict[tuple, int] = {}
    for meet, dist, c1, c2, c3, c4, n in rows:
        if not (meet and dist and n and n >= 30):
            continue
        # 절반 이상의 행에 기록이 있으면 그 코너를 실제로 돈 것으로 본다
        k = sum(1 for c in (c1, c2, c3, c4) if c and c / n >= 0.5)
        out[(meet, int(dist))] = max(2, k)
    return out


def corner_count(counts: Dict[tuple, int], meet: str, distance: float) -> int:
    """표에 없으면 거리로 어림한다 — 대략 한 코너에 400m 쯤이다."""
    if counts and (meet, int(distance or 0)) in counts:
        return counts[(meet, int(distance))]
    return int(np.clip(round((distance or 1200) / 450), 2, 4))


def lane_offsets(runners: Sequence[Runner], field_size: int) -> np.ndarray:
    """마필별 '안쪽에서 몇 레인 밖으로 도는가'.

    마번이 바깥일수록, 각질이 추입일수록 밖으로 돈다. 두수가 많으면 안으로
    파고들기 어려워 그 차이가 커진다. 실제 마번별 승률 곡선(1~4번이 뚜렷이
    유리하고 6번 이후로는 완만)에 맞춰 제곱근으로 눌렀다.
    """
    n = max(1, field_size)
    out = []
    for r in runners:
        gate = r.chul_no or n
        # 실제 마번별 승률은 1~4번이 거의 같고(10.3~10.6%) 6번부터 떨어진다
        # (8.1~8.6%). 안쪽 몇 두는 모두 레일을 잡을 수 있고, 그 밖부터 밀린다는
        # 뜻이다. 그래서 앞쪽은 평평하게 두고 그 뒤부터 벌린다.
        g = math.sqrt(max(0.0, gate - 3)) * 1.3
        out.append(max(0.0, g + STYLE_LANE.get(r.style, 0.2)))
    return np.asarray(out, dtype=float)


# 앞말이 이 시간차 안에 있으면 그 레인은 쓸 수 없다. 16m/s 에서 0.35초는 약 6m,
# 두 마신쯤이다 — 그보다 붙으면 앞말 뒷발에 걸린다. **뒷말은 막지 않는다** — 막는 것은 언제나 앞이다.
#
# 처음에는 양방향 0.18초(한 마신)로 뒀는데, 그러면 조금만 벌어져도 뒷말이
# 앞말 레인으로 들어가 결국 전원이 레일에 일렬로 붙었다. 실제 경주에서 무리가
# 2~4레인 폭을 유지하는 이유는 바로 앞말을 밟고 지나갈 수 없기 때문이다.
BLOCK_AHEAD = 0.25
LANE_MIN_GAP = 1.0      # 비켜설 때 필요한 최소 레인 간격 (말 폭)
LANE_SLEW = 4.0         # 한 구간(200m)에서 옆으로 옮길 수 있는 최대 레인 수

# 출발 후 무리가 머무는 레인 폭의 상한. 실제 경주에서 코너를 크게 도는 말은
# 드물다 — 거리 손해가 커서 기수가 어떻게든 안으로 붙인다. 기하만으로 자리를
# 다투게 두면 열 마리가 열 레인으로 부챗살처럼 퍼지는데, 그런 그림은 나오지
# 않는다. 안쪽이 전부 막혔을 때만 이 상한을 넘어선다.
LANE_CAP = 4.0

# 한 구간(200m)을 몇 조각으로 쪼개 자리를 다시 정할지. 촘촘할수록 추월 순간의
# 겹침을 잘 잡아내지만 대본이 그만큼 커진다. 50m 마다면 16m/s 에서 3초에 한 번,
# 말 길이 하나가 채 지나기 전이다.
SUB_STEPS = 4

# 미리보기 대본의 판(版). 레인 계산이나 payload 구조를 바꾸면 올린다.
#
# 시행된 경주는 예측을 다시 만들지 않으므로 옛 대본이 DB 에 남는다. 형식이
# 같아도 계산이 달라졌으면 다시 구워야 하는데, 'lanes 키가 있나' 로만 보면
# 그걸 잡아내지 못한다 — 실제로 그래서 배포본만 옛 움직임으로 남았다.
PAYLOAD_VERSION = 4


def lane_paths(runners: Sequence[Runner], seg_times: np.ndarray) -> np.ndarray:
    """구간마다 각 말이 **몇 레인에서 달리는가**.

    지금까지는 말마다 레인이 경주 내내 고정이었다. 그래서 앞이 뻥 뚫리고 인코스가
    비어도 계속 바깥으로 도는 그림이 나왔다. 실제 기수는 거리가 짧은 안쪽을
    잡으려 하고, 앞이 막히면 그때 밖으로 낸다.

    두 가지 제약으로 재현한다.
      * **안쪽 우선** — 비어 있으면 레일 쪽으로 들어간다
      * **앞을 밟지 못함** — 앞말이 BLOCK_AHEAD 안에 있으면 그 레인을 쓸 수 없고
        한 마리 폭 이상 비켜서야 한다. 뒤따르는 말은 막지 않는다.

    옆으로 움직이는 속도에도 한계를 둔다(LANE_SLEW). 한 구간 만에 트랙을 가로질러
    순간이동하면 그것대로 어색하다.
    """
    n, n_seg = seg_times.shape
    if n == 0:
        return np.zeros((0, 0))
    # 200m 마다만 자리를 정하면 그 사이에서 일어나는 추월을 아무도 막지 않는다.
    # 구간 경계 두 곳 모두에서 시간차가 벌어져 있어도, 중간에 앞뒤가 바뀌는
    # 순간에는 같은 레인에서 서로를 통과해 버린다. 그래서 잘게 나눠 잡는다.
    step = np.cumsum(seg_times, axis=1)
    edges = np.concatenate([np.zeros((n, 1)), step], axis=1)      # 0 포함 경계
    cum = np.empty((n, n_seg * SUB_STEPS))
    for j in range(n_seg):
        a, b = edges[:, j], edges[:, j + 1]
        for u in range(SUB_STEPS):
            f = (u + 1) / SUB_STEPS
            cum[:, j * SUB_STEPS + u] = a + (b - a) * f
    n_seg = cum.shape[1]
    slew = LANE_SLEW / SUB_STEPS
    # 출발선에서는 게이트에 **한 마리 폭씩** 벌려 일렬로 선다. 1번이 레일,
    # 바깥 게이트일수록 그만큼 밖이다. 여기서부터 안쪽으로 모여든다.
    start = np.array([max(0.0, float((r.chul_no or 1) - 1)) for r in runners])
    out = np.zeros((n, n_seg))
    prev = start.copy()

    for j in range(n_seg):
        if j == 0:
            # 출발선에서는 게이트 자리 그대로다. 여기서부터 자리를 다툰다.
            out[:, 0] = start
            prev = start
            continue
        t = cum[:, j]
        placed: List[tuple] = []            # (시각, 레인)
        for i in np.argsort(t):             # 앞선 말부터 자리를 잡는다
            lo = max(0.0, prev[i] - slew)
            # 게이트에서 출발한 직후에는 자기 자리에서 시작하되, 이후로는
            # 안쪽 무리 폭 안으로 들어오려 한다.
            hi = min(prev[i] + slew, max(LANE_CAP, lo))
            lane = None
            # 안쪽부터 훑어 비어 있는 첫 자리를 잡는다
            cand = np.arange(lo, hi + 0.001, 0.25)
            for c in cand:
                if all(t[i] - tk >= BLOCK_AHEAD or abs(c - lk) >= LANE_MIN_GAP
                       for tk, lk in placed):
                    lane = c
                    break
            if lane is None:                # 안쪽이 다 막히면 밖으로 낸다
                lane = hi
                while any(t[i] - tk < BLOCK_AHEAD and abs(lane - lk) < LANE_MIN_GAP
                          for tk, lk in placed):
                    lane += 0.25
            out[i, j] = lane
            placed.append((t[i], lane))
        prev = out[:, j]
    return out


def curve_fraction(distance: float, n_seg: int, straight: float,
                   curve: float) -> np.ndarray:
    """구간마다 **곡선 위를 달리는 비율**.

    결승선에서 거꾸로 재면 직선주로 → 곡선 → 반대편 직선 → 곡선 순이다. 어느
    구간이 코너에 걸쳐 있는지 알아야 '바깥으로 돈 대가' 를 그 구간에만 물릴 수
    있다. 직선에서는 바깥으로 나가도 손해가 없다.
    """
    lap = 2 * straight + 2 * curve
    seg = distance / max(1, n_seg)
    out = np.zeros(n_seg)
    for j in range(n_seg):
        # j 번째 구간이 차지하는 '결승선까지 남은 거리' 범위
        far, near = distance - j * seg, distance - (j + 1) * seg
        hit, steps = 0, 12
        for k in range(steps):                       # 잘게 나눠 곡선 여부를 센다
            back = (near + (far - near) * (k + 0.5) / steps) % lap
            if straight <= back < straight + curve or back >= 2 * straight + curve:
                hit += 1
        out[j] = hit / steps
    return out


def apply_lane_cost(seg_times: np.ndarray, lanes: np.ndarray, distance: float,
                    straight: float, curve: float) -> np.ndarray:
    """바깥 레인으로 돈 만큼 실제로 더 뛴 거리를 시간에 반영한다.

    이것이 없으면 레인은 그림에 불과하고 아웃코스 불리함이 기록에 남지 않는다.
    반경 r 만큼 밖에서 각도 θ 를 돌면 r·θ 만큼 더 뛴다. 곡선 길이 curve 가 π
    라디안이므로, 구간이 곡선에 걸친 비율만큼 각도를 환산해 물린다.
    """
    n, n_seg = seg_times.shape
    if n == 0 or n_seg == 0:
        return seg_times
    # lanes 는 구간을 더 잘게 쪼갠 격자로 온다(SUB_STEPS). 거리 손해는 구간
    # 단위로 물리므로 조각들을 구간별 평균으로 되돌린다.
    if lanes.shape[1] != n_seg:
        lanes = lanes.reshape(n, n_seg, -1).mean(axis=2)
    frac = curve_fraction(distance, n_seg, straight, curve)
    seg_len = distance / n_seg
    theta = frac * seg_len * math.pi / max(1e-6, curve)      # 구간별 회전 각도
    extra = lanes * LANE_WIDTH * theta[None, :]              # 추가 주행거리(m)
    return seg_times * (1.0 + extra / max(1e-6, seg_len))


def pace_factors(conn, before: str) -> Dict[str, float]:
    """마필별 '기준 기록 대비 비율'.

    1.0 이면 그 경마장·거리의 평균 우승 기록 수준, 낮을수록 빠르다. 경마장
    평균만 쓰면 국1군이든 국6군이든 같은 시간이 나오므로, 실제로 그 말이
    어떤 기록으로 달려 왔는지를 곱해 개별화한다.

    거리에 따른 속도 저하는 기준표(par)가 이미 담고 있으므로, 말에게서는
    **상대적인 빠르기만** 가져온다. 그래야 1200m 만 뛰던 말을 1800m 경주에
    올려도 말이 되는 시간이 나온다.
    """
    rows = conn.execute(
        "SELECT res.hr_no, r.meet, r.distance, res.record_sec "
        "FROM results res JOIN races r ON r.race_key = res.race_key "
        "WHERE res.record_sec BETWEEN 40 AND 300 AND r.rc_date < ? "
        "  AND r.distance BETWEEN 700 AND 3000 "
        "ORDER BY r.rc_date DESC", (before,)).fetchall()
    pars = par_times_from(rows)
    acc: Dict[str, list] = {}
    for hr_no, meet, dist, sec in rows:
        base = pars.get((meet, int(dist)))
        if not (hr_no and base):
            continue
        lst = acc.setdefault(hr_no, [])
        if len(lst) < 6:                      # 최근 6전이면 충분하다
            lst.append(float(sec) / base)
    return {h: float(np.median(v)) for h, v in acc.items() if v}


def par_times_from(rows) -> Dict[tuple, float]:
    """(경마장, 거리) → 그 조건의 평균 완주 기록. 개별화의 기준선이 된다."""
    buckets: Dict[tuple, list] = {}
    for _hr, meet, dist, sec in rows:
        if meet and dist and sec:
            buckets.setdefault((meet, int(dist)), []).append(float(sec))
    return {k: float(np.median(v)) for k, v in buckets.items() if len(v) >= 8}


def par_time(pars: Dict[tuple, float], meet: str, distance: float) -> Optional[float]:
    """그 경주에서 기대되는 우승 기록(초)."""
    if not pars or not distance:
        return None
    exact = pars.get((meet, int(distance)))
    if exact:
        return exact
    speed = pars.get((meet, 0))
    return float(distance) / speed if speed else None


def expected_run(runners: Sequence[Runner], distance: float,
                 n_sims: int = 800, noise_scale: float = 1.0,
                 seed: int = 20260808, corners: int = 0,
                 straight: float = 0.0, curve: float = 0.0) -> np.ndarray:
    """**예상대로 전개될 경우**의 구간 소요시간을 만든다.

    화면의 미리보기가 답해야 할 질문은 '이번엔 어떻게 될까'가 아니라
    '우리 예상대로 흘러가면 어떤 그림인가'다. 수천 판 중 한 판을 뽑아 보여 주면
    승률 30%인 말이 지는 그림이 나오고 — 확률적으로는 지극히 정상이지만 —
    추천 순서 옆에 붙는 순간 방문자에게는 모순으로 읽힌다.

    그래서 두 가지를 나눈다.
      * **승률**은 잡음을 넣은 수천 번의 시행에서 나온다 (분포)
      * **미리보기**는 예상 순서대로 들어오는 한 판이다 (요약)

    만드는 방법:
      1. 실제로 여러 판을 달려 보고 **예상 순서와 가장 가까운 판**을 고른다.
         도중 전개(누가 앞서 나가고 누가 따라붙는지)는 이 판에서 그대로 온다.
      2. 그 판의 완주 순서가 예상과 어긋나는 부분만 **마지막 구간에서** 바로잡는다.
         착차의 크기는 시뮬레이션이 정하고 순서만 예상이 정한다.

    잡음을 꺼 버리면 모든 말이 같은 모양으로 달려 구간 순위가 한 번도 바뀌지
    않는 정지화면이 된다. 전개를 보여 주는 것이 목적이므로 그렇게 하지 않는다.
    """
    n = len(runners)
    if n == 0:
        return np.zeros((0, 0))
    sim = simulate(runners, distance, n_sims=n_sims, noise_scale=noise_scale,
                   seed=seed, corners=corners)
    seg = sim.seg_times.copy()
    if seg.size == 0:
        return seg

    cum = np.cumsum(seg, axis=1)
    final = cum[:, -1]
    # runners 는 예상 순위대로 들어온다 → i 번째가 i+1 순위.
    # 관측된 완주 시간 분포는 그대로 두고, i 번째로 빠른 시간을 i 번째 말에게 준다.
    target = np.sort(final)
    delta = target - final

    # 게이트에서 출발해 안쪽으로 모여드는 실제 주행 코스를 만들고, 바깥으로
    # 돈 대가를 시간에 물린다. 그래야 아웃코스 불리함이 기록에 남는다.
    if straight and curve:
        lanes = lane_paths(runners, seg)
        seg = apply_lane_cost(seg, lanes, distance, straight, curve)
        cum = np.cumsum(seg, axis=1)
        final = cum[:, -1]
        target = np.sort(final)          # 순서는 예상이 정한다 — 다시 맞춘다
        delta = target - final

    # 보정은 마지막 두 구간에만 싣는다. 앞 구간을 건드리면 도중 전개가 바뀐다.
    k = min(2, seg.shape[1])
    tail = seg[:, -k:]
    weight = tail / np.maximum(tail.sum(axis=1, keepdims=True), 1e-6)
    adjusted = tail + delta[:, None] * weight
    # 구간 시간이 비현실적으로 줄지 않도록 바닥을 둔다
    adjusted = np.maximum(adjusted, tail * 0.45)
    seg[:, -k:] = adjusted
    return seg


def scale_to_par(seg: np.ndarray, target: Optional[float]) -> np.ndarray:
    """구간 시간을 실제 기록 수준으로 맞춘다.

    시뮬레이션이 정하는 것은 **말들 사이의 차이**이지 절대 시간이 아니다.
    절대 시간까지 물리에서 끌어내려 하면 기준 속도를 상수로 박게 되고,
    1700m 를 102초에 뛰는(실제 112초) 화면이 나온다. 착차의 비율은 그대로
    두고 전체만 실제 기록에 맞춘다.
    """
    if seg.size == 0 or not target:
        return seg
    winner = seg.sum(axis=1).min()
    if winner <= 0:
        return seg
    return seg * (target / winner)


def animation_payload(sim: RaceSim, distance: float, top_k: int = 99,
                      meet: str = "", corners: int = 0) -> Dict:
    """웹 애니메이션이 그대로 먹을 수 있는 형태로 압축한다.

    구간별 '통과 시각'을 주면 프런트에서 시간축을 따라 위치를 보간할 수 있다.
    """
    if sim.n_sims == 0 or sim.positions.size == 0:
        return {}
    n, n_seg = sim.positions.shape
    finish_time = sim.positions[:, -1]
    order = np.argsort(finish_time)
    # 레인은 **마번 순**으로 낸다. 도착순으로 늘어놓으면 위 레인부터 차례로
    # 들어오는 그림이 되어 경주로 보이지 않는다. 실제 경마 중계도 마번 순이다.
    lane = sorted(range(n), key=lambda i: sim.runners[i].chul_no or 99)
    # 구간마다 어느 레인에서 달리는지. 고정값을 쓰면 인코스가 비어도 계속
    # 바깥으로 도는 그림이 된다.
    lanes = lane_paths(sim.runners, sim.seg_times)
    spec = TRACK_SPEC.get(meet or "", TRACK_SPEC["서울"])
    return {
        "v": PAYLOAD_VERSION,
        "distance": float(distance),
        "segment_m": SEG_M,
        "n_segments": int(n_seg),
        # 주로 모양과 코너 수. 캔버스가 타원을 그리고 어디가 곡선인지 정한다.
        "track": {**spec, "corners": int(corners), "meet": meet or ""},
        "duration": float(finish_time.max()),
        "runners": [
            {
                "gate": sim.runners[i].chul_no,
                "name": sim.runners[i].hr_name,
                "style": sim.runners[i].style,
                "win": round(float(sim.win_prob[i]) * 100, 1),
                # 각 구간을 통과한 시각(초). 앞설수록 값이 작다.
                "splits": [round(float(t), 2) for t in sim.positions[i]],
                "sim_rank": int(np.where(order == i)[0][0]) + 1,
                # 구간별 주행 레인. 안쪽이 비면 들어가고, 막히면 밖으로 낸다.
                "lanes": [round(float(v), 2) for v in lanes[i]],
            }
            for i in lane
        ],
    }
