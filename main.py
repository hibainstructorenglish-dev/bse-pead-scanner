# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v10.4 (BS4 SCREENER RESOLVER)
# =========================================================

import io
import gc
import re
import os
import json
import time
import sqlite3
import logging
import requests
import pdfplumber
import yfinance as yf
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# --- MUTE YFINANCE SPAM ---
logging.getLogger('yfinance').setLevel(logging.CRITICAL)

# =========================================================
# CONFIG
# =========================================================
TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"

OPENAI_API_KEY = ""

CHECK_INTERVAL = 60
DB_NAME = "pead_results.db"
MIN_PEAD_SCORE = 40  
MICROCAP_LIMIT_CR = 500

client = OpenAI(api_key=OPENAI_API_KEY)
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

def save_to_db(news_id, company, ticker, final_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth):
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO pead_results 
            (news_id, company, ticker, pead_score, theme, entry_price, entry_date, revenue_growth, pat_growth, qoq_growth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (news_id, company, ticker, final_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB Save Error:", e)

def update_future_returns():
    print("\n🔄 Running Forward Return Tracker...")
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            SELECT news_id, ticker, entry_price, entry_date, 
                   price_day_1_pct, price_day_3_pct, price_day_5_pct, price_day_20_pct 
            FROM pead_results WHERE price_day_20_pct IS NULL AND ticker IS NOT NULL
        """)
        pending_records = cur.fetchall()
        today = datetime.now()
        
        for record in pending_records:
            news_id, ticker, entry_price, entry_date_str, d1, d3, d5, d20 = record
            if not entry_price or entry_price <= 0: continue
            
            entry_date = datetime.strptime(entry_date_str, "%Y-%m-%d")
            days_elapsed = (today - entry_date).days
            if days_elapsed < 1: continue
                
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")
            if hist.empty: continue
            
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
# DYNAMIC CACHE & TICKER ROUTING
# =========================================================
TICKER_MASTER_FILE = "ticker_master.json"

def load_ticker_master():
    if os.path.exists(TICKER_MASTER_FILE):
        try:
            with open(TICKER_MASTER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def update_ticker_cache(company_name, symbol):
    try:
        cache = load_ticker_master()
        cache[company_name] = symbol
        with open(TICKER_MASTER_FILE, "w") as f:
            json.dump(cache, f, indent=4)
    except Exception as e:
        print("Cache Save Error:", e)

# =========================================================
# SCREENER BSE -> NSE SYMBOL RESOLVER
# =========================================================
def get_symbol_from_screener_bse(bse_code):
    try:
        url = f"https://www.screener.in/company/{bse_code}/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        match = re.search(r'NSE:\s*([A-Z0-9]+)', text)
        if match:
            symbol = match.group(1)
            print(f"✅ Screener NSE Found: {symbol}")
            return symbol + ".NS"

        return None
    except Exception as e:
        print("Screener Resolver Error:", e)
        return None

def clean_name_for_search(company_name):
    name = re.sub(r'(?i)\b(ltd|limited|corp|corporation|inc|co)\b\.?', '', company_name)
    name = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    words = name.split()
    return " ".join(words[:3]).strip()

def get_live_stock_data(company_name, scrip_cd=""):
    try:
        # 1. MANUAL CACHE FIRST
        ticker_cache = load_ticker_master()
        for cached_name, symbol in ticker_cache.items():
            if cached_name.lower() in company_name.lower() or company_name.lower() in cached_name.lower():
                stock = yf.Ticker(symbol)
                hist = stock.history(period="1d")
                if not hist.empty: return symbol, hist['Close'].iloc[-1]

        # 2. SCREENER LOOKUP SECOND
        if scrip_cd:
            nse_symbol = get_symbol_from_screener_bse(scrip_cd)
            if nse_symbol:
                stock = yf.Ticker(nse_symbol)
                hist = stock.history(period="1d")
                if not hist.empty:
                    update_ticker_cache(company_name, nse_symbol)
                    return nse_symbol, hist['Close'].iloc[-1]

        # 3. YAHOO NSE SEARCH THIRD
        clean_name = clean_name_for_search(company_name)
        search = yf.Search(clean_name + " NSE")
        if search.quotes:
            symbol = search.quotes[0].get("symbol")
            if symbol:
                if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
                    symbol += ".NS"
                stock = yf.Ticker(symbol)
                hist = stock.history(period="1d")
                if not hist.empty:
                    update_ticker_cache(company_name, symbol)
                    return symbol, hist['Close'].iloc[-1]

        # 4. YAHOO BSE SYMBOL LAST
        if scrip_cd and len(scrip_cd) == 6:
            bse_symbol = f"{scrip_cd}.BO"
            stock = yf.Ticker(bse_symbol)
            hist = stock.history(period="1d")
            if not hist.empty:
                update_ticker_cache(company_name, bse_symbol)
                return bse_symbol, hist['Close'].iloc[-1]

        return None, 0.0

    except Exception:
        return None, 0.0

def is_microcap(company_name, scrip_cd=""):
    try:
        symbol, _ = get_live_stock_data(company_name, scrip_cd)
        if not symbol: return False
        
        stock = yf.Ticker(symbol)
        market_cap = stock.info.get("marketCap")
        
        if not market_cap or market_cap <= 0:
            print(f"⚠️ Market Cap data missing for {symbol}. Bypassing microcap filter.")
            return False
            
        market_cap_cr = market_cap / 10000000
        print(f"Market Cap: ₹{market_cap_cr:.0f} Cr")
        
        return market_cap_cr < MICROCAP_LIMIT_CR
    except Exception as e:
        print(f"Microcap Check Error for {company_name}:", e)
        return False

# =========================================================
# TECHNICAL BONUS LAYER
# =========================================================
def get_technical_bonus(ticker):
    try:
        if not ticker: return 0
        stock = yf.Ticker(ticker)
        hist = stock.history(period="6mo")
        if len(hist) < 50: return 0

        lookback = min(63, len(hist)-1)
        current_price = hist['Close'].iloc[-1]
        old_price = hist['Close'].iloc[-lookback]
        stock_return = ((current_price - old_price) / old_price) * 100
        
        bonus = 0
        if stock_return > 15: bonus += 5
        elif stock_return > 8: bonus += 3

        avg_vol = hist['Volume'].tail(20).mean()
        current_vol = hist['Volume'].iloc[-1]
        if current_vol > 2 * avg_vol: bonus += 5

        dma_period = min(200, len(hist))
        dma = hist['Close'].tail(dma_period).mean()
        if current_price > dma: bonus += 3

        return bonus
    except Exception:
        return 0

# =========================================================
# BSE FETCHING & EXTRACTION
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
        return session.get(url, params=params, timeout=20).json().get("Table", [])
    except Exception:
        return []

def download_pdf(pdf_url):
    try:
        response = session.get(pdf_url, timeout=20)
        response.raise_for_status()
        return response.content
    except Exception:
        return None

def extract_financial_pages(pdf_bytes):
    keywords = [
        "revenue from operations", "statement of audited financial results",
        "profit for the period", "profit before tax", "ebitda",
        "net profit", "total income", "financial results",
        "income from operations", "results for the quarter"
    ]
    extracted_text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text: continue
                if any(k in text.lower() for k in keywords):
                    print(f"✅ Financial Page Detected: Page {idx+1}")
                    extracted_text += text
                    if len(extracted_text) > 25000: break
    except Exception:
        pass
    return extracted_text

def gpt_extract(financial_text):
    prompt = f"""
    Extract latest quarterly earnings.
    Strictly extract from the CONSOLIDATED financial results table. Ignore Standalone results unless Consolidated is completely unavailable.
    Return ONLY valid JSON.
    Use null if unavailable.
    Infer likely sector and industry from company operations.
    PAT / Net Profit extraction is extremely important.
    If PAT exists anywhere in tables, extract it.
    Never silently omit PAT.
    Summarize management commentary in 1 sentence.

    Return:
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
    {financial_text[:25000]}
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
    except Exception:
        return None

# =========================================================
# FUNDAMENTAL INTELLIGENCE LAYER
# =========================================================
def validate(data):
    revenue = data.get("revenue_yoy_growth_pct")
    pat = data.get("pat_yoy_growth_pct")
    qoq = data.get("pat_qoq_growth_pct")

    if revenue is None and (pat is None or pat < 25): return False
    if pat is None and qoq is None: return False
    return True

def identify_theme_and_score(sector, industry):
    text = f"{sector} {industry}".lower()
    themes = {
        "Defense & Aerospace": {"keywords": ["defense", "defence", "aerospace", "shipyard", "drone", "aviation", "airport"], "score": 10},
        "Power & Energy": {"keywords": ["power", "energy", "transmission", "grid", "solar"], "score": 10},
        "Infrastructure & Capital Goods": {"keywords": ["infrastructure", "capital goods", "construction", "railway", "infra"], "score": 8},
        "EMS & Electronics": {"keywords": ["ems", "electronics", "semiconductor", "technology"], "score": 8},
        "Healthcare & Pharma": {"keywords": ["pharma", "healthcare", "api", "hospitals"], "score": 5},
        "IT & Software": {"keywords": ["it", "software"], "score": 4},
        "Financials": {"keywords": ["bank", "nbfc", "finance"], "score": 3}
    }
    for theme_name, t_data in themes.items():
        if any(kw in text for kw in t_data["keywords"]):
            return theme_name, t_data["score"]
    return "General / Other", 0

def calculate_pead(data, theme_score):
    score = 0
    rev_growth = data.get("revenue_yoy_growth_pct") or 0
    pat_growth = data.get("pat_yoy_growth_pct") or 0
    qoq_growth = data.get("pat_qoq_growth_pct") or 0

    if abs(rev_growth) > 300: rev_growth = 0
    if abs(pat_growth) > 500: pat_growth = 0
    if abs(qoq_growth) > 300: qoq_growth = 0

    if rev_growth > 50: score += 20
    elif rev_growth > 30: score += 15
    elif rev_growth > 15: score += 10
    elif rev_growth > 8: score += 5

    if pat_growth > 100: score += 30
    elif pat_growth > 50: score += 25
    elif pat_growth > 20: score += 15
    elif pat_growth > 10: score += 5

    if qoq_growth > 40: score += 15
    elif qoq_growth > 15: score += 10

    if (data.get("ebitda_margin_pct") or 0) > 25: score += 10
    elif (data.get("ebitda_margin_pct") or 0) > 18: score += 5

    if rev_growth > 10 and pat_growth > 25: score += 8
    if rev_growth > 80 and qoq_growth > 20: score += 10

    score += theme_score
    return {"score": max(min(score, 100), 0), "rev_growth": rev_growth, "pat_growth": pat_growth, "qoq_growth": qoq_growth}

def get_pead_grade(score):
    if score >= 85: return "Institutional Monster 🚀"
    elif score >= 65: return "High Momentum 🔥"
    elif score >= 45: return "Strong PEAD 📈"
    return "Normal 📊"

# =========================================================
# TELEGRAM & DASHBOARD
# =========================================================
def send_telegram_message(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg[:3500]}, timeout=20)
    except Exception:
        pass

def send_telegram_photo(image_bytes, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        response = requests.post(
            url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000]},
            files={"photo": ("dashboard.png", image_bytes, "image/png")}, timeout=30
        )
        return response.status_code == 200
    except Exception:
        return False

def generate_dashboard(company_name, pead, theme, final_score):
    img = Image.new("RGB", (900, 560), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    white, green, cyan, gold = (255,255,255), (34,197,94), (45,212,191), (250, 204, 21)

    draw.text((30,30), company_name, fill=white, font=font)
    draw.text((30,80), f"Theme: {theme}", fill=cyan, font=font)
    draw.text((650,30), f"FINAL SCORE {final_score}", fill=gold, font=font)
    
    draw.text((30,160), "--- FUNDAMENTALS ---", fill=white, font=font)
    draw.text((30,200), f"Revenue Growth: {pead['rev_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,240), f"PAT Growth: {pead['pat_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,280), f"QoQ PAT: {pead['qoq_growth']:+.1f}%", fill=green, font=font)

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
    print("🚀 GPT PEAD ENGINE v10.4 (BS4 SCREENER RESOLVER)")
    print("=" * 60)

    cycle = 0

    while True:
        try:
            if cycle % 300 == 0:
                update_future_returns()
            
            results = fetch_latest_results()
            for item in results:
                news_id = item.get("NEWSID")
                if not news_id or news_id in seen: continue
                
                seen.add(news_id)
                company = item.get("SLONGNAME", "Unknown")
                scrip_cd = str(item.get("SCRIP_CD", ""))
                attachment = item.get("ATTACHMENTNAME", "")

                if not attachment.endswith(".pdf"): continue

                print(f"\n{'=' * 60}\n🚀 NEW RESULT: {company}\n{'=' * 60}")

                if is_microcap(company, scrip_cd):
                    print(f"[REJECTED] {company} is a Microcap. Skipping.")
                    continue

                ticker, entry_price = get_live_stock_data(company, scrip_cd)
                
                if not ticker:
                    print("⚠️ Ticker unavailable. Continuing fundamental PEAD only.")
                    ticker = None
                    entry_price = 0

                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                pdf_bytes = download_pdf(pdf_url)
                if not pdf_bytes: continue

                financial_text = extract_financial_pages(pdf_bytes)
                if len(financial_text) < 500:
                    print("❌ No Financial Data")
                    continue

                data = gpt_extract(financial_text)
                if not data:
                    print("❌ Extraction Failed entirely")
                    continue

                print("\n🔍 DEBUG: RAW EXTRACTED DATA")
                print(json.dumps(data, indent=2))

                if not validate(data):
                    print("❌ Validation Failed (Missing/weak required metrics)")
                    continue

                theme, theme_score = identify_theme_and_score(str(data.get("sector")), str(data.get("industry")))
                
                pead = calculate_pead(data, theme_score)
                base_score = pead['score']
                tech_bonus = get_technical_bonus(ticker)
                final_score = base_score + tech_bonus
                
                print(f"DEBUG SCORING -> Base PEAD: {base_score} | Tech Bonus: +{tech_bonus} | Final: {final_score}")

                if final_score < MIN_PEAD_SCORE:
                    print("❌ Low Total Quant Score")
                    continue

                entry_date_str = datetime.now().strftime("%Y-%m-%d")
                save_to_db(news_id, company, ticker, final_score, theme, entry_price, entry_date_str, pead['rev_growth'], pead['pat_growth'], pead['qoq_growth'])

                dashboard = generate_dashboard(company, pead, theme, final_score)
                grade = get_pead_grade(final_score)
                quarter = data.get('quarter', 'Latest Quarter')

                caption = (
                    f"🎯 {company}\n\n"
                    f"{grade}\n\n"
                    f"📊 {quarter}\n\n"
                    f"Revenue YoY: {pead['rev_growth']:+.1f}%\n"
                    f"PAT YoY: {pead['pat_growth']:+.1f}%\n"
                    f"PAT QoQ: {pead['qoq_growth']:+.1f}%\n\n"
                    f"EBITDA Margin: {data.get('ebitda_margin_pct') or 0}%\n\n"
                    f"Theme: {theme}\n"
                    f"Technical Bonus: +{tech_bonus}\n\n"
                    f"Final PEAD Score: {final_score}\n\n"
                    f"🧠 Sector: {data.get('sector')}\n"
                    f"🏭 Industry: {data.get('industry')}"
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

                gc.collect()
                time.sleep(2)

            cycle += 1
            print(f"\n[{time.strftime('%H:%M:%S')}] Alive | Seen={len(seen)} | Cycle={cycle}")

        except Exception as e:
            print("MAIN LOOP ERROR:", e)

        gc.collect()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
