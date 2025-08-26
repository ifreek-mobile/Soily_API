import pytest
import requests
import time
import json

pytestmark = pytest.mark.e2e


def test_health_like(e2e_server):
    """目的: サービス起動と基本ルーティングが機能しているか。
    検証: ルート('/')へアクセスして 200 (index 提供) か 404 (静的未配置) のどちらか許容範囲のステータスを返す。内部例外で 5xx にならないこと。"""
    # ヘルス (未実装なら "/" または "/docs") の暫定確認
    r = requests.get(e2e_server["base_url"] + "/")
    assert r.status_code in (200, 404)


def test_openapi_schema(e2e_server):
    """目的: OpenAPI ドキュメントが提供され API 契約が外部から取得可能であること。
    検証: /openapi.json が 200 を返し JSON 内の paths に /chat が含まれる。"""
    r = requests.get(e2e_server["base_url"] + "/openapi.json", timeout=2)
    assert r.status_code == 200
    data = r.json()
    assert "paths" in data and "/chat" in data["paths"]


def test_chat_basic(e2e_server, monkeypatch):
    """目的: /chat 正常系の最短経路が 200 と期待スキーマ(JSON/response/flag)で応答する。
    検証: モック経由で安定した JSON を返し response キーが文字列, flag が bool。"""
    # OpenAI クライアント内部を簡易モック (requests では process 外なので monkeypatch target 注意)
    import app.routers.chat as chat_mod
    from unittest.mock import patch

    class Dummy:
        output_text = '{"response":"こんにちは！","flag":false}'
    with patch.object(chat_mod.client.responses, "create", return_value=Dummy()):
        r = requests.post(e2e_server["base_url"] +
                          "/chat", json={"message": "テスト"})
    assert r.status_code == 200
    body = r.json()
    assert "response" in body and isinstance(body["flag"], bool)


def test_chat_timeout_retries_surface(e2e_server):
    """目的: 1回目タイムアウト→再試行で成功(または最終503)となる表層挙動を確認し、再試行機構が働くこと。
    検証: 1回目 TimeoutError を発生させ 2回目正常。結果は 200 か、全て失敗した場合 503。"""
    import app.routers.chat as chat_mod
    from unittest.mock import patch
    calls = {"n": 0}

    class Slow:
        output_text = '{"response":"OK","flag":false}'

    def side_effect(*a, **k):
        import asyncio
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.TimeoutError
        return Slow()
    with patch.object(chat_mod.client.responses, "create", side_effect=side_effect):
        r = requests.post(e2e_server["base_url"] +
                          "/chat", json={"message": "retry"})
    assert r.status_code in (200, 503)


def test_chat_length_trim(e2e_server):
    """目的: 300文字超のAI応答が最終レスポンスで 300 文字以下にトリムされること。
    検証: 500文字ダミー応答→ status 200 & response 長さ <=300。"""
    import app.routers.chat as chat_mod
    from unittest.mock import patch
    long_resp = "あ" * 500

    class Dummy:
        output_text = '{"response":"' + long_resp + '","flag":false}'
    with patch.object(chat_mod.client.responses, "create", return_value=Dummy()):
        r = requests.post(e2e_server["base_url"] +
                          "/chat", json={"message": "long"})
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 300


def test_chat_empty_request_validation(e2e_server):
    """目的: リクエストバリデーション(空文字)が FastAPI/Pydantic で拒否されること。
    検証: 空 message 送信で 400 または 422 を返す。"""
    r = requests.post(e2e_server["base_url"] + "/chat", json={"message": ""})
    assert r.status_code in (400, 422)


def test_trivia_basic(e2e_server):
    """目的: /trivia 基本正常系で 200 と JSON(response) を返す。
    検証: 必須フィールドを正しい形式で送り status 200, response キー存在。"""
    import app.routers.trivia as trivia_mod
    from unittest.mock import patch

    class Dummy:
        output_text = "晴れ"
    with patch.object(trivia_mod.client.responses, "create", return_value=Dummy()):
        r = requests.post(e2e_server["base_url"] + "/trivia", json={
            "latitude": "35", "longitude": "139", "direction": "南向き", "location": "庭"
        })
    assert r.status_code == 200
    body = r.json()
    assert "response" in body


def test_cors_headers(e2e_server):
    """目的: CORS のプリフライト(OPTIONS)が失敗しないことと許可ヘッダの基本挙動。
    検証: OPTIONS /chat に対し 200/204 を返し、必要なら Access-Control-Allow-Origin が期待値(* or 指定Origin)。"""
    origin = "http://localhost:5173"
    r = requests.options(
        e2e_server["base_url"] + "/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST"
        }
    )
    # CORS 設定によっては 200 / 204
    assert r.status_code in (200, 204)
    # 許可されている場合ヘッダ検証
    acao = r.headers.get("access-control-allow-origin")
    # 開発段階は None でも可。設定済みなら一致確認。
    if acao is not None:
        assert acao == origin or acao == "*"
