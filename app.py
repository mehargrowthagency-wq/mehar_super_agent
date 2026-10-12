"""Mehar Super Agent v2 — hardened Streamlit prototype.

This is not a production payment service, PWA, or webhook server. See README.md.
"""
from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Mehar Super Agent v2", page_icon="🚀", layout="wide", initial_sidebar_state="collapsed")

NAVY, GOLD = "#0A1931", "#D4AF37"
DB_PATH = Path(os.getenv("MEHAR_DB_PATH", "leads.db")).expanduser()
RATE_LIMIT = 5
RATE_WINDOW_SECONDS = 60
LOGIN_LIMIT = 5
LOGIN_WINDOW_SECONDS = 300

st.markdown(
f"""<style>
.stApp {{background:{NAVY}; color:#fff}}
.stApp h1,.stApp h2,.stApp h3,.stApp label,.stApp p,.stApp li {{color:#fff}}
[data-testid="stSidebar"] {{background:#081427}}
.stButton>button {{background:{GOLD};color:{NAVY};font-weight:700;width:100%;min-height:2.75rem}}
@media(max-width:768px) {{
  .block-container {{padding:1rem 0.8rem}}
  [data-testid="stHorizontalBlock"] {{flex-wrap:wrap;gap:0.75rem}}
  [data-testid="stColumn"] {{min-width:100%!important;flex:1 1 100%!important}}
  input,textarea {{font-size:16px!important}}
}}
</style>""",
    unsafe_allow_html=True,
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    """Open a short-lived SQLite connection; do not share connections across sessions."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def db_session():
    """Yield a connection and always close it, including on exceptions."""
    conn = db_connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db_session() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
            phone TEXT NOT NULL CHECK(length(phone) BETWEEN 3 AND 20),
            niche TEXT NOT NULL CHECK(length(niche) BETWEEN 1 AND 100),
            created_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            action TEXT NOT NULL,
            ts REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_log(user, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
            slug TEXT NOT NULL UNIQUE CHECK(length(slug) BETWEEN 3 AND 60),
            niche TEXT NOT NULL,
            theme TEXT NOT NULL DEFAULT 'Minimal White',
            primary_color TEXT NOT NULL DEFAULT '#D4AF37',
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','exported')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 150),
            sku TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price_pkr REAL NOT NULL CHECK(price_pkr >= 0),
            stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0),
            image_url TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(store_id, sku)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS store_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
            customer_name TEXT NOT NULL,
            customer_phone TEXT NOT NULL,
            address TEXT NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL CHECK(quantity > 0),
            unit_price_pkr REAL NOT NULL CHECK(unit_price_pkr >= 0),
            total_pkr REAL NOT NULL CHECK(total_pkr >= 0),
            status TEXT NOT NULL DEFAULT 'Pending' CHECK(status IN ('Pending','Confirmed','Shipped','Delivered','Cancelled')),
            created_at TEXT NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_store ON products(store_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_store ON store_orders(store_id, created_at)")


def check_and_log_rate_limit(user: str, action: str, limit: int = RATE_LIMIT, window: int = RATE_WINDOW_SECONDS) -> bool:
    """Atomically enforce a per-user rolling-window limit across app sessions/processes using SQLite."""
    now = time.time()
    cutoff = now - window
    with db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM usage_log WHERE action = ? AND ts < ?", (action, cutoff - window))
            count = conn.execute(
                "SELECT COUNT(*) FROM usage_log WHERE user = ? AND action = ? AND ts > ?",
                (user, action, cutoff),
            ).fetchone()[0]
            if count >= limit:
                conn.execute("COMMIT")
                return False
            conn.execute("INSERT INTO usage_log(user, action, ts) VALUES (?, ?, ?)", (user, action, now))
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_secret(name: str) -> str:
    """Read from environment first, then Streamlit secrets; tolerate missing secrets file."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, "")).strip()
    except Exception:
        return ""


def groq_generate(description: str) -> str:
    api_key = get_secret("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY configured nahi hai. Environment variable ya Streamlit secrets mein set karein.")
    try:
        from groq import Groq
    except ImportError as exc:
        raise RuntimeError("Groq package installed nahi. requirements.txt install karein.") from exc

    client = Groq(api_key=api_key, timeout=30.0, max_retries=1)
    response = client.chat.completions.create(
        model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        messages=[
            {"role": "system", "content": (
                "Write useful, honest SEO copy. Do not invent product specifications, certifications, "
                "warranties, discounts, medical claims, or performance claims. If facts are missing, "
                "avoid stating them. Return a short title, meta description, and keywords."
            )},
            {"role": "user", "content": f"Create SEO copy from this product description:\n{description}"},
        ],
        temperature=0.4,
        max_tokens=700,
    )
    result = response.choices[0].message.content if response.choices else None
    if not result or not result.strip():
        raise RuntimeError("AI service ne empty response diya. Dobara try karein.")
    return result.strip()


init_db()

# Fail closed: no built-in admin username or password.
admin_user = get_secret("ADMIN_USER")
admin_pass = get_secret("ADMIN_PASS")
if not admin_user or not admin_pass:
    st.title("🔐 Mehar Super Agent v2")
    st.error("Admin login configured nahi hai. ADMIN_USER aur ADMIN_PASS environment variables ya Streamlit secrets mein set karein.")
    st.code('ADMIN_USER="your-admin-user"\nADMIN_PASS="use-a-long-unique-password"\nGROQ_API_KEY="your-groq-api-key"', language="toml")
    st.stop()

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "auth_user" not in st.session_state:
    st.session_state.auth_user = ""

if not st.session_state.authenticated:
    st.title("🔐 Login — Mehar Super Agent v2")
    with st.form("login_form"):
        username = st.text_input("Username", max_chars=100)
        password = st.text_input("Password", type="password", max_chars=256)
        login_clicked = st.form_submit_button("Login")
    if login_clicked:
        if not check_and_log_rate_limit("__login__", "login", LOGIN_LIMIT, LOGIN_WINDOW_SECONDS):
            st.error("Bohat zyada login attempts. 5 minute baad dobara try karein.")
        else:
            valid_user = hmac.compare_digest(username.encode("utf-8"), admin_user.encode("utf-8"))
            valid_pass = hmac.compare_digest(password.encode("utf-8"), admin_pass.encode("utf-8"))
            if valid_user and valid_pass:
                st.session_state.authenticated = True
                st.session_state.auth_user = username
                st.rerun()
            else:
                st.error("Credentials ghalat hain.")
    st.caption("Single-admin prototype authentication. Public production deployment requires stronger identity, session and access controls.")
    st.stop()

with st.sidebar:
    st.write(f"👤 {st.session_state.auth_user} | Admin session")
    if st.button("Logout"):
        st.session_state.authenticated = False
        st.session_state.auth_user = ""
        st.rerun()
    st.divider()
    st.caption("SQLite: local database | SEO limit: 5 attempts / rolling 60 seconds")

st.title("Mehar Super Agent v2")
st.caption("Hardened prototype — live payment verification, production webhooks, and installable PWA are not included.")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📦 Leads DB", "🔎 SEO (Rate Limited)", "💳 Easypaisa", "🔗 Webhooks / PWA", "🛒 Store Builder"])

with tab1:
    st.subheader("Leads — SQLite database")
    with st.form("lead_form", clear_on_submit=True):
        name = st.text_input("Name", max_chars=100).strip()
        phone = st.text_input("Phone", max_chars=20).strip()
        niche = st.text_input("Niche", max_chars=100).strip()
        submitted = st.form_submit_button("Save Lead to DB")
    if submitted:
        if not name or not phone or not niche:
            st.error("Name, phone aur niche teenon zaroori hain.")
        elif not re.fullmatch(r"\+?[0-9 \-]+", phone) or not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
            st.error("Phone number check karein (7-15 digits, sirf digits/+/space/-).")
        else:
            try:
                with db_session() as conn:
                    conn.execute(
                        "INSERT INTO leads(name, phone, niche, created_at) VALUES (?, ?, ?, ?)",
                        (name, phone, niche, utc_iso()),
                    )
                st.success("Lead local SQLite database mein save ho gayi.")
            except sqlite3.Error:
                st.error("Database mein save nahi ho saka. Server logs check karein.")
    try:
        with db_session() as conn:
            rows = conn.execute("SELECT id, name, phone, niche, created_at FROM leads ORDER BY id DESC LIMIT 1000").fetchall()
        st.dataframe(pd.DataFrame([dict(row) for row in rows]), use_container_width=True, hide_index=True)
        st.caption("Showing latest 1,000 leads. Database file local disk par hai; deployment par persistent storage/backup separately verify karein.")
    except sqlite3.Error:
        st.error("Leads read nahi ho sake. Server logs check karein.")

with tab2:
    st.subheader("SEO copy generator")
    st.caption("Limit: maximum 5 attempts per signed-in admin in a rolling 60-second window. Description Groq ko bheji jayegi jab aap generate press karenge.")
    description = st.text_area("Product Description", max_chars=2000, height=150, placeholder="Product ki asli details yahan likhein...")
    if st.button("Generate SEO", key="generate_seo"):
        if not description.strip():
            st.warning("Pehle product description enter karein.")
        elif not check_and_log_rate_limit(st.session_state.auth_user, "seo"):
            st.error("Rate limit reached: 60 seconds wait karein.")
        else:
            st.session_state.pop("last_seo_result", None)  # no stale result under a new error
            with st.spinner("SEO copy generate ho rahi hai..."):
                try:
                    st.session_state.last_seo_result = groq_generate(description.strip())
                except RuntimeError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("AI provider request fail hui. API key/model/network configuration check karein; sensitive provider details hide ki gayi hain.")
            if st.session_state.get("last_seo_result"):
                st.markdown(st.session_state.last_seo_result)
    elif st.session_state.get("last_seo_result"):
        st.markdown(st.session_state.last_seo_result)

with tab3:
    st.subheader("Easypaisa payment verification")
    st.warning("Not connected: yeh app Easypaisa transaction ko verify, charge, refund, ya approve nahi karti.")
    st.write("Real verification ke liye Easypaisa ke official merchant/API access, server-side credentials, signed callback or authenticated verification flow, duplicate-transaction protection, aur reconciliation required hain.")
    trx_id = st.text_input("Transaction reference (demo only)", max_chars=100)
    if st.button("Check reference (demo only)"):
        if not trx_id.strip():
            st.error("Reference enter karein.")
        else:
            st.info("Reference sirf input hua hai; payment status verify nahi hua. Isay paid/approved na samjhein.")

with tab4:
    st.subheader("Webhook and PWA status")
    st.info("Webhook endpoints is Streamlit app mein implemented nahi hain. Neeche sirf implementation checklist hai.")
    st.markdown("""
**WhatsApp webhook production checklist**
- Separate backend endpoint (e.g. FastAPI) with provider verification challenge.
- Validate request signature against the raw request body using the official provider secret.
- Enforce idempotency/deduplication on provider message ID; use database transactions.
- Authenticate admin actions, validate payload schemas, rate-limit abuse, and avoid logging secrets/PII.
- Return provider-required HTTP status codes and configure retries/monitoring.

**PWA status**
- `manifest.json` and `sw.js` generated inside a Streamlit Python process are not automatically served or linked by Streamlit.
- A proper installable PWA needs a supported frontend/static hosting setup, HTTPS, valid manifest/icons, and a carefully scoped service worker.
- The unsafe `fetch`-everything service worker pattern from the submitted code is intentionally removed; it is not a safe offline cache.
""")


with tab5:
    st.subheader("🛒 Mehar Store Builder")
    st.caption("Apna independent store builder — kisi third-party platform ki copy ya API dependency nahi. Data SQLite mein save hota hai.")
    create_tab, products_tab, theme_tab, orders_tab, publish_tab = st.tabs(["🏪 Store Create", "📦 Products", "🎨 Theme", "📊 Orders & Inventory", "🚀 Export / Publish"])

    def load_stores():
        with db_session() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM stores ORDER BY id DESC").fetchall()]

    stores = load_stores()
    with create_tab:
        st.markdown("#### Create your own store")
        with st.form("create_store_form", clear_on_submit=True):
            new_store_name = st.text_input("Store name", max_chars=100)
            new_store_slug = st.text_input("Store slug (URL-friendly, e.g. mehar-kitchen)", max_chars=60)
            new_store_niche = st.selectbox("Niche", ["Kitchen", "Beauty", "Mobile", "Fashion", "Home & Living", "Digital Services", "General"])
            create_store = st.form_submit_button("Create Store")
        if create_store:
            clean_name = new_store_name.strip()
            slug = re.sub(r"[^a-z0-9-]+", "-", new_store_slug.strip().lower()).strip("-")
            if not clean_name:
                st.error("Store name zaroori hai.")
            elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])", slug):
                st.error("Slug 3–60 characters ka ho: lowercase letters, numbers aur hyphens.")
            else:
                now = utc_iso()
                try:
                    with db_session() as conn:
                        conn.execute("INSERT INTO stores(name,slug,niche,created_at,updated_at) VALUES(?,?,?,?,?)", (clean_name, slug, new_store_niche, now, now))
                    st.success(f"Store '{clean_name}' database mein create ho gaya.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ye slug pehle se use ho raha hai. Doosra slug choose karein.")
        if stores:
            st.markdown("#### Your stores")
            st.dataframe(pd.DataFrame([{ "ID": x["id"], "Store": x["name"], "Slug": x["slug"], "Niche": x["niche"], "Status": x["status"], "Created": x["created_at"]} for x in stores]), use_container_width=True, hide_index=True)
        else:
            st.info("Pehle apna store create karein; phir products aur theme manage kar sakte hain.")

    stores = load_stores()
    if stores:
        store_labels = {f"{x['name']} · /{x['slug']} (ID {x['id']})": x['id'] for x in stores}
        chosen_label = st.selectbox("Manage store", list(store_labels.keys()), key="store_manager_select")
        store_id = store_labels[chosen_label]
        with db_session() as conn:
            store = dict(conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone())
            product_rows = [dict(r) for r in conn.execute("SELECT * FROM products WHERE store_id=? ORDER BY id DESC", (store_id,)).fetchall()]

        with products_tab:
            st.markdown("#### Add product")
            with st.form("add_store_product_form", clear_on_submit=True):
                c1, c2 = st.columns(2)
                product_name = c1.text_input("Product name", max_chars=150)
                product_sku = c2.text_input("SKU / unique code", max_chars=80)
                product_desc = st.text_area("Description", max_chars=2000)
                c3, c4 = st.columns(2)
                product_price = c3.number_input("Price (PKR)", min_value=0.0, value=0.0, step=50.0)
                product_stock = c4.number_input("Opening stock", min_value=0, value=0, step=1)
                product_image = st.text_input("Image URL (optional)", max_chars=500)
                add_product = st.form_submit_button("Save Product")
            if add_product:
                if not product_name.strip() or not product_sku.strip():
                    st.error("Product name aur SKU dono zaroori hain.")
                elif product_image.strip() and not re.match(r"^https?://", product_image.strip(), re.I):
                    st.error("Image URL http:// ya https:// se start honi chahiye.")
                else:
                    try:
                        with db_session() as conn:
                            conn.execute("INSERT INTO products(store_id,name,sku,description,price_pkr,stock,image_url,created_at) VALUES(?,?,?,?,?,?,?,?)", (store_id, product_name.strip(), product_sku.strip(), product_desc.strip(), float(product_price), int(product_stock), product_image.strip(), utc_iso()))
                        st.success("Product SQLite database mein save ho gaya.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Is store mein ye SKU pehle se mojood hai; unique SKU use karein.")
            st.markdown("#### Existing products — edit or delete")
            if product_rows:
                product_map = {f"{x['name']} · SKU {x['sku']} · ID {x['id']}": x for x in product_rows}
                edit_label = st.selectbox("Select product", list(product_map.keys()), key=f"edit_product_{store_id}")
                selected_product = product_map[edit_label]
                with st.form(f"edit_product_form_{store_id}_{selected_product['id']}"):
                    ename = st.text_input("Name", value=selected_product['name'], max_chars=150)
                    esku = st.text_input("SKU", value=selected_product['sku'], max_chars=80)
                    edesc = st.text_area("Description", value=selected_product['description'], max_chars=2000)
                    eprice = st.number_input("Price (PKR)", min_value=0.0, value=float(selected_product['price_pkr']), step=50.0)
                    estock = st.number_input("Stock", min_value=0, value=int(selected_product['stock']), step=1)
                    eimage = st.text_input("Image URL", value=selected_product['image_url'], max_chars=500)
                    eactive = st.checkbox("Active / visible in exported storefront", value=bool(selected_product['active']))
                    save_edit = st.form_submit_button("Update Product")
                if save_edit:
                    if not ename.strip() or not esku.strip():
                        st.error("Name aur SKU required hain.")
                    elif eimage.strip() and not re.match(r"^https?://", eimage.strip(), re.I):
                        st.error("Image URL http:// ya https:// se start honi chahiye.")
                    else:
                        try:
                            with db_session() as conn:
                                conn.execute("UPDATE products SET name=?,sku=?,description=?,price_pkr=?,stock=?,image_url=?,active=? WHERE id=? AND store_id=?", (ename.strip(), esku.strip(), edesc.strip(), float(eprice), int(estock), eimage.strip(), int(eactive), selected_product['id'], store_id))
                            st.success("Product updated.")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("SKU duplicate hai; unique SKU use karein.")
                if st.button("Delete selected product", key=f"delete_product_{store_id}_{selected_product['id']}"):
                    with db_session() as conn:
                        linked = conn.execute("SELECT COUNT(*) FROM store_orders WHERE product_id=?", (selected_product['id'],)).fetchone()[0]
                        if linked:
                            st.error("Is product ke orders mojood hain; audit history bachane ke liye delete nahi kiya. Product ko inactive kar dein.")
                        else:
                            conn.execute("DELETE FROM products WHERE id=? AND store_id=?", (selected_product['id'], store_id))
                            st.success("Product deleted.")
                            st.rerun()
                st.dataframe(pd.DataFrame([{ "ID":x['id'], "Name":x['name'], "SKU":x['sku'], "Price PKR":x['price_pkr'], "Stock":x['stock'], "Active":bool(x['active'])} for x in product_rows]), use_container_width=True, hide_index=True)
            else:
                st.info("Abhi is store mein koi product nahi. Upar se add karein.")

        with theme_tab:
            st.markdown("#### Store appearance")
            themes = ["Minimal White", "Dark Navy Gold", "Fresh Green"]
            theme_index = themes.index(store['theme']) if store['theme'] in themes else 0
            with st.form(f"theme_form_{store_id}"):
                selected_theme = st.selectbox("Theme", themes, index=theme_index)
                selected_color = st.color_picker("Primary color", value=store['primary_color'])
                save_theme = st.form_submit_button("Save Theme")
            if save_theme:
                with db_session() as conn:
                    conn.execute("UPDATE stores SET theme=?,primary_color=?,updated_at=? WHERE id=?", (selected_theme, selected_color, utc_iso(), store_id))
                st.success("Theme settings save ho gayi.")
                st.rerun()
            st.markdown("#### Preview")
            st.markdown(f"<div style='border:1px solid {store['primary_color']};border-radius:12px;padding:20px;background:{'#0A1931' if store['theme']=='Dark Navy Gold' else ('#effaf2' if store['theme']=='Fresh Green' else '#ffffff')};color:{'#ffffff' if store['theme']=='Dark Navy Gold' else '#17202a'}'><h2 style='color:{store['primary_color']}'>{store['name']}</h2><p>{store['niche']} Store · {store['theme']}</p><p>Products will appear here after export.</p></div>", unsafe_allow_html=True)

        with orders_tab:
            st.markdown("#### Create manual order")
            available_products = [x for x in product_rows if x['active'] and x['stock'] > 0]
            if available_products:
                with st.form(f"new_order_form_{store_id}", clear_on_submit=True):
                    customer_name = st.text_input("Customer name", max_chars=100)
                    customer_phone = st.text_input("Customer phone", max_chars=20)
                    customer_address = st.text_area("Delivery address", max_chars=500)
                    order_product_label = st.selectbox("Product", [f"{x['name']} — PKR {x['price_pkr']:.2f} (stock {x['stock']})" for x in available_products])
                    order_qty = st.number_input("Quantity", min_value=1, max_value=1000, value=1, step=1)
                    save_order = st.form_submit_button("Save Order & Reserve Stock")
                if save_order:
                    if not customer_name.strip() or not customer_phone.strip() or not customer_address.strip():
                        st.error("Customer name, phone aur address zaroori hain.")
                    elif not re.fullmatch(r"\+?[0-9 ()-]{7,25}", customer_phone.strip()):
                        st.error("Customer phone format check karein.")
                    else:
                        selected_order_product = available_products[[f"{x['name']} — PKR {x['price_pkr']:.2f} (stock {x['stock']})" for x in available_products].index(order_product_label)]
                        try:
                            with db_session() as conn:
                                conn.execute("BEGIN IMMEDIATE")
                                current = conn.execute("SELECT stock,price_pkr FROM products WHERE id=? AND store_id=?", (selected_order_product['id'], store_id)).fetchone()
                                if not current or current['stock'] < int(order_qty):
                                    conn.execute("ROLLBACK")
                                    st.error("Stock change ho gaya hai ya quantity available stock se zyada hai. Refresh karke try karein.")
                                else:
                                    total = float(current['price_pkr']) * int(order_qty)
                                    conn.execute("INSERT INTO store_orders(store_id,customer_name,customer_phone,address,product_id,quantity,unit_price_pkr,total_pkr,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (store_id, customer_name.strip(), customer_phone.strip(), customer_address.strip(), selected_order_product['id'], int(order_qty), float(current['price_pkr']), total, utc_iso()))
                                    conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (int(order_qty), selected_order_product['id']))
                                    conn.execute("COMMIT")
                                    st.success(f"Order saved. PKR {total:,.2f}; stock reserved. Payment is NOT automatically verified.")
                                    st.rerun()
                        except sqlite3.Error:
                            st.error("Order save nahi hua; database logs check karein.")
            else:
                st.info("Manual order banane ke liye pehle active product add karein aur stock 1 ya zyada rakhein.")
            with db_session() as conn:
                order_rows = [dict(r) for r in conn.execute("SELECT o.*,p.name AS product_name FROM store_orders o JOIN products p ON p.id=o.product_id WHERE o.store_id=? ORDER BY o.id DESC LIMIT 500", (store_id,)).fetchall()]
            if order_rows:
                st.dataframe(pd.DataFrame([{ "Order ID":x['id'], "Customer":x['customer_name'], "Phone":x['customer_phone'], "Product":x['product_name'], "Qty":x['quantity'], "Total PKR":x['total_pkr'], "Status":x['status'], "Created":x['created_at']} for x in order_rows]), use_container_width=True, hide_index=True)
                order_map = {f"#{x['id']} · {x['customer_name']} · {x['status']}": x for x in order_rows}
                status_label = st.selectbox("Update order status", list(order_map.keys()), key=f"order_status_select_{store_id}")
                new_status = st.selectbox("New status", ["Pending", "Confirmed", "Shipped", "Delivered", "Cancelled"], key=f"new_status_{store_id}")
                if st.button("Update Status", key=f"update_order_status_{store_id}"):
                    order = order_map[status_label]
                    if order['status'] == "Cancelled" and new_status != "Cancelled":
                        st.error("Cancelled order ko dobara activate nahi kar sakte; duplicate stock restoration se bachne ke liye naya order banayein.")
                    else:
                        with db_session() as conn:
                            conn.execute("BEGIN IMMEDIATE")
                            conn.execute("UPDATE store_orders SET status=? WHERE id=? AND store_id=?", (new_status, order['id'], store_id))
                            if new_status == "Cancelled" and order['status'] != "Cancelled":
                                conn.execute("UPDATE products SET stock=stock+? WHERE id=?", (order['quantity'], order['product_id']))
                            conn.execute("COMMIT")
                        st.success("Order status updated. Cancelled order ka reserved stock wapas add kiya gaya.")
                        st.rerun()
            else:
                st.info("Is store ke orders abhi nahi hain.")

        with publish_tab:
            st.markdown("#### Export your storefront")
            st.warning("Is prototype mein public hosting/domain publish API connected nahi. Export se standalone HTML file banegi; live URL automatically create nahi hota.")
            if st.button("Prepare Storefront HTML", key=f"prepare_export_{store_id}"):
                import html
                with db_session() as conn:
                    export_store = dict(conn.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone())
                    export_products = [dict(r) for r in conn.execute("SELECT * FROM products WHERE store_id=? AND active=1 ORDER BY id DESC", (store_id,)).fetchall()]
                bg = "#0A1931" if export_store['theme'] == "Dark Navy Gold" else ("#effaf2" if export_store['theme'] == "Fresh Green" else "#ffffff")
                fg = "#ffffff" if export_store['theme'] == "Dark Navy Gold" else "#17202a"
                cards = []
                for pr in export_products:
                    img = f'<img src="{html.escape(pr["image_url"], quote=True)}" alt="{html.escape(pr["name"], quote=True)}">' if pr['image_url'] else ''
                    cards.append(f'<article class="card">{img}<h2>{html.escape(pr["name"])}</h2><p>{html.escape(pr["description"])}</p><strong>PKR {pr["price_pkr"]:,.2f}</strong><p>Stock: {pr["stock"]}</p></article>')
                product_html = "\n".join(cards) or '<p>No products listed yet.</p>'
                page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(export_store['name'])}</title><style>body{{font-family:Arial,sans-serif;margin:0;background:{bg};color:{fg}}}header{{padding:2rem;background:{export_store['primary_color']};color:#fff}}main{{padding:1.5rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}}.card{{border:1px solid #ddd;border-radius:12px;padding:1rem;background:#fff;color:#17202a}}img{{width:100%;max-height:220px;object-fit:contain}}strong{{color:{export_store['primary_color']}}}</style></head><body><header><h1>{html.escape(export_store['name'])}</h1><p>{html.escape(export_store['niche'])} · {html.escape(export_store['theme'])}</p></header><main>{product_html}</main><footer style="padding:1rem">Catalog preview generated by Mehar Super Agent. Orders and payments are not processed on this static page.</footer></body></html>"""
                with db_session() as conn:
                    conn.execute("UPDATE stores SET status='exported',updated_at=? WHERE id=?", (utc_iso(), store_id))
                st.session_state[f"store_export_{store_id}"] = page
                st.success("Storefront HTML ready. Download it, then upload it to a hosting provider to make it public.")
            page = st.session_state.get(f"store_export_{store_id}")
            if page:
                st.download_button("Download storefront.html", data=page, file_name=f"{store['slug']}.html", mime="text/html", key=f"download_export_{store_id}")
                with st.expander("Preview generated catalog HTML"):
                    st.components.v1.html(page, height=520, scrolling=True)
            st.caption("Real public publishing needs hosting credentials or a supported deployment API. Never put hosting/API secrets in client-side HTML.")
    else:
        for child_tab in (products_tab, theme_tab, orders_tab, publish_tab):
            with child_tab:
                st.info("Pehle Store Create tab mein apna store create karein.")
