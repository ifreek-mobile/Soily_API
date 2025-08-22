from fastapi import APIRouter, HTTPException, Body
import json
import logging
import asyncio
import os
from app.models import ChatRequest, ChatResponse
from app.services.openai_client import client
from app.prompts.soylly import SOYLY_PROMPT

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# 同時実行・外部API制御（環境変数で調整可能）
# - CHAT_CONCURRENCY: プロセス内で同時に処理する最大リクエスト数
# - CHAT_OPENAI_TIMEOUT: OpenAI 呼び出し1回あたりのタイムアウト秒
# - CHAT_MAX_ATTEMPTS: タイムアウト/空応答時の再試行回数
CHAT_CONCURRENCY = int(os.getenv("CHAT_CONCURRENCY", "15"))
_CHAT_SEMAPHORE = asyncio.Semaphore(CHAT_CONCURRENCY)
CHAT_OPENAI_TIMEOUT = float(os.getenv("CHAT_OPENAI_TIMEOUT", "8.0"))
CHAT_MAX_ATTEMPTS = int(os.getenv("CHAT_MAX_ATTEMPTS", "2"))
# 一時的障害とみなして再試行対象にするステータスコード
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

@router.post("/chat", response_model=ChatResponse, summary="チャット応答", description="ユーザーからのメッセージを受け取り、AI（ソイリィ）が応答を返します。")
async def chat(request: ChatRequest = Body(..., description="ユーザーからのメッセージ")):
    try:
        # スパイク吸収：セマフォを2秒だけ待機。取れない場合は 429 を返し、行列肥大化を防止。
        try:
            await asyncio.wait_for(_CHAT_SEMAPHORE.acquire(), timeout=2.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=429, detail="混雑しています。しばらくしてからお試しください。")

        try:
            # 外部API呼び出し：軽いリトライ＋タイムアウト付き
            # - 一時的な混雑/遅延に備えて attempt ごとに短いバックオフを挟む
            ai_response = ""
            for attempt in range(CHAT_MAX_ATTEMPTS):
                try:
                    # 1回の OpenAI 呼び出しをタイムアウト監視
                    resp = await asyncio.wait_for(
                        client.responses.create(
                            model="gpt-4o-mini",
                            instructions=SOYLY_PROMPT,  # 出力口調・形式の制御プロンプト
                            input=request.message,       # ユーザー入力
                            # 出力は ChatResponse スキーマを満たす JSON 文字列を期待
                            text={
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
                            },
                        ),
                        timeout=CHAT_OPENAI_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning("/chat OpenAI タイムアウト attempt=%d", attempt + 1)
                    await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))
                    continue
                except Exception as e:
                    # OpenAI SDK / HTTP系例外から status_code を抽出（存在しない場合は None）
                    status = getattr(e, "status_code", None)
                    if status is None:
                        status = getattr(getattr(e, "response", None), "status_code", None)
                    if status in RETRY_STATUS_CODES:
                        # 残り試行があればバックオフして再試行
                        if attempt + 1 < CHAT_MAX_ATTEMPTS:
                            logger.warning("/chat OpenAI 一時エラー status=%s attempt=%d: %r", status, attempt + 1, e)
                            await asyncio.sleep(min(0.3 * (attempt + 1), 1.2))
                            continue
                        # 試行枯渇：429はそのまま、その他は503で利用不可を明示
                        if status == 429:
                            raise HTTPException(status_code=429, detail="リクエストが集中しています。少し待って再度お試しください。")
                        raise HTTPException(status_code=503, detail="外部サービスが混雑しています。時間をおいて再度お試しください。")
                    # 再試行対象外は従来通り想定外扱い
                    raise
                # 応答テキスト抽出
                ai_response = (getattr(resp, "output_text", None) or "").strip()
                if ai_response:
                    break
                await asyncio.sleep(min(0.2 * (attempt + 1), 1.0))

            # 全試行で空応答 → 一時的利用不能扱い
            if not ai_response:
                raise HTTPException(status_code=503, detail="外部サービスが混雑しています。時間をおいて再度お試しください。")
        finally:
            # 例外の有無に関わらず必ず解放（リーク防止）
            _CHAT_SEMAPHORE.release()

        # --- 応答の検証・整形 ---
        try:
            parsed = json.loads(ai_response)  # 文字列 → JSON
        except json.JSONDecodeError as je:
            # 上流の応答形式が不正（プロンプト逸脱など）として 502 を返す
            logger.error("AI応答のJSON解析に失敗: %s; text=%r", je, ai_response[:500])
            raise HTTPException(status_code=502, detail="AI応答の形式エラー")

        # 必須キーの存在と最小限の構造検証
        if not isinstance(parsed, dict) or "response" not in parsed or "flag" not in parsed:
            raise HTTPException(status_code=502, detail="AI応答のキー不足")

        # 値の型チェック（response: str, flag: bool）
        response_text = parsed.get("response")
        flag_value = parsed.get("flag")
        if not isinstance(response_text, str) or not isinstance(flag_value, bool):
            raise HTTPException(status_code=502, detail="AI応答の型エラー")

        # 最終ガード：300文字上限（仕様厳守）。逸脱時は警告ログ＋安全に切り詰め。
        response_text = response_text.strip()
        if len(response_text) > 300:
            logger.warning("AI応答300文字超過のため切り詰め head=%r", response_text[:60])
            response_text = response_text[:300]

        # Pydantic による最終バリデーション（response_model）
        return ChatResponse(response=response_text, flag=flag_value)
    except HTTPException:
        # 意図的なHTTPエラーはそのままクライアントへ
        raise
    except Exception as e:
        # 想定外は 500 に集約し、詳細はログへ
        logger.exception("Unexpected error in /chat: %r", e)
        raise HTTPException(status_code=500, detail="サーバーエラーが発生しました")
