# ターミナルから最小限で動作確認できるチャットスクリプト
# 使い方:
#   単発:  python test.py トマトの保存方法は？
#   対話:  python test.py （プロンプトに入力）

import sys
import asyncio
from app.services.openai_client import client  # 既存の OpenAI クライアントを利用


async def main() -> None:
    # 引数があればそれを使用、なければプロンプト入力
    message = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else input("You> ").strip()
    if not message:
        print("メッセージを入力してください。")
        return

    # Responses APIでテキスト出力を要求（モデルは安定版を指定）
    resp = await client.responses.create(
        model="gpt-5-nano",
        instructions="日本語で簡潔に答えてください。",
        input=message,
        text={"format": {"type": "text"}},
    )
    ai_text = (getattr(resp, "output_text", None) or "").strip()
    print(f"AI> {ai_text}")


if __name__ == "__main__":
    asyncio.run(main())