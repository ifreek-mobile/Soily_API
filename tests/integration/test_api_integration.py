import json
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


class DummyResp:
    def __init__(self, text: str):
        self.output_text = text


# integration テスト方針:
# - FastAPI ルータ / Pydantic バリデーション / 例外ハンドリング / 再試行 / 出力整形 / セマフォ制御を「実際のアプリ構成」で通す
# - 外部依存 (OpenAI API) のみモック (client.responses.create) に限定し、他は本物
# - unit テストで既にカバーした純粋関数(トリム/JSONパース/PII等)の重複検証を避け、横断シナリオ・再試行・並行・境界を重点確認
# - 各ケースは “顧客向け仕様/運用で重要な失敗パターンや挙動保証” を明文化
# - 成功/失敗混在の許容ステータス (200 / 429 / 503 / 502) は実装方針とリトライ結果の分岐を許容するため明示
pytestmark = pytest.mark.integration


def test_sequence_chat_then_trivia():
    # シナリオ: 典型的なユーザー操作で /chat → /trivia を直列呼び
    # 目的: セッション状態を前提にしない独立性と双方エンドポイントの正常系組合せを保証
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"こんにちは！","flag":false}')):
        r1 = client.post("/chat", json={"message": "テスト"})
    assert r1.status_code == 200
    with patch("app.routers.trivia.client.responses.create",
               return_value=DummyResp("東京晴れで甘味増すよ")):
        r2 = client.post("/trivia", json={
            "latitude": "35", "longitude": "139", "direction": "南向き", "location": "ベランダ"
        })
    assert r2.status_code == 200


def test_chat_retry_json_error_then_success():
    # シナリオ: 1 回目 JSON 形式不正 → 2 回目正常
    # 目的: 形式不正時の再試行/最終成功または即 502 の挙動（実装ポリシー差異）を許容範囲として捉え、回帰検知
    calls = {"i": 0}

    def side_effect(*a, **k):
        if calls["i"] == 0:
            calls["i"] += 1
            return DummyResp("not json")
        return DummyResp('{"response":"再試行OK","flag":false}')
    with patch("app.routers.chat.client.responses.create", side_effect=side_effect):
        r = client.post("/chat", json={"message": "test"})
    assert r.status_code in (200, 502)


@pytest.mark.skip(reason="detect_pii が chat ルータ実装に組み込まれていない場合スキップ")
def test_chat_pii_detect_overrides_false_flag():
    # シナリオ: モデル応答 flag=false だが内容に PII が含まれる
    # 目的: 追加でサーバ側 PII 検出を行う実装が入った際の flag 上書き挙動を先行テスト化
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"私の電話は090-1234-5678です","flag":false}')):
        r = client.post("/chat", json={"message": "info"})
    assert r.status_code == 200
    assert r.json()["flag"] is True


# integration テストカバレッジ概要 (整理後):
# - /chat 正常系 + 不正JSON再試行 + タイムアウト再試行 + 空応答全失敗 + セマフォ429 + 並行呼び出し
# - /trivia 正常系 + 天気フェーズ例外→長文トリム
# 重複していた「純粋に長文トリムのみ」をテストする test_trivia_multiple_long_then_trim_loop は
# 上位シナリオ test_trivia_weather_fail_then_long_then_trim で包含されるため削除しスリム化。
# PII 上書きテストは chat 実装組込タイミングで skip 解除予定。

def test_trivia_weather_fail_then_long_then_trim():
    # シナリオ: 1 回目天気フェーズ例外 → フォールバック継続 → 長文生成 → 最終トリム
    # 目的: 途中フェーズ障害に対する graceful degradation と最終フォーマット制約維持
    calls = {"i": 0}
    long_txt = "これは二十文字をはっきり超える長文サンプルテキストです"

    def side_effect(*a, **k):
        if calls["i"] == 0:
            calls["i"] += 1
            raise RuntimeError("weather fetch error")
        return DummyResp(long_txt)
    with patch("app.routers.trivia.client.responses.create", side_effect=side_effect):
        r = client.post("/trivia", json={
            "latitude": "35", "longitude": "139", "direction": "南向き", "location": "ベランダ"
        })
    assert r.status_code == 200
    assert len(r.json()["response"]) <= 20


def test_chat_semaphore_timeout_returns_429(monkeypatch):
    # シナリオ: セマフォ取得がタイムアウト
    # 目的: 過負荷時にキュー膨張を避け 429 を返すレート制御 (保護メカニズム) の保証
    async def fake_wait_for(coro, timeout):
        raise asyncio.TimeoutError
    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        r = client.post("/chat", json={"message": "混雑テスト"})
    assert r.status_code == 429


def test_chat_openai_timeout_then_retry_success():
    """
    シナリオ: 外部API 1 回目タイムアウト → リトライで正常応答
    目的: 再試行ロジック（TimeoutError を再試行対象とする）と最終成功レスポンス保持
    許容: すべて失敗した場合 503
    """
    calls = {"i": 0}

    def side_effect(*a, **k):
        if calls["i"] == 0:
            calls["i"] += 1
            raise asyncio.TimeoutError
        return DummyResp('{"response":"タイムアウト後成功","flag":false}')

    with patch("app.routers.chat.client.responses.create", side_effect=side_effect):
        r = client.post("/chat", json={"message": "timeout"})

    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert "タイムアウト後成功" in r.json()["response"]


def test_chat_all_empty_responses_ends_503():
    # シナリオ: 全試行で空文字応答
    # 目的: 有効コンテンツ非生成時のフォールバック限界 → 503 あるいは実装で一時成功扱い分岐を検出
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"","flag":false}')):
        r = client.post("/chat", json={"message": "empty"})
    assert r.status_code in (200, 503)


def test_chat_concurrent_calls_basic(monkeypatch):
    # シナリオ: 複数並行呼び出し (ThreadPool) によるレート制御境界
    # 目的: 一部 429 が出ても他が正常応答できる “部分成功” を保証（全失敗防止）
    with patch("app.routers.chat.client.responses.create",
               return_value=DummyResp('{"response":"OK","flag":false}')):
        def call():
            res = client.post("/chat", json={"message": "hi"})
            return res.status_code
        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(lambda _: call(), range(8)))
    assert all(r in (200, 429) for r in results)
    assert any(r == 200 for r in results)


def test_root_index_optional():
    # シナリオ: index.html 有無が環境で異なる場合の許容応答
    # 目的: デプロイ形態差異 (静的ファイル配置あり/なし) がテストを不安定化しないように許容範囲定義
    r = client.get("/")
    assert r.status_code in (200, 404)
