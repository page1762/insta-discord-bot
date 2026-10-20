"""
인스타그램 → 디스코드 새 게시물/릴스 알림 봇

- 지정한 인스타 계정(타인 공개 계정 가능)을 주기적으로 확인
- 새 게시물/릴스가 올라오면 디스코드 웹훅으로 썸네일 포함 임베드 전송
- 썸네일은 디스코드에 직접 업로드하므로 인스타 이미지 링크가 만료돼도 깨지지 않음

사용법:
  python insta_discord_bot.py          # 계속 실행 (주기적 확인)
  python insta_discord_bot.py --test   # 각 계정의 최신 게시물 1개를 바로 보내서 테스트
  python insta_discord_bot.py --once   # 한 번만 확인하고 종료 (작업 스케줄러용)
  python insta_discord_bot.py --setup  # 공식 API 토큰 설정 (설정되면 자동으로 공식 API 모드 사용)
"""

import argparse
import datetime as dt
import json
import logging
import os
import random
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

IG_APP_ID = "936619743392459"  # 인스타 웹 클라이언트 공용 App ID
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
EMBED_COLOR = 0xE1306C  # 인스타 핑크
MAX_SEEN = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("insta-bot")


class RateLimited(Exception):
    pass


# ---------------------------------------------------------------- 파일 저장
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json 이 없습니다. config.example.json 을 복사해서 config.json 으로 만들어 주세요.")
    cfg = load_json(CONFIG_PATH, {})
    accounts = []
    for a in cfg.get("accounts", []):
        if isinstance(a, str):
            a = {"username": a}
        a["username"] = a["username"].strip().lstrip("@")
        a.setdefault("webhook_url", cfg.get("webhook_url"))
        a.setdefault("mention", cfg.get("mention", ""))
        if not a["webhook_url"] or "discord.com/api/webhooks" not in a["webhook_url"]:
            sys.exit(f"[{a['username']}] 디스코드 웹훅 URL이 설정되지 않았습니다.")
        accounts.append(a)
    if not accounts:
        sys.exit("config.json 의 accounts 에 인스타 계정을 하나 이상 넣어 주세요.")
    cfg["accounts"] = accounts
    cfg.setdefault("interval_minutes", 10)
    cfg.setdefault("sessionid", "")
    return cfg


# ---------------------------------------------------------------- 인스타
MOBILE_UA = (
    "Instagram 361.0.0.46.88 Android (34/14; 480dpi; 1080x2340; samsung; "
    "SM-S921N; e1s; s5e9945; ko_KR; 674675155)"
)
MOBILE_APP_ID = "567067343352427"


def _clean(v):
    return (v or "").strip().strip('"').strip()


def _strategies(username, cfg):
    sessionid = _clean(cfg.get("sessionid"))
    csrftoken = _clean(cfg.get("csrftoken"))
    cookies = {}
    if sessionid:
        cookies["sessionid"] = sessionid
        if ":" in sessionid or "%3A" in sessionid:
            cookies["ds_user_id"] = sessionid.replace("%3A", ":").split(":")[0]
    if csrftoken:
        cookies["csrftoken"] = csrftoken

    web_headers = {
        "User-Agent": USER_AGENT,
        "x-ig-app-id": IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": f"https://www.instagram.com/{username}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if csrftoken:
        web_headers["X-CSRFToken"] = csrftoken
    yield "웹", "https://www.instagram.com/api/v1/users/web_profile_info/", web_headers, cookies

    if sessionid:  # 모바일 앱 경로는 로그인 쿠키가 있을 때만 의미 있음
        mobile_headers = {
            "User-Agent": MOBILE_UA,
            "x-ig-app-id": MOBILE_APP_ID,
            "Accept-Language": "ko-KR",
        }
        yield "모바일", "https://i.instagram.com/api/v1/users/web_profile_info/", mobile_headers, cookies


def fetch_profile(username, cfg):
    """공개 프로필 정보와 최근 게시물(최대 12개)을 가져온다. 여러 경로를 차례로 시도."""
    errors = []
    for name, url, headers, cookies in _strategies(username, cfg):
        try:
            r = requests.get(url, params={"username": username}, headers=headers,
                             cookies=cookies or None, timeout=20, allow_redirects=False)
        except requests.RequestException as e:
            errors.append(f"{name}: 연결 오류 {e}")
            continue
        if r.status_code == 404:
            raise ValueError(f"계정을 찾을 수 없음: {username}")
        if r.status_code != 200:
            snippet = r.text[:150].replace("\n", " ")
            errors.append(f"{name}: HTTP {r.status_code} {snippet}")
            continue
        try:
            user = r.json()["data"]["user"]
        except (ValueError, KeyError, TypeError):
            errors.append(f"{name}: 응답 형식 이상 {r.text[:150]!r}")
            continue
        if user is None:
            raise ValueError(f"계정을 찾을 수 없음: {username}")
        log.debug("[%s] %s 경로로 조회 성공", username, name)
        return parse_user(user)
    raise RateLimited(" | ".join(errors))


# ---------------------------------------------------------------- 공식 API (Business Discovery)
GRAPH = "https://graph.facebook.com"
MEDIA_FIELDS = "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp"


class TokenError(Exception):
    pass


def graph_get(path, params, version, token=None):
    # 토큰은 URL 이 아닌 헤더로 보냄 → 오류 메시지·로그에 토큰이 찍히지 않음
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        r = requests.get(f"{GRAPH}/{version}/{path}", params=params, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"그래프 API 연결 오류: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        r.raise_for_status()
        raise RuntimeError(f"그래프 API 응답 이상: {r.text[:200]}")
    if "error" in data:
        err = data["error"]
        msg = f"{err.get('message')} (code {err.get('code')})"
        if err.get("code") == 190:
            raise TokenError(msg)
        if err.get("code") in (4, 17, 32, 613):
            raise RateLimited(msg)
        raise RuntimeError(msg)
    return data


def api_tokens(api):
    return [t for t in (api.get("page_token"), api.get("access_token")) if t]


def fetch_profile_api(username, api):
    version = api.get("version", "v26.0")
    fields = (f"business_discovery.username({username})"
              f"{{username,name,profile_picture_url,media.limit(12){{{MEDIA_FIELDS}}}}}")
    last = None
    for token in api_tokens(api):
        try:
            data = graph_get(api["ig_user_id"], {"fields": fields}, version, token=token)
            return parse_api_user(data["business_discovery"])
        except (TokenError, RuntimeError) as e:  # 이 토큰이 안 되면 다음 토큰 시도
            last = e
            continue
    raise TokenError(f"사용 가능한 토큰이 없습니다 — python insta_discord_bot.py --setup 으로 다시 설정하세요. ({last})")


def _shortcode(permalink, fallback):
    parts = [p for p in (permalink or "").split("/") if p]
    for i, p in enumerate(parts):
        if p in ("p", "reel", "reels", "tv") and i + 1 < len(parts):
            return parts[i + 1]
    return fallback


def parse_api_user(bd):
    profile = {
        "username": bd["username"],
        "full_name": bd.get("name") or bd["username"],
        "avatar": bd.get("profile_picture_url"),
        "is_private": False,
    }
    posts = []
    for m in (bd.get("media") or {}).get("data", []):
        is_reel = m.get("media_product_type") == "REELS"
        thumb = m.get("thumbnail_url") or (m.get("media_url") if m.get("media_type") != "VIDEO" else None)
        try:
            ts = int(dt.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp())
        except (KeyError, ValueError):
            ts = int(time.time())
        code = _shortcode(m.get("permalink"), m["id"])
        posts.append({
            "shortcode": code,
            "is_reel": is_reel,
            "url": m.get("permalink") or f"https://www.instagram.com/p/{code}/",
            "thumb": thumb,
            "caption": m.get("caption") or "",
            "timestamp": ts,
        })
    posts.sort(key=lambda p: p["timestamp"])
    return profile, posts


def use_api(cfg):
    api = cfg.get("api") or {}
    return bool(api.get("ig_user_id") and api_tokens(api))


def setup_api():
    """짧은 토큰 → 장기 토큰·페이지 토큰 교환, 인스타 비즈니스 계정 ID 자동 조회 후 config.json 저장."""
    raw = load_json(CONFIG_PATH, {})
    api = raw.setdefault("api", {})
    version = api.setdefault("version", "v26.0")
    for key, label in (("app_id", "앱 ID"), ("app_secret", "앱 시크릿")):
        if not _clean(api.get(key)):
            api[key] = input(f"{label}: ").strip()
    short = input("그래프 API 탐색기에서 발급한 액세스 토큰을 붙여 넣고 Enter: ").strip()

    data = graph_get("oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": api["app_id"],
        "client_secret": api["app_secret"],
        "fb_exchange_token": short,
    }, version)
    long_token = data["access_token"]
    api["access_token"] = long_token
    expires = int(data.get("expires_in") or 60 * 86400)
    api["access_token_expires"] = int(time.time()) + expires

    pages = graph_get("me/accounts", {
        "fields": "name,access_token,instagram_business_account{id,username}",
    }, version, token=long_token).get("data", [])
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        sys.exit("인스타 계정이 연결된 페이스북 페이지를 찾지 못했습니다. 페이지-인스타 연결과 토큰 권한을 확인하세요.")
    page = linked[0]
    if len(linked) > 1:
        for i, p in enumerate(linked):
            print(f"  {i + 1}. {p['name']} → @{p['instagram_business_account'].get('username')}")
        page = linked[int(input("사용할 번호: ")) - 1]
    api["ig_user_id"] = page["instagram_business_account"]["id"]
    api["page_token"] = page.get("access_token", "")  # 장기 사용자 토큰으로 받은 페이지 토큰은 만료되지 않음

    # 실제 대상 계정으로 조회 테스트 — 페이지 토큰이 안 되면 사용자 토큰만 사용
    targets = [a if isinstance(a, str) else a.get("username") for a in raw.get("accounts", [])]
    if targets and api["page_token"]:
        try:
            fetch_profile_api(targets[0].strip().lstrip("@"), {**api, "access_token": ""})
            print("페이지 토큰(만료 없음)으로 조회 확인")
        except Exception:
            api["page_token"] = ""
            print("페이지 토큰으로는 조회가 안 돼 사용자 토큰(60일)을 사용합니다. 만료 전에 --setup 을 다시 실행하세요.")
    if targets:
        fetch_profile_api(targets[0].strip().lstrip("@"), api)  # 실패 시 예외로 알려줌
        print(f"@{targets[0]} 게시물 조회 성공")

    save_json(CONFIG_PATH, raw)
    print(f"설정 완료 — 페이지 '{page['name']}', 인스타 @{page['instagram_business_account'].get('username')}")
    print("이제 python insta_discord_bot.py --test 로 확인하세요.")


def parse_user(user):
    profile = {
        "username": user["username"],
        "full_name": user.get("full_name") or user["username"],
        "avatar": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "is_private": user.get("is_private", False),
    }
    posts = []
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges", [])
    for e in edges:
        n = e["node"]
        is_reel = n.get("product_type") == "clips"
        code = n["shortcode"]
        cap_edges = (n.get("edge_media_to_caption") or {}).get("edges", [])
        posts.append({
            "shortcode": code,
            "is_reel": is_reel,
            "url": f"https://www.instagram.com/{'reel' if is_reel else 'p'}/{code}/",
            "thumb": n.get("display_url") or n.get("thumbnail_src"),
            "caption": cap_edges[0]["node"]["text"] if cap_edges else "",
            "timestamp": n.get("taken_at_timestamp", int(time.time())),
        })
    posts.sort(key=lambda p: p["timestamp"])  # 오래된 것 → 새 것
    return profile, posts


def download(url):
    if not url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.warning("이미지 다운로드 실패: %s", e)
        return None


# ---------------------------------------------------------------- 디스코드
def send_to_discord(account, profile, post):
    kind = "릴스" if post["is_reel"] else "게시물"
    caption = post["caption"].strip()
    if len(caption) > 1000:
        caption = caption[:1000] + "…"

    thumb = download(post["thumb"])
    avatar = download(profile["avatar"])

    embed = {
        "author": {
            "name": profile["username"],
            "url": f"https://www.instagram.com/{profile['username']}/",
        },
        "url": post["url"],
        "description": caption or f"새 {kind}",
        "color": EMBED_COLOR,
        "footer": {"text": f"Instagram · {kind}"},
        "timestamp": dt.datetime.fromtimestamp(post["timestamp"], dt.timezone.utc).isoformat(),
    }

    files = {}
    attachments = []
    if thumb:
        files["files[0]"] = ("thumb.jpg", thumb, "image/jpeg")
        attachments.append({"id": 0, "filename": "thumb.jpg"})
        embed["image"] = {"url": "attachment://thumb.jpg"}
    if avatar:
        files["files[1]"] = ("avatar.jpg", avatar, "image/jpeg")
        attachments.append({"id": 1, "filename": "avatar.jpg"})
        embed["author"]["icon_url"] = "attachment://avatar.jpg"

    # 링크를 <> 로 감싸면 디스코드가 자체 인스타 미리보기(자주 깨짐)를 붙이지 않음
    content = f"<{post['url']}>"
    if account.get("mention"):
        content = f"{account['mention']} {content}"

    payload = {
        "username": account.get("display_name") or profile["username"],
        "content": content,
        "embeds": [embed],
        "attachments": attachments,
        "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
    }
    avatar_url = account.get("webhook_avatar_url") or profile["avatar"]
    if avatar_url:
        payload["avatar_url"] = avatar_url

    for attempt in range(5):
        try:
            r = requests.post(
                account["webhook_url"],
                params={"wait": "true"},
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files=files or None,
                timeout=60,
            )
        except requests.RequestException as e:  # 오류 메시지에 웹훅 주소가 찍히지 않게
            raise RuntimeError(f"디스코드 연결 오류: {type(e).__name__}") from None
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 2))
            log.info("디스코드 속도 제한, %.1f초 대기", wait)
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"디스코드 전송 실패 HTTP {r.status_code}: {r.text[:300]}")
        log.info("[%s] 새 %s 전송: %s", profile["username"], kind, post["url"])
        return
    raise RuntimeError("디스코드 전송 재시도 초과")


# ---------------------------------------------------------------- 메인 로직
def check_account(account, cfg, state, test=False):
    username = account["username"]
    if use_api(cfg):
        profile, posts = fetch_profile_api(username, cfg["api"])
    else:
        profile, posts = fetch_profile(username, cfg)
    if profile["is_private"] and not posts:
        log.warning("[%s] 비공개 계정이라 게시물을 볼 수 없습니다.", username)
        return

    acc_state = state.setdefault(username, {"seen": [], "initialized": False})
    seen = set(acc_state["seen"])

    if test:
        if posts:
            send_to_discord(account, profile, posts[-1])
            # 테스트로 보낸 게시물도 '보낸 것'으로 기록 → 다음 정기 확인 때 중복 전송 안 함
            if acc_state["initialized"] and posts[-1]["shortcode"] not in seen:
                acc_state["seen"].append(posts[-1]["shortcode"])
        else:
            log.warning("[%s] 게시물이 없습니다.", username)
        return

    if not acc_state["initialized"]:
        # 첫 실행: 기존 게시물은 알림 없이 '본 것'으로 기록만
        acc_state["seen"] = [p["shortcode"] for p in posts]
        acc_state["initialized"] = True
        log.info("[%s] 초기화 완료 — 기존 게시물 %d개는 건너뜀. 이후 새 게시물부터 알림.", username, len(posts))
        return

    new_posts = [p for p in posts if p["shortcode"] not in seen]
    for p in new_posts:  # 오래된 순서로 전송
        send_to_discord(account, profile, p)
        acc_state["seen"].append(p["shortcode"])
        time.sleep(1)
    acc_state["seen"] = acc_state["seen"][-MAX_SEEN:]
    if not new_posts:
        log.info("[%s] 새 게시물 없음", username)


def run_cycle(cfg, state, test=False):
    """모든 계정을 한 번씩 확인. 차단(속도 제한)이 있었으면 True 반환."""
    limited = False
    for i, account in enumerate(cfg["accounts"]):
        if i:
            time.sleep(random.uniform(5, 15))  # 계정 사이 간격
        try:
            check_account(account, cfg, state, test=test)
        except RateLimited as e:
            limited = True
            log.warning("[%s] 인스타가 요청을 막음 (%s). 잠시 후 재시도합니다.", account["username"], e)
        except Exception as e:
            log.error("[%s] 오류: %s", account["username"], e)
        finally:
            save_json(STATE_PATH, state)
    return limited


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="최신 게시물 1개를 바로 전송해 테스트")
    ap.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    ap.add_argument("--setup", action="store_true", help="공식 API 토큰 설정")
    args = ap.parse_args()

    if args.setup:
        setup_api()
        return

    cfg = load_config()
    state = load_json(STATE_PATH, {})
    if use_api(cfg):
        log.info("공식 API(Business Discovery) 모드")
        exp = (cfg["api"].get("access_token_expires") or 0) - time.time()
        if not cfg["api"].get("page_token") and 0 < exp < 7 * 86400:
            log.warning("액세스 토큰이 %d일 후 만료됩니다 — --setup 으로 갱신하세요.", exp // 86400)
    elif _clean(cfg.get("sessionid")):
        log.info("로그인 쿠키(sessionid) 사용 중")
    else:
        log.info("로그인 쿠키(sessionid) 없음 — config.json 에 넣으면 차단이 줄어듭니다")

    if args.test or args.once:
        run_cycle(cfg, state, test=args.test)
        return

    base = max(5, float(cfg["interval_minutes"])) * 60
    backoff = 1
    log.info("봇 시작 — 계정 %d개, %d분 간격으로 확인", len(cfg["accounts"]), base // 60)
    while True:
        limited = run_cycle(cfg, state)
        backoff = min(backoff * 2, 6) if limited else 1
        wait = base * backoff * random.uniform(0.85, 1.25)  # 매번 조금씩 다른 간격
        log.info("다음 확인까지 약 %d분", wait // 60)
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("종료")
