# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v9.5 STABLE
# FUNDAMENTAL-FIRST ARCHITECTURE
# =========================================================

import io
import gc
import re
import os
import json
import time
import sqlite3
import requests
import pdfplumber
import yfinance as yf

from datetime import datetime
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = 60
DB_NAME = "pead_results.db"

MIN_PEAD_SCORE = 35
MICROCAP_LIMIT_CR = 500

# =========================================================
# MANUAL TICKER CACHE
# =========================================================

TICKER_CACHE = {
    "GMR Airports": "GMRAIRPORT.NS",
    "Time Technoplast": "TIMETECHNO.NS",
    "Ramky Infrastructure": "RAMKY.NS",
    "Cello World": "CELLO.NS",
    "Axiscades": "AXISCADES.NS"
}

# =========================================================
# OPENAI
# =========================================================

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================================================
# REQUEST SESSION
# =========================================================

session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com"
})

# =========================================================
# DATABASE
# =========================================================

def init_db():

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pead_results (

        news_id TEXT PRIMARY KEY,

        company TEXT,
        ticker TEXT,

        pead_score INTEGER,
        theme TEXT,

        entry_price REAL,
        entry_date TEXT,

        revenue_growth REAL,
        pat_growth REAL,
        qoq_growth REAL,

        price_day_1_pct REAL DEFAULT NULL,
        price_day_3_pct REAL DEFAULT NULL,
        price_day_5_pct REAL DEFAULT NULL,
        price_day_20_pct REAL DEFAULT NULL,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()

# =========================================================
# SAVE SIGNAL
# =========================================================

def save_to_db(
    news_id,
    company,
    ticker,
    pead_score,
    theme,
    entry_price,
    entry_date,
    rev_growth,
    pat_growth,
    qoq_growth
):

    try:

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO pead_results
            (
                news_id,
                company,
                ticker,
                pead_score,
                theme,
                entry_price,
                entry_date,
                revenue_growth,
                pat_growth,
                qoq_growth
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            news_id,
            company,
            ticker,
            pead_score,
            theme,
            entry_price,
            entry_date,
            rev_growth,
            pat_growth,
            qoq_growth
        ))

        conn.commit()
        conn.close()

    except Exception as e:
        print("DB Save Error:", e)

# =========================================================
# FORWARD RETURN TRACKER
# =========================================================

def update_future_returns():

    print("\n🔄 Running Forward Return Tracker...")

    try:

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()

        cur.execute("""
            SELECT
                news_id,
                ticker,
                entry_price,
                entry_date,
                price_day_1_pct,
                price_day_3_pct,
                price_day_5_pct,
                price_day_20_pct
            FROM pead_results
            WHERE ticker IS NOT NULL
        """)

        rows = cur.fetchall()

        today = datetime.now()

        for row in rows:

            (
                news_id,
                ticker,
                entry_price,
                entry_date_str,
                d1,
                d3,
                d5,
                d20
            ) = row

            if not entry_price or entry_price <= 0:
                continue

            entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d")

            days_elapsed = (today - entry_date).days

            if days_elapsed < 1:
                continue

            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")

            if hist.empty:
                continue

            current_price = hist["Close"].iloc[-1]

            pct_return = (
                (current_price - entry_price)
                / entry_price
            ) * 100

            if days_elapsed >= 1 and d1 is None:
                cur.execute("""
                    UPDATE pead_results
                    SET price_day_1_pct = ?
                    WHERE news_id = ?
                """, (pct_return, news_id))

            if days_elapsed >= 3 and d3 is None:
                cur.execute("""
                    UPDATE pead_results
                    SET price_day_3_pct = ?
                    WHERE news_id = ?
                """, (pct_return, news_id))

            if days_elapsed >= 5 and d5 is None:
                cur.execute("""
                    UPDATE pead_results
                    SET price_day_5_pct = ?
                    WHERE news_id = ?
                """, (pct_return, news_id))

            if days_elapsed >= 20 and d20 is None:
                cur.execute("""
                    UPDATE pead_results
                    SET price_day_20_pct = ?
                    WHERE news_id = ?
                """, (pct_return, news_id))

        conn.commit()
        conn.close()

        print("✅ Forward returns updated successfully.")

    except Exception as e:
        print("Forward Tracker Error:", e)

# =========================================================
# TELEGRAM
# =========================================================

def send_telegram_message(msg):

    try:

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:3500]
            },
            timeout=20
        )

    except Exception as e:
        print("Telegram Message Error:", e)

def send_telegram_photo(image_bytes, caption):

    try:

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"

        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:1000]
            },
            files={
                "photo": (
                    "dashboard.png",
                    image_bytes,
                    "image/png"
                )
            },
            timeout=30
        )

        return response.status_code == 200

    except Exception as e:
        print("Telegram Photo Error:", e)
        return False

# =========================================================
# LIVE MARKET DATA
# =========================================================

def get_live_stock_data(company_name):

    try:

        for cached_name, symbol in TICKER_CACHE.items():

            if cached_name.lower() in company_name.lower():

                stock = yf.Ticker(symbol)
                hist = stock.history(period="1d")

                if not hist.empty:

                    return (
                        symbol,
                        hist["Close"].iloc[-1]
                    )

        search = yf.Search(company_name + " NSE")

        if not search.quotes:
            return None, 0.0

        symbol = search.quotes[0]["symbol"]

        if not symbol.endswith(".NS"):
            symbol += ".NS"

        stock = yf.Ticker(symbol)

        hist = stock.history(period="1d")

        if hist.empty:
            return symbol, 0.0

        return (
            symbol,
            hist["Close"].iloc[-1]
        )

    except Exception as e:

        print("Live Data Error:", e)

        return None, 0.0

# =========================================================
# MICROCAP FILTER
# =========================================================

def is_microcap(company_name):

    try:

        symbol, _ = get_live_stock_data(company_name)

        if not symbol:
            return False

        stock = yf.Ticker(symbol)

        market_cap = stock.info.get("marketCap")

        if not market_cap:
            return False

        market_cap_cr = market_cap / 10000000

        print(f"Market Cap: ₹{market_cap_cr:.0f} Cr")

        return market_cap_cr < MICROCAP_LIMIT_CR

    except Exception as e:

        print("Market Cap Error:", e)

        return False

# =========================================================
# LIGHT TECHNICAL BONUS
# =========================================================

def get_technical_bonus(ticker):

    try:

        if not ticker:
            return 0

        stock = yf.Ticker(ticker)

        hist = stock.history(period="6mo")

        if len(hist) < 70:
            return 0

        lookback = min(63, len(hist)-1)

        current_price = hist["Close"].iloc[-1]

        old_price = hist["Close"].iloc[-lookback]

        stock_return = (
            (current_price - old_price)
            / old_price
        ) * 100

        bonus = 0

        if stock_return > 15:
            bonus += 5

        avg_vol = hist["Volume"].tail(20).mean()

        current_vol = hist["Volume"].iloc[-1]

        if current_vol > (2 * avg_vol):
            bonus += 5

        dma = hist["Close"].mean()

        if current_price > dma:
            bonus += 3

        return bonus

    except Exception as e:

        print("Technical Error:", e)

        return 0

# =========================================================
# BSE FETCH
# =========================================================

def fetch_latest_results():

    today = time.strftime("%Y%m%d")

    params = {
        "pageno": 1,
        "strCat": "Result",
        "strPrevDate": today,
        "strToDate": today,
        "strScrip": "",
        "strSearch": "P",
        "strType": "C",
        "subcategory": "-1"
    }

    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

    try:

        response = session.get(
            url,
            params=params,
            timeout=20
        )

        return response.json().get("Table", [])

    except Exception as e:

        print("BSE Fetch Error:", e)

        return []

# =========================================================
# PDF
# =========================================================

def download_pdf(pdf_url):

    try:

        response = session.get(pdf_url, timeout=20)

        response.raise_for_status()

        return response.content

    except Exception as e:

        print("PDF Download Error:", e)

        return None

def extract_financial_pages(pdf_bytes):

    keywords = [
        "revenue from operations",
        "statement of audited financial results",
        "profit for the period",
        "profit before tax",
        "ebitda"
    ]

    extracted_text = ""

    try:

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

            for idx, page in enumerate(pdf.pages):

                text = page.extract_text()

                if not text:
                    continue

                if any(k in text.lower() for k in keywords):

                    print(f"✅ Financial Page Detected: Page {idx+1}")

                    extracted_text += text

                    if len(extracted_text) > 12000:
                        break

    except Exception as e:

        print("PDF Extract Error:", e)

    return extracted_text

# =========================================================
# REGEX EXTRACTION
# =========================================================

def regex_extract(financial_text):

    rev_match = re.search(
        r'revenue.*?(\d+(?:\.\d+)?)\s*%',
        financial_text,
        re.IGNORECASE
    )

    pat_match = re.search(
        r'profit.*?(\d+(?:\.\d+)?)\s*%',
        financial_text,
        re.IGNORECASE
    )

    if rev_match and pat_match:

        return {
            "company_name": "Regex Extraction",
            "quarter": "Latest",
            "sector": "General",
            "industry": "General",

            "revenue_yoy_growth_pct":
                float(rev_match.group(1)),

            "pat_yoy_growth_pct":
                float(pat_match.group(1)),

            "pat_qoq_growth_pct": None,

            "ebitda_margin_pct": None,

            "management_commentary":
                "Regex fast extraction",

            "order_book": "",
            "red_flags": ""
        }

    return None

# =========================================================
# GPT EXTRACTION
# =========================================================

def gpt_extract(financial_text):

    prompt = f"""
    Extract latest quarterly earnings.

    Return ONLY JSON.

    Use null if data unavailable.

    Infer probable sector and industry.

    PAT extraction is extremely important.

    Summarize management commentary in 1 sentence.

    {{
        "company_name": "",
        "quarter": "",

        "sector": "",
        "industry": "",

        "revenue_yoy_growth_pct": null,
        "pat_yoy_growth_pct": null,
        "pat_qoq_growth_pct": null,

        "ebitda_margin_pct": null,

        "management_commentary": "",
        "order_book": "",
        "red_flags": ""
    }}

    DOCUMENT:
    {financial_text[:12000]}
    """

    try:

        response = client.chat.completions.create(

            model="gpt-4o-mini",

            messages=[
                {
                    "role": "system",
                    "content": "You are a financial extraction engine."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],

            temperature=0
        )

        content = (
            response
            .choices[0]
            .message
            .content
            .replace("```json", "")
            .replace("```", "")
        )

        return json.loads(content)

    except Exception as e:

        print("GPT Error:", e)

        return None

# =========================================================
# VALIDATION
# =========================================================

def validate(data):

    revenue = data.get("revenue_yoy_growth_pct")
    pat = data.get("pat_yoy_growth_pct")
    qoq = data.get("pat_qoq_growth_pct")

    if revenue is None:
        return False

    if pat is None and qoq is None:
        return False

    return True

# =========================================================
# THEMATIC ENGINE
# =========================================================

def identify_theme_and_score(sector, industry):

    text = f"{sector} {industry}".lower()

    themes = {

        "Defense & Aerospace": {
            "keywords": [
                "defense",
                "defence",
                "drone",
                "shipyard",
                "aviation"
            ],
            "score": 10
        },

        "Power & Energy": {
            "keywords": [
                "power",
                "energy",
                "transmission",
                "solar",
                "grid"
            ],
            "score": 10
        },

        "Infrastructure & Capital Goods": {
            "keywords": [
                "infra",
                "infrastructure",
                "railway",
                "construction"
            ],
            "score": 8
        },

        "EMS & Electronics": {
            "keywords": [
                "electronics",
                "ems",
                "semiconductor"
            ],
            "score": 8
        },

        "Healthcare & Pharma": {
            "keywords": [
                "pharma",
                "healthcare",
                "api"
            ],
            "score": 5
        }
    }

    for theme_name, data in themes.items():

        if any(k in text for k in data["keywords"]):

            return (
                theme_name,
                data["score"]
            )

    return (
        "General / Other",
        0
    )

# =========================================================
# PEAD SCORE
# =========================================================

def calculate_pead(data, theme_score):

    score = 0

    rev_growth = data.get(
        "revenue_yoy_growth_pct"
    ) or 0

    pat_growth = data.get(
        "pat_yoy_growth_pct"
    ) or 0

    qoq_growth = data.get(
        "pat_qoq_growth_pct"
    ) or 0

    if abs(rev_growth) > 300:
        rev_growth = 0

    if abs(pat_growth) > 500:
        pat_growth = 0

    if abs(qoq_growth) > 300:
        qoq_growth = 0

    # Revenue

    if rev_growth > 30:
        score += 15

    elif rev_growth > 15:
        score += 10

    elif rev_growth > 8:
        score += 5

    # PAT

    if pat_growth > 50:
        score += 25

    elif pat_growth > 20:
        score += 15

    elif pat_growth > 10:
        score += 5

    # QoQ

    if qoq_growth > 15:
        score += 10

    # EBITDA

    if (data.get("ebitda_margin_pct") or 0) > 20:
        score += 8

    # Theme

    score += theme_score

    return {
        "score": max(min(score, 100), 0),
        "rev_growth": rev_growth,
        "pat_growth": pat_growth,
        "qoq_growth": qoq_growth
    }

# =========================================================
# PEAD GRADE
# =========================================================

def get_pead_grade(score):

    if score >= 80:
        return "Institutional Monster 🚀"

    elif score >= 65:
        return "High Momentum 🔥"

    elif score >= 45:
        return "Strong PEAD 📈"

    return "Normal 📊"

# =========================================================
# DASHBOARD
# =========================================================

def generate_dashboard(company_name, pead, theme):

    img = Image.new(
        "RGB",
        (900, 520),
        (15, 23, 42)
    )

    draw = ImageDraw.Draw(img)

    font = ImageFont.load_default()

    white = (255,255,255)
    green = (34,197,94)
    cyan = (45,212,191)

    draw.text(
        (30,30),
        company_name,
        fill=white,
        font=font
    )

    draw.text(
        (30,80),
        f"Theme: {theme}",
        fill=cyan,
        font=font
    )

    draw.text(
        (650,30),
        f"PEAD {pead['score']}",
        fill=cyan,
        font=font
    )

    draw.text(
        (30,180),
        f"Revenue Growth: {pead['rev_growth']:+.1f}%",
        fill=green,
        font=font
    )

    draw.text(
        (30,260),
        f"PAT Growth: {pead['pat_growth']:+.1f}%",
        fill=green,
        font=font
    )

    draw.text(
        (30,340),
        f"QoQ PAT: {pead['qoq_growth']:+.1f}%",
        fill=green,
        font=font
    )

    img_bytes = io.BytesIO()

    img.save(img_bytes, format="PNG")

    img_bytes.seek(0)

    return img_bytes

# =========================================================
# MAIN LOOP
# =========================================================

seen = set()

def main():

    init_db()

    print("=" * 60)
    print("🚀 GPT PEAD ENGINE v9.5 STABLE")
    print("=" * 60)

    cycle = 0

    while True:

        try:

            # Run tracker every 30 cycles
            if cycle % 30 == 0:
                update_future_returns()

            results = fetch_latest_results()

            for item in results:

                news_id = item.get("NEWSID")

                if not news_id:
                    continue

                if news_id in seen:
                    continue

                seen.add(news_id)

                company = item.get(
                    "SLONGNAME",
                    "Unknown"
                )

                attachment = item.get(
                    "ATTACHMENTNAME",
                    ""
                )

                if not attachment.endswith(".pdf"):
                    continue

                print(f"\n{'=' * 60}")
                print(f"🚀 NEW RESULT: {company}")
                print("=" * 60)

                if is_microcap(company):

                    print(
                        f"[REJECTED] {company} is a Microcap."
                    )

                    continue

                pdf_url = (
                    "https://www.bseindia.com/xml-data/"
                    f"corpfiling/AttachLive/{attachment}"
                )

                pdf_bytes = download_pdf(pdf_url)

                if not pdf_bytes:
                    continue

                financial_text = extract_financial_pages(
                    pdf_bytes
                )

                if len(financial_text) < 500:

                    print("❌ No Financial Data")

                    continue

                data = regex_extract(financial_text)

                if not data:

                    print(
                        "⚠️ Regex Missed. "
                        "Falling back to GPT..."
                    )

                    data = gpt_extract(financial_text)

                if not data:

                    print("❌ Extraction Failed")

                    continue

                print("\n🔍 DEBUG DATA")
                print(json.dumps(data, indent=2))

                if not validate(data):

                    print("❌ Validation Failed")

                    continue

                theme, theme_score = (
                    identify_theme_and_score(
                        str(data.get("sector")),
                        str(data.get("industry"))
                    )
                )

                pead = calculate_pead(
                    data,
                    theme_score
                )

                ticker, entry_price = (
                    get_live_stock_data(company)
                )

                tech_bonus = get_technical_bonus(
                    ticker
                )

                final_score = (
                    pead["score"]
                    + tech_bonus
                )

                print(
                    f"DEBUG SCORE -> "
                    f"Funda: {pead['score']} | "
                    f"Tech Bonus: {tech_bonus} | "
                    f"Final: {final_score}"
                )

                if final_score < MIN_PEAD_SCORE:

                    print("❌ Low PEAD Score")

                    continue

                entry_date = (
                    datetime.now()
                    .strftime("%Y-%m-%d")
                )

                save_to_db(
                    news_id,
                    company,
                    ticker,
                    final_score,
                    theme,
                    entry_price,
                    entry_date,
                    pead["rev_growth"],
                    pead["pat_growth"],
                    pead["qoq_growth"]
                )

                dashboard = generate_dashboard(
                    company,
                    pead,
                    theme
                )

                grade = get_pead_grade(
                    final_score
                )

                quarter = data.get(
                    "quarter",
                    "Latest Quarter"
                )

                caption = (
                    f"🎯 {company}\n\n"
                    f"{grade}\n\n"

                    f"📊 {quarter}\n"

                    f"Revenue YoY: "
                    f"{pead['rev_growth']:+.1f}%\n"

                    f"PAT YoY: "
                    f"{pead['pat_growth']:+.1f}%\n"

                    f"PAT QoQ: "
                    f"{pead['qoq_growth']:+.1f}%\n"

                    f"EBITDA Margin: "
                    f"{data.get('ebitda_margin_pct') or 0}%\n\n"

                    f"Theme: {theme}\n"

                    f"Technical Bonus: "
                    f"+{tech_bonus}\n\n"

                    f"Final PEAD Score: "
                    f"{final_score}"
                )

                print("📤 Sending Telegram Alert...")

                if send_telegram_photo(
                    dashboard,
                    caption
                ):

                    print("✅ PHOTO SENT")

                    commentary = (
                        f"Commentary:\n\n"
                        f"{data.get('management_commentary', '')}\n\n"

                        f"Order Book:\n\n"
                        f"{data.get('order_book', '')}\n\n"

                        f"Red Flags:\n\n"
                        f"{data.get('red_flags', '')}"
                    )

                    send_telegram_message(commentary)

                    print("✅ COMMENTARY SENT")

                gc.collect()

                time.sleep(2)

            print(
                f"\n[{time.strftime('%H:%M:%S')}] "
                f"Alive | Seen={len(seen)}"
            )

            cycle += 1

        except Exception as e:

            print("MAIN LOOP ERROR:", e)

        gc.collect()

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
