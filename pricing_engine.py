import os
import re
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "pos.db"
JUSTTCG_API_URL = "https://api.justtcg.com/v1/cards"
TCGDEX_API_BASE = "https://api.tcgdex.net/v2/en"
FRANKFURTER_RATE_URL = "https://api.frankfurter.dev/v2/rate/USD/MXN"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _text(v):
    return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()


def _number(v):
    value = re.sub(r"[^a-z0-9]", "", str(v or "").lower().replace("#", ""))
    if value.isdigit():
        return str(int(value or "0"))
    return value.lstrip("0") or value


def _price_block(tcgplayer, *keys):
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
    out = []
    for suffix, label, keys, exists in specs:
        block = _price_block(tcgplayer, *keys)
        if not exists and not block:
            continue
        out.append({
            "id": f"{card.get('id')}:{suffix}",
            "printing": label,
            "price": block.get("marketPrice"),
            "low": block.get("lowPrice"),
            "mid": block.get("midPrice"),
            "high": block.get("highPrice"),
            "updated": updated,
        })
    return out


def fetch_fx_rate(fallback):
    try:
        r = requests.get(FRANKFURTER_RATE_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        rate = float(data.get("rate"))
        if rate > 0:
            return rate, data.get("date") or now_iso()[:10], None
    except Exception as exc:
        return float(fallback or 17.0), None, str(exc)
    return float(fallback or 17.0), None, "Respuesta de tipo de cambio inválida"


def round_to_step(value, step):
    step = float(step or 1)
    if step <= 0:
        step = 1
    return round(round(float(value) / step) * step, 2)


def _find_justtcg_fallback(product, api_key):
    if not api_key:
        return None
    query = " ".join(x for x in [product["name"], product["card_number"]] if x).strip()
    params = {"q": query, "game": product["game"] or "Pokemon", "limit": 20}
    r = requests.get(JUSTTCG_API_URL, params=params, headers={"x-api-key": api_key}, timeout=12)
    r.raise_for_status()
    payload = r.json()
    target_name = _text(product["name"])
    target_number = _number(product["card_number"])
    target_set = _text(product["set_name"])
    target_printing = _text(product["printing"])
    target_condition = _text(product["condition"])
    ranked = []
    for card in payload.get("data", []):
        score = 0
        cname, cnumber, cset = _text(card.get("name")), _number(card.get("number")), _text(card.get("set_name") or card.get("set"))
        if cname == target_name: score += 8
        elif target_name and (target_name in cname or cname in target_name): score += 4
        if target_number and cnumber == target_number: score += 10
        if target_set and cset:
            if cset == target_set: score += 6
            elif target_set in cset or cset in target_set: score += 3
        for v in card.get("variants") or []:
            price = v.get("price")
            if price in (None, ""):
                continue
            vs = score
            cond, printing = _text(v.get("condition")), _text(v.get("printing"))
            if target_condition:
                if cond == target_condition: vs += 10
                elif target_condition in cond or cond in target_condition: vs += 4
            elif cond in {"near mint", "nm"}: vs += 6
            if target_printing:
                if printing == target_printing: vs += 8
                elif target_printing in printing or printing in target_printing: vs += 4
            ranked.append((vs, card, v))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    score, card, v = ranked[0]
    if score < (12 if target_number else 8):
        return None
    return {
        "provider": "JustTCG fallback",
        "price": v.get("price"),
        "low": v.get("minPrice7d"),
        "mid": v.get("avgPrice"),
        "high": v.get("maxPrice7d"),
        "updated": v.get("lastUpdated") or v.get("updatedAt"),
    }


def _refresh_tcgdex(product, api_key):
    card_id = product["external_card_id"]
    if not card_id:
        return None
    r = requests.get(f"{TCGDEX_API_BASE}/cards/{card_id}", timeout=12)
    r.raise_for_status()
    card = r.json()
    variants = normalize_tcgdex_variants(card)
    target_id = product["external_variant_id"] or ""
    target_printing = _text(product["printing"])
    selected = None
    for v in variants:
        if target_id and v["id"] == target_id:
            selected = v; break
    if selected is None:
        for v in variants:
            if target_printing and _text(v["printing"]) == target_printing:
                selected = v; break
    if selected is None and variants:
        selected = variants[0]
    if selected and selected.get("price") not in (None, ""):
        return {"provider":"TCGdex/TCGplayer", **selected}
    return _find_justtcg_fallback(product, api_key)


def _refresh_justtcg_batch(products, api_key):
    if not products:
        return {}
    if not api_key:
        return {p["id"]: (None, "JustTCG no está configurado") for p in products}
    out = {}
    # Use 20 per batch so this also works on the Free JustTCG plan.
    for start in range(0, len(products), 20):
        chunk = products[start:start+20]
        body = [{"variantId": p["external_variant_id"]} for p in chunk if p["external_variant_id"]]
        if not body:
            continue
        try:
            r = requests.post(JUSTTCG_API_URL, headers={"x-api-key": api_key, "Content-Type":"application/json"}, json=body, timeout=20)
            r.raise_for_status()
            data = r.json().get("data", [])
            variant_map = {}
            for card in data:
                for v in card.get("variants") or []:
                    for key in (v.get("uuid"), v.get("id")):
                        if key:
                            variant_map[str(key)] = v
            for p in chunk:
                v = variant_map.get(str(p["external_variant_id"]))
                if v:
                    out[p["id"]] = ({
                        "provider":"JustTCG",
                        "price":v.get("price"),
                        "low":v.get("minPrice7d"),
                        "mid":v.get("avgPrice"),
                        "high":v.get("maxPrice7d"),
                        "updated":v.get("lastUpdated") or v.get("updatedAt"),
                    }, None)
                else:
                    out[p["id"]] = (None, "Variante no encontrada en JustTCG")
        except Exception as exc:
            for p in chunk:
                out[p["id"]] = (None, str(exc))
    return out


def run_price_refresh(trigger="scheduled"):
    conn = get_db()
    settings = conn.execute("SELECT * FROM store_settings WHERE id=1").fetchone()
    if not settings:
        conn.close(); return {"ok":False,"error":"Configuración de tienda no encontrada"}
    settings = dict(settings)
    started = now_iso()
    cur = conn.execute("INSERT INTO price_refresh_runs(started_at,trigger,status,products_checked,products_updated,errors) VALUES (?,?,?,0,0,0)", (started, trigger, "running"))
    run_id = cur.lastrowid
    conn.commit()

    api_key = os.environ.get("JUSTTCG_API_KEY", "").strip()
    fallback_fx = float(settings.get("usd_mxn_rate") or 17.0)
    if int(settings.get("auto_fx_enabled") or 0):
        fx_rate, fx_date, fx_error = fetch_fx_rate(fallback_fx)
    else:
        fx_rate, fx_date, fx_error = fallback_fx, None, None
    if fx_rate <= 0: fx_rate = fallback_fx or 17.0
    conn.execute("UPDATE store_settings SET usd_mxn_rate=?,fx_updated_at=?,updated_at=? WHERE id=1", (fx_rate, fx_date or settings.get("fx_updated_at"), now_iso()))
    conn.commit()

    rows = conn.execute("""SELECT * FROM products WHERE active=1 AND category='TCG' AND auto_price_enabled=1 AND external_card_id IS NOT NULL""").fetchall()
    tcgdex = [r for r in rows if str(r["provider"] or "").lower() == "tcgdex"]
    just = [r for r in rows if str(r["provider"] or "").lower() == "justtcg"]
    just_results = _refresh_justtcg_batch(just, api_key)
    checked=updated=errors=0
    multiplier = float(settings.get("auto_price_multiplier") or 1.0)
    round_step = float(settings.get("auto_price_round_to") or 1.0)
    auto_store_price = int(settings.get("auto_price_tcg_enabled") or 0) == 1

    for p in rows:
        checked += 1
        result = None; err = None
        try:
            if str(p["provider"] or "").lower() == "tcgdex":
                result = _refresh_tcgdex(p, api_key)
                if not result: err = "Sin precio de mercado disponible"
            elif str(p["provider"] or "").lower() == "justtcg":
                result, err = just_results.get(p["id"], (None, "Sin resultado"))
            else:
                err = f"Proveedor no soportado: {p['provider']}"
        except Exception as exc:
            err = str(exc)

        if result and result.get("price") not in (None, ""):
            market = float(result["price"])
            old_market = p["market_price_usd"]
            old_price = float(p["price_mxn"] or 0)
            new_price = old_price
            if auto_store_price:
                condition_factor = 1.0
                if str(p["provider"] or "").lower() == "tcgdex":
                    condition_factor = {
                        "near mint": 1.0, "nm": 1.0,
                        "lightly played": 0.90, "lp": 0.90,
                        "moderately played": 0.75, "mp": 0.75,
                        "heavily played": 0.60, "hp": 0.60,
                        "damaged": 0.40, "dmg": 0.40,
                    }.get(_text(p["condition"]), 1.0)
                new_price = round_to_step(market * condition_factor * fx_rate * multiplier, round_step)
            conn.execute("""UPDATE products SET market_price_usd=?,market_low_usd=?,market_mid_usd=?,market_high_usd=?,market_updated_at=?,price_mxn=?,price_last_checked_at=?,price_last_error=NULL,updated_at=? WHERE id=?""",
                         (market,result.get("low"),result.get("mid"),result.get("high"),str(result.get("updated") or now_iso()),new_price,now_iso(),now_iso(),p["id"]))
            changed_market = old_market is None or abs(float(old_market)-market) > 1e-9
            changed_price = abs(old_price-new_price) > 1e-9
            if changed_market or changed_price:
                conn.execute("INSERT INTO product_price_history(product_id,market_price_usd,price_mxn,usd_mxn_rate,source,created_at) VALUES (?,?,?,?,?,?)", (p["id"],market,new_price,fx_rate,result.get("provider"),now_iso()))
                updated += 1
        else:
            errors += 1
            conn.execute("UPDATE products SET price_last_checked_at=?,price_last_error=? WHERE id=?", (now_iso(), (err or "Sin precio")[:300], p["id"]))
        conn.commit()

    status = "ok" if errors == 0 else ("partial" if updated or checked else "error")
    finished = now_iso()
    detail = None
    if fx_error:
        detail = f"Tipo de cambio: se usó respaldo {fx_rate:.4f}. {fx_error}"
    conn.execute("UPDATE price_refresh_runs SET finished_at=?,status=?,products_checked=?,products_updated=?,errors=?,fx_rate=?,details=? WHERE id=?", (finished,status,checked,updated,errors,fx_rate,detail,run_id))
    conn.execute("UPDATE store_settings SET last_price_refresh_at=?,last_price_refresh_status=?,updated_at=? WHERE id=1", (finished,f"{status}: {updated}/{checked} actualizados, {errors} errores",finished))
    conn.commit(); conn.close()
    return {"ok":True,"status":status,"checked":checked,"updated":updated,"errors":errors,"fx_rate":fx_rate,"run_id":run_id}


if __name__ == "__main__":
    print(json.dumps(run_price_refresh("manual-cli"), ensure_ascii=False))
