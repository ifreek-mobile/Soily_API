from typing import Optional
from pydantic import BaseModel, Field, StrictStr, field_validator


# /chat のリクエスト
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000, description="ユーザーからの質問メッセージ（必須、1〜1000文字）")


# /chat のレスポンス
class ChatResponse(BaseModel):
    response: str = Field(..., description="AI（ソイリィ）の応答メッセージ")
    flag: bool = Field(..., description="個人情報が含まれているかどうか / ある場合はTrue、ない場合はFalse")


# /trivia のリクエスト
class TriviaRequest(BaseModel):
    latitude: StrictStr = Field(..., description="緯度（文字列だが数値に変換可能であること。範囲: -90〜90）")
    longitude: StrictStr = Field(..., description="経度（文字列だが数値に変換可能であること。範囲: -180〜180）")
    direction: StrictStr = Field(..., min_length=1, max_length=20, description="方角（例: 南向き・北向き など）")
    location: StrictStr = Field(..., description="設置場所（ベランダ or 庭）")

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v: str) -> str:
        s = v.strip()
        try:
            fv = float(s)
        except Exception:
            raise ValueError("latitude は数値に変換可能な文字列である必要があります")
        if not (-90.0 <= fv <= 90.0):
            raise ValueError("latitude は -90〜90 の範囲で指定してください")
        return s

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v: str) -> str:
        s = v.strip()
        try:
            fv = float(s)
        except Exception:
            raise ValueError("longitude は数値に変換可能な文字列である必要があります")
        if not (-180.0 <= fv <= 180.0):
            raise ValueError("longitude は -180〜180 の範囲で指定してください")
        return s

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("direction は1文字以上で指定してください")
        return s

    @field_validator("location")
    @classmethod
    def validate_location(cls, v: str) -> str:
        s = v.strip()
        allowed = {"ベランダ", "庭"}
        if s not in allowed:
            raise ValueError(f"location は {', '.join(allowed)} のいずれかを指定してください")
        return s


# /trivia のレスポンス（現行どおり）
class TriviaResponse(BaseModel):
    response: str = Field(..., description="トリビアの内容")
