"""隔離 PGlite 内で RV 用 DDL/最小権限と本番単行 writer を併せて検証する。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from skillmind.agent.postgres_schema import read_table_schema
from skillmind.effects.database_write import build_database_write
from skillmind.effects.postgres_write import apply_database_write, lookup_database_write


async def probe_rv_reviewer(db):
    """原 Skill の DB 記録部分だけを合成し、MinIO/変換/model の成功とは扱わない。"""

    directory = Path(__file__).resolve().parent / "sql"
    await db.query((directory / "rv-reviewer-schema.sql").read_text(), batch=True)
    await db.query("CREATE ROLE rv_writer_fixture NOLOGIN")
    grants = (directory / "rv-reviewer-grants.sql").read_text()
    await db.query(grants.replace(':"rv_writer"', '"rv_writer_fixture"'), batch=True)
    await db.query("SET ROLE rv_writer_fixture")
    async with db.begin():
        await db.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        schemas = {}
        for table in ("test_run", "test_document", "test_artifact"):
            quoted = f'"test_automation"."{table}"'
            await db.exec_driver_sql(f"LOCK TABLE {quoted} IN ACCESS SHARE MODE")
            schemas[table] = await read_table_schema(db, quoted_table=quoted)
            assert (await db.query(f"SELECT count(*) AS n FROM {quoted}")).one()["n"] == 0
        assert schemas["test_run"]["primary_key"] == ["test_run_id"]
        assert schemas["test_document"]["primary_key"] == ["document_id"]
        assert schemas["test_artifact"]["primary_key"] == [
            "test_run_id",
            "document_id",
            "artifact_type",
        ]
        generated = next(
            column
            for column in schemas["test_document"]["columns"]
            if column["name"] == "spec_status"
        )
        assert generated["generated"] and generated["identity"] == "NONE"
        assert all(
            "default_expression" not in column
            for table in schemas.values()
            for column in table["columns"]
        )
    columns = (
        await db.query(
            "SELECT table_name, column_name FROM information_schema.column_privileges "
            "WHERE grantee = 'rv_writer_fixture' AND table_schema = 'test_automation' "
            "AND privilege_type = 'INSERT' ORDER BY table_name, column_name"
        )
    ).value["rows"]
    scope = {
        "tables": [
            f"test_automation.{name}" for name in ("test_run", "test_document", "test_artifact")
        ],
        "write_columns": [
            f"test_automation.{row['table_name']}.{row['column_name']}" for row in columns
        ],
        "operations": ["INSERT", "UPDATE"],
    }
    identity = {"project_id": uuid4(), "run_id": uuid4(), "integration_id": uuid4()}
    started, finished = "2026-09-11T09:00:00+09:00", "2026-09-11T09:30:00+09:00"
    test_run_id = str(uuid4())
    commands = []

    async def authorize():
        """合成呼出の許可点。現在会話や binding の権限は別の回帰で検証する。"""

    async def write(table, key, values, expected=None):
        """SQL 直書きの代わりに、本番の scope/主キー/原状態/回执処理を実行する。"""

        command = build_database_write(
            **identity,
            effect_id=uuid4(),
            table=f"test_automation.{table}",
            operation="INSERT" if expected is None else "UPDATE",
            key=key,
            values=values,
            expected=expected,
            scope=scope,
        )
        receipt = await apply_database_write(db, command, authorize=authorize)
        commands.append((command, receipt))
        return receipt.after

    run_values = {
        "project_id": str(identity["project_id"]),
        "document_library_id": "fixture-library",
        "bucket": "fixture-bucket",
        "source_prefix": "specifications/",
        "file_name": None,
        "timezone": "Asia/Tokyo",
        "target_date": "2026-09-11",
        "cutoff_at": started,
        "output_prefix": "rv/",
        "started_at": started,
        "status": "RUNNING",
    }
    run = await write("test_run", {"test_run_id": test_run_id}, run_values)
    documents = []
    for index, verdict in enumerate(("PASS", "FAIL", "PASS")):
        document_id = str(uuid4())
        key = {"document_id": document_id}
        document = await write(
            "test_document",
            key,
            {
                "test_run_id": test_run_id,
                "source_path": f"specifications/case-{index}.xlsx",
                "started_at": started,
                "status": "RUNNING",
            },
        )
        document = await write(
            "test_document",
            key,
            {
                "spec_version": "sha256:" + str(index) * 64,
                "last_modified": started,
                "version_id": "fixture-version",
                "etag": "fixture-etag",
                "markitdown_version": "fixture-converter",
            },
            document,
        )
        prefix = f"rv/{test_run_id}/{document_id}/"
        artifact = {
            "document_library_id": "fixture-library",
            "bucket": "fixture-bucket",
            "object_key": prefix + "source.md",
            "relative_path": prefix + "source.md",
        }
        await write(
            "test_artifact",
            {
                "test_run_id": test_run_id,
                "document_id": document_id,
                "artifact_type": "MARKDOWN",
            },
            artifact,
        )
        result = {
            "testRunId": test_run_id,
            "documentId": document_id,
            "specVersion": document["spec_version"],
            "verdict": verdict,
            "summary": "Synthetic persistence case",
            "findings": [],
            "references": [],
        }
        document = await write(
            "test_document", key, {"verdict": verdict, "rv_result": result}, document
        )
        if verdict == "FAIL":
            await write(
                "test_artifact",
                {
                    "test_run_id": test_run_id,
                    "document_id": document_id,
                    "artifact_type": "RV_RESULT",
                },
                {
                    **artifact,
                    "object_key": prefix + "rv-result.json",
                    "relative_path": prefix + "rv-result.json",
                },
            )
            assert document["spec_status"] == "NEEDS_SPEC_REVISION"
        document = await write(
            "test_document",
            key,
            {
                "status": "FAILED" if index == 2 else "COMPLETED",
                "finished_at": finished,
                "error": {"code": "fixture_post_review_failure"} if index == 2 else None,
            },
            document,
        )
        assert document["verdict"] == verdict
        documents.append(document)
    run = await write(
        "test_run",
        {"test_run_id": test_run_id},
        {
            "status": "FAILED",
            "finished_at": finished,
            "target_count": 3,
            "pass_count": 2,
            "fail_count": 1,
            "processing_error_count": 1,
        },
        run,
    )
    # 失敗件数を判定件数へ足して総数と比較すると、RV 後の保存異常を誤って拒否する。
    assert (
        run["pass_count"] + run["fail_count"] + run["processing_error_count"] > run["target_count"]
    )
    ready = (
        await db.query(
            "SELECT d.document_id FROM test_automation.test_document d "
            "JOIN test_automation.test_artifact a USING (test_run_id, document_id) "
            "WHERE d.status = 'COMPLETED' AND d.verdict = 'PASS' AND a.artifact_type = 'MARKDOWN'"
        )
    ).value["rows"]
    assert ready == [{"document_id": documents[0]["document_id"]}]

    empty_key = {"test_run_id": str(uuid4())}
    empty = await write("test_run", empty_key, run_values)
    await write("test_run", empty_key, {"status": "NO_TARGETS", "finished_at": finished}, empty)

    async def rejected(sql, params, code):
        """本物の SQLSTATE を確認し、制約/権限による拒否と adapter の故障を区別する。"""

        try:
            await db.query(sql, params)
        except RuntimeError as error:
            assert code in str(error), str(error)
        else:
            raise AssertionError(f"Expected PostgreSQL SQLSTATE {code}")

    doc_id = documents[0]["document_id"]
    for assignment in (
        "verdict = NULL",
        "rv_result = NULL",
        "rv_result = 'null'::jsonb",
        "rv_result = '{}'::jsonb",
        "rv_result = jsonb_set(rv_result, '{verdict}', '\"FAIL\"')",
        "rv_result = jsonb_set(rv_result, '{documentId}', '\"different-document\"')",
        "spec_version = 'sha256:invalid'",
        "finished_at = NULL",
        "status = 'RUNNING'",
    ):
        await rejected(
            f"UPDATE test_automation.test_document SET {assignment} WHERE document_id = $1",
            [doc_id],
            "23514",
        )
    await rejected(
        "UPDATE test_automation.test_run SET finished_at = NULL WHERE test_run_id = $1",
        [test_run_id],
        "23514",
    )
    await rejected(
        "UPDATE test_automation.test_document SET source_path = 'other.xlsx' "
        "WHERE document_id = $1",
        [doc_id],
        "42501",
    )
    await rejected("DELETE FROM test_automation.test_document", [], "42501")
    await rejected(
        "UPDATE test_automation.test_artifact SET relative_path = 'other.md'", [], "42501"
    )
    await rejected(
        "UPDATE skillmind_effects.execution_receipts SET before_row = 'null'", [], "42501"
    )
    await rejected(
        "INSERT INTO test_automation.test_artifact "
        "(test_run_id, document_id, artifact_type, document_library_id, "
        "bucket, object_key, relative_path) "
        "VALUES ($1, $2, 'RV_RESULT', 'foreign-library', 'fixture-bucket', "
        "'other.json', 'other.json')",
        [test_run_id, doc_id],
        "23503",
    )
    original, original_receipt = commands[0]
    assert (await apply_database_write(db, original, authorize=authorize)).replayed
    assert (await lookup_database_write(db, original)).after == original_receipt.after
    assert original_receipt.after["status"] == "RUNNING"  # 現在の FAILED 行とは異なる原回执。
    print(
        "RV database probe passed: empty-table metadata, composite keys and generated columns, "
        "three business tables, restricted column grants, "
        "PASS/FAIL and post-review errors, NO_TARGETS, paired results and identities, "
        "artifact ownership, original receipt replay. Synthetic data only; no MinIO/model used."
    )
