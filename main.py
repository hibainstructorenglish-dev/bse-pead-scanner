# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v12.0 (THE 10/10 MASTER)
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
# SECURE CONFIGURATION
# =========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    print("CRITICAL ERROR: OPENAI_API_KEY environment variable is missing.")
    exit(1)

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
# DYNAMIC CACHE & SCREENER CONTEXT PROCESSING
# =========================================================
TICKER_MASTER_FILE = "ticker_master.json"

def load_ticker_master():
    if os.path.exists(TICKER_MASTER_FILE):
        try:
            with open(TICKER_MASTER_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print("Load Cache Error:", e)
    return {}

def update_ticker_cache(company_name, symbol):
    try:
        cache = load_ticker_master()
        cache[company_name] = symbol
        with open(TICKER_MASTER_FILE, "w") as f:
            json.dump(cache, f, indent=4)
    except Exception as e:
        print("Cache Save Error:", e)

def get_screener_context(bse_code):
    if not bse_code or len(bse_code) != 6: return None
    try:
        url = "https://www.screener.in/company/" + str(bse_code) + "/"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        nse_match = re.search(r'NSE\s*:\s*([A-Z0-9\-]+)', text, re.IGNORECASE)
        nse_symbol = None
        if nse_match:
            nse_symbol = nse_match.group(1).strip() + ".NS"

        market_cap_match = re.search(r'Market Cap\s*₹\s*([\d,\.]+)', text)
        market_cap_cr = 0
        if market_cap_match:
            market_cap_cr = float(market_cap_match.group(1).replace(",", ""))

        quarterly_data = []
        tables = soup.find_all("table")
        for table in tables:
            table_text = table.get_text(" ", strip=True).lower()
            if "sales" in table_text and "profit" in table_text:
                rows = table.find_all("tr")
                parsed_rows = []
                for row in rows:
                    cols = row.find_all(["td", "th"])
                    cols_text = [c.get_text(strip=True) for c in cols]
                    if cols_text:
                        parsed_rows.append(cols_text)
                quarterly_data = parsed_rows
                break

        return {
            "nse_symbol": nse_symbol,
            "market_cap_cr": market_cap_cr,
            "quarterly_data": quarterly_data
        }
    except Exception as e:
        print("Screener Context Error:", e)
        return None

def clean_name_for_search(company_name):
    name = re.sub(r'(?i)\b(ltd|limited|corp|corporation|inc|co)\b\.?', '', company_name)
    name = re.sub(r'[^a-zA-Z0-9\s]', '', name)
    words = name.split()
    return " ".join(words[:3]).strip()

def get_live_stock_data(company_name, screener_context):
    try:
        ticker_cache = load_ticker_master()
        for cached_name, symbol in ticker_cache.items():
            if cached_name.lower() in company_name.lower() or company_name.lower() in cached_name.lower():
                stock = yf.Ticker(symbol)
                hist = stock.history(period="1d")
                if not hist.empty: return symbol, hist['Close'].iloc[-1]

        if screener_context and screener_context.get("nse_symbol"):
            nse_symbol = screener_context.get("nse_symbol")
            stock = yf.Ticker(nse_symbol)
            hist = stock.history(period="1d")
            if not hist.empty:
                update_ticker_cache(company_name, nse_symbol)
                return nse_symbol, hist['Close'].iloc[-1]

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

        return None, 0.0
    except Exception as e:
        print("Live Data Fetch Error:", e)
        return None, 0.0

def is_microcap(company_name, screener_context):
    try:
        if screener_context and screener_context.get("market_cap_cr") is not None:
            cap = screener_context.get("market_cap_cr")
            if cap is not None and cap > 0:
                print(f"Market Cap (via Screener): ₹{cap:.0f} Cr")
                return cap < MICROCAP_LIMIT_CR
            print("⚠️ Screener market cap unavailable/zero. Falling back to yFinance.")

        symbol, _ = get_live_stock_data(company_name, screener_context)
        if not symbol: return False
        
        stock = yf.Ticker(symbol)
        market_cap = stock.info.get("marketCap")
        if not market_cap or market_cap <= 0:
            return False
            
        market_cap_cr = market_cap / 10000000
        print(f"Market Cap (via yFinance): ₹{market_cap_cr:.0f} Cr")
        return market_cap_cr < MICROCAP_LIMIT_CR
    except Exception as e:
        print("Microcap Check Error:", e)
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
    except Exception as e:
        print("Technical Bonus Error:", e)
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
    # ELITE FIX: Two-pass system to prevent missing deep consolidated tables
    conso_keywords = ["consolidated financial results", "audited consolidated"]
    base_keywords = ["revenue from operations", "profit for the period", "financial results"]
    
    extracted_text = ""
    conso_found = False
    
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            # PASS 1: Hunt specifically for the word "Consolidated"
            for idx, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text: continue
                
                if any(k in text.lower() for k in conso_keywords):
                    print(f"✅ Priority Consolidated Page Detected: Page {idx+1}")
                    extracted_text += text
                    conso_found = True
                    if len(extracted_text) > 25000: break
            
            # PASS 2: If no Consolidated section exists, fall back to standard
            if not conso_found:
                print("⚠️ No explicit consolidated headers. Parsing standard financial sheets.")
                for idx, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if not text: continue
                    if any(k in text.lower() for k in base_keywords):
                        print(f"✅ Financial Page Detected: Page {idx+1}")
                        extracted_text += text
                        if len(extracted_text) > 25000: break
                        
    except Exception as e:
        print("PDF Extraction Error:", e)
        
    return extracted_text

def gpt_extract_with_context(pdf_text, company_name, screener_context):
    try:
        history_text = ""
        if screener_context:
            history_text = f"""
SCREENER HISTORICAL CONTEXT
NSE Symbol: {screener_context.get('nse_symbol')}
Market Cap: {screener_context.get('market_cap_cr')} Cr
Quarterly Historical Data:
{screener_context.get('quarterly_data')}
"""

        prompt = f"""
        You are a ruthless, highly precise institutional PEAD analyst.
        Use BOTH current quarter PDF and historical Screener quarterly data to evaluate structural performance trends.
        
        RULES:
        1. CONSOLIDATED ONLY: Strictly extract from CONSOLIDATED results. Ignore Standalone unless Consolidated literally does not exist.
        2. STRICT SIGNS: If profit or revenue decreased, the percentage MUST be negative (e.g., -15.4).
        3. NO HALLUCINATION: If a metric is completely missing, return null. Do not calculate it yourself unless the base numbers are explicitly clear.
        
        Evaluate:
        - acceleration, slowdown, or turnaround scenarios
        - operational leverage & margin expansion stability

        {history_text}

        CURRENT PDF:
        {pdf_text[:15000]}
        """

        # ELITE UPGRADE: Structured Outputs for 100% Deterministic Extraction
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "pead_extraction",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "company_name": {"type": "string"},
                            "quarter": {"type": "string"},
                            "sector": {"type": "string"},
                            "industry": {"type": "string"},
                            "revenue_yoy_growth_pct": {"type": ["number", "null"]},
                            "pat_yoy_growth_pct": {"type": ["number", "null"]},
                            "pat_qoq_growth_pct": {"type": ["number", "null"]},
                            "ebitda_margin_pct": {"type": ["number", "null"]},
                            "management_commentary": {"type": "string"},
                            "order_book": {"type": "string"},
                            "red_flags": {"type": "string"},
                            "earnings_quality": {"type": "string"},
                            "turnaround_signal": {"type": "string"},
                            "acceleration_signal": {"type": "string"}
                        },
                        "required": [
                            "company_name", "quarter", "sector", "industry", 
                            "revenue_yoy_growth_pct", "pat_yoy_growth_pct", 
                            "pat_qoq_growth_pct", "ebitda_margin_pct", 
                            "management_commentary", "order_book", "red_flags", 
                            "earnings_quality", "turnaround_signal", "acceleration_signal"
                        ],
                        "additionalProperties": False
                    }
                }
            }
        )
        
        raw = response.choices[0].message.content
        data = json.loads(raw)
        
        print("\n🔍 DEBUG: RAW EXTRACTED DATA")
        print(json.dumps(data, indent=2))
        return data
        
    except Exception as e:
        print("GPT Extraction Error:", e)
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
        "Financials": {"keywords": ["bank", "nbfc", "finance"], "score": 3},
        "Chemicals & Fertilizers": {"keywords": ["chemical", "fertilizer", "fertiliser", "specialty chemical", "agrochemical", "industrial gas", "petrochemical"], "score": 8}
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

    rev_growth = max(min(rev_growth, 300), -300)
    pat_growth = max(min(pat_growth, 500), -500)
    qoq_growth = max(min(qoq_growth, 300), -300)

    if rev_growth > 50: score += 20
    elif rev_growth > 30: score += 15
    elif rev_growth > 15: score += 10
    elif rev_growth > 8: score += 5

    if pat_growth > 100: score += 30
    elif pat_growth > 50: score += 25
    elif pat_growth > 20: score += 15
    elif pat_growth > 10: score += 5

    if qoq_growth > 100: score += 20  
    elif qoq_growth > 40: score += 15
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
    url = (
        "https://api.telegram.org/bot"
        + str(TELEGRAM_TOKEN)
        + "/sendMessage"
    )
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:3500]
            },
            timeout=20
        )
    except Exception as e:
        print("Telegram Msg Error:", e)

def send_telegram_photo(image_bytes, caption):
    url = (
        "https://api.telegram.org/bot"
        + str(TELEGRAM_TOKEN)
        + "/sendPhoto"
    )
    try:
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
    print("🚀 GPT PEAD ENGINE v12.0 (THE 10/10 MASTER)")
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

                screener_context = get_screener_context(scrip_cd)

                if is_microcap(company, screener_context):
                    print(f"[REJECTED] {company} is a Microcap. Skipping.")
                    continue

                ticker, entry_price = get_live_stock_data(company, screener_context)
                if not ticker:
                    print("⚠️ Ticker unavailable. Continuing fundamental PEAD only.")
                    ticker = None
                    entry_price = 0

                pdf_url = (
                    "https://www.bseindia.com/"
                    "xml-data/corpfiling/AttachLive/"
                    + str(attachment)
                )
                
                pdf_bytes = download_pdf(pdf_url)
                if not pdf_bytes: continue

                financial_text = extract_financial_pages(pdf_bytes)
                if len(financial_text) < 500:
                    print("❌ No Financial Data")
                    continue

                data = gpt_extract_with_context(financial_text, company, screener_context)
                if not data:
                    print("❌ Extraction Failed entirely")
                    continue

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
                    f"🏆 FINAL PEAD SCORE: {final_score}\n\n"
                    f"⚡ Theme: {theme}\n"
                    f"🏭 Sector: {data.get('sector')}\n"
                    f"🏢 Industry: {data.get('industry')}\n\n"
                    f"📈 REVENUE\n"
                    f"YoY Growth: {pead['rev_growth']:+.1f}%\n\n"
                    f"💰 PAT\n"
                    f"YoY Growth: {pead['pat_growth']:+.1f}%\n"
                    f"QoQ Growth: {pead['qoq_growth']:+.1f}%\n\n"
                    f"📊 EBITDA Margin: {data.get('ebitda_margin_pct', 0)}%\n\n"
                    f"📈 Technical Bonus: +{tech_bonus}"
                )

                print("📤 Sending Telegram Alert...")
                if send_telegram_photo(dashboard, caption):
                    print("✅ PHOTO SENT")
                    commentary = (
                        f"Commentary:\n{data.get('management_commentary', '')}\n\n"
                        f"Metrics Context:\n"
                        f"• Quality: {data.get('earnings_quality', 'N/A')}\n"
                        f"• Turnaround: {data.get('turnaround_signal', 'N/A')}\n"
                        f"• Acceleration: {data.get('acceleration_signal', 'N/A')}\n\n"
                        f"Order Book: {data.get('order_book', 'N/A')}\n"
                        f"Red Flags: {data.get('red_flags', 'N/A')}"
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
