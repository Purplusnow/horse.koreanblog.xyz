# -*- coding: utf-8 -*-
"""PWA 설치 요건 점검 — 배포된 사이트를 실제로 받아서 확인한다.

    python3 tools/check_pwa.py                       (라이브)
    python3 tools/check_pwa.py --dir dist            (빌드 산출물)

**로컬 파일만 봐서는 못 잡는 게 둘 있다.** 하나는 매니페스트의 Content-Type —
GitHub Pages 가 .webmanifest 를 엉뚱한 형식으로 내보내면 크롬이 파일을 읽고도
버린다. 다른 하나는 sw.js 의 위치 — /static/ 아래로 새면 서비스 워커가 예상
화면을 담당하지 않는데, 파일은 멀쩡히 존재하므로 눈으로는 통과한다.
그래서 '있는가' 가 아니라 '쓸 수 있는가' 를 본다.
"""
import argparse, json, pathlib, sys, urllib.request

SITE = "https://horse.koreanblog.xyz"
NEED_ICONS = {"192x192", "512x512"}


class Check:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, label, detail=""):
        print(f"  {'OK  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
        if not cond:
            self.fail += 1
        return cond


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "horseai-pwa-check"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="빌드 산출물 폴더(주면 네트워크 대신 이쪽을 본다)")
    ap.add_argument("--site", default=SITE)
    a = ap.parse_args()
    c = Check()

    def get(path):
        if a.dir:
            # 산출물 폴더에서는 "/" 가 index.html 이다 — 서버가 해 주던 일을
            # 여기서 대신한다. 안 하면 디렉터리를 글자로 읽으려다 터진다.
            rel = path.lstrip("/")
            if rel == "" or rel.endswith("/"):
                rel += "index.html"
            p = pathlib.Path(a.dir) / rel
            if not p.exists():
                return None, {}, ""
            # 아이콘(PNG)도 같은 길로 들어온다 — 글자로 못 읽는 바이트가 있어도
            # 존재 확인은 되어야 하므로 디코딩 실패를 치환으로 넘긴다.
            return 200, {}, p.read_bytes().decode("utf-8", "replace")
        try:
            return fetch(a.site.rstrip("/") + path)
        except Exception as e:                                   # noqa: BLE001
            print(f"  (가져오기 실패 {path}: {e})")
            return None, {}, ""

    print(f"대상: {a.dir or a.site}\n")

    # ── 1. 매니페스트 ───────────────────────────────────────
    print("매니페스트")
    st, hdr, body = get("/manifest.webmanifest")
    if not c.ok(st == 200, "/manifest.webmanifest 응답", f"status={st}"):
        sys.exit(1)
    if not a.dir:
        ct = hdr.get("Content-Type", "")
        # 크롬은 JSON 계열이면 받아 준다. octet-stream 으로 나가면 조용히 버린다.
        c.ok("json" in ct or "manifest" in ct, "Content-Type 이 JSON 계열", ct or "(없음)")
    try:
        m = json.loads(body)
    except json.JSONDecodeError as e:
        c.ok(False, "JSON 파싱", str(e)); sys.exit(1)
    c.ok(bool(m.get("name")), "name", m.get("name", ""))
    c.ok(bool(m.get("short_name")), "short_name", m.get("short_name", ""))
    c.ok(m.get("display") in ("standalone", "fullscreen", "minimal-ui"),
         "display 가 설치 가능 모드", str(m.get("display")))
    c.ok(bool(m.get("start_url")), "start_url", str(m.get("start_url")))
    sizes = {i.get("sizes") for i in m.get("icons", [])}
    c.ok(NEED_ICONS <= sizes, "192·512 아이콘", " ".join(sorted(s for s in sizes if s)))
    c.ok(any("maskable" in (i.get("purpose") or "") for i in m.get("icons", [])),
         "maskable 아이콘", "런처 모양에 맞춰 깎일 때 잘림 방지")

    # 아이콘이 실제로 받아지는가 — 주소만 맞고 파일이 없는 경우가 제일 흔하다.
    for ic in m.get("icons", []):
        src = (ic.get("src") or "").split("?")[0]
        st2, _, _ = get(src)
        c.ok(st2 == 200, f"아이콘 파일 {src}", f"status={st2}")

    # ── 2. 서비스 워커 ──────────────────────────────────────
    print("\n서비스 워커")
    st, _, sw = get("/sw.js")
    c.ok(st == 200, "/sw.js 가 **뿌리**에 있다", f"status={st}")
    c.ok("addEventListener('fetch'" in sw, "fetch 처리기 존재")
    c.ok("mode === 'navigate'" in sw, "문서 요청을 따로 다룬다")
    # 예상·결과가 캐시로 굳으면 안 된다 — 문서는 네트워크 우선이어야 한다.
    nav = sw.split("mode === 'navigate'", 1)[-1][:400] if "mode === 'navigate'" in sw else ""
    c.ok("await fetch(req)" in nav and nav.index("await fetch(req)") < nav.find("caches.match"),
         "문서는 네트워크 우선", "캐시 우선이면 어제 예상이 오늘자로 보인다")

    # ── 3. 페이지가 둘을 실제로 물고 있는가 ──────────────────
    print("\n페이지 연결")
    st, _, html = get("/")
    c.ok('rel="manifest"' in html, "홈에 manifest 링크")
    c.ok('name="theme-color"' in html, "theme-color 메타")
    c.ok("pwa.js" in html, "pwa.js 로드(서비스 워커 등록)")

    print(f"\n{'통과' if not c.fail else f'{c.fail}건 실패'}")
    sys.exit(1 if c.fail else 0)


if __name__ == "__main__":
    main()
