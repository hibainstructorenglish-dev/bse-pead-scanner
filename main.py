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
from openai import OpenAI

# =========================================================
# CONFIGURATION
# =========================================================
TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"
# Best Practice: Pull from Render Env Vars, fallback to string if local
# The script will look for the key in Render's secure vault
OPENAI_API_KEY = os.environ.get("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
")
DB_NAME = "bse_results.db"
BASE_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136.0.0.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

print("Initializing OpenAI Client...", flush=True)
client = OpenAI(api_key=OPENAI_API_KEY)

analysis_queue = Queue(maxsize=100)
yf_cache = {}

# =========================================================
# FLASK KEEP-ALIVE SERVER 
# =========================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "BSE PEAD Scanner V7 (OpenAI) is Alive!", 200

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
def escape_md(text):
    if not text: return ""
    return str(text).replace("*", "").replace("_", "").replace("[", "").replace("]", "").replace("`", "")

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
    extracted = {"revenue_cr": 0.0, "pat_cr": 0.0, "found": False, "raw_text": ""}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = ""
            for page in pdf.pages[:4]:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            
            extracted["raw_text"] = text # Save text so OpenAI doesn't have to re-read the PDF!
            
            rev_match = re.search(r"(?:Total Income|Revenue from operations)[\s\S]{1,50}?([\d,]+\.\d+)", text, re.IGNORECASE)
            pat_match = re.search(r"(?:Profit for the period|Profit after tax|Net Profit)[\s\S]{1,50}?([\d,]+\.\d+)", text, re.IGNORECASE)
            
            if rev_match and pat_match:
                rev_val = float(rev_match.group(1).replace(",", ""))
                pat_val = float(pat_match.group(1).replace(",", ""))
                
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
# QUANTITATIVE LAYER 2: SCORING & CACHING
# =========================================================
def get_technical_data(scrip):
    global yf_cache
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{scrip}_{today}"
    
    if cache_key in yf_cache:
        return yf_cache[cache_key]

    try:
        ticker = yf.Ticker(f"{scrip}.BO")
        hist = ticker.history(period="1y")
        
        if hist.empty:
            return None
            
        mcap = ticker.info.get("marketCap", 0) / 10000000 
        current_price = hist['Close'].iloc[-1]
        dma_200 = hist['Close'].rolling(window=200).mean().iloc[-1]
        vol_today = hist['Volume'].iloc[-1]
        vol_avg_20 = hist['Volume'].rolling(window=20).mean().iloc[-1]
        
        data = {
            "mcap_cr": mcap,
            "above_200dma": current_price > dma_200 if not pd.isna(dma_200) else False,
            "vol_surge": (vol_today / vol_avg_20) > 2 if vol_avg_20 > 0 else False
        }
        yf_cache[cache_key] = data
        return data
    except Exception as e:
        print(f"YFinance Error for {scrip}: {e}", flush=True)
        return None

# =========================================================
# AI WORKER (CONSUMER THREAD - OPENAI PIPELINE)
# =========================================================
def ai_worker_loop():
    session = create_session()
    while True:
        item = analysis_queue.get()
        company = escape_md(item.get("SLONGNAME"))
        scrip = item.get("SCRIP_CD")
        headline = escape_md(item.get("HEADLINE"))
        pdf_url = item.get("PDF_URL")
        temp_pdf_path = None
        
        print(f"\n[PIPELINE] Processing {company}...", flush=True)
        
        try:
            tech_data = get_technical_data(scrip)
            
            if tech_data and tech_data.get("mcap_cr", 0) < 500:
                print(f"[REJECTED] {company} is a Microcap. Skipping.")
                # UNCOMMENT NEXT LINE TO ENABLE PRODUCTION MODE (Skips microcaps)
                # continue 
            
            pdf_bytes = None
            if pdf_url != "No PDF":
                resp = session.get(pdf_url, stream=True, timeout=15)
                resp.raise_for_status() 
                pdf_bytes = resp.content

            if not pdf_bytes:
                raise ValueError("Empty PDF")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                temp_pdf.write(pdf_bytes)
                temp_pdf_path = temp_pdf.name
            
            local_data = extract_financials_locally(temp_pdf_path)
            ai_insights = ""
            
            if local_data["found"]:
                print(f"   -> Local Parse Success! PAT: ₹{local_data['pat_cr']:.2f} Cr")
                
                is_high_priority = (local_data['pat_cr'] > 15.0) and (tech_data and tech_data.get('vol_surge'))
                
                if is_high_priority:
                    print("   -> 🔥 High Priority! Sending text to OpenAI for Qualitative extraction...")
                    
                    response = client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {"role": "system", "content": "You are an expert equity analyst. Extract concise bullet points."},
                            {"role": "user", "content": f"Extract:\n1. Management guidance\n2. Order book updates\n3. Capex plans\n\nFrom this earnings text:\n{local_data['raw_text'][:12000]}"}
                        ],
                        temperature=0
                    )
                    ai_insights = f"\n\n🧠 *QUALITATIVE INSIGHTS*\n{escape_md(response.choices[0].message.content)}"
                else:
                    print("   -> Standard result. Bypassing OpenAI to save API quota.")
            else:
                print("   -> Local Parse Failed. Falling back to OpenAI JSON Extraction...")
                
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "You extract financial data. Always return JSON matching: {\"consolidated\": {\"revenue_cr\": 0.0, \"pat_cr\": 0.0}}"},
                        {"role": "user", "content": f"Extract Revenue and PAT in Crores from this text:\n{local_data['raw_text'][:15000]}"}
                    ],
                    temperature=0
                )
                
                ai_data = json.loads(response.choices[0].message.content)
                if ai_data and ai_data.get('consolidated'):
                    local_data['revenue_cr'] = ai_data['consolidated'].get('revenue_cr', 0)
                    local_data['pat_cr'] = ai_data['consolidated'].get('pat_cr', 0)
                    local_data['found'] = True
            
            msg = f"🚀 *{company}* ({scrip})\n\n"
            if tech_data:
                msg += f"📈 *Mkt Cap:* ₹{tech_data.get('mcap_cr', 0):.0f} Cr\n"
                if tech_data.get('vol_surge'): msg += f"🌊 Volume Surge Detected\n"
                
            if local_data["found"]:
                msg += f"\n📊 *CURRENT QTR METRICS*\nRev: ₹{local_data['revenue_cr']:.2f} Cr\nPAT: ₹{local_data['pat_cr']:.2f} Cr\n"
            
            msg += ai_insights
            msg += f"\n[📄 View PDF]({pdf_url})"
            
            send_telegram(msg)
            
        except Exception as e:
            error_str = str(e)
            print(f"[PIPELINE ERROR] {error_str}", flush=True)
            
            # OpenAI specific rate limit error catching
            if "429" in error_str or "RateLimitError" in error_str:
                item["retry_count"] = item.get("retry_count", 0) + 1
                if item["retry_count"] <= 3:
                    print(f"⚠️ OpenAI Rate Limit! Re-queueing {company} (Attempt {item['retry_count']}/3)...", flush=True)
                    time.sleep(20) # OpenAI limits usually reset faster than Gemini
                    analysis_queue.put(item)
                else:
                    send_telegram(f"🔔 *{company}* (AI Exhausted)\n{headline}\n[📄 View PDF]({pdf_url})")
            else:
                send_telegram(f"🔔 *{company}* (Parse Error)\n{headline}\n[📄 View PDF]({pdf_url})")
            
        finally:
            if temp_pdf_path and os.path.exists(temp_pdf_path):
                try: os.remove(temp_pdf_path)
                except: pass
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
    print("V7.0 OPENAI EVENT-DRIVEN PEAD ENGINE ONLINE")
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
                
                if not table: break
                    
                total_pages = int(table[0].get("TotalPageCnt", 1))
                all_old = True
                
                for item in table:
                    if process_item_fast(conn, item, seen_news):
                        new_count += 1
                        all_old = False
                        
                if all_old or page_no >= total_pages: break
                page_no += 1 
            
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Pulse | New: {new_count} | Queue: {analysis_queue.qsize()}", flush=True)
            time.sleep(45)

        except Exception as e:
            time.sleep(10)

if __name__ == "__main__":
    import pandas as pd
    main()
