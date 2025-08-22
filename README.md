# Soily API

家庭菜園アシスタント「ソイリィ」の FastAPI ベース API。

## バージョン (v1.0)

- /chat: 一時的外部エラーを 503 / 429 と区別（従来は 500 集約）
- /trivia: 緯度/経度 + 方角 + 設置場所 + 当日天気 (web_search_preview) を活用
- /trivia 応答: 20 文字以内（超過時トリム）リトライ + バックオフ
- OpenAI 呼び出し: タイムアウト + セマフォ + 軽量リトライ
- PII(個人情報) フラグ検出: プロンプト内ルール + サーバ側 JSON 構造検証

## エンドポイント概要

| メソッド | パス    | 説明                                 |
| -------- | ------- | ------------------------------------ |
| POST     | /chat   | 一問一答チャット（JSON 構造出力）    |
| POST     | /trivia | 地域・天気・月を加味した短文トリビア |
| GET      | /       | 簡易フロント (開発用)                |
| GET      | /docs   | OpenAPI (Swagger UI)                 |

---

## /chat

Request:

```json
{ "message": "トマトの脇芽かきは？" }
```

Validation:

- 1〜1000 文字 (Pydantic: ChatRequest)

Response:

```json
{ "response": "～だよ", "flag": false }
```

フィールド:

- response: ソイリィ口調で最大 300 文字（超過時サーバ側で切り詰め）
- flag: 個人情報（氏名、住所（市区町村以下の番地等）、電話番号、メールアドレス、生年月日（具体的日付）、クレジットカード/銀行口座/マイナンバー等の識別番号、ログイン ID、正確な位置情報、その他一意に個人を特定し得る情報。）検出時 true

内部制御:

- OpenAI Responses API + JSON Schema 強制
- JSON パース失敗/スキーマ逸脱 → 502
- 空応答 / リトライ枯渇 → 503

PII 判定方針（プロンプト内定義抜粋）:

- 個人情報らしき入力が含まれる場合: flag=true, 内容の再掲は避け注意喚起文を末尾付与

---

## /trivia

Request:

```json
{
  "latitude": "35.6895",
  "longitude": "139.6917",
  "direction": "南向き",
  "location": "ベランダ"
}
```

Validation:

- latitude: 数値文字列 / -90〜90
- longitude: 数値文字列 / -180〜180
- direction: 1〜20 文字
- location: "ベランダ" or "庭"

処理フロー:

1. 緯度経度 → 都市 / 当日天気 (web_search_preview, JSON Schema, タイムアウト)
2. 月を取得（サーバローカル）
3. 情報を instructions + payload に埋め込み生成
4. 20 文字以内になるまで最大 `TRIVIA_MAX_ATTEMPTS` 回生成
5. 未達なら先頭 20 文字へ切り詰め（WARN ログ）

Response:

```json
{ "response": "東京は◯◯だよ" }
```

---

## エラーポリシー

| ステータス | 意味                                                           |
| ---------- | -------------------------------------------------------------- |
| 400 / 422  | 入力バリデーション失敗                                         |
| 429        | 同時実行枠取得不可（2 秒待機タイムアウト） / 外部 429 最終結果 |
| 502        | AI 応答形式エラー（JSON 解析失敗 / 必須キー欠落 / 型不一致）   |
| 503        | 外部サービス一時的混雑 / 応答空で利用不能                      |
| 500        | 想定外例外（スタックはサーバログのみ）                         |

リトライ:

- /chat: 429/5xx (一部) + タイムアウト → 再試行（回数: `CHAT_MAX_ATTEMPTS`）
- /trivia: 文字数条件未達 or タイムアウト/例外 → 再試行（回数: `TRIVIA_MAX_ATTEMPTS`）

---

## 同時実行と耐性

| 項目            | /chat                        | /trivia                        |
| --------------- | ---------------------------- | ------------------------------ |
| セマフォ上限    | `CHAT_CONCURRENCY` (既定 15) | `TRIVIA_CONCURRENCY` (既定 10) |
| 外部呼び出し TO | `CHAT_OPENAI_TIMEOUT`        | `TRIVIA_OPENAI_TIMEOUT`        |
| リトライ回数    | `CHAT_MAX_ATTEMPTS`          | `TRIVIA_MAX_ATTEMPTS`          |

finally 解放でリーク防止。

---

## 文字数制約

| エンドポイント | 制約            | 実装方法                       |
| -------------- | --------------- | ------------------------------ |
| /chat          | 300 文字目安    | JSON Schema + サーバ最終トリム |
| /trivia        | 20 文字以内必須 | 生成ループで検査 + 最終トリム  |

---

## ディレクトリ構成

```
project_root/
├─ app/
│  ├─ main.py
│  ├─ models.py
│  ├─ prompts/
│  │   └─ soylly.py
│  ├─ routers/
│  │   ├─ chat.py
│  │   └─ trivia.py
│  ├─ services/
│  │   └─ openai_client.py
│  └─ templates/
│      └─ index.html
├─ requirements.txt
├─ README.md
└─ .env (開発用、Git管理除外推奨)
```

---

## 環境変数

| 変数                   | 既定   | 用途                                    |
| ---------------------- | ------ | --------------------------------------- |
| OPENAI_API_KEY         | (必須) | OpenAI API キー                         |
| CHAT_CONCURRENCY       | 15     | /chat 同時実行上限                      |
| TRIVIA_CONCURRENCY     | 10     | /trivia 同時実行上限                    |
| CHAT_OPENAI_TIMEOUT    | 8.0    | /chat 外部 1 リクエストタイムアウト(秒) |
| TRIVIA_OPENAI_TIMEOUT  | 8.0    | /trivia 各生成タイムアウト(秒)          |
| TRIVIA_WEATHER_TIMEOUT | 10.0   | 天気取得タイムアウト(秒)                |
| CHAT_MAX_ATTEMPTS      | 2      | /chat 再試行上限                        |
| TRIVIA_MAX_ATTEMPTS    | 5      | /trivia 文字数達成までの上限            |

---

## セットアップ (開発)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.sample .env  # なければ手動作成
# .env に OPENAI_API_KEY=sk-xxxx を記載
uvicorn app.main:app --reload --reload-exclude '.venv/*'
```

アクセス: http://127.0.0.1:8000/ /docs

---

## 動作確認例

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"土の水はけ改善法は？"}'

curl -s -X POST http://127.0.0.1:8000/trivia \
  -H "Content-Type: application/json" \
  -d '{"latitude":"35.6895","longitude":"139.6917","direction":"南向き","location":"ベランダ"}'
```

---

## マージ時（クライアント統合）のポイント

1. CORS
   - 現在は allow_origins=["*"]。本番統合時はフロントドメインへ限定推奨。
2. レート制御 / 429 表示
   - フロント側で 429 / 503 をユーザー向け再試行ガイダンス文にマッピングする UI を用意。
3. モデル切替余地
   - 現状モデル名ハードコード (gpt-4o-mini)。将来差し替え時は環境変数化を提案。
4. タイムアウト調整
   - 観測: 体感遅延が多い場合 `CHAT_OPENAI_TIMEOUT` を 8→10 へ増やすより、まず `CHAT_CONCURRENCY` 過大設定を見直す。

---

## セキュリティ/運用最低限

| 項目             | 現状              | コメント                              |
| ---------------- | ----------------- | ------------------------------------- |
| API Key 露出防止 | .env              | リポジトリ未コミット徹底              |
| 入力検証         | Pydantic          | 型/範囲 OK                            |
| PII 制御         | プロンプト + flag | 高精度保証ではない (将来ルール追加可) |
| ログ             | WARN/ERROR 中心   | 応答全量長期保存なし                  |
| HTTPS            | インフラ層依存    | リバースプロキシ(Nginx/ALB 等) 推奨   |

---

## 今後の拡張候補（任意）

- モデル名/URL の設定値化
- ファインチューニング(SFT)

---

## ファインチューニング(SFT) 概要（参考）

目的:

- ユーザーからの回答評価で品質向上を目指しユーザーの満足度を高める。

データ JSONL (例):

```
{"messages":[{"role":"user","context":"プランターの土が固い 改善法は？"},{"role":"assistant","content":"腐葉土とパーライト混ぜてふかふかにしよう！"}]}
{"messages":[{"role":"user","context":"夏レタスの徒長を抑えるには？"},{"role":"assistant","content":"早朝だけ直射、日中は遮光ネットで徒長抑制だよ"}]}
```

---
