-- 承認済み database.write/v1 の原実行を、業務行と同じ DB transaction で記録する。
-- 対象 DB の管理者が専用 owner で適用する。Worker は DDL/所有権を必要としない。
-- 業務 table や接続先、login/password は定義しない。
BEGIN;
CREATE SCHEMA skillmind_effects;
REVOKE ALL ON SCHEMA skillmind_effects FROM PUBLIC;
CREATE TABLE skillmind_effects.execution_receipts (
    effect_id uuid PRIMARY KEY,
    request_checksum text NOT NULL CHECK (request_checksum ~ '^sha256:[a-f0-9]{64}$'),
    before_row jsonb NOT NULL CHECK (jsonb_typeof(before_row) IN ('object', 'null')),
    after_row jsonb NOT NULL CHECK (jsonb_typeof(after_row) = 'object'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (octet_length(before_row::text) <= 1048576),
    CHECK (octet_length(after_row::text) <= 1048576)
);
REVOKE ALL ON skillmind_effects.execution_receipts FROM PUBLIC;
-- 設定する実行 role には schema USAGE と table SELECT/INSERT のみを別途付与する。
-- 回执 UPDATE/DELETE/TRUNCATE と schema CREATE は付与しない。
COMMIT;
