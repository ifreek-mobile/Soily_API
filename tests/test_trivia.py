from unittest.mock import patch
from .conftest import DummyResp

TRIVIA_URL = "/trivia"


def test_trivia_ok(client):
    """正常系: 全入力が仕様範囲内 (緯度/経度=数値文字列, direction=南向き, location=ベランダ)。
    モックは20文字以内の短文を返し、サーバはそのまま 200 / response 長 <=20 を返すことを確認。"""
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("東京晴れで甘味増すよ")):
        r = client.post(TRIVIA_URL, json={
            "latitude": "35.0",
            "longitude": "139.0",
            "direction": "南向き",
            "location": "ベランダ"
        })
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 20


def test_trivia_trim_over_20(client):
    """出力トリム: モックが 20 文字を大幅に超える長文を返すケース。
    生成ループ後サーバ最終処理で 20 文字以内へトリムされ 200 を返すことを検証。"""
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("これは二十文字を大幅に超える長い説明テキストです")):
        r = client.post(TRIVIA_URL, json={
            "latitude": "35.0",
            "longitude": "139.0",
            "direction": "南向き",
            "location": "ベランダ"
        })
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 20


def test_trivia_validation_latitude_fail(client):
    """緯度バリデーション: latitude=999 (範囲外) を送信し -90〜90 の制約違反で 422/400 が返ることを確認。"""
    r = client.post(TRIVIA_URL, json={
        "latitude": "999",
        "longitude": "139.0",
        "direction": "南向き",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_validation_location_fail(client):
    """location 値不正: 許容値(ベランダ/庭) 以外の '屋上' を指定 → バリデーションエラー 422/400。"""
    r = client.post(TRIVIA_URL, json={
        "latitude": "35",
        "longitude": "139",
        "direction": "南向き",
        "location": "屋上"
    })
    assert r.status_code in (400, 422)


def test_trivia_all_attempts_empty(client):
    """全試行空応答: モックが常に空文字を返し、生成ループで有効文が得られず最終的に 503 を返すことを確認。"""
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("")):
        r = client.post(TRIVIA_URL, json={
            "latitude": "35",
            "longitude": "139",
            "direction": "南向き",
            "location": "ベランダ"
        })
    assert r.status_code == 503


def test_trivia_weather_phase_exception_then_success(client):
    """天気フェーズ例外フォールバック: 1回目(weather取得)で例外 → 例外を握り潰し本体生成を続行し 200 を返す。"""
    calls = {"i": 0}

    def side_effect(*args, **kwargs):
        if calls["i"] == 0:
            calls["i"] += 1
            raise RuntimeError("weather error")
        return DummyResp("東京晴れで甘味増すよ")

    with patch("app.routers.trivia.client.responses.create", side_effect=side_effect):
        r = client.post(TRIVIA_URL, json={
            "latitude": "35",
            "longitude": "139",
            "direction": "南向き",
            "location": "ベランダ"
        })
    assert r.status_code == 200


def test_trivia_boundary_lat_lon(client):
    """境界値: 最小/最大許容値 latitude=-90, longitude=180 を入力し 200 かつ応答長 <=20 を確認。"""
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("南庭今日は涼しいよ")):
        r = client.post("/trivia", json={
            "latitude": "-90",
            "longitude": "180",
            "direction": "南向き",
            "location": "庭"
        })
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 20


def test_trivia_direction_trim(client):
    """direction 前後空白除去: '  北向き  ' を送信し strip 後バリデーション成功し 200 を返すことを確認。"""
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("北ベランダ昼は乾きやすいよ")):
        r = client.post("/trivia", json={
            "latitude": "35",
            "longitude": "139",
            "direction": "  北向き  ",
            "location": "ベランダ"
        })
    assert r.status_code == 200


def test_trivia_invalid_direction_empty(client):
    """direction 空白のみ: '   ' → strip 後空文字となり min_length などで 422/400 エラーになることを確認。"""
    r = client.post("/trivia", json={
        "latitude": "35",
        "longitude": "139",
        "direction": "   ",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_invalid_lat_non_numeric(client):
    """緯度数値化失敗: latitude='abc' → float 変換不能でカスタム validator がエラーを投げ 422/400 を返す。"""
    r = client.post("/trivia", json={
        "latitude": "abc",
        "longitude": "139",
        "direction": "南向き",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_multiple_attempts_then_trim(client, monkeypatch):
    """複数回長文: 各試行で常に >20 文字の長文を返すモック。
    ループ後、最終応答がサーバ側で強制トリムされ <=20 文字になり 200 で返ることを確認。"""
    long_txt = "これは二十文字を確実に超える長い説明テキストです"

    def side_effect(*args, **kwargs):
        return DummyResp(long_txt)

    with patch("app.routers.trivia.client.responses.create", side_effect=side_effect):
        r = client.post("/trivia", json={
            "latitude": "35",
            "longitude": "139",
            "direction": "南向き",
            "location": "ベランダ"
        })
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 20
