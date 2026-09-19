"""目录整理と履歴回収箱の公開 route・認証・静的拒否を検証する。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from skillmind.documents.domain import DocumentConflictError
from skillmind.runs.domain import RunNotFoundError
from skillmind.runs.history_deletion import RunHistoryConflict


def test_document_folder_and_conditional_move(client):
    """API は認可後に原 path 条件を保持して service へ渡す。"""
    project, document = uuid4(), uuid4()
    service = SimpleNamespace(
        list_folders=AsyncMock(return_value=["specs/empty"]), manage_documents=AsyncMock()
    )
    client.app.state.document_service = service
    assert client.get(f"/api/v1/projects/{project}/document-folders").json() == {
        "folders": ["specs/empty"]
    }
    body = {
        "action": "MOVE",
        "changes": [
            {
                "document_id": str(document),
                "expected_folder": "specs",
                "expected_name": "old.md",
                "folder": "new",
                "name": "renamed.md",
            }
        ],
    }
    response = client.post(f"/api/v1/projects/{project}/document-operations", json=body)
    assert response.status_code == 204
    assert service.manage_documents.await_args.kwargs["changes"][0]["document_id"] == document
    assert response.headers["cache-control"] == "no-store"
    service.manage_documents.side_effect = DocumentConflictError("private storage path")
    response = client.post(f"/api/v1/projects/{project}/document-operations", json=body)
    assert response.status_code == 409 and "private storage path" not in response.text


@pytest.mark.parametrize("action", ["TRASH", "RESTORE", "PURGE"])
def test_run_history_actions_use_original_actor_and_explicit_outputs(client, action):
    """同じ CSRF/Project 門禁で削除・復元・完全削除を受け付ける。"""
    project, run = uuid4(), uuid4()
    service = SimpleNamespace(
        manage_history=AsyncMock(
            return_value={
                "run_id": str(run),
                "deleted": action != "RESTORE",
                "output_count": 0,
                "protected_output_count": 0,
                "outputs": [],
            }
        )
    )
    client.app.state.run_service = service
    suffix = "/purge" if action == "PURGE" else "/restore" if action == "RESTORE" else ""
    response = client.request(
        "POST" if action == "RESTORE" else "DELETE",
        f"/api/v1/projects/{project}/runs/{run}{suffix}",
        **({} if action == "RESTORE" else {"json": {"include_outputs": True}}),
    )
    assert response.status_code == 200, response.text
    arguments = service.manage_history.await_args.kwargs
    assert arguments["action"] == action and arguments["project_id"] == project
    assert arguments["include_outputs"] is (action != "RESTORE")
    assert arguments["access"].csrf_token
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error,status", [(RunHistoryConflict, 409), (RunNotFoundError, 404)])
def test_run_purge_refusal_is_static_and_does_not_expose_records(client, error, status):
    """別 Project・未解決操作の詳細を Problem 本文へ反射しない。"""
    client.app.state.run_service = SimpleNamespace(
        manage_history=AsyncMock(side_effect=error("private data"))
    )
    response = client.request(
        "DELETE", f"/api/v1/projects/{uuid4()}/runs/{uuid4()}/purge", json={"include_outputs": True}
    )
    assert response.status_code == status and "private data" not in response.text


def test_management_cookie_requests_require_csrf_before_service(client):
    """不正 CSRF は新しい書込入口でも service 到達前に拒否する。"""
    service = SimpleNamespace(manage_documents=AsyncMock())
    client.app.state.document_service = service
    response = client.post(
        f"/api/v1/projects/{uuid4()}/document-operations",
        headers={"X-CSRF-Token": "wrong"},
        json={"action": "CREATE_FOLDER", "target": "safe"},
    )
    assert response.status_code == 403
    service.manage_documents.assert_not_called()
