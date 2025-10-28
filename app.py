import streamlit as st
import sqlite3
import json
import pandas as pd
from openai import OpenAI

# --- CONFIG ---
DB_PATH = "invoice_ledger.db"
st.set_page_config(page_title="Invoice Parser (GPT-only)", layout="wide")

# --- Initialize SQLite Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
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


# --- Prompt Template ---
def build_prompt():
    return """
You are an expert invoice parser for Indian business invoices.

Each invoice will have **two GSTINs**:
1. The buyer GSTIN — SKINNCELL (GSTIN: 36ABCCS0157Q1ZY)
2. The seller GSTIN — belongs to the vendor issuing the invoice.

➡️ Always extract the SELLER's details (ignore SKINNCELL’s GSTIN).

Return your output as a single JSON object with the following fields:

{
  "date_of_invoice": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "business_name": "seller's business name",
  "gst_no": "SELLER GSTIN (not SKINNCELL's)",
  "category": "Packaging, Shipping, Marketing, Office Supplies, Other",
  "amount_without_tax": number,
  "total_amount": number,
  "confidence": number (0.0 - 1.0)
}

Rules:
- Only include the seller's GSTIN, not the buyer (ignore 36ABCCS0157Q1ZY).
- "business_name" is the seller’s name as written in the invoice header or near GSTIN.
- Output ONLY JSON, no markdown or explanations.
- If a field is missing, return null.
- All numeric values must be numbers.
"""


# --- Streamlit UI ---
st.title("📄 Invoice Parser (GPT-only, SQLite Ledger)")

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

    with st.spinner("📤 Uploading PDF to OpenAI..."):
        try:
            uploaded = client.files.create(
                file=(uploaded_file.name, pdf_bytes, "application/pdf"),
                purpose="assistants"
            )
            file_id = uploaded.id
        except Exception as e:
            st.error(f"File upload failed: {e}")
            st.stop()

    with st.spinner("🤖 Parsing invoice with GPT..."):
        try:
            prompt = build_prompt()
            response = client.responses.create(
                model="gpt-4.1-mini",
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_file", "file_id": file_id}
                        ],
                    }
                ],
                temperature=0
            )

            content = response.output_text.strip()

        except Exception as e:
            st.error(f"OpenAI API error: {e}")
            st.stop()

    import re
    if content.startswith("```"):
        content = re.sub(r"```(json)?", "", content).strip().strip("`")

    try:
        parsed = json.loads(content)
    except:
        st.error("❌ Failed to parse JSON from GPT response.")
        st.text(content)
        st.stop()

    # Remove SKINNCELL GSTIN if GPT mistakenly outputs it
    if parsed.get("gst_no") == "36ABCCS0157Q1ZY":
        parsed["gst_no"] = None

    st.json(parsed)
    save_invoice(conn, {**parsed, "raw_json": parsed})
    st.success("✅ Invoice saved to ledger.")


# --- Ledger Table ---
st.header("🧾 Ledger")
rows = get_all_invoices(conn)

if not rows:
    st.info("No invoices parsed yet.")
else:
    df = pd.DataFrame([
        {
            "ID": r["id"],
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

    st.markdown("### Parsed Invoices")
    header_cols = st.columns(len(df.columns) + 1)
    for i, col_name in enumerate(df.columns):
        header_cols[i].markdown(f"**{col_name}**")

    # Data rows with delete buttons
    for idx, row in df.iterrows():
        cols = st.columns(len(df.columns) + 1)
        for i, c in enumerate(df.columns):
            cols[i].write(row[c] if row[c] not in [None, ""] else "-")
        if cols[len(df.columns)].button("🗑️ Delete", key=f"del-{row['ID']}"):
            delete_invoice(conn, row["ID"])
            try:
                st.rerun()
            except AttributeError:
                st.experimental_rerun()
