import pytest
import requests
import time
from concurrent.futures import ThreadPoolExecutor

pytestmark = pytest.mark.e2e


def test_chat_smoke_parallel(e2e_server, monkeypatch):
    """目的: 同時並行(軽度負荷)で /chat が部分成功(200)を維持しつつ過負荷時は 429 を返して落ちないこと。
    検証: 5ワーカー×15リクエストで全結果が 200 または 429 のみ、かつ少なくとも1件は 200 (全滅していない)。内部例外や 5xx を出さずにスロット制御が働くか確認。"""
    import app.routers.chat as chat_mod
    from unittest.mock import patch

    class Dummy:
        output_text = '{"response":"OK","flag":false}'
    # 同一 patch を各スレッドで共有
    with patch.object(chat_mod.client.responses, "create", return_value=Dummy()):
        def one(i):
            r = requests.post(
                e2e_server["base_url"] + "/chat", json={"message": f"id{i}"})
            return r.status_code
        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(one, range(15)))
    # 429 混在容認
    assert all(r in (200, 429) for r in results)
    assert any(r == 200 for r in results)
