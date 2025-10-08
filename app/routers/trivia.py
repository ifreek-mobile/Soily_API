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
# - TRIVIA_WEATHER_TIMEOUT: 天気取得（web_search_preview）のタイムアウト秒（既定 10.0）
# - TRIVIA_FALLBACK_MODEL: プライマリモデル失敗時のフォールバックモデル（既定 gpt-4o）
# - EXPOSE_OPENAI_REASON: エラー応答に原因(reason)を含めるか（既定 1 / 公開環境では 0 を推奨）
CONCURRENCY_LIMIT = int(os.getenv("TRIVIA_CONCURRENCY", "10"))
_TRIVIA_SEMAPHORE = asyncio.Semaphore(CONCURRENCY_LIMIT)
OPENAI_TIMEOUT = float(os.getenv("TRIVIA_OPENAI_TIMEOUT", "8.0"))
MAX_ATTEMPTS = int(os.getenv("TRIVIA_MAX_ATTEMPTS", "5"))
WEATHER_TIMEOUT = float(os.getenv("TRIVIA_WEATHER_TIMEOUT", "10.0"))
TRIVIA_FALLBACK_MODEL = os.getenv("TRIVIA_FALLBACK_MODEL", "gpt-4o")
# 開発デフォルトは有効化。本番運用では 0 に設定して詳細を隠蔽することを推奨
EXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "1") == "1" # 本番ではEXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "0") == "1"
# 一時的障害とみなして再試行対象にするステータスコード
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

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
            raise HTTPException(
                status_code=429, detail="混雑しています。しばらくしてからお試しください。")

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
                # print ではなくログ（DEBUG）に統一
                logger.debug("Weather resolved city=%s weather=%s raw_head=%r",
                             city, weather, raw[:60])
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
                "緯度経度から場所を特定しその情報を加味して回答をすること。"
                f"ユーザーは**{req.direction}**の**{req.location}**で野菜を栽培している情報も加味すること。"
                "嘘の情報は含めないこと。"
                "基本**すべて野菜の名前はカタカナ表記で統一してください。**、伝統野菜のみ、日本語（漢字など）で表記する場合は、カタカナ表記も併記してください。"
            )

            # モデルへ渡す補助情報（天気情報を追加）
            user_payload = {
                "month": month,
                "city": city,
                "weather": weather,
                "direction": req.direction,
                "location": req.location,
                "note": "短く簡潔に。読みやすく違和感のない一文**20文字以下に必ず**まとめる。回答には都市名か方角か天気か旬の情報のいずれかの情報は必ず含めつつ**自然な形**で回答すること。",
            }
            # 生成ループ：OpenAI呼び出しにタイムアウトを付け、20文字以下なら採用。
            # 超過時は軽いバックオフ(0.2, 0.4, ... 最大1.0秒)を挟み、最大 MAX_ATTEMPTS 回まで試行。
            text_format = {"format": {"type": "text"}}
            last_error_reason = ""
            for attempt in range(MAX_ATTEMPTS):
                try:
                    resp = await asyncio.wait_for(
                        client.responses.create(
                            model="gpt-4o-mini",
                            instructions=instructions,
                            input=json.dumps(user_payload, ensure_ascii=False),
                            text=text_format,
                        ),
                        timeout=OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    last_error_reason = "timeout"
                    logger.warning("trivia timeout attempt=%d", attempt + 1)
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
                    continue
                except Exception as e:
                    last_error_reason = type(e).__name__
                    status = getattr(e, "status_code", None)
                    if status is None:
                        status = getattr(
                            getattr(e, "response", None), "status_code", None)
                    err_msg = str(e)
                    if any(token in err_msg.lower() for token in ("api key", "unauthorized", "authentication")):
                        logger.error("trivia OpenAI 認証エラー: %s", err_msg)
                        raise HTTPException(
                            status_code=401, detail="OpenAI APIキーが無効または読み込めていません。")
                    fallback_resp = None
                    if status in RETRY_STATUS_CODES and TRIVIA_FALLBACK_MODEL and TRIVIA_FALLBACK_MODEL != "gpt-4o-mini":
                        logger.warning("trivia fallback を試行 model=%s status=%s attempt=%d",
                                       TRIVIA_FALLBACK_MODEL, status, attempt + 1)
                        try:
                            fallback_resp = await asyncio.wait_for(
                                client.responses.create(
                                    model=TRIVIA_FALLBACK_MODEL,
                                    instructions=instructions,
                                    input=json.dumps(
                                        user_payload, ensure_ascii=False),
                                    text=text_format,
                                ),
                                timeout=OPENAI_TIMEOUT + 2.0,
                            )
                            resp = fallback_resp
                            last_error_reason = f"fallback({TRIVIA_FALLBACK_MODEL})"
                            logger.info(
                                "trivia fallback 成功 model=%s attempt=%d", TRIVIA_FALLBACK_MODEL, attempt + 1)
                        except Exception as fallback_error:
                            last_error_reason = type(fallback_error).__name__
                            status = getattr(
                                fallback_error, "status_code", status)
                            logger.warning(
                                "trivia fallback 失敗: %r", fallback_error)
                            if attempt + 1 < MAX_ATTEMPTS:
                                await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
                                continue
                            if status == 429:
                                raise HTTPException(
                                    status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                            detail = "外部サービスが混雑しています。時間をおいて再度お試しください。"
                            if EXPOSE_OPENAI_REASON:
                                detail += f" (reason={last_error_reason})"
                            raise HTTPException(statusコード=503, detail=detail)
                    if status in RETRY_STATUS_CODES and fallback_resp is None:
                        if attempt + 1 < MAX_ATTEMPTS:
                            logger.warning(
                                "trivia retryable status=%s attempt=%d: %r", status, attempt + 1, e)
                            await asyncio.sleep(min(0.25 * (attempt + 1), 1.0))
                            continue
                        if status == 429:
                            raise HTTPException(
                                status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                        detail = "外部サービスが混雑しています。時間をおいて再度お試しください。"
                        if EXPOSE_OPENAI_REASON:
                            detail += f" (reason={last_error_reason or 'retry_exhausted'})"
                        raise HTTPException(status_code=503, detail=detail)
                    raise
                ai_text = (getattr(resp, "output_text", None) or "").strip()
                if not ai_text:
                    last_error_reason = last_error_reason or "empty_output"
                    logger.warning("trivia empty output attempt=%d", attempt)
                    if attempt + 1 < MAX_ATTEMPTS:
                        await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail=("外部サービスが混雑しています。時間をおいて再度お試しください。"
                                + (f" (reason={last_error_reason})" if EXPOSE_OPENAI_REASON else "")),
                    )
                if len(ai_text) <= 20:
                    break
                # 短いバックオフで外部APIの瞬間負荷を緩和
                await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))

            # ガード：応答が空なら 503（一時的利用不能）
            if not ai_text:
                detail = "外部サービスが混雑しています。時間をおいて再度お試しください。"
                if EXPOSE_OPENAI_REASON and last_error_reason:
                    detail += f" (reason={last_error_reason})"
                raise HTTPException(statusコード=503, detail=detail)
            # 最終ガード：まだ20文字超なら切り詰め（ログは先頭60文字のみ）
            if len(ai_text) > 20:
                logger.warning("20文字制約未達のため切り詰め実施 head=%r", ai_text[:60])
                ai_text = ai_text[:20].strip()

            return TriviaResponse(response=ai_text)
        finally:
            # 例外の有無に関わらずセマフォを解放し、枯渇（デッドロック）を防ぐ
            _TRIVIA_SEMAPHORE.release()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("trivia fatal err=%r", e)
        detail = "サーバーエラーが発生しました"
        if EXPOSE_OPENAI_REASON:
            detail += f" (reason={type(e).__name__})"
        raise HTTPException(status_code=500, detail=detail)
