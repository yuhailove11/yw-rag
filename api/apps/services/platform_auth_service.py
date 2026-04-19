"""RAGFlow 统一认证中心 SSO 服务。

RAGFlow 不能直接照搬 Dify 的多工作区切换模型。
因此这里采用兼容投影方案：
1. 平台用户 + 平台工作区 组合成稳定本地身份
2. 本地身份仍沿用 RAGFlow 原有 user.id == tenant.id 的约束
"""

from __future__ import annotations

import hashlib
import json

import requests

from api.db.db_models import PlatformAuthUserBinding, PlatformAuthWorkspaceBinding
from api.db.services.user_service import UserService
from common import settings
from common.misc_utils import get_uuid
from common.time_utils import get_format_time


def _platform_auth_config() -> dict:
    return (settings.get_base_config("platform_governance", {}) or {}).get("platform_auth", {}) or {}


def platform_auth_enabled() -> bool:
    cfg = _platform_auth_config()
    return bool(cfg.get("enabled") or str(cfg.get("enabled", "")).lower() == "true")


def _platform_base_url() -> str:
    governance_cfg = settings.get_base_config("platform_governance", {}) or {}
    return str(governance_cfg.get("api_base_url") or "http://yw-platform:8088/api").rstrip("/")


def _client_id() -> str:
    cfg = _platform_auth_config()
    return str(cfg.get("client_id") or "ragflow")


def _client_secret() -> str:
    cfg = _platform_auth_config()
    return str(cfg.get("client_secret") or "ragflow-internal-secret")


def _build_projection_identity(platform_user_id: str, platform_workspace_id: str) -> tuple[str, str]:
    raw = f"{platform_user_id}:{platform_workspace_id}"
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    local_id = digest[:32]
    local_email = f"{platform_user_id}.{platform_workspace_id}@platform.ragflow.local".lower()
    return local_id, local_email


def _consume_ticket(ticket: str) -> dict:
    response = requests.post(
        f"{_platform_base_url()}/internal/sso/consume",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "ticket": ticket,
                "target_system": "ragflow",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def _upsert_platform_bindings(platform_user: dict, platform_workspace: dict, local_user_id: str, local_username: str) -> None:
    user_binding = PlatformAuthUserBinding.get_or_none(
        PlatformAuthUserBinding.platform_user_id == platform_user["id"],
        PlatformAuthUserBinding.platform_workspace_id == platform_workspace["id"],
        PlatformAuthUserBinding.system_code == "ragflow",
    )
    if user_binding is None:
        PlatformAuthUserBinding.insert(
            id=get_uuid(),
            platform_user_id=platform_user["id"],
            platform_workspace_id=platform_workspace["id"],
            system_code="ragflow",
            local_user_id=local_user_id,
            local_username=local_username,
            status="active",
        ).execute()
    else:
        PlatformAuthUserBinding.update(
            local_user_id=local_user_id,
            local_username=local_username,
            status="active",
        ).where(PlatformAuthUserBinding.id == user_binding.id).execute()

    workspace_binding = PlatformAuthWorkspaceBinding.get_or_none(
        PlatformAuthWorkspaceBinding.platform_workspace_id == platform_workspace["id"],
        PlatformAuthWorkspaceBinding.system_code == "ragflow",
    )
    if workspace_binding is None:
        PlatformAuthWorkspaceBinding.insert(
            id=get_uuid(),
            platform_workspace_id=platform_workspace["id"],
            system_code="ragflow",
            local_tenant_id=local_user_id,
            local_tenant_name=platform_workspace.get("name") or "",
            status="active",
        ).execute()
    else:
        PlatformAuthWorkspaceBinding.update(
            local_tenant_id=local_user_id,
            local_tenant_name=platform_workspace.get("name") or "",
            status="active",
        ).where(PlatformAuthWorkspaceBinding.id == workspace_binding.id).execute()

    user_response = requests.post(
        f"{_platform_base_url()}/internal/bindings/users",
        headers={"X-Internal-Api-Key": str((settings.get_base_config('platform_governance', {}) or {}).get('api_key') or 'yw-platform-internal-key'), "Content-Type": "application/json"},
        data=json.dumps(
            {
                "platform_user_id": platform_user["id"],
                "system_code": "ragflow",
                "external_user_id": local_user_id,
                "external_username": local_username,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=10,
    )
    user_response.raise_for_status()

    workspace_response = requests.post(
        f"{_platform_base_url()}/internal/bindings/workspaces",
        headers={"X-Internal-Api-Key": str((settings.get_base_config('platform_governance', {}) or {}).get('api_key') or 'yw-platform-internal-key'), "Content-Type": "application/json"},
        data=json.dumps(
            {
                "platform_workspace_id": platform_workspace["id"],
                "system_code": "ragflow",
                "external_workspace_id": local_user_id,
                "external_workspace_name": platform_workspace.get("name") or "",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        timeout=10,
    )
    workspace_response.raise_for_status()


def exchange_platform_ticket(ticket: str):
    payload = _consume_ticket(ticket)
    platform_user = payload["user"]
    platform_workspace = payload["workspace"]

    local_user_id, local_email = _build_projection_identity(platform_user["id"], platform_workspace["id"])
    users = UserService.query_user_by_email(local_email)
    if users:
        user = users[0]
        user.nickname = str(platform_user.get("display_name") or platform_user.get("username") or local_user_id)
        user.is_active = "1"
        user.status = "1"
        user.access_token = get_uuid()
        user.save()
    else:
        nickname = str(platform_user.get("display_name") or platform_user.get("username") or local_user_id)
        from api.apps.user_app import user_register

        registered = user_register(
            local_user_id,
            {
                "access_token": get_uuid(),
                "email": local_email,
                "avatar": "",
                "nickname": nickname,
                "login_channel": "platform_sso",
                "last_login_time": get_format_time(),
                "is_superuser": bool(payload.get("is_super_admin", False)),
            },
        )
        if not registered:
            raise ValueError("统一认证中心创建本地投影账号失败")
        user = registered[0]

    _upsert_platform_bindings(
        platform_user,
        platform_workspace,
        user.id,
        user.nickname,
    )
    return user
