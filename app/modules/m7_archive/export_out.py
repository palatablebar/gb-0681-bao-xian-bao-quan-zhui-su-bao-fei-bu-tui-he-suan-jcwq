import io,csv,json,hashlib

def bundle(state):
    out=io.StringIO(newline='');writer=csv.writer(out)
    writer.writerow(['记录类型','批次','来源行','业务ID','原因码','内容'])
    rid=state.get('run_id','preview')
    writer.writerow(['运行清单',rid,'','','',json.dumps(state.get('manifest',{}),ensure_ascii=False,sort_keys=True)])
    writer.writerow(['对账',rid,'','','',json.dumps(state.get('reconcile',{}),ensure_ascii=False,sort_keys=True)])
    settlement=state.get('settlement')
    if settlement is not None:
        # 结案下载与复核界面使用同一份结算：退费、留存保额、待收金额三条口径
        # 都直接来自结算快照，避免界面与文件不一致。
        writer.writerow(['结算',rid,'','','',json.dumps(settlement,ensure_ascii=False,sort_keys=True)])
        totals=settlement.get('totals',{})
        writer.writerow(['退费',rid,'','','',json.dumps({
            'refund':totals.get('refund'),'receivable':totals.get('receivable'),
            'retained_coverage':totals.get('retained_coverage'),
            'retained_premium':totals.get('retained_premium'),
            'currency':settlement.get('currency'),'rule_version':settlement.get('rule_version')},
            ensure_ascii=False,sort_keys=True)])
        for line in settlement.get('lines',[]):
            writer.writerow(['结算明细',rid,'',line.get('policy_id',''),line.get('status',''),json.dumps(line,ensure_ascii=False,sort_keys=True)])
        for item in settlement.get('evidence',[]):
            writer.writerow(['缴费凭据',rid,item.get('line',''),item.get('policy_id',''),'PAID',json.dumps(item,ensure_ascii=False,sort_keys=True)])
    for row in state.get('calculations',[]):writer.writerow(['计算',rid,row.get('line',''),row.get('id',''),row.get('code',''),json.dumps(row,ensure_ascii=False,sort_keys=True)])
    for row in state['quarantine']:writer.writerow(['隔离',rid,row['line'],row.get('id',''),row['code'],json.dumps(row.get('raw',{}),ensure_ascii=False,sort_keys=True)])
    return '﻿'+out.getvalue()

def checksum(state):
    return hashlib.sha256(bundle(state).encode('utf-8')).hexdigest()
