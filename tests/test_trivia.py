from unittest.mock import patch
from .conftest import DummyResp

TRIVIA_URL = "/trivia"


def test_trivia_ok(client):
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
    r = client.post(TRIVIA_URL, json={
        "latitude": "999",
        "longitude": "139.0",
        "direction": "南向き",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_validation_location_fail(client):
    r = client.post(TRIVIA_URL, json={
        "latitude": "35",
        "longitude": "139",
        "direction": "南向き",
        "location": "屋上"
    })
    assert r.status_code in (400, 422)


def test_trivia_all_attempts_empty(client):
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("")):
        r = client.post(TRIVIA_URL, json={
            "latitude": "35",
            "longitude": "139",
            "direction": "南向き",
            "location": "ベランダ"
        })
    # 実装では空なら 503
    assert r.status_code == 503


def test_trivia_weather_phase_exception_then_success(client):
    """最初の天気取得が例外→フォールバック→本体生成成功"""
    calls = {"i": 0}

    def side_effect(*args, **kwargs):
        # routers.trivia 内で最初は tools=web_search_preview 付き呼び出し
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
    """緯度経度境界値 -90/90 -180/180 を許容"""
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
    """direction 前後空白除去"""
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
    """空文字（空白のみ）→ バリデーションエラー"""
    r = client.post("/trivia", json={
        "latitude": "35",
        "longitude": "139",
        "direction": "   ",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_invalid_lat_non_numeric(client):
    """緯度が数値化不能 → 422"""
    r = client.post("/trivia", json={
        "latitude": "abc",
        "longitude": "139",
        "direction": "南向き",
        "location": "ベランダ"
    })
    assert r.status_code in (400, 422)


def test_trivia_multiple_attempts_then_trim(client, monkeypatch):
    """
    連続で >20文字応答 → ループ後トリムされるか
    MAX_ATTEMPTS 内で break せず最後に20文字超 → 最終トリム確認
    """
    long_txt = "これは二十文字を確実に超える長い説明テキストです"
    calls = {"i": 0}

    def side_effect(*args, **kwargs):
        # weather 取得呼び出しと生成呼び出しが混在するため output_text として長文返す
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
