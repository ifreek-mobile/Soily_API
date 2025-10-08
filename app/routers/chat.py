from fastapi import APIRouter, HTTPException, Body
import json
import logging
import asyncio
import os
from app.models import ChatRequest, ChatResponse
from app.services.openai_client import client
from app.prompts.soylly import SOYLY_PROMPT
from app.prompts.katakana_examples import KATAKANA_VEGETABLE_EXAMPLES  # 追加

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# 同時実行・外部API制御（環境変数で調整可能）
# - CHAT_CONCURRENCY: プロセス内で同時に処理する最大リクエスト数
# - CHAT_OPENAI_TIMEOUT: OpenAI 呼び出し1回あたりのタイムアウト秒
# - CHAT_MAX_ATTEMPTS: タイムアウト/空応答時の再試行回数
# - CHAT_FALLBACK_MODEL: プライマリモデル失敗時に利用するフォールバックモデル
# - EXPOSE_OPENAI_REASON: エラー応答に原因(reason)を含めるか
CHAT_CONCURRENCY = int(os.getenv("CHAT_CONCURRENCY", "15"))
_CHAT_SEMAPHORE = asyncio.Semaphore(CHAT_CONCURRENCY)
CHAT_OPENAI_TIMEOUT = float(os.getenv("CHAT_OPENAI_TIMEOUT", "8.0"))
CHAT_MAX_ATTEMPTS = int(os.getenv("CHAT_MAX_ATTEMPTS", "2"))
CHAT_FALLBACK_MODEL = os.getenv("CHAT_FALLBACK_MODEL", "gpt-4o")
# 開発デフォルトは有効化。本番運用では 0 に設定して詳細を隠蔽することを推奨
EXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "1") == "1" # 本番ではEXPOSE_OPENAI_REASON = os.getenv("EXPOSE_OPENAI_REASON", "0") == "1"
# 一時的障害とみなして再試行対象にするステータスコード
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


@router.post("/chat", response_model=ChatResponse, summary="チャット応答", description="ユーザーからのメッセージを受け取り、AI（ソイリィ）が応答を返します。")
async def chat(request: ChatRequest = Body(..., description="ユーザーからのメッセージ")):
    try:
        # スパイク吸収：セマフォを2秒だけ待機。取れない場合は 429 を返し、行列肥大化を防止。
        try:
            await asyncio.wait_for(_CHAT_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # 外部API呼び出し：軽いリトライ＋タイムアウト付き
            # - 一時的な混雑/遅延に備えて attempt ごとに短いバックオフを挟む
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
                    last_error_reason = "timeout"
                    logger.warning(
                        "/chat OpenAI タイムアウト attempt=%d", attempt + 1)
                    await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                    continue
                except Exception as e:
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
                            detail = "外部サービスが混雑しています。時間をおいて再度お試しください。"
                            if EXPOSE_OPENAI_REASON:
                                detail += f" (reason={last_error_reason})"
                            raise HTTPException(status_code=503, detail=detail)
                    if status in RETRY_STATUS_CODES and fallback_resp is None:
                        if attempt + 1 < CHAT_MAX_ATTEMPTS:
                            logger.warning(
                                "/chat OpenAI 一時エラー status=%s attempt=%d: %r", status, attempt + 1, e)
                            await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                            continue
                        if status == 429:
                            raise HTTPException(
                                status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                        detail = "外部サービスが混雑しています。時間をおいて再度お試しください。"
                        if EXPOSE_OPENAI_REASON:
                            detail += f" (reason={last_error_reason or 'retry_exhausted'})"
                        raise HTTPException(statusコード=503, detail=detail)
                    raise
                ai_response = (
                    getattr(resp, "output_text", None) or "").strip()
                if not ai_response:
                    last_error_reason = last_error_reason or "empty_output"
                    logger.warning("chat empty output attempt=%d", attempt)
                    if attempt < CHAT_MAX_ATTEMPTS:
                        await asyncio.sleep(min(0.2 * attempt, 1.0))
                        continue
                    raise HTTPException(
                        status_code=503,
                        detail=("外部サービスが混雑しています。時間をおいて再度お試しください。"
                                + (f" (reason={last_error_reason})" if EXPOSE_OPENAI_REASON else "")),
                    )
                try:
                    parsed = json.loads(ai_response)
                except json.JSONDecodeError:
                    last_error_reason = "json_decode_error"
                    logger.warning("AI応答JSON不正 raw=%r", ai_response[:80])
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
                    logger.warning("AI応答300文字超過のため切り詰め head=%r",
                                   response_text[:60])
                    response_text = response_text[:300]

                return ChatResponse(response=response_text, flag=flag_value)
        finally:
            # 例外の有無に関わらず必ず解放（リーク防止）
            _CHAT_SEMAPHORE.release()

        # 全試行で有効な応答(JSON+必須キー+型)を取得できなかった場合
        detail = "応答を取得できませんでした"
        if EXPOSE_OPENAI_REASON and last_error_reason:
            detail += f" (reason={last_error_reason})"
        raise HTTPException(status_code=503, detail=detail)
    except HTTPException:
        # 意図的なHTTPエラーはそのままクライアントへ
        raise
    except Exception as e:
        # 想定外は 500 に集約し、詳細はログへ
        logger.exception("Unexpected error in /chat: %r", e)
        detail = "サーバーエラーが発生しました"
        if EXPOSE_OPENAI_REASON:
            detail += f" (reason={type(e).__name__})"
        raise HTTPException(status_code=500, detail=detail)
