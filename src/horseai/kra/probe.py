"""엔드포인트 자동 탐침.

포털 문서가 경로 세그먼트를 일관되게 공개하지 않으므로, 실제 서비스키로 후보
경로를 한 번씩 때려보고 살아 있는 것을 확정해 캐시한다. 응답 필드도 같이 덤프해
두면 이후 파서를 필드명 추측 없이 작성할 수 있다.

    python -m horseai.kra.probe            # 전체 확정 + 필드 덤프
    python -m horseai.kra.probe --json     # 결과를 JSON 으로
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ..clock import now_kst, today_kst
from .client import KraApiError, KraClient, redact
from .endpoints import ACTIVE_MEETS, REGISTRY, REQUIRED_KEYS, save_resolved

log = logging.getLogger(__name__)

FIELD_DUMP = Path("config/api_fields.json")


def recent_race_dates(n: int = 8, today: Optional[dt.date] = None) -> List[str]:
    """최근 경마 시행일 후보(금·토·일)를 최신순으로."""
    today = today or today_kst()
    out: List[str] = []
    d = today
    while len(out) < n:
        if d.weekday() in (4, 5, 6):  # 금, 토, 일
            out.append(d.strftime("%Y%m%d"))
        d -= dt.timedelta(days=1)
    return out


def _probe_one(client: KraClient, key: str, dates: List[str]) -> dict:
    """한 엔드포인트의 후보 경로를 순회하며 살아 있는 조합을 찾는다."""
    ep = REGISTRY[key]
    attempts: List[dict] = []

    for path in ep.candidate_paths():
        # 날짜 파라미터가 있는 API 는 실제 시행일을 넣어야 레코드가 나온다.
        param_sets: List[Dict[str, object]] = []
        if ep.date_params:
            for date in dates[:4]:
                for meet in ACTIVE_MEETS:
                    param_sets.append({"meet": meet, "rc_date": date})
            param_sets.append({"meet": 1})          # 날짜 없이(최근 한 달)
        else:
            param_sets.append({"meet": 1})
            param_sets.append({})

        for params in param_sets:
            try:
                body = client.raw(path, {**params, "pageNo": 1, "numOfRows": 5})
            except KraApiError as e:
                attempts.append({"path": path, "params": params, "error": f"[{e.code}] {e.msg}"})
                if e.fatal:
                    return {
                        "key": key,
                        "ok": False,
                        "fatal": True,
                        "reason": f"[{e.code}] {e.msg}",
                        "attempts": attempts,
                    }
                break  # 경로 자체가 없으면 다른 파라미터도 의미 없다
            except Exception as e:  # noqa: BLE001 - 네트워크/파싱 등
                attempts.append({"path": path, "params": params, "error": redact(repr(e))})
                break

            items = body.get("items")
            records = []
            if isinstance(items, dict):
                inner = items.get("item", [])
                records = [inner] if isinstance(inner, dict) else list(inner or [])
            elif isinstance(items, list):
                records = items

            if records:
                return {
                    "key": key,
                    "ok": True,
                    "path": path,
                    "sample_params": params,
                    "total_count": body.get("totalCount"),
                    "fields": sorted(records[0].keys()),
                    "sample_record": records[0],
                    "attempts": attempts,
                }
            attempts.append({"path": path, "params": params, "error": "빈 응답(레코드 0건)"})

    return {"key": key, "ok": False, "fatal": False, "reason": "살아 있는 경로를 찾지 못함", "attempts": attempts}


def probe_all(client: KraClient, keys: Optional[List[str]] = None) -> dict:
    dates = recent_race_dates()
    keys = keys or list(REGISTRY)
    results = {}
    for key in keys:
        log.info("프로브: %s (%s)", key, REGISTRY[key].title)
        results[key] = _probe_one(client, key, dates)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="한국마사회 오픈API 엔드포인트 탐침")
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    ap.add_argument("--only", nargs="*", help="특정 엔드포인트 키만 검사")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        client = KraClient.from_env()
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        print("  data.go.kr 에서 활용신청 후 발급된 키를 .env 에 넣어주세요.", file=sys.stderr)
        return 2

    results = probe_all(client, args.only)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for key, r in results.items():
            ep = REGISTRY[key]
            if r["ok"]:
                print(f"✓ {ep.title:<20} {r['path']:<32} 필드 {len(r['fields'])}개  총 {r.get('total_count')}건")
            else:
                mark = "✗✗" if r.get("fatal") else "✗ "
                print(f"{mark}{ep.title:<20} {r.get('reason')}")
                for a in r["attempts"][-3:]:
                    print(f"     · {a['path']} {a['params']} → {a['error']}")

    resolved = {k: r["path"] for k, r in results.items() if r["ok"]}
    if resolved:
        p = save_resolved(
            resolved,
            meta={"probed_at": now_kst().isoformat(timespec="seconds")},
        )
        FIELD_DUMP.parent.mkdir(parents=True, exist_ok=True)
        FIELD_DUMP.write_text(
            json.dumps(
                {k: {"fields": r["fields"], "sample": r["sample_record"]} for k, r in results.items() if r["ok"]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n확정 경로 → {p}\n응답 필드 덤프 → {FIELD_DUMP}")

    missing = [k for k in REQUIRED_KEYS if k not in resolved]
    if missing:
        print(f"\n필수 엔드포인트 확정 실패: {', '.join(REGISTRY[k].title for k in missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
