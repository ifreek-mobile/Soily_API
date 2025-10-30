# Soily API

家庭菜園アシスタント「ソイリィ」 FastAPI ベース API。

## バージョン

v1.1

## 主な特徴

- /chat: JSON Schema 強制, 形式不正で 502, 一時的失敗は再試行, 応答 300 文字上限
- /chat/real-time: 天気系質問時のみ Web Search ツールを呼び出し、位置情報を活かしたリアルタイム回答
- /trivia: (緯度/経度 → 都市/天気 取得) + 方角 + 設置場所 + 月 情報を統合し 20 文字以内の豆知識生成
- 共通: asyncio.Semaphore による同時実行制御, タイムアウト, リトライ, 文字数最終トリム

---

## エンドポイント一覧

| メソッド | パス            | 説明                                               |
| -------- | --------------- | -------------------------------------------------- |
| POST     | /chat           | 一問一答チャット (JSON 構造出力)                   |
| POST     | /chat/real-time | リアルタイム向けチャット (天気質問時は Web Search) |
| POST     | /trivia         | 栽培環境ミニ豆知識 (20 文字以内)                   |
| GET      | /               | 開発用簡易フロント (存在すれば)                    |
| GET      | /docs           | Swagger UI                                         |
| GET      | /redoc          | ReDoc (有効な場合)                                 |

---

## /chat 詳細

### リクエスト

```json
{ "message": "トマトの脇芽かきは？" }
```

- message: 1〜1000 文字

### レスポンス例

```json
{ "response": "脇芽は早めに摘むと株の栄養が主枝に集中します。", "flag": false }
```

| フィールド | 型     | 説明                                 |
| ---------- | ------ | ------------------------------------ |
| response   | string | 応答本文 (最大 300 文字, 超過トリム) |
| flag       | bool   | (将来) 個人情報等の簡易判定フラグ    |

### 処理フロー概要

1. JSON Schema (response<=300, flag:boolean) をモデルへ要求
2. 外部エラー/タイムアウト/空応答時は最大 `CHAT_MAX_ATTEMPTS` 回再試行
3. 形式不正 (JSON パース不能/キー欠落/型不一致) は 502
4. 全試行失敗で 503

(PII / NG ワード追加検知は今後拡張予定)

---

## /trivia 詳細

### リクエスト

```json
{
  "latitude": "35.6895",
  "longitude": "139.6917",
  "direction": "南向き",
  "location": "ベランダ"
}
```

| フィールド | 型     | 制約 / 備考           |
| ---------- | ------ | --------------------- |
| latitude   | string | 数値文字列, -90〜90   |
| longitude  | string | 数値文字列, -180〜180 |
| direction  | string | 1〜20 文字            |
| location   | string | "ベランダ" or "庭"    |

### レスポンス例

```json
{ "response": "東京晴れ甘味増すよ" }
```

- 20 文字以内必須: 達成しない場合再生成。最終的に超過なら先頭 20 文字にトリム。

### 処理フロー概要

1. 緯度/経度 → web_search_preview で都市/天気 (JSON Schema & タイムアウト, 失敗時は天気欠落で続行)
2. 月番号算出
3. 付加 (city, weather, direction, location, month) をプロンプトへ注入
4. 20 文字以内になるまで最大 `TRIVIA_MAX_ATTEMPTS` 回再生成
5. 空/不正継続で 503

---

## /chat/real-time (新規)

- username と message を必須で受け取り、任意の緯度/経度/方角/設置場所を添えて応答を生成
- 天気関連の質問を検知すると `web_search` を用いて最新の天気情報を検索
- それ以外の質問ではツールを使わずコスト最適なモデルで回答
- 応答形式は `{ "response": string, "flag": boolean }` を JSON Schema で強制
- セマフォ制御・リトライ・タイムアウト・300 文字超過トリムなど `/chat` と同様の堅牢性を維持

---

## /chat/real-time 詳細

### リクエスト

```json
{
  "username": "ユーザーネーム",
  "message": "今日の天気から野菜栽培のアドバイスをして",
  "latitude": "35.6895",
  "longitude": "139.6917",
  "direction": "南向き",
  "location": "ベランダ"
}
```

| フィールド | 型     | 制約 / 備考                                                      |
| ---------- | ------ | ---------------------------------------------------------------- |
| username   | string | 必須。利用者識別用。                                             |
| message    | string | 必須。チャット本文。天気関連語句が含まれると Web Search を使用。 |
| latitude   | string | 任意。緯度（数値文字列）。省略・空の場合は天気取得をスキップ。   |
| longitude  | string | 任意。経度（数値文字列）。省略・空の場合は天気取得をスキップ。   |
| direction  | string | 任意。設置面の方角メモ。                                         |
| location   | string | 任意。例: "ベランダ" / "庭" など。                               |

### レスポンス例

```json
{
  "response": "今日は晴れで、最高気温は20℃、最低気温は12℃だよ。ベランダでの栽培には最適な天気だね！...",
  "flag": false
}
```

| フィールド | 型     | 説明                                 |
| ---------- | ------ | ------------------------------------ |
| response   | string | 応答本文 (最大 300 文字, 超過トリム) |
| flag       | bool   | 個人情報を含むと True（今後拡張）    |

### 処理フロー概要

1. `_should_request_weather()` でメッセージ内の天気系キーワードを判定。
2. 判定結果が True の場合
   - `web_search` ツールを有効化し、検索結果を回答に反映。
   - 緯度経度が正しく与えられていなければ検索のみで補足。
3. 判定結果が False の場合
   - `gpt-4o-mini` でツール非使用の標準応答。
4. OpenAI Responses API の JSON Schema で `{response, flag}` のみ受理。
5. タイムアウト/空応答/一時的エラー時は最大 `REALTIME_CHAT_MAX_ATTEMPTS` 回リトライ。
6. JSON 形式不正は 502、全試行失敗は 503、セマフォ取得失敗は 429 を返却。
7. 応答文字列は 300 文字を超える場合末尾をトリム。

---

## エラーポリシー

| ステータス | 意味                                                      |
| ---------- | --------------------------------------------------------- |
| 400 / 422  | 入力バリデーション失敗                                    |
| 429        | セマフォ取得不能 (待機タイムアウト) / 上流 429 を最終判定 |
| 502        | /chat: JSON 形式エラー (パース失敗 / キー欠落 / 型不一致) |
| 503        | 再試行枯渇 / 外部混雑 / 空応答                            |
| 500        | 想定外例外 (内部バグ)                                     |

### リトライ対象

- /chat: 429, 500, 502, 503, 504, Timeout
- /trivia: 文字数超過, Timeout, 一時エラー

---

## 同時実行・タイムアウト

| 項目             | /chat                            | /chat/real-time                           | /trivia                        |
| ---------------- | -------------------------------- | ----------------------------------------- | ------------------------------ |
| セマフォ上限     | `CHAT_CONCURRENCY` (既定 30)     | `REALTIME_CHAT_CONCURRENCY` (既定 15)     | `TRIVIA_CONCURRENCY` (既定 10) |
| 外部呼び出し TO  | `CHAT_OPENAI_TIMEOUT` (既定 10s) | `REALTIME_CHAT_OPENAI_TIMEOUT` (既定 20s) | `TRIVIA_OPENAI_TIMEOUT`        |
| 天気取得 TO      | -                                | -                                         | `TRIVIA_WEATHER_TIMEOUT`       |
| リトライ最大回数 | `CHAT_MAX_ATTEMPTS`              | `REALTIME_CHAT_MAX_ATTEMPTS`              | `TRIVIA_MAX_ATTEMPTS`          |

---

## 文字数制約

| エンドポイント | 上限            | 実装手法                 |
| -------------- | --------------- | ------------------------ |
| /chat          | 300 文字        | JSON Schema + 最終トリム |
| /trivia        | 20 文字以内必須 | 生成ループ + 最終トリム  |

---

## ディレクトリ構成 (抜粋)

```
project_root/
  ├─ app/
  │   ├─ main.py
  │   ├─ models.py
  │   ├─ prompts/
  │   │    ├─ soylly.py
  │   │    └─ katakana_examples.py
  │   ├─ routers/
  │   │    ├─ chat.py
  │   │    ├─ chat_realtime.py
  │   │    └─ trivia.py
  │   ├─ services/
  │   │    ├─ openai_client.py
  │   │    └─ tools.py
  │   └─ templates/index.html (任意)
  ├─ tests/ (unit / integration / e2e)
  ├─ requirements.txt
  ├─ README.md
  └─ .env (Git 管理除外)
```

---

## 環境変数

| 変数                         | 既定            | 用途                                   |
| ---------------------------- | --------------- | -------------------------------------- |
| OPENAI_API_KEY               | 必須            | OpenAI API キー                        |
| CHAT_CONCURRENCY             | 30              | /chat 同時実行上限                     |
| TRIVIA_CONCURRENCY           | 10              | /trivia 同時実行上限                   |
| REALTIME_CHAT_CONCURRENCY    | 15              | /chat/real-time 同時実行上限           |
| CHAT_OPENAI_TIMEOUT          | 10.0            | /chat 外部呼び出しタイムアウト (秒)    |
| TRIVIA_OPENAI_TIMEOUT        | 8.0             | /trivia 生成タイムアウト (秒)          |
| TRIVIA_WEATHER_TIMEOUT       | 10.0            | /trivia 天気取得タイムアウト (秒)      |
| CHAT_MAX_ATTEMPTS            | 2               | /chat 最大再試行回数                   |
| REALTIME_CHAT_MAX_ATTEMPTS   | 2               | /chat/real-time 最大再試行回数         |
| TRIVIA_MAX_ATTEMPTS          | 5               | /trivia 最大再生成回数                 |
| CHAT_FALLBACK_MODEL          | gpt-4o          | /chat 用フォールバックモデル           |
| REALTIME_CHAT_FALLBACK_MODEL | gpt-4o          | /chat/real-time 用フォールバックモデル |
| TRIVIA_FALLBACK_MODEL        | gpt-4o          | /trivia 用フォールバックモデル         |
| EXPOSE_OPENAI_REASON         | 1 (本番 0 推奨) | エラー detail に内部理由を付与するか   |

#### エラー理由の表示制御

- `EXPOSE_OPENAI_REASON=1`（既定）: 開発・検証時に 503 などの detail に `reason=timeout` のような原因を表示
- 公開環境では `EXPOSE_OPENAI_REASON=0` とし、内部情報をクライアントへ返さない構成を推奨
- どちらの場合もログでは常に詳細が確認できます

---

## セットアップ (開発)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# .env を作成し OPENAI_API_KEY=sk-... を設定
uvicorn app.main:app --reload --reload-exclude '.venv/*'
```

アクセス: http://127.0.0.1:8000 (docs: /docs)

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

## 今後の拡張 (予定)

- ファインチューニング / chat の回答満足度向上
- 画像認識機能 / 栽培サポート、健康状態管理

---

## クローンとセットアップ (動作確認)

最小限の手順でローカル起動して 2 つのエンドポイントを確認できます。

```bash
# 1. クローン
git clone https://github.com/ifreek-mobile/Soily_API.git
cd Soily_API

# 2. 仮想環境 + 依存インストール
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 環境変数 (.env) 作成 ※実際のキーに置き換え
echo "OPENAI_API_KEY=sk-xxxxx" > .env

# 4. 起動
uvicorn app.main:app --reload

# 5. 動作確認 (別ターミナル / 同一 venv 推奨)
curl -s -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"土の水はけ改善法は？"}' | jq .

curl -s -X POST http://127.0.0.1:8000/trivia \
  -H 'Content-Type: application/json' \
  -d '{"latitude":"35.6895","longitude":"139.6917","direction":"南向き","location":"ベランダ"}' | jq .

# 6. ブラウザ
open http://127.0.0.1:8000/docs
```

---

## リクエスト サンプル (Python)

```python
import os
import requests

BASE_URL = os.getenv("SOILY_API_BASE", "http://127.0.0.1:8000")

# /chat
chat_payload = {"message": "トマトの葉が丸まる 原因は？"}
resp = requests.post(f"{BASE_URL}/chat", json=chat_payload, timeout=10)
print("/chat status:", resp.status_code)
print(resp.json())

# /trivia
trivia_payload = {
    "latitude": "35.6895",
    "longitude": "139.6917",
    "direction": "南向き",
    "location": "ベランダ"
}
resp2 = requests.post(f"{BASE_URL}/trivia", json=trivia_payload, timeout=10)
print("/trivia status:", resp2.status_code)
print(resp2.json())
```

依存インストール:

```bash
pip install requests
```

---
