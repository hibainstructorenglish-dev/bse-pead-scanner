# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v10.1 (DATA INGESTION FIX)
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
# CONFIG & LOCAL CACHES
# =========================================================
TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"

OPENAI_API_KEY = "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"

CHECK_INTERVAL = 60
DB_NAME = "pead_results.db"
MIN_PEAD_SCORE = 40  
MICROCAP_LIMIT_CR = 500

# Add manual mappings here as you notice yfinance failing
TICKER_CACHE = {
    "GMR Airports": "GMRINFRA.NS",
    "Time Technoplast": "TIMETECHNO.NS",
    "Ramky Infrastructure": "RAMKY.NS"
}

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

def save_to_db(news_id, company, ticker, total_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth):
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO pead_results 
            (news_id, company, ticker, pead_score, theme, entry_price, entry_date, revenue_growth, pat_growth, qoq_growth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (news_id, company, ticker, total_score, theme, entry_price, entry_date, rev_growth, pat_growth, qoq_growth))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB Save Error:", e)

# =========================================================
# QUANT ANALYTICS & RESEARCH ENGINE
# =========================================================
def generate_pead_analytics():
    print("\n" + "=" * 55)
    print("📊 PEAD THEMATIC PERFORMANCE REPORT (20-DAY DRIFT)")
    print("=" * 55)
    try:
        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("""
            SELECT theme, COUNT(news_id) as sample_size, ROUND(AVG(price_day_20_pct), 2) as avg_20d_ret
            FROM pead_results WHERE price_day_20_pct IS NOT NULL GROUP BY theme ORDER BY avg_20d_ret DESC
        """)
        rows = cur.fetchall()
        if not rows:
            print("Not enough 20-day tracking data yet to generate report.")
        else:
            print(f"{'Theme':<25} | {'Count':<6} | {'Avg 20-Day Return'}")
            print("-" * 55)
            for row in rows:
                theme, count, avg_ret = row
                print(f"{theme:<25} | {count:<6} | {avg_ret:+.2f}%")
        print("-" * 55 + "\n")
        conn.close()
    except Exception as e:
        print("Analytics Generation Error:", e)

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
# TECHNICAL / MARKET STRUCTURE ENGINE
# =========================================================
def get_technical_factors(ticker):
    try:
        if not ticker: return 0, 0, 0
            
        nifty = yf.Ticker("^NSEI")
        nifty_hist = nifty.history(period="3mo")
        nifty_3m_ret = 0 if nifty_hist.empty else ((nifty_hist['Close'].iloc[-1] - nifty_hist['Close'].iloc[0]) / nifty_hist['Close'].iloc[0]) * 100

        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y")
        
        # ELITE FIX: Allow stocks with > 70 days of history
        if len(hist) < 70: 
            return 0, 0, 0 

        current_price = hist['Close'].iloc[-1]
        
        # 1. RELATIVE STRENGTH (Stock vs Nifty over ~63 trading days)
        stock_3m_start = hist['Close'].iloc[-63]
        stock_3m_ret = ((current_price - stock_3m_start) / stock_3m_start) * 100
        rs = stock_3m_ret - nifty_3m_ret
        rs_score = 10 if rs > 15 else (5 if rs > 5 else 0)

        # 2. VOLUME BREAKOUT
        current_vol = hist['Volume'].iloc[-1]
        avg_vol_20 = hist['Volume'].iloc[-21:-1].mean() if len(hist) > 21 else hist['Volume'].mean()
        vol_score = 8 if current_vol > (2 * avg_vol_20) else 0

        # 3. ADAPTIVE DMA TREND
        dma_period = min(200, len(hist))
        dma = hist['Close'].iloc[-dma_period:].mean()
        trend_score = 5 if current_price > dma else 0

        return rs_score, vol_score, trend_score
    except Exception as e:
        print(f"Technical Fetch Error for {ticker}:", e)
        return 0, 0, 0

def get_live_stock_data(company_name):
    try:
        # 1. CHECK LOCAL CACHE FIRST
        for cached_name, symbol in TICKER_CACHE.items():
            if cached_name.lower() in company_name.lower():
                stock = yf.Ticker(symbol)
                hist = stock.history(period="1d")
                if not hist.empty: return symbol, hist['Close'].iloc[-1]

        # 2. FALLBACK TO YFINANCE SEARCH (With appended "NSE" for better India results)
        search = yf.Search(company_name + " NSE")
        if not search.quotes: return None, 0.0
        
        symbol = search.quotes[0]["symbol"]
        
        # Heuristic: Auto-append .NS if yfinance returns a raw ticker for India
        if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
            symbol += ".NS"
            
        stock = yf.Ticker(symbol)
        hist = stock.history(period="1d")
        if hist.empty: return symbol, 0.0
            
        return symbol, hist['Close'].iloc[-1]
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
    except Exception:
        return False

# =========================================================
# TELEGRAM
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
    keywords = ["revenue from operations", "statement of audited financial results", "profit for the period", "profit before tax", "ebitda"]
    extracted_text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text: continue
                if any(k in text.lower() for k in keywords):
                    print(f"✅ Financial Page Detected: Page {idx+1}")
                    extracted_text += text
                    if len(extracted_text) > 12000: break
    except Exception:
        pass
    return extracted_text

def regex_extract(financial_text):
    rev_match = re.search(r'revenue\s+from\s+operations.*?(\d+(?:\.\d+)?)\s*%', financial_text, re.IGNORECASE)
    pat_match = re.search(r'profit\s+for\s+the\s+period.*?(\d+(?:\.\d+)?)\s*%', financial_text, re.IGNORECASE)
    if rev_match and pat_match:
        return {
            "company_name": "Extracted via Regex", "quarter": "Latest",
            "sector": "General / Other", "industry": "General / Other",
            "revenue_yoy_growth_pct": float(rev_match.group(1)),
            "pat_yoy_growth_pct": float(pat_match.group(1)),
            "pat_qoq_growth_pct": 0, "ebitda_margin_pct": 0,
            "management_commentary": "Fast Regex Extraction (GPT Bypassed)",
            "order_book": "", "red_flags": ""
        }
    return None

def gpt_extract(financial_text):
    # ELITE FIX: Upgraded Inference Prompt
    prompt = f"""
    Extract latest quarterly earnings. Return ONLY JSON. Use null if a metric is completely missing.
    Prioritize extracting PAT / Net Profit growth. Do NOT omit PAT if present in tables.
    If exact sector unavailable, infer probable sector and industry from company operations.
    {{
        "company_name": "", "quarter": "", 
        "sector": "Guess likely sector from company and document", 
        "industry": "Guess likely industry from company and document",
        "revenue_yoy_growth_pct": null, "pat_yoy_growth_pct": null, "pat_qoq_growth_pct": null,
        "ebitda_margin_pct": null, "management_commentary": "", "order_book": "", "red_flags": ""
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
    except Exception:
        return None

# =========================================================
# FUNDAMENTAL INTELLIGENCE LAYER
# =========================================================
def validate(data):
    essential_fields = [
        data.get("revenue_yoy_growth_pct"), data.get("pat_yoy_growth_pct"),
        data.get("pat_qoq_growth_pct"), data.get("ebitda_margin_pct")
    ]
    valid_count = sum(1 for field in essential_fields if isinstance(field, (int, float)))
    return valid_count >= 3

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

    if rev_growth > 30: score += 15
    elif rev_growth > 15: score += 10
    elif rev_growth > 8: score += 5

    if pat_growth > 50: score += 25
    elif pat_growth > 20: score += 15
    elif pat_growth > 10: score += 5

    if qoq_growth > 15: score += 10
    if (data.get("ebitda_margin_pct") or 0) > 20: score += 8

    score += theme_score
    return {"score": max(min(score, 100), 0), "rev_growth": rev_growth, "pat_growth": pat_growth, "qoq_growth": qoq_growth}

def get_pead_grade(score):
    if score >= 85: return "Institutional Monster 🚀"
    elif score >= 65: return "High Momentum 🔥"
    elif score >= 45: return "Strong PEAD 📈"
    return "Normal 📊"

# =========================================================
# DASHBOARD
# =========================================================
def generate_dashboard(company_name, pead, theme, tech_score, rs, vol, trend):
    img = Image.new("RGB", (900, 560), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    white, green, cyan, gold = (255,255,255), (34,197,94), (45,212,191), (250, 204, 21)

    draw.text((30,30), company_name, fill=white, font=font)
    draw.text((30,80), f"Theme: {theme}", fill=cyan, font=font)
    draw.text((650,30), f"TOTAL SCORE {pead['score'] + tech_score}", fill=gold, font=font)
    
    draw.text((30,160), "--- FUNDAMENTALS ---", fill=white, font=font)
    draw.text((30,200), f"Revenue Growth: {pead['rev_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,240), f"PAT Growth: {pead['pat_growth']:+.1f}%", fill=green, font=font)
    draw.text((30,280), f"QoQ PAT: {pead['qoq_growth']:+.1f}%", fill=green, font=font)

    draw.text((30,360), "--- TECHNICAL & FLOWS ---", fill=white, font=font)
    draw.text((30,400), f"Relative Strength (vs Nifty): {'Strong 🟢' if rs > 0 else 'Weak 🔴'}", fill=white, font=font)
    draw.text((30,440), f"Volume Breakout: {'Detected 🟢' if vol > 0 else 'Normal'}", fill=white, font=font)
    draw.text((30,480), f"Market Structure: {'> DMA 🟢' if trend > 0 else '< DMA 🔴'}", fill=white, font=font)

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
    print("🚀 GPT PEAD ENGINE v10.1 (INFERENCE + CACHE)")
    print("=" * 60)

    while True:
        try:
            update_future_returns()
            generate_pead_analytics() 
            
            results = fetch_latest_results()
            for item in results:
                news_id = item.get("NEWSID")
                if not news_id or news_id in seen: continue
                
                seen.add(news_id)
                company = item.get("SLONGNAME", "Unknown")
                attachment = item.get("ATTACHMENTNAME", "")

                if not attachment.endswith(".pdf"): continue

                print(f"\n{'=' * 60}\n🚀 NEW RESULT: {company}\n{'=' * 60}")

                if is_microcap(company):
                    print(f"[REJECTED] {company} is a Microcap. Skipping.")
                    continue

                ticker, entry_price = get_live_stock_data(company)
                if not ticker:
                    print("❌ Could not map Ticker. Skipping.")
                    continue

                pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                pdf_bytes = download_pdf(pdf_url)
                if not pdf_bytes: continue

                financial_text = extract_financial_pages(pdf_bytes)
                if len(financial_text) < 500:
                    print("❌ No Financial Data")
                    continue

                data = regex_extract(financial_text)
                if not data:
                    print("⚠️ Regex Missed. Falling back to GPT...")
                    data = gpt_extract(financial_text)

                if not data:
                    print("❌ Extraction Failed entirely")
                    continue

                print("\n🔍 DEBUG: RAW EXTRACTED DATA")
                print(json.dumps(data, indent=2))

                if not validate(data):
                    print("❌ Validation Failed (Missing required metrics)")
                    continue

                theme, theme_score = identify_theme_and_score(str(data.get("sector")), str(data.get("industry")))
                pead = calculate_pead(data, theme_score)
                
                rs_score, vol_score, trend_score = get_technical_factors(ticker)
                tech_score = rs_score + vol_score + trend_score
                
                total_score = pead['score'] + tech_score
                print(f"DEBUG SCORING -> Funda: {pead['score']} | Tech: {tech_score} | Total: {total_score}")

                if total_score < MIN_PEAD_SCORE:
                    print("❌ Low Total Quant Score")
                    continue

                entry_date_str = datetime.now().strftime("%Y-%m-%d")

                save_to_db(news_id, company, ticker, total_score, theme, entry_price, entry_date_str, pead['rev_growth'], pead['pat_growth'], pead['qoq_growth'])

                dashboard = generate_dashboard(company, pead, theme, tech_score, rs_score, vol_score, trend_score)
                grade = get_pead_grade(total_score)
                quarter = data.get('quarter', 'Latest Quarter')

                caption = (
                    f"🎯 {company} ({ticker}) | {grade}\n\n"
                    f"📈 FUNDAMENTALS ({quarter})\n"
                    f"Revenue YoY: {pead['rev_growth']:+.1f}%\n"
                    f"PAT YoY: {pead['pat_growth']:+.1f}%\n"
                    f"PAT QoQ: {pead['qoq_growth']:+.1f}%\n"
                    f"EBITDA Margin: {data.get('ebitda_margin_pct') or 0}%\n\n"
                    f"⚙️ TECHNICALS\n"
                    f"Outperforming Nifty: {'Yes 🟢' if rs_score > 0 else 'No 🔴'}\n"
                    f"Volume Breakout: {'Yes 🟢' if vol_score > 0 else 'No 🔴'}\n"
                    f"Trend: {'> DMA 🟢' if trend_score > 0 else '< DMA 🔴'}\n\n"
                    f"Theme: {theme}\n"
                    f"TOTAL QUANT SCORE: {total_score}"
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

            print(f"\n[{time.strftime('%H:%M:%S')}] Alive | Seen={len(seen)}")

        except Exception as e:
            print("MAIN LOOP ERROR:", e)

        gc.collect()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
