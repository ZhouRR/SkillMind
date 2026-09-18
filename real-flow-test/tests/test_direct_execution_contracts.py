"""直接実行パッケージの自己完結性と業務結果の契約を検証する。"""
import json
from pathlib import Path
import re

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    """共有 Schema の正本を取得する。"""
    return json.loads((ROOT / 'schemas' / name).read_text())


def validator(name):
    """実際の format 検証を有効にした validator を返す。"""
    return Draft202012Validator(load(name), format_checker=FormatChecker())


@pytest.fixture
def basis():
    """操作と期待条件を原文位置で固定した二つの依存ステップ。"""
    return {
        'sourceMarkdown': {'documentLibraryId': 'fixture', 'bucket': 'fixture',
                           'objectKey': '仕様.md', 'sha256': 'sha256:' + 'a' * 64},
        'specVersion': 'sha256:' + 'b' * 64,
        'environment': {'appId': 'fixture', 'connectionRef': 'fixture',
                        'environmentLease': 'fixture-lease', 'maxParallelism': 1,
                        'caseTimeoutSeconds': 1800},
        'cases': [{'testCaseId': 'TC-1', 'steps': [
            {'stepId': 'S1', 'sourceLines': {'start': 3, 'end': 3},
             'expectedLines': {'start': 4, 'end': 4}, 'dependsOn': []},
            {'stepId': 'S2', 'sourceLines': {'start': 5, 'end': 5},
             'expectedLines': {'start': 6, 'end': 6},
             'dependsOn': [{'testCaseId': 'TC-1', 'stepId': 'S1'}]},
        ]}],
    }


@pytest.fixture
def result(basis):
    """途中失敗でも未実行ステップを欠落させない終了結果。"""
    return {
        'schemaVersion': '2.0', 'testRunId': '00000000-0000-4000-8000-000000000001',
        'documentId': '00000000-0000-4000-8000-000000000002',
        'executionId': '00000000-0000-4000-8000-000000000003',
        'specVersion': basis['specVersion'], 'executionBasis': basis,
        'versionSnapshot': {'skillVersion': 'fixture', 'environmentVersion': 'fixture',
                            'sutBuild': 'fixture', 'runnerVersions': {'MCP': 'fixture'},
                            'modelVersion': 'fixture'},
        'executionStatus': 'ERROR', 'startedAt': '2026-09-18T10:00:00Z',
        'finishedAt': '2026-09-18T10:01:00Z', 'error': {'code': 'fixture'},
        'cases': [{'testCaseId': 'TC-1', 'executionStatus': 'ERROR', 'error': None,
                   'steps': [
                       {'stepId': 'S1', 'executionStatus': 'TIMEOUT', 'actualResult': None,
                        'evidence': [], 'error': {'code': 'fixture'}},
                       {'stepId': 'S2', 'executionStatus': 'NOT_RUN', 'actualResult': None,
                        'evidence': [], 'error': None},
                   ]}],
    }


def test_direct_result_needs_no_plan(basis, result):
    """元文書と登録根拠だけで結果を検証し、非実行の事実を保持できる。"""
    validator('execution-basis.schema.json').validate(basis)
    validator('execution-result.schema.json').validate(result)
    assert result['cases'][0]['steps'][1]['executionStatus'] == 'NOT_RUN'


@pytest.mark.parametrize('change', ['missing_basis', 'legacy_plan', 'no_cases', 'no_steps',
                                   'no_expected', 'bad_hash', 'bad_uuid', 'bad_date', 'no_timezone'])
def test_invalid_result_is_rejected(result, change):
    """旧計画による代用・必要根拠の欠落・format 不正を拒否する。"""
    if change == 'missing_basis':
        result.pop('executionBasis')
    elif change == 'legacy_plan':
        result['sourcePlan'] = result.pop('executionBasis')['sourceMarkdown']
    elif change == 'no_cases':
        result['cases'] = []
    elif change == 'no_steps':
        result['cases'][0]['steps'] = []
    elif change == 'no_expected':
        result['executionBasis']['cases'][0]['steps'][0].pop('expectedLines')
    elif change == 'bad_hash':
        result['executionBasis']['sourceMarkdown']['sha256'] = 'unknown'
    elif change == 'bad_uuid':
        result['executionId'] = 'not-a-uuid'
    elif change == 'bad_date':
        result['finishedAt'] = '2026-02-30T10:01:00Z'
    else:
        result['finishedAt'] = '2026-09-18T10:01:00'
    assert list(validator('execution-result.schema.json').iter_errors(result))


def test_basis_is_the_same_contract_in_results():
    """登録用と結果用の埋込み定義のずれを防ぐ。"""
    basis = load('execution-basis.schema.json')
    basis.pop('$schema')
    assert basis == load('execution-result.schema.json')['properties']['executionBasis']


def test_packaged_schemas_and_links_are_self_contained():
    """各 Skill 単体の導入でリンクの root 逸脱や Schema のずれを生まない。"""
    for skill in ('execution-plan-generator', 'result-judge-triage'):
        package = ROOT / 'skills' / skill
        for link in re.findall(r'\]\(([^)]+)\)', (package / 'SKILL.md').read_text()):
            target = (package / link).resolve()
            assert target.is_relative_to(package.resolve()) and target.is_file()
        for path in (package / 'schemas').glob('*.json'):
            assert path.read_bytes() == (ROOT / 'schemas' / path.name).read_bytes()
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
            for ref in re.findall(r'"\$ref"\s*:\s*"([^"]+)"', path.read_text()):
                assert ref.startswith('#/')


def test_judgement_and_defect_use_markdown_reference(basis):
    """判定と不具合 Payload の両方が計画参照を必要としない。"""
    schema = load('verdict.schema.json')
    for node in (schema, schema['$defs']['defectPayload']):
        assert 'sourceMarkdown' in node['required']
        assert 'sourcePlan' not in node['properties']
        Draft202012Validator(node['properties']['sourceMarkdown']).validate(basis['sourceMarkdown'])
        assert node['properties']['schemaVersion']['const'] == '2.0'
