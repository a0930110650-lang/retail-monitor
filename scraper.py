import json
import os
import random
import re
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup
from curl_cffi import requests
import pandas as pd

# ================= 1. 參數設定區 =================
DAYS_LIMIT = 3
CUTOFF_TIME = datetime.now() - timedelta(days=DAYS_LIMIT)
CUTOFF_TS = int(CUTOFF_TIME.timestamp())

TARGET_CHANNELS = [
    "萬家福", "樂家康", "家樂福", "Mia C'bon",
    "全聯", "大全聯", "美廉社", "LOPIA",
    "7-11", "全家", "萊爾富", "OK超商",
    "酷澎", "好市多", "愛買", "大潤發"
]

PTT_BOARDS = ["CVS", "hypermall", "Lifeismoney", "Salary", "Gossiping"]

DCARD_QUERIES = [
    'site:dcard.tw ("萬家福" OR "樂家康" OR "家樂福" OR "Mia C\'bon")',
    'site:dcard.tw ("全聯" OR "大全聯" OR "美廉社" OR "LOPIA")',
    'site:dcard.tw ("7-11" OR "全家" OR "萊爾富" OR "OK超商") (員工 OR 排班 OR 奧客 OR 價格 OR 漲價)',
    'site:dcard.tw ("好市多" OR "酷澎" OR "a.mart" OR "愛買量販" OR "遠東愛買" OR "大潤發") (價格 OR 漲價 OR 退貨 OR 抱怨)'
]

PRICE_KEYWORDS = [
    "價格", "價錢", "多少錢", "好貴", "變貴", "漲價", "太貴", "標價", "標錯價",
    "結帳金額", "算錯錢", "多算", "優惠", "折扣", "特價", "買一送一", "點數",
    "CP值", "cp值", "縮水", "價差", "退差價", "刷錯", "原價", "促銷", "貴死", "划算"
]

EMPLOYEE_KEYWORDS = [
    "排班", "工時", "時數", "薪水", "時薪", "月薪", "獎金", "加班", "離職",
    "提離職", "店長", "課長", "主管", "同事", "新人", "PT", "pt", "正職",
    "兼職", "理貨", "補貨", "盤點", "站收銀", "奧客", "人手不足", "人力",
    "班表", "特休", "勞基法", "打卡", "業績壓力", "制服", "支援收銀", "靠北", "面試"
]

AD_NOISE_PATTERNS = [
    "團購", "代購", "開團", "結單", "留言區下單", "蝦皮賣場", "賣貨便", "好賣+", "抽獎活動"
]

TARGET_FOUR_BRANDS = ["萬家福", "家樂福", "樂家康", "Mia C'bon", "miacbon", "MIA C'BON"]

# ================= 2. 語意消歧過濾器 =================
AMART_STRICT_BRAND_WORDS = [
    "a.mart", "amart", "愛買量販", "遠東愛買", "happy go", "遠東百貨", "遠百",
    "南雅店", "忠孝店", "景美店", "三重店", "永和店", "桃園店", "楊梅店",
    "新竹店", "巨城店", "豐原店", "復興店", "水湳店", "員林店", "台南店", "花蓮店"
]

AMART_VALID_PHRASE = re.compile(
    r'((去|逛|在|到|跑去|衝去|路過)愛買|愛買(量販|賣場|超市|分店|門市|南雅|忠孝|景美|三重|永和|桃園|楊梅|新竹|巨城|豐原|復興|水湳|員林|台南|花蓮|DM|dm|APP|app|特價|周年慶|週年慶|員工|店員|收銀|客服|退貨))'
)

AMART_VERB_EXCLUDE = re.compile(
    r'((超|很|太|真|好|超級|非常|天生|就是|一樣|都)愛買|愛買(東西|衣服|衣|褲|鞋|包|化妝品|保養品|網拍|零食|玩具|書|精品|飾品|雜物|家電|3C|的女生|的男生|的人|一族|族|狂|魔|又|嗎|啊|喔|耶))'
)

FAMILYMART_CONTEXT_WORDS = [
    "familymart", "超商", "便利商店", "門市", "店員", "店長", "咖啡", "let's café",
    "霜淇淋", "友善食光", "取貨", "寄件", "店到店", "fami", "酷碰券", "點數", "7-11", "萊爾富", "ok"
]

FAMILYMART_NOISE_PATTERN = re.compile(
    r'全家(出遊|旅遊|福|大小|人|聚餐|吃飯|一起|移民|搬家|確診|平安|健康)'
)

def is_valid_channel_post(text, keyword=""):
    text_lower = text.lower()
    if "愛買" in text or keyword == "愛買":
        has_strict_brand = any(w in text_lower for w in AMART_STRICT_BRAND_WORDS)
        has_valid_phrase = bool(AMART_VALID_PHRASE.search(text))
        is_verb = bool(AMART_VERB_EXCLUDE.search(text))
        if is_verb and not has_strict_brand:
            return False
        if not (has_strict_brand or has_valid_phrase):
            return False

    if "全家" in text or keyword == "全家":
        has_fm_ctx = any(w in text_lower for w in FAMILYMART_CONTEXT_WORDS)
        if FAMILYMART_NOISE_PATTERN.search(text) and not has_fm_ctx:
            return False
        if "全家" in text and not has_fm_ctx:
            return False

    return True

def classify_three_buckets(text, source_name):
    text_lower = text.lower()
    source_lower = source_name.lower()
    matched_four = [b for b in TARGET_FOUR_BRANDS if b.lower() in text_lower or b.lower() in source_lower]
    brand_tag = f"重點四通路({','.join(set(matched_four))})" if matched_four else "其他通路/綜合"
    if any(pk in text for pk in PRICE_KEYWORDS):
        return "價格方面問題", brand_tag
    if "靠北" in text or "Salary" in source_name or any(ek in text for ek in EMPLOYEE_KEYWORDS):
        return "員工抱怨與建議", brand_tag
    return "顧客抱怨與建議", brand_tag

# ================= 3. 三平台抓取函式 =================
def fetch_all_threads(session):
    results = []
    for kw in TARGET_CHANNELS:
        search_queries = ["愛買量販", "愛買"] if kw == "愛買" else [kw]
        for sq in search_queries:
            encoded_kw = urllib.parse.quote(sq)
            url = f"https://www.threads.com/search?q={encoded_kw}&serp_type=default"
            try:
                resp = session.get(url, impersonate="chrome131", timeout=15)
                if resp.status_code == 200:
                    blocks = re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', resp.text)
                    for b in blocks:
                        if "caption" in b and "code" in b:
                            _parse_threads_json(json.loads(b), kw, results)
            except Exception:
                pass
            time.sleep(random.uniform(1.8, 2.8))
    return results

def _parse_threads_json(obj, keyword, results):
    if isinstance(obj, dict):
        if "code" in obj and "caption" in obj and isinstance(obj["caption"], dict):
            text = obj["caption"].get("text", "").strip()
            code = obj.get("code", "")
            taken_at = obj.get("taken_at", 0)
            username = obj.get("user", {}).get("username", "unknown")
            if text and code and taken_at and taken_at >= CUTOFF_TS:
                if any(ad in text for ad in AD_NOISE_PATTERNS):
                    return
                if not is_valid_channel_post(text, keyword):
                    return
                post_url = f"https://www.threads.com/@{username}/post/{code}"
                if not any(r["原文連結"] == post_url for r in results):
                    cat, b_tag = classify_three_buckets(text, keyword)
                    results.append({
                        "分類": cat,
                        "通路標註": b_tag,
                        "平台": "Threads",
                        "來源/看板": keyword,
                        "發布時間": datetime.fromtimestamp(taken_at).strftime("%Y-%m-%d %H:%M"),
                        "貼文內容": text.replace("\n", " "),
                        "原文連結": post_url
                    })
        for v in obj.values():
            _parse_threads_json(v, keyword, results)
    elif isinstance(obj, list):
        for item in obj:
            _parse_threads_json(item, keyword, results)

def parse_ptt_date(date_str):
    now = datetime.now()
    parts = date_str.strip().split("/")
    if len(parts) != 2:
        return None
    month, day = int(parts[0]), int(parts[1])
    year = now.year if month <= now.month else now.year - 1
    try:
        return datetime(year, month, day, 23, 59)
    except Exception:
        return None

def fetch_all_ptt(session):
    results = []
    cookies = {"over18": "1"}
    for board in PTT_BOARDS:
        for kw in TARGET_CHANNELS:
            encoded_kw = urllib.parse.quote(kw)
            search_url = f"https://www.ptt.cc/bbs/{board}/search?q={encoded_kw}"
            try:
                resp = session.get(search_url, cookies=cookies, impersonate="chrome131", timeout=12)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                for ent in soup.select("div.r-ent"):
                    title_a = ent.select_one("div.title a")
                    date_div = ent.select_one("div.date")
                    if not title_a or not date_div:
                        continue
                    post_dt = parse_ptt_date(date_div.get_text(strip=True))
                    if post_dt and post_dt < CUTOFF_TIME:
                        continue
                    title = title_a.get_text(strip=True)
                    href = title_a["href"]
                    ptt_url = "https://www.ptt.cc" + href
                    if any(r["原文連結"] == ptt_url for r in results):
                        continue
                    body_txt = ""
                    try:
                        art_resp = session.get(ptt_url, cookies=cookies, impersonate="chrome131", timeout=10)
                        if art_resp.status_code == 200:
                            art_soup = BeautifulSoup(art_resp.text, "html.parser")
                            mc = art_soup.select_one("#main-content")
                            if mc:
                                for tag in mc.select("div.article-metaline, div.article-metaline-right, div.push"):
                                    tag.decompose()
                                body_txt = mc.get_text(" ", strip=True).split("--")[0].strip()
                    except Exception:
                        pass
                    full_txt = f"{title}｜{body_txt}" if body_txt else title
                    if not is_valid_channel_post(full_txt, kw):
                        continue
                    cat, b_tag = classify_three_buckets(full_txt, f"PTT-{board}")
                    results.append({
                        "分類": cat,
                        "通路標註": b_tag,
                        "平台": "PTT",
                        "來源/看板": f"PTT {board}版 ({kw})",
                        "發布時間": post_dt.strftime("%Y-%m-%d") if post_dt else "近3日內",
                        "貼文內容": full_txt[:800],
                        "原文連結": ptt_url
                    })
            except Exception:
                pass
            time.sleep(0.3)
    return results

def fetch_all_dcard(session):
    results = []
    search_url = "https://html.duckduckgo.com/html/"
    for q in DCARD_QUERIES:
        try:
            resp = session.post(search_url, data={"q": q, "df": "w"}, impersonate="chrome131", timeout=15)
            soup = BeautifulSoup(resp.text, "html.parser")
            for res in soup.select(".result"):
                title_el = res.select_one(".result__a")
                snip_el = res.select_one(".result__snippet")
                if not title_el or not snip_el:
                    continue
                raw_href = title_el.get("href", "")
                m_url = re.search(r'uddg=([^&]+)', raw_href)
                real_url = urllib.parse.unquote(m_url.group(1)) if m_url else raw_href
                if "dcard.tw" not in real_url:
                    continue
                title_txt = title_el.get_text(strip=True)
                snip_txt = snip_el.get_text(strip=True)
                full_txt = f"{title_txt}｜{snip_txt}"
                if any(ad in full_txt for ad in AD_NOISE_PATTERNS):
                    continue
                if not is_valid_channel_post(full_txt, "Dcard"):
                    continue
                if re.search(r'([4-9]|[1-9]\d)\s*天前', snip_txt):
                    continue
                if not any(r["原文連結"] == real_url for r in results):
                    cat, b_tag = classify_three_buckets(full_txt, "Dcard")
                    results.append({
                        "分類": cat,
                        "通路標註": b_tag,
                        "平台": "Dcard",
                        "來源/看板": "Dcard 討論區",
                        "發布時間": "近期",
                        "貼文內容": full_txt[:800],
                        "原文連結": real_url
                    })
        except Exception:
            pass
        time.sleep(random.uniform(1.5, 2.5))
    return results

# ================= 4. 自動寄送 Email（夾帶完整 Excel 報表） =================
def send_email_with_excel(df_price, df_employee, df_customer, filename):
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    receivers = os.environ.get("RECEIVER_EMAILS", "")

    if not (gmail_user and gmail_pass and receivers):
        print("尚未設定完整 Gmail 寄信參數，略過寄信。")
        return

    today_tw = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    total_count = len(df_price) + len(df_employee) + len(df_customer)

    preview_lines = []
    for label, sub_df in [("價格方面問題", df_price), ("員工抱怨與建議", df_employee), ("顧客抱怨與建議", df_customer)]:
        for _, row in sub_df.head(3).iterrows():
            snippet = row["貼文內容"][:60].replace("\n", " ")
            preview_lines.append(f"<li><b>[{label} / {row['來源/看板']}]</b> {snippet}... (<a href='{row['原文連結']}'>原文</a>)</li>")
    preview_html = "".join(preview_lines) if preview_lines else "<li>今日無新增重點摘要。</li>"

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = receivers
    msg["Subject"] = f"【每日零售輿情日報】三分類彙整（共 {total_count} 筆） - {today_tw[:10]}"

    html_body = f"""
    <html>
      <body style="font-family: 'Microsoft JhengHei', sans-serif; line-height: 1.6;">
        <h3>📊 零售通路每日輿情監測日報 ({today_tw})</h3>
        <p>過去 3 天內共彙整 <b>{total_count} 筆</b>有效討論（涵蓋 Threads / PTT / Dcard，已排除團購廣告與動詞誤判）：</p>
        <ul>
          <li><b>1. 價格方面問題</b>：{len(df_price)} 筆</li>
          <li><b>2. 員工抱怨與建議</b>：{len(df_employee)} 筆</li>
          <li><b>3. 顧客抱怨與建議</b>：{len(df_customer)} 筆</li>
        </ul>
        <h4>📌 今日重點貼文速覽：</h4>
        <ul>{preview_html}</ul>
        <p>完整明細與原文超連結請直接下載本信件夾帶之 Excel 檔案：<b>{filename}</b></p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with open(filename, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={filename}")
    msg.attach(part)

    receiver_list = [r.strip() for r in receivers.split(",") if r.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, receiver_list, msg.as_string())
    print(f"已成功將日報與 Excel 附件寄送至：{receivers}")

if __name__ == "__main__":
    session = requests.Session()
    all_records = fetch_all_threads(session) + fetch_all_ptt(session) + fetch_all_dcard(session)
    
    df_all = pd.DataFrame(all_records).drop_duplicates(subset=["原文連結"]) if all_records else pd.DataFrame(columns=["分類", "通路標註", "平台", "來源/看板", "發布時間", "貼文內容", "原文連結"])
    df_price = df_all[df_all["分類"] == "價格方面問題"].copy()
    df_employee = df_all[df_all["分類"] == "員工抱怨與建議"].copy()
    df_customer = df_all[df_all["分類"] == "顧客抱怨與建議"].copy()

    date_tag = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y%m%d")
    filename = f"Retail_Monitor_Report_{date_tag}.xlsx"
    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        df_price.to_excel(writer, sheet_name="1_價格方面問題", index=False)
        df_employee.to_excel(writer, sheet_name="2_員工抱怨與建議", index=False)
        df_customer.to_excel(writer, sheet_name="3_顧客抱怨與建議", index=False)

    send_email_with_excel(df_price, df_employee, df_customer, filename)
