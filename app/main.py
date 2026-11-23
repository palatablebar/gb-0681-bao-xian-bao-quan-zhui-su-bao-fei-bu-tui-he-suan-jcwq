import importlib,os
from flask import Flask,jsonify,request,redirect,Response
from app.config import MODULES
from app.errors import DomainError
from app.workflow import run,load
from app import store
from app.web import payload
from app.modules.m7_archive.export_out import bundle

def _persist(state):
    """同一事务保存批次与唯一结算；校验或写入失败整体回滚，旧版保持可用。"""
    return store.persist(state['run_id'],state,state.get('settlement'))

def create_app():
    app=Flask(__name__);app.config['MAX_CONTENT_LENGTH']=2_000_000
    for entry in MODULES:app.register_blueprint(importlib.import_module('app.modules.'+entry[0]+'.handler').bp)
    @app.errorhandler(DomainError)
    def domain_error(error):return jsonify(code=error.code,message=error.message),error.status
    @app.errorhandler(404)
    def missing(error):return jsonify(code='NOT_FOUND',message='入口不存在'),404
    @app.get('/')
    def index():return redirect('/'+MODULES[0][0])
    @app.get('/healthz')
    def health():return jsonify(status='ok')
    @app.post('/api/runs')
    def submit():
        content,params=payload();state=run(content,params);return jsonify(_persist(state))
    @app.get('/api/runs')
    def list_runs():return jsonify(store.history())
    @app.get('/api/runs/<run_id>')
    def get_run(run_id):return jsonify(store.get(run_id))
    @app.get('/api/settlement/<run_id>')
    def get_settlement(run_id):
        # 唯一结算来源：复核界面与下载都以它为准。
        if not store.run_exists(run_id):raise DomainError('NOT_FOUND','批次不存在',404)
        if store.settlement_exists(run_id):return jsonify(store.get_settlement(run_id))
        state=store.get(run_id)
        if state.get('settlement') is None:raise DomainError('NOT_FOUND','结算不存在',404)
        return jsonify(store.save_settlement(run_id,state['settlement']))
    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data=request.get_json(silent=True) or {}
        if not isinstance(data.get('version'),int):raise DomainError('VERSION','请提交复核版本')
        return jsonify(store.review(run_id,data['version'],data.get('decision')))
    @app.get('/api/archive/<run_id>/export.csv')
    def download(run_id):
        state=store.get(run_id)
        if not state.get('reconcile',{}).get('balanced'):raise DomainError('UNBALANCED','对账未平')
        if state.get('settlement') is None:raise DomainError('NOT_FOUND','结算不存在',404)
        coverage=state['settlement'].get('coverage') or {}
        if not coverage.get('preserved',False):
            # 保障会缩水的结算不得结案，须先复核处置。
            raise DomainError('COVERAGE','总保障将缩水，须复核后再结案')
        return Response(bundle(state),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=evidence.csv'})
    with app.app_context():
        if not store.history():
            _persist(run(load('dirty.csv'),{}))
    return app

app=create_app()
if __name__=='__main__':app.run(host='0.0.0.0',port=8000)
