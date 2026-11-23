"""多缴款结算裁决。

背景：保费更正后，部分保单实收高于更正后应收，多缴款此前已被转成缴清增额。
财务要求尽快退现，核保坚持总保障不能缩水。两者冲突，必须对每一笔多缴款给出
唯一归属，并且整个批次只产生一份结算结论。

裁决规则（保障底线优先，余额退现）：
1. 保障底线 = 原保单原始年保费按保单期限折算后对应的保障：
   `original_annual × term_fraction × coverage_multiple`。
   这是投保人原本购买的保障水平，也是核保要求不得跌破的下限。
2. 更正后保障 = 更正后应收保费 × `coverage_multiple`。
3. 若更正为降费，二者之差即保障缺口 `gap`。缺口只能由缴清增额补足：
   每留存 1 元保费按 `paid_up_rate` 折算保额，故应留存保费 =
   `gap / paid_up_rate`（向上取整到分，保证补足）。
4. 留存保费以本保单多缴款为上限：
   - 多缴款足以补足缺口：留存后余额全部退现；
   - 多缴款不足以补足缺口：全额留存、不得退现，并登记保障缺口违规，
     绝不静默缩水。
5. 实收低于应收的保单形成待收金额；退费与待收分列，不做隐含抵销。

守恒：每份保单 `refund + retained_premium == overpayment`，因此退现合计 +
留存保费合计 == 多缴款合计，办结后每笔多缴款都有归属，无悬空资金。

幂等：结算完全由输入（应收/实收/保障口径）与规则版本决定，不累计历史。
重跑同一更正得到同一结算，不会新增退费或增额。
"""

from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING

from app.config import COVERAGE_MULTIPLE, PAID_UP_RATE, SETTLEMENT_RULE_VERSION

CENT = Decimal('.01')


def _d(value):
    if value is None:
        return Decimal('0')
    return Decimal(str(value))


def _money(value):
    return str(_d(value).quantize(CENT, rounding=ROUND_HALF_UP))


def _term_fraction(segments):
    """保单期限内按自然年分母折算的年数。整年保单为 1。"""
    total = Decimal('0')
    for seg in segments:
        days = _d(seg.get('days'))
        denominator = _d(seg.get('year_days')) or Decimal('365')
        total += days / denominator
    return total


def _final_annual(segments, fallback):
    if not segments:
        return fallback
    ordered = sorted(segments, key=lambda seg: seg['start'])
    return _d(ordered[-1]['annual'])


def _payment_evidence(state):
    """固化原始缴费凭据。

    结案后不再依赖原始文件：把每笔实收随来源行、原始字段与源文件哈希写入
    结算快照。即便原始凭据被清理，争议时仍可追溯到具体缴费记录。
    """
    source_sha = state.get('source_sha256') or (state.get('manifest') or {}).get('sha256', '')
    evidence = []
    for row in state.get('rows', []):
        paid = _d(row.get('paid'))
        if paid == 0:
            continue
        evidence.append({
            'policy_id': row.get('policy_id', ''),
            'line': row.get('_line'),
            'id': row.get('id', ''),
            'kind': row.get('kind', ''),
            'effective': row.get('effective', ''),
            'paid': _money(paid),
            'raw': row.get('_raw', {}),
            'source_sha256': source_sha,
        })
    evidence.sort(key=lambda item: (
        item['policy_id'], item['line'] if item['line'] is not None else -1))
    return evidence


def adjudicate(state):
    """生成唯一结算。同一输入与同一规则版本必得同一结算。"""
    parameters = state.get('parameters') or {}
    coverage_multiple = _d(parameters.get('coverage_multiple', COVERAGE_MULTIPLE))
    paid_up_rate = _d(parameters.get('paid_up_rate', PAID_UP_RATE))
    if paid_up_rate <= 0:
        paid_up_rate = Decimal('1')
    if coverage_multiple <= 0:
        coverage_multiple = Decimal('1')

    segments_by_policy = {}
    for seg in state.get('segments', []):
        segments_by_policy.setdefault(seg['id'], []).append(seg)

    original_annual = {
        p['id']: _d((p.get('base') or {}).get('annual_premium'))
        for p in state.get('policies', [])
    }

    lines = []
    for calc in state.get('calculations', []):
        pid = calc['id']
        expected = _d(calc.get('expected'))
        paid = _d(calc.get('paid'))
        overpayment = max(paid - expected, Decimal('0'))
        shortfall = max(expected - paid, Decimal('0'))

        segments = segments_by_policy.get(pid, [])
        base_annual = original_annual.get(pid, expected)

        coverage_floor = (base_annual * _term_fraction(segments) * coverage_multiple).quantize(
            CENT, rounding=ROUND_HALF_UP)
        corrected_coverage = (expected * coverage_multiple).quantize(
            CENT, rounding=ROUND_HALF_UP)
        coverage_gap = max(coverage_floor - corrected_coverage, Decimal('0'))

        # 补足缺口所需留存保费，向上取整到分；以本保单多缴款为上限。
        required_retained = (coverage_gap / paid_up_rate).quantize(
            CENT, rounding=ROUND_CEILING)
        retained_premium = min(overpayment, required_retained)
        refund = overpayment - retained_premium
        retained_coverage = (retained_premium * paid_up_rate).quantize(
            CENT, rounding=ROUND_HALF_UP)
        coverage_after = (corrected_coverage + retained_coverage).quantize(
            CENT, rounding=ROUND_HALF_UP)
        # 保障底线只约束“多缴款如何处置”：有退款动作的保单，退现后保障不得低于
        # 原保单水平；纯待收保单的缺口属于后续应收问题，另行以 receivable 列示。
        coverage_preserved = overpayment == 0 or coverage_after >= coverage_floor

        if overpayment > 0 and not coverage_preserved:
            # 资金不足补足缺口：全额留存、禁止退现，交复核关注，绝不静默缩水。
            status = '保障缺口未补足·禁止退现'
        elif overpayment > 0 and refund > 0 and retained_premium > 0:
            status = '部分退现/部分留存缴清增额'
        elif overpayment > 0 and refund > 0:
            status = '全额退现'
        elif overpayment > 0:
            status = '全额留存缴清增额'
        elif shortfall > 0:
            status = '待收'
        else:
            status = '已平'

        lines.append({
            'policy_id': pid,
            'expected': _money(expected),
            'paid': _money(paid),
            'overpayment': _money(overpayment),
            'refund': _money(refund),
            'retained_premium': _money(retained_premium),
            'retained_coverage': _money(retained_coverage),
            'receivable': _money(shortfall),
            'original_annual': _money(base_annual),
            'final_annual': _money(_final_annual(segments, base_annual)),
            'coverage_floor': _money(coverage_floor),
            'corrected_coverage': _money(corrected_coverage),
            'coverage_gap': _money(coverage_gap),
            'coverage_after': _money(coverage_after),
            'coverage_preserved': coverage_preserved,
            'status': status,
        })

    def total(field):
        return sum((_d(line[field]) for line in lines), Decimal('0'))

    overpayment_total = total('overpayment')
    refund_total = total('refund')
    retained_premium_total = total('retained_premium')
    receivable_total = total('receivable')
    violations = [line['policy_id'] for line in lines if not line['coverage_preserved']]
    fully_attributed = (
        overpayment_total == refund_total + retained_premium_total
        and all(
            _d(line['overpayment']) == _d(line['refund']) + _d(line['retained_premium'])
            for line in lines
        )
    )

    return {
        'id': 'SETTLE-' + (state.get('run_id') or 'preview'),
        'rule_version': SETTLEMENT_RULE_VERSION,
        'currency': 'CNY',
        'basis': {
            'coverage_multiple': _money(coverage_multiple),
            'paid_up_rate': _money(paid_up_rate),
        },
        'lines': lines,
        'totals': {
            'expected': _money(total('expected')),
            'paid': _money(total('paid')),
            'overpayment': _money(overpayment_total),
            'refund': _money(refund_total),
            'retained_premium': _money(retained_premium_total),
            'retained_coverage': _money(total('retained_coverage')),
            'receivable': _money(receivable_total),
            'net_refund': _money(refund_total - receivable_total),
        },
        'attribution': {
            'overpayment': _money(overpayment_total),
            'refund': _money(refund_total),
            'retained_premium': _money(retained_premium_total),
            'residual': _money(overpayment_total - refund_total - retained_premium_total),
            'fully_attributed': fully_attributed,
        },
        'coverage': {
            'preserved': not violations,
            'violations': violations,
        },
        'evidence': _payment_evidence(state),
    }
