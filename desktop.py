import argparse
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_VERSION = "3.0.0-desktop-preview"
DEFAULT_PORT = 8765


def app_data_dir():
    if sys.platform.startswith("win"):
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "CollectorPOS"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CollectorPOS"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "CollectorPOS"


def load_runtime_env(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    env_path = data_dir / "collector.env"
    values = {}
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    if not values.get("SECRET_KEY"):
        values["SECRET_KEY"] = secrets.token_hex(32)
    values.setdefault("JUSTTCG_API_KEY", "")
    values.setdefault("SMTP_HOST", "")
    values.setdefault("SMTP_PORT", "587")
    values.setdefault("SMTP_USERNAME", "")
    values.setdefault("SMTP_PASSWORD", "")
    values.setdefault("SMTP_FROM", "")
    values.setdefault("SMTP_USE_TLS", "1")
    env_path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return env_path


def lan_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def configure_runtime():
    data_dir = app_data_dir()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["COLLECTOR_DESKTOP"] = "1"
    os.environ.setdefault("COLLECTOR_PORT", str(DEFAULT_PORT))
    os.environ["COLLECTOR_LAN_IP"] = lan_ip()
    load_runtime_env(data_dir)
    return data_dir


class DesktopApi:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = Path(data_dir)
        self.customer_window = None

    def open_customer_display(self):
        import webview
        if self.customer_window is None:
            self.customer_window = webview.create_window(
                "Pantalla cliente · Collector POS",
                f"http://127.0.0.1:{self.port}/cliente",
                width=1280,
                height=800,
                min_size=(800, 500),
                resizable=True,
            )
        else:
            try:
                self.customer_window.show()
            except Exception:
                self.customer_window = None
                return self.open_customer_display()
        return True

    def open_data_folder(self):
        path = str(self.data_dir)
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f'open "{path}"')
        else:
            os.system(f'xdg-open "{path}" >/dev/null 2>&1 &')
        return True

    def get_runtime_info(self):
        return {
            "version": APP_VERSION,
            "port": self.port,
            "customer_display": f"http://{os.environ.get('COLLECTOR_LAN_IP','127.0.0.1')}:{self.port}/cliente",
            "data_dir": str(self.data_dir),
        }


class LocalServer:
    def __init__(self, flask_app, host, port):
        from werkzeug.serving import make_server
        self.server = make_server(host, port, flask_app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, name="collector-pos-http", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass


def run_task(mode):
    configure_runtime()
    if mode == "prices":
        from pricing_engine import run_price_refresh
        result = run_price_refresh("windows-scheduled-task")
        print(result)
        return 0
    if mode == "backup":
        from backup_scheduler import make_backup
        make_backup()
        return 0
    return 1


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--price-refresh", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--browser", action="store_true")
    args, _ = parser.parse_known_args()

    if args.price_refresh:
        return run_task("prices")
    if args.backup:
        return run_task("backup")

    data_dir = configure_runtime()
    port = int(os.environ.get("COLLECTOR_PORT", str(DEFAULT_PORT)))

    # Import only after DATA_DIR / secrets are configured.
    from app import app

    server = LocalServer(app, "0.0.0.0", port)
    server.start()
    time.sleep(0.25)

    if args.browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.stop()
            return 0

    import webview
    api = DesktopApi(port, data_dir)
    main_window = webview.create_window(
        "Collector POS",
        f"http://127.0.0.1:{port}/",
        js_api=api,
        width=1440,
        height=900,
        min_size=(1024, 700),
        resizable=True,
    )

    def on_closed():
        server.stop()

    main_window.events.closed += on_closed
    webview.start(gui="edgechromium", debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
