import base64
from flask import request,render_template,jsonify
from app.errors import DomainError
from app.config import TITLE,MODULES,SHORT
from app.workflow import run,load
from app import store

def payload():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    if 'csv' in data and not isinstance(data['csv'],str):raise DomainError('CSV','CSV内容必须是文本')
    sample=data.get('sample',request.args.get('sample','dirty.csv'))
    content=data.get('csv')
    return (content.encode() if isinstance(content,str) else load(sample)),data.get('parameters',{})

def _stored_settlement(run_id, computed):
    """取该批次唯一结算：已存为准，否则用本次结果补写；返回 (结算, rev)。

    结算内容与已存完全相同时不改写、不加版本，保证重跑不新增退费或增额。
    """
    if store.settlement_exists(run_id):
        stored=store.get_settlement(run_id)
        return stored, stored.pop('rev',None)
    if store.run_exists(run_id) and computed is not None:
        stored=store.save_settlement(run_id,computed)
        return stored, stored.pop('rev',None)
    return computed, None


def _with_stored_settlement(data, stage):
    """把展示数据对齐到该批次的唯一已存结算，必要时整份归档重算。

    复核界面、阶段接口与结案下载因此共用同一结算：退费、留存保额、待收金额、
    归档校验和与就绪状态全部来自已存批次，避免多口径。
    """
    if stage<6 or data.get('settlement') is None:return data
    data['settlement'],rev=_stored_settlement(data['run_id'],data['settlement'])
    if stage>=7 and store.run_exists(data['run_id']):
        from app.modules.m7_archive.service import execute as archive_execute
        data['archive']=archive_execute(store.get(data['run_id']))['archive']
    else:
        data['archive']=dict(data.get('archive') or {})
    totals=data['settlement'].get('totals',{})
    data['archive']['settlement_rev']=rev
    data['archive']['refund']=totals.get('refund')
    data['archive']['receivable']=totals.get('receivable')
    data['archive']['retained_coverage']=totals.get('retained_coverage')
    return data


def api(stage):
    content,parameters=payload();data=run(content,parameters,stage)
    return jsonify(_with_stored_settlement(data,stage))

def page(stage,template):
    latest=store.history();state=store.get(latest[0]['id']) if latest else run(load('dirty.csv'),{})
    source=load(request.args['sample']) if 'sample' in request.args else base64.b64decode(state['source_b64']) if state.get('source_b64') else load('dirty.csv')
    data=run(source,state.get('manifest',{}).get('parameters',{}),stage)
    # 结算按本次展示数据的批次号归属，预览其他样例时不会串用最新批次结算。
    data=_with_stored_settlement(data,stage)
    return render_template(template,title=TITLE,short=SHORT,modules=MODULES,current=stage,data=data,run=state,history=latest)
