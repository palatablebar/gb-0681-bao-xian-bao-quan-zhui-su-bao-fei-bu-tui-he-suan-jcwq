from app.modules.m7_archive.export_out import checksum

def execute(state):
    settlement=state.get('settlement') or {}
    attribution=settlement.get('attribution') or {}
    coverage=settlement.get('coverage') or {}
    state['archive']={
        'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(state['quarantine']),
        'ready':state.get('reconcile',{}).get('balanced',False)
            and settlement.get('lines') is not None
            and attribution.get('fully_attributed',False)
            and coverage.get('preserved',False),
        'settlement_id':settlement.get('id'),
        'settlement_rule_version':settlement.get('rule_version'),
        'settlement_fully_attributed':attribution.get('fully_attributed',False),
        'coverage_preserved':coverage.get('preserved',False),
    }
    return state
