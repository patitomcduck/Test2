import os
import re
import json
import sqlite3
import time
import uuid
import smtplib
import csv
import io
import shutil
import zipfile
import tempfile
import platform
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash
import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for, send_file, session, g, abort, Response
from pricing_engine import run_price_refresh

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "pos.db"
JUSTTCG_API_URL = "https://api.justtcg.com/v1/cards"
TCGDEX_API_BASE = "https://api.tcgdex.net/v2/en"
TCGDEX_CACHE_TTL = 600
_TCGDEX_DETAIL_CACHE = {}
_TCGDEX_CACHE_LOCK = Lock()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


DEFAULT_STORE_SETTINGS = {
    "store_name": "Collector POS",
    "store_subtitle": "PUNTO DE VENTA · TCG · COLECCIONABLES",
    "social_handle": "",
    "primary_color": "#7DD3FC",
    "secondary_color": "#A78BFA",
    "accent_color": "#34D399",
    "trade_credit_rate": 0.70,
    "trade_cash_rate": 0.60,
    "receipt_footer": "¡Gracias por tu compra!",
    "customer_header_label": "PUNTO DE VENTA",
    "customer_welcome_label": "BIENVENIDO",
    "customer_welcome_title": "Gracias por visitarnos",
    "customer_welcome_text": "Consulta nuestro catálogo y promociones",
    "customer_badge_1": "Compra segura",
    "customer_badge_2": "Inventario actualizado",
    "customer_badge_3": "Atención en tienda",
    "customer_help_title": "¿Necesitas ayuda?",
    "customer_help_text": "Pregunta al personal de la tienda.",
    "customer_thanks_title": "¡GRACIAS POR TU COMPRA!",
    "customer_receipt_note": "Recibo disponible por correo o WhatsApp",
    "customer_footer_1": "VENTA SEGURA",
    "customer_footer_2": "SERVICIO EN TIENDA",
    "customer_footer_3": "GRACIAS POR TU VISITA",
    "usd_mxn_rate": 17.00,
    "auto_fx_enabled": 1,
    "auto_price_tcg_enabled": 1,
    "auto_price_multiplier": 1.00,
    "auto_price_round_to": 1.00,
    "store_timezone": "UTC",
    "license_key": "",
    "license_status": "sin_asignar",
}

def get_store_settings(conn=None):
    own = conn is None
    conn = conn or get_db()
    row = conn.execute("SELECT * FROM store_settings WHERE id=1").fetchone()
    if own:
        conn.close()
    if not row:
        return dict(DEFAULT_STORE_SETTINGS)
    data = dict(DEFAULT_STORE_SETTINGS)
    data.update(dict(row))
    return data

def valid_hex(value, fallback):
    value = (value or '').strip()
    return value if re.fullmatch(r"#[0-9A-Fa-f]{6}", value) else fallback

def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Other',
            subcategory TEXT,
            game TEXT,
            set_name TEXT,
            card_number TEXT,
            rarity TEXT,
            condition TEXT,
            printing TEXT,
            language TEXT,
            justtcg_card_uuid TEXT,
            justtcg_variant_uuid TEXT,
            market_price_usd REAL,
            cost_mxn REAL NOT NULL DEFAULT 0,
            price_mxn REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 0 CHECK(qty >= 0),
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_number TEXT NOT NULL UNIQUE,
            subtotal_mxn REAL NOT NULL,
            discount_mxn REAL NOT NULL DEFAULT 0,
            total_mxn REAL NOT NULL,
            payment_method TEXT NOT NULL,
            amount_received_mxn REAL,
            change_mxn REAL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty > 0),
            unit_price_mxn REAL NOT NULL,
            unit_cost_mxn REAL NOT NULL DEFAULT 0,
            line_total_mxn REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            opening_cash_mxn REAL NOT NULL DEFAULT 0,
            closing_cash_mxn REAL,
            expected_cash_mxn REAL,
            difference_mxn REAL,
            status TEXT NOT NULL DEFAULT 'open',
            event_mode INTEGER NOT NULL DEFAULT 0,
            event_name TEXT,
            notes_open TEXT,
            notes_close TEXT
        );

        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('pay_in','pay_out')),
            amount_mxn REAL NOT NULL CHECK(amount_mxn > 0),
            reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_number TEXT NOT NULL UNIQUE,
            shift_id INTEGER REFERENCES shifts(id),
            payout_type TEXT NOT NULL CHECK(payout_type IN ('credit','cash')),
            market_total_mxn REAL NOT NULL,
            offer_total_mxn REAL NOT NULL,
            rate REAL NOT NULL,
            customer_name TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
            product_id INTEGER REFERENCES products(id),
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty > 0),
            market_value_unit_mxn REAL NOT NULL,
            sell_price_unit_mxn REAL NOT NULL,
            offer_unit_mxn REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS display_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            payload TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS store_settings (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            store_name TEXT NOT NULL,
            store_subtitle TEXT NOT NULL,
            social_handle TEXT,
            primary_color TEXT NOT NULL,
            secondary_color TEXT NOT NULL,
            accent_color TEXT NOT NULL,
            trade_credit_rate REAL NOT NULL DEFAULT 0.70,
            trade_cash_rate REAL NOT NULL DEFAULT 0.60,
            receipt_footer TEXT,
            customer_header_label TEXT,
            customer_welcome_label TEXT,
            customer_welcome_title TEXT,
            customer_welcome_text TEXT,
            customer_badge_1 TEXT,
            customer_badge_2 TEXT,
            customer_badge_3 TEXT,
            customer_help_title TEXT,
            customer_help_text TEXT,
            customer_thanks_title TEXT,
            customer_receipt_note TEXT,
            customer_footer_1 TEXT,
            customer_footer_2 TEXT,
            customer_footer_3 TEXT,
            customer_promo_filename TEXT,
            license_key TEXT,
            license_status TEXT NOT NULL DEFAULT 'sin_asignar',
            logo_filename TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','manager','cashier')),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS inventory_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            movement_type TEXT NOT NULL,
            qty_delta INTEGER NOT NULL,
            qty_before INTEGER NOT NULL,
            qty_after INTEGER NOT NULL,
            unit_cost_mxn REAL,
            supplier TEXT,
            reason TEXT,
            source_type TEXT,
            source_id INTEGER,
            employee_id INTEGER REFERENCES users(id),
            movement_date TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_number TEXT NOT NULL UNIQUE,
            sale_id INTEGER NOT NULL REFERENCES sales(id),
            shift_id INTEGER REFERENCES shifts(id),
            refund_method TEXT NOT NULL CHECK(refund_method IN ('cash','card','other')),
            total_mxn REAL NOT NULL,
            reason TEXT,
            employee_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS refund_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_id INTEGER NOT NULL REFERENCES refunds(id) ON DELETE CASCADE,
            sale_item_id INTEGER NOT NULL REFERENCES sale_items(id),
            product_id INTEGER REFERENCES products(id),
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL CHECK(qty > 0),
            unit_price_mxn REAL NOT NULL,
            unit_cost_mxn REAL NOT NULL DEFAULT 0,
            refund_total_mxn REAL NOT NULL,
            restocked INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS price_refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            products_checked INTEGER NOT NULL DEFAULT 0,
            products_updated INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            fx_rate REAL,
            details TEXT
        );

        CREATE TABLE IF NOT EXISTS product_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            market_price_usd REAL,
            price_mxn REAL,
            usd_mxn_rate REAL,
            source TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            notes TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS store_credit_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            amount_mxn REAL NOT NULL,
            kind TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            reason TEXT,
            employee_id INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backup_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            details TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
        CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
        CREATE INDEX IF NOT EXISTS idx_sales_created_at ON sales(created_at);
        CREATE INDEX IF NOT EXISTS idx_trades_shift_id ON trades(shift_id);
        """
    )
    product_cols = table_columns(conn, "products")
    auto_price_column_added = "auto_price_enabled" not in product_cols
    for column, definition in {
        "provider":"TEXT", "external_card_id":"TEXT", "external_variant_id":"TEXT", "image_url":"TEXT",
        "market_low_usd":"REAL", "market_mid_usd":"REAL", "market_high_usd":"REAL", "market_updated_at":"TEXT",
        "auto_price_enabled":"INTEGER NOT NULL DEFAULT 0", "price_last_checked_at":"TEXT", "price_last_error":"TEXT",
    }.items():
        if column not in product_cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")
    sales_cols = table_columns(conn, "sales")
    for column, definition in {
        "shift_id":"INTEGER REFERENCES shifts(id)",
        "customer_email":"TEXT",
        "customer_phone":"TEXT",
        "status":"TEXT NOT NULL DEFAULT 'completed'",
        "refunded_mxn":"REAL NOT NULL DEFAULT 0",
        "employee_id":"INTEGER REFERENCES users(id)",
    }.items():
        if column not in sales_cols:
            conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {definition}")
    shift_cols = table_columns(conn, "shifts")
    for column, definition in {
        "opened_by_employee_id":"INTEGER REFERENCES users(id)",
        "closed_by_employee_id":"INTEGER REFERENCES users(id)",
    }.items():
        if column not in shift_cols:
            conn.execute(f"ALTER TABLE shifts ADD COLUMN {column} {definition}")
    movement_cols = table_columns(conn, "cash_movements")
    if "employee_id" not in movement_cols:
        conn.execute("ALTER TABLE cash_movements ADD COLUMN employee_id INTEGER REFERENCES users(id)")
    trade_cols = table_columns(conn, "trades")
    if "employee_id" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN employee_id INTEGER REFERENCES users(id)")
    sales_cols = table_columns(conn, "sales")
    for column, definition in {
        "customer_id":"INTEGER REFERENCES customers(id)",
        "store_credit_used_mxn":"REAL NOT NULL DEFAULT 0",
    }.items():
        if column not in sales_cols:
            conn.execute(f"ALTER TABLE sales ADD COLUMN {column} {definition}")
    trade_cols = table_columns(conn, "trades")
    if "customer_id" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN customer_id INTEGER REFERENCES customers(id)")
    refund_cols = table_columns(conn, "refunds")
    if "store_credit_mxn" not in refund_cols:
        conn.execute("ALTER TABLE refunds ADD COLUMN store_credit_mxn REAL NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_shift_id ON sales(shift_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_movements_product ON inventory_movements(product_id,id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refunds_sale ON refunds(sale_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refunds_shift ON refunds(shift_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_history_product ON product_price_history(product_id,id DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_credit_customer ON store_credit_transactions(customer_id,id DESC)")
    conn.execute("""UPDATE products SET provider='JustTCG', external_card_id=COALESCE(external_card_id, justtcg_card_uuid), external_variant_id=COALESCE(external_variant_id, justtcg_variant_uuid) WHERE provider IS NULL AND justtcg_card_uuid IS NOT NULL""")
    if auto_price_column_added:
        conn.execute("UPDATE products SET auto_price_enabled=1 WHERE category='TCG' AND external_card_id IS NOT NULL")
    conn.execute("INSERT OR IGNORE INTO display_state(id,payload,updated_at) VALUES (1,?,?)", (json.dumps({"mode":"idle"}), now_iso()))
    settings_cols = table_columns(conn, "store_settings")
    customer_setting_defs = {
        "customer_header_label": "TEXT",
        "customer_welcome_label": "TEXT",
        "customer_welcome_title": "TEXT",
        "customer_welcome_text": "TEXT",
        "customer_badge_1": "TEXT",
        "customer_badge_2": "TEXT",
        "customer_badge_3": "TEXT",
        "customer_help_title": "TEXT",
        "customer_help_text": "TEXT",
        "customer_thanks_title": "TEXT",
        "customer_receipt_note": "TEXT",
        "customer_footer_1": "TEXT",
        "customer_footer_2": "TEXT",
        "customer_footer_3": "TEXT",
        "customer_promo_filename": "TEXT",
    }
    for column, definition in customer_setting_defs.items():
        if column not in settings_cols:
            conn.execute(f"ALTER TABLE store_settings ADD COLUMN {column} {definition}")
    pricing_setting_defs = {
        "usd_mxn_rate": "REAL NOT NULL DEFAULT 17.0",
        "auto_fx_enabled": "INTEGER NOT NULL DEFAULT 1",
        "auto_price_tcg_enabled": "INTEGER NOT NULL DEFAULT 1",
        "auto_price_multiplier": "REAL NOT NULL DEFAULT 1.0",
        "auto_price_round_to": "REAL NOT NULL DEFAULT 1.0",
        "store_timezone": "TEXT NOT NULL DEFAULT 'UTC'",
        "fx_updated_at": "TEXT",
        "last_price_refresh_at": "TEXT",
        "last_price_refresh_status": "TEXT",
    }
    settings_cols = table_columns(conn, "store_settings")
    for column, definition in pricing_setting_defs.items():
        if column not in settings_cols:
            conn.execute(f"ALTER TABLE store_settings ADD COLUMN {column} {definition}")
    conn.execute("""INSERT OR IGNORE INTO store_settings(id,store_name,store_subtitle,social_handle,primary_color,secondary_color,accent_color,trade_credit_rate,trade_cash_rate,receipt_footer,customer_header_label,customer_welcome_label,customer_welcome_title,customer_welcome_text,customer_badge_1,customer_badge_2,customer_badge_3,customer_help_title,customer_help_text,customer_thanks_title,customer_receipt_note,customer_footer_1,customer_footer_2,customer_footer_3,license_key,license_status,updated_at) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (DEFAULT_STORE_SETTINGS["store_name"], DEFAULT_STORE_SETTINGS["store_subtitle"], DEFAULT_STORE_SETTINGS["social_handle"], DEFAULT_STORE_SETTINGS["primary_color"], DEFAULT_STORE_SETTINGS["secondary_color"], DEFAULT_STORE_SETTINGS["accent_color"], DEFAULT_STORE_SETTINGS["trade_credit_rate"], DEFAULT_STORE_SETTINGS["trade_cash_rate"], DEFAULT_STORE_SETTINGS["receipt_footer"], DEFAULT_STORE_SETTINGS["customer_header_label"], DEFAULT_STORE_SETTINGS["customer_welcome_label"], DEFAULT_STORE_SETTINGS["customer_welcome_title"], DEFAULT_STORE_SETTINGS["customer_welcome_text"], DEFAULT_STORE_SETTINGS["customer_badge_1"], DEFAULT_STORE_SETTINGS["customer_badge_2"], DEFAULT_STORE_SETTINGS["customer_badge_3"], DEFAULT_STORE_SETTINGS["customer_help_title"], DEFAULT_STORE_SETTINGS["customer_help_text"], DEFAULT_STORE_SETTINGS["customer_thanks_title"], DEFAULT_STORE_SETTINGS["customer_receipt_note"], DEFAULT_STORE_SETTINGS["customer_footer_1"], DEFAULT_STORE_SETTINGS["customer_footer_2"], DEFAULT_STORE_SETTINGS["customer_footer_3"], DEFAULT_STORE_SETTINGS["license_key"], DEFAULT_STORE_SETTINGS["license_status"], now_iso()))
    for key in ("customer_header_label","customer_welcome_label","customer_welcome_title","customer_welcome_text","customer_badge_1","customer_badge_2","customer_badge_3","customer_help_title","customer_help_text","customer_thanks_title","customer_receipt_note","customer_footer_1","customer_footer_2","customer_footer_3"):
        conn.execute(f"UPDATE store_settings SET {key}=? WHERE id=1 AND ({key} IS NULL OR TRIM({key})='')", (DEFAULT_STORE_SETTINGS[key],))
    migrated_logo = DATA_DIR / "brand" / "store-logo.png"
    if migrated_logo.exists():
        conn.execute("UPDATE store_settings SET logo_filename=COALESCE(logo_filename,'store-logo.png') WHERE id=1")
    # Existing inventory gets one opening movement so its history starts from the
    # quantity that was present when V2.1 was installed.
    existing_products = conn.execute("SELECT id,qty,cost_mxn,created_at FROM products WHERE active=1").fetchall()
    for product in existing_products:
        exists = conn.execute("SELECT 1 FROM inventory_movements WHERE product_id=? LIMIT 1", (product["id"],)).fetchone()
        if not exists and int(product["qty"] or 0) != 0:
            conn.execute("""INSERT INTO inventory_movements(product_id,movement_type,qty_delta,qty_before,qty_after,unit_cost_mxn,reason,source_type,movement_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (product["id"], "opening", int(product["qty"] or 0), 0, int(product["qty"] or 0), float(product["cost_mxn"] or 0), "Stock existente al instalar V2.1", "migration", (product["created_at"] or now_iso())[:10], product["created_at"] or now_iso()))
    conn.commit(); conn.close()


def money(value):
    try: return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError): return "0.00"

app.jinja_env.filters["money"] = money

def category_es(value):
    return {"Figure":"Figuras","Other":"Otro","Comics":"Cómics","Video Games":"Videojuegos"}.get(value, value)

app.jinja_env.filters["category_es"] = category_es

def make_sku(prefix="ITM"):
    return f"{prefix.upper()}-{uuid.uuid4().hex[:8].upper()}"

BACKUP_DIR = DATA_DIR / "backups"

def customer_credit_balance(conn, customer_id):
    if not customer_id:
        return 0.0
    row = conn.execute("SELECT COALESCE(SUM(amount_mxn),0) FROM store_credit_transactions WHERE customer_id=?", (customer_id,)).fetchone()
    return round(float(row[0] or 0), 2)

def add_store_credit(conn, customer_id, amount_mxn, kind, *, source_type=None, source_id=None, reason=None, employee_id=None):
    amount = round(float(amount_mxn or 0), 2)
    if not customer_id or abs(amount) < 0.005:
        return
    conn.execute("""INSERT INTO store_credit_transactions(customer_id,amount_mxn,kind,source_type,source_id,reason,employee_id,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                 (customer_id, amount, kind, source_type, source_id, reason, employee_id, now_iso()))

def create_backup_archive(trigger="manual"):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"collector-pos-backup-{stamp}.zip"
    tmp_db = DATA_DIR / f".backup-{stamp}.db"
    conn = get_db()
    try:
        dst = sqlite3.connect(tmp_db)
        conn.backup(dst)
        dst.close()
    finally:
        conn.close()
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "pos.db")
            brand = DATA_DIR / "brand"
            if brand.exists():
                for f in brand.rglob("*"):
                    if f.is_file():
                        zf.write(f, f"brand/{f.relative_to(brand)}")
            zf.writestr("backup-info.json", json.dumps({"created_at":now_iso(),"version":"2.3.0-pilot","trigger":trigger}, ensure_ascii=False, indent=2))
    finally:
        tmp_db.unlink(missing_ok=True)
    conn = get_db()
    try:
        conn.execute("INSERT INTO backup_runs(filename,trigger,status,size_bytes,created_at,details) VALUES (?,?,?,?,?,?)", (target.name,trigger,"ok",target.stat().st_size,now_iso(),None))
        conn.commit()
    finally:
        conn.close()
    return target

def restore_backup_archive(file_storage):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        temp_path = Path(tmp) / "restore.zip"
        file_storage.save(temp_path)
        with zipfile.ZipFile(temp_path) as zf:
            names=set(zf.namelist())
            if "pos.db" not in names:
                raise ValueError("El respaldo no contiene pos.db.")
            zf.extract("pos.db", tmp)
            source = sqlite3.connect(Path(tmp)/"pos.db")
            check = source.execute("PRAGMA integrity_check").fetchone()[0]
            if str(check).lower() != "ok":
                source.close(); raise ValueError("La base de datos del respaldo no pasó la verificación de integridad.")
            current = get_db()
            try:
                source.backup(current)
                current.commit()
            finally:
                current.close(); source.close()
            brand_dir = DATA_DIR / "brand"
            for name in names:
                if name.startswith("brand/") and not name.endswith("/"):
                    rel=Path(name).relative_to("brand")
                    if ".." in rel.parts: continue
                    dest=brand_dir/rel; dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(dest,"wb") as out:
                        shutil.copyfileobj(src,out)

def csv_response(filename, headers, rows):
    stream=io.StringIO(); writer=csv.writer(stream); writer.writerow(headers); writer.writerows(rows)
    data='\ufeff'+stream.getvalue()
    return Response(data, mimetype='text/csv; charset=utf-8', headers={'Content-Disposition':f'attachment; filename="{filename}"'})

def api_key_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not os.environ.get("JUSTTCG_API_KEY"):
            return jsonify({"error":"JUSTTCG_API_KEY no está configurada en el servidor."}), 503
        return fn(*args, **kwargs)
    return wrapper

ROLE_LEVEL = {"cashier": 1, "manager": 2, "admin": 3}
ROLE_LABEL = {"cashier": "Cajero", "manager": "Encargado", "admin": "Administrador"}

def current_user_id():
    user = getattr(g, "user", None)
    return int(user["id"]) if user else None

def active_user_count(conn=None):
    own = conn is None
    conn = conn or get_db()
    count = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    if own:
        conn.close()
    return int(count or 0)

def role_required(min_role):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = getattr(g, "user", None)
            if not user or ROLE_LEVEL.get(user["role"], 0) < ROLE_LEVEL[min_role]:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "No tienes permiso para realizar esta acción."}), 403
                flash("No tienes permiso para realizar esta acción.", "error")
                return redirect(url_for("dashboard"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.before_request
def load_logged_in_user():
    g.user = None
    # Static assets, customer display, health and authentication screens do not
    # require an employee session.
    public = {"static", "health", "brand_logo", "customer_promo", "customer_display", "login", "setup_admin"}
    endpoint = request.endpoint
    if endpoint in public or (endpoint == "customer_display_api" and request.method == "GET"):
        return None
    conn = get_db()
    has_users = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0] > 0
    if not has_users:
        conn.close()
        if request.path.startswith("/api/"):
            return jsonify({"error":"Primero crea el usuario Administrador."}), 503
        return redirect(url_for("setup_admin"))
    uid = session.get("user_id")
    if uid:
        g.user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    conn.close()
    if not g.user:
        session.pop("user_id", None)
        if request.path.startswith("/api/"):
            return jsonify({"error":"La sesión del empleado expiró. Vuelve a ingresar tu PIN."}), 401
        next_url = request.full_path if request.method == "GET" and request.path != "/" else request.path
        return redirect(url_for("login", next=next_url))

def record_inventory_movement(conn, product_id, movement_type, qty_before, qty_after, *, unit_cost_mxn=None, supplier=None, reason=None, source_type=None, source_id=None, employee_id=None, movement_date=None):
    qty_before = int(qty_before or 0)
    qty_after = int(qty_after or 0)
    conn.execute("""INSERT INTO inventory_movements(product_id,movement_type,qty_delta,qty_before,qty_after,unit_cost_mxn,supplier,reason,source_type,source_id,employee_id,movement_date,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (product_id, movement_type, qty_after-qty_before, qty_before, qty_after, unit_cost_mxn, supplier, reason, source_type, source_id, employee_id, movement_date or now_iso()[:10], now_iso()))

def returnable_sale_items(conn, sale_id):
    return conn.execute("""
        SELECT si.*, COALESCE(SUM(ri.qty),0) returned_qty,
               si.qty-COALESCE(SUM(ri.qty),0) returnable_qty
        FROM sale_items si
        LEFT JOIN refund_items ri ON ri.sale_item_id=si.id
        WHERE si.sale_id=?
        GROUP BY si.id
        ORDER BY si.id
    """, (sale_id,)).fetchall()

def process_refund(conn, sale, selections, refund_method, reason, *, cancel_sale=False):
    shift = get_open_shift(conn)
    if not shift:
        raise ValueError("Abre un turno antes de registrar una devolución o cancelación.")
    if refund_method not in {"cash", "card", "other"}:
        raise ValueError("Método de devolución inválido.")
    available = {row["id"]: row for row in returnable_sale_items(conn, sale["id"])}
    normalized = []
    subtotal_selected = 0.0
    for raw in selections:
        sale_item_id = int(raw.get("sale_item_id", 0) or 0)
        qty = int(raw.get("qty", 0) or 0)
        restock = bool(raw.get("restock", True))
        row = available.get(sale_item_id)
        if not row or qty <= 0:
            continue
        if qty > int(row["returnable_qty"] or 0):
            raise ValueError(f"No puedes devolver más unidades de {row['name']} de las que quedan disponibles.")
        subtotal_selected += float(row["unit_price_mxn"] or 0) * qty
        normalized.append((row, qty, restock))
    if not normalized:
        raise ValueError("Selecciona al menos un artículo para devolver.")
    ratio = (float(sale["total_mxn"] or 0) / float(sale["subtotal_mxn"] or 1)) if float(sale["subtotal_mxn"] or 0) > 0 else 0
    refund_total = round(subtotal_selected * ratio, 2)
    remaining_refundable = max(0.0, round(float(sale["total_mxn"] or 0) - float(sale["refunded_mxn"] or 0), 2))
    refund_total = min(refund_total, remaining_refundable)
    number = datetime.now().strftime("D%y%m%d-%H%M%S-") + uuid.uuid4().hex[:4].upper()
    credit_ratio=(float(sale["store_credit_used_mxn"] or 0)/float(sale["total_mxn"] or 1)) if float(sale["total_mxn"] or 0)>0 else 0
    credit_refund=round(refund_total*credit_ratio,2) if sale["customer_id"] else 0.0
    cur = conn.execute("INSERT INTO refunds(refund_number,sale_id,shift_id,refund_method,total_mxn,store_credit_mxn,reason,employee_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (number, sale["id"], shift["id"], refund_method, refund_total, credit_refund, reason, current_user_id(), now_iso()))
    refund_id = cur.lastrowid
    if credit_refund>0:
        add_store_credit(conn,sale["customer_id"],credit_refund,'refund_restore',source_type='refund',source_id=refund_id,reason=f'Reposición de crédito por {number}',employee_id=current_user_id())
    line_unrounded = [float(row["unit_price_mxn"] or 0) * qty * ratio for row, qty, _ in normalized]
    assigned = 0.0
    for idx, (row, qty, restock) in enumerate(normalized):
        line_total = round(line_unrounded[idx], 2) if idx < len(normalized)-1 else round(refund_total-assigned, 2)
        assigned += line_total
        conn.execute("""INSERT INTO refund_items(refund_id,sale_item_id,product_id,sku,name,qty,unit_price_mxn,unit_cost_mxn,refund_total_mxn,restocked) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (refund_id,row["id"],row["product_id"],row["sku"],row["name"],qty,row["unit_price_mxn"],row["unit_cost_mxn"],line_total,1 if restock else 0))
        if restock:
            product = conn.execute("SELECT qty,cost_mxn FROM products WHERE id=?", (row["product_id"],)).fetchone()
            if product:
                before = int(product["qty"] or 0); after = before + qty
                current_cost = float(product["cost_mxn"] or 0); returned_cost = float(row["unit_cost_mxn"] or 0)
                avg_cost = round(((current_cost*before)+(returned_cost*qty))/after,2) if after else returned_cost
                conn.execute("UPDATE products SET qty=?,cost_mxn=?,updated_at=? WHERE id=?", (after,avg_cost,now_iso(),row["product_id"]))
                record_inventory_movement(conn,row["product_id"],"return",before,after,unit_cost_mxn=returned_cost,reason=reason or "Devolución de cliente",source_type="refund",source_id=refund_id,employee_id=current_user_id())
    new_refunded = round(float(sale["refunded_mxn"] or 0)+refund_total,2)
    remaining_units = conn.execute("""SELECT COALESCE(SUM(si.qty),0)-COALESCE((SELECT SUM(ri.qty) FROM refund_items ri JOIN sale_items sx ON sx.id=ri.sale_item_id WHERE sx.sale_id=?),0) FROM sale_items si WHERE si.sale_id=?""", (sale["id"],sale["id"])).fetchone()[0]
    if cancel_sale:
        new_status = "cancelled"
    elif int(remaining_units or 0) <= 0 or new_refunded >= float(sale["total_mxn"] or 0)-0.01:
        new_status = "refunded"
    else:
        new_status = "partially_refunded"
    conn.execute("UPDATE sales SET refunded_mxn=?,status=? WHERE id=?", (new_refunded,new_status,sale["id"]))
    return {"refund_id":refund_id,"refund_number":number,"total_mxn":refund_total,"status":new_status}

def get_open_shift(conn=None):
    own = conn is None; conn = conn or get_db()
    row = conn.execute("SELECT * FROM shifts WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    if own: conn.close()
    return row

def get_display_state(conn=None):
    own = conn is None; conn = conn or get_db()
    row = conn.execute("SELECT payload,updated_at FROM display_state WHERE id=1").fetchone()
    if own: conn.close()
    if not row:
        return {"mode":"idle"}
    try:
        data = json.loads(row["payload"] or "{}")
    except Exception:
        data = {"mode":"idle"}
    data["updated_at"] = row["updated_at"]
    return data

def set_display_state(payload, conn=None):
    own = conn is None; conn = conn or get_db()
    safe = payload if isinstance(payload, dict) else {"mode":"idle"}
    raw = json.dumps(safe, ensure_ascii=False)
    if len(raw) > 100000:
        raise ValueError("La pantalla de cliente recibió demasiados datos.")
    conn.execute("INSERT INTO display_state(id,payload,updated_at) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (raw, now_iso()))
    if own:
        conn.commit(); conn.close()


def shift_expected_cash(conn, shift_id):
    shift=conn.execute("SELECT * FROM shifts WHERE id=?",(shift_id,)).fetchone()
    if not shift: return 0.0
    cash_sales=conn.execute("SELECT COALESCE(SUM(total_mxn-COALESCE(store_credit_used_mxn,0)),0) FROM sales WHERE shift_id=? AND payment_method='cash'",(shift_id,)).fetchone()[0]
    cash_refunds=conn.execute("SELECT COALESCE(SUM(total_mxn-COALESCE(store_credit_mxn,0)),0) FROM refunds WHERE shift_id=? AND refund_method='cash'",(shift_id,)).fetchone()[0]
    mv=conn.execute("SELECT COALESCE(SUM(CASE WHEN kind='pay_in' THEN amount_mxn ELSE 0 END),0) pay_in, COALESCE(SUM(CASE WHEN kind='pay_out' THEN amount_mxn ELSE 0 END),0) pay_out FROM cash_movements WHERE shift_id=?",(shift_id,)).fetchone()
    cash_trades=conn.execute("SELECT COALESCE(SUM(offer_total_mxn),0) FROM trades WHERE shift_id=? AND payout_type='cash'",(shift_id,)).fetchone()[0]
    return round(float(shift['opening_cash_mxn'] or 0)+float(cash_sales or 0)-float(cash_refunds or 0)+float(mv['pay_in'] or 0)-float(mv['pay_out'] or 0)-float(cash_trades or 0),2)

def shift_stats(conn, shift_id):
    sale=conn.execute("""SELECT COUNT(*) transactions, COALESCE(SUM(total_mxn),0) gross_sales, COALESCE(SUM(CASE WHEN payment_method='cash' THEN total_mxn-COALESCE(store_credit_used_mxn,0) ELSE 0 END),0) cash_sales, COALESCE(SUM(CASE WHEN payment_method='card' THEN total_mxn-COALESCE(store_credit_used_mxn,0) ELSE 0 END),0) card_sales, COALESCE(SUM(CASE WHEN payment_method='other' THEN total_mxn-COALESCE(store_credit_used_mxn,0) ELSE 0 END),0) other_sales, COALESCE(SUM(discount_mxn),0) discounts FROM sales WHERE shift_id=?""",(shift_id,)).fetchone()
    refunds=conn.execute("""SELECT COUNT(*) refund_count, COALESCE(SUM(total_mxn),0) refund_total, COALESCE(SUM(CASE WHEN refund_method='cash' THEN total_mxn-COALESCE(store_credit_mxn,0) ELSE 0 END),0) cash_refunds, COALESCE(SUM(CASE WHEN refund_method='card' THEN total_mxn-COALESCE(store_credit_mxn,0) ELSE 0 END),0) card_refunds, COALESCE(SUM(CASE WHEN refund_method='other' THEN total_mxn-COALESCE(store_credit_mxn,0) ELSE 0 END),0) other_refunds FROM refunds WHERE shift_id=?""",(shift_id,)).fetchone()
    item=conn.execute("""SELECT COALESCE(SUM(si.qty),0) items, COALESCE(SUM((si.unit_price_mxn-si.unit_cost_mxn)*si.qty),0) gross_before_discount FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.shift_id=?""",(shift_id,)).fetchone()
    returned=conn.execute("""SELECT COALESCE(SUM(ri.qty),0) items_returned, COALESCE(SUM(ri.refund_total_mxn-(ri.unit_cost_mxn*ri.qty)),0) returned_margin FROM refund_items ri JOIN refunds r ON r.id=ri.refund_id WHERE r.shift_id=?""",(shift_id,)).fetchone()
    trade=conn.execute("""SELECT COUNT(*) trade_count, COALESCE(SUM(market_total_mxn),0) trade_market, COALESCE(SUM(offer_total_mxn),0) trade_offer, COALESCE(SUM(CASE WHEN payout_type='cash' THEN offer_total_mxn ELSE 0 END),0) trade_cash, COALESCE(SUM(CASE WHEN payout_type='credit' THEN offer_total_mxn ELSE 0 END),0) trade_credit FROM trades WHERE shift_id=?""",(shift_id,)).fetchone()
    gross_sales=float(sale['gross_sales'] or 0); refund_total=float(refunds['refund_total'] or 0)
    return {
        "transactions":sale['transactions'],
        "refund_count":int(refunds['refund_count'] or 0),
        "refund_total":refund_total,
        "sales_total":gross_sales-refund_total,
        "gross_sales":gross_sales,
        "cash_total":float(sale['cash_sales'] or 0)-float(refunds['cash_refunds'] or 0),
        "card_total":float(sale['card_sales'] or 0)-float(refunds['card_refunds'] or 0),
        "other_total":float(sale['other_sales'] or 0)-float(refunds['other_refunds'] or 0),
        "discounts":float(sale['discounts'] or 0),
        "items":int(item['items'] or 0),
        "gross_profit":float(item['gross_before_discount'] or 0)-float(sale['discounts'] or 0)-float(returned['returned_margin'] or 0),
        "trade_count":int(trade['trade_count'] or 0),
        "trade_market":float(trade['trade_market'] or 0),
        "trade_offer":float(trade['trade_offer'] or 0),
        "trade_cash":float(trade['trade_cash'] or 0),
        "trade_credit":float(trade['trade_credit'] or 0),
        "expected_cash":shift_expected_cash(conn,shift_id),
    }

@app.context_processor
def inject_globals():
    return {"open_shift": get_open_shift(), "store": get_store_settings(), "current_user": getattr(g, "user", None), "role_label": ROLE_LABEL}


@app.context_processor
def inject_desktop_runtime():
    return {
        "desktop_mode": os.environ.get("COLLECTOR_DESKTOP") == "1",
        "desktop_port": os.environ.get("COLLECTOR_PORT", "8765"),
        "desktop_lan_ip": os.environ.get("COLLECTOR_LAN_IP", ""),
    }

@app.route("/setup-admin", methods=["GET", "POST"])
def setup_admin():
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0] > 0:
        conn.close()
        return redirect(url_for("login"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        pin = (request.form.get("pin") or "").strip()
        confirm = (request.form.get("pin_confirm") or "").strip()
        if not name:
            conn.close(); flash("Escribe el nombre del administrador.", "error"); return redirect(url_for("setup_admin"))
        if not re.fullmatch(r"\d{4,8}", pin):
            conn.close(); flash("El PIN debe tener entre 4 y 8 números.", "error"); return redirect(url_for("setup_admin"))
        if pin != confirm:
            conn.close(); flash("Los PIN no coinciden.", "error"); return redirect(url_for("setup_admin"))
        ts = now_iso()
        cur = conn.execute("INSERT INTO users(name,pin_hash,role,active,created_at,updated_at) VALUES (?,?, 'admin',1,?,?)", (name,generate_password_hash(pin),ts,ts))
        conn.commit(); uid = cur.lastrowid; conn.close()
        session.clear(); session["user_id"] = uid; session.permanent = True
        flash("Administrador creado. Collector POS ya está protegido por PIN.", "success")
        return redirect(url_for("dashboard"))
    conn.close()
    return render_template("setup_admin.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    users = conn.execute("SELECT id,name,role FROM users WHERE active=1 ORDER BY CASE role WHEN 'admin' THEN 1 WHEN 'manager' THEN 2 ELSE 3 END,name COLLATE NOCASE").fetchall()
    if not users:
        conn.close(); return redirect(url_for("setup_admin"))
    if request.method == "POST":
        try: uid = int(request.form.get("user_id", 0) or 0)
        except ValueError: uid = 0
        pin = (request.form.get("pin") or "").strip()
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if not user or not check_password_hash(user["pin_hash"], pin):
            conn.close(); flash("Usuario o PIN incorrecto.", "error"); return render_template("login.html", users=users)
        conn.execute("UPDATE users SET last_login_at=?,updated_at=? WHERE id=?", (now_iso(),now_iso(),uid)); conn.commit(); conn.close()
        session.clear(); session["user_id"] = uid; session.permanent = True
        target = request.args.get("next") or request.form.get("next") or url_for("dashboard")
        if not target.startswith("/"): target = url_for("dashboard")
        return redirect(target)
    conn.close()
    return render_template("login.html", users=users)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/usuarios")
@role_required("admin")
def users():
    conn=get_db(); rows=conn.execute("SELECT * FROM users ORDER BY active DESC, CASE role WHEN 'admin' THEN 1 WHEN 'manager' THEN 2 ELSE 3 END,name COLLATE NOCASE").fetchall(); conn.close()
    return render_template("users.html", users=rows)

@app.route("/usuarios/nuevo", methods=["POST"])
@role_required("admin")
def user_new():
    name=(request.form.get("name") or "").strip(); pin=(request.form.get("pin") or "").strip(); role=(request.form.get("role") or "cashier").strip()
    if not name or role not in ROLE_LEVEL or not re.fullmatch(r"\d{4,8}",pin):
        flash("Revisa el nombre, rol y PIN (4 a 8 números).","error"); return redirect(url_for("users"))
    ts=now_iso(); conn=get_db(); conn.execute("INSERT INTO users(name,pin_hash,role,active,created_at,updated_at) VALUES (?,?,?,?,?,?)",(name,generate_password_hash(pin),role,1,ts,ts)); conn.commit(); conn.close(); flash("Empleado agregado.","success"); return redirect(url_for("users"))

@app.route("/usuarios/<int:user_id>/pin", methods=["POST"])
@role_required("admin")
def user_pin(user_id):
    pin=(request.form.get("pin") or "").strip()
    if not re.fullmatch(r"\d{4,8}",pin): flash("El PIN debe tener entre 4 y 8 números.","error"); return redirect(url_for("users"))
    conn=get_db(); conn.execute("UPDATE users SET pin_hash=?,updated_at=? WHERE id=?",(generate_password_hash(pin),now_iso(),user_id)); conn.commit(); conn.close(); flash("PIN actualizado.","success"); return redirect(url_for("users"))

@app.route("/usuarios/<int:user_id>/toggle", methods=["POST"])
@role_required("admin")
def user_toggle(user_id):
    if user_id == current_user_id(): flash("No puedes desactivar tu propio usuario mientras está en uso.","error"); return redirect(url_for("users"))
    conn=get_db(); user=conn.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
    if not user: conn.close(); abort(404)
    new_active=0 if user["active"] else 1
    if user["role"]=="admin" and new_active==0:
        admins=conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
        if admins<=1: conn.close(); flash("Debe quedar al menos un administrador activo.","error"); return redirect(url_for("users"))
    conn.execute("UPDATE users SET active=?,updated_at=? WHERE id=?",(new_active,now_iso(),user_id)); conn.commit(); conn.close(); flash("Usuario actualizado.","success"); return redirect(url_for("users"))

@app.route("/brand/logo")
def brand_logo():
    settings = get_store_settings()
    filename = settings.get("logo_filename")
    if filename:
        path = DATA_DIR / "brand" / filename
        if path.exists():
            return send_file(path)
    return send_file(BASE_DIR / "static" / "brand" / "default-logo.svg", mimetype="image/svg+xml")


@app.route("/brand/customer-promo")
def customer_promo():
    settings = get_store_settings()
    filename = settings.get("customer_promo_filename")
    if filename:
        path = DATA_DIR / "brand" / filename
        if path.exists():
            return send_file(path)
    return redirect(url_for("brand_logo"))

@app.route("/configuracion", methods=["GET", "POST"])
@role_required("admin")
def configuration():
    conn = get_db()
    current = get_store_settings(conn)
    if request.method == "POST":
        store_name = (request.form.get("store_name") or "").strip() or current["store_name"]
        subtitle = (request.form.get("store_subtitle") or "").strip() or DEFAULT_STORE_SETTINGS["store_subtitle"]
        social = (request.form.get("social_handle") or "").strip()
        primary = valid_hex(request.form.get("primary_color"), current["primary_color"])
        secondary = valid_hex(request.form.get("secondary_color"), current["secondary_color"])
        accent = valid_hex(request.form.get("accent_color"), current["accent_color"])
        try:
            credit = min(1.0, max(0.0, float(request.form.get("trade_credit_percent", 70) or 70) / 100.0))
            cash = min(1.0, max(0.0, float(request.form.get("trade_cash_percent", 60) or 60) / 100.0))
        except ValueError:
            credit, cash = current["trade_credit_rate"], current["trade_cash_rate"]
        footer = (request.form.get("receipt_footer") or "").strip() or DEFAULT_STORE_SETTINGS["receipt_footer"]
        try:
            usd_mxn_rate = max(0.01, float(request.form.get("usd_mxn_rate", current.get("usd_mxn_rate", 17.0)) or current.get("usd_mxn_rate", 17.0)))
            auto_price_multiplier = max(0.01, float(request.form.get("auto_price_multiplier", current.get("auto_price_multiplier", 1.0)) or 1.0))
            auto_price_round_to = max(0.01, float(request.form.get("auto_price_round_to", current.get("auto_price_round_to", 1.0)) or 1.0))
        except ValueError:
            usd_mxn_rate = float(current.get("usd_mxn_rate") or 17.0)
            auto_price_multiplier = float(current.get("auto_price_multiplier") or 1.0)
            auto_price_round_to = float(current.get("auto_price_round_to") or 1.0)
        auto_fx_enabled = 1 if request.form.get("auto_fx_enabled") else 0
        auto_price_tcg_enabled = 1 if request.form.get("auto_price_tcg_enabled") else 0
        store_timezone = (request.form.get("store_timezone") or current.get("store_timezone") or "UTC").strip() or "UTC"
        customer_values = {}
        for key in ("customer_header_label","customer_welcome_label","customer_welcome_title","customer_welcome_text","customer_badge_1","customer_badge_2","customer_badge_3","customer_help_title","customer_help_text","customer_thanks_title","customer_receipt_note","customer_footer_1","customer_footer_2","customer_footer_3"):
            customer_values[key] = (request.form.get(key) or "").strip() or DEFAULT_STORE_SETTINGS[key]
        license_key = (request.form.get("license_key") or "").strip() or current.get("license_key") or ""
        logo_filename = current.get("logo_filename")
        upload = request.files.get("logo")
        if upload and upload.filename:
            ext = Path(upload.filename).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                conn.close()
                flash("El logo debe ser PNG, JPG o WEBP.", "error")
                return redirect(url_for("configuration"))
            logo_dir = DATA_DIR / "brand"
            logo_dir.mkdir(parents=True, exist_ok=True)
            logo_filename = f"store-logo{ext}"
            for old in logo_dir.glob("store-logo.*"):
                try: old.unlink()
                except OSError: pass
            upload.save(logo_dir / logo_filename)
        customer_promo_filename = current.get("customer_promo_filename")
        promo_upload = request.files.get("customer_promo")
        if promo_upload and promo_upload.filename:
            ext = Path(promo_upload.filename).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
                conn.close()
                flash("La imagen promocional debe ser PNG, JPG o WEBP.", "error")
                return redirect(url_for("configuration"))
            logo_dir = DATA_DIR / "brand"
            logo_dir.mkdir(parents=True, exist_ok=True)
            customer_promo_filename = f"customer-promo{ext}"
            for old in logo_dir.glob("customer-promo.*"):
                try: old.unlink()
                except OSError: pass
            promo_upload.save(logo_dir / customer_promo_filename)
        conn.execute("""UPDATE store_settings SET store_name=?,store_subtitle=?,social_handle=?,primary_color=?,secondary_color=?,accent_color=?,trade_credit_rate=?,trade_cash_rate=?,receipt_footer=?,customer_header_label=?,customer_welcome_label=?,customer_welcome_title=?,customer_welcome_text=?,customer_badge_1=?,customer_badge_2=?,customer_badge_3=?,customer_help_title=?,customer_help_text=?,customer_thanks_title=?,customer_receipt_note=?,customer_footer_1=?,customer_footer_2=?,customer_footer_3=?,license_key=?,logo_filename=?,customer_promo_filename=?,updated_at=? WHERE id=1""", (store_name, subtitle, social, primary, secondary, accent, credit, cash, footer, customer_values["customer_header_label"], customer_values["customer_welcome_label"], customer_values["customer_welcome_title"], customer_values["customer_welcome_text"], customer_values["customer_badge_1"], customer_values["customer_badge_2"], customer_values["customer_badge_3"], customer_values["customer_help_title"], customer_values["customer_help_text"], customer_values["customer_thanks_title"], customer_values["customer_receipt_note"], customer_values["customer_footer_1"], customer_values["customer_footer_2"], customer_values["customer_footer_3"], license_key, logo_filename, customer_promo_filename, now_iso()))
        conn.execute("""UPDATE store_settings SET usd_mxn_rate=?,auto_fx_enabled=?,auto_price_tcg_enabled=?,auto_price_multiplier=?,auto_price_round_to=?,store_timezone=?,updated_at=? WHERE id=1""", (usd_mxn_rate, auto_fx_enabled, auto_price_tcg_enabled, auto_price_multiplier, auto_price_round_to, store_timezone, now_iso()))
        conn.commit(); conn.close()
        flash("Personalización guardada.", "success")
        return redirect(url_for("configuration"))
    conn.close()
    return render_template("configuration.html", settings=current)

@app.route("/api/prices/refresh", methods=["POST"])
@role_required("admin")
def prices_refresh_now():
    try:
        result = run_price_refresh("manual")
        return jsonify(result), (200 if result.get("ok") else 500)
    except Exception as exc:
        app.logger.exception("Manual price refresh failed")
        return jsonify({"ok":False,"error":str(exc)}), 500

@app.route("/api/prices/status")
@role_required("manager")
def prices_status():
    conn=get_db()
    settings=get_store_settings(conn)
    runs=[dict(r) for r in conn.execute("SELECT * FROM price_refresh_runs ORDER BY id DESC LIMIT 5").fetchall()]
    linked=conn.execute("SELECT COUNT(*) FROM products WHERE active=1 AND category='TCG' AND auto_price_enabled=1 AND external_card_id IS NOT NULL").fetchone()[0]
    conn.close()
    return jsonify({"last_at":settings.get("last_price_refresh_at"),"last_status":settings.get("last_price_refresh_status"),"usd_mxn_rate":settings.get("usd_mxn_rate"),"fx_updated_at":settings.get("fx_updated_at"),"linked_products":linked,"schedule":["00:00","12:00","17:00"],"timezone":settings.get("store_timezone"),"runs":runs})

@app.route("/")
def pos():
    conn=get_db()
    products=conn.execute("SELECT * FROM products WHERE active=1 AND qty>0 ORDER BY name COLLATE NOCASE LIMIT 300").fetchall()
    shift=get_open_shift(conn)
    customers=conn.execute("""SELECT c.*,COALESCE((SELECT SUM(amount_mxn) FROM store_credit_transactions t WHERE t.customer_id=c.id),0) credit_balance FROM customers c WHERE c.active=1 ORDER BY c.name COLLATE NOCASE LIMIT 500""").fetchall()
    conn.close()
    return render_template("pos.html",products=products,shift=shift,customers=customers)

@app.route("/dashboard")
def dashboard():
    conn=get_db(); shift=get_open_shift(conn) or conn.execute("SELECT * FROM shifts ORDER BY id DESC LIMIT 1").fetchone(); stats=shift_stats(conn,shift['id']) if shift else None
    top=[]; recent_sales=[]; recent_trades=[]
    if shift:
        top=conn.execute("SELECT si.name,SUM(si.qty) qty,SUM(si.line_total_mxn) revenue FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.shift_id=? GROUP BY si.name ORDER BY qty DESC,revenue DESC LIMIT 5",(shift['id'],)).fetchall()
        recent_sales=conn.execute("SELECT * FROM sales WHERE shift_id=? ORDER BY id DESC LIMIT 6",(shift['id'],)).fetchall(); recent_trades=conn.execute("SELECT * FROM trades WHERE shift_id=? ORDER BY id DESC LIMIT 5",(shift['id'],)).fetchall()
    inv=conn.execute("SELECT COUNT(*) sku_count, COALESCE(SUM(qty),0) units, COALESCE(SUM(cost_mxn*qty),0) cost_value, COALESCE(SUM(price_mxn*qty),0) retail_value FROM products WHERE active=1").fetchone(); low=conn.execute("SELECT * FROM products WHERE active=1 AND qty BETWEEN 0 AND 2 ORDER BY qty,name LIMIT 8").fetchall(); conn.close()
    return render_template("dashboard.html",shift=shift,stats=stats,inventory_stats=inv,top_products=top,low_stock=low,recent_sales=recent_sales,recent_trades=recent_trades)

@app.route("/inventory")
def inventory():
    q=request.args.get('q','').strip(); conn=get_db()
    if q:
        like=f"%{q}%"; products=conn.execute("SELECT * FROM products WHERE active=1 AND (name LIKE ? OR sku LIKE ? OR game LIKE ? OR set_name LIKE ?) ORDER BY name COLLATE NOCASE",(like,like,like,like)).fetchall()
    else: products=conn.execute("SELECT * FROM products WHERE active=1 ORDER BY updated_at DESC").fetchall()
    conn.close(); return render_template("inventory.html",products=products,q=q)

@app.route("/inventory/new",methods=["GET","POST"])
@role_required("manager")
def inventory_new():
    if request.method=='POST':
        name=request.form.get('name','').strip()
        if not name: flash('El nombre es obligatorio.','error'); return redirect(url_for('inventory_new'))
        sku=request.form.get('sku','').strip() or make_sku('COL'); category=request.form.get('category','Other').strip() or 'Other'; sub=request.form.get('subcategory','').strip() or None; qty=max(0,int(request.form.get('qty',0) or 0)); cost=max(0,float(request.form.get('cost_mxn',0) or 0)); price=max(0,float(request.form.get('price_mxn',0) or 0)); notes=request.form.get('notes','').strip() or None; supplier=request.form.get('supplier','').strip() or None; ts=now_iso(); conn=get_db()
        try:
            cur=conn.execute("INSERT INTO products(sku,name,category,subcategory,cost_mxn,price_mxn,qty,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(sku,name,category,sub,cost,price,qty,notes,ts,ts)); pid=cur.lastrowid
            if qty:
                record_inventory_movement(conn,pid,"stock_in",0,qty,unit_cost_mxn=cost,supplier=supplier,reason="Inventario inicial",source_type="manual",employee_id=current_user_id())
            conn.commit()
        except sqlite3.IntegrityError: conn.rollback(); conn.close(); flash('Ese SKU ya está en uso.','error'); return redirect(url_for('inventory_new'))
        conn.close(); flash('Producto agregado al inventario.','success'); return redirect(url_for('inventory_edit',product_id=pid))
    return render_template('new_item.html')

@app.route("/inventory/<int:product_id>/edit",methods=["GET","POST"])
@role_required("manager")
def inventory_edit(product_id):
    conn=get_db(); product=conn.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
    if not product: conn.close(); return 'No encontrado',404
    if request.method=='POST':
        vals=(request.form.get('sku','').strip(),request.form.get('name','').strip(),request.form.get('category','Other').strip() or 'Other',request.form.get('subcategory','').strip() or None,max(0,float(request.form.get('cost_mxn',0) or 0)),max(0,float(request.form.get('price_mxn',0) or 0)),request.form.get('notes','').strip() or None,now_iso(),product_id)
        try:
            conn.execute("UPDATE products SET sku=?,name=?,category=?,subcategory=?,cost_mxn=?,price_mxn=?,notes=?,updated_at=? WHERE id=?",vals)
            auto_price = 1 if request.form.get('auto_price_enabled') else 0
            if product['category'] == 'TCG' or request.form.get('category','') == 'TCG':
                conn.execute("UPDATE products SET auto_price_enabled=? WHERE id=?",(auto_price,product_id))
            conn.commit()
        except sqlite3.IntegrityError: conn.close(); flash('Ese SKU ya está en uso.','error'); return redirect(url_for('inventory_edit',product_id=product_id))
        conn.close(); flash('Producto actualizado.','success'); return redirect(url_for('inventory_edit',product_id=product_id))
    movements=conn.execute("""SELECT im.*,u.name employee_name FROM inventory_movements im LEFT JOIN users u ON u.id=im.employee_id WHERE im.product_id=? ORDER BY im.id DESC LIMIT 100""",(product_id,)).fetchall(); price_history=conn.execute("SELECT * FROM product_price_history WHERE product_id=? ORDER BY id DESC LIMIT 30",(product_id,)).fetchall(); conn.close(); return render_template('edit_item.html',product=product,movements=movements,price_history=price_history)

@app.route("/inventory/<int:product_id>/stock",methods=["POST"])
@role_required("manager")
def inventory_stock(product_id):
    try:
        qty=int(request.form.get('qty_received',0) or 0); unit_cost=float(request.form.get('unit_cost_mxn',0) or 0)
    except ValueError:
        flash('Cantidad o costo inválido.','error'); return redirect(url_for('inventory_edit',product_id=product_id))
    if qty<=0 or unit_cost<0: flash('La cantidad debe ser mayor a cero y el costo no puede ser negativo.','error'); return redirect(url_for('inventory_edit',product_id=product_id))
    supplier=(request.form.get('supplier') or '').strip() or None; movement_date=(request.form.get('movement_date') or '').strip() or now_iso()[:10]; note=(request.form.get('note') or '').strip() or 'Entrada de mercancía'
    conn=get_db()
    try:
        conn.execute('BEGIN IMMEDIATE'); product=conn.execute("SELECT * FROM products WHERE id=? AND active=1",(product_id,)).fetchone()
        if not product: raise ValueError('Producto no encontrado.')
        before=int(product['qty'] or 0); after=before+qty
        old_cost=float(product['cost_mxn'] or 0)
        avg_cost=round(((old_cost*before)+(unit_cost*qty))/after,2) if after>0 else unit_cost
        conn.execute("UPDATE products SET qty=?,cost_mxn=?,updated_at=? WHERE id=?",(after,avg_cost,now_iso(),product_id))
        record_inventory_movement(conn,product_id,'stock_in',before,after,unit_cost_mxn=unit_cost,supplier=supplier,reason=note,source_type='purchase',employee_id=current_user_id(),movement_date=movement_date)
        conn.commit(); flash(f'Se agregaron {qty} unidades. Stock actual: {after}. Costo promedio: ${money(avg_cost)}.','success')
    except (ValueError,TypeError) as exc: conn.rollback(); flash(str(exc),'error')
    finally: conn.close()
    return redirect(url_for('inventory_edit',product_id=product_id))

@app.route("/inventory/<int:product_id>/adjust",methods=["POST"])
@role_required("manager")
def inventory_adjust(product_id):
    try: counted=max(0,int(request.form.get('counted_qty',0) or 0))
    except ValueError: flash('Stock contado inválido.','error'); return redirect(url_for('inventory_edit',product_id=product_id))
    reason=(request.form.get('reason') or '').strip()
    if not reason: flash('Escribe el motivo del ajuste.','error'); return redirect(url_for('inventory_edit',product_id=product_id))
    conn=get_db()
    try:
        conn.execute('BEGIN IMMEDIATE'); product=conn.execute("SELECT * FROM products WHERE id=? AND active=1",(product_id,)).fetchone()
        if not product: raise ValueError('Producto no encontrado.')
        before=int(product['qty'] or 0); after=counted
        if before==after: raise ValueError('El stock contado es igual al stock del sistema.')
        conn.execute("UPDATE products SET qty=?,updated_at=? WHERE id=?",(after,now_iso(),product_id))
        record_inventory_movement(conn,product_id,'adjustment',before,after,unit_cost_mxn=product['cost_mxn'],reason=reason,source_type='adjustment',employee_id=current_user_id())
        conn.commit(); flash(f'Stock ajustado de {before} a {after}.','success')
    except ValueError as exc: conn.rollback(); flash(str(exc),'error')
    finally: conn.close()
    return redirect(url_for('inventory_edit',product_id=product_id))

@app.route("/inventory/<int:product_id>/archive",methods=["POST"])
@role_required("manager")
def inventory_archive(product_id):
    conn=get_db(); product=conn.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
    if product:
        record_inventory_movement(conn,product_id,'archive',int(product['qty'] or 0),int(product['qty'] or 0),unit_cost_mxn=product['cost_mxn'],reason='Producto archivado',source_type='manual',employee_id=current_user_id())
        conn.execute("UPDATE products SET active=0,updated_at=? WHERE id=?",(now_iso(),product_id)); conn.commit()
    conn.close(); flash('Producto archivado.','success'); return redirect(url_for('inventory'))

@app.route("/tcg")
@role_required("manager")
def tcg_import():
    return render_template("tcg_import.html")


def is_pokemon_game(game):
    normalized = (game or "").strip().lower().replace("é", "e")
    return normalized in {"", "pokemon", "pokemon tcg"}


def tcgdex_image_url(base, quality="high"):
    if not base:
        return None
    if re.search(r"\.(?:png|webp|jpe?g)$", base, re.I):
        return base
    return f"{base.rstrip('/')}/{quality}.webp"


def parse_pokemon_query(query):
    """Split a useful trailing card number from a name, e.g. 'Bulbasaur 037'."""
    query = re.sub(r"\s+", " ", query.strip())
    match = re.match(r"^(.*?)(?:\s+#?([A-Za-z]*\d+[A-Za-z0-9./-]*))$", query)
    if match and match.group(1).strip():
        return match.group(1).strip(), match.group(2).strip()
    return query, None


def get_tcgdex_detail(card_id):
    now = time.time()
    with _TCGDEX_CACHE_LOCK:
        cached = _TCGDEX_DETAIL_CACHE.get(card_id)
        if cached and now - cached[0] < TCGDEX_CACHE_TTL:
            return cached[1]

    response = requests.get(f"{TCGDEX_API_BASE}/cards/{card_id}", timeout=12)
    response.raise_for_status()
    detail = response.json()
    with _TCGDEX_CACHE_LOCK:
        _TCGDEX_DETAIL_CACHE[card_id] = (now, detail)
    return detail


def get_price_block(tcgplayer, *keys):
    for key in keys:
        block = tcgplayer.get(key)
        if isinstance(block, dict):
            return block
    return {}


def normalize_tcgdex_variants(card):
    tcgplayer = ((card.get("pricing") or {}).get("tcgplayer") or {})
    known = card.get("variants") or {}
    updated = tcgplayer.get("updated")

    specs = [
        ("normal", "Normal", ("normal",), bool(known.get("normal"))),
        ("reverse", "Reverse Holo", ("reverse", "reverse-holofoil", "reverseHolofoil"), bool(known.get("reverse"))),
        ("holo", "Holofoil", ("holo", "holofoil"), bool(known.get("holo"))),
        ("first-edition", "1st Edition", ("1st-edition", "firstEdition"), bool(known.get("firstEdition"))),
        ("first-edition-holo", "1st Edition Holofoil", ("1st-edition-holofoil", "firstEditionHolofoil"), False),
        ("unlimited", "Unlimited", ("unlimited",), False),
        ("unlimited-holo", "Unlimited Holofoil", ("unlimited-holofoil",), False),
    ]

    variants = []
    for variant_id, label, keys, exists in specs:
        block = get_price_block(tcgplayer, *keys)
        if not exists and not block:
            continue
        variants.append({
            "id": f"{card.get('id')}:{variant_id}",
            "printing": label,
            "price": block.get("marketPrice"),
            "low": block.get("lowPrice"),
            "mid": block.get("midPrice"),
            "high": block.get("highPrice"),
            "direct_low": block.get("directLowPrice"),
            "pricing_updated": updated,
        })

    if not variants:
        variants.append({
            "id": f"{card.get('id')}:unspecified",
            "printing": "Unspecified",
            "price": None,
            "low": None,
            "mid": None,
            "high": None,
            "direct_low": None,
            "pricing_updated": updated,
        })
    return variants


def normalize_tcgdex_card(card):
    set_info = card.get("set") or {}
    return {
        "provider": "TCGdex",
        "card_id": card.get("id"),
        "name": card.get("name"),
        "game": "Pokemon",
        "set_name": set_info.get("name"),
        "set_id": set_info.get("id"),
        "number": str(card.get("localId") or ""),
        "rarity": card.get("rarity"),
        "image_url": tcgdex_image_url(card.get("image"), "high"),
        "image_thumb": tcgdex_image_url(card.get("image"), "low"),
        "variants": normalize_tcgdex_variants(card),
    }


def search_tcgdex(query):
    name, card_number = parse_pokemon_query(query)
    base_params = {
        "pagination:page": 1,
        "pagination:itemsPerPage": 12,
    }
    if card_number:
        base_params["localId"] = card_number

    # People often type a set hint after the card name (for example
    # "Bulbasaur Promo 037"). TCGdex filters only the actual card name, so
    # progressively trim trailing words if the first exact-ish search is empty.
    name_tokens = name.split()
    candidates = [" ".join(name_tokens[:cut]) for cut in range(len(name_tokens), max(0, len(name_tokens) - 3), -1)]
    briefs = []
    try:
        for candidate in candidates:
            params = dict(base_params)
            params["name"] = candidate
            response = requests.get(f"{TCGDEX_API_BASE}/cards", params=params, timeout=12)
            response.raise_for_status()
            candidate_briefs = response.json()
            if isinstance(candidate_briefs, list) and candidate_briefs:
                briefs = candidate_briefs
                break
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"No se pudo conectar con TCGdex: {exc}") from exc

    if not isinstance(briefs, list):
        return []

    # Detail responses contain set, variants and TCGplayer pricing. Cache them to
    # respect TCGdex's guidance against repeatedly fetching the same card data.
    details = [None] * len(briefs)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(get_tcgdex_detail, brief.get("id")): idx for idx, brief in enumerate(briefs) if brief.get("id")}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                details[idx] = future.result()
            except (requests.RequestException, ValueError):
                # A single detail failure should not kill the whole search.
                brief = briefs[idx]
                details[idx] = {
                    "id": brief.get("id"),
                    "name": brief.get("name"),
                    "localId": brief.get("localId"),
                    "image": brief.get("image"),
                    "variants": {},
                    "set": {},
                }

    return [normalize_tcgdex_card(card) for card in details if card and card.get("id")]


def search_justtcg(query, game):
    api_key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    if not api_key or api_key == "tcg_replace_me":
        raise RuntimeError("JustTCG no está configurado. La búsqueda de Pokémon funciona sin clave.")

    params = {"q": query, "limit": 20}
    if game:
        params["game"] = game
    try:
        response = requests.get(
            JUSTTCG_API_URL,
            params=params,
            headers={"x-api-key": api_key},
            timeout=12,
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"No se pudo conectar con JustTCG: {exc}") from exc
    if not response.ok:
        raise RuntimeError(payload.get("error") or f"JustTCG returned HTTP {response.status_code}.")

    cards = []
    for card in payload.get("data", []):
        variants = []
        for variant in card.get("variants") or []:
            variants.append({
                "id": variant.get("uuid") or variant.get("id"),
                "printing": variant.get("printing") or "Unspecified",
                "condition": variant.get("condition"),
                "language": variant.get("language"),
                "price": variant.get("price"),
                "low": variant.get("minPrice7d"),
                "mid": variant.get("avgPrice"),
                "high": variant.get("maxPrice7d"),
                "pricing_updated": variant.get("lastUpdated") or variant.get("updatedAt"),
            })
        cards.append({
            "provider": "JustTCG",
            "card_id": card.get("uuid") or card.get("id"),
            "name": card.get("name"),
            "game": card.get("game") or game,
            "set_name": card.get("set_name") or card.get("set"),
            "number": card.get("number"),
            "rarity": card.get("rarity"),
            "image_url": card.get("image") or card.get("image_url"),
            "image_thumb": card.get("image") or card.get("image_url"),
            "variants": variants,
        })
    return cards, payload.get("_metadata", {})


@app.route("/api/tcg/search")
@role_required("manager")
def tcg_search():
    query = request.args.get("q", "").strip()
    game = request.args.get("game", "Pokemon").strip() or "Pokemon"
    if len(query) < 2:
        return jsonify({"error": "Escribe al menos 2 caracteres."}), 400

    try:
        if is_pokemon_game(game):
            cards = search_tcgdex(query)
            return jsonify({"data": cards, "provider": "TCGdex", "usage": {}})
        cards, usage = search_justtcg(query, game)
        return jsonify({"data": cards, "provider": "JustTCG", "usage": usage})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


def _match_text(value):
    value = str(value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _match_card_number(value):
    value = str(value or "").lower().replace("#", "").strip()
    value = re.sub(r"[^a-z0-9]", "", value)
    if value.isdigit():
        return str(int(value or "0"))
    return value.lstrip("0") or value


def find_justtcg_price_fallback(name, card_number=None, set_name=None, printing=None):
    """Use one JustTCG request only when TCGdex has no TCGplayer price."""
    query = " ".join(x for x in [name, card_number] if x).strip()
    cards, usage = search_justtcg(query, "Pokemon")
    target_name = _match_text(name)
    target_number = _match_card_number(card_number)
    target_set = _match_text(set_name)
    target_printing = _match_text(printing)

    ranked = []
    for card in cards:
        score = 0
        cname = _match_text(card.get("name"))
        cnumber = _match_card_number(card.get("number"))
        cset = _match_text(card.get("set_name"))
        if cname == target_name:
            score += 8
        elif target_name and (target_name in cname or cname in target_name):
            score += 4
        if target_number and cnumber == target_number:
            score += 10
        if target_set and cset:
            if target_set == cset:
                score += 6
            elif target_set in cset or cset in target_set:
                score += 3
        ranked.append((score, card))

    ranked.sort(key=lambda x: x[0], reverse=True)
    for score, card in ranked:
        if score < (12 if target_number else 8):
            continue
        variants = [v for v in (card.get("variants") or []) if v.get("price") not in (None, "")]
        if not variants:
            continue

        def variant_score(v):
            points = 0
            cond = _match_text(v.get("condition"))
            p = _match_text(v.get("printing"))
            lang = _match_text(v.get("language"))
            if cond in {"near mint", "nm"}:
                points += 12
            elif cond in {"lightly played", "lp"}:
                points += 2
            else:
                points -= 4
            if target_printing:
                if p == target_printing:
                    points += 8
                elif p and (p in target_printing or target_printing in p):
                    points += 4
            if lang in {"english", "en"}:
                points += 2
            return points

        variants.sort(key=variant_score, reverse=True)
        best = variants[0]
        # Do not use clearly different-condition pricing as the market baseline.
        condition = _match_text(best.get("condition"))
        if condition not in {"near mint", "nm", ""}:
            continue
        return {
            "found": True,
            "card_id": card.get("card_id"),
            "variant_id": best.get("id"),
            "price": best.get("price"),
            "low": best.get("low"),
            "mid": best.get("mid"),
            "high": best.get("high"),
            "condition": best.get("condition") or "Near Mint",
            "printing": best.get("printing"),
            "language": best.get("language"),
            "set_name": card.get("set_name"),
            "number": card.get("number"),
            "usage": usage,
        }
    return {"found": False, "usage": usage}


@app.route("/api/tcg/fallback-price")
@role_required("manager")
def tcg_fallback_price():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "Falta el nombre de la carta."}), 400
    try:
        result = find_justtcg_price_fallback(
            name,
            request.args.get("number", "").strip() or None,
            request.args.get("set", "").strip() or None,
            request.args.get("printing", "").strip() or None,
        )
        return jsonify(result)
    except RuntimeError as exc:
        return jsonify({"found": False, "error": str(exc)}), 503


@app.route("/api/tcg/import", methods=["POST"])
@role_required("manager")
def tcg_import_product():
    payload = request.get_json(silent=True) or {}
    required = ["card_id", "variant_id", "name", "game", "condition", "printing"]
    missing = [key for key in required if not payload.get(key)]
    if missing:
        return jsonify({"error": f"Faltan datos: {', '.join(missing)}"}), 400

    qty = max(0, int(payload.get("qty", 1) or 1))
    cost_mxn = max(0.0, float(payload.get("cost_mxn", 0) or 0))
    price_mxn = max(0.0, float(payload.get("price_mxn", 0) or 0))

    def maybe_float(value):
        if value in (None, ""):
            return None
        return float(value)

    market_price_usd = maybe_float(payload.get("market_price_usd"))
    market_low_usd = maybe_float(payload.get("market_low_usd"))
    market_mid_usd = maybe_float(payload.get("market_mid_usd"))
    market_high_usd = maybe_float(payload.get("market_high_usd"))

    provider = (payload.get("provider") or "Manual").strip()
    card_id = str(payload.get("card_id"))
    variant_id = str(payload.get("variant_id"))
    sku = payload.get("sku") or make_sku("TCG")
    ts = now_iso()
    justtcg_card_uuid = card_id if provider.lower() == "justtcg" else None
    justtcg_variant_uuid = variant_id if provider.lower() == "justtcg" else None

    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO products
            (sku, name, category, subcategory, game, set_name, card_number, rarity,
             condition, printing, language, justtcg_card_uuid, justtcg_variant_uuid,
             provider, external_card_id, external_variant_id, image_url,
             market_price_usd, market_low_usd, market_mid_usd, market_high_usd,
             market_updated_at, cost_mxn, price_mxn, qty, notes, auto_price_enabled, created_at, updated_at)
            VALUES (?, ?, 'TCG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                sku,
                payload["name"],
                payload.get("game"),
                payload.get("game"),
                payload.get("set_name"),
                payload.get("card_number"),
                payload.get("rarity"),
                payload.get("condition"),
                payload.get("printing"),
                payload.get("language") or "English",
                justtcg_card_uuid,
                justtcg_variant_uuid,
                provider,
                card_id,
                variant_id,
                payload.get("image_url"),
                market_price_usd,
                market_low_usd,
                market_mid_usd,
                market_high_usd,
                payload.get("market_updated_at"),
                cost_mxn,
                price_mxn,
                qty,
                payload.get("notes"),
                ts,
                ts,
            ),
        )
        pid = cur.lastrowid
        if qty:
            record_inventory_movement(conn,pid,"stock_in",0,qty,unit_cost_mxn=cost_mxn,reason="Importación TCG",source_type="tcg_import",employee_id=current_user_id())
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Ese SKU ya está en uso."}), 409
    conn.close()
    return jsonify({"ok": True, "sku": sku})




@app.route('/clientes')
def customers():
    conn=get_db()
    rows=conn.execute("""SELECT c.*,COALESCE((SELECT SUM(amount_mxn) FROM store_credit_transactions t WHERE t.customer_id=c.id),0) credit_balance,COALESCE((SELECT COUNT(*) FROM sales s WHERE s.customer_id=c.id),0) sales_count FROM customers c WHERE c.active=1 ORDER BY c.name COLLATE NOCASE""").fetchall()
    conn.close(); return render_template('customers.html',customers=rows)

@app.route('/clientes/nuevo',methods=['POST'])
def customer_new():
    name=(request.form.get('name') or '').strip(); phone=(request.form.get('phone') or '').strip() or None; email=(request.form.get('email') or '').strip() or None; notes=(request.form.get('notes') or '').strip() or None
    if not name: flash('El nombre del cliente es obligatorio.','error'); return redirect(url_for('customers'))
    ts=now_iso(); conn=get_db(); cur=conn.execute("INSERT INTO customers(name,phone,email,notes,active,created_at,updated_at) VALUES (?,?,?,?,1,?,?)",(name,phone,email,notes,ts,ts)); conn.commit(); cid=cur.lastrowid; conn.close(); flash('Cliente agregado.','success'); return redirect(url_for('customer_detail',customer_id=cid))

@app.route('/clientes/<int:customer_id>',methods=['GET','POST'])
def customer_detail(customer_id):
    conn=get_db(); customer=conn.execute("SELECT * FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()
    if not customer: conn.close(); abort(404)
    if request.method=='POST':
        name=(request.form.get('name') or '').strip(); phone=(request.form.get('phone') or '').strip() or None; email=(request.form.get('email') or '').strip() or None; notes=(request.form.get('notes') or '').strip() or None
        if not name: conn.close(); flash('El nombre es obligatorio.','error'); return redirect(url_for('customer_detail',customer_id=customer_id))
        conn.execute("UPDATE customers SET name=?,phone=?,email=?,notes=?,updated_at=? WHERE id=?",(name,phone,email,notes,now_iso(),customer_id)); conn.commit(); customer=conn.execute("SELECT * FROM customers WHERE id=?",(customer_id,)).fetchone()
        flash('Cliente actualizado.','success')
    balance=customer_credit_balance(conn,customer_id)
    credits=conn.execute("""SELECT t.*,u.name employee_name FROM store_credit_transactions t LEFT JOIN users u ON u.id=t.employee_id WHERE t.customer_id=? ORDER BY t.id DESC LIMIT 100""",(customer_id,)).fetchall()
    sales_rows=conn.execute("SELECT * FROM sales WHERE customer_id=? ORDER BY id DESC LIMIT 30",(customer_id,)).fetchall()
    trades_rows=conn.execute("SELECT * FROM trades WHERE customer_id=? ORDER BY id DESC LIMIT 30",(customer_id,)).fetchall()
    conn.close(); return render_template('customer_detail.html',customer=customer,balance=balance,credits=credits,sales=sales_rows,trades=trades_rows)

@app.route('/clientes/<int:customer_id>/credito',methods=['POST'])
@role_required('manager')
def customer_credit_adjust(customer_id):
    try: amount=float(request.form.get('amount_mxn',0) or 0)
    except ValueError: amount=0
    direction=(request.form.get('direction') or 'add').strip(); reason=(request.form.get('reason') or '').strip() or 'Ajuste manual'
    amount=abs(amount) * (-1 if direction=='subtract' else 1)
    if abs(amount)<0.01: flash('Escribe una cantidad válida.','error'); return redirect(url_for('customer_detail',customer_id=customer_id))
    conn=get_db(); customer=conn.execute("SELECT id FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()
    if not customer: conn.close(); abort(404)
    if amount<0 and abs(amount)>customer_credit_balance(conn,customer_id)+0.001: conn.close(); flash('El ajuste excede el crédito disponible.','error'); return redirect(url_for('customer_detail',customer_id=customer_id))
    add_store_credit(conn,customer_id,amount,'manual_adjustment',source_type='manual',reason=reason,employee_id=current_user_id()); conn.commit(); conn.close(); flash('Saldo de crédito actualizado.','success'); return redirect(url_for('customer_detail',customer_id=customer_id))

@app.route('/api/customers/<int:customer_id>')
def customer_api(customer_id):
    conn=get_db(); c=conn.execute("SELECT * FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()
    if not c: conn.close(); return jsonify({'error':'Cliente no encontrado.'}),404
    data=dict(c); data['credit_balance']=customer_credit_balance(conn,customer_id); conn.close(); return jsonify(data)

@app.route("/cliente")
def customer_display():
    return render_template("customer_display.html")

@app.route("/api/customer-display", methods=["GET", "POST"])
def customer_display_api():
    if request.method == "GET":
        return jsonify(get_display_state())
    payload = request.get_json(silent=True) or {}
    mode = (payload.get("mode") or "idle").strip().lower()
    if mode not in {"idle","sale","complete","trade","trade_complete"}:
        return jsonify({"error":"Modo de pantalla inválido."}), 400
    try:
        set_display_state(payload)
    except ValueError as exc:
        return jsonify({"error":str(exc)}), 400
    return jsonify({"ok":True})

@app.route("/api/products")
def api_products():
    q=request.args.get('q','').strip(); conn=get_db()
    if q:
        like=f"%{q}%"; rows=conn.execute("SELECT * FROM products WHERE active=1 AND qty>0 AND (name LIKE ? OR sku LIKE ? OR game LIKE ? OR set_name LIKE ?) ORDER BY name COLLATE NOCASE LIMIT 80",(like,like,like,like)).fetchall()
    else: rows=conn.execute("SELECT * FROM products WHERE active=1 AND qty>0 ORDER BY updated_at DESC LIMIT 80").fetchall()
    data=[dict(r) for r in rows]
    if getattr(g,'user',None) and g.user['role']=='cashier':
        for item in data:
            item.pop('cost_mxn',None)
    conn.close(); return jsonify(data)

@app.route("/api/checkout",methods=["POST"])
def checkout():
    payload=request.get_json(silent=True) or {}
    items=payload.get('items') or []
    payment=(payload.get('payment_method') or '').strip().lower()
    discount=max(0.0,float(payload.get('discount_mxn',0) or 0))
    amount_received=payload.get('amount_received_mxn')
    email=(payload.get('customer_email') or '').strip() or None
    phone=(payload.get('customer_phone') or '').strip() or None
    try: customer_id=int(payload.get('customer_id') or 0) or None
    except (TypeError,ValueError): customer_id=None
    requested_credit=max(0.0,float(payload.get('store_credit_mxn',0) or 0))
    if not items: return jsonify({'error':'El carrito está vacío.'}),400
    if payment not in {'cash','card','other'}: return jsonify({'error':'Elige efectivo, tarjeta u otro método.'}),400
    conn=get_db()
    try:
        conn.execute('BEGIN IMMEDIATE'); shift=get_open_shift(conn)
        if not shift: raise ValueError('No hay un turno abierto. Abre caja antes de cobrar.')
        customer=None; available_credit=0.0
        if customer_id:
            customer=conn.execute("SELECT * FROM customers WHERE id=? AND active=1",(customer_id,)).fetchone()
            if not customer: raise ValueError('El cliente seleccionado ya no existe.')
            available_credit=customer_credit_balance(conn,customer_id)
            if not email: email=customer['email']
            if not phone: phone=customer['phone']
        normalized=[]; subtotal=0.0
        for item in items:
            pid=int(item.get('product_id',0)); qty=int(item.get('qty',0))
            if qty<=0: raise ValueError('Cada cantidad debe ser de al menos 1.')
            product=conn.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if not product: raise ValueError('Un producto del carrito ya no existe.')
            if product['qty']<qty: raise ValueError(f"No hay suficiente stock de {product['name']}. Disponible: {product['qty']}.")
            line=round(float(product['price_mxn'])*qty,2); subtotal+=line; normalized.append((product,qty,line))
        subtotal=round(subtotal,2); total=max(0.0,round(subtotal-discount,2))
        credit_used=round(min(requested_credit,available_credit,total),2) if customer_id else 0.0
        remaining=round(total-credit_used,2)
        received=None; change=None; final_payment=payment if remaining>0 else 'credit'
        if remaining>0 and payment=='cash':
            if amount_received is None: raise ValueError('Ingresa el efectivo recibido.')
            received=round(float(amount_received),2)
            if received<remaining: raise ValueError('El efectivo recibido es menor al restante después del crédito.')
            change=round(received-remaining,2)
        number=datetime.now().strftime('V%y%m%d-%H%M%S-')+uuid.uuid4().hex[:4].upper()
        cur=conn.execute("""INSERT INTO sales(sale_number,subtotal_mxn,discount_mxn,total_mxn,payment_method,amount_received_mxn,change_mxn,created_at,shift_id,customer_email,customer_phone,employee_id,status,refunded_mxn,customer_id,store_credit_used_mxn) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'completed',0,?,?)""",(number,subtotal,discount,total,final_payment,received,change,now_iso(),shift['id'],email,phone,current_user_id(),customer_id,credit_used)); sale_id=cur.lastrowid
        if credit_used>0:
            add_store_credit(conn,customer_id,-credit_used,'sale_use',source_type='sale',source_id=sale_id,reason=f'Crédito usado en {number}',employee_id=current_user_id())
        for product,qty,line in normalized:
            conn.execute("INSERT INTO sale_items(sale_id,product_id,sku,name,qty,unit_price_mxn,unit_cost_mxn,line_total_mxn) VALUES (?,?,?,?,?,?,?,?)",(sale_id,product['id'],product['sku'],product['name'],qty,product['price_mxn'],product['cost_mxn'],line))
            before=int(product['qty'] or 0); after=before-qty
            conn.execute("UPDATE products SET qty=?,updated_at=? WHERE id=?",(after,now_iso(),product['id']))
            record_inventory_movement(conn,product['id'],'sale',before,after,unit_cost_mxn=product['cost_mxn'],reason=f"Venta {number}",source_type='sale',source_id=sale_id,employee_id=current_user_id())
        conn.commit(); receipt=build_receipt_text(conn,sale_id)
        display_items=[{"name":p[0]["name"],"sku":p[0]["sku"],"qty":p[1],"price":float(p[0]["price_mxn"] or 0),"image":p[0]["image_url"] or ""} for p in normalized]
        set_display_state({"mode":"complete","sale_number":number,"items":display_items,"subtotal":subtotal,"discount":discount,"total":total,"payment":final_payment,"received":received,"change":change,"credit_used":credit_used,"remaining":remaining}, conn)
        conn.commit()
        return jsonify({'ok':True,'sale_id':sale_id,'sale_number':number,'subtotal_mxn':subtotal,'discount_mxn':discount,'total_mxn':total,'store_credit_used_mxn':credit_used,'remaining_mxn':remaining,'change_mxn':change,'receipt_text':receipt,'receipt_url':url_for('receipt_print',sale_id=sale_id)})
    except (ValueError,TypeError) as exc: conn.rollback(); return jsonify({'error':str(exc)}),400
    except Exception: conn.rollback(); app.logger.exception('Checkout failed'); return jsonify({'error':'No se pudo completar la venta.'}),500
    finally: conn.close()

def build_receipt_text(conn,sale_id):
    sale=conn.execute("""SELECT s.*,u.name employee_name,c.name customer_name FROM sales s LEFT JOIN users u ON u.id=s.employee_id LEFT JOIN customers c ON c.id=s.customer_id WHERE s.id=?""",(sale_id,)).fetchone()
    if not sale: return ''
    items=conn.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id",(sale_id,)).fetchall()
    pay={'cash':'Efectivo','card':'Tarjeta','other':'Otro','credit':'Crédito de tienda'}.get(sale['payment_method'],sale['payment_method'])
    settings=get_store_settings(conn)
    lines=[settings['store_name'],f"Recibo {sale['sale_number']}",f"Atendió: {sale['employee_name'] or '—'}"]
    if sale['customer_name']: lines.append(f"Cliente: {sale['customer_name']}")
    lines += ['']+[f"{i['qty']} x {i['name']}  ${money(i['line_total_mxn'])}" for i in items]+['',f"Subtotal: ${money(sale['subtotal_mxn'])}",f"Descuento: -${money(sale['discount_mxn'])}",f"TOTAL: ${money(sale['total_mxn'])}"]
    credit=float(sale['store_credit_used_mxn'] or 0)
    if credit>0:
        lines.append(f"Crédito de tienda: -${money(credit)}")
        lines.append(f"Restante pagado: ${money(max(0,float(sale['total_mxn'])-credit))}")
    lines.append(f"Pago restante: {pay}")
    if sale['payment_method']=='cash': lines += [f"Recibido: ${money(sale['amount_received_mxn'])}",f"Cambio: ${money(sale['change_mxn'])}"]
    return '\n'.join(lines+['',settings.get('receipt_footer') or '¡Gracias por tu compra!'])

@app.route('/sales')
def sales():
    conn=get_db(); rows=conn.execute("""SELECT s.*,u.name employee_name,COUNT(si.id) line_count,COALESCE(SUM(si.qty),0) item_count,COALESCE(SUM((si.unit_price_mxn-si.unit_cost_mxn)*si.qty),0)-s.discount_mxn-COALESCE((SELECT SUM(ri.refund_total_mxn-(ri.unit_cost_mxn*ri.qty)) FROM refund_items ri JOIN refunds r ON r.id=ri.refund_id WHERE r.sale_id=s.id),0) gross_profit_mxn FROM sales s LEFT JOIN sale_items si ON si.sale_id=s.id LEFT JOIN users u ON u.id=s.employee_id GROUP BY s.id ORDER BY s.id DESC LIMIT 200""").fetchall(); conn.close(); return render_template('sales.html',sales=rows)

@app.route('/sales/<int:sale_id>')
def sale_detail(sale_id):
    conn=get_db(); sale=conn.execute("SELECT s.*,u.name employee_name FROM sales s LEFT JOIN users u ON u.id=s.employee_id WHERE s.id=?",(sale_id,)).fetchone()
    if not sale: conn.close(); return 'No encontrado',404
    items=returnable_sale_items(conn,sale_id)
    refunds=conn.execute("SELECT r.*,u.name employee_name FROM refunds r LEFT JOIN users u ON u.id=r.employee_id WHERE r.sale_id=? ORDER BY r.id DESC",(sale_id,)).fetchall()
    conn.close(); return render_template('sale_detail.html',sale=sale,items=items,refunds=refunds)

@app.route('/sales/<int:sale_id>/return',methods=['GET','POST'])
@role_required('manager')
def sale_return(sale_id):
    conn=get_db(); sale=conn.execute("SELECT * FROM sales WHERE id=?",(sale_id,)).fetchone()
    if not sale: conn.close(); abort(404)
    if sale['status'] in {'cancelled','refunded'}:
        conn.close(); flash('Esta venta ya no tiene artículos disponibles para devolución.','error'); return redirect(url_for('sale_detail',sale_id=sale_id))
    items=returnable_sale_items(conn,sale_id)
    if request.method=='POST':
        selections=[]
        for item in items:
            try: qty=int(request.form.get(f"qty_{item['id']}",0) or 0)
            except ValueError: qty=0
            if qty>0: selections.append({'sale_item_id':item['id'],'qty':qty,'restock':request.form.get(f"restock_{item['id']}")=='on'})
        method=(request.form.get('refund_method') or ('other' if sale['payment_method']=='credit' else sale['payment_method'])).strip(); reason=(request.form.get('reason') or '').strip() or 'Devolución de cliente'
        try:
            conn.execute('BEGIN IMMEDIATE'); result=process_refund(conn,sale,selections,method,reason); conn.commit(); flash(f"Devolución {result['refund_number']} registrada por ${money(result['total_mxn'])}.",'success'); conn.close(); return redirect(url_for('sale_detail',sale_id=sale_id))
        except (ValueError,TypeError) as exc:
            conn.rollback(); conn.close(); flash(str(exc),'error'); return redirect(url_for('sale_return',sale_id=sale_id))
    conn.close(); return render_template('return_sale.html',sale=sale,items=items)

@app.route('/sales/<int:sale_id>/cancel',methods=['POST'])
@role_required('manager')
def sale_cancel(sale_id):
    conn=get_db(); sale=conn.execute("SELECT * FROM sales WHERE id=?",(sale_id,)).fetchone()
    if not sale: conn.close(); abort(404)
    if sale['status']!='completed' or float(sale['refunded_mxn'] or 0)>0:
        conn.close(); flash('Solo puedes cancelar una venta completa que todavía no tenga devoluciones.','error'); return redirect(url_for('sale_detail',sale_id=sale_id))
    items=returnable_sale_items(conn,sale_id); selections=[{'sale_item_id':i['id'],'qty':int(i['returnable_qty'] or 0),'restock':True} for i in items if int(i['returnable_qty'] or 0)>0]
    reason=(request.form.get('reason') or '').strip() or 'Venta cancelada'
    try:
        conn.execute('BEGIN IMMEDIATE'); result=process_refund(conn,sale,selections,('other' if sale['payment_method']=='credit' else sale['payment_method']),reason,cancel_sale=True); conn.commit(); flash(f"Venta cancelada. Reembolso {result['refund_number']} por ${money(result['total_mxn'])}.",'success')
    except (ValueError,TypeError) as exc: conn.rollback(); flash(str(exc),'error')
    finally: conn.close()
    return redirect(url_for('sale_detail',sale_id=sale_id))

@app.route('/sales/<int:sale_id>/receipt')
def receipt_print(sale_id):
    conn=get_db(); sale=conn.execute("SELECT s.*,u.name employee_name FROM sales s LEFT JOIN users u ON u.id=s.employee_id WHERE s.id=?",(sale_id,)).fetchone(); items=conn.execute("SELECT * FROM sale_items WHERE sale_id=? ORDER BY id",(sale_id,)).fetchall(); conn.close(); return render_template('receipt.html',sale=sale,items=items) if sale else ('No encontrado',404)
@app.route('/api/sales/<int:sale_id>/receipt')
def receipt_api(sale_id):
    conn=get_db(); text=build_receipt_text(conn,sale_id); conn.close(); return jsonify({'text':text}) if text else (jsonify({'error':'Venta no encontrada.'}),404)
def smtp_configured(): return bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))
@app.route('/api/sales/<int:sale_id>/email',methods=['POST'])
def send_receipt_email(sale_id):
    payload=request.get_json(silent=True) or {}; to=(payload.get('email') or '').strip()
    if not to or '@' not in to: return jsonify({'error':'Ingresa un correo válido.'}),400
    if not smtp_configured(): return jsonify({'error':'El envío directo por correo todavía no está configurado en .env.','fallback':True}),503
    conn=get_db(); text=build_receipt_text(conn,sale_id); sale=conn.execute("SELECT * FROM sales WHERE id=?",(sale_id,)).fetchone(); conn.close()
    if not sale: return jsonify({'error':'Venta no encontrada.'}),404
    settings=get_store_settings(); msg=EmailMessage(); msg['Subject']=f"Tu recibo {settings['store_name']} · {sale['sale_number']}"; msg['From']=os.environ['SMTP_FROM']; msg['To']=to; msg.set_content(text)
    try:
        with smtplib.SMTP(os.environ['SMTP_HOST'],int(os.environ.get('SMTP_PORT','587')),timeout=15) as server:
            if os.environ.get('SMTP_USE_TLS','1').lower() not in {'0','false','no'}: server.starttls()
            if os.environ.get('SMTP_USERNAME'): server.login(os.environ['SMTP_USERNAME'],os.environ.get('SMTP_PASSWORD',''))
            server.send_message(msg)
    except Exception as exc: app.logger.exception('Email failed'); return jsonify({'error':f'No se pudo enviar el correo: {exc}'}),502
    conn=get_db(); conn.execute("UPDATE sales SET customer_email=? WHERE id=?",(to,sale_id)); conn.commit(); conn.close(); return jsonify({'ok':True})

@app.route('/shifts')
def shifts():
    conn=get_db(); current=get_open_shift(conn); recent=conn.execute("SELECT s.*,uo.name opened_by_name,uc.name closed_by_name FROM shifts s LEFT JOIN users uo ON uo.id=s.opened_by_employee_id LEFT JOIN users uc ON uc.id=s.closed_by_employee_id ORDER BY s.id DESC LIMIT 40").fetchall(); stats=shift_stats(conn,current['id']) if current else None; movements=conn.execute("SELECT cm.*,u.name employee_name FROM cash_movements cm LEFT JOIN users u ON u.id=cm.employee_id WHERE cm.shift_id=? ORDER BY cm.id DESC",(current['id'],)).fetchall() if current else []; conn.close(); return render_template('shifts.html',current=current,current_stats=stats,recent=recent,movements=movements)
@app.route('/shifts/open',methods=['POST'])
def shift_open():
    opening=max(0.0,float(request.form.get('opening_cash_mxn',0) or 0)); event=1 if request.form.get('event_mode') else 0; name=(request.form.get('event_name') or '').strip() or None; notes=(request.form.get('notes_open') or '').strip() or None
    if event and not name: flash('Escribe el nombre del evento.','error'); return redirect(url_for('shifts'))
    conn=get_db()
    if get_open_shift(conn): conn.close(); flash('Ya hay un turno abierto.','error'); return redirect(url_for('shifts'))
    conn.execute("INSERT INTO shifts(opened_at,opening_cash_mxn,status,event_mode,event_name,notes_open,opened_by_employee_id) VALUES (?,?,'open',?,?,?,?)",(now_iso(),opening,event,name,notes,current_user_id())); conn.commit(); conn.close(); flash('Turno abierto. La caja está lista.','success'); return redirect(url_for('pos'))
@app.route('/shifts/movement',methods=['POST'])
def shift_movement():
    kind=request.form.get('kind'); amount=float(request.form.get('amount_mxn',0) or 0); reason=(request.form.get('reason') or '').strip() or None
    if kind not in {'pay_in','pay_out'} or amount<=0: flash('Movimiento de caja inválido.','error'); return redirect(url_for('shifts'))
    conn=get_db(); shift=get_open_shift(conn)
    if not shift: conn.close(); flash('No hay un turno abierto.','error'); return redirect(url_for('shifts'))
    conn.execute("INSERT INTO cash_movements(shift_id,kind,amount_mxn,reason,created_at,employee_id) VALUES (?,?,?,?,?,?)",(shift['id'],kind,amount,reason,now_iso(),current_user_id())); conn.commit(); conn.close(); flash('Movimiento de caja registrado.','success'); return redirect(url_for('shifts'))
@app.route('/shifts/close',methods=['POST'])
def shift_close():
    try: counted=float(request.form.get('closing_cash_mxn','') or '')
    except ValueError: flash('Ingresa el efectivo contado.','error'); return redirect(url_for('shifts'))
    notes=(request.form.get('notes_close') or '').strip() or None; conn=get_db(); shift=get_open_shift(conn)
    if not shift: conn.close(); flash('No hay un turno abierto.','error'); return redirect(url_for('shifts'))
    expected=shift_expected_cash(conn,shift['id']); diff=round(counted-expected,2); conn.execute("UPDATE shifts SET status='closed',closed_at=?,closing_cash_mxn=?,expected_cash_mxn=?,difference_mxn=?,notes_close=?,closed_by_employee_id=? WHERE id=?",(now_iso(),counted,expected,diff,notes,current_user_id(),shift['id'])); conn.commit(); conn.close(); flash(f"Turno cerrado. Diferencia de caja: ${money(diff)} MXN.",'success'); return redirect(url_for('dashboard'))

@app.route('/trades')
@role_required('manager')
def trades():
    conn=get_db(); recent=conn.execute("SELECT t.*,u.name employee_name,c.name customer_display_name FROM trades t LEFT JOIN users u ON u.id=t.employee_id LEFT JOIN customers c ON c.id=t.customer_id ORDER BY t.id DESC LIMIT 50").fetchall(); shift=get_open_shift(conn); customers=conn.execute("SELECT c.*,COALESCE((SELECT SUM(amount_mxn) FROM store_credit_transactions x WHERE x.customer_id=c.id),0) credit_balance FROM customers c WHERE c.active=1 ORDER BY c.name COLLATE NOCASE").fetchall(); conn.close(); return render_template('trades.html',recent=recent,shift=shift,customers=customers)
@app.route('/api/trades',methods=['POST'])
@role_required('manager')
def create_trade():
    payload=request.get_json(silent=True) or {}; items=payload.get('items') or []; payout=(payload.get('payout_type') or '').strip().lower()
    if payout not in {'credit','cash'}: return jsonify({'error':'Elige crédito en producto o efectivo.'}),400
    if not items: return jsonify({'error':'Agrega al menos un producto.'}),400
    settings=get_store_settings(); rate=float(settings['trade_credit_rate']) if payout=='credit' else float(settings['trade_cash_rate']); customer=(payload.get('customer_name') or '').strip() or None; notes=(payload.get('notes') or '').strip() or None;
    try: customer_id=int(payload.get('customer_id') or 0) or None
    except (TypeError,ValueError): customer_id=None
    conn=get_db()
    try:
        conn.execute('BEGIN IMMEDIATE'); shift=get_open_shift(conn)
        if not shift: raise ValueError('Abre un turno antes de registrar intercambios.')
        selected_customer=conn.execute('SELECT * FROM customers WHERE id=? AND active=1',(customer_id,)).fetchone() if customer_id else None
        if payout=='credit' and not selected_customer: raise ValueError('Para dar crédito en producto debes seleccionar un cliente.')
        if selected_customer: customer=selected_customer['name']
        normalized=[]; market_total=0.0; api_linked_count=0
        for raw in items:
            name=(raw.get('name') or '').strip(); category=(raw.get('category') or 'Other').strip() or 'Other'; sku=(raw.get('sku') or '').strip() or None; qty=max(1,int(raw.get('qty',1) or 1)); market=max(0.0,float(raw.get('market_value_mxn',0) or 0)); sell=max(0.0,float(raw.get('sell_price_mxn',market) or market))
            if not name or market<=0: raise ValueError('Cada producto necesita nombre y valor de mercado mayor a $0.')
            api = {
                'provider': (raw.get('provider') or '').strip() or None,
                'card_id': str(raw.get('card_id') or '').strip() or None,
                'variant_id': str(raw.get('variant_id') or '').strip() or None,
                'game': (raw.get('game') or '').strip() or None,
                'set_name': (raw.get('set_name') or '').strip() or None,
                'card_number': (raw.get('card_number') or '').strip() or None,
                'rarity': (raw.get('rarity') or '').strip() or None,
                'condition': (raw.get('condition') or '').strip() or None,
                'printing': (raw.get('printing') or '').strip() or None,
                'language': (raw.get('language') or '').strip() or None,
                'image_url': (raw.get('image_url') or '').strip() or None,
                'market_price_usd': raw.get('market_price_usd'),
                'market_low_usd': raw.get('market_low_usd'),
                'market_mid_usd': raw.get('market_mid_usd'),
                'market_high_usd': raw.get('market_high_usd'),
                'market_updated_at': raw.get('market_updated_at'),
            }
            if api['provider'] and api['card_id'] and api['variant_id'] and category=='TCG': api_linked_count += 1
            offer=round(market*rate,2); market_total+=market*qty; normalized.append((name,category,sku,qty,market,sell,offer,api))
        market_total=round(market_total,2); offer_total=round(market_total*rate,2); number=datetime.now().strftime('I%y%m%d-%H%M%S-')+uuid.uuid4().hex[:4].upper(); cur=conn.execute("INSERT INTO trades(trade_number,shift_id,payout_type,market_total_mxn,offer_total_mxn,rate,customer_name,notes,created_at,employee_id,customer_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",(number,shift['id'],payout,market_total,offer_total,rate,customer,notes,now_iso(),current_user_id(),customer_id)); tid=cur.lastrowid
        for name,category,sku,qty,market,sell,offer,api in normalized:
            product=None
            if sku:
                product=conn.execute("SELECT * FROM products WHERE sku=? AND active=1",(sku,)).fetchone()
            if product is None and category=='TCG' and api['variant_id']:
                # Same linked card/variant + condition should increase existing stock instead of creating duplicates.
                if str(api['provider'] or '').lower()=='tcgdex':
                    product=conn.execute("SELECT * FROM products WHERE active=1 AND category='TCG' AND provider=? AND external_variant_id=? AND COALESCE(condition,'')=? LIMIT 1",(api['provider'],api['variant_id'],api['condition'] or '')).fetchone()
                else:
                    product=conn.execute("SELECT * FROM products WHERE active=1 AND category='TCG' AND provider=? AND external_variant_id=? LIMIT 1",(api['provider'],api['variant_id'])).fetchone()
            just_card = api['card_id'] if str(api['provider'] or '').lower()=='justtcg' else None
            just_variant = api['variant_id'] if str(api['provider'] or '').lower()=='justtcg' else None
            if product:
                old=int(product['qty']); new=old+qty; weighted=((float(product['cost_mxn'] or 0)*old)+(offer*qty))/new
                if category=='TCG' and api['card_id']:
                    conn.execute("""UPDATE products SET qty=?,cost_mxn=?,price_mxn=?,name=?,game=?,set_name=?,card_number=?,rarity=?,condition=?,printing=?,language=?,provider=?,external_card_id=?,external_variant_id=?,image_url=COALESCE(?,image_url),market_price_usd=?,market_low_usd=?,market_mid_usd=?,market_high_usd=?,market_updated_at=?,auto_price_enabled=1,justtcg_card_uuid=COALESCE(?,justtcg_card_uuid),justtcg_variant_uuid=COALESCE(?,justtcg_variant_uuid),updated_at=? WHERE id=?""",
                                 (new,round(weighted,2),sell,name,api['game'],api['set_name'],api['card_number'],api['rarity'],api['condition'],api['printing'],api['language'] or 'English',api['provider'],api['card_id'],api['variant_id'],api['image_url'],api['market_price_usd'],api['market_low_usd'],api['market_mid_usd'],api['market_high_usd'],api['market_updated_at'],just_card,just_variant,now_iso(),product['id']))
                else:
                    conn.execute("UPDATE products SET qty=?,cost_mxn=?,price_mxn=?,updated_at=? WHERE id=?",(new,round(weighted,2),sell,now_iso(),product['id']))
                pid=product['id']; final_sku=product['sku']; before=old
            else:
                final_sku=sku or make_sku('TCG' if category=='TCG' else 'TRD'); ts=now_iso()
                try:
                    pc=conn.execute("""INSERT INTO products(sku,name,category,subcategory,game,set_name,card_number,rarity,condition,printing,language,justtcg_card_uuid,justtcg_variant_uuid,provider,external_card_id,external_variant_id,image_url,market_price_usd,market_low_usd,market_mid_usd,market_high_usd,market_updated_at,cost_mxn,price_mxn,qty,notes,auto_price_enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (final_sku,name,category,api['game'] if category=='TCG' else None,api['game'] if category=='TCG' else None,api['set_name'],api['card_number'],api['rarity'],api['condition'],api['printing'],api['language'] or ('English' if category=='TCG' else None),just_card,just_variant,api['provider'],api['card_id'],api['variant_id'],api['image_url'],api['market_price_usd'],api['market_low_usd'],api['market_mid_usd'],api['market_high_usd'],api['market_updated_at'],offer,sell,qty,'Ingresado por intercambio/compra',1 if category=='TCG' and api['card_id'] else 0,ts,ts)); pid=pc.lastrowid; before=0; new=qty
                except sqlite3.IntegrityError: raise ValueError(f'El SKU {final_sku} ya existe.')
            record_inventory_movement(conn,pid,'trade_in',before,new,unit_cost_mxn=offer,reason=f"Intercambio {number}",source_type='trade',source_id=tid,employee_id=current_user_id())
            conn.execute("INSERT INTO trade_items(trade_id,product_id,sku,name,category,qty,market_value_unit_mxn,sell_price_unit_mxn,offer_unit_mxn) VALUES (?,?,?,?,?,?,?,?,?)",(tid,pid,final_sku,name,category,qty,market,sell,offer))
        if payout=='credit':
            add_store_credit(conn,customer_id,offer_total,'trade_credit',source_type='trade',source_id=tid,reason=f'Crédito por intercambio {number}',employee_id=current_user_id())
        conn.commit(); set_display_state({'mode':'trade_complete','trade_number':number,'market_total':market_total,'offer_total':offer_total,'rate':rate,'payout_type':payout}, conn); conn.commit(); return jsonify({'ok':True,'trade_number':number,'market_total_mxn':market_total,'offer_total_mxn':offer_total,'rate':rate,'api_linked_count':api_linked_count})
    except (ValueError,TypeError) as exc: conn.rollback(); return jsonify({'error':str(exc)}),400
    except Exception: conn.rollback(); app.logger.exception('Trade failed'); return jsonify({'error':'No se pudo registrar el intercambio.'}),500
    finally: conn.close()


@app.route('/herramientas')
@role_required('admin')
def tools_support():
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    conn=get_db(); integrity=conn.execute('PRAGMA integrity_check').fetchone()[0]
    counts={t:conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ['products','sales','customers','trades','shifts']}
    settings=get_store_settings(conn); last_price=conn.execute('SELECT * FROM price_refresh_runs ORDER BY id DESC LIMIT 1').fetchone(); conn.close()
    backups=sorted(BACKUP_DIR.glob('collector-pos-backup-*.zip'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]
    total,used,free=shutil.disk_usage(DATA_DIR)
    justtcg=bool(os.environ.get('JUSTTCG_API_KEY'))
    return render_template('tools.html',integrity=integrity,counts=counts,backups=backups,disk_free=free,disk_total=total,justtcg=justtcg,last_price=last_price,settings=settings)

@app.route('/herramientas/respaldo',methods=['POST'])
@role_required('admin')
def backup_create():
    try: path=create_backup_archive('manual'); flash(f'Respaldo creado: {path.name}','success')
    except Exception as exc: app.logger.exception('Backup failed'); flash(f'No se pudo crear el respaldo: {exc}','error')
    return redirect(url_for('tools_support'))

@app.route('/herramientas/respaldo/<path:filename>')
@role_required('admin')
def backup_download(filename):
    safe=Path(filename).name; path=BACKUP_DIR/safe
    if not path.exists() or not safe.endswith('.zip'): abort(404)
    return send_file(path,as_attachment=True,download_name=safe)

@app.route('/herramientas/restaurar',methods=['POST'])
@role_required('admin')
def backup_restore():
    f=request.files.get('backup')
    if not f or not f.filename: flash('Selecciona un archivo ZIP de respaldo.','error'); return redirect(url_for('tools_support'))
    try:
        create_backup_archive('pre_restore'); restore_backup_archive(f); init_db(); flash('Respaldo restaurado. Vuelve a iniciar sesión si tu usuario cambió.','success')
    except Exception as exc: app.logger.exception('Restore failed'); flash(f'No se pudo restaurar: {exc}','error')
    return redirect(url_for('tools_support'))

@app.route('/exportar/<kind>')
@role_required('admin')
def export_csv(kind):
    conn=get_db(); stamp=datetime.now().strftime('%Y%m%d')
    if kind=='inventario':
        rows=conn.execute("SELECT sku,name,category,game,set_name,card_number,condition,printing,qty,cost_mxn,price_mxn,market_price_usd FROM products WHERE active=1 ORDER BY name").fetchall(); headers=['SKU','Producto','Categoría','Juego','Set','Número','Condición','Impresión','Stock','Costo MXN','Precio MXN','Mercado USD']; data=[list(r) for r in rows]
    elif kind=='ventas':
        rows=conn.execute("""SELECT s.sale_number,s.created_at,c.name customer,u.name employee,s.subtotal_mxn,s.discount_mxn,s.total_mxn,s.store_credit_used_mxn,s.payment_method,s.status,s.refunded_mxn FROM sales s LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON u.id=s.employee_id ORDER BY s.id""").fetchall(); headers=['Folio','Fecha','Cliente','Empleado','Subtotal','Descuento','Total','Crédito usado','Pago','Estado','Devuelto']; data=[list(r) for r in rows]
    elif kind=='clientes':
        rows=conn.execute("""SELECT c.name,c.phone,c.email,c.notes,COALESCE((SELECT SUM(amount_mxn) FROM store_credit_transactions t WHERE t.customer_id=c.id),0) balance FROM customers c WHERE c.active=1 ORDER BY c.name""").fetchall(); headers=['Nombre','WhatsApp','Correo','Notas','Crédito MXN']; data=[list(r) for r in rows]
    elif kind=='turnos':
        rows=conn.execute("SELECT id,opened_at,closed_at,opening_cash_mxn,expected_cash_mxn,closing_cash_mxn,difference_mxn,event_mode,event_name,status FROM shifts ORDER BY id").fetchall(); headers=['Turno','Apertura','Cierre','Fondo inicial','Esperado','Contado','Diferencia','Evento','Nombre evento','Estado']; data=[list(r) for r in rows]
    elif kind=='intercambios':
        rows=conn.execute("""SELECT t.trade_number,t.created_at,c.name customer,t.payout_type,t.market_total_mxn,t.offer_total_mxn,t.rate,t.notes FROM trades t LEFT JOIN customers c ON c.id=t.customer_id ORDER BY t.id""").fetchall(); headers=['Folio','Fecha','Cliente','Tipo','Mercado','Oferta','Tasa','Notas']; data=[list(r) for r in rows]
    else: conn.close(); abort(404)
    conn.close(); return csv_response(f'{kind}-{stamp}.csv',headers,data)

@app.route('/herramientas/diagnostico')
@role_required('admin')
def support_package():
    conn=get_db(); report={'generated_at':now_iso(),'version':'3.0.0-desktop-preview','python':platform.python_version(),'platform':platform.platform(),'database_integrity':conn.execute('PRAGMA integrity_check').fetchone()[0],'counts':{},'store':{},'price_runs':[]}
    for t in ['products','sales','customers','trades','shifts','inventory_movements','refunds']: report['counts'][t]=conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    settings=get_store_settings(conn); report['store']={'store_name':settings.get('store_name'),'timezone':settings.get('store_timezone'),'license_status':settings.get('license_status'),'last_price_refresh_at':settings.get('last_price_refresh_at'),'last_price_refresh_status':settings.get('last_price_refresh_status'),'justtcg_configured':bool(os.environ.get('JUSTTCG_API_KEY'))}
    report['price_runs']=[dict(r) for r in conn.execute('SELECT * FROM price_refresh_runs ORDER BY id DESC LIMIT 10').fetchall()]; conn.close()
    with tempfile.NamedTemporaryFile(suffix='.zip',delete=False) as tmp: temp=Path(tmp.name)
    with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('diagnostico.json',json.dumps(report,ensure_ascii=False,indent=2,default=str))
        zf.writestr('README.txt','Paquete de diagnóstico de Collector POS. No incluye API keys, PINs ni base de datos completa.\n')
    return send_file(temp,as_attachment=True,download_name=f'collector-pos-diagnostico-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip')

@app.route('/health')
def health(): return jsonify({'ok':True,'version':'3.0.0-desktop-preview'})

init_db()
if __name__=='__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','8080')),debug=False)
