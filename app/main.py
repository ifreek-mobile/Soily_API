from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from app.routers.chat import router as chat_router
from app.routers.trivia import router as trivia_router
from app.routers import chat_realtime  # 1. 追加
import os
from dotenv import load_dotenv
import logging
from contextlib import asynccontextmanager  # 追加

# .envファイルから環境変数を読み込む
load_dotenv()

logger = logging.getLogger("uvicorn.error")  # 位置を前へ（lifespan 内で利用）


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup 相当
    if not os.getenv("OPENAI_API_KEY"):
        logger.warning("OPENAI_API_KEY が設定されていません。チャットAPIはエラーになります。")
    yield
    # shutdown 相当（現状なし。必要ならここに後処理）

app = FastAPI(
    title="ソイリィChat Bot API",
    description="野菜の妖精「ソイリィ」と会話できるAPIです。",
    version="1.0.0",
    lifespan=lifespan,  # 追加
)

# CORS: file:// や他ポートからのアクセスも許可（資格情報は使わない）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(chat_router)
app.include_router(trivia_router)
app.include_router(chat_realtime.router)  # 新エンドポイント登録


@app.get("/", summary="フロントページ")
def serve_index():
    """templates/index.html を返す（同一オリジンでCORS不要）。"""
    index_path = Path(__file__).resolve().parent / "templates" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html が見つかりません")
    return FileResponse(index_path, media_type="text/html")
