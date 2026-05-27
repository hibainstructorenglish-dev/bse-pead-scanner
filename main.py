# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v9.0 (MASTER + BACKTESTER)
# =========================================================

import io
import gc
import re
import json
import time
import sqlite3
import requests
import pdfplumber
import yfinance as yf
from datetime import datetime, timedelta

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# =========================================================
# CONFIG
# =========================================================
TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"

OPENAI_API_KEY = "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"

CHECK_INTERVAL = 60
DB_NAME = "pead_results.db"
MIN_PEAD_SCORE = 35
MICROCAP_LIMIT_CR = 500

# =========================================================
# OPENAI & REQUESTS
# =========================================================
client = OpenAI(api_key=OPENAI_API_KEY)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com"
})

# =========================================================
# DATABASE (With Forward Returns Schema)
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

def save_to_db(news_id, company, ticker, pead_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth):
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO pead_results 
            (news_id, company, ticker, pead_score, theme, entry_price, entry_date, revenue_growth, pat_growth, qoq_growth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (news_id, company, ticker, pead_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB Save Error:", e)

# =========================================================
# FORWARD RETURN TRACKER (The Institutional Alpha)
# =========================================================
def update_future_returns():
    print("\n🔄 Running Forward Return Tracker...")
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT news_id, ticker, entry_price, entry_date, 
                   price_day_1_pct, price_day_3_pct, price_day_5_pct, price_day_20_pct 
            FROM pead_results 
            WHERE price_day_20_pct IS NULL AND ticker IS NOT NULL
        """)
        pending_records = cur.fetchall()
        
        today = datetime.now()
        
        for record in pending_records:
            news_id, ticker, entry_price, entry_date_str, d1, d3, d5, d20 = record
            
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
            current_price = hist['Close'].iloc[-1]
            
            pct_return = ((current_price - entry_price) / entry_price) * 100
            
            if days_elapsed >= 1 and d1 is None:
                cur.execute("UPDATE pead_results SET price_day_1_pct = ? WHERE news_id = ?", (pct_return, news_id))
            if days_elapsed >= 3 and d3 is None:
                cur.execute("UPDATE pead_results SET price_day_3_pct = ? WHERE news_id = ?", (pct_return, news_id))
            if days_elapsed >= 5 and d5 is None:
                cur.execute("UPDATE pead_results SET price_day_5_pct = ? WHERE news_id = ?", (pct_return, news_id))
            if days_elapsed >= 20 and d20 is None:
                cur.execute("UPDATE pead_results SET price_day_20_pct = ? WHERE news_id = ?", (pct_return, news_id))
                
        conn.commit()
        conn.close()
        print("✅ Forward returns updated successfully.")
    except Exception as e:
        print("Update Returns Error:", e)

# =========================================================
# TELEGRAM
# =========================================================
def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:3500]}, timeout=20)
        print(f"TELEGRAM MESSAGE: {response.status_code}")
    except Exception as e:
        print("Telegram Message Error:", e)

def send_telegram_photo(image_bytes, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        response = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
            files={"photo": ("dashboard.png", image_bytes, "image/png")},
            timeout=30
        )
        return response.status_code == 200
    except Exception as e:
        print("Telegram Photo Error:", e)
        return False

# =========================================================
# MARKET DATA HELPERS
# =========================================================
def get_live_stock_data(company_name):
    try:
        search = yf.Search(company_name)
        if not search.quotes: return None, 0.0
        
        symbol = search.quotes[0]["symbol"]
        stock = yf.Ticker(symbol)
        hist = stock.history(period="1d")
        if hist.empty: return symbol, 0.0
            
        current_price = hist['Close'].iloc[-1]
        return symbol, current_price
    except Exception as e:
        print(f"Pricing Fetch Error for {company_name}:", e)
        return None, 0.0

def is_microcap(company_name):
    try:
        symbol, _ = get_live_stock_data(company_name)
        if not symbol: return False
        
        stock = yf.Ticker(symbol)
        market_cap_cr = stock.info.get("marketCap", 0) / 10000000
        print(f"Market Cap: ₹{market_cap_cr:.0f} Cr")
        return market_cap_cr < MICROCAP_LIMIT_CR
    except Exception as e:
        print("Market Cap Error:", e)
        return False

# =========================================================
# BSE FETCHING
# =========================================================
def fetch_latest_results():
    today = time.strftime("%Y%m%d")
    params = {
        "pageno": 1, "strCat": "Result", "strPrevDate": today,
        "strToDate": today, "strScrip": "", "strSearch": "P",
        "strType": "C", "subcategory": "-1"
    }
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    try:
        response = session.get(url, params=params, timeout=20)
        return response.json().get("Table", [])
    except Exception as e:
        print("BSE Fetch Error:", e)
        return []

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
        "revenue from operations", "statement of audited financial results",
        "profit for the period", "profit before tax", "ebitda"
    ]
    extracted_text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text: continue
                
                if any(keyword in text.lower() for keyword in keywords):
                    print(f"✅ Financial Page Detected: Page {idx+1}")
                    extracted_text += text
                    if len(extracted_text) > 12000: break
    except Exception as e:
        print("PDF Extraction Error:", e)
    return extracted_text

# =========================================================
# EXTRACTION LAYER
# =========================================================
def regex_extract(financial_text):
    rev_match = re.search(r'revenue\s+from\s+operations.*?(\d+(?:\.\d+)?)\s*%', financial_text, re.IGNORECASE)
    pat_match = re.search(r'profit\s+for\s+the\s+period.*?(\d+(?:\.\d+)?)\s*%', financial_text, re.IGNORECASE)

    if rev_match and pat_match:
        return {
            "company_name": "Extracted via Regex", "quarter": "Latest",
            "revenue_yoy_growth_pct": float(rev_match.group(1)),
            "pat_yoy_growth_pct": float(pat_match.group(1)),
            "pat_qoq_growth_pct": 0, "ebitda_margin_pct": 0,
            "management_commentary": "Fast Regex Extraction (GPT Bypassed)",
            "order_book": "", "red_flags": ""
        }
    return None

def gpt_extract(financial_text):
    prompt = f"""
    Extract latest quarterly earnings. Return ONLY JSON.
    {{
        "company_name": "", "quarter": "", "sector": "", "industry": "",
        "revenue_yoy_growth_pct": 0, "pat_yoy_growth_pct": 0, "pat_qoq_growth_pct": 0,
        "ebitda_margin_pct": 0, "management_commentary": "", "order_book": "", "red_flags": ""
    }}
    DOCUMENT:
    {financial_text[:12000]}
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a financial extraction engine."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        content = response.choices[0].message.content.replace("```json", "").replace("```", "")
        return json.loads(content)
    except Exception as e:
        print("GPT Error:", e)
        return None

# =========================================================
# INTELLIGENCE LAYER
# =========================================================
def validate(data):
    essential_fields = [
        data.get("revenue_yoy_growth_pct"), data.get("pat_yoy_growth_pct"),
        data.get("pat_qoq_growth_pct"), data.get("ebitda_margin_pct")
    ]
    valid_count = sum(1 for field in essential_fields if isinstance(field, (int, float)) and field != 0)
    return valid_count >= 3

def identify_theme_and_score(sector, industry):
    text = f"{sector} {industry}".lower()
    themes = {
        "Defense & Aerospace": {"keywords": ["defense", "defence", "aerospace", "shipyard", "drone"], "score": 10},
        "Power & Energy": {"keywords": ["power", "energy", "transmission", "grid", "solar"], "score": 10},
        "Infrastructure & Capital Goods": {"keywords": ["infrastructure", "capital goods", "construction", "railway"], "score": 8},
        "EMS & Electronics": {"keywords": ["ems", "electronics", "semiconductor"], "score": 8},
        "Healthcare & Pharma": {"keywords": ["pharma", "healthcare", "api", "hospitals"], "score": 5},
        "IT & Software": {"keywords": ["it", "software", "technology"], "score": 4},
        "Financials": {"keywords": ["bank", "nbfc", "finance"], "score": 3}
    }
    for theme_name, t_data in themes.items():
        if any(kw in text for kw in t_data["keywords"]):
            return theme_name, t_data["score"]
    return "General / Other", 0

def calculate_pead(data, theme_score):
    score = 0
    rev_growth = data.get("revenue_yoy_growth_pct", 0)
    pat_growth = data.get("pat_yoy_growth_pct", 0)
    qoq_growth = data.get("pat_qoq_growth_pct", 0)

    if abs(rev_growth) > 300: rev_growth = 0
    if abs(pat_growth) > 500: pat_growth = 0
    if abs(qoq_growth) > 300: qoq_growth = 0

    if rev_growth > 30: score += 15
    elif rev_growth > 15: score += 10

    if pat_growth > 50: score += 25
    elif pat_growth > 20: score += 15

    if qoq_growth > 15: score += 10

    if data.get("ebitda_margin_pct", 0) > 20: score += 8

    score += theme_score

    return {
        "score": max(min(score, 100), 0),
        "rev_growth": rev_growth,
        "pat_growth": pat_growth,
        "qoq_growth": qoq_growth
    }

def get_pead_grade(score):
    if score >= 80: return "Institutional Monster 🚀"
    elif score >= 65: return "High Momentum 🔥"
    elif score >= 50: return "Strong PEAD 📈"
    return "Normal 📊"

# =========================================================
# DASHBOARD
# =========================================================
def generate_dashboard(company_name, pead, theme):
    img = Image.new("RGB", (900, 520), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    white, green, cyan = (255,255,255), (34,197,94), (45,212,191)

    draw.text((30,30), company_name, fill=white, font=font)
    draw.text((30,80), f"Theme: {theme}", fill=cyan, font=font)
    draw.text((650,30), f"PEAD {pead['score']}", fill=cyan, font=font)
    draw.text((30,180), f"Revenue Growth: {pead['rev_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,260), f"PAT Growth: {pead['pat_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,340), f"QoQ PAT: {pead['qoq_growth']:+.1f}%", fill=green, font=font)

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
    print("🚀 GPT PEAD ENGINE v9.0 (INSTITUTIONAL MASTER)")
    print("=" * 60)

    while True:
        try:
            # 1. Update Historical Returns 
            update_future_returns()

            # 2. Fetch New Results
            results = fetch_latest_results()
            for item in results:
                news_id = item.get("NEWSID")
                if not news_id or news_id in seen:
                    continue
                
                seen.add(news_id)
                company = item.get("SLONGNAME", "Unknown")
                attachment = item.get("ATTACHMENTNAME", "")

                if not attachment.endswith(".pdf"):
                    continue

                print(f"\n{'=' * 60}\n🚀 NEW RESULT: {company}\n{'=' * 60}")

                if is_microcap(company):
                    print(f"[REJECTED] {company} is a Microcap. Skipping.")
                    continue

                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                pdf_bytes = download_pdf(pdf_url)
                if not pdf_bytes:
                    continue

                financial_text = extract_financial_pages(pdf_bytes)
                if len(financial_text) < 500:
                    print("❌ No Financial Data")
                    continue

                data = regex_extract(financial_text)
                if not data:
                    print("⚠️ Regex Missed. Falling back to GPT Extraction...")
                    data = gpt_extract(financial_text)

                if not data:
                    print("❌ Extraction Failed entirely")
                    continue

                if not validate(data):
                    print("❌ Validation Failed (Missing required metrics)")
                    continue

                theme, theme_score = identify_theme_and_score(
                    data.get("sector", ""), data.get("industry", "")
                )
                
                pead = calculate_pead(data, theme_score)
                print("DEBUG PEAD:", pead)

                if pead["score"] < MIN_PEAD_SCORE:
                    print("❌ Low PEAD Score")
                    continue

                # GET LIVE PRICING DATA BEFORE SAVING
                ticker, entry_price = get_live_stock_data(company)
                entry_date_str = datetime.now().strftime("%Y-%m-%d")

                # Save to Database for backtesting
                save_to_db(
                    news_id, company, ticker, pead['score'], theme, 
                    entry_price, entry_date_str, 
                    pead['rev_growth'], pead['pat_growth'], pead['qoq_growth']
                )

                dashboard = generate_dashboard(company, pead, theme)
                grade = get_pead_grade(pead['score'])
                quarter = data.get('quarter', 'Latest Quarter')

                caption = (
                    f"🎯 {company} | {grade}\n\n"
                    f"📊 {quarter}\n"
                    f"Revenue YoY: {pead['rev_growth']:+.1f}%\n"
                    f"PAT YoY: {pead['pat_growth']:+.1f}%\n"
                    f"PAT QoQ: {pead['qoq_growth']:+.1f}%\n"
                    f"EBITDA Margin: {data.get('ebitda_margin_pct', 0)}%\n\n"
                    f"Theme: {theme}\n"
                    f"PEAD Score: {pead['score']}"
                )

                print("📤 Sending Telegram Alert...")
                if send_telegram_photo(dashboard, caption):
                    print("✅ PHOTO SENT")
                    
                    commentary = (
                        f"Commentary:\n\n{data.get('management_commentary', '')}\n\n"
                        f"Order Book:\n\n{data.get('order_book', '')}\n\n"
                        f"Red Flags:\n\n{data.get('red_flags', '')}"
                    )
                    send_telegram_message(commentary)
                    print("✅ COMMENTARY SENT")

                gc.collect()
                time.sleep(2)

            print(f"\n[{time.strftime('%H:%M:%S')}] Alive | Seen={len(seen)}")

        except Exception as e:
            print("MAIN LOOP ERROR:", e)

        gc.collect()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
