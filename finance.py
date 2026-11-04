# finance.py
# FMS · 财务管理系统
#   增量 #30：财务地基（账套设置 + 会计科目表）
#   增量 #31：期初建账（期初余额录入 + 试算平衡 + 建账闭环）
#   增量 #32：记账凭证录入（多分录 + 借贷平衡校验 + 草稿保存 + 凭证列表/详情）
#   增量 #33：凭证流转（审核 / 记账 / 作废 / 红字冲销 + 制单≠审核 + 已记账锁定）
#   增量 #34：账簿查询（科目余额表 / 明细账 / 现金银行日记账，只取已记账凭证）
#   增量 #41：费用结算确认函（Excel 导入 → 防呆校验 → 批量出 PDF → 台账 → 签回归档）
#   增量 #35：报表中心（资产负债表 / 利润表 / 现金流量表简版 + 取数公式引擎 + 自定义 + Excel 导出）
#   增量 #36：期末结转与结账（结转损益到本年利润 + 期间锁定 + 结账/反结账）
# 增量 #37：发票台账（进项/销项 + 认证抵扣 + 增值税测算）。
# 后续增量在这块地基上依次做：自动记账（发票/佣金/工资/报销/奖金池自动生成凭证）。
# 至此「期初建账 → 录凭证 → 审核记账 → 账簿 → 报表 → 期末结转结账 → 发票台账」主线已通。
# 技术栈沿用系统现有方案（Flask + SQLAlchemy + Jinja2），不另起炉灶。
import ast
import io
import json
import os
import re
import shutil
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from flask import (Blueprint, render_template, request, redirect, url_for, flash,
                   session, send_file, abort, current_app)
from models import (db, FinAccount, FinConfig, FinOpening, FinVoucher, FinVoucherEntry,
                    FinReportTemplate, FinPeriod, FinInvoice, fin_level, cn_month,
                    ConfirmBatch, ConfirmLetter, ConfirmItem, ConfirmAccount,
                    Commission, Salary, Bonus, BonusAllocation, Approval, FinAuditLog, FinBackup,
                    FinBankAccount, FinBankStatement, FinCashOrder,
                    ExpenseReimburse, EXPENSE_CATEGORY_ACCOUNT)
from app import login_required, permission_required, log_audit
import confirm_doc as cd                      # 确认函的解析 / 校验 / PDF 生成（纯数据逻辑，独立成模块）

bp = Blueprint('fin', __name__)

# 页面下拉/勾选项（与 models 里的取值一一对应）
CATEGORY_OPTIONS = [('asset', '资产'), ('liability', '负债'), ('equity', '所有者权益'),
                    ('cost', '成本'), ('profit', '损益')]
DIRECTION_OPTIONS = [('debit', '借'), ('credit', '贷')]
AUX_OPTIONS = [('customer', '客户'), ('employee', '员工'), ('dept', '部门'), ('project', '项目')]
AUX_NAME = dict(AUX_OPTIONS)


def _cfg():
    """取账套配置。单账套：表里固定只有一条（code='default'），没有就现场建一条默认的。"""
    c = FinConfig.query.filter_by(code='default').first()
    if not c:
        c = FinConfig(code='default', company_name='广东海盈通保智能科技有限公司',
                      standard='small', taxpayer='general',
                      start_period=datetime.now().strftime('%Y-%m'),
                      currency='CNY', voucher_types='收,付,转',
                      agent_company='海腾保险代理有限公司')
        db.session.add(c)
        db.session.commit()
    return c


# ===================== FMS · 账务管理（hub 页） =====================

@bp.route('/fin/acct-hub', methods=['GET'])
@login_required
def acct_hub():
    """账务管理 hub：账套 / 科目 / 期初 / 凭证 / 流转 / 账簿（结账随后续增量）。"""
    stats = {
        'total': FinAccount.query.count(),
        'top': FinAccount.query.filter_by(level=1).count(),
        'leaf': FinAccount.query.filter_by(is_leaf=True).count(),
        'disabled': FinAccount.query.filter_by(enabled=False).count(),
        # 流转待办：财务主管一进这个页就该看见「有几张等着我审、几张等着我记」
        'vch_pending_audit': FinVoucher.query.filter_by(status='draft').count(),
        'vch_pending_post': FinVoucher.query.filter_by(status='audited').count(),
        'vch_posted': FinVoucher.query.filter_by(status='posted').count(),
        # 期末结转（#36）：已结账期间数，账务管理页据此显示「已结账 N 期」
        'closed_periods': FinPeriod.query.filter_by(status='closed').count(),
    }
    return render_template('fin/acct_hub.html', stats=stats, cfg=_cfg())


# ===================== FMS · 会计科目（增删改查 + 启停） =====================

@bp.route('/fin/accounts', methods=['GET'])
@login_required
def accounts():
    """会计科目表：按编码排序（编码顺序天然就是 父级→子级 的顺序），支持类别/状态筛选。"""
    cat = request.args.get('cat', '').strip()
    state = request.args.get('state', '').strip()   # on 只看启用 / off 只看停用
    q = FinAccount.query
    if cat:
        q = q.filter_by(category=cat)
    if state == 'on':
        q = q.filter_by(enabled=True)
    elif state == 'off':
        q = q.filter_by(enabled=False)
    items = q.order_by(FinAccount.code).all()
    # 上级科目下拉：列出启用中的科目（挂到某个科目下，该科目就自动变成"非明细"）
    parents = FinAccount.query.filter_by(enabled=True).order_by(FinAccount.code).all()
    return render_template('fin/account_list.html', items=items, parents=parents,
                           cat=cat, state=state,
                           category_options=CATEGORY_OPTIONS, direction_options=DIRECTION_OPTIONS,
                           aux_options=AUX_OPTIONS, aux_name=AUX_NAME)


@bp.route('/fin/accounts/new', methods=['POST'])
@login_required
@permission_required('fin.account.manage')
def account_new():
    """新增会计科目：编码唯一；挂了上级科目时，上级自动变为「非明细科目」。"""
    code = request.form.get('code', '').strip()
    name = request.form.get('name', '').strip()
    if not code or not name:
        flash('科目编码与科目名称不能为空')
        return redirect(url_for('fin.accounts'))
    if FinAccount.query.filter_by(code=code).first():
        flash('科目编码 %s 已存在，不能重复' % code)
        return redirect(url_for('fin.accounts'))
    parent_code = request.form.get('parent_code', '').strip()
    if parent_code and parent_code == code:
        flash('上级科目不能是自己')
        return redirect(url_for('fin.accounts'))
    acc = FinAccount(
        code=code, name=name,
        category=request.form.get('category', 'asset').strip() or 'asset',
        direction=request.form.get('direction', 'debit').strip() or 'debit',
        parent_code=parent_code,
        level=fin_level(code),
        is_leaf=True, enabled=True,
        aux=','.join(request.form.getlist('aux')),      # 多选辅助核算拼成 customer,dept
        note=request.form.get('note', '').strip(),
    )
    db.session.add(acc)
    warn = ''
    if parent_code:
        p = FinAccount.query.filter_by(code=parent_code).first()
        if p:
            p.is_leaf = False       # 有了下级，上级就不再是明细科目（不能直接做分录）
            # 上级转为汇总科目后自己不能再挂余额：已录的期初要清掉并提示，余额改录到明细科目上
            po = FinOpening.query.filter_by(account_code=p.code).first()
            if po:
                db.session.delete(po)
                warn = '；上级科目「%s %s」已转为汇总科目，其原期初余额已清除，请把该余额录到明细科目上' % (p.code, p.name)
    db.session.commit()
    flash('科目「%s %s」已新增%s' % (code, name, warn))
    return redirect(url_for('fin.accounts'))


@bp.route('/fin/accounts/<int:aid>/edit', methods=['POST'])
@login_required
@permission_required('fin.account.manage')
def account_edit(aid):
    """编辑科目：名称 / 类别 / 余额方向 / 辅助核算 / 备注。
    科目编码是所有数据的钥匙，一期不允许改编码（避免历史数据对不上）。"""
    acc = FinAccount.query.get_or_404(aid)
    acc.name = request.form.get('name', '').strip() or acc.name
    cat = request.form.get('category', '').strip()
    if cat in FinAccount.CATEGORY_CN:
        acc.category = cat
    direction = request.form.get('direction', '').strip()
    if direction in FinAccount.DIRECTION_CN:
        acc.direction = direction
    acc.aux = ','.join(request.form.getlist('aux'))
    acc.note = request.form.get('note', '').strip()
    db.session.commit()
    flash('科目「%s」已更新' % acc.code)
    return redirect(url_for('fin.accounts'))


@bp.route('/fin/accounts/<int:aid>/toggle', methods=['POST'])
@login_required
@permission_required('fin.account.manage')
def account_toggle(aid):
    """启用 / 停用科目：停用只影响"能不能再选它做新分录"，历史数据照常保留。"""
    acc = FinAccount.query.get_or_404(aid)
    acc.enabled = not acc.enabled
    db.session.commit()
    flash('科目「%s %s」已%s' % (acc.code, acc.name, '启用' if acc.enabled else '停用'))
    return redirect(url_for('fin.accounts'))


@bp.route('/fin/accounts/<int:aid>/delete', methods=['POST'])
@login_required
@permission_required('fin.account.manage')
def account_delete(aid):
    """删除科目：下有明细科目的父科目不允许直接删（先处理下级）。"""
    acc = FinAccount.query.get_or_404(aid)
    if FinAccount.query.filter_by(parent_code=acc.code).first():
        flash('科目「%s」下还有明细科目，请先删除或用上级科目下拉改挂它的明细科目' % acc.code)
        return redirect(url_for('fin.accounts'))
    db.session.delete(acc)
    db.session.commit()
    flash('科目「%s %s」已删除' % (acc.code, acc.name))
    return redirect(url_for('fin.accounts'))


# ===================== FMS · 账套设置 =====================

@bp.route('/fin/config', methods=['GET', 'POST'])
@login_required
@permission_required('fin.config.manage')
def config():
    """账套设置：核算主体、会计制度、纳税人身份、启用期间、本位币、凭证字别。"""
    c = _cfg()
    if request.method == 'POST':
        c.company_name = request.form.get('company_name', '').strip() or c.company_name
        c.standard = request.form.get('standard', '').strip() or c.standard
        c.taxpayer = request.form.get('taxpayer', '').strip() or c.taxpayer
        c.start_period = request.form.get('start_period', '').strip() or c.start_period
        c.currency = request.form.get('currency', '').strip() or c.currency
        c.voucher_types = request.form.get('voucher_types', '').strip() or c.voucher_types
        c.agent_company = request.form.get('agent_company', '').strip()
        c.note = request.form.get('note', '').strip()
        c.updated_at = datetime.utcnow()
        db.session.commit()
        flash('账套设置已保存')
        return redirect(url_for('fin.config'))
    return render_template('fin/config.html', cfg=c)


# ===================== FMS · 期初建账（期初余额录入 + 试算平衡，增量 #31） =====================
# 业务背景：启用账套前，要把各科目在启用期间的期初余额搬到系统里。
# 规则（会计通用，也是本模块的硬约束）：
#   ① 只有「明细科目」能录期初——汇总科目的余额由下级明细自动汇总；
#   ② 每个科目的期初余额只能挂在一个方向（借方或贷方）；
#   ③ 全部科目借方合计必须等于贷方合计（试算平衡），平了才允许确认建账；
#   ④ 建账后才可以录凭证（#32 会据此校验）。

def _fmt(v):
    """金额显示：千分位 + 两位小数（用于提示文案与页面显示）。"""
    return '{:,.2f}'.format(round(v or 0, 2))


def _to_amount(raw):
    """解析金额输入：容忍千分位逗号与空格；空 = 0；非法或负数返回 None（由调用方给提示）。
    金额统一按两位小数（元角分）落库，避免试算平衡出现分位误差。"""
    s = (raw or '').strip().replace(',', '').replace('，', '').replace(' ', '')
    if not s:
        return 0.0
    try:
        v = float(Decimal(s))
    except (InvalidOperation, ValueError):
        return None
    if v < 0:
        return None
    return round(v, 2)


def _opening_rows():
    """期初录入页的数据行：所有明细科目（含已停用，历史余额要保留）+ 已录入的期初值。"""
    accs = FinAccount.query.filter_by(is_leaf=True).order_by(FinAccount.code).all()
    om = {o.account_code: o for o in FinOpening.query.all()}
    rows = []
    for a in accs:
        o = om.get(a.code)
        d = (o.debit if o else 0) or 0
        c = (o.credit if o else 0) or 0
        rows.append({
            'acc': a, 'op': o, 'd': d, 'c': c,
            # 方向异常：录入方向与科目余额方向相反（如资产类录成贷方）——只提示，不阻止保存
            'abnormal': (d > 0 and a.direction == 'credit') or (c > 0 and a.direction == 'debit'),
        })
    return rows


def _opening_summary():
    """试算平衡汇总：期初借/贷合计、差额、是否平衡，以及本年累计发生额合计。
    只统计「当前仍是明细科目」的期初行——科目若已转为汇总科目，那行不再参与平账。"""
    leaf = {a.code for a in FinAccount.query.filter_by(is_leaf=True).all()}
    rows = [o for o in FinOpening.query.all() if o.account_code in leaf]
    d = round(sum((o.debit or 0) for o in rows), 2)
    c = round(sum((o.credit or 0) for o in rows), 2)
    yd = round(sum((o.ytd_debit or 0) for o in rows), 2)
    yc = round(sum((o.ytd_credit or 0) for o in rows), 2)
    return {'count': len(rows), 'debit': d, 'credit': c,
            'diff': round(d - c, 2), 'balanced': abs(round(d - c, 2)) < 0.005,
            'ytd_debit': yd, 'ytd_credit': yc, 'ytd_diff': round(yd - yc, 2)}


def _opening_by_category(rows):
    """按科目类别小计（资产/负债/所有者权益/成本/损益），给财务核对用。"""
    order = ['asset', 'liability', 'equity', 'cost', 'profit']
    tot = {k: [0.0, 0.0] for k in order}
    for r in rows:
        k = r['acc'].category if r['acc'].category in tot else 'profit'
        tot[k][0] = round(tot[k][0] + r['d'], 2)
        tot[k][1] = round(tot[k][1] + r['c'], 2)
    return [{'key': k, 'name': FinAccount.CATEGORY_CN.get(k, k),
             'debit': tot[k][0], 'credit': tot[k][1]} for k in order]


@bp.route('/fin/opening', methods=['GET'])
@login_required
def opening():
    """期初余额录入：上面是试算平衡卡片，下面逐科目录入（只列明细科目）。"""
    only = request.args.get('only') == 'filled'
    cfg = _cfg()
    all_rows = _opening_rows()
    rows = [r for r in all_rows if r['op']] if only else all_rows
    return render_template('fin/opening.html', cfg=cfg, rows=rows, only=only,
                           summary=_opening_summary(),
                           cat_totals=_opening_by_category(all_rows),
                           filled=len([r for r in all_rows if r['op']]),
                           total_acc=len(all_rows))


@bp.route('/fin/opening/save', methods=['POST'])
@login_required
@permission_required('fin.opening.manage')
def opening_save():
    """批量保存期初余额。
    只处理「本次表单里出现过的科目」——这样在"仅看已填"筛选下保存，不会误清空其它科目。"""
    cfg = _cfg()
    who = session.get('username', '')
    saved, cleared, errs = 0, 0, []
    for a in FinAccount.query.filter_by(is_leaf=True).order_by(FinAccount.code).all():
        key_d = 'debit_%s' % a.code
        key_c = 'credit_%s' % a.code
        # 该行的输入框没出现在本次表单里（被筛选掉/未渲染）→ 保持原样不动，避免误清空
        if key_d not in request.form and key_c not in request.form:
            continue
        d = _to_amount(request.form.get(key_d))
        c = _to_amount(request.form.get(key_c))
        yd = _to_amount(request.form.get('ytd_debit_%s' % a.code))
        yc = _to_amount(request.form.get('ytd_credit_%s' % a.code))
        if None in (d, c, yd, yc):
            errs.append('%s %s：金额只能填 0 或正数，最多两位小数' % (a.code, a.name))
            continue
        if d > 0 and c > 0:
            errs.append('%s %s：同一科目的期初余额不能同时挂在借方和贷方' % (a.code, a.name))
            continue
        o = FinOpening.query.filter_by(account_code=a.code).first()
        if d == 0 and c == 0 and yd == 0 and yc == 0:
            if o:                        # 四项全清空 → 删掉这行，避免"已录入科目数"虚高
                db.session.delete(o)
                cleared += 1
            continue
        if not o:
            o = FinOpening(account_code=a.code)
            db.session.add(o)
        o.period = cfg.start_period
        o.account_name, o.category, o.direction = a.name, a.category, a.direction
        o.debit, o.credit, o.ytd_debit, o.ytd_credit = d, c, yd, yc
        o.updated_by = who
        o.updated_at = datetime.utcnow()
        saved += 1
    db.session.commit()
    for e in errs:
        flash(e)
    s = _opening_summary()
    msg = '期初余额已保存：更新 %d 个科目' % saved
    if cleared:
        msg += '，清空 %d 个科目' % cleared
    msg += '；当前借方合计 %s、贷方合计 %s、差额 %s（%s）' % (
        _fmt(s['debit']), _fmt(s['credit']), _fmt(s['diff']),
        '已平衡' if s['balanced'] else '尚未平衡')
    flash(msg)
    return redirect(url_for('fin.opening'))


@bp.route('/fin/opening/close', methods=['POST'])
@login_required
@permission_required('fin.opening.manage')
def opening_close():
    """确认建账：试算平衡通过才允许；建账后账套才进入"可录凭证"状态。"""
    s = _opening_summary()
    if s['count'] == 0:
        flash('还没有录入任何期初余额，无法建账。请先录入各科目期初余额')
        return redirect(url_for('fin.opening'))
    if not s['balanced']:
        flash('试算不平衡，不能建账：借方合计 %s、贷方合计 %s、差额 %s。请先调整到借贷相等'
              % (_fmt(s['debit']), _fmt(s['credit']), _fmt(s['diff'])))
        return redirect(url_for('fin.opening'))
    c = _cfg()
    c.opening_done = True
    c.opening_done_at = datetime.utcnow()
    c.opening_done_by = session.get('username', '')
    db.session.commit()
    flash('期初建账完成：%s（%s）借方 %s = 贷方 %s，共 %d 个科目。现在可以开始录凭证了'
          % (c.company_name, c.start_period, _fmt(s['debit']), _fmt(s['credit']), s['count']))
    log_audit('update', module='fin', target_type='opening', target_no=c.start_period, detail='确认期初建账')
    return redirect(url_for('fin.opening'))


@bp.route('/fin/opening/reopen', methods=['POST'])
@login_required
@permission_required('fin.opening.manage')
def opening_reopen():
    """反建账：把账套退回"未建账"状态，方便继续调整期初。
    提醒：已经录过凭证后反建账，会让期初与账上数据对不上——凭证上线后此处会加硬校验。"""
    c = _cfg()
    c.opening_done = False
    c.opening_done_at = None
    c.opening_done_by = ''
    db.session.commit()
    flash('已撤销建账状态，可继续调整期初余额；调整完记得回来重新确认建账')
    return redirect(url_for('fin.opening'))


@bp.route('/fin/opening/clear', methods=['POST'])
@login_required
@permission_required('fin.opening.manage')
def opening_clear():
    """清空期初：删除全部期初行并撤销建账状态（正式建账前用来清掉演示/试录数据）。"""
    n = FinOpening.query.count()
    FinOpening.query.delete()
    c = _cfg()
    c.opening_done = False
    c.opening_done_at = None
    c.opening_done_by = ''
    db.session.commit()
    flash('已清空全部期初余额（共 %d 个科目），并撤销建账状态' % n)
    return redirect(url_for('fin.opening'))


# =====================================================================
# FMS · 记账凭证（增量 #32 录入 / #33 流转）
# ---------------------------------------------------------------------
# 记账规则（本模块的硬约束）：
#   ① 借贷必须相等（借方合计 = 贷方合计），不等不允许保存；
#   ② 分录只能选「启用中的明细科目」——汇总科目的余额由下级汇总，不能直接做分录；
#   ③ 同一行不能同时填借方和贷方，也不能两个都为 0；
#   ④ 必须先完成「期初建账」才能录凭证（期初不平 → 账上起点是错的）；
#   ⑤ 字别取 收/付/转 三类，各自独立连续编号，形如 收-202609-001。
# 流转规则（#33）：
#   ⑥ 制单 ≠ 审核：同一个人不能既录这张凭证又审这张凭证；
#   ⑦ 先审核后记账，未审核不许记账（不能自己给自己批）；
#   ⑧ 已记账的凭证不可改、不可删、不可作废，只能由主管红字冲销；
#   ⑨ 只有「已记账」的凭证才进账簿与报表（#34 / #35 据此取数）。
# =====================================================================

WORD_FALLBACK = '收,付,转'      # 账套没配字别时的兜底
ENTRY_BLANK_ROWS = 4            # 新增凭证时默认渲染几行空分录


def _voucher_words():
    """账套里配置的凭证字别（默认 收/付/转）。"""
    raw = _cfg().voucher_types or WORD_FALLBACK
    ws = [w.strip() for w in raw.replace('，', ',').split(',') if w.strip()]
    return ws or [w for w in WORD_FALLBACK.split(',')]


def _d2(v):
    """金额统一按两位小数（元角分）——避免浮点误差导致"看着平了、其实差一分"。"""
    return round(float(v or 0), 2)


def _voucher_account_groups():
    """分录科目下拉：按科目类别分组，只列「启用中的明细科目」。"""
    order = ['asset', 'liability', 'equity', 'cost', 'profit']
    accs = FinAccount.query.filter_by(is_leaf=True, enabled=True).all()
    accs.sort(key=lambda a: (order.index(a.category) if a.category in order else 9, a.code))
    groups, cur = [], None
    for a in accs:
        cn = a.category_cn or '其他'
        if not cur or cur[0] != cn:
            cur = (cn, [])
            groups.append(cur)
        cur[1].append(a)
    return groups


def _next_voucher_no(word, period):
    """生成凭证号：字别-期间-序号（三个字别各自独立连续编号，互不占号段）。
    period 存 'YYYY-MM'，凭证号里用去掉横杠的 'YYYYMM'。"""
    last = (FinVoucher.query.filter_by(word=word, period=period)
            .order_by(FinVoucher.seq.desc()).first())
    seq = (last.seq or 0) + 1 if last else 1
    return '%s-%s-%03d' % (word, period.replace('-', ''), seq), seq


def _parse_entries(form):
    """解析表单里的分录行 → (entries, errors)。
    表单字段约定：entry_code_<n> / entry_summary_<n> / entry_debit_<n> / entry_credit_<n>。
    整行全空的行直接跳过（表单默认渲染的空行不算错）。"""
    entries, errs = [], []
    idxs = []
    for k in form.keys():
        if k.startswith('entry_code_') and k.rsplit('_', 1)[1].isdigit():
            idxs.append(int(k.rsplit('_', 1)[1]))
    n = 0
    for i in sorted(set(idxs)):
        code = (form.get('entry_code_%d' % i) or '').strip()
        summ = (form.get('entry_summary_%d' % i) or '').strip()
        d = _to_amount(form.get('entry_debit_%d' % i))
        c = _to_amount(form.get('entry_credit_%d' % i))
        if not code and not summ and not (d or 0) and not (c or 0):
            continue                                   # 纯空行
        n += 1
        if None in (d, c):
            errs.append('第 %d 行：金额只能填 0 或正数，最多两位小数' % n)
            continue
        if not code:
            errs.append('第 %d 行：请选择会计科目' % n)
            continue
        acc = FinAccount.query.filter_by(code=code).first()
        if not acc:
            errs.append('第 %d 行：科目编码 %s 不存在' % (n, code))
            continue
        if not acc.is_leaf:
            errs.append('第 %d 行：%s %s 是汇总科目，不能做分录（请选它的明细科目）' % (n, acc.code, acc.name))
            continue
        if not acc.enabled:
            errs.append('第 %d 行：%s %s 已停用，不能做分录' % (n, acc.code, acc.name))
            continue
        if d == 0 and c == 0:
            errs.append('第 %d 行：%s %s 的借方和贷方不能都为 0' % (n, acc.code, acc.name))
            continue
        if d > 0 and c > 0:
            errs.append('第 %d 行：%s %s 不能同时填借方和贷方金额' % (n, acc.code, acc.name))
            continue
        entries.append({'seq': n, 'summary': summ, 'acc': acc, 'debit': _d2(d), 'credit': _d2(c)})
    return entries, errs


def _voucher_stats(items):
    """列表页概览：笔数、借贷合计、差额、附件张数，以及按状态/字别的分布。
    另给一档「已记账口径」——按会计规则只有已记账的凭证才算进账簿与报表（#34/#35 据此取数），
    所以草稿与作废的金额会体现在「全部口径」里、但不进「已记账口径」。"""
    d = _d2(sum((v.amount_debit or 0) for v in items))
    c = _d2(sum((v.amount_credit or 0) for v in items))
    posted = [v for v in items if v.status == 'posted']
    pd_, pc = (_d2(sum((v.amount_debit or 0) for v in posted)),
               _d2(sum((v.amount_credit or 0) for v in posted)))
    return {
        'count': len(items), 'debit': d, 'credit': c,
        'diff': _d2(d - c), 'balanced': abs(_d2(d - c)) < 0.005,
        'attach': sum((v.attach_count or 0) for v in items),
        'lines': sum(len(v.entries) for v in items),
        'by_status': {k: len([v for v in items if v.status == k]) for k in FinVoucher.STATUS_CN},
        'by_word': {w: len([v for v in items if v.word == w]) for w in _voucher_words()},
        # 已记账口径（真正进账簿与报表的部分）
        'posted_count': len(posted), 'posted_debit': pd_, 'posted_credit': pc,
        # 待办口径：草稿 = 等审核；已审核 = 等记账。这两个数是财务主管的日常待办
        'pending_audit': len([v for v in items if v.status == 'draft']),
        'pending_post': len([v for v in items if v.status == 'audited']),
        'red_count': len([v for v in items if v.is_red]),
    }


def _voucher_or_404(vid):
    """取凭证，不存在直接 404（流转类路由统一入口）。"""
    v = FinVoucher.query.get(vid)
    if not v:
        abort(404)
    return v


def _voucher_balanced(v):
    """按分录复核借贷是否相等。红字凭证是负数金额，同样要求两边相等。
    保存时已经校验过一次，但审核 / 记账前再复核一遍——账上的钱不能只信前端。"""
    d = _d2(sum((e.debit or 0) for e in v.entries))
    c = _d2(sum((e.credit or 0) for e in v.entries))
    return abs(_d2(d - c)) < 0.005


@bp.route('/fin/vouchers', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def vouchers():
    """记账凭证列表：按期间 / 字别 / 状态 / 关键字筛选，并给出本期借贷合计。"""
    cfg = _cfg()
    period = (request.args.get('period') or '').strip()
    if not period:                                     # 默认看"最近有凭证的期间"，没有再退回启用期间
        last = FinVoucher.query.order_by(FinVoucher.period.desc()).first()
        period = last.period if last else (cfg.start_period or '')
    if period == 'all':
        period = ''
    word = (request.args.get('word') or '').strip()
    status = (request.args.get('status') or '').strip()
    kw = (request.args.get('kw') or '').strip()

    q = FinVoucher.query
    if period:
        q = q.filter_by(period=period)
    if word:
        q = q.filter_by(word=word)
    if status:
        q = q.filter_by(status=status)
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(FinVoucher.voucher_no.like(like), FinVoucher.summary.like(like)))
    items = q.order_by(FinVoucher.period.desc(), FinVoucher.word, FinVoucher.seq.desc()).all()

    return render_template('fin/voucher_list.html', items=items, cfg=cfg,
                           stats=_voucher_stats(items),
                           period=period, word=word, status=status, kw=kw,
                           words=_voucher_words(),
                           status_options=list(FinVoucher.STATUS_CN.items()),
                           can_edit=_has_perm('fin.voucher.edit'),
                           can_flow=_has_perm('fin.voucher.audit'),
                           closed=_closed_periods(),     # #36：列表里标出哪些期间的账已结
                           me=session.get('user_id'))


@bp.route('/fin/vouchers/new', methods=['GET'])
@login_required
@permission_required('fin.voucher.edit')
def voucher_new():
    """新增凭证：渲染 ENTRIES_BLANK_ROWS 行空分录，日期默认今天。"""
    cfg = _cfg()
    if not cfg.opening_done:                           # 没建账不许录，避免"账上没有起点"
        flash('还没有完成期初建账，不能录凭证。请先到「期初建账」录入期初余额并确认建账')
        return redirect(url_for('fin.opening'))
    if _period_locked(datetime.now().strftime('%Y-%m')):   # #36：当前月已结账，先提醒一句
        flash('提醒：当前月份 %s 已结账，凭证日期落在该期间将无法保存；'
              '如需补录请先到「期末结转」反结账' % datetime.now().strftime('%Y-%m'))
    rows = [{'seq': i + 1, 'summary': '', 'account_code': '', 'debit': '', 'credit': ''}
            for i in range(ENTRY_BLANK_ROWS)]
    return render_template('fin/voucher_edit.html', v=None, rows=rows, cfg=cfg,
                           words=_voucher_words(), groups=_voucher_account_groups(),
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/fin/vouchers/<int:vid>', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def voucher_detail(vid):
    """凭证详情：与打印版式一致的单据视图（抬头 + 分录 + 合计 + 备注）+ 流转操作区。"""
    v = _voucher_or_404(vid)
    # 红字关系：被哪些红字凭证冲销过 / 自己冲的是哪张（双向可追溯，会计查账要看这个）
    reds = FinVoucher.query.filter_by(red_of_id=v.id).order_by(FinVoucher.id).all()
    origin = FinVoucher.query.get(v.red_of_id) if v.red_of_id else None
    return render_template('fin/voucher_detail.html', v=v, reds=reds, origin=origin,
                           locked=_period_locked(v.period),
                           can_edit=(_has_perm('fin.voucher.edit') and v.is_editable
                                     and not v.is_red and not v.is_closing
                                     and not _period_locked(v.period)),
                           can_flow=(_has_perm('fin.voucher.audit') and not _period_locked(v.period)
                                     and not v.is_closing),
                           self_made=(v.maker_id == session.get('user_id')))


@bp.route('/fin/vouchers/<int:vid>/edit', methods=['GET'])
@login_required
@permission_required('fin.voucher.edit')
def voucher_edit(vid):
    """修改凭证：只有草稿可改；已审核/已记账的凭证只能由主管红字冲销（#33）。"""
    v = FinVoucher.query.get(vid)
    if not v:
        abort(404)
    if not v.is_editable:
        flash('%s 当前状态是「%s」，不能修改。已审核 / 已记账的凭证只能由主管红字冲销'
              % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.is_red:                                       # 红字凭证金额是负数，由系统生成，手工改会破坏对冲关系
        flash('%s 是系统生成的红字凭证，不能手工修改。如不需要这张冲销，直接删除它即可'
              % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if _period_locked(v.period):                       # #36 期间锁定
        flash('%s 所属期间 %s 已结账，不能再修改。如需调整请先到「期末结转」反结账'
              % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.is_closing:                                   # #36 系统结转凭证
        flash('%s 是系统生成的期末结转凭证，不能手工修改（要调整请先反结账）' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    rows = [{'seq': e.seq, 'summary': e.summary or '', 'account_code': e.account_code or '',
             'debit': ('%.2f' % (e.debit or 0)) if (e.debit or 0) else '',
             'credit': ('%.2f' % (e.credit or 0)) if (e.credit or 0) else ''}
            for e in v.entries]
    while len(rows) < ENTRY_BLANK_ROWS:                # 至少留几行空行，方便加行
        rows.append({'seq': len(rows) + 1, 'summary': '', 'account_code': '', 'debit': '', 'credit': ''})
    return render_template('fin/voucher_edit.html', v=v, rows=rows, cfg=_cfg(),
                           words=_voucher_words(), groups=_voucher_account_groups(),
                           today=v.voucher_date.strftime('%Y-%m-%d') if v.voucher_date else '')


@bp.route('/fin/vouchers/save', methods=['POST'])
@login_required
@permission_required('fin.voucher.edit')
def voucher_save():
    """保存凭证（新增或修改草稿）。借贷不等、科目不合规一律拒绝落库。"""
    cfg = _cfg()
    if not cfg.opening_done:
        flash('还没有完成期初建账，不能录凭证。请先到「期初建账」录入期初余额并确认建账')
        return redirect(url_for('fin.opening'))

    vid = (request.form.get('vid') or '').strip()
    v = FinVoucher.query.get(int(vid)) if vid.isdigit() else None
    if v and not v.is_editable:
        flash('%s 当前状态是「%s」，不能修改' % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=v.id))
    if v and v.is_red:                                 # 同上：红字凭证不接受表单回写
        flash('%s 是系统生成的红字凭证，不能手工修改' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=v.id))
    if v and _period_locked(v.period):                 # #36 已结账期间整期锁定
        flash('%s 所属期间 %s 已结账，不能再修改。如需调整请先到「期末结转」反结账'
              % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=v.id))
    if v and v.is_closing:                             # #36 系统结转凭证不接受表单回写
        flash('%s 是系统生成的期末结转凭证，不能手工修改（要调整请先反结账）' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=v.id))
    back = url_for('fin.voucher_edit', vid=v.id) if v else url_for('fin.voucher_new')

    # —— 抬头字段 ——
    ds = (request.form.get('voucher_date') or '').strip()
    try:
        vdate = datetime.strptime(ds, '%Y-%m-%d').date()
    except ValueError:
        flash('请选择凭证日期')
        return redirect(back)
    word = (request.form.get('word') or '').strip()
    if word not in _voucher_words():
        flash('凭证字别只能是：%s' % '、'.join(_voucher_words()))
        return redirect(back)
    summary = (request.form.get('summary') or '').strip()
    if not summary:
        flash('请填写凭证摘要')
        return redirect(back)
    try:
        attach = int((request.form.get('attach_count') or '0').strip() or 0)
    except ValueError:
        attach = -1
    if attach < 0:
        flash('附件张数只能填 0 或正整数')
        return redirect(back)

    # —— 分录与平衡校验 ——
    entries, errs = _parse_entries(request.form)
    if errs:
        for e in errs:
            flash(e)
        return redirect(back)
    if len(entries) < 2:
        flash('一张凭证至少要有两行分录（有借有贷）')
        return redirect(back)
    d = _d2(sum(e['debit'] for e in entries))
    c = _d2(sum(e['credit'] for e in entries))
    if abs(_d2(d - c)) >= 0.005:
        flash('借贷不平衡，不能保存：借方合计 %s、贷方合计 %s、差额 %s'
              % (_fmt(d), _fmt(c), _fmt(_d2(d - c))))
        return redirect(back)

    # —— 落库 ——
    is_new = v is None
    if is_new:
        v = FinVoucher(maker_id=session.get('user_id'), maker_name=session.get('username', ''))
        db.session.add(v)
    period = vdate.strftime('%Y-%m')
    if _period_locked(period):                         # #36：不许往已结账的期间里塞新凭证
        flash('期间 %s 已结账并锁定，不能在该期间录入或修改凭证。'
              '如需补录请先到「期末结转」反结账' % period)
        return redirect(back)
    # 期间或字别变了才重新取号，否则保留原凭证号（避免草稿改日期就换号）
    if is_new or v.period != period or v.word != word:
        v.voucher_no, v.seq = _next_voucher_no(word, period)
    v.period, v.word, v.voucher_date = period, word, vdate
    v.summary, v.attach_count = summary, attach
    v.remark = (request.form.get('remark') or '').strip()
    v.amount_debit, v.amount_credit = d, c
    v.updated_at = datetime.utcnow()
    v.entries.clear()                                  # delete-orphan：旧分录一并清掉，再按表单重写
    for e in entries:
        v.entries.append(FinVoucherEntry(
            seq=e['seq'], summary=e['summary'],
            account_code=e['acc'].code, account_name=e['acc'].name,
            debit=e['debit'], credit=e['credit']))
    db.session.commit()
    flash('凭证 %s 已保存：%d 行分录，借 %s = 贷 %s%s'
          % (v.voucher_no, len(entries), _fmt(d), _fmt(c),
             '，附件 %d 张' % attach if attach else ''))
    log_audit('add', module='fin', target_type='voucher', target_id=v.id, target_no=v.voucher_no, detail='新增凭证草稿')
    return redirect(url_for('fin.voucher_detail', vid=v.id))


@bp.route('/fin/vouchers/<int:vid>/delete', methods=['POST'])
@login_required
@permission_required('fin.voucher.edit')
def voucher_delete(vid):
    """删除凭证：只允许删草稿（已审核/已记账不能删，避免账上凭空少一张）。"""
    v = FinVoucher.query.get(vid)
    if not v:
        abort(404)
    if not v.is_editable:
        flash('%s 当前状态是「%s」，不能删除' % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.is_closing:                                   # #36 结转凭证归反结账管，不给人手删
        flash('%s 是系统生成的期末结转凭证，不能手工删除（要撤销请到「期末结转」反结账）'
              % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if _period_locked(v.period):                       # #36 期间锁定
        flash('%s 所属期间 %s 已结账，不能删除。如需调整请先反结账' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    no = v.voucher_no
    db.session.delete(v)
    db.session.commit()
    flash('已删除凭证 %s' % no)
    return redirect(url_for('fin.vouchers'))


# ---------------------------------------------------------------------
# 凭证流转（增量 #33）：草稿 → 已审核 → 已记账；未记账可作废；已记账只能红冲
# 状态机很小，但每条边都是一条会计纪律，所以在路由里逐个写清楚，不做通用流转函数——
# 将来真要看懂"为什么这张凭证动不了"，读这一段就够了。
# ---------------------------------------------------------------------

@bp.route('/fin/vouchers/<int:vid>/audit', methods=['POST'])
@login_required
@permission_required('fin.voucher.audit')
def voucher_audit(vid):
    """审核：草稿 → 已审核。**制单 ≠ 审核**（同一个人不能既录这张凭证又审这张凭证）。"""
    v = _voucher_or_404(vid)
    if _period_locked(v.period):                       # #36：结账后整期冻结，不能再审核
        flash('%s 所属期间 %s 已结账，不能再审核' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not v.can_audit:
        flash('%s 当前状态是「%s」，不能审核' % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    uid = session.get('user_id')
    if v.maker_id and uid and v.maker_id == uid:
        flash('制单与审核必须是两个人：%s 是你自己录的凭证，不能由你审核（避免监守自盗）'
              % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if len(v.entries) < 2 or not _voucher_balanced(v):
        flash('%s 借贷不平衡或分录不足两行，不能审核' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    v.status, v.checker_id = 'audited', uid
    v.checker_name = session.get('username', '')
    v.checked_at = v.updated_at = datetime.utcnow()
    db.session.commit()
    flash('凭证 %s 已审核，等待记账' % v.voucher_no)
    log_audit('audit', module='fin', target_type='voucher', target_id=v.id, target_no=v.voucher_no, detail='审核凭证')
    return redirect(url_for('fin.voucher_detail', vid=vid))


@bp.route('/fin/vouchers/<int:vid>/unaudit', methods=['POST'])
@login_required
@permission_required('fin.voucher.audit')
def voucher_unaudit(vid):
    """弃审：已审核 → 草稿。审核发现录错了，退回制单人改，而不是红冲（红冲是给已记账用的）。"""
    v = _voucher_or_404(vid)
    if _period_locked(v.period):                       # #36：结账后整期冻结
        flash('%s 所属期间 %s 已结账，不能弃审' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.status != 'audited':
        flash('%s 当前状态是「%s」，不能弃审' % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    v.status = 'draft'
    v.checker_id = v.checker_name = None
    v.checked_at = None
    v.updated_at = datetime.utcnow()
    db.session.commit()
    flash('凭证 %s 已弃审，退回草稿状态，可以继续修改' % v.voucher_no)
    log_audit('unaudit', module='fin', target_type='voucher', target_id=v.id, target_no=v.voucher_no, detail='弃审退回草稿')
    return redirect(url_for('fin.voucher_detail', vid=vid))


@bp.route('/fin/vouchers/<int:vid>/post', methods=['POST'])
@login_required
@permission_required('fin.voucher.audit')
def voucher_post(vid):
    """记账：已审核 → 已记账。记账后凭证进账簿与报表，不可再改。"""
    v = _voucher_or_404(vid)
    if _period_locked(v.period):                       # #36：结账后整期冻结
        flash('%s 所属期间 %s 已结账，不能再记账' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not v.can_post:
        flash('%s 当前状态是「%s」，不能记账（必须先审核通过）' % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not _voucher_balanced(v):
        flash('%s 借贷不平衡，不能记账' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    v.status, v.posted_at = 'posted', datetime.utcnow()
    v.updated_at = datetime.utcnow()
    db.session.commit()
    flash('凭证 %s 已记账，正式进入账簿。记账后不可修改，如需更正请用红字冲销' % v.voucher_no)
    log_audit('post', module='fin', target_type='voucher', target_id=v.id, target_no=v.voucher_no, detail='凭证记账')
    return redirect(url_for('fin.voucher_detail', vid=vid))


@bp.route('/fin/vouchers/<int:vid>/void', methods=['POST'])
@login_required
@permission_required('fin.voucher.audit')
def voucher_void(vid):
    """作废：草稿 / 已审核 → 已作废。作废是留痕不是删除（凭证号不回收，审计要看得见）。
    已记账的凭证不能作废——账已经进了账簿，只能红字冲销。"""
    v = _voucher_or_404(vid)
    if _period_locked(v.period):                       # #36：结账后整期冻结
        flash('%s 所属期间 %s 已结账，不能作废' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.is_closing:                                   # #36：结转凭证由反结账撤销，不走作废
        flash('%s 是系统生成的期末结转凭证，不能手工作废（要撤销请反结账）' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not v.can_void:
        flash('%s 当前状态是「%s」，不能作废%s'
              % (v.voucher_no, v.status_cn,
                 '（已记账的凭证只能红字冲销）' if v.status == 'posted' else ''))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    reason = (request.form.get('void_reason') or '').strip()
    v.status, v.void_at = 'void', datetime.utcnow()
    v.void_by = session.get('username', '')
    if reason:
        v.remark = ('%s ｜ 作废原因：%s' % (v.remark, reason) if v.remark
                    else '作废原因：' + reason)[:200]
    v.updated_at = datetime.utcnow()
    db.session.commit()
    flash('凭证 %s 已作废（凭证号与内容留痕保留，不参与账簿与报表）' % v.voucher_no)
    return redirect(url_for('fin.voucher_detail', vid=vid))


@bp.route('/fin/vouchers/<int:vid>/reverse', methods=['POST'])
@login_required
@permission_required('fin.voucher.audit')
def voucher_reverse(vid):
    """红字冲销：对已审核 / 已记账的凭证生成一张红字凭证（同字别同期间、金额取负）。
    原凭证保持原样不动——这是会计上「不涂改历史账」的标准做法。
    红字凭证本身还要经「审核 → 记账」两关，不是一点就生效。"""
    v = _voucher_or_404(vid)
    if _period_locked(v.period):                       # #36：结账后整期冻结
        flash('%s 所属期间 %s 已结账，不能红冲' % (v.voucher_no, v.period))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if v.is_closing:                                   # #36：结转凭证由反结账撤销，不走红冲
        flash('%s 是系统生成的期末结转凭证，不能红冲（要撤销请先反结账）' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not v.can_reverse:
        flash('%s 当前状态是「%s」，不能红冲%s'
              % (v.voucher_no, v.status_cn,
                 '（已作废的凭证无需红冲）' if v.status == 'void' else '（草稿直接改就行）'))
        return redirect(url_for('fin.voucher_detail', vid=vid))
    if not v.entries:
        flash('%s 没有分录，无法红冲' % v.voucher_no)
        return redirect(url_for('fin.voucher_detail', vid=vid))
    reason = (request.form.get('reverse_reason') or '').strip()
    # 沿用原凭证的字别与期间/日期：同一个会计期间内对冲，账上才不会留下半截差额
    who = session.get('username') or ''
    red = FinVoucher(word=v.word, period=v.period, voucher_date=v.voucher_date,
                     attach_count=0, is_red=True, red_of_id=v.id, status='draft',
                     source_type=v.source_type,
                     summary=('红字冲销 %s：%s' % (v.voucher_no, v.summary or ''))[:200],
                     # 制单人沿用原凭证：红冲在会计上是「替原制单人更正」，责任仍归属原制单。
                     # 这样主管红冲后仍能审核这张红字凭证（「制单≠审核」照样成立），
                     # 否则红字凭证会永远卡在草稿、入不了账，报表也就永远对不平。
                     # 执行红冲的人记在备注里，追溯链一样清楚。
                     maker_id=v.maker_id, maker_name=v.maker_name,
                     remark=('红冲执行人：%s%s' % (who, '；' + reason if reason else ''))[:200])
    red.voucher_no, red.seq = _next_voucher_no(v.word, v.period)
    # 科目与方向都不动，只把金额取负 → 新红字凭证与原凭证逐行相加恰好为 0
    for e in v.entries:
        red.entries.append(FinVoucherEntry(
            seq=e.seq or 0, summary=e.summary,
            account_code=e.account_code, account_name=e.account_name,
            debit=-_d2(e.debit or 0), credit=-_d2(e.credit or 0)))
    red.amount_debit = _d2(sum((e.debit or 0) for e in red.entries))
    red.amount_credit = _d2(sum((e.credit or 0) for e in red.entries))
    red.updated_at = datetime.utcnow()
    db.session.add(red)
    db.session.commit()
    flash('已生成红字凭证 %s（冲销 %s，金额取负）。它当前是草稿，需经审核 → 记账后才真正入账'
          % (red.voucher_no, v.voucher_no))
    log_audit('reverse', module='fin', target_type='voucher', target_id=red.id, target_no=red.voucher_no, detail='红字冲销 %s' % v.voucher_no)
    return redirect(url_for('fin.voucher_detail', vid=red.id))


# =====================================================================
# FMS · 账簿查询（增量 #34）
# ---------------------------------------------------------------------
# 账簿是「账」的成品：凭证是原料，账簿把原料按科目 / 按时间重新排版。
# 取数口径四条（与凭证流转的约定严格一致）：
#   ① 只有 status='posted'（已记账）的凭证进账簿——草稿、已审核、已作废都不进；
#   ② 红字凭证金额是负数，天然抵减，不需要特殊处理；
#   ③ 期初余额来自「期初建账」（FinOpening，那是启用期间的期初）。查更靠后的期间时，
#      期初 = 启用期初 + 启用期间至上期末的已记账净发生额——账是滚出来的，不另存一份；
#   ④ 汇总科目不单独记账，其余额由下级明细科目向上汇总得出。
# 设计取舍：账簿**不新增任何表**，永远是凭证与期初的实时投影。
#          好处是改了凭证账立刻跟着变，不存在"账表对不上"的可能。
# 三张账：科目余额表（时点数） / 明细账（单科目流水） / 现金银行日记账（出纳收付余口径）。
# =====================================================================

LEDGER_EPS = 0.005                 # 金额判定容差（半分钱以内视为 0，与借贷平衡校验同口径）
JOURNAL_ROOTS = ['1001', '1002']   # 日记账科目前缀：1001 库存现金 / 1002 银行存款


def _period_add(period, delta):
    """会计期间加减：_period_add('2026-01', -1) → '2025-12'。
    折算成"年×12+月"的月序号再回退，省得自己写跨年判断。"""
    p = (period or '').strip()
    if len(p) < 7 or not p[:4].isdigit() or not p[5:7].isdigit():
        return ''
    t = int(p[:4]) * 12 + (int(p[5:7]) - 1) + delta
    return '%04d-%02d' % (t // 12, t % 12 + 1)


def _cfg_start():
    """账套启用期间——账簿的起点（期初余额就是这一期期初的数）。"""
    return (_cfg().start_period or '').strip()


def _default_period():
    """账簿默认看哪一期：最近有已记账凭证的期间 → 没有就退启用期间 → 再退当月。"""
    last = (FinVoucher.query.filter_by(status='posted')
            .order_by(FinVoucher.period.desc()).first())
    if last and last.period:
        return last.period
    return _cfg_start() or datetime.now().strftime('%Y-%m')


def _posted_flows(p_from='', p_to='', codes=None, exclude_closing=False):
    """按科目汇总「已记账」凭证的发生额 → {科目编码: [借方合计, 贷方合计]}。
    期间用字符串比较（YYYY-MM 定长，字典序即时间序），比转日期更省事也更稳。
    exclude_closing=True 时剔除「期末结转」系统凭证（#36）——利润表 / 现金流量表要用这个口径：
    结转只是把损益搬到本年利润的账内转账，不是业务发生额，混进来会把利润表和现金流算歪。"""
    q = (db.session.query(FinVoucherEntry.account_code,
                          db.func.sum(FinVoucherEntry.debit),
                          db.func.sum(FinVoucherEntry.credit))
         .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
         .filter(FinVoucher.status == 'posted'))
    if exclude_closing:
        q = q.filter(FinVoucher.source_type != 'closing')
    if p_from:
        q = q.filter(FinVoucher.period >= p_from)
    if p_to:
        q = q.filter(FinVoucher.period <= p_to)
    if codes:
        q = q.filter(FinVoucherEntry.account_code.in_(list(codes)))
    return {code: [float(d or 0), float(c or 0)]
            for code, d, c in q.group_by(FinVoucherEntry.account_code).all()}


def _rollup(leaf_vals, accounts):
    """把金额沿 parent_code 一层层加到上级，算出汇总科目的数。
    汇总科目自己没有分录，它的数 = 所有下级明细科目之和（会计准则就是这么定的）。"""
    parent_of = {a.code: (a.parent_code or '') for a in accounts}
    out = {a.code: [0.0, 0.0] for a in accounts}
    for code, vals in leaf_vals.items():
        if code not in out:
            continue                                  # 科目被删过：分录里有、科目表里没有，跳过
        c, guard = code, 0
        while c and c in out and guard < 20:          # guard 防止科目树自环时死循环
            out[c][0] += vals[0]
            out[c][1] += vals[1]
            c = parent_of.get(c, '')
            guard += 1
    return out


def _two_col(net):
    """把「借 − 贷」的净额拆成余额表的借方列 / 贷方列。
    会计上余额不写负数：净额为正 = 借方余额，为负 = 贷方余额。"""
    n = _d2(net)
    if n > LEDGER_EPS:
        return n, 0.0
    if n < -LEDGER_EPS:
        return 0.0, _d2(-n)
    return 0.0, 0.0


def _opening_net(codes, period):
    """某科目（组）在 period 这一期的期初净额（借 − 贷，正=借方余额）。
    = 期初建账余额 + 启用期间..period 上一期 的已记账净发生额。"""
    codes = list(codes)
    if not codes:
        return 0.0
    start = _cfg_start()
    if start and period and period < start:
        return 0.0                                     # 查启用之前的期间：账套还没有起点，一律 0
    net = 0.0
    for o in FinOpening.query.filter(FinOpening.account_code.in_(codes)).all():
        net += float(o.debit or 0) - float(o.credit or 0)
    before_to = _period_add(period, -1)
    if before_to and (not start or before_to >= start):
        for v in _posted_flows(start, before_to, codes).values():
            net += v[0] - v[1]
    return _d2(net)


def _ledger_lines(codes, p_from, p_to):
    """取这些科目在期间范围内的已记账分录流水，按 日期 → 凭证号 → 科目 排序。
    明细账与日记账共用这一份流水（区别只在展示口径：一个是科目口径，一个是出纳口径）。"""
    codes = list(codes)
    if not codes:
        return []
    q = (db.session.query(FinVoucherEntry, FinVoucher)
         .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
         .filter(FinVoucher.status == 'posted'))
    if p_from:
        q = q.filter(FinVoucher.period >= p_from)
    if p_to:
        q = q.filter(FinVoucher.period <= p_to)
    q = q.filter(FinVoucherEntry.account_code.in_(codes))
    rows = []
    for e, v in q.all():
        rows.append({
            'date': v.voucher_date, 'period': v.period,
            'voucher_no': v.voucher_no or '', 'voucher_id': v.id, 'word': v.word,
            'summary': e.summary or v.summary or '—',
            'code': e.account_code or '', 'name': e.account_name or '',
            'debit': _d2(e.debit or 0), 'credit': _d2(e.credit or 0),
            'is_red': bool(v.is_red),
        })
    rows.sort(key=lambda r: (r['date'] or datetime.min.date(), r['voucher_no'], r['code']))
    return rows


def _run_balance(rows, opening_net):
    """给流水逐行补上滚动余额（借 − 贷 净额）。余额从期初开始一行行滚下来。"""
    bal, out = _d2(opening_net), []
    for r in rows:
        bal = _d2(bal + r['debit'] - r['credit'])
        x = dict(r)
        x['balance'] = bal
        x['bal_d'], x['bal_c'] = _two_col(bal)
        out.append(x)
    return out


def _balance_rows(period, cat='', code_from='', code_to='', hide_zero=True):
    """科目余额表：逐科目算出 期初余额 / 本期发生额 / 期末余额（各分借贷两列）。
    返回 (rows, meta)；meta 里的合计**始终是全账口径**（只加一级科目，全加会重复计算），
    不受页面筛选影响——避免用户筛了一个类别后把"合计"误读成全公司数。"""
    accounts = FinAccount.query.order_by(FinAccount.code).all()
    start = _cfg_start()
    ob = {}
    if not (start and period and period < start):      # 启用之前的期间：期初一律 0
        for o in FinOpening.query.all():
            ob[o.account_code] = [float(o.debit or 0) - float(o.credit or 0), 0.0]
        before_to = _period_add(period, -1)
        if before_to and (not start or before_to >= start):
            for code, v in _posted_flows(start, before_to).items():
                cur = ob.setdefault(code, [0.0, 0.0])
                cur[0] += v[0] - v[1]
    in_p = _posted_flows(period, period)
    ob_r = _rollup(ob, accounts)
    in_r = _rollup(in_p, accounts)

    rows = []
    for a in accounts:
        net_open = ob_r.get(a.code, [0.0, 0.0])[0]
        pd_, pc = in_r.get(a.code, [0.0, 0.0])
        net_end = _d2(net_open + pd_ - pc)             # 期末 = 期初 + 本期借 − 本期贷
        od, oc = _two_col(net_open)
        ed, ec = _two_col(net_end)
        pd_, pc = _d2(pd_), _d2(pc)
        rows.append({
            'code': a.code, 'name': a.name, 'category': a.category,
            'category_cn': a.category_cn, 'direction': a.direction,
            'direction_cn': a.direction_cn, 'level': a.level, 'is_leaf': a.is_leaf,
            'open_d': od, 'open_c': oc, 'debit': pd_, 'credit': pc,
            'end_d': ed, 'end_c': ec,
            'zero': not (abs(od) + abs(oc) + abs(pd_) + abs(pc) + abs(ed) + abs(ec) > LEDGER_EPS),
        })

    tops = [r for r in rows if r['level'] == 1]
    meta = {
        'period': period, 'start': start,
        'vch_count': FinVoucher.query.filter_by(status='posted', period=period).count(),
        'open_d': _d2(sum(r['open_d'] for r in tops)),
        'open_c': _d2(sum(r['open_c'] for r in tops)),
        'debit': _d2(sum(r['debit'] for r in tops)),
        'credit': _d2(sum(r['credit'] for r in tops)),
        'end_d': _d2(sum(r['end_d'] for r in tops)),
        'end_c': _d2(sum(r['end_c'] for r in tops)),
    }
    meta['open_balanced'] = abs(meta['open_d'] - meta['open_c']) < LEDGER_EPS
    meta['flow_balanced'] = abs(meta['debit'] - meta['credit']) < LEDGER_EPS
    meta['end_balanced'] = abs(meta['end_d'] - meta['end_c']) < LEDGER_EPS

    if cat:
        rows = [r for r in rows if r['category'] == cat]
    if code_from:
        rows = [r for r in rows if r['code'] >= code_from]
    if code_to:
        rows = [r for r in rows if r['code'] <= code_to]
    meta['matched'] = len(rows)
    if hide_zero:
        rows = [r for r in rows if not r['zero']]
    meta['shown'] = len(rows)
    return rows, meta


def _journal_accounts(scope='all'):
    """日记账科目集合：现金（1001*）/ 银行（1002*）两棵树，只取明细科目。
    返回 (codes, label)。按前缀取而不是写死两个编码——银行将来拆多个户也不用改这里。"""
    leaves = FinAccount.query.filter_by(is_leaf=True).order_by(FinAccount.code).all()
    cash = [a for a in leaves if a.code.startswith(JOURNAL_ROOTS[0])]
    bank = [a for a in leaves if a.code.startswith(JOURNAL_ROOTS[1])]
    if scope == 'cash':
        return [a.code for a in cash], '库存现金'
    if scope == 'bank':
        return [a.code for a in bank], '银行存款'
    return [a.code for a in cash + bank], '货币资金（现金 + 银行）'


def _journal_scopes():
    """日记账的范围下拉项。"""
    return [('all', '货币资金（现金 + 银行）'), ('cash', '库存现金'), ('bank', '银行存款')]


@bp.route('/fin/ledger', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def ledger_hub():
    """账簿首页：三张账的入口 + 本期试算平衡自检。
    财务主管一进来最想知道两件事：本期记了多少账、账还平不平。"""
    period = (request.args.get('period') or '').strip() or _default_period()
    _rows, meta = _balance_rows(period)
    return render_template('fin/ledger_hub.html', cfg=_cfg(), period=period, meta=meta)


@bp.route('/fin/ledger/balance', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def ledger_balance():
    """科目余额表：期末各科目的期初余额 / 本期发生额 / 期末余额。
    硬等式：期末余额 = 期初余额 + 本期借方 − 本期贷方；勾稽不上说明凭证有问题。"""
    period = (request.args.get('period') or '').strip() or _default_period()
    cat = (request.args.get('cat') or '').strip()
    code_from = (request.args.get('code_from') or '').strip()
    code_to = (request.args.get('code_to') or '').strip()
    hide_zero = request.args.get('all') != '1'         # 默认只看有数的行
    rows, meta = _balance_rows(period, cat, code_from, code_to, hide_zero)
    return render_template('fin/ledger_balance.html', cfg=_cfg(), rows=rows, meta=meta,
                           period=period, cat=cat, code_from=code_from, code_to=code_to,
                           hide_zero=hide_zero, category_options=CATEGORY_OPTIONS)


@bp.route('/fin/ledger/detail', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def ledger_detail():
    """明细账：一个明细科目在某期间范围内的逐笔发生 + 滚动余额。
    明细账只认明细科目——汇总科目的数看科目余额表（那是它的本职）。"""
    accounts = FinAccount.query.filter_by(is_leaf=True).order_by(FinAccount.code).all()
    code = (request.args.get('code') or '').strip()
    if not code and accounts:
        code = accounts[0].code
    # period 是简写：传了它就等于起止期间都取这一期（账簿首页的入口卡统一带这个参数）
    period = (request.args.get('period') or '').strip()
    p_from = (request.args.get('p_from') or '').strip() or period or _default_period()
    p_to = (request.args.get('p_to') or '').strip() or period or p_from
    if p_from and p_to and p_from > p_to:              # 起止填反了自动纠正，不用报错折腾
        p_from, p_to = p_to, p_from

    acc = FinAccount.query.filter_by(code=code).first() if code else None
    lines, opening = [], 0.0
    if acc and not acc.is_leaf:
        flash('%s %s 是汇总科目，明细账只认明细科目；请选它的下级明细科目，或看科目余额表'
              % (acc.code, acc.name))
    elif acc:
        opening = _opening_net([acc.code], p_from)
        lines = _run_balance(_ledger_lines([acc.code], p_from, p_to), opening)
    # meta 一律算齐——模板要直接取用；汇总科目时流水为空，各项自然为 0
    d = _d2(sum(r['debit'] for r in lines))
    c = _d2(sum(r['credit'] for r in lines))
    meta = {'debit': d, 'credit': c, 'count': len(lines),
            'closing': _d2(opening + d - c)}
    return render_template('fin/ledger_detail.html', cfg=_cfg(), acc=acc, lines=lines,
                           opening=opening, meta=meta, code=code, accounts=accounts,
                           p_from=p_from, p_to=p_to, start=_cfg_start(),
                           category_options=CATEGORY_OPTIONS)


@bp.route('/fin/ledger/journal', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def ledger_journal():
    """现金 / 银行日记账：出纳口径的流水账（收入 / 支出 / 余额三栏）。
    一期不单设出纳岗，由财务员工兼管；二期拆岗时这套账直接沿用，不返工。"""
    scope = (request.args.get('scope') or 'all').strip()
    if scope not in ('all', 'cash', 'bank'):
        scope = 'all'
    codes, label = _journal_accounts(scope)
    # period 是简写：账簿首页的入口卡统一带这个参数，传了就只看这一期
    period = (request.args.get('period') or '').strip()
    p_from = (request.args.get('p_from') or '').strip() or period or _cfg_start() or _default_period()
    p_to = (request.args.get('p_to') or '').strip() or period or _default_period()
    if p_from and p_to and p_from > p_to:
        p_from, p_to = p_to, p_from

    opening = _opening_net(codes, p_from)
    lines = _run_balance(_ledger_lines(codes, p_from, p_to), opening)
    income = _d2(sum(r['debit'] for r in lines))
    outlay = _d2(sum(r['credit'] for r in lines))
    meta = {'income': income, 'outlay': outlay, 'count': len(lines),
            'closing': _d2(opening + income - outlay),
            'has_account': bool(codes)}
    return render_template('fin/ledger_journal.html', cfg=_cfg(), lines=lines, meta=meta,
                           opening=opening, scope=scope, scopes=_journal_scopes(),
                           label=label, codes=codes, p_from=p_from, p_to=p_to,
                           start=_cfg_start())


# =====================================================================
# FMS · 报表中心（增量 #35）
# ---------------------------------------------------------------------
# 两条不变的原则（与账簿一致，是整套财务数据的地基）：
#   ① 报表是「投影」，不另建数据表——库里只存「报表长什么样」（模板 + 公式），
#      打开报表时用当前账实时算。凭证一改，三张表立刻跟着变，永不"账表对不上"。
#   ② 只有 status='posted'（已记账）的凭证进报表，草稿 / 已审核 / 已作废一律不算。
# 公式引擎：每个行项目 = 一条表达式，支持六个取数函数 + 加减与括号：
#   QC 期初 / QM 期末 / DF 本期借方发生 / CF 本期贷方发生 /
#   BF 本期净发生（按科目余额方向取正）/ LF 本年累计净发生
# 求值方式：先把函数调用替换成数字，替换后只允许 数字 与 + - ( ) 参与运算，
#   再走 AST 白名单求值（不裸跑 eval）。公式写错一律给 0，绝不让报表页整页崩。
# 同一份公式出两列：换一个取数上下文，就得到「年初余额 / 期末余额」两列、
#   或「本期金额 / 本年累计金额」两列，不用把公式写两遍。
# =====================================================================

REPORT_FUNC_CN = {
    'QC': '期初余额', 'QM': '期末余额',
    'DF': '本期借方发生额', 'CF': '本期贷方发生额',
    'BF': '本期净发生额（按科目余额方向取正）',
    'LF': '本年累计净发生额',
}
_FUNC_RE = re.compile(r'\b(QC|QM|DF|CF|BF|LF)\s*\(\s*([0-9A-Za-z]*)\s*\)')
_NUM_OK = re.compile(r'[0-9eE+\-().\s]+')
_AST_OK = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
           ast.Add, ast.Sub, ast.USub, ast.UAdd)
CF_INVEST = ('16', '17')          # 投资活动：固定资产 / 累计折旧 / 无形资产
CF_FINANCE = '3'                  # 筹资活动：所有者权益类科目


class _RepCtx:
    """报表取数上下文：把某一期间要用到的三套数一次性算好，之后公式求值只读内存。
    三套数：① 期初净额（期初建账 + 启用期以来逐期滚存）② 本期发生额 ③ 本年累计发生额。"""

    def __init__(self, period, mode='end', amount='bf', exclude_closing=False):
        self.mode = mode          # 'end' 期末口径 / 'begin' 开头余额列（取该期期初）
        self.amount = amount      # 'bf' 本期净发生 / 'lf' 本年累计净发生
        self.exclude_closing = exclude_closing   # #36：利润表口径要剔除期末结转系统凭证
        self.start = _cfg_start()
        p = (period or '').strip()
        # 报表期早于启用期时退到启用期——否则整张表全是 0，看不出建账起点在哪
        if self.start and p and p < self.start:
            p = self.start
        self.period = p
        accs = FinAccount.query.order_by(FinAccount.code).all()
        self.dirs = {a.code: (a.direction or 'debit') for a in accs}
        self.open_r = _rollup(self._open_leaf(), accs)
        self.cur_r = _rollup(_posted_flows(self.period, self.period,
                                           exclude_closing=self.exclude_closing), accs)
        y = self.period[:4]
        ytd_from = ('%s-01' % y) if (len(y) == 4 and y.isdigit()) else self.period
        if self.start and ytd_from and ytd_from < self.start:
            ytd_from = self.start
        self.ytd_from = ytd_from
        self.ytd_r = _rollup(_posted_flows(ytd_from, self.period,
                                           exclude_closing=self.exclude_closing), accs)

    def _open_leaf(self):
        """期初净额（明细科目级）：期初建账余额 + 启用期..报表期上一期 的已记账净额。
        先按明细科目算，再统一 rollup 到汇总科目。"""
        out = {}
        for o in FinOpening.query.all():
            out[o.account_code] = [float(o.debit or 0) - float(o.credit or 0), 0.0]
        before_to = _period_add(self.period, -1)
        if before_to and (not self.start or before_to >= self.start):
            for code, v in _posted_flows(self.start, before_to,
                                         exclude_closing=self.exclude_closing).items():
                cur = out.setdefault(code, [0.0, 0.0])
                cur[0] += v[0] - v[1]
        return out

    def _sign(self, code):
        """余额方向系数：贷方科目把「借−贷」翻成「贷−借」，这样收入 / 负债 / 权益都是正数。"""
        return -1.0 if self.dirs.get(code) == 'credit' else 1.0

    def get(self, fn, code, amount=None):
        """取一个数。fn 取不到或科目不存在一律给 0，绝不让报表页报错。"""
        code = (code or '').strip()
        if not code:
            return 0.0
        if fn == 'DF':
            return _d2(self.cur_r.get(code, [0.0, 0.0])[0])
        if fn == 'CF':
            return _d2(self.cur_r.get(code, [0.0, 0.0])[1])
        if fn in ('BF', 'LF'):
            src = self.ytd_r if (fn == 'LF' or (amount or self.amount) == 'lf') else self.cur_r
            d, c = src.get(code, [0.0, 0.0])
            return _d2((d - c) * self._sign(code))
        if fn in ('QC', 'QM'):
            net = self.open_r.get(code, [0.0, 0.0])[0]
            if fn == 'QM' and self.mode != 'begin':
                cur = self.cur_r.get(code, [0.0, 0.0])
                net = _d2(net + cur[0] - cur[1])
            return _d2(net * self._sign(code))
        return 0.0


def _eval_formula(expr, getter):
    """求一条报表公式。getter(fn, code) → float。
    先把函数调用替换成数字，替换后只允许 数字 与 + - ( ) 参与运算，再走 AST 白名单求值。
    公式乱写 / 括号不配对 / 科目不存在，一律返回 0，绝不让报表页整页崩。"""
    s = (expr or '').strip()
    if not s:
        return 0.0
    s = (s.replace('（', '(').replace('）', ')')
          .replace('＋', '+').replace('－', '-'))
    s = _FUNC_RE.sub(lambda m: '(%s)' % repr(getter(m.group(1).upper(), m.group(2))), s)
    if not _NUM_OK.fullmatch(s):
        return 0.0                                     # 替换后还有别的字符 → 公式不合法
    try:
        tree = ast.parse(s, mode='eval')
    except Exception:
        return 0.0
    for node in ast.walk(tree):
        if not isinstance(node, _AST_OK):
            return 0.0                                 # 白名单外的一切（调用 / 名字 / 属性）都拒
    try:
        return _d2(float(eval(compile(tree, '<report>', 'eval'), {'__builtins__': {}}, {})))
    except Exception:
        return 0.0


def _report_rows(rows_def, ctx, amount=None):
    """逐行求值。amount 传了就覆盖上下文的净发生口径（用于同一模板出「本期 / 累计」两列）。"""
    getter = (lambda fn, code: ctx.get(fn, code, amount)) if amount else ctx.get
    out = []
    for r in rows_def:
        if not isinstance(r, dict):
            continue
        it = dict(r)
        it['value'] = 0.0 if r.get('head') else _eval_formula(r.get('formula'), getter)
        out.append(it)
    return out


def _report_meta(rows, kind):
    """报表级自检。资产负债表核对「资产 = 负债 + 所有者权益」；利润表把关键行拎出来。"""
    v = {r.get('key'): r.get('value', 0.0) for r in rows if r.get('key')}
    m = {'kind': kind}
    if kind == 'bs':
        a, b = v.get('total_assets', 0.0), v.get('total_liab_equity', 0.0)
        m.update({'assets': a, 'liab_equity': b, 'diff': _d2(a - b),
                  'balanced': abs(a - b) < LEDGER_EPS})
    elif kind == 'pl':
        m.update({'revenue': v.get('revenue', 0.0), 'op_profit': v.get('op_profit', 0.0),
                  'total_profit': v.get('total_profit', 0.0),
                  'net_profit': v.get('net_profit', 0.0)})
    return m


def _sys_tpl(cat):
    """取某类别的系统内置模板。内置的永远是最先建的那条，不会被就地改。"""
    return (FinReportTemplate.query.filter_by(category=cat, is_system=True)
            .order_by(FinReportTemplate.id).first())


def _calc_bs(period):
    """资产负债表（会小企01表）：左资产 / 右负债与所有者权益，年初余额 + 期末余额两列。
    同一份公式跑两个上下文：mode='begin' 取年初、mode='end' 取期末。"""
    tpl = _sys_tpl('bs')
    if not tpl:
        return [], {'kind': 'bs', 'balanced': True, 'diff': 0.0, 'assets': 0.0,
                    'liab_equity': 0.0, 'tpl': None, 'period': period,
                    'start': _cfg_start(), 'vch_count': 0}
    cb, ce = _RepCtx(period, mode='begin'), _RepCtx(period, mode='end')
    rows = []
    for r in tpl.rows_list:
        if not isinstance(r, dict):
            continue
        it = dict(r)
        it['value'] = 0.0 if r.get('head') else _eval_formula(r.get('formula'), ce.get)
        it['value_b'] = 0.0 if r.get('head') else _eval_formula(r.get('formula'), cb.get)
        rows.append(it)
    meta = _report_meta(rows, 'bs')
    meta.update({'tpl': tpl, 'period': period, 'start': _cfg_start(),
                 'vch_count': FinVoucher.query.filter_by(status='posted', period=period).count()})
    return rows, meta


def _calc_pl(period):
    """利润表（会小企02表）：本期金额 + 本年累计金额两列。
    同一份公式跑两个净发生口径：amount='bf' 本期 / amount='lf' 本年累计。"""
    tpl = _sys_tpl('pl')
    if not tpl:
        return [], {'kind': 'pl', 'revenue': 0.0, 'op_profit': 0.0, 'total_profit': 0.0,
                    'net_profit': 0.0, 'tpl': None, 'period': period,
                    'start': _cfg_start(), 'vch_count': 0}
    ctx = _RepCtx(period, exclude_closing=True)   # #36：剔除期末结转凭证，结账前后利润一致
    cur_rows = _report_rows(tpl.rows_list, ctx, amount='bf')
    ytd_rows = _report_rows(tpl.rows_list, ctx, amount='lf')
    rows = []
    for a, b in zip(cur_rows, ytd_rows):
        it = dict(a)
        it['value_b'] = b.get('value', 0.0)
        rows.append(it)
    meta = _report_meta(rows, 'pl')
    meta.update({'tpl': tpl, 'period': period, 'start': _cfg_start(),
                 'vch_count': FinVoucher.query.filter_by(status='posted', period=period).count()})
    return rows, meta


def _cf_class(code):
    """按「对方科目」判定现金流量类别：
    所有者权益类(3xx) → 筹资；固定资产 / 无形资产(16xx / 17xx) → 投资；其余 → 经营。"""
    c = (code or '').strip()
    if c.startswith(CF_FINANCE):
        return 'fin'
    if c.startswith(CF_INVEST):
        return 'inv'
    return 'oper'


def _is_cash_code(code):
    """是不是货币资金科目（库存现金 1001* / 银行存款 1002*）。"""
    return (code or '').startswith(tuple(JOURNAL_ROOTS))


def _calc_cf(p_from, p_to):
    """现金流量表（简版）：把「涉及货币资金的已记账凭证」按对方科目归类。
    为什么一定对得上：三个类别的合计 = 期间内货币资金分录的净发生额，
    而它天然等于「期末货币资金 − 期初货币资金」，这个恒等式不依赖任何人工标注。
    精确归集需要凭证分录标注「现金流量项目」（字段 cashflow_item 已预留），
    二期在凭证录入页开放标注后，本函数即升级为精确版。"""
    start = _cfg_start()
    if start and p_from and p_from < start:
        p_from = start                                  # 起点不能早于启用期，否则期初取不到
    codes, _label = _journal_accounts('all')
    rows = (db.session.query(FinVoucherEntry, FinVoucher)
            .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
            .filter(FinVoucher.status == 'posted')
            # #36：剔除期末结转系统凭证——它是账内转账，不是业务现金流，
            #      否则结转的对方科目（本年利润 3103）会被误判成"筹资活动"
            .filter(FinVoucher.source_type != 'closing')
            .filter(FinVoucher.period >= p_from, FinVoucher.period <= p_to)
            .all())
    by_v = {}
    for e, v in rows:
        by_v.setdefault(v.id, []).append((e, v))
    buckets = {'oper': 0.0, 'inv': 0.0, 'fin': 0.0}
    detail = {'oper': [], 'inv': [], 'fin': []}
    vch_count = 0
    for vid in sorted(by_v.keys()):
        es = by_v[vid]
        cash_net = _d2(sum((e.debit or 0) - (e.credit or 0) for e, _v in es
                           if _is_cash_code(e.account_code)))
        if abs(cash_net) < LEDGER_EPS:
            continue                                    # 这张凭证没动现金，不算
        vch_count += 1
        others = [(e, v) for e, v in es if not _is_cash_code(e.account_code)]
        if others:
            # 多个对方科目时，取金额最大的那个来定类别（一笔业务的主方向）
            big = max(others, key=lambda p: abs((p[0].debit or 0) - (p[0].credit or 0)))
            cls = _cf_class(big[0].account_code)
            why = '%s %s' % (big[0].account_code or '', big[0].account_name or '')
        else:
            cls, why = 'oper', '货币资金内部划转'          # 提现 / 存现这类
        buckets[cls] = _d2(buckets[cls] + cash_net)
        if len(detail[cls]) < 80:                       # 明细最多留 80 条，页面不撑爆
            v = es[0][1]
            detail[cls].append({'date': v.voucher_date, 'voucher_no': v.voucher_no,
                                'voucher_id': v.id, 'summary': v.summary or '—',
                                'amount': cash_net, 'why': why, 'is_red': bool(v.is_red)})
    net = _d2(buckets['oper'] + buckets['inv'] + buckets['fin'])
    opening = _opening_net(codes, p_from)
    closing = _d2(opening + net)
    out_rows = [
        {'label': '一、经营活动产生的现金流量净额', 'value': buckets['oper'], 'key': 'oper'},
        {'label': '二、投资活动产生的现金流量净额', 'value': buckets['inv'], 'key': 'inv'},
        {'label': '三、筹资活动产生的现金流量净额', 'value': buckets['fin'], 'key': 'fin'},
        {'label': '四、现金及现金等价物净增加额', 'value': net, 'key': 'net', 'total': True},
    ]
    meta = {'kind': 'cf', 'p_from': p_from, 'p_to': p_to,
            'oper': buckets['oper'], 'inv': buckets['inv'], 'fin': buckets['fin'],
            'net': net, 'opening': opening, 'closing': closing,
            'detail': detail, 'has_account': bool(codes), 'vch_count': vch_count,
            # 自检：按分录累加的净额，必须等于按余额算的「期末 − 期初」
            'balanced': abs(net - (_opening_net(codes, _period_add(p_to, 1)) - opening)) < LEDGER_EPS}
    return out_rows, meta


def _xlsx_response(ws_title, title, headers, data, meta_rows=None):
    """把一张报表写进 xlsx 并返回下载响应（openpyxl 属既有依赖，确认函模块已在用）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = (ws_title or '报表')[:28]
    ncol = max(len(headers), 2)
    thin = Side(style='thin', color='BFBFBF')
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    r = 1
    c = ws.cell(row=r, column=1, value=title)
    c.font = Font(bold=True, size=14)
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
    r += 1
    for line in (meta_rows or []):
        ws.cell(row=r, column=1, value=line).font = Font(size=10, color='666666')
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncol)
        r += 1
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = Font(bold=True)
        c.alignment = Alignment(horizontal='center')
        c.border = box
    r += 1
    for row in data:
        for i, val in enumerate(row, start=1):
            c = ws.cell(row=r, column=i, value=val)
            c.border = box
            if isinstance(val, float):
                c.number_format = '#,##0.00'
        r += 1
    for i in range(1, ncol + 1):
        w = 8
        for row in [list(headers)] + list(data):
            if i <= len(row):
                t = str(row[i - 1])
                w = max(w, len(t) * 2 if any('\u4e00' <= ch <= '\u9fff' for ch in t) else len(t))
        ws.column_dimensions[get_column_letter(i)].width = min(max(w + 3, 10), 40)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    name = '%s_%s.xlsx' % (title, datetime.now().strftime('%Y%m%d_%H%M'))
    return send_file(bio, as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _who():
    """当前登录人的显示名（报表创建人 / 保存人留痕用）。"""
    return session.get('name') or session.get('username') or '—'


def _rep_period():
    """报表默认看哪一期：URL 带的 → 最近有已记账凭证的期间 → 启用期间 → 本月。"""
    return (request.args.get('period') or '').strip() or _default_period()


@bp.route('/fin/reports', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_hub():
    """报表中心：三张表的入口 + 报表级自检。
    一进页面先回答三个问题：账平不平、本期赚没赚、现金净增了多少。"""
    period = _rep_period()
    _b, m_bs = _calc_bs(period)
    _p, m_pl = _calc_pl(period)
    _c, m_cf = _calc_cf(period, period)
    custom = (FinReportTemplate.query.filter_by(category='custom')
              .order_by(FinReportTemplate.id.desc()).all())
    return render_template('fin/report_hub.html', cfg=_cfg(), period=period, start=_cfg_start(),
                           m_bs=m_bs, m_pl=m_pl, m_cf=m_cf, custom=custom,
                           func_cn=REPORT_FUNC_CN, can_design=_has_perm('fin.report.design'))


@bp.route('/fin/reports/bs', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_bs():
    """资产负债表（会小企01表）：左资产 / 右负债与所有者权益，年初余额 + 期末余额两列。
    这张表是「账做对了没有」的最终裁判：资产 = 负债 + 所有者权益，不平就说明哪儿错了。"""
    period = _rep_period()
    rows, meta = _calc_bs(period)
    return render_template('fin/report_bs.html', cfg=_cfg(), period=period, start=_cfg_start(),
                           left=[r for r in rows if r.get('side') != 'right'],
                           right=[r for r in rows if r.get('side') == 'right'], meta=meta)


@bp.route('/fin/reports/pl', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_pl():
    """利润表（会小企02表）：本期金额 + 本年累计金额。"""
    period = _rep_period()
    rows, meta = _calc_pl(period)
    return render_template('fin/report_pl.html', cfg=_cfg(), period=period, start=_cfg_start(),
                           rows=rows, meta=meta)


@bp.route('/fin/reports/cf', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_cf():
    """现金流量表（简版）：经营 / 投资 / 筹资三分类 + 现金净增加额。"""
    period = _rep_period()
    start = _cfg_start()
    p_from = (request.args.get('p_from') or '').strip() or period
    p_to = (request.args.get('p_to') or '').strip() or period
    if start and p_from and p_from < start:
        p_from = start
    if p_from and p_to and p_from > p_to:
        p_from, p_to = p_to, p_from
    rows, meta = _calc_cf(p_from, p_to)
    return render_template('fin/report_cf.html', cfg=_cfg(), rows=rows, meta=meta,
                           p_from=p_from, p_to=p_to, start=start)


@bp.route('/fin/reports/t/<int:tid>', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_custom(tid):
    """自定义报表（内置模板的行公式也在这里查看）。
    单列渲染：本期金额 / 本年累计金额；有改公式权限的人可直接在页面上编辑。"""
    tpl = FinReportTemplate.query.get(tid)
    if not tpl:
        abort(404)
    period = _rep_period()
    ctx = _RepCtx(period)
    cur_rows = _report_rows(tpl.rows_list, ctx, amount='bf')
    ytd_rows = _report_rows(tpl.rows_list, ctx, amount='lf')
    rows = []
    for a, b in zip(cur_rows, ytd_rows):
        it = dict(a)
        it['value_b'] = b.get('value', 0.0)
        rows.append(it)
    return render_template('fin/report_custom.html', cfg=_cfg(), tpl=tpl, rows=rows,
                           period=period, start=_cfg_start(), func_cn=REPORT_FUNC_CN,
                           can_design=_has_perm('fin.report.design'))


@bp.route('/fin/reports/new', methods=['POST'])
@login_required
@permission_required('fin.report.design')
def report_new():
    """新建一张空白自定义报表，建好直接进编辑页。"""
    name = (request.form.get('name') or '').strip() or '未命名报表'
    rows = [{'label': '行项目一', 'formula': 'QM(1001)'},
            {'label': '行项目二', 'formula': 'QM(1002)'}]
    tpl = FinReportTemplate(name=name, category='custom',
                            rows=json.dumps(rows, ensure_ascii=False), is_system=False,
                            created_by=_who(), note='自定义')
    db.session.add(tpl)
    db.session.commit()
    flash('已新建报表「%s」，可直接改公式' % name)
    return redirect(url_for('fin.report_custom', tid=tpl.id))


@bp.route('/fin/reports/t/<int:tid>/save', methods=['POST'])
@login_required
@permission_required('fin.report.design')
def report_save(tid):
    """保存行公式。内置模板不允许就地改——那是系统的标准报表，改了别人就对不上口径；
    系统自动另存成一条新的自定义报表，内置的保持原样。"""
    tpl = FinReportTemplate.query.get(tid)
    if not tpl:
        abort(404)
    name = (request.form.get('name') or '').strip() or tpl.name
    labels = request.form.getlist('label[]')
    formulas = request.form.getlist('formula[]')
    lines = request.form.getlist('line[]')
    notes = request.form.getlist('note[]')
    sides = request.form.getlist('side[]')
    totals = request.form.getlist('total[]')
    rows = []
    for i, lb in enumerate(labels):
        lb = (lb or '').strip()
        if not lb:
            continue
        rows.append({
            'label': lb,
            'formula': (formulas[i] if i < len(formulas) else '').strip(),
            'line': (lines[i] if i < len(lines) else '').strip(),
            'note': (notes[i] if i < len(notes) else '').strip(),
            'side': (sides[i] if i < len(sides) else ''),
            'total': (totals[i] if i < len(totals) else '') == '1',
            'head': lb.endswith('：') or lb.endswith(':'),   # 以冒号结尾 = 分类小标题
        })
    if not rows:
        flash('报表至少要有一行')
        return redirect(url_for('fin.report_custom', tid=tid))
    payload = json.dumps(rows, ensure_ascii=False)
    if tpl.is_system:
        new = FinReportTemplate(name='%s（自定义）' % name, category='custom', rows=payload,
                                is_system=False, created_by=_who(),
                                note='由内置「%s」另存' % tpl.name)
        db.session.add(new)
        db.session.commit()
        flash('内置报表不可就地修改，已另存为「%s」' % new.name)
        return redirect(url_for('fin.report_custom', tid=new.id))
    tpl.name, tpl.rows = name, payload
    db.session.commit()
    flash('报表「%s」已保存' % name)
    return redirect(url_for('fin.report_custom', tid=tpl.id))


@bp.route('/fin/reports/t/<int:tid>/delete', methods=['POST'])
@login_required
@permission_required('fin.report.design')
def report_delete(tid):
    """删除自定义报表。内置模板不允许删除（那是系统的标准报表）。"""
    tpl = FinReportTemplate.query.get(tid)
    if not tpl:
        abort(404)
    if tpl.is_system:
        flash('「%s」是系统内置报表，不能删除' % tpl.name)
        return redirect(url_for('fin.report_custom', tid=tid))
    name = tpl.name
    db.session.delete(tpl)
    db.session.commit()
    flash('已删除报表「%s」' % name)
    return redirect(url_for('fin.report_hub'))


@bp.route('/fin/reports/export', methods=['GET'])
@login_required
@permission_required('fin.report.view')
def report_export():
    """把报表导出成 Excel。kind = bs / pl / cf / custom（custom 需带 tid）。"""
    kind = (request.args.get('kind') or 'bs').strip()
    period = _rep_period()
    log_audit('export', module='fin', target_type='report', target_no=kind, detail='导出报表 %s' % kind)
    if kind == 'bs':
        rows, meta = _calc_bs(period)
        left = [r for r in rows if r.get('side') != 'right']
        right = [r for r in rows if r.get('side') == 'right']
        data = []
        for i in range(max(len(left), len(right))):
            a = left[i] if i < len(left) else {}
            b = right[i] if i < len(right) else {}
            data.append([a.get('line', ''), a.get('label', ''), a.get('value_b'), a.get('value'),
                         b.get('line', ''), b.get('label', ''), b.get('value_b'), b.get('value')])
        headers = ['行次', '资产', '年初余额', '期末余额',
                   '行次', '负债和所有者权益', '年初余额', '期末余额']
        title = '资产负债表'
        meta_rows = ['会计期间：%s' % period,
                     '资产总计 %s ｜ 负债和所有者权益总计 %s ｜ %s'
                     % ('{:,.2f}'.format(meta.get('assets', 0.0)),
                        '{:,.2f}'.format(meta.get('liab_equity', 0.0)),
                        '平衡' if meta.get('balanced')
                        else '不平衡，差额 %s' % '{:,.2f}'.format(meta.get('diff', 0.0)))]
    elif kind == 'pl':
        rows, meta = _calc_pl(period)
        data = [[r.get('line', ''), r.get('label', ''), r.get('value'), r.get('value_b')]
                for r in rows]
        headers = ['行次', '项目', '本期金额', '本年累计金额']
        title = '利润表'
        meta_rows = ['会计期间：%s' % period,
                     '本期净利润 %s' % '{:,.2f}'.format(meta.get('net_profit', 0.0))]
    elif kind == 'cf':
        rows, meta = _calc_cf(period, period)
        data = [[r.get('label', ''), r.get('value')] for r in rows]
        data.append(['期初现金及现金等价物余额', meta.get('opening', 0.0)])
        data.append(['期末现金及现金等价物余额', meta.get('closing', 0.0)])
        headers = ['项目', '本期金额']
        title = '现金流量表简版'
        meta_rows = ['会计期间：%s' % period,
                     '现金净增加额 %s' % '{:,.2f}'.format(meta.get('net', 0.0))]
    else:
        tpl = FinReportTemplate.query.get(request.args.get('tid', type=int) or 0)
        if not tpl:
            abort(404)
        ctx = _RepCtx(period)
        a_rows = _report_rows(tpl.rows_list, ctx, amount='bf')
        b_rows = _report_rows(tpl.rows_list, ctx, amount='lf')
        data = [[r.get('line', ''), r.get('label', ''), r.get('value'), b_rows[i].get('value')]
                for i, r in enumerate(a_rows)]
        headers = ['行次', '项目', '本期金额', '本年累计金额']
        title = tpl.name
        meta_rows = ['会计期间：%s' % period]
    return _xlsx_response(title, title, headers, data, meta_rows)


# =====================================================================
# FMS · 期末结转与结账（增量 #36）—— 财务主线最小闭环的最后一站
# ---------------------------------------------------------------------
# 一期账务的完整闭环：期初建账 → 录凭证 → 审核记账 → 账簿 → 报表 → **期末结转结账**。
#
# 结账做两件事：
#   ① 结转损益：把损益类科目（category='profit'）本期的净发生额转到「本年利润」(3103)，
#      生成一张系统凭证。转出方向按科目余额方向来：
#        · 收入类（贷方科目，本期贷方净额）→ 借 收入科目 / 贷 3103
#        · 费用类（借方科目，本期借方净额）→ 借 3103 / 贷 费用科目
#      两段合起来借贷天然相等，3103 的贷方净增加就是本期净利润。
#      结转后损益类科目本期余额归零，下期从 0 开始重新累计。
#   ② 锁定期间：该期间置为「已结账」，此后这一期的凭证不能新增/修改/删除/审核/记账/
#      作废/红冲（见 _period_locked），从机制上防止事后改账。
#
# 口径（与账簿 #34 / 报表 #35 保持一致）：
#   · 只认 status='posted'（已记账）的凭证；草稿、已审核、已作废都不参与结转；
#   · 结转金额 = 本期净发生额（本期借 − 本期贷），**不含期初**——
#     损益类科目在期初建账时本就该是平的（上年损益已结平），不该带余额进场；
#   · 必须**按期顺序结账**：上一期没结账，这一期不许结（跳期结账会让利润串档）。
#
# 为什么结转凭证不建审核流：
#   它的每一分钱都是系统按账上的数算出来的，没有人录入的空间，不存在"录错"；
#   而"执行结账"这个动作本身由有 fin.closing.manage 权限的财务主管发起，等价于审核。
#   所以凭证直接落成「已记账」，制单记「系统（期末结转）」、审核人记操作人，
#   既不让"制单≠审核"卡住自己，追溯链也完整。要改？走「反结账」。
# =====================================================================

PROFIT_EPS = 0.005                 # 损益结转的金额容差（半分钱以内视为 0，视同无损益）
CLOSING_ACCOUNT = '3103'           # 本年利润
CLOSING_SUMMARY = '结转本期损益'    # 结转凭证摘要前缀


def _period_row(period):
    """取某期间的期间记录；库里没有就现场建一条（open 状态）——期间记录按需创建。"""
    period = (period or '').strip()
    if not period:
        return None
    p = FinPeriod.query.filter_by(period=period).first()
    if not p:
        p = FinPeriod(period=period, status='open')
        db.session.add(p)
        db.session.flush()
    return p


def _period_locked(period):
    """该期间是否已结账（结账后期间锁定，凭证一律不能再动）。"""
    period = (period or '').strip()
    if not period:
        return False
    p = FinPeriod.query.filter_by(period=period).first()
    return bool(p and p.status == 'closed')


def _closed_periods():
    """已结账期间集合，页面批量判断用（避免逐条查库）。"""
    return {p.period for p in FinPeriod.query.filter_by(status='closed').all()}


def _profit_leaf_accounts():
    """损益类明细科目（只有明细科目能做分录）。"""
    accs = FinAccount.query.filter_by(category='profit').all()
    return [a for a in accs if a.is_leaf]


def _closing_plan(period):
    """待结转明细：[(科目编码, 科目名称, 方向, 金额, 净额)]。
    金额 = 本期净发生额（借 − 贷）的绝对值；方向 = 该科目本期净额的借贷方向。
    净额为正表示借方净发生（费用性质），为负表示贷方净发生（收入性质）。"""
    accs = _profit_leaf_accounts()
    codes = [a.code for a in accs]
    if not codes:
        return []
    flows = _posted_flows(period, period, codes,
                          exclude_closing=True)     # 口径与利润表一致（不含结转凭证本身）
    name_of = {a.code: a.name for a in accs}
    plan = []
    for code in codes:
        d, c = flows.get(code, [0.0, 0.0])
        net = _d2(d - c)
        if abs(net) < PROFIT_EPS:
            continue                                   # 本期没有发生额，不参与结转
        plan.append({'code': code, 'name': name_of.get(code, ''),
                     'direction': 'debit' if net > 0 else 'credit',
                     'amount': abs(net), 'net': net})
    plan.sort(key=lambda x: x['code'])
    return plan


def _closing_preview(period):
    """结转预览：算出这张结转凭证会长什么样（借贷合计、本年利润净额），页面与落库共用。"""
    plan = _closing_plan(period)
    # 收入段：借 收入科目 / 贷 3103；费用段：借 3103 / 贷 费用科目
    debit_to_3103 = _d2(sum(p['amount'] for p in plan if p['net'] > 0))    # 费用转出 → 3103 在借方
    credit_to_3103 = _d2(sum(p['amount'] for p in plan if p['net'] < 0))   # 收入转出 → 3103 在贷方
    profit = _d2(credit_to_3103 - debit_to_3103)                          # >0 盈利 / <0 亏损
    return {
        'plan': plan,
        'income_total': credit_to_3103,     # 收入类转出合计
        'expense_total': debit_to_3103,     # 费用类转出合计
        'profit': profit,
        # 这张凭证的借贷合计 = 收入段 + 费用段（两段各自借贷相等，整体必然平衡）
        'amount': _d2(credit_to_3103 + debit_to_3103),
        'lines': len(plan) + (2 if plan else 0),
    }


def _closing_check(period):
    """结账前置检查 → (blockers, warnings)。blockers 非空就不许结账。"""
    blockers, warns = [], []
    cfg = _cfg()
    start = _cfg_start()
    if not cfg.opening_done:
        blockers.append('还没完成期初建账，先到「期初建账」确认建账')
    if not period:
        blockers.append('请选择要结账的会计期间')
        return blockers, warns
    if start and period < start:
        blockers.append('%s 早于账套启用期间 %s，不能结账' % (period, start))
    prev = _period_add(period, -1)
    if prev and start and prev >= start and not _period_locked(prev):
        blockers.append('上一期 %s 还没结账。会计期间必须按期顺序结，跳期结账会让利润串档' % prev)
    # 本期未记账的凭证必须处理完（草稿先删或审、已审核先记账），否则这期结不干净
    draft = FinVoucher.query.filter_by(period=period, status='draft').count()
    audited = FinVoucher.query.filter_by(period=period, status='audited').count()
    if draft:
        blockers.append('本期还有 %d 张草稿凭证，请先审核记账或删除' % draft)
    if audited:
        blockers.append('本期还有 %d 张已审核未记账的凭证，请先记账' % audited)
    if not _profit_leaf_accounts():
        warns.append('科目表里没有损益类明细科目，本期不产生结转凭证（只做期间锁定）')
    return blockers, warns


def _close_period(period, who):
    """执行结账：生成结转损益凭证（直接记账）+ 期间置为已结账。返回结转凭证或 None。"""
    preview = _closing_preview(period)
    voucher = None
    if preview['plan']:
        # —— 生成结转凭证 ——
        # 字别用「转」；账套里恰巧把「转」去掉时退用第一个字别，保证不造出非法字别的凭证
        words = _voucher_words()
        word = '转' if '转' in words else words[0]
        v = FinVoucher(word=word, period=period,
                       voucher_date=_period_last_day(period),
                       attach_count=0, status='posted',
                       source_type='closing',
                       summary='%s %s，结转本年利润' % (CLOSING_SUMMARY, period),
                       maker_id=None, maker_name='系统（期末结转）',
                       checker_id=session.get('user_id'), checker_name=who,
                       checked_at=datetime.utcnow(), posted_at=datetime.utcnow())
        v.voucher_no, v.seq = _next_voucher_no(word, period)
        seq = 0
        # 段一：收入类转出 —— 借 收入科目 / 贷 本年利润
        for p in preview['plan']:
            if p['net'] >= 0:
                continue
            seq += 1
            v.entries.append(FinVoucherEntry(
                seq=seq, summary='结转收入至本年利润',
                account_code=p['code'], account_name=p['name'],
                debit=p['amount'], credit=0.0))
        if preview['income_total']:
            seq += 1
            v.entries.append(FinVoucherEntry(
                seq=seq, summary='结转收入至本年利润',
                account_code=CLOSING_ACCOUNT, account_name=_acct_name(CLOSING_ACCOUNT),
                debit=0.0, credit=preview['income_total']))
        # 段二：费用类转出 —— 借 本年利润 / 贷 费用科目
        if preview['expense_total']:
            seq += 1
            v.entries.append(FinVoucherEntry(
                seq=seq, summary='结转费用至本年利润',
                account_code=CLOSING_ACCOUNT, account_name=_acct_name(CLOSING_ACCOUNT),
                debit=preview['expense_total'], credit=0.0))
        for p in preview['plan']:
            if p['net'] <= 0:
                continue
            seq += 1
            v.entries.append(FinVoucherEntry(
                seq=seq, summary='结转费用至本年利润',
                account_code=p['code'], account_name=p['name'],
                debit=0.0, credit=p['amount']))
        v.amount_debit = _d2(sum(e.debit or 0 for e in v.entries))
        v.amount_credit = _d2(sum(e.credit or 0 for e in v.entries))
        v.updated_at = datetime.utcnow()
        db.session.add(v)
        db.session.flush()
        voucher = v
    # —— 期间置为已结账 ——
    row = _period_row(period)
    row.status = 'closed'
    row.closed_at = datetime.utcnow()
    row.closed_by = who
    row.closing_voucher_id = voucher.id if voucher else None
    row.closing_voucher_no = voucher.voucher_no if voucher else None
    row.profit_amount = preview['profit']
    row.note = ('本期无损益发生，仅锁定期间' if not voucher else '')
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return voucher


def _reopen_period(row, who):
    """反结账：删除结转凭证 + 期间回 open。返回 (是否成功, 提示语)。
    反结账只能从**最靠后的已结账期间**开始往回退 —— 后面的期还结着，前面就先打开，
    会让两期之间的利润重叠计算。"""
    later = FinPeriod.query.filter(FinPeriod.period > row.period,
                                   FinPeriod.status == 'closed').first()
    if later:
        return False, ('后面还有已结账的期间（%s），请先反结账后面的期间，再往前退'
                       % later.period)
    # 结转凭证是系统凭证：反结账时直接撤掉，而不是留一张红字（红冲会把它计入下期损益）
    if row.closing_voucher_id:
        v = FinVoucher.query.get(row.closing_voucher_id)
        if v:
            db.session.delete(v)
    row.status = 'open'
    row.closing_voucher_id = None
    row.closing_voucher_no = None
    row.profit_amount = 0
    row.reopen_at = datetime.utcnow()
    row.reopen_by = who
    row.reopen_count = (row.reopen_count or 0) + 1
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return True, None


def _period_last_day(period):
    """取某期间的最后一天（结转凭证默认落在期末日），如 2026-02 → 2026-02-28。"""
    p = (period or '').strip()
    if len(p) < 7 or not p[:4].isdigit() or not p[5:7].isdigit():
        return datetime.now().date()
    y, m = int(p[:4]), int(p[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(ny, nm, 1) - timedelta(days=1)


def _acct_name(code):
    """取科目名称（结转凭证写分录快照用），科目不存在时回退编码本身。"""
    a = FinAccount.query.filter_by(code=code).first()
    return a.name if a else code


def _period_range():
    """可结账的期间列表：从账套启用期间 → 最近有凭证的期间（含当期），倒序，供页面台账。"""
    start = _cfg_start()
    end = _default_period()
    if not start or not end or end < start:
        return []
    out, cur, guard = [], end, 0
    while cur >= start and guard < 600:               # guard 防极端值把页面拖死（50 年足够）
        out.append(cur)
        cur = _period_add(cur, -1)
        guard += 1
    return out


def _closing_rows():
    """结账台账：每个期间的凭证情况 + 结账状态，页面直接渲染。"""
    closed = {p.period: p for p in FinPeriod.query.filter_by(status='closed').all()}
    rows = []
    for period in _period_range():
        posted = FinVoucher.query.filter_by(period=period, status='posted').count()
        pending = FinVoucher.query.filter_by(period=period).filter(
            FinVoucher.status.in_(('draft', 'audited'))).count()
        flows = _posted_flows(period, period, exclude_closing=True)
        # 本期损益净发生（收入为正、费用为负，两者相加 = 本期利润）
        income = expense = 0.0
        for a in _profit_leaf_accounts():
            d, c = flows.get(a.code, [0.0, 0.0])
            net = _d2(c - d) if a.direction == 'credit' else _d2(d - c)
            if a.direction == 'credit':
                income += net
            else:
                expense += net
        p = closed.get(period)
        rows.append({
            'period': period,
            'period_cn': (p or FinPeriod(period=period)).period_cn,
            'is_closed': bool(p),
            'closed_by': p.closed_by if p else '',
            'closed_at': p.closed_at if p else None,
            'closing_voucher_no': p.closing_voucher_no if p else '',
            'profit_amount': (p.profit_amount or 0) if p else _d2(income - expense),
            'reopen_count': (p.reopen_count or 0) if p else 0,
            'posted_count': posted,
            'pending_count': pending,
            'income': _d2(income),
            'expense': _d2(expense),
            'profit': _d2(income - expense),
        })
    return rows


@bp.route('/fin/closing', methods=['GET'])
@login_required
@permission_required('fin.voucher.view')
def closing():
    """期末结转与结账：结转预览 + 结账前检查清单 + 期间台账（反结账入口）。"""
    cfg = _cfg()
    period = (request.args.get('period') or '').strip() or _default_period()
    pickable = _period_range()
    if period not in pickable and pickable:
        period = pickable[0]
    if not period:
        period = _cfg_start() or datetime.now().strftime('%Y-%m')
    row = FinPeriod.query.filter_by(period=period).first()
    preview = _closing_preview(period)
    blockers, warns = _closing_check(period)
    return render_template('fin/closing.html', cfg=cfg, period=period,
                           period_cn=(row or FinPeriod(period=period)).period_cn,
                           row=row, locked=_period_locked(period),
                           preview=preview, blockers=blockers, warns=warns,
                           rows=_closing_rows(), pickable=pickable,
                           can_manage=_has_perm('fin.closing.manage'),
                           closing_acct=_acct_name(CLOSING_ACCOUNT))


@bp.route('/fin/closing/close', methods=['POST'])
@login_required
@permission_required('fin.closing.manage')
def closing_close():
    """执行结账：生成结转损益凭证 + 锁定期间。前置检查不过一律拒绝。"""
    period = (request.form.get('period') or '').strip()
    back = url_for('fin.closing', period=period)
    if _period_locked(period):
        flash('%s 已经结账了，不用重复结账' % period)
        return redirect(back)
    blockers, _warns = _closing_check(period)
    if blockers:
        for b in blockers:
            flash('不能结账：' + b)
        return redirect(back)
    who = session.get('username') or ''
    profit = _closing_preview(period)['profit']
    v = _close_period(period, who)
    if v:
        flash('%s 已结账：已生成结转凭证 %s 并直接记账，损益类科目本期余额已结平，'
              '本年利润 %s %s。该期间凭证已锁定'
              % (period, v.voucher_no, _fmt(abs(profit)), '（盈利）' if profit >= 0 else '（亏损）'))
        log_audit('close', module='fin', target_type='period', target_no=period, detail='结账并生成结转凭证 %s' % v.voucher_no)
    else:
        flash('%s 已结账：本期没有损益发生额，未生成结转凭证，仅锁定该期间' % period)
        log_audit('close', module='fin', target_type='period', target_no=period, detail='结账（无损益，仅锁定）')
    return redirect(back)


@bp.route('/fin/closing/reopen', methods=['POST'])
@login_required
@permission_required('fin.closing.manage')
def closing_reopen():
    """反结账：撤销结账 + 删除结转凭证，期间回到可编辑状态（从最后一期往回退）。"""
    period = (request.form.get('period') or '').strip()
    back = url_for('fin.closing', period=period)
    row = FinPeriod.query.filter_by(period=period).first()
    if not row or row.status != 'closed':
        flash('%s 没有结账记录，不需要反结账' % period)
        return redirect(back)
    ok, msg = _reopen_period(row, session.get('username') or '')
    if ok:
        flash('%s 已反结账：结转凭证已撤销，该期间恢复可编辑。'
              '（第 %d 次反结账，操作留痕）' % (period, row.reopen_count or 0))
        log_audit('reopen', module='fin', target_type='period', target_no=period, detail='反结账（第 %d 次）' % (row.reopen_count or 0))
    else:
        flash('不能反结账：' + msg)
    return redirect(back)


# =====================================================================
# FMS · 发票台账（增量 #37）：进项/销项台账 + 认证抵扣 + 增值税测算
# =====================================================================

# 发票页面下拉项（与 models.FinInvoice 对应）
INVOICE_DIR_OPTIONS = [('in', '进项（收票）'), ('out', '销项（开票）')]
INVOICE_CERT_OPTIONS = [('uncert', '未认证'), ('certed', '已认证'), ('nodeduct', '不予抵扣')]


def _parse_date(s):
    """解析 YYYY-MM-DD / YYYY/M/D 等常见日期；非法返回 None（复用账务模块习惯）。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _invoice_parse(form):
    """从表单解析一张发票的字段（新增/编辑共用）。返回 (属性字典, 错误信息)。"""
    direction = (form.get('direction') or 'in').strip()
    if direction not in ('in', 'out'):
        direction = 'in'
    try:                                           # 税率：用户填百分比（如 6），存小数（0.06）
        rate = float(form.get('tax_rate') or 0) / 100.0
    except (ValueError, TypeError):
        rate = 0.0
    try:
        amount = float(form.get('amount') or 0)
    except (ValueError, TypeError):
        amount = 0.0
    tax_input = (form.get('tax') or '').strip()
    try:
        tax = float(tax_input) if tax_input else 0.0
    except (ValueError, TypeError):
        tax = 0.0
    if not tax_input:                             # 没手填税额就按 金额×税率 自动算
        tax = round(amount * rate, 2)
    total = round(amount + tax, 2)
    idate = _parse_date(form.get('invoice_date') or '')
    period = (form.get('period') or '').strip()
    if not period and idate:
        period = idate.strftime('%Y-%m')
    if direction != 'in':                         # 销项没有认证概念
        cert_status, cert_period = 'uncert', None
    else:
        cert_status = (form.get('cert_status') or 'uncert').strip()
        cert_period = (form.get('cert_period') or '').strip() or period
    return {
        'direction': direction,
        'invoice_code': (form.get('invoice_code') or '').strip(),
        'invoice_no': (form.get('invoice_no') or '').strip(),
        'invoice_type': (form.get('invoice_type') or '数电票').strip(),
        'invoice_date': idate,
        'period': period,
        'seller_name': (form.get('seller_name') or '').strip(),
        'buyer_name': (form.get('buyer_name') or '').strip(),
        'amount': amount,
        'tax_rate': rate,
        'tax': tax,
        'total': total,
        'cert_status': cert_status,
        'cert_period': cert_period,
        'remark': (form.get('remark') or '').strip(),
    }, None


@bp.route('/fin/invoices', methods=['GET'])
@login_required
@permission_required('fin.invoice.manage')
def invoices():
    """发票台账：列表 + 进/销筛选 + 所属期筛选 + 关键字。"""
    direction = (request.args.get('dir') or 'in').strip()
    if direction not in ('in', 'out'):
        direction = 'in'
    period = (request.args.get('period') or '').strip()
    q = (request.args.get('q') or '').strip()
    query = FinInvoice.query.filter_by(direction=direction)
    if period:
        query = query.filter_by(period=period)
    if q:
        like = '%%%s%%' % q
        query = query.filter(db.or_(FinInvoice.invoice_no.like(like),
                                    FinInvoice.seller_name.like(like),
                                    FinInvoice.buyer_name.like(like),
                                    FinInvoice.remark.like(like)))
    items = query.order_by(FinInvoice.invoice_date.desc(), FinInvoice.id.desc()).all()
    sums = {'cnt': len(items), 'amount': 0.0, 'tax': 0.0, 'total': 0.0}
    for it in items:
        sums['amount'] += it.amount or 0
        sums['tax'] += it.tax or 0
        sums['total'] += it.total or 0
    periods = [r[0] for r in db.session.query(FinInvoice.period)
               .filter(FinInvoice.period.isnot(None)).distinct()
               .order_by(FinInvoice.period.desc()).all()]
    return render_template('fin/invoices.html', items=items, sums=sums,
                           direction=direction, period=period, q=q, periods=periods,
                           dir_cn=dict(INVOICE_DIR_OPTIONS).get(direction))


@bp.route('/fin/invoices/new', methods=['GET'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_new():
    """新增发票表单（GET 给空表单）。"""
    direction = (request.args.get('dir') or 'in').strip()
    if direction not in ('in', 'out'):
        direction = 'in'
    blank = {'direction': direction, 'invoice_type': '数电票', 'cert_status': 'uncert',
             'tax_rate': 6, 'invoice_date': date.today().strftime('%Y-%m-%d')}
    return render_template('fin/invoice_form.html', inv=None, blank=blank,
                           direction=direction, is_edit=False)


@bp.route('/fin/invoices/save', methods=['POST'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_save():
    """保存发票（新增或编辑，靠 iid 区分）。"""
    iid = request.form.get('iid') or ''
    iid = int(iid) if iid.isdigit() else None
    data, err = _invoice_parse(request.form)
    if err:
        flash(err)
        return redirect(url_for('fin.invoices'))
    if iid:
        inv = FinInvoice.query.get(iid)
        if not inv:
            flash('发票不存在')
            return redirect(url_for('fin.invoices'))
    else:
        inv = FinInvoice()
        inv.created_by = session.get('username') or ''
    for k, v in data.items():
        setattr(inv, k, v)
    db.session.add(inv)
    db.session.commit()
    flash('发票已保存：%s %s，价税合计 %.2f' % (inv.direction_cn, inv.invoice_no or '—', inv.total))
    log_audit('add', module='fin', target_type='invoice', target_id=inv.id, target_no=inv.invoice_no, detail='登记发票 %s' % inv.direction_cn)
    return redirect(url_for('fin.invoices', dir=inv.direction))


@bp.route('/fin/invoices/<int:iid>', methods=['GET'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_detail(iid):
    """发票详情（只读展示，改走编辑页）。"""
    inv = FinInvoice.query.get(iid)
    if not inv:
        flash('发票不存在')
        return redirect(url_for('fin.invoices'))
    return render_template('fin/invoice_form.html', inv=inv, blank=None,
                           direction=inv.direction, is_edit=True)


@bp.route('/fin/invoices/<int:iid>/delete', methods=['POST'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_delete(iid):
    """删除发票（台账数据，非凭证，直接删）；已关联凭证的不许删。"""
    inv = FinInvoice.query.get(iid)
    if not inv:
        flash('发票不存在')
        return redirect(url_for('fin.invoices'))
    if inv.voucher_id:
        flash('%s 已关联记账凭证（#38 自动记账生成），请先处理关联凭证再删除' % inv.invoice_no)
        return redirect(url_for('fin.invoices', dir=inv.direction))
    d = inv.direction
    db.session.delete(inv)
    db.session.commit()
    flash('已删除发票 %s' % inv.invoice_no)
    return redirect(url_for('fin.invoices', dir=d))


@bp.route('/fin/invoices/<int:iid>/cert', methods=['POST'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_cert(iid):
    """进项认证 / 取消认证 / 标记不予抵扣（仅进项有意义）。"""
    inv = FinInvoice.query.get(iid)
    if not inv:
        flash('发票不存在')
        return redirect(url_for('fin.invoices'))
    if inv.direction != 'in':
        flash('只有进项发票需要认证')
        return redirect(url_for('fin.invoices', dir=inv.direction))
    act = (request.form.get('act') or 'cert').strip()
    if act == 'cert':
        inv.cert_status = 'certed'
        inv.cert_period = (request.form.get('cert_period') or inv.period or '').strip()
    elif act == 'uncert':
        inv.cert_status = 'uncert'
        inv.cert_period = None
    elif act == 'nodeduct':
        inv.cert_status = 'nodeduct'
        inv.cert_period = None
    db.session.commit()
    flash('进项发票 %s 已设为「%s」' % (inv.invoice_no, inv.cert_cn))
    log_audit('update', module='fin', target_type='invoice', target_id=inv.id, target_no=inv.invoice_no, detail='发票认证状态→%s' % inv.cert_cn)
    return redirect(url_for('fin.invoices', dir='in'))


def _vat_summary(to_period):
    """增值税测算（累计口径，从账套启用期滚到 to_period）：
    逐期算 销项税额 − 已认证进项税额 − 期初留抵 = 本期应缴；期末留抵 = max(0, 期初留抵+已认证进项−销项)。
    返回 (逐期明细列表, 期末留抵余额)。认证抵扣用 cert_period（认证所属期），不是开票期。"""
    start = _cfg_start() or to_period
    periods, p = [], start
    while p <= to_period:
        periods.append(p)
        p = _period_add(p, 1)
    rows, carry = [], 0.0                     # carry = 期初留抵（跨期滚动）
    for p in periods:
        out_tax = db.session.query(db.func.sum(FinInvoice.tax)).filter(
            FinInvoice.direction == 'out', FinInvoice.period == p).scalar() or 0.0
        in_certed = db.session.query(db.func.sum(FinInvoice.tax)).filter(
            FinInvoice.direction == 'in', FinInvoice.cert_status == 'certed',
            FinInvoice.cert_period == p).scalar() or 0.0
        in_uncert = db.session.query(db.func.sum(FinInvoice.tax)).filter(
            FinInvoice.direction == 'in', FinInvoice.cert_status == 'uncert',
            FinInvoice.period == p).scalar() or 0.0
        in_noded = db.session.query(db.func.sum(FinInvoice.tax)).filter(
            FinInvoice.direction == 'in', FinInvoice.cert_status == 'nodeduct',
            FinInvoice.period == p).scalar() or 0.0
        payable = max(0.0, out_tax - in_certed - carry)     # 本期实际应缴（扣掉期初留抵）
        carry_next = max(0.0, carry + in_certed - out_tax)  # 期末留抵
        rows.append({'period': p, 'out_tax': out_tax, 'in_certed': in_certed,
                     'in_uncert': in_uncert, 'in_noded': in_noded,
                     'payable': payable, 'carry': carry_next})
        carry = carry_next
    return rows, carry


@bp.route('/fin/invoices/vat', methods=['GET'])
@login_required
@permission_required('fin.invoice.manage')
def invoice_vat():
    """增值税测算页：累计显示各期销项、已认证进项、应缴、期末留抵。"""
    to_period = (request.args.get('period') or '').strip() or (_cfg_start() or '')
    rows, carry = _vat_summary(to_period)
    tot = {'out_tax': sum(r['out_tax'] for r in rows),
           'in_certed': sum(r['in_certed'] for r in rows),
           'in_uncert': sum(r['in_uncert'] for r in rows),
           'in_noded': sum(r['in_noded'] for r in rows),
           'payable': sum(r['payable'] for r in rows),
           'carry': carry}
    periods = [r[0] for r in db.session.query(FinInvoice.period)
               .filter(FinInvoice.period.isnot(None)).distinct()
               .order_by(FinInvoice.period.desc()).all()]
    periods = [to_period] + [x for x in periods if x != to_period]
    return render_template('fin/invoice_vat.html', rows=rows, tot=tot,
                           to_period=to_period, periods=periods)


# =====================================================================
# FMS · 费用结算确认函（增量 #41）
# ---------------------------------------------------------------------
# 业务流程：财务导入《费用结算数据》Excel → 系统四表勾稽校验 → 逐人批量生成 PDF
#           → 线下签署后上传扫描件归档；二期对接微信小程序做实名核验 + 电子签名。
# 归属：FMS 财务管理系统（确认函是费用结算的凭据，与佣金/工资凭证明细天然相邻）。
# 目录约定：文件统一放 static/uploads/confirm/ 下，分 src（导入原表）/ pdf（生成件）/ signed（签回件）。
# =====================================================================

CONFIRM_BASE = 'uploads/confirm'


def _conf_dir(*parts):
    """确认函文件目录（static/uploads/confirm/…），返回绝对路径并确保已创建。"""
    p = os.path.join(current_app.static_folder, CONFIRM_BASE, *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _safe_name(name):
    """文件名净化：去掉路径分隔符与引号，避免路径穿越。"""
    t = (name or '').strip().replace('\\', '_').replace('/', '_')
    for ch in ['"', "'", ':', '*', '?', '<', '>', '|', '\n', '\r', '\t']:
        t = t.replace(ch, '')
    return t or 'unnamed'


def _confirm_logo():
    """确认函抬头 LOGO（可选，默认不加，保持与已确认样张一致）。
    想加：把图片命名为 static/img/confirm-logo.png（或 .jpg）即可自动出现。"""
    for ext in ('png', 'jpg', 'jpeg'):
        p = os.path.join(current_app.static_folder, 'img', 'confirm-logo.%s' % ext)
        if os.path.exists(p):
            return p
    return None


def _next_batch_no(settle_month):
    """批次号：C + 结算月度 + 两位流水，如 C202607-01（同月内顺延，保证唯一）。"""
    base = 'C%s' % (settle_month or datetime.now().strftime('%Y%m'))
    n = ConfirmBatch.query.filter(ConfirmBatch.batch_no.like(base + '-%')).count() + 1
    while ConfirmBatch.query.filter_by(batch_no='%s-%02d' % (base, n)).first():
        n += 1
    return '%s-%02d' % (base, n)


def _batch_check(batch):
    """取批次导入时存下的校验结果（错误/提醒/跳过名单）。"""
    try:
        d = json.loads(batch.check_json or '{}')
    except (ValueError, TypeError):
        d = {}
    return {'errors': d.get('errors') or [], 'warnings': d.get('warnings') or [],
            'skipped': d.get('skipped') or []}


def _letter_data(l):
    """把库里的确认函还原成生成 PDF / 页面渲染用的数据字典。"""
    def item(i):
        return {'kind': i.kind, 'seq': i.seq, 'policy_no': i.policy_no,
                'holder_name': i.holder_name, 'product_name': i.product_name,
                'insure_date': i.insure_date, 'premium': i.premium or 0,
                'ratio': i.ratio or 0, 'fee': i.fee or 0}
    return {
        'name': l.name, 'employee_no': l.employee_no, 'position': l.position, 'team': l.team,
        'id_card': l.id_card, 'bank_card': l.bank_card, 'bank_name': l.bank_name,
        'sign_state': l.sign_state, 'sign_date': l.sign_date, 'settle_month': l.settle_month,
        'self_items': [item(i) for i in l.items if i.kind == 'self'],
        'channel_items': [item(i) for i in l.items if i.kind == 'channel'],
        'accounts': [{'seq': a.seq, 'payee': a.payee, 'bank_name': a.bank_name,
                      'bank_account': a.bank_account, 'id_card': a.id_card,
                      'phone': a.phone, 'amount': a.amount or 0} for a in l.accounts],
        'premium_total': l.premium_total or 0,
        'fee_total': l.fee_total or 0,
        'account_total': l.account_total or 0,
    }


@bp.route('/fin/confirm', methods=['GET'])
@login_required
def confirm_hub():
    """费用结算确认函 hub：导入入口 + 批次台账。"""
    batches = ConfirmBatch.query.order_by(ConfirmBatch.id.desc()).all()
    stats = {
        'batches': len(batches),
        'letters': ConfirmLetter.query.count(),
        'pending': ConfirmLetter.query.filter_by(sign_status='pending').count(),
        'signed': ConfirmLetter.query.filter_by(sign_status='signed').count(),
    }
    return render_template('fin/confirm_hub.html', batches=batches, stats=stats,
                           cfg=_cfg(), missing=cd.missing_deps(),
                           can_manage=_has_perm('fin.confirm.manage'))


@bp.route('/fin/confirm/upload', methods=['POST'])
@login_required
@permission_required('fin.confirm.manage')
def confirm_upload():
    """导入《费用结算数据》Excel：解析 → 四表勾稽校验 → 建批次（不直接出 PDF）。
    结构性错误（缺表/缺列/打不开）→ 不建批次，直接提示；
    勾稽错误（工号无主/姓名不符等）→ 仍然建批次并在详情页列出来，但禁止生成 PDF。"""
    f = request.files.get('file')
    if not f or not f.filename:
        flash('请先选择要导入的《费用结算数据》Excel 文件')
        return redirect(url_for('fin.confirm_hub'))
    if not f.filename.lower().endswith(('.xlsx', '.xlsm')):
        flash('只支持 .xlsx 格式的 Excel（若是 .xls，请先用 Excel 另存为 .xlsx）')
        return redirect(url_for('fin.confirm_hub'))

    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    fname = '%s_%s' % (stamp, _safe_name(f.filename))
    src_path = os.path.join(_conf_dir('src'), fname)
    f.save(src_path)

    parsed, errs = cd.parse_workbook(src_path)
    if errs:
        flash('Excel 解析失败，未导入：' + '；'.join(errs))
        return redirect(url_for('fin.confirm_hub'))

    v = cd.validate(parsed)
    letters, skipped = cd.build_letters(parsed)

    # 结算月度：取人员信息表里出现的第一个月度
    months = sorted({cd.norm_month(r.get('结算年月')) for r in parsed['person'].values()} - {''})
    settle_month = months[0] if months else datetime.now().strftime('%Y%m')

    cfg = _cfg()
    batch = ConfirmBatch(
        batch_no=_next_batch_no(settle_month),
        settle_month=settle_month,
        issuer_name=cfg.company_name, agent_company=cfg.agent_company,
        source_name=f.filename,
        source_path='%s/src/%s' % (CONFIRM_BASE, fname),
        person_count=len(parsed['person']),
        letter_count=len(letters),
        premium_total=round(sum(L['premium_total'] for L in letters), 2),
        fee_total=round(sum(L['fee_total'] for L in letters), 2),
        status='checked',
        check_json=json.dumps({'errors': v['errors'], 'warnings': v['warnings'],
                               'skipped': v['skipped']}, ensure_ascii=False),
        created_by=session.get('username', ''),
        note=request.form.get('note', '').strip(),
    )
    db.session.add(batch)
    db.session.flush()

    for L in letters:
        letter = ConfirmLetter(
            batch_id=batch.id, employee_no=L['employee_no'], name=L['name'],
            position=L['position'], team=L['team'], id_card=L['id_card'],
            bank_card=L['bank_card'], bank_name=L['bank_name'],
            sign_state=L['sign_state'], sign_date=L['sign_date'],
            settle_month=L['settle_month'],
            has_self=bool(L['self_items']), has_channel=bool(L['channel_items']),
            item_count=len(L['self_items']) + len(L['channel_items']),
            premium_total=L['premium_total'], fee_total=L['fee_total'],
            account_total=L['account_total'],
        )
        db.session.add(letter)
        db.session.flush()
        for it in L['self_items'] + L['channel_items']:
            db.session.add(ConfirmItem(
                letter_id=letter.id, kind=it['kind'], seq=it['seq'],
                policy_no=it['policy_no'], holder_name=it['holder_name'],
                product_name=it['product_name'], insure_date=it['insure_date'],
                premium=it['premium'], ratio=it['ratio'], fee=it['fee']))
        for a in L['accounts']:
            db.session.add(ConfirmAccount(
                letter_id=letter.id, seq=a['seq'], payee=a['payee'], bank_name=a['bank_name'],
                bank_account=a['bank_account'], id_card=a['id_card'],
                phone=a['phone'], amount=a['amount']))
    db.session.commit()

    if v['errors']:
        flash('批次 %s 已导入，但发现 %d 条校验错误：请先按清单修正后重新导入，'
              '修正前不能生成 PDF' % (batch.batch_no, len(v['errors'])))
    else:
        flash('批次 %s 导入成功：%d 人参与、将出函 %d 份，保费合计 %s、费用合计 %s'
              % (batch.batch_no, batch.person_count, batch.letter_count,
                 _fmt(batch.premium_total), _fmt(batch.fee_total)))
    return redirect(url_for('fin.confirm_batch', bid=batch.id))


@bp.route('/fin/confirm/<int:bid>', methods=['GET'])
@login_required
def confirm_batch(bid):
    """批次详情：校验结果清单 + 人员出函清单 + 批量生成 / 归档进度。"""
    batch = ConfirmBatch.query.get_or_404(bid)
    letters = ConfirmLetter.query.filter_by(batch_id=batch.id).order_by(ConfirmLetter.id).all()
    letters.sort(key=lambda x: x.id)
    return render_template('fin/confirm_batch.html', batch=batch, letters=letters,
                           check=_batch_check(batch), cfg=_cfg(),
                           missing=cd.missing_deps(),
                           can_manage=_has_perm('fin.confirm.manage'))


@bp.route('/fin/confirm/<int:bid>/generate', methods=['POST'])
@login_required
@permission_required('fin.confirm.manage')
def confirm_generate(bid):
    """批量生成确认函 PDF。有校验错误时坚决不出函（防呆），保证不会把错数据发出去。"""
    batch = ConfirmBatch.query.get_or_404(bid)
    chk = _batch_check(batch)
    if chk['errors']:
        flash('该批次有 %d 条校验错误，修正后重新导入才能生成 PDF' % len(chk['errors']))
        return redirect(url_for('fin.confirm_batch', bid=batch.id))
    miss = cd.missing_deps()
    if miss:
        flash('服务器缺少依赖：%s，无法生成 PDF。请在项目目录双击「安装依赖.bat」后重试' % '、'.join(miss))
        return redirect(url_for('fin.confirm_batch', bid=batch.id))

    cfg = _cfg()
    outdir = _conf_dir('pdf', batch.batch_no)
    ok, fails = 0, []
    for l in ConfirmLetter.query.filter_by(batch_id=batch.id).order_by(ConfirmLetter.id).all():
        try:
            path = os.path.join(outdir, _safe_name(l.pdf_name))
            cd.generate_pdf(_letter_data(l), path,
                            batch.issuer_name or cfg.company_name,
                            batch.agent_company or cfg.agent_company,
                            logo_path=_confirm_logo())
            l.pdf_path = '%s/pdf/%s/%s' % (CONFIRM_BASE, batch.batch_no, os.path.basename(path))
            ok += 1
        except Exception as e:                      # 单人失败不影响其他人
            fails.append('%s：%s' % (l.name, e))
    if ok:
        batch.status = 'generated'
        batch.generated_at = datetime.utcnow()
    db.session.commit()

    if fails:
        flash('已生成 %d 份，另有 %d 份失败：%s' % (ok, len(fails), '；'.join(fails[:3])))
    else:
        flash('已生成 %d 份确认函 PDF' % ok)
        log_audit('export', module='fin', target_type='confirm', target_id=batch.id, target_no=batch.no, detail='出具 %d 份确认函 PDF' % ok)
    return redirect(url_for('fin.confirm_batch', bid=batch.id))


@bp.route('/fin/confirm/<int:bid>/delete', methods=['POST'])
@login_required
@permission_required('fin.confirm.manage')
def confirm_delete(bid):
    """删除批次：连同确认函、明细、账户与已生成的文件一起清理（导入错了重来的入口）。"""
    batch = ConfirmBatch.query.get_or_404(bid)
    no, n, src = batch.batch_no, len(batch.letters), batch.source_path
    for l in list(batch.letters):
        db.session.delete(l)                        # 明细/账户由 delete-orphan 连带删除
    db.session.delete(batch)
    db.session.commit()
    for sub in ('pdf', 'signed'):
        shutil.rmtree(os.path.join(current_app.static_folder, CONFIRM_BASE, sub, no),
                      ignore_errors=True)
    if src:
        try:
            os.remove(os.path.join(current_app.static_folder, src))
        except OSError:
            pass
    flash('批次 %s 及其 %d 份确认函已删除' % (no, n))
    return redirect(url_for('fin.confirm_hub'))


@bp.route('/fin/confirm/<int:bid>/zip', methods=['GET'])
@login_required
def confirm_zip(bid):
    """把批次内已生成的 PDF 打包成一个 zip 下载（一次拿走去分发/打印）。"""
    import io
    import zipfile
    batch = ConfirmBatch.query.get_or_404(bid)
    letters = [l for l in ConfirmLetter.query.filter_by(batch_id=batch.id)
               .order_by(ConfirmLetter.id).all() if l.pdf_path]
    if not letters:
        flash('该批次还没有生成 PDF，无法打包下载')
        return redirect(url_for('fin.confirm_batch', bid=bid))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for l in letters:
            p = os.path.join(current_app.static_folder, l.pdf_path)
            if os.path.exists(p):
                z.write(p, l.pdf_name)
    buf.seek(0)
    return send_file(buf, as_attachment=True, mimetype='application/zip',
                     download_name='%s_费用结算确认函.zip' % batch.batch_no)


@bp.route('/fin/confirm/<int:bid>/letter/<int:lid>', methods=['GET'])
@login_required
def confirm_letter(bid, lid):
    """单人确认函：网页版预览（与 PDF 版式同源数据），附下载与签回归档。"""
    l = ConfirmLetter.query.get_or_404(lid)
    if l.batch_id != bid:
        abort(404)
    d = _letter_data(l)
    total_premium = round(sum(i['premium'] for i in d['self_items'] + d['channel_items']), 2)
    return render_template('fin/confirm_letter.html', l=l, batch=l.batch, d=d,
                           commit_text=cd.COMMIT_TEMPLATE.format(l.batch.agent_company or ''),
                           notes=cd.NOTE_PARAGRAPHS, month_cn=cn_month(l.settle_month),
                           raw_premium_total=total_premium,
                           can_manage=_has_perm('fin.confirm.manage'))


@bp.route('/fin/confirm/<int:bid>/letter/<int:lid>/pdf', methods=['GET'])
@login_required
def confirm_pdf(bid, lid):
    """下载/预览某人的确认函 PDF（?dl=1 强制下载，默认在浏览器里直接打开）。"""
    l = ConfirmLetter.query.get_or_404(lid)
    if l.batch_id != bid:
        abort(404)
    if not l.pdf_path:
        flash('这份确认函还没生成 PDF，请先在批次页点「生成 PDF」')
        return redirect(url_for('fin.confirm_batch', bid=bid))
    path = os.path.join(current_app.static_folder, l.pdf_path)
    if not os.path.exists(path):
        flash('PDF 文件已不存在（可能被手工清理），请重新生成')
        return redirect(url_for('fin.confirm_batch', bid=bid))
    return send_file(path, as_attachment=request.args.get('dl') == '1',
                     download_name=l.pdf_name, mimetype='application/pdf')


@bp.route('/fin/confirm/<int:bid>/letter/<int:lid>/sign', methods=['POST'])
@login_required
@permission_required('fin.confirm.manage')
def confirm_sign(bid, lid):
    """签回归档：上传本人手签后的扫描件/照片，标记已签；全部签完批次自动转「已归档」。"""
    l = ConfirmLetter.query.get_or_404(lid)
    if l.batch_id != bid:
        abort(404)
    f = request.files.get('signed')
    if not f or not f.filename:
        flash('请选择签署后的扫描件或照片')
        return redirect(url_for('fin.confirm_letter', bid=bid, lid=lid))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.pdf', '.jpg', '.jpeg', '.png', '.webp'):
        flash('签回文件只支持 PDF 或图片（jpg / png / webp）')
        return redirect(url_for('fin.confirm_letter', bid=bid, lid=lid))

    no = l.batch.batch_no
    fname = '%s_%s%s' % (l.id, _safe_name(l.name or 'letter'), ext)
    f.save(os.path.join(_conf_dir('signed', no), fname))
    l.signed_file = '%s/signed/%s/%s' % (CONFIRM_BASE, no, fname)
    l.sign_status = 'signed'
    l.signed_at = datetime.utcnow()
    l.signed_by = session.get('username', '')
    db.session.flush()

    b = l.batch
    b.signed_count = ConfirmLetter.query.filter_by(batch_id=b.id, sign_status='signed').count()
    if b.signed_count >= ConfirmLetter.query.filter_by(batch_id=b.id).count():
        b.status = 'archived'
    db.session.commit()
    flash('已归档 %s 的签回件（本批次进度 %d/%d）' % (l.name, b.signed_count, b.letter_count))
    return redirect(url_for('fin.confirm_letter', bid=bid, lid=lid))


@bp.route('/fin/confirm/<int:bid>/letter/<int:lid>/unsign', methods=['POST'])
@login_required
@permission_required('fin.confirm.manage')
def confirm_unsign(bid, lid):
    """撤销归档：签错了/传错文件时回退到待签状态。"""
    l = ConfirmLetter.query.get_or_404(lid)
    if l.batch_id != bid:
        abort(404)
    if l.signed_file:
        try:
            os.remove(os.path.join(current_app.static_folder, l.signed_file))
        except OSError:
            pass
    l.signed_file = ''
    l.sign_status = 'pending'
    l.signed_at = None
    l.signed_by = ''
    db.session.flush()
    b = l.batch
    b.signed_count = ConfirmLetter.query.filter_by(batch_id=b.id, sign_status='signed').count()
    if b.status == 'archived':
        b.status = 'generated'
    db.session.commit()
    flash('已撤销 %s 的归档状态，可重新上传签回件' % l.name)
    return redirect(url_for('fin.confirm_letter', bid=bid, lid=lid))


def _has_perm(code):
    """模板里判断当前登录人有没有某个权限（用于显示/隐藏操作按钮）。"""
    from models import User
    u = User.query.get(session.get('user_id'))
    return bool(u and u.level and any(p.code == code for p in u.level.permissions))
# ===================== 七、FMS · 自动记账（增量 #38） =====================
# 把五条业务线（佣金 / 工资 / 奖金池 / 报销 / 发票）按期间汇总成一张「草稿」凭证，
# 借贷模板按《小企业会计准则》+ 一般纳税人（增值税 6%）口径预设；生成后回填各单据 voucher_id，
# 既能从凭证看到来源，又能防重复生成。生成的凭证仍是 draft，需走「审核→记账」流转（制单≠审核）。
import calendar


def _fin_acc(code):
    # 取科目对象（带名称快照）；科目缺失直接抛错，避免生成错凭证。
    a = FinAccount.query.filter_by(code=code).first()
    if not a:
        raise RuntimeError('自动记账缺少科目 %s，请先到「科目设置」确认' % code)
    return a


def _period_last_day(period):
    # YYYY-MM -> 该月最后一天 date（凭证日期落在目标期间，凭证号/期间才正确）。
    y, m = period.split('-')
    d = calendar.monthrange(int(y), int(m))[1]
    return date(int(y), int(m), d)


def _quarter_last_month(quarter):
    # '2026-Q2' -> '2026-06'（奖金池凭证期间用季度末月，保持 YYYY-MM 口径）。
    y, q = quarter.split('-Q')
    return '%s-%02d' % (y, int(q) * 3)


def _mk_auto_voucher(word, period, source_type, summary, entries, docs, maker):
    # 通用：构造一张 draft 凭证 + 分录，回填 docs 的 voucher_id。
    # entries: [{'summary':.., 'code':.., 'debit':.., 'credit':..}, ...]
    # docs: 需要回填 voucher_id 的 ORM 对象列表。
    v = FinVoucher(maker_id=maker.get('id'), maker_name=maker.get('name'),
                   source_type=source_type, source_id=None, status='draft',
                   word=word, period=period, voucher_date=_period_last_day(period),
                   summary=summary)
    v.voucher_no, v.seq = _next_voucher_no(word, period)  # 编凭证号+序号（与 #32 手工凭证同口径）
    db.session.add(v)
    db.session.flush()
    for i, e in enumerate(entries, 1):
        acc = _fin_acc(e['code'])
        v.entries.append(FinVoucherEntry(
            seq=i, summary=e['summary'],
            account_code=acc.code, account_name=acc.name,
            debit=_d2(e.get('debit', 0)), credit=_d2(e.get('credit', 0))))
    for doc in docs:
        doc.voucher_id = v.id
    db.session.commit()
    return v


def _auto_commission(period, maker):
    # 佣金（外勤，海腾代发）：按月汇总已确认(confirmed)且未生成凭证的佣金。
    docs = Commission.query.filter_by(month=period, status='confirmed').filter(
        Commission.voucher_id.is_(None)).all()
    if not docs:
        return None
    total = _d2(sum(_d2(c.commission_amount) for c in docs))
    sm = '%s 代理佣金结算（海腾代发）' % period
    entries = [
        {'summary': sm, 'code': '540101', 'debit': total, 'credit': 0},
        {'summary': sm, 'code': '224101', 'debit': 0, 'credit': total},
    ]
    return _mk_auto_voucher('转', period, 'commission', '%s 代理佣金结算' % period, entries, docs, maker)


def _auto_salary(period, maker):
    # 工资（内勤，计提口径）：按月汇总未生成凭证的工资清单。
    # 借 管理费用-工资薪酬（应发）；贷 应付工资（实发）+ 应付社保（个人）+ 应付公积金（个人）。
    docs = Salary.query.filter_by(month=period).filter(Salary.voucher_id.is_(None)).all()
    if not docs:
        return None
    gross = _d2(0); social = _d2(0); fund = _d2(0)
    for s in docs:
        g = _d2(_d2(s.base_salary) + _d2(s.position_salary) + _d2(s.performance_salary) + _d2(s.subsidy))
        if s.custom and isinstance(s.custom, dict):
            g = _d2(g + sum(_d2(v) for v in s.custom.values()))
        gross = _d2(gross + g)
        social = _d2(social + _d2(s.pension) + _d2(s.medical))
        fund = _d2(fund + _d2(s.housing_fund))
    sm = '%s 工资计提' % period
    entries = [{'summary': sm, 'code': '560201', 'debit': gross, 'credit': 0}]
    entries.append({'summary': sm, 'code': '221101', 'debit': 0, 'credit': _d2(gross - social - fund)})
    if social > 0:
        entries.append({'summary': sm + '（个人社保）', 'code': '221102', 'debit': 0, 'credit': social})
    if fund > 0:
        entries.append({'summary': sm + '（个人公积金）', 'code': '221103', 'debit': 0, 'credit': fund})
    return _mk_auto_voucher('转', period, 'salary', sm, entries, docs, maker)


def _auto_bonus(quarter, maker):
    # 奖金池（外勤奖励）：按奖金池(quarter)汇总已分配且未生成凭证的明细。
    docs = BonusAllocation.query.join(Bonus).filter(Bonus.quarter == quarter).filter(
        BonusAllocation.voucher_id.is_(None)).all()
    if not docs:
        return None
    total = _d2(sum(_d2(a.amount) for a in docs))
    period = _quarter_last_month(quarter)
    sm = '%s 渠道奖励（海腾代发）' % quarter
    entries = [
        # 销售激励费（560103）：5601 已因新增明细科目变为汇总科目，不能再直接做分录
        {'summary': sm, 'code': '560103', 'debit': total, 'credit': 0},
        {'summary': sm, 'code': '224101', 'debit': 0, 'credit': total},
    ]
    return _mk_auto_voucher('转', period, 'bonus', sm, entries, docs, maker)


def _auto_expense(period, maker):
    # 报销（OA 审批 type=报销 且 approved）：按审批月份汇总未生成凭证的报销单，按费用科目分组。
    all_a = Approval.query.filter_by(type='报销', status='approved').filter(
        Approval.voucher_id.is_(None)).all()
    docs = [a for a in all_a if (a.decided_at or a.created_at) and
            (a.decided_at or a.created_at).strftime('%Y-%m') == period]
    if not docs:
        return None
    by_acc = {}
    for a in docs:
        code = a.expense_account or '560202'
        by_acc[code] = _d2(by_acc.get(code, 0) + _d2(a.amount))
    total = _d2(sum(by_acc.values()))
    sm = '%s 费用报销' % period
    entries = [{'summary': sm, 'code': code, 'debit': amt, 'credit': 0} for code, amt in by_acc.items()]
    entries.append({'summary': sm, 'code': '1002', 'debit': 0, 'credit': total})
    return _mk_auto_voucher('付', period, 'expense', sm, entries, docs, maker)


def _auto_reimburse(period, maker):
    # 费用报销单（OA「费用申请与报销」模块）：取「已通过 / 已付款」且未生成凭证的报销单，
    # 按「费用大类 → 会计科目」映射分组合并生成一张「付字」凭证，并回填各单 voucher_id 防重复。
    # 分录：借 费用科目（净额）+ 借 222101 应交增值税（进项税，如有）／ 贷 1002 银行存款（含税合计）。
    # ⚠️ 必须把 status='paid' 也算进来：单子先付款后记账是常态，只认 approved 会漏单、凭证永远生不出来。
    docs = ExpenseReimburse.query.filter(
        ExpenseReimburse.status.in_(['approved', 'paid']),
        ExpenseReimburse.period == period,
        ExpenseReimburse.voucher_id.is_(None)).all()
    if not docs:
        return None
    by_acc = {}
    tax_total = _d2(0)
    for r in docs:
        code = r.account_code or EXPENSE_CATEGORY_ACCOUNT.get(r.category, '560299')
        net = _d2(_d2(r.amount) - _d2(r.tax_amount))
        by_acc[code] = _d2(by_acc.get(code, 0) + net)
        tax_total = _d2(tax_total + _d2(r.tax_amount))
    total = _d2(sum(by_acc.values()) + tax_total)
    sm = '%s 费用报销单' % period
    entries = [{'summary': sm, 'code': code, 'debit': amt, 'credit': 0}
               for code, amt in by_acc.items() if amt]
    if tax_total:
        entries.append({'summary': sm, 'code': '222101', 'debit': tax_total, 'credit': 0})
    entries.append({'summary': sm, 'code': '1002', 'debit': 0, 'credit': total})
    return _mk_auto_voucher('付', period, 'reimburse', sm, entries, docs, maker)


def _auto_invoice(period, maker):
    # 发票（#37）：按期间分别汇总进项/销项，各生成一张转字凭证并回填。返回凭证列表。
    out = []
    ins = FinInvoice.query.filter_by(period=period, direction='in').filter(
        FinInvoice.voucher_id.is_(None)).all()
    if ins:
        amt = _d2(sum(_d2(i.amount) for i in ins))
        tax = _d2(sum(_d2(i.tax) for i in ins))
        entries = [
            {'summary': '%s 进项发票（费用）' % period, 'code': '560202', 'debit': amt, 'credit': 0},
            {'summary': '%s 进项发票（进项税）' % period, 'code': '222101', 'debit': tax, 'credit': 0},
            {'summary': '%s 进项发票' % period, 'code': '2202', 'debit': 0, 'credit': _d2(amt + tax)},
        ]
        out.append(_mk_auto_voucher('转', period, 'invoice', '%s 进项发票' % period, entries, ins, maker))
    outs = FinInvoice.query.filter_by(period=period, direction='out').filter(
        FinInvoice.voucher_id.is_(None)).all()
    if outs:
        amt = _d2(sum(_d2(i.amount) for i in outs))
        tax = _d2(sum(_d2(i.tax) for i in outs))
        entries = [
            {'summary': '%s 销项发票（应收）' % period, 'code': '1122', 'debit': _d2(amt + tax), 'credit': 0},
            {'summary': '%s 销项发票（收入）' % period, 'code': '5001', 'debit': 0, 'credit': amt},
            {'summary': '%s 销项发票（销项税）' % period, 'code': '222101', 'debit': 0, 'credit': tax},
        ]
        out.append(_mk_auto_voucher('转', period, 'invoice', '%s 销项发票' % period, entries, outs, maker))
    return out


@bp.route('/fin/auto-book')
@login_required
@permission_required('fin.auto_book')
def auto_book():
    # 自动记账中心：列出五条业务线，各自的可生成期间与待生成数量。
    cfg = _cfg()
    comm_months = db.session.query(Commission.month).filter_by(status='confirmed').filter(
        Commission.voucher_id.is_(None)).distinct().all()
    sal_months = db.session.query(Salary.month).filter(Salary.voucher_id.is_(None)).distinct().all()
    bonus_q = BonusAllocation.query.join(Bonus).filter(
        BonusAllocation.voucher_id.is_(None)).with_entities(Bonus.quarter).distinct().all()
    exp_rows = Approval.query.filter_by(type='报销', status='approved').filter(
        Approval.voucher_id.is_(None)).all()
    exp_months = sorted({(a.decided_at or a.created_at).strftime('%Y-%m') for a in exp_rows})
    inv_periods = db.session.query(FinInvoice.period).filter(
        FinInvoice.voucher_id.is_(None)).distinct().all()
    # 费用报销单（OA「费用申请与报销」模块）：已通过/已付款且未生成凭证的，按其记账期间列出
    reimb_periods = db.session.query(ExpenseReimburse.period).filter(
        ExpenseReimburse.status.in_(['approved', 'paid'])).filter(
        ExpenseReimburse.voucher_id.is_(None)).distinct().all()
    reimb_months = sorted({p[0] for p in reimb_periods if p[0]})
    blocks = [
        {'key': 'commission', 'name': '代理佣金', 'unit': '月份', 'periods': [m[0] for m in comm_months]},
        {'key': 'salary', 'name': '工资清单（计提）', 'unit': '月份', 'periods': [m[0] for m in sal_months]},
        {'key': 'bonus', 'name': '奖金池', 'unit': '季度', 'periods': [q[0] for q in bonus_q]},
        {'key': 'expense', 'name': '费用报销', 'unit': '月份', 'periods': exp_months},
        {'key': 'reimburse', 'name': '费用报销单（OA）', 'unit': '月份', 'periods': reimb_months},
        {'key': 'invoice', 'name': '发票（进/销项）', 'unit': '税款所属期', 'periods': [p[0] for p in inv_periods]},
    ]
    return render_template('fin/auto_book.html', cfg=cfg, blocks=blocks)


@bp.route('/fin/auto-book/gen', methods=['POST'])
@login_required
@permission_required('fin.auto_book')
def auto_book_gen():
    # 生成凭证：source + period -> 汇总生成 draft 凭证；借贷平衡与科目合规在构造时保证。
    cfg = _cfg()
    if not cfg.opening_done:
        flash('还没有完成期初建账，不能自动记账')
        return redirect(url_for('fin.auto_book'))
    source = (request.form.get('source') or '').strip()
    period = (request.form.get('period') or '').strip()
    valid = {'commission', 'salary', 'bonus', 'expense', 'reimburse', 'invoice'}
    if source not in valid or not period:
        flash('参数错误')
        return redirect(url_for('fin.auto_book'))
    if source == 'bonus':
        if not re.match(r'^\d{4}-Q[1-4]$', period):
            flash('奖金池期间格式应为 2026-Q2')
            return redirect(url_for('fin.auto_book'))
        chk_period = _quarter_last_month(period)
    else:
        if not re.match(r'^\d{4}-\d{2}$', period):
            flash('期间格式应为 2026-08')
            return redirect(url_for('fin.auto_book'))
        chk_period = period
    if _period_locked(chk_period):
        flash('目标期间 %s 已结账锁定，不能生成凭证' % chk_period)
        return redirect(url_for('fin.auto_book'))
    maker = {'id': session.get('user_id'), 'name': session.get('username', '')}
    try:
        if source == 'commission':
            v = _auto_commission(period, maker)
        elif source == 'salary':
            v = _auto_salary(period, maker)
        elif source == 'bonus':
            v = _auto_bonus(period, maker)
        elif source == 'expense':
            v = _auto_expense(period, maker)
        elif source == 'reimburse':
            v = _auto_reimburse(period, maker)
        else:
            vs = _auto_invoice(period, maker)
    except RuntimeError as e:
        flash(str(e))
        return redirect(url_for('fin.auto_book'))
    if source == 'invoice':
        if not vs:
            flash('%s 没有可生成的发票凭证（可能已生成或无可生成发票）' % period)
        else:
            flash('%s 自动生成 %d 张发票凭证：%s' % (
                period, len(vs), '、'.join(v.voucher_no for v in vs)))
            log_audit('add', module='fin', target_type='voucher', target_no='、'.join(v.voucher_no for v in vs), detail='自动记账-发票 %s' % period)
    else:
        if not v:
            flash('%s 没有可生成的%s凭证（可能已生成或无可生成单据）' % (period, source))
        else:
            flash('%s 自动生成凭证 %s（草稿，待审核记账）' % (period, v.voucher_no))
            log_audit('add', module='fin', target_type='voucher', target_id=v.id, target_no=v.voucher_no, detail='自动记账-%s %s' % (source, period))
    return redirect(url_for('fin.auto_book'))


# ===================== FMS · 备份与恢复（#40） =====================

@bp.route('/fin/backup', methods=['GET'])
@login_required
@permission_required('fin.backup')
def fin_backup():
    """财务数据备份中心（#40）：列出历史备份 + 当前库信息 + 一键备份/恢复。"""
    import os, shutil
    db_path = db.engine.url.database
    bk_dir = os.path.join(os.path.dirname(db_path), 'backups')
    backups = FinBackup.query.order_by(FinBackup.id.desc()).all()
    cur_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    return render_template('fin/backup.html', backups=backups, cur_size=cur_size,
                           db_name=os.path.basename(db_path), bk_dir=bk_dir)


@bp.route('/fin/backup/do', methods=['POST'])
@login_required
@permission_required('fin.backup')
def fin_backup_do():
    """一键备份：复制当前 SQLite 库到 backups/，记录 FinBackup。"""
    import os, shutil
    db_path = db.engine.url.database
    bk_dir = os.path.join(os.path.dirname(db_path), 'backups')
    os.makedirs(bk_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = 'fin_backup_%s.db' % ts
    dst = os.path.join(bk_dir, fname)
    shutil.copy2(db_path, dst)
    size = os.path.getsize(dst)
    rec = FinBackup(filename=fname, size=size, type='manual',
                    created_by=session.get('username', '') or '未知')
    db.session.add(rec)
    db.session.commit()
    log_audit('backup', module='fin', target_no=fname,
              detail='一键备份数据库 %s 字节' % size)
    flash('已备份：%s（%d 字节）' % (fname, size))
    return redirect(url_for('fin.fin_backup'))


@bp.route('/fin/backup/restore', methods=['POST'])
@login_required
@permission_required('fin.backup')
def fin_backup_restore():
    """恢复指定备份：先自动备份当前库（防回退），再覆盖。危险操作，前端已二次确认。"""
    import os, shutil
    bid = request.form.get('bid', type=int)
    bk = FinBackup.query.get(bid)
    if not bk:
        flash('备份记录不存在')
        return redirect(url_for('fin.fin_backup'))
    db_path = db.engine.url.database
    bk_dir = os.path.join(os.path.dirname(db_path), 'backups')
    src = os.path.join(bk_dir, bk.filename)
    if not os.path.exists(src):
        flash('备份文件 %s 已丢失，无法恢复' % bk.filename)
        return redirect(url_for('fin.fin_backup'))
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    pre = os.path.join(bk_dir, 'pre_restore_%s.db' % ts)
    shutil.copy2(db_path, pre)          # 恢复前先留快照，防止误操作无法回退
    db.session.close()
    db.engine.dispose()                 # 关闭现有连接，避免 Windows 文件锁
    shutil.copy2(src, db_path)
    log_audit('restore', module='fin', target_no=bk.filename,
              detail='恢复至备份 %s（恢复前快照 %s）' % (bk.filename, os.path.basename(pre)))
    flash('已恢复至备份 %s' % bk.filename)
    return redirect(url_for('fin.fin_backup'))


@bp.route('/fin/audit', methods=['GET'])
@login_required
@permission_required('fin.audit.view')
def fin_audit():
    """操作日志审计页（#40）：列出财务关键操作，可按动作 / 模块筛选。"""
    action = (request.args.get('action') or '').strip()
    module = (request.args.get('module') or '').strip()
    q = FinAuditLog.query
    if action:
        q = q.filter_by(action=action)
    if module:
        q = q.filter_by(module=module)
    logs = q.order_by(FinAuditLog.id.desc()).limit(500).all()
    actions = list(FinAuditLog.ACTION_CN.items())
    modules = [('fin', '财务'), ('auth', '登录'), ('oa', 'OA'), ('hr', '人事'), ('it', 'IT')]
    return render_template('fin/audit.html', logs=logs, action=action, module=module,
                           actions=actions, modules=modules)


# ===================== 六·8、FMS · 银企对账（二期 #42） =====================
# 补齐一期"账实不符"的半截子：银行账户 → 流水导入 → 自动+手动对账 → 余额调节表。
# 方向口径：企业账 借 1002 = 存款增、贷 1002 = 存款减；
#          银行流水（银行视角）贷方 = 银行收(企业增)、借方 = 银行付(企业减)。
# 因此：流水贷方(d) ↔ 凭证 借 1002(d)；流水借方(d) ↔ 凭证 贷 1002(d)。
from flask import abort


def _bank_or_404(baid):
    ba = FinBankAccount.query.get(baid)
    if not ba:
        abort(404)
    return ba


def _bank_unreached_entries(account_code):
    """该 1002 科目下、已记账(posted)的每笔分录，标注是否已对账（工作台右栏 + 调节表用）。"""
    matched_ids = {s.entry_id for s in FinBankStatement.query.filter(FinBankStatement.entry_id.isnot(None)).all()}
    rows = (db.session.query(FinVoucherEntry, FinVoucher)
            .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
            .filter(FinVoucherEntry.account_code == account_code, FinVoucher.status == 'posted')
            .order_by(FinVoucher.voucher_date)
            .all())
    out = []
    for e, v in rows:
        out.append({'entry': e, 'voucher': v, 'matched': e.id in matched_ids,
                    'date': v.voucher_date, 'summary': e.summary or v.summary,
                    'debit': e.debit or 0, 'credit': e.credit or 0})
    return out


def _bank_auto_reconcile(ba):
    """自动对账：未对账流水 vs 未对账 1002 分录，按 金额相等 + 日期±7天 + 方向对应 配对。"""
    stmts = FinBankStatement.query.filter_by(bank_account_id=ba.id, reconciled=False).all()
    matched_ids = {s.entry_id for s in FinBankStatement.query.filter(FinBankStatement.entry_id.isnot(None)).all()}
    rows = (db.session.query(FinVoucherEntry, FinVoucher)
            .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
            .filter(FinVoucherEntry.account_code == ba.account_code, FinVoucher.status == 'posted')
            .all())
    cand = [(e, v) for e, v in rows if e.id not in matched_ids]
    cnt = 0
    for s in stmts:
        target = None
        amt = round(s.credit or 0, 2)            # 流水贷方 ↔ 凭证借 1002
        if amt:
            for e, v in cand:
                if e.id in matched_ids:
                    continue
                if round(e.debit or 0, 2) == amt and abs((v.voucher_date - s.trans_date).days) <= 7:
                    target = (e, v); break
        if not target:
            damt = round(s.debit or 0, 2)        # 流水借方 ↔ 凭证贷 1002
            if damt:
                for e, v in cand:
                    if e.id in matched_ids:
                        continue
                    if round(e.credit or 0, 2) == damt and abs((v.voucher_date - s.trans_date).days) <= 7:
                        target = (e, v); break
        if target:
            e, v = target
            s.reconciled = True
            s.voucher_id = v.id
            s.entry_id = e.id
            matched_ids.add(e.id)
            cnt += 1
    db.session.commit()
    return cnt


def _bank_adjust(ba, period):
    """银行存款余额调节表数据。"""
    op = FinOpening.query.filter_by(account_code=ba.account_code).first()
    opening = 0.0
    if op:
        opening = (op.debit - op.credit) if op.direction == 'debit' else (op.credit - op.debit)
    flow = (db.session.query(db.func.sum(FinVoucherEntry.debit), db.func.sum(FinVoucherEntry.credit))
            .join(FinVoucher, FinVoucherEntry.voucher_id == FinVoucher.id)
            .filter(FinVoucherEntry.account_code == ba.account_code, FinVoucher.status == 'posted',
                    FinVoucher.period <= period).first())
    book_balance = opening + (flow[0] or 0) - (flow[1] or 0)

    last = FinBankStatement.query.filter_by(bank_account_id=ba.id).order_by(FinBankStatement.trans_date.desc()).first()
    bank_balance = last.balance if last else (ba.opening_balance or 0)

    entries = _bank_unreached_entries(ba.account_code)
    book_recv_un = sum(x['debit'] for x in entries if not x['matched'] and x['debit'])    # 企业已收、银行未收
    book_pay_un = sum(x['credit'] for x in entries if not x['matched'] and x['credit'])   # 企业已付、银行未付
    stmts = FinBankStatement.query.filter_by(bank_account_id=ba.id, reconciled=False).all()
    bank_recv_un = sum(s.credit or 0 for s in stmts if s.credit)   # 银行已收、企业未收
    bank_pay_un = sum(s.debit or 0 for s in stmts if s.debit)      # 银行已付、企业未付

    book_adjusted = book_balance + book_recv_un - book_pay_un
    bank_adjusted = bank_balance + bank_recv_un - bank_pay_un
    return {
        'period': period, 'book_balance': book_balance, 'bank_balance': bank_balance,
        'book_recv_un': book_recv_un, 'book_pay_un': book_pay_un,
        'bank_recv_un': bank_recv_un, 'bank_pay_un': bank_pay_un,
        'book_adjusted': book_adjusted, 'bank_adjusted': bank_adjusted,
        'diff': round(book_adjusted - bank_adjusted, 2),
    }


def _bank_parse_rows(raw_rows):
    """从原始表（list of list）解析流水行。首行含关键字则视为表头定位列，否则固定列序：
    日期,摘要,对方户名,借方,贷方,余额。返回 list of dict（未强校验日期/金额）。"""
    if not raw_rows:
        return []
    kw = {'date': ['日期', '交易日期', 'date', '交易时间'],
          'summary': ['摘要', '备注', 'summary', 'desc', '用途'],
          'counter': ['对方', '对方户名', '交易对手', 'counterparty'],
          'debit': ['借方', '支出', 'debit', '付'],
          'credit': ['贷方', '收入', 'credit', '收'],
          'balance': ['余额', 'balance']}
    header = [str(c or '').strip().lower() for c in raw_rows[0]]
    col, has_header = {}, False
    for key, words in kw.items():
        for i, h in enumerate(header):
            if any(w in h for w in words):
                col[key] = i; has_header = True; break
    data = raw_rows[1:] if has_header else raw_rows
    out = []
    for r in data:
        if not any(r):
            continue
        def get(key, default=''):
            idx = col.get(key)
            return r[idx] if (idx is not None and idx < len(r)) else default
        out.append({'date': get('date'), 'summary': get('summary'),
                    'counterparty': get('counter'), 'debit': get('debit'),
                    'credit': get('credit'), 'balance': get('balance')})
    return out


@bp.route('/fin/bank', methods=['GET'])
@login_required
@permission_required('fin.bank.view')
def bank_hub():
    """银企对账中心：账户卡片 + 各账户待对账数。"""
    accounts = FinBankAccount.query.order_by(FinBankAccount.is_default.desc(), FinBankAccount.code).all()
    for ba in accounts:
        ba.pending = FinBankStatement.query.filter_by(bank_account_id=ba.id, reconciled=False).count()
        ba.stmt_count = FinBankStatement.query.filter_by(bank_account_id=ba.id).count()
    return render_template('fin/bank_hub.html', accounts=accounts)


@bp.route('/fin/bank/accounts', methods=['GET'])
@login_required
@permission_required('fin.bank.manage')
def bank_accounts():
    accounts = FinBankAccount.query.order_by(FinBankAccount.code).all()
    subs = FinAccount.query.filter(FinAccount.code.like('1002%'), FinAccount.enabled.is_(True)).order_by(FinAccount.code).all()
    return render_template('fin/bank_accounts.html', accounts=accounts, subs=subs)


@bp.route('/fin/bank/accounts/new', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_account_new():
    code = (request.form.get('code') or '').strip()
    name = (request.form.get('name') or '').strip()
    account_code = (request.form.get('account_code') or '').strip()
    if not code or not name or not account_code:
        flash('账户编码、名称、对应科目均为必填'); return redirect(url_for('fin.bank_accounts'))
    if FinBankAccount.query.filter_by(code=code).first():
        flash('账户编码已存在'); return redirect(url_for('fin.bank_accounts'))
    ba = FinBankAccount(
        code=code, name=name,
        bank_name=(request.form.get('bank_name') or '').strip(),
        account_no=(request.form.get('account_no') or '').strip(),
        account_code=account_code,
        currency=(request.form.get('currency') or 'CNY').strip(),
        opening_balance=round(_to_amount(request.form.get('opening_balance')) or 0, 2),
        is_default=bool(request.form.get('is_default')),
        note=(request.form.get('note') or '').strip())
    if ba.is_default:
        for o in FinBankAccount.query.filter_by(is_default=True).all():
            o.is_default = False
    db.session.add(ba); db.session.commit()
    log_audit('add', module='fin', target_type='bank_account', target_no=code, detail='新增银行账户 %s' % name)
    flash('银行账户已添加')
    return redirect(url_for('fin.bank_accounts'))


@bp.route('/fin/bank/accounts/<int:baid>/edit', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_account_edit(baid):
    ba = _bank_or_404(baid)
    ba.name = (request.form.get('name') or '').strip()
    ba.bank_name = (request.form.get('bank_name') or '').strip()
    ba.account_no = (request.form.get('account_no') or '').strip()
    ba.account_code = (request.form.get('account_code') or '').strip()
    ba.currency = (request.form.get('currency') or 'CNY').strip()
    ba.opening_balance = round(_to_amount(request.form.get('opening_balance')) or 0, 2)
    ba.is_default = bool(request.form.get('is_default'))
    ba.note = (request.form.get('note') or '').strip()
    if ba.is_default:
        for o in FinBankAccount.query.filter(FinBankAccount.id != baid, FinBankAccount.is_default.is_(True)).all():
            o.is_default = False
    db.session.commit()
    log_audit('update', module='fin', target_type='bank_account', target_no=ba.code, detail='修改银行账户')
    flash('账户已保存')
    return redirect(url_for('fin.bank_accounts'))


@bp.route('/fin/bank/accounts/<int:baid>/delete', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_account_delete(baid):
    ba = _bank_or_404(baid)
    FinBankStatement.query.filter_by(bank_account_id=baid).delete()
    db.session.delete(ba); db.session.commit()
    log_audit('delete', module='fin', target_type='bank_account', target_no=ba.code, detail='删除银行账户及流水')
    flash('账户已删除')
    return redirect(url_for('fin.bank_accounts'))


@bp.route('/fin/bank/accounts/<int:baid>/statements', methods=['GET'])
@login_required
@permission_required('fin.bank.view')
def bank_statements(baid):
    ba = _bank_or_404(baid)
    stmts = FinBankStatement.query.filter_by(bank_account_id=baid).order_by(FinBankStatement.trans_date).all()
    return render_template('fin/bank_statements.html', ba=ba, stmts=stmts)


@bp.route('/fin/bank/accounts/<int:baid>/import', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_import(baid):
    ba = _bank_or_404(baid)
    f = request.files.get('file')
    if not f or not f.filename:
        flash('请选择要导入的流水文件'); return redirect(url_for('fin.bank_statements', baid=baid))
    ext = f.filename.rsplit('.', 1)[-1].lower()
    raw = []
    try:
        if ext in ('xlsx', 'xls'):
            from openpyxl import load_workbook
            wb = load_workbook(f.stream, read_only=True, data_only=True)
            for row in wb.active.iter_rows(values_only=True):
                raw.append(list(row))
        elif ext == 'csv':
            import csv, io
            content = f.stream.read().decode('utf-8-sig', errors='ignore')
            raw = list(csv.reader(io.StringIO(content)))
        else:
            flash('仅支持 xlsx / csv 文件'); return redirect(url_for('fin.bank_statements', baid=baid))
    except Exception as e:
        flash('文件解析失败：%s' % e); return redirect(url_for('fin.bank_statements', baid=baid))
    parsed = _bank_parse_rows(raw)
    added = 0
    for p in parsed:
        d = _parse_date(p['date'])
        if not d:
            continue
        db.session.add(FinBankStatement(
            bank_account_id=baid, trans_date=d,
            summary=(p['summary'] or '').strip(),
            counterparty=(p['counterparty'] or '').strip(),
            debit=round(_to_amount(p['debit']) or 0, 2),
            credit=round(_to_amount(p['credit']) or 0, 2),
            balance=round(_to_amount(p['balance']) or 0, 2)))
        added += 1
    db.session.commit()
    log_audit('add', module='fin', target_type='bank_stmt', target_no=ba.code, detail='导入 %d 行流水' % added)
    flash('成功导入 %d 行流水' % added)
    return redirect(url_for('fin.bank_statements', baid=baid))


@bp.route('/fin/bank/accounts/<int:baid>/reconcile', methods=['GET'])
@login_required
@permission_required('fin.bank.view')
def bank_reconcile(baid):
    ba = _bank_or_404(baid)
    stmts = FinBankStatement.query.filter_by(bank_account_id=baid, reconciled=False).order_by(FinBankStatement.trans_date).all()
    reconciled = FinBankStatement.query.filter_by(bank_account_id=baid, reconciled=True).order_by(FinBankStatement.trans_date).all()
    entries = _bank_unreached_entries(ba.account_code)
    unreached_entries = [e for e in entries if not e['matched']]
    return render_template('fin/bank_reconcile.html', ba=ba, stmts=stmts, reconciled=reconciled, entries=unreached_entries)


@bp.route('/fin/bank/accounts/<int:baid>/reconcile/auto', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_reconcile_auto(baid):
    ba = _bank_or_404(baid)
    cnt = _bank_auto_reconcile(ba)
    log_audit('update', module='fin', target_type='bank', target_no=ba.code, detail='自动对账匹配 %d 笔' % cnt)
    flash('自动对账完成，匹配 %d 笔' % cnt)
    return redirect(url_for('fin.bank_reconcile', baid=baid))


@bp.route('/fin/bank/accounts/<int:baid>/reconcile/match', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_reconcile_match(baid):
    ba = _bank_or_404(baid)
    sid = int(request.form.get('statement_id') or 0)
    eid = int(request.form.get('entry_id') or 0)
    s = FinBankStatement.query.get(sid)
    e = FinVoucherEntry.query.get(eid)
    if s and e and s.bank_account_id == baid and not s.reconciled:
        s.reconciled = True
        s.voucher_id = e.voucher_id
        s.entry_id = e.id
        db.session.commit()
        log_audit('update', module='fin', target_type='bank', target_no=ba.code,
                  detail='手动勾对流水#%d ↔ 凭证分录#%d' % (sid, eid))
    return redirect(url_for('fin.bank_reconcile', baid=baid))


@bp.route('/fin/bank/accounts/<int:baid>/reconcile/unmatch', methods=['POST'])
@login_required
@permission_required('fin.bank.manage')
def bank_reconcile_unmatch(baid):
    ba = _bank_or_404(baid)
    sid = int(request.form.get('statement_id') or 0)
    s = FinBankStatement.query.get(sid)
    if s and s.bank_account_id == baid and s.reconciled:
        s.reconciled = False
        s.voucher_id = None
        s.entry_id = None
        db.session.commit()
        log_audit('update', module='fin', target_type='bank', target_no=ba.code, detail='取消勾对流水#%d' % sid)
    return redirect(url_for('fin.bank_reconcile', baid=baid))


@bp.route('/fin/bank/accounts/<int:baid>/adjust', methods=['GET'])
@login_required
@permission_required('fin.bank.view')
def bank_adjust(baid):
    ba = _bank_or_404(baid)
    period = request.args.get('period') or _default_period()
    data = _bank_adjust(ba, period)
    return render_template('fin/bank_adjust.html', ba=ba, d=data)


# ============================================================
# 出纳收付款单（二期 #43）：录入收/付款单 → 自动生成 1002 银行存款凭证
# 与银企对账衔接：凭证记账 posted 后，其 1002 分录自动成为对账候选（对账侧零改动）
# ============================================================

def _cash_next_code(typ, period):
    """收付款单号：SKD/FKD-YYYYMM-001，收/付各自独立连续编号。"""
    prefix = 'SKD' if typ == 'receive' else 'FKD'
    like = '%s-%s-%%' % (prefix, period.replace('-', ''))
    last = (FinCashOrder.query.filter(FinCashOrder.code.like(like))
            .order_by(FinCashOrder.code.desc()).first())
    seq = int(last.code.split('-')[-1]) + 1 if last else 1
    return '%s-%s-%03d' % (prefix, period.replace('-', ''), seq)


def _mk_cash_voucher(order, maker):
    """收付款单 → 生成一张 draft 收/付凭证（含 1002 分录），回填 order.voucher_id。
    收款：借 1002(账户) + 贷 对方科目；付款：借 对方科目 + 贷 1002(账户)。
    voucher_date = 单据业务日期（非期间末日）。"""
    ba = FinBankAccount.query.get(order.bank_account_id)
    if not ba:
        raise ValueError('银行账户不存在')
    bank_code = ba.account_code
    other_code = order.account_code
    amt = _d2(order.amount)
    if amt <= 0:
        raise ValueError('金额必须大于 0')
    if order.type == 'receive':
        entries = [
            {'summary': order.summary or '收款', 'code': bank_code, 'debit': amt, 'credit': 0},
            {'summary': order.summary or '收款', 'code': other_code, 'debit': 0, 'credit': amt},
        ]
        word, source_type = '收', 'cash_receive'
    else:
        entries = [
            {'summary': order.summary or '付款', 'code': other_code, 'debit': amt, 'credit': 0},
            {'summary': order.summary or '付款', 'code': bank_code, 'debit': 0, 'credit': amt},
        ]
        word, source_type = '付', 'cash_pay'
    period = order.trans_date.strftime('%Y-%m')
    v = FinVoucher(maker_id=maker.get('id'), maker_name=maker.get('name'),
                   source_type=source_type, source_id=order.id, status='draft',
                   word=word, period=period, voucher_date=order.trans_date,
                   summary=order.summary or ('%s单' % order.type_cn))
    v.voucher_no, v.seq = _next_voucher_no(word, period)
    db.session.add(v)
    db.session.flush()
    for i, e in enumerate(entries, 1):
        try:
            acc = _fin_acc(e['code'])
        except RuntimeError:
            raise ValueError('科目 %s 不存在或已停用' % e['code'])
        v.entries.append(FinVoucherEntry(
            seq=i, summary=e['summary'],
            account_code=acc.code, account_name=acc.name,
            debit=_d2(e.get('debit', 0)), credit=_d2(e.get('credit', 0))))
    order.voucher_id = v.id
    order.status = 'done'
    db.session.commit()
    return v


@bp.route('/fin/cash', methods=['GET'])
@login_required
@permission_required('fin.cash.view')
def cash_orders():
    orders = FinCashOrder.query.order_by(FinCashOrder.trans_date.desc(), FinCashOrder.id.desc()).all()
    for o in orders:
        v = FinVoucher.query.get(o.voucher_id) if o.voucher_id else None
        o.can_delete = (v is None) or (v.status == 'draft')
        o.voucher_no = v.voucher_no if v else None
        ba = FinBankAccount.query.get(o.bank_account_id)
        o.bank_name = ba.name if ba else '—'
    accounts = FinBankAccount.query.order_by(FinBankAccount.code).all()
    # 对方科目下拉：启用的末级科目（排除 1002 类，避免选自己）
    others = (FinAccount.query.filter_by(enabled=True, is_leaf=True)
              .filter(FinAccount.code.notlike('1002%'))
              .order_by(FinAccount.code).all())
    return render_template('fin/cash_orders.html', orders=orders, accounts=accounts,
                           others=others, now=date.today(), default_period=_default_period())


@bp.route('/fin/cash/new', methods=['POST'])
@login_required
@permission_required('fin.cash.edit')
def cash_order_new():
    typ = request.form.get('type', 'receive').strip()
    if typ not in ('receive', 'pay'):
        typ = 'receive'
    baid = request.form.get('bank_account_id', '').strip()
    acode = request.form.get('account_code', '').strip()
    amount = _to_amount(request.form.get('amount', ''))
    summary = (request.form.get('summary') or '').strip()
    tdate = _parse_date(request.form.get('trans_date', ''))
    note = (request.form.get('note') or '').strip()
    ba = FinBankAccount.query.get(baid) if baid else None
    other = FinAccount.query.filter_by(code=acode).first() if acode else None
    if not ba:
        flash('请选择有效的银行账户')
        return redirect(url_for('fin.cash_orders'))
    if not other:
        flash('请选择有效的对方科目')
        return redirect(url_for('fin.cash_orders'))
    if amount <= 0:
        flash('金额必须大于 0')
        return redirect(url_for('fin.cash_orders'))
    if not tdate:
        flash('请填写有效的业务日期')
        return redirect(url_for('fin.cash_orders'))
    period = tdate.strftime('%Y-%m')
    if _period_locked(period):
        flash('期间 %s 已结账并锁定，不能在当期录入收付款单。如需补录请先到「期末结转」反结账' % period)
        return redirect(url_for('fin.cash_orders'))
    order = FinCashOrder(
        type=typ, bank_account_id=ba.id, account_code=other.code, account_name=other.name,
        amount=amount, summary=summary, trans_date=tdate, status='draft',
        created_by=session.get('user_id'), created_name=session.get('username', ''), note=note)
    order.code = _cash_next_code(typ, period)
    db.session.add(order)
    db.session.flush()
    maker = {'id': session.get('user_id'), 'name': session.get('username', '')}
    try:
        v = _mk_cash_voucher(order, maker)
    except ValueError as e:
        db.session.rollback()
        flash(str(e))
        return redirect(url_for('fin.cash_orders'))
    log_audit('add', module='fin', target_type='cash_order', target_id=order.id,
              target_no=order.code, detail='生成%s凭证 %s' % (order.type_cn, v.voucher_no))
    flash('%s单 %s 已生成凭证 %s（待会计审核记账）' % (order.type_cn, order.code, v.voucher_no))
    return redirect(url_for('fin.cash_orders'))


@bp.route('/fin/cash/<int:oid>/delete', methods=['POST'])
@login_required
@permission_required('fin.cash.edit')
def cash_order_delete(oid):
    order = FinCashOrder.query.get_or_404(oid)
    v = FinVoucher.query.get(order.voucher_id) if order.voucher_id else None
    if v and v.status != 'draft':
        flash('该收付款单关联的凭证 %s 已%s，不能从此处删除。如需更正请到凭证管理红字冲销'
              % (v.voucher_no, v.status_cn))
        return redirect(url_for('fin.cash_orders'))
    code = order.code
    if v:
        db.session.delete(v)
    db.session.delete(order)
    db.session.commit()
    log_audit('delete', module='fin', target_type='cash_order', target_no=code, detail='删除收付款单')
    flash('收付款单 %s 已删除' % code)
    return redirect(url_for('fin.cash_orders'))


# ===================== FMS · 数据体检（增量 #44，纯查询，不改账） =====================
EPS = 0.005  # 金额容差（分位）


def _run_audit(period):
    """数据体检引擎：纯查询、不改账。返回检查结果 dict。

    5 类检查：①试算平衡 ②资产负债勾稽 ③发票-凭证匹配 ④孤立数据 ⑤滞留提醒。
    每项 level：ok 绿 / warn 黄（提醒） / err 红（必须修）。
    """
    checks = []
    err_total = 0
    warn_total = 0

    # —— 检查1：试算平衡（全局，最硬）——
    # 每张凭证借方合计必须等于贷方合计（含红字冲销凭证，借贷仍应相等）。
    vouchers = FinVoucher.query.all()
    unbalanced = [v for v in vouchers
                  if abs(round((v.amount_debit or 0) - (v.amount_credit or 0), 2)) > EPS]
    lvl1 = 'ok' if not unbalanced else 'err'
    if unbalanced:
        err_total += 1
    checks.append({
        'key': 'trial', 'name': '试算平衡（凭证借贷相等）', 'level': lvl1,
        'count': len(unbalanced),
        'samples': [{'label': v.voucher_no,
                     'val': '借%.2f / 贷%.2f / 差%.2f'
                            % (v.amount_debit or 0, v.amount_credit or 0,
                               round((v.amount_debit or 0) - (v.amount_credit or 0), 2))}
                    for v in unbalanced[:10]],
        'hint': '每张凭证借方合计必须等于贷方合计；不等说明有脏数据或手动改库，必须修正',
    })

    # —— 检查2：资产负债勾稽（按期间，复用 _balance_rows）——
    # 会计恒等式（任何期间恒成立）：资产 + 成本 = 负债 + 权益 + 损益净额。
    # 资产/成本类为借方余额(正值)；负债/权益类为贷方余额(取正)；损益类净额结账后归零。
    rows, _ = _balance_rows(period)
    cat_net = {}
    for r in rows:
        cat_net[r['category']] = round(cat_net.get(r['category'], 0.0) + (r['end_d'] - r['end_c']), 2)
    asset = cat_net.get('asset', 0.0)
    cost = cat_net.get('cost', 0.0)
    le = -round(cat_net.get('liability', 0.0) + cat_net.get('equity', 0.0), 2)  # 贷方余额取正
    profit = cat_net.get('profit', 0.0)
    ext_diff = round((asset + cost) - (le + profit), 2)
    total_net = round(sum(r['end_d'] - r['end_c'] for r in rows), 2)  # 全局借贷净额，必为 0
    ok2 = abs(ext_diff) < EPS and abs(total_net) < EPS
    if not ok2:
        err_total += 1
    checks.append({
        'key': 'balance', 'name': '资产负债勾稽（%s 期末）' % period, 'level': 'ok' if ok2 else 'err',
        'count': 0 if ok2 else 1,
        'samples': [{'label': '资产净值', 'val': '%.2f' % asset},
                    {'label': '成本净值', 'val': '%.2f' % cost},
                    {'label': '负债+权益净值(贷正)', 'val': '%.2f' % le},
                    {'label': '损益净值', 'val': '%.2f' % profit},
                    {'label': '差额(应=0)', 'val': '%.2f' % ext_diff}] if not ok2 else [],
        'hint': '资产+成本 应等于 负债+权益+损益净值；不等说明科目类别标错或金额录错',
    })

    # —— 检查3：发票-凭证匹配 ——
    invoices = FinInvoice.query.all()
    no_vch = [iv for iv in invoices if not iv.voucher_id]          # 悬空发票（警告）
    inv_ids = {iv.id for iv in invoices}
    orphan_vch = [v for v in FinVoucher.query.filter_by(source_type='invoice').all()
                  if v.source_id not in inv_ids]                   # 凭证指向不存在发票（错误）
    lvl3 = 'ok'
    if orphan_vch:
        lvl3 = 'err'
    elif no_vch:
        lvl3 = 'warn'
    if orphan_vch:
        err_total += 1
    if no_vch:
        warn_total += 1
    checks.append({
        'key': 'invoice', 'name': '发票-凭证匹配', 'level': lvl3,
        'count': len(no_vch) + len(orphan_vch),
        'samples': [{'label': '发票无凭证(警告)', 'val': '%s …%s' % (iv.direction_cn, iv.invoice_no or iv.id)}
                    for iv in no_vch[:5]]
                  + [{'label': '凭证指向不存在发票(错误)', 'val': v.voucher_no} for v in orphan_vch[:5]],
        'hint': '发票应生成凭证（#38 自动记账）；凭证 source_type=invoice 必须能找到对应发票',
    })

    # —— 检查4：孤立数据扫描 ——
    acc_codes = {a.code for a in FinAccount.query.all()}
    bad_entries = (FinVoucherEntry.query.filter(FinVoucherEntry.account_code.notin_(list(acc_codes))).all()
                   if acc_codes else FinVoucherEntry.query.all())  # 分录科目码不在科目表
    src_map = {'invoice': FinInvoice, 'cash_receive': FinCashOrder, 'cash_pay': FinCashOrder,
               'commission': Commission, 'salary': Salary, 'bonus': BonusAllocation}
    src_orphan = []
    for v in FinVoucher.query.filter(FinVoucher.source_id.isnot(None),
                                     FinVoucher.source_type.in_(list(src_map.keys()))).all():
        if v.source_id and src_map[v.source_type].query.get(v.source_id) is None:
            src_orphan.append(v)                                  # 凭证来源单据已消失
    vch_ids = {v.id for v in FinVoucher.query.all()}
    co_orphan = [o for o in FinCashOrder.query.filter(FinCashOrder.voucher_id.isnot(None)).all()
                 if o.voucher_id not in vch_ids]
    bs_orphan = [s for s in FinBankStatement.query.filter(FinBankStatement.voucher_id.isnot(None)).all()
                 if s.voucher_id not in vch_ids]
    inv_orphan = [iv for iv in FinInvoice.query.filter(FinInvoice.voucher_id.isnot(None)).all()
                  if iv.voucher_id not in vch_ids]
    orphan_all = bad_entries + src_orphan + co_orphan + bs_orphan + inv_orphan
    if orphan_all:
        err_total += 1
    checks.append({
        'key': 'orphan', 'name': '孤立数据扫描', 'level': 'ok' if not orphan_all else 'err',
        'count': len(orphan_all),
        'samples': [{'label': '分录科目不存在', 'val': e.account_code} for e in bad_entries[:4]]
                  + [{'label': '凭证来源孤儿', 'val': '%s#%s' % (v.source_type, v.source_id)} for v in src_orphan[:3]]
                  + [{'label': '单据凭证孤儿', 'val': 'cash=%d/statement=%d/invoice=%d'
                                                    % (len(co_orphan), len(bs_orphan), len(inv_orphan))}],
        'hint': '分录科目码应在科目表内；收付款单/银行流水/发票的 voucher_id 必须指向存在的凭证',
    })

    # —— 检查5：滞留/异常提醒（黄色，非错误）——
    draft_n = FinVoucher.query.filter_by(status='draft').count()
    audited_n = FinVoucher.query.filter_by(status='audited').count()
    unrecon_n = FinBankStatement.query.filter_by(reconciled=False).count()
    uncert_n = FinInvoice.query.filter_by(direction='in', cert_status='uncert').count()
    pend = draft_n + audited_n + unrecon_n + uncert_n
    if pend:
        warn_total += 1
    checks.append({
        'key': 'pending', 'name': '滞留/异常提醒（非错误）', 'level': 'ok' if pend == 0 else 'warn',
        'count': pend,
        'samples': [{'label': '待审核凭证', 'val': draft_n},
                    {'label': '待记账凭证', 'val': audited_n},
                    {'label': '未对账银行流水', 'val': unrecon_n},
                    {'label': '待认证进项发票', 'val': uncert_n}],
        'hint': '黄色提醒，不影响账套正确性，但建议及时处理',
    })

    return {'period': period, 'checks': checks,
            'summary': {'err': err_total, 'warn': warn_total, 'total': len(checks)}}


@bp.route('/fin/data-check', methods=['GET'])
@login_required
@permission_required('fin.audit.view')
def audit_hub():
    """数据体检中心：一键跑全部检查，纯查询不改账。"""
    period = request.args.get('period') or _default_period()
    data = _run_audit(period)
    return render_template('fin/audit_hub.html', data=data, period=period)
