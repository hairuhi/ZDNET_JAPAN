import os
import re
import json
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from googletrans import Translator


load_dotenv()

# --- 환경 변수 ---

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 기본 URL (필요하면 .env나 GitHub Secrets에서 덮어쓰기 가능)
JAPAN_SOFTWARE_URL = os.getenv(
    "JAPAN_SOFTWARE_URL", "https://japan.zdnet.com/software/"
)
KOREA_AI_URL = os.getenv(
    "KOREA_AI_URL",
    "https://zdnet.co.kr/newskey/?lstcode=%EC%9D%B8%EA%B3%B5%EC%A7%80%EB%8A%A5",
)

# 중복 방지용 스토리지 파일 경로
STORAGE_PATH = os.getenv("STORAGE_PATH", "sent_articles.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ZDNetCrawler/1.0; +https://github.com/yourname)"
}

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
        raise ConfigError("환경변수가 부족해요: " + ", ".join(missing))


# --- 공통 유틸 ---


def fetch_html(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def load_sent_storage() -> dict:
    if not os.path.exists(STORAGE_PATH):
        return {}
    try:
        with open(STORAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception:
        return {}


def save_sent_storage(data: dict):
    with open(STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_within_last_24h(dt: datetime) -> bool:
    """
    기사 시간은 JST/KST(+9) 기준이라고 가정하고,
    현재 UTC에 +9시간을 더한 '로컬 시간'과 비교해요.
    """
    if dt is None:
        return False
    now_local = datetime.utcnow() + timedelta(hours=9)
    cutoff = now_local - timedelta(hours=24)
    return cutoff <= dt <= now_local


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
    source = item.get("source", "")
    url = item.get("url", "")
    published_at = item.get("published_at")

    if source == "zdnet_jp":
        title_ja = item.get("title_ja") or "(제목 없음)"
        title_ko = item.get("title_ko") or "(번역 실패ㅠㅠ)"
        source_label = "🇯🇵 ZDNet Japan (Software)"
        text = (
            f"{source_label}\n"
            f"📰 원문 제목(JP): {title_ja}\n"
            f"🇰🇷 번역 제목(KO): {title_ko}\n"
        )
    elif source == "zdnet_kr_ai":
        title_ko = item.get("title_ko") or "(제목 없음)"
        source_label = "🇰🇷 ZDNet Korea (AI)"
        text = f"{source_label}\n📰 제목: {title_ko}\n"
    else:
        title = item.get("title") or "(제목 없음)"
        source_label = "📰 ZDNet"
        text = f"{source_label}\n제목: {title}\n"

    if isinstance(published_at, datetime):
        text += f"🕒 기사 시각: {published_at.strftime('%Y-%m-%d %H:%M')}\n"

    text += f"🔗 URL: {url}"
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
            resp = requests.post(api_url, json=payload, timeout=20)
            if not resp.ok:
                print("텔레그램 전송 실패:", resp.status_code, resp.text)
        except Exception as e:
            print("텔레그램 요청 에러:", e)


# --- 일본 ZDNet (software) ---


def clean_title_jp(raw_title: str) -> str:
    """
    제목 뒤에 붙은 날짜/시간(예: ' ... 2025-11-16 08:00') 부분 제거.
    """
    if not raw_title:
        return ""
    cleaned = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}.*$", "", raw_title).strip()
    return cleaned


def extract_new_articles_jp_list(html: str, base_url: str) -> list[dict]:
    """
    일본 ZDNet software 페이지에서 '新着' 섹션 위주로 기사 목록을 가져와요.
    여기서는 '제목 + URL'까지만 뽑고, 시간 정보는 기사 본문에서 다시 가져와요.
    """
    soup = BeautifulSoup(html, "html.parser")

    header = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and tag.get_text(strip=True).startswith("新着")
    )
    if not header:
        print("[JP] 新着 섹션을 못 찾았어요 ㅠㅠ")
        return []

    articles: list[dict] = []

    # '新着' 이후 형제들을 돌다가 다른 큰 섹션(h2/h3)이 나오면 종료
    for sibling in header.find_next_siblings():
        if sibling.name in ["h2", "h3"]:
            break

        for a in sibling.find_all("a", href=True):
            title = a.get_text(strip=True)
            if not title:
                continue
            if len(title) < 8:
                continue

            url = urljoin(base_url, a["href"])
            articles.append(
                {
                    "source": "zdnet_jp",
                    "title_ja_raw": title,
                    "title_ja": clean_title_jp(title),
                    "url": url,
                }
            )

    return articles


def fetch_published_at_jp(article_url: str) -> datetime | None:
    """
    일본 ZDNet 기사 본문에서 '2025-11-16 08:00' 같은 형식으로 된 날짜를 찾고 datetime으로 변환.
    """
    try:
        html = fetch_html(article_url)
    except Exception as e:
        print(f"[JP] 기사 페이지 요청 실패: {article_url} ({e})")
        return None

    soup = BeautifulSoup(html, "html.parser")
    # '2025-11-16 08:00' 같은 문자열을 포함한 텍스트 노드 찾기
    text_node = soup.find(string=re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}"))
    if not text_node:
        return None

    m = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", text_node)
    if not m:
        return None

    dt_str = m.group(1)
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        return dt
    except ValueError:
        return None


def collect_recent_articles_jp() -> list[dict]:
    print(f"[JP] Fetching list page: {JAPAN_SOFTWARE_URL}")
    html = fetch_html(JAPAN_SOFTWARE_URL)
    candidates = extract_new_articles_jp_list(html, JAPAN_SOFTWARE_URL)
    print(f"[JP] 후보 기사 {len(candidates)}개 발견")

    recent: list[dict] = []
    for item in candidates:
        url = item["url"]
        dt = fetch_published_at_jp(url)
        if not dt:
            print(f"[JP] 날짜 파싱 실패, 스킵: {url}")
            continue
        item["published_at"] = dt
        if is_within_last_24h(dt):
            recent.append(item)

    print(f"[JP] 지난 24시간 기사 {len(recent)}개")
    return recent


# --- 한국 ZDNet (인공지능 리스트) ---


def extract_new_articles_kr_ai_list(html: str, base_url: str) -> list[dict]:
    """
    인공지능 리스트 페이지에서 기사 제목 + URL만 추출.
    """
    soup = BeautifulSoup(html, "html.parser")

    header = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and "인공지능 최신뉴스" in tag.get_text()
    )
    if not header:
        print("[KR] '인공지능 최신뉴스' 섹션을 못 찾았어요 ㅠㅠ")
        return []

    articles: list[dict] = []

    # '인공지능 최신뉴스' 이후 형제들을 돌다가 '지금 뜨는 기사' 섹션(h2/h3) 나오면 종료
    for sibling in header.find_next_siblings():
        if sibling.name in ["h2", "h3"] and "지금 뜨는 기사" in sibling.get_text():
            break

        for a in sibling.find_all("a", href=True):
            href = a["href"]
            if "/view/?no=" not in href:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            url = urljoin(base_url, href)
            articles.append(
                {
                    "source": "zdnet_kr_ai",
                    "title_ko": title,
                    "url": url,
                }
            )

    return articles


def fetch_published_at_kr(article_url: str) -> datetime | None:
    """
    한국 ZDNet 기사 페이지에서
    '입력 :2025/11/14 17:46    수정: 2025/11/14 17:48'
    같은 부분에서 '입력' 시각을 파싱.
    """
    try:
        html = fetch_html(article_url)
    except Exception as e:
        print(f"[KR] 기사 페이지 요청 실패: {article_url} ({e})")
        return None

    soup = BeautifulSoup(html, "html.parser")
    text_node = soup.find(string=re.compile(r"입력\s*:?\s*\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}"))
    if not text_node:
        return None

    m = re.search(r"입력\s*:?\s*(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})", text_node)
    if not m:
        return None

    dt_str = m.group(1)
    try:
        dt = datetime.strptime(dt_str, "%Y/%m/%d %H:%M")
        return dt
    except ValueError:
        return None


def collect_recent_articles_kr_ai() -> list[dict]:
    print(f"[KR] Fetching AI list page: {KOREA_AI_URL}")
    html = fetch_html(KOREA_AI_URL)
    candidates = extract_new_articles_kr_ai_list(html, KOREA_AI_URL)
    print(f"[KR] 후보 기사 {len(candidates)}개 발견")

    recent: list[dict] = []
    for item in candidates:
        url = item["url"]
        dt = fetch_published_at_kr(url)
        if not dt:
            print(f"[KR] 날짜 파싱 실패, 스킵: {url}")
            continue
        item["published_at"] = dt
        if is_within_last_24h(dt):
            recent.append(item)

    print(f"[KR] 지난 24시간 기사 {len(recent)}개")
    return recent


# --- 메인 로직 ---


def main():
    ensure_config()

    sent_storage = load_sent_storage()
    if not isinstance(sent_storage, dict):
        sent_storage = {}

    # 1) 각 사이트에서 지난 24시간 기사 수집
    jp_articles = collect_recent_articles_jp()
    kr_articles = collect_recent_articles_kr_ai()

    all_candidates: list[dict] = jp_articles + kr_articles
    print(f"[ALL] 총 후보 기사 {len(all_candidates)}개")

    # 2) 중복(이미 보낸 URL) 제거 + 일본 기사 제목 번역
    new_items: list[dict] = []
    for item in all_candidates:
        url = item["url"]
        if url in sent_storage:
            print(f"[SKIP] 이미 전송한 기사라 스킵: {url}")
            continue

        if item.get("source") == "zdnet_jp":
            ja = item.get("title_ja")
            ko = translate_title_ja_to_ko(ja)
            item["title_ko"] = ko

        # 새 기사로 인정 → 스토리지에 기록
        sent_storage[url] = datetime.utcnow().isoformat()
        new_items.append(item)

    print(f"[ALL] 새로 보낼 기사 {len(new_items)}개")

    if not new_items:
        print("[INFO] 보낼 새로운 기사가 없어요.")
        return

    # 3) 텔레그램 전송
    send_to_telegram(new_items)

    # 4) 스토리지 저장
    save_sent_storage(sent_storage)
    print("[INFO] 완료!")


if __name__ == "__main__":
    main()
