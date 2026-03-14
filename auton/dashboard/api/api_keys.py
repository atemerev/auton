"""API key management endpoints — ported from vdemo2, adapted for PostgreSQL."""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..sessions import get_user_from_request
from ..db_client import get_db
from ..db_models import ApiKey

router = APIRouter(prefix="/api/keys", tags=["api_keys"])


async def get_current_user_id(request: Request) -> str:
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return str(user.id)


class APIKeyCreate(BaseModel):
    name: Optional[str] = None
    expires_in_days: Optional[int] = None


class APIKeyResponse(BaseModel):
    id: int
    created_at: str
    name: Optional[str]
    key_prefix: str
    last_used_at: Optional[str]
    expires_at: Optional[str]
    is_active: bool


class APIKeyCreated(BaseModel):
    id: int
    api_key: str
    key_prefix: str
    name: Optional[str]
    created_at: str
    expires_at: Optional[str]
    warning: str = "Save this API key now. You won't be able to see it again!"


@router.post("", response_model=APIKeyCreated, status_code=201)
async def create_api_key(key_data: APIKeyCreate, user_id: str = Depends(get_current_user_id)):
    api_key = f"atn_{secrets.token_urlsafe(48)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    key_prefix = api_key[:12]

    expires_at = None
    if key_data.expires_in_days:
        expires_at = datetime.utcnow() + timedelta(days=key_data.expires_in_days)

    with get_db() as db:
        new_key = ApiKey(
            user_id=user_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=key_data.name,
            expires_at=expires_at,
            is_active=True,
        )
        db.add(new_key)
        db.commit()
        db.refresh(new_key)

        return {
            "id": new_key.id,
            "api_key": api_key,
            "key_prefix": key_prefix,
            "name": key_data.name,
            "created_at": str(new_key.created_at),
            "expires_at": str(expires_at) if expires_at else None,
        }


@router.get("", response_model=List[APIKeyResponse])
async def list_api_keys(user_id: str = Depends(get_current_user_id)):
    with get_db() as db:
        keys = db.query(ApiKey).filter(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc()).all()
        return [
            {
                "id": k.id,
                "created_at": str(k.created_at),
                "name": k.name,
                "key_prefix": k.key_prefix,
                "last_used_at": str(k.last_used_at) if k.last_used_at else None,
                "expires_at": str(k.expires_at) if k.expires_at else None,
                "is_active": k.is_active,
            }
            for k in keys
        ]


@router.delete("/{key_id}", status_code=204)
async def delete_api_key(key_id: int, user_id: str = Depends(get_current_user_id)):
    with get_db() as db:
        key = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user_id).first()
        if not key:
            raise HTTPException(status_code=404, detail="API key not found")
        db.delete(key)
        db.commit()
    return None


@router.patch("/{key_id}/toggle", response_model=APIKeyResponse)
async def toggle_api_key(key_id: int, user_id: str = Depends(get_current_user_id)):
    with get_db() as db:
        key = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user_id).first()
        if not key:
            raise HTTPException(status_code=404, detail="API key not found")
        key.is_active = not key.is_active
        db.commit()
        db.refresh(key)
        return {
            "id": key.id,
            "created_at": str(key.created_at),
            "name": key.name,
            "key_prefix": key.key_prefix,
            "last_used_at": str(key.last_used_at) if key.last_used_at else None,
            "expires_at": str(key.expires_at) if key.expires_at else None,
            "is_active": key.is_active,
        }
