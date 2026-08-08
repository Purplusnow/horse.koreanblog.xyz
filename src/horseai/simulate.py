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

import numpy as np
import pandas as pd

SEG_M = 200.0          # 구간 길이(m). 구간기록이 200m(1F) 단위로 제공된다.
N_SIMS = 2000          # 반복 횟수. 12두 경주에서 승률 표준오차가 약 1%p 수준.

# 각질별 에너지 배분 곡선의 기울기.
# 양수면 초반에 힘을 쓰고 후반에 떨어진다(선행), 음수면 그 반대(추입).
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
              target_prob: Sequence[float], iters: int = 9) -> float:
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
                                    noise_scale=mid).win_prob))
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


def animation_payload(sim: RaceSim, distance: float, top_k: int = 99) -> Dict:
    """웹 애니메이션이 그대로 먹을 수 있는 형태로 압축한다.

    구간별 '통과 시각'을 주면 프런트에서 시간축을 따라 위치를 보간할 수 있다.
    """
    if sim.n_sims == 0 or sim.positions.size == 0:
        return {}
    n, n_seg = sim.positions.shape
    finish_time = sim.positions[:, -1]
    order = np.argsort(finish_time)
    return {
        "distance": float(distance),
        "segment_m": SEG_M,
        "n_segments": int(n_seg),
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
            }
            for i in range(n)
        ],
    }
