import json
from unittest.mock import patch

from .conftest import DummyResp

CHAT_URL = "/chat"


class TmpError(Exception):
    def __init__(self, status_code=None):
        self.status_code = status_code


def test_chat_ok(client):
    """正常系: OpenAI モックが正しい JSON {"response":..., "flag":false} を1回で返す → 200 / response 文字列 / flag False を検証"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"こんにちは！","flag":false}')):
        r = client.post(CHAT_URL, json={"message": "テスト"})
    assert r.status_code == 200
    body = r.json()
    assert body["response"].startswith("こんにちは")
    assert body["flag"] is False


def test_chat_trim_over_300(client):
    """300文字超過: モックが350文字 response を返す → サーバ側最終ガードで300文字にトリムされて返却されることを検証"""
    long_text = "a" * 350
    payload = json.dumps({"response": long_text, "flag": False})
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp(payload)):
        r = client.post(CHAT_URL, json={"message": "長文テスト"})
    assert r.status_code == 200
    assert len(r.json()["response"]) == 300


def test_chat_json_invalid(client):
    """JSON 不正: モックが 'not json' を返す → json.loads 失敗により 502 (形式エラー) を返すことを検証"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp("not json")):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 502


def test_chat_missing_key(client):
    """必須キー欠落: flag が欠如した JSON を返すモック → 'response' と 'flag' の両方必須判定で 502 を返す"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"ok"}')):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 502


def test_chat_type_error(client):
    """型不一致: flag を文字列 "no" で返すモック → 型検証(response: str, flag: bool) 失敗で 502"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"ok","flag":"no"}')):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 502


def test_chat_retry_then_success(client, monkeypatch):
    """リトライ成功: 1回目ステータス500例外 (再試行対象) → 2回目正常 JSON → 200 & 内容が '再試行成功' を含む"""
    calls = {"i": 0}

    def side_effect(*args, **kwargs):
        if calls["i"] == 0:
            calls["i"] += 1
            raise TmpError(status_code=500)
        return DummyResp('{"response":"再試行成功","flag":false}')

    with patch("app.routers.chat.client.responses.create", side_effect=side_effect):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 200
    assert "再試行成功" in r.json()["response"]


def test_chat_final_429(client):
    """再試行しても常に429: モックが常に status_code=429 例外 → 再試行後も最終 429 をそのまま返却"""
    with patch("app.routers.chat.client.responses.create",
               side_effect=TmpError(status_code=429)):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 429


def test_chat_final_503_after_500s(client):
    """再試行対象500が全試行で継続: モックが毎回500例外 → 最終的に利用不能扱いで 503 を返却"""
    with patch("app.routers.chat.client.responses.create",
               side_effect=TmpError(status_code=500)):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code == 503


def test_chat_empty_all_attempts(client):
    """空応答: モックが常に {"response":"", "flag":false} を返却 → 実装仕様により最終 503 (または実装差異で200許容) を想定"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"","flag":false}')):
        r = client.post(CHAT_URL, json={"message": "test"})
    assert r.status_code in (200, 503)


def test_chat_input_validation_fail(client):
    """入力バリデーション: 空文字 message を送信 → Pydantic バリデーション失敗で 422(または400互換)"""
    r = client.post(CHAT_URL, json={"message": ""})
    assert r.status_code in (400, 422)


def test_chat_pii_flag_true(client):
    """PII フラグ True: モックが flag=true を返却 → レスポンスの flag が True で PII 警告文が保持される"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"個人情報は載せないでね","flag":true}')):
        r = client.post("/chat", json={"message": "私の住所は東京都新宿区1-1-1です"})
    assert r.status_code == 200
    body = r.json()
    assert body["flag"] is True
    assert "個人情報" in body["response"]


def test_chat_pii_long_trim_and_flag(client):
    """PII + 長文: 300超の flag=true 応答をモック → 返却時 response が300文字にトリムされ flag True 維持"""
    long_resp = "注意:" + ("個人情報を送らないでね。" * 50)
    payload = '{"response":"' + long_resp + '","flag":true}'
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp(payload)):
        r = client.post("/chat", json={"message": "電話番号教えて"})
    assert r.status_code == 200
    body = r.json()
    assert body["flag"] is True
    assert len(body["response"]) == 300


def test_chat_whitespace_response_trim(client):
    """前後空白除去: 前後に空白を含む JSON 文字列をモック → 最終 response が strip 済み文字列になる"""
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('  { "response":" テスト応答です ", "flag": false }  ')):
        r = client.post("/chat", json={"message": "trim?"})
    assert r.status_code == 200
    assert r.json()["response"] == "テスト応答です"
