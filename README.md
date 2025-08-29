# Soily API

家庭菜園アシスタント「ソイリィ」 FastAPI ベース API。

## バージョン

v1.0

## 主な特徴

- /chat: JSON Schema 強制, 形式不正で 502, 一時的失敗は再試行, 応答 300 文字上限
- /trivia: (緯度/経度 → 都市/天気 取得) + 方角 + 設置場所 + 月 情報を統合し 20 文字以内の豆知識生成
- 共通: asyncio.Semaphore による同時実行制御, タイムアウト, リトライ, 文字数最終トリム

---

## エンドポイント一覧

| メソッド | パス    | 説明                             |
| -------- | ------- | -------------------------------- |
| POST     | /chat   | 一問一答チャット (JSON 構造出力) |
| POST     | /trivia | 栽培環境ミニ豆知識 (20 文字以内) |
| GET      | /       | 開発用簡易フロント (存在すれば)  |
| GET      | /docs   | Swagger UI                       |
| GET      | /redoc  | ReDoc (有効な場合)               |

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

| 項目             | /chat                        | /trivia                        |
| ---------------- | ---------------------------- | ------------------------------ |
| セマフォ上限     | `CHAT_CONCURRENCY` (既定 15) | `TRIVIA_CONCURRENCY` (既定 10) |
| 外部呼び出し TO  | `CHAT_OPENAI_TIMEOUT`        | `TRIVIA_OPENAI_TIMEOUT`        |
| 天気取得 TO      | -                            | `TRIVIA_WEATHER_TIMEOUT`       |
| リトライ最大回数 | `CHAT_MAX_ATTEMPTS`          | `TRIVIA_MAX_ATTEMPTS`          |

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
  │   ├─ prompts/soylly.py
  │   ├─ routers/
  │   │    ├─ chat.py
  │   │    └─ trivia.py
  │   ├─ services/openai_client.py
  │   └─ templates/index.html (任意)
  ├─ tests/ (unit / integration / e2e)
  ├─ requirements.txt
  ├─ README.md
  └─ .env (Git 管理除外)
```

---

## 環境変数

| 変数                   | 既定 | 用途                                |
| ---------------------- | ---- | ----------------------------------- |
| OPENAI_API_KEY         | 必須 | OpenAI API キー                     |
| CHAT_CONCURRENCY       | 15   | /chat 同時実行上限                  |
| TRIVIA_CONCURRENCY     | 10   | /trivia 同時実行上限                |
| CHAT_OPENAI_TIMEOUT    | 8.0  | /chat 外部呼び出しタイムアウト (秒) |
| TRIVIA_OPENAI_TIMEOUT  | 8.0  | /trivia 生成タイムアウト (秒)       |
| TRIVIA_WEATHER_TIMEOUT | 10.0 | 天気取得タイムアウト (秒)           |
| CHAT_MAX_ATTEMPTS      | 2    | /chat 最大再試行回数                |
| TRIVIA_MAX_ATTEMPTS    | 5    | /trivia 最大再生成回数              |
| E2E_PORT               | 8800 | e2e テスト用ポート                  |
| E2E_EXTERNAL           | (無) | 1= 既存起動サーバ流用               |

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

## テスト戦略概要

| 種類        | コマンド                 | 目的                         | 例                     |
| ----------- | ------------------------ | ---------------------------- | ---------------------- |
| unit        | `pytest -m unit`         | 純粋関数/軽量検証            | 文字列/補助関数 (将来) |
| integration | `pytest -m integration`  | ルータ内部/再試行/エラーパス | chat/trivia 異常系     |
| e2e         | `pytest -m e2e`          | サーバ起動+HTTP 契約         | OpenAPI, CORS          |
| smoke       | `pytest -m e2e -k smoke` | 軽並行 (429/5xx 制御確認)    | smoke_parallel         |

主なカバレッジ:

- /chat: JSON 不正 →502, リトライ成功/失敗, タイムアウト →503, 300 文字トリム
- /trivia: 天気フェーズ例外フォールバック, 20 文字制約, バリデーション境界値, 空応答 503
- 共通: CORS, OpenAPI, 部分並行での 429 挙動

---

## 今後の拡張 (予定)

- PII/NG ワード検知による flag のサーバ側上書き
- 監視/メトリクス (構造化ログ, Prometheus エンドポイント)
- 追加モデル/詳細植物ケア情報

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
