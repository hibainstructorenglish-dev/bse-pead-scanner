# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v6.2
# TELEGRAM FIX EDITION
# =========================================================

import io
import gc
import json
import time
import sqlite3
import requests
import pdfplumber

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"

CHECK_INTERVAL = 60

DB_NAME = "pead_results.db"

MIN_PEAD_SCORE = 25

# =========================================================
# OPENAI
# =========================================================

client = OpenAI(
    api_key=OPENAI_API_KEY
)

# =========================================================
# REQUEST SESSION
# =========================================================

session = requests.Session()

session.headers.update({

    "User-Agent":
    "Mozilla/5.0",

    "Referer":
    "https://www.bseindia.com/",

    "Origin":
    "https://www.bseindia.com"
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

        quarter TEXT,

        revenue_growth REAL,

        pat_growth REAL,

        pead_score INTEGER,

        theme TEXT,

        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP

    )

    """)

    conn.commit()

    conn.close()

# =========================================================
# TELEGRAM MESSAGE
# =========================================================

def send_telegram_message(msg):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(

            url,

            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:3500]
            },

            timeout=20
        )

        print("TELEGRAM MESSAGE:")
        print(response.status_code)
        print(response.text)

    except Exception as e:

        print("Telegram Message Error:", e)

# =========================================================
# TELEGRAM PHOTO
# =========================================================

def send_telegram_photo(image_bytes, caption):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendPhoto"
    )

    try:

        response = requests.post(

            url,

            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:300]
            },

            files={
                "photo":
                (
                    "dashboard.png",
                    image_bytes,
                    "image/png"
                )
            },

            timeout=30
        )

        print("\n📤 TELEGRAM PHOTO RESPONSE")
        print(response.status_code)
        print(response.text)

        if response.status_code == 200:
            return True

        return False

    except Exception as e:

        print("Telegram Photo Error:", e)

        return False

# =========================================================
# FETCH BSE RESULTS
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

    url = (
        "https://api.bseindia.com/"
        "BseIndiaAPI/api/"
        "AnnSubCategoryGetData/w"
    )

    try:

        response = session.get(
            url,
            params=params,
            timeout=20
        )

        data = response.json()

        return data.get("Table", [])

    except Exception as e:

        print("BSE Fetch Error:", e)

        return []

# =========================================================
# DOWNLOAD PDF
# =========================================================

def download_pdf(pdf_url):

    try:

        response = session.get(
            pdf_url,
            timeout=20
        )

        response.raise_for_status()

        return response.content

    except Exception as e:

        print("PDF Download Error:", e)

        return None

# =========================================================
# FINANCIAL PAGE EXTRACTION
# =========================================================

def extract_financial_pages(pdf_bytes):

    keywords = [

        "revenue from operations",
        "statement of audited financial results",
        "statement of unaudited financial results",
        "profit for the period",
        "profit before tax",
        "total income",
        "earnings per share",
        "financial results",
        "ebitda"
    ]

    extracted_text = ""

    MAX_TEXT = 12000

    try:

        with pdfplumber.open(
            io.BytesIO(pdf_bytes)
        ) as pdf:

            for idx, page in enumerate(pdf.pages):

                text = page.extract_text()

                if not text:
                    continue

                lower = text.lower()

                score = 0

                for keyword in keywords:

                    if keyword in lower:
                        score += 1

                if score >= 1:

                    print(
                        f"✅ Financial Page Detected: "
                        f"Page {idx+1}"
                    )

                    extracted_text += (
                        f"\n\n--- PAGE {idx+1} ---\n\n"
                    )

                    extracted_text += text

                    if len(extracted_text) > MAX_TEXT:
                        break

    except Exception as e:

        print("PDF Extraction Error:", e)

    return extracted_text

# =========================================================
# GPT EXTRACTION
# =========================================================

def gpt_extract(financial_text):

    prompt = f"""
Extract latest quarterly earnings.

Return ONLY JSON.

{{
    "company_name": "",
    "quarter": "",

    "sector": "",
    "industry": "",

    "revenue_yoy_growth_pct": 0,
    "pat_yoy_growth_pct": 0,
    "pat_qoq_growth_pct": 0,

    "ebitda_margin_pct": 0,

    "management_commentary": "",
    "order_book": "",
    "red_flags": "",

    "confidence_score": 0
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
                    "content":
                    "You are a financial extraction engine."
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
        )

        content = content.replace(
            "```json",
            ""
        )

        content = content.replace(
            "```",
            ""
        )

        data = json.loads(content)

        return data

    except Exception as e:

        print("GPT Error:", e)

        return None

# =========================================================
# VALIDATION
# =========================================================

def validate(data):

    confidence = data.get(
        "confidence_score",
        0
    )

    if confidence < 40:
        return False

    return True

# =========================================================
# THEME ENGINE
# =========================================================

def identify_theme_and_score(sector, industry):

    text = f"{sector} {industry}".lower()

    if "power" in text:
        return "Power Infra", 9

    if "renewable" in text:
        return "Renewable", 9

    if "defense" in text:
        return "Defense", 8

    if "pharma" in text:
        return "Healthcare", 6

    if "real estate" in text:
        return "Real Estate", 5

    return "Other", 3

# =========================================================
# PEAD ENGINE
# =========================================================

def calculate_pead(data, theme_score):

    score = 0

    rev_growth = data.get(
        "revenue_yoy_growth_pct",
        0
    )

    pat_growth = data.get(
        "pat_yoy_growth_pct",
        0
    )

    qoq_growth = data.get(
        "pat_qoq_growth_pct",
        0
    )

    # HALLUCINATION FILTERS

    if abs(rev_growth) > 300:
        rev_growth = 0

    if abs(pat_growth) > 500:
        pat_growth = 0

    if abs(qoq_growth) > 300:
        qoq_growth = 0

    # REVENUE

    if rev_growth > 30:
        score += 15

    elif rev_growth > 15:
        score += 10

    # PAT

    if pat_growth > 50:
        score += 25

    elif pat_growth > 20:
        score += 15

    # QOQ

    if qoq_growth > 15:
        score += 10

    # EBITDA

    if data.get(
        "ebitda_margin_pct",
        0
    ) > 20:

        score += 8

    score += theme_score

    return {

        "score":
        max(min(score, 100), 0),

        "rev_growth":
        rev_growth,

        "pat_growth":
        pat_growth,

        "qoq_growth":
        qoq_growth
    }

# =========================================================
# DASHBOARD
# =========================================================

def generate_dashboard(data, pead, theme):

    img = Image.new(
        "RGB",
        (900, 520),
        (15, 23, 42)
    )

    draw = ImageDraw.Draw(img)

    try:

        title_font = ImageFont.load_default()

        data_font = ImageFont.load_default()

        score_font = ImageFont.load_default()

    except:

        title_font = data_font = score_font = (
            ImageFont.load_default()
        )

    white = (255,255,255)
    green = (34,197,94)
    cyan = (45,212,191)

    draw.text(
        (30,30),
        data["company_name"],
        fill=white,
        font=title_font
    )

    draw.text(
        (30,80),
        f"Theme: {theme}",
        fill=cyan,
        font=data_font
    )

    draw.text(
        (650,30),
        f"PEAD {pead['score']}",
        fill=cyan,
        font=score_font
    )

    draw.text(
        (30,180),
        f"Revenue Growth: {pead['rev_growth']:+.1f}%",
        fill=green,
        font=data_font
    )

    draw.text(
        (30,260),
        f"PAT Growth: {pead['pat_growth']:+.1f}%",
        fill=green,
        font=data_font
    )

    draw.text(
        (30,340),
        f"QoQ PAT: {pead['qoq_growth']:+.1f}%",
        fill=green,
        font=data_font
    )

    img_bytes = io.BytesIO()

    img.save(
        img_bytes,
        format="PNG"
    )

    img_bytes.seek(0)

    return img_bytes

# =========================================================
# MAIN ENGINE
# =========================================================

seen = set()

def main():

    init_db()

    print("=" * 60)
    print("🚀 GPT PEAD ENGINE v6.2")
    print("=" * 60)

    while True:

        try:

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

                print("\n" + "=" * 60)
                print("🚀 NEW RESULT")
                print("=" * 60)

                print("Company:", company)

                pdf_url = (
                    "https://www.bseindia.com/"
                    "xml-data/corpfiling/AttachLive/"
                    + attachment
                )

                # DOWNLOAD PDF

                pdf_bytes = download_pdf(
                    pdf_url
                )

                if not pdf_bytes:

                    print("❌ PDF Failed")

                    continue

                # EXTRACT TEXT

                financial_text = extract_financial_pages(
                    pdf_bytes
                )

                if len(financial_text) < 500:

                    print(
                        "❌ No Financial Data"
                    )

                    continue

                # GPT

                data = gpt_extract(
                    financial_text
                )

                if not data:

                    print("❌ GPT Failed")

                    continue

                # VALIDATION

                if not validate(data):

                    print("❌ Validation Failed")

                    continue

                # THEME

                theme, theme_score = (

                    identify_theme_and_score(

                        data.get(
                            "sector",
                            ""
                        ),

                        data.get(
                            "industry",
                            ""
                        )
                    )
                )

                # PEAD

                pead = calculate_pead(
                    data,
                    theme_score
                )

                print("DEBUG PEAD:", pead)

                # SCORE FILTER

                if pead["score"] < MIN_PEAD_SCORE:

                    print("❌ Low PEAD Score")

                    continue

                # DASHBOARD

                dashboard = generate_dashboard(
                    data,
                    pead,
                    theme
                )

                # CAPTION

                caption = (

                    f"{company}\n"
                    f"PEAD: {pead['score']}\n"
                    f"Revenue: {pead['rev_growth']:+.1f}%\n"
                    f"PAT: {pead['pat_growth']:+.1f}%\n"
                    f"Theme: {theme}"
                )

                print("📤 Sending Telegram Dashboard...")

                # PHOTO

                photo_ok = send_telegram_photo(
                    dashboard,
                    caption
                )

                if photo_ok:

                    print("✅ PHOTO SENT")

                else:

                    print("❌ PHOTO FAILED")

                # COMMENTARY

                commentary = (

                    f"Commentary:\n\n"
                    f"{data.get('management_commentary', '')}\n\n"

                    f"Order Book:\n\n"
                    f"{data.get('order_book', '')}\n\n"

                    f"Red Flags:\n\n"
                    f"{data.get('red_flags', '')}"
                )

                send_telegram_message(
                    commentary
                )

                print("✅ ALERT SENT")

                gc.collect()

                time.sleep(2)

            print(

                f"\n[{time.strftime('%H:%M:%S')}] "
                f"Alive | Seen={len(seen)}"
            )

        except Exception as e:

            print("MAIN LOOP ERROR:", e)

        gc.collect()

        time.sleep(CHECK_INTERVAL)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
