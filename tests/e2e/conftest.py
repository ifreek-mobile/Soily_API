import os
import time
import subprocess
import socket
import requests
import signal
import pytest

E2E_PORT = int(os.getenv("E2E_PORT", "8800"))
BASE_URL = f"http://127.0.0.1:{E2E_PORT}"


def _port_open(p: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", p)) == 0


@pytest.fixture(scope="session")
def e2e_server():
    # 既存サーバ利用モード: E2E_EXTERNAL=1 をセットすると起動せず接続確認のみ
    if os.getenv("E2E_EXTERNAL") == "1":
        if not _port_open(E2E_PORT):
            raise RuntimeError(f"外部サーバ未起動: port {E2E_PORT}")
        yield {"base_url": BASE_URL}
        return

    env = os.environ.copy()
    env.setdefault("OPENAI_API_KEY", "DUMMY")
    cmd = [
        "python", "-m", "uvicorn",
        "app.main:app",
        "--host", "127.0.0.1",
        "--port", str(E2E_PORT),
        "--log-level", "info"
    ]
    proc = subprocess.Popen(cmd, env=env)
    deadline = time.time() + 15
    while time.time() < deadline:
        if _port_open(E2E_PORT):
            try:
                r = requests.get(f"{BASE_URL}/openapi.json", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                pass
        time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("E2E サーバ起動失敗")

    yield {"base_url": BASE_URL}

    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
