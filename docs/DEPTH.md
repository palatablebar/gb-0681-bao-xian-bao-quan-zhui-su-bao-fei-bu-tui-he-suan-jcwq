# 深度登记 保险保全追溯与保费补退核算

规模：中型。核心规则：按保全有效日拆分责任区间，撤销回滚指向原变更；实收补退按日精确分配尾差。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 保单采信 | 查看并复算保单采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 责任映射 | 查看并复算责任映射 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 保全选版 | 查看并复算保全选版 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 承保切片 | 查看并复算承保切片 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 保费重算 | 查看并复算保费重算 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 补退对账 | 查看并复算补退对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py`、`settlement.py` | `tests/test_reconcile.py`、`tests/test_settlement.py` |
| M7 结案归档 | 查看并复算结案归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py`、`tests/test_settlement.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-23 | 生效日端点 | 双计生效日 | 半开区间 | `app/modules/m4_segments/rules.py` | `tests/test_segments.py::test_halfopen_conservation` |
| TRAP-24 | 分币尾差 | 段合计不等 | 最大余额法 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_largest_remainder_conserves` |
| TRAP-27 | 多缴款处置冲突 | 只退现导致保障缩水，或全额留存侵占退费 | 保障底线优先、余额退现 | `app/modules/m6_reconcile/settlement.py` | `tests/test_settlement.py::test_retain_only_what_is_needed_to_restore_coverage` |
| TRAP-28 | 多缴款悬空 | 退费+留存≠多缴合计 | 逐单守恒、批合计残差为0 | `app/modules/m6_reconcile/settlement.py` | `tests/test_settlement.py::test_every_overpayment_is_attributed` |
| TRAP-29 | 重跑重复退费/增额 | 每次运行累加结算 | 结算由输入决定，内容相同不改写 | `app/store.py` | `tests/test_settlement.py::test_rerun_does_not_add_refund_or_increase` |
| TRAP-30 | 替换结算失败 | 半写入、界面与文件不一致 | 原子事务＋回读校验＋回滚旧版 | `app/store.py` | `tests/test_settlement.py::test_failed_replace_restores_old_version` |
| TRAP-31 | 界面与下载不一致 | 各自重算得不同结算 | 复核/接口/下载共用已存结算 | `app/modules/m7_archive/export_out.py`、`app/web.py` | `tests/test_settlement.py::test_review_and_download_share_settlement` |
| TRAP-32 | 凭据清理后争议无据 | 只留金额不留来源 | 结算快照固化缴费凭据与源哈希 | `app/modules/m6_reconcile/settlement.py` | `tests/test_settlement.py::test_payment_evidence_survives_without_source` |
| TRAP-33 | 保障缩水仍结案 | 只看对账平衡 | 下载前校验 coverage.preserved | `app/main.py` | `tests/test_settlement.py::test_download_blocked_when_coverage_would_shrink` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。多缴款结算须满足逐单守恒 `refund+retained_premium=overpayment` 与批合计 `attribution.residual=0`，且退款保单 `coverage_after>=coverage_floor`；三者任一不满足则归档不就绪。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。
