"""Projects against a real Postgres with the aios migrations: KB_TEST_DATABASE_URL=postgresql://... (skipped otherwise)."""

import os
import uuid

import pytest

from kb import memory, projects
from kb.store import connect

URL = os.environ.get("KB_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set KB_TEST_DATABASE_URL to run against Postgres")


@pytest.fixture
def conn():
    with connect(URL) as c:
        yield c


def test_upsert_list_conflict_and_project_memory(conn):
    slug = f"t-{uuid.uuid4().hex[:10]}"
    try:
        p, created = projects.upsert(conn, projects.ProjectIn(tenant="pessoal", slug=slug, name="App X", domain="projects",
                                                               repository="git@github.com:me/app-x.git"))
        assert created and p.tenant == "pessoal" and p.repository == "git@github.com:me/app-x.git"
        assert p.status == "active" and p.domain == "projects"
        p, created = projects.upsert(conn, projects.ProjectIn(tenant="pessoal", slug=slug, name="App X v2",
                                                               status="paused"))
        assert not created and p.name == "App X v2" and p.status == "paused"
        assert p.repository == "git@github.com:me/app-x.git" and p.domain == "projects"  # omitted fields kept
        with pytest.raises(projects.ProjectConflict):
            projects.upsert(conn, projects.ProjectIn(tenant="nitro", slug=slug, name="hijack"))
        assert slug in {x.slug for x in projects.list_projects(conn, ["pessoal"])}
        assert slug not in {x.slug for x in projects.list_projects(conn, ["nitro"])}
        assert slug in {x.slug for x in projects.list_projects(conn, ["pessoal"], status="paused")}
        # the point of registering: project-scoped memories now resolve the slug
        assert memory.project_id(conn, slug, "pessoal")
        with pytest.raises(memory.MemoryInputError):
            memory.project_id(conn, slug, "nitro")
    finally:
        conn.execute("DELETE FROM projects WHERE slug = %s", (slug,))
