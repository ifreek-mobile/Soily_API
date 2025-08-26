# Soily API

家庭菜園アシスタント「ソイリィ」 FastAPI ベース API。

## バージョン (v1.0)

- /chat: 一時的外部エラーを 503 / 429 と区別（従来 500 集約を改善）
- /chat: AI 応答 JSON Schema 強制 + 不正時 502、300 文字トリム
- /trivia: 緯度/経度 + 方角 + 設置場所 + 月 + (取得成功時) 天気 を反映
- /trivia: 20 文字以内制約 + 文字数未達再生成 + 最終トリム
- 両エンドポイント: asyncio.Semaphore + タイムアウト + 軽量リトライ

---

## エンドポイント概要

| メソッド | パス    | 説明                                 |
| -------- | ------- | ------------------------------------ |
| POST     | /chat   | 一問一答チャット（JSON 構造出力）    |
| POST     | /trivia | 地域・天気・月を加味した短文トリビア |
| GET      | /       | 簡易フロント (開発用 / 無い場合 404) |
| GET      | /docs   | OpenAPI (Swagger UI)                 |

---

## /chat

Request 例:

```json
{ "message": "トマトの脇芽かきは？" }
```

Validation: 1〜1000 文字 (Pydantic)

Response 例:

```json
{ "response": "◯◯だよ", "flag": false }
```

仕様要点:

- JSON Schema (response:str<=300, flag:bool) をモデルに要求
- 不正 JSON / 必須キー欠落 / 型不一致: 502
- タイムアウト/一時 5xx/429: 所定回数リトライ (`CHAT_MAX_ATTEMPTS`)
- 成功応答 300 文字超過はサーバ最終トリム
- 全試行失敗: 503

PII フラグについて:

- 現状: モデル出力の flag 値をそのまま返却
- 未実装: サーバ側追加正規表現/辞書照合による上書き検知
- 今後: 実装追加時に README / テスト (skip 解除) 更新予定

---

## /trivia

Request 例:

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

1. 緯度経度 → 都市/天気 (web_search_preview, JSON Schema, タイムアウト許容/失敗フォールバック)
2. 月番号取得
3. 付加情報 (city, weather, direction, location, month) を埋め込み生成
4. 20 文字以内になるまで最大 `TRIVIA_MAX_ATTEMPTS` 回再生成
5. 未達なら先頭 20 文字を最終トリム (WARN ログ)
6. 空応答継続時は 503

Response 例:

```json
{ "response": "東京晴れ甘味増すよ" }
```

---

## エラーポリシー

| ステータス | 意味                                                        |
| ---------- | ----------------------------------------------------------- |
| 400 / 422  | 入力バリデーション失敗                                      |
| 429        | セマフォ取得不能 (2 秒待ちタイムアウト) / 外部 429 最終     |
| 502        | AI 応答形式エラー (JSON 解析失敗 / 必須キー欠落 / 型不一致) |
| 503        | 外部サービス一時的混雑 / 再試行枯渇 / 応答空                |
| 500        | 想定外例外 (内部バグ)                                       |

リトライ方針:

- /chat: 429, 500, 502, 503, 504, Timeout を対象 (設定: `CHAT_MAX_ATTEMPTS`)
- /trivia: 文字数超過 / Timeout / 一時エラーで再生成 (`TRIVIA_MAX_ATTEMPTS`)

---

## 同時実行・耐性

| 項目            | /chat                      | /trivia                      |
| --------------- | -------------------------- | ---------------------------- |
| セマフォ上限    | `CHAT_CONCURRENCY` (15)    | `TRIVIA_CONCURRENCY` (10)    |
| 外部呼び出し TO | `CHAT_OPENAI_TIMEOUT` (秒) | `TRIVIA_OPENAI_TIMEOUT` (秒) |
| リトライ回数    | `CHAT_MAX_ATTEMPTS`        | `TRIVIA_MAX_ATTEMPTS`        |
| 天気取得 TO     | ー                         | `TRIVIA_WEATHER_TIMEOUT`     |

finally ブロックで必ず release しリーク防止。

---

## 文字数制約

| エンドポイント | 制約            | 実装方法                 |
| -------------- | --------------- | ------------------------ |
| /chat          | 300 文字上限    | JSON Schema + 最終トリム |
| /trivia        | 20 文字以内必須 | 生成ループ + 最終トリム  |

---

## ディレクトリ構成 (抜粋)

```
project_root/
├─ app/
│  ├─ main.py
│  ├─ models.py
│  ├─ prompts/soylly.py
│  ├─ routers/{chat.py,trivia.py}
│  ├─ services/openai_client.py
│  └─ templates/index.html (存在すれば / 提供)
├─ tests/ (unit/integration/e2e)
├─ requirements.txt
├─ README.md
└─ .env (Git管理除外推奨)
```

---

## 環境変数

| 変数                   | 既定 | 用途                                       |
| ---------------------- | ---- | ------------------------------------------ |
| OPENAI_API_KEY         | 必須 | OpenAI API キー                            |
| CHAT_CONCURRENCY       | 15   | /chat 同時実行上限                         |
| TRIVIA_CONCURRENCY     | 10   | /trivia 同時実行上限                       |
| CHAT_OPENAI_TIMEOUT    | 8.0  | /chat 外部 1 呼び出しタイムアウト (秒)     |
| TRIVIA_OPENAI_TIMEOUT  | 8.0  | /trivia 生成タイムアウト (秒)              |
| TRIVIA_WEATHER_TIMEOUT | 10.0 | 天気取得タイムアウト (秒)                  |
| CHAT_MAX_ATTEMPTS      | 2    | /chat 再試行上限                           |
| TRIVIA_MAX_ATTEMPTS    | 5    | /trivia 再生成上限                         |
| E2E_PORT               | 8800 | e2e テスト用サーバポート                   |
| E2E_EXTERNAL           | (無) | 1= 既存起動サーバを流用 (e2e フィクスチャ) |

---

## セットアップ (開発)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.sample .env  # なければ新規作成
# .env に OPENAI_API_KEY=sk-xxx を設定
uvicorn app.main:app --reload --reload-exclude '.venv/*'
```

アクセス: http://127.0.0.1:8000 /docs

---

## 動作確認例

```bash
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"土の水はけ改善法は？"}' | jq .

curl -s -X POST http://127.0.0.1:8000/trivia \
  -H 'Content-Type: application/json' \
  -d '{"latitude":"35.6895","longitude":"139.6917","direction":"南向き","location":"ベランダ"}' | jq .
```

---

## テスト戦略概要

| 種類        | 実行方法                 | 目的                               | 例                 |
| ----------- | ------------------------ | ---------------------------------- | ------------------ |
| unit (少)   | `pytest -m unit`         | 純粋関数/軽量検証 (最小)           | text 処理 (将来)   |
| integration | `pytest -m integration`  | FastAPI 内部ルータ/再試行/検証     | chat/trivia 異常系 |
| e2e         | `pytest -m e2e`          | サーバ実起動 + 契約(HTTP) 黒箱確認 | OpenAPI, CORS      |
| smoke 負荷  | `pytest -m e2e -k smoke` | 軽並行で 5xx 無し/429 制御確認     | smoke_parallel     |

主なカバレッジ:

- /chat: JSON 不正 →502, 500/429 リトライ成功/失敗, 全タイムアウト →503, 300 文字トリム, 環境変数上書き
- /trivia: 天気フェーズ例外フォールバック, 20 文字制約, バリデーション境界値, 空応答 503
- CORS / OpenAPI / ヘルス (簡易) / 並行 429 部分成功

---

## ファインチューニング(SFT) 概要（参考）

目的: 応答品質向上。収集データを JSONL で蓄積 → SFT → 再デプロイ。

サンプル:

```
{"messages":[{"role":"user","context":"プランターの土が固い 改善法は？"},{"role":"assistant","content":"腐葉土とパーライト混ぜてふかふかにしよう！"}]}
{"messages":[{"role":"user","context":"夏レタスの徒長を抑えるには？"},{"role":"assistant","content":"早朝だけ直射、日中は遮光ネットで徒長抑制だよ"}]}
```

---
