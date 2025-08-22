# ソイリィ チャットボット（FastAPI + OpenAI Responses API）

野菜の妖精「ソイリィ」と一問一答で会話できる、最小構成のチャットボット API です。開発時は簡易チャット画面（index.html）を同一オリジンで配信します。

## 1. 概要

- 一問一答のチャット API（履歴保存なし）
- OpenAI Responses API（instructions + input 形式、`resp.output_text` を利用）
- /chat は「構造化出力（Structured Outputs）」で JSON を強制
  - 返却: `{ response: string, flag: boolean }`（最大 300 文字）
- /trivia はプレーンテキスト（text/plain）で返却
  - 入力の野菜リストからランダムに 1 つ選び、月情報を踏まえた短いトリビアを返す（空なら「その他」を選び全般トリビア）
- ルート `/` は `templates/index.html` を返却（ローカル確認用）
- API ドキュメントは `/docs`（Swagger UI）

## 2. ディレクトリ構成

```
project_root/
├─ app/
│  ├─ main.py               # FastAPI エントリ（CORS/ルータ登録/トップページ）
│  ├─ models.py             # Pydantic モデル（Chat/Trivia の入出力）
│  ├─ routers/
│  │  ├─ chat.py            # POST /chat（JSON構造化出力）
│  │  └─ trivia.py          # POST /trivia（text/plain 返却）
│  ├─ services/
│  │  └─ openai_client.py   # 共有 AsyncOpenAI クライアント（dotenv読込 + api_key 指定）
│  ├─ prompts/
│  │  └─ soylly.py          # ソイリィの命令書（プロンプト）
│  └─ templates/
│     └─ index.html         # 簡易フロント（同一オリジンで動作）
├─ data/
│  └─ history.csv           # 参考（未使用の場合あり）
├─ requirements.txt
├─ README.md
└─ .env                      # 開発時だけ配置（OPENAI_API_KEY を設定）
```

## 3. セットアップ

前提:

- Python 3.10 以上
- OpenAI API キー

手順:

```bash
# 仮想環境の作成と有効化（macOS/Linux）
python3 -m venv venv
source venv/bin/activate

# 依存インストール
pip install -r requirements.txt

# .env を作成し API キーを設定（開発時）
cat > .env << 'EOF'
OPENAI_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxx"
EOF
```

## 4. 起動と確認

起動:

```bash
uvicorn app.main:app --reload
```

アクセス:

- チャット UI: http://127.0.0.1:8000/
- API 仕様: http://127.0.0.1:8000/docs

curl 例:

```bash
# /chat（JSON構造化出力）
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"トマトの脇芽かきのコツは？"}'
# => {"response":"...","flag":false}

# /trivia（配列ボディ or {"vegetables":[...]} の両方対応）
curl -s -X POST http://127.0.0.1:8000/trivia \
  -H "Content-Type: application/json" \
  -d '["はつかだいこん","小松菜","ラディッシュ"]'
# => プレーンテキストが返る（例: "今月は...だよ！"）
```

## 5. API 仕様（要点）

- POST /chat
  - Request: `{ "message": "<ユーザーの質問>" }`
  - Response(JSON):
    - `response` (string, <=300): ソイリィ口調の回答
    - `flag` (boolean): 個人情報を検出したら true
- POST /trivia
  - Request(JSON): `['トマト','きゅうり']` または `{ "vegetables": ["トマト", "きゅうり"] }`
  - Response(text/plain): 短いトリビア 1〜2 文（ソイリィ口調）。入力が空でも「その他」扱いで返す。
- GET /
  - `templates/index.html` を返却（ローカル確認用）。本番で別フロントから呼び出す場合は不要。
- GET /docs
  - Swagger UI（必要に応じて本番では無効化可）

## 6. トラブルシューティング

- OpenAI の API キー関連エラー
  - 例: `openai.OpenAIError: The api_key client option must be set ...`
  - 対応: `.env` に `OPENAI_API_KEY` を設定し、サーバを再起動。プロダクションは環境変数で注入。
- VS Code で `import "dotenv" を解決できません` と出る
  - 対応: VS Code の Python インタープリタを venv に切替、`pip install -r requirements.txt` 済みか確認。
- CORS/OPTIONS のエラー
  - 開発は `allow_origins=["*"]`。本番はフロントのドメインに限定してください。