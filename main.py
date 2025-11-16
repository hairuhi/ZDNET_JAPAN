import os
import re
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from googletrans import Translator


# ======== 설정 로드 ========
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL", "https://japan.zdnet.com/software/")
STORAGE_FILE = os.getenv("STORAGE_FILE", "seen_articles.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZDNetCrawler/1.0; +https://github.com/yourname)"
}

# JST (일본 시간)
JST = timezone(timedelta(hours=9))

translator = Translator()


class ConfigError(Exception):
    pass


def ensure_config():
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise ConfigError(
            "환경변수가 부족해요: " + ", ".join(missing)
        )


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


def parse_title_and_datetime(raw_text: str):
    """
    앵커 텍스트에서
    '... 2025-11-16 10:01' 형태의 날짜/시간을 떼어내고
    (제목, datetime) 을 리턴.
    datetime 파싱 실패 시 published_at은 None.
    """
    if not raw_text:
        return "", None

    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*$", raw_text)
    if not m:
        # 날짜가 없으면 제목만 반환
        return raw_text.strip(), None

    dt_str = m.group(1)
    title_part = raw_text[:m.start(1)].strip()
    try:
        published_at = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        published_at = None

    return title_part, published_at


def extract_new_articles(html: str, base_url: str, now_jst: datetime):
    """
    '新着' 섹션에서 지난 24시간 이내 기사만 추출.
    각 아이템: {title_ja_raw, title_ja, url, published_at}
    """
    soup = BeautifulSoup(html, "html.parser")

    # "新着" 헤더 찾기 (h2 / h3)
    header = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and tag.get_text(strip=True).startswith("新着")
    )
    if not header:
        print("[WARN] '新着' 섹션을 찾지 못했어요.")
        return []

    articles = []

    # '新着' 이후 형제들을 훑다가 다른 섹션(예: '読まれている記事') 나오면 중단
    for sibling in header.find_next_siblings():
        if sibling.name in ["h2", "h3"]:
            # 새 섹션 시작 → 종료
            break

        for a in sibling.find_all("a", href=True):
            raw_title = a.get_text(strip=True)
            if not raw_title:
                continue
            # 너무 짧은 텍스트(아이콘 등)는 스킵
            if len(raw_title) < 8:
                continue

            title_ja, published_at = parse_title_and_datetime(raw_title)

            # 지난 24시간 이내 필터
            if published_at is not None:
                if now_jst - published_at > timedelta(hours=24):
                    continue

            url = urljoin(base_url, a["href"])

            articles.append(
                {
                    "title_ja_raw": raw_title,
                    "title_ja": title_ja,
                    "url": url,
                    "published_at": published_at.isoformat() if published_at else None,
                }
            )

    return articles


def translate_title_ja_to_ko(text_ja: str) -> str | None:
    if not text_ja:
        return None
    try:
        result = translator.translate(text_ja, src="ja", dest="ko")
        return result.text
    except Exception as e:
        print(f"구글 번역(무료) 오류: {e}")
        return None


def format_telegram_message(item: dict) -> str:
    title_ja = item.get("title_ja") or item.get("title_ja_raw") or "(제목 없음)"
    title_ko = item.get("title_ko") or "(번역 실패ㅠㅠ)"
    url = item.get("url", "")
    published_at = item.get("published_at")

    if published_at:
        # 보기 좋게 포맷팅
        try:
            dt = datetime.fromisoformat(published_at)
            published_str = dt.astimezone(JST).strftime("%Y-%m-%d %H:%M (%Z)")
        except Exception:
            published_str = published_at
    else:
        published_str = "알 수 없음"

    text = (
        "📰 원문 제목 (JP): " + title_ja + "\n"
        "🇰🇷 번역 제목 (KO): " + title_ko + "\n"
        "🕒 게재 시각: " + published_str + "\n"
        "🔗 URL: " + url
    )
    return text


def send_to_telegram(items: list[dict]):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for item in items:
        text = format_telegram_message(item)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
        }
        try:
            resp = requests.post(api_url, json=payload, timeout=15)
            if not resp.ok:
                print("텔레그램 전송 실패:", resp.status_code, resp.text)
        except Exception as e:
            print("텔레그램 요청 에러:", e)


# ======== 중복 방지용 storage ========
def load_seen_urls(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 리스트 혹은 dict 모두 대비
        if isinstance(data, list):
            return set(data)
        elif isinstance(data, dict):
            return set(data.get("urls", []))
        else:
            return set()
    except Exception as e:
        print(f"[WARN] storage 파일 로드 실패 ({path}): {e}")
        return set()


def save_seen_urls(path: str, urls: set[str]):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(urls)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] storage 파일 저장 실패 ({path}): {e}")


def main():
    ensure_config()

    now_jst = datetime.now(JST)
    print(f"[INFO] 현재(JST): {now_jst.isoformat()}")
    print(f"[INFO] Fetching page: {TARGET_URL}")

    html = fetch_html(TARGET_URL)
    articles = extract_new_articles(html, TARGET_URL, now_jst)

    if not articles:
        print("[INFO] 조건에 맞는 기사가 없어요 (지난 24시간 & 新着).")
        return

    print(f"[INFO] {len(articles)}개의 후보 기사 발견")

    # 중복 방지: 이미 보낸 URL은 제외
    seen = load_seen_urls(STORAGE_FILE)
    print(f"[INFO] storage에서 {len(seen)}개 URL 로드")

    new_articles = [item for item in articles if item["url"] not in seen]

    if not new_articles:
        print("[INFO] 새로 보낼 기사가 없어요 (모두 이미 전송된 URL).")
        return

    print(f"[INFO] 실제 전송 대상: {len(new_articles)}개")

    # 제목 번역
    for item in new_articles:
        ja = item["title_ja"]
        print(f"[INFO] Translating: {ja}")
        ko = translate_title_ja_to_ko(ja)
        item["title_ko"] = ko

    # 텔레그램 전송
    send_to_telegram(new_articles)

    # storage 업데이트
    for item in new_articles:
        seen.add(item["url"])
    save_seen_urls(STORAGE_FILE, seen)

    print("[INFO] 완료!")


if __name__ == "__main__":
    main()
