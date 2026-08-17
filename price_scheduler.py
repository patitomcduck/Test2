import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pricing_engine import run_price_refresh, get_db

SCHEDULE = [(0,0), (12,0), (17,0)]


def timezone_name():
    try:
        conn = get_db()
        row = conn.execute("SELECT store_timezone FROM store_settings WHERE id=1").fetchone()
        conn.close()
        return (row[0] if row and row[0] else "UTC")
    except Exception:
        return "UTC"


def next_run(now, tz):
    candidates=[]
    for day_offset in (0,1):
        date=(now + timedelta(days=day_offset)).date()
        for hour,minute in SCHEDULE:
            dt=datetime(date.year,date.month,date.day,hour,minute,tzinfo=tz)
            if dt > now:
                candidates.append(dt)
    return min(candidates)


def main():
    print("[price-scheduler] iniciado", flush=True)
    while True:
        try:
            tz=ZoneInfo(timezone_name())
        except Exception:
            tz=ZoneInfo("UTC")
        now=datetime.now(tz)
        target=next_run(now,tz)
        seconds=max(1,(target-now).total_seconds())
        print(f"[price-scheduler] próxima actualización: {target.isoformat()}", flush=True)
        time.sleep(seconds)
        try:
            result=run_price_refresh("scheduled")
            print(f"[price-scheduler] resultado: {result}", flush=True)
        except Exception as exc:
            print(f"[price-scheduler] error: {exc}", flush=True)
            time.sleep(60)

if __name__ == "__main__":
    main()
