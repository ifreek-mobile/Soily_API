from fastapi import APIRouter, HTTPException, Body
from app.models import TriviaResponse, TriviaRequest
from datetime import datetime
import json
import logging
import asyncio
import os
import re

from app.services.openai_client import client

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# 同時実行制限や外部API制御のためのパラメータ群
# - TRIVIA_CONCURRENCY: プロセス内の同時実行上限（asyncio.Semaphore）。全体の上限は「ワーカー数×この値」が目安。
# - TRIVIA_OPENAI_TIMEOUT: OpenAI呼び出しの1回あたりタイムアウト秒。短すぎると失敗増、長すぎると滞留。
# - TRIVIA_MAX_ATTEMPTS: 20文字制約に収まるまでの再生成回数。大きいほど成功率↑/レイテンシとコスト↑。
CONCURRENCY_LIMIT = int(os.getenv("TRIVIA_CONCURRENCY", "10"))
_TRIVIA_SEMAPHORE = asyncio.Semaphore(CONCURRENCY_LIMIT)
OPENAI_TIMEOUT = float(os.getenv("TRIVIA_OPENAI_TIMEOUT", "8.0"))
MAX_ATTEMPTS = int(os.getenv("TRIVIA_MAX_ATTEMPTS", "5"))
WEATHER_TIMEOUT = float(os.getenv("TRIVIA_WEATHER_TIMEOUT", "10.0"))

# web_search_preview で都市と天気をJSONとして取得するためのスキーマ
WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "特定した都市名"},
        "weather": {"type": "string", "description": "本日の天気情報（晴れ、曇り、雨、雷、雪、時々〇〇）"},
    },
    "required": ["city", "weather"],
    "additionalProperties": False,
    "strict": True,
}

def _safe_json(text: str) -> dict:
    t = text.strip()
    # コードフェンス除去
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
    # 本体抽出
    if "{" in t and "}" in t:
        t = t[t.find("{"): t.rfind("}") + 1]
    # 制御文字除去
    t = re.sub(r"[\x00-\x1F\x7F]", "", t)
    try:
        return json.loads(t)
    except Exception:
        return {}

@router.post(
    "/trivia",
    summary="野菜トリビア",
    description="緯度/経度・方角・設置場所（ベランダ/庭）と現在の月を加味したトリビアを返します（非同期）。",
    response_model=TriviaResponse,)
async def trivia(req: TriviaRequest = Body(..., description='{"latitude":"...", "longitude":"...", "direction":"...", "location":"..."} 形式')):
    try:
        # スパイク吸収用：セマフォ取得を最大2秒待機。取れない場合は 429 を返し、滞留を防止。
        try:
            await asyncio.wait_for(_TRIVIA_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # 現在の月（ローカルタイム）をプロンプトに渡す
            month = datetime.now().month

            # 先に緯度経度から「都市」と「本日の天気」を検索（web_search_preview）
            city, weather = "", ""
            try:
                weather_resp = await asyncio.wait_for(
                    client.responses.create(
                        model="gpt-4o-mini",
                        input=f"緯度{req.latitude} 経度{req.longitude}から場所の特定と本日の天気の情報を取得して",
                        tools=[{"type": "web_search_preview"}],
                        tool_choice={"type": "web_search_preview"},
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": "WeatherJson",
                                "schema": WEATHER_SCHEMA,
                            }
                        },
                    ),
                    timeout=WEATHER_TIMEOUT,
                )
                # 応答から都市と天気を抽出
                raw = (getattr(weather_resp, "output_text", None) or "").strip()
                data = _safe_json(raw)
                city = str(data.get("city", "")).strip()
                weather = str(data.get("weather", "")).strip()
                print(f"都市: {city}, 天気: {data.get('weather', '')}")
            except Exception as we:
                logger.warning("天気取得に失敗（フォールバック）: %r", we)
                # city/weather は空のまま進める

            # 指示文（要件どおりに統一）
            instructions = (
                "あなたは野菜のトリビア案内役です。特定の野菜の指定はありません。"
                "現在の月に関係する旬の野菜にまつわる**誰も知られていない役に立つ豆知識**を主題に日本語で簡潔にまとめてください。"
                "豆知識を読みやすく違和感のない一文**20文字以下に必ず**まとめる。出力はテキストのみ。"
                "敬語は使わない。"
                "語尾は『〜だよ』『〜だね』『〜なんだ』『〜かな？』『〜しよう！』『！』などを用いる。"
                "絵文字は使わない。必ず日本語で回答する。"
                "緯度経度から場所を特定しその情報を加味して回答をすること"
                f"ユーザーは**{req.direction}**の**{req.location}**で野菜を栽培している情報も加味すること"
                "嘘の情報は含めないこと"
            )

            # モデルへ渡す補助情報（天気情報を追加）
            user_payload = {
                "month": month,
                "city": city,
                "weather": weather,
                "direction": req.direction,
                "location": req.location,
                "note": "短く簡潔に。読みやすく違和感のない一文**20文字以下に必ず**まとめる。回答には都市名か方角か天気か旬の情報のいずれかの情報は必ず含めつつ**自然な形**で回答すること",
            }
            # 生成ループ：OpenAI呼び出しにタイムアウトを付け、20文字以下なら採用。
            # 超過時は軽いバックオフ(0.2, 0.4, ... 最大1.0秒)を挟み、最大 MAX_ATTEMPTS 回まで試行。
            ai_text = ""
            for attempt in range(MAX_ATTEMPTS):
                try:
                    resp = await asyncio.wait_for(
                        client.responses.create(
                            model="gpt-4o-mini",
                            instructions=instructions,
                            input=json.dumps(user_payload, ensure_ascii=False),
                            text={"format": {"type": "text"}},
                        ),
                        timeout=OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # タイムアウトはWARNで記録し、次の試行へ（スロットリングや一時障害を想定）
                    logger.warning("OpenAI 呼び出しがタイムアウト（attempt=%d）", attempt + 1)
                    continue
                except Exception as oe:
                    # 上流の一時的エラー（429/5xx/接続エラーなど）も次試行へ
                    logger.warning("OpenAI 呼び出しで例外（attempt=%d）: %r", attempt + 1, oe)
                    await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                    continue
                ai_text = (getattr(resp, "output_text", None) or "").strip()
                if ai_text and len(ai_text) <= 20:
                    break
                # 短いバックオフで外部APIの瞬間負荷を緩和
                await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))

            # ガード：応答が空なら 500（上位で HTTPException に変換）
            if not ai_text:
                raise RuntimeError("AIからの応答が空でした")
            # 最終ガード：まだ20文字超ならログを残し、先頭20文字に切り詰めて返却
            if len(ai_text) > 20:
                logger.warning("20文字制約未達のため切り詰めを実施: %s", ai_text)
                ai_text = ai_text[:20].strip()

            return TriviaResponse(response=ai_text)
        finally:
            # 例外の有無に関わらずセマフォを解放し、枯渇（デッドロック）を防ぐ
            _TRIVIA_SEMAPHORE.release()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error in /trivia: %r", e)
        raise HTTPException(status_code=500, detail="サーバーエラーが発生しました")
