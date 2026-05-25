# =========================================================
# INSTITUTIONAL GPT PEAD ENGINE v6.1
# FINAL CLEAN STABLE VERSION
# =========================================================

import io
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

TELEGRAM_TOKEN = "8841109141:AAHc002BrBRD3Y5-7pBRAKQgxPBRVkeGJ_U"
TELEGRAM_CHAT_ID = "7630276313"

OPENAI_API_KEY = "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"

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
# TELEGRAM TEXT
# =========================================================

def send_telegram_message(msg):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    try:

        requests.post(

            url,

            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:3900]
            },

            timeout=20
        )

    except Exception as e:

        print("Telegram Error:", e)

# =========================================================
# TELEGRAM PHOTO
# =========================================================

def send_telegram_photo(image_bytes, caption):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendPhoto"
    )

    try:

        requests.post(

            url,

            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:900]
            },

            files={
                "photo":
                (
                    "dashboard.png",
                    image_bytes,
                    "image/png"
                )
            },

            timeout=20
        )

    except Exception as e:

        print("Telegram Photo Error:", e)

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
# SMART PAGE EXTRACTION
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

                # RELAXED FILTER
                if score >= 1:

                    print(
                        f"✅ Financial Page Detected: "
                        f"Page {idx+1}"
                    )

                    extracted_text += (
                        f"\n\n--- PAGE {idx+1} ---\n\n"
                    )

                    extracted_text += text

    except Exception as e:

        print("PDF Extraction Error:", e)

    return extracted_text

# =========================================================
# GPT EXTRACTION
# =========================================================

def gpt_extract(financial_text):

    prompt = f"""
You are a professional institutional equity analyst.

Extract ONLY latest quarterly earnings data.

IMPORTANT RULES:
- Use latest quarterly numbers
- Ignore yearly totals
- Ignore balance sheet
- Ignore cash flow
- Prefer consolidated results
- Use financial result table only
- Never hallucinate values
- Return 0 if unavailable

Return ONLY valid JSON.

FORMAT:

{{
    "company_name": "",
    "quarter": "",

    "sector": "",
    "industry": "",

    "revenue_current_cr": 0,
    "revenue_prev_q_cr": 0,
    "revenue_yoy_q_cr": 0,

    "pat_current_cr": 0,
    "pat_prev_q_cr": 0,
    "pat_yoy_q_cr": 0,

    "revenue_yoy_growth_pct": 0,
    "pat_yoy_growth_pct": 0,
    "pat_qoq_growth_pct": 0,

    "ebitda_margin_pct": 0,

    "guidance": "",
    "management_commentary": "",
    "order_book": "",
    "red_flags": "",

    "confidence_score": 0
}}

DOCUMENT:

{financial_text[:25000]}
"""

    try:

        response = client.chat.completions.create(

            model="gpt-4o-mini",

            messages=[

                {
                    "role": "system",
                    "content":
                    "You are an expert financial extraction engine."
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

        # =================================================
        # AUTO CONFIDENCE ENGINE
        # =================================================

        if (
            "confidence_score" not in data
            or
            data["confidence_score"] == 0
        ):

            confidence = 80

            if data.get(
                "revenue_current_cr",
                0
            ) <= 0:

                confidence -= 30

            if data.get(
                "pat_current_cr",
                0
            ) == 0:

                confidence -= 20

            if data.get(
                "revenue_yoy_q_cr",
                0
            ) == 0:

                confidence -= 10

            if data.get(
                "pat_yoy_q_cr",
                0
            ) == 0:

                confidence -= 10

            data["confidence_score"] = max(
                confidence,
                10
            )

        return data

    except Exception as e:

        print("GPT Error:", e)

        return None

# =========================================================
# VALIDATION
# =========================================================

def validate(data):

    try:

        confidence = data.get(
            "confidence_score",
            0
        )

        if confidence < 50:
            return False

        revenue = data["revenue_current_cr"]

        pat = data["pat_current_cr"]

        if revenue <= 0:
            return False

        if abs(pat) > revenue * 2:
            return False

        return True

    except:

        return False

# =========================================================
# THEME ENGINE
# =========================================================

def identify_theme_and_score(sector, industry):

    if not sector and not industry:
        return "Unclassified", 0

    text = f"{sector} {industry}".lower()

    # TIER 1
    if any(k in text for k in [

        'ai',
        'fiber',
        'optical',
        'software',
        'information technology',
        'communication equipment'

    ]):
        return "AI Infra/Fiber", 10

    if any(k in text for k in [

        'solar',
        'wind',
        'renewable',
        'clean energy'

    ]):
        return "Renewable", 9

    if any(k in text for k in [

        'power',
        'transmission',
        'transformer',
        'switchgear',
        'cable',
        'wires',
        'grid',
        'utility'

    ]):
        return "Power Infra", 9

    # TIER 2
    if any(k in text for k in [

        'defense',
        'aerospace'

    ]):
        return "Defense", 8

    if any(k in text for k in [

        'semiconductor',
        'electronics',
        'ems'

    ]):
        return "Electronics Manufacturing", 8

    if any(k in text for k in [

        'telecom',
        'telecommunications'

    ]):
        return "Telecom", 8

    # TIER 3
    if any(k in text for k in [

        'construction',
        'infrastructure',
        'capital goods'

    ]):
        return "Infra/Capital Goods", 7

    if any(k in text for k in [

        'industrial',
        'engineering',
        'machinery'

    ]):
        return "Industrial", 7

    # TIER 4
    if any(k in text for k in [

        'bank',
        'financial',
        'insurance'

    ]):
        return "Financial", 6

    if any(k in text for k in [

        'health',
        'pharmaceutical',
        'medical'

    ]):
        return "Healthcare", 6

    if any(k in text for k in [

        'chemical',
        'chemicals'

    ]):
        return "Chemical Specialty", 6

    if any(k in text for k in [

        'real estate',
        'property'

    ]):
        return "Real Estate", 5

    if any(k in text for k in [

        'fmcg',
        'food',
        'beverage'

    ]):
        return "Consumer Brand", 5

    if any(k in text for k in [

        'retail',
        'consumer cyclical'

    ]):
        return "Retail/Consumer", 5

    if any(k in text for k in [

        'logistics',
        'shipping',
        'transportation'

    ]):
        return "Logistics", 5

    if any(k in text for k in [

        'metal',
        'steel',
        'mining'

    ]):
        return "Metal/Commodity", 4

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

    # =====================================================
    # HALLUCINATION CLAMPS
    # =====================================================

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

    # PAT
    if pat_growth > 50:
        score += 25

    elif pat_growth > 20:
        score += 15

    # QoQ
    if qoq_growth > 15:
        score += 10

    # EBITDA
    if data.get(
        "ebitda_margin_pct",
        0
    ) > 20:

        score += 8

    commentary = (
        data.get("guidance", "")
        +
        data.get(
            "management_commentary",
            ""
        )
    ).lower()

    bullish_keywords = [

        "strong demand",
        "healthy pipeline",
        "capacity expansion",
        "record order",
        "growth momentum",
        "margin improvement",
        "positive outlook"
    ]

    bearish_keywords = [

        "margin pressure",
        "weak demand",
        "slowdown",
        "uncertainty",
        "decline",
        "loss"
    ]

    for word in bullish_keywords:

        if word in commentary:
            score += 3

    for word in bearish_keywords:

        if word in commentary:
            score -= 3

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

        title_font = ImageFont.truetype(
            "arial.ttf",
            34
        )

        data_font = ImageFont.truetype(
            "arial.ttf",
            24
        )

        score_font = ImageFont.truetype(
            "arial.ttf",
            60
        )

    except:

        title_font = data_font = score_font = (
            ImageFont.load_default()
        )

    white = (255,255,255)
    green = (34,197,94)
    red = (255,80,80)
    cyan = (45,212,191)

    draw.text(
        (30,30),
        data["company_name"],
        fill=white,
        font=title_font
    )

    draw.text(
        (30,85),
        f"Theme: {theme}",
        fill=cyan,
        font=data_font
    )

    draw.text(
        (700,20),
        "PEAD",
        fill=cyan,
        font=data_font
    )

    draw.text(
        (700,60),
        str(pead["score"]),
        fill=cyan,
        font=score_font
    )

    metrics = [

        ("Revenue Growth", pead["rev_growth"]),
        ("PAT Growth", pead["pat_growth"]),
        ("QoQ PAT", pead["qoq_growth"])
    ]

    y = 180

    for name, value in metrics:

        color = green if value >= 0 else red

        draw.text(
            (30,y),
            name,
            fill=white,
            font=data_font
        )

        draw.text(
            (350,y),
            f"{value:+.1f}%",
            fill=color,
            font=data_font
        )

        y += 70

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
    print("🚀 INSTITUTIONAL GPT PEAD ENGINE v6.1")
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

                headline = item.get(
                    "HEADLINE",
                    ""
                )

                attachment = item.get(
                    "ATTACHMENTNAME",
                    ""
                )

                if not attachment.endswith(".pdf"):
                    continue

                if "board meeting" in headline.lower():
                    continue

                pdf_url = (
                    "https://www.bseindia.com/"
                    "xml-data/corpfiling/AttachLive/"
                    + attachment
                )

                print("\n" + "=" * 60)
                print("🚀 NEW RESULT")
                print("=" * 60)

                print("Company:", company)

                # DOWNLOAD PDF
                pdf_bytes = download_pdf(
                    pdf_url
                )

                if not pdf_bytes:

                    print("❌ PDF Download Failed")

                    continue

                # EXTRACT FINANCIAL PAGES
                financial_text = extract_financial_pages(
                    pdf_bytes
                )

                if len(financial_text) < 1000:

                    print(
                        "❌ No Financial Pages Found"
                    )

                    continue

                # GPT EXTRACTION
                data = gpt_extract(
                    financial_text
                )

                if not data:

                    print(
                        "❌ GPT Extraction Failed"
                    )

                    continue

                # VALIDATION
                if not validate(data):

                    print("\n❌ VALIDATION FAILED")

                    print(
                        json.dumps(
                            data,
                            indent=2
                        )
                    )

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

                # LOW SCORE FILTER
                if pead["score"] < MIN_PEAD_SCORE:

                    print(
                        "❌ Low PEAD Score"
                    )

                    continue

                # DASHBOARD
                dashboard = generate_dashboard(
                    data,
                    pead,
                    theme
                )

                # TELEGRAM CAPTION
                caption = (

                    f"🚀 {company}\n\n"

                    f"🎯 PEAD Score: "
                    f"{pead['score']}\n"

                    f"📈 Revenue Growth: "
                    f"{pead['rev_growth']:+.1f}%\n"

                    f"💰 PAT Growth: "
                    f"{pead['pat_growth']:+.1f}%\n"

                    f"⚡ Theme: "
                    f"{theme}\n"

                    f"🏭 Sector: "
                    f"{data.get('sector', '')}\n"

                    f"🏢 Industry: "
                    f"{data.get('industry', '')}"
                )

                # SEND PHOTO
                send_telegram_photo(
                    dashboard,
                    caption
                )

                # COMMENTARY
                commentary = (

                    f"🧠 Commentary\n\n"

                    f"{data.get('management_commentary', '')}\n\n"

                    f"📦 Order Book\n\n"

                    f"{data.get('order_book', '')}\n\n"

                    f"⚠️ Red Flags\n\n"

                    f"{data.get('red_flags', '')}"
                )

                send_telegram_message(
                    commentary
                )

                # DATABASE
                try:

                    conn = sqlite3.connect(DB_NAME)

                    cur = conn.cursor()

                    cur.execute("""

                    INSERT OR REPLACE INTO pead_results (

                        news_id,
                        company,
                        quarter,
                        revenue_growth,
                        pat_growth,
                        pead_score,
                        theme

                    )

                    VALUES (?, ?, ?, ?, ?, ?, ?)

                    """, (

                        news_id,
                        company,
                        data.get("quarter", ""),
                        pead["rev_growth"],
                        pead["pat_growth"],
                        pead["score"],
                        theme
                    ))

                    conn.commit()

                    conn.close()

                except Exception as db_error:

                    print(
                        "DB Error:",
                        db_error
                    )

                print("✅ ALERT SENT")

            print(

                f"\n[{time.strftime('%H:%M:%S')}] "

                f"Alive | Seen={len(seen)}"
            )

        except Exception as e:

            print(
                "MAIN LOOP ERROR:",
                e
            )

        time.sleep(CHECK_INTERVAL)

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    main()
