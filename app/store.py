import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def _path():
    return Path(os.environ.get('DB_PATH','data/records.db'))

def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS settlements (run_id TEXT PRIMARY KEY, body TEXT NOT NULL, rule_version TEXT NOT NULL, rev INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')

def connection():
    path=_path();path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    _schema(db)
    return db

def _autocommit():
    path=_path();path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10,isolation_level=None);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    _schema(db)
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def _settlement_problems(settlement):
    """结算自洽校验：任何一条不满足都拒绝替换，旧版继续生效。"""
    from decimal import Decimal, InvalidOperation
    problems=[]
    def d(value):
        try:return Decimal(str(value))
        except (InvalidOperation,TypeError):return None
    lines=settlement.get('lines')
    if not isinstance(lines,list) or not lines:problems.append('结算明细缺失')
    totals=settlement.get('totals') or {}
    attribution=settlement.get('attribution') or {}
    if not attribution.get('fully_attributed'):problems.append('多缴款未全部归属')
    if d(attribution.get('residual')) not in (None,Decimal('0')):problems.append('存在未归属余额')
    refund=Decimal('0');retained=Decimal('0');over=Decimal('0')
    for line in lines or []:
        o,r,p=d(line.get('overpayment')),d(line.get('refund')),d(line.get('retained_premium'))
        if None in (o,r,p):problems.append('结算明细金额无效');continue
        if r<0 or p<0 or o<0:problems.append('结算明细金额为负');continue
        if o!=r+p:problems.append('明细归属不平')
        over+=o;refund+=r;retained+=p
    if refund+retained!=over:problems.append('退费与留存不等于多缴款合计')
    if d(totals.get('refund')) not in (None,refund):problems.append('退费合计不一致')
    if d(totals.get('retained_premium')) not in (None,retained):problems.append('留存合计不一致')
    return problems

def settlement_problems(settlement):
    return _settlement_problems(settlement)

def save_settlement(run_id, settlement):
    """原子替换结算：整笔写入并回读校验，失败回滚，旧版保持可用。

    同一批次重跑得到内容完全相同的结算时不改写、不新增版本，从而不会新增
    退费或增额；内容确实变化（例如结算规则升级）才替换并递增 rev。
    写入前先做自洽校验，替换失败时事务回滚，旧版结算继续对外提供。
    """
    problems=_settlement_problems(settlement)
    if problems:raise DomainError('SETTLEMENT','结算不自洽：'+'；'.join(problems))
    body=json.dumps(settlement,ensure_ascii=False,sort_keys=True)
    rule_version=settlement.get('rule_version','')
    db=_autocommit()
    try:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT body FROM settlements WHERE run_id=?',(run_id,)).fetchone()
        if existing is not None and existing['body']==body:
            db.execute('COMMIT')
            return get_settlement(run_id)
        db.execute('INSERT INTO settlements(run_id,body,rule_version,rev,updated_at) VALUES (?,?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(run_id) DO UPDATE SET body=excluded.body,rule_version=excluded.rule_version,rev=settlements.rev+1,updated_at=CURRENT_TIMESTAMP',(run_id,body,rule_version))
        written=db.execute('SELECT body FROM settlements WHERE run_id=?',(run_id,)).fetchone()
        if written is None or written['body']!=body:
            raise RuntimeError('结算写入校验失败')
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    return get_settlement(run_id)

def get_settlement(run_id):
    with connection() as db:r=db.execute('SELECT body,rule_version,rev FROM settlements WHERE run_id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','结算不存在',404)
    body=json.loads(r['body']);body['rev']=r['rev'];body['rule_version']=r['rule_version'];return body

def settlement_exists(run_id):
    with connection() as db:return db.execute('SELECT 1 FROM settlements WHERE run_id=?',(run_id,)).fetchone() is not None

def run_exists(run_id):
    with connection() as db:return db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone() is not None

def persist(run_id, body, settlement):
    """同一事务写入批次快照与结算，任一失败整体回滚，不留孤儿批次。

    结算内容与已存版本相同时不改写、不加版本，重跑不新增退费或增额。
    """
    if settlement is not None:
        problems=_settlement_problems(settlement)
        if problems:raise DomainError('SETTLEMENT','结算不自洽：'+'；'.join(problems))
    db=_autocommit()
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        if settlement is not None:
            payload=json.dumps(settlement,ensure_ascii=False,sort_keys=True)
            rule_version=settlement.get('rule_version','')
            existing=db.execute('SELECT body FROM settlements WHERE run_id=?',(run_id,)).fetchone()
            if existing is None or existing['body']!=payload:
                db.execute('INSERT INTO settlements(run_id,body,rule_version,rev,updated_at) VALUES (?,?,?,1,CURRENT_TIMESTAMP) ON CONFLICT(run_id) DO UPDATE SET body=excluded.body,rule_version=excluded.rule_version,rev=settlements.rev+1,updated_at=CURRENT_TIMESTAMP',(run_id,payload,rule_version))
            written=db.execute('SELECT body FROM settlements WHERE run_id=?',(run_id,)).fetchone()
            if written is None or written['body']!=payload:
                raise RuntimeError('结算写入校验失败')
        db.execute('COMMIT')
    except Exception:
        if db.in_transaction:db.execute('ROLLBACK')
        raise
    finally:
        db.close()
    return get(run_id)

def get(run_id):
    with connection() as db:
        r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not r:raise DomainError('NOT_FOUND','批次不存在',404)
        s=db.execute('SELECT body,rule_version,rev FROM settlements WHERE run_id=?',(run_id,)).fetchone()
    body=json.loads(r['body'])
    if s is not None:
        # 已存结算为唯一权威口径，覆盖批次快照里的旧结算。rev 作为版本元数据
        # 单列，不写回结算正文，保证导出校验和只由结算内容决定。
        body['settlement']=json.loads(s['body'])
        body['settlement_rev']=s['rev']
    body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)
