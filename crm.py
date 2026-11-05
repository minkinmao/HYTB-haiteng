# crm.py
# 核心业务管理系统（CRM）· 产品合约管理（一期，2026-10-01）
# 三块：① 保司档案（保险公司基础信息主数据） ② 产品合约（总总 / 分分方案台账） ③ 产品资料库
#
# 定位（红线）：产品合约 = 我司内部的「协议备份台账」。
#   签约主体是海腾（我司无保险牌照、无资质），本系统只做登记 / 查阅 / 政策留痕：
#   · 不产生佣金与业绩数据（佣金算钱在 commission.py）
#   · 不作法律归档（行政的「商务合同」另有其表）
#   · 不自动记账（不生成凭证）
#
# 口径（威哥 2026-10-01 拍板）：
#   · FYD 已剔除；原「代理人第一年佣金」统一并入 FYC → 拆为 总总FYC / 分分FYC；
#   · FYC、RYC 一律是「佣金率（%）」，页面按 2 位小数展示；
#   · 首年三个合计由后端自动算（_calc_totals），不手工录入。
import os
import io
import json
import tempfile
import re
import math
import time
import uuid
import urllib.parse
import urllib.request
import http.cookiejar
import openpyxl
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, flash, send_from_directory, send_file, current_app, abort)
from werkzeug.utils import secure_filename
from models import (db, Insurer, InsurerBranch, InsurerContact, InsurerLinkman, InsurerMetric,
                    CrmProductContract, CrmContractTier, CrmContractPlan, CrmContractComponent, CrmProductDoc,
                    CrmContractFile, CrmContractRycStep, CrmRycRatio,
                    CrmPersistRule, CrmPersistRuleItem, User,
                    CrmUniqueRule, CrmManpower, CrmOrgStruct, CrmAgency, CrmTeamCoefficient,
                    CrmTaxConfig, CrmCustomer, CrmPolicyOrder, CrmPremiumFactor, CrmProposal,
                    CrmProposalProduct, CrmFetchConfig,
                    CrmProductCommission, CrmCommCoefConfig, CrmCommCoefBackup,
                    COMM_CHANNELS, COMM_ITEMS, ROUND_RULES, COMM_ROUND_RULE)
from app import login_required, permission_required

bp = Blueprint('crm', __name__)

# 附件白名单与大小上限（与 OA 报销 / EHR 休假 / ADM 台账保持一致口径）
CRM_ATTACH_EXT = {'pdf', 'ofd', 'xml', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp',
                  'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'zip'}
CRM_ATTACH_MAX = 10 * 1024 * 1024  # 单文件 10MB

# ---------- 保司档案字典 ----------
INSURER_TYPES = [('life', '寿险'), ('property', '财险'), ('health', '健康险'),
                 ('pension', '养老险'), ('other', '其他')]
INSURER_STATUS = [('coop', '合作中'), ('paused', '暂停合作'), ('none', '未合作')]
INSURER_SOURCE = [('manual', '手工录入'), ('builtin', '内置库'), ('fetch', '自动抓取'), ('import', '批量导入')]

# ---------- 产品合约字典 ----------
SIGN_LEVELS = [('tt', '总-总'), ('tf', '总-分'), ('ff', '分-分')]
CONTRACT_STATUS = [('draft', '草稿'), ('active', '生效'), ('ended', '终止')]

# ---------- 五改（2026-10-02）合约口径：三个签约级别收敛为两类 ----------
# 威哥 2026-10-02 确认：原来「总-总 / 总-分 / 分-分」三个概念不再各自成入口，
#   统一按两个口径区分——
#     总公司合约协议 = 总-总(tt) + 总-分(tf)   （签约主体含总公司）
#     分公司合约协议 = 分-分(ff)               （分公司为主体）
# sign_level 三值在库里保持不动（历史数据零迁移），只是入口与展示按下面的映射归口。
SIDE_LEVELS = [('hq', '总公司合约协议'), ('br', '分公司合约协议')]
LEVEL_SIDE = {'tt': 'hq', 'tf': 'hq', 'ff': 'br'}
SIDE_LEVEL_CODES = {'hq': ['tt', 'tf'], 'br': ['ff']}


def _side_of(sign_level):
    """签约级别 → 口径代码 hq / br（未知一律按总公司口径，保证老数据不丢）。"""
    return LEVEL_SIDE.get(sign_level or '', 'hq')


def _side_name(sign_level):
    """签约级别 → 口径中文名（总公司合约协议 / 分公司合约协议）。"""
    return dict(SIDE_LEVELS).get(_side_of(sign_level), '总公司合约协议')
PAY_TYPES = [('year', '年缴'), ('month', '月缴')]
# 险种类型（常量：行业标准，极少变）
INSURANCE_TYPES = ['寿险', '重疾险', '医疗险', '意外险', '年金险', '终身寿险',
                   '分红险', '万能险', '投连险', '税优健康险', '养老年金', '护理险', '其他']
# 缴别（常量）：趸交 + 2~30 年交 + 交至 50/55/60/65 岁
PAY_PERIODS = ['趸交'] + ['%d年交' % n for n in range(2, 31)] + \
              ['交至50岁', '交至55岁', '交至60岁', '交至65岁']

# 三十五改（2026-10-29）：保障期限 —— 一款产品可能同时有「保终身」和「保至70岁」两套费率政策，
#   所以**一条合约 = 一个保障期限**，同一产品不同保障期限分别建合约。
#   下面只是常用项，表单用 datalist 呈现，用户可直接手工输入库里没有的新值（如「保至88岁」），
#   新值保存后会自动进入下拉（_coverage_terms 会把库里已用过的值合并进来）。
COVERAGE_TERMS = ['保终身', '保至60岁', '保至65岁', '保至70岁', '保至75岁', '保至80岁',
                  '保至85岁', '保至88岁', '保至90岁', '保至100岁', '保至105岁',
                  '保20年', '保30年', '保至被保人60岁', '保至被保人70岁']

# 四十三改（2026-10-29）：销售渠道 —— 同一保司的不同产品可能走不同渠道销售，
#   渠道不同费用政策口径也不同（网销多为短险/线上专属费率），故在合约上标一个渠道。
#   留空＝未指定（历史数据全部为空，不做迁移）。
SALE_CHANNELS = [('offline', '线下'), ('online', '网销')]

# ---------- 产品资料库字典 ----------
DOC_TYPES = [('guide', '保司微投指南'), ('clause', '产品条款'), ('manual', '产品说明书'),
             ('rate', '费率表'), ('cash', '现价表'), ('rule', '投保规则'),
             ('plan', 'EXCEL版计划书'), ('other', '其他材料')]

# ---------- 二期（2026-10-01）保司画像字典 ----------
# 岗位人员（联络簿）：威哥点名的三个关键岗位 + 常规对接人
CONTACT_ROLES = [('gm', '总经理'), ('mid_head', '中介分管总'),
                 ('channel_head', '中介渠道负责人'), ('contact', '对接人'), ('other', '其他')]
CONTACT_STATUS = [('current', '现任'), ('left', '离任')]
# 风险综合评级（监管口径）
RATING_LEVELS = ['AAA', 'AA', 'A', 'BBB', 'BB', 'B', 'C', 'D']
PREMIUM_UNITS = [('yi', '亿元'), ('wan', '万元')]

# ---------- 二期 缴别明细字典 ----------
# 政策归属：八改起**由它决定费用形式**（不再让两者自由组合，后端强校验）
POLICY_SCOPES = [('in', '协议内'), ('out', '协议外')]
# 费用形式（八改 2026-10-02）：
#   协议内 → 只能是 transfer「协议内结算」（表单里只有一个选项，等于锁定）
#   协议外 → coef 系数 / cash 现金 / other 购物或其他（选「购物或其他」填备注）
PAY_FORMS = [('transfer', '协议内结算'), ('coef', '系数'),
             ('cash', '现金'), ('other', '购物或其他')]
PAY_FORM_IN = ['transfer']                  # 协议内可选费用形式
PAY_FORM_OUT = ['coef', 'cash', 'other']    # 协议外可选费用形式（含历史文字类，仅用于兼容展示）
# 十三改（2026-10-02）方案按「费用形式」分流为两类：
#   · **系数类**（transfer 协议内结算 / coef 系数）——可计算、报表可合并，走「方案块 + 按缴别系数表」
#   · **文字类**（cash 现金 / other 购物或其他）——金额/旅游/实物/权益，纯文字描述、**不参与合计与报表合并**，
#     统一收进合约级「其他现金、实物或权益方案」文本框。
PAY_FORM_COEF = ['transfer', 'coef']        # 系数类（表单方案块可选）
PAY_FORM_COEF_IN = ['transfer']             # 协议内·系数类（只有「协议内结算」）
PAY_FORM_COEF_OUT = ['coef']                # 协议外·系数类（只有「系数」）
PAY_FORM_TEXT = ['cash', 'other']           # 文字类（进「其他现金、实物或权益方案」文本框）
MISC_PLAN_LABEL = '其他现金、实物或权益方案'   # 文字类方案的统一标识
# （兼容保留）六改的「协议外净费渠道」，八改起不再从表单录入，仅用于展示历史数据
PAY_CHANNELS = [('cash', '现金'), ('card', '购物卡')]

# ---------- 二十一改 业务团队系数设置（2026-10-03）----------
# 威哥口径：对齐「保险佣金查询」小程序 —— 系数 × 基准费率，而非职级分层比例。
#   · basic   基本法团队：统一一套 kf/kr/ka（owner_name 为空）
#   · channel 渠道团队：每个渠道负责人各挂一套 kf/kr/ka
# 三类系数互相独立、都是百分比数值（如 70 = 70%）：
#   kf = FYC 系数 / kr = RYC 系数 / ka = 加佣方案系数
# ⚠️ 只作用于 FYC + 方案加佣；RYC 用 kr 单独乘；继续率 / 增员奖不受影响。
TEAM_COEF_TYPES = [('basic', '基本法团队'), ('channel', '渠道团队')]
COEF_FIELDS = [('kf', 'FYC 系数', '基础协议费用'), ('kr', 'RYC 系数', '续年度佣金'),
               ('ka', '加佣方案系数', '变动费用（方案加佣）')]

# ---------- 十九改 业务团队人力数据清单（2026-10-02）----------
# 威哥口径：费用政策要先有「发给谁」的底表 —— 人群按**团队类型**分两大类：
#   · basic   基本法团队：统一一套**标准化**费用政策（不分层，全员同口径）
#   · channel 渠道团队：可**分层级**（渠道总 → 团队长 → 营销员）下发不同费用政策
TEAM_TYPES = [('basic', '基本法团队'), ('channel', '渠道团队')]
# ⭐ 职级**按团队类型分两套**（十九改++ 2026-10-02 威哥口径）：
#   · 基本法团队 → 部经理 / 主管 / 营销员
#   · 渠道团队   → 渠道总 / 团队长 / 营销员
# 两套只有「营销员」同名，**表单按团队类型联动**，保存时后端再按 CROSS_RANK 兜一层
MP_RANKS_BASIC = [('dept_manager', '部经理'), ('supervisor', '主管'), ('agent', '营销员')]
MP_RANKS_CHANNEL = [('channel_boss', '渠道总'), ('team_leader', '团队长'), ('agent', '营销员')]
RANKS_OF_TYPE = {'basic': MP_RANKS_BASIC, 'channel': MP_RANKS_CHANNEL}
# 并集（列表筛选 / 导出名称翻译 / 导入反查用）；同名只留一次
MP_RANKS = MP_RANKS_BASIC + [x for x in MP_RANKS_CHANNEL if x not in MP_RANKS_BASIC]
# 跨类型对照：职级与团队类型对不上时按这张表折算（如基本法团队存了「渠道总」→ 部经理）
CROSS_RANK = {'channel_boss': 'dept_manager', 'dept_manager': 'channel_boss',
              'team_leader': 'supervisor', 'supervisor': 'team_leader'}
# ⭐ 销售**资质**等级（十九改+ 2026-10-02 威哥口径，监管「持证 + 分级」上岗制度）：
#   一级最高（专家级，可售 P1-P5 全品类）→ 四级入门（新人，仅可售 P1-P2）。
#   铁律：**销售资质等级不得低于所售产品等级**；组合产品按**最高等级**产品定资质。
SALE_LEVELS = ['一级', '二级', '三级', '四级']
# 改名前的旧值（A/B/C/D 级）：导入历史导出文件时自动折算，不用人工改表
OLD_SALE_LEVEL_OF = {'A级': '一级', 'B级': '二级', 'C级': '三级', 'D级': '四级'}
SALE_GRADE_SCOPE = {
    '一级': '专家级 · 可售 P1-P5 全品类（含投连险、变额年金等 P5 极高风险产品）',
    '二级': '资深级 · 可售 P1-P4（不可售 P5）',
    '三级': '专业级 · 可售 P1-P3（不可售 P4 / P5）',
    '四级': '入门级 · 仅可售 P1-P2（不可售 P3 及以上）',
}
MP_GENDERS = [('male', '男'), ('female', '女')]
MP_YESNO = [('yes', '是'), ('no', '否')]
# 开户行（十九改+）：国内主流银行清单，单选下拉，不再手工输入
BANKS = ['中国工商银行', '中国农业银行', '中国银行', '中国建设银行', '交通银行',
         '中国邮政储蓄银行', '招商银行', '中信银行', '中国光大银行', '华夏银行',
         '中国民生银行', '广发银行', '兴业银行', '平安银行', '上海浦东发展银行',
         '恒丰银行', '浙商银行', '渤海银行', '北京银行', '上海银行', '江苏银行',
         '宁波银行', '南京银行', '杭州银行', '徽商银行', '中原银行', '成都银行',
         '长沙银行', '青岛银行', '重庆银行', '广州银行', '东莞银行', '广东华兴银行',
         '广州农商银行', '东莞农商银行', '顺德农商银行', '深圳农商银行',
         '农村信用社', '汇丰银行', '花旗银行', '渣打银行', '星展银行']
# 所属经代公司（十九改+）：下拉选择、不让手填。
# 十九改++：**取值来自「合作经代」基础信息表 CrmAgency**，内置清单只作兜底（表空时也能选）
MP_AGENCIES = ['广东海盈保险代理有限公司', '广州海盈保险代理有限公司',
               '广州海腾保险代理有限公司', '广东海腾保险代理有限公司']
# 十九改+：非强制电子版附件（键 = 模型列名，落 static/uploads/crm/manpower/）
#   sign_file    渠道协议电子版（选「已签」时才要求）
#   idcard_file  身份证电子版**正面** ／ idcard_file2 **反面**（十九改++：正反面各一张）
#   bank_file    银行卡电子版
MP_ATTACH = ['sign_file', 'idcard_file', 'idcard_file2', 'bank_file']
MP_ATTACH_LABEL = {'sign_file': '渠道协议电子版',
                   'idcard_file': '身份证电子版 · 正面',
                   'idcard_file2': '身份证电子版 · 反面',
                   'bank_file': '银行卡电子版'}
# 导入 / 导出表头（顺序即列顺序，改这里两边同步）
# ⚠️ 电子版附件（协议 / 身份证 / 银行卡）是文件，不进 Excel；导出后手工补传
# 十九改++：删掉「职级编码」（无用）与「所属团队」（＝所挂营业组，多余，看所属架构即可）
MP_HEADERS = ['系统工号', '省公司', '分支', '代理人工号', '所属经代公司', '姓名', '职级',
              '销售资质等级', '入司时间', '离职时间', '推荐人姓名', '推荐人工号', '推荐人职级',
              '身份证号码', '性别', '出生年月', '团队类型', '所属架构',
              '开户行', '开户支行', '银行卡号', '是否签署渠道协议', '签署日期', '备注']

# ---------- 业绩管理 · 投保订单录入（2026-10）----------
# 缴费方式（威哥口径：趸交=1 年缴）
PAY_METHODS = [('year', '年缴'), ('month', '月缴'), ('lump', '趸交')]
# 缴费年期（实际缴费年限，年）：独立录入，按威哥示例精确折标行业标保
PAY_YEARS_OPTS = [(1, '1年(趸交)'), (3, '3年'), (5, '5年'), (10, '10年'),
                  (15, '15年'), (20, '20年'), (30, '30年')]
# 保险年期标志
PERIOD_FLAGS = [('blank', '空白'), ('year', '年'), ('age', '岁')]
# 保险年期
PERIODS = [('lifetime', '终身'), ('1year', '1年')]
# 保单主状态 / 子状态 / 险种状态（自由登记，给常用候选项）
POLICY_MAIN_STATUS = [('有效', '有效'), ('失效', '失效'), ('终止', '终止'), ('撤单', '撤单')]
POLICY_SUB_STATUS = [('正常', '正常'), ('冻结', '冻结'), ('理赔中', '理赔中'), ('犹豫期', '犹豫期')]
POLICY_RISK_STATUS = [('正常', '正常'), ('失效', '失效'), ('终止', '终止')]
# 出单类型
ORDER_TYPES = [('online', '线上单'), ('offline', '线下单')]
# 保单类型（威哥定义）
POLICY_KINDS = [('in_service', '在职单'), ('orphan', '孤儿单'), ('inherit', '继承单'), ('recursive', '递归单')]
# 长短险标识
LS_FLAGS = [('short', '短期险'), ('long', '长期险')]
# 双录类型
DUAL_TYPES = [('reg', '保司监管双录'), ('company', '公司内部双录'), ('none', '不需要'), ('blank', '空白')]
DUAL_STATUS = [('pass', '通过'), ('fail', '未通过')]
# 自互保件标识
SELF_FLAGS = [('self', '自保件'), ('mutual', '互保件')]
# 8 类后台附件（键 = 模型列名，落 static/uploads/crm/policy/）
POLICY_ATTACH = ['app_idcard_front', 'app_idcard_back', 'app_bank_front', 'app_bank_back',
                 'ins_idcard_front', 'ins_idcard_back', 'policy_contract_file', 'decl_file']
POLICY_ATTACH_LABEL = {
    'app_idcard_front': '投保人身份证 · 正面',
    'app_idcard_back': '投保人身份证 · 反面',
    'app_bank_front': '投保人银行卡 · 正面',
    'app_bank_back': '投保人银行卡 · 反面',
    'ins_idcard_front': '被保人身份证 · 正面',
    'ins_idcard_back': '被保人身份证 · 反面',
    'policy_contract_file': '保单电子合同',
    'decl_file': '投保声明（纸质电子版 / 视频）',
}
# 强制正反面（上传时前端提示必须正 + 反都传）
POLICY_ATTACH_REQUIRED_PAIR = [('app_idcard_front', 'app_idcard_back'),
                               ('ins_idcard_front', 'ins_idcard_back')]
# 导出 / 导入表头（顺序即列顺序）；附件不进 Excel
POLICY_HEADERS = [
    '出单平台', '代理人工号', '投保单号', '保单号',
    '投保人姓名', '投保人身份证号', '投保人手机号', '投保人省份', '投保人地市', '投保人县区', '投保人街道',
    '投保人开户行', '投保人银行账号',
    '被保人姓名', '被保人身份证号', '被保人手机号', '被保人省份', '被保人地市', '被保人县区', '被保人街道',
    '保单主状态', '保单子状态', '险种状态', '保险公司名称', '产品名称',
    '投保计划', '保额', '基本保费', '额外保费', '追加保费', '职业健康加费', '规模保费',
    '佣金系数(%)', '缴费方式', '保险年期标志', '保险年期',
    '投保日期', '承保日期', '保单生效日期', '回访成功日期', '撤单日期', '撤单原因',
    '出单类型', '是否自动垫交', '保单类型', '双录单标识', '双录类型', '双录状态', '税优产品标识',
    '备注', '缴费年期',
]


def _pay_years(pay_method, ins_period=None, pay_years=None):
    """缴费年期（数值，威哥口径）：优先用表单录入的「实际缴费年限」，否则按缴费方式+保险年期兜底推算。

    · 表单录入 pay_years（1/3/5/10/…）→ 直接采用（封顶 99，折标率处再 min(×10,100)）
    · 未录入：趸交=1；保险年期=1年 → 1；年缴/月缴 + 终身（或未知）→ 10（长期封顶）
    用于行业标保折标率、受理件数、长短险标识的统一推算。
    """
    if pay_years:
        try:
            v = int(pay_years)
            if v >= 1:
                return min(v, 99)
        except (ValueError, TypeError):
            pass
    if pay_method == 'lump':
        return 1
    if (ins_period or '') == '1year':
        return 1
    return 10


def _industry_std_rate(pay_years):
    """行业标保折标率%（威哥口径）：min(缴费年期×10, 100)。

    趸交(1年)=10% · 3年=30% · 5年=50% · 10年=100% · 10年及以上封顶100%。
    """
    return round(min(pay_years * 10, 100), 2)


def _accept_count_of(pay_years):
    """受理件数：缴费年期>1 → 1，否则 0。"""
    return 1 if pay_years > 1 else 0


def _ls_flag_of(pay_years):
    """长短险标识：缴费年期==1 → 短期险，>1 → 长期险。"""
    return 'short' if pay_years == 1 else 'long'


def _customer_code(idcard):
    """按身份证号生成唯一客户编码（确定性 hash，历史复用）。"""
    import hashlib
    s = re.sub(r'\s', '', idcard or '').upper()
    return 'KH' + hashlib.md5(s.encode('utf-8')).hexdigest()[:10].upper()


def _upsert_customer(idcard, **kw):
    """按身份证号 upsert 客户主表，返回 (CrmCustomer, is_new)。"""
    idc = re.sub(r'\s', '', idcard or '')
    if not idc:
        return None, False
    row = CrmCustomer.query.filter_by(idcard=idc).first()
    if row is None:
        row = CrmCustomer(idcard=idc, customer_code=_customer_code(idc))
        db.session.add(row)
    for k, v in kw.items():
        if v not in (None, ''):
            setattr(row, k, v)
    # 性别/出生年月从身份证补
    gd, bt = _idcard_info(idc)
    if not row.gender and gd:
        row.gender = gd
    if not row.birth_date and bt:
        row.birth_date = bt
    if not row.age and bt:
        row.age = _calc_age(bt)
    return row, row.id is None


def _calc_age(birth):
    """出生年月 YYYY-MM-DD → 周岁年龄（取不到返回 None）。"""
    if not birth:
        return None
    try:
        b = datetime.strptime(birth, '%Y-%m-%d')
        today = datetime.now()
        return today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    except (ValueError, TypeError):
        return None


def _norm_no(s):
    """工号归一化：去空格、全角转半角、转大写（容错匹配，兼容复制粘贴/大小写差异）。"""
    s = (s or '').strip().upper()
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:          # 全角 ASCII → 半角
            ch = chr(o - 0xFEE0)
        out.append(ch)
    return ''.join(out)


def _agent_by_no(agent_no):
    """代理人工号 → 自动带出架构信息（姓名/省公司/分支/经代公司/营业组）。
    先按「代理人工号」再按「系统工号」精确匹配；仍失败则按归一化（大小写/全角）容错匹配。
    """
    no = (agent_no or '').strip()
    if not no:
        return None
    m = CrmManpower.query.filter_by(agent_no=no).first()
    if m is None:
        m = CrmManpower.query.filter_by(sys_no=no).first()
    if m is None:
        key = _norm_no(no)
        for mm in CrmManpower.query.all():
            if _norm_no(mm.agent_no) == key or _norm_no(mm.sys_no) == key:
                m = mm
                break
    if m is None:
        return None
    return {
        'agent_name': m.name or '',
        'agent_province': m.province or '',
        'agent_branch': m.branch or '',
        'agent_agency': m.agency or '',
        'agent_org': m.org_path or m.team_name or '',
        'sys_no': m.sys_no or '',
    }


def _product_info(insurer_name, product_name):
    """保险公司+产品 → 带出 产品编码 / 险种类别 / 该产品的 FYC（用于佣金系数预填）。"""
    ins = (insurer_name or '').strip()
    pnm = (product_name or '').strip()
    if not ins or not pnm:
        return None
    c = CrmProductContract.query.filter_by(
        insurer_name=ins, product_name=pnm).first()
    if c is None:
        # 容错：只按产品名匹配（保险公司填法略有差异时也能带出）
        c = CrmProductContract.query.filter_by(product_name=pnm).first()
    if c is None:
        return None
    fyc = 0
    for t in _contract_tiers(c.id):
        fyc = round((t.tt_fyc or 0) + (t.ff_fyc or 0), 2)
        if fyc:
            break
    return {'product_code': c.product_code or '', 'insurance_type': c.insurance_type or '',
            'fyc': fyc}


def _is_company_agent(idcard):
    """该身份证号是否对应公司入职代理人（在人力清单里）。"""
    idc = re.sub(r'\s', '', idcard or '')
    if not idc:
        return False
    return CrmManpower.query.filter_by(idcard=idc).first() is not None


def _self_policy_flag_of(app_idcard, ins_idcard):
    """自互保件判定：被保人是公司代理人→自保件；投保人+被保人都→互保件；否则空。"""
    ins_is = _is_company_agent(ins_idcard)
    if not ins_is:
        return ''
    app_is = _is_company_agent(app_idcard)
    return 'mutual' if app_is else 'self'


def _round2(v):
    """金额四舍五入 2 位（ROUND_HALF_UP，规避 Python 银行家舍入）。"""
    return float(Decimal(str(v or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


# ---------- 二期 继续率考核规则字典 ----------
RULE_STATUS = [('on', '启用'), ('off', '停用')]
# 继续率考核标准：八改（2026-10-02）威哥确认，固定四种——
#   13 / 25 / 37 / 49 个月继续率考核标准（行业标准档位，每 12 个月一档）
PERSIST_CYCLES = ['13个月继续率考核标准', '25个月继续率考核标准',
                  '37个月继续率考核标准', '49个月继续率考核标准']
# 奖励项目：继续率奖金系数 / 长期服务奖系数
#   · 继续率奖金系数 → 需再**多选**继续率考核标准，档位按标准分块（下限/上限/奖金系数）
#   · 长期服务奖系数 → 只跟下限/上限有关，与 13/25 这些月份**无关**，单独一块
AWARD_ITEMS = [('bonus', '继续率奖金系数'), ('long_service', '长期服务奖系数')]
# 档位归属哨兵值：cycle 列 = 本值时表示「长期服务奖系数档位」（与月份无关）
LS_CYCLE = 'long_service'

# 九改（2026-10-02）：档位边界运算符 —— 让「下限 / 上限」看得出**是否含等号**。
#   威哥口径举例：13J＜88 → 系数 0；88%≤13J＜90 → 系数 2.5%
#   只填 88 / 90 看不出是 ≤ 还是 ＜，所以每行带上运算符。
#   默认即行业惯用的「左闭右开」[下限, 上限)：下限 ≥、上限 ＜。
RANGE_OPS_MIN = [('>=', '≥'), ('>', '＞')]
RANGE_OPS_MAX = [('<', '＜'), ('<=', '≤')]
# 十四改（2026-10-02）档位区间改用「值 符号 指标 符号 值」的读法，例如 80 ≤ 指标 < 90：
#   · 左侧（下限）**符号反向书写**：下限 ≥ 80 读作「80 ≤ 指标」→ 下拉字形用 ≤ / <
#   · 右侧（上限）符号原样在「指标」之后：指标 < 90 → 下拉字形用 < / ≤
#   ⚠️ 只是**展示层反向**，存储值仍是 min_op / max_op，零迁移。
RANGE_OPS_LEFT = [('>=', '≤'), ('>', '<')]
RANGE_OPS_RIGHT = [('<', '<'), ('<=', '≤')]
RANGE_OP_REV = {'>=': '≤', '>': '<', '<': '>', '<=': '≥'}
RANGE_OP_SYM = {'>=': '≥', '>': '>', '<': '<', '<=': '≤'}

# 十五改（2026-10-02）：继续率算法口径（caliber）与精度规则（round_mode）整块删除。
#   原「各家不同、需按合约登记」的口径/精度选择取消，系统统一为：
#   继续率精度 = 四舍五入，保留 1 位小数（round_persist）。
PERSIST_PRECISION = 1
def round_persist(v):
    """继续率精度统一规则：四舍五入，保留 1 位小数（ROUND_HALF_UP）。

    用于继续率区间阈值等继续率数值的展示；非继续率类（FYC/RYC 等）仍按各自口径不变。
    """
    try:
        return float(Decimal(str(v)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError, Exception):
        return 0.0

# ③ 倒扣（不达标退回）扣费基数：幸福人寿按「全部推广费」线性倒扣（知识库 §3.5）
CLAWBACK_BASES = [('all', '全部推广费（首期佣金 + 推动费 + 其他活动费用）'),
                  ('fyc', '首年佣金（FYC）'),
                  ('ryc', '续期佣金（RYC）'),
                  ('persist', '继续率奖金'),
                  ('other', '其他（见倒扣标准说明）')]

# ④ 变动费用·费项类型（对齐真实协议 7 类费项中的变动部分，知识库 §二）
#    基础协议费用（FYC/RYC/继续率奖金）在 CrmContractTier，不在此列。
PLAN_FEE_TYPES = [('service', '续期服务津贴'),
                  ('quality', '业务品质奖金'),
                  ('monthly', '月度销售奖金（仅首年）'),
                  ('stage', '阶段补充佣金 / 推动费'),
                  ('other', '其他')]

# 八改+：短名映射（标签用，避免长文案撑爆界面）
CBASE_SHORT = {'all': '全部推广费', 'fyc': '首年佣金', 'ryc': '续期佣金',
               'persist': '继续率奖金', 'other': '其他'}

DNAME = dict(SIGN_LEVELS + CONTRACT_STATUS + PAY_TYPES + INSURER_TYPES +
             INSURER_STATUS + INSURER_SOURCE + DOC_TYPES +
             CONTACT_ROLES + CONTACT_STATUS + PREMIUM_UNITS +
             POLICY_SCOPES + PAY_FORMS + PAY_CHANNELS + RULE_STATUS +
             AWARD_ITEMS + SIDE_LEVELS)
# 注意：**不要**把 PLAN_FEE_TYPES 并进 DNAME —— 它与 PAY_FORMS 的键 'other' 撞名
# （费项类型 'other'→'其他' 会盖掉费用形式 'other'→'购物或其他'）。
# 费项类型一律用 fee_types / fee_short 渲染（表单 & 详情已如此）。


# ===================== 内置保司库 =====================
# 说明：只预置「公知且稳定」的信息（全称 / 简称 / 公司类型 / 官网）。
#   统一社会信用代码、保险许可证号、税号、法人、联系电话等属动态工商数据，
#   需核实后补录，或接入数据源后由「抓取更新」自动填充（接口已预留）。
#   数据来源标注为 builtin（内置库）。
BUILTIN_INSURERS = [
    # —— 寿险 ——
    {'name': '中国人寿保险股份有限公司', 'short_name': '中国人寿', 'insurer_type': 'life', 'website': 'www.e-chinalife.com'},
    {'name': '中国平安人寿保险股份有限公司', 'short_name': '平安人寿', 'insurer_type': 'life', 'website': 'www.pingan.com'},
    {'name': '中国太平洋人寿保险股份有限公司', 'short_name': '太平洋寿险', 'insurer_type': 'life', 'website': 'www.cpic.com.cn'},
    {'name': '新华人寿保险股份有限公司', 'short_name': '新华保险', 'insurer_type': 'life', 'website': 'www.newchinalife.com'},
    {'name': '泰康人寿保险有限责任公司', 'short_name': '泰康人寿', 'insurer_type': 'life', 'website': 'www.taikanglife.com'},
    {'name': '中国人民人寿保险股份有限公司', 'short_name': '人保寿险', 'insurer_type': 'life', 'website': 'www.picclife.com'},
    {'name': '太平人寿保险有限公司', 'short_name': '太平人寿', 'insurer_type': 'life', 'website': 'www.tplife.com'},
    {'name': '富德生命人寿保险股份有限公司', 'short_name': '富德生命人寿', 'insurer_type': 'life', 'website': 'www.sino-life.com'},
    {'name': '阳光人寿保险股份有限公司', 'short_name': '阳光人寿', 'insurer_type': 'life', 'website': 'www.sinosig.com'},
    {'name': '友邦人寿保险有限公司', 'short_name': '友邦人寿', 'insurer_type': 'life', 'website': 'www.aia.com.cn'},
    {'name': '中宏人寿保险有限公司', 'short_name': '中宏人寿', 'insurer_type': 'life', 'website': 'www.manulife-sinochem.com'},
    {'name': '中意人寿保险有限公司', 'short_name': '中意人寿', 'insurer_type': 'life', 'website': 'www.generalichina.com'},
    {'name': '中信保诚人寿保险有限公司', 'short_name': '中信保诚', 'insurer_type': 'life', 'website': 'www.citic-prudential.com.cn'},
    {'name': '中美联泰大都会人寿保险有限公司', 'short_name': '大都会人寿', 'insurer_type': 'life', 'website': 'www.metlife.com.cn'},
    {'name': '工银安盛人寿保险有限公司', 'short_name': '工银安盛', 'insurer_type': 'life', 'website': 'www.icbc-axa.com'},
    {'name': '建信人寿保险股份有限公司', 'short_name': '建信人寿', 'insurer_type': 'life', 'website': 'www.ccb-life.com.cn'},
    {'name': '招商信诺人寿保险有限公司', 'short_name': '招商信诺', 'insurer_type': 'life', 'website': 'www.cignacmb.com'},
    {'name': '中英人寿保险有限公司', 'short_name': '中英人寿', 'insurer_type': 'life', 'website': 'www.aviva-cofco.com.cn'},
    {'name': '同方全球人寿保险有限公司', 'short_name': '同方全球人寿', 'insurer_type': 'life', 'website': 'www.aegonthtf.com'},
    {'name': '复星保德信人寿保险有限公司', 'short_name': '复星保德信', 'insurer_type': 'life', 'website': 'www.prudential-fosun.com'},
    {'name': '弘康人寿保险股份有限公司', 'short_name': '弘康人寿', 'insurer_type': 'life', 'website': 'www.hongkang-life.com'},
    {'name': '百年人寿保险股份有限公司', 'short_name': '百年人寿', 'insurer_type': 'life', 'website': 'www.aeonlife.com.cn'},
    {'name': '财信吉祥人寿保险股份有限公司', 'short_name': '财信吉祥人寿', 'insurer_type': 'life', 'website': 'www.jxlife.com.cn'},
    {'name': '长城人寿保险股份有限公司', 'short_name': '长城人寿', 'insurer_type': 'life', 'website': 'www.greatlife.cn'},
    {'name': '信泰人寿保险股份有限公司', 'short_name': '信泰人寿', 'insurer_type': 'life', 'website': 'www.xintai.com'},
    {'name': '国华人寿保险股份有限公司', 'short_name': '国华人寿', 'insurer_type': 'life', 'website': 'www.95549.cn'},
    {'name': '民生人寿保险股份有限公司', 'short_name': '民生保险', 'insurer_type': 'life', 'website': 'www.minshenglife.com'},
    {'name': '合众人寿保险股份有限公司', 'short_name': '合众人寿', 'insurer_type': 'life', 'website': 'www.unionlife.com.cn'},
    {'name': '华泰人寿保险股份有限公司', 'short_name': '华泰人寿', 'insurer_type': 'life', 'website': 'www.ehuatai.com'},
    {'name': '幸福人寿保险股份有限公司', 'short_name': '幸福人寿', 'insurer_type': 'life', 'website': 'www.happyinsurance.com.cn'},
    # —— 财险 ——
    {'name': '中国人民财产保险股份有限公司', 'short_name': '人保财险', 'insurer_type': 'property', 'website': 'www.picc.com'},
    {'name': '中国平安财产保险股份有限公司', 'short_name': '平安产险', 'insurer_type': 'property', 'website': 'www.pingan.com'},
    {'name': '中国太平洋财产保险股份有限公司', 'short_name': '太平洋产险', 'insurer_type': 'property', 'website': 'www.cpic.com.cn'},
    {'name': '中国大地财产保险股份有限公司', 'short_name': '大地保险', 'insurer_type': 'property', 'website': 'www.ccic-net.com.cn'},
    {'name': '中华联合财产保险股份有限公司', 'short_name': '中华财险', 'insurer_type': 'property', 'website': 'www.cic.cn'},
    {'name': '阳光财产保险股份有限公司', 'short_name': '阳光产险', 'insurer_type': 'property', 'website': 'www.sinosig.com'},
    {'name': '太平财产保险有限公司', 'short_name': '太平财险', 'insurer_type': 'property', 'website': 'www.cntaiping.com'},
    {'name': '众安在线财产保险股份有限公司', 'short_name': '众安保险', 'insurer_type': 'property', 'website': 'www.zhongan.com'},
    {'name': '永安财产保险股份有限公司', 'short_name': '永安保险', 'insurer_type': 'property', 'website': 'www.yaic.com.cn'},
    {'name': '紫金财产保险股份有限公司', 'short_name': '紫金保险', 'insurer_type': 'property', 'website': 'www.zking.com'},
]
BUILTIN_BY_NAME = {b['name']: b for b in BUILTIN_INSURERS}


# ===================== 通用工具 =====================

def _can(code):
    """当前登录用户是否拥有某权限码。"""
    uid = session.get('user_id')
    user = User.query.get(uid) if uid else None
    return bool(user and user.level and any(p.code == code for p in user.level.permissions))


def _f(name):
    """取字段并去空白：优先 POST 表单，其次 URL 查询串（兼容 GET 自动带出接口）。"""
    return (request.form.get(name) or request.args.get(name) or '').strip()


def _to_num(s, default=0.0):
    """表单数值 → float（容错：空 / 非法 → default）。佣金率与金额共用。"""
    try:
        return round(float((s or '').replace(',', '').strip()), 2)
    except (ValueError, AttributeError):
        return default


def _next_code(model, prefix):
    """生成单号：前缀-YYYYMM-NNN（按当月已有条数递增）。"""
    head = '%s-%s-' % (prefix, datetime.now().strftime('%Y%m'))
    mx = 0
    for r in model.query.filter(model.code.like(head + '%')).all():
        try:
            mx = max(mx, int((r.code or '').rsplit('-', 1)[-1]))
        except (ValueError, IndexError):
            pass
    return '%s%03d' % (head, mx + 1)


def _name_of(pairs, key):
    """字典 code → 中文名（找不到时原样返回）。"""
    return dict(pairs).get(key or '', key or '')


def _calc_tier_totals(t):
    """缴别基础合约：只算 FYC 合计（% 系数）。

    方案政策已拆到 CrmContractPlan（可多条），缴别级不再有方案合计 / 总合计，
    故这里只维护 fyc_total = 总总FYC + 分分FYC（威哥 2026-10-01 三改口径）。
    """
    t.fyc_total = round((t.tt_fyc or 0) + (t.ff_fyc or 0), 2)


def _calc_plan_total(p):
    """方案政策：算方案合计（% 系数），并按「政策归属」强校验费用形式（八改）。

    plan_total = 总总方案系数 + 分分方案系数；奖金金额（元）不参与合计，仅展示。
    归属 ↔ 费用形式（前端已做联动，这里再做一次后端强校验，防手工改参数）：
      · 协议内 → 费用形式只能是 transfer 协议内结算，其他字段一律清空；
      · 协议外 → 费用形式只能是 coef 系数 / cash 现金 / other 购物或其他
                 （非法值兜底成 coef；备注仅在 other 时有意义）。
    """
    p.plan_total = round((p.tt_coef or 0) + (p.ff_coef or 0), 2)
    if (p.policy_scope or 'in') == 'in':
        p.pay_form = 'transfer'
        p.pay_channel = ''
        p.other_remark = ''
    else:
        if (p.pay_form or '') not in PAY_FORM_OUT:
            p.pay_form = 'coef'
        p.pay_channel = ''            # 八改起不再用渠道二级下拉
        if p.pay_form != 'other':
            p.other_remark = ''


def _contract_tiers(cid):
    """取某合约的全部缴别基础合约（按排序号）。"""
    return CrmContractTier.query.filter_by(contract_id=cid) \
        .order_by(CrmContractTier.sort_no, CrmContractTier.id).all()


def _contract_plans(tid):
    """取某缴别基础合约的全部方案政策（按排序号）。"""
    return CrmContractPlan.query.filter_by(tier_id=tid) \
        .order_by(CrmContractPlan.sort_no, CrmContractPlan.id).all()


def _tier_stats(c):
    """合约概况：(缴别数, 方案政策数, 协议外方案数)。

    一期存量合约（尚无明细）回退主表旧列，保证列表页不出现空白。
    """
    ts = _contract_tiers(c.id)
    if not ts:
        return (1 if (c.grand_total or 0) else 0), 0, 0
    # 十五改：方案已全部提到合约级（文字类方案不挂缴别），故按 contract_id 取，
    # 不能再按 tier_id 取（那样会漏掉文字类方案）。
    plans = CrmContractPlan.query.filter_by(contract_id=c.id).all()
    # 十二改：方案已提到合约级 —— 列表里「N 方案」按**去重后的方案条数**算，
    # 不能再用行数（一条方案挂多个缴别会变成多行，数字会虚高）。
    # 十五改：文字类方案（现金 / 购物或其他）现在也是**方案**，一并计入条数。
    heads = {_head_key(p) for p in plans}
    return (len(ts),
            len(heads),
            len({k for k in heads if k[2] == 'out'}))


def _plan_heads(form):
    """解析**合约级方案清单**（十二改；十五改扩展为两类方案）。

    方案头（一份，全缴别共用）：
        ph_name_<k> / ph_fee_<k> / ph_scope_<k> / ph_form_<k> /
        ph_remark_<k> / ph_tt_other_<k> / ph_ff_other_<k> / ph_note_<k> / ph_text_<k>

    十五改（2026-10-02）：方案按「费用形式」分两类 ——
      · **系数类**（transfer 协议内结算 / coef 系数）→ 走系数表，按缴别落多行；
      · **文字类**（cash 现金 / other 购物或其他）→ 走 `ph_text_<k>` 文字说明，
        **不按缴别展开**（合约下只落一行），也不参与系数合计与报表合并。
      文字类方案**必须有名称**（与系数类方案并列在「四、」节里，名称是唯一标识）；
      名称留空时兜底为「其他现金、实物或权益方案」。
    返回按表单顺序排列的 dict 列表。
    """
    heads = []
    for k in form.getlist('ph_idx'):
        scope = (form.get('ph_scope_%s' % k) or 'in').strip()
        if scope not in dict(POLICY_SCOPES):
            scope = 'in'
        # 八改：政策归属**决定**费用形式——这里再做一次后端强校验，不信前端。
        pf = (form.get('ph_form_%s' % k) or '').strip()
        if scope == 'in':
            pf = 'transfer'                      # 协议内只有「协议内结算」
        elif pf not in PAY_FORM_OUT:
            pf = 'coef'                          # 协议外乱值兜底为「系数」
        is_text = pf in PAY_FORM_TEXT            # 十五改：现金 / 购物或其他 = 文字类方案
        if is_text:
            scope = 'out'                        # 文字类只可能出现在协议外
        name = (form.get('ph_name_%s' % k) or '').strip()
        if not name:
            if not is_text:
                continue                         # 系数类没名称就无从对应，按空行跳过
            name = MISC_PLAN_LABEL               # 文字类兜底名称
        fee = (form.get('ph_fee_%s' % k) or 'stage').strip()
        if fee not in dict(PLAN_FEE_TYPES):
            fee = 'stage'
        heads.append(dict(
            k=k, name=name, fee=fee, scope=scope, form=pf, text=is_text,
            remark=(form.get('ph_remark_%s' % k) or '').strip(),
            tt_other=(form.get('ph_tt_other_%s' % k) or '').strip(),
            ff_other=(form.get('ph_ff_other_%s' % k) or '').strip(),
            note=(form.get('ph_note_%s' % k) or '').strip(),
            plan_text=(form.get('ph_text_%s' % k) or '').strip()))
    return heads


def _contract_text_plans(cid):
    """取某合约的**文字类方案**（十五改）。

    文字类方案不挂缴别（`tier_id` 为空、合约下只落一行），所以不能走 `_contract_plans(tid)`，
    必须按 `contract_id` 查。用于编辑页回填、详情页展示、导出。
    """
    return CrmContractPlan.query.filter_by(contract_id=cid, tier_id=None) \
        .order_by(CrmContractPlan.sort_no, CrmContractPlan.id).all()


def _head_key(p):
    """方案头去重键：名称 + 费项类型 + 政策归属 + 费用形式（十二改）。"""
    return ((p.plan_name or '').strip(), p.fee_type or 'stage',
            p.policy_scope or 'in', p.pay_form or 'transfer')


def _legacy_misc_text(plans, pre):
    """把历史「文字类方案」（现金 / 购物或其他）拼成一段可编辑文字（十三改）。

    十三改起表单不再按缴别收这类方案，改为合约级一个文本框。为了让老数据不丢，
    编辑页/详情页在此把历史行**文字化**展示；用户保存后即落进 `misc_plan_text`。
    """
    lines, seen = [], set()
    for p in plans:
        if (p.pay_form or '') not in PAY_FORM_TEXT:
            continue
        nm = (p.plan_name or '').strip() or '（未命名方案）'
        if nm in seen:
            continue
        seen.add(nm)
        bits = []
        c = getattr(p, pre + '_coef', 0) or 0
        a = getattr(p, pre + '_amount', 0) or 0
        if c:
            bits.append('系数 %.2f%%' % c)
        if a:
            bits.append('金额 {:,.2f} 元'.format(a))
        if p.other_remark:
            bits.append('形式：%s' % p.other_remark)
        if p.note:
            bits.append('备注：%s' % p.note)
        lines.append('【%s】%s' % (nm, '；'.join(bits)))
    return '\n'.join(lines)


def _misc_text_of(c, pre=''):
    """合约的「其他现金、实物或权益方案」文字（十五改）。

    十五改：文字类方案已归并为**方案**（不挂缴别、文字存 `plan_text`）——
    这里按「方案名称：文字」逐条汇总。兜底顺序（保证老数据不丢）：
      ① 新的文字类方案行 → ② 十三改遗留的合约级 `misc_plan_text`
      → ③ 六改/八改时代「按缴别登记的文字类方案」行（`_legacy_misc_text` 文字化）。
    `pre` 只为兼容旧调用签名保留（决定读 tt_/ff_ 侧）。
    """
    lines = []
    for p in _contract_text_plans(c.id):
        txt = (p.plan_text or '').strip()
        if not txt:
            continue
        nm = (p.plan_name or '').strip()
        lines.append('%s：%s' % (nm, txt) if nm else txt)
    if lines:
        return '\n'.join(lines)
    saved = (getattr(c, 'misc_plan_text', '') or '').strip()
    if saved:
        return saved
    ts = _contract_tiers(c.id)
    plans = [p for t in ts for p in _contract_plans(t.id)]
    return _legacy_misc_text(plans, pre or 'tt')


def _components_of(cid):
    """定制类产品·组合组件列表（按 sort_no, id 排序）。"""
    if not cid:
        return []
    return (CrmContractComponent.query.filter_by(contract_id=cid)
            .order_by(CrmContractComponent.sort_no, CrmContractComponent.id).all())


def _comp_map_of(c):
    """该合约可用于展示 / 计算的组件映射 {组件id: 组件对象}。

    四十七改（2026-10-29）：分口径（分-分）**不再自己建组件**，而是沿用总总合约的组件
    （缴别行的 `component_id` 直接指向总总的组件 id）。所以只查本合约的组件是不够的 ——
    详情页 / 导出 / 佣金查询 / 试算器 / 销售费用清单都要能认出「这条缴别属于哪个组件」，
    否则分口径的组件名会整片空白。这里把「本合约组件」＋「缴别实际引用到的组件」并起来。
    """
    if not c or not getattr(c, 'id', None):
        return {}
    out = {x.id: x for x in _components_of(c.id)}
    miss = {t.component_id for t in _contract_tiers(c.id)
            if t.component_id and t.component_id not in out}
    if miss:
        for cp in CrmContractComponent.query.filter(
                CrmContractComponent.id.in_(miss)).all():
            out[cp.id] = cp
    return out


def _hq_components(insurer_id, product_name, exclude_cid=0):
    """总口径（tt 优先、其次 tf）合约的「组合组件」—— 分口径只读沿用。

    返回 (组件列表, 来源合约)。找不到返回 ([], None)。
    ⚠️ 必须排除 `ai_draft=1`（AI 草稿不是正式合约，它的组件不能当依据传下去）。
    """
    if not insurer_id or not (product_name or '').strip():
        return [], None
    nm = _nk(product_name)
    rows = CrmProductContract.query.filter(
        CrmProductContract.sign_level.in_(SIDE_LEVEL_CODES['hq']),
        CrmProductContract.insurer_id == insurer_id,
        db.func.coalesce(CrmProductContract.ai_draft, 0) == 0)
    if exclude_cid:
        rows = rows.filter(CrmProductContract.id != exclude_cid)
    best = None
    for c in rows.all():
        if _nk(c.product_name) != nm:
            continue
        if c.sign_level == 'tt':
            best = c
            break
        if best is None:
            best = c
    if not best:
        return [], None
    return _components_of(best.id), best


def _save_components(c, form):
    """全量替换某合约的「组合组件」（先删后建，口径同 _save_tiers）。

    二十七改2：只有勾选了「定制类产品」(`is_custom=1`) 才登记组件；未勾选则清空组件，
    避免用户取消勾选后残留脏数据。表单字段：
        comp_idx / comp_name_<ix> / comp_code_<ix> / comp_insurer_<ix> /
        comp_type_<ix> / comp_note_<ix>
    组件产品名称为空的行跳过；组件保司为空＝同主保司。

    二十八改（2026-10-29）：改为返回 `{表单 ix: 组件 id}` —— 「三、」节把费率按组件落的时
    候要靠这张映射把表单里写的组件序号翻成真实 component_id（组件必须先存，缴别才能挂上去）。

    三十九改（2026-10-29）：组件产品代码口径 ——
        · 历史没录过：手填优先，留空自动发号（三十八改）；
        · 历史录过：默认沿用历史代码（前端只读）；
        · **历史录过但本次填了不同的代码** ＝ 用户手工改码 → 用新码，并把全库引用同一组件的
          记录一起改（一键同步，见 `_sync_comp_code`），返回同步条数供 flash 提示。
    故返回值改为 `(idmap, synced_cnt)`。
    """
    for old in CrmContractComponent.query.filter_by(contract_id=c.id).all():
        db.session.delete(old)
    db.session.flush()
    if not getattr(c, 'is_custom', 0):
        return {}, 0
    idmap, n, changes = {}, 0, []
    for ix in form.getlist('comp_idx'):
        nm = (form.get('comp_name_%s' % ix) or '').strip()
        if not nm:
            continue
        iid = (form.get('comp_insurer_%s' % ix) or '').strip()
        iid = int(iid) if iid.isdigit() else None
        it = Insurer.query.get(iid) if iid else None
        _civ = iid or c.insurer_id
        _typed = (form.get('comp_code_%s' % ix) or '').strip()
        _have = _comp_code_of(_civ, nm)
        if _typed:
            _code = _typed
            # 与历史代码不同 → 记下来，存完统一同步到所有引用同一组件的记录
            if _have and _pc_norm(_have) != _pc_norm(_typed):
                changes.append((_civ, nm, _typed))
        else:
            _code = _have or _gen_product_code()
        cp = CrmContractComponent(
            contract_id=c.id, sort_no=n, insurer_id=iid,
            insurer_name=(it.name if it else ''),
            product_name=nm,
            product_code=_code,
            insurance_type=(form.get('comp_type_%s' % ix) or '').strip(),
            note=(form.get('comp_note_%s' % ix) or '').strip())
        db.session.add(cp)
        db.session.flush()                 # 先拿到 component_id
        idmap[ix] = cp.id
        n += 1
    synced = 0
    for (_ci, _nm, _new) in changes:
        synced += _sync_comp_code(_ci, _nm, _new)
    return idmap, synced


# ---------- 二十九改（2026-10-29）：续期服务费·继续率分档（续期佣金「特殊通道」） ----------
# 业务来源：达尔文12号《服务费详情》实证——
#   20年交·保终身：次年 0%≤X<80% → 0 / 80%≤X<88% → 30 / 88%≤X≤100% → 50；
#                 第3年 0%≤X<80% → 0 / 80%≤X<93% → 12.6 / 93%≤X≤100% → 18。
# ⚠️ 每个年度的阈值各不相同（次年 80/88，第3年 80/93），所以**不能共用一套区间**，
#    必须「按年度各存一组档位」—— 这也是它与「继续率奖金率」（区间全表共用）的根本区别。
RYC_YEARS = list(range(2, 11))


def _ryc_year_label(n):
    """续期年度中文名：2 → 次年，3 → 第3年 …"""
    n = int(n or 0)
    return '次年' if n == 2 else '第%d年' % n


def _ryc_steps_of(tier_id):
    """某缴别的全部分档行（按年度、继续率下限升序）。"""
    if not tier_id:
        return []
    return (CrmContractRycStep.query.filter_by(tier_id=tier_id)
            .order_by(CrmContractRycStep.ryc_year, CrmContractRycStep.min_rate,
                      CrmContractRycStep.id).all())


def _ryc_steps_map(cid):
    """{tier_id: 矩阵} —— 表单回填 / 详情展示共用。

    三十一改（2026-10-29）：**区间做行 × 年度做列**，威哥「继续率是要有区间的」。
    结构：
        {'years': [2, 3],                  # 本缴别涉及继续率的年度（升序）
         'rows': [{'min': 0,  'max': 80, 'fees': {'2': 0.0,  '3': 0.0}},
                  {'min': 80, 'max': 88, 'fees': {'2': 30.0, '3': 50.0}}],
         'full': {'2': 30.0, '3': 50.0},   # 各年度满档费率（下限最大的那档）
         'cnt': 2, 'n': 4}
    · max 为空 ＝ 无上限（≥min 即该档）。
    · 一个区间行可以同时有多个年度的费率（这正是删掉「年度分块」的原因：同一套区间不必每年度重录一遍）。
    · 兼容二十九改的老数据（只存了下限）：同年度内按 min 升序自动补出上限＝下一档 min。
    """
    out = {}
    if not cid:
        return out
    raw = (CrmContractRycStep.query.filter_by(contract_id=cid)
           .order_by(CrmContractRycStep.ryc_year, CrmContractRycStep.min_rate,
                     CrmContractRycStep.id).all())
    per = {}                                    # tier_id -> {year: [(min, max, fee)]}
    for s in raw:
        per.setdefault(s.tier_id, {}).setdefault(s.ryc_year, []).append(
            (round(s.min_rate or 0, 2),
             None if s.max_rate is None else round(s.max_rate, 2),
             round(s.fee_rate or 0, 2)))
    for tid, ymap in per.items():
        years = sorted(ymap)
        # ① 老数据补齐上限：同年内按 min 升序，max＝下一档 min；最后一档保持无上限
        norm = {}
        for y, lst in ymap.items():
            lst2 = sorted(lst, key=lambda x: x[0])
            for i, (mn, mx, f) in enumerate(lst2):
                if mx is None and i + 1 < len(lst2):
                    mx = lst2[i + 1][0]
                norm.setdefault(y, []).append((mn, mx, f))
        # ② 行键＝出现过的 (下限, 上限) 组合；某年度缺该区间就留空
        keys, seen = [], set()
        for y in years:
            for (mn, mx, _f) in norm[y]:
                if (mn, mx) not in seen:
                    seen.add((mn, mx))
                    keys.append((mn, mx))
        keys.sort(key=lambda k: (k[0], k[1] is None, k[1] or 0))
        rows = []
        for (mn, mx) in keys:
            fees = {}
            for y in years:
                for (a, b, f) in norm[y]:
                    if a == mn and b == mx:
                        fees[str(y)] = f
                        break
            if fees:
                rows.append({'min': mn, 'max': mx, 'fees': fees})
        full = {}
        for y in years:
            best = None
            for (a, _b, f) in norm[y]:
                if best is None or a >= best[0]:
                    best = (a, f)
            if best:
                full[str(y)] = best[1]
        out[tid] = {'years': years, 'rows': rows, 'full': full,
                    'cnt': len(rows), 'n': sum(len(v) for v in norm.values())}
    return out


def _ryc_ratio_map(cid):
    """{years:[2,3], rows:[{'min','max','ratios':{'2':60.0,'3':70.0}}], cnt, n}

    四十改（2026-10-29）：续期服务费比例矩阵 —— 与 _ryc_steps_map 同构，便于表单回填与详情
    展示复用同一套表格结构。区别只有两点：
      ① **不挂缴别**（整个定制产品/合约共用一份，威哥「对于定制产品整体」）；
      ② 值是「比例 %」（乘数）而不是「费率 %」—— 实发＝缴别当年基础费率 RYC-N × 本比例。
    · max 为空 ＝ 无上限；同一 (year, min) 只保留第一次出现的行，避免脏数据把表撑开。
    """
    out = {'years': [], 'rows': [], 'cnt': 0, 'n': 0}
    if not cid:
        return out
    raw = (CrmRycRatio.query.filter_by(contract_id=cid)
           .order_by(CrmRycRatio.ryc_year, CrmRycRatio.min_rate, CrmRycRatio.id).all())
    if not raw:
        return out
    years = sorted({r.ryc_year for r in raw if r.ryc_year})
    # ① 按年度分组并补齐上限（老数据只存下限时，上限＝同年度下一档下限；最后一档无上限）
    per = {}
    for r in raw:
        per.setdefault(r.ryc_year, []).append(
            (round(r.min_rate or 0, 2),
             None if r.max_rate is None else round(r.max_rate, 2),
             round(r.ratio_pct if r.ratio_pct is not None else 100.0, 2)))
    norm = {}
    for y, lst in per.items():
        lst2 = sorted(lst, key=lambda x: x[0])
        for i, (mn, mx, v) in enumerate(lst2):
            if mx is None and i + 1 < len(lst2):
                mx = lst2[i + 1][0]
            norm.setdefault(y, []).append((mn, mx, v))
    # ② 行键＝出现过的 (下限, 上限)；某年度缺该区间就留空
    keys, seen = [], set()
    for y in years:
        for (mn, mx, _v) in norm.get(y, []):
            if (mn, mx) not in seen:
                seen.add((mn, mx))
                keys.append((mn, mx))
    keys.sort(key=lambda k: (k[0], k[1] is None, k[1] or 0))
    rows = []
    for (mn, mx) in keys:
        ratios = {}
        for y in years:
            for (a, b, v) in norm.get(y, []):
                if a == mn and b == mx:
                    ratios[str(y)] = v
                    break
        if ratios:
            rows.append({'min': mn, 'max': mx, 'ratios': ratios})
    out.update(years=years, rows=rows, cnt=len(rows),
               n=sum(len(v) for v in norm.values()))
    return out


def _save_ryc_ratios(c, form):
    """保存「续期服务费比例」（合约级 / 整个定制产品共用一份，全量替换）。返回写入的行数。

    四十改字段（不带缴别序号，整份产品只录一次）：
        rr_years            勾选的续期年度（多值，2~10）—— 决定矩阵有哪几列
        rr_rcnt             区间行数
        rr_min_<r>          第 r 行「继续率下限 %」
        rr_max_<r>          第 r 行「继续率上限 %」（空＝无上限）
        rr_ratio_<r>_<y>    第 r 行第 y 年度的服务费比例 %
    · 整块空白 → 清空且不建任何行（＝该产品没有按继续率分档的比例政策）；
    · 与 _save_tiers「先删后建」口径一致，避免残留旧区间。
    """
    for old in CrmRycRatio.query.filter_by(contract_id=c.id).all():
        db.session.delete(old)
    db.session.flush()
    # 四十三改：本块改成**可选项** —— 表单头上有「本产品有续期服务费比例政策」勾选框
    # （name=has_ratio）。没勾＝该产品不按继续率分档，整块不保存（同时也清掉旧数据，
    # 这样「取消勾选再保存」＝撤销该政策）。为兼容不提交勾选框的老前端/回归脚本，
    # 只要表单里带了年度（rr_years）就仍按「有政策」处理。
    if not form.get('has_ratio') and not form.getlist('rr_years'):
        return 0
    years = []
    for y in form.getlist('rr_years'):
        try:
            y = int(str(y).strip())
        except ValueError:
            continue
        if y in RYC_YEARS and y not in years:
            years.append(y)
    years.sort()
    if not years:
        return 0
    try:
        rcnt = int(form.get('rr_rcnt') or 0)
    except ValueError:
        rcnt = 0
    rows = []
    for r in range(rcnt):
        mn = (form.get('rr_min_%d' % r) or '').strip()
        mx = (form.get('rr_max_%d' % r) or '').strip()
        ratios = {}
        for y in years:
            v = (form.get('rr_ratio_%d_%d' % (r, y)) or '').strip()
            if v != '':
                ratios[y] = _to_num(v)
        if mn == '' and not ratios:              # 整行空 → 跳过
            continue
        rows.append({'min': _to_num(mn),
                     'max': (_to_num(mx) if mx != '' else None),
                     'ratios': ratios})
    if not rows:
        return 0
    rows.sort(key=lambda x: x['min'])
    n = 0
    for row in rows:
        for y in years:
            if y not in row['ratios']:
                continue
            db.session.add(CrmRycRatio(
                contract_id=c.id, ryc_year=y,
                min_rate=row['min'], max_rate=row['max'],
                ratio_pct=row['ratios'][y], sort_no=n))
            n += 1
    return n


def _ryc_full_of(steps):
    """一组档位的满档费率 = 继续率下限最大的那一档（保留给旧调用，矩阵口径见 _ryc_steps_map）。"""
    best = None
    for st in (steps or []):
        if best is None or (st.get('min') or 0) >= (best.get('min') or 0):
            best = st
    return round((best or {}).get('fee') or 0, 2)


def _save_ryc_steps(t, form, ix):
    """保存某缴别的「续期继续率分档」（全量替换），返回 {年度: 满档费率}。

    三十一改（2026-10-29）：表单从「按年度分块」改为**区间矩阵**，字段：
        rs_years_<ix>           勾选的续期年度（多值，2~10）—— 决定矩阵有哪几列
        rs_rcnt_<ix>            区间行数
        rs_min_<ix>_<r>         第 r 行「继续率下限 %」
        rs_max_<ix>_<r>         第 r 行「继续率上限 %」（空＝无上限）
        rs_fee_<ix>_<r>_<y>     第 r 行第 y 年度的续期服务费率 %
    · 一个区间行里填了几个年度就存几行（(tier, year, min, max) 一行）；
    · 某年度一档都没填 → 该年度不建分档、也不返回（继续用固定 ryc_N，不会被误覆盖）；
    · 满档＝该年度「下限最大的、填了费率的那一行」→ 由 _save_tiers 回写 ryc_N。
    """
    for old in CrmContractRycStep.query.filter_by(tier_id=t.id).all():
        db.session.delete(old)
    db.session.flush()
    years = []
    for y in form.getlist('rs_years_%s' % ix):
        try:
            y = int(str(y).strip())
        except ValueError:
            continue
        if y in RYC_YEARS and y not in years:
            years.append(y)
    years.sort()
    if not years:
        return {}
    try:
        rcnt = int(form.get('rs_rcnt_%s' % ix) or 0)
    except ValueError:
        rcnt = 0
    rows = []
    for r in range(rcnt):
        mn = (form.get('rs_min_%s_%d' % (ix, r)) or '').strip()
        mx = (form.get('rs_max_%s_%d' % (ix, r)) or '').strip()
        fees = {}
        for y in years:
            v = (form.get('rs_fee_%s_%d_%d' % (ix, r, y)) or '').strip()
            if v != '':
                fees[y] = _to_num(v)
        if mn == '' and not fees:                 # 整行空 → 跳过
            continue
        rows.append({'min': _to_num(mn),
                     'max': (_to_num(mx) if mx != '' else None),
                     'fees': fees})
    if not rows:
        return {}
    rows.sort(key=lambda x: x['min'])
    full, n = {}, 0
    for y in years:
        best = None
        for row in rows:
            if y in row['fees'] and (best is None or row['min'] >= best['min']):
                best = row
        if best:
            full[y] = round(best['fees'][y], 2)
    for row in rows:
        for y in years:
            if y not in row['fees']:
                continue
            db.session.add(CrmContractRycStep(
                contract_id=t.contract_id, tier_id=t.id, ryc_year=y,
                min_rate=row['min'], max_rate=row['max'],
                fee_rate=row['fees'][y], sort_no=n))
            n += 1
    return full


def _save_tiers(c, form, compmap=None):
    """保存合约的缴别基础合约 + **合约级**方案政策（全量替换）。

    结构（两层，十二改起彻底分开）：
      · 缴别基础合约（必录，按缴别）：tier_idx / tier_period_<ix> /
        tier_<pre>_fyc_<ix> / tier_ryc_<n>_<ix> / tier_note_<ix> / tier_comp_<ix>
        （合约起止日已提到合约级：base_start / base_end 写进每个缴别）
      · 方案政策（**合约级，独立于缴别**，可多条可空）：
          方案头（一份，全缴别共用）—— ph_*_<k>
          奖励（按缴别）—— ph_tt_coef_<k>_<组件键>_<缴别> / ph_tt_amount_…
                        ph_ff_coef_<k>_<组件键>_<缴别> / ph_ff_amount_…
    十改：方案起止日「全合约共用一条」→ plan_base_start / plan_base_end 各填一次，
      这里写进每条方案的 tt_*/ff_* 同值。
    十二改：方案不再嵌在缴别卡内（威哥：不要跟基础合约和在一起）。方案头只解析一次，
      奖励按「方案 × 缴别」落库；某缴别对某方案四项奖励**全空 → 该缴别不适用该方案**，不生成记录。
    二十八改（2026-10-29）：定制类产品**按组件分别录费率** ——
      ① 每张缴别卡带 `tier_comp_<ix>`（组件表单序号），查 compmap 落 `component_id`；
      ② 不同组件可以有**相同缴别**（如主险与附加险都 20年交），故排序键改为
         「组件顺序 → 缴别正序」，保证同组件的行连在一起；
      ③ 方案奖励字段加一层「组件键」—— 否则两个组件同缴别会读到同一个字段，
         佣金查询里方案加佣被重复计一次。组件键 `ck`＝组件 id（无组件时固定 '0'）。
    先清空该合约全部缴别与方案，再按表单重建。缴别留空行跳过。
    """
    compmap = compmap or {}
    for old in CrmContractTier.query.filter_by(contract_id=c.id).all():
        db.session.delete(old)
    for old in CrmContractPlan.query.filter_by(contract_id=c.id).all():
        db.session.delete(old)
    # 二十九改：续期继续率分档跟着缴别一起重建（旧 tier_id 会被删，分档必须先清，
    # 否则重建后旧 tier_id 的分档会变成孤儿数据）
    for old in CrmContractRycStep.query.filter_by(contract_id=c.id).all():
        db.session.delete(old)
    db.session.flush()
    # 组件排序号：让「主险 → 附加险」的行按组件顺序排列（不按表单 DOM 顺序）
    # 四十七改：分口径的组件来自总总（本合约没有组件记录），所以排序表要把
    #   compmap 里的外部组件 id 一并收进来，否则 `_comp_of` 判 `in corder` 不成立 → 组件归属丢失。
    corder = {x.id: (x.sort_no or 0) for x in _components_of(c.id)}
    for _v in (compmap or {}).values():
        if _v not in corder:
            corder[_v] = len(corder)

    # 六改：缴别一律按 PAY_PERIODS 正序落库（趸交 → 2年交 → … → 30年交 → 交至X岁），
    # 不受表单 DOM 顺序影响——用户勾选的缴别、分公司从总总带入的缴别都能保证正序。
    order = {p: i for i, p in enumerate(PAY_PERIODS)}

    def _ckey_of(ix):
        """缴别卡所属组件的**表单序号**（方案奖励字段的组件键，老数据为 '0'）。"""
        return (form.get('tier_comp_%s' % ix) or '').strip()

    def _comp_of(ix):
        """缴别卡所属组件 id（没有/非定制类 → None）。"""
        cid_ = compmap.get(_ckey_of(ix))
        return cid_ if cid_ in corder else None

    idxs = sorted(form.getlist('tier_idx'),
                  key=lambda ix: (corder.get(_comp_of(ix), -1),
                                  order.get((form.get('tier_period_%s' % ix) or '').strip(), 999)))
    # 七改：合约起止日「所有缴别共用一条」——表单只在费用政策表上方填一次，
    # 这里写入每个缴别的对应侧字段（tt_* 或 ff_* 都写同一个值，保证详情/导出不缺项）。
    base_start = (form.get('base_start') or '').strip()
    base_end = (form.get('base_end') or '').strip()
    # 十改：方案起止日同样「全合约共用一条」——不因缴别、不因方案条数而不同。
    # 一个产品下的所有阶段方案共用同一段方案期间，表单只填一次。
    plan_start = (form.get('plan_base_start') or '').strip()
    plan_end = (form.get('plan_base_end') or '').strip()
    # 十二改：方案头（名称/费项类型/政策归属/费用形式）在合约级只解析一次，
    # 每个缴别按同一份清单落奖励行 —— 这正是「不要每个缴别重复填写」的落点。
    # 十五改：方案分两类——系数类按缴别落多行；文字类（现金/购物或其他）不按缴别、合约下只落一行。
    heads = _plan_heads(form)
    coef_heads = [h for h in heads if not h['text']]
    text_heads = [h for h in heads if h['text']]
    n = 0
    for ix in idxs:
        period = (form.get('tier_period_%s' % ix) or '').strip()
        if not period:
            continue
        comp_id = _comp_of(ix)
        ck = _ckey_of(ix) or '0'          # 组件键：方案奖励字段的第三段（表单序号，与前端一致）
        t = CrmContractTier(
            contract_id=c.id, component_id=comp_id, sort_no=n, pay_period=period,
            tt_start=base_start, tt_end=base_end,
            ff_start=base_start, ff_end=base_end,
            tt_fyc=_to_num(form.get('tier_tt_fyc_%s' % ix)),
            ff_fyc=_to_num(form.get('tier_ff_fyc_%s' % ix)),
            note=(form.get('tier_note_%s' % ix) or '').strip())
        for nn in range(2, 11):
            setattr(t, 'ryc_%d' % nn, _to_num(form.get('tier_ryc_%d_%s' % (nn, ix))))
        _calc_tier_totals(t)
        db.session.add(t)
        db.session.flush()                 # 先落 tier.id，方案才能挂上去
        # 二十九改：续期继续率分档 —— 某年度一旦建了分档，就用「满档费率」覆盖该年度 RYC，
        # 这样佣金查询 / 销售费用清单 / 团队系数 / 试算器全链路**零改动**即按满档口径计算。
        for _y, _r in _save_ryc_steps(t, form, ix).items():
            setattr(t, 'ryc_%d' % _y, _r)
        # —— 合约级方案政策：按清单逐条挂到本缴别（奖励按「组件 × 缴别」）——
        pn = 0
        for h in coef_heads:
            k = h['k']
            # 二十八改：先读「组件键＋缴别」，读不到再退回旧键（兼容二十八改前存的老数据）
            def _fld(pre, kind):
                v = form.get('ph_%s_%s_%s_%s_%s' % (pre, kind, k, ck, period))
                if v is None and not comp_id:
                    v = form.get('ph_%s_%s_%s_%s' % (pre, kind, k, period))
                return _to_num(v)
            tt_c, tt_a = _fld('tt', 'coef'), _fld('tt', 'amount')
            ff_c, ff_a = _fld('ff', 'coef'), _fld('ff', 'amount')
            # 十二改：该缴别对这条方案四项奖励全空 → 视为「本缴别不适用该方案」，不生成记录
            if not (tt_c or tt_a or ff_c or ff_a):
                continue
            p = CrmContractPlan(
                contract_id=c.id, tier_id=t.id, sort_no=pn,
                plan_name=h['name'], fee_type=h['fee'],
                tt_start=plan_start, tt_end=plan_end,
                ff_start=plan_start, ff_end=plan_end,
                policy_scope=h['scope'],
                # 八改：费用形式的合法值由「政策归属」决定，最终以 _calc_plan_total 强校验为准
                pay_form=h['form'],
                # 六改遗留字段：八改起表单不再录入（仅兼容展示历史数据）
                pay_channel='',
                other_remark=h['remark'],
                tt_coef=tt_c, tt_amount=tt_a, tt_other=h['tt_other'],
                ff_coef=ff_c, ff_amount=ff_a, ff_other=h['ff_other'],
                note=h['note'])
            _calc_plan_total(p)
            db.session.add(p)
            pn += 1
        n += 1
    # 十五改：文字类方案（现金 / 购物或其他）**不按缴别展开** ——
    #   tier_id 留空、合约下只落一行，文字存 plan_text；仍带方案名称与方案期间（与系数类共用一套期间）。
    #   sort_no 从 500 起，保证排在系数类方案之后、彼此之间保持表单顺序。
    for i, h in enumerate(text_heads):
        p = CrmContractPlan(
            contract_id=c.id, tier_id=None, sort_no=500 + i,
            plan_name=h['name'], fee_type=h['fee'],
            tt_start=plan_start, tt_end=plan_end,
            ff_start=plan_start, ff_end=plan_end,
            policy_scope='out', pay_form=h['form'],
            pay_channel='', other_remark=h['remark'], plan_text=h['plan_text'],
            tt_coef=0, tt_amount=0, tt_other=h['tt_other'],
            ff_coef=0, ff_amount=0, ff_other=h['ff_other'],
            note=h['note'])
        _calc_plan_total(p)
        db.session.add(p)
    return n


# ---------- 附件（单个文件：原文件名 + 落盘文件名）----------

def _attach_dir(sub):
    """附件目录 static/uploads/crm/<sub>/（不存在则建）。"""
    d = os.path.join(current_app.config['UPLOAD_DIR'], 'crm', sub)
    os.makedirs(d, exist_ok=True)
    return d


def _save_file(fs, sub, code):
    """保存单个附件，返回 (原文件名, 落盘文件名, 错误说明)。无文件时全部为 None。"""
    if not fs or not (fs.filename or '').strip():
        return None, None, None
    fn = fs.filename.strip()
    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
    if ext not in CRM_ATTACH_EXT:
        return None, None, '%s：不支持的格式' % fn
    data = fs.read()
    if len(data) > CRM_ATTACH_MAX:
        return None, None, '%s：超过 10MB' % fn
    disk = '%s_%s.%s' % ((code or 'CRM').replace('-', ''), uuid.uuid4().hex[:8], ext)
    with open(os.path.join(_attach_dir(sub), disk), 'wb') as f:
        f.write(data)
    return fn, disk, None


def _drop_file(sub, disk):
    """删除落盘附件（容错：文件不在也不报错）。"""
    if not disk:
        return
    try:
        os.remove(os.path.join(_attach_dir(sub), disk))
    except OSError:
        pass


# ---------- 四十三改（2026-10-29）：产品合约「多附件」 ----------
# 威哥口径：上传成功的文件要**以列表方式呈现**、并且能逐个删除。
# 老版只有主表 file_name/file_path 两个字段（单文件、再传即覆盖），这里升级成子表
# CrmContractFile（一条记录=一个文件），旧字段保留不删 —— `_contract_files()` 会把
# 旧字段里那个文件自动补登进子表，老合约打开详情照样能看到、能下载。

def _contract_files(cid, migrate=True):
    """取某合约的附件列表（按上传顺序）。

    migrate=True 时把主表旧字段 file_name/file_path 里的那个文件补登成一条子表记录
    （只补一次，之后就以子表为准），这样历史合约的扫描件不会「消失」。
    """
    rows = (CrmContractFile.query.filter_by(contract_id=cid)
            .order_by(CrmContractFile.sort_no, CrmContractFile.id).all())
    if not rows and migrate:
        c = CrmProductContract.query.get(cid)
        if c and (c.file_path or '').strip():
            f = CrmContractFile(contract_id=cid, file_name=c.file_name or c.file_path,
                               file_path=c.file_path, file_size=0,
                               upload_by=c.created_by, sort_no=0)
            db.session.add(f)
            db.session.commit()
            rows = [f]
    return rows


def _add_contract_files(c, files):
    """把上传的多个文件追加到合约附件表 → (新增条数, 错误说明列表, 本次新增的文件对象)。

    与旧单文件逻辑不同：**不再覆盖**，一次可以选多个、全部追加；
    文件名重复的（同名同大小）跳过，避免手滑传两遍。
    第三个返回值给调用方更新主表旧字段 file_name/file_path（语义＝**最近一次上传的那份**），
    这样老的「下载扫描件」入口、导出、以及历史页面都不会指到已删的文件上。
    """
    added, errs, news = 0, [], []
    exists = {(f.file_name or '', f.file_size or 0)
              for f in CrmContractFile.query.filter_by(contract_id=c.id).all()}
    base_no = (CrmContractFile.query.filter_by(contract_id=c.id).count()) or 0
    for fs in (files or []):
        if not fs or not (fs.filename or '').strip():
            continue
        fn, disk, err = _save_file(fs, 'contract', c.code)
        if err:
            errs.append(err)
            continue
        size = 0
        try:
            size = os.path.getsize(os.path.join(_attach_dir('contract'), disk))
        except OSError:
            size = 0
        if (fn, size) in exists:
            _drop_file('contract', disk)         # 同名同大小 → 视为重复，不重复登记
            continue
        exists.add((fn, size))
        base_no += 1
        f = CrmContractFile(contract_id=c.id, file_name=fn, file_path=disk,
                           file_size=size, upload_by=session.get('user_id'),
                           sort_no=base_no)
        db.session.add(f)
        news.append(f)
        added += 1
    return added, errs, news


def _del_contract_file(cid, fid):
    """删掉一个合约附件（落盘文件一起删）→ True 删除成功 / False 记录不存在。"""
    f = CrmContractFile.query.filter_by(id=fid, contract_id=cid).first()
    if not f:
        return False
    _drop_file('contract', f.file_path)
    db.session.delete(f)
    # 兼容旧字段：删的正好是主表登记的那个 → 主表也清掉，避免详情页「幽灵附件」
    c = CrmProductContract.query.get(cid)
    if c and (c.file_path or '') == (f.file_path or ''):
        c.file_name, c.file_path = None, None
    db.session.commit()
    return True



def _size_text(n):
    """字节数 → 可读大小。"""
    n = n or 0
    if n < 1024:
        return '%d B' % n
    if n < 1024 * 1024:
        return '%.1f KB' % (n / 1024.0)
    return '%.1f MB' % (n / 1024.0 / 1024.0)


def _xlsx_response(sheet_title, title, headers, data, groups=None, pct_cols=None):
    """把一张表写进 xlsx 并返回下载响应（openpyxl 为项目既有依赖）。

    ⚠️ groups（十六改微调新增，可选）：两级表头时传 [(分组名, 跨列数), ...]，
       各分组跨列数之和须等于 len(headers)；不传就是原来的单层表头（老调用点零影响）。
       例：[('', 3), ('首年度销售费用', 4), ('续年期销售费用', 9)]

    ⚠️ pct_cols（可选）：百分比列配置 {列号(1起): 小数位数}。
       单元格仍存「百分点数值」（如 4.5 表示 4.5%），但用 Excel 数字格式
       0.00"%" 显示成 4.50%，与页面显示一致，且仍是数字、可参与 Excel 计算。
       不传＝普通数值列（老调用点零影响）。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or '导出')[:28]
    ncol = max(len(headers), 2)
    thin = Side(style='thin', color='BFBFBF')
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    c = ws.cell(row=1, column=1, value=title)
    c.font = Font(bold=True, size=14)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c.alignment = Alignment(horizontal='center')

    hrow = 2
    if groups:
        col = 1
        for lab, span in groups:
            span = int(span or 1)
            if span > 1:
                ws.merge_cells(start_row=2, start_column=col,
                               end_row=2, end_column=col + span - 1)
            gc = ws.cell(row=2, column=col, value=lab)
            gc.font = Font(bold=True, color='C86432')
            gc.alignment = Alignment(horizontal='center', vertical='center')
            for k in range(col, col + span):      # 合并区每个格子都要描边
                ws.cell(row=2, column=k).border = box
            col += span
        hrow = 3

    for i, h in enumerate(headers, 1):
        cell = ws.cell(row=hrow, column=i, value=h)
        cell.font = Font(bold=True)
        cell.border = box
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for ri, row in enumerate(data, hrow + 1):
        for ci, v in enumerate(row, 1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.border = box
            # 百分比列：用 Excel 数字格式显示成 XX.XX%（值仍是数字，可继续参与计算）
            if pct_cols and ci in pct_cols and isinstance(v, (int, float)):
                cell.number_format = '0.' + '0' * int(pct_cols[ci]) + '"%"'

    for i in range(1, ncol + 1):
        w = 10
        for row in [headers] + list(data):
            if i - 1 < len(row):
                t = str(row[i - 1])
                wide = len(t) * 2 if any('\u4e00' <= ch <= '\u9fff' for ch in t) else len(t)
                w = max(w, wide)
        ws.column_dimensions[get_column_letter(i)].width = min(max(w + 3, 10), 40)

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    name = '%s_%s.xlsx' % (title, datetime.now().strftime('%Y%m%d_%H%M'))
    return send_file(bio, as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ===================== ① 保司档案 =====================

@bp.route('/crm/insurers')
@login_required
@permission_required('crm.view')
def insurers():
    """保司档案列表：KPI + 关键词搜索 + 类型筛选 + 合作状态筛选。"""
    kw = (request.args.get('kw') or '').strip()
    t = (request.args.get('type') or '').strip()
    st = (request.args.get('status') or '').strip()
    q = Insurer.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(Insurer.name.like(like), Insurer.short_name.like(like),
                            Insurer.credit_code.like(like), Insurer.legal_person.like(like)))
    if t:
        q = q.filter(Insurer.insurer_type == t)
    if st:
        q = q.filter(Insurer.status == st)
    rows = q.order_by(Insurer.id.desc()).all()

    allrows = Insurer.query.all()
    kpi = {
        'total': len(allrows),
        'coop': len([r for r in allrows if (r.status or '') == 'coop']),
        'life': len([r for r in allrows if (r.insurer_type or '') == 'life']),
        'property': len([r for r in allrows if (r.insurer_type or '') == 'property']),
    }
    return render_template('crm/insurer_list.html', rows=rows, kpi=kpi, kw=kw, t=t, st=st,
                           types=INSURER_TYPES, statuses=INSURER_STATUS, dname=DNAME,
                           can_manage=_can('crm.insurer.manage'))


# ========== 联络人（一对多，保司 / 省分公司共用）==========

def _linkmen(owner_type, owner_id):
    """取某主体（保司 insurer / 省分公司 branch）的全部联络人，按排序号。"""
    return InsurerLinkman.query.filter_by(
        owner_type=owner_type, owner_id=owner_id) \
        .order_by(InsurerLinkman.sort_no, InsurerLinkman.id).all()


def _save_linkmen(owner_type, owner_id, form):
    """全量替换某主体的联络人列表（先删后建，口径同 _save_tiers）。

    提交约定：`lm_idx` 系列序号 + `lm_name_<ix>` / `lm_title_<ix>` /
    `lm_phone_<ix>` / `lm_email_<ix>` / `lm_note_<ix>`；五列全空的行自动跳过。
    """
    InsurerLinkman.query.filter_by(owner_type=owner_type, owner_id=owner_id).delete()
    idxs = form.getlist('lm_idx')
    for i, ix in enumerate(idxs):
        def _v(field):
            return (form.get(field + '_' + ix) or '').strip()
        vals = {k: _v('lm_' + k) for k in ('name', 'title', 'phone', 'email', 'note')}
        if not any(vals.values()):
            continue  # 整行空白则跳过
        db.session.add(InsurerLinkman(owner_type=owner_type, owner_id=owner_id,
                                      sort_no=i, **vals))


def _sync_legacy_contact(obj, linkmen, has_title):
    """把首个联络人回写主表 legacy 字段，兼容 Excel 导出与内置库逻辑。"""
    if not linkmen:
        return
    lm = linkmen[0]
    obj.contact_person = lm.name
    obj.contact_phone = lm.phone
    if has_title:
        obj.contact_title = lm.title


@bp.route('/crm/insurers/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_new():
    """新建保司档案（支持从内置保司库一键填充）。"""
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('保司全称不能为空')
            return redirect(url_for('crm.insurer_new'))
        # 十六改：唯一性校验（全称 / 简称 / 统一社会信用代码，归一化比对）
        dup = _dup_insurer(name, _f('short_name'), _f('credit_code'))
        if dup:
            flash(dup)
            return redirect(url_for('crm.insurer_new'))
        it = Insurer(
            name=name, short_name=_f('short_name'), credit_code=_f('credit_code'),
            reg_no=_f('reg_no'), license_no=_f('license_no'),
            license_date=_f('license_date'),   # 四十一改：许可证批准设立日期（监管口径）
            tax_no=_f('tax_no'),
            legal_person=_f('legal_person'), reg_capital=_f('reg_capital'),
            establish_date=_f('establish_date'), address=_f('address'),
            phone=_f('phone'), website=_f('website'), business_scope=_f('business_scope'),
            insurer_type=_f('insurer_type') or 'life', status=_f('status') or 'coop',
            source='builtin' if _f('from_builtin') == '1' else 'manual',
            data_updated_at=datetime.now().strftime('%Y-%m-%d'),
            note=_f('note'), created_by=session.get('user_id'))
        db.session.add(it)
        db.session.flush()  # 拿 id 后落联络人
        _save_linkmen('insurer', it.id, request.form)
        _sync_legacy_contact(it, _linkmen('insurer', it.id), has_title=True)
        # 四十一改：威哥要「只填全称、其余自动来」——新增保存时直接抓一次。
        # 只补空字段（不会覆盖表单里刚填的），抓取异常不阻断保存。
        filled, auto_msgs = [], []
        try:
            filled, auto_msgs = _fetch_fill(it, _fetch_cfg())
        except Exception as e:
            auto_msgs = ['自动抓取未成功（%s），可稍后在详情页点「一键抓取」重试' % e]
        db.session.commit()
        flash('保司档案已新增：%s' % name)
        if filled:
            flash('已自动抓取回填 %d 项：%s'
                  % (len(filled), '、'.join('%s=%s' % (k, v) for k, v in filled)))
        for m in auto_msgs:
            flash(m)
        return redirect(url_for('crm.insurers') if not filled else
                        url_for('crm.insurer_detail', iid=it.id))
    return render_template('crm/insurer_form.html', row=None, types=INSURER_TYPES,
                           statuses=INSURER_STATUS, linkmen=[],
                           builtin=json.dumps(BUILTIN_INSURERS, ensure_ascii=False))


def _to_yi(v, unit):
    """保费 / 净利润统一折算为「亿元」，便于趋势图同轴展示。"""
    v = v or 0
    return round(v / 100.0, 4) if (unit or 'yi') == 'wan' else round(v, 4)


@bp.route('/crm/insurers/<int:iid>')
@login_required
@permission_required('crm.view')
def insurer_detail(iid):
    """保司档案详情：基础信息 + 省分公司 + 岗位联络 + 年度经营指标 + 合约与资料。"""
    it = Insurer.query.get_or_404(iid)
    contracts = CrmProductContract.query.filter_by(insurer_id=iid) \
        .order_by(CrmProductContract.id.desc()).all()
    docs = CrmProductDoc.query.filter_by(insurer_id=iid) \
        .order_by(CrmProductDoc.id.desc()).all()
    branches = InsurerBranch.query.filter_by(insurer_id=iid) \
        .order_by(InsurerBranch.province, InsurerBranch.id).all()
    contacts = InsurerContact.query.filter_by(insurer_id=iid) \
        .order_by(InsurerContact.id.desc()).all()
    metrics = InsurerMetric.query.filter_by(insurer_id=iid) \
        .order_by(InsurerMetric.year.desc()).all()

    # 岗位分组：现任排最前，其次按上任日期倒序（任职历史可查）
    role_map = {}
    for ct in contacts:
        role_map.setdefault(ct.role or 'other', []).append(ct)
    for k in role_map:
        role_map[k].sort(key=lambda x: x.start_date or '', reverse=True)
        role_map[k].sort(key=lambda x: 0 if (x.status or '') == 'current' else 1)
    current_by_role = {}
    for k, lst in role_map.items():
        cur = [x for x in lst if (x.status or '') == 'current']
        if cur:
            current_by_role[k] = cur[0]

    # 经营指标趋势（保费统一折算「亿元」；页内用迷你柱状图渲染，零外部依赖、离线可用）
    asc = sorted(metrics, key=lambda x: x.year or '')
    prem = [_to_yi(m.premium, m.premium_unit) for m in asc]
    chart = {
        'years': [m.year or '' for m in asc],
        'premium': prem,
        'roi': [m.roi or 0 for m in asc],
        'cy': [m.composite_yield or 0 for m in asc],
        'rating': [m.rating or '' for m in asc],
        'max_premium': max(prem or [0]),
    }
    # 各合约的缴别概况（列表用）
    cstats = {}
    for c in contracts:
        ts = _contract_tiers(c.id)
        cstats[c.id] = {
            'n': len(ts),
            'out': len([x for x in ts if (x.policy_scope or '') == 'out']),
            'total': (round(sum((x.grand_total or 0) for x in ts), 2) if ts
                      else round(c.grand_total or 0, 2)),
        }
    linkmen = _linkmen('insurer', iid)
    branch_linkmen = {b.id: _linkmen('branch', b.id) for b in branches}
    return render_template('crm/insurer_detail.html', row=it, contracts=contracts, docs=docs,
                           branches=branches, role_map=role_map, contacts=contacts,
                           linkmen=linkmen, branch_linkmen=branch_linkmen,
                           current_by_role=current_by_role, metrics=metrics,
                           chart=chart, cstats=cstats,
                           roles=CONTACT_ROLES, rating_levels=RATING_LEVELS,
                           premium_units=PREMIUM_UNITS, scopes=POLICY_SCOPES, forms=PAY_FORMS,
                           today=datetime.now().strftime('%Y-%m-%d'),
                           dname=DNAME, can_manage=_can('crm.insurer.manage'))


# ========== 二级：省分公司 ==========

def _fill_branch(b, form):
    """把表单值写进省分公司对象。"""
    b.province = _f('province')
    b.name = _f('name')
    b.short_name = _f('short_name')
    b.credit_code = _f('credit_code')
    b.reg_no = _f('reg_no')
    b.license_no = _f('license_no')
    b.tax_no = _f('tax_no')
    b.principal = _f('principal')
    b.phone = _f('phone')
    b.address = _f('address')
    b.establish_date = _f('establish_date')
    b.license_date = _f('license_date')      # 四十二改：许可证批准日期（监管口径）
    b.note = _f('note')
    # 联络人改由 _save_linkmen 落 InsurerLinkman，主表 legacy 字段仅在保存后回写首个


@bp.route('/crm/branches')
@login_required
@permission_required('crm.view')
def branch_list():
    """分公司档案：跨保司的省分公司全量列表（可按保司 / 关键词筛选）。

    五改（2026-10-02）威哥要求：一家总公司旗下可以有多家省级分公司，
    分公司必须能独立汇总查看，而不是只能挨个钻保司详情去翻。
    """
    iid = (request.args.get('insurer_id') or '').strip()
    kw = (request.args.get('kw') or '').strip()
    q = InsurerBranch.query
    if iid.isdigit():
        q = q.filter(InsurerBranch.insurer_id == int(iid))
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(InsurerBranch.name.like(like),
                            InsurerBranch.short_name.like(like),
                            InsurerBranch.province.like(like),
                            InsurerBranch.insurer_name.like(like),
                            InsurerBranch.principal.like(like)))
    rows = q.order_by(InsurerBranch.insurer_name, InsurerBranch.province).all()
    allrows = InsurerBranch.query.all()
    kpi = {'total': len(allrows),
           'insurers': len(set([r.insurer_id for r in allrows])),
           'provinces': len(set([(r.province or '').strip() for r in allrows if (r.province or '').strip()])),
           'signed': len(set([r.id for r in allrows
                              if CrmProductContract.query.filter_by(branch_id=r.id).count()]))}
    linkmen = {b.id: _linkmen('branch', b.id) for b in rows}
    # 每家分公司挂在多少份签约下（删除保护用）
    used = {b.id: CrmProductContract.query.filter_by(branch_id=b.id).count() for b in rows}
    return render_template('crm/branch_list.html', rows=rows, kpi=kpi, linkmen=linkmen,
                           used=used, iid=iid, kw=kw,
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           # 十六改：保司列统一按**简称**展示（简称缺失回落全称）
                           ishort=_insurer_short_map(),
                           can_manage=_can('crm.insurer.manage'))


@bp.route('/crm/insurers/<int:iid>/branches/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def branch_new(iid):
    """新增保司省分公司。"""
    it = Insurer.query.get_or_404(iid)
    if request.method == 'POST':
        if not _f('name'):
            flash('分公司全称不能为空')
            return redirect(url_for('crm.branch_new', iid=iid))
        # 十六改：同一保司下省分公司全称 / 简称唯一
        dup = _dup_branch(it.id, _f('name'), _f('short_name'))
        if dup:
            flash(dup)
            return redirect(url_for('crm.branch_new', iid=iid))
        b = InsurerBranch(insurer_id=it.id, insurer_name=it.name,
                          created_by=session.get('user_id'))
        _fill_branch(b, request.form)
        db.session.add(b)
        db.session.flush()  # 拿 id 后落联络人
        _save_linkmen('branch', b.id, request.form)
        _sync_legacy_contact(b, _linkmen('branch', b.id), has_title=False)
        db.session.commit()
        # 四十二改：新建分公司保存后自动抓一次（只补空、失败静默），省得再点按钮
        _auto_fetch_branch(b)
        flash('省分公司已新增：%s' % b.name)
        if _f('back_flag') == 'list':      # 从分公司档案列表发起的，回列表更快接着录下一家
            return redirect(url_for('crm.branch_list', insurer_id=it.id))
        return redirect(url_for('crm.insurer_detail', iid=iid))
    return render_template('crm/branch_form.html', row=None, insurer=it, linkmen=[],
                           back_flag=request.args.get('back_flag') or '')


@bp.route('/crm/branches/<int:bid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def branch_edit(bid):
    """编辑保司省分公司。"""
    b = InsurerBranch.query.get_or_404(bid)
    if request.method == 'POST':
        if not _f('name'):
            flash('分公司全称不能为空')
            return redirect(url_for('crm.branch_edit', bid=bid))
        # 十六改：同一保司下省分公司全称 / 简称唯一（排除自己）
        dup = _dup_branch(b.insurer_id, _f('name'), _f('short_name'), exclude_id=bid)
        if dup:
            flash(dup)
            return redirect(url_for('crm.branch_edit', bid=bid))
        _fill_branch(b, request.form)
        _save_linkmen('branch', bid, request.form)
        _sync_legacy_contact(b, _linkmen('branch', bid), has_title=False)
        db.session.commit()
        flash('省分公司已保存')
        if _f('back_flag') == 'list':
            return redirect(url_for('crm.branch_list'))
        return redirect(url_for('crm.insurer_detail', iid=b.insurer_id))
    return render_template('crm/branch_form.html', row=b, insurer=None,
                           linkmen=_linkmen('branch', bid),
                           back_flag=request.args.get('back_flag') or '')


@bp.route('/crm/branches/<int:bid>/delete', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def branch_delete(bid):
    """删除省分公司（若已被产品合约引用则拦下）。"""
    b = InsurerBranch.query.get_or_404(bid)
    iid = b.insurer_id
    if CrmProductContract.query.filter_by(branch_id=bid).count():
        flash('该分公司已被产品合约引用，不能删除')
        if _f('back_flag') == 'list':
            return redirect(url_for('crm.branch_list'))
        return redirect(url_for('crm.insurer_detail', iid=iid))
    InsurerLinkman.query.filter_by(owner_type='branch', owner_id=bid).delete()
    db.session.delete(b)
    db.session.commit()
    flash('省分公司已删除')
    if _f('back_flag') == 'list':
        return redirect(url_for('crm.branch_list'))
    return redirect(url_for('crm.insurer_detail', iid=iid))


@bp.route('/crm/branches/<int:bid>/fetch', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def branch_fetch(bid):
    """省分公司一键抓取（四十二改）—— 与总公司同一套数据源、「只补空」同一口径。"""
    b = InsurerBranch.query.get_or_404(bid)
    cfg = _fetch_cfg()
    if _no_source(cfg):
        flash('还没有可用的数据源 —— 请先到「数据源配置」开启监管总局许可证库，或填写天眼查 token')
        return redirect(url_for('crm.insurer_source'))
    overwrite = request.form.get('overwrite') == '1'
    filled, msgs = _fetch_fill_branch(b, cfg, overwrite=overwrite)
    b.fetch_try_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if filled:
        db.session.commit()
        flash('已抓取并回填 %d 项：%s' %
              (len(filled), '、'.join('%s=%s' % (k, v) for k, v in filled)))
    else:
        db.session.commit()          # 只落 fetch_try_at，不改业务字段
        flash('本次没有可回填的字段（已填的不覆盖）')
    for m in msgs:
        flash(m)
    if _f('back_flag') == 'list':
        return redirect(url_for('crm.branch_list'))
    return redirect(url_for('crm.branch_edit', bid=b.id))


@bp.route('/crm/branches/fetch-all', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def branch_fetch_all():
    """省分公司批量抓取：只跑缺项，一次最多 5 家（监管站限流，分批点更稳）（四十二改）。"""
    cfg = _fetch_cfg()
    if _no_source(cfg):
        flash('还没有可用的数据源 —— 请先到「数据源配置」开启监管总局许可证库，或填写天眼查 token')
        return redirect(url_for('crm.insurer_source'))
    iid = (request.form.get('insurer_id') or '').strip()
    r = _fetch_branch_batch(overwrite=request.form.get('overwrite') == '1', cfg=cfg,
                            iid=int(iid) if iid.isdigit() else None)
    # 回跳：从保司详情页发起的回详情页，其余回分公司列表（保留了保司筛选）
    if request.form.get('back') == 'insurer' and iid.isdigit():
        back = url_for('crm.insurer_detail', iid=iid)
    elif iid.isdigit():
        back = url_for('crm.branch_list', insurer_id=iid)
    else:
        back = url_for('crm.branch_list')
    if r.get('empty'):
        flash('要抓的分公司都已填全，无需批量抓取（要强制刷新请勾选「覆盖已有值」）')
        if r.get('recent'):
            flash('另有 %d 家在 1 小时内试过仍缺字段，本次跳过' % r['recent'])
        return redirect(back)
    flash('批量抓取完成：处理 %d 家，其中 %d 家有字段被回填' % (r['ok'], r['changed']))
    if r.get('left'):
        flash('还有 %d 家待抓，歇 1 分钟再点一次即可（已抓到的会自动跳过）' % r['left'])
    if r.get('recent'):
        flash('另有 %d 家 1 小时内试过仍缺字段，本次跳过（要强制重试请勾选「覆盖已有值」）' % r['recent'])
    if r.get('stopped'):
        flash('已触发监管站限流（「请五分钟后再试」），批量抓取提前停止，5 分钟后再点一次')
    if r.get('fails'):
        flash('以下 %d 家未能抓全：%s' % (len(r['fails']),
                                     '；'.join(r['fails'][:5]) + ('…' if len(r['fails']) > 5 else '')))
    return redirect(back)


# ========== 二级：岗位人员联络簿（动态手工更新，留痕）==========

def _fill_contact(c, form):
    """把表单值写进岗位人员对象。"""
    c.role = _f('role') or 'contact'
    c.name = _f('name')
    c.phone = _f('phone')
    c.start_date = _f('start_date')
    c.note = _f('note')
    bid = (form.get('branch_id') or '').strip()
    c.branch_id = int(bid) if bid.isdigit() else None


@bp.route('/crm/insurers/<int:iid>/contacts/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def contact_new(iid):
    """新增岗位人员。默认「顶替」——同一岗位的原现任自动标离任（保留历史）。

    这就是威哥要的「动态手工更新」：人员换人时新增一条记录，旧记录自动留痕。
    """
    it = Insurer.query.get_or_404(iid)
    if request.method == 'POST':
        if not _f('name'):
            flash('姓名不能为空')
            return redirect(url_for('crm.contact_new', iid=iid))
        role = _f('role') or 'contact'
        if _f('auto_leave') == '1':
            for old in InsurerContact.query.filter_by(insurer_id=it.id, role=role,
                                                      status='current').all():
                old.status = 'left'
                old.end_date = _f('start_date') or datetime.now().strftime('%Y-%m-%d')
        c = InsurerContact(insurer_id=it.id, insurer_name=it.name, status='current',
                           created_by=session.get('user_id'))
        _fill_contact(c, request.form)
        db.session.add(c)
        db.session.commit()
        flash('岗位人员已登记：%s（%s）' % (c.name, _name_of(CONTACT_ROLES, c.role)))
        return redirect(url_for('crm.insurer_detail', iid=iid) + '#contacts')
    return render_template('crm/contact_form.html', row=None, insurer=it,
                           branches=InsurerBranch.query.filter_by(insurer_id=iid).all(),
                           roles=CONTACT_ROLES, today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/crm/contacts/<int:kid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def contact_edit(kid):
    """编辑岗位人员（改电话 / 补备注 / 修正任职日期）。"""
    c = InsurerContact.query.get_or_404(kid)
    if request.method == 'POST':
        if not _f('name'):
            flash('姓名不能为空')
            return redirect(url_for('crm.contact_edit', kid=kid))
        _fill_contact(c, request.form)
        db.session.commit()
        flash('岗位人员已保存')
        return redirect(url_for('crm.insurer_detail', iid=c.insurer_id) + '#contacts')
    return render_template('crm/contact_form.html', row=c, insurer=None,
                           branches=InsurerBranch.query.filter_by(insurer_id=c.insurer_id).all(),
                           roles=CONTACT_ROLES, today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/crm/contacts/<int:kid>/leave', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def contact_leave(kid):
    """把某人标记为离任（不新增记录）——用于岗位空缺或对方离职。"""
    c = InsurerContact.query.get_or_404(kid)
    c.status = 'left'
    c.end_date = _f('end_date') or datetime.now().strftime('%Y-%m-%d')
    db.session.commit()
    flash('已标记离任：%s' % c.name)
    return redirect(url_for('crm.insurer_detail', iid=c.insurer_id) + '#contacts')


@bp.route('/crm/contacts/<int:kid>/delete', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def contact_delete(kid):
    """删除岗位人员记录。"""
    c = InsurerContact.query.get_or_404(kid)
    iid = c.insurer_id
    db.session.delete(c)
    db.session.commit()
    flash('岗位人员记录已删除')
    return redirect(url_for('crm.insurer_detail', iid=iid) + '#contacts')


# ========== 二级：年度经营指标（按年 Upsert，实时手工更新）==========

def _fill_metric(m, form):
    """把表单值写进经营指标对象。"""
    m.roi = _to_num(form.get('roi'))
    m.composite_yield = _to_num(form.get('composite_yield'))
    m.rating = _f('rating')
    m.rating_org = _f('rating_org')
    m.premium = _to_num(form.get('premium'))
    m.premium_unit = _f('premium_unit') or 'yi'
    m.net_profit = _to_num(form.get('net_profit'))
    m.solvency = _to_num(form.get('solvency'))
    m.source = _f('source')
    m.note = _f('note')


@bp.route('/crm/insurers/<int:iid>/metrics/save', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def metric_save(iid):
    """保存某年度的经营指标：该年度不存在则新建，已存在则覆盖（Upsert）。"""
    it = Insurer.query.get_or_404(iid)
    year = _f('year')
    if not year:
        flash('请填写年度')
        return redirect(url_for('crm.insurer_detail', iid=iid) + '#metrics')
    m = InsurerMetric.query.filter_by(insurer_id=iid, year=year).first()
    is_new = m is None
    if is_new:
        m = InsurerMetric(insurer_id=it.id, insurer_name=it.name, year=year)
        db.session.add(m)
    _fill_metric(m, request.form)
    m.updated_by = session.get('user_id')
    m.updated_at = datetime.utcnow()
    db.session.commit()
    flash('经营指标已%s：%s 年度' % ('新增' if is_new else '更新', year))
    return redirect(url_for('crm.insurer_detail', iid=iid) + '#metrics')


@bp.route('/crm/metrics/<int:mid>/delete', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def metric_delete(mid):
    """删除某年度经营指标。"""
    m = InsurerMetric.query.get_or_404(mid)
    iid = m.insurer_id
    db.session.delete(m)
    db.session.commit()
    flash('经营指标已删除')
    return redirect(url_for('crm.insurer_detail', iid=iid) + '#metrics')


@bp.route('/crm/insurers/<int:iid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_edit(iid):
    """编辑保司档案。"""
    it = Insurer.query.get_or_404(iid)
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('保司全称不能为空')
            return redirect(url_for('crm.insurer_edit', iid=iid))
        # 十六改：唯一性校验（全称 / 简称 / 统一社会信用代码，排除自己）
        dup = _dup_insurer(name, _f('short_name'), _f('credit_code'), exclude_id=iid)
        if dup:
            flash(dup)
            return redirect(url_for('crm.insurer_edit', iid=iid))
        it.name = name
        it.short_name = _f('short_name')
        it.credit_code = _f('credit_code')
        it.reg_no = _f('reg_no')
        it.license_no = _f('license_no')
        it.license_date = _f('license_date')   # 四十一改：许可证批准设立日期（监管口径）
        it.tax_no = _f('tax_no')
        it.legal_person = _f('legal_person')
        it.reg_capital = _f('reg_capital')
        it.establish_date = _f('establish_date')
        it.address = _f('address')
        it.phone = _f('phone')
        it.website = _f('website')
        it.business_scope = _f('business_scope')
        it.insurer_type = _f('insurer_type')
        it.status = _f('status')
        it.note = _f('note')
        it.data_updated_at = datetime.now().strftime('%Y-%m-%d')
        _save_linkmen('insurer', iid, request.form)
        _sync_legacy_contact(it, _linkmen('insurer', iid), has_title=True)
        db.session.commit()
        flash('保司档案已保存')
        return redirect(url_for('crm.insurer_detail', iid=iid))
    return render_template('crm/insurer_form.html', row=it, types=INSURER_TYPES,
                           statuses=INSURER_STATUS, linkmen=_linkmen('insurer', iid),
                           builtin=json.dumps(BUILTIN_INSURERS, ensure_ascii=False))


@bp.route('/crm/insurers/<int:iid>/delete', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_delete(iid):
    """删除保司档案（若已挂产品合约或资料，先拦下）。"""
    it = Insurer.query.get_or_404(iid)
    if CrmProductContract.query.filter_by(insurer_id=iid).count():
        flash('该保司下已有产品合约，不能删除')
        return redirect(url_for('crm.insurers'))
    if CrmProductDoc.query.filter_by(insurer_id=iid).count():
        flash('该保司下已有产品资料，不能删除')
        return redirect(url_for('crm.insurers'))
    InsurerLinkman.query.filter_by(owner_type='insurer', owner_id=iid).delete()
    for b in InsurerBranch.query.filter_by(insurer_id=iid).all():
        InsurerLinkman.query.filter_by(owner_type='branch', owner_id=b.id).delete()
    db.session.delete(it)
    db.session.commit()
    flash('保司档案已删除')
    return redirect(url_for('crm.insurers'))


@bp.route('/crm/insurers/fetch', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_fetch():
    """按保司全称抓取基础信息并回填（四十一改）。

    数据源：① 金融监管总局许可证库（免费，抓许可证号 + 批准设立日期）；
            ② 天眼查开放平台（需在数据源配置页填 token，抓工商字段）。
    默认**只补空**，勾选「覆盖已有值」才刷新已填字段。
    """
    iid = request.form.get('iid')
    it = Insurer.query.get(int(iid)) if (iid or '').isdigit() else None
    if not it:
        flash('保司不存在')
        return redirect(url_for('crm.insurers'))
    cfg = _fetch_cfg()
    if cfg.get('nfra') != 'on' and not (cfg.get('tyc') == 'on' and cfg.get('token')):
        flash('还没有可用的数据源 —— 请先到「数据源配置」开启监管总局许可证库，或填写天眼查 token')
        return redirect(url_for('crm.insurer_source'))
    overwrite = request.form.get('overwrite') == '1'
    filled, msgs = _fetch_fill(it, cfg, overwrite=overwrite)
    if filled:
        db.session.commit()
        flash('已抓取并回填 %d 项：%s' %
              (len(filled), '、'.join('%s=%s' % (k, v) for k, v in filled)))
    else:
        db.session.rollback()
        flash('本次没有可回填的字段（已填的不覆盖）')
    for m in msgs:
        flash(m)
    return redirect(url_for('crm.insurer_detail', iid=it.id))


@bp.route('/crm/insurers/fetch-all', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_fetch_all():
    """批量抓取：把档案里**指定字段为空**的保司逐个跑一遍（四十一改）。

    只跑缺项，已填满的跳过；每条之间间隔 FETCH_SLEEP 秒，不并发。
    """
    cfg = _fetch_cfg()
    if cfg.get('nfra') != 'on' and not (cfg.get('tyc') == 'on' and cfg.get('token')):
        flash('还没有可用的数据源 —— 请先到「数据源配置」开启监管总局许可证库，或填写天眼查 token')
        return redirect(url_for('crm.insurer_source'))
    overwrite = request.form.get('overwrite') == '1'
    todo = []
    for r in Insurer.query.order_by(Insurer.id).all():
        if overwrite:
            todo.append(r)
        # 缺项判定：许可证号 / 成立日期 / 许可证批准日期 任一为空就算缺
        elif (not (r.license_no or '').strip() or not (r.establish_date or '').strip()
              or not (r.license_date or '').strip()):
            todo.append(r)
        elif cfg.get('tyc') == 'on' and cfg.get('token') and not (r.credit_code or '').strip():
            todo.append(r)
    if not todo:
        flash('所有保司的关键字段都已填写，无需批量抓取（要强制刷新请勾选「覆盖已有值」）')
        return redirect(url_for('crm.insurers'))

    # 二次过滤：1 小时内试过、仍然缺字段的都跳过 —— 这类基本是分支机构上千家的大保司
    #   （国寿 1.6 万条、人保 1.18 万条），监管站每页只给 10 条，重试也不会有结果，
    #   跳过它们省时间也省限流额度。想强制再试就勾「覆盖已有值」。
    if not overwrite:
        now = datetime.now()
        fresh, recent = [], []
        for r in todo:
            t = (r.fetch_try_at or '').strip()
            skip = False
            if t:
                try:
                    if (now - datetime.strptime(t, '%Y-%m-%d %H:%M:%S')).total_seconds() < 3600:
                        skip = True
                except Exception:
                    skip = False
            (recent if skip else fresh).append(r)
        if not fresh:
            flash('要抓的都已在 1 小时内试过（抓不到的多是分支机构上千家的大保司，'
                  '监管站每页只回 10 条翻不到总公司）。手动填一下许可证号即可，'
                  '或勾选「覆盖已有值」强制重试')
            return redirect(url_for('crm.insurers'))
        todo = fresh
    else:
        recent = []

    # 一次最多跑 FETCH_BATCH_MAX 家：监管站限流太狠，一次打十几发必然被限 5 分钟。
    # 按 fetch_try_at 升序排队（没试过的排最前），这样不会每次都卡在前几家。
    todo.sort(key=lambda r: ((r.fetch_try_at or ''), r.id))
    batch = todo[:FETCH_BATCH_MAX]
    left = max(0, len(todo) - len(batch))

    ok, changed, fails = 0, 0, []
    for i, it in enumerate(batch):
        msgs = []
        try:
            filled, msgs = _fetch_fill(it, cfg, overwrite=overwrite)
            # 无论成功与否都记一次尝试时间（批量按它轮转排队）
            it.fetch_try_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if filled:
                changed += 1
            ok += 1
            real_err = [m for m in msgs if '未匹配到' in m or '失败' in m or '一页只回' in m]
            if real_err:
                fails.append('%s：%s' % (it.name, real_err[0]))
        except Exception as e:
            fails.append('%s：%s' % (it.name, e))
            msgs = []
        # 监管站限流（"您的操作过于频繁，请五分钟后再试"）→ 立刻停，别把 IP 打到黑名单
        if any('过于频繁' in m for m in msgs):
            fails.append('已触发监管站限流，批量抓取提前停止（剩余的请 5 分钟后再点一次）')
            break
        if i < len(batch) - 1:
            time.sleep(FETCH_SLEEP)
    db.session.commit()
    flash('批量抓取完成：处理 %d 家，其中 %d 家有字段被回填' % (ok, changed))
    if left:
        flash('还有 %d 家待抓，歇 1 分钟再点一次「批量抓取缺项」即可（已抓到的会自动跳过）' % left)
    if recent:
        flash('另有 %d 家 1 小时内试过仍缺字段，本次跳过（要强制重试请勾选「覆盖已有值」）' % len(recent))
    if fails:
        flash('以下 %d 家未能抓全：%s' % (len(fails), '；'.join(fails[:5]) +
                                     ('…' if len(fails) > 5 else '')))
    return redirect(url_for('crm.insurers'))


@bp.route('/crm/insurers/source', methods=['GET', 'POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_source():
    """保司档案 · 外部数据源配置（四十一改）。"""
    row = CrmFetchConfig.query.order_by(CrmFetchConfig.id).first()
    if row is None:
        row = CrmFetchConfig(id=1)
        db.session.add(row)
        db.session.commit()
    if request.method == 'POST':
        row.nfra_enabled = 'on' if request.form.get('nfra_enabled') else 'off'
        row.tyc_enabled = 'on' if request.form.get('tyc_enabled') else 'off'
        row.tyc_token = (request.form.get('tyc_token') or '').strip()
        row.updated_by = session.get('user_id')
        row.updated_at = datetime.utcnow()
        db.session.commit()
        flash('数据源配置已保存')
        return redirect(url_for('crm.insurer_source'))
    return render_template('crm/insurer_source.html', row=row,
                           active='insurers', back_flag='insurers')


@bp.route('/crm/insurers/source/test', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_source_test():
    """连通性自测：拿一个真实保司名各打一次接口，报结果（四十一改）。"""
    cfg = _fetch_cfg()
    name = (request.form.get('test_name') or '').strip() or '复星联合健康保险股份有限公司'
    if cfg.get('nfra') == 'on':
        hit, total, err = _nfra_query(name)
        if err:
            flash('监管总局许可证库：失败 —— %s' % err)
        elif hit:
            flash('监管总局许可证库：正常 —— 命中「%s」，许可证号 %s，批准设立 %s'
                  % (hit.get('fullName'), hit.get('certCode'), hit.get('setDate')))
        elif total > 10:
            flash('监管总局许可证库：接口正常，但「%s」有 %d 条分支机构、一页只回 10 条，'
                  '首页没找到总公司那条' % (name, total))
        else:
            flash('监管总局许可证库：接口正常（共 %d 条候选），但没匹配到「%s」的总公司记录'
                  % (total, name))
    if cfg.get('tyc') == 'on':
        if not cfg.get('token'):
            flash('天眼查：已开启但未填 token')
        else:
            r, err = _tyc_query(name, cfg['token'])
            if err:
                flash('天眼查：失败 —— %s' % err)
            else:
                flash('天眼查：正常 —— %s / 信用代码 %s'
                      % (r.get('name'), r.get('creditCode')))
    return redirect(url_for('crm.insurer_source'))


@bp.route('/crm/insurers/export')
@login_required
@permission_required('crm.view')
def insurer_export():
    """导出保司档案为 Excel。"""
    headers = ['保司全称', '保司简称', '公司类型', '统一社会信用代码', '工商注册号', '保险许可证号',
               '税号', '法定代表人', '注册资本', '成立日期', '工商营业地址', '联系电话', '官网',
               '合作状态', '我方对接人', '对接电话', '经营范围', '数据来源', '最后更新', '备注',
               # 四十一改：追加在**最后一列**，前面列序一律不动（导入按列序取值，插中间会错位）
               '许可证批准日期']
    data = []
    for r in Insurer.query.order_by(Insurer.id).all():
        lms = _linkmen('insurer', r.id)
        # 联络人已改一对多：导出时把全部姓名 / 电话分别用「、」拼接（无则回退 legacy 单值）
        cp = '、'.join([x.name for x in lms if x.name]) or (r.contact_person or '')
        cph = '、'.join([x.phone for x in lms if x.phone]) or (r.contact_phone or '')
        data.append([r.name, r.short_name or '', _name_of(INSURER_TYPES, r.insurer_type),
                     r.credit_code or '', r.reg_no or '', r.license_no or '', r.tax_no or '',
                     r.legal_person or '', r.reg_capital or '', r.establish_date or '',
                     r.address or '', r.phone or '', r.website or '',
                     _name_of(INSURER_STATUS, r.status), cp, cph,
                     r.business_scope or '', _name_of(INSURER_SOURCE, r.source),
                     r.data_updated_at or '', r.note or '', r.license_date or ''])
    return _xlsx_response('保司档案', '保司档案台账', headers, data)


@bp.route('/crm/insurers/import', methods=['POST'])
@login_required
@permission_required('crm.insurer.manage')
def insurer_import():
    """从 Excel 批量导入保司档案（按「保司全称」去重，已存在则跳过）。"""
    fs = request.files.get('file')
    if not fs or not (fs.filename or '').strip():
        flash('请选择要导入的 Excel 文件')
        return redirect(url_for('crm.insurers'))
    if not fs.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('仅支持 .xlsx 格式')
        return redirect(url_for('crm.insurers'))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(fs, data_only=True)
        ws = wb.active
        added = 0
        skipped = 0
        for ri, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
            if not row:
                continue
            vals = [('' if v is None else str(v).strip()) for v in row]
            name = vals[0] if vals else ''
            if not name:
                continue
            def g(i):
                return vals[i] if i < len(vals) else ''
            tmap = {v: k for k, v in INSURER_TYPES}
            # 十六改：导入同样走唯一性校验（全称 / 简称 / 统一社会信用代码）
            if _dup_insurer(name, g(1), g(3)):
                skipped += 1
                continue
            smap = {v: k for k, v in INSURER_STATUS}
            it = Insurer(
                name=name, short_name=g(1),
                insurer_type=tmap.get(g(2), 'life'), credit_code=g(3), reg_no=g(4),
                license_no=g(5), tax_no=g(6), legal_person=g(7), reg_capital=g(8),
                establish_date=g(9), address=g(10), phone=g(11), website=g(12),
                status=smap.get(g(13), 'coop'), contact_person=g(14), contact_phone=g(15),
                business_scope=g(16), source='import',
                data_updated_at=datetime.now().strftime('%Y-%m-%d'), note=g(19) if len(vals) > 19 else '',
                # 四十一改：第 21 列（索引 20）＝许可证批准日期；老模板没有这列就留空
                license_date=g(20) if len(vals) > 20 else '')
            db.session.add(it)
            db.session.flush()
            # 导入的"我方对接人"顺带建成一条联络人（多值用「、」分隔则拆开建多条）
            nm = g(14)
            ph = g(15)
            if nm:
                names = [x.strip() for x in nm.split('、') if x.strip()]
                phones = [x.strip() for x in ph.split('、') if x.strip()]
                for k, n in enumerate(names):
                    db.session.add(InsurerLinkman(
                        owner_type='insurer', owner_id=it.id, sort_no=k, name=n,
                        phone=phones[k] if k < len(phones) else ''))
            added += 1
        db.session.commit()
        flash('导入完成：新增 %d 条，跳过已存在 %d 条' % (added, skipped))
    except Exception as e:
        db.session.rollback()
        flash('导入失败：%s' % e)
    return redirect(url_for('crm.insurers'))


# ========== 十六改（2026-10-02）：唯一性校验 ==========
# 威哥口径：① 保司基础信息唯一；② 同一保司 + 同一产品 + 同一缴别 只允许录入一次，
#   不允许同一款产品反复录。规则开关存 CrmUniqueRule 表，后台「唯一性规则」页可改。
# ⚠️ 口径红线：总总 / 分分是**两个独立口径**，同一产品两个口径各录一次属正常业务
#   （分分的产品与缴别还是从总总带出来的），故默认只在「同一口径内」判重；
#   真要连跨口径也拦，去设置页打开 `contract_cross_side`。

UNIQUE_RULES = [
    ('insurer_name', '保司全称唯一', 'insurer'),
    ('insurer_short', '保司简称唯一', 'insurer'),
    ('insurer_credit', '统一社会信用代码唯一', 'insurer'),
    ('branch_name', '省分公司全称唯一', 'branch'),
    ('branch_short', '省分公司简称唯一', 'branch'),
    ('product_code', '同一保司下产品代码唯一', 'contract'),
    ('contract_product', '同一保司+产品 仅一条合约', 'contract'),
    ('tier_period', '同一保司+产品+缴别 仅一条', 'contract'),
    ('contract_cross_side', '跨口径（总总/分分）也唯一', 'contract'),
]


def _uniq_on(code):
    """某条唯一性规则是否启用。

    ⚠️ 查不到规则行时**默认按启用**处理（零配置 / 老库也安全），并顺手补种开关行。
    """
    try:
        r = CrmUniqueRule.query.filter_by(code=code).first()
    except Exception:
        r = None
    if r is None:
        return True
    return (r.enabled or 'on') == 'on'


def _nk(s):
    """唯一性比对用的归一化：去空白、全角括号转半角、忽略大小写。

    同一家保司常被录成「中国人寿保险股份有限公司」/「中国人寿 保险股份有限公司」，
    肉眼是一家、字符串却是两家 —— 这里统一捏成一把钥匙。
    """
    s = (s or '').strip()
    if not s:
        return ''
    s = s.replace('（', '(').replace('）', ')').replace('　', '')
    return re.sub(r'\s+', '', s).lower()


def _dup_insurer(name, short_name, credit_code, exclude_id=0):
    """保司唯一性：全称 / 简称 / 统一社会信用代码。无冲突返回 ''，有则返回提示语。"""
    n_nm, n_sh, n_cr = _nk(name), _nk(short_name), _nk(credit_code)
    if not (n_nm or n_sh or n_cr):
        return ''
    for r in Insurer.query.all():
        if r.id == exclude_id:
            continue
        if n_nm and _uniq_on('insurer_name') and _nk(r.name) == n_nm:
            return '保司全称已存在：%s（不能重复录入）' % r.name
        if n_sh and _uniq_on('insurer_short') and _nk(r.short_name) == n_sh:
            return '保司简称已存在：%s → %s（不能重复录入）' % (r.short_name, r.name)
        if n_cr and _uniq_on('insurer_credit') and _nk(r.credit_code) == n_cr:
            return '统一社会信用代码已存在：%s → %s（不能重复录入）' % (r.credit_code, r.name)
    return ''


def _dup_branch(iid, name, short_name, exclude_id=0):
    """省分公司唯一性：同一保司旗下的全称 / 简称。无冲突返回 ''。"""
    n_nm, n_sh = _nk(name), _nk(short_name)
    if not (n_nm or n_sh):
        return ''
    for r in InsurerBranch.query.filter_by(insurer_id=iid).all():
        if r.id == exclude_id:
            continue
        if n_nm and _uniq_on('branch_name') and _nk(r.name) == n_nm:
            return '该保司下已有同名省分公司：%s（不能重复录入）' % r.name
        if n_sh and _uniq_on('branch_short') and _nk(r.short_name) == n_sh:
            return '该保司下已有同简称省分公司：%s → %s（不能重复录入）' % (r.short_name, r.name)
    return ''


def _contract_side_key(sign_level):
    """把 sign_level 折成口径键：hq 总公司口径（tt/tf） / br 分公司口径（ff）。"""
    return LEVEL_SIDE.get(sign_level or '', 'hq')


def _dup_contract(iid, product_name, side, branch_id=None, exclude_cid=0, coverage=''):
    """合约级唯一性：同一保司 + 同一产品 + 同一保障期限（+ 同一口径）只允许一条合约。

    分公司口径按**省分公司**分开设键——同一产品在广东分公司、江苏分公司各签一份是正常业务。
    打开 `contract_cross_side` 后连总总 / 分分之间也互斥。
    ⚠️ 三十五改：**保障期限是唯一键的一部分** —— 达尔文12号的「保终身」和「保至70岁」
       各建一条合约是正常业务，不能被判重拦下。
    """
    if not iid or not _nk(product_name) or not _uniq_on('contract_product'):
        return ''
    cross = _uniq_on('contract_cross_side')
    # 四十四改：AI 草稿不参与判重 —— 否则一条还没审完的草稿会把真合约挡在门外
    for r in CrmProductContract.query.filter_by(insurer_id=iid).filter(
            db.func.coalesce(CrmProductContract.ai_draft, 0) == 0).all():
        if r.id == exclude_cid:
            continue
        if _nk(r.product_name) != _nk(product_name):
            continue
        if _nk(getattr(r, 'coverage_term', '') or '') != _nk(coverage):
            continue
        if not cross and _contract_side_key(r.sign_level) != side:
            continue
        if side == 'br' and (r.branch_id or 0) != (branch_id or 0):
            continue
        return ('该产品（保障期限：%s）在本口径下已有合约：%s（%s）' %
                ((coverage or '未设'), r.product_name or '', r.code or ''))
    return ''


def _dup_tiers(iid, product_name, side, periods, branch_id=None, exclude_cid=0, coverage=''):
    """缴别级唯一性：同一保司 + 同一产品 + 同一保障期限 + 同一缴别（+ 同一口径）只允许录一次。

    防止「拆成两条合约各录一部分缴别」绕过合约级校验。
    """
    if not iid or not _nk(product_name) or not periods or not _uniq_on('tier_period'):
        return ''
    want = [_nk(p) for p in periods if _nk(p)]
    if not want:
        return ''
    cross = _uniq_on('contract_cross_side')
    hits = {}
    # 四十四改：同样跳过 AI 草稿
    for r in CrmProductContract.query.filter_by(insurer_id=iid).filter(
            db.func.coalesce(CrmProductContract.ai_draft, 0) == 0).all():
        if r.id == exclude_cid:
            continue
        if _nk(r.product_name) != _nk(product_name):
            continue
        if _nk(getattr(r, 'coverage_term', '') or '') != _nk(coverage):   # 三十五改
            continue
        rs = _contract_side_key(r.sign_level)
        if not cross and rs != side:
            continue
        if rs == 'br' and side == 'br' and (r.branch_id or 0) != (branch_id or 0):
            continue
        for t in _contract_tiers(r.id):
            k = _nk(t.pay_period)
            if k in want:
                hits.setdefault(k, []).append('%s（%s）' % (t.pay_period, r.code or ''))
    if not hits:
        return ''
    for p in periods:
        if _nk(p) in hits:
            return ('缴别「%s」已录入过：%s —— 同一保司+产品+缴别只能录一次' %
                    (p, '、'.join(hits[_nk(p)][:3])))
    return ''


# ---------- 四十一改（2026-10-29）：保司档案 · 外部数据源一键抓取 ----------
# 威哥诉求：保司全称填好，其余（信用代码 / 法人 / 注册资本 / 成立日期 / 地址 / 许可证号）
#   不想一条条手填，要系统自动抓。
#
# 实测结论（2026-10-29，别再重复踩）：
#   ✅ 国家金融监督管理总局「金融许可证信息（保险机构）」xkz.nfra.gov.cn
#      公开接口，可编程：能拿「保险许可证号 / 批准设立日期 / 规范全称」。
#      注意两点：① 接口前缀 baseUrl 是**每次访问首页随机的 6 位字符串**（防爬），
#                每次抓取都要先 GET 首页解析出来；② 查询接口要带齐一堆空参数
#                （organNo/fatherOrganNo/organNoZhiju/organType/branchType/orgLevel/address/flowNo），
#                少一个就永远返回 0 条（踩过，排查很久）。
#   ❌ 天眼查 tianyancha.com / 企查查 qcc.com / 爱企查 aiqicha.baidu.com / 启信宝 qixin.com
#      匿名访问一律 302 跳登录，或返回加密混淆的 JS 反爬页；官方开放平台 API 按次收费需 token。
#   ❌ 信用中国 412、国家企业信用信息公示系统 gsxt 521（滑块验证）。
#   → 所以工商字段只有两条路：**买天眼查开放平台 token**（本页可填，填了即全自动），
#     或者手工填。系统不提供任何绕过验证码/登录的抓取。
#
# 合规口径：只用政府公开查询系统的公开接口，低频访问（批量时每条间隔 0.8s）、
#   不并发、不绕过任何验证。抓到的值一律**只补空**（不覆盖已有人工核准数据），
#   除非显式勾选「覆盖已有值」。
NFRA_HOME = 'https://xkz.nfra.gov.cn/bx/'
NFRA_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
TYC_API = 'http://open.api.tianyancha.com/services/open/ic/baseinfo/normal'
FETCH_SLEEP = 3.0                  # 批量抓取时每条之间的间隔（秒）
# 监管站限流极严：实测连续请求十几次就返回
#   {"success":false,"msg":"您的操作过于频繁，请五分钟后再试。"}
# 所以**一次抓取只发 1 个查询请求、不翻页**（翻页只会更快被限），
# 批量时每条之间隔 3 秒。命中不了就如实提示，不去硬试。
NFRA_MAX_PAGES = 1
FETCH_BATCH_MAX = 5                # 一次批量最多跑几家（监管站限流，分批点更稳）

# ---------- 四十一改 A+C（2026-10-29 威哥拍板）----------
# A：抓取方案就按现状（监管站免费源为主，天眼查 token 可选、不采购），不再折腾。
# C：抓不全的补一个「去天眼查查这家」直达链接 —— 分支机构上千家的大保司
#    （国寿、人保等）监管站翻不到总公司，让威哥自己搜公司名太费事，
#    点一下就到天眼查的这家公司页面，把 6 个工商字段抄回来即可。
TYC_WEB_SEARCH = 'https://www.tianyancha.com/search?key='
TYC_OPEN_HOME = 'https://open.tianyancha.com/'    # 以后要买 token 的申请入口


def _tyc_search_url(name):
    """天眼查网页版按公司名搜索的直达链接（中文按 UTF-8 百分号编码）。"""
    return TYC_WEB_SEARCH + urllib.parse.quote((name or '').strip())


@bp.app_template_global('tyc_url')
def _tpl_tyc_url(name):
    """模板里直接写 {{ tyc_url(保司全称) }} 即得天眼查搜索链接。"""
    return _tyc_search_url(name)


@bp.app_template_global('tyc_open_home')
def _tpl_tyc_open_home():
    """天眼查开放平台首页（申请 token 的入口）。"""
    return TYC_OPEN_HOME


def _fetch_cfg():
    """取数据源配置（全局单例）→ {'nfra': on/off, 'tyc': on/off, 'token': str}。

    监管站是免费公共源，**空值也按 on 处理**（开箱即用）；天眼查必须显式开启 + 有 token。
    """
    row = CrmFetchConfig.query.order_by(CrmFetchConfig.id).first()
    if row is None:
        return {'nfra': 'on', 'tyc': 'off', 'token': ''}
    return {'nfra': row.nfra_enabled if row.nfra_enabled in ('on', 'off') else 'on',
            'tyc': row.tyc_enabled if row.tyc_enabled in ('on', 'off') else 'off',
            'token': (row.tyc_token or '').strip()}


def _http_text(url, headers=None, timeout=15, jar=None, retry=2):
    """GET 一个 URL → 解码后的文本。jar 传 CookieJar 就在会话里带上 cookie。

    政府站偶尔抽风超时（实测爱心人寿那一次等满 15s 后断），故默认重试 2 次。
    """
    h = {'User-Agent': NFRA_UA}
    h.update(headers or {})
    last = None
    for i in range(max(1, retry + 1)):
        try:
            req = urllib.request.Request(url, headers=h)
            if jar is not None:
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode('utf-8', 'ignore')
        except Exception as e:
            last = e
            if i < retry:
                time.sleep(1.2)
    raise last


def _nfra_page(base, jar, name, start, limit=10):
    """取一页（接口 limit 实际被服务端固定为 10 条，传大了也只回 10 条）。"""
    q = urllib.parse.urlencode({
        'useState': 3, 'organNo': '', 'fatherOrganNo': '', 'organNoZhiju': '',
        'organType': '', 'branchType': '', 'orgLevel': '',
        'fullName': name, 'address': '', 'flowNo': '', 'start': start, 'limit': limit})
    txt = _http_text(NFRA_HOME + base + 'getLicence.do?' + q,
                     {'Referer': NFRA_HOME, 'X-Requested-With': 'XMLHttpRequest'}, jar=jar)
    return json.loads(txt)


def _nfra_query(name, max_pages=NFRA_MAX_PAGES, exact_only=False):
    """按保司全称查监管总局许可证库，翻页找「总公司」那条 → (hit, total, err)。

    ⚠️ 硬限制（2026-10-29 实测）：getLicence.do 的 limit 传 10/50/200 都只回 **10 条**，
       所以大保司（国寿 1.6 万条分支机构）靠翻页根本翻不到总公司记录。
       故这里最多翻 NFRA_MAX_PAGES 页，翻不到就如实返回「未匹配」，不瞎猜。
    """
    jar = http.cookiejar.CookieJar()
    try:
        home = _http_text(NFRA_HOME, jar=jar)
    except Exception as e:
        return None, 0, '监管站访问失败（%s）' % e
    m = re.search(r"var baseUrl = '([^']*)'", home)
    base = m.group(1) if m else ''
    if not base:
        return None, 0, '监管站页面结构变化，未取到接口前缀'
    try:
        d = _nfra_page(base, jar, name, 0)
    except Exception as e:
        return None, 0, '监管站查询失败（%s）' % e
    # 限流是常态（连续几十次就被限 5 分钟），必须单独识别，不能当成「未匹配」
    if not d.get('success', True):
        return None, 0, '监管站：%s' % (d.get('msg') or '返回失败')
    total = d.get('total') or 0
    datas = d.get('datas') or []
    # 四十二改：省分公司（12 位分支证号）只认「名称完全相同」——首页那条「6 位证号唯一」
    #   的兜底只适用于总公司，对分支用会误命中某家支公司，故 exact_only=True 时关掉。
    hit = _nfra_pick(datas, name, strict=not exact_only)
    if hit:
        return hit, total, ''
    pages = min(max_pages, (total + 9) // 10)
    for p in range(1, pages):
        time.sleep(2.0)                          # 翻页也要给足间隔，否则秒限
        try:
            dd = _nfra_page(base, jar, name, p * 10)
        except Exception:
            continue
        hit = _nfra_pick(dd.get('datas') or [], name, strict=False)   # 后续页只认名称完全相同
        if hit:
            return hit, total, ''
    return None, total, ''


def _nfra_pick(datas, name, strict=False):
    """从查询结果里挑「总公司（法人机构）」那条。

    规则：① 规范化后名称**完全相同**优先（永远认这一条）；
         ② strict=True（只在第一页做）时，退一步认「证号长度为 6」且只有唯一一条的
            —— 监管编码里 6 位＝法人机构，分公司是 12 位；
         ③ 都不中返回 None（不发散猜，避免把分公司当总公司写进档案）。
    """
    tgt = _nk(name)
    exact = [x for x in datas if _nk(x.get('fullName')) == tgt]
    if exact:
        return exact[0]
    if strict:
        heads = [x for x in datas if len(str(x.get('certCode') or '')) == 6]
        if len(heads) == 1:
            return heads[0]
    return None


def _tyc_query(name, token):
    """天眼查开放平台「企业基本信息」→ (result dict, err)。token 为空直接跳过。"""
    if not token:
        return None, '未配置天眼查 token'
    url = TYC_API + '?' + urllib.parse.urlencode({'keyword': name})
    try:
        txt = _http_text(url, {'Authorization': token}, timeout=15)
        d = json.loads(txt)
    except Exception as e:
        return None, '天眼查调用失败（%s）' % e
    if str(d.get('error_code')) != '0':
        return None, '天眼查返回：%s' % (d.get('reason') or d.get('error_code'))
    return (d.get('result') or {}), ''


def _ms_to_date(v):
    """天眼查的成立日期是毫秒时间戳 → 'YYYY-MM-DD'；已是字符串就原样截断。"""
    s = str(v or '').strip()
    if not s:
        return ''
    if s.isdigit() and len(s) >= 10:
        try:
            return datetime.fromtimestamp(int(s[:10])).strftime('%Y-%m-%d')
        except Exception:
            return ''
    return s[:10]


def _fetch_fill(it, cfg=None, overwrite=False):
    """抓取单个保司并回填 → (filled:list[(字段中文名, 值)], msgs:list[str])。

    overwrite=False（默认）：**只补空字段**，已有人工核准的值一律不覆盖。
    """
    cfg = cfg or _fetch_cfg()
    filled, msgs = [], []

    def put(col, label, val):
        if not val:
            return
        val = str(val).strip()
        if not val:
            return
        old = (getattr(it, col) or '').strip()
        if old and not overwrite:
            return
        if old == val:
            return
        # 按列长度裁一下，防止外部数据超长把 SQLite 写崩（text 列不裁）
        cap = {'credit_code': 32, 'reg_no': 32, 'license_no': 64, 'tax_no': 32,
               'legal_person': 32, 'reg_capital': 32, 'establish_date': 16,
               'license_date': 16,
               'address': 200, 'phone': 32, 'website': 128, 'note': 200}.get(col)
        if cap:
            val = val[:cap]
        setattr(it, col, val)
        filled.append((label, val))

    # ① 监管总局许可证库（免费）—— 许可证号 + 批准设立日期 + 名称规范性校验
    if cfg.get('nfra') == 'on':
        hit, total, err = _nfra_query(it.name)
        if err:
            msgs.append(err)
        elif not hit:
            if total > 10:
                msgs.append('监管站「%s」有 %d 条分支机构记录、一页只回 10 条，'
                            '首页没找到总公司那条 —— 点「去天眼查查这家」抄一下许可证号即可'
                            % (it.name, total))
            else:
                msgs.append('监管站未匹配到「%s」的总公司许可证记录（共 %d 条候选），'
                            '校验一下全称是否与监管登记一致；也可点「去天眼查查这家」人工核对'
                            % (it.name, total))
        else:
            cname = (hit.get('fullName') or '').strip()
            if cname and _nk(cname) != _nk(it.name):
                msgs.append('监管站登记全称为「%s」，与档案名称不一致，请核对' % cname)
            put('license_no', '保险许可证号', hit.get('certCode'))
            # 四十一改：监管口径「批准设立日期」单独存 license_date，
            # 不写 establish_date（那是工商成立日期，两者差几天到几个月）
            put('license_date', '许可证批准日期', (hit.get('setDate') or '')[:10])
            if not filled:
                msgs.append('监管站已匹配，但档案里这些字段都已有值（未覆盖）')

    # ② 天眼查开放平台（需 token）—— 工商字段
    if cfg.get('tyc') == 'on' and cfg.get('token'):
        r, err = _tyc_query(it.name, cfg['token'])
        if err:
            msgs.append(err)
        elif r:
            put('credit_code', '统一社会信用代码', r.get('creditCode'))
            put('legal_person', '法定代表人', r.get('legalPersonName'))
            put('reg_capital', '注册资本', r.get('regCapital'))
            put('establish_date', '成立日期', _ms_to_date(r.get('estiblishTime')))
            put('address', '工商营业地址', r.get('regLocation'))
            put('business_scope', '经营范围', r.get('businessScope'))
            put('phone', '联系电话', r.get('phoneNumber'))
            if not (it.website or '').strip() and r.get('websiteList'):
                put('website', '官网', str(r.get('websiteList')).split(';')[0])
    elif cfg.get('tyc') == 'on' and not cfg.get('token'):
        msgs.append('天眼查已开启但未填 token，工商字段（信用代码/法人/注册资本/地址/经营范围）未抓取；'
                    '可点「去天眼查查这家」手工抄回来')

    if filled:
        it.source = 'fetch'
        it.data_updated_at = datetime.now().strftime('%Y-%m-%d')
    return filled, msgs


# ---------- 四十二改（2026-10-29）：省分公司也能一键抓取 ----------
# 威哥要求：省级分公司相关的信息也要能自动爬，别再一条条手填。
# 实测（2026-10-29）：分公司全称在监管站是**精确命中**的 ——
#   查「平安健康保险股份有限公司广东分公司」→ total=1，直接拿到 certCode=000084440000
#   （12 位分支证号）、setDate=2007-09-12，比查总公司还准（总公司常被上万条分支淹掉）。
# 所以分公司这条路是真正「填个全称就能自动抓全」，反而比总公司好用。
# ⚠️ 只认名称完全相同：分公司的 12 位证号不能套用总公司那条「6 位证号唯一」兜底，
#    否则会误命中某家支公司。
BRANCH_MISS_COLS = ('license_no', 'license_date', 'credit_code',
                    'principal', 'address', 'establish_date')


def _no_source(cfg):
    """没有任何可用数据源（监管站关闭 且 天眼查没 token）→ True。"""
    return cfg.get('nfra') != 'on' and not (cfg.get('tyc') == 'on' and cfg.get('token'))


def _fetch_fill_branch(b, cfg=None, overwrite=False):
    """抓取单个**省分公司**并回填 → (filled, msgs)（四十二改）。

    口径与总公司完全一致：**只补空**，overwrite=True 才覆盖已填值。
    ① 监管站（免费）：许可证号 + 许可证批准日期（监管口径，单独一列）
    ② 天眼查 token：信用代码 / 负责人 / 营业地址 / 电话 / 成立日期
    """
    cfg = cfg or _fetch_cfg()
    filled, msgs = [], []

    def put(col, label, val):
        if not val:
            return
        val = str(val).strip()
        if not val:
            return
        old = (getattr(b, col) or '').strip()
        if old and not overwrite:
            return
        if old == val:
            return
        cap = {'credit_code': 32, 'license_no': 64, 'principal': 32, 'phone': 32,
               'address': 200, 'establish_date': 16, 'license_date': 16}.get(col)
        if cap:
            val = val[:cap]
        setattr(b, col, val)
        filled.append((label, val))

    # ① 监管总局许可证库 —— 分公司按全称精确命中
    if cfg.get('nfra') == 'on':
        hit, total, err = _nfra_query(b.name, exact_only=True)
        if err:
            msgs.append(err)
        elif not hit:
            msgs.append('监管站未匹配到「%s」的许可证记录（共 %d 条候选）—— '
                        '分局改名（如「广东分公司」/「广东省分公司」）或已改制，'
                        '可点「去天眼查查这家」人工核对' % (b.name, total))
        else:
            cname = (hit.get('fullName') or '').strip()
            if cname and _nk(cname) != _nk(b.name):
                msgs.append('监管站登记全称为「%s」，与档案名称不一致，请核对' % cname)
            put('license_no', '许可证号', hit.get('certCode'))
            put('license_date', '许可证批准日期', (hit.get('setDate') or '')[:10])
            if not filled:
                msgs.append('监管站已匹配，但许可证号 / 批准日期都已有值（未覆盖）')

    # ② 天眼查开放平台 —— 工商字段（分公司也有信用代码与负责人）
    if cfg.get('tyc') == 'on' and cfg.get('token'):
        r, err = _tyc_query(b.name, cfg['token'])
        if err:
            msgs.append(err)
        elif r:
            put('credit_code', '统一社会信用代码', r.get('creditCode'))
            put('principal', '负责人', r.get('legalPersonName'))
            put('address', '营业地址', r.get('regLocation'))
            put('phone', '联系电话', r.get('phoneNumber'))
            put('establish_date', '成立日期', _ms_to_date(r.get('estiblishTime')))
    elif cfg.get('tyc') == 'on' and not cfg.get('token'):
        msgs.append('天眼查已开启但未填 token，工商字段（信用代码/负责人/地址/成立日期）未抓取；'
                    '可点「去天眼查查这家」手工抄回来')

    return filled, msgs


# 连续新建分公司时不要每条都去打监管站（会秒限流）—— 距上次自动抓不满这个秒数就跳过，
# 让威哥保存后走「批量抓取缺项」按钮（那里有 3 秒间隔 + 1 小时豁免，更稳）。
AUTO_FETCH_GAP = 10
_LAST_AUTO_FETCH = [0.0]


def _auto_fetch_branch(b):
    """新建省分公司后自动抓一次（四十二改）—— 只补空、出错静默，绝不打断保存流程。

    分公司全称在监管站是精确命中的（实测 total=1），所以新建完基本就自动填全了，
    威哥不用再点一次按钮。限流 / 查不到都只是不填，不报错。
    """
    try:
        cfg = _fetch_cfg()
        if _no_source(cfg):
            return
        if (b.license_no or '').strip() and (b.credit_code or '').strip():
            return
        if time.time() - _LAST_AUTO_FETCH[0] < AUTO_FETCH_GAP:
            return                 # 刚抓过，跳过（防连续录入把监管站打到限流）
        _LAST_AUTO_FETCH[0] = time.time()
        filled, msgs = _fetch_fill_branch(b, cfg)
        b.fetch_try_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.session.commit()
        if filled:
            flash('已自动抓取 %d 项：%s' %
                  (len(filled), '、'.join('%s=%s' % (k, v) for k, v in filled)))
        for m in msgs:
            if '过于频繁' in m or '未匹配到' in m:
                flash(m)
    except Exception:
        db.session.rollback()      # 抓取失败绝不影响已保存的分公司档案


def _fetch_branch_batch(overwrite=False, cfg=None, iid=None):
    """省分公司批量抓取跑一轮 → dict（四十二改）。

    与保司批量同口径：只补空 → 1 小时内试过的跳过 → 按 fetch_try_at 轮转排队
    → 一次最多 FETCH_BATCH_MAX 家 → 每条间隔 FETCH_SLEEP 秒 → 遇限流立即停。
    iid 传了保司 id 就只抓这家保司旗下的分公司（列表页按保司筛选时的语义）。
    返回 {ok, changed, left, recent, fails, stopped}
    """
    cfg = cfg or _fetch_cfg()
    todo, recent = [], []
    q = InsurerBranch.query
    if iid:
        q = q.filter(InsurerBranch.insurer_id == iid)
    for r in q.order_by(InsurerBranch.id).all():
        if not overwrite:
            miss = [c for c in BRANCH_MISS_COLS if not (getattr(r, c) or '').strip()]
            if not miss:
                continue
            t = (r.fetch_try_at or '').strip()
            if t:
                try:
                    if (datetime.now() - datetime.strptime(t, '%Y-%m-%d %H:%M:%S')).total_seconds() < 3600:
                        recent.append(r)
                        continue
                except Exception:
                    pass
        todo.append(r)
    if not todo:
        return {'ok': 0, 'changed': 0, 'left': 0, 'recent': len(recent),
                'fails': [], 'stopped': False, 'empty': True}
    todo.sort(key=lambda r: ((r.fetch_try_at or ''), r.id))
    batch = todo[:FETCH_BATCH_MAX]
    left = max(0, len(todo) - len(batch))

    ok, changed, fails, stopped = 0, 0, [], False
    for i, b in enumerate(batch):
        msgs = []
        try:
            filled, msgs = _fetch_fill_branch(b, cfg, overwrite=overwrite)
            b.fetch_try_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if filled:
                changed += 1
            ok += 1
            real_err = [m for m in msgs if '未匹配到' in m or '失败' in m]
            if real_err:
                fails.append('%s：%s' % (b.name, real_err[0]))
        except Exception as e:
            fails.append('%s：%s' % (b.name, e))
            msgs = []
        if any('过于频繁' in m for m in msgs):
            stopped = True
            break
        if i < len(batch) - 1:
            time.sleep(FETCH_SLEEP)
    db.session.commit()          # 循环里只 setattr，统一在这里落库（漏了这句＝白抓）
    return {'ok': ok, 'changed': changed, 'left': left, 'recent': len(recent),
            'fails': fails, 'stopped': stopped, 'empty': False}


# ---------- 三十八改（2026-10-29）：产品代码 自动生成 / 唯一性 ----------
# 威哥口径：
#   ① 产品代码**留空** → 系统自动生成一个（保存之后可以在编辑页手工改）；
#   ② 产品代码是**产品级**属性、不是合约级 —— 同一保司同一产品的多个保障期限
#      （达尔文12号「保终身」/「保至70岁」各一条合约）**必须共用一个代码**；
#   ③ 唯一性：同一保司下，一个代码只能属于一个产品。不同保司互不影响
#      （各家保司自己的编码体系本来就容易撞号，跨保司强行唯一没有业务意义）。
PC_PREFIX = 'P'                    # 自动生成代码前缀：P0001 / P0002 …


def _pc_norm(s):
    """产品代码归一化：去空白 + 转大写（判重时不区分大小写）。"""
    return (s or '').strip().upper()


def _pc_used_codes():
    """全库已占用的产品代码集合（合约主表 + 组合组件），返回归一化后的大写集合。"""
    used = set()
    for (v,) in db.session.query(CrmProductContract.product_code).all():
        v = _pc_norm(v)
        if v:
            used.add(v)
    for (v,) in db.session.query(CrmContractComponent.product_code).all():
        v = _pc_norm(v)
        if v:
            used.add(v)
    return used


def _gen_product_code():
    """自动生成产品代码：P + 4 位流水（接着全库最大号往下发，跳过已占用的）。"""
    used = _pc_used_codes()
    n = 1
    for v in used:
        m = re.match(r'^P(\d+)$', v)
        if m:
            n = max(n, int(m.group(1)) + 1)
    while ('P%04d' % n) in used:
        n += 1
    return 'P%04d' % n


def _product_code_of(iid, product_name, exclude_cid=0):
    """同一产品（同保司 + 同产品名，**不分保障期限、不分总总/分分口径**）已登记的产品代码。

    这就是「达尔文12号 保终身 / 保至70岁」两条合约能自动共用一个代码的原因 ——
    产品名相同即视为同一个产品。找不到返回 ''。
    """
    if not iid or not _nk(product_name):
        return ''
    for r in CrmProductContract.query.filter_by(insurer_id=iid).all():
        if r.id == exclude_cid:
            continue
        if _nk(r.product_name) != _nk(product_name):
            continue
        v = (r.product_code or '').strip()
        if v:
            return v
    return ''


def _pc_owner(code, iid, product_name='', exclude_cid=0):
    """该产品代码是否已被「本保司下的**别的产品**」占用 → 返回占用者描述，未被占用返回 ''。

    ⚠️ 同一产品（同保司 + 同产品名）出现同一个代码是**正常业务**，不算占用 —— 直接放行。
    """
    c = _pc_norm(code)
    if not c or not iid or not _uniq_on('product_code'):
        return ''
    for r in CrmProductContract.query.filter_by(insurer_id=iid).all():
        if r.id == exclude_cid or _pc_norm(r.product_code) != c:
            continue
        if _nk(r.product_name) == _nk(product_name):
            continue                      # 同一产品 → 允许共用同一个代码
        return ('产品代码「%s」已被本保司其他产品占用：%s（合约 %s）'
                % (code, r.product_name or '', r.code or ''))
    # 组合组件的产品代码同样占位（组件保司为空＝跟主保司走）
    for cp in CrmContractComponent.query.all():
        if _pc_norm(cp.product_code) != c:
            continue
        cid_iid = cp.insurer_id
        if not cid_iid:
            _p = CrmProductContract.query.get(cp.contract_id)
            cid_iid = (_p.insurer_id if _p else None)
        if cid_iid != iid:
            continue
        if _nk(cp.product_name) == _nk(product_name):
            continue
        return ('产品代码「%s」已被组合组件占用：%s'
                % (code, cp.product_name or ''))
    return ''


def _comp_iid_of(cp, contract_map):
    """组件的归属保司 id：组件自己填了就用它，没填（＝同主保司）回落到合约的保司。"""
    return cp.insurer_id or contract_map.get(cp.contract_id)


def _comp_code_of(iid, comp_name, exclude_comp_id=0):
    """同一保司 + 同一组件产品名**历史已登记过**的组件代码；没有则返回 ''。

    三十九改（2026-10-29）：定制类产品的组件（主险/附加险）都是保司现成的产品，
    历史上录过一次之后就固定了 —— 下次再建合约、再挂同一个组件，应当自动带出这个代码，
    而且**不允许改**（前端只读 + 后端强制沿用），避免同一个组件被写成两个代码。
    """
    if not iid or not _nk(comp_name):
        return ''
    cmap = {c.id: c.insurer_id for c in CrmProductContract.query.all()}
    for cp in CrmContractComponent.query.all():
        if cp.id == exclude_comp_id:
            continue
        if _nk(cp.product_name) != _nk(comp_name):
            continue
        if _comp_iid_of(cp, cmap) != iid:
            continue
        v = (cp.product_code or '').strip()
        if v:
            return v
    return ''


def _comp_code_map():
    """{保司id(字符串): {组件名归一化: 代码}} —— 喂给前端做「自动带出 + 只读」。

    键统一用 `_nk()` 归一化（去空白 / 全角括号转半角 / 小写），前端用同一把钥匙查表，
    这样「复星联合优选K重疾」与「复星联合 优选k重疾（...）」不会因为空格大小写对不上而漏带。
    同名的多个组件取第一个非空代码（它们本来就应该同码）。
    """
    out = {}
    cmap = {c.id: c.insurer_id for c in CrmProductContract.query.all()}
    for cp in CrmContractComponent.query.all():
        nm = _nk(cp.product_name)
        code = (cp.product_code or '').strip()
        if not nm or not code:
            continue
        iid = _comp_iid_of(cp, cmap)
        if not iid:
            continue
        out.setdefault(str(iid), {}).setdefault(nm, code)
    return out


def _sync_comp_code(iid, comp_name, new_code, exclude_comp_id=0):
    """组件代码被手工改了 → 把全库「同保司 + 同组件名」的组件记录一起改成新码，返回改动条数。

    定制类产品的组件（主险 / 附加险）本身就是保司的现成产品，同一个组件会在多个定制合约里
    反复出现。威哥口径：在一个合约里改一次，后台**一键同步**所有引用它的记录，
    不用逐个合约进去改（也就不会出现同一组件两个代码）。
    """
    new_code = (new_code or '').strip()
    if not iid or not _nk(comp_name) or not new_code:
        return 0
    n = 0
    cmap = {c.id: c.insurer_id for c in CrmProductContract.query.all()}
    for cp in CrmContractComponent.query.all():
        if cp.id == exclude_comp_id:
            continue
        if _nk(cp.product_name) != _nk(comp_name):
            continue
        if _comp_iid_of(cp, cmap) != iid:
            continue
        if (cp.product_code or '').strip() != new_code:
            cp.product_code = new_code
            n += 1
    return n


def _resolve_product_code(iid, product_name, typed='', exclude_cid=0):
    """决定本次保存要写入的产品代码 → (code, err)。

    · 留空：先沿用同产品已登记的代码（保证「一个产品一个代码」）；没有就自动生成。
    · 手工填：先判唯一性，被本保司别的产品占用则返回 err（拦下，不让脏数据进库）。
    """
    typed = (typed or '').strip()
    if typed:
        err = _pc_owner(typed, iid, product_name, exclude_cid=exclude_cid)
        if err:
            return '', '产品代码未通过唯一性校验 —— ' + err
        return typed, ''
    have = _product_code_of(iid, product_name, exclude_cid=exclude_cid)
    if have:
        return have, ''
    return _gen_product_code(), ''


def _sync_product_code(iid, product_name, code, exclude_cid=0):
    """手工改了产品代码 → 把同产品的其他合约一起改成一致的，返回改动条数。

    产品代码是产品级属性，同一产品的所有合约必须同码；改了主险那条，
    同产品下的其他保障期限 / 分公司口径那条也跟着改，不需要人工再逐条改一遍。
    """
    if not code or not iid or not _nk(product_name) or not _uniq_on('product_code'):
        return 0
    n = 0
    for r in CrmProductContract.query.filter_by(insurer_id=iid).all():
        if r.id == exclude_cid:
            continue
        if _nk(r.product_name) != _nk(product_name):
            continue
        if (r.product_code or '').strip() != code:
            r.product_code = code
            n += 1
    return n


def _spread_product_fields(c):
    """四十九改（2026-10-29）：总口径改了产品级属性 → **实时下发**给分口径同产品合约。

    背景：四十六改让分口径在**录入时**跟随总总（前端只读 + 后端强制），但总总事后
    修改不会传播到已经存在的分口径记录 —— 威哥遇到的就是「总总补了爱心人寿的渠道，
    分分列表还显示 —」。这里在总口径保存成功后顺手把产品级属性刷给下级合约。

    下发范围：总-总 → 总-分 + 分-分；总-分 → 分-分（同级之间不互相覆盖）。
    下发字段：销售渠道 / 险种类型 / 缴费类型 / 定制类标识。
    ⚠️ 保障期限**不下发**：它是一条合约一个值（同一产品可能「保终身」「保至70岁」各一条），
       分口径可能刻意选了另一个，强行覆盖会改错；产品编码走既有的 _sync_product_code()。
    返回被改动的合约条数。
    """
    if not c or c.sign_level not in ('tt', 'tf'):
        return 0
    targets = ('ff',) if c.sign_level == 'tf' else ('tf', 'ff')
    vals = [('sale_channel', (c.sale_channel or '').strip()),
            ('insurance_type', (c.insurance_type or '').strip()),
            ('pay_type', (c.pay_type or '').strip())]
    _ic = 1 if getattr(c, 'is_custom', 0) else 0
    n = 0
    for r in CrmProductContract.query.filter(
            CrmProductContract.insurer_id == c.insurer_id,
            CrmProductContract.sign_level.in_(targets)).all():
        if r.id == c.id or _nk(r.product_name) != _nk(c.product_name):
            continue
        hit = False
        for f, v in vals:
            if v and (getattr(r, f, '') or '').strip() != v:
                setattr(r, f, v)
                hit = True
        if getattr(r, 'is_custom', 0) != _ic:
            r.is_custom = _ic
            hit = True
        if hit:
            n += 1
    return n


def _insurer_short_map():
    """{insurer_id: 保司简称} —— 简称缺失回落全称。一次查全表，避免列表页 N+1。"""
    return {i.id: (i.short_name or i.name or '') for i in Insurer.query.all()}


def _branch_short_map():
    """{branch_id: 省分公司简称} —— 简称缺失回落全称。"""
    return {b.id: (b.short_name or b.name or '') for b in InsurerBranch.query.all()}


def _coverage_terms():
    """保障期限下拉词库 = 内置常用项（COVERAGE_TERMS）＋ 库里已用过的自定义值（去重保序）。

    三十五改：表单用 `<input list>` + datalist 呈现，既能选也能直接手打新值；
    用户手工输入的值保存后会出现在这里，下次就能直接选。
    """
    out = list(COVERAGE_TERMS)
    try:
        rows = db.session.query(CrmProductContract.coverage_term).distinct().all()
    except Exception:
        return out
    for (v,) in rows:
        v = (v or '').strip()
        if v and v not in out:
            out.append(v)
    return out


def _uniq_issues():
    """体检：列出库里**已经**违反唯一性规则的记录（只读，不改数据）。

    规则是「往后拦」，历史脏数据得让人看得见才清得掉 —— 设置页底部列出违规项。
    """
    out = []

    def _add(group, text):
        out.append(dict(group=group, text=text))

    # ① 保司：全称 / 简称 / 统一社会信用代码
    if any(_uniq_on(c) for c in ('insurer_name', 'insurer_short', 'insurer_credit')):
        rows = Insurer.query.all()
        for field, label, code in [('name', '全称', 'insurer_name'),
                                   ('short_name', '简称', 'insurer_short'),
                                   ('credit_code', '统一社会信用代码', 'insurer_credit')]:
            if not _uniq_on(code):
                continue
            bucket = {}
            for r in rows:
                k = _nk(getattr(r, field, ''))
                if k:
                    bucket.setdefault(k, []).append(r)
            for k, lst in bucket.items():
                if len(lst) > 1:
                    _add('insurer', '保司%s重复：%s —— 共 %d 条（%s）'
                         % (label, getattr(lst[0], field), len(lst),
                            '、'.join((x.name or '')[:18] for x in lst[:4])))
    # ② 省分公司：同一保司旗下全称 / 简称
    if any(_uniq_on(c) for c in ('branch_name', 'branch_short')):
        for field, label, code in [('name', '全称', 'branch_name'),
                                   ('short_name', '简称', 'branch_short')]:
            if not _uniq_on(code):
                continue
            bucket = {}
            for r in InsurerBranch.query.all():
                k = (_nk(getattr(r, field, '')), r.insurer_id or 0)
                if k[0]:
                    bucket.setdefault(k, []).append(r)
            for k, lst in bucket.items():
                if len(lst) > 1:
                    _add('branch', '省分公司%s重复：%s —— 共 %d 条（%s）'
                         % (label, getattr(lst[0], field), len(lst),
                            '、'.join((x.name or '')[:18] for x in lst[:4])))
    # ②.5 产品代码（三十八改）：一个代码只属于一个产品 / 一个产品只有一个代码
    if _uniq_on('product_code'):
        by_code, by_prod = {}, {}
        for r in CrmProductContract.query.all():
            pc = _pc_norm(r.product_code)
            if not pc:
                continue
            iid = r.insurer_id or 0
            by_code.setdefault((pc, iid), []).append(r)
            by_prod.setdefault((iid, _nk(r.product_name)), set()).add((r.product_code or '').strip())
        for (pc, _i), lst in by_code.items():
            names = []
            for x in lst:
                nm = (x.product_name or '').strip()
                if nm not in names:
                    names.append(nm)
            if len(names) > 1:
                _add('contract', '产品代码重复：%s 被 %d 个产品共用（%s）'
                     % (lst[0].product_code, len(names), '、'.join(names[:4])))
        for (_i, nk), codes in by_prod.items():
            if nk and len(codes) > 1:
                nm = ''
                for r in CrmProductContract.query.all():
                    if _nk(r.product_name) == nk:
                        nm = r.product_name or ''
                        break
                _add('contract', '同一产品出现多个代码：%s —— %s'
                     % (nm, '、'.join(sorted(codes))))
    # ③ 合约：同一保司 + 产品（+ 口径 / 省分公司）被重复录入，且缴别有交集
    if _uniq_on('contract_product') or _uniq_on('tier_period'):
        cross = _uniq_on('contract_cross_side')
        gmap = {}
        for c in CrmProductContract.query.all():
            pk = _nk(c.product_name)
            if not pk:
                continue
            key = (c.insurer_id or 0, pk)
            if not cross:
                sd = _contract_side_key(c.sign_level)
                key += (sd, (c.branch_id or 0) if sd == 'br' else 0)
            gmap.setdefault(key, []).append(c)
        for key, lst in gmap.items():
            if len(lst) > 1 and _uniq_on('contract_product'):
                _add('contract', '同一保司+产品重复录入：%s —— %d 条合约（%s）'
                     % (lst[0].product_name, len(lst),
                        '、'.join((x.code or '') for x in lst[:4])))
            if not _uniq_on('tier_period'):
                continue
            seen = {}
            for c in lst:
                # 二十八改：定制类产品按组件录费率，同一缴别（如 20年交）会在主险、附加险下各出现一次，
                # 这是正常业务，不算「缴别重复」——体检时直接跳过定制类合约。
                if getattr(c, 'is_custom', 0):
                    continue
                for t in _contract_tiers(c.id):
                    seen.setdefault(_nk(t.pay_period), []).append((t.pay_period, c.code or ''))
            for k, items in seen.items():
                if len(items) > 1:
                    _add('contract', '缴别重复：%s · %s 出现在 %d 条记录里（%s）'
                         % (lst[0].product_name, items[0][0], len(items),
                            '、'.join(x[1] for x in items[:4])))
    return out


@bp.route('/crm/unique-rules')
@login_required
@permission_required('crm.view')
def unique_rules():
    """唯一性规则设置：查看 / 开关各条「不允许重复录入」的校验规则。"""
    rows = CrmUniqueRule.query.order_by(CrmUniqueRule.sort_no, CrmUniqueRule.id).all()
    have = {r.code: r for r in rows}
    # 表里缺行时（老库 / 表刚建）按常量补齐展示，避免设置页空白
    show = []
    for i, (code, nm, gp) in enumerate(UNIQUE_RULES, 1):
        r = have.get(code)
        show.append(dict(code=code, name=(r.name if r else nm),
                         group=(r.group if r else gp),
                         desc=(r.desc or '' if r else ''),
                         enabled=((r.enabled or 'on') if r else 'on'),
                         sort_no=(r.sort_no if r is not None else i)))
    return render_template('crm/unique_rules.html', rows=show,
                           groups=[('insurer', '保司档案'), ('branch', '省分公司'),
                                   ('contract', '产品合约')],
                           issues=_uniq_issues(),
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/unique-rules/save', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def unique_rules_save():
    """保存唯一性规则开关（勾选=启用=拦重复，不勾=放行）。"""
    n = 0
    for code, nm, gp in UNIQUE_RULES:
        r = CrmUniqueRule.query.filter_by(code=code).first()
        if r is None:
            r = CrmUniqueRule(code=code, name=nm, group=gp, sort_no=len(UNIQUE_RULES))
            db.session.add(r)
        r.enabled = 'on' if request.form.get('u_' + code) else 'off'
        r.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        n += 1
    db.session.commit()
    flash('唯一性规则已保存（共 %d 条）' % n)
    return redirect(url_for('crm.unique_rules'))


# ===================== ② 产品合约 =====================

def _fill_contract(row, form):
    """把表单值写进合约**主表**对象（新建/编辑共用）——只写产品级信息。

    二期起：缴别 / 费用政策 / RYC 由 _save_tiers 写入子表 CrmContractTier，
    继续率考核引用规则库 CrmPersistRule；主表旧列（first_income / tt_* / ff_* /
    ryc_* / 三个合计 / 继续率系数）**一律不再写入**，仅为兼容一期数据保留。
    """
    row.insurer_id = int(form.get('insurer_id') or 0) or None
    it = Insurer.query.get(row.insurer_id) if row.insurer_id else None
    row.insurer_name = it.name if it else _f('insurer_name')
    row.insurer_short = it.short_name if it else ''
    # 省分公司（总-分 / 分-分 签约时指明是哪个省分）
    bid = (form.get('branch_id') or '').strip()
    row.branch_id = int(bid) if bid.isdigit() else None
    br = InsurerBranch.query.get(row.branch_id) if row.branch_id else None
    row.branch_name = br.name if br else ''
    row.sign_level = _f('sign_level')
    row.product_name = _f('product_name')
    # 三十八改：产品代码先按表单原值写，最终值由 _contract_form 里的
    # `_resolve_product_code()` 定稿（留空自动生成 / 同产品沿用 / 手工值判唯一）后覆盖。
    row.product_code = _f('product_code')
    # 三十五改：保障期限（保终身 / 保至70岁 / 自定义）——一条合约一个口径
    row.coverage_term = _f('coverage_term')
    # 二十七改2：定制类产品（组合产品）标记 —— 勾选=1，常规初始化不勾=0
    row.is_custom = 1 if form.get('is_custom') else 0
    # 四十三改：销售渠道 —— 线下 / 网销，留空＝未指定（历史数据不动）
    row.sale_channel = _f('sale_channel')
    row.insurance_type = _f('insurance_type')
    row.status = _f('status') or 'active'
    row.pay_type = _f('pay_type')
    # 方案政策期间已下放到 CrmContractPlan（每条方案自带起止日），主表 tt_*/ff_*_start/end 不再写入
    # 继续率考核规则（引用规则库，多产品共用一条）
    rid = (form.get('persist_rule_id') or '').strip()
    row.persist_rule_id = int(rid) if rid.isdigit() else None
    rule = CrmPersistRule.query.get(row.persist_rule_id) if row.persist_rule_id else None
    row.persist_rule_name = rule.name if rule else ''
    # 十五改：文字类方案已归并回「四、」按方案存（`CrmContractPlan.plan_text`）。
    # 这里的 `misc_plan_text` 是十三改遗留的**合约级**文本框字段，表单已不再提交它 ——
    # 每次保存都清空，确保老数据迁移完成后不会「复活」；列本身保留、不迁移。
    row.misc_plan_text = ''
    row.note = _f('note')


@bp.route('/crm/contracts/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_new():
    """（旧入口，已废弃）统一跳转到「新建总公司合约协议」。

    五改（2026-10-02）：合约已按总公司 / 分公司两个口径分录，
    老链接重定向到新入口，避免书签与历史 URL 失效。
    """
    return redirect(url_for('crm.hq_contract_new'))


# ========== 五改（2026-10-02）：合约按「总公司 / 分公司」两个口径独立分录 ==========
# 入口分工（威哥 2026-10-02 拍板）：
#   /crm/hq-contracts      总公司合约协议 —— 签约级别 总-总 / 总-分
#   /crm/branch-contracts  分公司合约协议 —— 签约级别 分-分，必挂某省分公司
#   /crm/contracts          产品合约（十五改）：两边合并后的统一查询 + 导出 + 页签入口
# 两者共用同一张物理表 CrmProductContract，只靠 sign_level 归口，历史数据零迁移。

def _branch_rows(iid=None):
    """省分公司下拉数据（传 insurer_id 时只取该保司旗下的）。"""
    q = InsurerBranch.query
    if iid:
        q = q.filter_by(insurer_id=iid)
    return q.order_by(InsurerBranch.province, InsurerBranch.name).all()


def _branch_json():
    """全部省分公司序列化给前端，供「选保司 → 联动分公司」下拉使用。"""
    return json.dumps([{'id': b.id, 'insurer_id': b.insurer_id,
                        'text': '%s%s' % (b.name,
                                          ('（%s）' % b.province) if b.province else '')}
                       for b in _branch_rows()], ensure_ascii=False)


def _product_json():
    """保司 id → 已录产品名称列表，供分公司协议表单做产品候选下拉（允许手输兜底）。"""
    m = {}
    for r in CrmProductContract.query.order_by(CrmProductContract.product_name).all():
        n = (r.product_name or '').strip()
        if not n:
            continue
        k = str(r.insurer_id or 0)
        m.setdefault(k, [])
        if n not in m[k]:
            m[k].append(n)
    return json.dumps(m, ensure_ascii=False)


def _product_options(iid):
    """某保司已录过的产品名列表（含总总 / 分分合约），供「产品匹配」页做多选。

    七改第二步：管理员选保司 → 系统自动弹出产品名称 → 多选后与规则建立关联。
    """
    if not iid:
        return []
    try:
        m = json.loads(_product_json())
    except Exception:
        return []
    return m.get(str(iid), [])


def _hq_products(exclude_cid=0):
    """总公司合约协议（tt / tf）已有产品 → 缴别 + 产品级字段。

    六改：分公司合约协议的产品名**只能从总总已录产品里下拉筛选**（不可手填），
    选完产品后自动把总总已录的缴别带出来，用户在缴别下只填分分系数即可。
    七改：顺带把总总已录的**产品编码 / 险种类型**一起带出，分分公司不必再手工选。
    四十六改（2026-10-29）：再带出**保障期限 / 销售渠道 / 缴费类型**——
    这五项都是「产品级属性」，分口径不该另填一遍，一律跟总总走（前端只读＋后端强制）。
    返回 dict：{保司id: {产品名称: {periods, code, type, coverage, channel, pay_type}}}
    """
    m = {}
    for c in CrmProductContract.query.filter(
            CrmProductContract.sign_level.in_(SIDE_LEVEL_CODES['hq']),
            # ⚠️ 四十六改：AI 草稿（ai_draft=1）不算已录产品。曾因草稿里的
            #   「每年」缴别混进分分下拉、且分公司页勾选框是 disabled，导致威哥「删不掉」。
            db.func.coalesce(CrmProductContract.ai_draft, 0) == 0).all():
        if exclude_cid and c.id == exclude_cid:
            continue
        iid = str(c.insurer_id or 0)
        nm = (c.product_name or '').strip()
        if not nm:
            continue
        item = m.setdefault(iid, {}).setdefault(nm, {
            'periods': [], 'code': '', 'type': '', 'coverage': '',
            'coverages': [], 'channel': '', 'pay_type': '', 'custom': 0,
            'comps': [], 'from_tt': False})
        # 定制类标识：任一条总口径合约勾了「定制类产品」就带出去
        # （分口径表单没有这个勾选框，不同步的话分分列表就丢了「定制类」标）
        if getattr(c, 'is_custom', 0):
            item['custom'] = 1
        # 保障期限是**一条合约一个值**（三十五改）：同一产品可能同时有保终身 / 保至70岁，
        # 所以收集成集合，分口径只在「唯一」时才锁定，多个时让用户从里面挑。
        _cov0 = (c.coverage_term or '').strip()
        if _cov0 and _cov0 not in item['coverages']:
            item['coverages'].append(_cov0)
        # 产品级字段：总-总（tt）优先于总-分（tf）—— 分口径本来就该跟总总一致
        _is_tt = (c.sign_level == 'tt')
        for _k, _v in (('code', c.product_code), ('type', c.insurance_type),
                       ('coverage', c.coverage_term), ('channel', c.sale_channel),
                       ('pay_type', c.pay_type)):
            _v = (_v or '').strip()
            if not _v:
                continue
            if _is_tt or not item[_k]:
                item[_k] = _v
        if _is_tt:
            item['from_tt'] = True
        # 四十七改（2026-10-29）：定制类产品的**组合组件**也一起带出去 ——
        #   分口径（分-分）不再自己建组件，选完产品就按总总的组件结构生成只读组件区，
        #   费率按组件分别录。否则分分把定制产品当普通产品平铺录，报表里就认不出主险/附加险。
        _cps = _components_of(c.id) if getattr(c, 'is_custom', 0) else []
        _pp_of_comp = {}
        for t in _contract_tiers(c.id):
            p = (t.pay_period or '').strip()
            if p and p not in item['periods']:
                item['periods'].append(p)
            # 组件自己的缴别（同一组件可能有多个缴别）
            if t.component_id and p and p not in _pp_of_comp.setdefault(t.component_id, []):
                _pp_of_comp[t.component_id].append(p)
        if _cps and (_is_tt or not item['comps']):
            item['comps'] = [{'name': (x.product_name or '').strip(),
                              'code': (x.product_code or '').strip(),
                              'type': (x.insurance_type or '').strip(),
                              'note': (x.note or '').strip(),
                              'periods': _pp_of_comp.get(x.id, [])}
                             for x in _cps if (x.product_name or '').strip()]
    # 缴别按 PAY_PERIODS 的正序重排（趸交 → 2年交 → … → 30年交 → 交至X岁）
    order = {p: i for i, p in enumerate(PAY_PERIODS)}
    for iid in m:
        for nm in m[iid]:
            it = m[iid][nm]
            it['periods'].sort(key=lambda x: order.get(x, 999))
            # coverage 只保留「唯一值」；多个保障期限时置空，由前端让用户自己挑
            it['coverage'] = it['coverages'][0] if len(it['coverages']) == 1 else ''
    return m


def _hq_of(iid, pname, exclude_cid=0):
    """单个产品在总公司口径已录的产品级字段 —— 分口径「强制跟随」用。

    找不到（该产品只在天分公司口径录过、或根本是新产品）就返回 {}，
    调用方据此决定「不锁定、允许自行填写」。
    """
    if not iid or not pname:
        return {}
    try:
        return ((_hq_products(exclude_cid) or {}).get(str(iid), {}) or {}).get(pname) or {}
    except Exception:
        return {}


def _hq_product_json(exclude_cid=0):
    """_hq_products() 的 JSON 序列化版（喂给前端做二级联动）。"""
    return json.dumps(_hq_products(exclude_cid), ensure_ascii=False)


def _rule_scope(r):
    """把规则的适用范围 JSON 列解析成 python list（坏数据兜底空列表）。"""
    def _load(s):
        try:
            v = json.loads(s or '[]')
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return _load(r.product_names), _load(r.pay_periods)


def _rule_branch_ids(r):
    """规则的适用省分公司 id 列表（七改：多选，存 JSON 数组文本）。"""
    if not r:
        return []
    try:
        v = json.loads(r.branch_ids or '[]')
    except Exception:
        return []
    return [int(x) for x in v if str(x).strip().isdigit()]


def _match_rules(c):
    """七改：按「规则侧登记的适用范围」反向匹配本合约适用哪些继续率规则。

    匹配条件（七改口径）：
      ① **保险公司必须一致**（保险公司是必选字段；规则不选保司＝不匹配任何合约）
      ② 保司分公司：规则多选的分公司里包含本合约分公司即命中（规则不选＝不限）
      ③ 产品名称：规则「产品匹配」页关联的产品里包含本合约产品即命中
         （规则未关联产品＝不限产品）
      ⚠️ 缴别已不参与匹配（七改起只用产品建立关联）。
    ⚠️ 本函数定义位置**必须在路由装饰器之前**（紧邻 _rule_scope），
       否则会被上面的 @bp.route / @login_required 误包装成视图函数。
    """
    out = []
    for r in CrmPersistRule.query.filter_by(status='on').all():
        # 二十四改（2026-10-28）：规则已挂到产品合约上（contract_id）——
        #   这种规则只属于那一条合约，不再走「保司/分公司/产品」范围匹配。
        if r.contract_id:
            if r.contract_id == c.id:
                out.append(r)
            continue
        # ↓ 二十四改之前建的独立规则（contract_id 为空）：保持原有范围匹配逻辑
        if not r.insurer_id or (c.insurer_id or 0) != r.insurer_id:
            continue
        bids = _rule_branch_ids(r)
        if bids and (c.branch_id or 0) not in bids:
            continue
        pros, _pers = _rule_scope(r)
        if pros and (c.product_name or '').strip() not in pros:
            continue
        out.append(r)
    return out


# ============ 二十四改（2026-10-28）：继续率规则跟着产品合约走 ============
# 威哥口径：继续率不再单独建模块，改为在录产品合约时一并登记（结构照保司协议原表）：
#   继续率区间 × 缴别 → R13 / R25 / R37 / R49 奖金率（%），外加长期服务奖系数与倒扣机制。
# 存储映射：一条合约 = 一条 CrmPersistRule（挂 contract_id）；
#           每个「缴别 × 周期 × 区间」= 一条 CrmPersistRuleItem（cycle + pay_period）。
# 展示结构：缴别分块 → 块内每个继续率区间一行 → 行内填各周期奖金率（附件表就是这个样子）。

PERSIST_BONUS_CYCLES = PERSIST_CYCLES       # R13 / R25 / R37 / R49


def _persist_cycle_label(cy):
    """「13个月继续率考核标准」→「R13」（表头短标）。"""
    return 'R' + ''.join(ch for ch in (cy or '') if ch.isdigit())


def _persist_of(cid):
    """取某产品合约挂的继续率规则（没有则 None）。"""
    if not cid:
        return None
    return CrmPersistRule.query.filter_by(contract_id=cid).first()


def _rule_matrix(r, items=None):
    """把一条继续率规则的档位整理成「区间行 × 缴别列」矩阵（合约表单与只读页共用）。

    二十五改（2026-10-28）：区间只定义一次（行），缴别展开成列。
    返回 [{'min','max','minop','maxop','text','b': {缴别: {周期: 系数}}, 'ls': {缴别: 系数}}]
    —— 同区间的 R13/R25/R37 合成一行，正好对上保司协议那张表；
    `text` 是渲染好的区间文本（如「88.0% ≤ 继续率 < 92.0%」）。
    items 可外部传入已查好的档位列表，避免列表页 N+1 查询。
    """
    if not r:
        return []
    its = items if items is not None else CrmPersistRuleItem.query.filter_by(rule_id=r.id) \
        .order_by(CrmPersistRuleItem.sort_no, CrmPersistRuleItem.id).all()
    rows, rmap = [], {}
    for it in its:
        p = it.pay_period or ''
        key = (round(it.rate_min or 0, 2), round(it.rate_max or 0, 2),
               it.min_op or '>=', it.max_op or '<')
        row = rmap.get(key)
        if row is None:
            row = dict(min=key[0], max=key[1], minop=key[2], maxop=key[3],
                       text=_fmt_range_vals(key[0], key[1], key[2], key[3]),
                       b={}, ls={})
            rmap[key] = row
            rows.append(row)
        if (it.cycle or '') == LS_CYCLE:
            row['ls'][p] = round(it.long_service_coef or 0, 2)
        else:
            row['b'].setdefault(p, {})[it.cycle or ''] = round(it.bonus_coef or 0, 2)
    return rows


def _persist_rows(cid):
    """取某产品合约的继续率矩阵（供合约表单渲染）—— 见 `_rule_matrix`。"""
    return _rule_matrix(_persist_of(cid))


def _matrix_periods(rows):
    """矩阵里出现的缴别，按 PAY_PERIODS 正序（未收录的排最后）。"""
    seen = []
    for r in rows:
        for p in list(r['b'].keys()) + list(r['ls'].keys()):
            if p and p not in seen:
                seen.append(p)
    return sorted(seen, key=lambda x: (PAY_PERIODS.index(x) if x in PAY_PERIODS else 99, x))


def _save_persist_of(c, form):
    """保存合约的继续率奖金率（全量替换：先删旧规则与档位，再按表单重建）。

    二十五改（2026-10-28）表格化填写；二十六改（2026-10-28）表格转置为「缴别做行、区间做列」，
    表单字段：
      pr_periods      **涉及继续率的缴别**（逗号分隔）—— 前端根据「三、」的缴别自动列出复选框，
                      不涉及继续率的缴别用户取消勾选，这里就只收勾上的那些（决定表格行）
      pr_idx          区间列序号（一列一个 hidden）
      pr_min_<i> / pr_max_<i> / pr_minop_<i> / pr_maxop_<i>   该列的区间上下限与运算符
      pr_p_<pi>_<ci>_<i>   第 pi 个缴别 × 第 ci 个周期（R13/R25/R37/R49）× 第 i 个区间的奖金率
      pr_ls_<pi>_<i>       第 pi 个缴别 × 第 i 个区间的长期服务奖系数
      pr_has_claw / pr_claw_line / pr_claw_base / pr_claw_std   倒扣机制（合约级一份）
    区间只定义一次，缴别在行上分；某缴别某格留空即「该缴别无此档政策」。
    整表空白（没填任何系数也没倒扣）→ 不建规则：该产品没有继续率奖金政策也很正常。
    返回写入的档位条数。
    """
    # ① 先清旧（全量替换，避免改了一行残留一行）
    old = _persist_of(c.id)
    if old:
        for it in CrmPersistRuleItem.query.filter_by(rule_id=old.id).all():
            db.session.delete(it)
        db.session.delete(old)
        db.session.flush()

    g = lambda n: (form.get(n) or '').strip()
    periods = [x.strip()[:32] for x in (g('pr_periods') or '').split(',') if x.strip()][:20]
    # 二十六改：科目勾选 —— 没勾的科目整块表格不出现，也就不会提交它的字段；
    # 这里再按 pr_awards 过滤一次，防止「勾了又取消」的残值被写进去。
    if 'pr_awards' in form:
        _aw = [x.strip() for x in (g('pr_awards') or '').split(',') if x.strip()]
        do_bonus, do_ls = ('bonus' in _aw), ('long_service' in _aw)
    else:
        do_bonus = do_ls = True      # 老表单（无 pr_awards 字段）兜底：都读
    rows = []                       # (缴别, 周期名, 下限, 上限, 下限符, 上限符, 奖金率, 长期服务奖)
    cycles_used, has_ls = set(), False
    for i in form.getlist('pr_idx'):
        lo = _to_num(g('pr_min_%s' % i))
        hi = _to_num(g('pr_max_%s' % i))
        mop = g('pr_minop_%s' % i) or '>='
        if mop not in dict(RANGE_OPS_MIN):
            mop = '>='
        xop = g('pr_maxop_%s' % i) or '<'
        if xop not in dict(RANGE_OPS_MAX):
            xop = '<'
        for pi, period in enumerate(periods):
            vals = []
            if do_bonus:
                for ci, cy in enumerate(PERSIST_BONUS_CYCLES):
                    v = _to_num(g('pr_p_%d_%d_%s' % (pi, ci, i)))
                    if v:
                        vals.append((cy, v))
                        cycles_used.add(cy)
            ls = _to_num(g('pr_ls_%d_%s' % (pi, i))) if do_ls else 0
            if ls:
                has_ls = True
            if not vals and not ls:
                continue            # 只填了区间没填系数 → 跳过，不产生垃圾档位
            for cy, v in vals:
                rows.append((period, cy, lo, hi, mop, xop, v, 0.0))
            if ls:
                rows.append((period, LS_CYCLE, lo, hi, mop, xop, 0.0, ls))

    # ② 倒扣机制（合约级一份）
    has_claw = bool(g('pr_has_claw'))
    claw_line = _to_num(g('pr_claw_line'))
    claw_base = g('pr_claw_base') or 'all'
    if claw_base not in dict(CLAWBACK_BASES):
        claw_base = 'all'
    claw_std = (form.get('pr_claw_std') or '').strip()

    if not rows and not has_claw and not claw_std:
        return 0                    # 什么都没填 → 该产品不设继续率政策

    cy_sorted = sorted(cycles_used, key=lambda x: PERSIST_CYCLES.index(x))
    awards = []
    if cy_sorted:
        awards.append('bonus')
    if has_ls:
        awards.append('long_service')
    r = CrmPersistRule(
        contract_id=c.id,
        insurer_id=c.insurer_id, insurer_name=c.insurer_name or '',
        product_names=json.dumps([c.product_name or ''], ensure_ascii=False),
        status='on',
        has_clawback=has_claw, clawback_line=claw_line,
        clawback_base=claw_base, clawback_std=claw_std,
        award_items=','.join(awards),
        cycles=json.dumps(cy_sorted, ensure_ascii=False),
        cycle=(cy_sorted[0] if cy_sorted else ''),
        name=_unique_rule_name('%s-继续率考核规则' % (c.product_name or c.code)),
        created_by=session.get('user_id'))
    db.session.add(r)
    db.session.flush()
    for n, (period, cy, lo, hi, mop, xop, bc, lsc) in enumerate(rows):
        db.session.add(CrmPersistRuleItem(
            rule_id=r.id, cycle=cy, pay_period=period,
            rate_min=lo, rate_max=hi, min_op=mop, max_op=xop,
            bonus_coef=bc, long_service_coef=lsc, sort_no=n))
    return len(rows)


def _side_stats(rows):
    """列表页用的缴别 / 方案概况（一次性取回，避免 N+1 查询）。

    十五改：方案已全部提到合约级 —— **文字类方案不挂缴别（`tier_id=None`）**，
    故方案一律按 `contract_id` 归集，不能再按 `tier_id` 取（那样会漏掉文字类方案，
    导致只有文字类方案的合约在列表里显示「0 方案」）。
    计数口径与 `_tier_stats` 一致：按 `_head_key` 去重后的方案条数。
    """
    by_cid = {}
    for tier in CrmContractTier.query.all():
        by_cid.setdefault(tier.contract_id, []).append(tier)
    plans_by_cid = {}
    for p in CrmContractPlan.query.all():
        plans_by_cid.setdefault(p.contract_id, []).append(p)
    stats = {}
    for r in rows:
        ts = by_cid.get(r.id) or []
        if not ts:
            # 一期存量合约（尚未平移明细）回退主表旧值，保证不出现空白
            stats[r.id] = {'n': 1 if (r.grand_total or 0) else 0, 'plans': 0, 'out': 0}
        else:
            heads = {_head_key(p) for p in (plans_by_cid.get(r.id) or [])}
            stats[r.id] = {'n': len(ts), 'plans': len(heads),
                           'out': len([k for k in heads if k[2] == 'out'])}
    return stats


def _contract_list(side):
    """三个入口共用的列表视图。

    side = 'hq'  总公司合约协议（总-总 / 总-分）
           'br'  分公司合约协议（分-分）
           'all' 产品合约（两边合并；十五改：原「合约总览」改名）
    ⚠️ 十五改：side='all' 的页面名统一叫**产品合约**（菜单已合并），
       总/分公司两个口径改为页内页签，URL 全部保留。
    """
    sname = dict(SIDE_LEVELS).get(side, '产品合约')
    kw = (request.args.get('kw') or '').strip()
    iid = (request.args.get('insurer_id') or '').strip()
    it = (request.args.get('type') or '').strip()
    bid = (request.args.get('branch_id') or '').strip()
    lv = (request.args.get('level') or '').strip()
    q = CrmProductContract.query
    # 四十四改（2026-10-29）：AI 识别出来的**待审草稿**不是正式合约 —— 列表一律排除，
    # 免得草稿混在真数据里被当真。草稿只在页面顶部提示条里出现，点进去审核或放弃。
    q = q.filter(db.func.coalesce(CrmProductContract.ai_draft, 0) == 0)
    if side in SIDE_LEVEL_CODES:
        q = q.filter(CrmProductContract.sign_level.in_(SIDE_LEVEL_CODES[side]))
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(CrmProductContract.product_name.like(like),
                            CrmProductContract.product_code.like(like),
                            CrmProductContract.insurer_name.like(like)))
    if iid.isdigit():
        q = q.filter(CrmProductContract.insurer_id == int(iid))
    if bid.isdigit():
        q = q.filter(CrmProductContract.branch_id == int(bid))
    if it:
        q = q.filter(CrmProductContract.insurance_type == it)
    if lv in ('tt', 'tf', 'ff'):
        q = q.filter(CrmProductContract.sign_level == lv)
    rows = q.order_by(CrmProductContract.id.desc()).all()
    # 十七改：到期状态筛选（expiring 临期 / expired 已过期）——状态是现算的，只能在 python 侧过滤
    st = (request.args.get('st') or '').strip()
    if st in ('expiring', 'expired'):
        rows = [r for r in rows if _expire_state(_contract_span(r)[1])[0]
                in (('danger', 'warn') if st == 'expiring' else ('expired',))]

    # KPI 一律按全库统计（含另一口径），方便用户知道整体盘子
    allrows = CrmProductContract.query.filter(
        db.func.coalesce(CrmProductContract.ai_draft, 0) == 0).all()
    # 四十四改：待审草稿单列出来（不进 KPI、不进列表），供页面顶部提示条使用
    drafts = (CrmProductContract.query
              .filter(CrmProductContract.ai_draft == 1)
              .order_by(CrmProductContract.id.desc()).all())
    kpi = {'total': len(allrows),
           'active': len([r for r in allrows if (r.status or '') == 'active']),
           'hq': len([r for r in allrows if _side_of(r.sign_level) == 'hq']),
           'br': len([r for r in allrows if _side_of(r.sign_level) == 'br']),
           'out': CrmContractPlan.query.filter_by(policy_scope='out').count()}
    if side in SIDE_LEVEL_CODES:
        # 单口径页面：本口径的规模 / 生效 / 协议外都只算自己，另两格给全库对照
        mine = [r for r in allrows if _side_of(r.sign_level) == side]
        kpi['total'] = len(mine)
        kpi['active'] = len([r for r in mine if (r.status or '') == 'active'])
        kpi['out'] = (CrmContractPlan.query.filter(
            CrmContractPlan.policy_scope == 'out',
            CrmContractPlan.contract_id.in_([r.id for r in mine])).count() if mine else 0)
    return render_template('crm/contract_list.html', rows=rows, kpi=kpi,
                           drafts=drafts,
                           stats=_side_stats(rows), side=side, sname=sname, tab=lv,
                           sids={r.id: _side_of(r.sign_level) for r in rows},
                           kw=kw, iid=iid, it=it, bid=bid,
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           branch_opts=_branch_rows(),
                           levels=(SIGN_LEVELS if side == 'all'
                                   else [x for x in SIGN_LEVELS if LEVEL_SIDE.get(x[0]) == side]),
                           types=INSURANCE_TYPES, dname=DNAME,
                           # 四十三改：销售渠道中文名（列表「渠道」列直接取名）
                           sale_names=dict(SALE_CHANNELS),
                           # 十六改：省分公司列也统一按**简称**展示（简称缺失回落全称）
                           bshort=_branch_short_map(),
                           # 十七改：合约到期提醒（红黄灯）——列表「有效期」列 + 顶部提醒条 + 系统总览
                           # ⚠️ 变量名用 est 不用 st：模板里 st 已被「缴别/方案统计」占用
                           est=st, expiry=_expiry_alerts(), ekpi=_expiry_kpi(),
                           spans={r.id: _contract_span(r) for r in rows},
                           estates={r.id: _expire_state(_contract_span(r)[1]) for r in rows},
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/contracts')
@login_required
@permission_required('crm.view')
def contracts():
    """产品合约：总公司合约协议 + 分公司合约协议 合并后的统一查询入口。

    十五改（2026-10-02）：原来的三个菜单（合约总览 / 总公司合约协议 / 分公司合约协议）
    与「继续率考核规则」合并为一个菜单**产品合约**，这里承担统一查询、导出与页签入口；
    三个 URL 各自保留，仍可直接访问 / 从页签切换。
    """
    return _contract_list('all')


@bp.route('/crm/hq-contracts')
@login_required
@permission_required('crm.view')
def hq_contracts():
    """总公司合约协议列表：签约级别为 总-总 / 总-分。"""
    return _contract_list('hq')


@bp.route('/crm/branch-contracts')
@login_required
@permission_required('crm.view')
def branch_contracts():
    """分公司合约协议列表：签约级别为 分-分（必挂某省分公司）。"""
    return _contract_list('br')


def _contract_form(side, cid=None):
    """两个口径共用的新建 / 编辑表单视图。

    side=hq 总公司合约协议：签约级别下拉限「总-总 / 总-分」，选总-分时再指明省分公司。
    side=br 分公司合约协议：签约级别固定 分-分，且必须「选保司 → 选省分公司 → 选产品」。
    """
    c = CrmProductContract.query.get_or_404(cid) if cid else None
    # 进错口径时自动切换（例如拿分-分合约点开总公司入口），不让流程卡住
    if c and LEVEL_SIDE.get(c.sign_level or '') != side:
        side = LEVEL_SIDE.get(c.sign_level or '', 'hq')
        flash('该合约属于「%s」，已为你切到对应口径' % dict(SIDE_LEVELS)[side])
    sname = dict(SIDE_LEVELS)[side]
    list_url = url_for('crm.hq_contracts') if side == 'hq' else url_for('crm.branch_contracts')

    if request.method == 'POST':
        if not _f('product_name'):
            flash('产品名称不能为空')
            return redirect(request.url)
        if side == 'br' and not (request.form.get('branch_id') or '').strip().isdigit():
            flash('分公司合约协议必须选择省分公司')
            return redirect(request.url)
        # ---- 十六改：唯一性校验（同一保司 + 同一产品 + 同一缴别 只允许录一次）----
        _iid = (request.form.get('insurer_id') or '').strip()
        _iid = int(_iid) if _iid.isdigit() else None
        _pnm = (_f('product_name') or '').strip()
        _cov = (_f('coverage_term') or '').strip()      # 三十五改：保障期限参与唯一性
        _bid = (request.form.get('branch_id') or '').strip()
        _bid = int(_bid) if _bid.isdigit() else None
        if _iid and _pnm:
            dup = _dup_contract(_iid, _pnm, side, _bid, exclude_cid=(c.id if c else 0),
                                coverage=_cov)
            if dup:
                flash('唯一性校验未通过 —— ' + dup)
                return redirect(request.url)
            # 缴别级：防止拆成两条合约各录一部分缴别绕过上面这条
            _ps = []
            for _ix in request.form.getlist('tier_idx'):
                _p = (request.form.get('tier_period_%s' % _ix) or '').strip()
                if _p:
                    _ps.append(_p)
            dup = _dup_tiers(_iid, _pnm, side, _ps, _bid, exclude_cid=(c.id if c else 0),
                             coverage=_cov)
            if dup:
                flash('唯一性校验未通过 —— ' + dup)
                return redirect(request.url)
        # ---- 三十八改：产品代码定稿 ----
        # 留空 → 沿用同产品已有代码（达尔文12号两个保障期限共用一个码）／没有就自动生成；
        # 手填 → 必须通过「同一保司下一个代码只属于一个产品」的唯一性校验。
        _pcode, _pcerr = _resolve_product_code(
            _iid, _pnm, _f('product_code'), exclude_cid=(c.id if c else 0))
        if _pcerr:
            flash(_pcerr)
            return redirect(request.url)
        # ---- 三十九改：组件产品代码校验 ----
        # 历史已录过的组件 → 代码固定沿用（只读），不用校验；只有**新组件手工填**的代码才判唯一，
        # 被本保司别的产品/组件占用了就拦下。
        for _ix in request.form.getlist('comp_idx'):
            _cnm = (request.form.get('comp_name_%s' % _ix) or '').strip()
            if not _cnm:
                continue
            _civ = (request.form.get('comp_insurer_%s' % _ix) or '').strip()
            _civ = int(_civ) if _civ.isdigit() else _iid
            _ccd = (request.form.get('comp_code_%s' % _ix) or '').strip()
            if not _ccd:
                continue                    # 留空 → 自动发号，无需校验
            _chave = _comp_code_of(_civ, _cnm)
            if _chave and _pc_norm(_chave) == _pc_norm(_ccd):
                continue                    # 原样沿用的历史代码 → 必然唯一
            _cerr = _pc_owner(_ccd, _civ, _cnm)
            if _cerr:
                flash('组件「%s」%s' % (_cnm, _cerr))
                return redirect(request.url)
        if c is None:
            c = CrmProductContract(code=_next_code(CrmProductContract, 'PTC'),
                                   created_by=session.get('user_id'))
            _fill_contract(c, request.form)
            c.product_code = _pcode
            db.session.add(c)
            db.session.flush()          # 先落 id，才能把缴别明细挂上去
            # 四十三改：附件改为**多文件列表**（一次可选多个，全部追加、不再覆盖）
            _new_files, _ferr, _news = _add_contract_files(c, request.files.getlist('attach'))
            if _ferr:
                db.session.rollback()
                flash(_ferr[0])
                return redirect(request.url)
            if _news:
                # 主表旧字段同时留一份（本次第一个文件）—— 老页面/导出仍能显示「有附件」
                c.file_name, c.file_path = _news[0].file_name, _news[0].file_path
        else:
            _fill_contract(c, request.form)
            c.product_code = _pcode
            # 四十三改：编辑页上传的附件**追加**到列表，不再覆盖已有文件
            _new_files, _ferr, _news = _add_contract_files(c, request.files.getlist('attach'))
            if _ferr:
                flash(_ferr[0])
                return redirect(request.url)
            if _news:
                c.file_name, c.file_path = _news[0].file_name, _news[0].file_path
        # 口径兜底：分公司一律 分-分；总公司只接受 总-总 / 总-分
        if side == 'br' or c.sign_level not in ('tt', 'tf'):
            c.sign_level = 'ff' if side == 'br' else 'tt'
        # 四十六改（2026-10-29）：总-分 / 分-分 的**产品级字段一律跟随总总**。
        #   产品编码 / 保障期限 / 销售渠道 / 险种类型 / 缴费类型 都是产品属性，
        #   分口径不该另填一遍（威哥口径：总总录完，分口径只补费率）。
        #   前端已置只读，这里再做**后端强制**——不依赖前端，改包也绕不过去；
        #   总总没录该产品时 _hq 为空 → 不动，允许分口径自行填写。
        _hq_follow = {}
        if c.sign_level in ('tf', 'ff'):
            _hq_follow = _hq_of(c.insurer_id, c.product_name, exclude_cid=c.id)
            if _hq_follow:
                if (_hq_follow.get('code') or '').strip():
                    c.product_code = _hq_follow['code'].strip()
                for _fk, _fa in (('sale_channel', 'channel'),
                                 ('insurance_type', 'type'),
                                 ('pay_type', 'pay_type')):
                    _fv = (_hq_follow.get(_fa) or '').strip()
                    if _fv:
                        setattr(c, _fk, _fv)
                # 保障期限：总总只有**一个**候选时强制跟随；有多个（达尔文12号那种
                # 保终身 / 保至70岁 各建一条）则由用户自己挑，只兜底填空值。
                _covs = _hq_follow.get('coverages') or []
                if len(_covs) == 1:
                    c.coverage_term = _covs[0]
                elif _covs and not (c.coverage_term or '').strip():
                    c.coverage_term = _covs[0]
                # 定制类标识同样跟随（分口径表单没有这个勾选框，不同步就会被打回 0）
                c.is_custom = 1 if _hq_follow.get('custom') else 0
        # 二十八改：组件**必须先存** —— 「三、」的缴别行要按组件落 component_id，
        # 所以先建组件拿到 id 映射，再保存费率；未勾选定制类时 _save_components 返回空映射。
        # 三十九改：第二个返回值＝组件改码后一键同步的条数。
        # 四十七改（2026-10-29）：分口径**不建自己的组件**，直接把表单序号映射到
        #   总总合约的组件 id（前端组件区是只读展示，每行仍带 comp_idx＝序号）。
        if side == 'br':
            _cps_hq, _src_hq = _hq_components(c.insurer_id, c.product_name, exclude_cid=c.id)
            # 表单里的组件键可能是 '0' 也可能是 'c0'（JS 动态生成），所以**按组件名**配对，
            # 配不上（名字被改过/DOM 被改）再退回按渲染顺序对齐。
            _by_name = {_nk(x.product_name): x.id for x in _cps_hq}
            compmap = {}
            for _i, _k in enumerate(request.form.getlist('comp_idx')):
                _k = (_k or '').strip()
                _nm2 = (request.form.get('comp_name_%s' % _k) or '').strip()
                _hit = _by_name.get(_nk(_nm2))
                if not _hit and _i < len(_cps_hq):
                    _hit = _cps_hq[_i].id
                if _hit:
                    compmap[_k] = _hit
            _csync = 0
        else:
            compmap, _csync = _save_components(c, request.form)
        n = _save_tiers(c, request.form, compmap)
        if not n:
            db.session.rollback()
            flash('请至少填写一个缴别的费用政策')
            return redirect(request.url)
        # 二十四改：继续率奖金率跟着合约一起保存（全量替换，整块空白则不建规则）
        _save_persist_of(c, request.form)
        # 四十改（2026-10-29）：续期服务费比例 —— 合约级 / 整个定制产品共用一份，全量替换。
        # 与缴别的 RYC 基础费率分开存：实发＝RYC-N × 本比例（威哥「对定制产品整体加一个系数」）。
        rr_n = _save_ryc_ratios(c, request.form)
        # 三十八改：产品代码是产品级属性 —— 手工改过就同步同产品的其他合约，
        # 避免同一产品留下两个代码（比如改了保终身那条，保至70岁那条没跟着变）。
        _synced = _sync_product_code(c.insurer_id, c.product_name, c.product_code,
                                     exclude_cid=c.id)
        # 四十九改（2026-10-29）：产品级属性（销售渠道 / 险种类型 / 缴费类型 / 定制类）
        # 实时下发给同产品的分口径合约 —— 总总改一次，分分列表跟着变，不用逐条重存。
        _spread = _spread_product_fields(c)
        # 四十四改（2026-10-29）：AI 识别草稿在人工核对并**保存**这一刻转正 ——
        # 清掉 ai_draft 标记，它就变成列表里的一条正常合约；不保存则一直是草稿。
        if getattr(c, 'ai_draft', 0):
            c.ai_draft = 0
            flash('AI 草稿已核对保存，正式转为生效合约')
        db.session.commit()
        flash('%s已保存：%s（含 %d 个缴别，产品代码 %s）'
              % (sname, c.code, n, c.product_code or '—'))
        if _synced:
            flash('产品代码已同步到同产品的其他 %d 条合约（同一产品只保留一个代码）' % _synced)
        if _spread:
            flash('产品级属性（渠道 / 险种 / 缴费类型）已实时同步到分口径的 %d 条合约' % _spread)
        if _csync:
            flash('组件产品代码已一键同步：%d 处引用同一组件的记录已改为新代码' % _csync)
        return redirect(list_url)

    tiers = _contract_tiers(cid) if cid else []
    # 二十七改（2026-10-29）：手工添加缴别——把已存、但不在 PAY_PERIODS 常量里的缴别
    # （即用户手工补录的自定义缴别）收集出来，喂给表单做「勾选框 + tier 下拉项」回填，
    # 否则编辑老合约时自定义缴别会丢失。分公司口径同样收集（数据来自总总，只读展示）。
    custom_periods = []
    for _t in tiers:
        _p = (_t.pay_period or '').strip()
        if _p and _p not in PAY_PERIODS and _p not in custom_periods:
            custom_periods.append(_p)
    # 七改：合约起止日所有缴别共用一条 → 编辑时从首条缴别回填一次
    _pre = 'tt' if side == 'hq' else 'ff'
    base_start = (getattr(tiers[0], _pre + '_start', '') or '') if tiers else ''
    base_end = (getattr(tiers[0], _pre + '_end', '') or '') if tiers else ''
    # 十改：方案起止日同样共用一条 → 编辑时从任意一条已有方案回填一次
    _plans = [p for t in tiers for p in _contract_plans(t.id)]
    plan_base_start = (getattr(_plans[0], _pre + '_start', '') or '') if _plans else ''
    plan_base_end = (getattr(_plans[0], _pre + '_end', '') or '') if _plans else ''
    # 十二改：方案清单已提到合约级 —— 编辑时把已存方案按「名称+费项类型+归属+形式」去重成清单，
    # 每个缴别再按同一把键回找自己的奖励行（这样老数据「方案曾按缴别各存一份」也能平滑升级）。
    plan_heads, _hmap = [], {}
    plan_awards = {t.id: {} for t in tiers}
    for t in tiers:
        for p in _contract_plans(t.id):
            # 十五改：文字类方案不挂缴别（tier_id 为空），不可能出现在这里；
            # 这里仍加一道保险，避免历史脏数据把文字类当系数类渲染。
            if (p.pay_form or 'transfer') not in PAY_FORM_COEF:
                continue
            key = _head_key(p)
            hk = _hmap.get(key)
            if hk is None:
                hk = str(len(plan_heads))
                _hmap[key] = hk
                plan_heads.append(dict(k=hk, name=p.plan_name or '',
                                       fee=p.fee_type or 'stage',
                                       scope=p.policy_scope or 'in',
                                       form=p.pay_form or 'transfer',
                                       remark=p.other_remark or '',
                                       tt_other=p.tt_other or '',
                                       ff_other=p.ff_other or '',
                                       note=p.note or '',
                                       text=False, plan_text=''))
            plan_awards[t.id][hk] = p
    # 十五改：文字类方案（现金 / 购物或其他）**并入同一份方案清单** —— 它们在「四、」节里
    #   与系数类方案并列，同样带方案名称与方案期间，不再单列一节。
    if cid:
        for p in _contract_text_plans(cid):
            plan_heads.append(dict(k=str(len(plan_heads)), name=p.plan_name or '',
                                   fee=p.fee_type or 'stage', scope='out',
                                   form=p.pay_form or 'other',
                                   remark=p.other_remark or '',
                                   tt_other=p.tt_other or '', ff_other=p.ff_other or '',
                                   note=p.note or '',
                                   text=True, plan_text=p.plan_text or ''))
        # 老数据平滑迁移：十三改的 `misc_plan_text` 是**合约级一个文本框**，十五改归并成方案。
        # 若这条合约还没迁过，就虚拟生成一条文字类方案回填到表单，用户保存一次即落到方案表。
        legacy_misc = (getattr(c, 'misc_plan_text', '') or '').strip()
        if not legacy_misc:
            # 再兜一层：六改/八改时代「按缴别登记的文字类方案」行 —— 它们带 tier_id，
            # 不在 `_contract_text_plans()` 里，靠文字化拼出来，同样迁成一条文字类方案。
            _allp = [p for t in tiers for p in _contract_plans(t.id)]
            legacy_misc = _legacy_misc_text(_allp, _pre)
        if legacy_misc and not [h for h in plan_heads if h['text']]:
            plan_heads.append(dict(k=str(len(plan_heads)), name=MISC_PLAN_LABEL,
                                   fee='stage', scope='out', form='other',
                                   remark='', tt_other='', ff_other='', note='',
                                   text=True, plan_text=legacy_misc))
    # 二十七改2 / 二十八改：定制类产品的组合组件（编辑时回填）——
    # 同时算出「组件 id → 表单序号」与「每个组件已勾了哪些缴别」，表单据此渲染组件内缴别芯片。
    components = _components_of(cid)
    # 四十七改（2026-10-29）：**分口径（分-分）的组件结构跟随总总合约**。
    #   分口径从来不在自己身上建组件（组件是产品级结构，只在总总录一次），
    #   所以这里取同一产品的总口径组件，只读展示 + 按组件生成费率表；
    #   保存时缴别的 component_id 直接指向**总总的组件 id**，两边永远是同一套结构。
    comp_src = None
    comp_readonly = False
    if side == 'br' and c is not None:
        _cps_hq, _src = _hq_components(c.insurer_id, c.product_name, exclude_cid=cid or 0)
        if _cps_hq and not components:
            components, comp_src, comp_readonly = _cps_hq, _src, True
    comp_ix = {cp.id: i for i, cp in enumerate(components)}
    comp_periods = {str(cp.id): [] for cp in components}
    # 分口径：先用总总「该组件已录的缴别」打底（新建时一张卡都没有 → 从这里生成费率表）
    if comp_readonly and comp_src:
        for _cp in components:
            comp_periods[str(_cp.id)] = [
                (t.pay_period or '') for t in _contract_tiers(comp_src.id)
                if t.component_id == _cp.id and (t.pay_period or '')]
    # 二十八改：**老定制合约兼容** —— 组件级费率上线前存的缴别行没有 component_id。
    # 若不管，打开编辑页时前端会认为它们「无归属」而整表重建（费率就白填了）。
    # 这里统一按「第 1 个组件」回填（表单字段 tier_comp_<ix> 也给它），用户保存一次即真正落到该组件。
    _is_custom = bool(getattr(c, 'is_custom', 0)) and bool(components)
    tier_ck = {}                     # tier.id -> 组件表单序号（字符串，''＝非定制/无组件）
    for t in tiers:
        _cid = t.component_id
        if _is_custom:
            tier_ck[t.id] = str(comp_ix[_cid]) if _cid in comp_ix else '0'
        else:
            tier_ck[t.id] = ''
        # ⚠️ comp_periods 的键是「组件 id 的字符串」('1'/'2')，而 component_id 是整数 1/2，
        # 直接 `_cid in comp_periods` 永远不命中 → 所有缴别都被塞进兜底分支（第 1 个组件），
        # 编辑页看起来就是「主险把附加险的缴别全吞了、附加险反而空了」。必须先转成字符串再比。
        _ck = str(_cid) if _cid else ''
        if _ck not in comp_periods:
            _ck = str(components[0].id) if _is_custom else ''
        if _ck and _ck in comp_periods and (t.pay_period or '') not in comp_periods[_ck]:
            comp_periods[_ck].append(t.pay_period or '')
    # 十二改：奖励行由前端按「当前勾选的缴别」动态生成，这里把已存值按「方案 → 缴别」组织好，
    # 供 JS 生成行时回填（编辑老合约不丢数）。
    # 二十八改：定制类产品按组件录费率后，同一缴别可能出现在多个组件里 —— 回填键改为
    # `组件键|缴别`（组件键＝组件的表单序号，无组件为 '0'），与表单字段的三段式保持一致。
    plan_awards_map = {}
    for t in tiers:
        _ck = tier_ck.get(t.id) or '0'
        for hk, p in plan_awards.get(t.id, {}).items():
            plan_awards_map.setdefault(hk, {})[_ck + '|' + (t.pay_period or '')] = {
                'c': (getattr(p, _pre + '_coef', 0) or 0),
                'a': (getattr(p, _pre + '_amount', 0) or 0)}
    # 二十六改：继续率表格转置（缴别做行、区间做列），需要知道「已登记过的缴别」→
    # 决定「涉及继续率的缴别」复选框初始勾选状态（有数据的勾上，没数据的留空待选）
    _prows = _persist_rows(cid)
    _pperiods = _matrix_periods(_prows)
    # 缴别芯片词库：标准缴别 + 手工缴别（含只在组件里出现的自定义缴别）
    for t in tiers:
        _p = (t.pay_period or '').strip()
        if _p and _p not in PAY_PERIODS and _p not in custom_periods:
            custom_periods.append(_p)
    comp_default = str(components[0].id) if components else ''
    # 二十九改：续期继续率分档（缴别 id → 年度块列表），表单据此回填分档录入区
    ryc_steps_map = _ryc_steps_map(cid)
    # 四十改：续期服务费比例（整个定制产品共用一份）—— 表单回填矩阵
    ryc_ratio_map = _ryc_ratio_map(cid)
    return render_template('crm/contract_form.html', row=c, side=side, sname=sname,
                           tiers=tiers, components=components,
                           comp_ix=comp_ix, comp_periods=comp_periods,
                           # 四十七改：分口径组件只读跟随总总（模板据此渲染只读组件区）
                           comp_readonly=comp_readonly, comp_src=comp_src,
                           tier_ck=tier_ck,
                           # 三十九改：历史组件已录代码 {保司id: {组件名: 代码}} →
                           # 前端按「组件保司+组件名」自动带出并锁定为只读
                           comp_code_json=json.dumps(_comp_code_map(), ensure_ascii=False),
                           # 二十九改：续期继续率分档（某缴别一旦有分档 → 该缴别卡显示分档录入区）
                           ryc_steps=ryc_steps_map,
                           # 四十改：续期服务费比例（定制产品整体，一份矩阵）
                           ryc_ratio=ryc_ratio_map,
                           comp_default=comp_default,
                           base_start=base_start, base_end=base_end,
                           plan_base_start=plan_base_start, plan_base_end=plan_base_end,
                           plan_heads=plan_heads, plan_awards_map=plan_awards_map,
                           # 十五改：文字类方案已并入方案清单，不再单列一节
                           pay_coef=PAY_FORM_COEF, pay_text=PAY_FORM_TEXT,
                           misc_label=MISC_PLAN_LABEL,
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           levels=[x for x in SIGN_LEVELS if LEVEL_SIDE.get(x[0]) == side],
                           statuses=CONTRACT_STATUS, pay_types=PAY_TYPES,
                           periods=PAY_PERIODS, types=INSURANCE_TYPES,
                           custom_periods=custom_periods,
                           # 三十五改：保障期限（保终身 / 保至70岁…，可手工输入新值）
                           coverage_terms=_coverage_terms(),
                           channels=PAY_CHANNELS,
                           branches=_branch_rows(),
                           branch_json=_branch_json(), product_json=_product_json(),
                           # 六改：分公司口径的产品名只能从「总总已录产品」下拉筛，且自动带出总总缴别
                           hq_product_json=_hq_product_json(exclude_cid=cid or 0),
                           # 二十四改：继续率奖金率（区间 × 缴别 → R13/R25/R37/R49）跟着合约录
                           # 二十六改：缴别做行、区间做列；并可按「涉及继续率的缴别」勾选子集
                           persist_rows=_prows,
                           persist_periods=_pperiods,
                           persist_rule=_persist_of(cid),
                           persist_cycles=[(cy, _persist_cycle_label(cy))
                                           for cy in PERSIST_BONUS_CYCLES],
                           ops_left=RANGE_OPS_LEFT, ops_right=RANGE_OPS_RIGHT,
                           claw_bases=CLAWBACK_BASES,
                           rules=CrmPersistRule.query.filter_by(status='on')
                                 .order_by(CrmPersistRule.name).all(),
                           scopes=POLICY_SCOPES, forms=PAY_FORMS,
                           # 八改：费用形式按政策归属分组渲染（协议内只有协议内结算）
                           # 十三改：方案块只给**系数类**两项；现金 / 购物或其他改走下方文字文本框
                           pay_in=PAY_FORM_IN, pay_out=PAY_FORM_OUT,
                           pay_coef_in=PAY_FORM_COEF_IN, pay_coef_out=PAY_FORM_COEF_OUT,
                           form_names=dict(PAY_FORMS),
                           # 八改+：变动费用·费项类型（对齐真实协议 4 类）
                           fee_types=PLAN_FEE_TYPES,
                           # 四十三改：销售渠道（线下 / 网销）；附件列表（编辑时展示已有文件）
                           sale_channels=SALE_CHANNELS,
                           files=(_contract_files(cid) if cid else []),
                           list_url=list_url)


@bp.route('/crm/hq-contracts/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def hq_contract_new():
    """新建总公司合约协议。"""
    return _contract_form('hq')


@bp.route('/crm/hq-contracts/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def hq_contract_edit(cid):
    """编辑总公司合约协议。"""
    return _contract_form('hq', cid)


@bp.route('/crm/branch-contracts/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def branch_contract_new():
    """新建分公司合约协议。"""
    return _contract_form('br')


@bp.route('/crm/branch-contracts/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def branch_contract_edit(cid):
    """编辑分公司合约协议。"""
    return _contract_form('br', cid)


@bp.route('/crm/contracts/<int:cid>')
@login_required
@permission_required('crm.view')
def contract_detail(cid):
    """产品合约详情：产品级信息 + 各缴别基础合约 + 卡内方案政策 + 继续率考核规则 + 附件与资料。"""
    c = CrmProductContract.query.get_or_404(cid)
    docs = CrmProductDoc.query.filter_by(contract_id=cid).order_by(CrmProductDoc.id.desc()).all()
    tiers = _contract_tiers(cid)
    plans_by_tier = {t.id: _contract_plans(t.id) for t in tiers}
    rule = CrmPersistRule.query.get(c.persist_rule_id) if c.persist_rule_id else None
    rule_items = (CrmPersistRuleItem.query.filter_by(rule_id=rule.id)
                  .order_by(CrmPersistRuleItem.sort_no, CrmPersistRuleItem.id).all()) if rule else []
    # 六改：按适用范围自动匹配到的规则（可能多条），连带各自的档位明细
    matched_rules = _match_rules(c)
    rule_items_map = {}
    for it in CrmPersistRuleItem.query.order_by(CrmPersistRuleItem.sort_no,
                                                CrmPersistRuleItem.id).all():
        rule_items_map.setdefault(it.rule_id, []).append(it)
    branch = InsurerBranch.query.get(c.branch_id) if c.branch_id else None
    # 汇总：各缴别 FYC 合计之和 / 方案条数 / 协议外方案数
    sum_fyc = round(sum((t.fyc_total or 0) for t in tiers), 2)
    all_plans = [p for ps in plans_by_tier.values() for p in ps]
    # 十三改：只有**系数类**方案进方案块、算条数、进合计；
    # 现金 / 购物或其他（金额·旅游·实物·权益）单列「其他现金、实物或权益方案」文字标识。
    coef_plans = [p for p in all_plans if (p.pay_form or 'transfer') in PAY_FORM_COEF]
    # 十二改：方案已提到合约级 —— 先按「名称+费项类型+政策归属+费用形式」去重成方案清单，
    # 每个方案下再挂「按缴别」的奖励行（plan 行仍带 tier_id，零迁移、老数据照常读）。
    plan_heads, _hmap = [], {}
    for t in tiers:
        for p in _contract_plans(t.id):
            # 十五改：文字类方案不挂缴别，不会出现在这里；保留一道保险防历史脏数据。
            if (p.pay_form or 'transfer') not in PAY_FORM_COEF:
                continue
            key = _head_key(p)
            hd = _hmap.get(key)
            if hd is None:
                hd = dict(k=str(len(plan_heads)), name=p.plan_name or '',
                          fee=p.fee_type or 'stage', scope=p.policy_scope or 'in',
                          form=p.pay_form or 'transfer', remark=p.other_remark or '',
                          tt_other=p.tt_other or '', ff_other=p.ff_other or '',
                          note=p.note or '', text=False, plan_text='', rows=[])
                _hmap[key] = hd
                plan_heads.append(hd)
            hd['rows'].append(dict(period=t.pay_period or '—', p=p))
    # 十五改：文字类方案（现金 / 购物或其他）并入同一份方案清单 ——
    #   与系数类方案并列展示，同样带方案名称；期间走合约级共用那条（段首已展示一次）。
    text_plans = _contract_text_plans(cid)
    for p in text_plans:
        plan_heads.append(dict(k=str(len(plan_heads)), name=p.plan_name or '',
                               fee=p.fee_type or 'stage', scope='out',
                               form=p.pay_form or 'other', remark=p.other_remark or '',
                               tt_other=p.tt_other or '', ff_other=p.ff_other or '',
                               note=p.note or '', text=True,
                               plan_text=p.plan_text or '', rows=[]))
    plan_cnt = len(plan_heads)                         # 方案条数（去重后，含文字类方案）
    coef_cnt = len([h for h in plan_heads if not h['text']])
    text_cnt = len(text_plans)                         # 文字类方案条数
    award_cnt = len(coef_plans)                        # 奖励行数（系数类方案 × 缴别）
    out_cnt = len([h for h in plan_heads if (h['scope'] or '') == 'out'])
    sum_plan = round(sum((p.plan_total or 0) for p in coef_plans), 2)
    # 十改：方案起止日全合约共用一条 → 详情页只在段首展示一次，方案表不再逐行重复
    _pd = 'tt' if _side_of(c.sign_level) == 'hq' else 'ff'
    # 十五改：文字类方案不挂缴别，合约里可能**只有文字类方案** → 取期间时要把它们算进来
    _any_plan = (all_plans + text_plans)
    plan_start_d = (getattr(_any_plan[0], _pd + '_start', '') or '') if _any_plan else ''
    plan_end_d = (getattr(_any_plan[0], _pd + '_end', '') or '') if _any_plan else ''
    return render_template('crm/contract_detail.html', row=c, docs=docs, tiers=tiers,
                           # 四十三改：协议附件改成列表（含老字段自动补登）
                           files=_contract_files(cid),
                           sale_names=dict(SALE_CHANNELS),
                           # 四十七改：分口径的组件沿用总总（_comp_map_of 会自动并入外部组件）
                           components=list(_comp_map_of(c).values()),
                           # 二十八改：组件 id → 组件名称（缴别卡头标出这份费率属于哪个组件）
                           comp_names={k: (v.product_name or '') for k, v in _comp_map_of(c).items()},
                           plans_by_tier=plans_by_tier,
                           # 二十九改：续期继续率分档（缴别 id → 年度块）—— 详情页展示分档明细
                           ryc_steps=_ryc_steps_map(cid),
                           # 四十改：续期服务费比例（整个产品一份）—— 详情页表头展示
                           ryc_ratio=_ryc_ratio_map(cid),
                           round_persist=round_persist,
                           side=_side_of(c.sign_level), sname=_side_name(c.sign_level),
                           rule=rule, rule_items=rule_items,
                           matched_rules=matched_rules, rule_items_map=rule_items_map,
                           rule_cycles_map={r.id: _rule_cycles(r) for r in matched_rules},
                           rule_cycles=(_rule_cycles(rule) if rule else []),
                           ls_cycle=LS_CYCLE, fmt_range=_fmt_range,
                           branch=branch,
                           sum_fyc=sum_fyc, plan_cnt=plan_cnt, out_cnt=out_cnt,
                           sum_plan=sum_plan, plan_heads=plan_heads, award_cnt=award_cnt,
                           plan_start_d=plan_start_d, plan_end_d=plan_end_d,
                           # 十五改：方案条数拆成「系数类 / 文字类」（文字类方案合并进同一清单）
                           coef_cnt=coef_cnt, text_cnt=text_cnt,
                           misc_label=MISC_PLAN_LABEL,
                           scopes=POLICY_SCOPES, forms=PAY_FORMS,
                           # 八改+：倒扣基数短名 + 方案费项类型名（十五改已删口径/精度短名）
                           cbase_short=CBASE_SHORT,
                           fee_short=dict(PLAN_FEE_TYPES), dname=DNAME,
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/contracts/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_edit(cid):
    """（旧入口，已废弃）按合约口径跳到对应的编辑页。"""
    c = CrmProductContract.query.get_or_404(cid)
    if _side_of(c.sign_level) == 'br':
        return redirect(url_for('crm.branch_contract_edit', cid=cid))
    return redirect(url_for('crm.hq_contract_edit', cid=cid))


@bp.route('/crm/contracts/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_delete(cid):
    """删除产品合约（级联清掉缴别明细、关联的资料记录与附件）。"""
    c = CrmProductContract.query.get_or_404(cid)
    # 二十四改：继续率规则挂在合约上，合约删了规则与档位一并清掉
    _pr = _persist_of(cid)
    if _pr:
        for _it in CrmPersistRuleItem.query.filter_by(rule_id=_pr.id).all():
            db.session.delete(_it)
        db.session.delete(_pr)
    for t in CrmContractTier.query.filter_by(contract_id=cid).all():
        db.session.delete(t)
    # 二十九改：删合约连带删续期继续率分档
    for rs in CrmContractRycStep.query.filter_by(contract_id=cid).all():
        db.session.delete(rs)
    # 四十改：续期服务费比例同样跟着合约走
    for rr in CrmRycRatio.query.filter_by(contract_id=cid).all():
        db.session.delete(rr)
    for p in CrmContractPlan.query.filter_by(contract_id=cid).all():
        db.session.delete(p)
    # 二十七改2：删定制类产品时连带删组合组件
    for cp in CrmContractComponent.query.filter_by(contract_id=cid).all():
        db.session.delete(cp)
    for d in CrmProductDoc.query.filter_by(contract_id=cid).all():
        _drop_file('doc', d.file_path)
        db.session.delete(d)
    # 四十三改：多附件 —— 子表记录连同落盘文件一起删
    for _cf in CrmContractFile.query.filter_by(contract_id=cid).all():
        _drop_file('contract', _cf.file_path)
        db.session.delete(_cf)
    _drop_file('contract', c.file_path)
    db.session.delete(c)
    db.session.commit()
    flash('产品合约已删除')
    return redirect(url_for('crm.contracts'))


@bp.route('/crm/contracts/<int:cid>/attach')
@login_required
@permission_required('crm.view')
def contract_attach(cid):
    """下载合约扫描件（鉴权下载）—— 兼容老链接，取列表里的第一个文件。"""
    c = CrmProductContract.query.get_or_404(cid)
    files = _contract_files(cid)
    if not files:
        flash('该合约没有附件')
        return redirect(url_for('crm.contract_detail', cid=cid))
    f = files[0]
    return send_from_directory(_attach_dir('contract'), f.file_path, as_attachment=True,
                               download_name=f.file_name or f.file_path)


# 四十三改：多附件 —— 逐个下载 / 逐个删除（详情页列表每行一对按钮）
@bp.route('/crm/contracts/<int:cid>/files/<int:fid>')
@login_required
@permission_required('crm.view')
def contract_file_download(cid, fid):
    """下载合约附件列表里的某一个文件（鉴权下载）。"""
    f = CrmContractFile.query.filter_by(id=fid, contract_id=cid).first_or_404()
    return send_from_directory(_attach_dir('contract'), f.file_path, as_attachment=True,
                               download_name=f.file_name or f.file_path)


@bp.route('/crm/contracts/<int:cid>/files/<int:fid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_file_delete(cid, fid):
    """删除合约附件列表里的某一个文件（落盘文件一并删）。

    表单页列表里的「删除」也打这个路由（formaction + formnovalidate），
    带 `next` 就回原页（只接受站内 /crm/ 开头的相对地址），否则回详情页。
    """
    if _del_contract_file(cid, fid):
        flash('附件已删除')
    else:
        flash('附件不存在或已被删除')
    nx = (request.form.get('next') or '').strip()
    if nx.startswith('/crm/'):
        return redirect(nx)
    return redirect(url_for('crm.contract_detail', cid=cid))


@bp.route('/crm/contracts/<int:cid>/attach/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_attach_delete(cid):
    """删除合约扫描件（老入口：删主表 file_path 指的那份；找不到就删列表第一份）。"""
    c = CrmProductContract.query.get_or_404(cid)
    files = _contract_files(cid)
    target = None
    for f in files:                       # 优先匹配主表旧字段指向的文件，避免误删别的附件
        if (f.file_path or '') == (c.file_path or ''):
            target = f
            break
    if target is None and files:
        target = files[0]
    if target is not None:
        _del_contract_file(cid, target.id)
    else:
        _drop_file('contract', c.file_path)
        c.file_name, c.file_path = None, None
        db.session.commit()
    flash('附件已删除')
    return redirect(url_for('crm.contract_detail', cid=cid))


@bp.route('/crm/contracts/<int:cid>/attach/upload', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def contract_attach_upload(cid):
    """在详情页补传合约扫描件（支持一次选多个，全部追加）。"""
    c = CrmProductContract.query.get_or_404(cid)
    n, errs, news = _add_contract_files(c, request.files.getlist('attach'))
    if errs:
        flash(errs[0])
        return redirect(url_for('crm.contract_detail', cid=cid))
    if not n:
        flash('请选择文件（同名同大小的重复文件会被跳过）')
        return redirect(url_for('crm.contract_detail', cid=cid))
    # 主表旧字段跟「最近一次上传」保持一致（老下载入口 / 导出仍可用）
    c.file_name, c.file_path = news[0].file_name, news[0].file_path
    db.session.commit()
    flash('附件已上传：%d 个' % n)
    return redirect(url_for('crm.contract_detail', cid=cid))


@bp.route('/crm/contracts/export')
@login_required
@permission_required('crm.view')
def contract_export():
    """导出合约为 Excel：**一行 = 一个方案政策**（无方案时一行 = 缴别基础）。

    五改（2026-10-02）：支持 ?side=hq|br|all 与页面筛选条件联动，
    并新增「合约口径」列，便于一眼区分总公司合约协议 / 分公司合约协议。
    """
    side = (request.args.get('side') or 'all').strip()
    kw = (request.args.get('kw') or '').strip()
    iid = (request.args.get('insurer_id') or '').strip()
    bid = (request.args.get('branch_id') or '').strip()
    lv = (request.args.get('level') or '').strip()
    it = (request.args.get('type') or '').strip()
    q = CrmProductContract.query
    if side in SIDE_LEVEL_CODES:
        q = q.filter(CrmProductContract.sign_level.in_(SIDE_LEVEL_CODES[side]))
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(CrmProductContract.product_name.like(like),
                            CrmProductContract.product_code.like(like),
                            CrmProductContract.insurer_name.like(like)))
    if iid.isdigit():
        q = q.filter(CrmProductContract.insurer_id == int(iid))
    if bid.isdigit():
        q = q.filter(CrmProductContract.branch_id == int(bid))
    if lv in ('tt', 'tf', 'ff'):
        q = q.filter(CrmProductContract.sign_level == lv)
    if it:
        q = q.filter(CrmProductContract.insurance_type == it)
    rows = q.order_by(CrmProductContract.id).all()

    headers = ['合约口径', '合约编号', '保司名称', '保司简称', '签约级别', '省分公司', '产品名称',
               '组合组件', '产品编码', '保障期限', '险种类型', '销售渠道', '合约状态', '缴费类型',
               '缴别', '总总合约起始日', '总总合约终止日', '总总FYC（%）',
               '分分合约起始日', '分分合约终止日', '分分FYC（%）', '首年FYC合计（%）']
    headers += ['RYC-%d（%%）' % n for n in range(2, 11)]
    headers += ['方案名称', '总总方案起止日', '分分方案起止日', '政策归属', '费用形式',
                '总总方案-奖金系数（%）', '总总方案-奖金金额（元）', '总总方案-其他',
                '分分方案-奖金系数（%）', '分分方案-奖金金额（元）', '分分方案-其他',
                '方案合计（%）', '继续率考核规则', '备注', MISC_PLAN_LABEL]
    data = []
    for r in rows:
        base = [_side_name(r.sign_level), r.code, r.insurer_name or '', r.insurer_short or '',
                _name_of(SIGN_LEVELS, r.sign_level), r.branch_name or '',
                r.product_name or '', r.product_code or '', r.insurance_type or '',
                # 四十三改：「销售渠道」紧跟险种（_name_of 取中文，空值导出空串）
                _name_of(SALE_CHANNELS, r.sale_channel),
                _name_of(CONTRACT_STATUS, r.status), _name_of(PAY_TYPES, r.pay_type)]
        # 二十八改：定制类产品按组件录费率 → 在「产品名称」后插一列「组合组件」
        # 四十七改：分口径的组件在总总合约上，用 _comp_map_of 才能取到
        comp_names = {k: (v.product_name or '') for k, v in _comp_map_of(r).items()}

        def _with_comp(row, comp):
            """在「产品名称」后插组件名、「产品编码」后插保障期限（三十五改）。"""
            return row[:7] + [comp] + row[7:8] + [r.coverage_term or ''] + row[8:]

        tail = [r.persist_rule_name or '']
        # 十五改：文字类方案（现金/购物/其他，tier_id=None）不按缴别展开、不参与合并 ——
        # 仅在该合约首行以统一标识呈现一次（与系数类方案并列在四、节，共用方案名称/期间）。
        once = [_misc_text_of(r, 'tt' if _side_of(r.sign_level) == 'hq' else 'ff')]
        tiers = _contract_tiers(r.id)
        if not tiers:
            # 一期存量合约兜底：按主表旧列导出一行（无方案概念）
            data.append(_with_comp(base, '') + ['（未拆分缴别）', '', '', r.tt_fyc or 0,
                                '', '', r.ff_fyc or 0, r.fyc_total or 0] +
                        [getattr(r, 'ryc_%d' % n) or 0 for n in range(2, 11)] +
                        ['', '', '', '', '', r.tt_coef or 0, r.tt_amount or 0, r.tt_other or '',
                         r.ff_coef or 0, r.ff_amount or 0, r.ff_other or '', r.plan_total or 0] +
                        tail + [r.note or '', (once.pop() if once else '')])
            continue
        for t in tiers:
            tbase = _with_comp(base, comp_names.get(t.component_id, '') if t.component_id else '') + \
                    [t.pay_period or '', t.tt_start or '', t.tt_end or '', t.tt_fyc or 0,
                     t.ff_start or '', t.ff_end or '', t.ff_fyc or 0, t.fyc_total or 0] + \
                    [getattr(t, 'ryc_%d' % n) or 0 for n in range(2, 11)]
            # 十五改：导出只铺**系数类**方案（文字类走最后一列的标识，不逐缴别展开）
            plans = [p for p in _contract_plans(t.id)
                     if (p.pay_form or 'transfer') in PAY_FORM_COEF]
            if not plans:
                data.append(tbase + ['（无系数类方案）', '', '', '', '', 0, 0, '', 0, 0, '', 0] +
                             tail + [t.note or '', (once.pop() if once else '')])
                continue
            for p in plans:
                data.append(tbase + [
                    p.plan_name or '',
                    '%s ~ %s' % (p.tt_start or '', p.tt_end or ''),
                    '%s ~ %s' % (p.ff_start or '', p.ff_end or ''),
                    _name_of(POLICY_SCOPES, p.policy_scope),
                    _name_of(PAY_FORMS, p.pay_form),
                    p.tt_coef or 0, p.tt_amount or 0, p.tt_other or '',
                    p.ff_coef or 0, p.ff_amount or 0, p.ff_other or '',
                    p.plan_total or 0] + tail + [p.note or '', (once.pop() if once else '')])
    fname = {'hq': '总公司合约协议', 'br': '分公司合约协议'}.get(side, '合约总览')
    return _xlsx_response(fname, '%s（按方案政策）' % fname, headers, data)


# ===================== ③+ 佣金查询（产品合约 · 按缴别） =====================
# 威哥口径：合约总览下给一个查询出口 ——
#   固定三列：保司简称 / 产品名称 / 产品缴别
#   【首年度销售费用】佣金总计 / 总总FYC / 分分FYC / 方案加佣（十六改微调：总计排第一列）
#   【续年期销售费用】RYC-2~10（分年展示）
#   筛选字段：数据月度 + 保司
# 口径红线：方案加佣只看**系数类**方案（transfer / coef）的加佣系数；
#           佣金总计 = 总总FYC + 分分FYC + 方案加佣（RYC 属续期，不并入总佣金）。
# 二十四改（2026-10-28）：同一产品的「总总签约」与「分分签约」**合并成一行**显示，
#           不再一个口径一行（原实现按合约逐条铺，看起来像两个产品）。详见 _commission_rows。
# 展示口径（十六改微调）：两级表头；数值一律百分比（表头不再写「（%）」）；**0 一律显示 "-"**。

def _plan_comm_of(tier):
    """某缴别的「方案加佣」= 其系数类方案的加佣系数合计（%）。

    十五改口径：系数类方案（transfer / coef）逐条 plan_total 相加；
    文字类方案（cash / other）不参与任何合计。
    返回 (加佣合计, 参与的方案列表)——方案列表还要用来取「方案加佣时间」做月度筛选。
    """
    ps = [p for p in _contract_plans(tier.id) if (p.pay_form or 'transfer') in PAY_FORM_COEF]
    return round(sum((p.plan_total or 0) for p in ps), 2), ps


def _tier_months(tier, ps):
    """一条缴别记录可用于「数据月度」筛选的候选月份集合。

    三类日期源（威哥口径，任一命中即算命中）：
      ① 方案加佣时间 —— 系数类方案的起始日
      ② 总总签约时间 —— tier.tt_start
      ③ 分分签约时间 —— tier.ff_start
    ⚠️ 一条日期都没有时 → 按「当月自然月」初始化，故只在查当月时被命中。
    """
    ms = set()
    for d in [(tier.tt_start or ''), (tier.ff_start or '')]:
        if len(d or '') >= 7:
            ms.add(d[:7])
    for p in ps:
        for d in [(p.tt_start or ''), (p.ff_start or '')]:
            if len(d or '') >= 7:
                ms.add(d[:7])
    if not ms:
        ms.add(datetime.now().strftime('%Y-%m'))   # 总总/分分初始化按当月自然月
    return ms


def _month_options():
    """数据月度下拉选项：库里出现过的年月 + 当月，倒序去重。"""
    ms = set()
    for t in CrmContractTier.query.all():
        for d in [(t.tt_start or ''), (t.ff_start or '')]:
            if len(d or '') >= 7:
                ms.add(d[:7])
    for p in CrmContractPlan.query.all():
        for d in [(p.tt_start or ''), (p.ff_start or '')]:
            if len(d or '') >= 7:
                ms.add(d[:7])
    ms.add(datetime.now().strftime('%Y-%m'))
    return sorted(ms, reverse=True)


def _commission_rows(month=None, insurer_id=None):
    """佣金查询数据：一行 = 一个产品 × 一个缴别（总总 / 分分已合并）。

    二十四改（2026-10-28，威哥口径）：
      同一款产品可能既有「总总签约」又有「分分签约」，在合约表里是两条独立合约。
      原实现按合约逐条铺缴别 → 同一产品同一缴别会出现两行（一行只有总总FYC、
      另一行只有分分FYC），看着像两个产品。现改为按 (保司, 产品名称, 缴别) 聚合：
        总总FYC  = 各口径 tt_fyc 之和
        分分FYC  = 各口径 ff_fyc 之和
        方案加佣 = 各口径系数类方案加佣之和
        RYC-n    = 各口径续期率之和
        佣金总计 = 总总FYC + 分分FYC + 方案加佣（RYC 属续期，仍不并入）
      排序：先按产品首次出现顺序，产品内再按标准缴别顺序（PAY_PERIODS）。
    二十八改（2026-10-29）：定制类产品**按组件录费率** —— 组件行以「组件产品名称」独立成行，
    并带出所属定制产品名（`custom_name` 列）便于归组；无组件的行 `custom_name` 为空，与旧版一致。
      同一组件在「总总合约」与「分分合约」各录一条时，仍按产品名合并成一行（总总/分分各占一列）。
    """
    agg = {}
    prod_seq = {}          # (保司, 产品名) -> 出现序号，保证同一产品的缴别行连在一起
    # 四十改：哪几个年度是按继续率分档的（页面用 ＊ 标出「此处为满档费率」）。
    # 比例表是**合约级**（整个定制产品一份），所以按 contract_id 收集后再下发到各缴别行。
    step_years = {}
    for _s in CrmRycRatio.query.all():
        step_years.setdefault(_s.contract_id, set()).add(_s.ryc_year)
    if not step_years:                       # 兼容：老合约若只有缴别级分档，仍按原口径标 ＊
        for _s in CrmContractRycStep.query.all():
            step_years.setdefault(_s.tier_id, set()).add(_s.ryc_year)
        _by_tier = True
    else:
        _by_tier = False
    q = CrmProductContract.query
    if insurer_id:
        q = q.filter(CrmProductContract.insurer_id == insurer_id)
    for c in q.order_by(CrmProductContract.id).all():
        comps = _comp_map_of(c)
        for t in _contract_tiers(c.id):
            plan_comm, ps = _plan_comm_of(t)
            if month and month not in _tier_months(t, ps):
                continue
            cp = comps.get(t.component_id or 0)
            if cp is not None:
                pnm = (cp.product_name or '').strip() or (c.product_name or '')
                custom_nm = c.product_name or ''
            else:
                pnm = c.product_name or ''
                custom_nm = ''
            pkey = ((c.insurer_id or 0), pnm)
            if pkey not in prod_seq:
                prod_seq[pkey] = len(prod_seq) + 1
            key = pkey + ((t.pay_period or ''),)
            r = agg.get(key)
            if r is None:
                r = agg[key] = dict(
                    sort_c=prod_seq[pkey], cid=c.id, code=c.code or '',
                    insurer_short=c.insurer_short or c.insurer_name or '',
                    product_name=pnm, custom_name=custom_nm,
                    pay_period=t.pay_period or '',
                    tt_fyc=0.0, ff_fyc=0.0, plan_comm=0.0,
                    ryc={n: 0.0 for n in range(2, 11)}, ryc_step=set())
            # 同一产品的两条合约可能只有一条填了保司简称，取有值的那条
            if not r['insurer_short']:
                r['insurer_short'] = c.insurer_short or c.insurer_name or ''
            if not r['custom_name']:
                r['custom_name'] = custom_nm
            r['tt_fyc'] = round(r['tt_fyc'] + (t.tt_fyc or 0), 2)
            r['ff_fyc'] = round(r['ff_fyc'] + (t.ff_fyc or 0), 2)
            r['plan_comm'] = round(r['plan_comm'] + plan_comm, 2)
            for n in range(2, 11):
                r['ryc'][n] = round(r['ryc'][n] + (getattr(t, 'ryc_%d' % n) or 0), 2)
            r['ryc_step'] |= step_years.get(t.id if _by_tier else c.id, set())

    rows = list(agg.values())
    for r in rows:
        r['total'] = round(r['tt_fyc'] + r['ff_fyc'] + r['plan_comm'], 2)

    def _pk(x):
        p = x['pay_period']
        return (x['sort_c'], PAY_PERIODS.index(p) if p in PAY_PERIODS else 999)
    rows.sort(key=_pk)
    return rows


@bp.route('/crm/contracts/commission')
@login_required
@permission_required('crm.view')
def commission_query():
    """佣金查询页：按缴别看「总总FYC / 分分FYC / RYC / 方案加佣 / 佣金总计」。"""
    month = (request.args.get('month') or '').strip()
    iid = (request.args.get('insurer_id') or '').strip()
    rows = _commission_rows(month, int(iid) if iid.isdigit() else None)
    # 局部刷新：仅返回结果表格片段（AJAX），不整页渲染
    if request.args.get('partial') == '1':
        return render_template('crm/_commission_query_table.html', rows=rows, n=len(rows))
    return render_template('crm/commission_query.html', rows=rows, month=month,
                           iid=iid, months=_month_options(),
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           n=len(rows))
    # 十六改微调：底部「合计」行已删除（百分比系数求和无业务意义），
    #            故不再传 sum_row；_commission_sum 一并废弃。


@bp.route('/crm/contracts/commission/export')
@login_required
@permission_required('crm.view')
def commission_export():
    """佣金查询导出 Excel（与页面同一套筛选条件）。"""
    month = (request.args.get('month') or '').strip()
    iid = (request.args.get('insurer_id') or '').strip()
    rows = _commission_rows(month, int(iid) if iid.isdigit() else None)
    # ⚠️ 十六改微调：列顺序与页面一致（佣金总计 → 总总FYC → 分分FYC → 方案加佣 → RYC 分年），
    #    表头两级：首年度销售费用（4 列） / 续年期销售费用（9 列）。数值均为百分比，不再写「（%）」。
    # 二十八改：加「所属定制产品」列 —— 定制类产品按组件分行后，靠它一眼看出组件属于哪个定制产品。
    headers = ['保司简称', '所属定制产品', '产品名称', '产品缴别',
               '佣金总计', '总总FYC', '分分FYC', '方案加佣']
    headers += ['RYC-%d' % n for n in range(2, 11)]
    groups = [('', 4), ('首年度销售费用', 4), ('续年期销售费用', 9)]
    data = []
    for x in rows:
        data.append([x['insurer_short'], x.get('custom_name', ''), x['product_name'], x['pay_period'],
                     round(x['total'], 2), round(x['tt_fyc'], 2),
                     round(x['ff_fyc'], 2), round(x['plan_comm'], 2)] +
                    [x['ryc'][n] for n in range(2, 11)])
    # 十六改微调：末行「合计」已删（与页面一致）
    # 百分比列（第5~17列）按 Excel 百分比格式显示，与页面一致（页面 4.50%，导出也是 4.50%）
    pct_cols = {ci: 2 for ci in range(5, 18)}
    return _xlsx_response('佣金查询', '佣金查询（按产品＋缴别 · 百分比）', headers, data,
                          groups=groups, pct_cols=pct_cols)


# 十六改微调：_commission_sum 已废弃删除（页面与导出都不再需要合计行）。


# ===================== ③-2 产品查询（十七改 2026-10-02）=====================
# 威哥规划（《核心业务_产品合约管理_框架规划.md》十一 · 三期候选 3）：
#   「产品查询模块：从合约表抽产品维度做查询视图（按险种 / 保司 / 缴费方式检索）」。
# 口径（与佣金查询互补，不重复）：
#   · 佣金查询 = 一行一个**缴别**，看费率明细；
#   · 产品查询 = 一行一个**产品**（保司＋产品名称＋险种＋缴费类型 聚合），看产品台账；
#     缴别合并展示；首年佣金率给**区间**（同一口径：总总FYC＋分分FYC＋方案加佣）。
# 零新表、零迁移：全部从 CrmProductContract / CrmContractTier / CrmContractPlan 现算。

def _product_rows(iid=None, itype=None, ptype=None, kw=None):
    """产品查询数据：一行 = 一个产品（按 保司 + 产品名称 + 险种 + 缴费类型 聚合）。"""
    kw = (kw or '').strip().lower()
    agg, order = {}, []
    q = CrmProductContract.query
    if iid:
        q = q.filter(CrmProductContract.insurer_id == iid)
    for c in q.order_by(CrmProductContract.id).all():
        ity, pty = (c.insurance_type or ''), (c.pay_type or '')
        # 筛选：险种 / 缴费方式 / 关键词（产品名 + 产品编码 + 保司简称）
        if itype and ity != itype:
            continue
        if ptype and pty != ptype:
            continue
        if kw and kw not in ('%s%s%s' % (c.product_name or '', c.product_code or '',
                                         c.insurer_short or c.insurer_name or '')).lower():
            continue
        key = ((c.insurer_id or 0), (c.product_name or '').strip().lower(), ity, pty)
        r = agg.get(key)
        if r is None:
            r = dict(insurer_id=c.insurer_id or 0,
                     insurer_short=c.insurer_short or c.insurer_name or '—',
                     product_name=c.product_name or '—',
                     product_code=c.product_code or '',
                     insurance_type=ity or '—',
                     pay_type=pty,
                     periods=[], levels=[], codes=[], cids=[],
                     rates=[], dmin='', dmax='', statuses=[])
            agg[key] = r
            order.append(key)
        for t in _contract_tiers(c.id):
            if t.pay_period and t.pay_period not in r['periods']:
                r['periods'].append(t.pay_period)
            plan_comm, _ps = _plan_comm_of(t)
            r['rates'].append(round((t.tt_fyc or 0) + (t.ff_fyc or 0) + plan_comm, 2))
            # 合约期间：取所有日期源里的最早起 / 最晚止
            for d in [(t.tt_start or ''), (t.ff_start or '')]:
                if len(d) >= 8 and (not r['dmin'] or d < r['dmin']):
                    r['dmin'] = d
            for d in [(t.tt_end or ''), (t.ff_end or '')]:
                if len(d) >= 8 and d > r['dmax']:
                    r['dmax'] = d
        lv = dict(SIGN_LEVELS).get(c.sign_level, c.sign_level or '')
        if lv and lv not in r['levels']:
            r['levels'].append(lv)
        if c.code and c.code not in r['codes']:
            r['codes'].append(c.code)
        if c.id not in r['cids']:
            r['cids'].append(c.id)
        if c.status:
            r['statuses'].append(c.status)

    rows = []
    for k in order:
        r = agg[k]
        # 缴别按 PAY_PERIODS 常量顺序排（未在常量里的排最后）
        def _pidx(p):
            return PAY_PERIODS.index(p) if p in PAY_PERIODS else 999
        r['periods'].sort(key=_pidx)
        r['rate_min'] = min(r['rates']) if r['rates'] else 0
        r['rate_max'] = max(r['rates']) if r['rates'] else 0
        # 状态优先级：生效 > 草稿 > 终止（一个产品可能挂多条合约）
        for want in ('active', 'draft', 'ended'):
            if want in r['statuses']:
                r['status'] = want
                break
        else:
            r['status'] = ''
        r['docs'] = _doc_index().get((r['insurer_id'], (r['product_name'] or '').strip().lower()), [])
        rows.append(r)
    rows.sort(key=lambda x: (x['insurer_short'], x['product_name']))
    return rows


def _doc_index():
    """十八改：产品 → 材料 的索引 {(insurer_id, 产品名小写): [CrmProductDoc, ...]}。

    合并「产品查询」与「产品资料库」后，产品台账每一行要能直接看到该产品的材料，
    关联键就是 保司 + 产品名称（资料上传时这两项都是手填的，故按名称匹配，不加外键）。
    """
    idx = {}
    for d in CrmProductDoc.query.order_by(CrmProductDoc.id.desc()).all():
        k = (d.insurer_id or 0, (d.product_name or '').strip().lower())
        idx.setdefault(k, []).append(d)
    return idx


@bp.route('/crm/products')
@login_required
@permission_required('crm.view')
def product_query():
    """十八改：原「产品查询」已并入**产品资料库**（/crm/docs?tab=product），这里只做重定向壳。"""
    args = request.args.to_dict()
    args['tab'] = 'product'
    return redirect(url_for('crm.docs', **args))


@bp.route('/crm/products/export')
@login_required
@permission_required('crm.view')
def product_export():
    """产品查询导出 Excel（与页面同一套筛选条件）。"""
    iid = (request.args.get('insurer_id') or '').strip()
    itype = (request.args.get('itype') or '').strip()
    ptype = (request.args.get('ptype') or '').strip()
    kw = (request.args.get('kw') or '').strip()
    rows = _product_rows(int(iid) if iid.isdigit() else None, itype, ptype, kw)
    headers = ['保司简称', '产品名称', '产品编码', '险种', '缴费方式',
               '缴别', '合约口径', '首年佣金率（%）', '合约期间', '状态', '合约编号']
    data = []
    for r in rows:
        rate = ('%.2f' % r['rate_min']) if r['rate_min'] == r['rate_max'] \
            else ('%.2f ~ %.2f' % (r['rate_min'], r['rate_max']))
        period = '%s ~ %s' % (r['dmin'], r['dmax']) if (r['dmin'] or r['dmax']) else ''
        data.append([r['insurer_short'], r['product_name'], r['product_code'],
                     r['insurance_type'], dict(PAY_TYPES).get(r['pay_type'], r['pay_type']),
                     ' / '.join(r['periods']), ' + '.join(r['levels']),
                     rate, period, DNAME.get(r['status'], ''), ' / '.join(r['codes'])])
    return _xlsx_response('产品查询', '产品查询（按产品维度）', headers, data)


# ---------- 十七改 B：合约到期提醒（红黄灯）----------
# 威哥规划（三期候选 4）：「合约到期提醒：方案终止日临期提醒（复用 ADM 的红黄灯范式）」。
# 日期源：缴别 / 方案上的「总总 / 分分终止日」（十六改起日期全合约共用一条，两侧同值）。
# 取**最晚**的一个作为合约有效期止（只要还有一条费率在有效期内，合约整体就不算过期）。
# 阈值沿用 ADM：≤7 天 danger（红）/ ≤30 天 warn（黄）/ <0 已过期（红）。

EXPIRE_WARN_DAYS = 30
EXPIRE_DANGER_DAYS = 7


def _pdate(s):
    """'YYYY-MM-DD' → date；解析失败返回 None（日期列是 String，历史数据可能脏）。"""
    s = (s or '').strip()
    for f in ('%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _contract_span(c):
    """一条合约的有效期（起, 止）—— **起始日与终止日分开收集**。

    ⚠️ 三十改（2026-10-29）：原实现把起始日、终止日混在一个池子里取最早/最晚，
       于是「只填起始日、不填终止日」的长期合约，止日会被算成「最近的那个起始日」→
       误判「已过期」（威哥 10-29 截图里那 7 条已过期就是这么来的）。
       现口径：**不填终止日＝长期有效**，到期提醒一律不提示。
       故：止 = 所有终止日里最晚的一个；**一个终止日都没填 → 返回空串**
       （`_expire_state('')` 判 `none`，天然不进提醒 / KPI / 临期筛选）。
    """
    starts, ends = [], []
    for t in _contract_tiers(c.id):
        if (t.tt_start or '').strip():
            starts.append(t.tt_start.strip())
        if (t.ff_start or '').strip():
            starts.append(t.ff_start.strip())
        if (t.tt_end or '').strip():
            ends.append(t.tt_end.strip())
        if (t.ff_end or '').strip():
            ends.append(t.ff_end.strip())
    for p in CrmContractPlan.query.filter_by(contract_id=c.id).all():
        if (p.tt_start or '').strip():
            starts.append(p.tt_start.strip())
        if (p.ff_start or '').strip():
            starts.append(p.ff_start.strip())
        if (p.tt_end or '').strip():
            ends.append(p.tt_end.strip())
        if (p.ff_end or '').strip():
            ends.append(p.ff_end.strip())
    return (min(starts) if starts else '', max(ends) if ends else '')


def _expire_state(end_date):
    """按「有效期止」算状态，返回 (状态, 距到期天数)。

    none 未设日期 / expired 已过期 / danger 临期 ≤7 天 / warn 临近 ≤30 天 / ok 正常
    """
    d = _pdate(end_date)
    if not d:
        return 'none', None
    n = (d - datetime.now().date()).days
    if n < 0:
        return 'expired', n
    if n <= EXPIRE_DANGER_DAYS:
        return 'danger', n
    if n <= EXPIRE_WARN_DAYS:
        return 'warn', n
    return 'ok', n


def _expiry_alerts(limit=8):
    """临期 / 已过期的合约（按到期日升序，最近的排前面），供列表页顶部与系统总览提醒用。

    ⚠️ 已终止（status='ended'）的合约不再提醒——那是正常结束，不是需要跟进的事。
    """
    out = []
    for c in CrmProductContract.query.all():
        if (c.status or '') == 'ended':
            continue
        _s, e = _contract_span(c)
        st, n = _expire_state(e)
        if st in ('expired', 'danger', 'warn'):
            out.append(dict(c=c, end=e, state=st, days=n,
                            short=c.insurer_short or c.insurer_name or '',
                            name=c.product_name or ''))
    out.sort(key=lambda x: (x['end'] or '9999'))
    return out[:limit]


def _expiry_kpi():
    """合约到期概况：临期（含已过期）与已过期的条数。"""
    expiring = expired = 0
    for c in CrmProductContract.query.all():
        if (c.status or '') == 'ended':
            continue
        _s, e = _contract_span(c)
        st, _n = _expire_state(e)
        if st == 'expired':
            expired += 1
        elif st in ('danger', 'warn'):
            expiring += 1
    return {'expiring': expiring, 'expired': expired}


# ===================== ④ 继续率考核规则库（二期）=====================
# 威哥：「继续率考核基本上多产品都是一个规则」——抽成独立规则库，
# 合约主表 persist_rule_id 引用；改规则 → 所有引用它的合约同步生效。

def _fill_rule(r, form, name=None):
    """把表单值写进规则对象（八改：奖励项目 ↔ 考核标准 ↔ 档位 三者绑定）。

    ⚠️ 本表单**不再读写 product_names / pay_periods**——
       产品关联统一在「产品匹配」页（七改第二步）建立，
       若在这里也写，提交一次规则就会把已匹配的产品清空。

    ⚠️ name 参数是路由算好的最终名。
       九改起规则名**全自动生成**（保司简称 + 继续率考核规则），表单已无 name 输入项，
       这里只认路由传入；早期「表单空值把自动名覆盖成空串」的坑就此绝迹。

    ⚠️ 九改起表单也**不再录入 metric（考核指标说明）**：本模块登记的就是保费继续率，
       该列保留不动以兼容老数据，但不再读写，避免编辑一次就被清空。
    """
    r.name = (name or r.name or '').strip()
    r.has_clawback = _f('has_clawback') == '1'
    r.clawback_std = _f('clawback_std')
    # 八改+：倒扣结构化（起扣线 / 扣费基数）——选「否」时字段隐藏但仍提交，值保留不丢
    r.clawback_line = _to_num(_f('clawback_line'))
    cb = (_f('clawback_base') or '').strip()
    r.clawback_base = cb if cb in dict(CLAWBACK_BASES) else 'all'
    # 十五改：继续率算法口径（caliber）与精度规则（round_mode）已整块删除，
    # 系统统一精度 = 四舍五入 1 位（round_persist），不再按合约登记。
    r.status = _f('status') or 'on'
    r.note = _f('note')
    # ① 保险公司（七改起必选）
    iid = (_f('insurer_id') or '').strip()
    r.insurer_id = int(iid) if iid.isdigit() else None
    ins = Insurer.query.get(r.insurer_id) if r.insurer_id else None
    r.insurer_name = ins.name if ins else ''
    # ② 保司分公司（可多选、可空）—— branch_ids 存 JSON；branch_name 存「、」拼接串
    bids = []
    for x in form.getlist('branch_ids'):
        x = (x or '').strip()
        if x.isdigit() and int(x) not in bids:
            bids.append(int(x))
    r.branch_ids = json.dumps(bids)
    r.branch_id = bids[0] if bids else None          # 兼容旧字段，保留首个
    names = []
    for b in bids:
        br = InsurerBranch.query.get(b)
        if br:
            names.append(br.name)
    r.branch_name = '、'.join(names)
    # ③ 奖励项目（多选：继续率奖金系数 / 长期服务奖系数）
    aw = [x.strip() for x in form.getlist('award_items') if (x or '').strip()]
    r.award_items = ','.join([x for x in aw if x in dict(AWARD_ITEMS)])
    # ④ 继续率考核标准（八改：多选，**仅勾选「继续率奖金系数」时有效**）
    #    未勾继续率奖金系数 → 清空，避免留下与奖励项目无关的残值
    cy = []
    if 'bonus' in aw:
        for x in form.getlist('cycles'):
            x = (x or '').strip()
            if x in PERSIST_CYCLES and x not in cy:
                cy.append(x)
    cy.sort(key=lambda c: PERSIST_CYCLES.index(c))    # 固定按 13→25→37→49 正序
    r.cycles = json.dumps(cy, ensure_ascii=False)
    r.cycle = cy[0] if cy else ''                     # 兼容旧字段，保留首个


def _auto_rule_name(insurer_id):
    """规则名称**全自动生成**（九改）：保司简称（或全称）+ 继续率考核规则。

    威哥口径（九改）：规则名称不再手工填写，系统按保司自动命名。
    """
    ins = Insurer.query.get(insurer_id) if insurer_id else None
    base = (ins.short_name or ins.name) if ins else '通用保司'
    return '%s-继续率考核规则' % base


def _unique_rule_name(base, exclude_id=None):
    """名称唯一化：name 列有唯一约束，同一保司建多条规则时自动追加（2）（3）…

    九改起规则名全自动生成，同一保司可能建多条，必须自动让位，否则会撞唯一约束。
    """
    def _taken(nm):
        q = CrmPersistRule.query.filter(CrmPersistRule.name == nm)
        if exclude_id:
            q = q.filter(CrmPersistRule.id != exclude_id)
        return q.first() is not None

    if not _taken(base):
        return base
    i = 2
    while _taken('%s（%d）' % (base, i)):
        i += 1
    return '%s（%d）' % (base, i)


def _rule_cycles(r):
    """规则的继续率考核标准列表（八改：多选，存 JSON 数组文本）。

    读取时兼容三种历史形态：新列 cycles（JSON）/ 旧单值 cycle / 都没有。
    """
    if not r:
        return []
    try:
        v = json.loads(r.cycles or '[]')
        v = [x for x in v if x in PERSIST_CYCLES] if isinstance(v, list) else []
    except Exception:
        v = []
    if v:
        return v
    old = (r.cycle or '').strip()
    return [old] if old else []


def _fmt_range_vals(lo, hi, min_op=None, max_op=None, empty='—'):
    """按「值 符号 指标 符号 值」渲染区间 —— 供 dict / 矩阵行等无模型对象的场景用。

    与 `_fmt_range(it)` 同一套读法（十四改）：下限符号反转写在数值右侧，上限符号写在指标之后。
    上下限都为 0/空 → 返回 empty（默认 '—'，保持与老展示一致）。
    """
    lo = round_persist(lo or 0)                           # 继续率精度统一：四舍五入 1 位
    hi = round_persist(hi or 0)
    lo_rev = RANGE_OP_REV.get((min_op or '>='), '≤')      # 下限 → 写在数值右侧的「反转」符号
    hs = RANGE_OP_SYM.get((max_op or '<'), '<')            # 上限 → 写在数值左侧的原符号
    if lo and hi:
        return '%.1f%% %s 继续率 %s %.1f%%' % (lo, lo_rev, hs, hi)
    if lo:
        return '%.1f%% %s 继续率' % (lo, lo_rev)
    if hi:
        return '继续率 %s %.1f%%' % (hs, hi)
    return empty


def _fmt_range(it):
    """把档位的「下限/上限 + 运算符」渲染成一句人能看懂的区间（九改 / 十四改改读法）。

    十四改：统一按「值 符号 指标 符号 值」阅读 ——
        上下限都有：80.00% ≤ 继续率 < 90.00%
        只有下限　：90.00% ≤ 继续率
        只有上限　：继续率 < 80.00%
    ⚠️ 九改版把下限写成「80.00% ≥ 继续率」——**方向是反的**（下限 ≥ 80 意为 指标 ≥ 80），
       十四改起用 RANGE_OP_REV 反向字形修正；老数据没有运算符列时按默认值兜底，不报错。
    """
    if not it:
        return '—'
    return _fmt_range_vals(it.rate_min, it.rate_max, it.min_op, it.max_op)


def _save_rule_items(r, form):
    """保存规则的考核档位明细（全量替换）。

    八改：档位**按考核标准分块**提交，块键固定——
      · c0 / c1 / c2 / c3 → 对应 PERSIST_CYCLES 的第 0~3 条继续率考核标准
        行字段：item_min_<块>_<行> / item_max_<块>_<行> /
                item_bonus_<块>_<行> / item_note_<块>_<行>
      · ls               → 长期服务奖系数档位（与月份无关）
        行字段：item_min_ls_<行> / item_max_ls_<行> /
                item_lsc_ls_<行> / item_note_ls_<行>
    九改：每行再多两个边界运算符 item_minop_<块>_<行>（>= / >）
          与 item_maxop_<块>_<行>（< / <=）。
    块是否入库由「奖励项目 + 已选考核标准」决定（前端隐藏块已 disable，不会误提交）。
    """
    for old in CrmPersistRuleItem.query.filter_by(rule_id=r.id).all():
        db.session.delete(old)
    db.session.flush()

    awards = [x for x in (r.award_items or '').split(',') if x]
    cycles = _rule_cycles(r)
    # 按固定顺序组装要保存的块：先各考核标准（13→25→37→49），再长期服务奖
    blocks = []
    if 'bonus' in awards:
        for i, cy in enumerate(PERSIST_CYCLES):
            if cy in cycles:
                blocks.append(('c%d' % i, cy, 'bonus'))
    if 'long_service' in awards:
        blocks.append(('ls', LS_CYCLE, 'lsc'))

    n = 0
    for key, cy, kind in blocks:
        for ix in form.getlist('item_idx_%s' % key):
            lo = _to_num(form.get('item_min_%s_%s' % (key, ix)))
            hi = _to_num(form.get('item_max_%s_%s' % (key, ix)))
            # 九改：边界运算符（是否含等号）——非法值一律兜回默认「左闭右开」
            mop = form.get('item_minop_%s_%s' % (key, ix)) or '>='
            if mop not in dict(RANGE_OPS_MIN):
                mop = '>='
            xop = form.get('item_maxop_%s_%s' % (key, ix)) or '<'
            if xop not in dict(RANGE_OPS_MAX):
                xop = '<'
            nt = (form.get('item_note_%s_%s' % (key, ix)) or '').strip()
            if kind == 'lsc':
                bc, lsc = 0.0, _to_num(form.get('item_lsc_%s_%s' % (key, ix)))
            else:
                bc, lsc = _to_num(form.get('item_bonus_%s_%s' % (key, ix))), 0.0
            if lo == 0 and hi == 0 and bc == 0 and lsc == 0 and not nt:
                continue                      # 整行空白 → 跳过
            db.session.add(CrmPersistRuleItem(rule_id=r.id, cycle=cy,
                                              rate_min=lo, rate_max=hi,
                                              min_op=mop, max_op=xop,
                                              bonus_coef=bc, long_service_coef=lsc,
                                              sort_no=n, note=nt))
            n += 1
    return n


@bp.route('/crm/rules')
@login_required
@permission_required('crm.view')
def rule_list():
    """继续率考核规则列表：规则 + 各档位 + 被多少产品合约引用。"""
    rows = CrmPersistRule.query.order_by(CrmPersistRule.id.desc()).all()
    used = {}
    for r in rows:
        used[r.id] = CrmProductContract.query.filter_by(persist_rule_id=r.id).count()
    items = {}
    for it in CrmPersistRuleItem.query \
            .order_by(CrmPersistRuleItem.sort_no, CrmPersistRuleItem.id).all():
        items.setdefault(it.rule_id, []).append(it)
    # 七改：每条规则展示「适用产品」（第二步「产品匹配」页写入）
    scope = {r.id: _rule_scope(r)[0] for r in rows}
    # 八改：每条规则展示「继续率考核标准」（可多选）
    cyc = {r.id: _rule_cycles(r) for r in rows}
    # 二十五改：档位改成「区间行 × 缴别列」矩阵展示，与合约表单的填法一致
    matrix = {r.id: _rule_matrix(r, items.get(r.id, [])) for r in rows}
    mperiods = {r.id: _matrix_periods(matrix[r.id]) for r in rows}
    return render_template('crm/rule_list.html', rows=rows, used=used, items=items,
                           scope=scope, cycles=cyc, dname=DNAME, ls_cycle=LS_CYCLE,
                           fmt_range=_fmt_range,
                           matrix=matrix, mperiods=mperiods,
                           pcybs=[(cy, _persist_cycle_label(cy)) for cy in PERSIST_BONUS_CYCLES],
                           # 八改+：倒扣基数短名（十五改已删口径/精度短名）
                           cbase_short=CBASE_SHORT, round_persist=round_persist,
                           # 十六改：保司列统一按**简称**展示（简称缺失回落全称）
                           ishort=_insurer_short_map(),
                           can_manage=_can('crm.contract.manage'))


def _rule_form_ctx(row, items):
    """规则新建 / 编辑页共用上下文（八改：奖励项目 ↔ 考核标准 ↔ 档位分块）。"""
    cur_awards = [x for x in ((row.award_items or '') if row else '').split(',') if x]
    if not row and not cur_awards:
        cur_awards = ['bonus']          # 新建时默认勾「继续率奖金系数」，少点一下
    return dict(row=row, items=items, cycles=PERSIST_CYCLES, awards=AWARD_ITEMS,
                ls_cycle=LS_CYCLE, insurers=Insurer.query.order_by(Insurer.name).all(),
                ops_min=RANGE_OPS_MIN, ops_max=RANGE_OPS_MAX,
                ops_left=RANGE_OPS_LEFT, ops_right=RANGE_OPS_RIGHT, fmt_range=_fmt_range,
                # 八改+：倒扣基数（下拉选项）；十五改已删口径/精度（caliber/round_mode）
                clawback_bases=CLAWBACK_BASES,
                cur_cbase=((row.clawback_base or 'all') if row else 'all'),
                branches=_branch_rows(), branch_json=_branch_json(),
                cur_branches=(_rule_branch_ids(row) if row else []),
                cur_awards=cur_awards,
                cur_cycles=(_rule_cycles(row) if row else []),
                # 只读展示：本规则已在「产品匹配」页关联的产品
                rule_products=(_rule_scope(row)[0] if row else []))


@bp.route('/crm/rules/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def rule_new():
    """新建继续率考核规则（七改第一步：建初始续期考核规则）。"""
    if request.method == 'POST':
        iid = (_f('insurer_id') or '').strip()
        if not iid.isdigit():
            flash('请先选择保险公司（七改起为必选）')
            return redirect(url_for('crm.rule_new'))
        if not [x for x in request.form.getlist('award_items')
                if (x or '').strip() in dict(AWARD_ITEMS)]:
            flash('请至少勾选一个奖励项目（继续率奖金系数 / 长期服务奖系数）')
            return redirect(url_for('crm.rule_new'))
        # 九改：规则名**全自动生成**「保司简称-继续率考核规则」，同名自动加序号
        name = _unique_rule_name(_auto_rule_name(int(iid)))
        r = CrmPersistRule(name=name, created_by=session.get('user_id'))
        _fill_rule(r, request.form, name)
        db.session.add(r)
        db.session.flush()
        n = _save_rule_items(r, request.form)
        db.session.commit()
        flash('规则已新增：%s（含 %d 个档位）；下一步可到「产品匹配」关联产品' % (name, n))
        return redirect(url_for('crm.rule_list'))
    return render_template('crm/rule_form.html', **_rule_form_ctx(None, []))


@bp.route('/crm/rules/<int:rid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def rule_edit(rid):
    """编辑规则（改动对所有命中它的合约同步生效）。"""
    r = CrmPersistRule.query.get_or_404(rid)
    if request.method == 'POST':
        iid = (_f('insurer_id') or '').strip()
        if not iid.isdigit():
            flash('请先选择保险公司（七改起为必选）')
            return redirect(url_for('crm.rule_edit', rid=rid))
        if not [x for x in request.form.getlist('award_items')
                if (x or '').strip() in dict(AWARD_ITEMS)]:
            flash('请至少勾选一个奖励项目（继续率奖金系数 / 长期服务奖系数）')
            return redirect(url_for('crm.rule_edit', rid=rid))
        # 九改：名称全自动——换了保司就跟着变；同保司已有多条时自动加序号；
        #       保司没变则保留原名（老数据的历史名称不被强行改掉）
        if r.insurer_id == int(iid) and r.name:
            name = r.name
        else:
            name = _unique_rule_name(_auto_rule_name(int(iid)), rid)
        _fill_rule(r, request.form, name)
        n = _save_rule_items(r, request.form)
        db.session.commit()
        flash('规则已保存：%s（%d 个档位）' % (name, n))
        return redirect(url_for('crm.rule_list'))
    return render_template('crm/rule_form.html', **_rule_form_ctx(r, _rule_items(rid)))


@bp.route('/crm/rules/match', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def rule_match():
    """七改第二步：**产品 → 继续率考核规则** 关联（不用缴别）。

    流程：选保险公司 → 系统自动弹出该保司已录产品 → 产品多选 →
          勾选要关联的规则（默认全选）→ 保存。
    匹配方式：并入（追加去重，默认）／覆盖（用所选产品替换规则的适用产品）。
    """
    iid = (request.args.get('insurer_id') or '').strip()
    if request.method == 'POST':
        iid = (request.form.get('insurer_id') or '').strip()
        if not iid.isdigit():
            flash('请先选择保险公司')
            return redirect(url_for('crm.rule_match'))
        mode = _f('mode') or 'append'
        picked = [x.strip() for x in request.form.getlist('product_names') if (x or '').strip()]
        rids = [int(x) for x in request.form.getlist('rule_ids') if (x or '').strip().isdigit()]
        if not rids:
            flash('请至少选择一条要关联的继续率规则')
            return redirect(url_for('crm.rule_match', insurer_id=iid))
        n = 0
        for r in CrmPersistRule.query.filter(CrmPersistRule.id.in_(rids)).all():
            cur = _rule_scope(r)[0]
            if mode == 'replace':
                new = list(picked)
            else:
                new = cur + [p for p in picked if p not in cur]
            r.product_names = json.dumps(new, ensure_ascii=False)
            n += 1
        db.session.commit()
        flash('已为 %d 条规则%s %d 款产品' % (n, '覆盖为' if mode == 'replace' else '并入',
                                            len(picked)))
        return redirect(url_for('crm.rule_list'))

    cur_ins = int(iid) if iid.isdigit() else None
    if not cur_ins:
        r0 = CrmPersistRule.query.order_by(CrmPersistRule.id.desc()).first()
        cur_ins = r0.insurer_id if r0 else None
    rules = (CrmPersistRule.query.filter_by(insurer_id=cur_ins)
             .order_by(CrmPersistRule.id.desc()).all()) if cur_ins else []
    pre = (request.args.get('rule_id') or '').strip()
    pre_rule = CrmPersistRule.query.get(int(pre)) if pre.isdigit() else None
    pre_products = _rule_scope(pre_rule)[0] if pre_rule else []
    sel_rules = ([pre_rule.id] if pre_rule else [r.id for r in rules])
    r_items = {}
    for it in CrmPersistRuleItem.query.all():
        r_items[it.rule_id] = r_items.get(it.rule_id, 0) + 1
    r_products = {r.id: _rule_scope(r)[0] for r in rules}
    return render_template('crm/rule_match.html',
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           cur_ins=cur_ins, rules=rules,
                           products=_product_options(cur_ins),
                           pre_products=pre_products, pre_rule=pre_rule,
                           sel_rules=sel_rules, r_items=r_items, r_products=r_products,
                           r_cycles={r.id: _rule_cycles(r) for r in rules},
                           dname=DNAME)


def _rule_items(rid):
    """取规则档位明细（按排序号）。"""
    return CrmPersistRuleItem.query.filter_by(rule_id=rid) \
        .order_by(CrmPersistRuleItem.sort_no, CrmPersistRuleItem.id).all()


@bp.route('/crm/rules/<int:rid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def rule_delete(rid):
    """删除规则（仍被合约引用时拦下）。"""
    r = CrmPersistRule.query.get_or_404(rid)
    if CrmProductContract.query.filter_by(persist_rule_id=rid).count():
        flash('该规则已被产品合约引用，不能删除（可先改合约引用或把规则停用）')
        return redirect(url_for('crm.rule_list'))
    for it in CrmPersistRuleItem.query.filter_by(rule_id=rid).all():
        db.session.delete(it)
    db.session.delete(r)
    db.session.commit()
    flash('规则已删除')
    return redirect(url_for('crm.rule_list'))


# ===================== ③ 产品资料库 =====================

@bp.route('/crm/docs')
@login_required
@permission_required('crm.view')
def docs():
    """产品资料库（十八改：与「产品查询」合并后的**统一入口**）。

    威哥口径：管理员手工维护产品资料库；要能**搜产品 → 看到该产品的相关材料**。
    故拆成两个页签，共用一套筛选：
      · tab=product **产品台账**（默认）—— 一行一个产品（合约聚合），行内直接挂该产品的材料；
      · tab=doc     **资料清单**      —— 按资料条目浏览（原产品资料库）。
    """
    tab = (request.args.get('tab') or 'product').strip()
    if tab not in ('product', 'doc'):
        tab = 'product'
    iid = (request.args.get('insurer_id') or '').strip()
    itype = (request.args.get('itype') or '').strip()
    ptype = (request.args.get('ptype') or '').strip()
    dt = (request.args.get('doc_type') or '').strip()
    kw = (request.args.get('kw') or '').strip()

    q = CrmProductDoc.query
    if iid.isdigit():
        q = q.filter(CrmProductDoc.insurer_id == int(iid))
    if dt:
        q = q.filter(CrmProductDoc.doc_type == dt)
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(CrmProductDoc.title.like(like), CrmProductDoc.product_name.like(like),
                            CrmProductDoc.insurer_name.like(like)))
    rows = q.order_by(CrmProductDoc.id.desc()).all()

    allrows = CrmProductDoc.query.all()
    prows = _product_rows(int(iid) if iid.isdigit() else None, itype, ptype, kw)
    kpi = {
        'product': len(prows),
        'total': len(allrows),
        'guide': len([r for r in allrows if (r.doc_type or '') == 'guide']),
        'clause': len([r for r in allrows if (r.doc_type or '') == 'clause']),
        'rate': len([r for r in allrows if (r.doc_type or '') in ('rate', 'cash')]),
    }
    return render_template('crm/doc_list.html', tab=tab, rows=rows, prows=prows, kpi=kpi,
                           iid=iid, dt=dt, kw=kw, itype=itype, ptype=ptype,
                           # 从产品台账点「上传」过来的：预填保司与产品名称
                           pname=(request.args.get('pname') or '').strip(),
                           insurers=Insurer.query.order_by(Insurer.name).all(),
                           doc_types=DOC_TYPES, dname=DNAME, size_text=_size_text,
                           itypes=INSURANCE_TYPES, ptypes=PAY_TYPES,
                           # 十六改：保司列统一按**简称**展示（简称缺失回落全称）
                           ishort=_insurer_short_map(),
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/docs/new', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def doc_new():
    """上传一份产品资料。"""
    iid = (request.form.get('insurer_id') or '').strip()
    it = Insurer.query.get(int(iid)) if iid.isdigit() else None
    if not it:
        flash('请先选择保司')
        return redirect(url_for('crm.docs'))
    if not _f('doc_type'):
        flash('请选择资料类型')
        return redirect(url_for('crm.docs'))
    fs = request.files.get('file')
    if not fs or not (fs.filename or '').strip():
        flash('请选择要上传的文件')
        return redirect(url_for('crm.docs'))
    fn, disk, err = _save_file(fs, 'doc', 'DOC')
    if err:
        flash(err)
        return redirect(url_for('crm.docs'))
    title = _f('title') or fn.rsplit('.', 1)[0]
    cid = (request.form.get('contract_id') or '').strip()
    d = CrmProductDoc(insurer_id=it.id, insurer_name=it.name, product_name=_f('product_name'),
                      contract_id=int(cid) if cid.isdigit() else None,
                      doc_type=_f('doc_type'), title=title, version=_f('version'),
                      file_name=fn, file_path=disk, file_size=0,
                      note=_f('note'), upload_by=session.get('user_id'))
    # 文件大小从落盘文件取（_save_file 已消费过文件流，直接读磁盘最稳）
    try:
        d.file_size = os.path.getsize(os.path.join(_attach_dir('doc'), disk))
    except OSError:
        d.file_size = 0
    db.session.add(d)
    db.session.commit()
    flash('资料已上传：%s' % title)
    return redirect(url_for('crm.docs'))


@bp.route('/crm/docs/<int:did>/download')
@login_required
@permission_required('crm.view')
def doc_download(did):
    """下载产品资料（鉴权下载）。"""
    d = CrmProductDoc.query.get_or_404(did)
    return send_from_directory(_attach_dir('doc'), d.file_path, as_attachment=True,
                               download_name=d.file_name or d.file_path)


@bp.route('/crm/docs/<int:did>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def doc_delete(did):
    """删除产品资料。"""
    d = CrmProductDoc.query.get_or_404(did)
    _drop_file('doc', d.file_path)
    db.session.delete(d)
    db.session.commit()
    flash('资料已删除')
    return redirect(url_for('crm.docs'))


# ===================== ⑤ 业务团队系数设置（二十一改 2026-10-03｜二十二改 2026-10-05）=====
# 威哥口径：对齐「保险佣金查询」小程序的分配机制 —— **系数 × 基准费率**，
#       取代原来的「职级分层往下分比例」（CrmTeamQuota / CrmTeamPolicy 已废弃）。
# 基准费率直接取自现有合约表（不另建底稿表）：
#   · FYC 基准    = CrmContractTier.tt_fyc + ff_fyc（首年佣金率）
#   · 加佣基准    = 系数类方案（transfer/coef）的 plan_total 合计
#   · RYC 基准    = CrmContractTier.ryc_2 ~ ryc_10（续期佣金率）
# ⭐ 二十二改（2026-10-05）：分配前**必须先扣公司承担的税费** ——
#   保司按签约费率结算给我方的是「含税价」（我方开 6% 增值税票、税费我方承担），
#   真实可分配基数是**税后净费率**，所以系数乘的是净费率而非签约费率：
#       含税结算 → −增值税 → −附加税费 → 税后净额 → ×系数 → 团队费用
#   三类系数互相独立、都是百分比数值（如 70 = 70%）：
#     kf = FYC 系数 / kr = RYC 系数 / ka = 加佣方案系数
# ⚠️ 只作用于 FYC + 方案加佣；RYC 用 kr 单独乘；继续率考核 / 增员奖不受影响。


def _tax_of():
    """取全局税费参数 {vat, sur}（%）：增值税率 / 附加税费率；无配置按 6% / 12% 兜底。"""
    row = CrmTaxConfig.query.order_by(CrmTaxConfig.id).first()
    if row is None:
        return {'vat': 6.0, 'sur': 12.0}
    return {'vat': row.vat_rate if row.vat_rate is not None else 6.0,
            'sur': row.surcharge_rate if row.surcharge_rate is not None else 12.0}


def _net_rate(gross_pct, tax=None):
    """含税费率 → 税后净费率，并返回税费明细（二十二改）。

    保司按签约费率结算给我方的是**含税价**（如 62%），我方开 6% 增值税票、税费自担：
        不含税收入 = 含税 ÷ (1 + 增值税率)
        增值税     = 不含税 × 增值税率
        附加税费   = 增值税 × 附加税率
        税后净额   = 含税 − 增值税 − 附加税费
    返回 {'gross','net_ex','vat','sur','net'}，全部为费率百分数。
    """
    t = tax or _tax_of()
    vat_r = (t.get('vat') or 0) / 100.0
    sur_r = (t.get('sur') or 0) / 100.0
    gross = gross_pct or 0
    net_ex = gross / (1 + vat_r) if (1 + vat_r) else gross   # 不含税收入
    vat = net_ex * vat_r
    sur = vat * sur_r
    net = gross - vat - sur
    return {'gross': round(gross, 4), 'net_ex': round(net_ex, 4),
            'vat': round(vat, 4), 'sur': round(sur, 4), 'net': round(net, 4)}


def _org_zones():
    """适用分区下拉：从人力架构（CrmOrgStruct）取去重的「分区名称」（空则用短代号）。"""
    out = []
    for o in CrmOrgStruct.query.order_by(CrmOrgStruct.id).all():
        z = (o.zone_name or o.zone or '').strip()
        if z and z not in out:
            out.append(z)
    return out


def _org_depts():
    """适用营业部下拉：从人力架构（CrmOrgStruct）取去重的「营业部」。"""
    out = []
    for o in CrmOrgStruct.query.order_by(CrmOrgStruct.id).all():
        d = (o.dept or '').strip()
        if d and d not in out:
            out.append(d)
    return out


def _coef_of(team_type, owner_name='', zone='', dept=''):
    """取生效系数 {kf, kr, ka}（二十二改：支持「分区 / 营业部」适用范围）。

    匹配规则（从最具体到最通用，取命中分最高的一条）：
      ① 团队类型 + 分区 + 营业部（+ 渠道负责人）
      ② 团队类型 + 分区 + [不限营业部]
      ③ 团队类型 + [不限分区] + [不限营业部]
    打分：命中「分区」记 2 分、命中「营业部」记 1 分；系数行里空值 = 不限，不参与比较。
    查不到时按 {kf:100, kr:100, ka:100} 兜底（100% = 不放大不缩小）。
    """
    q = CrmTeamCoefficient.query.filter_by(team_type=team_type, status='on')
    if team_type == 'channel':
        q = q.filter_by(owner_name=(owner_name or '').strip())
    rows = q.order_by(CrmTeamCoefficient.sort_no, CrmTeamCoefficient.id).all()
    z = (zone or '').strip()
    d = (dept or '').strip()
    best, best_score = None, -1
    for r in rows:
        rz = (r.scope_zone or '').strip()
        rd = (r.scope_dept or '').strip()
        if rz and rz != z:           # 指定了分区但不匹配 → 跳过
            continue
        if rd and rd != d:           # 指定了营业部但不匹配 → 跳过
            continue
        score = (2 if rz else 0) + (1 if rd else 0)
        if score > best_score:
            best, best_score = r, score
    if best is None:
        return {'kf': 100.0, 'kr': 100.0, 'ka': 100.0}
    return {'kf': best.kf if best.kf is not None else 100.0,
            'kr': best.kr if best.kr is not None else 100.0,
            'ka': best.ka if best.ka is not None else 100.0}


def _coefficient_rows():
    """团队系数清单：按「团队类型 + sort_no + id」排。附带团队类型中文名。"""
    rows = CrmTeamCoefficient.query.order_by(CrmTeamCoefficient.team_type,
                                             CrmTeamCoefficient.sort_no,
                                             CrmTeamCoefficient.id).all()
    type_names = dict(TEAM_COEF_TYPES)
    out = []
    for r in rows:
        out.append({'row': r, 'type_name': type_names.get(r.team_type, r.team_type)})
    return out


def _channel_owners():
    """**渠道负责人**候选（口径统一：以「人力数据清单」为准）。

    威哥口径（2026-10-05 对齐）：渠道负责人只能来自人力清单，取
    「团队类型 = 渠道团队（channel）**且仍在职**」的人员，按录入顺序返回：
        [{'name': 姓名, 'rank': 职级中文名, 'label': '陈志强（渠道总）'}]

    末尾**历史兜底**：系数表里已录、但人力清单已查不到（或已离岗）的负责人
    一并带上，避免编辑老系数时下拉里选不到、一保存就把人名清空。
    ⚠️ 下游 `owners` 一律传这个列表（别再用「全部人力姓名」的宽口径）。
    """
    rank_names = dict(MP_RANKS_CHANNEL)
    out, seen = [], set()
    for m in CrmManpower.query.order_by(CrmManpower.id).all():
        nm = (m.name or '').strip()
        if not nm or nm in seen:
            continue
        if m.team_type != 'channel' or not _mp_active(m):
            continue
        seen.add(nm)
        rn = rank_names.get(m.rank) or _name_of(MP_RANKS, m.rank) or ''
        out.append({'name': nm, 'rank': rn,
                    'label': nm + ('（%s）' % rn if rn else '')})
    for r in CrmTeamCoefficient.query.filter_by(team_type='channel') \
            .order_by(CrmTeamCoefficient.id).all():
        nm = (r.owner_name or '').strip()
        if nm and nm not in seen:
            seen.add(nm)
            out.append({'name': nm, 'rank': '', 'label': nm + '（历史）'})
    return out


def _commission_calc(coef, fyc_pct, plan_pct, premium, tax=None):
    """按「(签约费率 − 公司税费) × 系数」试算（二十二改）。

    链路：含税费率 → 扣增值税+附加 → 税后净费率 → ×系数 → 团队最终费率。
    ⚠️ 先扣公司承担税费、再按系数分配（威哥 2026-10-05 口径）。
    返回的 `*_gross/vat/sur/net` 是税费明细，`*_eff` 是团队最终费率。
    """
    tax = tax or _tax_of()
    kf = (coef.get('kf', 100) or 100) / 100.0
    ka = (coef.get('ka', 100) or 100) / 100.0
    ft = _net_rate(fyc_pct, tax)
    pt = _net_rate(plan_pct, tax)
    fyc_eff = round(ft['net'] * kf, 4)        # 最终 FYC 费率 %
    plan_eff = round(pt['net'] * ka, 4)       # 最终加佣费率 %
    total_eff = round(fyc_eff + plan_eff, 4)  # 最终总费率 %

    def _amt(rate):
        return round(rate / 100 * (premium or 0), 2)

    return {
        'fyc_gross': ft['gross'], 'fyc_vat': ft['vat'], 'fyc_sur': ft['sur'], 'fyc_net': ft['net'],
        'plan_gross': pt['gross'], 'plan_vat': pt['vat'], 'plan_sur': pt['sur'], 'plan_net': pt['net'],
        'fyc_eff': fyc_eff, 'plan_eff': plan_eff, 'total_eff': total_eff,
        'fyc_amount': _amt(fyc_eff), 'plan_amount': _amt(plan_eff),
        'total_amount': _amt(total_eff),
    }


def _ryc_calc(coef, ryc_map, tax=None):
    """RYC 续期佣金：先扣税费得净费率，再 × kr（二十二改：RYC 同样先扣税）。"""
    tax = tax or _tax_of()
    kr = (coef.get('kr', 100) or 100) / 100.0
    out = {}
    for n in range(2, 11):
        net = _net_rate(ryc_map.get(n) or 0, tax)['net']
        out[n] = round(net * kr, 4)
    return out


@bp.route('/crm/team-coefficient', methods=['GET', 'POST'])
@login_required
@permission_required('crm.view')
def team_coefficient():
    """业务团队系数设置：按团队类型配置 kf/kr/ka 三套系数 + 实时试算。

    GET  展示系数清单与试算器；
    POST 只试算（不落库），把选中的合约 / 缴别 / 保费 × 生效系数算一遍。
    """
    rows = _coefficient_rows()
    contracts = CrmProductContract.query.order_by(
        CrmProductContract.insurer_name, CrmProductContract.product_name).all()
    # 渠道负责人下拉：统一以人力清单（渠道团队在职人员）为准（2026-10-05 对齐）
    owners = _channel_owners()

    if request.method == 'POST':
        cid = (request.form.get('contract_id') or '').strip()
        tid = (request.form.get('tier_id') or '').strip()
        premium = (request.form.get('premium') or '0').strip()
        tt = (request.form.get('trial_team_type') or 'basic').strip()
        own = (request.form.get('trial_owner') or '').strip()
    else:
        cid = (request.args.get('contract_id') or '').strip()
        tid = (request.args.get('tier_id') or '').strip()
        premium = (request.args.get('premium') or '100000').strip()
        tt = (request.args.get('trial_team_type') or 'basic').strip()
        own = (request.args.get('trial_owner') or '').strip()
    try:
        premium_v = float(premium.replace(',', '')) if premium else 0.0
    except ValueError:
        premium_v = 0.0

    tax = _tax_of()   # 二十二改：全局税费参数（增值税 + 附加税费）
    trial = None
    picked = None
    picked_tier = None
    if cid.isdigit():
        picked = CrmProductContract.query.get(int(cid))
    if picked:
        tiers = _contract_tiers(picked.id)
        if tiers:
            picked_tier = tiers[0]
            if tid.isdigit():
                for t in tiers:
                    if t.id == int(tid):
                        picked_tier = t
                        break
        coef = _coef_of(tt, own)
        if picked_tier:
            fyc = round((picked_tier.tt_fyc or 0) + (picked_tier.ff_fyc or 0), 2)
            plan_comm, _ps = _plan_comm_of(picked_tier)
            ryc_map = {n: (getattr(picked_tier, 'ryc_%d' % n) or 0) for n in range(2, 11)}
        else:
            fyc = round(sum((t.tt_fyc or 0) + (t.ff_fyc or 0) for t in tiers), 2)
            plan_comm = round(sum(_plan_comm_of(t)[0] for t in tiers), 2)
            ryc_map = {n: round(sum((getattr(t, 'ryc_%d' % n) or 0) for t in tiers), 2)
                       for n in range(2, 11)}
        calc = _commission_calc(coef, fyc, plan_comm, premium_v, tax)
        trial = {
            'coef': coef, 'fyc': fyc, 'plan': plan_comm, 'tax': tax,
            'fyc_gross': calc['fyc_gross'], 'fyc_vat': calc['fyc_vat'],
            'fyc_sur': calc['fyc_sur'], 'fyc_net': calc['fyc_net'],
            'plan_gross': calc['plan_gross'], 'plan_vat': calc['plan_vat'],
            'plan_sur': calc['plan_sur'], 'plan_net': calc['plan_net'],
            'fyc_eff': calc['fyc_eff'], 'plan_eff': calc['plan_eff'],
            'total_eff': calc['total_eff'],
            'fyc_amount': calc['fyc_amount'], 'plan_amount': calc['plan_amount'],
            'total_amount': calc['total_amount'],
            'ryc': _ryc_calc(coef, ryc_map, tax),
            'team_type': tt, 'owner': own,
        }
    # 二十八改：定制类产品按组件录费率后，同一缴别会在多个组件下重复出现 ——
    # 试算器的缴别下拉必须带组件名，否则「20年交」会出现两条一模一样的选项。
    _cmap = {}
    for _c in contracts:
        for _x in _comp_map_of(_c).values():
            _cmap[_x.id] = _x.product_name or ''
    tier_labels = {}
    for _c in contracts:
        for _i, _t in enumerate(_contract_tiers(_c.id)):
            _nm = _cmap.get(_t.component_id) or ''
            tier_labels[_t.id] = ((_nm + ' · ') if _nm else '') + \
                                 (_t.pay_period or '缴别%d' % (_i + 1))
    cjs = json.dumps({str(c.id): [{'id': t.id, 'label': tier_labels.get(t.id, '')}
                                  for t in _contract_tiers(c.id)]
                      for c in contracts}, ensure_ascii=False)
    tjs = json.dumps({str(t.id): {'fyc': round((t.tt_fyc or 0) + (t.ff_fyc or 0), 2)}
                      for c in contracts for t in _contract_tiers(c.id)}, ensure_ascii=False)
    return render_template('crm/team_coefficient.html', rows=rows,
                           contracts=contracts, team_types=TEAM_COEF_TYPES,
                           coef_fields=COEF_FIELDS, owners=owners,
                           picked=picked, picked_tier=picked_tier, trial=trial,
                           tier_labels=tier_labels,
                           premium=premium_v, tax=tax,
                           tax_row=CrmTaxConfig.query.order_by(CrmTaxConfig.id).first(),
                           zones=_org_zones(), depts=_org_depts(),
                           tiers=(_contract_tiers(picked.id) if picked else []),
                           cjs=cjs, tjs=tjs,
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/team-coefficient/save', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def team_coefficient_save():
    """新建 / 编辑一条团队系数（同一「团队类型 + 渠道负责人」只保留一条，重复即覆盖）。"""
    tt = _f('team_type')
    if tt not in dict(TEAM_COEF_TYPES):
        flash('请选择团队类型')
        return redirect(url_for('crm.team_coefficient'))
    own = (_f('owner_name') or '').strip()
    if tt == 'channel' and not own:
        flash('渠道团队必须填写渠道负责人')
        return redirect(url_for('crm.team_coefficient'))
    zone = (_f('scope_zone') or '').strip()   # 适用分区（空 = 总计）
    dept = (_f('scope_dept') or '').strip()   # 适用营业部（空 = 全员）

    def _coef_val(key):
        v = _to_num(request.form.get(key), None)
        if v is None:
            return None
        if not (0 <= v <= 1000):
            return None
        return v

    kf = _coef_val('kf')
    kr = _coef_val('kr')
    ka = _coef_val('ka')
    if kf is None or kr is None or ka is None:
        flash('三个系数需为 0 ~ 1000 之间的数字')
        return redirect(url_for('crm.team_coefficient'))

    cid = (request.form.get('cid') or '').strip()
    row = CrmTeamCoefficient.query.get(int(cid)) if cid.isdigit() else None
    if row is None:
        # 同一「团队类型 + 渠道负责人 + 适用分区 + 适用营业部」只保留一条：没传 cid 时按组合键覆盖
        q = CrmTeamCoefficient.query.filter_by(team_type=tt, owner_name=own,
                                               scope_zone=zone, scope_dept=dept)
        row = q.first()
    if row is None:
        row = CrmTeamCoefficient(created_by=session.get('user_id'))
    row.team_type = tt
    row.owner_name = own
    row.scope_zone = zone
    row.scope_dept = dept
    row.kf = kf
    row.kr = kr
    row.ka = ka
    row.name = _f('name') or (dict(TEAM_COEF_TYPES).get(tt, tt) + (' · ' + own if own else ''))
    row.sort_no = int(request.form.get('sort_no') or 0)
    row.status = _f('status') or 'on'
    row.note = _f('note')
    if row.id is None:
        db.session.add(row)
    db.session.commit()
    tn = dict(TEAM_COEF_TYPES).get(tt, tt)
    scope_txt = ' · ' + (zone or '总计') + ' / ' + (dept or '全员')
    flash('团队系数已保存：%s%s%s · kf=%.2f%% kr=%.2f%% ka=%.2f%%'
          % (tn, (' · ' + own if own else ''), scope_txt, kf, kr, ka))
    return redirect(url_for('crm.team_coefficient'))


@bp.route('/crm/team-coefficient/tax', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def team_tax_save():
    """保存全局税费参数（二十二改）：增值税率 / 附加税费率（全局一套）。"""
    vat = _to_num(request.form.get('vat_rate'), None)
    sur = _to_num(request.form.get('surcharge_rate'), None)
    if vat is None or sur is None or not (0 <= vat <= 100) or not (0 <= sur <= 100):
        flash('税率需为 0 ~ 100 之间的数字')
        return redirect(url_for('crm.team_coefficient'))
    row = CrmTaxConfig.query.order_by(CrmTaxConfig.id).first()
    if row is None:
        row = CrmTaxConfig(status='on')
        db.session.add(row)
    row.vat_rate = vat
    row.surcharge_rate = sur
    row.name = _f('name') or row.name or '默认税费参数'
    row.note = _f('note') or row.note
    row.status = 'on'
    row.updated_by = session.get('user_id')
    row.updated_at = datetime.utcnow()
    db.session.commit()
    flash('税费参数已保存：增值税 %.2f%% · 附加税费 %.2f%%' % (vat, sur))
    return redirect(url_for('crm.team_coefficient'))


@bp.route('/crm/team-coefficient/<int:cid>/toggle', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def team_coefficient_toggle(cid):
    """启用 / 停用某条团队系数。"""
    row = CrmTeamCoefficient.query.get_or_404(cid)
    row.status = 'off' if row.status == 'on' else 'on'
    db.session.commit()
    flash('系数已%s' % ('停用' if row.status == 'off' else '启用'))
    return redirect(url_for('crm.team_coefficient'))


@bp.route('/crm/team-coefficient/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def team_coefficient_delete(cid):
    """删除某条团队系数。"""
    row = CrmTeamCoefficient.query.get_or_404(cid)
    db.session.delete(row)
    db.session.commit()
    flash('团队系数已删除')
    return redirect(url_for('crm.team_coefficient'))


# ===================== ⑤-2 销售费用清单（二十二改 2026-10-05）=====================
# 威哥口径：完成系数设置后，要能**直接查询对应人员的销售费用清单**（权限复用 crm.view）。
# 一行 = 一个产品合约的一个缴别；费用一律「**先扣公司税费、再乘对应团队系数**」：
#     首年佣金 = (总总FYC + 分分FYC) 扣税后 × kf
#     加佣     = (总总方案加佣 + 分分方案加佣) 扣税后 × ka
#     首月小计 = 首年佣金 + 加佣
#     续年期佣金 n = RYC_n 扣税后 × kr
# 「费用适用团队」= 该系数适用的 团队类型 / 适用分区 / 适用营业部。


def _sales_fee_rows(team_type='basic', owner='', zone='', dept=''):
    """销售费用清单数据（一行 = 一个产品合约的一个缴别）。返回 (rows, coef, tax)。

    二十八改（2026-10-29）：定制类产品按组件录费率后，组件行以「组件」为险种名称/代码/险种类型
    各占一行（费率各算各的，不会重复计数）；无组件的行仍取合约本体的产品信息。
    """
    tax = _tax_of()
    coef = _coef_of(team_type, owner, zone, dept)
    rows = []
    for c in CrmProductContract.query.order_by(
            CrmProductContract.insurer_name, CrmProductContract.product_name).all():
        comps = _comp_map_of(c)
        for t in _contract_tiers(c.id):
            cp = comps.get(t.component_id or 0)
            fyc_base = round((t.tt_fyc or 0) + (t.ff_fyc or 0), 2)
            plan_base, ps = _plan_comm_of(t)
            ryc_base = {n: (getattr(t, 'ryc_%d' % n) or 0) for n in range(2, 11)}
            calc = _commission_calc(coef, fyc_base, plan_base, 0, tax)
            ryc = _ryc_calc(coef, ryc_base, tax)
            desc = ''
            if ps:
                desc = (getattr(ps[0], 'plan_text', '') or getattr(ps[0], 'name', '') or '')
            rows.append({
                'insurer': (c.insurer_short or c.insurer_name or ''),
                'product': ((cp.product_name or '') if cp else (c.product_name or '')),
                'custom': (c.product_name or '') if cp else '',
                'ins_type': ((cp.insurance_type or '') if cp else (c.insurance_type or '')),
                'note': ((cp.note or '') if cp else (c.note or '')),
                'code': ((cp.product_code or '') if cp else (c.product_code or '')),
                'period': t.pay_period or '',
                'fyc': round(calc['fyc_eff'], 2),
                'plan': round(calc['plan_eff'], 2),
                'subtotal': round(calc['fyc_eff'] + calc['plan_eff'], 2),
                'desc': desc,
                'ryc': {n: round(ryc[n], 2) for n in range(2, 6)},
            })
    return rows, coef, tax


@bp.route('/crm/sales-fee')
@login_required
@permission_required('crm.view')
def sales_fee():
    """销售费用清单：按「团队类型 / 适用分区 / 适用营业部 / 渠道负责人」查询费用明细。"""
    tt = (request.args.get('team_type') or 'basic').strip()
    own = (request.args.get('owner') or '').strip()
    zone = (request.args.get('zone') or '').strip()
    dept = (request.args.get('dept') or '').strip()
    if tt not in dict(TEAM_COEF_TYPES):
        tt = 'basic'
    rows, coef, tax = _sales_fee_rows(tt, own, zone, dept)
    owners = _channel_owners()
    return render_template('crm/sales_fee.html', rows=rows, coef=coef, tax=tax,
                           team_types=TEAM_COEF_TYPES, owners=owners,
                           zones=_org_zones(), depts=_org_depts(),
                           sel={'team_type': tt, 'owner': own, 'zone': zone, 'dept': dept})


# ===================== ⑥ 业务团队 · 人力架构基础信息（十九改 2026-10-02）=================
# 威哥口径：**录人之前先建架构**。层级：省公司 → 分支 → 分区 → 营业部 → 营业组。
# 一条记录 = 一个营业组（最末级），上级链冗余存同一行，读起来最直观。
# 分区 / 营业部 / 营业组编码由系统自动生成（层级式，一眼看出从属）：
#     分区 FQ001 ／ 营业部 FQ001-B01 ／ 营业组 FQ001-B01-G01
# 新建时同一「省公司+分支+分区」复用同一分区编码，避免一个分区被建成 FQ001 / FQ002 两套。


def _org_zone_code():
    """下一个分区编码（全局递增：FQ001、FQ002…）。"""
    mx = 0
    for r in CrmOrgStruct.query.all():
        c = (r.zone_code or '').strip()
        if c.startswith('FQ') and c[2:].isdigit():
            mx = max(mx, int(c[2:]))
    return 'FQ%03d' % (mx + 1)


def _org_child_code(parent_code, kind, existing):
    """在 parent_code 下生成子级编码：kind='B' 营业部 / 'G' 营业组。

    existing 传「同级已有编码」，编号从 01 起递增（取最大值 +1，删中间行也不会撞号）。
    """
    pre = '%s-%s' % (parent_code, kind)
    n = 0
    for c in existing:
        c = (c or '').strip()
        if c.startswith(pre) and c[len(pre):].isdigit():
            n = max(n, int(c[len(pre):]))
    return '%s%02d' % (pre, n + 1)


def _org_zone_of(province, branch, zone, zone_name):
    """找同「省公司 + 分支 + 分区」的已有记录 —— 命中就复用它的分区编码。"""
    for r in CrmOrgStruct.query.filter_by(province=province, branch=branch).all():
        if (r.zone or '') == zone and (r.zone_name or '') == zone_name:
            return r
    return None


def _org_options():
    """人员表单用的架构下拉：[{'id','label','province','branch','group'}]。"""
    return [{'id': r.id, 'label': r.path, 'province': r.province or '',
             'branch': r.branch or '', 'group': r.group_name or ''}
            for r in CrmOrgStruct.query.order_by(CrmOrgStruct.province, CrmOrgStruct.branch,
                                                 CrmOrgStruct.zone_code, CrmOrgStruct.dept_code,
                                                 CrmOrgStruct.group_code).all()]


def _org_path(org_id, orgs=None):
    """按 id 取架构全路径（取不到返回空串）。"""
    if not org_id:
        return ''
    for o in (orgs if orgs is not None else _org_options()):
        if o['id'] == org_id:
            return o['label']
    return ''


@bp.route('/crm/org-struct')
@login_required
@permission_required('crm.view')
def org_struct():
    """人力架构基础信息：层级清单 + 每级数量 + 每个营业组挂了多少人。"""
    rows = CrmOrgStruct.query.order_by(CrmOrgStruct.province, CrmOrgStruct.branch,
                                       CrmOrgStruct.zone_code, CrmOrgStruct.dept_code,
                                       CrmOrgStruct.group_code).all()
    cnt = {}
    for m in CrmManpower.query.all():
        if m.org_id:
            cnt[m.org_id] = cnt.get(m.org_id, 0) + 1
    kpi = {
        'province': len({r.province or '' for r in rows}),
        'branch': len({(r.province or '', r.branch or '') for r in rows}),
        'zone': len({r.zone_code or '' for r in rows}),
        'dept': len({r.dept_code or '' for r in rows}),
        'group': len(rows),
        'people': CrmManpower.query.count(),
    }
    return render_template('crm/org_struct_list.html', rows=rows, kpi=kpi, cnt=cnt,
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/org-struct/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def org_struct_new():
    """新增架构（一条 = 一个营业组，编码自动生成）。"""
    if request.method == 'POST':
        return _org_save(None)
    return render_template('crm/org_struct_form.html', row=None,
                           provinces=_org_provinces(), branches=_org_branches())


@bp.route('/crm/org-struct/<int:oid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def org_struct_edit(oid):
    """编辑架构（编码不变，只改层级名称）。"""
    row = CrmOrgStruct.query.get_or_404(oid)
    if request.method == 'POST':
        return _org_save(row)
    return render_template('crm/org_struct_form.html', row=row,
                           provinces=_org_provinces(), branches=_org_branches())


def _org_provinces():
    """已有省公司清单（表单下拉建议 + 人员表单联动）。"""
    return sorted({r.province for r in CrmOrgStruct.query.all() if r.province})


def _org_branches():
    """已有「省公司 → 分支」清单，供表单二级联动与 datalist 建议。"""
    out = {}
    for r in CrmOrgStruct.query.all():
        if r.province and r.branch:
            out.setdefault(r.province, set()).add(r.branch)
    return {k: sorted(v) for k, v in out.items()}


def _org_save(row):
    """新建 / 编辑人力架构（编码自动生成，同级营业组不允许重名）。"""
    province = _f('province')
    branch = _f('branch')
    zone = _f('zone')
    zone_name = _f('zone_name')
    dept = _f('dept')
    group_name = _f('group_name')
    if not (province and branch and (zone or zone_name) and dept and group_name):
        flash('省公司 / 分支 / 分区 / 营业部 / 营业组 均为必填项')
        return redirect(url_for('crm.org_struct'))
    if row is None:
        z = _org_zone_of(province, branch, zone, zone_name)
        zone_code = z.zone_code if z else _org_zone_code()
        d = CrmOrgStruct.query.filter_by(zone_code=zone_code, dept=dept).first()
        dept_code = d.dept_code if d else _org_child_code(
            zone_code, 'B', [x.dept_code for x in CrmOrgStruct.query.filter_by(
                zone_code=zone_code).all()])
        if CrmOrgStruct.query.filter_by(dept_code=dept_code, group_name=group_name).first():
            flash('该营业部下已有营业组「%s」，不允许重复' % group_name)
            return redirect(url_for('crm.org_struct'))
        group_code = _org_child_code(dept_code, 'G', [x.group_code for x in CrmOrgStruct.query.filter_by(
            dept_code=dept_code).all()])
        row = CrmOrgStruct(created_by=session.get('user_id'), zone_code=zone_code,
                           dept_code=dept_code, group_code=group_code)
    else:
        dup = CrmOrgStruct.query.filter(CrmOrgStruct.id != row.id,
                                        CrmOrgStruct.dept_code == row.dept_code,
                                        CrmOrgStruct.group_name == group_name).first()
        if dup:
            flash('该营业部下已有营业组「%s」，不允许重复' % group_name)
            return redirect(url_for('crm.org_struct'))
    row.province, row.branch = province, branch
    row.zone, row.zone_name = zone, zone_name
    row.dept, row.group_name = dept, group_name
    row.note = _f('note')
    if row.id is None:
        db.session.add(row)
    db.session.commit()
    flash('架构已保存：%s（分区 %s / 营业部 %s / 营业组 %s）'
          % (row.path, row.zone_code, row.dept_code, row.group_code))
    return redirect(url_for('crm.org_struct'))


@bp.route('/crm/org-struct/<int:oid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def org_struct_delete(oid):
    """删除架构：**已挂人员**的营业组不允许删（先把人挪走）。"""
    row = CrmOrgStruct.query.get_or_404(oid)
    used = CrmManpower.query.filter_by(org_id=row.id).count()
    if used:
        flash('该营业组下还挂着 %d 人，不能删除' % used)
        return redirect(url_for('crm.org_struct'))
    db.session.delete(row)
    db.session.commit()
    flash('架构已删除：%s' % row.path)
    return redirect(url_for('crm.org_struct'))


# ===================== ⑤-2 合作经代公司基础信息（十九改++ 2026-10-02）=====================
# 威哥口径：人力档案的「所属经代公司」要有**维护端口**，登记规则**参照保司基础信息 /
# 保司省分公司**（工商注册 + 许可证 + 法人 + 地址 + 对接人 + 合作状态）。
# 建好后，人力档案的经代公司下拉只从这里取值。

AGENCY_TYPES = [('agent', '保险代理'), ('broker', '保险经纪'),
                ('adjust', '保险公估'), ('other', '其他')]
AGENCY_STATUS = [('coop', '合作中'), ('paused', '暂停合作'), ('none', '未合作')]


def _agency_dup(name, exclude_id=None):
    """经代公司全称是否已被占用（编辑时排除自己）。"""
    nm = (name or '').strip()
    if not nm:
        return True                      # 空名直接判重，避免建出无名记录
    q = CrmAgency.query.filter(CrmAgency.name == nm)
    if exclude_id:
        q = q.filter(CrmAgency.id != exclude_id)
    return q.first() is not None


def _agency_apply(row, form):
    """表单 → CrmAgency（新建 / 编辑共用）。"""
    def g(k):
        v = form.get(k)
        return ('' if v is None else str(v)).strip()

    row.name = g('name')
    row.short_name = g('short_name')
    row.credit_code = g('credit_code')
    row.license_no = g('license_no')
    row.tax_no = g('tax_no')
    row.legal_person = g('legal_person')
    row.reg_capital = g('reg_capital')
    row.establish_date = _mp_date(form.get('establish_date'))
    row.address = g('address')
    row.phone = g('phone')
    row.website = g('website')
    row.business_scope = g('business_scope')
    row.agency_type = g('agency_type') or 'agent'
    row.status = g('status') or 'coop'
    row.contact_person = g('contact_person')
    row.contact_title = g('contact_title')
    row.contact_phone = g('contact_phone')
    row.note = g('note')
    return row


@bp.route('/crm/agencies')
@login_required
@permission_required('crm.view')
def agencies():
    """合作经代公司：列表 + 关键词 / 类型 / 状态筛选 + 每家挂了多少人。"""
    kw = (request.args.get('kw') or '').strip()
    tp = (request.args.get('tp') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = CrmAgency.query
    if tp:
        q = q.filter_by(agency_type=tp)
    if st:
        q = q.filter_by(status=st)
    if kw:
        like = '%%%s%%' % kw
        q = q.filter(db.or_(CrmAgency.name.like(like), CrmAgency.short_name.like(like),
                            CrmAgency.credit_code.like(like),
                            CrmAgency.license_no.like(like),
                            CrmAgency.contact_person.like(like)))
    rows = q.order_by(CrmAgency.status, CrmAgency.short_name, CrmAgency.name).all()
    # 每家经代挂了多少人：人力表里存的是**下拉里选中的那个名字**（简称优先），
    # 所以全称 / 简称都算同一家，避免显示成 0
    cnt = {}
    used = {}
    for m in CrmManpower.query.all():
        nm = (m.agency or '').strip()
        if nm:
            used[nm] = used.get(nm, 0) + 1
    for a in CrmAgency.query.all():
        cnt[a.id] = sum(used.get(x, 0) for x in {(a.name or '').strip(),
                                                 (a.short_name or '').strip()} if x)
    allrows = CrmAgency.query.all()
    kpi = {'total': len(allrows),
           'coop': len([r for r in allrows if r.status == 'coop']),
           'paused': len([r for r in allrows if r.status == 'paused']),
           'people': CrmManpower.query.count()}
    return render_template('crm/agency_list.html', rows=rows, kpi=kpi, cnt=cnt,
                           types=AGENCY_TYPES, statuses=AGENCY_STATUS,
                           kw=kw, tp=tp, st=st, can_manage=_can('crm.contract.manage'))


@bp.route('/crm/agencies/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def agency_new():
    """新增合作经代公司。"""
    if request.method == 'POST':
        return _agency_save(None)
    return render_template('crm/agency_form.html', row=None,
                           types=AGENCY_TYPES, statuses=AGENCY_STATUS)


@bp.route('/crm/agencies/<int:aid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def agency_edit(aid):
    """编辑合作经代公司。"""
    row = CrmAgency.query.get_or_404(aid)
    if request.method == 'POST':
        return _agency_save(row)
    return render_template('crm/agency_form.html', row=row,
                           types=AGENCY_TYPES, statuses=AGENCY_STATUS)


def _agency_save(row):
    """新建 / 编辑落库：**全称必填且唯一**。"""
    nm = _f('name')
    if not nm:
        flash('经代公司名称不能为空')
        return redirect(url_for('crm.agencies'))
    if _agency_dup(nm, row.id if row else None):
        flash('经代公司「%s」已存在，不允许重复录入' % nm)
        return redirect(url_for('crm.agencies'))
    if row is None:
        row = CrmAgency(created_by=session.get('user_id'))
    _agency_apply(row, request.form)
    if row.id is None:
        db.session.add(row)
    db.session.commit()
    flash('合作经代已保存：%s' % nm)
    return redirect(url_for('crm.agencies'))


@bp.route('/crm/agencies/<int:aid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def agency_delete(aid):
    """删除合作经代：**名下挂了人的不让删**（先把人挪走）。"""
    row = CrmAgency.query.get_or_404(aid)
    used = CrmManpower.query.filter_by(agency=row.name).count()
    if not used and row.short_name:
        used = CrmManpower.query.filter_by(agency=row.short_name).count()
    if used:
        flash('该经代公司名下还挂着 %d 人，不能删除' % used)
        return redirect(url_for('crm.agencies'))
    db.session.delete(row)
    db.session.commit()
    flash('合作经代已删除：%s' % (row.name or ''))
    return redirect(url_for('crm.agencies'))


# ===================== ⑥ 业务团队 · 人力数据清单（十九改 2026-10-02）=====================
# 威哥口径：做「业务团队费用设置」先要有人员底表 —— 费用政策是**发给谁**的，
#   团队类型两大类：
#     · basic   基本法团队：统一套一套**标准化**费用政策（不分层，全员同一口径）
#     · channel 渠道团队：可**分层级**（渠道总 → 团队长 → 营销员）下发不同费用政策
# 业务主键＝代理人工号（代码层判重，不建数据库唯一约束，避免批量导入整批失败）；
# 在职状态**不落库**，由离职时间推导（空 或 晚于今天 = 在职），避免两处数据打架。

def _mp_active(m, today=None):
    """在职判定：离职时间为空，或离职时间晚于今天 → 在职。"""
    d = (m.leave_date or '').strip()
    if not d:
        return True
    return d > (today or datetime.now().strftime('%Y-%m-%d'))


def _mp_date(v):
    """日期规范化：Excel 里的 datetime / 2026-10-01 / 2026/10/1 一律收成 YYYY-MM-DD 字符串。"""
    if v is None:
        return ''
    if hasattr(v, 'strftime'):        # openpyxl 读出来的是 datetime
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    if not s or s.lower() in ('none', 'nan', 'nat'):
        return ''
    return s.replace('/', '-').replace('.', '-')


def _idcard_info(idcard):
    """18 位身份证 → (性别, 出生日期)；识别不了返回 (None, None)。

    第 17 位奇数为男、偶数为女；第 7~14 位是出生日期（YYYYMMDD）。
    只用于**自动补全**（导入 / 表单填身份证后带出性别与出生年月），不覆盖已填内容。
    """
    s = re.sub(r'\s', '', idcard or '')
    if len(s) != 18:
        return None, None
    try:
        birth = '%s-%s-%s' % (s[6:10], s[10:12], s[12:14])
        gender = 'male' if int(s[16]) % 2 == 1 else 'female'
        return gender, birth
    except (ValueError, IndexError, TypeError):
        return None, None


def _mp_dup(agent_no, exclude_id=None):
    """**代理人工号**是否已被别人占用（编辑时排除自己）。

    ⚠️ 代理人工号是**选填**的（不是每个人都在海腾上号），空值不参与判重。
    """
    no = (agent_no or '').strip()
    if not no:
        return False
    q = CrmManpower.query.filter(CrmManpower.agent_no == no)
    if exclude_id:
        q = q.filter(CrmManpower.id != exclude_id)
    return q.first() is not None


def _mp_dup_sys(sys_no, exclude_id=None):
    """**系统工号**是否已存在（系统生成，导入按它判重）。"""
    no = (sys_no or '').strip()
    if not no:
        return False
    q = CrmManpower.query.filter(CrmManpower.sys_no == no)
    if exclude_id:
        q = q.filter(CrmManpower.id != exclude_id)
    return q.first() is not None


def _mp_next_sys_no():
    """下一个**系统工号**：SA-001 起全局递增（取现有最大序号 +1，删中间行也不会撞号）。"""
    mx = 0
    for r in CrmManpower.query.all():
        c = (r.sys_no or '').strip().upper()
        if c.startswith('SA-') and c[3:].isdigit():
            mx = max(mx, int(c[3:]))
    return 'SA-%03d' % (mx + 1)


def _org_tree():
    """人力架构 → **五级级联下拉**（省公司 → 分支 → 分区 → 营业部 → 营业组）用的嵌套结构。

    结构（十九改++：分区 / 营业部 / 营业组**都带编码**，下拉里显示「名称（编码）」）：

        {省: {分支: {分区名: {'c': 分区码, 'd': {营业部名: {'c': 部码,
                    'g': [{'id': 营业组id, 'name': 组名, 'code': 组码}]}}}}}}

    ⭐ 关键：**只有最后一级（营业组）带 id** —— 前四级纯粹是筛选路径，
    这样就不会出现「选了省级却提交了别级 id」的错乱；表单只需提交营业组 id（`org_id`）。
    遍历顺序按编码排（省/分支/分区码/部码/组码），dict 插入顺序即展示顺序。
    """
    tree = {}
    rows = CrmOrgStruct.query.order_by(CrmOrgStruct.province, CrmOrgStruct.branch,
                                       CrmOrgStruct.zone_code, CrmOrgStruct.dept_code,
                                       CrmOrgStruct.group_code).all()
    for r in rows:
        prov = r.province or ''
        br = r.branch or ''
        zone = r.zone_name or r.zone or ''
        dept = r.dept or ''
        z = tree.setdefault(prov, {}).setdefault(br, {}).setdefault(zone, {'c': '', 'd': {}})
        z['c'] = z['c'] or (r.zone_code or '')          # 同一分区可能有多条（多个营业部）
        d = z['d'].setdefault(dept, {'c': '', 'g': []})
        d['c'] = d['c'] or (r.dept_code or '')
        d['g'].append({'id': r.id, 'name': r.group_name or '', 'code': r.group_code or ''})
    return tree


def _org_node(org_id):
    """某个营业组在五级级联里的各级取值（编辑时逐级回填用）。取不到返回 {}。"""
    if not org_id:
        return {}
    r = CrmOrgStruct.query.get(org_id)
    if not r:
        return {}
    return {'province': r.province or '', 'branch': r.branch or '',
            'zone': r.zone_name or r.zone or '', 'zone_code': r.zone_code or '',
            'dept': r.dept or '', 'dept_code': r.dept_code or '',
            'group_id': r.id, 'group': r.group_name or '',
            'group_code': r.group_code or ''}


def _agency_options():
    """**所属经代公司**下拉候选（十九改++）：以「合作经代」基础信息表为准。

    取值顺序：① CrmAgency 里**合作中 / 暂停**的（按简称排序，简称空退回全称）；
    ② 内置清单 MP_AGENCIES（表还没建数据时也能选）；③ 人力表里已存在的历史值兜底，
    避免老数据在新下拉里「选不到、一保存就被清空」。
    """
    ags = []
    for a in CrmAgency.query.order_by(CrmAgency.short_name, CrmAgency.name).all():
        nm = (a.short_name or a.name or '').strip()
        if nm:
            ags.append(nm)
    for x in MP_AGENCIES:
        if x not in ags:
            ags.append(x)
    for m in CrmManpower.query.all():
        nm = (m.agency or '').strip()
        if nm and nm not in ags:
            ags.append(nm)
    return ags


def _mp_ref_map():
    """推荐人候选：姓名 →（系统工号 / 代理人工号 / 职级）。

    威哥口径：填**推荐人姓名**后，推荐人工号自动识别 —— 前端按这个表回填工号与职级。
    """
    out = []
    for m in CrmManpower.query.order_by(CrmManpower.name).all():
        if not m.name:
            continue
        no = m.agent_no or m.sys_no or ''
        out.append({'name': m.name, 'no': no,
                    'rank': _name_of(MP_RANKS, m.rank) or ''})
    return out


def _mp_fix_rank(rank, team_type):
    """把职级**规整到该团队类型允许的档位内**（十九改++）。

    基本法团队只允许 部经理/主管/营销员；渠道团队只允许 渠道总/团队长/营销员。
    前端已经按团队类型重建下拉，这里是**后端兜底**（导入 / 老数据 / 接口调用都可能带错值）：
    同义职级按 CROSS_RANK 折算（渠道总↔部经理、团队长↔主管），折算不了退回营销员。
    """
    valid = [c for c, _ in RANKS_OF_TYPE.get(team_type, MP_RANKS_CHANNEL)]
    if rank in valid:
        return rank
    x = CROSS_RANK.get(rank)
    if x in valid:
        return x
    return 'agent'


def _mp_apply(row, form):
    """把一行数据写进 CrmManpower（新建 / 编辑 / 批量导入共用）。

    form 可以是 request.form，也可以是导入时拼出来的 dict——两者都有 .get()。
    """
    def g(k):
        v = form.get(k)
        return ('' if v is None else str(v)).strip()

    # 系统工号：由系统生成，表单不提交 → 保留原值（新建时在 _mp_save / 导入里补号）
    row.sys_no = g('sys_no') or (row.sys_no or '')
    row.agent_no = g('agent_no')
    row.agency = g('agency')
    row.name = g('name')
    # 十九改++：**职级编码删除**（无用），一律清空，不再从表单 / Excel 读
    row.rank_code = ''
    row.sale_level = g('sale_level')
    row.hire_date = _mp_date(form.get('hire_date'))
    row.leave_date = _mp_date(form.get('leave_date'))
    # 推荐人：**先填姓名**、工号由前端按 _mp_ref_map 自动带出（导入时按姓名反查兜底）
    row.ref_name = g('ref_name')
    row.ref_no = g('ref_no')
    row.ref_rank = g('ref_rank')
    if row.ref_name and not row.ref_no:
        ref = CrmManpower.query.filter_by(name=row.ref_name).first()
        if ref:
            row.ref_no = ref.agent_no or ref.sys_no or ''
            if not row.ref_rank:
                row.ref_rank = _name_of(MP_RANKS, ref.rank) or ''
    row.idcard = g('idcard')
    row.gender = g('gender')
    row.birth_date = _mp_date(form.get('birth_date'))
    # 职级**随团队类型**：基本法＝部经理/主管/营销员，渠道＝渠道总/团队长/营销员
    row.team_type = g('team_type') or 'channel'
    row.rank = _mp_fix_rank(g('rank'), row.team_type)
    # 十九改++：**省公司 / 分支 / 所属团队不再手工录入**（组织架构归属已选好，重复且易打架），
    # 一律由所挂营业组带出；所属团队＝营业组名（表单与 Excel 都不再出现这三个字段的录入项）
    # ⚠️ 只有提交了 org_id 才按架构改写：批量导入（dict 可能无该键）走下面的兜底分支
    if 'org_id' in form:
        oid = g('org_id')
        if oid.isdigit() and int(oid) > 0:
            row.org_id = int(oid)
            row.org_path = _org_path(row.org_id)
            nd = _org_node(row.org_id)
            if nd:                       # 架构＝权威口径
                row.province = nd['province'] or ''
                row.branch = nd['branch'] or ''
                row.team_name = nd['group'] or ''
        else:
            row.org_id, row.org_path = None, ''
            row.province = row.branch = row.team_name = ''
    else:                                # 批量导入：没有架构列 → 省/分支按表里填的，团队取营业组名
        row.province = g('province') or row.province
        row.branch = g('branch') or row.branch
        if not row.org_id:
            row.team_name = g('team_name') or row.team_name or ''
    row.bank_name = g('bank_name')
    row.bank_branch = g('bank_branch')
    row.bank_card = g('bank_card')
    row.signed = g('signed') or 'no'
    row.sign_date = _mp_date(form.get('sign_date'))
    row.note = g('note')
    # 性别 / 出生年月空着时，从身份证自动带出（不覆盖已填）
    gd, bt = _idcard_info(row.idcard)
    if not row.gender and gd:
        row.gender = gd
    if not row.birth_date and bt:
        row.birth_date = bt
    return row


def _mp_form(row):
    """人力表单渲染（新建 / 编辑共用）：下拉数据全在这里备齐。

    十九改++（威哥）：
      · 组织架构归属五级级联，**每级选项都带编码标识**（分区 FQ001 / 营业部 FQ001-B01 …）；
      · **省公司 / 分支 / 所属团队不再出现在表单里**（归属已选好，重复且易打架），由所挂营业组带出；
      · **职级按团队类型联动**（基本法：部经理/主管/营销员；渠道：渠道总/团队长/营销员）；
      · 经代公司下拉取「合作经代」基础信息表。
    """
    return render_template('crm/manpower_form.html', row=row, orgs=_org_options(),
                           org_tree=_org_tree(),
                           cur_org=_org_node(row.org_id if row else None),
                           team_types=TEAM_TYPES, ranks=MP_RANKS,
                           ranks_of_type={k: [list(x) for x in v]
                                          for k, v in RANKS_OF_TYPE.items()},
                           rank_names={c: n for c, n in MP_RANKS},
                           levels=SALE_LEVELS,
                           grade_scope=SALE_GRADE_SCOPE, banks=BANKS,
                           genders=MP_GENDERS, yesno=MP_YESNO,
                           agencies=_agency_options(),
                           ref_map=_mp_ref_map())


def _mp_row_values(m):
    """一行记录 → 导出用的 24 列（顺序同 MP_HEADERS）。

    十九改++：表头删掉「职级编码」「所属团队」两列（职级编码无用、所属团队＝所挂营业组）。
    """
    return [m.sys_no or '', m.province or '', m.branch or '', m.agent_no or '',
            m.agency or '', m.name or '',
            _name_of(MP_RANKS, m.rank), m.sale_level or '',
            m.hire_date or '', m.leave_date or '', m.ref_name or '', m.ref_no or '',
            m.ref_rank or '',
            m.idcard or '', _name_of(MP_GENDERS, m.gender), m.birth_date or '',
            _name_of(TEAM_TYPES, m.team_type), m.org_path or '',
            m.bank_name or '', m.bank_branch or '',
            m.bank_card or '', _name_of(MP_YESNO, m.signed), m.sign_date or '', m.note or '']


@bp.route('/crm/manpower')
@login_required
@permission_required('crm.view')
def manpower():
    """人力数据清单：列表 + 筛选 + KPI（两类团队人数 / 在职 / 已签协议）。"""
    kw = (request.args.get('kw') or '').strip()
    tt = (request.args.get('tt') or '').strip()        # 团队类型 basic / channel
    rk = (request.args.get('rk') or '').strip()        # 职级
    st = (request.args.get('st') or 'all').strip()     # 在职状态 all / active / left
    ag = (request.args.get('ag') or '').strip()        # 所属经代公司
    sg = (request.args.get('sg') or '').strip()        # 是否签署渠道协议
    lv = (request.args.get('lv') or '').strip()        # 销售资质等级（一级~四级）
    q = CrmManpower.query
    if tt:
        q = q.filter_by(team_type=tt)
    if rk:
        q = q.filter_by(rank=rk)
    if lv:
        q = q.filter_by(sale_level=lv)
    if ag:
        q = q.filter_by(agency=ag)
    if sg:
        q = q.filter_by(signed=sg)
    if kw:
        like = '%%%s%%' % kw
        q = q.filter(db.or_(CrmManpower.name.like(like), CrmManpower.agent_no.like(like),
                            CrmManpower.agency.like(like), CrmManpower.team_name.like(like),
                            CrmManpower.ref_no.like(like), CrmManpower.branch.like(like)))
    rows = q.order_by(CrmManpower.team_type, CrmManpower.rank, CrmManpower.agent_no).all()
    if st == 'active':
        rows = [r for r in rows if _mp_active(r)]
    elif st == 'left':
        rows = [r for r in rows if not _mp_active(r)]
    # KPI 按**全量**算（不受上面的筛选影响，方便对着看总盘）
    allrows = CrmManpower.query.all()
    kpi = {
        'total': len(allrows),
        'active': len([r for r in allrows if _mp_active(r)]),
        'left': len([r for r in allrows if not _mp_active(r)]),
        'basic': len([r for r in allrows if r.team_type == 'basic']),
        'channel': len([r for r in allrows if r.team_type == 'channel']),
        'signed': len([r for r in allrows if r.signed == 'yes']),
        # 十九改++：职级分两套（基本法 部经理/主管/营销员；渠道 渠道总/团队长/营销员）
        'boss': len([r for r in allrows if r.rank in ('channel_boss', 'dept_manager')]),
        'leader': len([r for r in allrows if r.rank in ('team_leader', 'supervisor')]),
        'agent': len([r for r in allrows if r.rank == 'agent']),
    }
    # 筛选项：经代公司 / 所属团队 从库里现取（避免维护第二套字典）
    agencies = sorted({(r.agency or '') for r in allrows if r.agency})
    teams = sorted({(r.team_name or '') for r in allrows if r.team_name})
    return render_template('crm/manpower_list.html', rows=rows, kpi=kpi,
                           team_types=TEAM_TYPES, ranks=MP_RANKS, levels=SALE_LEVELS,
                           rank_names={c: n for c, n in MP_RANKS},
                           genders=MP_GENDERS, yesno=MP_YESNO,
                           agencies=agencies, teams=teams, grade_scope=SALE_GRADE_SCOPE,
                           kw=kw, tt=tt, rk=rk, st=st, ag=ag, sg=sg, lv=lv,
                           active_of=_mp_active, can_manage=_can('crm.contract.manage'))


@bp.route('/crm/manpower/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def manpower_new():
    """新增人力档案。"""
    if request.method == 'POST':
        return _mp_save(None)
    return _mp_form(None)


@bp.route('/crm/manpower/<int:mid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def manpower_edit(mid):
    """编辑人力档案。"""
    row = CrmManpower.query.get_or_404(mid)
    if request.method == 'POST':
        return _mp_save(row)
    return _mp_form(row)


def _mp_save(row):
    """新建 / 编辑的落库动作。

    校验口径（威哥 2026-10-02）：
      · **姓名必填**；
      · **代理人工号（海腾上号的工号）选填** —— 填了就必须唯一，没填不拦；
      · **系统工号（海盈通保）由系统自动生成** SA-001 格式，不让人工填、也不参与表单校验。
    """
    if not _f('name'):
        flash('姓名不能为空')
        return redirect(url_for('crm.manpower'))
    no = _f('agent_no')
    if no and _mp_dup(no, row.id if row else None):
        flash('代理人工号 %s 已存在，不允许重复录入' % no)
        return redirect(url_for('crm.manpower'))
    if row is None:
        row = CrmManpower(created_by=session.get('user_id'))
    _mp_apply(row, request.form)
    if not row.sys_no:
        row.sys_no = _mp_next_sys_no()        # 系统工号自动生成
    if row.id is None:
        db.session.add(row)
    db.session.commit()
    # 三个非强制电子版附件（传新的覆盖旧的；勾了「删除」就清掉）
    for kind in MP_ATTACH:
        fs = request.files.get(kind)
        fn, disk, err = _save_file(fs, 'manpower', row.sys_no or 'MP')
        if err:
            flash(err)
        elif disk:
            _drop_file('manpower', getattr(row, kind))
            setattr(row, kind, disk)
            setattr(row, kind + '_name', fn)
        elif request.form.get('del_' + kind) == '1':
            _drop_file('manpower', getattr(row, kind))
            setattr(row, kind, None)
            setattr(row, kind + '_name', None)
    db.session.commit()
    flash('人力档案已保存：%s · 系统工号 %s' % (row.name or '', row.sys_no))
    return redirect(url_for('crm.manpower'))


@bp.route('/crm/manpower/<int:mid>/file/<kind>')
@login_required
@permission_required('crm.view')
def manpower_file(mid, kind):
    """下载人力档案的电子版附件（kind = sign_file / idcard_file / bank_file），鉴权下载。"""
    if kind not in MP_ATTACH:
        abort(404)
    row = CrmManpower.query.get_or_404(mid)
    disk = getattr(row, kind)
    if not disk:
        abort(404)
    return send_from_directory(_attach_dir('manpower'), disk, as_attachment=True,
                               download_name=getattr(row, kind + '_name') or disk)


@bp.route('/crm/manpower/<int:mid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def manpower_delete(mid):
    """删除人力档案（只删清单本身，不影响任何费用 / 佣金数据）。"""
    row = CrmManpower.query.get_or_404(mid)
    nm = row.name or row.agent_no or ''
    for kind in MP_ATTACH:          # 电子版附件一并清掉，不留孤儿文件
        _drop_file('manpower', getattr(row, kind))
    db.session.delete(row)
    db.session.commit()
    flash('已删除人力档案：%s' % nm)
    return redirect(url_for('crm.manpower'))


@bp.route('/crm/manpower/export')
@login_required
@permission_required('crm.view')
def manpower_export():
    """导出人力数据清单（列顺序同导入模板，可直接改完再导回来）。"""
    rows = CrmManpower.query.order_by(CrmManpower.team_type, CrmManpower.rank,
                                      CrmManpower.agent_no).all()
    data = [_mp_row_values(r) for r in rows]
    return _xlsx_response('人力数据清单', '业务团队 · 人力数据清单', MP_HEADERS, data)


@bp.route('/crm/manpower/template')
@login_required
@permission_required('crm.view')
def manpower_template():
    """下载导入模板（表头 + 一行示例）。"""
    # ⚠️ 列必须与 MP_HEADERS 同序（十九改++：删掉「职级编码」「所属团队」两列 → 24 列）
    demo = ['', '广东省', '广州本部', 'A0001', '广州海盈保险代理有限公司', '张三',
            '渠道总', '一级', '2024-01-01', '', '李四', 'A0000', '团队长',
            '440101199001011234', '男',
            '1990-01-01', '渠道团队',
            '广东省 / 广州本部 / 广州第一分区 / 天河营业部 / 广州一区',
            '中国银行', '广州天河支行', '6217000000000000001',
            '是', '2024-01-05', '示例行：导入前请删除本行']
    return _xlsx_response('导入模板', '人力数据清单 · 导入模板（按列顺序填写，第 1 行为表头）',
                          MP_HEADERS, [demo])


@bp.route('/crm/manpower/import', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def manpower_import():
    """Excel 批量导入：按**表头名**匹配列（顺序颠倒也能认），代理人工号重复则跳过。"""
    fs = request.files.get('file')
    if not fs or not (fs.filename or '').strip():
        flash('请选择要导入的 Excel 文件')
        return redirect(url_for('crm.manpower'))
    if not fs.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('仅支持 .xlsx 格式')
        return redirect(url_for('crm.manpower'))
    # 表头名 → 字段名的对照（导出表头即导入表头，两边共用 MP_HEADERS）
    FIELD_OF = {'系统工号': 'sys_no', '省公司': 'province', '分支': 'branch',
                '代理人工号': 'agent_no',
                '所属经代公司': 'agency', '姓名': 'name', '职级编码': 'rank_code',
                '职级': 'rank', '销售资质等级': 'sale_level', '入司时间': 'hire_date',
                '离职时间': 'leave_date', '推荐人姓名': 'ref_name', '推荐人工号': 'ref_no',
                '推荐人职级': 'ref_rank',
                '身份证号码': 'idcard', '性别': 'gender', '出生年月': 'birth_date',
                '团队类型': 'team_type', '所属团队': 'team_name', '所属架构': 'org_path',
                '开户行': 'bank_name', '开户支行': 'bank_branch', '银行卡号': 'bank_card',
                '是否签署渠道协议': 'signed',
                '签署日期': 'sign_date', '备注': 'note'}
    # 老表头「销售能力等级」仍认（十九改+ 改名前的导出文件能直接导回来）
    FIELD_OF['销售能力等级'] = 'sale_level'
    RANK_OF = {v: k for k, v in MP_RANKS}
    TT_OF = {v: k for k, v in TEAM_TYPES}
    GD_OF = {v: k for k, v in MP_GENDERS}
    YN_OF = {v: k for k, v in MP_YESNO}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(fs, data_only=True)
        ws = wb.active
        head_map = {}
        first = next(ws.iter_rows(values_only=True), None)
        if first:                       # 有表头 → 按列名取值（更抗造）
            for i, v in enumerate(first):
                key = ('' if v is None else str(v)).strip()
                if key in FIELD_OF:
                    head_map[FIELD_OF[key]] = i
        use_pos = not head_map          # 没表头 → 退回按列顺序（MP_HEADERS 顺序）
        added, skipped, blank = 0, 0, 0
        n0 = int(_mp_next_sys_no()[3:])  # 本批自动补号的起始序号
        for row in ws.iter_rows(min_row=2 if first else 1, values_only=True):
            if not row or all(v is None for v in row):
                continue
            if use_pos:
                d = {FIELD_OF[h]: (row[i] if i < len(row) else '')
                     for i, h in enumerate(MP_HEADERS)}
            else:
                def gv(k):
                    i = head_map.get(k)
                    if i is None or i >= len(row) or row[i] is None:
                        return ''
                    return str(row[i]).strip()
                d = {k: gv(k) for k in FIELD_OF.values()}
            # 判空 / 判重：姓名必填；系统工号（有值则按它判重）；代理人工号选填、填了才判重
            if not (d.get('name') or '').strip():
                blank += 1
                continue
            no = (d.get('agent_no') or '').strip()
            sno = (d.get('sys_no') or '').strip()
            if sno and _mp_dup_sys(sno):
                skipped += 1
                continue
            if not sno and no and _mp_dup(no):
                skipped += 1
                continue
            d['sys_no'] = sno or ('SA-%03d' % (n0 + added))   # 没给系统工号就自动补
            # 旧等级（A/B/C/D 级）折算成资质等级；不在四档里的原样留着，不拦
            lv = (d.get('sale_level') or '').strip()
            d['sale_level'] = OLD_SALE_LEVEL_OF.get(lv, lv)
            d['rank'] = RANK_OF.get((d.get('rank') or '').strip(), 'agent')
            d['team_type'] = TT_OF.get((d.get('team_type') or '').strip(), 'channel')
            d['gender'] = GD_OF.get((d.get('gender') or '').strip(), '')
            sg = (d.get('signed') or '').strip()
            d['signed'] = YN_OF.get(sg, 'yes' if sg in ('是', 'Y', 'y', '1') else 'no')
            # 所属架构（可选列）：能按末段（营业组名）匹配到已建架构就自动挂上
            op = (d.get('org_path') or '').strip()
            if op:
                hit = CrmOrgStruct.query.filter_by(
                    group_name=op.replace('\\', '/').split('/')[-1].strip()).first()
                if hit:
                    d['org_id'] = str(hit.id)
            r = CrmManpower(created_by=session.get('user_id'))
            _mp_apply(r, d)
            db.session.add(r)
            added += 1
        db.session.commit()
        flash('导入完成：新增 %d 条（未填系统工号的已自动补号），跳过已存在 %d 条，缺姓名忽略 %d 条'
              % (added, skipped, blank))
    except Exception as e:
        db.session.rollback()
        flash('导入失败：%s' % e)
    return redirect(url_for('crm.manpower'))


# ===================== 业绩管理 · 投保订单录入（2026-10）=====================
# 威哥口径：一个模块「投保订单录入」= 新单录入（手工）+ 新单导入（Excel）两端口汇总。
# 自动带出 / 自动测算：代理人架构（工号）、客户编码（身份证）、性别/出生/年龄、
# 行业标保折标率、受理件数、长短险标识、自互保件标识、代理人佣金（佣金系数）。


def _policy_build(row, form):
    """把一份数据（request.form 或导入 dict，都有 .get）写进 CrmPolicyOrder 并算自动字段。

    自动带出 / 自动测算逻辑集中在此，手工录入与 Excel 导入共用，保证两端口口径一致。
    """
    def g(k):
        v = form.get(k)
        return ('' if v is None else str(v)).strip()

    # —— 代理人（工号 → 带出架构；查不到时允许手填兜底）——
    no = g('agent_no')
    row.agent_no = no
    row.agency_platform = g('agency_platform')
    ag = _agent_by_no(no) if no else None
    if ag:
        row.agent_name = ag['agent_name']
        row.agent_province = ag['agent_province']
        row.agent_branch = ag['agent_branch']
        row.agent_agency = ag['agent_agency']
        row.agent_org = ag['agent_org']
    else:
        row.agent_name = g('agent_name')
        row.agent_province = g('agent_province')
        row.agent_branch = g('agent_branch')
        row.agent_agency = g('agent_agency')
        row.agent_org = g('agent_org')

    # —— 投保人 ——
    row.app_form_no = g('app_form_no')
    row.policy_no = g('policy_no')
    row.app_name = g('app_name')
    row.app_idcard = g('app_idcard')
    gd, bt = _idcard_info(row.app_idcard)
    row.app_gender = gd or g('app_gender')
    row.app_birth = bt or g('app_birth')
    row.app_age = _calc_age(row.app_birth) if row.app_birth else (_to_num(g('app_age'), None) or None)
    row.app_mobile = g('app_mobile')
    row.app_addr_province = g('app_addr_province')
    row.app_addr_city = g('app_addr_city')
    row.app_addr_county = g('app_addr_county')
    row.app_addr_street = g('app_addr_street')
    row.app_bank_name = g('app_bank_name')
    row.app_bank_account = g('app_bank_account')
    cu, _ = _upsert_customer(row.app_idcard, name=row.app_name, gender=row.app_gender,
                            birth_date=row.app_birth, age=row.app_age, mobile=row.app_mobile,
                            addr_province=row.app_addr_province, addr_city=row.app_addr_city,
                            addr_county=row.app_addr_county, addr_street=row.app_addr_street,
                            bank_name=row.app_bank_name, bank_account=row.app_bank_account)
    row.app_code = cu.customer_code if cu else ''

    # —— 被保人 ——（勾选「同一人」时直接复用投保人，免重复录入；前端会把被保人输入框禁用不提交）
    same = g('same_as_app') == '1'
    if same:
        row.ins_name = row.app_name
        row.ins_idcard = row.app_idcard
        row.ins_gender = row.app_gender
        row.ins_birth = row.app_birth
        row.ins_age = row.app_age
        row.ins_mobile = row.app_mobile
        row.ins_addr_province = row.app_addr_province
        row.ins_addr_city = row.app_addr_city
        row.ins_addr_county = row.app_addr_county
        row.ins_addr_street = row.app_addr_street
        row.ins_bank_name = row.app_bank_name
        row.ins_bank_account = row.app_bank_account
    else:
        row.ins_name = g('ins_name')
        row.ins_idcard = g('ins_idcard')
        igd, ibt = _idcard_info(row.ins_idcard)
        row.ins_gender = igd or g('ins_gender')
        row.ins_birth = ibt or g('ins_birth')
        row.ins_age = _calc_age(row.ins_birth) if row.ins_birth else (_to_num(g('ins_age'), None) or None)
        row.ins_mobile = g('ins_mobile')
        row.ins_addr_province = g('ins_addr_province')
        row.ins_addr_city = g('ins_addr_city')
        row.ins_addr_county = g('ins_addr_county')
        row.ins_addr_street = g('ins_addr_street')
    ciu, _ = _upsert_customer(row.ins_idcard, name=row.ins_name, gender=row.ins_gender,
                             birth_date=row.ins_birth, age=row.ins_age, mobile=row.ins_mobile,
                             addr_province=row.ins_addr_province, addr_city=row.ins_addr_city,
                             addr_county=row.ins_addr_county, addr_street=row.ins_addr_street)
    row.ins_code = ciu.customer_code if ciu else ''

    # —— 保单状态 / 产品 ——
    row.policy_main_status = g('policy_main_status')
    row.policy_sub_status = g('policy_sub_status')
    row.policy_risk_status = g('policy_risk_status')
    row.insurer_name = g('insurer_name')
    row.product_name = g('product_name')
    pi = _product_info(row.insurer_name, row.product_name)
    if pi:
        row.product_code = pi['product_code']
        row.insurance_type = pi['insurance_type']
    else:
        row.product_code = g('product_code')
        row.insurance_type = g('insurance_type')
    row.plan = g('plan')

    # —— 缴费 / 年期 + 自动测算（行业标保 / 受理件数 / 长短险）——
    row.pay_method = g('pay_method')
    row.ins_period_flag = g('ins_period_flag')
    row.ins_period = g('ins_period')
    py_raw = _to_num(g('pay_years'))          # 表单录入的实际缴费年限（年），空=0
    py = _pay_years(row.pay_method, row.ins_period, py_raw or None)
    row.pay_years = py                        # 落库实际年限（10 年及以上封顶 10 由折标率处 min 处理）
    coef = min(py, 10)                       # 折标系数：趸交=1，期交=缴费年期，10 年及以上封顶 10
    row.accept_count = _accept_count_of(py)
    row.ls_flag = _ls_flag_of(py)
    row.industry_std_rate = _industry_std_rate(py)   # 折标率% = min(缴费年期×10, 100)

    # —— 保费（2 位）——
    row.sum_assured = _to_num(g('sum_assured'))
    row.basic_premium = _to_num(g('basic_premium'))
    row.extra_premium = _to_num(g('extra_premium'))
    row.add_premium = _to_num(g('add_premium'))
    row.health_load = _to_num(g('health_load'))
    row.scale_premium = _to_num(g('scale_premium'))
    # 行业标保(元) = 规模保费 × 折标率% ÷ 100（曾漏除 100，导致放大 10 倍）
    row.industry_std_premium = _round2((row.scale_premium or 0) * (row.industry_std_rate or 0) / 100.0)

    # —— 佣金系数（优先带出产品 FYC，可手改）+ 代理人佣金 ——
    cr = g('commission_rate')
    if cr == '':
        cr = pi['fyc'] if pi else 0
    row.commission_rate = _to_num(cr)
    row.commission = _round2(row.scale_premium * (row.commission_rate or 0) / 100.0)

    # —— 关键日期 / 标识 ——
    row.apply_date = g('apply_date')
    row.underwrite_date = g('underwrite_date')
    row.effective_date = g('effective_date')
    row.callback_date = g('callback_date')
    row.cancel_date = g('cancel_date')
    row.cancel_reason = g('cancel_reason')
    row.order_type = g('order_type')
    row.auto_loan = g('auto_loan') or 'no'
    # —— 手工录单强制标记为在职单；Excel 导入单保留原保单类型（src='import'）——
    row.policy_kind = 'in_service' if (g('src') or 'manual') == 'manual' else g('policy_kind')
    row.dual_record_flag = g('dual_record_flag') or 'no'
    row.dual_record_type = g('dual_record_type')
    row.dual_record_status = g('dual_record_status')
    row.tax_pref_flag = g('tax_pref_flag')
    row.self_policy_flag = _self_policy_flag_of(row.app_idcard, row.ins_idcard)
    row.note = g('note')
    # —— 数据来源：手工录入默认 manual；Excel 导入时调用方会把 form['src'] 设为 'import' ——
    row.src = g('src') or 'manual'
    return row


# 中国大陆省级行政区（投保订单「家庭住址·省份」下拉；不含港澳台）
CN_PROVINCES = ['北京市', '天津市', '河北省', '山西省', '内蒙古自治区', '辽宁省', '吉林省', '黑龙江省',
                '上海市', '江苏省', '浙江省', '安徽省', '福建省', '江西省', '山东省', '河南省',
                '湖北省', '湖南省', '广东省', '广西壮族自治区', '海南省', '重庆市', '四川省', '贵州省',
                '云南省', '西藏自治区', '陕西省', '甘肃省', '青海省', '宁夏回族自治区', '新疆维吾尔自治区']

# 省 → 市 → 县区 三级联动数据（投保订单「家庭住址」下拉）。
# 广东省为业务大本营，地市/县区录全；直辖市按「市→区」结构；其余省份列主要地市，
# 县区统一给「市辖区」兜底（如需更细可在本字典补）。结构：{省: {市: [县区,...]}}
from regions_cn import CN_REGION




def _policy_form_ctx(row):
    """录入 / 编辑页所需的下拉与预载数据。"""
    insurers = sorted({(c.insurer_name or '') for c in
                       CrmProductContract.query.filter(CrmProductContract.insurer_name.isnot(None)).all()
                       if c.insurer_name})
    agencies = [a.name for a in CrmAgency.query.order_by(CrmAgency.short_name, CrmAgency.name).all()]
    prod_map = {}
    for c in CrmProductContract.query.order_by(CrmProductContract.insurer_name,
                                               CrmProductContract.product_name).all():
        if c.insurer_name and c.product_name:
            prod_map.setdefault(c.insurer_name, [])
            if c.product_name not in prod_map[c.insurer_name]:
                prod_map[c.insurer_name].append(c.product_name)
    return {
        'row': row,
        'provinces': CN_PROVINCES,
        'regions': CN_REGION,
        'insurers': insurers,
        'agencies': agencies,
        'prod_map': prod_map,
        'pay_methods': PAY_METHODS, 'period_flags': PERIOD_FLAGS, 'periods': PERIODS,
        'pay_years_opts': PAY_YEARS_OPTS,
        'main_status': POLICY_MAIN_STATUS, 'sub_status': POLICY_SUB_STATUS, 'risk_status': POLICY_RISK_STATUS,
        'order_types': ORDER_TYPES, 'policy_kinds': POLICY_KINDS, 'ls_flags': LS_FLAGS,
        'dual_types': DUAL_TYPES, 'dual_status': DUAL_STATUS, 'self_flags': SELF_FLAGS,
        'attach_kinds': POLICY_ATTACH, 'attach_labels': POLICY_ATTACH_LABEL,
        'insurance_types': INSURANCE_TYPES, 'yesno': MP_YESNO, 'banks': BANKS,
        'can_manage': _can('crm.contract.manage'),
    }


def _policy_row_values(r):
    """一行投保订单 → 导出用的 41 列（顺序同 POLICY_HEADERS）。"""
    return [
        r.agency_platform or '', r.agent_no or '', r.app_form_no or '', r.policy_no or '',
        r.app_name or '', r.app_idcard or '', r.app_mobile or '',
        r.app_addr_province or '', r.app_addr_city or '', r.app_addr_county or '', r.app_addr_street or '',
        r.app_bank_name or '', r.app_bank_account or '',
        r.ins_name or '', r.ins_idcard or '', r.ins_mobile or '',
        r.ins_addr_province or '', r.ins_addr_city or '', r.ins_addr_county or '', r.ins_addr_street or '',
        r.policy_main_status or '', r.policy_sub_status or '', r.policy_risk_status or '',
        r.insurer_name or '', r.product_name or '',
        r.plan or '',
        _round2(r.sum_assured), _round2(r.basic_premium), _round2(r.extra_premium),
        _round2(r.add_premium), _round2(r.health_load), _round2(r.scale_premium),
        _round2(r.commission_rate),
        _name_of(PAY_METHODS, r.pay_method), _name_of(PERIOD_FLAGS, r.ins_period_flag),
        _name_of(PERIODS, r.ins_period),
        r.apply_date or '', r.underwrite_date or '', r.effective_date or '', r.callback_date or '',
        r.cancel_date or '', r.cancel_reason or '',
        _name_of(ORDER_TYPES, r.order_type), _name_of(MP_YESNO, r.auto_loan),
        _name_of(POLICY_KINDS, r.policy_kind),
        _name_of(MP_YESNO, r.dual_record_flag), _name_of(DUAL_TYPES, r.dual_record_type),
        _name_of(DUAL_STATUS, r.dual_record_status),
        _name_of(MP_YESNO, r.tax_pref_flag),
        r.note or '',
        r.pay_years or '',
    ]


def _policy_calc_sa(insurer, product, sex, age, pay_term, mode, value):
    """投保订单「保额 ↔ 规模保费」互算：复用保费测算因子表（base_unit 折算）。

    因子表 policy_year=0 为费率行，sa = 每 base_unit(默认1000) 保费对应的基本保额。
    关系（与 _premium_calc_rows 一致）：base_sa = premium / base * sa。
    mode='sa'      → 已知保额，算规模保费 = 保额 / sa * base
    mode='premium' → 已知规模保费，算保额 = 规模保费 / base * sa
    年龄精确匹配优先，找不到则取最近年龄行（返回 approx_age 提示前端）。
    """
    rows = CrmPremiumFactor.query.filter_by(
        insurer_name=insurer, product_name=product, pay_term=pay_term, policy_year=0).all()
    if not rows:
        return None, '该产品未配置费率表（保费测算模块未导入），无法自动换算，请手动填写保额与规模保费'
    norm = {'男': '男', '女': '女', 'male': '男', 'female': '女', 'M': '男', 'F': '女'}
    sx = norm.get((sex or '').strip())
    cand = [r for r in rows if norm.get((r.sex or '').strip()) == sx] or [r for r in rows if (r.sa or 0) > 0]
    cand = [r for r in cand if (r.sa or 0) > 0]
    if not cand:
        return None, '该产品的费率行缺少基本保额（sa）数据，无法自动换算'
    ages = sorted({r.issue_age for r in cand})
    try:
        age_i = int(float(age))
    except (TypeError, ValueError):
        age_i = None
    if age_i is not None and age_i in ages:
        pick = [r for r in cand if r.issue_age == age_i]
        approx = False
    elif age_i is not None:
        near = min(ages, key=lambda a: abs(a - age_i))
        pick = [r for r in cand if r.issue_age == near]
        approx = True
    else:
        pick = cand
        approx = True
    r = pick[0]
    base = r.base_unit or 1000.0
    sa_per = r.sa or 0.0
    if sa_per <= 0:
        return None, '费率行基本保额为 0，无法自动换算'
    if mode == 'sa':
        sp = value / sa_per * base
        return {'sum_assured': round(value, 2), 'scale_premium': round(sp, 2), 'approx_age': approx}, None
    sa = value / base * sa_per
    return {'scale_premium': round(value, 2), 'sum_assured': round(sa, 2), 'approx_age': approx}, None


@bp.route('/crm/policy-orders')
@login_required
@permission_required('crm.view')
def policy_orders():
    """投保订单录入：列表 + 筛选（两端口汇总入口）。"""
    kw = (request.args.get('kw') or '').strip()
    ins = (request.args.get('ins') or '').strip()
    no = (request.args.get('no') or '').strip()
    sf = (request.args.get('sf') or '').strip()        # 自互保件标识
    lf = (request.args.get('lf') or '').strip()        # 长短险
    q = CrmPolicyOrder.query
    if ins:
        q = q.filter_by(insurer_name=ins)
    if no:
        q = q.filter(CrmPolicyOrder.agent_no == no)
    if sf:
        q = q.filter_by(self_policy_flag=sf)
    if lf:
        q = q.filter_by(ls_flag=lf)
    rows = q.order_by(CrmPolicyOrder.id.desc()).all()
    if kw:
        like = '%%%s%%' % kw
        rows = [r for r in rows if any(kw in (getattr(r, f) or '') for f in
                ('app_form_no', 'policy_no', 'app_name', 'ins_name', 'agent_no',
                 'insurer_name', 'product_name', 'code'))]
    insurers = sorted({(r.insurer_name or '') for r in CrmPolicyOrder.query.all() if r.insurer_name})
    return render_template('crm/policy_orders.html', rows=rows, kw=kw, ins=ins, no=no,
                           sf=sf, lf=lf, insurers=insurers,
                           self_flags=SELF_FLAGS, ls_flags=LS_FLAGS,
                           nm_pay={c: n for c, n in PAY_METHODS},
                           nm_kind={c: n for c, n in POLICY_KINDS},
                           nm_dual={c: n for c, n in DUAL_TYPES},
                           can_manage=_can('crm.contract.manage'))


@bp.route('/crm/policy-order/new', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def policy_order_new():
    """新单录入（手工端口）。"""
    if request.method == 'POST':
        return _policy_save(None)
    return render_template('crm/policy_order_form.html', **_policy_form_ctx(None))


@bp.route('/crm/policy-order/<int:oid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def policy_order_edit(oid):
    """编辑投保订单。"""
    row = CrmPolicyOrder.query.get_or_404(oid)
    if request.method == 'POST':
        return _policy_save(row)
    return render_template('crm/policy_order_form.html', **_policy_form_ctx(row))


def _policy_save(row):
    """新建 / 编辑落库：校验 → 自动测算 → 附件。"""
    is_new = row is None
    if is_new:
        row = CrmPolicyOrder(created_by=session.get('user_id'))
    if not _f('app_name') or not _f('app_idcard'):
        flash('投保人姓名与身份证号必填')
        return render_template('crm/policy_order_form.html', **_policy_form_ctx(row))
    if _f('same_as_app') != '1':
        if not _f('ins_name') or not _f('ins_idcard'):
            flash('被保险人姓名与身份证号必填')
            return render_template('crm/policy_order_form.html', **_policy_form_ctx(row))
    # 代理人工号查不到 → 报错拒绝（威哥口径）
    no = _f('agent_no')
    if no and _agent_by_no(no) is None:
        flash('代理人工号 %s 在人力清单中查不到，无法录入' % no)
        return render_template('crm/policy_order_form.html', **_policy_form_ctx(row))
    _policy_build(row, request.form)
    if not row.code:
        row.code = _next_code(CrmPolicyOrder, 'PO')
    if row.id is None:
        db.session.add(row)
    db.session.commit()                     # 先落库拿 code/id，再保存附件
    for kind in POLICY_ATTACH:
        fs = request.files.get(kind)
        fn, disk, err = _save_file(fs, 'policy', row.code or 'PO')
        if err:
            flash(err)
        elif disk:
            _drop_file('policy', getattr(row, kind))
            setattr(row, kind, disk)
            setattr(row, kind + '_name', fn)
        elif request.form.get('del_' + kind) == '1':
            _drop_file('policy', getattr(row, kind))
            setattr(row, kind, None)
            setattr(row, kind + '_name', None)
    db.session.commit()
    flash('投保订单已保存：%s' % row.code)
    return redirect(url_for('crm.policy_orders'))


@bp.route('/crm/policy-order/<int:oid>/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def policy_order_delete(oid):
    """删除投保订单（含 8 类附件一并清掉）。"""
    row = CrmPolicyOrder.query.get_or_404(oid)
    code = row.code or ''
    for kind in POLICY_ATTACH:
        _drop_file('policy', getattr(row, kind))
    db.session.delete(row)
    db.session.commit()
    flash('已删除投保订单：%s' % code)
    return redirect(url_for('crm.policy_orders'))


@bp.route('/crm/policy-order/<int:oid>/file/<kind>')
@login_required
@permission_required('crm.view')
def policy_order_file(oid, kind):
    """下载投保订单附件（鉴权）。"""
    if kind not in POLICY_ATTACH:
        abort(404)
    row = CrmPolicyOrder.query.get_or_404(oid)
    disk = getattr(row, kind)
    if not disk:
        abort(404)
    return send_from_directory(_attach_dir('policy'), disk, as_attachment=True,
                               download_name=getattr(row, kind + '_name') or disk)


@bp.route('/crm/policy-order/agent-json')
@login_required
@permission_required('crm.view')
def policy_order_agent_json():
    """代理人工号 → 自动带出架构（JSON，前端录入时调用）。"""
    ag = _agent_by_no(_f('no'))
    if ag is None:
        return json.dumps({'found': False}), 200, {'Content-Type': 'application/json'}
    return json.dumps({'found': True, **ag}, ensure_ascii=False), 200, \
        {'Content-Type': 'application/json'}


@bp.route('/crm/policy-order/product-json')
@login_required
@permission_required('crm.view')
def policy_order_product_json():
    """保险公司 + 产品 → 自动带出 产品编码 / 险种类别 / FYC（JSON）。"""
    pi = _product_info(_f('insurer'), _f('product'))
    if pi is None:
        return json.dumps({'found': False}), 200, {'Content-Type': 'application/json'}
    return json.dumps({'found': True, **pi}, ensure_ascii=False), 200, \
        {'Content-Type': 'application/json'}


@bp.route('/crm/policy-order/customer-json')
@login_required
@permission_required('crm.view')
def policy_order_customer_json():
    """身份证号 → 自动生成/带出客户编码与历史信息（JSON）。"""
    idc = re.sub(r'\s', '', _f('idcard') or '')
    if len(idc) != 18:
        return json.dumps({'found': False}), 200, {'Content-Type': 'application/json'}
    code = _customer_code(idc)
    cu = CrmCustomer.query.filter_by(idcard=idc).first()
    if cu is None:
        return json.dumps({'found': False, 'code': code}, ensure_ascii=False), 200, \
            {'Content-Type': 'application/json'}
    return json.dumps({
        'found': True, 'code': cu.customer_code,
        'name': cu.name or '', 'mobile': cu.mobile or '',
        'bank_name': cu.bank_name or '', 'bank_account': cu.bank_account or '',
        'addr_province': cu.addr_province or '', 'addr_city': cu.addr_city or '',
        'addr_county': cu.addr_county or '', 'addr_street': cu.addr_street or '',
    }, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/policy-order/calc-json')
@login_required
@permission_required('crm.view')
def policy_order_calc_json():
    """投保订单「保额 ↔ 规模保费」互算（GET；复用保费测算因子表）。前端录入时调用。"""
    insurer = (request.args.get('insurer') or '').strip()
    product = (request.args.get('product') or '').strip()
    sex = (request.args.get('sex') or '').strip()
    age = (request.args.get('age') or '').strip()
    pay_term = _to_num(request.args.get('pay_term'))
    mode = (request.args.get('mode') or '').strip()
    try:
        value = float(request.args.get('value') or 0)
    except (TypeError, ValueError):
        value = 0.0
    if not insurer or not product or mode not in ('sa', 'premium') or value <= 0:
        return json.dumps({'ok': False, 'msg': '信息不全，无法自动换算'}), 200, {'Content-Type': 'application/json'}
    res, err = _policy_calc_sa(insurer, product, sex, age, pay_term, mode, value)
    if err:
        return json.dumps({'ok': False, 'msg': err}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    return json.dumps({'ok': True, **res}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/policy-order/export')
@login_required
@permission_required('crm.view')
def policy_order_export():
    """导出投保订单清单（列顺序同导入模板）。"""
    rows = CrmPolicyOrder.query.order_by(CrmPolicyOrder.id.desc()).all()
    data = [_policy_row_values(r) for r in rows]
    return _xlsx_response('投保订单清单', '业绩管理 · 投保订单录入清单', POLICY_HEADERS, data)


@bp.route('/crm/policy-order/template')
@login_required
@permission_required('crm.view')
def policy_order_template():
    """下载 Excel 导入模板（表头 + 一行示例）。"""
    demo = ['广州海盈保险代理有限公司', 'A0001', 'P20260101001', 'P20260101001-1',
            '张三', '440101199001011234', '13800000001', '广东省', '广州市', '天河区', '天河路 1 号',
            '中国银行', '6217000000000000001',
            '李四', '440101199003031234', '13800000002', '广东省', '广州市', '越秀区', '越秀路 2 号',
            '有效', '正常', '正常', '中国人寿', '国寿福',
            '终身寿险计划', '1000000', '5000', '0', '0', '0', '5000',
            '30', '年缴', '年', '终身',
            '2026-01-01', '2026-01-05', '2026-01-06', '', '', '',
            '线上单', '否', '在职单', '双录单', '保司监管双录', '通过', '税优产品',
            '示例行：导入前请删除本行']
    return _xlsx_response('投保订单导入模板',
                          '投保订单录入 · 导入模板（按列顺序填写，第 1 行为表头）',
                          POLICY_HEADERS, [demo])


@bp.route('/crm/policy-order/import', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def policy_order_import():
    """Excel 批量导入：按表头名匹配列（顺序颠倒也能认）。"""
    fs = request.files.get('file')
    if not fs or not (fs.filename or '').strip():
        flash('请选择要导入的 Excel 文件')
        return redirect(url_for('crm.policy_orders'))
    if not fs.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('仅支持 .xlsx 格式')
        return redirect(url_for('crm.policy_orders'))
    # 表头名 → 字段名（与 POLICY_HEADERS 同序；导入只读文本字段，金额/自动字段后端算）
    FIELD_OF = {
        '出单平台': 'agency_platform', '代理人工号': 'agent_no', '投保单号': 'app_form_no',
        '保单号': 'policy_no',
        '投保人姓名': 'app_name', '投保人身份证号': 'app_idcard', '投保人手机号': 'app_mobile',
        '投保人省份': 'app_addr_province', '投保人地市': 'app_addr_city', '投保人县区': 'app_addr_county',
        '投保人街道': 'app_addr_street', '投保人开户行': 'app_bank_name', '投保人银行账号': 'app_bank_account',
        '被保人姓名': 'ins_name', '被保人身份证号': 'ins_idcard', '被保人手机号': 'ins_mobile',
        '被保人省份': 'ins_addr_province', '被保人地市': 'ins_addr_city', '被保人县区': 'ins_addr_county',
        '被保人街道': 'ins_addr_street',
        '保单主状态': 'policy_main_status', '保单子状态': 'policy_sub_status', '险种状态': 'policy_risk_status',
        '保险公司名称': 'insurer_name', '产品名称': 'product_name',
        '投保计划': 'plan', '保额': 'sum_assured', '基本保费': 'basic_premium',
        '额外保费': 'extra_premium', '追加保费': 'add_premium', '职业健康加费': 'health_load',
        '规模保费': 'scale_premium', '佣金系数(%)': 'commission_rate', '缴费方式': 'pay_method',
        '保险年期标志': 'ins_period_flag', '保险年期': 'ins_period',
        '投保日期': 'apply_date', '承保日期': 'underwrite_date', '保单生效日期': 'effective_date',
        '回访成功日期': 'callback_date', '撤单日期': 'cancel_date', '撤单原因': 'cancel_reason',
        '出单类型': 'order_type', '是否自动垫交': 'auto_loan', '保单类型': 'policy_kind',
        '双录单标识': 'dual_record_flag', '双录类型': 'dual_record_type', '双录状态': 'dual_record_status',
        '税优产品标识': 'tax_pref_flag', '备注': 'note', '缴费年期': 'pay_years',
    }
    PM_OF = {v: k for k, v in PAY_METHODS}
    PF_OF = {v: k for k, v in PERIOD_FLAGS}
    PP_OF = {v: k for k, v in PERIODS}
    OT_OF = {v: k for k, v in ORDER_TYPES}
    PK_OF = {v: k for k, v in POLICY_KINDS}
    DT_OF = {v: k for k, v in DUAL_TYPES}
    DS_OF = {v: k for k, v in DUAL_STATUS}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(fs, data_only=True)
        ws = wb.active
        head_map = {}
        first = next(ws.iter_rows(values_only=True), None)
        if first:
            for i, v in enumerate(first):
                key = ('' if v is None else str(v)).strip()
                if key in FIELD_OF:
                    head_map[FIELD_OF[key]] = i
        use_pos = not head_map
        added, skipped, blank = 0, 0, 0
        for row in ws.iter_rows(min_row=2 if first else 1, values_only=True):
            if not row or all(v is None for v in row):
                continue
            if use_pos:
                d = {FIELD_OF[h]: (row[i] if i < len(row) else '')
                     for i, h in enumerate(POLICY_HEADERS)}
            else:
                def gv(k):
                    i = head_map.get(k)
                    if i is None or i >= len(row) or row[i] is None:
                        return ''
                    return str(row[i]).strip()
                d = {k: gv(k) for k in FIELD_OF.values()}
            # 中文值反查为 code
            d['pay_method'] = PM_OF.get((d.get('pay_method') or '').strip(),
                                        'lump' if (d.get('pay_method') or '') == '趸交' else 'year')
            d['ins_period_flag'] = PF_OF.get((d.get('ins_period_flag') or '').strip(), '')
            d['ins_period'] = PP_OF.get((d.get('ins_period') or '').strip(), '')
            d['order_type'] = OT_OF.get((d.get('order_type') or '').strip(), '')
            d['policy_kind'] = PK_OF.get((d.get('policy_kind') or '').strip(), '')
            d['dual_record_type'] = DT_OF.get((d.get('dual_record_type') or '').strip(), '')
            d['dual_record_status'] = DS_OF.get((d.get('dual_record_status') or '').strip(), '')
            d['auto_loan'] = 'yes' if (d.get('auto_loan') or '') in ('是', 'Y', 'y', '1') else 'no'
            d['dual_record_flag'] = 'yes' if (d.get('dual_record_flag') or '') in ('双录单', '是', 'Y', 'y', '1') else 'no'
            d['tax_pref_flag'] = 'yes' if (d.get('tax_pref_flag') or '') in ('税优产品', '是', 'Y', 'y', '1') else ''
            d['src'] = 'import'        # 标记本行来自 Excel 导入
            if not (d.get('app_name') or '').strip() or not (d.get('app_idcard') or '').strip():
                blank += 1
                continue
            if not (d.get('ins_name') or '').strip() or not (d.get('ins_idcard') or '').strip():
                blank += 1
                continue
            no = (d.get('agent_no') or '').strip()
            if no and _agent_by_no(no) is None:
                skipped += 1          # 工号查不到 → 跳过（与手工录入同口径报错拒绝）
                continue
            r = CrmPolicyOrder(created_by=session.get('user_id'))
            r.code = _next_code(CrmPolicyOrder, 'PO')
            _policy_build(r, d)
            db.session.add(r)
            added += 1
        db.session.commit()
        flash('导入完成：新增 %d 条，跳过（工号查不到）%d 条，缺必填忽略 %d 条'
              % (added, skipped, blank))
    except Exception as e:
        db.session.rollback()
        flash('导入失败：%s' % e)
    return redirect(url_for('crm.policy_orders'))


# ===================== 新契约业务清单查询与提取（端口汇总查询） =====================

SRC_MAP = [('manual', '手工录入'), ('import', 'Excel导入')]


def _policy_query_rows():
    """新契约查询：解析筛选条件 → 返回投保订单列表（查询页与导出共用同一套口径）。"""
    kw = (request.args.get('kw') or '').strip()
    ins = (request.args.get('ins') or '').strip()
    no = (request.args.get('no') or '').strip()
    ap = (request.args.get('ap') or '').strip()          # 省公司
    ab = (request.args.get('ab') or '').strip()          # 分支
    pn = (request.args.get('pn') or '').strip()          # 产品
    it = (request.args.get('it') or '').strip()          # 险种
    sf = (request.args.get('sf') or '').strip()          # 自互保件
    lf = (request.args.get('lf') or '').strip()          # 长短险
    pm = (request.args.get('pm') or '').strip()          # 缴费方式
    rs = (request.args.get('rs') or '').strip()          # 险种状态
    ds = (request.args.get('ds') or '').strip()          # 数据来源
    dstart = (request.args.get('dstart') or '').strip()  # 投保日期起
    dend = (request.args.get('dend') or '').strip()      # 投保日期止
    q = CrmPolicyOrder.query
    if ins: q = q.filter_by(insurer_name=ins)
    if no: q = q.filter(CrmPolicyOrder.agent_no == no)
    if ap: q = q.filter_by(agent_province=ap)
    if ab: q = q.filter_by(agent_branch=ab)
    if pn: q = q.filter(CrmPolicyOrder.product_name == pn)
    if it: q = q.filter_by(insurance_type=it)
    if sf: q = q.filter_by(self_policy_flag=sf)
    if lf: q = q.filter_by(ls_flag=lf)
    if pm: q = q.filter_by(pay_method=pm)
    if rs: q = q.filter_by(policy_risk_status=rs)
    if ds: q = q.filter_by(src=ds)
    if dstart: q = q.filter(CrmPolicyOrder.apply_date >= dstart)
    if dend: q = q.filter(CrmPolicyOrder.apply_date <= dend)
    rows = q.order_by(CrmPolicyOrder.id.desc()).all()
    if kw:
        flds = ('app_form_no', 'policy_no', 'app_name', 'ins_name', 'agent_no', 'agent_name',
                'insurer_name', 'product_name', 'code', 'app_code', 'ins_code', 'app_addr_province')
        rows = [r for r in rows if any(kw in (getattr(r, f) or '') for f in flds)]
    return rows


def _policy_query_row_values(r):
    """新契约查询导出：一行一列（含数据来源标记 + 关键经营维度）。"""
    return [
        r.code or '',
        '手工录入' if r.src == 'manual' else 'Excel导入' if r.src == 'import' else (r.src or ''),
        r.apply_date or '', r.agent_no or '', r.agent_name or '',
        r.agent_province or '', r.agent_branch or '', r.agent_org or '',
        r.app_name or '', r.app_code or '', r.ins_name or '', r.ins_code or '',
        r.insurer_name or '', r.product_name or '', r.insurance_type or '',
        _round2(r.scale_premium), _round2(r.industry_std_premium), _round2(r.commission),
        r.accept_count,
        '长期险' if r.ls_flag == 'long' else '短期险' if r.ls_flag == 'short' else '',
        '自保件' if r.self_policy_flag == 'self' else '互保件' if r.self_policy_flag == 'mutual' else '',
        _name_of(PAY_METHODS, r.pay_method), r.pay_years or '',
        r.policy_risk_status or '',
    ]


POLICY_QUERY_HEADERS = [
    '订单编号', '数据来源', '投保日期', '代理人工号', '代理人姓名', '省公司', '分支', '营业组',
    '投保人', '投保人编码', '被保人', '被保人编码', '保险公司', '产品', '险种',
    '规模保费', '行业标保', '代理人佣金', '受理件数', '长短险', '自互保件', '缴费方式', '缴费年期', '险种状态',
]


@bp.route('/crm/policy-query')
@login_required
@permission_required('crm.view')
def policy_query():
    """新契约业务清单查询与提取：多维筛选投保订单（手工录入 + Excel 导入 汇总）。"""
    kw = (request.args.get('kw') or '').strip()
    ins = (request.args.get('ins') or '').strip()
    no = (request.args.get('no') or '').strip()
    ap = (request.args.get('ap') or '').strip()
    ab = (request.args.get('ab') or '').strip()
    pn = (request.args.get('pn') or '').strip()
    it = (request.args.get('it') or '').strip()
    sf = (request.args.get('sf') or '').strip()
    lf = (request.args.get('lf') or '').strip()
    pm = (request.args.get('pm') or '').strip()
    rs = (request.args.get('rs') or '').strip()
    ds = (request.args.get('ds') or '').strip()
    dstart = (request.args.get('dstart') or '').strip()
    dend = (request.args.get('dend') or '').strip()
    rows = _policy_query_rows()
    insurers = sorted({(r.insurer_name or '') for r in CrmPolicyOrder.query.all() if r.insurer_name})
    provinces = sorted({(r.agent_province or '') for r in CrmPolicyOrder.query.all() if r.agent_province})
    branches = sorted({(r.agent_branch or '') for r in CrmPolicyOrder.query.all() if r.agent_branch})
    products = sorted({(r.product_name or '') for r in CrmPolicyOrder.query.all() if r.product_name})
    itypes = sorted({(r.insurance_type or '') for r in CrmPolicyOrder.query.all() if r.insurance_type})
    return render_template('crm/policy_query.html',
        rows=rows, kw=kw, ins=ins, no=no, ap=ap, ab=ab, pn=pn, it=it,
        sf=sf, lf=lf, pm=pm, rs=rs, ds=ds, dstart=dstart, dend=dend,
        insurers=insurers, provinces=provinces, branches=branches, products=products, itypes=itypes,
        self_flags=SELF_FLAGS, ls_flags=LS_FLAGS, pay_methods=PAY_METHODS,
        risk_status=POLICY_RISK_STATUS, src_map=SRC_MAP,
        nm_pay={c: n for c, n in PAY_METHODS},
        can_manage=_can('crm.contract.manage'))


@bp.route('/crm/policy-query/export')
@login_required
@permission_required('crm.view')
def policy_query_export():
    """新契约业务清单查询 → 导出 Excel（沿用当前筛选条件）。"""
    rows = _policy_query_rows()
    data = [_policy_query_row_values(r) for r in rows]
    return _xlsx_response('新契约业务清单', '核心业务 · 新契约业务清单（查询导出）',
                          POLICY_QUERY_HEADERS, data)


# ===================== 核心业务经营报表（可视化） =====================

def _dashboard_data():
    """经营报表聚合：从 request.args 读筛选（月份区间 / 省公司 / 保司）→ 返回各面板数据。

    口径（威哥 2026-10 拍板）：实际数据为主、全国地区口径、暂不做目标达成。
    金额均按 规模保费 求和；月度用 apply_date 前缀 YYYY-MM。
    """
    ym_start = (request.args.get('ym_start') or '').strip()
    ym_end = (request.args.get('ym_end') or '').strip()
    ap = (request.args.get('ap') or '').strip()          # 省公司
    ins = (request.args.get('ins') or '').strip()        # 保司
    q = CrmPolicyOrder.query
    if ap: q = q.filter_by(agent_province=ap)
    if ins: q = q.filter_by(insurer_name=ins)
    if ym_start: q = q.filter(CrmPolicyOrder.apply_date >= ym_start + '-01')
    if ym_end: q = q.filter(CrmPolicyOrder.apply_date <= ym_end + '-31')
    rows = q.all()

    # KPI
    scale = sum(r.scale_premium or 0 for r in rows)
    std = sum(r.industry_std_premium or 0 for r in rows)
    cnt = sum(r.accept_count or 0 for r in rows)
    agents = {(r.agent_no or '') for r in rows if r.agent_no}
    orders = len(rows)
    avg = scale / orders if orders else 0

    # 月度趋势
    month_map = {}
    for r in rows:
        m = (r.apply_date or '')[:7]
        if m:
            d = month_map.setdefault(m, [0, 0])
            d[0] += r.scale_premium or 0
            d[1] += r.industry_std_premium or 0
    months = sorted(month_map.keys())
    trend = {'months': months,
             'scale': [round(month_map[m][0], 2) for m in months],
             'std': [round(month_map[m][1], 2) for m in months]}

    def top_map(keyfn, label_none='未填', n=10):
        mp = {}
        for r in rows:
            k = keyfn(r) or label_none
            mp[k] = mp.get(k, 0) + (r.scale_premium or 0)
        return sorted(mp.items(), key=lambda x: -x[1])[:n]

    team = [{'name': k, 'value': round(v, 2)} for k, v in top_map(lambda r: r.agent_org, '未归属')]
    product = [{'name': k, 'value': round(v, 2)} for k, v in
               top_map(lambda r: r.product_name, '未填')]
    insurer = [{'name': k, 'value': round(v, 2)} for k, v in
               top_map(lambda r: r.insurer_name, '未填')]
    itype = [{'name': k, 'value': round(v, 2)} for k, v in
             top_map(lambda r: r.insurance_type, '未填')]
    # 全国地区（投保人省份）
    prov_map = {}
    for r in rows:
        k = r.app_addr_province or '未填'
        prov_map[k] = prov_map.get(k, 0) + (r.scale_premium or 0)
    province = [{'name': k, 'value': round(v, 2)} for k, v in
                sorted(prov_map.items(), key=lambda x: -x[1])]

    # 代理人热榜
    ag_map, ag_name = {}, {}
    for r in rows:
        if r.agent_no:
            ag_map[r.agent_no] = ag_map.get(r.agent_no, 0) + (r.scale_premium or 0)
            ag_name[r.agent_no] = r.agent_name or ''
    agent_top = [{'no': n, 'name': ag_name.get(n, ''), 'scale': round(v, 2)}
                 for n, v in sorted(ag_map.items(), key=lambda x: -x[1])[:10]]

    # 自互保 / 长短险 占比（按单计数）
    self_map, ls_map = {}, {}
    for r in rows:
        sk = {'self': '自保件', 'mutual': '互保件'}.get(r.self_policy_flag, '非自互保')
        self_map[sk] = self_map.get(sk, 0) + 1
        lk = '长期险' if r.ls_flag == 'long' else '短期险' if r.ls_flag == 'short' else '未标记'
        ls_map[lk] = ls_map.get(lk, 0) + 1
    self_pie = [{'name': k, 'value': v} for k, v in self_map.items()]
    ls_pie = [{'name': k, 'value': v} for k, v in ls_map.items()]

    return {
        'kpi': {'scale': round(scale, 2), 'std': round(std, 2), 'cnt': cnt,
                'agents': len(agents), 'orders': orders, 'avg': round(avg, 2)},
        'trend': trend, 'team': team, 'product': product, 'insurer': insurer,
        'itype': itype, 'province': province, 'agent_top': agent_top,
        'self_pie': self_pie, 'ls_pie': ls_pie,
        'filters': {'ym_start': ym_start, 'ym_end': ym_end, 'ap': ap, 'ins': ins},
    }


@bp.route('/crm/dashboard')
@login_required
@permission_required('crm.view')
def dashboard():
    """核心业务经营报表（可视化）：KPI + 多面板，品牌色，全国地区口径。"""
    data = _dashboard_data()
    insurers = sorted({(r.insurer_name or '') for r in CrmPolicyOrder.query.all() if r.insurer_name})
    provinces = sorted({(r.agent_province or '') for r in CrmPolicyOrder.query.all() if r.agent_province})
    return render_template('crm/dashboard.html', data=data, insurers=insurers, provinces=provinces)


@bp.route('/crm/dashboard-data')
@login_required
@permission_required('crm.view')
def dashboard_data():
    """经营报表数据接口（JSON，供页面局部刷新 / 外部调用）。"""
    return json.dumps(_dashboard_data(), ensure_ascii=False), 200, \
        {'Content-Type': 'application/json'}


# ======================================================================
# 保费测算 · 智能体导入（费率表 / 现金价值表）+ 标准试算
# ----------------------------------------------------------------------
# 口径：所有数值以「每 base_unit(默认1000)元保费」为基准单位。
# 导入双件必备：费率表(rate) + 现金价值表(cash)；计划书可选。
# 解析靠中文表头关键词自动识别（长表 / 宽表），导入前弹预览确认。
# ======================================================================

def _norm_sex(v):
    """男/M/male → M；女/F/female → F；其余空。"""
    s = (str(v or '').strip()).lower()
    if s in ('男', 'm', 'male', '1'):
        return 'M'
    if s in ('女', 'f', 'female', '2'):
        return 'F'
    return ''


def _norm_pay_term(v):
    """趸交/一次交清 → 0；保司表中交费期间=1 即趸交；其余取年数。"""
    s = str(v or '').strip()
    if not s:
        return 0
    if '趸' in s or '一次' in s:
        return 0
    m = re.search(r'(\d+)', s)
    if not m:
        return 0
    n = int(m.group(1))
    return 0 if n == 1 else n  # 交费期间=1 在保司费率表中即趸交


def _to_int(v):
    if v is None:
        return 0
    s = str(v).strip().replace('岁', '').replace('周', '')
    m = re.search(r'-?\d+', s)
    return int(m.group(0)) if m else 0


def _to_float(v):
    if v is None:
        return 0.0
    s = str(v).replace(',', '').replace('%', '').strip()
    if s in ('', '—', '-', 'None', 'nan', 'Null'):
        return 0.0
    try:
        return float(s)
    except ValueError:
        m = re.search(r'-?\d+(?:\.\d+)?', s)
        return float(m.group(0)) if m else 0.0


def _premium_map_header(header):
    """长表表头 → {列索引: 字段}。按优先级匹配，避免 现金价值/保单年度 等冲突。"""
    field_of = {}
    for i, h in enumerate(header):
        if h is None:
            continue
        key = str(h).strip().lower()
        # 派生/演示列一律不映射：它们名字里也含「现金价值 / 保险费 / 基本保险金额」，
        # 会是同一行里后写覆盖前写，把基准现价/保额冲掉。
        # 例：'交清增额保险年初现金价值'、'交清增额净保险费'、'退保金（基本保险金额对应的现金价值）'。
        if '交清增额' in key or '退保金' in key:
            # 「购买交清增额保险」演示口径要用到「增额部分自己的现金价值」——
            # 它是【每 1000 元增额保额】的单价，不是「每 base_unit 保费」，别拿去当 cv。
            # '交清增额净保险费' / '交清增额保险TR' 是精算中间量，一律跳过。
            if '现金价值' in key and '年初' not in key:
                field_of[i] = 'pua_cv'
            continue
        # ⚠️ 保额分红（增额）≠ 现金红利：它们名字里也带「红利」，晚判会覆盖掉 div_d
        # 例：幸福如意步步高〔当年增额（红利利益演示）〕〔累积增额（红利利益演示）〕
        #     ——这两个是「红利买到的增额保额」，不是发给客户的现金红利。
        if '增额' in key:
            if '保证' in key:
                continue                       # 保证档增额恒为 0，演示不用
            if '累积' in key:
                field_of[i] = 'pua_sa'         # 累积交清增额基本保险金额
            elif '当年' in key or '年度' in key:
                field_of[i] = 'pua_add'        # 当年新增的增额基本保险金额
            continue
        # 保单年度 必须最先判：'保费' 是其子串，晚判会被误认成 premium；
        # 且只用精确匹配，避免 '保单年度末养老年金' 等被误判成保单年度。
        if key in ('保单年度', 'polyr', 'pol_yr'):
            field_of[i] = 'policy_year'; continue
        # 年初现金价值 必须在 现金价值 之前判：它含「现金价值」子串，
        # 晚判会被吞掉（原顺序导致 cv_begin 列被误当 cv）。
        if '年初现金' in key or key == 'cv_begin':
            field_of[i] = 'cv_begin'; continue
        if '现金价值' in key or key == 'cv':
            field_of[i] = 'cv'; continue
        if '基本保险金额' in key or '基本保额' in key or key == 'sa':
            field_of[i] = 'sa'; continue
        if '净保费责任准备金' in key or key == 'trnp':
            field_of[i] = 'trnp'; continue
        if '责任准备金' in key or key == 'tr':
            field_of[i] = 'tr'; continue
        if '转换现价' in key:
            field_of[i] = 'convert_cv'; continue
        # 英文因子档的红利列（爱心人寿等把红利拆成独立「红利因子档」，表头是 Div / Div_Ratio）。
        # 必须精确匹配：'dividend_rate' 含 'div' 子串，宽松匹配会被误判成当年红利。
        if key == 'div':
            field_of[i] = 'div_d'; continue
        if key in ('div_ratio', 'divrate', 'div_rate', 'dividendrate'):
            field_of[i] = 'dividend_rate'; continue
        # 分红型产品的红利：区分「保证利益演示」与「红利利益演示（非保证）」两列。
        # 注意：①'累积红利' 是含息滚存值，逐年累加会重复计息 → 跳过，只用当年红利自行累加；
        #      ②必须在「分红利率/分红」之前判，否则被吞成 dividend_rate。
        if ('红利' in key or '分红' in key) and '分红利率' not in key and '利率' not in key:
            if '累积' in key or '累计' in key:
                continue
            field_of[i] = 'div_g' if '保证' in key else 'div_d'; continue
        if '分红利率' in key or '分红' in key:
            field_of[i] = 'dividend_rate'; continue
        if key == 'sb':
            field_of[i] = 'sb'; continue
        if key == '保费' or key == 'premium' or '保险费' in key:
            field_of[i] = 'premium'; continue
        if ('投保年龄' in key or key == '年龄' or key == 'age') and '保单' not in key:
            field_of[i] = 'age'; continue
        if '交费' in key or '缴费' in key or key == 'np':
            field_of[i] = 'pay_term'; continue
        if '性别' in key or key == 'sex':
            field_of[i] = 'sex'; continue
    return field_of


def _premium_row_to_dict(row, field_of, base_unit, keep_div_only=False):
    """keep_div_only=True：解析「红利因子档」专用。
    爱心的红利是独立文件，只有 Div 列、没有 基本保额/现金价值，
    若沿用下面 sa/cv 门槛会把全部红利行滤干净 → 必须单独开口子。"""
    d = {'sex': '', 'issue_age': 0, 'pay_term': 0, 'policy_year': 0,
         'sa': 0.0, 'cv': 0.0, 'cv_begin': 0.0, 'tr': 0.0, 'trnp': 0.0,
         'cvnp': 0.0, 'sb': 0.0, 'dividend_rate': 0.0, 'convert_cv': 0.0,
         'div_g': 0.0, 'div_d': 0.0, 'pua_sa': 0.0, 'pua_add': 0.0, 'pua_cv': 0.0,
         'premium': base_unit}
    for i, fld in field_of.items():
        v = row[i] if i < len(row) else None
        if fld == 'sex':
            d['sex'] = _norm_sex(v)
        elif fld == 'age':
            d['issue_age'] = _to_int(v)
        elif fld == 'pay_term':
            d['pay_term'] = _norm_pay_term(v)
        elif fld == 'policy_year':
            d['policy_year'] = _to_int(v)
        elif fld == 'premium':
            d['premium'] = _to_float(v) or base_unit
        else:
            d[fld] = _to_float(v)
    if d['issue_age'] < 0:
        return None
    # 仅保留确有数值的行（基本保额/现金价值任一>0），避免表头/空行混入；
    # 投保年龄 0（新生儿）合法，不得丢弃。
    if not (d['sa'] > 0 or d['cv'] > 0):
        if not (keep_div_only and ((d['div_g'] or 0) > 0 or (d['div_d'] or 0) > 0)):
            return None
    pr = d.pop('premium')
    if pr and abs(pr - base_unit) > 1e-9:
        scale = base_unit / pr
        # ⚠️ pua_cv 是「每 1000 元增额保额」的单价，与保费无关，绝不参与缩放
        for k in ('sa', 'cv', 'cv_begin', 'tr', 'trnp', 'cvnp', 'sb', 'dividend_rate', 'convert_cv',
                  'div_g', 'div_d', 'pua_sa', 'pua_add'):
            d[k] = round((d[k] or 0) * scale, 6)
    return d


def _premium_normalize_long(rows, base_unit, keep_div_only=False):
    """长表(一列一字段)解析。返回 (norm_rows, report)。"""
    report = {'layout': 'unknown', 'fields': [], 'warn': []}
    header_idx = -1
    for i, row in enumerate(rows[:25]):
        hits = sum(1 for c in row if c is not None and any(
            k in str(c).lower() for k in ('年龄', '性别', '交费', '缴费', '保费', '现金价值', '基本保险', '保单年度', 'np', 'sex', 'age', 'sa', 'cv')))
        if hits >= 2:
            header_idx = i
            break
    if header_idx < 0:
        return [], report
    field_of = _premium_map_header(rows[header_idx])
    report['fields'] = sorted(set(field_of.values()))
    if not field_of:
        report['warn'].append('未识别到任何字段，请确认文件为结构化长表（一列一字段）')
        return [], report
    norm = []
    for row in rows[header_idx + 1:]:
        if all(c is None for c in row):
            continue
        d = _premium_row_to_dict(row, field_of, base_unit, keep_div_only)
        if d:
            norm.append(d)
    report['layout'] = 'long'
    return norm, report


def _premium_normalize_wide(rows, base_unit):
    """宽表(年龄行 × 性别×交费方式 列组，如保司 PDF 基本保额表)解析为基本保额(rate)。"""
    report = {'layout': 'wide', 'fields': ['sex', 'issue_age', 'pay_term', 'sa'], 'warn': []}
    age_idx = -1
    for i, row in enumerate(rows[:30]):
        for c in row:
            if c is not None and str(c).strip() in ('年龄', '投保年龄'):
                age_idx = i
                break
        if age_idx >= 0:
            break
    if age_idx < 0:
        report['warn'].append('宽表未能定位「年龄」列，请提供长表格式')
        return [], report
    # 列组表头在 age_idx 上方 1~2 行：识别每列的 性别 / 交费方式
    grp_rows = [r for r in rows[max(0, age_idx - 2):age_idx] if any(c is not None for c in r)]
    col_meta = {}
    for j in range(1, 60):
        sex = ''; pt = 0
        for gr in grp_rows:
            if j < len(gr) and gr[j] is not None:
                txt = str(gr[j])
                if '男' in txt and '女' not in txt:
                    sex = 'M'
                elif '女' in txt:
                    sex = 'F'
                pt = _norm_pay_term(txt) or pt
        if not sex and not pt:
            continue
        col_meta[j] = (sex, pt)
    if not col_meta:
        report['warn'].append('宽表未能识别 性别/交费方式 列组')
        return [], report
    norm = []
    for row in rows[age_idx + 1:]:
        age = _to_int(row[0]) if row else 0
        if age <= 0:
            continue
        for j, (sex, pt) in col_meta.items():
            val = _to_float(row[j]) if j < len(row) else 0
            if val <= 0:
                continue
            norm.append({'sex': sex, 'issue_age': age, 'pay_term': pt,
                         'policy_year': 0, 'sa': round(val, 4), 'cv': 0.0,
                         'cv_begin': 0.0, 'tr': 0.0, 'trnp': 0.0, 'cvnp': 0.0,
                         'sb': 0.0, 'dividend_rate': 0.0, 'convert_cv': 0.0, 'premium': base_unit})
    return norm, report


def _premium_detect_sheet(rows, kind_hint, base_unit, keep_div_only=False):
    """对单个工作表的行做长表/宽表解析。返回 (norm_rows, report)。"""
    norm, rep = _premium_normalize_long(rows, base_unit, keep_div_only)
    if not norm:
        norm, rep = _premium_normalize_wide(rows, base_unit)
    if not norm and kind_hint == 'cash':
        rep['warn'].append('未在本工作表识别到现金价值数据，请确认列含「现金价值」')
    if not norm and kind_hint == 'rate':
        rep['warn'].append('未在本工作表识别到费率（基本保额）数据，请确认列含「基本保险金额」')
    return norm, rep


def _premium_parse_file(path, kind_hint, insurer, product, base_unit=1000.0):
    """解析单个上传文件（逐 sheet 解析，按 kind 挑最匹配的 sheet）→ (norm_rows, report, kind)。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    best, best_rep, best_score = [], {'layout': 'unknown', 'warn': ['未识别到可用数据']}, -1
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        norm, rep = _premium_detect_sheet(rows, kind_hint, base_unit)
        sa_n = sum(1 for r in norm if (r.get('sa') or 0) > 0)
        cv_n = sum(1 for r in norm if (r.get('cv') or 0) > 0)
        score = len(norm)
        if kind_hint == 'rate':
            if sa_n > 0 and cv_n == 0:
                score += 100000          # 纯费率表（最佳）
            elif sa_n > 0:
                score += 5000            # 含费率的因子档
            else:
                score = -1               # 无费率，不合格
        elif kind_hint == 'cash':
            if cv_n > 0 and sa_n == 0:
                score += 100000          # 纯现金价值表（最佳）
            elif cv_n > 0:
                score += 5000            # 含现价的因子档
            else:
                score = -1               # 无现价，不合格
        if score > best_score:
            best_score, best, best_rep = score, norm, rep
    wb.close()
    norm = best
    kind = 'factor' if (any(r['sa'] for r in norm) and any(r['cv'] for r in norm)) else (
        'cash' if any(r['cv'] for r in norm) else 'rate')
    for r in norm:
        r['insurer_name'] = insurer
        r['product_name'] = product
        r['kind'] = kind
        r['base_unit'] = base_unit
    return norm, best_rep, kind


def _premium_parse_plan(path, insurer, product, base_unit=1000.0):
    """解析 EXCEL 版计划书（单文件路径，2026-10-05）：不用挑单个 sheet，
    而是全 sheet 扫描——凡是能识别出 费率行(基本保额) / 现价行(现金价值) 的 sheet 全部合并。
    原因：保司计划书常把 保额/现价/利益演示 拆成多个 sheet（如富德生命），单 sheet 挑选会漏一半。
    返回 (norm_rows, report, kind)，行 kind 统一重打：有sa无cv=rate / 有cv无sa=cash / 都有=factor，
    与双件路径、入库逻辑、已导入产品聚合完全兼容。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    all_rows, warns, skipped = [], [], []
    for ws in wb.worksheets:
        try:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception:
            continue
        norm, rep = _premium_detect_sheet(rows, 'factor', base_unit)
        # 只收「结构化因子表」：必须有 性别 列（费率表/现金价值表/因子档都有），
        # 或 年龄 +（交费期间 或 基本保额）齐全。
        # 为什么：计划书里的「利益演示」页是宽表，按某个具体场景（年龄/性别/保费）
        # 算出的一串 保单年度/现价，不是因子；被当长表硬抽后会产出
        # 「无性别/无年龄/无保额」的空壳行，混进合并结果、还占满预览首屏。
        flds = set(rep.get('fields') or [])
        structured = ('sex' in flds) or ('age' in flds and ('pay_term' in flds or 'sa' in flds))
        if norm and structured:
            all_rows.extend(norm)
            if rep.get('warn'):
                warns.extend('%s: %s' % (ws.title, w) for w in rep['warn'])
        else:
            # 说明页/填写页/利益演示宽表 → 不是因子表，跳过并列出来（不再报「未定位年龄」这种噪音）
            skipped.append(ws.title)
    wb.close()
    # 只留确有数值的行（解析器已滤大部分，这里再兜一层，防说明页/填写页残留）
    norm = [r for r in all_rows if (r.get('sa') or 0) > 0 or (r.get('cv') or 0) > 0]
    has_sa = any((r.get('sa') or 0) > 0 for r in norm)
    has_cv = any((r.get('cv') or 0) > 0 for r in norm)
    kind = 'factor' if (has_sa and has_cv) else ('cash' if has_cv else 'rate')
    for r in norm:
        r['insurer_name'] = insurer
        r['product_name'] = product
        r['kind'] = kind
        r['base_unit'] = base_unit
    layout = 'plan(全sheet合并)'
    if skipped:
        layout += '·已跳过%d个非因子表' % len(skipped)
    if not norm:
        warns.append('未在任何工作表识别到费率（基本保额）或现金价值数据——'
                     '若该计划书是「填参数出结果」的单机版计算器，请改用方式B 上传费率表+现价表')
    rep = {'layout': layout, 'fields': [], 'warn': warns[:6], 'skipped': skipped[:8]}
    return norm, rep, kind


# ============================================================================
# 分红演示口径（2026-10-06 融合分红险演示）
# ----------------------------------------------------------------------------
# 条款依据（爱心人寿映山红条款 3.2 保单红利的领取）：投保时可选两种领取方式
#   （一）现金领取：红利每年发放给客户，领走即不再产生收益；
#   （二）累积生息：红利留存在本公司，以年复利方式累积生息，年利率由公司确定。
# 保司因子档「分红信息配置」给的：定价利率 1.75% / 红利演示利率 3.5% / 分红比例 70%；
# 官方建议书「填写信息」里 红利累积生息利率 = 1.75%（同定价利率）→ 作为系统默认值。
# ⚠️ 两种领取方式的「当年度红利」是同一个数，差别只在领走还是留存复利滚存。
# ============================================================================
DIV_ACCUM_RATE_DEFAULT = 0.0175
# 条款 3.1 红利领取方式：①现金领取 ②累积生息 ③购买交清增额保险
# （幸福如意步步高条款 3.1/3.2；三种方式的「当年度红利」是同一个数）
DIV_MODES = [('cash', '现金分红（现金领取）'),
             ('accum', '累积生息（年复利）'),
             ('pua', '购买交清增额保险')]


# ============================================================================
# 计划书产品分类 & 分类智能体（2026-10-06 二十三改，威哥拍板）
# ----------------------------------------------------------------------------
# 起因：每家保司给的文件都不一样——爱心人寿是「因子档 + 红利因子档 + 建议书 xlsm」，
# 幸福人寿是「一张试算工具表全包（费率/现价/红利/增额都在一张表的不同 sheet）」。
# 用一个万能解析器硬啃，必然这家对了那家错。
# 解法：**先给产品定分类，再由「该分类的智能体」决定**
#   ① 要收哪些文件  ② 认哪些列  ③ 能出几种演示口径
#   ④ 必须体检哪些字段（缺了当场拦下来）  ⑤ 给管理员什么提示话术
# ⚠️ 配置驱动：新增一类 = 在下面加一条 dict，不改任何逻辑代码。
# ============================================================================
# need 字段的中文名（体检报告用）
_FLD_LABEL = {
    'sa': '基本保险金额（费率）', 'cv': '现金价值', 'cv_begin': '年初现金价值',
    'div_g': '红利·保证利益演示', 'div_d': '红利·红利利益演示',
    'pua_sa': '累积交清增额基本保额', 'pua_cv': '增额部分现金价值单价',
    'pua_add': '当年新增增额保额', 'dividend_rate': '分红利率',
}
PROPOSAL_CATEGORIES = [
    {
        'key': 'div_life',
        'name': '分红型终身寿 / 两全（现金红利）',
        'desc': '条款「保单红利」可三选一领取：现金领取 / 累积生息 / 购买交清增额保险；'
                '演示表必须同时给「保证利益」与「红利利益（非保证）」两栏。',
        'kw': ('分红型', '分红', '两全'),
        'files': ['plan_file', 'rate_file', 'cash_file', 'div_file'],
        'need': ['sa', 'cv', 'div_d'],
        'plus': ['pua_sa', 'pua_cv', 'div_g'],
        'modes': ['cash', 'accum', 'pua'],
        'default_mode': 'accum',
        'tips': [
            '认列要点：先抓「保单年度」，再抓「年末现金价值」（别用年初）；'
            '红利要抓「红利（红利利益演示）」，别抓到「当年/累积增额」那两列——那是买增额的保额数。',
            '当年度红利三种领取方式共用同一个数，差别只在领走 / 留存生息 / 买保额。',
            'IRR：保费年初交(t=0..n-1)、退保金年末收(t=T)；单利＝-Σf ÷ Σ[f·(T-t)]。'
            '这两个口径对不上保司官方试算工具就是算错了。',
        ],
    },
    {
        'key': 'nondiv_life',
        'name': '非分红传统险（增额寿 / 定额终身寿 / 定期寿）',
        'desc': '现价写死在合同里，没有红利演示，保证利益即全部利益。',
        'kw': ('终身寿险', '定期寿险', '增额'),
        'files': ['plan_file', 'rate_file', 'cash_file'],
        'need': ['sa', 'cv'],
        'plus': ['cv_begin'],
        'modes': [],
        'default_mode': '',
        'tips': [
            '只认「基本保险金额 + 年末现金价值」两列，出现红利列说明归类可能选错了。',
            '增额终身寿的保额会按合同约定比例逐年递增，演示以「年末现金价值」为准。',
        ],
    },
    {
        'key': 'annuity',
        'name': '年金保险（养老年金 / 快返年金）',
        'desc': '除现金价值外还有「年金 / 生存金」给付，演示要分「生存总利益」口径。',
        'kw': ('年金', '养老年金'),
        'files': ['plan_file', 'rate_file', 'cash_file'],
        'need': ['sa', 'cv'],
        'plus': ['div_d'],
        'modes': ['accum'],
        'default_mode': 'accum',
        'tips': [
            '年金类要把「保单年度末年金 / 养老年金」与现金价值分开看，'
            '当前版本先演示现金价值口径，年金给付需在计划书备注中手工说明。',
            '若保司同时给了现金价值与年金两列，请注意「年末现金价值（不含年金）」才是可退金额。',
        ],
    },
    {
        'key': 'universal',
        'name': '万能型（结算利率演示）',
        'desc': '账户价值随结算利率浮动，演示要按低/中两档结算利率出。',
        'kw': ('万能',),
        'files': ['plan_file', 'rate_file'],
        'need': ['sa', 'cv'],
        'plus': [],
        'modes': [],
        'default_mode': '',
        'tips': [
            '万能账户结算利率超过保证利率的部分为非保证，务必在计划书声明中写明。',
            '当前版本按保司提供的账户价值表演示，暂不内置结算利率滚动算法。',
        ],
    },
    {
        'key': 'health',
        'name': '健康险（重疾 / 医疗 / 意外 / 护理）',
        'desc': '无现金价值类利益演示，计划书以「保障责任 + 费率」为主。',
        'kw': ('重疾', '疾病', '医疗', '意外', '护理', '防癌'),
        'files': ['rate_file'],
        'need': ['sa'],
        'plus': [],
        'modes': [],
        'default_mode': '',
        'tips': [
            '健康险一般没有现金价值演示，只出费率（基本保额/保费）即可。',
            '计划书里应重点列保险责任、等待期、责任免除，而不是收益率。',
        ],
    },
    {
        'key': 'other',
        'name': '其他 / 待归类',
        'desc': '暂时归不了类的产品，先按通用流程跑，确认口径后再移正式分类。',
        'kw': (),
        'files': ['plan_file', 'rate_file', 'cash_file', 'div_file'],
        'need': ['sa', 'cv'],
        'plus': ['div_d'],
        'modes': ['cash', 'accum', 'pua'],
        'default_mode': 'accum',
        'tips': [
            '先用默认口径跑通，再回来把这类产品的专属列与口径补上去。',
        ],
    },
]


def _pp_cat(key):
    """按 key 取分类配置；取不到回落 other。"""
    k = (key or '').strip()
    return next((c for c in PROPOSAL_CATEGORIES if c['key'] == k),
                next(c for c in PROPOSAL_CATEGORIES if c['key'] == 'other'))


def _pp_cat_name(key):
    return _pp_cat(key)['name']


def _pp_detect_category(product_name, rows=None):
    """给产品自动归类：①看名称关键字 ②看解析出来的因子（有红利列→分红型）。

    命中 divert：分红类优先看「有没有红利」，因为有些公司产品名里不写「分红型」三个字，
    但因子表里确实有红利列 → 归错类就出不来红利演示。
    """
    rows = rows or []
    has_div = any((r.get('div_d') or 0) > 0 or (r.get('div_g') or 0) > 0 for r in rows)
    has_cv = any((r.get('cv') or 0) > 0 for r in rows)
    nm = (product_name or '')
    if has_div and has_cv:
        return 'div_life'
    if has_div:
        return 'div_life'
    # 没有红利数据 → 按名称关键字排优先级：健康 > 年金 > 万能 > 传统 > 其他
    for key in ('health', 'annuity', 'universal', 'nondiv_life'):
        c = _pp_cat(key)
        if any(k in nm for k in c['kw']):
            return key
    if has_cv:
        return 'nondiv_life'
    return 'other'


def _pp_run_checks(cat, rows):
    """分类智能体的「体检」：按该类要求的必备字段逐条检查，缺了当场拦。

    返回 [{key, name, ok, level, msg}]；level=必须 / 加分。
    """
    rows = rows or []
    out = []

    def _has(f):
        # 红利演示口径 div_d 与保证口径 div_g 互为备胎：保司只给保证档也算认出来了
        fs = ('div_d', 'div_g') if f in ('div_d', 'div_g') else (f,)
        return any((r.get(x) or 0) for r in rows for x in fs)

    for f in cat.get('need') or []:
        ok = _has(f)
        out.append({'key': f, 'name': _FLD_LABEL.get(f, f), 'ok': ok, 'level': '必须',
                    'msg': '' if ok else '没解析到这一列，演示算不出来 —— 检查表头名或换一张表'})
    for f in cat.get('plus') or []:
        ok = _has(f)
        out.append({'key': f, 'name': _FLD_LABEL.get(f, f), 'ok': ok, 'level': '加分',
                    'msg': ('已识别，可演示「购买交清增额保险」' if f == 'pua_sa' and ok else
                            '保司没给这一列，对应口径不可用' if not ok else '已识别')})
    return out


def _premium_norm_rate(v, default=None):
    """利率归一化：页面可能填 1.75（百分数）也可能填 0.0175（小数）→ 统一成小数。

    ⚠️ 别漏这一步：前端利率框写的是「1.75」，直接当小数用会算出 175% 复利的天文数字。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x <= 0:
        return default
    return x / 100.0 if x > 1 else x


def _premium_parse_div_config(path):
    """抓保司因子档里的「分红信息配置」小表（定价利率/红利演示利率/累积生息利率/分红比例）。

    这类小表形如两列键值对（爱心人寿因子档第二个 sheet）：
        测试配置: | 定价利率 | 分红利率 | 分红比例
                  | 0.0175   | 0.035    | 0.7
    官方建议书「填写信息」里还有一行：红利累积生息利率 0.0175。
    返回 dict，取不到就是空。用于：导入报告里展示 + 累积生息利率默认值。
    """
    out = {}
    key_map = [
        ('accum_rate', ('累积生息利率', '累计生息利率', '累积利率')),
        ('price_rate', ('定价利率',)),
        ('demo_rate', ('红利演示利率', '分红利率', '演示利率', '红利利率')),
        ('ratio', ('分红比例',)),
    ]
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception:
        return out

    def _norm(x):
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            return None
        return x / 100.0 if (x > 1 and x <= 100) else float(x)

    try:
        for ws in wb.worksheets:
            grid = [list(r) for r in ws.iter_rows(values_only=True)]
            for ri, row in enumerate(grid):
                for ci, v in enumerate(row):
                    if v is None:
                        continue
                    s = str(v).replace(' ', '')
                    if len(s) > 20 or not any(k in s for _f, ks in key_map for k in ks):
                        continue
                    fld = next((f for f, ks in key_map if any(k in s for k in ks)), None)
                    if not fld:
                        continue
                    # ① 同行右侧（"定价利率 | 0.0175"）② 同行左侧 ③ 下一行同列（表头行 + 数值行）
                    num = None
                    for w in row[ci + 1:ci + 4]:
                        num = _norm(w)
                        if num is not None:
                            break
                    if num is None:
                        for w in row[max(0, ci - 3):ci]:
                            num = _norm(w)
                            if num is not None:
                                break
                    if num is None and ri + 1 < len(grid):
                        for w in grid[ri + 1][ci:ci + 4]:
                            num = _norm(w)
                            if num is not None:
                                break
                    if num is None:
                        continue
                    out.setdefault(fld, num)
    finally:
        wb.close()
    # 累积生息利率取不到时，退而用定价利率（官方建议书两者一致）
    if 'accum_rate' not in out and out.get('price_rate'):
        out['accum_rate'] = out['price_rate']
    return out


def _premium_parse_div(path, insurer, product, base_unit=1000.0):
    """解析「红利因子档」——分红型产品的红利单独成一个文件时用（2026-10-06 爱心人寿场景）。

    背景：爱心人寿映山红的主因子档只有 SA/CV（无红利列），红利在另一个文件
    「红利因子档-现金分红&累积生息 3.5%分红」，表头 Code/Vrsn/Sex/Age/NP/BP/PolYr/Div/Div_Ratio。
    这类文件只有红利、没有保额与现价，所以：
      · 表头映射要认英文 Div/Div_Ratio（见 _premium_map_header）；
      · 行过滤要开口子（keep_div_only=True），否则会被「必须有 sa/cv」滤干净 → 解析 0 行。
    返回 (div_rows, report)；div_rows 每行含 四键(性别/年龄/交费/保单年度) + div_g/div_d。
    """
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    all_rows, skipped = [], []
    for ws in wb.worksheets:
        try:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception:
            continue
        norm, rep = _premium_detect_sheet(rows, 'factor', base_unit, keep_div_only=True)
        flds = set(rep.get('fields') or [])
        # 只收「有红利字段 且 能定位保单年度」的结构化 sheet；说明页/填写页跳过
        if norm and ('div_d' in flds or 'div_g' in flds) and 'policy_year' in flds:
            all_rows.extend(norm)
        else:
            skipped.append(ws.title)
    wb.close()
    div_rows = [r for r in all_rows if (r.get('div_g') or 0) > 0 or (r.get('div_d') or 0) > 0]
    for r in div_rows:
        r['insurer_name'] = insurer
        r['product_name'] = product
        r['base_unit'] = base_unit
    rep = {'layout': 'div(红利因子档)', 'fields': ['div_d', 'div_g'],
           'warn': [], 'skipped': skipped[:8]}
    if not div_rows:
        rep['warn'].append('未在红利表中识别到红利数据——请确认列含「红利 / Div / 分红」且有数值，'
                           '并能定位到「保单年度 / PolYr」列')
    return div_rows, rep


def _premium_tmp_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'crm', 'premium')
    os.makedirs(d, exist_ok=True)
    return d


def _premium_read_div_file(tmp, token, insurer, product):
    """读取可选的「红利表」（分红型产品红利单独成一个文件时的补充件）。
    未上传返回 ([], None, '')；扩展名不合法直接抛 ValueError（调用方转成人话提示）。"""
    df = request.files.get('div_file')
    if not df or not df.filename:
        return [], None, ''
    if '.' in df.filename and df.filename.rsplit('.', 1)[-1].lower() not in ('xlsx', 'xlsm'):
        raise ValueError('红利表「%s」不是 Excel 文件，系统只支持 .xlsx / .xlsm。'
                         '请用 Excel/WPS 打开后「另存为 .xlsx」再上传。' % df.filename)
    dp = os.path.join(tmp, 'div_%s.xlsx' % token)
    df.save(dp)
    rows, rep = _premium_parse_div(dp, insurer, product)
    return rows, rep, df.filename


def _premium_purge_stale(max_age_h=24):
    """清理超过 max_age_h 小时的解析临时件（plan_/rate_/cash_/pv_）。

    这些是「上传预览 → 确认入库」之间的中间产物；成功入库时会删，但用户放弃预览
    就会残留。日积月累会占空间（曾见单目录 89MB / 一份计划书就 13MB）。"""
    d = _premium_tmp_dir()
    now = time.time()
    for fn in os.listdir(d):
        if not re.match(r'^(plan|rate|cash|pv)_', fn):
            continue
        p = os.path.join(d, fn)
        try:
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age_h * 3600:
                os.remove(p)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 收益率（IRR / 单利）口径 —— 2026-10-06 二十三改，逐条对齐保司官方试算工具表
#
# ⚠️ 时点约定（幸福如意步步高「利益演示」sheet 的 IRR现金流/收益 列就是对表依据）：
#   t=0 保单生效日，t=1 第1保单年度末 …… t=T 第T保单年度末；
#   · 保费在【年初】交：10年交 → t=0..9 共 10 笔；趸交 → 只有 t=0 一笔；
#   · 退保（现金价值）发生在第 T 保单年度末 → 落 t=T；
#   · 红利：现金领取 = 每年末各领一笔(t=1..T)；累积生息/交清增额 = 退保时随终值拿(t=T)。
# ⚠️ 单位时间必须这样取：保费早投，IRR 才对得上（旧版把保费放 t=1..n，等于晚贴现一年，IRR 偏大）。
# ----------------------------------------------------------------------------
def _irr_of(flows):
    """年化 IRR。flows[t] = 时点 t 的净现金流（t 从 0 起）。无根返回 0。"""
    if not flows or all(abs(f) < 1e-12 for f in flows):
        return 0.0

    def npv(r):
        try:
            return sum(f / ((1 + r) ** t) for t, f in enumerate(flows))
        except ZeroDivisionError:
            return float('inf')

    lo, hi = -0.95, 2.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return 0.0
    for _ in range(160):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-8:
            return mid
        if f_lo * fm < 0:
            hi, f_hi = mid, fm
        else:
            lo, f_lo = mid, fm
    return (lo + hi) / 2


def _simple_of(flows):
    """时间加权单利 r = -Σf ÷ Σ[f_t × (T-t)]，T = 最后一个时点。

    推导：把每笔现金流按单利滚到第 T 年末，正负相抵为 0：
        Σ_t f_t·[1 + r·(T-t)] = 0  →  r = -Σf / Σ f_t(T-t)
    ⚠️ 旧版写的是 (终值-累计保费)/(累计保费×年数)，默认把保费当成「一次性期初投入」，
       分期交的结果会系统性偏低（10年交 20 年期实测 0.63% vs 官方 0.81%）。
       ❗别再用 (cv - cum) / (cum * y)。"""
    if not flows:
        return 0.0
    T = len(flows) - 1
    wsum = sum(f * (T - t) for t, f in enumerate(flows))
    if abs(wsum) < 1e-12:
        return 0.0
    return -sum(flows) / wsum


def _premium_flows(years, premium, pay_term, terminal_map):
    """按上述时点约定拼现金流。

    years       持有到第几个保单年度末（T）
    premium     年交保费（趸交时 payload 同样传年交额，pay_term=0 只交一笔）
    pay_term    0=趸交 / >0=交费年数
    terminal_map {t: 金额}  该时点收回的钱（退保金 + 届时到手的红利/增额现价）
    返回长度 years+1 的 list（索引即时点 t）。
    """
    n = 1 if pay_term == 0 else pay_term        # 交费笔数
    flows = [0.0] * (years + 1)
    for t in range(min(years, n)):              # 保费：t=0 .. n-1
        flows[t] -= premium
    for t, amt in (terminal_map or {}).items():
        if 0 <= t < len(flows):
            flows[t] += amt
    return flows


def _premium_merge_rows(rows):
    """按 四键(性别/年龄/交费/保单年度) 合并重复行。

    为什么需要：同一份数据常被两个文件都解析到（如费率表与计划书是同一份），
    入库后同一年度出现两行 → 演示表每年重复两行。合并规则：
    sa/cv/div 等数值字段「非零优先」，正好把只有红利的那一行并进有现价的那一行。
    """
    idx, order = {}, []
    for r in rows or []:
        k = (r.get('sex', ''), r.get('issue_age', 0), r.get('pay_term', 0), r.get('policy_year', 0))
        if k not in idx:
            idx[k] = dict(r)
            order.append(k)
            continue
        t = idx[k]
        for f in ('sa', 'cv', 'cv_begin', 'tr', 'trnp', 'cvnp', 'sb', 'dividend_rate',
                  'convert_cv', 'div_g', 'div_d', 'pua_sa', 'pua_add', 'pua_cv'):
            if not (t.get(f) or 0) and (r.get(f) or 0):
                t[f] = r.get(f)
        if not t.get('base_unit'):
            t['base_unit'] = r.get('base_unit') or 1000.0
    return [idx[k] for k in order]


def _premium_calc_rows(factor_rows, sex, age, pay_term, premium,
                       div_mode='accum', accum_rate=None):
    """内存版试算：直接吃解析出来的因子行 dict 列表（不查库）。

    给智能体工作台「入库前试跑」用——管理员还没点安装，数据不能先落库，
    但必须能先看到演示结果对不对。⚠️ 正式测算 _premium_calc 也走这个函数，
    保证两条路同一套算法、口径绝不漂移。

    分红演示（div_mode，条款3.1 红利领取方式）：
      cash  现金领取：红利每年领走 → 累计已领红利逐年累加（不计息）；总利益 = 现价 + 累计已领
      accum 累积生息：红利留存公司以 accum_rate 年复利滚存 →
                     累积 = 上年累积×(1+利率) + 当年红利；总利益 = 现价 + 累积
      pua   购买交清增额保险：红利去买增额保额 → 增额部分的现金价值 = 累积增额保额/1000 × 增额现价单价；
                     总利益 = 现价 + 增额部分现价
    ⚠️ 三种方式的「当年度现金红利」是同一个数，差别只在领走 / 留存生息 / 买保额。
       所以三种口径的累积值在每一行都算出来一起返回，前端切换口径不必重算。
    ⚠️ IRR / 单利 走的时点约定见 _irr_of / _simple_of（对齐保司官方试算工具表）。
    """
    if not factor_rows:
        return None, '没有可用的因子数据，请先解析文件'
    ordered = sorted(factor_rows, key=lambda r: (r.get('policy_year') or 0))
    sa_row = next((r for r in ordered if (r.get('sa') or 0) > 0), None)
    if not sa_row:
        return None, '未识别到该产品的费率（基本保额）数据，请确认文件含「基本保险金额 / SA」列'
    base = sa_row.get('base_unit') or 1000.0
    base_sa = premium / base * (sa_row.get('sa') or 0.0)
    cash_rows = [r for r in ordered if (r.get('policy_year') or 0) >= 1 and (r.get('cv') or 0) > 0]
    if not cash_rows:
        return None, '未识别到该产品的现金价值数据，请确认文件含「现金价值 / CV」列'
    if div_mode not in ('accum', 'cash', 'pua'):
        div_mode = 'accum'
    # 选了「购买交清增额保险」但保司压根没给增额因子 → 自动回落累积生息，别报错
    if div_mode == 'pua' and not any((r.get('pua_sa') or 0) > 0 for r in ordered):
        div_mode = 'accum'
    try:
        rate = DIV_ACCUM_RATE_DEFAULT if accum_rate is None else float(accum_rate)
    except (TypeError, ValueError):
        rate = DIV_ACCUM_RATE_DEFAULT
    rows = []
    cum_div_g = 0.0   # 累计红利（保证利益演示口径，分红险保证部分通常为 0）
    cum_cash = 0.0    # 现金领取：累计已领红利（逐年累加、不计息）
    cum_acc = 0.0     # 累积生息：红利留存公司按 rate 年复利滚存后的账户余额
    div_of = {}       # 保单年度 → 当年现金红利（实际金额），算 IRR 要逐年发放的现金流
    for r in cash_rows:
        y = r.get('policy_year') or 0
        cv = premium / base * (r.get('cv') or 0.0)
        div_g = premium / base * (r.get('div_g') or 0.0)
        div_d = premium / base * (r.get('div_d') or 0.0)
        cum_div_g += div_g
        cum_cash += div_d                       # 领走：累计到手
        cum_acc = cum_acc * (1 + rate) + div_d  # 留存：上年本息 + 当年红利
        # 增额：累积增额保额（每 base_unit 保费）→ 实际金额；现金价值＝保额/1000 × 单价
        pua_sa_amt = premium / base * (r.get('pua_sa') or 0.0)
        pua_cv_amt = pua_sa_amt / 1000.0 * (r.get('pua_cv') or 0.0)
        div_of[y] = div_d
        cum = premium * min(y, 1 if pay_term == 0 else pay_term)
        # —— 收益率：①当前演示口径（含红利）②保证利益演示（不含红利）——
        #   现金领取：红利 t=1..y 逐年到手；累积生息 / 交清增额：红利随退保金在 t=y 一次性到手
        if div_mode == 'cash':
            # ⚠️ 现金领取的红利已经在 mid 里逐年发过，终值只剩退保金——
            #    再塞一次「累计已领」等于把红利算两遍（IRR 会虚高）。
            term, mid = 0.0, dict(div_of)
        elif div_mode == 'pua':
            term, mid = pua_cv_amt, {}
        else:
            term, mid = cum_acc, {}
        _flows = _premium_flows(y, premium, pay_term, {y: cv + term})
        for t, v in mid.items():
            if 0 < t < len(_flows):
                _flows[t] += v
        _flows_g = _premium_flows(y, premium, pay_term, {y: cv})
        rows.append({
            'year': y, 'age_end': age + y, 'premium': premium, 'cum_prem': round(cum, 2),
            'base_sa': round(base_sa, 2), 'cv': round(cv, 2),
            'simple': round(_simple_of(_flows) * 100, 3), 'irr': round(_irr_of(_flows) * 100, 3),
            'simple_g': round(_simple_of(_flows_g) * 100, 3), 'irr_g': round(_irr_of(_flows_g) * 100, 3),
            # 分红演示（非保证）：当年红利 / 三种领取口径的累积红利与总利益
            'div_g': round(div_g, 2), 'div_d': round(div_d, 2),
            'cum_div_g': round(cum_div_g, 2),
            'cum_cash': round(cum_cash, 2), 'total_cash': round(cv + cum_cash, 2),
            'cum_acc': round(cum_acc, 2), 'total_acc': round(cv + cum_acc, 2),
            'pua_sa': round(pua_sa_amt, 2), 'cum_pua': round(pua_cv_amt, 2),
            'total_pua': round(cv + pua_cv_amt, 2),
            # 兼容旧字段：按当前演示口径给值（老页面/小程序直接读这两个）
            'cum_div_d': round({'cash': cum_cash, 'accum': cum_acc, 'pua': pua_cv_amt}[div_mode], 2),
            'total_d': round(cv + {'cash': cum_cash, 'accum': cum_acc, 'pua': pua_cv_amt}[div_mode], 2),
        })
    has_div = any((r['div_g'] or 0) > 0 or (r['div_d'] or 0) > 0 for r in rows)
    has_pua = any((r.get('pua_sa') or 0) > 0 for r in cash_rows)
    return {'base_sa': round(base_sa, 2), 'pay_term': pay_term,
            'has_div': has_div, 'has_pua': has_pua, 'div_mode': div_mode,
            'accum_rate': round(rate, 6), 'rows': rows}, None


def _premium_product_cfg(insurer, product):
    """取产品的计划书引擎配置（已安装的那条）——红利口径与累积生息利率默认值用它。"""
    return CrmProposalProduct.query.filter_by(
        insurer_name=insurer, product_name=product).order_by(CrmProposalProduct.id.desc()).first()


def _premium_calc(insurer, product, sex, age, pay_term, premium, div_mode=None, accum_rate=None):
    """标准试算（已入库产品）：读库 → 转 dict → 复用内存版算法。返回 (result_dict, err)。

    分红口径优先级：调用方传参 > 产品引擎配置(CrmProposalProduct) > 默认累积生息 1.75%。
    """
    qs = CrmPremiumFactor.query.filter_by(
        insurer_name=insurer, product_name=product, sex=sex, issue_age=age, pay_term=pay_term).all()
    if not qs:
        return None, '未找到该产品的费率与现金价值数据，请先导入'
    cfg = _premium_product_cfg(insurer, product)
    _cat = _pp_cat(getattr(cfg, 'category', '') or '')
    if not div_mode:
        div_mode = (getattr(cfg, 'div_mode', '') or '') or (_cat.get('default_mode') or 'accum')
    if accum_rate is None:
        accum_rate = getattr(cfg, 'accum_rate', None) if cfg else None
    # 老库可能还没迁到 div_g/div_d 列（迁移在 seed._migrate_columns）→ 用 getattr 兜底
    dicts = [{'sex': r.sex or '', 'issue_age': r.issue_age or 0, 'pay_term': r.pay_term or 0,
              'policy_year': r.policy_year, 'sa': r.sa or 0.0, 'cv': r.cv or 0.0,
              'div_g': getattr(r, 'div_g', 0.0) or 0.0, 'div_d': getattr(r, 'div_d', 0.0) or 0.0,
              # 交清增额（二十三改）：老库没有这三列 → getattr 兜底
              'pua_sa': getattr(r, 'pua_sa', 0.0) or 0.0,
              'pua_add': getattr(r, 'pua_add', 0.0) or 0.0,
              'pua_cv': getattr(r, 'pua_cv', 0.0) or 0.0,
              'base_unit': r.base_unit or 1000.0} for r in qs]
    # 同年度重复行合并（历史导入可能留了重复数据），并把红利行并进现价行
    return _premium_calc_rows(_premium_merge_rows(dicts), sex, age, pay_term, premium,
                              div_mode, accum_rate)


@bp.route('/crm/premium-calc')
@login_required
@permission_required('crm.view')
def premium_calc():
    """保费测算模块：智能导入（费率表+现金价值表）+ 标准试算。"""
    products = db.session.query(
        CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name
    ).distinct().order_by(CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name).all()
    return render_template('crm/premium_calc.html',
                           products=[{'insurer': p[0], 'product': p[1]} for p in products])


@bp.route('/crm/premium-import/preview', methods=['POST'])
@login_required
@permission_required('crm.view')
def premium_import_preview():
    """上传费率表+现金价值表 → 智能解析，返回预览（不入库）。"""
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    if not insurer or not product:
        return json.dumps({'ok': False, 'msg': '请填写保司名称与产品名称'}), 200, {'Content-Type': 'application/json'}
    _premium_purge_stale()  # 顺手清掉超过 24h 的旧解析临时件，防目录膨胀
    # ---- 路径A（2026-10-05）：仅上传 EXCEL 版计划书，全 sheet 自动抽取费率+现价 ----
    if _f('plan_only') == '1':
        plan = request.files.get('plan_file')
        if not plan or not plan.filename:
            return json.dumps({'ok': False, 'msg': '请上传 EXCEL 版计划书（.xlsx/.xlsm）'}), 200, {'Content-Type': 'application/json'}
        if '.' in plan.filename and plan.filename.rsplit('.', 1)[-1].lower() not in ('xlsx', 'xlsm'):
            return json.dumps({'ok': False, 'msg': '计划书「%s」不是 Excel 文件——计划书单文件路径只支持 .xlsx/.xlsm；'
                               'PDF 版请先用 Excel/WPS 另存，或改走「费率表+现价表」双件路径。' % plan.filename},
                              ensure_ascii=False), 200, {'Content-Type': 'application/json'}
        tmp = _premium_tmp_dir()
        token = datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6]
        pp = os.path.join(tmp, 'plan_%s.xlsx' % token)
        plan.save(pp)
        try:
            rows, rep, kind = _premium_parse_plan(pp, insurer, product)
        except Exception as e:
            msg = str(e)
            if 'not a zip' in msg.lower():
                msg = ('文件不是有效的 Excel（.xlsx/.xlsm）——它可能是 PDF、老版 .xls 或加密文件，'
                       '请用 Excel/WPS 打开后「另存为 .xlsx」再重新上传。')
            return json.dumps({'ok': False, 'msg': '解析失败：%s' % msg}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
        if not rows:
            return json.dumps({'ok': False, 'msg': '计划书中未识别到可用的费率/现金价值数据——'
                               '请确认是含「基本保险金额/现金价值」数值的 EXCEL 版计划书，或改走「费率表+现价表」双件路径。'},
                              ensure_ascii=False), 200, {'Content-Type': 'application/json'}
        # 红利表（可选）：分红型产品若红利单独成文件，在这里一并传上来，入库时按四键合并
        try:
            div_rows, div_rep, div_name = _premium_read_div_file(tmp, token, insurer, product)
        except ValueError as e:
            return json.dumps({'ok': False, 'msg': str(e)}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
        except Exception as e:
            return json.dumps({'ok': False, 'msg': '红利表解析失败：%s' % e}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
        payload = {
            'insurer': insurer, 'product': product, 'plan_name': plan.filename, 'plan_only': True,
            'rate': rows, 'cash': [], 'div': div_rows, 'div_name': div_name,
            'rate_rep': rep, 'cash_rep': {'layout': '—', 'fields': [], 'warn': []},
            'div_rep': div_rep or {'layout': '—', 'fields': [], 'warn': []},
            'rate_kind': kind, 'cash_kind': '',
        }
        with open(os.path.join(tmp, 'pv_%s.json' % token), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        return json.dumps({
            'ok': True, 'token': token, 'insurer': insurer, 'product': product,
            'plan_name': plan.filename, 'plan_only': True,
            'rate_total': len(rows), 'cash_total': sum(1 for r in rows if (r.get('cv') or 0) > 0),
            'div_total': len(div_rows), 'div_name': div_name,
            'rate_rep': rep, 'cash_rep': payload['cash_rep'], 'div_rep': payload['div_rep'],
            'rate_sample': rows[:8], 'cash_sample': [],
        }, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    # ---- 路径B：费率表 + 现金价值表 双件（原逻辑） ----
    rate = request.files.get('rate_file')
    cash = request.files.get('cash_file')
    if not rate or not rate.filename:
        return json.dumps({'ok': False, 'msg': '请上传费率表（必备）'}), 200, {'Content-Type': 'application/json'}
    # 扩展名防呆：PDF/扫描件/老版 .xls 都无法按 xlsx 解析（xlsx 本质是 zip 包），
    # 提前拦下并说人话，不让「File is not a zip file」这种英文报错直接甩给用户。
    def _ext_bad(f):
        if not f or not f.filename or '.' not in f.filename:
            return False
        return f.filename.rsplit('.', 1)[-1].lower() not in ('xlsx', 'xlsm')
    for label, f in (('费率表', rate), ('现金价值表', cash)):
        if _ext_bad(f):
            return json.dumps(
                {'ok': False,
                 'msg': '%s「%s」不是 Excel 文件，无法解析——系统只支持 .xlsx / .xlsm。'
                        '请把表格用 Excel/WPS 打开后「另存为 .xlsx」再上传；若手上只有 PDF 版，请告诉我，我来做 PDF 解析方案。'
                        % (label, f.filename)},
                ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    tmp = _premium_tmp_dir()
    token = datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6]
    rp = os.path.join(tmp, 'rate_%s.xlsx' % token)
    cp = os.path.join(tmp, 'cash_%s.xlsx' % token) if (cash and cash.filename) else ''
    rate.save(rp)
    if cp:
        cash.save(cp)
    try:
        rate_rows, rate_rep, rate_kind = _premium_parse_file(rp, 'rate', insurer, product)
        cash_rows, cash_rep, cash_kind = ([], {'layout': 'none', 'fields': [], 'warn': []}, '') \
            if not cp else _premium_parse_file(cp, 'cash', insurer, product)
    except Exception as e:
        msg = str(e)
        if 'not a zip' in msg.lower():
            msg = ('文件不是有效的 Excel（.xlsx/.xlsm）——它可能是 PDF、老版 .xls 或加密文件，'
                   '请用 Excel/WPS 打开后「另存为 .xlsx」再重新上传。')
        return json.dumps({'ok': False, 'msg': '解析失败：%s' % msg}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    # 红利表（可选）：分红型产品若红利单独成文件（如爱心人寿「红利因子档」），在这里一并传上来
    try:
        div_rows, div_rep, div_name = _premium_read_div_file(tmp, token, insurer, product)
    except ValueError as e:
        return json.dumps({'ok': False, 'msg': str(e)}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        return json.dumps({'ok': False, 'msg': '红利表解析失败：%s' % e}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    plan = request.files.get('plan_file')
    plan_name = plan.filename if (plan and plan.filename) else ''
    # 分红参数（保司因子档里的「分红信息配置」小表）：演示利率/分红比例/累积生息利率
    div_cfg = _premium_parse_div_config(rp)
    if not div_cfg.get('accum_rate'):
        div_cfg['accum_rate'] = DIV_ACCUM_RATE_DEFAULT
    # 合并校验：费率(基本保额) 与 现金价值 双件必备（费率表含现价则可不传现金价值表）
    has_sa = any((r.get('sa') or 0) > 0 for r in rate_rows + cash_rows)
    has_cv = any((r.get('cv') or 0) > 0 for r in rate_rows + cash_rows)
    if not has_sa:
        return json.dumps({'ok': False, 'msg': '费率表未识别到基本保额，请确认列含「基本保险金额」'},
                          ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    if not has_cv:
        return json.dumps({'ok': False, 'msg': '未识别到现金价值：请上传现金价值表（该费率表未含现价）'},
                          ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    # 暂存归一化结果，供确认入库读取
    payload = {
        'insurer': insurer, 'product': product, 'plan_name': plan_name,
        'rate': rate_rows, 'cash': cash_rows, 'div': div_rows, 'div_name': div_name,
        'rate_rep': rate_rep, 'cash_rep': cash_rep,
        'div_rep': div_rep or {'layout': '—', 'fields': [], 'warn': []},
        'rate_kind': rate_kind, 'cash_kind': cash_kind,
    }
    with open(os.path.join(tmp, 'pv_%s.json' % token), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    sample = lambda rs: rs[:8]
    return json.dumps({
        'ok': True, 'token': token, 'insurer': insurer, 'product': product, 'plan_name': plan_name,
        'rate_total': len(rate_rows), 'cash_total': len(cash_rows),
        'div_total': len(div_rows), 'div_name': div_name, 'div_cfg': div_cfg,
        'rate_rep': rate_rep, 'cash_rep': cash_rep, 'div_rep': payload['div_rep'],
        'rate_sample': sample(rate_rows), 'cash_sample': sample(cash_rows),
    }, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/premium-import/save', methods=['POST'])
@login_required
@permission_required('crm.view')
def premium_import_save():
    """确认入库：读取预览 token，批量写入费率+现金价值，双件必备。"""
    token = _f('token')
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    tmp = _premium_tmp_dir()
    pf = os.path.join(tmp, 'pv_%s.json' % token)
    if not os.path.exists(pf):
        return json.dumps({'ok': False, 'msg': '预览已过期，请重新上传解析'}), 200, {'Content-Type': 'application/json'}
    with open(pf, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    rate_rows = payload['rate']
    cash_rows = payload['cash']
    if not rate_rows and not cash_rows:
        return json.dumps({'ok': False, 'msg': '解析数据为空，无法入库'}), 200, {'Content-Type': 'application/json'}
    has_sa = any((r.get('sa') or 0) > 0 for r in rate_rows + cash_rows)
    has_cv = any((r.get('cv') or 0) > 0 for r in rate_rows + cash_rows)
    if not has_sa or not has_cv:
        return json.dumps({'ok': False, 'msg': '费率与现金价值数据不完整，无法入库（双件必备）'},
                          ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    batch = datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:4]
    maps = []
    # 同年度重复行先合并（费率表与计划书是同一份时会解析出两遍），避免演示表每年重复两行
    for r in _premium_merge_rows(rate_rows + cash_rows):
        maps.append({
            'insurer_name': insurer, 'product_name': product, 'sex': r.get('sex', ''),
            'issue_age': r.get('issue_age', 0), 'pay_term': r.get('pay_term', 0),
            'policy_year': r.get('policy_year', 0), 'sa': r.get('sa', 0.0), 'cv': r.get('cv', 0.0),
            'cv_begin': r.get('cv_begin', 0.0), 'tr': r.get('tr', 0.0), 'trnp': r.get('trnp', 0.0),
            'cvnp': r.get('cvnp', 0.0), 'sb': r.get('sb', 0.0), 'dividend_rate': r.get('dividend_rate', 0.0),
            'convert_cv': r.get('convert_cv', 0.0),
            # 分红型的红利（计划书「红利利益演示」用）：保证口径 div_g / 演示口径 div_d（非保证）
            'div_g': r.get('div_g', 0.0), 'div_d': r.get('div_d', 0.0),
            # 第三种领取方式「购买交清增额保险」的因子
            'pua_sa': r.get('pua_sa', 0.0), 'pua_add': r.get('pua_add', 0.0),
            'pua_cv': r.get('pua_cv', 0.0),
            'base_unit': r.get('base_unit', 1000.0),
            'kind': r.get('kind', ''), 'src_file': payload.get('plan_name', '') or '导入',
            'batch_no': batch, 'created_at': now,
        })
    try:
        db.session.bulk_insert_mappings(CrmPremiumFactor, maps)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return json.dumps({'ok': False, 'msg': '入库失败：%s' % e}), 200, {'Content-Type': 'application/json'}
    # 红利表（可选）：按 四键(性别/年龄/交费期间/保单年度) 把红利写回已入库的行。
    # 为什么单独合：爱心人寿这类把红利拆成独立文件，红利文件里没有保额/现价，
    # 不能自成一行入库（那样测算时找不到现价），只能挂到已有的现价行上。
    div_n, div_miss = 0, 0
    div_rows = payload.get('div') or []
    if div_rows:
        idx = {}
        for r in div_rows:
            idx[(r.get('sex', ''), r.get('issue_age', 0),
                 r.get('pay_term', 0), r.get('policy_year', 0))] = r
        try:
            for f in CrmPremiumFactor.query.filter_by(
                    insurer_name=insurer, product_name=product).all():
                r = idx.get((f.sex, f.issue_age, f.pay_term, f.policy_year))
                if not r:
                    div_miss += 1
                    continue
                f.div_g = r.get('div_g', 0.0) or 0.0
                f.div_d = r.get('div_d', 0.0) or 0.0
                div_n += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return json.dumps({'ok': False, 'msg': '红利合并失败：%s' % e}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    # 清理临时文件
    for fn in ('rate_%s.xlsx' % token, 'cash_%s.xlsx' % token, 'plan_%s.xlsx' % token,
               'div_%s.xlsx' % token, 'pv_%s.json' % token):
        try:
            os.remove(os.path.join(tmp, fn))
        except OSError:
            pass
    # 双件完整性提示
    # ⚠️ 不能只查 policy_year==0：因子档型产品（爱心人寿等）的 基本保额 与 现金价值 在
    # 同一行（PolYr 从 1 起），根本没有 policy_year=0 的行 → 会被误报「尚缺费率表」。
    has_rate = CrmPremiumFactor.query.filter(CrmPremiumFactor.insurer_name == insurer,
                                             CrmPremiumFactor.product_name == product,
                                             CrmPremiumFactor.sa > 0).first()
    has_cash = CrmPremiumFactor.query.filter(CrmPremiumFactor.insurer_name == insurer,
                                             CrmPremiumFactor.product_name == product,
                                             CrmPremiumFactor.policy_year >= 1).first()
    warn = ''
    if not has_rate or not has_cash:
        warn = '（注意：该产品目前尚缺%s，测算暂不可用；请补齐双件）' % ('现金价值表' if has_rate else '费率表')
    div_msg = ''
    if div_rows:
        div_msg = '；红利表「%s」已合并 %d 行' % (payload.get('div_name') or '红利表', div_n)
        if div_miss:
            div_msg += '（%d 个现价行未匹配到红利，多为红利表未覆盖的年龄/交费档）' % div_miss
    if payload.get('plan_only'):
        cv_n = sum(1 for r in rate_rows if (r.get('cv') or 0) > 0)
        msg = '已从计划书「%s」入库 %d 行（其中现金价值 %d 行）%s%s' % (
            payload.get('plan_name') or '计划书', len(rate_rows), cv_n, warn, div_msg)
    else:
        msg = '已入库费率 %d 行 + 现金价值 %d 行 %s%s' % (len(rate_rows), len(cash_rows), warn, div_msg)
    return json.dumps({'ok': True, 'msg': msg}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/premium-calc/api', methods=['POST'])
@login_required
@permission_required('crm.view')
def premium_calc_api():
    """标准试算接口：输入要素 → 基本保额/各年现金价值/单利/IRR。"""
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    sex = _norm_sex(_f('sex'))
    age = _to_int(_f('age'))
    pay_term = _norm_pay_term(_f('pay_term'))
    premium = _to_float(_f('premium'))
    div_mode = (_f('div_mode') or '').strip() or None          # cash 现金领取 / accum 累积生息
    accum_rate = _premium_norm_rate(_f('accum_rate'), None)    # 红利累积生息年利率（页面填 1.75 或 0.0175 均可）
    if not insurer or not product:
        return json.dumps({'ok': False, 'msg': '请选择保司与产品'}), 200, {'Content-Type': 'application/json'}
    if premium <= 0:
        return json.dumps({'ok': False, 'msg': '请输入保费（元）'}), 200, {'Content-Type': 'application/json'}
    res, err = _premium_calc(insurer, product, sex, age, pay_term, premium, div_mode, accum_rate)
    if err:
        return json.dumps({'ok': False, 'msg': err}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    return json.dumps({'ok': True, 'data': res}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/premium-products')
@login_required
@permission_required('crm.view')
def premium_products():
    """已导入产品清单（含行数/双件完整性），供页面展示与删除。"""
    rows = db.session.query(
        CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name,
        CrmPremiumFactor.kind, db.func.count(CrmPremiumFactor.id)
    ).group_by(CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name, CrmPremiumFactor.kind).all()
    agg = {}
    for ins, prod, kind, cnt in rows:
        k = (ins, prod)
        agg.setdefault(k, {'insurer': ins, 'product': prod, 'rate': 0, 'cash': 0})
        if kind == 'cash' or (kind == 'factor' and cnt):
            agg[k]['cash'] += cnt
        if kind == 'rate' or kind == 'factor':
            agg[k]['rate'] += cnt
    out = []
    for v in agg.values():
        v['ready'] = (v['rate'] > 0 and v['cash'] > 0)
        out.append(v)
    out.sort(key=lambda x: (x['insurer'], x['product']))
    return json.dumps(out, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/premium-product/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def premium_product_delete():
    """删除某保司产品的全部因子数据（清测试/错导数据）。"""
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    if not insurer or not product:
        return json.dumps({'ok': False, 'msg': '参数缺失'}), 200, {'Content-Type': 'application/json'}
    n = CrmPremiumFactor.query.filter_by(insurer_name=insurer, product_name=product).delete()
    db.session.commit()
    return json.dumps({'ok': True, 'msg': '已删除 %d 行' % n}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


# ===================== 计划书模块（行销辅助：录入 + 测算 + 保存 + 查看） =====================
# 复用 _premium_calc 统一测算内核：手工录入保单要素 → 调已导入费率/现金价值因子 →
# 自动算基本保额与各保单年度现金价值/单利/IRR；逐年演示实时重算，不入库。

@bp.route('/crm/proposals')
@login_required
@permission_required('crm.view')
def proposals():
    """计划书列表 + 新建表单（手工录入保单要素，实时测算预览）。"""
    products = db.session.query(
        CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name
    ).distinct().order_by(CrmPremiumFactor.insurer_name, CrmPremiumFactor.product_name).all()
    rows = CrmProposal.query.order_by(CrmProposal.id.desc()).all()
    return render_template('crm/proposals.html',
                           products=[{'insurer': p[0], 'product': p[1]} for p in products],
                           proposals=rows)


@bp.route('/crm/proposal/new', methods=['POST'])
@login_required
@permission_required('crm.view')
def proposal_new():
    """保存计划书：先复用 _premium_calc 验证因子数据是否齐全，再入库。"""
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    client = (_f('client_name') or '').strip()
    sex = _norm_sex(_f('sex'))
    age = _to_int(_f('age'))
    pay_term = _norm_pay_term(_f('pay_term'))
    premium = _to_float(_f('premium'))
    illustration_mode = (_f('illustration_mode') or '标准').strip()
    # 分红演示口径（条款3.2）：cash 现金领取 / accum 累积生息；累积生息年利率默认 1.75%
    div_mode = (_f('div_mode') or 'accum').strip()
    accum_rate = _premium_norm_rate(_f('accum_rate'), DIV_ACCUM_RATE_DEFAULT)
    notes = (_f('notes') or '').strip()
    if not insurer or not product:
        flash('请选择保司与产品')
        return redirect(url_for('crm.proposals'))
    if premium <= 0:
        flash('请输入年交保费（元）')
        return redirect(url_for('crm.proposals'))
    # 试算：校验对应产品因子数据存在，并取基本保额快照
    res, err = _premium_calc(insurer, product, sex, age, pay_term, premium)
    if err:
        flash('测算失败，无法保存：' + err)
        return redirect(url_for('crm.proposals'))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    no = 'PR' + datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:4]
    p = CrmProposal(
        proposal_no=no, insurer_name=insurer, product_name=product, client_name=client,
        sex=sex, issue_age=age, pay_term=pay_term, premium=premium,
        base_sa=res['base_sa'], illustration_mode=illustration_mode,
        div_mode=div_mode, accum_rate=accum_rate,
        owner_user=session.get('username', ''), status='draft',
        notes=notes, created_at=now, updated_at=now)
    db.session.add(p)
    db.session.commit()
    flash('计划书「%s」已保存' % no)
    return redirect(url_for('crm.proposal_view', pid=p.id))


@bp.route('/crm/proposal/<int:pid>')
@login_required
@permission_required('crm.view')
def proposal_view(pid):
    """查看计划书：加载要素，实时重算逐年利益演示（保证数据始终与最新因子一致）。

    分红演示口径可临时切换：?mode=cash（现金领取）/ ?mode=accum（累积生息）
    / ?mode=pua（购买交清增额保险），
    ?rate=0.0175 覆盖累积生息年利率——不落库，只影响本次展示（打印/长图同口径）。
    """
    p = CrmProposal.query.get_or_404(pid)
    mode = (request.args.get('mode') or '').strip() or (p.div_mode or 'accum')
    if mode not in ('cash', 'accum', 'pua'):
        mode = 'accum'
    rate = _premium_norm_rate(request.args.get('rate'),
                              getattr(p, 'accum_rate', None) or DIV_ACCUM_RATE_DEFAULT)
    res, err = _premium_calc(p.insurer_name, p.product_name, p.sex, p.issue_age,
                             p.pay_term, p.premium, mode, rate)
    if err:
        flash('该计划书对应产品的因子数据缺失，无法演示：' + err)
    return render_template('crm/proposal_view.html', p=p, illu=res, err=err,
                           div_mode=mode, accum_rate=rate)


@bp.route('/crm/proposal/delete', methods=['POST'])
@login_required
@permission_required('crm.view')
def proposal_delete():
    """删除计划书（仅删录入记录，不影响已导入的因子数据）。"""
    pid = _to_int(_f('pid'))
    p = CrmProposal.query.get(pid)
    if p:
        db.session.delete(p)
        db.session.commit()
        flash('计划书「%s」已删除' % p.proposal_no)
    return redirect(url_for('crm.proposals'))


# ============================================================================
# 智能体工作台：产品计划书引擎配置（2026-10-06 威哥拍板）
#   选产品 → 上传保司文件 → 智能体自动解析并列出列映射（可人工修正）
#   → 输入要素「试跑」看演示结果 → 确认后点「安装」→ 配置落库 + 因子入库
#   ⚠️ 配置驱动，不生成代码：换一家公司只是多一条 CrmProposalProduct 配置。
# ============================================================================

def _peek_header(path):
    """窥探上传文件的表头：返回 (sheet_name, header_row_no, header_list, field_of)。
    找不到结构化表头返回 (None, 0, [], {})。用于工作台把「识别到的列映射」摆给管理员看。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    got = (None, 0, [], {})
    # ⚠️ 别一命中就收工：幸福人寿这类「一张表装所有东西」的文件，
    #    第一个 sheet 往往只是参数页（"红利演示利率 | 其对应分红实现率"），
    #    看起来也像表头 → 会把毫不相干的列摆给管理员当「识别到的列映射」。
    #    改成：扫全部 sheet，取「识别出的字段最多」的那张作为展示样本。
    for ws in wb.worksheets:
        try:
            rows = [list(r) for r in ws.iter_rows(min_row=1, max_row=30, values_only=True)]
        except Exception:
            continue
        for i, row in enumerate(rows[:25]):
            if not row:
                continue
            fof = _premium_map_header(row)
            if len(fof) > len(got[3]):
                got = (ws.title, i + 1, list(row), fof)
    wb.close()
    # 至少要命中「保单年度」这类定位列才算一张真正的因子表，否则宁愿什么都不展示
    if 'policy_year' not in got[3].values():
        return (None, 0, [], {})
    return got


def _pp_next_code():
    """生成配置编号 PPE-YYYYMM-NNN。"""
    prefix = 'PPE-' + datetime.now().strftime('%Y%m')
    last = CrmProposalProduct.query.filter(
        CrmProposalProduct.code.like(prefix + '%')).order_by(CrmProposalProduct.id.desc()).first()
    n = 1
    if last and last.code:
        try:
            n = int(last.code.rsplit('-', 1)[-1]) + 1
        except ValueError:
            n = 1
    return '%s-%03d' % (prefix, n)


def _pp_match_contract(insurer, product):
    """签约匹配：按 保司+产品名 在 CrmProductContract 里找已签约合约。
    返回 (contract_id, signed)；查不到返回 (None, 'no')。"""
    c = CrmProductContract.query.filter(
        CrmProductContract.insurer_name == insurer,
        CrmProductContract.product_name == product,
        CrmProductContract.status != 'ended').order_by(CrmProductContract.id.desc()).first()
    if not c and product:
        # 保司简称/全称差异兜底：按产品名精确匹配一次
        c = CrmProductContract.query.filter(
            CrmProductContract.product_name == product,
            CrmProductContract.status != 'ended').order_by(CrmProductContract.id.desc()).first()
    return (c.id if c else None), ('yes' if c else 'no')


def _pp_signed_products():
    """已签约产品清单（供工作台「应配置」提示）：[{insurer, product, insurance_type, contract_id}]。"""
    rows = CrmProductContract.query.filter(CrmProductContract.status != 'ended').all()
    out, seen = [], set()
    for c in rows:
        k = ((c.insurer_name or '').strip(), (c.product_name or '').strip())
        if not k[1] or k in seen:
            continue
        seen.add(k)
        out.append({'insurer': k[0], 'product': k[1],
                    'insurance_type': c.insurance_type or '', 'contract_id': c.id})
    return out


@bp.route('/crm/proposal-engine')
@login_required
@permission_required('crm.view')
def proposal_engine():
    """智能体工作台：签约匹配（应配置清单）+ 已配置清单 + 上传解析入口。"""
    cfgs = CrmProposalProduct.query.order_by(
        CrmProposalProduct.id.desc()).all()
    done = {}
    for c in cfgs:
        done.setdefault((c.insurer_name or '').strip(), set()).add((c.product_name or '').strip())
    signed = _pp_signed_products()
    todo = [s for s in signed if s['product'] not in done.get(s['insurer'], set())]
    return render_template('crm/proposal_engine.html',
                           configs=cfgs, signed=signed, todo=todo,
                           cats=PROPOSAL_CATEGORIES, cat_label={c['key']: c['name'] for c in PROPOSAL_CATEGORIES},
                           mode_label=dict(DIV_MODES),
                           can_manage=session.get('perms', {}).get('crm.contract.manage'))


@bp.route('/crm/proposal-engine/parse', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def proposal_engine_parse():
    """智能体第一步：上传文件 → 自动解析 → 返回统计与「识别到的列映射」（不入库）。"""
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    if not insurer or not product:
        return json.dumps({'ok': False, 'msg': '请填写保司名称与产品名称'}), 200, {'Content-Type': 'application/json'}
    _premium_purge_stale()
    _lbl = {'rate_file': '费率表', 'cash_file': '现金价值表',
            'plan_file': '计划书', 'div_file': '红利表'}
    files = {k: request.files.get(k) for k in ('rate_file', 'cash_file', 'plan_file', 'div_file')}
    files = {k: v for k, v in files.items() if v and v.filename}
    if not files:
        return json.dumps({'ok': False, 'msg': '请至少上传一个文件（计划书 / 费率表 / 现金价值表 / 红利表）'}), 200, {'Content-Type': 'application/json'}
    for k, v in files.items():
        ext = v.filename.rsplit('.', 1)[-1].lower() if '.' in v.filename else ''
        if ext not in ('xlsx', 'xlsm'):
            return json.dumps({'ok': False, 'msg': '%s「%s」不是 Excel 文件，系统只支持 .xlsx / .xlsm'
                               % (_lbl.get(k, '文件'), v.filename)}), 200, {'Content-Type': 'application/json'}
    tmp = _premium_tmp_dir()
    token = datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6]
    saved = {}
    for k, v in files.items():
        p = os.path.join(tmp, 'pe_%s_%s.xlsx' % (k, token))
        v.save(p)
        saved[k] = (p, v.filename)
    try:
        all_rows, warns = [], []
        if 'plan_file' in saved:      # 计划书单文件：全 sheet 抽取费率+现价
            rows, rep, _kind = _premium_parse_plan(saved['plan_file'][0], insurer, product)
            all_rows.extend(rows)
            warns.extend(rep.get('warn') or [])
        if 'rate_file' in saved:
            rows, rep, _k = _premium_parse_file(saved['rate_file'][0], 'rate', insurer, product)
            all_rows.extend(rows)
            warns.extend(rep.get('warn') or [])
        if 'cash_file' in saved:
            rows, rep, _k = _premium_parse_file(saved['cash_file'][0], 'cash', insurer, product)
            all_rows.extend(rows)
            warns.extend(rep.get('warn') or [])
        # 红利表：按四键挂到已有行上（红利文件没有现价，自成一行测算会缺现价）
        div_name = ''
        if 'div_file' in saved:
            div_rows, _dr = _premium_parse_div(saved['div_file'][0], insurer, product)
            div_name = saved['div_file'][1]
            if div_rows:
                idx = {}
                for r in all_rows:
                    idx[(r.get('sex', ''), r.get('issue_age', 0),
                         r.get('pay_term', 0), r.get('policy_year', 0))] = r
                for d in div_rows:
                    t = idx.get((d.get('sex', ''), d.get('issue_age', 0),
                                 d.get('pay_term', 0), d.get('policy_year', 0)))
                    if t is not None:
                        t['div_g'] = d.get('div_g', 0.0) or 0.0
                        t['div_d'] = d.get('div_d', 0.0) or 0.0
    except Exception as e:
        return json.dumps({'ok': False, 'msg': '解析失败：%s' % e}), 200, {'Content-Type': 'application/json'}
    # —— 分类智能体：页面选了就听页面的，没选就按「名称关键字 + 有没有红利列」自动归类 ——
    cat = _pp_cat((_f('category') or '').strip() or _pp_detect_category(product, all_rows))
    need_cv = 'cv' in (cat.get('need') or [])
    has_sa = any((r.get('sa') or 0) > 0 for r in all_rows)
    has_cv = any((r.get('cv') or 0) > 0 for r in all_rows)
    if not has_sa or (need_cv and not has_cv):
        lack = '基本保额（费率）' if not has_sa else '现金价值'
        return json.dumps({'ok': False, 'msg': '未识别到%s数据——分类「%s」需要它，'
                           '请补齐文件（费率表+现金价值表，或含两者的 EXCEL 计划书）'
                           % (lack, cat['name'])},
                          ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    checks = _pp_run_checks(cat, all_rows)
    miss = [c['name'] for c in checks if c['level'] == '必须' and not c['ok']]
    if miss:
        return json.dumps({'ok': False,
                           'msg': '分类「%s」要求的数据不全：缺少 %s。'
                                  '①换一张更全的表 ②或把产品改到别的分类（推荐：%s）'
                                  % (cat['name'], '、'.join(miss),
                                     _pp_cat_name(_pp_detect_category(product, all_rows)))},
                          ensure_ascii=False), 200, {'Content-Type': 'application/json'}
    # 列映射：摆给管理员看，人工可修正
    first = saved.get('plan_file') or saved.get('rate_file') or saved.get('cash_file') or saved.get('div_file')
    sheet, hrow, header, fof = _peek_header(first[0])
    col_map = {}
    for i, fld in (fof or {}).items():
        if i < len(header) and header[i] is not None:
            col_map[fld] = str(header[i]).strip()
    # 分红参数：保司因子档里的「分红信息配置」小表（定价利率/红利演示利率/分红比例/累积生息利率）
    _cfg_src = (saved.get('plan_file') or saved.get('rate_file')
                or saved.get('div_file') or saved.get('cash_file'))
    div_cfg = _premium_parse_div_config(_cfg_src[0]) if _cfg_src else {}
    if not div_cfg.get('accum_rate'):
        div_cfg['accum_rate'] = DIV_ACCUM_RATE_DEFAULT
    payload = {'insurer': insurer, 'product': product, 'rows': all_rows,
               'files': {k: v[1] for k, v in saved.items()},
               'col_map': col_map, 'sheet': sheet or '', 'header_row': hrow,
               'div_name': div_name, 'div_cfg': div_cfg, 'category': cat['key']}
    with open(os.path.join(tmp, 'pe_%s.json' % token), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False)
    return json.dumps({
        'ok': True, 'token': token, 'insurer': insurer, 'product': product,
        'total': len(all_rows),
        'rate_total': sum(1 for r in all_rows if (r.get('sa') or 0) > 0),
        'cash_total': sum(1 for r in all_rows if (r.get('cv') or 0) > 0),
        'div_total': sum(1 for r in all_rows if (r.get('div_g') or 0) > 0 or (r.get('div_d') or 0) > 0),
        'pua_total': sum(1 for r in all_rows if (r.get('pua_sa') or 0) > 0),
        'col_map': col_map, 'sheet': sheet or '', 'header_row': hrow,
        'div_cfg': div_cfg,
        # —— 分类智能体 ——
        'category': cat['key'], 'category_name': cat['name'],
        'cat_detected': (_f('category') or '').strip() == '',
        'cat_tips': cat.get('tips') or [], 'cat_files': cat.get('files') or [],
        'cat_modes': cat.get('modes') or [], 'cat_default_mode': cat.get('default_mode') or '',
        'checks': _pp_run_checks(cat, all_rows),
        'warn': warns[:5], 'sample': all_rows[:5],
    }, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/proposal-engine/trial', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def proposal_engine_trial():
    """智能体第二步：入库前试跑——用解析结果直接演示，先看对不对，再决定装不装。"""
    token = _f('token')
    pf = os.path.join(_premium_tmp_dir(), 'pe_%s.json' % token)
    if not os.path.exists(pf):
        return json.dumps({'ok': False, 'msg': '解析已过期，请重新上传文件'}), 200, {'Content-Type': 'application/json'}
    with open(pf, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    sex = _norm_sex(_f('sex'))
    age = _to_int(_f('age'))
    pay_term = _norm_pay_term(_f('pay_term'))
    premium = _to_float(_f('premium'))
    if premium <= 0:
        return json.dumps({'ok': False, 'msg': '请输入保费（元）'}), 200, {'Content-Type': 'application/json'}
    rows = payload['rows']
    sub = [r for r in rows if r.get('sex') == sex and r.get('issue_age') == age
           and r.get('pay_term') == pay_term]
    if not sub:
        # 兜底：只按 性别+年龄（个别因子档交费方式口径不同）
        sub = [r for r in rows if r.get('sex') == sex and r.get('issue_age') == age]
    if not sub:
        return json.dumps({'ok': False, 'msg': '解析结果里没有 性别=%s / 年龄=%s 的数据，'
                           '请换一组要素试，或检查文件是否覆盖该档' % (sex, age)}), 200, {'Content-Type': 'application/json'}
    # 分红口径：页面选的 > 该产品分类的默认口径 > 累积生息
    cat = _pp_cat(payload.get('category'))
    div_mode = (_f('div_mode') or '').strip() or (cat.get('default_mode') or 'accum')
    if div_mode == 'pua' and not any((r.get('pua_sa') or 0) > 0 for r in rows):
        div_mode = 'accum'
    cfgd = payload.get('div_cfg') or {}
    rate = _premium_norm_rate(_f('accum_rate'), cfgd.get('accum_rate') or DIV_ACCUM_RATE_DEFAULT)
    res, err = _premium_calc_rows(sub, sex, age, pay_term, premium, div_mode, rate)
    if err:
        return json.dumps({'ok': False, 'msg': err}), 200, {'Content-Type': 'application/json'}
    res['div_cfg'] = payload.get('div_cfg') or {}
    res['category'] = cat['key']
    res['category_name'] = cat['name']
    return json.dumps({'ok': True, 'data': res}, ensure_ascii=False), 200, {'Content-Type': 'application/json'}


@bp.route('/crm/proposal-engine/install', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def proposal_engine_install():
    """智能体第三步：确认安装——配置落 CrmProposalProduct + 因子入库，产品即刻可用。"""
    token = _f('token')
    insurer = (_f('insurer') or '').strip()
    product = (_f('product') or '').strip()
    tmp = _premium_tmp_dir()
    pf = os.path.join(tmp, 'pe_%s.json' % token)
    if not os.path.exists(pf):
        flash('解析已过期，请重新上传文件')
        return redirect(url_for('crm.proposal_engine'))
    with open(pf, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    rows = _premium_merge_rows(payload.get('rows') or [])
    if not rows:
        flash('解析数据为空，无法安装')
        return redirect(url_for('crm.proposal_engine'))
    cid, signed = _pp_match_contract(insurer, product)
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cfg = CrmProposalProduct.query.filter_by(insurer_name=insurer, product_name=product).first()
    if not cfg:
        cfg = CrmProposalProduct(code=_pp_next_code(), created_at=now)
    cfg.insurer_name = insurer
    cfg.product_name = product
    cfg.insurance_type = (_f('insurance_type') or '').strip()
    cfg.contract_id = cid
    cfg.signed = signed
    cfg.source_files = json.dumps(payload.get('files') or {}, ensure_ascii=False)
    cfg.sheet_name = payload.get('sheet') or ''
    cfg.header_row = payload.get('header_row') or 0
    cfg.col_map = json.dumps(payload.get('col_map') or {}, ensure_ascii=False)
    cfg.base_unit = 1000.0
    cfg.has_dividend = 1 if any((r.get('div_g') or 0) > 0 or (r.get('div_d') or 0) > 0 for r in rows) else 0
    # 分类随解析结果一起存（该类的智能体认哪些列、能出哪几种演示口径都由它决定）
    cfg.category = (_f('category') or '').strip() or payload.get('category') or \
        _pp_detect_category(product, rows)
    cat = _pp_cat(cfg.category)
    cfg.div_mode = (_f('div_mode') or '').strip() or (cat.get('default_mode') or 'accum')
    # 分红演示参数（保司「分红信息配置」小表读出来的，页面可覆盖累积生息利率）
    dcfg = payload.get('div_cfg') or {}
    cfg.accum_rate = _premium_norm_rate(_f('accum_rate'), dcfg.get('accum_rate') or DIV_ACCUM_RATE_DEFAULT)
    cfg.div_ratio = dcfg.get('ratio') or 0.0
    cfg.div_demo_rate = dcfg.get('demo_rate') or 0.0
    cfg.status = 'installed'
    cfg.factor_rows = len(rows)
    cfg.trial_note = (_f('trial_note') or '').strip()
    cfg.installed_at = now
    cfg.updated_at = now
    cfg.owner_user = session.get('username', '')
    db.session.add(cfg)
    # 因子入库：同产品全量替换，避免重复导入叠加出脏数据
    try:
        CrmPremiumFactor.query.filter_by(insurer_name=insurer, product_name=product).delete()
        batch = datetime.now().strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:4]
        maps = [{
            'insurer_name': insurer, 'product_name': product, 'sex': r.get('sex', ''),
            'issue_age': r.get('issue_age', 0), 'pay_term': r.get('pay_term', 0),
            'policy_year': r.get('policy_year', 0), 'sa': r.get('sa', 0.0), 'cv': r.get('cv', 0.0),
            'cv_begin': r.get('cv_begin', 0.0), 'tr': r.get('tr', 0.0), 'trnp': r.get('trnp', 0.0),
            'cvnp': r.get('cvnp', 0.0), 'sb': r.get('sb', 0.0),
            'dividend_rate': r.get('dividend_rate', 0.0), 'convert_cv': r.get('convert_cv', 0.0),
            'div_g': r.get('div_g', 0.0), 'div_d': r.get('div_d', 0.0),
            'pua_sa': r.get('pua_sa', 0.0), 'pua_add': r.get('pua_add', 0.0),
            'pua_cv': r.get('pua_cv', 0.0),
            'base_unit': r.get('base_unit', 1000.0),
            'kind': 'factor' if ((r.get('sa') or 0) > 0 and (r.get('cv') or 0) > 0)
                    else ('cash' if (r.get('cv') or 0) > 0 else 'rate'),
            'src_file': ','.join((payload.get('files') or {}).values()) or '智能体导入',
            'batch_no': batch, 'created_at': now,
        } for r in rows]
        db.session.bulk_insert_mappings(CrmPremiumFactor, maps)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash('安装失败：%s' % e)
        return redirect(url_for('crm.proposal_engine'))
    try:
        os.remove(os.path.join(tmp, 'pe_%s.json' % token))
    except OSError:
        pass
    for k in ('rate_file', 'cash_file', 'plan_file', 'div_file'):
        try:
            os.remove(os.path.join(tmp, 'pe_%s_%s.xlsx' % (k, token)))
        except OSError:
            pass
    tip = '' if signed == 'yes' else '；⚠️ 该产品未签约，请及时签约'
    flash('已安装「%s %s」：配置 %s，分类【%s】，因子 %d 行%s'
          % (insurer, product, cfg.code, cat['name'], len(rows), tip))
    return redirect(url_for('crm.proposal_engine'))


@bp.route('/crm/proposal-engine/delete', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def proposal_engine_delete():
    """删除产品引擎配置（同时清掉该产品已入库因子，避免留孤儿数据）。"""
    cfg = CrmProposalProduct.query.get(_to_int(_f('cfg_id')))
    if not cfg:
        flash('配置不存在')
        return redirect(url_for('crm.proposal_engine'))
    name = '%s %s' % (cfg.insurer_name, cfg.product_name)
    n = CrmPremiumFactor.query.filter_by(
        insurer_name=cfg.insurer_name, product_name=cfg.product_name).delete()
    db.session.delete(cfg)
    db.session.commit()
    flash('已删除配置「%s」，同时清理因子 %d 行' % (name, n))
    return redirect(url_for('crm.proposal_engine'))


# ===================== ⑦ 产品佣金系数（2026-11-02）=====================
# 威哥口径：一键导入「采购成本下发」Excel → 按渠道类型(互联网/线下)设销售系数 →
# 自动生成 基础佣金 / 销售费用 / 经营费用。
#   · 导入：解析 11月采购成本表（Sheet1，17 列：B保险公司/C险种/D渠道/E互联网产品/
#           F互险网重疾/G投保区域/H保障期限/I缴费期/J首年采购/K首年基础佣金/L首年方案/
#           M备注/N第二年采购/O第三年/P第四年/Q第五年采购）；E列="是"→互联网，空→线下。
#   · 系数：按渠道类型各一套（每套含 6 个销售系数 + 基础佣金比例 + 经营费用比例 + 取整规则）。
#   · 计算：对每个费用项 V（首年基础佣金/首年方案/第二~五年采购）分别
#           基础佣金 = V × 基础佣金比例%
#           销售费用 = 取整规则(V × 销售系数%)
#           经营费用 = V × 经营费用比例%
#           再跨 6 项求和；线下/互联网各自套用本渠道系数。

def _comm_coef_of(channel_type):
    """取某渠道类型的系数配置；未配置则返回带默认值的「瞬态」实例（不落库）。"""
    cfg = CrmCommCoefConfig.query.filter_by(channel_type=channel_type).first()
    if cfg is None:
        cfg = CrmCommCoefConfig(channel_type=channel_type)
        if channel_type == 'internet':
            # 互联网默认：基础佣金 80% / 销售费用 0 / 经营费用 5%
            cfg.base_ratio, cfg.operate_ratio, cfg.sales_ratio, cfg.round_rule, cfg.team_base_ratio = 80.0, 5.0, 0.0, '3', 80.0
            cfg.ryc_base_ratio = 80.0
        else:
            # 线下默认：基础佣金 80% / 销售费用 5% / 经营费用 4%
            cfg.base_ratio, cfg.operate_ratio, cfg.sales_ratio, cfg.round_rule, cfg.team_base_ratio = 80.0, 4.0, 5.0, '3', 80.0
            cfg.ryc_base_ratio = 80.0
    return cfg


def _comm_net_ratio_of(cfg):
    """销售净费比例(%) = 基础佣金比例 + 销售费用比例 + 经营费用比例（随渠道/设置自动测算）。

    线下默认 80+5+4=89%；互联网默认 80+0+5=85%。用户改任一比例，净费比例自动跟随。
    """
    if cfg is None:
        return 0.0
    return (cfg.base_ratio or 0.0) + (cfg.sales_ratio or 0.0) + (cfg.operate_ratio or 0.0)


@bp.app_template_filter('trunc_pct')
def _comm_trunc_pct(v):
    """模板显示用：占保费比例 → 百分比文本，第三位起直接舍去（四舍五也舍，不四舍五入）。

    先 round 到 6 位消掉浮点尾差（如 18.24/100 → 0.18239999999999998 会误显示 18.23%），
    再用 Decimal 截断而非浮点 floor：避免 0.0425 → 424.9999 → 4.24% 这类尾差。
    """
    if not v:
        return ''
    d = Decimal(str(round(float(v) * 100, 6)))
    return str(d.quantize(Decimal('0.01'), rounding=ROUND_DOWN)) + '%'


def _comm_pct_num(v):
    """导 Excel 用：占保费比例 → 百分比数值（保留 2 位，第三位起直接舍去，与页面显示同口径）。
    先 round 到 6 位消掉浮点尾差（如 5.6/100 → 0.055999999999999994 会误截成 5.59）。
    0/空 → None（与页面留空一致）。"""
    if not v:
        return None
    d = Decimal(str(round(float(v) * 100, 6)))      # 百分点（已消尾差）
    return float(d.quantize(Decimal('0.01'), rounding=ROUND_DOWN))


def _comm_ryc_pct_num(v, hide=False):
    """导 Excel 用：RYC续年度佣金（二改 2026-11-03）—— 数据已为整数值，导出时保留 1 位小数；
    若开启「<1% 自动置空」且该年佣金 < 1% → 留空（与页面一致）。"""
    if v is None:
        return None
    if hide and v < 1:
        return None
    return round(float(v), 1)


def _comm_apply_round(val, rule):
    """销售费用取整规则（全部为「直接舍去」，四舍五也舍，不做四舍五入），val 为占保费比例（0.0215 = 2.15%）：

    1 = 保留2位小数（第三位起舍去）  → 2.159% → 2.15%
    2 = 取整（舍去小数）             → 2.9%   → 2%
    3 = 减1%后保留2位（默认）        → 2.15% × 99% = 2.1285% → 2.12%
    """
    d = Decimal(str(val)) * Decimal('100')          # 占保费比例 → 百分点
    if rule == '2':
        return float(d.quantize(Decimal('1'), rounding=ROUND_DOWN)) / 100.0
    if rule == '3':
        # 先减 1%（×99%），再由第三位起直接舍去
        return float(
            (d * Decimal('0.99')).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        ) / 100.0
    return float(d.quantize(Decimal('0.01'), rounding=ROUND_DOWN)) / 100.0


# ---- 投保区域（销售分公司）归一化：用于按销售分公司查询，全国视为匹配任意省份 ----
_COMM_VALID_REGIONS = {
    '北京', '上海', '天津', '重庆', '广东', '江苏', '浙江', '安徽', '福建', '江西',
    '山东', '河南', '湖北', '湖南', '广西', '海南', '四川', '贵州', '云南', '陕西',
    '甘肃', '青海', '河北', '山西', '辽宁', '吉林', '黑龙江', '西藏', '新疆',
    '内蒙古', '宁夏', '深圳', '全国',
}

def _comm_region_tokens(raw):
    """把『投保区域』自由文本归一化为省/市令牌集合；全国单独成令牌。"""
    if not raw:
        return set()
    s = str(raw)
    s = re.sub(r'\([^)]*\)|（[^）]*）', '', s)   # 去掉括号及其内容，如（除深圳市）
    s = s.replace('省', '').replace('市', '')
    toks = re.split(r'[、，,\s/]+', s)
    out = set()
    for t in toks:
        t = t.strip()
        if t in _COMM_VALID_REGIONS:
            out.add(t)
    return out


def _comm_region_options():
    """下拉框可选的销售分公司（去重、排序；全国单独列出）。"""
    opts = set()
    for r in CrmProductCommission.query.with_entities(CrmProductCommission.region).all():
        for t in _comm_region_tokens(r[0]):
            if t != '全国':
                opts.add(t)
    return sorted(opts)


# 明显非产品合约的「说明/活动」关键字段（如采购成本表夹带的「活动信息：…」整行）
_COMM_STRONG_NOTE_KW = ('活动信息', '活动奖励')
_COMM_SOFT_NOTE_KW = ('说明', '注：', '注:', '备注', '提示', '注意', '须知', '公告', '通知', '公示')


def _comm_is_note_row(insurer, product, note, vals):
    """自动识别与产品合约无关的说明/活动行，返回 True 表示应跳过：
    · 含 活动信息/活动奖励 等强信号 → 直接判定为非产品行；
    · 无任何采购/佣金数值，且含 说明/备注 等软信号 → 判定为非产品行；
    · 无产品名，且「保险公司」一栏是一整段带标点的说明文字（如「结算要求：…。」）→ 非产品行。"""
    text = ' '.join([insurer or '', product or '', note or ''])
    if any(kw in text for kw in _COMM_STRONG_NOTE_KW):
        return True
    if not any(vals) and any(kw in text for kw in _COMM_SOFT_NOTE_KW):
        return True
    if not any(vals) and not (product or '').strip() and _comm_has_punct(insurer):
        return True
    return False


def _comm_has_punct(s):
    """是否含标点（说明文字的特征）。保司简称正常不含标点。"""
    return any(ch in (s or '') for ch in '：:。；;，,、|/\\%％（）()【】[]《》')


def _comm_valid_month(s):
    """归属月份合法性：必须形如 YYYY-MM，年份 2000~2100、月份 01~12。
    非法值（如 2099-13、随意字符）一律判无效，避免脏月份污染「归属月份」下拉。"""
    m = re.match(r'^(\d{4})-(\d{1,2})$', (s or '').strip())
    if not m:
        return False
    y, mo = int(m.group(1)), int(m.group(2))
    return 2000 <= y <= 2100 and 1 <= mo <= 12


def _comm_valid_insurer(s):
    """保司名合法性（用于筛选下拉）：2~20 字、不含标点、不含说明类关键词。
    挡掉历史导入把整段说明/活动文字写进「保司」的情况。"""
    t = (s or '').strip()
    if not (2 <= len(t) <= 20):
        return False
    if _comm_has_punct(t):
        return False
    if any(kw in t for kw in _COMM_STRONG_NOTE_KW + _COMM_SOFT_NOTE_KW):
        return False
    return True



def _comm_build_rows(channel=None, region='all', month=None, insurer=None, pay_period=None, product=None):
    """生成结果行（含三层口径 + 首年/续期明细），并按 销售分公司/归属月份/保司/缴别/产品 过滤。

    三层口径（2026-11-05 最终定稿）：
      · 股东层面：签约费用 = 首佣合计(FYC+首年方案)；销售净费 = 签约费用 × 净费比例%（不取整）
      · 子公司层面：销售费用合计 = 签约费用 × 净费比例% → 减1% → 直接舍去；
                    FYC首佣/销售费用/经营费用 = 销售费用合计 × 对应设置比例%（各截断1位）
      · 团队长层面：FYC首佣 = 子公司层面 FYC首佣
    销售分公司过滤：选具体省 → 命中该省或全国；选全国 → 仅全国；all → 全部。
    归属月份过滤：month 非空时只取该月数据。
    保司/缴别/产品：非空时按精确/模糊匹配。
    """
    cfgs = {c[0]: _comm_coef_of(c[0]) for c in COMM_CHANNELS}
    q = CrmProductCommission.query
    if month:
        q = q.filter_by(month=month)
    if insurer:
        q = q.filter_by(insurer=insurer)
    if pay_period:
        q = q.filter_by(pay_period=pay_period)
    if product:
        q = q.filter(CrmProductCommission.product.like('%' + product + '%'))
    src = q.order_by(CrmProductCommission.insurer, CrmProductCommission.product).all()
    # 佣金（基础佣金）：首年 + 续期全部 6 项都计提
    items_all = ['base_comm', 'plan', 'purchase_y2', 'purchase_y3', 'purchase_y4', 'purchase_y5']
    out = []
    for r in src:
        ct = 'internet' if r.is_internet else 'offline'
        if channel and channel != ct:
            continue
        # 销售分公司（投保区域）过滤
        rtoks = _comm_region_tokens(r.region)
        if region and region != 'all':
            if not (('全国' in rtoks) or (region in rtoks)):
                continue
        cfg = cfgs[ct]
        br = cfg.base_ratio or 0        # 基础佣金比例%（首年佣金系数）
        orr = cfg.operate_ratio or 0    # 经营费用比例%（仅首年）
        sr = cfg.sales_ratio or 0       # 销售费用比例%（仅首年；只参与净费比例测算，不再单独计提）
        # RYC 分年期佣金比例（每年可不同）：
        #   本年系数「显式值优先」，含 0 = 该年续期佣金 0%（不再把 0 当作「未设→沿用基础比例」）；
        #   仅当该年系数为 None（确实未配置）时才回退到统一兜底 ryc_base_ratio，再退基础佣金比例。
        _rb = cfg.ryc_base_ratio
        rr2 = cfg.ryc_ratio_y2 if cfg.ryc_ratio_y2 is not None else (_rb or br)
        rr3 = cfg.ryc_ratio_y3 if cfg.ryc_ratio_y3 is not None else (_rb or br)
        rr4 = cfg.ryc_ratio_y4 if cfg.ryc_ratio_y4 is not None else (_rb or br)
        rr5 = cfg.ryc_ratio_y5 if cfg.ryc_ratio_y5 is not None else (_rb or br)
        fyc = r.base_comm or 0.0
        plan = r.plan or 0.0
        y2 = r.purchase_y2 or 0.0
        y3 = r.purchase_y3 or 0.0
        y4 = r.purchase_y4 or 0.0
        y5 = r.purchase_y5 or 0.0
        # 历史导入遗留的说明/活动行（既无产品名、又无任何数值）：读取时直接过滤，避免污染清单与下拉
        if not (r.product or '').strip() and not any([fyc, plan, y2, y3, y4, y5]):
            continue
        # 归属月份非法（如历史脏数据 2099-13）：读取时过滤，避免出现在「归属月份」下拉
        if (r.month or '').strip() and not _comm_valid_month(r.month):
            continue
        first_amt = fyc + plan          # 首佣合计（FYC + 首年方案）
        # RYC 各年计算佣金（仅佣金；各层级一致）
        # 二改（2026-11-03）：RYC续年度佣金数据取整数值 —— 直接舍去小数（int 截断，非四舍五入）。
        #   purchase 以「百分点/100」存（5%→0.05），ratio 为百分比数值（40 即 40%），
        #   故 实算佣金% = purchase × ratio（如 0.05×40 = 2.0%）；先取该百分比整数值，
        #   再 ÷100 还原为占保费比例用于「基础佣金」汇总；展示时补 1 位小数（2 → 2.0%）。
        ryc_raw = {
            'y2': y2 * rr2, 'y3': y3 * rr3,
            'y4': y4 * rr4, 'y5': y5 * rr5,
        }
        ryc = {yn: int(ryc_raw[yn] or 0.0) for yn in ('y2', 'y3', 'y4', 'y5')}
        ryc_hide_lt1 = bool(cfg.ryc_hide_lt1)   # RYC续年度佣金<1%自动置空（线下/互联网统一规则）
        # 股东·签约费用 = 首佣合计（FYC + 首年方案）
        #   口径（威哥 2026-11-05 定稿）：签约费用就是首佣合计本身，
        #   续期采购成本（RYC2~RYC5）另有四列单独展示，不并入。
        sign_total = first_amt                                  # 股东·签约费用
        # 股东·净费比例 = 基础佣金比例 + 销售费用比例 + 经营费用比例（随渠道自动测算）
        #   线下默认 80+5+4=89%；互联网默认 80+0+5=85%
        net_ratio = _comm_net_ratio_of(cfg)
        # 股东·销售净费 = 签约费用 × 净费比例%
        #   口径（威哥 2026-11-05 最终确认）：股东层面不套用取整规则，直接相乘（页面/导出显示2位）
        #   例：签约费用 5.50% × 89% = 4.895% → 4.89%
        #   线下 89% / 互联网 85%，按渠道自动取值
        _pct_first = Decimal(str(round(first_amt * 100, 4)))
        _net_pct_raw = _pct_first * Decimal(str(net_ratio)) / Decimal('100')   # 百分点，如 4.895
        net = float(_net_pct_raw.quantize(Decimal('0.01'), rounding=ROUND_DOWN)) / 100.0
        # ---- 子公司层面（威哥 2026-11-05 最终定稿：以「销售费用合计」为基数，各按设置比例计提）----
        #   销售费用合计 = 签约费用 × 净费比例% → 减1% → 直接舍去（取整规则<b>仅</b>用于此处）
        #   FYC首佣  = 销售费用合计 × 基础佣金比例%
        #   销售费用 = 销售费用合计 × 销售费用比例%
        #   经营费用 = 销售费用合计 × 经营费用比例%
        #   三项各自「保留1位小数、直接舍去」，页面/导出统一显示 2 位小数。
        #   例（净费比例 89%、80/5/4，签约费用 5.50%）：
        #       销售费用合计 5.50×89% = 4.895 → 减1 → 3.895 → 舍去 3.00%
        #       FYC = 3.00×80% = 2.40%；销售 = 3.00×5% = 0.15% → 0.1%；经营 = 3.00×4% = 0.12% → 0.1%
        _sub_int = int(_net_pct_raw.quantize(Decimal('0.0001'), rounding=ROUND_DOWN))
        _sub_pct = Decimal(str(max(0, _sub_int - 1)))            # 销售费用合计（整数百分点）
        sub_total = float(_sub_pct) / 100.0
        if net_ratio and _sub_pct:
            _d_base = (_sub_pct * Decimal(str(br)) / Decimal('100')) \
                .quantize(Decimal('0.1'), rounding=ROUND_DOWN)
            _d_sales = (_sub_pct * Decimal(str(sr)) / Decimal('100')) \
                .quantize(Decimal('0.1'), rounding=ROUND_DOWN)
            _d_op = (_sub_pct * Decimal(str(orr)) / Decimal('100')) \
                .quantize(Decimal('0.1'), rounding=ROUND_DOWN)
        else:
            _d_base = _d_op = _d_sales = Decimal('0')
        base = float(_d_base) / 100.0                            # 子公司·FYC首佣
        op = float(_d_op) / 100.0                                # 子公司·经营费用
        sales = float(_d_sales) / 100.0                          # 子公司·销售费用
        # 团队长·FYC首佣 = 子公司·FYC首佣（保持不变）
        team = base
        out.append({
            'id': r.id, 'insurer': r.insurer or '', 'product': r.product or '',
            'channel_name': r.channel or '', 'issue_platform': r.issue_platform or '',
            'platform_name': r.platform_name or '', 'need_supplement': r.need_supplement or '',
            'coverage_term': r.coverage_term or '',
            'pay_period': r.pay_period or '', 'region': r.region or '',
            'is_internet': r.is_internet, 'channel': ct,
            'fyc': fyc, 'plan': plan, 'y2': y2, 'y3': y3, 'y4': y4, 'y5': y5,
            'first_year_comm': first_amt,  # 首佣合计 = FYC + 首年方案
            'sign_total': sign_total, 'net': net, 'net_ratio': net_ratio,
            'base': base, 'sales': sales, 'op': op, 'team': team,
            'sub_total': sub_total,   # 子公司·销售费用合计（= 净费比例减1舍去后的基数）
            'base_ratio': br, 'sales_ratio': sr, 'op_ratio': orr,   # 明细作用域提示用
            'ryc_comm': ryc,
            'ryc_ratio': {'y2': rr2, 'y3': rr3, 'y4': rr4, 'y5': rr5},
            'ryc_hide_lt1': ryc_hide_lt1,   # RYC续年度佣金<1%自动置空开关（线下/互联网统一）
        })
    return out, cfgs


def _comm_query_args():
    """解析「产品佣金系数」页面的筛选参数 —— 列表页与一键导出共用同一套口径，
    保证导出的行/列与当前页面所见完全一致（各组织层面不混列）。
    返回 (channel, region, level, insurer, pay_period, product, month)；
    level 只认 all / shareholder / subsidiary / team，非法值回退 all。
    """
    channel = (request.args.get('channel') or 'all').strip()
    region = (request.args.get('region') or 'all').strip()
    level = (request.args.get('level') or 'all').strip()
    if level not in ('all', 'shareholder', 'subsidiary', 'team'):
        level = 'all'
    insurer = (request.args.get('insurer') or 'all').strip()
    pay_period = (request.args.get('pay_period') or 'all').strip()
    product = (request.args.get('product') or '').strip()
    if product == 'all':
        product = ''          # 下拉框「全部产品」等同不过滤
    month = (request.args.get('month') or '').strip()
    if month and month != 'all' and not _comm_valid_month(month):
        month = ''            # 非法月份（脏参数/历史脏数据）忽略，回退最新月
    return channel, region, level, insurer, pay_period, product, month


def _comm_latest_month():
    """最新导入月份（只收合法 YYYY-MM）；无数据返回空串。"""
    opts = sorted({m[0] for m in CrmProductCommission.query.with_entities(
        CrmProductCommission.month).all() if _comm_valid_month(m[0])}, reverse=True)
    return opts[0] if opts else ''


# ---- 月度备份：自动归档 + 覆盖保护（2026-11-02）----
_COMM_BACKUP_CHECK = {'at': None}       # 进程内节流：页面访问时顺带检查，10 分钟内不重复扫描
# 备份快照字段（与 CrmCommCoefBackup 列一一对应）
_COMM_BACKUP_SNAP_FIELDS = ('base_ratio', 'sales_ratio', 'operate_ratio', 'team_base_ratio',
                            'ryc_base_ratio', 'ryc_ratio_y2', 'ryc_ratio_y3',
                            'ryc_ratio_y4', 'ryc_ratio_y5', 'ryc_hide_lt1',
                            'round_rule', 'net_ratio')
# 备份里的计算结果汇总字段
_COMM_BACKUP_SUM_FIELDS = ('row_count', 'base_pct_total', 'sales_pct_total',
                           'operate_pct_total', 'net_pct_total')


def _comm_backup_calc(month, channel_type, cfg):
    """算某渠道某月的「可成功生成销售费用」台账：返回 (行数, 汇总 dict)。

    判定口径：系数设置完成（销售净费比例 > 0）且该月该渠道存在产品行；
    否则返回 (0, {}) 不做归档 —— 避免把「系数没设完、算不出费用」的空壳存成备份。
    """
    if not month or cfg is None or (_comm_net_ratio_of(cfg) or 0) <= 0:
        return 0, {}
    rows, _cfgs = _comm_build_rows(channel=channel_type, month=month)
    if not rows:
        return 0, {}
    return len(rows), {
        'base_pct_total': round(sum((r['base'] or 0) for r in rows), 4),
        'sales_pct_total': round(sum((r['sales'] or 0) for r in rows), 4),
        'operate_pct_total': round(sum((r['op'] or 0) for r in rows), 4),
        'net_pct_total': round(sum((r['net'] or 0) for r in rows), 4),
    }


def _comm_backup_as_dict(rec):
    """备份记录 → dict（用于 prev_snapshot 留档）。"""
    d = {'month': rec.month, 'channel_type': rec.channel_type, 'saved_at': rec.updated_at}
    for f in _COMM_BACKUP_SNAP_FIELDS + _COMM_BACKUP_SUM_FIELDS:
        d[f] = getattr(rec, f, None)
    return d


def _comm_backup_save(month, channel_type, source='auto', overwrite=False, note=''):
    """生成 / 覆盖某月某渠道的系数备份。返回 created / updated / skip_exist / skip_nodata。

    保护机制：已存在备份且 overwrite=False → 返回 skip_exist，绝不静默覆盖；
    overwrite=True（用户二次确认后）才覆盖，覆盖前的快照写进 prev_snapshot 并累计覆盖次数。
    """
    month = (month or '').strip()
    if not _comm_valid_month(month):
        return 'skip_nodata'
    cfg = _comm_coef_of(channel_type)
    row_count, sums = _comm_backup_calc(month, channel_type, cfg)
    if not row_count:
        return 'skip_nodata'
    rec = CrmCommCoefBackup.query.filter_by(month=month, channel_type=channel_type).first()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    if rec is not None and not overwrite:
        return 'skip_exist'
    is_new = rec is None
    if is_new:
        rec = CrmCommCoefBackup(month=month, channel_type=channel_type, created_at=now)
        db.session.add(rec)
    else:
        rec.prev_snapshot = json.dumps(_comm_backup_as_dict(rec), ensure_ascii=False)
        rec.override_count = (rec.override_count or 0) + 1
        rec.last_override_at = now
    for f in _COMM_BACKUP_SNAP_FIELDS:
        setattr(rec, f, getattr(cfg, f, None))
    rec.net_ratio = _comm_net_ratio_of(cfg)
    rec.row_count = row_count
    for k, v in sums.items():
        setattr(rec, k, v)
    rec.source = source
    rec.updated_at = now
    if note:
        rec.note = note
    return 'created' if is_new else 'updated'


def _comm_backup_latest(channel_type):
    """该渠道最近一份备份（用于「是否已归档 / 受保护」判断）。"""
    return (CrmCommCoefBackup.query.filter_by(channel_type=channel_type)
            .order_by(CrmCommCoefBackup.month.desc()).first())


def _comm_backup_auto(force=False):
    """月度自动归档（惰性触发）：对「有产品数据、尚未备份」的月份 × 渠道各存一份。

    没有调度器也照样每月留痕：任何一次访问本页面都会补归档（停用几个月后回来也能补齐），
    进程内 10 分钟节流；force=True 跳过节流（手动备份按钮用）。返回新增条数。
    """
    now = datetime.now()
    last = _COMM_BACKUP_CHECK.get('at')
    if not force and last is not None and (now - last).total_seconds() < 600:
        return 0
    _COMM_BACKUP_CHECK['at'] = now
    months = sorted({m[0] for m in CrmProductCommission.query.with_entities(
        CrmProductCommission.month).all() if _comm_valid_month(m[0])}, reverse=True)
    created = 0
    for m in months[:12]:
        for ct, _cn in COMM_CHANNELS:
            if CrmCommCoefBackup.query.filter_by(month=m, channel_type=ct).first():
                continue
            if _comm_backup_save(m, ct, source='auto') == 'created':
                created += 1
    if created:
        db.session.commit()
    return created


@bp.route('/crm/commission-coefficient')
@login_required
@permission_required('crm.view')
def commission_coefficient():
    """产品佣金系数：导入区 + 系数设置区 + 结果清单（三层口径 + 首年/续期明细）。"""
    channel, region, level, insurer, pay_period, product, month = _comm_query_args()
    # 月度选项（去重倒序），默认取最新月份；空则全量（只收合法月份 YYYY-MM）
    month_options = sorted({m[0] for m in CrmProductCommission.query.with_entities(
        CrmProductCommission.month).all() if _comm_valid_month(m[0])}, reverse=True)
    if not month:
        month = month_options[0] if month_options else ''
    month_filter = month if month and month != 'all' else None
    rows, cfgs = _comm_build_rows(
        None if channel == 'all' else channel, region, month_filter,
        None if insurer == 'all' else insurer,
        None if pay_period == 'all' else pay_period,
        product or None)
    # 计数按所选月份（或全部）；排除历史遗留的说明/活动行（无产品名）
    base_q = CrmProductCommission.query.filter(
        CrmProductCommission.product.isnot(None), CrmProductCommission.product != '')
    if month_filter:
        base_q = base_q.filter_by(month=month_filter)
    total = base_q.count()
    n_int = base_q.filter_by(is_internet=1).count()
    n_off = total - n_int
    imported = CrmProductCommission.query.order_by(CrmProductCommission.id.desc()).first()
    cfgs_both = {ct: _comm_coef_of(ct) for ct in ('offline', 'internet')}
    # 销售净费比例（基础+销售+经营，随渠道自动测算）：线下默认 89% / 互联网默认 85%
    net_ratio_both = {ct: _comm_net_ratio_of(cfgs_both[ct]) for ct in ('offline', 'internet')}
    region_opts = _comm_region_options()
    insurer_opts = sorted({m[0] for m in CrmProductCommission.query
                           .filter(CrmProductCommission.insurer.isnot(None),
                                   CrmProductCommission.insurer != '',
                                   CrmProductCommission.product.isnot(None),
                                   CrmProductCommission.product != '')
                           .with_entities(CrmProductCommission.insurer).all()
                           if _comm_valid_insurer(m[0])})
    pay_period_opts = sorted({m[0] for m in CrmProductCommission.query.with_entities(
        CrmProductCommission.pay_period).all() if m[0]})
    # 产品选项随保司联动：选中保司时只列出该保司产品，避免手工输入
    if insurer != 'all':
        product_opts = sorted({m[0] for m in CrmProductCommission.query.filter_by(insurer=insurer)
                               .with_entities(CrmProductCommission.product).all() if m[0]})
    else:
        product_opts = sorted({m[0] for m in CrmProductCommission.query.with_entities(
            CrmProductCommission.product).all() if m[0]})
    # 月度备份：访问时惰性补归档（每月自动留痕），并取出历史备份列表
    _comm_backup_auto()
    backups = (CrmCommCoefBackup.query
               .order_by(CrmCommCoefBackup.month.desc(), CrmCommCoefBackup.channel_type).all())
    backup_lock = {ct: _comm_backup_latest(ct) for ct, _n in COMM_CHANNELS}
    backup_lock_months = {ct: (rec.month if rec is not None else '')
                          for ct, rec in backup_lock.items()}
    # 覆盖留档的可读摘要（prev_snapshot 是 JSON，页面 title 里直接展示不友好，转成一句话）
    for b in backups:
        b.prev_summary = ''
        if b.prev_snapshot:
            try:
                d = json.loads(b.prev_snapshot)
                b.prev_summary = ('基础 %g%% / 销售 %g%% / 经营 %g%% → 净费 %g%%；取整 %s；RYC %g/%g/%g/%g'
                                  % (d.get('base_ratio') or 0, d.get('sales_ratio') or 0,
                                     d.get('operate_ratio') or 0, d.get('net_ratio') or 0,
                                     {'1': '①保留2位', '2': '②取整', '3': '③减1%后保2位'}
                                     .get(d.get('round_rule') or '3', d.get('round_rule') or '-'),
                                     d.get('ryc_ratio_y2') or 0, d.get('ryc_ratio_y3') or 0,
                                     d.get('ryc_ratio_y4') or 0, d.get('ryc_ratio_y5') or 0))
            except (ValueError, TypeError):
                b.prev_summary = ''
    cur_month = datetime.now().strftime('%Y-%m')
    # 局部刷新：仅返回结果清单片段（AJAX），不整页渲染
    if request.args.get('partial') == '1':
        return render_template('crm/_commission_coefficient_result.html',
                               rows=rows, channel=channel, region=region, level=level,
                               insurer=insurer, pay_period=pay_period, product=product,
                               month=month, month_options=month_options,
                               counts={'total': total, 'internet': n_int, 'offline': n_off},
                               net_ratio_both=net_ratio_both,
                               region_opts=region_opts, insurer_opts=insurer_opts,
                               pay_period_opts=pay_period_opts, product_opts=product_opts,
                               can_import=_can('crm.commission.import'),
                               can_coef=_can('crm.commission.coef'))
    return render_template('crm/commission_coefficient.html',
                           rows=rows, channel=channel, region=region, level=level,
                           insurer=insurer, pay_period=pay_period, product=product,
                           month=month, month_options=month_options, cur_month=cur_month,
                           counts={'total': total, 'internet': n_int, 'offline': n_off},
                           imported_at=(imported.imported_at if imported else ''),
                           cfgs=cfgs_both, net_ratio_both=net_ratio_both, items=COMM_ITEMS, channels=COMM_CHANNELS,
                           round_rules=ROUND_RULES, can_import=_can('crm.commission.import'),
                           can_coef=_can('crm.commission.coef'),
                           can_manage=_can('crm.commission.import') or _can('crm.commission.coef'),
                           region_opts=region_opts, insurer_opts=insurer_opts,
                           pay_period_opts=pay_period_opts, product_opts=product_opts,
                           backups=backups, backup_lock=backup_lock,
                           backup_lock_months=backup_lock_months)


@bp.route('/crm/commission-coefficient/export')
@login_required
@permission_required('crm.view')
def commission_coefficient_export():
    """一键导出：按当前所选组织层面导出对应列（与页面所见完全一致）。

    ⚠️ 列结构严格跟随 level，各层面不混列：
      · 全部       = 产品信息 10 + 首年明细 3 + RYC采购成本 4 + 股东 2 + 子公司 3 + 团队长 1 + RYC 4
      · 股东层面   = 产品信息 10 + 首年明细 3 + RYC采购成本 4 + 股东 2 + RYC 4
      · 子公司层面 = 产品信息 10 + 子公司 3 + RYC 4
      · 团队长层面 = 产品信息 10 + 团队长 1 + RYC 4
    RYC采购成本（第二年~第五年采购，原始百分点，不经系数折算）只在「全部 / 股东层面」导出，与最右侧「RYC续年度佣金」区分。
    数值均为百分点数值（保留 2 位、第三位起舍去；RYC续年度佣金列保留 1 位小数、且开启「<1%自动置空」时该列 0 值留空），口径与页面一致。
    """
    channel, region, level, insurer, pay_period, product, month = _comm_query_args()
    if not month:
        month = _comm_latest_month()          # 与页面一致：未指定月份时取最新导入月份
    month_filter = month if month and month != 'all' else None
    rows, _cfgs = _comm_build_rows(
        None if channel == 'all' else channel, region, month_filter,
        None if insurer == 'all' else insurer,
        None if pay_period == 'all' else pay_period,
        product or None)

    heads = ['保险公司', '险种名称', '渠道', '类型', '出单平台', '平台名称', '是否需要补录', '保障期限', '缴费期', '销售分公司']
    groups = [('产品信息', 10)]
    pct_cols = {}          # 列号(1起) → 小数位数；Excel 按百分比格式显示，与页面一致
    col = 10
    if level in ('all', 'shareholder'):
        heads += ['FYC', '首年方案', '首佣合计']
        groups.append(('首年明细（采购成本 %）', 3))
        col += 1; pct_cols[col] = 2
        col += 1; pct_cols[col] = 2
        col += 1; pct_cols[col] = 2
    if level in ('all', 'shareholder'):
        heads += ['RYC2采购', 'RYC3采购', 'RYC4采购', 'RYC5采购']
        groups.append(('RYC采购成本（%）', 4))
        for _ in range(4):
            col += 1; pct_cols[col] = 2
    if level in ('all', 'shareholder'):
        heads += ['签约费用', '销售净费']
        groups.append(('股东层面', 2))
        col += 1; pct_cols[col] = 2
        col += 1; pct_cols[col] = 2
    if level in ('all', 'subsidiary'):
        heads += ['FYC首佣', '销售费用', '经营费用', '销售费用合计']
        groups.append(('子公司层面', 4))
        col += 1; pct_cols[col] = 2     # FYC首佣：值截断1位，显示2位（威哥要求）
        col += 1; pct_cols[col] = 2     # 销售费用：同上
        col += 1; pct_cols[col] = 2     # 经营费用：同上
        col += 1; pct_cols[col] = 2     # 销售费用合计：2 位小数
    if level in ('all', 'team'):
        heads += ['FYC首佣']
        groups.append(('团队长层面', 1))
        col += 1; pct_cols[col] = 2     # 团队长FYC首佣：值截断1位，显示2位
    heads += ['RYC2佣金', 'RYC3佣金', 'RYC4佣金', 'RYC5佣金']
    groups.append(('RYC续年度佣金', 4))
    for _ in range(4):
        col += 1; pct_cols[col] = 1     # RYC续年度佣金显示 1 位小数（与页面一致）

    data = []
    for r in rows:
        # 销售分公司与页面同逻辑：选了具体省份显示所选省份，否则显示产品自身投保区域
        reg_disp = region if (region and region not in ('all', '全国')) else r['region']
        line = [r['insurer'], r['product'], r['channel_name'],
                '互联网' if r['is_internet'] else '线下',
                r['issue_platform'], r['platform_name'], r['need_supplement'],
                r['coverage_term'], r['pay_period'], reg_disp]
        if level in ('all', 'shareholder'):
            line += [_comm_pct_num(r['fyc']), _comm_pct_num(r['plan']),
                     _comm_pct_num(r['first_year_comm'])]
        if level in ('all', 'shareholder'):
            line += [_comm_pct_num(r['y2']), _comm_pct_num(r['y3']),
                     _comm_pct_num(r['y4']), _comm_pct_num(r['y5'])]
        if level in ('all', 'shareholder'):
            line += [_comm_pct_num(r['sign_total']), _comm_pct_num(r['net'])]
        if level in ('all', 'subsidiary'):
            # 子公司四项 0 也输出 0.0（页面显示 0.00%，导出保持一致）
            line += [_comm_pct_num(r['base']) or 0.0, _comm_pct_num(r['sales']) or 0.0,
                     _comm_pct_num(r['op']) or 0.0, _comm_pct_num(r['sub_total']) or 0.0]
        if level in ('all', 'team'):
            line += [_comm_pct_num(r['team']) or 0.0]
        line += [_comm_ryc_pct_num(r['ryc_comm']['y2'], r['ryc_hide_lt1']),
                 _comm_ryc_pct_num(r['ryc_comm']['y3'], r['ryc_hide_lt1']),
                 _comm_ryc_pct_num(r['ryc_comm']['y4'], r['ryc_hide_lt1']),
                 _comm_ryc_pct_num(r['ryc_comm']['y5'], r['ryc_hide_lt1'])]
        data.append(line)

    lv_name = {'all': '全部层级', 'shareholder': '股东层面',
               'subsidiary': '子公司层面', 'team': '团队长层面'}.get(level, '全部层级')
    title = '产品佣金系数 · %s（归属月份 %s · 单位 %% · 共 %d 条）' % (lv_name, month or '全部', len(data))
    return _xlsx_response('佣金系数' + lv_name, title, heads, data, groups=groups, pct_cols=pct_cols)


@bp.route('/crm/commission-coefficient/import', methods=['POST'])
@login_required
@permission_required('crm.commission.import')
def commission_coefficient_import():
    """一键导入产品合约（采购成本下发 Excel）。解析 Sheet1，按 E 列区分互联网/线下，按「归属月份」覆盖该月数据（不同月份互不影响）。"""
    f = request.files.get('file')
    if not f or not f.filename:
        flash('请先选择要导入的 Excel 文件')
        return redirect(url_for('crm.commission_coefficient'))
    if not f.filename.lower().endswith(('.xlsx', '.xls')):
        flash('仅支持 .xlsx / .xls 格式的 Excel 文件')
        return redirect(url_for('crm.commission_coefficient'))
    fn = secure_filename(f.filename)
    tmp = os.path.join(tempfile.gettempdir(), 'comm_%s' % fn)
    f.save(tmp)
    try:
        wb = openpyxl.load_workbook(tmp, data_only=True)
        ws = wb['Sheet1'] if 'Sheet1' in wb.sheetnames else wb[wb.sheetnames[0]]
        # 定位表头行（含「互联网产品」那一行的上一行/本行）
        hrow = 1
        for r in range(1, min(ws.max_row, 12) + 1):
            for c in range(1, ws.max_column + 1):
                if '互联网产品' in str(ws.cell(r, c).value or ''):
                    hrow = r
                    break
            else:
                continue
            break
        cmap = {}
        for c in range(1, ws.max_column + 1):
            name = str(ws.cell(hrow, c).value or '').strip().replace('\n', '')
            cmap[name] = c

        def col(*names):
            for n in names:
                if n in cmap:
                    return cmap[n]
            return None

        def g(row, *names):
            c = col(*names)
            return ws.cell(row, c).value if c else None

        def f2(val):
            try:
                return float(str(val).replace(',', '').strip())
            except (ValueError, AttributeError):
                return 0.0

        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        # 归属月份：表单指定 > 文件名识别(YYYY年M月) > 当前月
        # 一律做合法性校验（YYYY-MM，月 01~12），非法值直接丢弃后兜底，避免写入 2099-13 这类脏月份
        month = (request.form.get('month') or '').strip()
        if month and not _comm_valid_month(month):
            month = ''
        if not month:
            m = re.search(r'(\d{4})年(\d{1,2})月', f.filename or '')
            if m:
                month = '%s-%02d' % (m.group(1), int(m.group(2)))
        if not month or not _comm_valid_month(month):
            month = datetime.now().strftime('%Y-%m')
        maps, n_int, n_off, n_skip = [], 0, 0, 0
        for r in range(hrow + 1, ws.max_row + 1):
            insurer = (str(g(r, '保险公司') or '')).strip()
            product = (str(g(r, '险种名称') or '')).strip()
            note = (str(g(r, '备注') or '')).strip()
            # 关键数值（用于自动识别非产品说明行）
            purchase_y1 = f2(g(r, '首年采购'))
            base_comm = f2(g(r, '首年基础佣金'))
            plan = f2(g(r, '首年方案'))
            purchase_y2 = f2(g(r, '第二年采购', '第二年\n采购'))
            purchase_y3 = f2(g(r, '第三年采购', '第三年\n采购'))
            purchase_y4 = f2(g(r, '第四年采购', '第四年\n采购'))
            purchase_y5 = f2(g(r, '第五年采购', '第五年\n采购'))
            vals = [purchase_y1, base_comm, plan, purchase_y2, purchase_y3, purchase_y4, purchase_y5]
            # 自动识别：与产品合约无关的说明/活动行（无公司且无产品，或含活动信息关键字且无数值）→ 跳过
            if (not insurer and not product) or _comm_is_note_row(insurer, product, note, vals):
                n_skip += 1
                continue
            is_int = 1 if str(g(r, '互联网产品') or '').strip() == '是' else 0
            if is_int:
                n_int += 1
            else:
                n_off += 1
            maps.append({
                'insurer': insurer,
                'product': product,
                'channel': (str(g(r, '渠道') or '')).strip(),
                'is_internet': is_int,
                'internet_heavy': (str(g(r, '互险网重疾') or '')).strip(),
                'region': (str(g(r, '投保区域') or '')).strip(),
                'issue_platform': (str(g(r, '出单平台') or '')).strip(),
                'platform_name': (str(g(r, '平台名称') or '')).strip(),
                'need_supplement': (str(g(r, '是否需要补录') or '')).strip(),
                'coverage_term': (str(g(r, '保障期限') or '')).strip(),
                'pay_period': (str(g(r, '缴费期') or '')).strip(),
                'purchase_y1': purchase_y1,
                'base_comm': base_comm,
                'plan': plan,
                'purchase_y2': purchase_y2,
                'purchase_y3': purchase_y3,
                'purchase_y4': purchase_y4,
                'purchase_y5': purchase_y5,
                'note': note,
                'imported_at': now,
                'month': month,
            })
        if not maps:
            flash('未解析到任何产品数据，请确认文件格式（需含「互联网产品」「首年基础佣金」等表头）')
            return redirect(url_for('crm.commission_coefficient'))
        # 按月覆盖：清同月旧数据 + 批量写入（不同月份互不影响）
        CrmProductCommission.query.filter_by(month=month).delete()
        db.session.bulk_insert_mappings(CrmProductCommission, maps)
        db.session.commit()
        # 导入即留痕：数据落地后立即触发一次月度归档（跳过节流），保证本月自动备份不滞后
        archived_before = ['%s' % cn for ct, cn in COMM_CHANNELS
                           if CrmCommCoefBackup.query.filter_by(
                               month=month, channel_type=ct).first() is not None]
        _comm_backup_auto(force=True)
        msg = ('成功导入 %d 条产品合约（归属月份 %s，互联网 %d 条 / 线下 %d 条），'
               '自动跳过非产品说明行 %d 条，导入时间 %s'
               % (len(maps), month, n_int, n_off, n_skip, now))
        if archived_before:
            msg += ('；该月此前已有备份（%s）受保护未覆盖，如需把本次数据同步进备份，'
                    '请点「↻ 覆盖本月备份」' % '、'.join(archived_before))
        flash(msg)
    except Exception as e:
        db.session.rollback()
        flash('导入失败：%s' % e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return redirect(url_for('crm.commission_coefficient'))


@bp.route('/crm/commission-coefficient/config', methods=['GET', 'POST'])
@login_required
@permission_required('crm.commission.coef')
def commission_coefficient_config():
    """系数设置：线下 / 互联网 各一套（各自独立表单）。
    ⚠️ 只保存「本次实际提交」的那个板块，另一板块完全不动 —— 两个板块互不影响。"""
    if request.method == 'POST':
        # ---- 覆盖保护：已归档（已生成月度备份）的渠道不允许静默修改 ----
        # 必须由用户在二次确认弹窗勾选「我已确认覆盖已备份系数」，表单带 overwrite_backup=1 才放行。
        locked = []
        for ct, ct_name in COMM_CHANNELS:
            if ('%s_base_ratio' % ct) not in request.form:
                continue
            latest = _comm_backup_latest(ct)
            if latest is not None and request.form.get('overwrite_backup') != '1':
                locked.append('%s（已归档至 %s）' % (ct_name, latest.month))
        if locked:
            flash('已拦截保存：%s 的系数已有月度备份，处于保护状态。'
                  '如确需调整并覆盖备份，请重新打开系数设置，在确认弹窗勾选「我已确认覆盖已备份系数」后再提交。'
                  % '；'.join(locked))
            return redirect(url_for('crm.commission_coefficient'))

        def _setnum(cfg, attr, key, default=0.0):
            """字段没提交就不动，避免用默认值把另一板块的已有配置冲掉。"""
            if key in request.form:
                setattr(cfg, attr, _to_num(request.form.get(key), default))

        saved = []
        for ct, ct_name in COMM_CHANNELS:
            # 只处理提交了本板块字段的表单（每个板块表单都带 channel_type 与 base_ratio）
            if ('%s_base_ratio' % ct) not in request.form:
                continue
            cfg = CrmCommCoefConfig.query.filter_by(channel_type=ct).first()
            if cfg is None:
                cfg = CrmCommCoefConfig(channel_type=ct)
                db.session.add(cfg)
            _setnum(cfg, 'base_ratio', '%s_base_ratio' % ct, 80)
            _setnum(cfg, 'operate_ratio', '%s_operate_ratio' % ct, 4)
            _setnum(cfg, 'sales_ratio', '%s_sales_ratio' % ct, 5)
            _setnum(cfg, 'ryc_base_ratio', '%s_ryc_base_ratio' % ct, 0)
            for yn in ('y2', 'y3', 'y4', 'y5'):
                _setnum(cfg, 'ryc_ratio_' + yn, '%s_ryc_ratio_%s' % (ct, yn), 0)
            cfg.team_base_ratio = cfg.base_ratio  # 团队长基础佣金比例 = 基础佣金比例（统一）
            # RYC续年度佣金<1%自动置空（线下/互联网统一规则）：任一板块提交即同步两套配置
            cfg.ryc_hide_lt1 = (request.form.get('ryc_hide_lt1') == '1')
            # 取整规则已写死（威哥 2026-11-05）：统一「减1%后数据取整（直接舍去）」，忽略表单传值
            cfg.round_rule = COMM_ROUND_RULE
            cfg.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
            cfg.note = _f('%s_note' % ct)
            saved.append(ct_name)
        db.session.commit()
        if saved:
            # 确认覆盖已归档数据：同步刷新对应月份备份（覆盖前旧快照留档，覆盖次数 +1）
            refreshed, skipped = [], []
            if request.form.get('overwrite_backup') == '1':
                for ct, ct_name in COMM_CHANNELS:
                    if ('%s_base_ratio' % ct) not in request.form:
                        continue
                    latest = _comm_backup_latest(ct)
                    m = latest.month if latest is not None else _comm_latest_month()
                    if _comm_backup_save(m, ct, source='manual', overwrite=True,
                                         note='系数调整后覆盖备份') in ('created', 'updated'):
                        refreshed.append('%s %s' % (ct_name, m))
                    else:
                        skipped.append(ct_name)
                db.session.commit()
            msg = '已保存「%s」系数（另一板块配置保持原值不变）' % ' / '.join(saved)
            if refreshed:
                msg += '；已覆盖对应月度备份：%s（旧快照已留档，覆盖次数 +1）' % '、'.join(refreshed)
            if skipped:
                msg += '；%s 当期无可归档数据，备份未改动' % '、'.join(skipped)
            flash(msg)
        else:
            flash('未检测到任何系数提交，配置未做修改')
        return redirect(url_for('crm.commission_coefficient'))
    # GET 直接回到主页面（配置表单内嵌在主页面）
    return redirect(url_for('crm.commission_coefficient'))


@bp.route('/crm/commission-coefficient/init', methods=['POST'])
@login_required
@permission_required('crm.commission.coef')
def commission_coefficient_init():
    """一键生成默认系数：按威哥日常常规比例写入（线下 基础80/销售5/经营4；互联网 基础80/销售0/经营5；取整规则默认「四舍五入后减1%取整」）。"""
    # 保护机制：已归档的渠道不允许「一键初始化」整体覆盖，必须走系数设置弹窗的覆盖确认流程
    archived = [ct_name for ct, ct_name in COMM_CHANNELS if _comm_backup_latest(ct) is not None]
    if archived:
        flash('系数已归档保护：%s 已有月度备份，无法「一键初始化」整体覆盖。'
              '如需调整，请打开「系数设置」弹窗修改，并在确认弹窗勾选「我已确认覆盖已备份系数」后保存。'
              % '、'.join(archived))
        return redirect(url_for('crm.commission_coefficient'))
    defaults = {
        'offline': {'base_ratio': 80.0, 'operate_ratio': 4.0, 'sales_ratio': 5.0,
                    'ryc_base_ratio': 80.0, 'ryc': {'y2': 80.0, 'y3': 80.0, 'y4': 80.0, 'y5': 80.0},
                    'round_rule': COMM_ROUND_RULE, 'team_base_ratio': 80.0},
        'internet': {'base_ratio': 80.0, 'operate_ratio': 5.0, 'sales_ratio': 0.0,
                     'ryc_base_ratio': 80.0, 'ryc': {'y2': 80.0, 'y3': 80.0, 'y4': 80.0, 'y5': 80.0},
                     'round_rule': COMM_ROUND_RULE, 'team_base_ratio': 80.0},
    }
    for ct, d in defaults.items():
        cfg = CrmCommCoefConfig.query.filter_by(channel_type=ct).first()
        if cfg is None:
            cfg = CrmCommCoefConfig(channel_type=ct)
            db.session.add(cfg)
        cfg.base_ratio = d['base_ratio']
        cfg.operate_ratio = d['operate_ratio']
        cfg.sales_ratio = d['sales_ratio']
        cfg.ryc_base_ratio = d['ryc_base_ratio']
        for yn, val in d['ryc'].items():
            setattr(cfg, 'ryc_ratio_' + yn, val)
        cfg.round_rule = d['round_rule']
        cfg.team_base_ratio = d['team_base_ratio']
        cfg.note = '初始化默认比例'
        cfg.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    db.session.commit()
    flash('已按常规比例一键生成：线下 基础80%/销售5%/经营4% ｜ 互联网 基础80%/销售0%/经营5%（RYC2~5佣金比例默认均＝基础佣金比例，可按年单独调整；取整规则已写死为「减1%后数据取整（直接舍去）」）')
    return redirect(url_for('crm.commission_coefficient'))


@bp.route('/crm/commission-coefficient/backup', methods=['POST'])
@login_required
@permission_required('crm.commission.coef')
def commission_coefficient_backup():
    """立即备份（手动补档）：对指定归属月份（默认最新月）按渠道各归档一份。

    保护机制：已归档的月份默认「不覆盖」（skip_exist）；只有显式 overwrite=1
    （页面「覆盖本月备份」按钮，带二次确认）才覆盖，且旧快照留档。
    """
    month = (_f('month') or '').strip() or _comm_latest_month()
    overwrite = _f('overwrite') == '1'
    if not _comm_valid_month(month):
        flash('请先导入产品合约数据（归属月份需为 YYYY-MM）后再备份')
        return redirect(url_for('crm.commission_coefficient'))
    bucket = {'created': [], 'updated': [], 'skip_exist': [], 'skip_nodata': []}
    for ct, ct_name in COMM_CHANNELS:
        r = _comm_backup_save(month, ct, source='manual', overwrite=overwrite,
                              note=('手动覆盖备份' if overwrite else '手动备份'))
        bucket.get(r, bucket['skip_nodata']).append(ct_name)
    db.session.commit()
    parts = []
    if bucket['created']:
        parts.append('已备份 %s' % '、'.join(bucket['created']))
    if bucket['updated']:
        parts.append('已覆盖 %s（旧快照已留档）' % '、'.join(bucket['updated']))
    if bucket['skip_exist']:
        parts.append('%s 此前已归档，本次未覆盖（如需覆盖请点「覆盖本月备份」）' % '、'.join(bucket['skip_exist']))
    if bucket['skip_nodata']:
        parts.append('%s 当期无可归档数据（无产品数据，或系数未设置完成）' % '、'.join(bucket['skip_nodata']))
    flash('【%s 月度备份】%s' % (month, '；'.join(parts) or '无变化'))
    return redirect(url_for('crm.commission_coefficient'))

