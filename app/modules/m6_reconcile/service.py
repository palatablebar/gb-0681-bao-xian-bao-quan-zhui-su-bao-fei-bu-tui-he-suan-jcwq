from app.modules.m6_reconcile.compare import balance
from app.modules.m6_reconcile.settlement import adjudicate

def execute(state):
    state['reconcile']=balance(state)
    state['settlement']=adjudicate(state)
    return state
