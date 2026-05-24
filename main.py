import os
import re
import json
import time
import sqlite3
import tempfile
import requests
import threading
import yfinance as yf
import pdfplumber
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

# BOUNDED QUEUE: Prevents memory leaks during network/AI outages
analysis_queue = Queue(maxsize=100)

# =========================================================
# FLASK KEEP-ALIVE SERVER (FOR RENDER.COM)
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "BSE Hybrid PEAD Scanner is Alive and Running!", 200

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
# QUANTITATIVE LAYER 1: LOCAL DETERMINISTIC PARSER
# =========================================================
def extract_financials_locally(pdf_path):
    """
    Lightning-fast regex extraction using pdfplumber.
    Costs $0 and takes milliseconds.
    """
    extracted = {
        "revenue_cr": 0.0,
        "pat_cr": 0.0,
        "found": False
    }
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            # Only scan the first 4 pages to maximize speed
            for page in pdf.pages[:4]:
                text += page.extract_text() + "\n"
                
            rev_match = re.search(r"(?:Total Income|Revenue from operations)[\s\S]{1,50}?([\d,]+\.\d+)", text, re.IGNORECASE)
            pat_match = re.search(r"(?:Profit for the period|Profit after tax|Net Profit)[\s\S]{1,50}?([\d,]+\.\d+)", text, re.IGNORECASE)
            
            if rev_match and pat_match:
                rev_val = float(rev_match.group(1).replace(",", ""))
                pat_val = float(pat_match.group(1).replace(",", ""))
                
                # Auto-convert Lakhs to Crores if number is massive
                if rev_val > 5000: 
                    rev_val = rev_val / 100
                    pat_val = pat_val / 100
                    
                extracted["revenue_cr"] = rev_val
                extracted["pat_cr"] = pat_val
                extracted["found"] = True
                
    except Exception as e:
        print(f"[LOCAL PARSE ERROR] {e}", flush=True)
        
    return extracted

# =========================================================
# QUANTITATIVE LAYER 2: MOMENTUM & SCORING
# =========================================================
def get_technical_data(scrip):
    try:
        ticker = yf.Ticker(f"{scrip}.BO")
        info = ticker.info
        hist = ticker.history(period="1y")
        
        if hist.empty:
            return None
            
        mcap = info.get("marketCap", 0) / 10000000 
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

# =========================================================
# AI WORKER (CONSUMER THREAD - THE HYBRID PIPELINE)
# =========================================================
def ai_worker_loop():
    session = create_session()
    while True:
        item = analysis_queue.get()
        
        company = item.get("SLONGNAME")
        scrip = item.get("SCRIP_CD")
        headline = item.get("HEADLINE")
        pdf_url = item.get("PDF_URL")
        temp_pdf_path = None
        
        print(f"\n[PIPELINE] Processing {company}...", flush=True)
        
        try:
            # 1. Fetch Technicals
            tech_data = get_technical_data(scrip)
            
            # Reject Microcaps instantly (saves bandwidth and CPU)
            if tech_data and tech_data.get("mcap_cr", 0) < 500:
                print(f"[REJECTED] {company} is a Microcap (< ₹500Cr). Skipping.")
                analysis_queue.task_done()
                continue
            
            # 2. Download PDF Robustly
            pdf_bytes = None
            if pdf_url != "No PDF":
                resp = session.get(pdf_url, stream=True, timeout=15)
                resp.raise_for_status() # Catches 403s and empty HTML blocks
                pdf_bytes = resp.content

            if not pdf_bytes:
                raise ValueError("Empty PDF Bytes")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                temp_pdf.write(pdf_bytes)
                temp_pdf_path = temp_pdf.name
            
            # 3. Layer 1: Fast Local Parse
            local_data = extract_financials_locally(temp_pdf_path)
            gemini_insights = ""
            
            if local_data["found"]:
                print(f"   -> Local Parse Success! PAT: ₹{local_data['pat_cr']:.2f} Cr")
                
                # Layer 2: The Gatekeeper (Defining a "Strong Result")
                # *Note: As you improve the regex to grab YoY%, update this condition!
                is_high_priority = (local_data['pat_cr'] > 25.0) and (tech_data and tech_data.get('above_200dma'))
                
                # Layer 3: Targeted AI Insights
                if is_high_priority:
                    print("   -> 🔥 High Priority! Waking up Gemini for Guidance/Orderbook extraction...")
                    gemini_file = client.files.upload(file=temp_pdf_path, config={'mime_type': 'application/pdf'})
                    
                    prompt = """
                    You are an expert equity researcher. Scan this PDF and extract only:
                    1. Management Guidance for the upcoming quarters.
                    2. Order book size or major contract wins.
                    3. Capex (capital expenditure) plans.
                    Be extremely concise. Use bullet points. If not mentioned, state 'Not explicitly mentioned.'
                    """
                    
                    response = client.models.generate_content(
                        model='gemini-2.0-flash',
                        contents=[gemini_file, prompt],
                    )
                    client.files.delete(name=gemini_file.name)
                    gemini_insights = f"\n\n🧠 *GEMINI ALPHA INSIGHTS*\n{response.text}"
                else:
                    print("   -> Standard result. Bypassing Gemini.")
            
            else:
                # FALLBACK: If PDF is scanned or regex fails, use Gemini as backup parser
                print("   -> Local Parse Failed. Falling back to Gemini Extraction...")
                gemini_file = client.files.upload(file=temp_pdf_path, config={'mime_type': 'application/pdf'})
                
                json_schema = {
                    "type": "OBJECT",
                    "properties": {
                        "consolidated": {
                            "type": "OBJECT",
                            "properties": {
                                "revenue_cr": {"type": "NUMBER"},
                                "pat_cr": {"type": "NUMBER"}
                            }
                        }
                    }
                }
                
                prompt = "Extract financials in Crores."
                response = client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[gemini_file, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=json_schema, temperature=0.0)
                )
                client.files.delete(name=gemini_file.name)
                
                ai_data = json.loads(response.text)
                if ai_data and ai_data.get('consolidated'):
                    local_data['revenue_cr'] = ai_data['consolidated'].get('revenue_cr', 0)
                    local_data['pat_cr'] = ai_data['consolidated'].get('pat_cr', 0)
                    local_data['found'] = True
            
            # 4. Generate Telegram Alert
            msg = f"🚀 *{company}* ({scrip})\n\n"
            if tech_data:
                msg += f"📈 *Mkt Cap:* ₹{tech_data.get('mcap_cr', 0):.0f} Cr\n"
                if tech_data.get('vol_surge'): msg += f"🌊 Volume Surge Detected\n"
                
            if local_data["found"]:
                msg += f"\n📊 *METRICS (Current Qtr)*\nRev: ₹{local_data['revenue_cr']:.2f} Cr\nPAT: ₹{local_data['pat_cr']:.2f} Cr\n"
            
            msg += gemini_insights
            msg += f"\n[📄 View Original PDF]({pdf_url})"
            
            send_telegram(msg)
            
        except Exception as e:
            error_str = str(e)
            print(f"[PIPELINE ERROR] {error_str}", flush=True)
            
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                item["retry_count"] = item.get("retry_count", 0) + 1
                
                if item["retry_count"] <= 3:
                    print(f"⚠️ Rate Limit! Sleeping 60s... Re-queueing {company} (Attempt {item['retry_count']}/3)", flush=True)
                    time.sleep(60)
                    analysis_queue.put(item)
                else:
                    send_telegram(f"🔔 *{company}* (AI Failed)\n{headline}\n[📄 View PDF]({pdf_url})")
            else:
                send_telegram(f"🔔 *{company}* (System Error)\n{headline}\n[📄 View PDF]({pdf_url})")
            
        finally:
            # Bulletproof cleanup to prevent Ghost Files filling up the server SSD
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                try:
                    os.remove(temp_pdf_path)
                except:
                    pass
            
            time.sleep(2)
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
    
    if item.get("CATEGORYNAME") == "Result":
        item["PDF_URL"] = pdf_url
        analysis_queue.put(item) 
    return True

def main():
    conn = init_db()
    seen_news = load_seen_news(conn)
    session = create_session()

    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=ai_worker_loop, daemon=True).start()

    print("\n" + "=" * 80)
    print("V5.0 HYBRID QUANTITATIVE PEAD ENGINE ONLINE")
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
