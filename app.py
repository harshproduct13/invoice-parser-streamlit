import streamlit as st
import pdfplumber
import sqlite3
import json
import io
import openai
from datetime import datetime
from PIL import Image
import pytesseract

# --- CONFIG ---
DB_PATH = "invoice_ledger.db"
st.set_page_config(page_title="Invoice Parser", layout="wide")

# --- Initialize DB ---
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_of_invoice TEXT,
        due_date TEXT,
        gst_no TEXT,
        category TEXT,
        amount_without_tax REAL,
        total_amount REAL,
        confidence REAL,
        raw_json TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    return conn

# --- DB Functions ---
def save_invoice(conn, data):
    c = conn.cursor()
    c.execute("""
        INSERT INTO ledger (date_of_invoice, due_date, gst_no, category, amount_without_tax, total_amount, confidence, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("date_of_invoice"),
        data.get("due_date"),
        data.get("gst_no"),
        data.get("category"),
        data.get("amount_without_tax"),
        data.get("total_amount"),
        data.get("confidence"),
        json.dumps(data.get("raw_json", {}))
    ))
    conn.commit()

def get_all_invoices(conn):
    c = conn.cursor()
    c.execute("SELECT * FROM ledger ORDER BY created_at DESC")
    cols = [desc[0] for desc in c.description]
    return [dict(zip(cols, row)) for row in c.fetchall()]

# --- PDF Extraction ---
def extract_text(pdf_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text.strip()

def ocr_fallback(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = ""
        for page in pdf.pages:
            im = page.to_image(resolution=300).original
            text += pytesseract.image_to_string(im)
    return text.strip()

# --- LLM Prompt ---
def build_prompt(invoice_text):
    example_invoice = """
EXAMPLE INVOICE:
Supplier: Fancy Packaging Co.
Invoice No: INV-2024-701
Invoice Date: 2025-07-05
Due Date: 2025-07-20
GSTIN: 07ABCDE1234F1Z5
Taxable Value: ₹7,500.00
CGST 9%: ₹675.00
SGST 9%: ₹675.00
Total Amount: ₹8,850.00
Category: Packaging
"""
    return f"""
You are an extraction assistant. Given a single-page Indian expense invoice, output EXACTLY one JSON object (no commentary) with these keys:

date_of_invoice (YYYY-MM-DD or null)
due_date (YYYY-MM-DD or null)
gst_no (GSTIN string or null)
category (e.g., Packaging, Shipping, Marketing, Office Supplies, Other)
amount_without_tax (numeric)
total_amount (numeric)
confidence (0.0-1.0, optional)

If a field is missing, use null.
Output ONLY JSON.

INVOICE TEXT:
{invoice_text}
END
"""

# --- Streamlit UI ---
st.title("📄 Invoice Parser (Streamlit + SQLite)")

st.sidebar.header("🔑 OpenAI API Key")
api_key = st.sidebar.text_input("Enter your OpenAI API key", type="password")
if not api_key and "openai_api_key" in st.secrets:
    api_key = st.secrets["openai_api_key"]

if not api_key:
    st.warning("Please provide your OpenAI API key in the sidebar or Streamlit secrets.")
    st.stop()

openai.api_key = api_key
conn = init_db()

uploaded_file = st.file_uploader("Upload a single-page invoice PDF", type=["pdf"])

if uploaded_file and st.button("Parse Invoice"):
    pdf_bytes = uploaded_file.read()
    st.info(f"Processing: {uploaded_file.name}")

    text = extract_text(pdf_bytes)
    if not text:
        st.write("Trying OCR...")
        text = ocr_fallback(pdf_bytes)

    st.text_area("Extracted Text (preview)", text[:2000], height=200)

    with st.spinner("Parsing invoice using OpenAI..."):
        prompt = build_prompt(text)
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Return only JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0
            )
            content = response.choices[0].message.content.strip()
        except Exception as e:
            st.error(f"OpenAI API error: {e}")
            st.stop()

    import re
    if content.startswith("```"):
        content = re.sub(r"```(json)?", "", content).strip().strip("`")

    try:
        parsed = json.loads(content)
    except:
        match = re.search(r"\{[\s\S]*\}", content)
        parsed = json.loads(match.group(0)) if match else {}
    
    st.json(parsed)
    save_invoice(conn, {**parsed, "raw_json": parsed})
    st.success("Saved to ledger ✅")

st.header("🧾 Ledger")
rows = get_all_invoices(conn)
if not rows:
    st.info("No invoices parsed yet.")
else:
    st.dataframe([
        {
            "Date": r["date_of_invoice"],
            "Due": r["due_date"],
            "GSTIN": r["gst_no"],
            "Category": r["category"],
            "Subtotal": r["amount_without_tax"],
            "Total": r["total_amount"],
            "Confidence": r["confidence"]
        }
        for r in rows
    ])
