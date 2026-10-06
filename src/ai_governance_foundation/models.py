"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示科研创新机构下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class EvidenceRecord:
    """表示证据谱系中的一个版本化证据。"""

    evidence_id: str
    site_id: str
    evidence_key: str
    evidence_type: str
    version: int
    payload: dict[str, Any] | None
    payload_hash: str
    status: str
    supersedes: str | None
    replaced_by: str | None
    expires_at: str | None
    retraction_reason: str
    retracted_by: str
    retracted_at: str | None
    created_by: str
    created_at: str
    payload_redacted: bool = False


@dataclass(frozen=True)
class RunRecord:
    """表示一次使用指定数据集与参数的模型运行。"""

    run_id: str
    site_id: str
    client_run_key: str
    dataset_id: str
    parameters_id: str
    result_id: str | None
    note: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class BasisLink:
    """表示结论引用的一条证据及其定格时的摘要。"""

    evidence_id: str
    basis_role: str
    snapshot_hash: str | None


@dataclass(frozen=True)
class ConclusionRecord:
    """表示报告结论的一个版本。"""

    conclusion_id: str
    site_id: str
    conclusion_key: str
    version: int
    title: str
    content: dict[str, Any] | None
    status: str
    supersedes: str | None
    published_at: str | None
    invalidated_at: str | None
    invalidation: dict[str, Any]
    created_by: str
    created_at: str
    basis: tuple[BasisLink, ...] = ()
    content_redacted: bool = False


@dataclass(frozen=True)
class ImpactStatement:
    """描述证据状态变化对运行或结论产生的影响。"""

    statement_id: str
    site_id: str
    trigger_evidence_id: str
    trigger_status: str
    conclusion_id: str | None
    run_id: str | None
    scope: str
    message: str
    detail: dict[str, Any]
    created_by: str
    created_at: str
