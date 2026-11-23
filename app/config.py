TITLE='保险保全追溯与保费补退核算'
SHORT='endorsement'
RULE_VERSION='2026.11.17-v1'
# 多缴款结算独立版本：分配规则变更只影响结算身份，不改变采信/重算规则。
SETTLEMENT_RULE_VERSION='2026.11.23-v1'
# 保额按年保费倍数折算；缴清增额按每元留存多缴折算的保额。产品可通过参数覆盖。
COVERAGE_MULTIPLE='1'
PAID_UP_RATE='1'
MODULES=[('m1_ingest', '保单采信'), ('m2_mapping', '责任映射'), ('m3_versions', '保全选版'), ('m4_segments', '承保切片'), ('m5_calculate', '保费重算'), ('m6_reconcile', '补退对账'), ('m7_archive', '结案归档')]
REQUIRED=['id', 'policy_id', 'version', 'kind', 'effective', 'start', 'end', 'annual_premium', 'paid', 'cover']
NUMERIC=['version', 'annual_premium', 'paid']
DATES=['effective', 'start', 'end']
TIMES=[]
