from pathlib import Path

from bidflow.connectors import ManualExternalConnector, get_connector
from bidflow.ingest import ingest
from bidflow.project import Project
from bidflow.utils import write_text


def test_local_library_search_and_copy_to_project(tmp_path: Path):
    library = Project.create(tmp_path / "library", "公司资料库")
    source = tmp_path / "证书.md"
    write_text(source, "甲级工程咨询资信证书，有效期至2027年。")
    imported = ingest(library, source, "公司资质")
    project = Project.create(tmp_path / "project", "测试投标", library.root)
    connector = get_connector(project, "local_library")
    hits = connector.search("工程咨询")
    assert hits and hits[0].source_id == imported["file_id"] and hits[0].status == "candidate"
    copied = connector.materialize(project, hits[0].source_id, "公司资质")
    assert project.safe_path(copied["path"]).is_file()
    assert project.safe_path(copied["path"]).resolve() != library.safe_path(imported["path"]).resolve()


def test_external_connector_is_explicit_manual_boundary(tmp_path: Path):
    project = Project.create(tmp_path / "project", "测试投标")
    connector = ManualExternalConnector("dingtalk")
    hits = connector.search("张三主持业绩", ["人员业绩证明"])
    assert hits[0].status == "manual_required"
    assert "人工下载" in hits[0].metadata["next"]
