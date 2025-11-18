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
from app.prompts.Output_limit import OUTPUT_LIMIT_EXAMPLES
from app.services.geocode import resolve_pref_city

# --- FastAPI ルータとロガーの初期化。アプリ全体で共通利用する ---
router = APIRouter()
logger = logging.getLogger("uvicorn.error")

#! デバッグ出力のオン/オフ（True にするとリクエスト/位置情報などをログ出力）
REALTIME_DEBUG_ENABLED = False
#! トークンコスト計算ログのオン/オフ（True で課金見積りをログ出力）
REALTIME_COST_DEBUG_ENABLED = False

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
# ↑ 同時実行数/タイムアウト/リトライ回数/フォールバック設定を一括して読み込み

MODEL_PRICING_USD = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o": {"input": 0.00500, "output": 0.01500},
}
USD_TO_JPY = 150.0
# ↑ OpenAIの課金計算で使用する単価表と為替レート。REALTIME_COST_DEBUG_ENABLED=True 時に参照される

# OpenAI 応答形式（JSON Schema）は使い回す
JSON_RESPONSE_FORMAT: Dict[str, Any] = {
    "format": {
        "type": "json_schema",
        "name": "RealTimeChatResponse",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["response", "flag"],
            "properties": {
                "response": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": "AIの応答",
                },
                "flag": {
                    "type": "boolean",
                    "description": "個人情報が含まれているかどうか",
                },
            },
        },
    }
}
# ↑ Responses API へ毎回同じ schema を渡すことで出力形式を強制し、パース処理を単純化

# 挨拶候補はループごとに都度生成しない
GREETING_SAMPLES = (
    "今日も一緒に頑張ろうね！",
    "最近の栽培の調子はどうかな？",
    "一日の終わりにお疲れさま！",
    "調子はどうかな？今日も少しずつ進めようね！",
    "質問ありがとう！君の家庭菜園をサポートできて嬉しいよ！",
    "素晴らしい質問だね！一緒に解決策を考えよう！",
)
# ↑ モデルが参照するサンプル挨拶のテンプレ。payload 内 constraints/greeting_samples で共有


def _build_user_payload(
    request: RealTimeChatRequest,
    *,
    prefecture: str | None,
    city: str | None,
    current_time_iso: str,
    weather_requested: bool,
) -> Dict[str, Any]:
    """OpenAI へ渡すペイロードを一元的に構築"""
    # ユーザー固有情報・位置情報・制約などを一つの辞書にまとめ、モデル入力を標準化
    return {
        "username": request.username,
        "user_message": request.message,
        "context": {
            "prefecture": prefecture,
            "city": city,
            "direction": request.direction,
            "location": request.location,
            "current_time": current_time_iso,
            "vegetable": request.vegetable,
            "quest_progress": request.quest_progress,
        },
        "weather_requested": weather_requested,
        # constraints や examples はモデル挙動を固定化するための追加コンテキスト
        "constraints": [
            "冒頭は「{username}さん、{挨拶}、{寄り添い文章}」の形式にする。挨拶文章構成は「ユーザーの質問に対する助けになる言葉」を含めた構成にすること",
            "「寄り添い文章」は greeting_samples から時間帯や文脈に最適なものを選ぶこと",
            "野菜名は必ずカタカナ表記で統一する（入力がひらがな/漢字でも変換）",
            "JSONのみを返す（response, flag）",
            "weather_requested が true のときは web_search を活用し、最新の天気情報を回答に反映する",
            "weather_requested が false のときは web search を使用せず通常回答を行う",
            "最高気温や最低気温、降水量などの数値などの明言の回答を控えること",
            "current_time を基準に時間表現（今、◯時間後、明日など）を解釈し、矛盾のない回答を返す",
            "絶対に具体的な住所の情報を出力しないこと",
            "回答内に URL や参照リンク（例: (weather.com)/[名称](https://...)）を含めないこと",
            "「Markdownを使わず平文で」「郵便番号や番地を出さない」「天気情報時間帯別の出力は求められない限り不要とする」",
            "vegetable は現在育てている野菜名を表すが、質問が別の野菜に関する場合は無理にこの野菜を推さず、質問意図に沿った品種を話をすること",
            "quest_progress は家庭菜園の進捗を示す。対応する助言が求められたときのみ活用し、無関係な場面では言及しない",
            OUTPUT_LIMIT_EXAMPLES.strip(),
        ],
        "examples": KATAKANA_VEGETABLE_EXAMPLES.strip(),
        "prohibited_responses": OUTPUT_LIMIT_EXAMPLES.strip(),
        "greeting_samples": GREETING_SAMPLES,
    }


def _should_suppress_weather(
    weather_requested: bool,
    *,
    latitude: float | None,
    longitude: float | None,
    prefecture: str | None,
    city: str | None,
) -> bool:
    """位置情報が不足している場合に天気検索を抑制"""
    # 天気質問でも緯度経度や行政区が欠ける場合は web_search を止める
    if not weather_requested:
        return False
    if latitude is None or longitude is None:
        return True
    return not (prefecture or city)


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
        # ここから先はセマフォ獲得済みのため、処理終了時に必ず release する
        try:
            ai_response = ""
            last_error_reason = ""
            for attempt in range(REALTIME_CHAT_MAX_ATTEMPTS):
                # --- 現在時刻 (JST) を取得し payload に含める ---
                current_time_iso = datetime.now(
                    timezone(timedelta(hours=9))
                ).isoformat()
                # ↑ 毎ループで現在時刻を取り直し、遅延時にも最新タイムスタンプを提供

                weather_requested = _should_request_weather(request.message)
                # ↑ ユーザー質問から天気関連フラグを算出し、ツール利用の可否を判断

                latitude_str = str(
                    request.latitude) if request.latitude is not None else None
                longitude_str = str(
                    request.longitude) if request.longitude is not None else None
                prefecture, city = await resolve_pref_city(latitude_str, longitude_str)
                # ↑ 緯度経度→都道府県/市区町村を逆ジオ。None のままの場合は suppress 判定で止める

                weather_requested_initial = weather_requested
                # ↑ 抑制前後の差分をログ出しするために初期値を保持

                if _should_suppress_weather(
                    weather_requested,
                    latitude=request.latitude,
                    longitude=request.longitude,
                    prefecture=prefecture,
                    city=city,
                ):
                    logger.info(
                        "天気リクエストを抑制（位置情報が不足しているため） username=%s",
                        request.username,
                    )
                    weather_requested = False
                # ↑ 位置情報が欠ける場合は web_search を強制オフ。APIコスト/誤情報を抑える

                _debug_log(
                    "request username=%s lat=%s lon=%s prefecture=%s city=%s vegetable=%s quest_progress=%s weather_requested_initial=%s weather_requested=%s",
                    request.username,
                    request.latitude,
                    request.longitude,
                    prefecture,
                    city,
                    request.vegetable,
                    request.quest_progress,
                    weather_requested_initial,
                    weather_requested,
                )
                # ↑ デバッグフラグが True のときのみ詳細ログを吐き、運用時のトレース性を確保

                payload = _build_user_payload(
                    request,
                    prefecture=prefecture,
                    city=city,
                    current_time_iso=current_time_iso,
                    weather_requested=weather_requested,
                )
                # ↑ ここで OpenAI へ渡す入力を作成。実際の API 呼び出しは下の openai_kwargs で制御

                openai_kwargs: Dict[str, Any] = {
                    "model": "gpt-4o-mini",
                    "instructions": SOYLY_PROMPT,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "text": JSON_RESPONSE_FORMAT,
                }
                model_used = openai_kwargs["model"]
                if weather_requested:
                    # --- 天気系質問の場合のみ web_search ツールを付与 ---
                    openai_kwargs["tools"] = REALTIME_OPENAI_TOOLS
                    openai_kwargs["tool_choice"] = "auto"
                else:
                    # --- 通常質問ではツールを無効のまま使用 ---
                    pass
                # ↑ tool_choice を必要時のみ有効化し、OpenAI 側の無駄な web_search 呼び出しを抑制

                try:
                    # --- OpenAI Responses API を呼び出し、タイムアウトを監視 ---
                    resp = await asyncio.wait_for(
                        client.responses.create(**openai_kwargs),
                        timeout=REALTIME_CHAT_OPENAI_TIMEOUT,
                    )
                    # ↑ asyncio.wait_for で API 応答を監視。timeout は環境変数で調整可能

                    if REALTIME_COST_DEBUG_ENABLED:
                        usage = getattr(resp, "usage", None)
                        if usage:
                            usage_dict = usage if isinstance(
                                usage, dict) else getattr(usage, "__dict__", {})
                            # --- トークン数に応じた概算コストをデバッグ用に記録 ---
                            _log_usage_cost(model_used, usage_dict)
                    # ↑ usage が存在する場合のみ課金ログを出力。True/False 切り替えでノイズを制御
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
                    # status がない場合でも response.status_code から拾う
                    if status is None:
                        status = getattr(
                            getattr(e, "response", None), "status_code", None)
                    # ↑ HTTPException でない例外にも status_code が潜んでいる場合があるため、多段で取得

                    err_msg = str(e)
                    if any(token in err_msg.lower() for token in ("api key", "unauthorized", "authentication")):
                        # ↑ 認証系の失敗はリトライしても改善しないので即 401 を返す
                        logger.error(
                            "/chat/real-time OpenAI 認証エラー: %s", err_msg)
                        raise HTTPException(
                            status_code=401, detail="OpenAI APIキーが無効または読み込めていません。")
                    fallback_resp = None
                    if status in RETRY_STATUS_CODES and REALTIME_CHAT_FALLBACK_MODEL and REALTIME_CHAT_FALLBACK_MODEL != "gpt-4o-mini":
                        # --- フォールバックモデルによる再試行 ---
                        # フォールバック成功時は resp を置き換えて処理継続
                        logger.warning("/chat/real-time fallback を試行 model=%s status=%s attempt=%d",
                                       REALTIME_CHAT_FALLBACK_MODEL, status, attempt + 1)
                        try:
                            fallback_resp = await asyncio.wait_for(
                                client.responses.create(
                                    model=REALTIME_CHAT_FALLBACK_MODEL,
                                    instructions=SOYLY_PROMPT,
                                    input=json.dumps(
                                        payload, ensure_ascii=False),
                                    text=JSON_RESPONSE_FORMAT,
                                ),
                                timeout=REALTIME_CHAT_OPENAI_TIMEOUT + 2.0,
                            )
                            resp = fallback_resp
                            model_used = REALTIME_CHAT_FALLBACK_MODEL
                            last_error_reason = f"fallback({REALTIME_CHAT_FALLBACK_MODEL})"
                            logger.info(
                                "/chat/real-time fallback 成功 model=%s attempt=%d", REALTIME_CHAT_FALLBACK_MODEL, attempt + 1)
                            if REALTIME_COST_DEBUG_ENABLED:
                                usage = getattr(resp, "usage", None)
                                if usage:
                                    usage_dict = usage if isinstance(
                                        usage, dict) else getattr(usage, "__dict__", {})
                                    # --- フォールバック側の使用トークンも同じくコスト計算 ---
                                    _log_usage_cost(model_used, usage_dict)
                        except Exception as fallback_error:
                            # --- フォールバック失敗時のログとリトライ制御 ---
                            last_error_reason = type(fallback_error).__name__
                            status = getattr(
                                fallback_error, "status_code", status)
                            logger.warning(
                                "/chat/real-time fallback 失敗: %r", fallback_error)
                            # ↑ フォールバックも失敗した場合は attempt を進めて再ループさせる
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
                        # ↑ rate limit/5xx の一時的な障害はリトライで回復を狙う
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
                # ↑ output_text が空文字の場合は OpenAI 側の異常なので再試行を行う

                if not ai_response:
                    # --- 空応答は再試行対象 ---
                    last_error_reason = last_error_reason or "empty_output"
                    logger.warning(
                        "chat/real-time empty output attempt=%d", attempt)
                    # ↑ attempt カウンタは 0-index のため、ログではそのまま表示して追跡
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
                    # ↑ モデル不備を迅速に検知するため raw を頭 120 文字だけ残す
                    raise HTTPException(status_code=502, detail="AI応答形式不正")
                if not isinstance(parsed, dict):
                    raise HTTPException(status_code=502, detail="AI応答形式不正")

                response_text = parsed.get("response")
                flag_value = parsed.get("flag")
                # 天気検索した場合は flag を強制 False
                if weather_requested:
                    flag_value = False
                # ↑ 天気回答は外部ソース由来のため PII 判定を常に False に倒す（安全側に制限）

                if not isinstance(response_text, str) or not isinstance(flag_value, bool):
                    raise HTTPException(status_code=502, detail="AI応答の型エラー")
                # ↑ schema 準拠でない場合はバグ扱いとし、呼び出し元に 502 を返却

                response_text = response_text.strip()

                # --- 応答冒頭に「ユーザー名＋さん」を必ず付与し、挨拶文そのものはモデル出力に任せる ---
                greeting_prefix = f"{request.username}さん"
                if response_text:
                    if not response_text.startswith(greeting_prefix):
                        # 既存テキストの冒頭に差し込む。句読点は重複しないよう調整
                        response_text = f"{greeting_prefix}、{response_text}"
                else:
                    response_text = f"{greeting_prefix}、ご質問ありがとうございます。"
                # ↑ 万が一空文字でも最低限のレスポンスを保証し、UI 側の崩れを防ぐ

                # --- Web検索結果による Markdown リンクを除去 ---
                response_text = _MARKDOWN_LINK_RE.sub(r"\1", response_text)
                # ↑ UI 要件「URL NG」に従い、モデル出力中のリンク表現はタイトルのみ残す

                if len(response_text) > 1000:
                    # --- 1000文字を超えた場合は切り捨て ---
                    logger.warning("AI応答1000文字超過のため切り詰め head=%r",
                                   response_text[:60])
                    response_text = response_text[:1000]
                # ↑ schema の maxLength 1000 に合わせてトリミングし、DB/クライアント側と整合させる

                return RealTimeChatResponse(response=response_text, flag=flag_value)
            # ↑ ループ成功時はここで終了。失敗し続けた場合は下記 detail でエラーレスポンス

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
# ↑ _should_request_weather で利用。語彙を追加する場合はここに列挙


def _should_request_weather(message: str) -> bool:
    # --- メッセージ内に天気の語句が含まれているかを判定 ---
    text = message or ""
    return any(keyword in text for keyword in WEATHER_KEYWORDS)
    # ↑ 大文字小文字の区別は不要なため単純な in 判定で十分


def _safe_parse_json_response(raw: str) -> Dict[str, Any]:
    # --- モデル出力の先頭/末尾に余計な文字があっても JSON を抽出 ---
    def _normalize(s: str) -> str:
        return s.replace("\n", "\\n")
    # ↑ JSON の中に改行コードが混ざっていてもエスケープしてから loads する
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
    # ↑ JSON が壊れている場合は呼び出し元で 502 を返し、観測可能なログを残す


def _log_usage_cost(model: str, usage: Dict[str, Any]) -> None:
    if not REALTIME_COST_DEBUG_ENABLED:
        return
    # --- OpenAIの課金表を用い、入出力トークンをJPY換算してログ出力 ---
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    pricing = MODEL_PRICING_USD.get(model)
    cost_jpy = None
    if pricing and input_tokens is not None and output_tokens is not None:
        # --- 入力／出力トークンを1000トークン単位に換算し、モデル別単価を適用 ---
        cost_usd = (
            (input_tokens / 1000.0) * pricing["input"]
            + (output_tokens / 1000.0) * pricing["output"]
        )
        # --- USD から JPY へ換算し、ログで可視化 ---
        cost_jpy = cost_usd * USD_TO_JPY
    logger.info(
        "OpenAI usage model=%s input_tokens=%s output_tokens=%s total_tokens=%s cost_jpy=%s",
        model,
        input_tokens,
        output_tokens,
        total_tokens,
        f"{cost_jpy:.4f}" if cost_jpy is not None else "N/A",
    )
    # ↑ コスト試算は本番ではノイズになり得るため、REALTIME_COST_DEBUG_ENABLED で明示制御


def _debug_log(message: str, *args) -> None:
    if REALTIME_DEBUG_ENABLED:
        logger.info("[debug] " + message, *args)
    # ↑ すべてのデバッグログはこの関数を経由。切り替えポイントを一箇所に集約
