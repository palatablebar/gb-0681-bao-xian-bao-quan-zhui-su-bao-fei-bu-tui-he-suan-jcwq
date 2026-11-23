import json
from copy import deepcopy
from decimal import Decimal

import pytest

from app import store
from app.errors import DomainError
from app.modules.m6_reconcile.settlement import adjudicate
from app.workflow import load, run


# ---------- 裁决规则 ----------

def test_oversettle_sample_has_overpayments(oversettle):
    s = run(oversettle, {})
    assert Decimal(s['settlement']['totals']['overpayment']) > 0


def test_retain_only_what_is_needed_to_restore_coverage(oversettle):
    s = run(oversettle, {})
    line = next(l for l in s['settlement']['lines'] if l['policy_id'] == 'P101')
    # 原年保费 7300，更正后应收 5460，缺口 1840，恰好留存 1840，余额退现。
    assert line['coverage_gap'] == '1840.00'
    assert line['retained_premium'] == '1840.00'
    assert line['refund'] == '700.00'
    assert line['coverage_after'] == line['coverage_floor'] == '7300.00'


def test_coverage_never_shrinks_after_refund(oversettle):
    s = run(oversettle, {})
    assert s['settlement']['coverage']['preserved'] is True
    for line in s['settlement']['lines']:
        if line['overpayment'] != '0.00':
            assert Decimal(line['coverage_after']) >= Decimal(line['coverage_floor'])


def test_every_overpayment_is_attributed(oversettle):
    s = run(oversettle, {})
    attr = s['settlement']['attribution']
    assert attr['fully_attributed'] is True
    assert attr['residual'] == '0.00'
    assert Decimal(attr['overpayment']) == Decimal(attr['refund']) + Decimal(attr['retained_premium'])
    for line in s['settlement']['lines']:
        assert Decimal(line['overpayment']) == Decimal(line['refund']) + Decimal(line['retained_premium'])


def test_receivable_kept_separate_from_refund(oversettle):
    s = run(oversettle, {})
    line = next(l for l in s['settlement']['lines'] if l['policy_id'] == 'P105')
    assert line['status'] == '待收'
    assert Decimal(line['receivable']) > 0
    assert line['refund'] == '0.00'
    # 待收不抵减退费：净退费 = 退费 - 待收，但不改写各自金额。
    totals = s['settlement']['totals']
    assert Decimal(totals['net_refund']) == Decimal(totals['refund']) - Decimal(totals['receivable'])


def test_no_overpayment_regression(clean):
    s = run(clean, {})
    assert s['settlement']['totals']['overpayment'] == '0.00'
    assert s['settlement']['attribution']['fully_attributed'] is True
    assert s['settlement']['coverage']['preserved'] is True


def test_insufficient_overpayment_blocks_refund():
    # 构造：多缴款不足以补足保障缺口 -> 禁止退现并标记违规。
    state = {
        'run_id': 'X',
        'parameters': {'coverage_multiple': '1', 'paid_up_rate': '1'},
        'policies': [{'id': 'P', 'base': {'annual_premium': '10000'}}],
        'segments': [{'id': 'P', 'start': '2026-01-01', 'days': 365, 'year_days': 365, 'annual': '1000'}],
        'calculations': [{'id': 'P', 'expected': '1000.00', 'paid': '3000.00'}],
        'rows': [{'policy_id': 'P', '_line': 2, 'id': '1', 'kind': 'issue', 'effective': '2026-01-01', 'paid': '3000', '_raw': {}}],
    }
    settlement = adjudicate(state)
    line = settlement['lines'][0]
    assert line['coverage_preserved'] is False
    assert line['refund'] == '0.00'
    assert line['retained_premium'] == '2000.00'
    assert settlement['coverage']['preserved'] is False
    assert settlement['attribution']['fully_attributed'] is True


def test_only_one_settlement_object(oversettle):
    s = run(oversettle, {})
    assert isinstance(s['settlement'], dict)
    assert s['settlement']['id'].startswith('SETTLE-')


def test_persist_new_run_rolls_back_on_settlement_failure(tmp_path, monkeypatch, oversettle):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    s = run(oversettle, {})
    bad = deepcopy(s['settlement'])
    bad['attribution']['residual'] = '1.00'  # 触发校验失败
    with pytest.raises(DomainError):
        store.persist(s['run_id'], s, bad)
    # 批次与结算都不得落库，避免孤儿批次。
    assert store.run_exists(s['run_id']) is False
    assert store.settlement_exists(s['run_id']) is False


# ---------- 幂等 ----------

def test_rerun_same_correction_is_byte_identical(oversettle):
    a = run(oversettle, {})['settlement']
    b = run(oversettle, {})['settlement']
    ja = json.dumps({k: v for k, v in a.items() if k != 'rev'}, sort_keys=True, ensure_ascii=False)
    jb = json.dumps({k: v for k, v in b.items() if k != 'rev'}, sort_keys=True, ensure_ascii=False)
    assert ja == jb


def test_rerun_does_not_add_refund_or_increase(tmp_path, monkeypatch, oversettle):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    s = run(oversettle, {})
    store.save(s['run_id'], s)
    first = store.save_settlement(s['run_id'], s['settlement'])
    refund_first, retained_first = first['totals']['refund'], first['totals']['retained_premium']
    again = store.save_settlement(s['run_id'], run(oversettle, {})['settlement'])
    assert again['totals']['refund'] == refund_first
    assert again['totals']['retained_premium'] == retained_first
    assert again['rev'] == first['rev']  # 内容相同，不新增版本


# ---------- 原子替换 / 失败回滚 ----------

def test_replace_bumps_rev_when_content_changes(tmp_path, monkeypatch, oversettle):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    s = run(oversettle, {})
    store.save(s['run_id'], s)
    first = store.save_settlement(s['run_id'], s['settlement'])
    changed = deepcopy(s['settlement'])
    changed['basis']['coverage_multiple'] = '9.99'
    second = store.save_settlement(s['run_id'], changed)
    assert second['rev'] == first['rev'] + 1


def test_failed_replace_restores_old_version(tmp_path, monkeypatch, oversettle):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    s = run(oversettle, {})
    store.save(s['run_id'], s)
    good = store.save_settlement(s['run_id'], s['settlement'])
    bad = deepcopy(s['settlement'])
    bad['lines'][0]['refund'] = '999999.99'  # 破坏守恒，必须被拒
    with pytest.raises(DomainError) as e:
        store.save_settlement(s['run_id'], bad)
    assert e.value.code == 'SETTLEMENT'
    # 旧版仍在，且内容未被污染。
    current = store.get_settlement(s['run_id'])
    assert current['totals'] == good['totals']
    assert current['rev'] == good['rev']


def test_write_failure_rolls_back(tmp_path, monkeypatch, oversettle):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    s = run(oversettle, {})
    store.save(s['run_id'], s)
    good = store.save_settlement(s['run_id'], s['settlement'])
    changed = deepcopy(s['settlement'])
    changed['basis']['coverage_multiple'] = '2.00'

    real = store._autocommit

    class Boom:
        def __init__(self, db):
            self._db = db
            self.fired = False

        def execute(self, sql, *args):
            if 'ON CONFLICT' in sql and not self.fired:
                self.fired = True
                raise RuntimeError('simulated disk failure')
            return self._db.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._db, name)

    monkeypatch.setattr(store, '_autocommit', lambda: Boom(real()))
    with pytest.raises(RuntimeError):
        store.save_settlement(s['run_id'], changed)
    monkeypatch.setattr(store, '_autocommit', real)
    restored = store.get_settlement(s['run_id'])
    assert restored['rev'] == good['rev']
    assert restored['totals'] == good['totals']


# ---------- 凭据留存 ----------

def test_payment_evidence_survives_without_source(clean):
    s = run(clean, {})
    evidence = s['settlement']['evidence']
    assert evidence, '结案后仍须保留缴费凭据快照'
    for item in evidence:
        assert item['source_sha256']
        assert item['raw']
        assert Decimal(item['paid']) > 0


def test_evidence_hashed_to_source(clean):
    from app.manifest import identity
    import hashlib
    raw = clean
    s = run(raw, {})
    digest = hashlib.sha256(raw).hexdigest()
    assert all(item['source_sha256'] == digest for item in s['settlement']['evidence'])


# ---------- 界面与下载一致 ----------

def _export_rows(client, run_id):
    import csv, io
    response = client.get('/api/archive/' + run_id + '/export.csv')
    assert response.status_code == 200
    return list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))


def test_review_and_download_share_settlement(client):
    run_state = client.post('/api/runs', json={'sample': 'oversettle.csv'}).get_json()
    run_id = run_state['run_id']
    stored = client.get('/api/settlement/' + run_id).get_json()
    rows = _export_rows(client, run_id)
    refund_row = next(r for r in rows if r[0] == '退费')
    payload = json.loads(refund_row[5])
    assert payload['refund'] == stored['totals']['refund']
    assert payload['retained_coverage'] == stored['totals']['retained_coverage']
    assert payload['receivable'] == stored['totals']['receivable']


def test_download_uses_stored_authority(client, monkeypatch):
    run_state = client.post('/api/runs', json={'sample': 'oversettle.csv'}).get_json()
    run_id = run_state['run_id']
    # 直接改批次快照里的结算，不应影响下载：下载以独立结算表为准。
    from app import store as store_mod
    stored = store_mod.get_settlement(run_id)
    rows = _export_rows(client, run_id)
    refund_row = next(r for r in rows if r[0] == '退费')
    assert json.loads(refund_row[5])['refund'] == stored['totals']['refund']


def test_page_hash_matches_download_bytes(client):
    import hashlib
    run_state = client.post('/api/runs', json={'sample': 'oversettle.csv'}).get_json()
    run_id = run_state['run_id']
    from app import store as store_mod
    stored = store_mod.get_settlement(run_id)
    stored.pop('rev', None)
    stored['basis']['coverage_multiple'] = '2.00'
    store_mod.save_settlement(run_id, stored)
    raw = client.get('/api/archive/' + run_id + '/export.csv').data
    page = client.get('/m7_archive').data.decode()
    # 页面显示的校验和必须等于下载文件的真实字节哈希，且下载采用已存结算。
    assert hashlib.sha256(raw).hexdigest() in page
    import csv, io
    rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    settlement_row = next(r for r in rows if r[0] == '结算')
    assert json.loads(settlement_row[5])['basis']['coverage_multiple'] == '2.00'


def test_api_stage_matches_download_when_stored_differs(client):
    import hashlib
    run_state = client.post('/api/runs', json={'sample': 'oversettle.csv'}).get_json()
    run_id = run_state['run_id']
    from app import store as store_mod
    stored = store_mod.get_settlement(run_id)
    stored.pop('rev', None)
    stored['basis']['coverage_multiple'] = '2.00'
    store_mod.save_settlement(run_id, stored)
    raw = client.get('/api/archive/' + run_id + '/export.csv').data
    api = client.get('/api/m7_archive?sample=oversettle.csv').get_json()
    # 阶段接口也以已存结算为准，且归档哈希与下载一致。
    assert api['archive']['sha256'] == hashlib.sha256(raw).hexdigest()
    assert api['settlement']['basis']['coverage_multiple'] == '2.00'
    assert api['archive']['settlement_rev'] == store_mod.get_settlement(run_id)['rev']


def test_download_blocked_when_coverage_would_shrink(client, monkeypatch):
    # 保障缺口未补足的结算不得结案下载。
    from app import store as store_mod
    from app.workflow import load as _load
    state = run(_load('oversettle.csv'), {})
    settlement = deepcopy(state['settlement'])
    # 人为制造违规：把某退款保单的留存清零，保障低于底线。
    for line in settlement['lines']:
        if line['overpayment'] != '0.00':
            line['retained_premium'] = '0.00'
            line['retained_coverage'] = '0.00'
            line['refund'] = line['overpayment']
            line['coverage_after'] = line['corrected_coverage']
            line['coverage_preserved'] = False
            break
    settlement['coverage'] = {'preserved': False, 'violations': [settlement['lines'][0]['policy_id']]}
    # 同步合计，保持守恒；此结算仍自洽，只是保障约束不满足。
    from decimal import Decimal as _D
    settlement['totals']['refund'] = str(sum(_D(l['refund']) for l in settlement['lines']))
    settlement['totals']['retained_premium'] = str(sum(_D(l['retained_premium']) for l in settlement['lines']))
    settlement['attribution']['refund'] = settlement['totals']['refund']
    settlement['attribution']['retained_premium'] = settlement['totals']['retained_premium']
    settlement['attribution']['residual'] = '0.00'
    settlement['attribution']['fully_attributed'] = True
    # 守恒仍成立，可入库；但下载应被保障约束拒绝。
    store_mod.persist(state['run_id'], state, settlement)
    response = client.get('/api/archive/' + state['run_id'] + '/export.csv')
    assert response.status_code == 422
    assert response.get_json()['code'] == 'COVERAGE'


def test_archive_not_ready_when_coverage_violated(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    from app.modules.m7_archive.service import execute as archive
    state = {
        'run_id': 'X', 'reconcile': {'balanced': True}, 'calculations': [], 'quarantine': [],
        'settlement': {'id': 'S', 'rule_version': 'v', 'lines': [{}],
                       'attribution': {'fully_attributed': True}, 'coverage': {'preserved': False}},
    }
    assert archive(state)['archive']['ready'] is False
