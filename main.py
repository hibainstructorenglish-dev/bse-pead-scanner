import os
import json
import time
import sqlite3
import tempfile
import requests
import threading
import yfinance as yf
from flask import Flask
from queue import Queue
from datetime import datetime
from collections import deque
from google import genai
from google.genai import types

# =========================================================
# CONFIGURATION
# =========================================================
TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"
GEMINI_API_KEY = "YOUR_GEMINI_KEY" # <-- REPLACE WITH YOUR SECURE KEY

DB_NAME = "bse_results.db"
BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

print("Initializing Gemini Client...", flush=True)
client = genai.Client(api_key=GEMINI_API_KEY)

# The holding pen for the Producer-Consumer architecture
analysis_queue = Queue()

# =========================================================
# FLASK KEEP-ALIVE SERVER (FOR RENDER.COM)
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "BSE PEAD Scanner is Alive and Running!", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# =========================================================
# DATABASE LAYER
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            news_id TEXT PRIMARY KEY,
            scrip_cd TEXT,
            company TEXT,
            headline TEXT,
            pdf_url TEXT,
            inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

def load_seen_news(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT news_id FROM announcements ORDER BY inserted_at DESC LIMIT 5000")
    return deque([x[0] for x in cursor.fetchall()], maxlen=5000)

def save_announcement(conn, item, pdf_url):
    conn.execute("""
        INSERT OR IGNORE INTO announcements (news_id, scrip_cd, company, headline, pdf_url) 
        VALUES (?, ?, ?, ?, ?)
    """, (item.get("NEWSID"), str(item.get("SCRIP_CD")), item.get("SLONGNAME"), item.get("HEADLINE"), pdf_url))
    conn.commit()

# =========================================================
# COMMUNICATIONS LAYER
# =========================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID, 
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
    except Exception as e:
        print("Telegram Error:", e, flush=True)

def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.bseindia.com/", timeout=10)
        return session
    except:
        return None

# =========================================================
# QUANTITATIVE SCORING ENGINE
# =========================================================
def get_technical_data(scrip):
    """Fetches Market Cap and Momentum via Yahoo Finance"""
    try:
        ticker = yf.Ticker(f"{scrip}.BO")
        info = ticker.info
        hist = ticker.history(period="1y")
        
        if hist.empty:
            return None
            
        mcap = info.get("marketCap", 0) / 10000000 # Convert to Crores
        current_price = hist['Close'].iloc[-1]
        dma_200 = hist['Close'].rolling(window=200).mean().iloc[-1]
        vol_today = hist['Volume'].iloc[-1]
        vol_avg_20 = hist['Volume'].rolling(window=20).mean().iloc[-1]
        
        return {
            "mcap_cr": mcap,
            "above_200dma": current_price > dma_200 if not hist['Close'].rolling(window=200).mean().isna().iloc[-1] else False,
            "vol_surge": (vol_today / vol_avg_20) > 2 if vol_avg_20 > 0 else False
        }
    except Exception as e:
        print(f"YFinance Error for {scrip}: {e}", flush=True)
        return None

def calculate_advanced_pead_score(data, tech_data, company_name):
    """The master quant engine evaluating momentum, quality, and sector parameters"""
    score = 0
    flags = []
    
    # 1. Quality Filter (Microcap elimination)
    if tech_data and tech_data.get("mcap_cr", 0) < 500:
        return -100, "❌ MICROCAP NOISE (< ₹500Cr) - SKIPPING"

    # 2. Fundamental Layer
    if data and data.get('consolidated'):
        c = data['consolidated']
        pat_yoy = c.get('pat_yoy_pct', 0)
        pat_qoq = c.get('pat_qoq_pct', 0)
        rev_yoy = c.get('revenue_yoy_pct', 0)
        margin = c.get('ebitda_margin_pct', 0)

        if pat_yoy > 40:
            score += 20
            flags.append("🔥 Massive YoY Profit Beat")
        if pat_qoq > 15:
            score += 15
            flags.append("⚡ Strong QoQ Acceleration")
        if rev_yoy > 20:
            score += 10
            flags.append("📈 Topline Expansion")
        if margin > 15:
            score += 5
            flags.append("💎 High Margin Business")

    # 3. Momentum Layer
    if tech_data:
        if tech_data.get("above_200dma"):
            score += 10
            flags.append("🐂 Long-Term Uptrend (Above 200 DMA)")
        if tech_data.get("vol_surge"):
            score += 15
            flags.append("🌊 Institutional Volume Surge (>2x Avg)")

    # 4. Sector Multiplier 
    power_defense_keywords = ["POWER", "ENERGY", "DEFENCE", "AERO", "INFRA", "TRANSFORMER"]
    if any(word in company_name.upper() for word in power_defense_keywords):
        score = int(score * 1.5)
        flags.insert(0, "🛡️ [HIGH-PRIORITY SECTOR]")

    return score, "\n".join(flags)

# =========================================================
# AI WORKER (CONSUMER THREAD)
# =========================================================
def ai_worker_loop():
    session = create_session()
    while True:
        item = analysis_queue.get()
        
        company = item.get("SLONGNAME")
        scrip = item.get("SCRIP_CD")
        headline = item.get("HEADLINE")
        pdf_url = item.get("PDF_URL")
        
        print(f"\n[AI WORKER] Analyzing {company}...", flush=True)
        
        try:
            # 1. Tech & Momentum Fetch
            tech_data = get_technical_data(scrip)
            
            # 2. Download PDF
            pdf_bytes = None
            if pdf_url != "No PDF":
                resp = session.get(pdf_url, stream=True, timeout=10)
                pdf_bytes = resp.content

            # 3. LLM Extraction (Strict JSON formatting)
            extracted_data = None
            if pdf_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    temp_pdf.write(pdf_bytes)
                    temp_pdf_path = temp_pdf.name
                
                json_schema = {
                    "type": "OBJECT",
                    "properties": {
                        "consolidated": {
                            "type": "OBJECT",
                            "properties": {
                                "revenue_cr": {"type": "NUMBER"},
                                "revenue_yoy_pct": {"type": "NUMBER"},
                                "revenue_qoq_pct": {"type": "NUMBER"},
                                "pat_cr": {"type": "NUMBER"},
                                "pat_yoy_pct": {"type": "NUMBER"},
                                "pat_qoq_pct": {"type": "NUMBER"},
                                "ebitda_margin_pct": {"type": "NUMBER"}
                            }
                        }
                    }
                }
                    
                gemini_file = client.files.upload(file=temp_pdf_path, config={'mime_type': 'application/pdf'})
                prompt = "Extract financials in Crores. Calculate YoY and QoQ percentages. Extract EBITDA margin percentage."
                
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[gemini_file, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=json_schema, temperature=0.0)
                )
                client.files.delete(name=gemini_file.name)
                os.remove(temp_pdf_path)
                extracted_data = json.loads(response.text)

            # 4. Quant Engine Scoring
            score, flags = calculate_advanced_pead_score(extracted_data, tech_data, company)
            
            # Hide microcap noise completely from Telegram
            if score == -100:
                print(f"[REJECTED] {company} is below market cap threshold.")
                analysis_queue.task_done()
                continue
            
            # 5. Alert Generation
            msg = f"🚀 *{company}* ({scrip})\n\n"
            if flags:
                msg += f"{flags}\n*🧠 PEAD SCORE: {score}/100*\n\n"
                
            if extracted_data and extracted_data.get('consolidated'):
                c = extracted_data['consolidated']
                msg += f"📊 *METRICS*\n"
                msg += f"Rev: ₹{c.get('revenue_cr',0):.1f}Cr (YoY: {c.get('revenue_yoy_pct',0):+.1f}%, QoQ: {c.get('revenue_qoq_pct',0):+.1f}%)\n"
                msg += f"PAT: ₹{c.get('pat_cr',0):.1f}Cr (YoY: {c.get('pat_yoy_pct',0):+.1f}%, QoQ: {c.get('pat_qoq_pct',0):+.1f}%)\n"
                msg += f"Margin: {c.get('ebitda_margin_pct',0):.1f}%\n\n"
            
            if tech_data:
                msg += f"📈 *Mkt Cap:* ₹{tech_data.get('mcap_cr', 0):.0f} Cr\n\n"
            
            msg += f"[📄 View PDF]({pdf_url})"
            send_telegram(msg)
            
        except Exception as e:
            error_str = str(e)
            print(f"[AI WORKER ERROR] {error_str}", flush=True)
            
            # THE FIX: Rate Limit Handler & Automatic Re-queueing
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                print("⚠️ Hit Gemini Rate Limit! Sleeping for 60 seconds...", flush=True)
                time.sleep(60)
                print(f"🔄 Re-queueing {company} so we don't lose it...", flush=True)
                analysis_queue.put(item)
            else:
                send_telegram(f"🔔 *{company}*\n{headline}\n[📄 View PDF]({pdf_url})")
            
        finally:
            # THE FIX: Guarantee a 5-second pace between PDFs so we don't hit the limit
            time.sleep(5)
            analysis_queue.task_done()

# =========================================================
# FAST SCANNER (PRODUCER THREAD)
# =========================================================
def process_item_fast(conn, item, seen_news):
    news_id = item.get("NEWSID")
    if not news_id or news_id in seen_news:
        return False

    seen_news.append(news_id)
    filename = item.get("ATTACHMENTNAME", "")
    pdf_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{filename}" if filename else "No PDF"
    save_announcement(conn, item, pdf_url)
    
    # Send all "Result" filings to the AI Worker
    if item.get("CATEGORYNAME") == "Result":
        item["PDF_URL"] = pdf_url
        analysis_queue.put(item)
    return True

def main():
    conn = init_db()
    seen_news = load_seen_news(conn)
    session = create_session()

    # START THE BACKGROUND SERVICES
    threading.Thread(target=run_web_server, daemon=True).start() # Render keep-alive
    threading.Thread(target=ai_worker_loop, daemon=True).start() # AI Processing

    print("\n" + "=" * 80)
    print("V3.0 QUANTITATIVE PEAD ENGINE ONLINE")
    print("=" * 80 + "\n")

    while True:
        try:
            if session is None:
                time.sleep(10)
                session = create_session()
                continue

            today = datetime.now().strftime("%Y%m%d")
            page_no = 1
            new_count = 0
            
            # THE FIX: PAGINATION LOOP
            while True:
                response = session.get(BASE_URL, params={
                    "pageno": page_no, "strCat": "Result", "strSearch": "P", 
                    "strToDate": today, "strPrevDate": today, "strType": "C"
                }, timeout=10)
                
                data = response.json()
                table = data.get("Table", [])
                
                if not table:
                    break
                    
                total_pages = int(table[0].get("TotalPageCnt", 1))
                all_old = True
                
                for item in table:
                    if process_item_fast(conn, item, seen_news):
                        new_count += 1
                        all_old = False
                        
                if all_old or page_no >= total_pages:
                    break
                    
                page_no += 1 
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Pulse | New: {new_count} | Queue: {analysis_queue.qsize()}", flush=True)
            time.sleep(45)

        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    main()
