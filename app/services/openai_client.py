from openai import AsyncOpenAI
from dotenv import load_dotenv
import os

# .env から環境変数を読み込む（uvicornのモジュール読み込み順対策）
load_dotenv()

# 単一の非同期OpenAIクライアントを共有して利用する
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
