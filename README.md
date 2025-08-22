# Soily API

家庭菜園アシスタント「ソイリィ」の FastAPI ベース API。

## バージョン (v1.1)

- /trivia 仕様を「野菜リスト入力」→「緯度/経度 + 方角 + 設置場所」入力へ刷新
- 緯度経度から都市名と当日の天気を `web_search_preview` で取得（失敗時フォールバック）
- 20 文字以内ワンフレーズ強制（リトライ + バックオフ）
- セマフォで同時実行制御 + タイムアウト + 軽量リトライ導入

## エンドポイント概要

| メソッド | パス    | 説明                                         |
| -------- | ------- | -------------------------------------------- |
| POST     | /chat   | 一問一答チャット（構造化 JSON 出力）         |
| POST     | /trivia | 地域・天気・月を加味した 20 文字以内トリビア |
| GET      | /       | 簡易フロント (開発用)                        |
| GET      | /docs   | OpenAPI (Swagger UI)                         |

## /chat

Request (JSON):

```json
{ "message": "トマトの脇芽かきは？" }
```

Validation: 1〜1000 文字  
Response (JSON):

```json
{ "response": "～な感じだよ", "flag": false }
```

内部: OpenAI Responses API + JSON Schema (Structured Outputs) で強制。

## /trivia

Request (JSON):

```json
{
  "latitude": "35.6895",
  "longitude": "139.6917",
  "direction": "南向き",
  "location": "ベランダ"
}
```

Validation:

- latitude: 数値文字列, -90 ～ 90
- longitude: 数値文字列, -180 ～ 180
- direction: 1 ～ 20 文字
- location: "ベランダ" / "庭"
  処理フロー:

1. 緯度経度 → 都市 / 天気 (web_search_preview, JSON Schema 指定, タイムアウト)
2. 月 (サーバローカル) 取得
3. 上記情報を instructions + payload に埋め込み生成
4. 20 文字以内でなければ最大 `TRIVIA_MAX_ATTEMPTS` リトライ（バックオフ 0.2s, 0.4s...）
5. なお超過最終結果は切り詰め（ログ警告）
   Response (JSON / Pydantic):

```json
{ "response": "東京は○○だよ" }
```

必ず 20 文字以内（切り詰め後を含む）。

## エラーポリシー

| ステータス | 意味                                            |
| ---------- | ----------------------------------------------- |
| 400 / 422  | バリデーション失敗（FastAPI 既定は 422）        |
| 429        | 同時実行枠取得不可 (2 秒待ちタイムアウト)       |
| 500        | 予期せぬ内部エラー / 上流失敗で最終的に応答無し |

タイムアウト / 一時的ネットワーク例外はリトライ（上限到達後は 500）。

## 同時実行と耐性

- asyncio.Semaphore で `/chat` `/trivia` それぞれ並列制限
- finally で必ず release（リーク防止）
- OpenAI 呼び出し: 個別タイムアウト + 軽量リトライ
- web_search_preview: 1 回のみ（生成ループ外）→ 失敗時は city/weather 空で続行

## 文字数制約 (/trivia)

- 20 文字以下判定はサーバ側で毎試行
- 成功条件満たすまでリトライ
- 最終的に超過ならログ警告し先頭 20 文字へ切り詰め

## ディレクトリ構成

```
project_root/
├─ app/
│  ├─ main.py
│  ├─ models.py            # ChatRequest / ChatResponse / TriviaRequest / TriviaResponse
│  ├─ routers/
│  │  ├─ chat.py
│  │  └─ trivia.py
│  ├─ services/
│  │  └─ openai_client.py
│  └─ templates/
│     └─ index.html
├─ web_search_test.py      # 天気取得テストスクリプト
├─ requirements.txt
├─ README.md
└─ .env (開発のみ配置)
```

## 環境変数

| 変数                   | デフォルト | 用途                             |
| ---------------------- | ---------- | -------------------------------- |
| OPENAI_API_KEY         | (必須)     | OpenAI API キー                  |
| CHAT_CONCURRENCY       | 10         | /chat 同時実行上限               |
| TRIVIA_CONCURRENCY     | 10         | /trivia 同時実行上限             |
| CHAT_OPENAI_TIMEOUT    | 8.0        | /chat 1 リクエストタイムアウト秒 |
| TRIVIA_OPENAI_TIMEOUT  | 8.0        | /trivia 生成 1 回タイムアウト    |
| TRIVIA_WEATHER_TIMEOUT | 10.0       | 天気取得タイムアウト             |
| CHAT_MAX_ATTEMPTS      | 2          | /chat リトライ上限               |
| TRIVIA_MAX_ATTEMPTS    | 5          | 20 文字達成までの上限            |

## セットアップ

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cat > .env <<'EOF'
OPENAI_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxx"
EOF
uvicorn app.main:app --reload
```

アクセス: http://127.0.0.1:8000/ /docs

## 動作確認例

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"土の水はけ改善法は？"}'

curl -s -X POST http://127.0.0.1:8000/trivia \
  -H "Content-Type: application/json" \
  -d '{"latitude":"35.6895","longitude":"139.6917","direction":"南向き","location":"ベランダ"}'
```

## 今後の拡張候補

### ファインチューニング (SFT: Supervised Fine-Tuning) 計画

目的

- ソイリィ固有表現（口調 / 20 文字内要約適合 / ドメイン知識）を向上
- 応答の一貫性と PII (個人情報) フィルタ性能の改善

データ要件

- JSONL 形式、最低 20 サンプル（推奨は数百以上で漸進）
- 1 行 = 1 会話タスク
- 各行フォーマット:

```
{"messages":[
  {"role":"user","context":"ユーザー質問テキスト"},
  {"role":"assistant","content":"理想的な回答（ソイリィ口調・制約順守）"}
]}
```

注意: user 側は key を content ではなく context として管理（内部前処理で content に rename 可能）。一貫性確保のため社内スキーマで固定。

JSONL サンプル (抜粋):

```
{"messages":[{"role":"user","context":"プランターの土が固い 改善法は？"},{"role":"assistant","content":"腐葉土とパーライト混ぜてふかふかにしよう！"}]}
{"messages":[{"role":"user","context":"夏レタスの徒長を抑えるには？"},{"role":"assistant","content":"早朝だけ直射、日中は遮光ネットで徒長抑制だよ"}]}
{"messages":[{"role":"user","context":"ミニトマトの脇芽かき頻度？"},{"role":"assistant","content":"週1で主茎基部から5cm以内をこまめに摘むと養分集中だね"}]}
...
```
