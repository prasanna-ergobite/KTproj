"""
Pydantic schemas and domain data models for AutoKT graph entities.
"""

from datetime import datetime, timezone
from typing import Optional, List
from pydantic import BaseModel, Field


class BaseNode(BaseModel):
    """
    Base domain model containing multi-tenant identification fields.
    """
    id: str = Field(..., description="Unique entity identifier within the organization")
    organization_id: str = Field(..., description="Tenant organization identifier")

    @property
    def tenant_key(self) -> str:
        """
        Returns tenant-scoped composite key 'organization_id:id' for Neo4j Community uniqueness constraint.
        """
        return f"{self.organization_id}:{self.id}"

    def to_node_params(self) -> dict:
        """
        Serializes model attributes to a parameter dict for Cypher queries,
        explicitly injecting the computed 'tenant_key' property.
        """
        data = self.model_dump()
        data["tenant_key"] = self.tenant_key
        # Convert datetime objects to ISO strings if needed by Cypher
        for key, val in data.items():
            if isinstance(val, datetime):
                data[key] = val.isoformat()
        return data


class Repository(BaseNode):
    """
    Represents a code repository ingested into AutoKT.
    """
    name: str
    url: str
    default_branch: str = "main"
    last_synced_at: Optional[datetime] = None


class Module(BaseNode):
    """
    Represents a logical code module or package within a repository.
    """
    repository_id: str
    name: str
    path: str
    description: Optional[str] = None


class File(BaseNode):
    """
    Represents a source file contained within a module.
    """
    module_id: str
    path: str
    language: str
    last_modified_at: Optional[datetime] = None


class Person(BaseNode):
    """
    Represents a developer, code owner, or contributor.
    """
    name: str
    email: Optional[str] = None
    github_handle: Optional[str] = None


class Document(BaseNode):
    """
    Represents an ingested knowledge document or design doc.
    """
    title: str
    content_path: str
    document_type: str
    linked_module_id: Optional[str] = None


class KTSession(BaseNode):
    """
    Represents a knowledge-transfer session recorded or generated.
    """
    title: str
    date: datetime
    module_id: Optional[str] = None
    participants: List[str] = Field(default_factory=list)
    summary: Optional[str] = None


class Decision(BaseNode):
    """
    Represents an Architectural Decision Record (ADR) or technical decision.
    """
    title: str
    context: str
    rationale: str
    status: str
    author_id: Optional[str] = None


class Risk(BaseNode):
    """
    Represents an identified technical risk or single point of failure.
    """
    title: str
    description: str
    severity: str
    module_id: Optional[str] = None
    owner_id: Optional[str] = None


class Dependency(BaseNode):
    """
    Represents an inter-module or external system dependency relationship.
    """
    source_id: str
    target_id: str
    dependency_type: str
    description: Optional[str] = None


class Topic(BaseNode):
    """
    Represents a semantic topic or domain concept linked across code and docs.
    """
    name: str
    description: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)


class Package(BaseNode):
    """
    Represents an external library or package dependency detected during ingestion.
    """
    name: str
    version: str
    ecosystem: str
    repository_id: str

