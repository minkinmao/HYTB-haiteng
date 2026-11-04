# seed.py
# 初始化数据库表 + 写入演示数据（仅开发期使用）
# 用法：venv/Scripts/python.exe seed.py
# 设计为「可重复执行」：已有数据不会重复插入，方便增量开发时追加新表/新数据
from app import app, db
from models import Permission, Level, User, Organization, Channel, Product, Announcement, Approval, Employee, Salary, Contract, SalaryField, Commission, Bonus, BonusAllocation, LeaveType, LeaveTier, Holiday, AttendanceRule, ApprovalFlow, ApprovalFlowStep, Onboarding, Mail, HrSocialConfig, HrAllowanceItem
from models import FinAccount, FinConfig, FinOpening, FinReportTemplate, FinPeriod, fin_level
# 行政管理系统（ADM 一期）：三本台账 + 分类字典
# 行政管理系统（ADM 二期）：行政事务（办公用品 / 印章使用登记 / 快递收发）
from models import (AdmCategory, AdmLicense, AdmContract, AdmAsset,
                    AdmSupply, AdmSupplyFlow, AdmSealUse, AdmExpress, AdmExpense,
                    AdmVehicle, AdmVehRecord)
# 核心业务管理系统（CRM 一期）：保司档案 + 产品合约 + 产品资料库
from models import (Insurer, CrmProductContract, CrmProductDoc, InsurerBranch,
                    InsurerContact, InsurerLinkman, InsurerMetric, CrmContractTier,
                    CrmContractPlan, CrmPersistRule, CrmPersistRuleItem)
# 二十一改（2026-10-03）：业务团队系数设置（系数 × 基准费率）
from models import CrmTeamCoefficient, CrmTaxConfig
# 十六改（2026-10-02）：CRM 唯一性校验规则（后台可开关）
from models import CrmUniqueRule
# 十九改（2026-10-02）：业务团队 · 人力架构 + 人力数据清单（费用政策的取数底表）
# 十九改++（2026-10-02）：合作经代公司基础信息（人力档案「所属经代公司」的维护端口）
from models import CrmManpower, CrmOrgStruct, CrmAgency
from werkzeug.security import generate_password_hash
import json
import os
from datetime import date, datetime, timedelta

# ======================================================================
# ⭐ 演示数据总开关（2026-10-28 新增）
# ----------------------------------------------------------------------
# 海盈通保已进入「真实使用」阶段：CRM 核心业务的演示数据（保司档案、
# 产品合约、缴别费用政策、方案、产品资料、经代公司、人力架构、人力清单、
# 继续率规则、团队系数等）不再自动铺入。
#   False = 只建表 + 建权限 + 建必须的参数配置（税率等），不写业务数据  ← 当前
#   True  = 恢复旧行为，重新灌入整套演示数据（想回到演示环境时改这里）
# 注意：只影响 CRM 演示数据；OA/ADM/EHR/FMS 的种子逻辑不受影响。
# ======================================================================
SEED_CRM_DEMO = False


def get_or_create(model, defaults=None, **kwargs):
    """查重：有就返回，没有就新建。让 seed 可重复执行。"""
    instance = model.query.filter_by(**kwargs).first()
    if instance:
        return instance
    params = dict(kwargs)
    if defaults:
        params.update(defaults)
    instance = model(**params)
    db.session.add(instance)
    db.session.flush()  # 立刻拿到 id，方便后续关联
    return instance


def _migrate_columns():
    """轻量迁移：SQLite 对已存在的表，create_all 不会加新列。
    这里手工检查并补加新增列（仅开发期 SQLite 用，生产 MySQL/PG 换正式迁移工具）。"""
    from sqlalchemy import text, inspect
    inspector = inspect(db.engine)
    # 需要补加的列：表名 -> [(列名, 建列SQL片段)]
    pending = {
        'contract': [('file_name', "VARCHAR(255)")],
        # 公告：发布范围（internal 内部 / website 官网，为未来官网对接预留）
        'announcement': [('scope', "VARCHAR(16)")],
        # 入职登记流程：员工表扩展个人档案字段（个人填报后由主管确认写入）
        'employee': [
            ('gender', "VARCHAR(8)"), ('birth_date', "DATE"), ('email', "VARCHAR(64)"),
            ('work_start_date', "DATE"), ('marital', "VARCHAR(16)"), ('children', "VARCHAR(8)"),
            ('hometown', "VARCHAR(64)"), ('hobby', "VARCHAR(128)"), ('last_job', "VARCHAR(64)"),
            ('leave_date', "DATE"), ('leave_reason', "VARCHAR(200)"),
            # 个人电子信息（#145 第2项：员工登录"我的资料"自助完善）
            ('address', "VARCHAR(200)"), ('emergency_name', "VARCHAR(32)"),
            ('emergency_phone', "VARCHAR(20)"), ('bank_name', "VARCHAR(64)"),
            ('bank_account', "VARCHAR(32)"), ('need_profile', "BOOLEAN DEFAULT 0"),
            # 入职照片（系统总览「员工工卡」展示，存相对 static 的路径）
            ('photo', "VARCHAR(255)"),
            # 身份证号码（人事卡片展示；入册时从入职登记 id_card 带入）
            ('idcard', "VARCHAR(18)"),
        ],
        # 期初建账（#31）：账套表补加「是否已建账」与确认人/时间（新表 fin_opening 由 create_all 直接建）
        'fin_config': [
            ('opening_done', "BOOLEAN DEFAULT 0"),
            ('opening_done_at', "DATETIME"),
            ('opening_done_by', "VARCHAR(32)"),
        ],
        # 凭证流转（#33）：凭证表补加红冲关联与作废留痕（新表 fin_voucher 已存在，create_all 不会加列）
        'fin_voucher': [
            ('red_of_id', "INTEGER"),
            ('void_at', "DATETIME"),
            ('void_by', "VARCHAR(32)"),
        ],
        # 自动记账（#38）：业务单据反查凭证 id
        'commission': [('voucher_id', "INTEGER")],
        'salary': [('voucher_id', "INTEGER")],
        'bonus': [('voucher_id', "INTEGER")],
        'bonus_allocation': [('voucher_id', "INTEGER")],
        # 报销审批单（#38）：金额 + 费用科目 + 凭证反查（仅 type=报销 时填写）
        # 统一申请中心（2026-10-01）：code = 申请单号 前缀-YYYYMM-NNN（如 BX-202610-001）
        # 差勤申请结构化（2026-10-01）：时间/时长/地点/出行方式/子类（请假类别或补卡时段）
        # 薪酬发放申请（2026-10-01）：月份/发薪单位/人数/应发/绩效/实发/单位社保公积金/成本/附件/关联申请
        # 通用附件（2026-10-01）：休假申请的病例/结婚证/生育证明等（JSON 数组文本）
        'approval': [('amount', "FLOAT"), ('expense_account', "VARCHAR(32)"),
                     ('voucher_id', "INTEGER"), ('code', "VARCHAR(32)"),
                     ('start_time', "VARCHAR(20)"), ('end_time', "VARCHAR(20)"),
                     ('att_days', "FLOAT"), ('place', "VARCHAR(128)"),
                     ('transport', "VARCHAR(32)"), ('sub_type', "VARCHAR(32)"),
                     ('sa_month', "VARCHAR(16)"), ('pay_entity', "VARCHAR(64)"),
                     ('headcount', "INTEGER"), ('gross_amount', "FLOAT"),
                     ('perf_amount', "FLOAT"), ('actual_amount', "FLOAT"),
                     ('corp_social', "FLOAT"), ('corp_fund', "FLOAT"),
                     ('salary_cost', "FLOAT"), ('labor_cost', "FLOAT"),
                     ('attach_file', "VARCHAR(255)"), ('related_code', "VARCHAR(200)"),
                     ('attach_files', "TEXT")],
        # 假期类型（2026-10-01）：当年折算 / 计量单位（事假按小时）/ 是否必须附件 + 附件说明
        # （新表 leave_tier「假期分级档位」由 create_all 直接建，无需在此登记）
        'leave_type': [('prorate', "VARCHAR(4)"), ('unit', "VARCHAR(8)"),
                       ('need_attach', "VARCHAR(4)"), ('attach_hint', "VARCHAR(64)")],
        # 费用报销单（2026-10-01）：电子发票上传（JSON 数组文本，存文件名清单）
        'expense_reimburse': [('attach_files', "TEXT")],
        # 企业微信接入（2026-09-28）：用户绑定企微 userid，实现免密登录
        # 信息技术管理系统（2026-11-03）：用户级二级菜单模块授权，新增 is_admin 标识
        'user': [('wecom_userid', "VARCHAR(64)"), ('is_admin', "BOOLEAN DEFAULT 0")],
        # 保司档案（2026-10-01 三改）：联系与对接仅对接人三项 + 身份职务
        # 四十一改（2026-10-29）：再加「许可证批准设立日期」。
        # ⚠️ 必须并到**同一个 'insurer' 键**里 —— dict 字面量重复键会后者覆盖前者，
        #    分开写两处 `'insurer': [...]` 只有最后一处生效（本次踩过：license_date 没加上）。
        'insurer': [('contact_title', "VARCHAR(64)"),
                    ('license_date', "VARCHAR(16)"),
                    ('fetch_try_at', "VARCHAR(20)")],
        # 四十二改（2026-10-29）：省分公司也支持一键抓取（监管站按分公司全称精确命中）
        #   license_date＝监管口径批准日；fetch_try_at＝批量轮转排队的时间戳。
        'insurer_branch': [('license_date', "VARCHAR(16)"),
                           ('fetch_try_at', "VARCHAR(20)")],
        # 四十三改（2026-10-29）：产品合约加「销售渠道」（online 网销 / offline 线下）
        # 注：CrmContractFile（合约多附件新表）由 create_all 直建，无需在此登记
        # ⚠️ 同表名只能有一个键：本块在下方 199 行处，加列一律并进那一处，勿在此另起。
        # 产品合约·缴别基础合约（2026-10-02 四改）：总总/分分 各自的合约起止日（费率有效期）
        # 注：CrmContractPlan（新表）与 InsurerLinkman（新表）由 create_all 直建，无需在此登记
        'crm_contract_tier': [
            ('tt_start', "VARCHAR(16)"), ('tt_end', "VARCHAR(16)"),
            ('ff_start', "VARCHAR(16)"), ('ff_end', "VARCHAR(16)"),
            # 二十八改（2026-10-29）：定制类产品「按组件录费率」→ 缴别行挂所属组件
            ('component_id', "INTEGER"),
        ],
        # 三十一改（2026-10-29）：续期继续率分档补「区间上限」——威哥「继续率是要有区间的」。
        # ⚠️ crm_contract_ryc_step 是二十九改建的表（create_all 已建过），加列必须登记 pending，
        #    否则库里没这列、一保存就报错。
        'crm_contract_ryc_step': [
            ('max_rate', "FLOAT"),
        ],
        # 团队系数（2026-10-05 二十二改）：增加「适用分区 / 适用营业部」适用范围
        # （空 = 总计/全员；与 team_type + owner_name 一起构成唯一组合键）
        'crm_team_coefficient': [
            ('scope_zone', "VARCHAR(32)"), ('scope_dept', "VARCHAR(64)"),
        ],
        # 保费测算因子表（2026-10-05）：分红型产品的红利——计划书「红利利益演示」用
        # div_g＝保证利益演示口径的当年红利；div_d＝红利利益演示口径的当年红利（非保证）
        'crm_premium_factor': [
            ('div_g', "FLOAT DEFAULT 0"), ('div_d', "FLOAT DEFAULT 0"),
            # 二十 three 改（2026-10-06）：第三种领取方式「购买交清增额保险」的因子
            ('pua_sa', "FLOAT DEFAULT 0"), ('pua_add', "FLOAT DEFAULT 0"), ('pua_cv', "FLOAT DEFAULT 0"),
        ],
        # 分红演示（2026-10-06）：红利两种领取方式 —— 现金分红(现金领取) / 累积生息(年复利)
        # 条款3.2：现金领取 / 累积生息；累积生息利率默认 1.75%（保司因子档「分红信息配置」定价利率）
        'crm_proposal': [
            ('div_mode', "VARCHAR(16) DEFAULT 'accum'"),
            ('accum_rate', "FLOAT DEFAULT 0.0175"),
        ],
        'crm_proposal_product': [
            ('accum_rate', "FLOAT DEFAULT 0.0175"),
            ('div_ratio', "FLOAT DEFAULT 0"),
            ('div_demo_rate', "FLOAT DEFAULT 0"),
            # 二十三改（2026-10-06）：计划书产品分类（驱动分类智能体）
            ('category', "VARCHAR(24) DEFAULT ''"),
        ],
        # 部门架构设置（#145 第4项）：组织表补加编制人数
        'organization': [('headcount', "INTEGER")],
        # 新员工招募（#145）：Onboarding 补加招募阶段字段（身份证/年龄/试用期/薪资/五险一金）
        'onboarding': [
            ('sys_code', "VARCHAR(16)"),
            ('id_card', "VARCHAR(18)"), ('age', "INTEGER"),
            ('probation', "VARCHAR(1)"), ('probation_start', "DATE"),
            ('probation_end', "DATE"), ('probation_salary_pct', "FLOAT"),
            ('regular_total', "FLOAT"), ('regular_base', "FLOAT"),
            ('social_pension', "FLOAT"), ('social_medical', "FLOAT"),
            ('social_unemploy', "FLOAT"), ('social_fund', "FLOAT"),
            # 退回修改（#145）：主管审核不通过时填写退回原因
            ('return_reason', "VARCHAR(200)"),
            # 薪资构成拆分（基本/绩效/岗位补贴 + 补贴明细）
            ('regular_perf', "FLOAT"), ('regular_allow', "FLOAT"),
            ('allowance_detail', "TEXT"),
        ],
        # 五险一金配置：补加「单位承担比例」（用于测算总人力成本）
        'hr_social_config': [
            ('corp_pension_pct', "FLOAT"), ('corp_medical_pct', "FLOAT"),
            ('corp_unemploy_pct', "FLOAT"), ('corp_injury_pct', "FLOAT"),
            ('corp_maternity_pct', "FLOAT"), ('corp_fund_pct', "FLOAT"),
        ],
        # 核心业务（CRM）二期（2026-10-01）：产品合约主表挂省分公司 + 继续率考核规则
        #（省分公司 / 岗位联络 / 经营指标 / 缴别明细 / 考核规则均为纯新增表，create_all 直建）
        'crm_product_contract': [
            ('branch_id', "INTEGER"), ('branch_name', "VARCHAR(128)"),
            ('persist_rule_id', "INTEGER"), ('persist_rule_name', "VARCHAR(64)"),
            # 十三改（2026-10-02）：文字类方案（现金 / 旅游 / 实物 / 权益）合约级一个文本框
            ('misc_plan_text', "TEXT"),
            # 二十七改2（2026-10-29）：定制类产品（组合产品）标记
            #（新表 crm_contract_component 由 create_all 直建，无需在此登记）
            ('is_custom', "INTEGER DEFAULT 0"),
            # 三十五改（2026-10-29）：保障期限（保终身 / 保至70岁 / 自定义）—— 一条合约一个
            ('coverage_term', "VARCHAR(64) DEFAULT ''"),
            # 四十三改（2026-10-29）：销售渠道（online 网销 / offline 线下 / 空 未指定）
            # 注：CrmContractFile（合约多附件新表）由 create_all 直建，无需在此登记
            ('sale_channel', "VARCHAR(16) DEFAULT ''"),
            # 四十四改（2026-10-29）：AI 机器人识别协议后的待审草稿标记
            # ai_draft=1 时合约列表默认不显示，人工在编辑页保存一次即清 0
            # 注：AiConfig（大模型接入配置新表）由 create_all 直建
            ('ai_draft', "INTEGER DEFAULT 0"),
            ('ai_note', "TEXT"),
        ],
        # 核心业务六改（2026-10-02）：
        # ① 方案政策加「协议外净费形式 + 其他备注」（费用形式改造）
        #    八改+：再补 fee_type 费项类型（对齐真实协议「变动费用」4 类）
        'crm_contract_plan': [
            ('pay_channel', "VARCHAR(16)"), ('other_remark', "VARCHAR(200)"),
            ('fee_type', "VARCHAR(16)"),
            # 十五改（2026-10-02）：文字类方案说明（现金/购物或其他）——归并回「四、」后按方案存
            ('plan_text', "TEXT"),
        ],
        # ② 继续率规则改为「规则侧登记适用范围」，反向匹配产品合约
        'crm_persist_rule': [
            ('insurer_id', "INTEGER"), ('insurer_name', "VARCHAR(128)"),
            ('branch_id', "INTEGER"), ('branch_name', "VARCHAR(128)"),
            ('product_names', "TEXT"), ('pay_periods', "TEXT"),
            # 七改：分公司改多选 + 奖励项目
            ('branch_ids', "TEXT"), ('award_items', "VARCHAR(64)"),
            # 八改：继续率考核标准改多选
            ('cycles', "TEXT"),
            # 八改+：倒扣结构化（caliber / round_mode 已于十五改删除，见模型注释）
            ('clawback_line', "FLOAT"), ('clawback_base', "VARCHAR(16)"),
            # 二十四改（2026-10-28）：继续率规则跟着产品合约走 —— 规则挂合约
            ('contract_id', "INTEGER"),
        ],
        # 八改：档位加「归属标准」列（继续率考核标准名 / long_service）
        # 九改：档位加「下限 / 上限 运算符」列（是否含等号）
        'crm_persist_rule_item': [
            ('cycle', "VARCHAR(32)"),
            ('min_op', "VARCHAR(4)"), ('max_op', "VARCHAR(4)"),
            # 二十四改（2026-10-28）：档位带缴别（同一区间不同缴费年期奖金率不同）
            ('pay_period', "VARCHAR(32)"),
        ],
        # 十九改（2026-10-02）：人力数据清单 —— 挂人力架构 + 系统工号
        # （CrmOrgStruct 是纯新增表，由 create_all 直建，无需在此登记）
        'crm_manpower': [
            ('org_id', "INTEGER"), ('org_path', "VARCHAR(200)"),
            ('sys_no', "VARCHAR(32)"),
            # 十九改+（2026-10-02）：推荐人姓名 / 开户支行 / 三个非强制电子版附件
            ('ref_name', "VARCHAR(32)"), ('bank_branch', "VARCHAR(128)"),
            ('sign_file_name', "VARCHAR(255)"), ('sign_file', "VARCHAR(255)"),
            ('idcard_file_name', "VARCHAR(255)"), ('idcard_file', "VARCHAR(255)"),
            ('bank_file_name', "VARCHAR(255)"), ('bank_file', "VARCHAR(255)"),
            # 十九改++（2026-10-02）：身份证电子版要**正反面**两张
            ('idcard_file2_name', "VARCHAR(255)"), ('idcard_file2', "VARCHAR(255)"),
        ],
        # 投保订单录入（2026-10-05 落地）：加「缴费年期」字段，按实际年限精确折标行业标保
        'crm_policy_order': [
            ('pay_years', "INTEGER"),
            # 新契约查询（2026-10）：数据来源标记（手工录入 / Excel 导入），默认 manual
            ('src', "VARCHAR(16) DEFAULT 'manual'"),
        ],
        # 产品佣金系数（2026-11-02 新增模块）：
        #   crm_product_commission 加「归属月份」（按月度区分导入批次，同月覆盖）
        #   crm_comm_coef_config 加 团队长/销售费用/RYC佣金 三项比例
        'crm_product_commission': [('month', "VARCHAR(16)")],
        'crm_comm_coef_config': [
            ('team_base_ratio', "FLOAT"),
            ('sales_ratio', "FLOAT"),
            ('ryc_base_ratio', "FLOAT"),
            # RYC 分年期佣金比例（每年可不同）
            ('ryc_ratio_y2', "FLOAT"), ('ryc_ratio_y3', "FLOAT"),
            ('ryc_ratio_y4', "FLOAT"), ('ryc_ratio_y5', "FLOAT"),
            # 二改（2026-11-03）：RYC续年度佣金<1%自动置空开关
            ('ryc_hide_lt1', "BOOLEAN DEFAULT 0"),
        ],
        # 产品佣金系数月度备份：同步记录该开关（与 crm_comm_coef_config 口径一致）
        'crm_comm_coef_backup': [('ryc_hide_lt1', "BOOLEAN DEFAULT 0")],
    }
    for table, cols in pending.items():
        if table not in inspector.get_table_names():
            continue
        existing = {c['name'] for c in inspector.get_columns(table)}
        for col, dtype in cols:
            if col not in existing:
                db.session.execute(text('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, dtype)))
                print('  [迁移] 表 %s 补加列 %s' % (table, col))
    # 投保订单：历史数据（新增 src 列前录入的）补默认 manual，避免「手工录入」筛选漏掉
    if 'crm_policy_order' in inspector.get_table_names():
        try:
            rb = db.session.execute(text("UPDATE crm_policy_order SET src='manual' WHERE src IS NULL OR src=''"))
            if rb.rowcount:
                print('  [迁移] 投保订单补 src=manual %d 条' % rb.rowcount)
        except Exception:
            db.session.rollback()
    db.session.commit()


def _migrate_values():
    """值级迁移（幂等）：只改历史数据的字段取值，不动表结构。

    十一改（2026-10-02）：险种类型常量里「增额终身寿险」并入「终身寿险」，
    产品合约上已存旧值的记录同步改过来，否则下拉里选不到、编辑保存会被清空。
    """
    from sqlalchemy import inspect
    if 'crm_product_contract' not in inspect(db.engine).get_table_names():
        return
    n = CrmProductContract.query.filter_by(insurance_type='增额终身寿险').update(
        {'insurance_type': '终身寿险'}, synchronize_session=False)
    if n:
        db.session.commit()
        print('  [迁移] 险种类型 增额终身寿险 → 终身寿险 %d 条' % n)

    # 十九改+（2026-10-02）：销售能力等级 → **销售资质等级**（监管「持证+分级」口径）
    #   A级→一级（最高），B级→二级，C级→三级，D级→四级（入门）
    if 'crm_manpower' in inspect(db.engine).get_table_names():
        for old, new in (('A级', '一级'), ('B级', '二级'), ('C级', '三级'), ('D级', '四级')):
            k = CrmManpower.query.filter_by(sale_level=old).update(
                {'sale_level': new}, synchronize_session=False)
            if k:
                db.session.commit()
                print('  [迁移] 销售资质等级 %s → %s %d 条' % (old, new, k))

        # 十九改++（2026-10-02）：**职级按团队类型分两套** ——
        #   基本法团队：部经理 / 主管 / 营销员；渠道团队：渠道总 / 团队长 / 营销员。
        #   历史数据里基本法团队用的是渠道那套职级，这里折算过去（营销员两边同名，不用动）。
        for old, new in (('channel_boss', 'dept_manager'), ('team_leader', 'supervisor')):
            k = CrmManpower.query.filter_by(team_type='basic', rank=old).update(
                {'rank': new}, synchronize_session=False)
            if k:
                db.session.commit()
                print('  [迁移] 基本法团队职级 %s → %s %d 条' % (old, new, k))


def _backfill_onboard_sys_code():
    """给历史入职登记补系统编码（#145 补充）：RC0001 起顺序编号，跳过已占用的号码。
    工号可由人事修改，系统编码是登记记录的稳定标识，历史数据必须补齐。"""
    rows = Onboarding.query.order_by(Onboarding.id).all()
    used = {r.sys_code for r in rows if r.sys_code}
    seq, changed = 1, 0
    for r in rows:
        if r.sys_code:
            continue
        while ('RC%04d' % seq) in used:
            seq += 1
        r.sys_code = 'RC%04d' % seq
        used.add(r.sys_code)
        seq += 1
        changed += 1
    if changed:
        db.session.commit()
        print('  [迁移] 入职登记补系统编码 %d 条' % changed)


def _backfill_approval_code():
    """给历史申请单补单号（统一申请中心，2026-10-01）：
    按申请类型映射前缀（请假 QJ / 出差 CC / 补卡 BK / 报销 BX / 其他 SP），
    同前缀同月份从 001 起顺序编号；已有单号的不动。幂等，可重复执行。"""
    ap_prefix = {'请假': 'QJ', '出差': 'CC', '补卡': 'BK', '报销': 'BX', '通用': 'SP'}
    rows = Approval.query.order_by(Approval.id).all()
    used = {r.code for r in rows if r.code}
    seq, changed = {}, 0
    for r in rows:
        if r.code:
            continue
        pfx = ap_prefix.get(r.type or '', 'SP')
        ym = (r.created_at or datetime.utcnow()).strftime('%Y%m')
        key = (pfx, ym)
        n = seq.get(key, 0) + 1
        code = '%s-%s-%03d' % (pfx, ym, n)
        while code in used:            # 撞号就往后顺延
            n += 1
            code = '%s-%s-%03d' % (pfx, ym, n)
        seq[key] = n
        r.code = code
        used.add(code)
        changed += 1
    if changed:
        db.session.commit()
        print('  [迁移] 申请单补单号 %d 条' % changed)


# 预设会计科目（《小企业会计准则》编号口径 + 保险中介业务特点）
# 每条：(科目编码, 科目名称, 类别, 余额方向, 上级科目编码, 辅助核算)
# 说明：公司不代收客户保费，所以没有"代收保费"负债科目；
#       应付代理人佣金走 224101（海腾代发），佣金外费用公对公结算。
FIN_ACCOUNTS_PRESET = [
    # ---- 资产类 ----
    ('1001', '库存现金', 'asset', 'debit', '', ''),
    ('1002', '银行存款', 'asset', 'debit', '', ''),
    ('1122', '应收账款', 'asset', 'debit', '', 'customer'),
    ('112201', '应收代理手续费', 'asset', 'debit', '1122', 'customer'),
    ('1123', '预付账款', 'asset', 'debit', '', ''),
    ('1221', '其他应收款', 'asset', 'debit', '', 'employee'),
    ('1601', '固定资产', 'asset', 'debit', '', 'dept'),
    ('1602', '累计折旧', 'asset', 'credit', '', ''),
    ('1701', '无形资产', 'asset', 'debit', '', ''),
    # ---- 负债类 ----
    ('2202', '应付账款', 'liability', 'credit', '', ''),
    ('2203', '预收账款', 'liability', 'credit', '', ''),
    ('2211', '应付职工薪酬', 'liability', 'credit', '', ''),
    ('221101', '应付工资', 'liability', 'credit', '2211', 'dept'),
    ('221102', '应付社会保险费', 'liability', 'credit', '2211', ''),
    ('221103', '应付住房公积金', 'liability', 'credit', '2211', ''),
    ('2221', '应交税费', 'liability', 'credit', '', ''),
    ('222101', '应交增值税', 'liability', 'credit', '2221', ''),
    ('222102', '应交城市维护建设税', 'liability', 'credit', '2221', ''),
    ('222103', '应交教育费附加', 'liability', 'credit', '2221', ''),
    ('222104', '应交企业所得税', 'liability', 'credit', '2221', ''),
    ('222105', '应交个人所得税', 'liability', 'credit', '2221', ''),
    ('2241', '其他应付款', 'liability', 'credit', '', ''),
    ('224101', '应付代理人佣金（海腾代发）', 'liability', 'credit', '2241', 'employee'),
    ('224102', '应付其他款项', 'liability', 'credit', '2241', ''),
    # ---- 所有者权益类 ----
    ('3001', '实收资本', 'equity', 'credit', '', ''),
    ('3002', '资本公积', 'equity', 'credit', '', ''),
    ('3101', '盈余公积', 'equity', 'credit', '', ''),
    ('3103', '本年利润', 'equity', 'credit', '', ''),
    ('3104', '利润分配', 'equity', 'credit', '', ''),
    # ---- 损益类（收入）----
    ('5001', '主营业务收入', 'profit', 'credit', '', ''),
    ('500101', '代理手续费收入', 'profit', 'credit', '5001', 'customer'),
    ('5051', '其他业务收入', 'profit', 'credit', '', ''),
    ('5301', '营业外收入', 'profit', 'credit', '', ''),
    # ---- 损益类（成本费用）----
    ('5401', '主营业务成本', 'profit', 'debit', '', ''),
    ('540101', '代理手续费支出', 'profit', 'debit', '5401', 'employee'),
    ('5402', '其他业务成本', 'profit', 'debit', '', ''),
    ('5403', '税金及附加', 'profit', 'debit', '', ''),
    ('5601', '销售费用', 'profit', 'debit', '', 'dept'),
    ('5602', '管理费用', 'profit', 'debit', '', 'dept'),
    ('560201', '工资薪酬', 'profit', 'debit', '5602', 'dept'),
    ('560202', '办公费', 'profit', 'debit', '5602', 'dept'),
    ('560203', '差旅费', 'profit', 'debit', '5602', 'dept'),
    ('560204', '房租水电费', 'profit', 'debit', '5602', ''),
    ('560205', '折旧费', 'profit', 'debit', '5602', ''),
    ('560206', '业务招待费', 'profit', 'debit', '5602', ''),
    # —— OA 费用申请/报销 专用明细科目（2026-09-30 新增，供费用大类映射记账）——
    ('560207', '通讯费', 'profit', 'debit', '5602', 'dept'),
    ('560208', '车辆使用费', 'profit', 'debit', '5602', 'dept'),
    ('560209', '会议费', 'profit', 'debit', '5602', 'dept'),
    ('560299', '其他管理费用', 'profit', 'debit', '5602', 'dept'),
    # —— 费用类别扩充（2026-10-01）：报销/费用申请的类别细化后，配套补明细科目 ——
    # 口径：界面类别可以很细（油费/路桥费/停车费…），但会计科目适度合并，
    #       避免小公司科目表碎掉。见 models.EXPENSE_CATEGORY_GROUPS 的映射表。
    # 销售费用 5601 明细（补上后 5601 自动变汇总科目，不能再直接做分录）
    ('560101', '市场推广费', 'profit', 'debit', '5601', 'dept'),
    ('560102', '广告宣传费', 'profit', 'debit', '5601', 'dept'),
    ('560103', '销售激励费', 'profit', 'debit', '5601', 'dept'),
    # 管理费用 5602 明细
    ('560210', '交通费', 'profit', 'debit', '5602', 'dept'),
    ('560211', '租赁费', 'profit', 'debit', '5602', 'dept'),
    ('560212', '快递费', 'profit', 'debit', '5602', 'dept'),
    ('560213', '印刷费', 'profit', 'debit', '5602', 'dept'),
    ('560214', '维修费', 'profit', 'debit', '5602', 'dept'),
    ('560215', '培训费', 'profit', 'debit', '5602', 'dept'),
    ('560216', '社会保险费', 'profit', 'debit', '5602', 'dept'),
    ('560217', '住房公积金', 'profit', 'debit', '5602', 'dept'),
    ('560218', '职工福利费', 'profit', 'debit', '5602', 'dept'),
    ('560219', '员工商业保险', 'profit', 'debit', '5602', 'dept'),
    ('560220', '招聘费', 'profit', 'debit', '5602', ''),
    ('560221', '残疾人就业保障金', 'profit', 'debit', '5602', ''),
    ('560222', '咨询费', 'profit', 'debit', '5602', ''),
    ('560223', '律师费', 'profit', 'debit', '5602', ''),
    ('560224', '审计费', 'profit', 'debit', '5602', ''),
    ('560225', '技术服务费', 'profit', 'debit', '5602', ''),
    ('560226', '装修费', 'profit', 'debit', '5602', ''),
    ('560227', '开办费', 'profit', 'debit', '5602', ''),
    ('560228', '研发费用', 'profit', 'debit', '5602', 'dept'),
    ('560229', '服务费', 'profit', 'debit', '5602', ''),
    # 财务费用 5603 明细（利息收入为贷方余额，记贷方冲减财务费用）
    ('560301', '银行手续费', 'profit', 'debit', '5603', ''),
    ('560302', '利息支出', 'profit', 'debit', '5603', ''),
    ('560303', '利息收入', 'profit', 'credit', '5603', ''),
    # 税金及附加 5403 明细
    ('540301', '印花税', 'profit', 'debit', '5403', ''),
    ('540302', '其他税费', 'profit', 'debit', '5403', ''),
    ('5603', '财务费用', 'profit', 'debit', '', ''),
    ('5711', '营业外支出', 'profit', 'debit', '', ''),
    ('5801', '所得税费用', 'profit', 'debit', '', ''),
]


def _seed_fin_accounts():
    """写入预设会计科目。
    可重复执行：按编码查重，已存在的直接跳过——不覆盖你后来改过的名称与设置。"""
    for code, name, category, direction, parent, aux in FIN_ACCOUNTS_PRESET:
        if FinAccount.query.filter_by(code=code).first():
            continue
        db.session.add(FinAccount(code=code, name=name, category=category,
                                  direction=direction, parent_code=parent,
                                  level=fin_level(code), is_leaf=True,
                                  enabled=True, aux=aux, note='系统预设'))
    db.session.flush()
    # 回填"是否明细科目"：凡被别的科目挂了上级的，自己就是汇总科目（不能直接做分录）
    all_acc = FinAccount.query.all()
    used_as_parent = {a.parent_code for a in all_acc if a.parent_code}
    for a in all_acc:
        a.is_leaf = a.code not in used_as_parent


# 演示期初余额（一组借贷平衡的数据：银行存款 50 万 = 实收资本 50 万）
# 用途：让「期初建账」页一打开就能看到试算平衡长什么样；正式建账前可在页面点「清空期初」重录。
FIN_OPENING_DEMO = [
    # (科目编码, 期初借方, 期初贷方)
    ('1002', 500000.00, 0.0),      # 银行存款
    ('3001', 0.0, 500000.00),      # 实收资本
]


def _seed_fin_opening():
    """写入演示期初。可重复执行：只要已有任何期初数据就整体跳过，不覆盖你录的正式数据。"""
    if FinOpening.query.first():
        return
    cfg = FinConfig.query.filter_by(code='default').first()
    period = cfg.start_period if cfg else ''
    for code, d, c in FIN_OPENING_DEMO:
        a = FinAccount.query.filter_by(code=code).first()
        if not a:
            continue
        db.session.add(FinOpening(period=period, account_code=a.code, account_name=a.name,
                                  category=a.category, direction=a.direction,
                                  debit=d, credit=c, ytd_debit=0, ytd_credit=0,
                                  updated_by='system'))
    db.session.flush()


# 预设系统报表模板（增量 #35）
# 每个行项目 = 一条取数公式，支持六个函数：QC 期初 / QM 期末 / DF 本期借方 /
#   CF 本期贷方 / BF 本期净发生 / LF 本年累计净发生。支持 + - 与括号。
# 两条关键口径（写公式时必须守住）：
#   ① **只取一级科目**——明细科目已由系统 rollup 汇总到一级，再写明细会重复计算；
#   ② 资产负债表「未分配利润」要把**未结转的损益类科目余额**一起算进来，
#      否则没做期末结转账就永远"资产 ≠ 负债 + 所有者权益"。结账后 5xxx 余额归零，
#      该式自动退化为 3103 本年利润 + 3104 利润分配，无需改公式。
BS_ROWS = [
    {'label': '流动资产：', 'side': 'left', 'head': True},
    {'label': '货币资金', 'formula': 'QM(1001)+QM(1002)', 'line': '1', 'side': 'left'},
    {'label': '应收账款', 'formula': 'QM(1122)', 'line': '4', 'side': 'left'},
    {'label': '预付账款', 'formula': 'QM(1123)', 'line': '5', 'side': 'left'},
    {'label': '其他应收款', 'formula': 'QM(1221)', 'line': '8', 'side': 'left'},
    {'label': '流动资产合计', 'line': '15', 'side': 'left', 'total': True,
     'formula': 'QM(1001)+QM(1002)+QM(1122)+QM(1123)+QM(1221)'},
    {'label': '非流动资产：', 'side': 'left', 'head': True},
    {'label': '固定资产原价', 'formula': 'QM(1601)', 'line': '19', 'side': 'left'},
    {'label': '减：累计折旧', 'formula': 'QM(1602)', 'line': '20', 'side': 'left'},
    {'label': '固定资产账面价值', 'formula': 'QM(1601)-QM(1602)', 'line': '21', 'side': 'left'},
    {'label': '无形资产', 'formula': 'QM(1701)', 'line': '26', 'side': 'left'},
    {'label': '非流动资产合计', 'line': '30', 'side': 'left', 'total': True,
     'formula': 'QM(1601)-QM(1602)+QM(1701)'},
    {'label': '资产总计', 'line': '31', 'side': 'left', 'total': True, 'key': 'total_assets',
     'formula': 'QM(1001)+QM(1002)+QM(1122)+QM(1123)+QM(1221)+QM(1601)-QM(1602)+QM(1701)'},

    {'label': '流动负债：', 'side': 'right', 'head': True},
    {'label': '应付账款', 'formula': 'QM(2202)', 'line': '34', 'side': 'right'},
    {'label': '预收账款', 'formula': 'QM(2203)', 'line': '35', 'side': 'right'},
    {'label': '应付职工薪酬', 'formula': 'QM(2211)', 'line': '36', 'side': 'right'},
    {'label': '应交税费', 'formula': 'QM(2221)', 'line': '37', 'side': 'right'},
    {'label': '其他应付款', 'formula': 'QM(2241)', 'line': '40', 'side': 'right'},
    {'label': '流动负债合计', 'line': '42', 'side': 'right', 'total': True,
     'formula': 'QM(2202)+QM(2203)+QM(2211)+QM(2221)+QM(2241)'},
    {'label': '负债合计', 'line': '49', 'side': 'right', 'total': True,
     'formula': 'QM(2202)+QM(2203)+QM(2211)+QM(2221)+QM(2241)'},
    {'label': '所有者权益：', 'side': 'right', 'head': True},
    {'label': '实收资本', 'formula': 'QM(3001)', 'line': '50', 'side': 'right'},
    {'label': '资本公积', 'formula': 'QM(3002)', 'line': '51', 'side': 'right'},
    {'label': '盈余公积', 'formula': 'QM(3101)', 'line': '52', 'side': 'right'},
    {'label': '未分配利润', 'line': '53', 'side': 'right',
     'formula': 'QM(3103)+QM(3104)+QM(5001)+QM(5051)+QM(5301)'
                '-QM(5401)-QM(5402)-QM(5403)-QM(5601)-QM(5602)-QM(5603)-QM(5711)-QM(5801)'},
    {'label': '所有者权益合计', 'line': '54', 'side': 'right', 'total': True,
     'formula': 'QM(3001)+QM(3002)+QM(3101)+QM(3103)+QM(3104)'
                '+QM(5001)+QM(5051)+QM(5301)'
                '-QM(5401)-QM(5402)-QM(5403)-QM(5601)-QM(5602)-QM(5603)-QM(5711)-QM(5801)'},
    {'label': '负债和所有者权益总计', 'line': '55', 'side': 'right', 'total': True,
     'key': 'total_liab_equity',
     'formula': 'QM(2202)+QM(2203)+QM(2211)+QM(2221)+QM(2241)'
                '+QM(3001)+QM(3002)+QM(3101)+QM(3103)+QM(3104)'
                '+QM(5001)+QM(5051)+QM(5301)'
                '-QM(5401)-QM(5402)-QM(5403)-QM(5601)-QM(5602)-QM(5603)-QM(5711)-QM(5801)'},
]

# 利润表（会小企02表）：损益类用 BF（净发生额，按科目方向取正），
# 所以收入是加项、成本费用是减项；换 amount 口径即得「本期金额 / 本年累计金额」两列。
_PL_BIZ = 'BF(5001)+BF(5051)-BF(5401)-BF(5402)-BF(5403)-BF(5601)-BF(5602)-BF(5603)'
PL_ROWS = [
    {'label': '一、营业收入', 'formula': 'BF(5001)+BF(5051)', 'line': '1', 'key': 'revenue'},
    {'label': '减：营业成本', 'formula': 'BF(5401)+BF(5402)', 'line': '2'},
    {'label': '税金及附加', 'formula': 'BF(5403)', 'line': '3'},
    {'label': '销售费用', 'formula': 'BF(5601)', 'line': '11'},
    {'label': '管理费用', 'formula': 'BF(5602)', 'line': '14'},
    {'label': '财务费用', 'formula': 'BF(5603)', 'line': '18'},
    {'label': '二、营业利润', 'formula': _PL_BIZ, 'line': '20', 'total': True, 'key': 'op_profit'},
    {'label': '加：营业外收入', 'formula': 'BF(5301)', 'line': '21'},
    {'label': '减：营业外支出', 'formula': 'BF(5711)', 'line': '22'},
    {'label': '三、利润总额', 'line': '30', 'total': True, 'key': 'total_profit',
     'formula': _PL_BIZ + '+BF(5301)-BF(5711)'},
    {'label': '减：所得税费用', 'formula': 'BF(5801)', 'line': '31'},
    {'label': '四、净利润', 'line': '32', 'total': True, 'key': 'net_profit',
     'formula': _PL_BIZ + '+BF(5301)-BF(5711)-BF(5801)'},
]

FIN_REPORT_PRESET = [
    ('bs', '资产负债表（会小企01表）',
     '资产 = 负债 + 所有者权益；年初余额列取年初数，期末余额列取报表期期末数'),
    ('pl', '利润表（会小企02表）',
     '本期金额列 + 本年累计金额列；损益类取净发生额，不受是否已结转影响'),
]


def _seed_fin_reports():
    """写入系统内置报表模板。可重复执行：同类别已有内置模板就跳过，
    不覆盖你后来另存的自定义报表。（现金流量表由系统按凭证对方科目自动归类，不走公式引擎）"""
    preset_rows = {'bs': BS_ROWS, 'pl': PL_ROWS}
    for cat, name, note in FIN_REPORT_PRESET:
        if FinReportTemplate.query.filter_by(category=cat, is_system=True).first():
            continue
        db.session.add(FinReportTemplate(
            name=name, category=cat,
            rows=json.dumps(preset_rows[cat], ensure_ascii=False),
            is_system=True, created_by='system', note=note))
    db.session.flush()


# 行政管理系统（ADM）分类字典默认值：证照类型 / 合同类型 / 资产类别
ADM_CATEGORY_SEED = [
    ('license', ['营业执照', '经营许可证', '资质证书', '公司印章', '商标专利', '其他']),
    ('contract', ['房屋租赁', '物业服务', '采购合同', '服务外包', '保险合同', '其他']),
    ('asset', ['电子设备', '办公家具', '车辆', '其他']),
    # 二期（行政事务）
    ('supply', ['文具耗材', '打印耗材', '办公设备', '清洁用品', '饮用水', '劳保用品', '其他']),
    ('seal', ['公司公章', '合同专用章', '财务专用章', '发票专用章', '法人章', '人事专用章']),
    # 三期（费用台账）
    ('expense', ['水费', '电费', '物业费', '房租', '宽带通讯', '车险', '办公费', '其他']),
    # 四期（车辆管理）
    ('vehicle', ['轿车', 'SUV / 越野车', '商务车 / MPV', '货车', '客车', '其他']),
]


def _seed_adm_categories():
    """写入行政分类字典（可重复执行：同 kind+name 已存在则跳过）。"""
    for kind, names in ADM_CATEGORY_SEED:
        for i, nm in enumerate(names):
            get_or_create(AdmCategory, kind=kind, name=nm, defaults={'sort': i + 1})
    db.session.flush()


def _seed_adm_demo():
    """写入行政台账演示数据（可重复执行：按编号查重）。"""
    admin = User.query.filter_by(username='admin').first()
    uid = admin.id if admin else None

    # ① 证照资质：营业执照（长期有效）+ 保险中介许可证（有到期日）
    get_or_create(AdmLicense, code='ZZ-DEMO-001', defaults={
        'name': '营业执照', 'category': '营业执照', 'no': '91440101MA5DEMO0X',
        'issuer': '广州市市场监督管理局', 'issue_date': '2020-06-18', 'expire_date': '',
        'keeper': '行政专员', 'place': '公司保险柜', 'note': '长期有效，每年 6 月前完成企业年报',
        'created_by': uid})
    get_or_create(AdmLicense, code='ZZ-DEMO-002', defaults={
        'name': '保险中介业务许可证', 'category': '经营许可证', 'no': '粤保中介字 2020-0018',
        'issuer': '国家金融监督管理总局广东监管局', 'issue_date': '2023-03-01', 'expire_date': '2027-03-01',
        'keeper': '行政专员', 'place': '公司保险柜', 'note': '到期前 3 个月启动换证',
        'created_by': uid})

    # ② 商务合同：办公室房屋租赁
    get_or_create(AdmContract, code='SWHT-DEMO-001', defaults={
        'name': '广州办公室房屋租赁合同', 'category': '房屋租赁', 'party': '示范物业管理有限公司',
        'sign_date': '2024-12-20', 'start_date': '2025-01-01', 'end_date': '2027-12-31',
        'amount': 288000.0, 'pay_mode': 'install', 'pay_plan': '每月 5 日前付 ¥8,000，共 36 期',
        'handler': '行政专员', 'dept': '人事行政部', 'status': 'active',
        'note': '到期前 2 个月谈续租', 'created_by': uid})

    # ③ 固定资产：笔记本电脑 + 办公桌椅
    get_or_create(AdmAsset, code='ZC-DEMO-001', defaults={
        'name': '笔记本电脑', 'category': '电子设备', 'spec': 'ThinkPad E14 / i5-1340P / 16G',
        'price': 5600.0, 'buy_date': '2025-03-10', 'life_years': 3, 'salvage_rate': 5,
        'dept': '营销部', 'keeper': '张伟', 'place': '公司办公室', 'status': 'in_use',
        'note': '资产编号贴纸已贴机身底部', 'created_by': uid})
    get_or_create(AdmAsset, code='ZC-DEMO-002', defaults={
        'name': '办公桌椅套装', 'category': '办公家具', 'spec': '1.4m 屏风工位 + 人体工学椅',
        'price': 1800.0, 'buy_date': '2025-01-05', 'life_years': 5, 'salvage_rate': 5,
        'dept': '财务部', 'keeper': '财务专员', 'place': '公司办公室', 'status': 'in_use',
        'created_by': uid})
    db.session.flush()


def _seed_adm_service_demo():
    """行政事务（二期）演示数据：办公用品 + 出入库流水 + 印章使用 + 快递收发。

    可重复执行：用品/印章/快递按编号查重；流水按「该用品是否已有记录」查重。
    """
    admin = User.query.filter_by(username='admin').first()
    uid = admin.id if admin else None

    # ① 办公用品：A4 纸（库存充足）/ 签字笔（**低于安全库存**，演示预警）/ 矿泉水
    s1 = get_or_create(AdmSupply, code='BYP-DEMO-001', defaults={
        'name': 'A4 复印纸', 'category': '打印耗材', 'spec': '70g / 500 张 / 包', 'unit': '包',
        'stock': 12, 'min_stock': 6, 'place': '行政仓库', 'note': '常用耗材，按月补货',
        'created_by': uid})
    s2 = get_or_create(AdmSupply, code='BYP-DEMO-002', defaults={
        'name': '中性签字笔', 'category': '文具耗材', 'spec': '0.5mm 黑色', 'unit': '支',
        'stock': 4, 'min_stock': 10, 'place': '前台柜', 'note': '消耗快，注意及时补货',
        'created_by': uid})
    get_or_create(AdmSupply, code='BYP-DEMO-003', defaults={
        'name': '瓶装矿泉水', 'category': '饮用水', 'spec': '350ml × 24 瓶', 'unit': '箱',
        'stock': 8, 'min_stock': 3, 'place': '茶水间', 'created_by': uid})
    db.session.flush()

    # 出入库流水（仅首次灌入，避免重复执行时越积越多）
    if not AdmSupplyFlow.query.filter_by(supply_id=s1.id).first():
        db.session.add(AdmSupplyFlow(supply_id=s1.id, direction='in', qty=12,
                                     flow_date='2026-09-05', purpose='9 月采购入库',
                                     operator='行政专员', created_by=uid))
    if not AdmSupplyFlow.query.filter_by(supply_id=s2.id).first():
        db.session.add(AdmSupplyFlow(supply_id=s2.id, direction='out', qty=6, who='张伟',
                                     dept='营销部', flow_date='2026-09-26',
                                     purpose='新员工入职办公用品配置', operator='行政专员',
                                     created_by=uid))

    # ② 印章使用登记：现场加盖（已完成）+ 借出使用（已归还）
    get_or_create(AdmSealUse, code='YZ-DEMO-001', defaults={
        'seal_name': '公司公章', 'use_date': '2026-09-20', 'applicant': '张伟', 'dept': '营销部',
        'use_type': 'seal', 'purpose': '签订保险代理合作协议',
        'doc_name': '《保险代理合作协议》一式两份', 'copies': 2,
        'approval_code': 'SP-202609-001', 'keeper': '行政专员', 'status': 'done',
        'created_by': uid})
    get_or_create(AdmSealUse, code='YZ-DEMO-002', defaults={
        'seal_name': '合同专用章', 'use_date': '2026-09-28', 'applicant': '财务专员', 'dept': '财务部',
        'use_type': 'borrow', 'purpose': '外出办理银行开户资料用章',
        'doc_name': '银行开户资料（借出盖章）', 'copies': 3, 'keeper': '财务专员',
        'status': 'returned', 'note': '已于当日归还', 'created_by': uid})

    # ③ 快递收发：一条已签收收件 + 一条待寄出寄件
    get_or_create(AdmExpress, code='KD-DEMO-001', defaults={
        'direction': 'in', 'company': '顺丰速运', 'waybill': 'SF1234567890',
        'counterpart': '国家金融监督管理总局广东监管局', 'handler': '行政专员',
        'dept': '人事行政部', 'item': '监管备案回执（纸质）',
        'express_date': '2026-09-25', 'status': 'done', 'created_by': uid})
    get_or_create(AdmExpress, code='KD-DEMO-002', defaults={
        'direction': 'out', 'company': '京东物流', 'waybill': 'JD9876543210',
        'counterpart': '海腾保险代理有限公司', 'handler': '行政专员', 'dept': '营销部',
        'item': '佣金结算明细（盖章原件）', 'fee': 23.0,
        'express_date': '2026-09-29', 'status': 'pending', 'created_by': uid})
    db.session.flush()


def _seed_adm_expense_demo():
    """费用台账（三期）演示数据：已缴 / 待缴 / 临期 / 逾期 各一条。

    可重复执行：按编号查重。
    """
    admin = User.query.filter_by(username='admin').first()
    uid = admin.id if admin else None
    ctr = AdmContract.query.filter_by(code='SWHT-DEMO-001').first()

    # 房租（已缴，关联租赁合同 + 已回填 OA 报销单号）
    get_or_create(AdmExpense, code='FY-DEMO-001', defaults={
        'category': '房租', 'period': '2026-10', 'amount': 8000.0,
        'payee': '示范物业管理有限公司', 'due_date': '2026-10-05', 'pay_date': '2026-10-03',
        'pay_mode': 'bank', 'invoice_no': 'FP2026100001',
        'contract_id': ctr.id if ctr else None,
        'handler': '行政专员', 'dept': '人事行政部',
        'approval_code': 'BX-202610-001', 'status': 'paid',
        'note': '10 月办公室租金，已在 OA 提交报销', 'created_by': uid})
    # 物业费（待缴，4 天内到期 → 临期提醒）
    get_or_create(AdmExpense, code='FY-DEMO-002', defaults={
        'category': '物业费', 'period': '2026-10', 'amount': 1200.0,
        'payee': '示范物业管理有限公司', 'due_date': '2026-10-05',
        'pay_mode': 'bank', 'handler': '行政专员', 'dept': '人事行政部',
        'status': 'unpaid', 'note': '写字楼 10 月物业管理费', 'created_by': uid})
    # 电费（待缴且已逾期，演示逾期红字提醒）
    get_or_create(AdmExpense, code='FY-DEMO-003', defaults={
        'category': '电费', 'period': '2026-09', 'amount': 1860.5,
        'payee': '南方电网广州供电局', 'due_date': '2026-09-25',
        'pay_mode': 'bank', 'invoice_no': 'FP2026090128',
        'handler': '行政专员', 'dept': '人事行政部',
        'status': 'unpaid', 'note': '9 月办公用电，已逾期请尽快缴纳', 'created_by': uid})
    # 宽带通讯费（已缴，无附件）
    get_or_create(AdmExpense, code='FY-DEMO-004', defaults={
        'category': '宽带通讯费', 'period': '2026-09', 'amount': 599.0,
        'payee': '中国电信广州分公司', 'due_date': '2026-09-10', 'pay_date': '2026-09-08',
        'pay_mode': 'bank', 'handler': '行政专员', 'dept': '人事行政部',
        'status': 'paid', 'created_by': uid})
    db.session.flush()


def _seed_adm_vehicle_demo():
    """车辆管理（四期）演示数据：2 台车 + 保养 / 维修 / 违章 / 用车 四类动态。

    到期日刻意做出层次：① 粤A·88888 保险 20 天后到期（黄）/ 年检 5 天后到期（红）；
    ② 粤A·66666 保险与年检均在 1 年后（正常），但下次保养 19 天后（黄）。
    可重复执行：车辆按编号查重，动态按「车辆 + 类型 + 日期」查重。
    """
    admin = User.query.filter_by(username='admin').first()
    uid = admin.id if admin else None

    v1 = get_or_create(AdmVehicle, code='CAR-DEMO-001', defaults={
        'plate_no': '粤A·88888', 'name': '公司公务用车', 'kind': '轿车',
        'brand': '大众帕萨特 330TSI', 'vin': 'LSVCD6A4XDN000001', 'engine_no': 'CEA123456',
        'color': '黑色', 'seats': 5, 'owner': '广东海盈通保智能科技有限公司',
        'buy_date': '2024-05-20', 'price': 218000.0, 'mileage': 38500,
        'keeper': '张伟', 'dept': '人事行政部', 'place': '公司地库 B12',
        'status': 'in_use', 'insurer': '中国人民财产保险', 'insurance_no': 'PICC2026A0001',
        'insurance_expire': '2026-10-21', 'inspect_expire': '2026-10-06',
        'note': '公务接待与领导用车', 'created_by': uid})
    v2 = get_or_create(AdmVehicle, code='CAR-DEMO-002', defaults={
        'plate_no': '粤A·66666', 'name': '业务用车', 'kind': '商务车 / MPV',
        'brand': '别克 GL8 陆尊', 'vin': 'LSGKB54U0DH000002', 'engine_no': 'LE9B234567',
        'color': '银色', 'seats': 7, 'owner': '广东海盈通保智能科技有限公司',
        'buy_date': '2023-09-01', 'price': 289000.0, 'mileage': 62000,
        'keeper': '行政专员', 'dept': '营销部', 'place': '公司地面停车场',
        'status': 'in_use', 'insurer': '中国平安财产保险', 'insurance_no': 'PAIC2026B0002',
        'insurance_expire': '2027-03-15', 'inspect_expire': '2027-05-20',
        'note': '业务外出与客户接送', 'created_by': uid})
    db.session.flush()

    # ① 保养（含下次保养提醒日）
    get_or_create(AdmVehRecord, vehicle_id=v1.id, kind='maintain', rec_date='2026-08-12', defaults={
        'mileage': 36500, 'cost': 680.0, 'place': '广州大众 4S 店', 'detail': '更换机油机滤、空调滤芯',
        'next_date': '2027-02-12', 'note': '质保期内，建议回 4S 店', 'created_by': uid})
    get_or_create(AdmVehRecord, vehicle_id=v2.id, kind='maintain', rec_date='2026-04-15', defaults={
        'mileage': 58000, 'cost': 720.0, 'place': '广州别克 4S 店', 'detail': '常规保养（机油 + 四轮换位）',
        'next_date': '2026-10-20', 'note': '已到保养周期，注意预约', 'created_by': uid})
    # ② 维修
    get_or_create(AdmVehRecord, vehicle_id=v2.id, kind='repair', rec_date='2026-07-05', defaults={
        'mileage': 60500, 'cost': 1200.0, 'place': '广州粤通汽修厂', 'detail': '更换前刹车片 + 刹车油',
        'note': '已留存维修发票', 'created_by': uid})
    # ③ 违章（未处理，演示红字）
    get_or_create(AdmVehRecord, vehicle_id=v1.id, kind='fine', rec_date='2026-09-18', defaults={
        'cost': 200.0, 'place': '广州市广州大道中', 'detail': '违反禁止标线指示',
        'fine_point': 2, 'fine_status': 'pending', 'note': '待驾驶人到交警大队处理', 'created_by': uid})
    # ④ 用车
    get_or_create(AdmVehRecord, vehicle_id=v2.id, kind='use', rec_date='2026-09-28', defaults={
        'mileage': 62000, 'driver': '张伟', 'place': '广州 → 佛山',
        'detail': '拜访客户送保单', 'note': '当日往返', 'created_by': uid})
    db.session.flush()


def _demo_pdf(diskpath, title, rows):
    """生成一份演示用 PDF 占位件（reportlab 属既有依赖，确认函模块已在用）。"""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        try:
            pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
            font = 'STSong-Light'
        except Exception:
            font = 'Helvetica'
        c = canvas.Canvas(diskpath, pagesize=A4)
        w, h = A4
        c.setFont(font, 16)
        c.drawString(60, h - 80, title)
        c.setFont(font, 11)
        y = h - 120
        for line in rows:
            c.drawString(60, y, line)
            y -= 22
        c.setFont(font, 9)
        c.drawString(60, 60, '本文件为系统演示占位文件，非真实保单材料。')
        c.save()
        return True
    except Exception:
        return False


# ---------- 十九改（2026-10-02）：业务团队 · 人力数据清单（演示）----------
# 费用政策是「发给谁」的，所以先有人力底表。团队类型两大类：
#   basic   基本法团队 —— 统一套一套标准化费用政策（不分层，全员同一口径）
#   channel 渠道团队 —— 可按层级（渠道总 → 团队长 → 营销员）分别下发费用政策
# 字段顺序：工号 / 姓名 / 省公司 / 分支 / 经代公司 / 职级 / 职级编码 / 销售能力等级 /
#           入司 / 离职 / 推荐人工号 / 推荐人职级 / 身份证 / 性别 / 出生年月 /
#           团队类型 / 所属团队 / 开户行 / 银行卡号 / 是否签署 / 签署日期 / 备注
MP_DEMO = [
    # —— 渠道团队：一条完整的推荐链 渠道总 → 团队长 → 营销员 ——
    ('A0001', '陈志强', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'channel_boss', 'CH01', '一级', '2023-01-05', '', '', '',
     '440101198201154517', 'male', '1982-01-15', 'channel', '广州一区',
     '中国银行广州天河支行', '6217001234560001', 'yes', '2023-01-06', '演示数据 · 渠道总'),
    ('A0002', '李美玲', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'team_leader', 'TL01', '一级', '2023-03-01', '', 'A0001', '渠道总',
     '440101198705203628', 'female', '1987-05-20', 'channel', '广州一区',
     '中国建设银行广州分行', '6217001234560002', 'yes', '2023-03-05', '演示数据 · 团队长'),
    ('A0003', '王小明', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'agent', 'AG01', '二级', '2023-06-12', '', 'A0002', '团队长',
     '440101199203084213', 'male', '1992-03-08', 'channel', '广州一区',
     '中国工商银行广州分行', '6217001234560003', 'yes', '2023-06-15', '演示数据 · 营销员'),
    ('A0004', '赵敏', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'agent', 'AG02', '二级', '2024-02-20', '', 'A0002', '团队长',
     '440101199506127428', 'female', '1995-06-12', 'channel', '广州一区',
     '招商银行广州分行', '6217001234560004', 'yes', '2024-02-25', '演示数据 · 营销员'),
    ('A0005', '刘建国', '广东省', '深圳分支', '广东海盈保险代理有限公司',
     'team_leader', 'TL02', '二级', '2023-08-01', '', 'A0001', '渠道总',
     '440101198811304411', 'male', '1988-11-30', 'channel', '深圳二区',
     '中国银行深圳福田支行', '6217001234560005', 'yes', '2023-08-05', '演示数据 · 团队长'),
    ('A0006', '周慧', '广东省', '深圳分支', '广东海盈保险代理有限公司',
     'agent', 'AG03', '三级', '2024-05-06', '', 'A0005', '团队长',
     '440101199309176827', 'female', '1993-09-17', 'channel', '深圳二区',
     '平安银行深圳分行', '6217001234560006', 'yes', '2024-05-10', '演示数据 · 营销员'),
    ('A0007', '林俊杰', '广东省', '东莞分支', '广州海腾保险代理有限公司',
     'agent', 'AG04', '三级', '2025-03-18', '', '', '',
     '440101199603114216', 'male', '1996-03-11', 'channel', '东莞一区',
     '中国工商银行东莞分行', '6217001234560007', 'no', '', '演示数据 · 未签渠道协议'),
    # —— 基本法团队：统一一套标准化政策，不分层 ——
    ('B0001', '黄伟豪', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'team_leader', 'TL03', '一级', '2022-09-01', '', '', '',
     '440101198506093312', 'male', '1985-06-09', 'basic', '广州二区',
     '中国银行广州越秀支行', '6217001234560011', 'yes', '2022-09-05', '演示数据 · 基本法团队'),
    ('B0002', '吴丽芳', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'agent', 'AG11', '二级', '2023-04-10', '', 'B0001', '团队长',
     '440101199102283627', 'female', '1991-02-28', 'basic', '广州二区',
     '中国建设银行广州分行', '6217001234560012', 'yes', '2023-04-15', '演示数据 · 基本法团队'),
    ('B0003', '郑凯', '广东省', '广州本部', '广东海盈保险代理有限公司',
     'agent', 'AG12', '三级', '2024-07-08', '', 'B0001', '团队长',
     '440101199407154518', 'male', '1994-07-15', 'basic', '广州二区',
     '中国农业银行广州分行', '6217001234560013', 'no', '', '演示数据 · 基本法团队'),
    ('B0004', '孙悦', '广东省', '佛山分支', '广州海腾保险代理有限公司',
     'agent', 'AG13', '二级', '2023-11-02', '2025-06-30', 'B0001', '团队长',
     '440101198909226824', 'female', '1989-09-22', 'basic', '佛山一区',
     '中国银行佛山分行', '6217001234560014', 'yes', '2023-11-06', '演示数据 · 已离职'),
    ('B0005', '何静', '广东省', '佛山分支', '广州海腾保险代理有限公司',
     'agent', 'AG14', '四级', '2025-01-15', '', 'B0004', '营销员',
     '440101199712055628', 'female', '1997-12-05', 'basic', '佛山一区',
     '中国邮政储蓄银行佛山分行', '6217001234560015', 'no', '', '演示数据 · 基本法团队'),
]


# 人力架构演示：省公司 / 分支 / 分区（短代号）/ 分区名称 / 营业部 / 营业组
# 编码按行序自动生成：FQ00n / FQ00n-B01 / FQ00n-B01-G01
ORG_DEMO = [
    ('广东省', '广州本部', '一区', '广州第一分区', '天河营业部', '广州一区'),
    ('广东省', '广州本部', '二区', '广州第二分区', '越秀营业部', '广州二区'),
    ('广东省', '深圳分支', '深圳分区', '深圳第一分区', '福田营业部', '深圳二区'),
    ('广东省', '东莞分支', '东莞分区', '东莞第一分区', '南城营业部', '东莞一区'),
    ('广东省', '佛山分支', '佛山分区', '佛山第一分区', '禅城营业部', '佛山一区'),
]


# 十九改++：合作经代公司演示数据（字段登记规则 **参照保司基础信息 / 保司省分公司**）
# (全称, 简称, 统一社会信用代码, 中介许可证号, 法定代表人, 注册资本, 成立日期, 地址,
#  电话, 机构类型, 合作状态, 对接人, 职务, 对接电话, 经营范围, 备注)
AGENCY_DEMO = [
    ('广东海盈保险代理有限公司', '广东海盈', '91440101MA5CXXXX1A', '0000000000000001',
     '陈海盈', '1000万元', '2016-05-18', '广州市天河区珠江新城华夏路 8 号 20 层',
     '020-88886666', 'agent', 'coop', '李经理', '业务总监', '13800000001',
     '代理销售保险产品；代理收取保险费；代理相关保险业务的损失勘查和理赔', '我司主要合作经代主体'),
    ('广州海盈保险代理有限公司', '广州海盈', '91440101MA5CXXXX1B', '0000000000000002',
     '陈海盈', '500万元', '2018-03-09', '广州市越秀区东风中路 300 号 15 层',
     '020-88886668', 'agent', 'coop', '王主管', '渠道经理', '13800000002',
     '代理销售保险产品；代理收取保险费', '广州本地主体'),
    ('广州海腾保险代理有限公司', '广州海腾', '91440101MA5CXXXX1C', '0000000000000003',
     '林海腾', '800万元', '2015-09-22', '广州市海珠区新港东路 1000 号 12 层',
     '020-88889999', 'agent', 'coop', '张经理', '运营总监', '13800000003',
     '代理销售保险产品；代理收取保险费；代理相关保险业务的损失勘查和理赔', '非持牌业务承接主体（佣金代发）'),
    ('广东海腾保险代理有限公司', '广东海腾', '91440101MA5CXXXX1D', '0000000000000004',
     '林海腾', '2000万元', '2013-11-05', '广东省广州市番禺区万博商务区万达广场 B4 座',
     '020-88889998', 'agent', 'coop', '刘经理', '省分负责人', '13800000004',
     '代理销售保险产品；代理收取保险费', '省级主体'),
]


def seed_agency():
    """十九改++：合作经代公司基础信息（按全称去重，可重复执行）。"""
    u = User.query.filter_by(username='admin').first()
    uid = u.id if u else None
    n = 0
    for t in AGENCY_DEMO:
        (name, short, credit, lic, legal, capital, est, addr, tel,
         atype, status, cname, ctitle, cphone, scope, note) = t
        if CrmAgency.query.filter_by(name=name).first():
            continue
        db.session.add(CrmAgency(
            name=name, short_name=short, credit_code=credit, license_no=lic,
            legal_person=legal, reg_capital=capital, establish_date=est,
            address=addr, phone=tel, business_scope=scope, agency_type=atype,
            status=status, contact_person=cname, contact_title=ctitle,
            contact_phone=cphone, note=note, created_by=uid))
        n += 1
    db.session.commit()
    return n


def seed_org_struct():
    """十九改：人力架构基础信息（演示）+ 把演示人员回填挂到对应营业组。

    幂等：按「分支 + 营业组」去重；人员只回填 org_id 为空的。
    """
    u = User.query.filter_by(username='admin').first()
    uid = u.id if u else None
    n = 0
    for i, (prov, br, zone, zname, dept, gname) in enumerate(ORG_DEMO, 1):
        if CrmOrgStruct.query.filter_by(branch=br, group_name=gname).first():
            continue
        db.session.add(CrmOrgStruct(
            province=prov, branch=br, zone=zone, zone_name=zname,
            zone_code='FQ%03d' % i, dept=dept, dept_code='FQ%03d-B01' % i,
            group_name=gname, group_code='FQ%03d-B01-G01' % i,
            note='演示数据', created_by=uid))
        n += 1
    db.session.commit()
    back = 0
    for m in CrmManpower.query.filter(CrmManpower.org_id.is_(None)).all():
        hit = CrmOrgStruct.query.filter_by(branch=m.branch or '',
                                           group_name=m.team_name or '').first()
        if hit:
            m.org_id, m.org_path = hit.id, hit.path
            back += 1
    db.session.commit()
    return n, back


# 演示数据里开户信息是「中国银行广州天河支行」一整串，拆成**开户行 + 开户支行**两栏
# （长名在前，避免「中国建设银行」被「中国银行」抢先匹配）
_BANK_HEADS = ['中国工商银行', '中国建设银行', '中国农业银行', '中国邮政储蓄银行',
               '中国银行', '交通银行', '招商银行', '中信银行', '中国光大银行',
               '华夏银行', '中国民生银行', '广发银行', '兴业银行', '平安银行',
               '上海浦东发展银行', '恒丰银行', '浙商银行', '渤海银行',
               '广州银行', '东莞银行']


def _split_bank(s):
    """'中国银行广州天河支行' → ('中国银行', '广州天河支行')；认不出就整串当开户行。"""
    s = (s or '').strip()
    for h in _BANK_HEADS:
        if s.startswith(h):
            return h, s[len(h):].lstrip('（(').rstrip()
    return s, ''


def seed_manpower():
    """十九改：业务团队人力数据清单演示数据（按代理人工号去重，可重复执行）。

    系统工号 sys_no 由这里按顺序补 **SA-001 ~ SA-0nn**（真实录入时由系统自动发号）。
    十九改+：开户信息拆「开户行 / 开户支行」，推荐人姓名按推荐人工号反查补上。
    """
    u = User.query.filter_by(username='admin').first()
    uid = u.id if u else None
    n = 0
    for i, t in enumerate(MP_DEMO, 1):
        (no, name, prov, br, agency, rank, rcode, level, hire, leave, ref_no, ref_rank,
         idcard, gender, birth, ttype, team, bank, card, signed, sdate, note) = t
        head, sub = _split_bank(bank)
        r = CrmManpower.query.filter_by(agent_no=no).first()
        if r:
            if not r.sys_no:                       # 老数据回填系统工号
                r.sys_no = 'SA-%03d' % i
            if not r.bank_branch and r.bank_name:  # 老数据拆开户行 / 开户支行
                r.bank_name, r.bank_branch = _split_bank(r.bank_name)
            continue
        db.session.add(CrmManpower(
            sys_no='SA-%03d' % i,
            province=prov, branch=br, agent_no=no, agency=agency, name=name,
            rank_code=rcode, rank=rank, sale_level=level, hire_date=hire, leave_date=leave,
            ref_no=ref_no, ref_rank=ref_rank, idcard=idcard, gender=gender, birth_date=birth,
            team_type=ttype, team_name=team, bank_name=head, bank_branch=sub, bank_card=card,
            signed=signed, sign_date=sdate, note=note, created_by=uid))
        n += 1
    db.session.commit()
    # 二遍：**推荐人姓名**按推荐人工号反查（建人时推荐人可能还没建出来）
    for t in MP_DEMO:
        no, ref_no = t[0], t[10]
        if not ref_no:
            continue
        r = CrmManpower.query.filter_by(agent_no=no).first()
        ref = CrmManpower.query.filter_by(agent_no=ref_no).first()
        if r and ref and not r.ref_name:
            r.ref_name = ref.name
    db.session.commit()
    return n


def _seed_crm_demo():
    """核心业务（CRM 一期）演示数据：4 家保司 + 5 条产品合约 + 4 份产品资料。

    保司仅预置公知信息（全称 / 简称 / 类型 / 官网），来源标 builtin；
    工商注册号、许可证号、税号、法人等动态数据留空，待核实补录或接入数据源。
    可重复执行：保司按 name、合约按 code、资料按（保司 + 标题）查重。
    """

    def _seed_plan(c, tier, j, p):
        """按 tier_sets 里的一条方案政策 dict 落库到 CrmContractPlan（口径同 crm._calc_plan_total）。"""
        plan = CrmContractPlan(
            contract_id=c.id, tier_id=tier.id, sort_no=j,
            plan_name=p.get('name', ''),
            fee_type=p.get('fee', 'stage'),
            tt_start=p.get('tt_start', ''), tt_end=p.get('tt_end', ''),
            ff_start=p.get('ff_start', ''), ff_end=p.get('ff_end', ''),
            policy_scope=p.get('scope', 'in'),
            pay_form=p.get('form', 'transfer'),
            tt_coef=p.get('ttc', 0), tt_amount=p.get('tta', 0), tt_other=p.get('tto', ''),
            ff_coef=p.get('ffc', 0), ff_amount=p.get('ffa', 0), ff_other=p.get('ffo', ''))
        plan.plan_total = round((plan.tt_coef or 0) + (plan.ff_coef or 0), 2)
        if (plan.pay_form or '') == 'cash':
            plan.policy_scope = 'out'
        db.session.add(plan)

    admin = User.query.filter_by(username='admin').first()
    uid = admin.id if admin else None

    # ① 保司档案（4 家）
    specs = [
        ('中国人寿保险股份有限公司', '中国人寿', 'life', 'www.e-chinalife.com'),
        ('中国平安人寿保险股份有限公司', '平安人寿', 'life', 'www.pingan.com'),
        ('中国太平洋人寿保险股份有限公司', '太平洋寿险', 'life', 'www.cpic.com.cn'),
        ('中国人民财产保险股份有限公司', '人保财险', 'property', 'www.picc.com'),
    ]
    insurers = {}
    for name, short, t, site in specs:
        ins = get_or_create(Insurer, name=name, defaults={
            'short_name': short, 'insurer_type': t, 'website': site,
            'status': 'coop', 'source': 'builtin',
            'contact_person': '李娜', 'contact_phone': '020-8888 6001',
            'data_updated_at': datetime.now().strftime('%Y-%m-%d'),
            'note': '演示数据：工商注册号 / 许可证号 / 法人等待核实补录',
            'created_by': uid})
        insurers[short] = ins
    db.session.flush()

    # ①.5 继续率考核规则（八改 2026-10-02：奖励项目 ↔ 考核标准 ↔ 档位 三者绑定）
    #   第一步：规则侧登记「保司 + 分公司(可多选) + 继续率考核标准(多选) + 奖励项目 + 档位 + 倒扣」
    #   第二步：到「产品匹配」页把产品关联到规则（demo 直接预置好，一打开就能看到命中效果）
    #   档位归属：继续率奖金系数 → **按考核标准分块**；长期服务奖系数 → **单独一块**（与月份无关）
    _awards = 'bonus,long_service'
    _CY13 = '13个月继续率考核标准'
    _CY25 = '25个月继续率考核标准'
    _LS = 'long_service'

    def _mk_rule(name, ins_key, cycle, metric, claw, clawstd, note, prods,
                 claw_line=0, claw_base='all'):
        """建演示规则。十五改：继续率口径 / 精度已删除，系统统一精度（见 crm.round_persist）。"""
        r = get_or_create(CrmPersistRule, name=name, defaults={
            'metric': metric, 'cycle': cycle,
            'cycles': json.dumps([cycle], ensure_ascii=False),
            'has_clawback': claw,
            'clawback_std': clawstd, 'status': 'on', 'note': note, 'created_by': uid,
            'insurer_id': insurers[ins_key].id, 'insurer_name': insurers[ins_key].name,
            'award_items': _awards,
            'clawback_line': claw_line, 'clawback_base': claw_base,
            'product_names': json.dumps(prods, ensure_ascii=False)})
        return r

    def _mk_items(rule, cy_name, bonus_rows, ls_rows):
        """给规则铺档位：bonus_rows 挂在规则的考核标准下，ls_rows 挂在长期服务奖块。

        已是八改结构（所有档位都有归属 cycle）时不重复铺，保证幂等。
        """
        old = CrmPersistRuleItem.query.filter_by(rule_id=rule.id).all()
        if old and all((x.cycle or '').strip() for x in old):
            return
        for x in old:
            db.session.delete(x)
        n = 0
        for lo, hi, bc, nt in bonus_rows:
            db.session.add(CrmPersistRuleItem(rule_id=rule.id, cycle=cy_name,
                                              rate_min=lo, rate_max=hi, bonus_coef=bc,
                                              long_service_coef=0.0, sort_no=n, note=nt))
            n += 1
        for lo, hi, lsc, nt in ls_rows:
            db.session.add(CrmPersistRuleItem(rule_id=rule.id, cycle=_LS,
                                              rate_min=lo, rate_max=hi, bonus_coef=0.0,
                                              long_service_coef=lsc, sort_no=n, note=nt))
            n += 1

    rule_std = _mk_rule('标准继续率考核（13 个月）', '中国人寿', _CY13,
                        '13 个月继续率', True,
                        '13 个月继续率低于 85% 时，按首期保费 5% 倒扣首年佣金',
                        '多数产品共用',
                        ['国寿鑫享金生年金保险', '国寿百万如意行意外伤害保险'],
                        claw_line=85, claw_base='fyc')
    _mk_items(rule_std, _CY13,
              [(95, 0, 1.20, '优秀档'), (90, 95, 1.00, '达标档'),
               (85, 90, 0.80, '基本达标'), (0, 85, 0.00, '不达标（无奖金）')],
              [(95, 0, 0.20, '优秀档'), (90, 95, 0.15, '达标档'),
               (85, 90, 0.10, '基本达标'), (0, 85, 0.00, '不达标')])

    rule_loose = _mk_rule('宽松继续率考核（25 个月）', '太平洋寿险', _CY25,
                          '25 个月继续率', False, '',
                          '总-分合作产品适用',
                          ['太保长相伴（盛世版）增额终身寿险'])
    _mk_items(rule_loose, _CY25,
              [(90, 0, 1.10, '优秀档'), (85, 90, 0.95, '达标档'), (0, 85, 0.70, '未达标')],
              [(90, 0, 0.15, '优秀档'), (85, 90, 0.10, '达标档'), (0, 85, 0.00, '未达标')])

    # 七改：演示「一个保司一套规则」——平安人寿 / 人保财险 各建一条 13 个月口径规则
    rule_pa = _mk_rule('平安人寿-13个月继续率考核', '平安人寿', _CY13,
                       '13 个月继续率', True,
                       '当年 13 个月继续率不达标时，按差额比例倒扣方案奖金', '',
                       ['平安御享金瑞增额终身寿险'],
                       claw_line=88, claw_base='all')
    _mk_items(rule_pa, _CY13,
              [(92, 0, 1.15, '优秀档'), (88, 92, 0.95, '达标档'), (0, 88, 0.60, '不达标')],
              [(92, 0, 0.18, '优秀档'), (88, 92, 0.12, '达标档'), (0, 88, 0.00, '不达标')])

    rule_picc = _mk_rule('人保财险-13个月继续率考核', '人保财险', _CY13,
                         '13 个月继续率', False, '', '财险健康险条线',
                         ['人保健康一生重大疾病保险'])
    _mk_items(rule_picc, _CY13,
              [(90, 0, 1.00, '优秀档'), (85, 90, 0.85, '达标档'), (0, 85, 0.50, '不达标')],
              [(90, 0, 0.15, '优秀档'), (85, 90, 0.10, '达标档'), (0, 85, 0.00, '不达标')])

    # 八改：老库幂等升级 —— 归一考核标准文案、补 cycles / 奖励项目 / 保司 / 适用产品 / 档位归属
    _cyc_map = {
        '13个月': _CY13, '13个月继续率考核': _CY13,
        '25个月': _CY25, '25个月继续率考核': _CY25,
        '37个月继续率考核': '37个月继续率考核标准',
        '39个月继续率考核': '37个月继续率考核标准',   # 七改口径 39 → 八改回归行业标准 37
        '49个月继续率考核': '49个月继续率考核标准',
    }
    _demo_bind = {
        '标准继续率考核（13 个月）': ('中国人寿', ['国寿鑫享金生年金保险', '国寿百万如意行意外伤害保险']),
        '宽松继续率考核（25 个月）': ('太平洋寿险', ['太保长相伴（盛世版）增额终身寿险']),
        '平安人寿-13个月继续率考核': ('平安人寿', ['平安御享金瑞增额终身寿险']),
        '人保财险-13个月继续率考核': ('人保财险', ['人保健康一生重大疾病保险']),
    }
    for _r in CrmPersistRule.query.all():
        _cv = (_r.cycle or '').replace(' ', '')
        if _cv in _cyc_map:
            _r.cycle = _cyc_map[_cv]
        if not (_r.cycles or '').strip():
            _r.cycles = json.dumps([_r.cycle], ensure_ascii=False) if _r.cycle else '[]'
        if not _r.award_items:
            _r.award_items = _awards
        # 八改+：老库补倒扣基数的默认值（ALTER 加列不会写 Python 默认值）
        if not (_r.clawback_base or '').strip():
            _r.clawback_base = 'all'
        if _r.clawback_line is None:
            _r.clawback_line = 0.0
        _bind = _demo_bind.get(_r.name)
        if _bind:
            _ins = insurers.get(_bind[0])
            if _ins and not _r.insurer_id:
                _r.insurer_id = _ins.id
                _r.insurer_name = _ins.name
            if not _r.product_names:
                _r.product_names = json.dumps(_bind[1], ensure_ascii=False)
    # 八改+：演示规则补「倒扣」示范值（口径/精度已于十五改删除，系统统一精度）
    #   仅当仍是通用默认值时才升级，避免覆盖用户在界面里改过的选择。
    _demo_meta = {
        # 规则名: (起扣线, 扣费基数)
        '标准继续率考核（13 个月）': (85.0, 'fyc'),
        '平安人寿-13个月继续率考核': (88.0, 'all'),
        '人保财险-13个月继续率考核': (0.0, 'all'),
    }
    for _r in CrmPersistRule.query.all():
        _m = _demo_meta.get(_r.name)
        if not _m:
            continue
        if not (_r.clawback_line or 0) and _m[0]:
            _r.clawback_line = _m[0]
        if (_r.clawback_base or '') in ('', 'all') and _m[1] != 'all':
            _r.clawback_base = _m[1]
    # 档位归属回填：先把旧考核标准文案归一，再补空
    #（纯长期服务奖档位归 ls 块；其余归到所属规则的第一个考核标准）
    for _it in CrmPersistRuleItem.query.all():
        _ic = (_it.cycle or '').strip()
        if _ic in _cyc_map:
            _it.cycle = _cyc_map[_ic]
            continue
        if _ic:
            continue
        if _it.long_service_coef and not _it.bonus_coef:
            _it.cycle = _LS
            continue
        _rp = CrmPersistRule.query.get(_it.rule_id)
        _rl = []
        if _rp:
            try:
                _rl = [x for x in json.loads(_rp.cycles or '[]') if x]
            except Exception:
                _rl = []
            if not _rl and _rp.cycle:
                _rl = [_rp.cycle]
        _it.cycle = _rl[0] if _rl else ''
    # 九改：老档位补「下限 / 上限 运算符」默认值（行业惯用左闭右开 [下限, 上限)）
    for _it in CrmPersistRuleItem.query.all():
        if not (_it.min_op or '').strip():
            _it.min_op = '>='
        if not (_it.max_op or '').strip():
            _it.max_op = '<'
    # 八改+：老方案政策补「费项类型」默认值（stage 阶段补充佣金·推动费）
    for _pl in CrmContractPlan.query.all():
        if not (_pl.fee_type or '').strip():
            _pl.fee_type = 'stage'
    db.session.flush()

    # ①.6 多缴别演示：001 三个缴别（含 1 条「协议外 · 现金」）、003 两个缴别
    # 缴别基础合约 + 卡内方案政策（演示：多缴别 / 协议外现金加佣 / 一缴别挂多条方案）
    #   tier: period/income/ttf(总总FYC)/fff(分分FYC)/ryc
    #   tier.plans: name/fee(费项类型)/tt_start/tt_end/ff_start/ff_end/scope(协议内外)/form(费用形式)/
    #               ttc(总总系数)/tta(总总金额)/tto / ffc/ffa/ffo
    tier_sets = {
        'PTC-DEMO-001': [
            dict(period='3年交', income=3000000.0, ttf=30.00, fff=11.00,
                 ryc=[4.50, 2.50, 1.50, 1.00, 0.80, 0.50, 0.50, 0.50, 0.50],
                 plans=[dict(name='2026开门红总对总', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-01-01', ff_end='2026-06-30', scope='in', form='transfer',
                              ttc=1.10, tta=180000.0, tto='3年交基础方案', ffc=0.75, ffa=60000.0, ffo='分公司配套',
                              fee='monthly')]),
            dict(period='5年交', income=5000000.0, ttf=32.50, fff=12.00,
                 ryc=[5.00, 3.00, 2.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.50],
                 plans=[dict(name='5年交主推方案', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-01-01', ff_end='2026-06-30', scope='in', form='transfer',
                              ttc=1.20, tta=260000.0, tto='5年交主推', ffc=0.80, ffa=90000.0, ffo='分公司配套',
                              fee='service')]),
            dict(period='10年交', income=8000000.0, ttf=35.00, fff=13.00,
                 ryc=[5.50, 3.50, 2.50, 1.50, 1.00, 0.50, 0.50, 0.50, 0.50],
                 plans=[dict(name='10年交激励（协议外现金加佣）', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-07-01', ff_end='2026-12-31', scope='out', form='cash',
                              ttc=1.30, tta=300000.0, tto='含协议外现金加佣', ffc=0.85, ffa=120000.0, ffo='协议外现金加佣',
                              fee='stage')]),
        ],
        'PTC-DEMO-002': [
            dict(period='5年交', income=1200000.0, ttf=0.0, fff=28.00,
                 ryc=[4.00, 2.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.00, 0.00],
                 plans=[dict(name='分公司开门红加佣', tt_start='2026-03-01', tt_end='2026-05-31',
                              ff_start='2026-03-01', ff_end='2026-05-31', scope='in', form='transfer',
                              ttc=0.0, tta=0.0, tto='', ffc=1.00, ffa=150000.0, ffo='分公司开门红加佣',
                              fee='stage')]),
        ],
        'PTC-DEMO-003': [
            dict(period='5年交', income=1800000.0, ttf=23.00, fff=7.00,
                 ryc=[4.00, 2.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.50, 0.00],
                 plans=[dict(name='总对总基础方案', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-01-01', ff_end='2026-12-31', scope='in', form='transfer',
                              ttc=0.90, tta=120000.0, tto='总对总基础', ffc=0.45, ffa=40000.0, ffo='分公司达成奖励',
                              fee='service')]),
            dict(period='10年交', income=3600000.0, ttf=25.00, fff=8.00,
                 ryc=[4.50, 2.50, 1.50, 1.00, 0.50, 0.50, 0.50, 0.50, 0.00],
                 plans=[dict(name='总对总基础方案', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-01-01', ff_end='2026-12-31', scope='in', form='transfer',
                              ttc=1.00, tta=180000.0, tto='总对总基础', ffc=0.50, ffa=60000.0, ffo='分公司达成奖励',
                              fee='service')]),
        ],
        'PTC-DEMO-004': [
            dict(period='20年交', income=960000.0, ttf=45.00, fff=0.0,
                 ryc=[3.00, 2.00, 1.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.50],
                 plans=[dict(name='健康险专项激励', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='', ff_end='', scope='in', form='transfer',
                              ttc=1.50, tta=120000.0, tto='健康险专项', ffc=0.0, ffa=0.0, ffo='',
                              fee='quality')]),
        ],
        'PTC-DEMO-005': [
            dict(period='趸交', income=560000.0, ttf=0.0, fff=15.00,
                 ryc=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                 plans=[dict(name='月缴业务专项奖励', tt_start='2026-01-01', tt_end='2026-12-31',
                              ff_start='2026-01-01', ff_end='2026-12-31', scope='in', form='transfer',
                              ttc=0.0, tta=0.0, tto='', ffc=0.60, ffa=45000.0, ffo='月缴专项',
                              fee='monthly')]),
        ],
    }

    # ② 产品合约（5 条：覆盖 总-总 / 总-分 / 分-分 三种签约级别）
    #    费用政策（FYC/RYC/方案）已拆到 tier_sets 子表，这里只登记产品级信息与继续率规则
    cs = [
        dict(code='PTC-DEMO-001', ins='中国人寿', level='tt',
             pname='国寿鑫享金生年金保险', pcode='GS-XXJS-2026', itype='年金险',
             pay='year', ryc=[5.00, 3.00, 2.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.50],
             metric='13 个月继续率 ≥ 90%', pc=0.30, lsc=0.10, claw=True,
             clawstd='13 个月继续率低于 85% 时，按首期保费 5% 倒扣首年佣金',
             note='总总框架协议配套分公司方案'),
        dict(code='PTC-DEMO-002', ins='平安人寿', level='ff',
             pname='平安御享金瑞增额终身寿险', pcode='PA-YHJR-2026', itype='终身寿险',
             pay='year', ryc=[4.00, 2.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.00, 0.00],
             metric='13 个月继续率 ≥ 88%', pc=0.25, lsc=0.10, claw=True,
             clawstd='当年 13 个月继续率不达标时，按差额比例倒扣方案奖金',
             note='仅分公司层级协议'),
        dict(code='PTC-DEMO-003', ins='太平洋寿险', level='tf',
             pname='太保长相伴（盛世版）增额终身寿险', pcode='CPIC-CXB-2026', itype='终身寿险',
             pay='year', ryc=[4.50, 2.50, 1.50, 1.00, 0.50, 0.50, 0.50, 0.50, 0.00],
             metric='25 个月继续率 ≥ 85%', pc=0.30, lsc=0.15, claw=False,
             clawstd='', note='总-分合作，方案分两层叠加'),
        dict(code='PTC-DEMO-004', ins='人保财险', level='tt',
             pname='人保健康一生重大疾病保险', pcode='PICC-JKYS-2026', itype='重疾险',
             pay='year', ryc=[3.00, 2.00, 1.00, 1.00, 1.00, 0.50, 0.50, 0.50, 0.50],
             metric='13 个月继续率 ≥ 90%', pc=0.20, lsc=0.10, claw=False,
             clawstd='', note='财险公司健康险条线'),
        dict(code='PTC-DEMO-005', ins='中国人寿', level='ff',
             pname='国寿百万如意行意外伤害保险', pcode='GS-BWYRX-2026', itype='意外险',
             pay='month', ryc=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
             metric='—', pc=0.0, lsc=0.0, claw=False, clawstd='',
             note='月缴趸交型意外险，无续期佣金'),
    ]
    for s in cs:
        ins = insurers[s['ins']]
        # 继续率规则：25 个月口径的产品用宽松规则，其余共用标准规则
        rule = rule_loose if '25 个月' in (s['metric'] or '') else rule_std
        c = get_or_create(CrmProductContract, code=s['code'], defaults={
            'insurer_id': ins.id, 'insurer_name': ins.name, 'insurer_short': ins.short_name,
            'sign_level': s['level'], 'product_name': s['pname'], 'product_code': s['pcode'],
            'insurance_type': s['itype'], 'status': 'active',
            'pay_type': s['pay'],
            'persist_rule_id': rule.id, 'persist_rule_name': rule.name,
            'note': s['note'], 'created_by': uid})
        db.session.flush()
        # 兼容：一期已存在的演示合约不会被 get_or_create 的 defaults 覆盖，
        # 这里幂等补写 二期新增的「继续率规则引用」列（为空才写，不覆盖用户改动）。
        if not c.persist_rule_id:
            c.persist_rule_id = rule.id
            c.persist_rule_name = rule.name
        db.session.flush()
        # 缴别基础合约 + 方案政策（每条合约至少 1 缴别 + 1 方案；001 演示多缴别/协议外现金）
        tiers = CrmContractTier.query.filter_by(contract_id=c.id) \
            .order_by(CrmContractTier.sort_no).all()
        if not tiers:
            # 全新合约：建缴别基础合约 + 方案政策
            tset = tier_sets.get(s['code']) or []
            for i, t in enumerate(tset):
                tier = CrmContractTier(
                    contract_id=c.id, sort_no=i, pay_period=t['period'],
                    tt_start=t.get('tts', '2026-01-01'), tt_end=t.get('tte', '2026-12-31'),
                    ff_start=t.get('ffs', '2026-01-01'), ff_end=t.get('ffe', '2026-12-31'),
                    tt_fyc=t['ttf'], ff_fyc=t['fff'])
                for k, n in enumerate(range(2, 11)):
                    setattr(tier, 'ryc_%d' % n, t['ryc'][k])
                tier.fyc_total = round((tier.tt_fyc or 0) + (tier.ff_fyc or 0), 2)
                db.session.add(tier)
                db.session.flush()
                for j, p in enumerate(t.get('plans', [])):
                    _seed_plan(c, tier, j, p)
        elif not CrmContractPlan.query.filter_by(contract_id=c.id).count():
            # 一期遗留合约：缴别已存在但无方案，按 tier_sets 顺序给每个缴别补齐方案政策
            tset = tier_sets.get(s['code']) or []
            for tier, t in zip(tiers, tset):
                for j, p in enumerate(t.get('plans', [])):
                    _seed_plan(c, tier, j, p)
        # 四改：给演示缴别补上「总总 / 分分」费率有效期（为空才写，默认 2026 全年）
        for _t in CrmContractTier.query.filter_by(contract_id=c.id).all():
            if not _t.tt_start:
                _t.tt_start, _t.tt_end = '2026-01-01', '2026-12-31'
            if not _t.ff_start:
                _t.ff_start, _t.ff_end = '2026-01-01', '2026-12-31'
        # 十七改：演示「合约到期提醒」——两条演示合约做成 临期 / 已过期。
        # 用**相对今天**的日期（+12 天 / -8 天），任何时候重建演示库都能看到红黄灯效果。
        if s['code'] in ('PTC-DEMO-004', 'PTC-DEMO-005'):
            _off = 12 if s['code'] == 'PTC-DEMO-004' else -8
            _end = (date.today() + timedelta(days=_off)).strftime('%Y-%m-%d')
            for _t in CrmContractTier.query.filter_by(contract_id=c.id).all():
                _t.tt_end, _t.ff_end = _end, _end
            for _p in CrmContractPlan.query.filter_by(contract_id=c.id).all():
                _p.tt_end, _p.ff_end = _end, _end
        db.session.flush()

    # ③ 产品资料库（4 份，生成演示 PDF 占位件）
    up = app.config.get('UPLOAD_DIR')
    docs = [
        ('中国人寿', '国寿鑫享金生年金保险', 'guide', '国寿鑫享金生 · 保司微投指南',
         '2026 版', '国寿鑫享金生_微投指南.pdf',
         ['一、产品定位与目标客群', '二、投保规则速览', '三、微投出单操作指引', '四、常见问题']),
        ('中国人寿', '国寿鑫享金生年金保险', 'clause', '国寿鑫享金生 · 产品条款',
         '监管备案版', '国寿鑫享金生_产品条款.pdf',
         ['第一条 保险合同的构成', '第二条 保险责任', '第三条 责任免除', '第四条 保险金申请']),
        ('平安人寿', '平安御享金瑞增额终身寿险', 'rate', '平安御享金瑞 · 费率表',
         '2026-01 生效', '平安御享金瑞_费率表.pdf',
         ['投保年龄：0-70 周岁', '交费期间：3/5/10 年交', '保额递增比例：3.0%', '（示例费率，仅供演示）']),
        ('太平洋寿险', '太保长相伴（盛世版）增额终身寿险', 'manual', '太保长相伴 · 产品说明书',
         '2026 版', '太保长相伴_产品说明书.pdf',
         ['一、产品特色', '二、保障责任', '三、现金价值说明', '四、投保示例']),
    ]
    if up:
        ddir = os.path.join(up, 'crm', 'doc')
        os.makedirs(ddir, exist_ok=True)
        for ins_short, pname, dt, title, ver, fname, rows in docs:
            ins = insurers[ins_short]
            disk = 'DOC' + fname.replace('.pdf', '').replace(' ', '')[:20] + '.pdf'
            full = os.path.join(ddir, disk)
            if not os.path.exists(full):
                _demo_pdf(full, title, rows)
            size = os.path.getsize(full) if os.path.exists(full) else 0
            get_or_create(CrmProductDoc, insurer_name=ins.name, title=title, defaults={
                'insurer_id': ins.id, 'product_name': pname, 'doc_type': dt, 'version': ver,
                'file_name': fname, 'file_path': disk, 'file_size': size,
                'note': '演示资料', 'upload_by': uid})
    db.session.flush()

    # ④ 省分公司（4 家）
    bspecs = [
        ('中国人寿', '广东省', '中国人寿保险股份有限公司广东省分公司', '广东国寿',
         '陈国华', '020-8333 1188', '广州市越秀区东风东路 767 号'),
        ('中国人寿', '深圳市', '中国人寿保险股份有限公司深圳市分公司', '深圳国寿',
         '林志强', '0755-2688 6001', '深圳市福田区深南大道 4001 号'),
        ('平安人寿', '广东省', '中国平安人寿保险股份有限公司广东分公司', '平安广分',
         '黄文辉', '020-3888 2200', '广州市天河区珠江新城珠江西路 5 号'),
        ('太平洋寿险', '广东省', '中国太平洋人寿保险股份有限公司广东分公司', '太保广分',
         '吴海东', '020-2888 3300', '广州市天河区体育西路 189 号'),
    ]
    for ins_short, prov, name, short, principal, phone, addr in bspecs:
        ins = insurers[ins_short]
        get_or_create(InsurerBranch, name=name, defaults={
            'insurer_id': ins.id, 'insurer_name': ins.name, 'province': prov,
            'short_name': short, 'principal': principal, 'phone': phone, 'address': addr,
            'establish_date': '2003-06-18', 'contact_person': '李娜',
            'contact_phone': '020-8888 6001', 'note': '演示数据：工商信息待核实补录',
            'created_by': uid})
    db.session.flush()

    # 兼容：把涉及省分公司的演示合约挂到对应省分公司（为空才写；旧行靠这段补齐）
    # 五改后「总-分(tf) / 分-分(ff)」都归分公司口径，都必须指明省分公司
    _br_link = {'PTC-DEMO-002': '平安广分', 'PTC-DEMO-003': '太保广分',
                'PTC-DEMO-005': '广东国寿'}
    for cid_code, br_short in _br_link.items():
        _c = CrmProductContract.query.filter_by(code=cid_code).first()
        _b = InsurerBranch.query.filter_by(short_name=br_short).first()
        if _c and _b and not _c.branch_id:
            _c.branch_id, _c.branch_name = _b.id, _b.name
    db.session.flush()

    # ④·B 联络人（联系与对接，一对多：保司 / 省分公司各可多人）
    lmspecs = [
        ('中国人寿', '李娜', '中介渠道部 渠道经理', '020-8333 1189', 'lina@gd.chinalife.cn', '主对接人'),
        ('中国人寿', '王志明', '中介渠道部 高级经理', '020-8333 1190', 'wangzm@gd.chinalife.cn', '方案 / 加佣对接'),
        ('中国人寿', '张建国', '总经理室 总经理', '020-8333 1001', '', '重大事项'),
        ('平安人寿', '郑丽华', '中介业务部 部门经理', '0755-2688 1199', 'zhenglh@pingan.com', ''),
        ('平安人寿', '周文杰', '中介业务部 渠道主管', '0755-2688 1200', '', ''),
        ('太平洋寿险', '徐建平', '经代渠道部 负责人', '020-2888 1199', 'xujp@cpic.com.cn', ''),
    ]
    for ins_short, nm, title, phone, email, note in lmspecs:
        ins = insurers.get(ins_short)
        if ins and not InsurerLinkman.query.filter_by(
                owner_type='insurer', owner_id=ins.id, name=nm).first():
            db.session.add(InsurerLinkman(
                owner_type='insurer', owner_id=ins.id, name=nm, title=title,
                phone=phone, email=email, note=note))
    blmspecs = [
        ('广东国寿', '李娜', '中介渠道部 渠道经理', '020-8888 6001', 'lina@gd.chinalife.cn', '省分主对接'),
        ('广东国寿', '刘晓峰', '银保部 高级主管', '020-8888 6002', '', ''),
        ('平安广分', '郑丽华', '中介业务部 部门经理', '020-3888 2299', 'zhenglh@pingan.com', ''),
    ]
    for br_short, nm, title, phone, email, note in blmspecs:
        _b = InsurerBranch.query.filter_by(short_name=br_short).first()
        if _b and not InsurerLinkman.query.filter_by(
                owner_type='branch', owner_id=_b.id, name=nm).first():
            db.session.add(InsurerLinkman(
                owner_type='branch', owner_id=_b.id, name=nm, title=title,
                phone=phone, email=email, note=note))
    db.session.flush()

    # ⑤ 岗位人员联络簿（含一条「已离任」历史，演示动态留痕）
    cspecs = [
        ('中国人寿', 'gm', '张建国', '020-8333 1001', 'current', '2019-03-01', ''),
        ('中国人寿', 'mid_head', '刘海燕', '020-8333 1002', 'current', '2024-01-01', '分管经代渠道'),
        ('中国人寿', 'channel_head', '李国强', '020-8333 1009', 'left', '2019-03-01', '2026-03-31'),
        ('中国人寿', 'channel_head', '王志明', '020-8333 1003', 'current', '2026-04-01', '接手经代渠道'),
        ('平安人寿', 'gm', '陈永昌', '0755-2688 1001', 'current', '2018-07-01', ''),
        ('平安人寿', 'mid_head', '周文杰', '0755-2688 1002', 'current', '2023-05-01', '分管中介业务'),
        ('平安人寿', 'channel_head', '郑丽华', '0755-2688 1003', 'current', '2025-02-01', ''),
        ('太平洋寿险', 'gm', '黄伟民', '020-2888 1001', 'current', '2020-09-01', ''),
        ('太平洋寿险', 'mid_head', '徐建平', '020-2888 1002', 'current', '2024-03-15', ''),
        ('人保财险', 'channel_head', '吴天成', '020-3838 1001', 'current', '2022-06-01', ''),
    ]
    for ins_short, role, nm, phone, st, sd, ed in cspecs:
        ins = insurers[ins_short]
        get_or_create(InsurerContact, insurer_name=ins.name, name=nm, role=role, defaults={
            'insurer_id': ins.id, 'phone': phone, 'status': st,
            'start_date': sd, 'end_date': ed,
            'note': '演示数据', 'created_by': uid})
    db.session.flush()

    # ⑥ 年度经营指标（近 5 年：投资回报率 / 综合投资收益率 / 评级 / 总保费）
    #    字段顺序：(年度, 投资回报率%, 综合投资收益率%, 评级, 总保费(亿元), 净利润(亿元), 偿付能力充足率%, 数据来源)
    mspecs = {
        '中国人寿': [
            ('2021', 4.98, 4.61, 'AAA', 6183, 502, 244, '公司年报'),
            ('2022', 4.36, 4.05, 'AAA', 6152, 321, 208, '公司年报'),
            ('2023', 4.65, 4.28, 'AAA', 6414, 211, 218, '公司年报'),
            ('2024', 5.12, 4.86, 'AAA', 6717, 1069, 212, '公司年报'),
            ('2025', 4.85, 4.32, 'AAA', 6930, 980, 205, '年报快报'),
        ],
        '平安人寿': [
            ('2021', 4.20, 4.02, 'AA', 4215, 812, 236, '公司年报'),
            ('2022', 3.85, 3.66, 'AA', 4290, 762, 224, '公司年报'),
            ('2023', 4.10, 3.88, 'AA', 4511, 902, 231, '公司年报'),
            ('2024', 4.55, 4.21, 'AA', 4720, 1051, 228, '公司年报'),
            ('2025', 4.40, 4.10, 'AA', 4835, 1120, 226, '年报快报'),
        ],
        '太平洋寿险': [
            ('2021', 4.42, 4.18, 'AA', 2095, 268, 232, '公司年报'),
            ('2022', 4.05, 3.82, 'AA', 2224, 248, 219, '公司年报'),
            ('2023', 4.28, 4.01, 'AA', 2331, 272, 226, '公司年报'),
            ('2024', 4.65, 4.35, 'AA', 2480, 355, 230, '公司年报'),
            ('2025', 4.50, 4.22, 'AA', 2601, 372, 228, '年报快报'),
        ],
    }
    for ins_short, rows in mspecs.items():
        ins = insurers[ins_short]
        for y, roi, cy, rating, prem, profit, solv, src in rows:
            get_or_create(InsurerMetric, insurer_id=ins.id, year=y, defaults={
                'insurer_name': ins.name, 'roi': roi, 'composite_yield': cy,
                'rating': rating, 'rating_org': '监管（风险综合评级）',
                'premium': prem, 'premium_unit': 'yi', 'net_profit': profit,
                'solvency': solv, 'source': src,
                'note': '演示数据，仅供销售策略参考', 'updated_by': uid})
    db.session.flush()

    # ---- 二十一改：业务团队系数设置（系数 × 基准费率，对齐小程序）----
    # 基本法团队：统一一套 kf/kr/ka；渠道团队：每个渠道负责人各挂一套。
    # ⚠️ 渠道负责人必须与「人力数据清单」里的渠道团队人员一致（2026-10-05 口径对齐），
    #    否则会出现「系数挂了个人名，人却不在清单里」的孤儿数据。
    cspecs = [
        ('basic', '', '基本法团队统一系数', 100.0, 100.0, 100.0,
         '基本法团队一套系数，全员同一口径（100% 不放大不缩小）'),
        ('channel', '陈志强', '陈志强渠道系数', 80.0, 90.0, 85.0,
         '渠道负责人陈志强（广州一区渠道总）：FYC 8 折、RYC 9 折、加佣 8.5 折'),
    ]
    # 清理历史演示残留：旧 seed 用过「张三 / 李四」，人力清单里并无此人 → 孤儿行
    for _nm in ('张三', '李四'):
        for _r in CrmTeamCoefficient.query.filter_by(team_type='channel',
                                                     owner_name=_nm).all():
            db.session.delete(_r)
    db.session.flush()
    for tt, own, nm, kf, kr, ka, note in cspecs:
        get_or_create(CrmTeamCoefficient, team_type=tt, owner_name=own,
                      defaults={'name': nm, 'kf': kf, 'kr': kr, 'ka': ka,
                                'sort_no': 0, 'status': 'on',
                                'note': note, 'created_by': uid})
    db.session.flush()

    # ---- 二十二改：分配税费参数（全局一套，默认增值税 6% + 附加 12%）----
    # 给团队分配前先扣公司承担税费：含税结算 → −增值税 → −附加税费 → 税后净额 → ×系数。
    get_or_create(CrmTaxConfig, name='默认税费参数', defaults={
        'vat_rate': 6.0, 'surcharge_rate': 12.0, 'status': 'on',
        'note': '增值税 6%（开票税率）＋附加税费 12%（城建7+教育3+地方教育2，按增值税额计）',
        'updated_by': uid})
    db.session.flush()

    # ---- 十九改：业务团队人力架构 + 人力数据清单（费用政策的取数底表）----
    # 顺序不能反：先建经代主体与架构，再把人员挂到营业组上。
    seed_agency()          # 十九改++：合作经代公司（人力档案「所属经代公司」的取值来源）
    seed_org_struct()
    seed_manpower()


def seed():
    with app.app_context():
        db.create_all()  # 按 models.py 创建所有表（已存在的表不会重建）

        # --- 0. 轻量迁移：给已存在的旧表补加新增列（SQLite 的 create_all 不会改旧表）---
        _migrate_columns()
        # --- 0.1 值级迁移：历史数据取值调整（幂等，每个改单加一条）---
        _migrate_values()

        # --- 0.2 十六改：CRM 唯一性校验规则（后台「唯一性规则」页可开关）---
        # 只种开关行，不覆盖管理员已改的状态（get_or_create 命中旧行时不应用 defaults 的坑见 §6.8）。
        _uniq_specs = [
            ('insurer_name', '保司全称唯一', 'insurer',
             '同一家保险公司的工商全称只能录一次（比对时忽略空格与大小写）', 'on', 1),
            ('insurer_short', '保司简称唯一', 'insurer',
             '保司简称不能重复——列表统一按简称展示，重复会导致认不出是哪家', 'on', 2),
            ('insurer_credit', '统一社会信用代码唯一', 'insurer',
             '18 位统一社会信用代码是工商唯一标识，重复即为同一家公司的不同马甲', 'on', 3),
            ('branch_name', '省分公司全称唯一', 'branch',
             '同一家保司旗下的省分公司全称不能重复', 'on', 4),
            ('branch_short', '省分公司简称唯一', 'branch',
             '同一家保司旗下的省分公司简称不能重复', 'on', 5),
            # 三十八改（2026-10-29）：产品代码唯一 —— 同一保司下一个代码只属于一个产品
            ('product_code', '同一保司下产品代码唯一', 'contract',
             '同一保司下一个产品代码只能属于一个产品；同一产品的多个保障期限共用一个代码',
             'on', 6),
            ('contract_product', '同一保司+产品 仅一条合约', 'contract',
             '同一保司的同一款产品，在同一口径（总/分）下只能建一条合约', 'on', 7),
            ('tier_period', '同一保司+产品+缴别 仅一条', 'contract',
             '同一产品的同一个缴别（如 3年交）在同一口径下只能录一次费用政策', 'on', 8),
            ('contract_cross_side', '跨口径（总总/分分）也唯一', 'contract',
             '打开后：同一产品+缴别连总总与分分之间也互斥。默认关闭——两个口径各录一次属正常业务',
             'off', 9),
        ]
        for _c, _nm, _gp, _ds, _en, _sn in _uniq_specs:
            get_or_create(CrmUniqueRule, code=_c, defaults={
                'name': _nm, 'group': _gp, 'desc': _ds,
                'enabled': _en, 'sort_no': _sn})
        db.session.flush()

        # --- 0.5 五险一金比例配置（#145 单例 id=1）：默认国家标准值，HR 后台可调 ---
        sc = get_or_create(HrSocialConfig, id=1, defaults={
            'pension_pct': 8, 'medical_pct': 2, 'unemploy_pct': 0.5, 'fund_pct': 12,
            'corp_pension_pct': 16, 'corp_medical_pct': 8, 'corp_unemploy_pct': 0.5,
            'corp_injury_pct': 0.5, 'corp_maternity_pct': 0.8, 'corp_fund_pct': 12,
            'base_min': 0, 'base_max': 999999,
            'note': '默认国家标准；养老8%/医疗2%/失业0.5%/公积金12%，基数上下限按当地社平调整',
        })
        # 老库补加列后单位比例为 NULL → 填默认值（仅在为空时补，不覆盖 HR 已改的值）
        for _f, _d in [('corp_pension_pct', 16), ('corp_medical_pct', 8), ('corp_unemploy_pct', 0.5),
                       ('corp_injury_pct', 0.5), ('corp_maternity_pct', 0.8), ('corp_fund_pct', 12),
                       ('pension_pct', 8), ('medical_pct', 2), ('unemploy_pct', 0.5), ('fund_pct', 12)]:
            if getattr(sc, _f) is None:
                setattr(sc, _f, _d)
        db.session.flush()

        # --- 0.6 岗位补贴项（后台端口）：默认给两项示例，HR 可增删改（仅在表为空时种）---
        if HrAllowanceItem.query.count() == 0:
            db.session.add(HrAllowanceItem(name='通讯费', default_amount=100, enabled=True, sort=1,
                                           note='示例项，可在「岗位补贴项」设置里修改或删除'))
            db.session.add(HrAllowanceItem(name='交通费', default_amount=200, enabled=True, sort=2,
                                           note='示例项，可在「岗位补贴项」设置里修改或删除'))
            db.session.flush()
            print('  [种子] 岗位补贴项：通讯费 / 交通费（示例）')

        # --- 0.7 历史数据回填：老记录的 regular_total 曾是"单一总薪资"，拆出绩效部分，
        #         保证「基本+绩效+岗位补贴 = 总薪资」恒等（只补 regular_perf 为空的记录）---
        _fixed = 0
        for _o in Onboarding.query.all():
            if _o.regular_perf is None:
                _o.regular_perf = max(0.0, round((_o.regular_total or 0) - (_o.regular_base or 0)
                                                 - (_o.regular_allow or 0), 2))
                _fixed += 1
        if _fixed:
            db.session.flush()
            print('  [迁移] 入职登记薪资拆分回填 %d 条' % _fixed)

        # --- 1. 权限（数据可见范围的三档）---
        p_self = get_or_create(Permission, name='查看本人数据', code='data.view_self')
        p_team = get_or_create(Permission, name='查看团队数据', code='data.view_team')
        p_all = get_or_create(Permission, name='查看全量数据', code='data.view_all')
        # 审批权限（之前漏建会被 seed 引用报错，这里补上）
        p_approve = get_or_create(Permission, name='审批-通过/驳回', code='approval.approve')
        # 人事权限：员工管理（新增/修改员工档案）
        p_hr = get_or_create(Permission, name='人事-员工管理', code='hr.employee.manage')
        # 人事权限：工资管理（录入/修改工资清单，敏感，仅管理人员）
        p_salary = get_or_create(Permission, name='人事-工资管理', code='hr.salary.manage')
        # 人事权限：组织架构/部门设置（含人员部门归属）
        p_org = get_or_create(Permission, name='人事-组织架构管理', code='hr.org.manage')
        # 人事权限：假期类型与额度设置（人事管理员在后台配置可请天数）
        p_leave = get_or_create(Permission, name='人事-假期设置', code='hr.leave.manage')
        # 人事权限：出勤数据导入（差勤模块 CSV 导入每日出勤）
        p_attendance = get_or_create(Permission, name='人事-出勤数据导入', code='hr.attendance.manage')
        # 人事权限：合同电子版上传备份（签署后归档）
        p_contract_upload = get_or_create(Permission, name='人事-合同上传', code='hr.contract.upload')
        # 人事权限：法定节假日导入（人事管理员每年年初导入国务院假期数据）
        p_holiday = get_or_create(Permission, name='人事-节假日设置', code='hr.holiday.manage')
        # 人事权限：日常考勤规则设置（上下班时间/打卡规则）
        p_attrule = get_or_create(Permission, name='人事-考勤规则设置', code='hr.attrule.manage')
        # 行政权限：新建/管理审批流（行政管理员在 OA 后台设置审批流、手工添加审批人）
        p_flow = get_or_create(Permission, name='行政-审批流管理', code='oa.flow.manage')
        # 行政权限：公告发布/删除（行政负责人负责公司内部重要公示公告的编辑发布，及未来官网信息发布）
        p_announce = get_or_create(Permission, name='行政-公告管理', code='oa.announce.manage')
        # 财务权限：账套设置（核算主体/会计制度/纳税人身份/启用期间/凭证字别）
        p_fin_cfg = get_or_create(Permission, name='财务-账套设置', code='fin.config.manage')
        # 财务权限：会计科目管理（科目表增删改、启停、辅助核算）
        p_fin_acc = get_or_create(Permission, name='财务-科目管理', code='fin.account.manage')
        # 财务权限：期初建账（期初余额录入、试算平衡、确认/撤销建账）
        p_fin_open = get_or_create(Permission, name='财务-期初建账', code='fin.opening.manage')
        # 财务权限：费用结算确认函（导入 Excel、生成 PDF、签回归档；查看只需登录，不单独设权限）
        p_fin_confirm = get_or_create(Permission, name='财务-费用结算确认函', code='fin.confirm.manage')
        # 财务权限：凭证录入/修改草稿（会计日常操作，增量 #32）
        p_fin_vch_edit = get_or_create(Permission, name='财务-凭证录入', code='fin.voucher.edit')
        # 财务权限：凭证查询（增量 #32）
        p_fin_vch_view = get_or_create(Permission, name='财务-凭证查询', code='fin.voucher.view')
        # 财务权限：凭证审核 / 记账 / 作废 / 红冲（增量 #33）——只给财务主管层（S/A/B），会计(C)拿不到
        p_fin_vch_audit = get_or_create(Permission, name='财务-凭证审核记账', code='fin.voucher.audit')
        # 财务权限：报表中心（增量 #35）——查看/导出归财务主管层与会计；
        #   改公式（报表自定义）只归财务主管层，会计看得到表但改不了口径
        p_fin_rpt_view = get_or_create(Permission, name='财务-报表查看导出', code='fin.report.view')
        p_fin_rpt_design = get_or_create(Permission, name='财务-报表自定义', code='fin.report.design')
        # 财务权限：期末结转与结账（增量 #36）——只给财务主管层（S/A/B）。
        #   结账 = 关账锁期，是主管的把关动作；会计(C)能看结账状态但按不下这个按钮
        p_fin_closing = get_or_create(Permission, name='财务-期末结转结账', code='fin.closing.manage')
        # 财务权限：发票管理（增量 #37）——进项/销项台账 + 认证抵扣 + 增值税测算。
        #   会计(C)也要能录入发票，所以 S/A/B/C 全给；D/E 无（发票是财务专用数据）
        p_fin_invoice = get_or_create(Permission, name='财务-发票管理', code='fin.invoice.manage')
        p_fin_auto = get_or_create(Permission, name='财务-自动记账', code='fin.auto_book')
        # 系统管理：IT 后台（权限流配置）闸门权限（#40）
        p_it_sys = get_or_create(Permission, name='IT-系统管理', code='it.system.manage')
        # 财务审计：操作日志查看（#40，S/A/B + IT 级可看，但不含任何 fin.* 数据权限）
        p_fin_audit = get_or_create(Permission, name='财务-审计查看', code='fin.audit.view')
        # 财务备份：一键备份/恢复（#40，S/A/B + IT 级可操作）
        p_fin_backup = get_or_create(Permission, name='财务-备份恢复', code='fin.backup')
        # 财务权限：银企对账（二期 #42）——银行账户/流水/对账/余额调节表（S/A/B）
        p_fin_bank_view = get_or_create(Permission, name='财务-银企对账查看', code='fin.bank.view')
        p_fin_bank_manage = get_or_create(Permission, name='财务-银企对账管理', code='fin.bank.manage')
        p_fin_cash_view = get_or_create(Permission, name='财务-出纳收付款查看', code='fin.cash.view')
        p_fin_cash_edit = get_or_create(Permission, name='财务-出纳收付款制单', code='fin.cash.edit')
        # OA 费用申请与报销（2026-09-30）：审批 / 付款确认
        # 审批 = 主管层（S/A/B/C）；付款确认 = 财务层（S/A/B）
        p_oa_exp_approve = get_or_create(Permission, name='OA-费用申请报销审批', code='oa.expense.approve')
        p_oa_exp_pay = get_or_create(Permission, name='OA-费用付款确认', code='oa.expense.pay')
        # 行政管理系统（ADM 一期，2026-10-01）：三本台账 + 本版块分类设置
        #   adm.view = 台账查看（S/A/B/C/D/E 全员，普通员工也能看公司资产/证照台账）
        #   三个 manage = 行政管理员维护对应台账（新建/编辑/删除）
        p_adm_view = get_or_create(Permission, name='行政-台账查看', code='adm.view')
        p_adm_lic = get_or_create(Permission, name='行政-证照资质管理', code='adm.license.manage')
        p_adm_ctr = get_or_create(Permission, name='行政-商务合同管理', code='adm.contract.manage')
        p_adm_ast = get_or_create(Permission, name='行政-固定资产管理', code='adm.asset.manage')
        # 二期预留：车辆管理 / 费用台账（页面未上线，权限先建好，上线时无需再改权限流）
        p_adm_veh = get_or_create(Permission, name='行政-车辆管理', code='adm.vehicle.manage')
        p_adm_exp = get_or_create(Permission, name='行政-费用台账管理', code='adm.expense.manage')
        # 二期（2026-10-01）：行政事务（办公用品 / 印章使用登记 / 快递收发）
        p_adm_svc = get_or_create(Permission, name='行政-行政事务管理', code='adm.service.manage')
        # 核心业务管理系统（CRM 一期，2026-10-01）：保司档案 + 产品合约
        #   crm.view = 查看（S/A/B/C/D/E 全员，产品政策需要让业务人员能查）
        #   两个 manage = 保司档案 / 产品合约（含产品资料库）维护
        p_crm_view = get_or_create(Permission, name='核心业务-查看', code='crm.view')
        p_crm_ins = get_or_create(Permission, name='核心业务-保司档案管理', code='crm.insurer.manage')
        p_crm_ctr = get_or_create(Permission, name='核心业务-产品合约管理', code='crm.contract.manage')

        # --- 2. 职级（S/A/B/C/D/E）：按人员职级设定，是权限分配的载体 ---
        lv_s = get_or_create(Level, code='S', name='最高负责人', titles='总经理/老板')
        lv_a = get_or_create(Level, code='A', name='部门负责人', titles='部门负责人')
        lv_b = get_or_create(Level, code='B', name='部门总经理', titles='部门总经理')
        lv_c = get_or_create(Level, code='C', name='部门主管', titles='部门主管')
        lv_d = get_or_create(Level, code='D', name='部门员工', titles='部门员工')
        lv_e = get_or_create(Level, code='E', name='其他人员', titles='其他')
        db.session.flush()

        # 佣金/奖金权限（营销负责人专属，S/A/B/C 级默认授予）
        p_comm_admin = get_or_create(Permission, name='佣金-管理员', code='commission.admin')
        # 默认权限流（IT 管理员后台可随时改）：
        # S/A/B/C 有审批权 + 人事管理权 + 佣金管理权（管理人员），D/E 无
        # 数据可见范围：S/A/B=全量，C=团队，D/E=本人
        # 财务：S/A/B 是财务主管层（账套/科目/期初/确认函/审核记账/报表自定义/期末结转结账全权）；
        #      C 是会计层——只给「凭证录入 + 凭证查询 + 报表查看」，
        #      审核 / 记账 / 红冲（#33）、报表改公式（#35）、结账锁期（#36）都归主管
        lv_s.permissions = [p_all, p_approve, p_hr, p_salary, p_org, p_leave, p_comm_admin, p_attendance, p_contract_upload, p_holiday, p_attrule, p_flow, p_announce, p_fin_cfg, p_fin_acc, p_fin_open, p_fin_confirm, p_fin_vch_edit, p_fin_vch_view, p_fin_vch_audit, p_fin_rpt_view, p_fin_rpt_design, p_fin_closing, p_fin_invoice, p_fin_auto, p_it_sys, p_fin_audit, p_fin_backup, p_fin_bank_view, p_fin_bank_manage, p_fin_cash_view, p_fin_cash_edit, p_oa_exp_approve, p_oa_exp_pay, p_adm_view, p_adm_lic, p_adm_ctr, p_adm_ast, p_adm_veh, p_adm_exp, p_adm_svc, p_crm_view, p_crm_ins, p_crm_ctr]
        lv_a.permissions = [p_all, p_approve, p_hr, p_salary, p_org, p_leave, p_comm_admin, p_attendance, p_contract_upload, p_holiday, p_attrule, p_flow, p_announce, p_fin_cfg, p_fin_acc, p_fin_open, p_fin_confirm, p_fin_vch_edit, p_fin_vch_view, p_fin_vch_audit, p_fin_rpt_view, p_fin_rpt_design, p_fin_closing, p_fin_invoice, p_fin_auto, p_it_sys, p_fin_audit, p_fin_backup, p_fin_bank_view, p_fin_bank_manage, p_fin_cash_view, p_fin_cash_edit, p_oa_exp_approve, p_oa_exp_pay, p_adm_view, p_adm_lic, p_adm_ctr, p_adm_ast, p_adm_veh, p_adm_exp, p_adm_svc, p_crm_view, p_crm_ins, p_crm_ctr]
        lv_b.permissions = [p_all, p_approve, p_hr, p_salary, p_org, p_leave, p_comm_admin, p_attendance, p_contract_upload, p_holiday, p_attrule, p_flow, p_announce, p_fin_cfg, p_fin_acc, p_fin_open, p_fin_confirm, p_fin_vch_edit, p_fin_vch_view, p_fin_vch_audit, p_fin_rpt_view, p_fin_rpt_design, p_fin_closing, p_fin_invoice, p_fin_auto, p_it_sys, p_fin_audit, p_fin_backup, p_fin_bank_view, p_fin_bank_manage, p_fin_cash_view, p_fin_cash_edit, p_oa_exp_approve, p_oa_exp_pay, p_adm_view, p_adm_lic, p_adm_ctr, p_adm_ast, p_adm_veh, p_adm_exp, p_adm_svc, p_crm_view, p_crm_ins, p_crm_ctr]
        lv_c.permissions = [p_team, p_approve, p_hr, p_salary, p_org, p_leave, p_comm_admin, p_attendance, p_contract_upload, p_holiday, p_attrule, p_flow, p_announce, p_fin_vch_edit, p_fin_vch_view, p_fin_rpt_view, p_fin_invoice, p_oa_exp_approve, p_adm_view, p_adm_lic, p_adm_ctr, p_adm_ast, p_adm_veh, p_adm_exp, p_adm_svc, p_crm_view, p_crm_ins, p_crm_ctr]
        lv_d.permissions = [p_self, p_adm_view, p_crm_view]
        lv_e.permissions = [p_self, p_adm_view, p_crm_view]
        # IT 系统管理员：独立职级（#40 红线）—— 只管权限流配置 + 看审计 + 做备份，
        # 不挂任何 fin.* 财务数据权限，进不了任何财务页面（财务路由都要求 fin.*）。
        lv_it = get_or_create(Level, code='IT', name='IT系统管理员')
        lv_it.permissions = [p_it_sys, p_fin_audit, p_fin_backup, p_fin_bank_view, p_fin_bank_manage, p_fin_cash_view, p_fin_cash_edit]
        db.session.flush()

        # --- 3. 组织（公司 -> 团队，构成树）---
        # 公司节点名称统一用「工商全称」，人事卡片等正式单据的抬头取此值。
        org_company = get_or_create(Organization, name='广东海盈通保智能科技有限公司',
                                    parent_id=None, type='company')
        db.session.flush()
        get_or_create(Organization, name='广州一部', parent_id=org_company.id, type='team')
        # 演示部门（人事组织架构管理会用到）：营销部 / 财务部 / 人事行政部
        org_marketing = get_or_create(Organization, name='营销部', parent_id=org_company.id, type='team')
        org_finance = get_or_create(Organization, name='财务部', parent_id=org_company.id, type='team')
        org_hr = get_or_create(Organization, name='人事行政部', parent_id=org_company.id, type='team')

        # --- 4. 渠道 / 产品（主数据样例）---
        get_or_create(Channel, name='个险渠道', company='')
        get_or_create(Product, name='安心意外险', company='平安', status='on')

        # --- 5. 演示用户（密码统一 123456，仅开发用）---
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(
                username='admin',
                password_hash=generate_password_hash('123456'),
                level_id=lv_s.id,  # 演示账号设为 S 级（总经理/老板），可进 IT 后台
                org_id=org_company.id,
                is_admin=True,   # 系统管理员：拥有全部二级菜单模块权限
                must_change_password=True,  # 初始弱口令，强制首次登录改密码
            ))
        # 兜底：旧库 admin 已存在但无 is_admin 标记时，确保其为管理员
        _admin = User.query.filter_by(username='admin').first()
        if _admin and not _admin.is_admin:
            _admin.is_admin = True
            db.session.commit()

        # --- 6. 演示公告（OA 公告公示）---
        if not Announcement.query.first():
            admin = User.query.filter_by(username='admin').first()
            get_or_create(Announcement,
                          title='欢迎使用海盈通保内部管理系统',
                          content='本系统先上线 OA 办公模块（公告公示），后续将逐步扩展营销、财务等模块。',
                          publisher_id=admin.id,
                          is_pinned=True)

        # --- 7. 演示员工（人事系统 · 员工花名册）---
        # 这里逐个按工号 get_or_create，不用"整块跳过"的守卫：
        # 这样以后新增演示人员时，老库里再跑一次 seed 也能把人补齐。
        # 员工去重只按 employee_no（避免旧库 org_id 不一致导致重复插入）；
        # 取到实例后再统一赋值，保证可重复执行、可增量修正部门归属。
        zhang = get_or_create(Employee, employee_no='HY001')
        zhang.name = '张伟'; zhang.level_id = lv_s.id; zhang.org_id = org_company.id
        zhang.title = '总经理'; zhang.status = 'active'; zhang.staff_type = 'internal'

        li = get_or_create(Employee, employee_no='HY002')
        li.name = '李娜'; li.level_id = lv_c.id; li.org_id = org_marketing.id
        li.title = '营销主管'; li.status = 'active'; li.staff_type = 'internal'

        wang = get_or_create(Employee, employee_no='HY003')
        wang.name = '王芳'; wang.level_id = lv_d.id; wang.org_id = org_finance.id
        wang.title = '财务员工'; wang.status = 'active'; wang.staff_type = 'internal'

        # 外勤示例：签代理合同，归销售渠道（不挂 S/A/B/C/D/E 职级）
        # 三层代理链条：DL001 渠道总 -> DL101 主管 -> DL103 代理人
        # （佣金/奖金的查看范围就靠这条链条，缺人就查不到数据）
        dl001 = get_or_create(Employee, employee_no='DL001')
        dl001.name = '赵磊'; dl001.staff_type = 'external'; dl001.agent_type = '渠道总'
        dl001.title = '渠道总'; dl001.status = 'active'

        dl101 = get_or_create(Employee, employee_no='DL101')
        dl101.name = '孙浩'; dl101.staff_type = 'external'; dl101.agent_type = '主管'
        dl101.title = '渠道主管'; dl101.status = 'active'

        dl103 = get_or_create(Employee, employee_no='DL103')
        dl103.name = '周敏'; dl103.staff_type = 'external'; dl103.agent_type = '代理人'
        dl103.title = '代理人'; dl103.status = 'active'
        db.session.flush()
        # 建立上下级关系（每次执行都刷新一遍，保证链条完整）
        dl001.sub_team_ids = str(dl101.id)
        dl101.sub_team_ids = str(dl103.id)

        # --- 8. 演示工资（人事系统 · 工资清单，仅内部员工）---
        if not Salary.query.first():
            zhang = Employee.query.filter_by(employee_no='HY001').first()
            if zhang:
                get_or_create(Salary, employee_id=zhang.id, month='2026-08',
                              base_salary=20000, position_salary=5000, performance_salary=8000,
                              subsidy=1000, pension=1600, medical=400, housing_fund=2000,
                              note='月度工资示例')

        # --- 9. 演示差勤（OA · 差勤管理，复用审批单）---
        if not Approval.query.filter_by(type='请假').first():
            admin = User.query.filter_by(username='admin').first()
            if admin:
                get_or_create(Approval, type='请假', title='年假申请',
                              content='申请休年假3天', applicant_id=admin.id, status='pending')

        # 外勤关键人员（佣金 / 奖金演示数据共用）
        # 统一在这里查询：避免变量只在某个 if 分支里赋值，后续代码引用时报 NameError
        zhao = Employee.query.filter_by(employee_no='DL001').first()
        dl101 = Employee.query.filter_by(employee_no='DL101').first()
        dl103 = Employee.query.filter_by(employee_no='DL103').first()
        zhang = Employee.query.filter_by(employee_no='HY001').first()

        # --- 10. 演示佣金（佣金系统，仅外勤人员）---
        if not Commission.query.first() and zhao and dl101 and dl103:
            get_or_create(Commission, employee_id=zhao.id, month='2026-08',
                          product_name='安心意外险', channel_name='个险渠道',
                          premium=50000, commission_rate=30.0, commission_amount=15000,
                          status='confirmed', note='渠道总佣金示例')
            get_or_create(Commission, employee_id=dl101.id, month='2026-08',
                          product_name='安心意外险', channel_name='个险渠道',
                          premium=20000, commission_rate=25.0, commission_amount=5000,
                          status='pending', note='主管佣金-待确认')
            get_or_create(Commission, employee_id=dl103.id, month='2026-08',
                          product_name='安心意外险', channel_name='个险渠道',
                          premium=10000, commission_rate=20.0, commission_amount=2000,
                          status='confirmed', note='代理人佣金')

        # --- 11. 演示奖金池（佣金系统 · 季度奖金分配）---
        # 同样按"季度+名称"和"奖金池+员工"查重，可重复执行、可增量补人
        b = get_or_create(Bonus, quarter='2026-Q2', name='第二季度渠道奖励',
                          total_amount=50000, note='按渠道业绩分配')
        db.session.flush()
        if zhao:
            get_or_create(BonusAllocation, bonus_id=b.id, employee_id=zhao.id, amount=20000)
        if dl101:
            get_or_create(BonusAllocation, bonus_id=b.id, employee_id=dl101.id, amount=10000)

        # --- 12. 演示合同（人事系统 · 合同档案）---
        # 内勤=劳动合同(labor)，外勤=代理合同(agency)，由员工 staff_type 推导
        # 按"员工+合同类型"查重，天然幂等，不需要整块守卫，将来可随时补
        if zhang:
            get_or_create(Contract, employee_id=zhang.id, contract_type='labor',
                          contract_no='LD-2026-001', start_date=date(2026, 1, 1),
                          end_date=date(2028, 12, 31), status='active',
                          note='内勤劳动合同示例')
        if zhao:
            get_or_create(Contract, employee_id=zhao.id, contract_type='agency',
                          contract_no='DL-2026-001', start_date=date(2026, 1, 1),
                          end_date=date(2027, 12, 31), status='active',
                          note='外勤代理合同示例')

        # --- 13. 演示工资自定义字段（HR 后台可增删，工资页会动态多出这几列）---
        get_or_create(SalaryField, code='traffic_subsidy', name='交通补贴', is_deduction=False)
        get_or_create(SalaryField, code='late_deduction', name='迟到扣款', is_deduction=True)

        # --- 14. 假期类型（人事管理员在后台设置，休假申请按此校验）---
        # 17 类假期；事假以「小时」为单位且无休假标准；病假 / 婚假 / 产假 等需上传证明附件。
        # total_days=0 表示不限额度（如 病假凭证明）；配了「分级档位」的类型以档位为准。
        # ⚠️ code 是唯一键，取值只按 code 查重、其余字段放 defaults ——
        #    否则改了 total_days 后 filter 匹配不到旧行，会撞唯一约束报错。
        # (code, 名称, 单位, 年度额度, 需附件, 附件说明, 说明, 当年是否折算)
        LEAVE_SEED = [
            ('annual',      '年假',       'day',  5,   0, '',
             '带薪年假，按累计工作年限分级（法定：满1年5天/满10年10天/满20年15天）', '1'),
            ('personal',    '事假',       'hour', 0,   0, '',
             '员工因私事处理不能出勤的，须请假，并按事假处理，以小时为单位，不足1小时为1小时', '0'),
            ('comp',        '调休假',     'day',  0,   0, '',
             '加班 / 出差换休，按实际加班时长调休', '0'),
            ('sick',        '病假',       'day',  0,   1, '病例 / 诊断证明',
             '凭医院证明，不限额度', '0'),
            ('marriage',    '婚假',       'day',  3,   1, '结婚证',
             '法定婚假 3 天（广东）', '0'),
            ('prenatal',    '孕检假',     'day',  0,   0, '',
             '孕期产前检查，按出勤处理，不限额度', '0'),
            ('maternity',   '产假',       'day',  178, 1, '生育证明 / 出生医学证明',
             '广东：国家 98 天 + 奖励假 80 天 = 178 天', '0'),
            ('paternity',   '陪产假',     'day',  15,  0, '',
             '配偶生育护理假，广东 15 天', '0'),
            ('abortion',    '流产假',     'day',  15,  1, '医院证明',
             '怀孕未满 4 个月 15 天 / 满 4 个月 42 天，按实际证明核定', '0'),
            ('nursing',     '哺乳假',     'day',  0,   0, '',
             '婴儿 1 周岁内，每天 1 小时哺乳时间，不限额度', '0'),
            ('parenting',   '育儿假',     'day',  10,  0, '',
             '子女 3 周岁内，父母每年各 10 天（广东）', '0'),
            ('family_plan', '计划生育假', 'day',  0,   0, '',
             '计划生育手术等，按实际核定', '0'),
            ('child_edu',   '子女教育假', 'day',  0,   0, '',
             '子女入学 / 家长会等，按公司规定', '0'),
            ('bereavement', '丧假',       'day',  3,   0, '',
             '直系亲属丧亡，法定 3 天', '0'),
            ('work_injury', '工伤假',     'day',  0,   1, '工伤认定 / 医院证明',
             '按工伤认定结论停工留薪，不限额度', '0'),
            ('home_visit',  '探亲假',     'day',  0,   0, '',
             '按公司规定核定', '0'),
            ('other',       '其他假',     'day',  0,   0, '',
             '其他情形，需审批说明', '0'),
        ]
        lt_annual = None
        for _code, _name, _unit, _days, _na, _hint, _desc, _pro in LEAVE_SEED:
            _lt = get_or_create(LeaveType, code=_code, defaults={
                'name': _name, 'unit': _unit, 'total_days': _days,
                'need_attach': str(_na), 'attach_hint': _hint,
                'prorate': _pro, 'description': _desc})
            # 老行（本次改版前建的）新列是 NULL → 结构性字段（单位/是否需附件/附件说明）直接补齐；
            # 这些是「假期定义的固有属性」，不作为人事可自由调整的额度项，因此补齐不影响手工配置。
            if not _lt.unit:
                _lt.unit = _unit
            if not _lt.need_attach:
                _lt.need_attach = str(_na)
            if not _lt.attach_hint:
                _lt.attach_hint = _hint
            if _code == 'annual':
                lt_annual = _lt
        # 老库兼容（幂等）：早期版本把调休假叫「调休」、事假记成固定 5 天、产假按国标 98 天 —— 按新口径校准
        _lt_comp = LeaveType.query.filter_by(code='comp').first()
        if _lt_comp and _lt_comp.name == '调休':
            _lt_comp.name = '调休假'
        _lt_personal = LeaveType.query.filter_by(code='personal').first()
        if _lt_personal:
            _lt_personal.unit = 'hour'          # 事假法定按小时计
            if _lt_personal.total_days == 5:    # 早期默认值 → 改为无休假标准
                _lt_personal.total_days = 0
        _lt_mat = LeaveType.query.filter_by(code='maternity').first()
        if _lt_mat and _lt_mat.total_days == 98:  # 老默认国标 98 → 广东口径 178
            _lt_mat.total_days = 178
        if lt_annual is None:
            lt_annual = LeaveType.query.filter_by(code='annual').first()

        # --- 14b. 年假分级档位（法定标准；人事可在「假期设置」里逐档调整）---
        # 依据：《职工带薪年休假条例》第三条
        #   累计工作满 1 年不满 10 年 = 5 天；满 10 年不满 20 年 = 10 天；满 20 年 = 15 天
        # 注意：档位一旦被人工改过，重复执行 seed 不会覆盖（get_or_create 命中即返回）。
        for _i, (_lo, _hi, _d) in enumerate([(1, 10, 5), (10, 20, 10), (20, 0, 15)]):
            get_or_create(LeaveTier, leave_type_id=lt_annual.id, min_years=_lo, max_years=_hi,
                          defaults={'days': _d, 'sort': _i})

        # --- 15. 演示法定节假日（2026 年示例，国务院口径，人事管理员每年年初导入更新）---
        # 按日期唯一，可重复执行；day_type=rest 放假 / work 调休补班
        get_or_create(Holiday, holiday_date=date(2026, 1, 1), name='元旦', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 2, 16), name='春节', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 2, 17), name='春节', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 5, 1), name='劳动节', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 10, 1), name='国庆节', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 10, 2), name='国庆节', day_type='rest', year=2026)
        get_or_create(Holiday, holiday_date=date(2026, 10, 5), name='国庆节', day_type='work', year=2026, note='调休补班')

        # --- 16. 演示日常考勤规则（全局一条，人事管理员后台可改）---
        # 注意：只按 code 查重，其余字段走 defaults，避免字段值变化导致重复插入
        get_or_create(AttendanceRule, code='global',
                      defaults=dict(work_start='09:00', work_end='18:00',
                                    late_after=0, early_before=0,
                                    need_check_in=True, need_check_out=True,
                                    workdays='1111100',
                                    note='默认考勤规则：周一至周五 09:00-18:00'))

        # --- 17. 演示审批流（OA 系统 · 行政管理员后台配置）---
        # 一条通用审批流：第一级 admin（总经理），作为示例
        if not ApprovalFlow.query.first():
            admin = User.query.filter_by(username='admin').first()
            if admin:
                flow = get_or_create(ApprovalFlow, name='通用审批流',
                                     apply_type='', enabled=True, note='默认通用审批流示例')
                db.session.flush()
                get_or_create(ApprovalFlowStep, flow_id=flow.id, user_id=admin.id, seq=1)

        # --- 18. 演示入职登记（EHR · 入职全流程演示）---
        # 一条"待个人填报"的登记：操作员已建号（自动 HY 工号），个人填报链接可复制推送。
        # token 固定，方便开发期演示；正式使用由系统随机生成。
        if not Onboarding.query.first():
            org_hr2 = Organization.query.filter_by(name='人事行政部').first()
            get_or_create(Onboarding, employee_no='HY004', name='陈新', phone='13811112222',
                          token='demo-onboard-hy004', status='pending',
                          org_id=org_hr2.id if org_hr2 else None,
                          position='人事专员', job_title='人事专员',
                          note='演示数据：待个人填报')

        # 系统编码回填（#145 补充）：历史/演示入职登记补 RC****（幂等，可重复执行）
        _backfill_onboard_sys_code()

        # 申请单号回填（统一申请中心）：历史申请单补 前缀-YYYYMM-NNN
        _backfill_approval_code()

        # --- 19. 演示站内邮件（OA 邮箱）---
        if not Mail.query.first():
            admin = User.query.filter_by(username='admin').first()
            if admin:
                # 系统欢迎信：发件人=收件人=admin（自收，演示收件箱有内容）
                get_or_create(Mail, sender_id=admin.id, recipient_id=admin.id,
                              title='欢迎使用海盈通保站内邮箱',
                              content='这是公司内部站内邮箱。用于内部人员之间收发通知与消息，'
                                      '欢迎使用 OA 办公系统。')

        # --- 20. FMS · 账套设置（一期单账套，固定一条 code='default'）---
        # 口径（用户确认）：不代收客户保费；代理人佣金由第三方代发，佣金外费用公对公结算；
        # 凭证字别用 收/付/转 三类，各自独立连续编号。
        get_or_create(FinConfig, code='default',
                      defaults=dict(company_name='广东海盈通保智能科技有限公司',
                                    standard='small', taxpayer='general',
                                    start_period=date.today().strftime('%Y-%m'),
                                    currency='CNY', voucher_types='收,付,转',
                                    agent_company='海腾保险代理有限公司',
                                    note='公司不代收客户保费；代理人佣金由海腾代发，佣金外费用公对公结算'))

        # --- 21. FMS · 会计科目（预设《小企业会计准则》保险中介常用科目）---
        _seed_fin_accounts()

        # --- 22. FMS · 演示期初余额（借贷平衡；正式建账前可在页面上「清空期初」后重录）---
        _seed_fin_opening()

        # --- 23. FMS · 系统内置报表模板（资产负债表 / 利润表；现金流量表走专用算法）---
        _seed_fin_reports()

        # --- 24. ADM · 行政分类字典（证照 / 合同 / 资产 / 办公用品 / 印章）---
        _seed_adm_categories()

        # --- 25. ADM · 行政台账演示数据（证照 / 商务合同 / 固定资产）---
        _seed_adm_demo()

        # --- 26. ADM · 行政事务演示数据（办公用品 / 印章使用 / 快递收发）---
        _seed_adm_service_demo()

        # --- 27. ADM · 费用台账演示数据（水电 / 物业 / 房租 / 通讯）---
        _seed_adm_expense_demo()

        # --- 28. ADM · 车辆管理演示数据（车辆档案 + 保养 / 维修 / 违章 / 用车）---
        _seed_adm_vehicle_demo()

        # --- 29. CRM · 核心业务演示数据（受文件顶部 SEED_CRM_DEMO 开关控制）---
        if SEED_CRM_DEMO:
            _seed_crm_demo()
        else:
            # 真实使用阶段：不铺演示数据，只保证「税费参数」这条必需配置存在。
            # 团队系数 / 销售费用 / 续期佣金都靠它先扣税再乘系数，缺了就算不出净额。
            get_or_create(CrmTaxConfig, name='默认税费参数', defaults={
                'vat_rate': 6.0, 'surcharge_rate': 12.0, 'status': 'on',
                'note': '增值税 6%（开票税率）＋附加税费 12%（城建7+教育3+地方教育2，按增值税额计）'})
            db.session.flush()
            print('  [种子] CRM 演示数据已关闭（SEED_CRM_DEMO=False），仅保留税费参数')

        db.session.commit()
        print('✅ 数据库初始化/更新完成（可重复执行）。')


if __name__ == '__main__':
    seed()
