from fastapi import APIRouter, HTTPException, Body
import json
import logging
import asyncio
import os
import re
from typing import Any, Dict
from datetime import datetime, timezone, timedelta
from app.models import RealTimeChatRequest, RealTimeChatResponse
from app.services.openai_client import client
from app.services.tools import REALTIME_OPENAI_TOOLS
from app.prompts.soylly import SOYLY_PROMPT
from app.prompts.katakana_examples import KATAKANA_VEGETABLE_EXAMPLES
from app.services.geocode import resolve_pref_city

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# 再試行対象とする HTTP ステータスの集合
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# Web検索結果で返る Markdown 形式のリンクを検出するための正規表現
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")

# リアルタイムチャット用設定
# - REALTIME_CHAT_CONCURRENCY: プロセス内で同時に処理する最大リクエスト数（既定 15）
# - REALTIME_CHAT_OPENAI_TIMEOUT: OpenAI 呼び出し1回あたりのタイムアウト秒（既定 15.0）
# - REALTIME_CHAT_MAX_ATTEMPTS: タイムアウト/空応答時の再試行回数（既定 2）
# - REALTIME_CHAT_FALLBACK_MODEL: プライマリ失敗時のフォールバックモデル（既定 gpt-4o）
# - EXPOSE_OPENAI_REASON: エラー応答に原因(reason)を含めるか（既定 1 / 本番では 0 推奨）
REALTIME_CHAT_CONCURRENCY = int(os.getenv("REALTIME_CHAT_CONCURRENCY", "15"))
_REALTIME_CHAT_SEMAPHORE = asyncio.Semaphore(REALTIME_CHAT_CONCURRENCY)
REALTIME_CHAT_OPENAI_TIMEOUT = float(
    os.getenv("REALTIME_CHAT_OPENAI_TIMEOUT", "20.0"))
REALTIME_CHAT_MAX_ATTEMPTS = int(os.getenv("REALTIME_CHAT_MAX_ATTEMPTS", "2"))
REALTIME_CHAT_FALLBACK_MODEL = os.getenv(
    "REALTIME_CHAT_FALLBACK_MODEL", "gpt-4o")
REALTIME_EXPOSE_OPENAI_REASON = os.getenv(
    "REALTIME_EXPOSE_OPENAI_REASON", "1") == "1"


@router.post(
    "/chat/real-time",
    response_model=RealTimeChatResponse,
    summary="リアルタイムチャット応答",
    description="ユーザー名と質問を受け取り、任意の地理情報を添えて AI が応答します。",
)
async def chat_real_time(request: RealTimeChatRequest = Body(..., description="リアルタイムチャットのリクエスト")):
    try:
        # --- セマフォで同時実行数を制御し、過負荷を防止 ---
        try:
            await asyncio.wait_for(_REALTIME_CHAT_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # --- Responses API に期待する JSON スキーマを構築 ---
            response_format = {
                "format": {
                    "type": "json_schema",
                    "name": "RealTimeChatResponse",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["response", "flag"],
                        "properties": {
                            "response": {"type": "string", "maxLength": 300, "description": "AIの応答"},
                            "flag": {"type": "boolean", "description": "個人情報が含まれているかどうか"}
                        }
                    }
                }
            }
            ai_response = ""
            last_error_reason = ""
            for attempt in range(REALTIME_CHAT_MAX_ATTEMPTS):
                # --- 現在時刻 (JST) を取得し payload に含める ---
                current_time_iso = datetime.now(
                    timezone(timedelta(hours=9))
                ).isoformat()
                # --- 天気質問かどうかを判定し、位置情報を逆ジオコーディング ---
                weather_requested = _should_request_weather(request.message)
                prefecture, city = await resolve_pref_city(request.latitude, request.longitude)
                # --- デバック用リクエスト内容のログ記録 ---
                logger.info(
                    "chat_real_time request username=%s lat=%s lon=%s direction=%s location=%s weather_requested=%s prefecture=%s city=%s",
                    request.username,
                    request.latitude,
                    request.longitude,
                    request.direction,
                    request.location,
                    weather_requested,
                    prefecture,
                    city,
                )
                # --- モデルへ渡す入力ペイロード ---
                user_payload = {
                    "username": request.username,
                    "user_message": request.message,
                    "context": {
                        "prefecture": prefecture,
                        "city": city,
                        "direction": request.direction,
                        "location": request.location,
                        "current_time": current_time_iso,
                    },
                    "weather_requested": weather_requested,
                    "constraints": [
                        "野菜名は必ずカタカナ表記で統一する（入力がひらがな/漢字でも変換）",
                        "冒頭は「{username}さん、{挨拶一言}」の形式にする。挨拶は current_time の時間帯に合わせた語（おはようございます／こんにちは／こんばんは 等）と、短い一言を組み合わせる。",
                        "JSONのみを返す（response, flag）",
                        "weather_requested が true のときは web_search を活用し、最新の天気情報を回答に反映する",
                        "weather_requested が false のときは web search を使用せず通常回答を行う",
                        "current_time を基準に時間表現（今、◯時間後、明日など）を解釈し、矛盾のない回答を返す",
                        "絶対に具体的な住所の情報を出力しないこと",
                        "回答内に URL や参照リンク（例: [名称](https://...)）を含めないこと",
                    ],
                    "examples": KATAKANA_VEGETABLE_EXAMPLES.strip(),
                }
                openai_kwargs: Dict[str, Any] = {
                    "model": "gpt-4o-mini",
                    "instructions": SOYLY_PROMPT,
                    "input": json.dumps(user_payload, ensure_ascii=False),
                    "text": response_format,
                }
                if weather_requested:
                    # --- 天気系質問の場合のみ web_search ツールを付与 ---
                    openai_kwargs["tools"] = REALTIME_OPENAI_TOOLS
                    openai_kwargs["tool_choice"] = "auto"
                else:
                    # --- 通常質問ではツールを無効のまま使用 ---
                    pass

                try:
                    # --- OpenAI Responses API を呼び出し、タイムアウトを監視 ---
                    resp = await asyncio.wait_for(
                        client.responses.create(**openai_kwargs),
                        timeout=REALTIME_CHAT_OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # --- タイムアウト時はログ記録の上で必要ならリトライ ---
                    last_error_reason = "timeout"
                    logger.warning(
                        "/chat/real-time OpenAI タイムアウト attempt=%d", attempt + 1)
                    if attempt + 1 < REALTIME_CHAT_MAX_ATTEMPTS:
                        await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail="外部サービスが混雑しています。時間をおいて再試行してください。"
                        + (f" (reason={last_error_reason})" if REALTIME_EXPOSE_OPENAI_REASON else ""),
                    )
                except Exception as e:
                    # --- APIキー不備や HTTP エラー時の処理 ---
                    last_error_reason = type(e).__name__
                    status = getattr(e, "status_code", None)
                    if status is None:
                        status = getattr(
                            getattr(e, "response", None), "status_code", None)
                    err_msg = str(e)
                    if any(token in err_msg.lower() for token in ("api key", "unauthorized", "authentication")):
                        logger.error(
                            "/chat/real-time OpenAI 認証エラー: %s", err_msg)
                        raise HTTPException(
                            status_code=401, detail="OpenAI APIキーが無効または読み込めていません。")
                    fallback_resp = None
                    if status in RETRY_STATUS_CODES and REALTIME_CHAT_FALLBACK_MODEL and REALTIME_CHAT_FALLBACK_MODEL != "gpt-4o-mini":
                        # --- フォールバックモデルによる再試行 ---
                        logger.warning("/chat/real-time fallback を試行 model=%s status=%s attempt=%d",
                                       REALTIME_CHAT_FALLBACK_MODEL, status, attempt + 1)
                        try:
                            fallback_resp = await asyncio.wait_for(
                                client.responses.create(
                                    model=REALTIME_CHAT_FALLBACK_MODEL,
                                    instructions=SOYLY_PROMPT,
                                    input=json.dumps(
                                        user_payload, ensure_ascii=False),
                                    text=response_format,
                                ),
                                timeout=REALTIME_CHAT_OPENAI_TIMEOUT + 2.0,
                            )
                            resp = fallback_resp
                            last_error_reason = f"fallback({REALTIME_CHAT_FALLBACK_MODEL})"
                            logger.info(
                                "/chat/real-time fallback 成功 model=%s attempt=%d", REALTIME_CHAT_FALLBACK_MODEL, attempt + 1)
                        except Exception as fallback_error:
                            # --- フォールバック失敗時のログとリトライ制御 ---
                            last_error_reason = type(fallback_error).__name__
                            status = getattr(
                                fallback_error, "status_code", status)
                            logger.warning(
                                "/chat/real-time fallback 失敗: %r", fallback_error)
                            if attempt + 1 < REALTIME_CHAT_MAX_ATTEMPTS:
                                await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                                continue
                            if status == 429:
                                raise HTTPException(
                                    status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                            detail = "外部サービスが混雑しています。時間をおいて再試行してください。"
                            if REALTIME_EXPOSE_OPENAI_REASON:
                                detail += f" (reason={last_error_reason})"
                            raise HTTPException(status_code=503, detail=detail)
                    if status in RETRY_STATUS_CODES and fallback_resp is None:
                        # --- フォールバック無しの場合のリトライ分岐 ---
                        if attempt + 1 < REALTIME_CHAT_MAX_ATTEMPTS:
                            logger.warning(
                                "/chat/real-time OpenAI 一時エラー status=%s attempt=%d: %r", status, attempt + 1, e)
                            await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                            continue
                        if status == 429:
                            raise HTTPException(
                                status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                        detail = "外部サービスが混雑しています。時間をおいて再試行してください。"
                        if REALTIME_EXPOSE_OPENAI_REASON:
                            detail += f" (reason={last_error_reason or 'retry_exhausted'})"
                        raise HTTPException(status_code=503, detail=detail)
                    raise
                ai_response = (
                    getattr(resp, "output_text", None) or "").strip()
                if not ai_response:
                    # --- 空応答は再試行対象 ---
                    last_error_reason = last_error_reason or "empty_output"
                    logger.warning(
                        "chat/real-time empty output attempt=%d", attempt)
                    if attempt < REALTIME_CHAT_MAX_ATTEMPTS:
                        await asyncio.sleep(min(0.2 * attempt, 1.0))
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail=("外部サービスが混雑しています。時間をおいて再試行してください。"
                                + (f" (reason={last_error_reason})" if REALTIME_EXPOSE_OPENAI_REASON else "")),
                    )
                try:
                    parsed = _safe_parse_json_response(ai_response)
                except json.JSONDecodeError:
                    # --- JSON 形式でない応答は 502 として扱う ---
                    last_error_reason = "json_decode_error"
                    logger.warning("AI応答JSON不正 raw=%r", ai_response[:120])
                    raise HTTPException(status_code=502, detail="AI応答形式不正")
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=502, detail="AI応答形式不正")

                response_text = parsed.get("response")
                flag_value = parsed.get("flag")
                if weather_requested:
                    flag_value = False
                if not isinstance(response_text, str) or not isinstance(flag_value, bool):
                    raise HTTPException(status_code=502, detail="AI応答の型エラー")

                response_text = response_text.strip()

                # --- 応答冒頭に「ユーザー名＋さん」を必ず付与し、挨拶文そのものはモデル出力に任せる ---
                greeting_prefix = f"{request.username}さん"
                if response_text:
                    if not response_text.startswith(greeting_prefix):
                        # 既存テキストの冒頭に差し込む。句読点は重複しないよう調整
                        response_text = f"{greeting_prefix}、{response_text}"
                else:
                    response_text = f"{greeting_prefix}、ご質問ありがとうございます。"

                # --- Web検索結果による Markdown リンクを除去 ---
                response_text = _MARKDOWN_LINK_RE.sub(r"\1", response_text)

                if len(response_text) > 300:
                    # --- 300文字を超えた場合は切り捨て ---
                    logger.warning("AI応答300文字超過のため切り詰め head=%r",
                                   response_text[:60])
                    response_text = response_text[:300]

                return RealTimeChatResponse(response=response_text, flag=flag_value)

            detail = "応答を取得できませんでした"
            if REALTIME_EXPOSE_OPENAI_REASON and last_error_reason:
                detail += f" (reason={last_error_reason})"
            raise HTTPException(status_code=503, detail=detail)
        finally:
            # --- セマフォを必ず解放 ---
            _REALTIME_CHAT_SEMAPHORE.release()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # --- 予期しない例外は 500 としてログ出力 ---
        logger.exception("Unexpected error in /chat/real-time: %r", e)
        detail = "サーバーエラーが発生しました"
        if REALTIME_EXPOSE_OPENAI_REASON:
            detail += f" (reason={type(e).__name__})"
        raise HTTPException(status_code=500, detail=detail)


# --- 天気関連キーワードによる判定用タプル ---
WEATHER_KEYWORDS = ("天気", "気温", "気候", "雨", "晴れ",
                    "曇り", "雪", "雷", "湿度", "風速", "天候")


def _should_request_weather(message: str) -> bool:
    # --- メッセージ内に天気の語句が含まれているかを判定 ---
    text = message or ""
    return any(keyword in text for keyword in WEATHER_KEYWORDS)


def _safe_parse_json_response(raw: str) -> Dict[str, Any]:
    # --- モデル出力の先頭/末尾に余計な文字があっても JSON を抽出 ---
    def _normalize(s: str) -> str:
        return s.replace("\n", "\\n")
    try:
        return json.loads(_normalize(raw))
    except json.JSONDecodeError as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            trimmed = raw[start:end + 1]
            try:
                return json.loads(_normalize(trimmed))
            except json.JSONDecodeError:
                logger.warning("AI応答JSON切り出し後も不正 raw=%r", trimmed[:120])
        raise exc
