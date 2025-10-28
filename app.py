import streamlit as st
import pdfplumber
import sqlite3
import json
import io
from datetime import datetime
from PIL import Image
import pytesseract
from openai import OpenAI

# --- CONFIG ---
DB_PATH = "invoice_ledger.db"
st.set_page_config(page_title="Invoice Parser", layout="wide")

# --- Initialize DB ---
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    # ✅ Added business_name column
    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_of_invoice TEXT,
        due_date TEXT,
        gst_no TEXT,
        business_name TEXT,
        category TEXT,
        amount_without_tax REAL,
        total_amount REAL,
        confidence REAL,
        raw_json TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()

    # Ensure backward compatibility (add business_name column if missing)
    existing_cols = [r[1] for r in c.execute("PRAGMA table_info(ledger)")]
    if "business_name" not in existing_cols:
        c.execute("ALTER TABLE ledger ADD COLUMN business_name TEXT;")
        conn.commit()

    return conn


def save_invoice(conn, data):
    c = conn.cursor()
    c.execute("""
        INSERT INTO ledger (date_of_invoice, due_date, gst_no, business_name, category, amount_without_tax, total_amount, confidence, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("date_of_invoice"),
        data.get("due_date"),
        data.get("gst_no"),
        data.get("business_name"),
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


def delete_invoice(conn, row_id):
    c = conn.cursor()
    c.execute("DELETE FROM ledger WHERE id=?", (row_id,))
    conn.commit()


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
GSTIN (Seller): 07ABCDE1234F1Z5
Buyer GSTIN (SKINNCELL): 36ABCCS0157Q1ZY
Taxable Value: ₹7,500.00
CGST 9%: ₹675.00
SGST 9%: ₹675.00
Total Amount: ₹8,850.00
Category: Packaging
"""

    return f"""
You are an expert invoice parser for Indian GST invoices.

Each invoice will have **two GSTINs**:
1. The buyer GSTIN — SKINNCELL (GSTIN: 36ABCCS0157Q1ZY)
2. The seller GSTIN — belongs to the vendor issuing the invoice

➡️ Always extract the SELLER's details (ignore SKINNCELL’s GSTIN).

Your task: return a strict JSON object with these keys:

date_of_invoice (YYYY-MM-DD or null)
due_date (YYYY-MM-DD or null)
business_name (seller's business name)
gst_no (SELLER's GSTIN, not SKINNCELL's)
category (e.g., Packaging, Shipping, Marketing, Office Supplies, Other)
amount_without_tax (numeric)
total_amount (numeric)
confidence (0.0-1.0, optional)

Rules:
- Only include the SELLER’s GSTIN, not the buyer (ignore 36ABCCS0157Q1ZY).
- "business_name" is the seller’s name as written in the invoice header or near GSTIN.
- Output ONLY JSON, no markdown or explanations.
- If a field is missing, return null.
- All numeric values must be numeric.

Example invoice:
{example_invoice}

Now extract details from this invoice:

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

client = OpenAI(api_key=api_key)
conn = init_db()

uploaded_file = st.file_uploader("Upload a single-page invoice PDF", type=["pdf"])

if uploaded_file and st.button("Parse Invoice"):
    pdf_bytes = uploaded_file.read()
    st.info(f"Processing: {uploaded_file.name}")

    # Extract text
    text = extract_text(pdf_bytes)
    if not text:
        st.write("Trying OCR...")
        text = ocr_fallback(pdf_bytes)

    st.text_area("Extracted Text (preview)", text[:2000], height=200)

    # Parse via OpenAI
    with st.spinner("Parsing invoice using OpenAI..."):
        prompt = build_prompt(text)
        try:
            response = client.chat.completions.create(
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

    # Automatic post-validation: remove SKINNCELL GSTIN if wrongly picked
    if parsed.get("gst_no") == "36ABCCS0157Q1ZY":
        parsed["gst_no"] = None

    st.json(parsed)
    save_invoice(conn, {**parsed, "raw_json": parsed})
    st.success("Saved to ledger ✅")


# --- Ledger UI ---
st.header("🧾 Ledger")
rows = get_all_invoices(conn)

if not rows:
    st.info("No invoices parsed yet.")
else:
    for r in rows:
        col1, col2, col3 = st.columns([7, 2, 1])
        with col1:
            st.markdown(f"""
            **{r['business_name'] or 'Unknown Vendor'}**  
            📅 *{r['date_of_invoice'] or '-'}* → 💰 ₹{r['total_amount'] or '-'}  
            🧾 GSTIN: `{r['gst_no'] or '-'}`  
            🏷️ Category: {r['category'] or '-'}
            """)
        with col2:
            st.write("")
        with col3:
            if st.button("🗑️ Delete", key=f"del-{r['id']}"):
                delete_invoice(conn, r["id"])
                st.experimental_rerun()

    st.divider()
    st.dataframe([
        {
            "Date": r["date_of_invoice"],
            "Due": r["due_date"],
            "Business Name": r["business_name"],
            "GSTIN": r["gst_no"],
            "Category": r["category"],
            "Subtotal": r["amount_without_tax"],
            "Total": r["total_amount"],
            "Confidence": r["confidence"]
        }
        for r in rows
    ])
