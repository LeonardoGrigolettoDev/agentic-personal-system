"""Projects: the slugs that project-scoped memories and compiled context hang on (docs/ARCHITECTURE.md §11).

A project belongs to exactly one tenant; its slug is unique across tenants, so an upsert never moves or
rewrites another tenant's project. `repository` is an abstract reference (git remote or storage://), never
a host path: agents and the sandbox clone from it.
"""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import psycopg

from kb.sources import DOMAINS, TENANTS

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
STATUSES = ("active", "paused", "archived")
REPOSITORY = re.compile(r"^(https://|ssh://|git@[\w.-]+:|storage://)\S+$")


class ProjectInputError(ValueError):
    pass


class ProjectConflict(ProjectInputError):
    """The slug already belongs to another tenant."""


@dataclass(frozen=True)
class ProjectIn:
    tenant: str
    slug: str
    name: str
    domain: str | None = None  # None: 'engineering' on create, unchanged on update (same for status)
    description: str | None = None
    status: str | None = None
    repository: str | None = None

    def validate(self) -> None:
        if self.tenant not in TENANTS:
            raise ProjectInputError(f"unknown tenant {self.tenant!r}")
        if not SLUG.match(self.slug):
            raise ProjectInputError("slug must be 2-63 chars of a-z, 0-9 and '-' (e.g. 'app-checkout')")
        if not self.name.strip() or len(self.name) > 200:
            raise ProjectInputError("name is required (<=200 chars)")
        if self.domain is not None and self.domain not in DOMAINS:
            raise ProjectInputError(f"unknown domain {self.domain!r}")
        if self.status is not None and self.status not in STATUSES:
            raise ProjectInputError(f"status must be one of {STATUSES}")
        if self.repository and not REPOSITORY.match(self.repository):
            raise ProjectInputError("repository must be a git remote (https://, ssh://, git@host:) or storage:// — "
                                    "never a host path")
        if self.description and len(self.description) > 2000:
            raise ProjectInputError("description is limited to 2000 chars")


@dataclass(frozen=True)
class Project:
    slug: str
    name: str
    tenant: str
    domain: str
    status: str
    repository: str | None
    description: str | None
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


_COLUMNS = "slug, name, tenant::text, domain::text, status, repository, description, updated_at::text"


def upsert(conn: psycopg.Connection, p: ProjectIn) -> tuple[Project, bool]:
    """Create or update a project of p.tenant. Returns (project, created). Raises ProjectConflict when the slug
    belongs to another tenant. Omitted (None) fields keep their stored values on update."""
    p.validate()
    v = {"slug": p.slug, "name": p.name.strip(), "tenant": p.tenant, "domain": p.domain, "status": p.status,
         "description": p.description, "repository": p.repository}
    row = conn.execute(
        f"""INSERT INTO projects (slug, name, tenant, domain, description, status, repository)
            VALUES (%(slug)s, %(name)s, %(tenant)s::tenant, coalesce(%(domain)s::domain, 'engineering'),
                    %(description)s, coalesce(%(status)s, 'active'), %(repository)s)
            ON CONFLICT (slug) DO UPDATE SET
              name = EXCLUDED.name,
              domain = coalesce(%(domain)s::domain, projects.domain),
              status = coalesce(%(status)s, projects.status),
              description = coalesce(%(description)s, projects.description),
              repository = coalesce(%(repository)s, projects.repository)
            WHERE projects.tenant = EXCLUDED.tenant
            RETURNING {_COLUMNS}, (xmax = 0) AS created""",
        v,
    ).fetchone()
    if row is None:
        raise ProjectConflict(f"project {p.slug!r} belongs to another tenant")
    return Project(*row[:-1]), bool(row[-1])


def list_projects(conn: psycopg.Connection, tenants: Sequence[str], *, status: str | None = None) -> list[Project]:
    if status is not None and status not in STATUSES:
        raise ProjectInputError(f"status must be one of {STATUSES}")
    rows = conn.execute(
        f"""SELECT {_COLUMNS} FROM projects WHERE tenant = ANY(%s::tenant[])
            AND (%s::text IS NULL OR status = %s) ORDER BY tenant, slug""",
        (list(tenants), status, status),
    ).fetchall()
    return [Project(*r) for r in rows]
