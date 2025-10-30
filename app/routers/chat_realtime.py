from fastapi import APIRouter, HTTPException, Body
import json
import logging
import asyncio
import os
from typing import Any, Dict, List  # noqa: F401
from app.models import ChatRequest, ChatResponse, RealTimeChatRequest, RealTimeChatResponse
from app.services.openai_client import client
from app.services.tools import REALTIME_OPENAI_TOOLS
from app.prompts.soylly import SOYLY_PROMPT
from app.prompts.katakana_examples import KATAKANA_VEGETABLE_EXAMPLES

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# 同時実行・外部API制御（環境変数で調整可能）
# - CHAT_CONCURRENCY: プロセス内で同時に処理する最大リクエスト数
# - CHAT_OPENAI_TIMEOUT: OpenAI 呼び出し1回あたりのタイムアウト秒
# - CHAT_MAX_ATTEMPTS: タイムアウト/空応答時の再試行回数
# - CHAT_FALLBACK_MODEL: プライマリモデル失敗時に利用するフォールバックモデル
# - EXPOSE_OPENAI_REASON: エラー応答に原因(reason)を含めるか
CHAT_CONCURRENCY = int(os.getenv("CHAT_CONCURRENCY", "30"))
_CHAT_SEMAPHORE = asyncio.Semaphore(CHAT_CONCURRENCY)
CHAT_OPENAI_TIMEOUT = float(os.getenv("CHAT_OPENAI_TIMEOUT", "10.0"))
CHAT_MAX_ATTEMPTS = int(os.getenv("CHAT_MAX_ATTEMPTS", "2"))
CHAT_FALLBACK_MODEL = os.getenv("CHAT_FALLBACK_MODEL", "gpt-4o")
# 開発デフォルトは有効化。本番運用では 0 に設定して詳細を隠蔽することを推奨
# 本番ではEXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "0") == "1"
EXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "1") == "1"
# 一時的障害とみなして再試行対象にするステータスコード
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# リアルタイムチャット用
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
REALTIME_WEATHER_TIMEOUT = float(os.getenv("REALTIME_WEATHER_TIMEOUT", "10.0"))


@router.post("/chat", response_model=ChatResponse, summary="チャット応答", description="ユーザーからのメッセージを受け取り、AI（ソイリィ）が応答を返します。")
async def chat(request: ChatRequest = Body(..., description="ユーザーからのメッセージ")):
    try:
        # --- セマフォで同時実行数を制限（2秒で諦めて 429） ---
        try:
            await asyncio.wait_for(_CHAT_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # --- OpenAI 応答を通常チャット用に取得 ---
            response_format = {
                "format": {
                    "type": "json_schema",
                    "name": "ChatResponse",
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["response", "flag"],
                        "properties": {
                            "response": {"type": "string", "maxLength": 300, "description": "ソイリィの口調での回答"},
                            "flag": {"type": "boolean", "description": "個人情報が含まれているかどうか"}
                        }
                    }
                }
            }
            ai_response = ""
            last_error_reason = ""
            for attempt in range(CHAT_MAX_ATTEMPTS):
                try:
                    # --- モデルへ渡すプロンプト／入力ペイロード ---
                    user_payload = {
                        "user_message": request.message,
                        "constraints": [
                            "野菜名は必ずカタカナ表記で統一する（入力がひらがな/漢字でも変換）",
                            "JSONのみを返す（response, flag）"
                        ],
                        "examples": KATAKANA_VEGETABLE_EXAMPLES.strip()
                    }
                    resp = await asyncio.wait_for(
                        client.responses.create(
                            model="gpt-4o-mini",
                            instructions=SOYLY_PROMPT,
                            input=json.dumps(user_payload, ensure_ascii=False),
                            text=response_format,
                        ),
                        timeout=CHAT_OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # --- タイムアウト時はバックオフしながらリトライ ---
                    last_error_reason = "timeout"
                    logger.warning(
                        "/chat OpenAI タイムアウト attempt=%d", attempt + 1)
                    await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                    continue
                except Exception as e:
                    # --- APIキー不備・HTTPエラーなどの例外ハンドリング ---
                    last_error_reason = type(e).__name__
                    status = getattr(e, "status_code", None)
                    if status is None:
                        status = getattr(
                            getattr(e, "response", None), "status_code", None)
                    err_msg = str(e)
                    if any(token in err_msg.lower() for token in ("api key", "unauthorized", "authentication")):
                        logger.error("/chat OpenAI 認証エラー: %s", err_msg)
                        raise HTTPException(
                            status_code=401, detail="OpenAI APIキーが無効または読み込めていません。")
                    fallback_resp = None
                    if status in RETRY_STATUS_CODES and CHAT_FALLBACK_MODEL and CHAT_FALLBACK_MODEL != "gpt-4o-mini":
                        # --- フォールバックモデルでの再試行 ---
                        logger.warning("/chat fallback を試行 model=%s status=%s attempt=%d",
                                       CHAT_FALLBACK_MODEL, status, attempt + 1)
                        try:
                            fallback_resp = await asyncio.wait_for(
                                client.responses.create(
                                    model=CHAT_FALLBACK_MODEL,
                                    instructions=SOYLY_PROMPT,
                                    input=json.dumps(
                                        user_payload, ensure_ascii=False),
                                    text=response_format,
                                ),
                                timeout=CHAT_OPENAI_TIMEOUT + 2.0,
                            )
                            resp = fallback_resp
                            last_error_reason = f"fallback({CHAT_FALLBACK_MODEL})"
                            logger.info(
                                "/chat fallback 成功 model=%s attempt=%d", CHAT_FALLBACK_MODEL, attempt + 1)
                        except Exception as fallback_error:
                            # --- フォールバック失敗時の追処理 ---
                            last_error_reason = type(fallback_error).__name__
                            status = getattr(
                                fallback_error, "status_code", status)
                            logger.warning(
                                "/chat fallback 失敗: %r", fallback_error)
                            if attempt + 1 < CHAT_MAX_ATTEMPTS:
                                await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                                continue
                            if status == 429:
                                raise HTTPException(
                                    status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                            detail = "外部サービスが混雑しています。時間をおいて再試行してください。"
                            if EXPOSE_OPENAI_REASON:
                                detail += f" (reason={last_error_reason})"
                            raise HTTPException(status_code=503, detail=detail)
                    if status in RETRY_STATUS_CODES and fallback_resp is None:
                        # --- フォールバックなしでの再試行判断 ---
                        if attempt + 1 < CHAT_MAX_ATTEMPTS:
                            logger.warning(
                                "/chat OpenAI 一時エラー status=%s attempt=%d: %r", status, attempt + 1, e)
                            await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                            continue
                        if status == 429:
                            raise HTTPException(
                                status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                        detail = "外部サービスが混雑しています。時間をおいて再試行してください。"
                        if EXPOSE_OPENAI_REASON:
                            detail += f" (reason={last_error_reason or 'retry_exhausted'})"
                        raise HTTPException(statusコード=503, detail=detail)
                    raise
                ai_response = (
                    getattr(resp, "output_text", None) or "").strip()
                if not ai_response:
                    # --- 空文字応答対策 ---
                    last_error_reason = last_error_reason or "empty_output"
                    logger.warning("chat empty output attempt=%d", attempt)
                    if attempt < CHAT_MAX_ATTEMPTS:
                        await asyncio.sleep(min(0.2 * attempt, 1.0))
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail=("外部サービスが混雑しています。時間をおいて再試行してください。"
                                + (f" (reason={last_error_reason})" if EXPOSE_OPENAI_REASON else "")),
                    )
                try:
                    parsed = _safe_parse_json_response(ai_response)
                except json.JSONDecodeError:
                    # --- JSON 変換失敗時 ---
                    last_error_reason = "json_decode_error"
                    logger.warning("AI応答JSON不正 raw=%r", ai_response[:120])
                    raise HTTPException(status_code=502, detail="AI応答形式不正")
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=502, detail="AI応答形式不正")

                response_text = parsed.get("response")
                flag_value = parsed.get("flag")
                if not isinstance(response_text, str) or not isinstance(flag_value, bool):
                    raise HTTPException(status_code=502, detail="AI応答の型エラー")

                # 文字数制限
                response_text = response_text.strip()
                if len(response_text) > 300:
                    # --- 文字数オーバーは切り詰め ---
                    logger.warning("AI応答300文字超過のため切り詰め head=%r",
                                   response_text[:60])
                    response_text = response_text[:300]

                return ChatResponse(response=response_text, flag=flag_value)
        finally:
            # --- セマフォの解放を保証 ---
            _CHAT_SEMAPHORE.release()

        # --- 全試行失敗時の最終エラー応答 ---
        detail = "応答を取得できませんでした"
        if EXPOSE_OPENAI_REASON and last_error_reason:
            detail += f" (reason={last_error_reason})"
        raise HTTPException(status_code=503, detail=detail)
    except HTTPException:
        # 意図的なHTTPエラーはそのままクライアントへ
        raise
    except Exception as e:
        # --- 想定外例外のログ出力 ---
        logger.exception("Unexpected error in /chat: %r", e)
        detail = "サーバーエラーが発生しました"
        if EXPOSE_OPENAI_REASON:
            detail += f" (reason={type(e).__name__})"
        raise HTTPException(status_code=500, detail=detail)


@router.post(
    "/chat/real-time",
    response_model=RealTimeChatResponse,
    summary="リアルタイムチャット応答",
    description="ユーザー名と質問を受け取り、任意の地理情報を添えて AI が応答します。",
)
async def chat_real_time(request: RealTimeChatRequest = Body(..., description="リアルタイムチャットのリクエスト")):
    try:
        # --- リアルタイム用セマフォ（2秒待機でタイムアウト） ---
        try:
            await asyncio.wait_for(_REALTIME_CHAT_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # --- AI 応答の JSON スキーマ定義 ---
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
                weather_requested = _should_request_weather(request.message)
                # --- デバッグログでリクエスト内容を可視化 ---
                # print("[chat_real_time] username=", request.username,
                #       "lat=", request.latitude,
                #       "lon=", request.longitude,
                #       "direction=", request.direction,
                #       "location=", request.location)
                # print("[chat_real_time] weather_requested=", weather_requested)
                # --- モデルへ渡す入力ペイロード ---
                user_payload = {
                    "username": request.username,
                    "user_message": request.message,
                    "context": {
                        "latitude": request.latitude,
                        "longitude": request.longitude,
                        "direction": request.direction,
                        "location": request.location,
                    },
                    "weather_requested": weather_requested,
                    "constraints": [
                        "野菜名は必ずカタカナ表記で統一する（入力がひらがな/漢字でも変換）",
                        "JSONのみを返す（response, flag）",
                        "weather_requested が true のときは web_search_preview を活用し、最新の天気情報を回答に反映する",
                        "weather_requested が false のときは web search を使用せず通常回答を行う",
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
                    # --- 天気質問時のみ web_search ツールを付与 ---
                    openai_kwargs["tools"] = REALTIME_OPENAI_TOOLS
                    openai_kwargs["tool_choice"] = "auto"
                    # print("[chat_real_time] tools enabled:", openai_kwargs["tools"])
                else:
                    # print("[chat_real_time] tools disabled (standard response)")
                    pass

                try:
                    # --- OpenAI Responses API 呼び出し ---
                    resp = await asyncio.wait_for(
                        client.responses.create(**openai_kwargs),
                        timeout=REALTIME_CHAT_OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # --- 応答遅延時のリトライ処理 ---
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
                    # --- 認証/HTTP エラーなどの例外ハンドリング ---
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
                        # --- フォールバックモデル（リアルタイム用） ---
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
                            # --- フォールバック失敗時のリトライ判断 ---
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
                        # --- フォールバック無しでの再試行判断 ---
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
                    # --- 空応答時は再試行 ---
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
                    parsed = json.loads(ai_response)
                except json.JSONDecodeError:
                    # --- JSON 解析失敗時の扱い ---
                    last_error_reason = "json_decode_error"
                    logger.warning("AI応答JSON不正 raw=%r", ai_response[:120])
                    raise HTTPException(status_code=502, detail="AI応答形式不正")
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=502, detail="AI応答形式不正")

                response_text = parsed.get("response")
                flag_value = parsed.get("flag")
                if not isinstance(response_text, str) or not isinstance(flag_value, bool):
                    raise HTTPException(status_code=502, detail="AI応答の型エラー")

                response_text = response_text.strip()
                if len(response_text) > 300:
                    # --- 文字数調整 ---
                    logger.warning("AI応答300文字超過のため切り詰め head=%r",
                                   response_text[:60])
                    response_text = response_text[:300]

                return RealTimeChatResponse(response=response_text, flag=flag_value)

            detail = "応答を取得できませんでした"
            if REALTIME_EXPOSE_OPENAI_REASON and last_error_reason:
                detail += f" (reason={last_error_reason})"
            raise HTTPException(status_code=503, detail=detail)
        finally:
            # --- セマフォ解放 ---
            _REALTIME_CHAT_SEMAPHORE.release()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        # --- 想定外例外のログ記録 ---
        logger.exception("Unexpected error in /chat/real-time: %r", e)
        detail = "サーバーエラーが発生しました"
        if REALTIME_EXPOSE_OPENAI_REASON:
            detail += f" (reason={type(e).__name__})"
        raise HTTPException(status_code=500, detail=detail)


WEATHER_KEYWORDS = ("天気", "気温", "気候", "雨", "晴れ",
                    "曇り", "雪", "雷", "湿度", "風速", "天候")


def _should_request_weather(message: str) -> bool:
    # --- メッセージ内に天気関連キーワードが含まれるか判定 ---
    text = message or ""
    return any(keyword in text for keyword in WEATHER_KEYWORDS)


def _safe_parse_json_response(raw: str) -> Dict[str, Any]:
    # --- モデル出力から安全に JSON オブジェクトを抽出 ---
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            trimmed = raw[start:end + 1]
            try:
                return json.loads(trimmed)
            except json.JSONDecodeError:
                logger.warning("AI応答JSON切り出し後も不正 raw=%r", trimmed[:120])
        raise exc
