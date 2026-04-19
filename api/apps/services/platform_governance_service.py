#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
"""RAGFlow 统一平台治理接入服务。"""

from __future__ import annotations

import json
import logging
import secrets

import requests

from api.db import CanvasCategory
from api.db.db_models import APIToken, RegistryBinding, RegistrySyncEvent
from common.config_utils import get_base_config
from common.misc_utils import get_uuid

logger = logging.getLogger(__name__)


def _governance_config() -> dict:
    return get_base_config("platform_governance", {}) or {}


def governance_enabled() -> bool:
    cfg = _governance_config()
    return bool(cfg.get("enabled") or str(cfg.get("enabled", "")).lower() == "true")


def agent_capability_disabled() -> bool:
    cfg = _governance_config()
    return bool(cfg.get("disable_agent_capability", True))


def _base_url() -> str:
    cfg = _governance_config()
    return str(cfg.get("api_base_url") or "http://yw-platform:8088/api").rstrip("/")


def _headers() -> dict[str, str]:
    cfg = _governance_config()
    return {
        "X-Internal-Api-Key": str(cfg.get("api_key") or "yw-platform-internal-key"),
        "Content-Type": "application/json",
    }


def _ragflow_public_base_url() -> str:
    cfg = _governance_config()
    return str(cfg.get("ragflow_public_base_url") or "http://yw-rag:9380").rstrip("/")


def _ensure_retrieval_token(tenant_id: str) -> str:
    token_row = (
        APIToken.select()
        .where(APIToken.tenant_id == tenant_id, APIToken.source == "dify_external_retrieval")
        .order_by(APIToken.create_time.asc())
        .first()
    )
    if token_row is not None:
        return token_row.token
    token_value = "ragflow-" + secrets.token_urlsafe(32)
    APIToken.insert(
        tenant_id=tenant_id,
        token=token_value,
        dialog_id=None,
        source="dify_external_retrieval",
        beta="governed",
    ).execute()
    return token_value


def _write_binding(source_id: str, tenant_id: str, resource_code: str, payload: dict, sync_status: str) -> None:
    binding = RegistryBinding.get_or_none(source_system="ragflow", source_id=source_id, tenant_id=tenant_id)
    if binding is None:
        RegistryBinding.insert(
            id=get_uuid(),
            source_system="ragflow",
            source_id=source_id,
            resource_code=resource_code,
            tenant_id=tenant_id,
            sync_status=sync_status,
            payload=payload,
        ).execute()
        return
    RegistryBinding.update(
        source_system="ragflow",
        resource_code=resource_code,
        tenant_id=tenant_id,
        sync_status=sync_status,
        payload=payload,
    ).where(RegistryBinding.id == binding.id).execute()


def _append_sync_event(source_id: str, event_type: str, status: str, message: str, payload: dict) -> None:
    RegistrySyncEvent.insert(
        id=get_uuid(),
        source_system="ragflow",
        source_id=source_id,
        event_type=event_type,
        status=status,
        message=message,
        payload=payload,
    ).execute()


def build_kb_payload(kb) -> dict:
    retrieval_token = _ensure_retrieval_token(kb.tenant_id)
    retrieval_endpoint = f"{_ragflow_public_base_url()}/api/v1/dify"
    return {
        "source_id": kb.id,
        "workspace_id": kb.tenant_id,
        "owner_user_id": kb.created_by,
        "name": kb.name,
        "summary": kb.description or kb.name,
        "version_label": f"kb-{kb.update_time or kb.create_time or 0}",
        "knowledge_base_id": kb.id,
        "parser_id": kb.parser_id,
        "pipeline_id": kb.pipeline_id,
        "permission": kb.permission,
        "doc_num": kb.doc_num,
        "chunk_num": kb.chunk_num,
        "token_num": kb.token_num,
        "parser_config": kb.parser_config or {},
        "retrieval_endpoint": retrieval_endpoint,
        "retrieval_api_key": retrieval_token,
    }


def sync_knowledge_base(kb) -> None:
    if not governance_enabled():
        return
    payload = build_kb_payload(kb)
    resource_code = f"ragflow:knowledge_base:{kb.id}"
    try:
        response = requests.post(
            f"{_base_url()}/internal/sync/ragflow/kbs",
            headers=_headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=10,
        )
        response.raise_for_status()
        _write_binding(kb.id, kb.tenant_id, resource_code, payload, "synced")
        _append_sync_event(kb.id, "upsert", "success", "", payload)
        from api.db.db_models import DB

        DB.commit()
    except Exception:
        _write_binding(kb.id, kb.tenant_id, resource_code, payload, "failed")
        _append_sync_event(kb.id, "upsert", "failed", "sync_knowledge_base_failed", payload)
        from api.db.db_models import DB

        DB.commit()
        logger.exception("同步知识库到统一平台注册中心失败: kb_id=%s", kb.id)


def delete_knowledge_base(kb_id: str) -> None:
    if not governance_enabled():
        return
    binding = RegistryBinding.get_or_none(source_id=kb_id)
    tenant_id = binding.tenant_id if binding is not None else ""
    payload = {"resource_code": f"ragflow:knowledge_base:{kb_id}"}
    try:
        response = requests.delete(
            f"{_base_url()}/internal/sync/ragflow/kbs/{kb_id}",
            headers=_headers(),
            timeout=10,
        )
        response.raise_for_status()
        if binding is not None:
            RegistryBinding.update(sync_status="deleted", payload=payload).where(RegistryBinding.id == binding.id).execute()
        _append_sync_event(kb_id, "delete", "success", "", payload)
        from api.db.db_models import DB

        DB.commit()
    except Exception:
        if binding is not None:
            RegistryBinding.update(sync_status="failed", payload=payload).where(RegistryBinding.id == binding.id).execute()
        _append_sync_event(kb_id, "delete", "failed", "delete_knowledge_base_failed", payload)
        from api.db.db_models import DB

        DB.commit()
        logger.exception("删除知识库统一平台注册映射失败: kb_id=%s", kb_id)


def ensure_agent_operation_allowed(canvas_category: str | None = None) -> tuple[bool, str | None]:
    if not governance_enabled() or not agent_capability_disabled():
        return True, None
    if canvas_category is None or canvas_category == CanvasCategory.Agent:
        return False, "平台治理已启用，RAGFlow 智能体能力已禁用，请在 Dify 中创建和管理智能体。"
    return True, None
