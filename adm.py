# adm.py
# 行政管理系统（ADM）· 一期（2026-10-01）
# 三本台账：证照资质 / 商务合同 / 固定资产。
#
# 定位（红线）：本系统只管"物"（台账登记 + 到期提醒），不管"钱"——
#   · 报销 / 请款 / 借支 → 走 OA 申请中心（本模块不做单据流、不生成凭证）
#   · 固定资产记账(1601) / 折旧凭证 → 走 FMS 财务（本模块只出台账，折旧在页面实时算）
#   · 员工劳动合同 / 代理合同 → 归 EHR（本模块的商务合同只管对外采购/租赁/服务）
#
# 蓝图名用 'adm'，避免与 IT 后台的 admin.py（蓝图 'admin'）撞名。
import os
import uuid
from datetime import datetime, date
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, flash, send_from_directory, current_app)
from models import (db, AdmCategory, AdmLicense, AdmContract, AdmAsset, User,
                    AdmSupply, AdmSupplyFlow, AdmSealUse, AdmExpress, AdmExpense,
                    AdmVehicle, AdmVehRecord)
from app import login_required, permission_required

bp = Blueprint('adm', __name__)

# 行政台账各本账的「管理」权限（分类设置页允许任一者维护）
ADM_MANAGE_CODES = ('adm.license.manage', 'adm.contract.manage', 'adm.asset.manage',
                    'adm.service.manage', 'adm.expense.manage', 'adm.vehicle.manage')

# 附件白名单与大小上限（与 OA 报销、EHR 休假保持一致的口径）
ADM_ATTACH_EXT = {'pdf', 'ofd', 'xml', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp',
                  'doc', 'docx', 'xls', 'xlsx', 'zip'}
ADM_ATTACH_MAX = 10 * 1024 * 1024  # 单文件 10MB

# 固定资产状态字典（页面下拉与展示共用）
ASSET_STATUS = [('in_use', '在用'), ('idle', '闲置'), ('repair', '维修中'), ('scrap', '已报废')]
CONTRACT_STATUS = [('active', '履行中'), ('ended', '已终止')]

# ⑥ 行政事务（二期）字典
FLOW_DIRS = [('in', '入库'), ('out', '领用')]
SEAL_USE_TYPES = [('seal', '现场加盖'), ('borrow', '借出使用')]
SEAL_STATUS = [('pending', '待盖章'), ('done', '已盖章'), ('returned', '已归还')]
EXPRESS_DIRS = [('in', '收件'), ('out', '寄件')]
EXPRESS_STATUS = [('pending', '待签收/待寄出'), ('done', '已签收/已寄出'), ('returned', '已退回')]

# ⑤ 费用台账（三期）字典
EXPENSE_STATUS = [('unpaid', '待缴'), ('paid', '已缴')]
PAY_MODES = [('bank', '银行转账'), ('cash', '现金'), ('other', '其他')]

# ⑦ 车辆管理（四期）字典
VEH_STATUS = [('in_use', '在用'), ('idle', '闲置'), ('repair', '维修中'), ('disposed', '已处置')]
VEH_REC_KINDS = [('maintain', '保养'), ('repair', '维修'), ('fine', '违章'), ('use', '用车')]
FINE_STATUS = [('pending', '未处理'), ('done', '已处理')]


# ===================== 通用工具 =====================

def _can(code):
    """当前登录用户是否拥有某权限码。"""
    uid = session.get('user_id')
    user = User.query.get(uid) if uid else None
    return bool(user and user.level and any(p.code == code for p in user.level.permissions))


def _any_manage():
    """是否拥有任一行政台账管理权（分类设置页用）。"""
    return any(_can(c) for c in ADM_MANAGE_CODES)


def _f(name):
    """取表单文本字段并去空白。"""
    return (request.form.get(name) or '').strip()


def _to_money(s):
    """表单金额 → float（容错：空/非法 → 0）。"""
    try:
        return round(float((s or '').replace(',', '').strip()), 2)
    except (ValueError, AttributeError):
        return 0.0


def _to_int(s, default=0):
    """表单整数 → int（容错）。"""
    try:
        return int(float((s or '').strip()))
    except (ValueError, AttributeError):
        return default


def _parse_date(s):
    """YYYY-MM-DD 字符串 → date 对象；空或非法返回 None（用于算到期天数、折旧月数）。"""
    s = (s or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _expire_state(expire_date, warn_days=30, danger_days=7):
    """按到期日算状态，返回 (状态, 距到期天数)。

    状态：none 长期有效 / ok 正常 / warn 即将到期（≤30 天）
         / danger 临期（≤7 天）/ expired 已过期
    """
    d = _parse_date(expire_date)
    if not d:
        return 'none', None
    n = (d - date.today()).days
    if n < 0:
        return 'expired', n
    if n <= danger_days:
        return 'danger', n
    if n <= warn_days:
        return 'warn', n
    return 'ok', n


def _due_state(row):
    """费用台账的缴费状态（实时算，不落库）。

    返回 (状态, 距截止天数)：paid 已缴 / overdue 已逾期 / danger 临期（≤7 天）
         / warn 临近（≤30 天）/ ok 正常 / none 未设截止日
    """
    if (row.status or '') == 'paid':
        return 'paid', None
    d = _parse_date(row.due_date)
    if not d:
        return 'none', None
    n = (d - date.today()).days
    if n < 0:
        return 'overdue', n
    if n <= 7:
        return 'danger', n
    if n <= 30:
        return 'warn', n
    return 'ok', n


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


def _cats(kind):
    """取某分类域下已启用的分类名列表（按 sort 排序）。"""
    rows = AdmCategory.query.filter_by(kind=kind).order_by(AdmCategory.sort, AdmCategory.id).all()
    return [c.name for c in rows if c.enabled == '1']


# ---------- 附件（单个文件：原文件名 + 落盘文件名）----------

def _attach_dir(sub):
    """附件目录 static/uploads/adm/<sub>/（不存在则建）。"""
    d = os.path.join(current_app.config['UPLOAD_DIR'], 'adm', sub)
    os.makedirs(d, exist_ok=True)
    return d


def _save_file(fs, sub, code):
    """保存单个附件，返回 (原文件名, 落盘文件名, 错误说明)。无文件时全部为 None。"""
    if not fs or not (fs.filename or '').strip():
        return None, None, None
    fn = fs.filename.strip()
    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
    if ext not in ADM_ATTACH_EXT:
        return None, None, '%s：不支持的格式' % fn
    data = fs.read()
    if len(data) > ADM_ATTACH_MAX:
        return None, None, '%s：超过 10MB' % fn
    disk = '%s_%s.%s' % ((code or 'ADM').replace('-', ''), uuid.uuid4().hex[:8], ext)
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


def _depreciation(a):
    """年限平均法算折旧，返回 dict（不落库，页面实时算）。

    月折旧额 = 原值 ×(1 − 残值率) ÷ (年限 × 12)
    已提月数 = 购置日 → 今天的整月数（封顶到 年限×12）
    """
    price = a.price or 0
    life = a.life_years or 0
    rate = (a.salvage_rate or 0) / 100.0
    if price <= 0 or life <= 0:
        return {'monthly': 0.0, 'months': 0, 'acc': 0.0, 'net': round(price, 2)}
    salvage = price * rate
    monthly = (price - salvage) / (life * 12)
    buy = _parse_date(a.buy_date)
    if not buy:
        return {'monthly': round(monthly, 2), 'months': 0, 'acc': 0.0, 'net': round(price, 2)}
    months = (date.today().year - buy.year) * 12 + (date.today().month - buy.month)
    months = max(0, min(months, life * 12))
    acc = min(monthly * months, price - salvage)
    return {'monthly': round(monthly, 2), 'months': months,
            'acc': round(acc, 2), 'net': round(price - acc, 2)}


# ===================== ① 证照资质 =====================

@bp.route('/adm/licenses')
@login_required
@permission_required('adm.view')
def licenses():
    """证照资质台账：列表 + 关键词/类型/到期状态筛选，到期红黄绿灯。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = AdmLicense.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmLicense.name.like(like), AdmLicense.no.like(like),
                            AdmLicense.keeper.like(like), AdmLicense.issuer.like(like)))
    if cat:
        q = q.filter_by(category=cat)
    items = []
    for r in q.order_by(AdmLicense.id.desc()).all():
        state, days = _expire_state(r.expire_date)
        if st and st != state:
            continue
        items.append({'r': r, 'state': state, 'days': days})
    alert_n = sum(1 for x in items if x['state'] in ('warn', 'danger', 'expired'))
    return render_template('adm/license_list.html', items=items, cats=_cats('license'),
                           kw=kw, cat=cat, st=st, alert_n=alert_n,
                           can_edit=_can('adm.license.manage'))


@bp.route('/adm/licenses/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.license.manage')
def license_new():
    """登记新证照。"""
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('证照名称不能为空')
            return redirect(url_for('adm.license_new'))
        code = _next_code(AdmLicense, 'ZZ')
        fn, disk, err = _save_file(request.files.get('file'), 'license', code)
        if err:
            flash(err)
            return redirect(url_for('adm.license_new'))
        db.session.add(AdmLicense(
            code=code, name=name, category=_f('category'), no=_f('no'),
            issuer=_f('issuer'), issue_date=_f('issue_date'), expire_date=_f('expire_date'),
            keeper=_f('keeper'), place=_f('place'), note=_f('note'),
            file_name=fn, file_path=disk, created_by=session.get('user_id')))
        db.session.commit()
        flash('证照「%s」已登记，编号 %s' % (name, code))
        return redirect(url_for('adm.licenses'))
    return render_template('adm/license_form.html', row=None, cats=_cats('license'))


@bp.route('/adm/licenses/<int:lid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.license.manage')
def license_edit(lid):
    """修改证照（可换附件）。"""
    r = AdmLicense.query.get_or_404(lid)
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('证照名称不能为空')
            return redirect(url_for('adm.license_edit', lid=lid))
        fn, disk, err = _save_file(request.files.get('file'), 'license', r.code)
        if err:
            flash(err)
            return redirect(url_for('adm.license_edit', lid=lid))
        r.name, r.category, r.no = name, _f('category'), _f('no')
        r.issuer, r.issue_date, r.expire_date = _f('issuer'), _f('issue_date'), _f('expire_date')
        r.keeper, r.place, r.note = _f('keeper'), _f('place'), _f('note')
        if disk:  # 换了新附件 → 删旧文件
            _drop_file('license', r.file_path)
            r.file_name, r.file_path = fn, disk
        db.session.commit()
        flash('证照「%s」已更新' % name)
        return redirect(url_for('adm.licenses'))
    return render_template('adm/license_form.html', row=r, cats=_cats('license'))


@bp.route('/adm/licenses/<int:lid>/delete', methods=['POST'])
@login_required
@permission_required('adm.license.manage')
def license_delete(lid):
    """删除证照（连同附件）。"""
    r = AdmLicense.query.get_or_404(lid)
    _drop_file('license', r.file_path)
    db.session.delete(r)
    db.session.commit()
    flash('证照已删除')
    return redirect(url_for('adm.licenses'))


@bp.route('/adm/licenses/<int:lid>/file')
@login_required
@permission_required('adm.view')
def license_file(lid):
    """下载证照扫描件（鉴权下载，不暴露静态目录）。"""
    r = AdmLicense.query.get_or_404(lid)
    if not r.file_path:
        flash('该证照未上传附件')
        return redirect(url_for('adm.licenses'))
    return send_from_directory(_attach_dir('license'), r.file_path,
                               as_attachment=True, download_name=r.file_name or r.file_path)


# ===================== ② 商务合同 =====================

@bp.route('/adm/contracts')
@login_required
@permission_required('adm.view')
def contracts():
    """商务合同台账：列表 + 关键词/类型/到期状态筛选，到期与续签提醒。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = AdmContract.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmContract.name.like(like), AdmContract.party.like(like),
                            AdmContract.handler.like(like)))
    if cat:
        q = q.filter_by(category=cat)
    items, total = [], 0.0
    for r in q.order_by(AdmContract.id.desc()).all():
        state, days = _expire_state(r.end_date)
        if st and st != state:
            continue
        total += (r.amount or 0)
        items.append({'r': r, 'state': state, 'days': days})
    alert_n = sum(1 for x in items if x['state'] in ('warn', 'danger', 'expired'))
    return render_template('adm/contract_list.html', items=items, cats=_cats('contract'),
                           kw=kw, cat=cat, st=st, alert_n=alert_n, total=round(total, 2),
                           can_edit=_can('adm.contract.manage'))


@bp.route('/adm/contracts/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.contract.manage')
def contract_new():
    """登记新商务合同。"""
    assets = AdmAsset.query.order_by(AdmAsset.id.desc()).all()
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('合同名称不能为空')
            return redirect(url_for('adm.contract_new'))
        code = _next_code(AdmContract, 'SWHT')
        fn, disk, err = _save_file(request.files.get('file'), 'contract', code)
        if err:
            flash(err)
            return redirect(url_for('adm.contract_new'))
        db.session.add(AdmContract(
            code=code, name=name, category=_f('category'), party=_f('party'),
            sign_date=_f('sign_date'), start_date=_f('start_date'), end_date=_f('end_date'),
            amount=_to_money(request.form.get('amount')), pay_mode=_f('pay_mode'),
            pay_plan=_f('pay_plan'), handler=_f('handler'), dept=_f('dept'),
            asset_id=_to_int(request.form.get('asset_id')) or None,
            status=_f('status') or 'active', note=_f('note'),
            file_name=fn, file_path=disk, created_by=session.get('user_id')))
        db.session.commit()
        flash('合同「%s」已登记，编号 %s' % (name, code))
        return redirect(url_for('adm.contracts'))
    return render_template('adm/contract_form.html', row=None, cats=_cats('contract'),
                           assets=assets, statuses=CONTRACT_STATUS)


@bp.route('/adm/contracts/<int:cid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.contract.manage')
def contract_edit(cid):
    """修改商务合同（可换附件）。"""
    r = AdmContract.query.get_or_404(cid)
    assets = AdmAsset.query.order_by(AdmAsset.id.desc()).all()
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('合同名称不能为空')
            return redirect(url_for('adm.contract_edit', cid=cid))
        fn, disk, err = _save_file(request.files.get('file'), 'contract', r.code)
        if err:
            flash(err)
            return redirect(url_for('adm.contract_edit', cid=cid))
        r.name, r.category, r.party = name, _f('category'), _f('party')
        r.sign_date, r.start_date, r.end_date = _f('sign_date'), _f('start_date'), _f('end_date')
        r.amount = _to_money(request.form.get('amount'))
        r.pay_mode, r.pay_plan = _f('pay_mode'), _f('pay_plan')
        r.handler, r.dept = _f('handler'), _f('dept')
        r.asset_id = _to_int(request.form.get('asset_id')) or None
        r.status, r.note = _f('status') or 'active', _f('note')
        if disk:
            _drop_file('contract', r.file_path)
            r.file_name, r.file_path = fn, disk
        db.session.commit()
        flash('合同「%s」已更新' % name)
        return redirect(url_for('adm.contracts'))
    return render_template('adm/contract_form.html', row=r, cats=_cats('contract'),
                           assets=assets, statuses=CONTRACT_STATUS)


@bp.route('/adm/contracts/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('adm.contract.manage')
def contract_delete(cid):
    """删除商务合同（连同附件）。"""
    r = AdmContract.query.get_or_404(cid)
    _drop_file('contract', r.file_path)
    db.session.delete(r)
    db.session.commit()
    flash('合同已删除')
    return redirect(url_for('adm.contracts'))


@bp.route('/adm/contracts/<int:cid>/file')
@login_required
@permission_required('adm.view')
def contract_file(cid):
    """下载合同电子版（鉴权下载）。"""
    r = AdmContract.query.get_or_404(cid)
    if not r.file_path:
        flash('该合同未上传附件')
        return redirect(url_for('adm.contracts'))
    return send_from_directory(_attach_dir('contract'), r.file_path,
                               as_attachment=True, download_name=r.file_name or r.file_path)


# ===================== ③ 固定资产 =====================

@bp.route('/adm/assets')
@login_required
@permission_required('adm.view')
def assets():
    """固定资产台账：列表 + 关键词/类别/状态筛选，实时折旧与净值。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = AdmAsset.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmAsset.name.like(like), AdmAsset.code.like(like),
                            AdmAsset.keeper.like(like), AdmAsset.spec.like(like)))
    if cat:
        q = q.filter_by(category=cat)
    if st:
        q = q.filter_by(status=st)
    items, total, net_total = [], 0.0, 0.0
    for r in q.order_by(AdmAsset.id.desc()).all():
        d = _depreciation(r)
        total += (r.price or 0)
        net_total += d['net']
        items.append({'r': r, 'dep': d})
    return render_template('adm/asset_list.html', items=items, cats=_cats('asset'),
                           kw=kw, cat=cat, st=st, statuses=ASSET_STATUS,
                           total=round(total, 2), net_total=round(net_total, 2),
                           can_edit=_can('adm.asset.manage'))


@bp.route('/adm/assets/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.asset.manage')
def asset_new():
    """登记新固定资产。"""
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('资产名称不能为空')
            return redirect(url_for('adm.asset_new'))
        code = _next_code(AdmAsset, 'ZC')
        fn, disk, err = _save_file(request.files.get('file'), 'asset', code)
        if err:
            flash(err)
            return redirect(url_for('adm.asset_new'))
        db.session.add(AdmAsset(
            code=code, name=name, category=_f('category'), spec=_f('spec'),
            price=_to_money(request.form.get('price')), buy_date=_f('buy_date'),
            life_years=_to_int(request.form.get('life_years'), 3),
            salvage_rate=_to_money(request.form.get('salvage_rate')),
            dept=_f('dept'), keeper=_f('keeper'), place=_f('place'),
            status=_f('status') or 'in_use', note=_f('note'),
            file_name=fn, file_path=disk, created_by=session.get('user_id')))
        db.session.commit()
        flash('资产「%s」已登记，编号 %s' % (name, code))
        return redirect(url_for('adm.assets'))
    return render_template('adm/asset_form.html', row=None, cats=_cats('asset'),
                           statuses=ASSET_STATUS)


@bp.route('/adm/assets/<int:aid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.asset.manage')
def asset_edit(aid):
    """修改固定资产（可换附件）。"""
    r = AdmAsset.query.get_or_404(aid)
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('资产名称不能为空')
            return redirect(url_for('adm.asset_edit', aid=aid))
        fn, disk, err = _save_file(request.files.get('file'), 'asset', r.code)
        if err:
            flash(err)
            return redirect(url_for('adm.asset_edit', aid=aid))
        r.name, r.category, r.spec = name, _f('category'), _f('spec')
        r.price = _to_money(request.form.get('price'))
        r.buy_date = _f('buy_date')
        r.life_years = _to_int(request.form.get('life_years'), 3)
        r.salvage_rate = _to_money(request.form.get('salvage_rate'))
        r.dept, r.keeper, r.place = _f('dept'), _f('keeper'), _f('place')
        r.status, r.note = _f('status') or 'in_use', _f('note')
        if disk:
            _drop_file('asset', r.file_path)
            r.file_name, r.file_path = fn, disk
        db.session.commit()
        flash('资产「%s」已更新' % name)
        return redirect(url_for('adm.assets'))
    return render_template('adm/asset_form.html', row=r, cats=_cats('asset'),
                           statuses=ASSET_STATUS)


@bp.route('/adm/assets/<int:aid>/delete', methods=['POST'])
@login_required
@permission_required('adm.asset.manage')
def asset_delete(aid):
    """删除固定资产（连同附件）。"""
    r = AdmAsset.query.get_or_404(aid)
    _drop_file('asset', r.file_path)
    db.session.delete(r)
    db.session.commit()
    flash('资产已删除')
    return redirect(url_for('adm.assets'))


@bp.route('/adm/assets/<int:aid>/file')
@login_required
@permission_required('adm.view')
def asset_file(aid):
    """下载资产凭证/发票（鉴权下载）。"""
    r = AdmAsset.query.get_or_404(aid)
    if not r.file_path:
        flash('该资产未上传附件')
        return redirect(url_for('adm.assets'))
    return send_from_directory(_attach_dir('asset'), r.file_path,
                               as_attachment=True, download_name=r.file_name or r.file_path)


# ===================== ⑥ 行政事务（二期，2026-10-01） =====================
# 三本子台账：办公用品（带库存流水）/ 印章使用登记 / 快递收发。
# 边界不变：本模块只做"登记 + 提醒"——用章申请走 OA 审批中心，相关付款走 OA / FMS。

@bp.route('/adm/services')
@login_required
@permission_required('adm.view')
def services():
    """行政事务总览：办公用品 / 印章使用登记 / 快递收发 三本子台账入口 + 关键数字。"""
    supplies = AdmSupply.query.all()
    low = [s for s in supplies
           if (s.min_stock or 0) > 0 and (s.stock or 0) <= (s.min_stock or 0)]
    return render_template(
        'adm/service_home.html',
        supply_n=len(supplies), supply_low=len(low),
        seal_n=AdmSealUse.query.count(),
        seal_pending=AdmSealUse.query.filter_by(status='pending').count(),
        exp_n=AdmExpress.query.count(),
        exp_pending=AdmExpress.query.filter_by(status='pending').count(),
        can_edit=_can('adm.service.manage'))


# ---------- ⑥-1 办公用品（台账 + 出入库流水） ----------

@bp.route('/adm/supplies')
@login_required
@permission_required('adm.view')
def supplies():
    """办公用品台账：列表 + 关键词/类别/库存状态筛选，低于安全库存预警。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()       # low 库存预警 / zero 零库存
    q = AdmSupply.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmSupply.name.like(like), AdmSupply.spec.like(like),
                            AdmSupply.place.like(like)))
    if cat:
        q = q.filter_by(category=cat)
    items = []
    for r in q.order_by(AdmSupply.id.desc()).all():
        alert = bool((r.min_stock or 0) > 0 and (r.stock or 0) <= (r.min_stock or 0))
        if st == 'low' and not alert:
            continue
        if st == 'zero' and (r.stock or 0) > 0:
            continue
        items.append({'r': r, 'alert': alert})
    return render_template('adm/supply_list.html', items=items, cats=_cats('supply'),
                           kw=kw, cat=cat, st=st,
                           alert_n=sum(1 for x in items if x['alert']),
                           can_edit=_can('adm.service.manage'))


@bp.route('/adm/supplies/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def supply_new():
    """新建办公用品（建档时可填初始库存）。"""
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('用品名称不能为空')
            return redirect(url_for('adm.supply_new'))
        code = _next_code(AdmSupply, 'BYP')
        db.session.add(AdmSupply(
            code=code, name=name, category=_f('category'), spec=_f('spec'), unit=_f('unit'),
            stock=_to_money(_f('stock')), min_stock=_to_money(_f('min_stock')),
            place=_f('place'), note=_f('note'), created_by=session.get('user_id')))
        db.session.commit()
        flash('办公用品「%s」已建档，编号 %s' % (name, code))
        return redirect(url_for('adm.supplies'))
    return render_template('adm/supply_form.html', row=None, cats=_cats('supply'))


@bp.route('/adm/supplies/<int:sid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def supply_edit(sid):
    """修改办公用品档案。

    注意：**库存不在本页直改**，一律通过「出入库登记」变动，保证账实相符。
    """
    r = AdmSupply.query.get_or_404(sid)
    if request.method == 'POST':
        name = _f('name')
        if not name:
            flash('用品名称不能为空')
            return redirect(url_for('adm.supply_edit', sid=sid))
        r.name, r.category, r.spec, r.unit = name, _f('category'), _f('spec'), _f('unit')
        r.min_stock, r.place, r.note = _to_money(_f('min_stock')), _f('place'), _f('note')
        db.session.commit()
        flash('办公用品「%s」已更新' % name)
        return redirect(url_for('adm.supplies'))
    return render_template('adm/supply_form.html', row=r, cats=_cats('supply'))


@bp.route('/adm/supplies/<int:sid>/delete', methods=['POST'])
@login_required
@permission_required('adm.service.manage')
def supply_delete(sid):
    """删除办公用品（连同其出入库流水）。"""
    r = AdmSupply.query.get_or_404(sid)
    AdmSupplyFlow.query.filter_by(supply_id=sid).delete()
    db.session.delete(r)
    db.session.commit()
    flash('办公用品已删除')
    return redirect(url_for('adm.supplies'))


@bp.route('/adm/supplies/<int:sid>/flow', methods=['GET', 'POST'])
@login_required
@permission_required('adm.view')
def supply_flow(sid):
    """出入库登记：GET 看该用品与历史流水；POST 登记一笔并自动增减库存。"""
    r = AdmSupply.query.get_or_404(sid)
    if request.method == 'POST':
        if not _can('adm.service.manage'):
            flash('无权限登记出入库')
            return redirect(url_for('adm.supply_flow', sid=sid))
        direction = _f('direction') or 'out'
        if direction not in ('in', 'out'):
            flash('出入库方向不合法')
            return redirect(url_for('adm.supply_flow', sid=sid))
        qty = _to_money(_f('qty'))
        if qty <= 0:
            flash('数量必须大于 0')
            return redirect(url_for('adm.supply_flow', sid=sid))
        if direction == 'out' and qty > (r.stock or 0):
            flash('领用数量超过当前库存（现有 %g %s）' % (r.stock or 0, r.unit or ''))
            return redirect(url_for('adm.supply_flow', sid=sid))
        r.stock = round((r.stock or 0) + (qty if direction == 'in' else -qty), 2)
        db.session.add(AdmSupplyFlow(
            supply_id=sid, direction=direction, qty=qty, who=_f('who'), dept=_f('dept'),
            flow_date=_f('flow_date') or datetime.now().strftime('%Y-%m-%d'),
            purpose=_f('purpose'), operator=_f('operator'), note=_f('note'),
            created_by=session.get('user_id')))
        db.session.commit()
        flash('已登记%s %g %s，当前库存 %g %s'
              % ('入库' if direction == 'in' else '领用', qty, r.unit or '',
                 r.stock, r.unit or ''))
        return redirect(url_for('adm.supply_flow', sid=sid))
    flows = AdmSupplyFlow.query.filter_by(supply_id=sid).order_by(AdmSupplyFlow.id.desc()).all()
    return render_template('adm/supply_flow.html', r=r, flows=flows,
                           today=datetime.now().strftime('%Y-%m-%d'),
                           can_edit=_can('adm.service.manage'))


@bp.route('/adm/supplies/flow/<int:fid>/delete', methods=['POST'])
@login_required
@permission_required('adm.service.manage')
def supply_flow_delete(fid):
    """撤销一笔出入库流水（按相反方向回滚库存）。"""
    f = AdmSupplyFlow.query.get_or_404(fid)
    sid = f.supply_id
    r = AdmSupply.query.get(sid)
    if r:  # 用品还在 → 回滚库存
        r.stock = round((r.stock or 0) + (-(f.qty or 0) if f.direction == 'in' else (f.qty or 0)), 2)
    db.session.delete(f)
    db.session.commit()
    flash('出入库记录已撤销，库存已回滚')
    return redirect(url_for('adm.supply_flow', sid=sid))


# ---------- ⑥-2 印章使用登记 ----------

@bp.route('/adm/seals')
@login_required
@permission_required('adm.view')
def seals():
    """印章使用登记台账：列表 + 关键词/印章/状态筛选。"""
    kw = (request.args.get('kw') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = AdmSealUse.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmSealUse.seal_name.like(like), AdmSealUse.applicant.like(like),
                            AdmSealUse.purpose.like(like), AdmSealUse.doc_name.like(like),
                            AdmSealUse.code.like(like)))
    items = [r for r in q.order_by(AdmSealUse.id.desc()).all() if not st or st == (r.status or '')]
    return render_template('adm/seal_list.html', items=items, kw=kw, st=st,
                           seal_names=_cats('seal'), status_list=SEAL_STATUS,
                           pending_n=AdmSealUse.query.filter_by(status='pending').count(),
                           can_edit=_can('adm.service.manage'))


@bp.route('/adm/seals/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def seal_new():
    """登记一次用章。"""
    if request.method == 'POST':
        seal_name = _f('seal_name')
        if not seal_name:
            flash('印章名称不能为空')
            return redirect(url_for('adm.seal_new'))
        code = _next_code(AdmSealUse, 'YZ')
        db.session.add(AdmSealUse(
            code=code, seal_name=seal_name,
            use_date=_f('use_date') or datetime.now().strftime('%Y-%m-%d'),
            applicant=_f('applicant'), dept=_f('dept'),
            use_type=_f('use_type') or 'seal', purpose=_f('purpose'), doc_name=_f('doc_name'),
            copies=_to_int(_f('copies'), 1), approval_code=_f('approval_code'),
            keeper=_f('keeper'), status=_f('status') or 'done', note=_f('note'),
            created_by=session.get('user_id')))
        db.session.commit()
        flash('用章登记已提交，编号 %s' % code)
        return redirect(url_for('adm.seals'))
    return render_template('adm/seal_form.html', row=None, seal_names=_cats('seal'),
                           use_types=SEAL_USE_TYPES, status_list=SEAL_STATUS,
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/adm/seals/<int:vid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def seal_edit(vid):
    """修改用章登记（含状态流转：待盖章 → 已盖章 → 已归还）。"""
    r = AdmSealUse.query.get_or_404(vid)
    if request.method == 'POST':
        seal_name = _f('seal_name')
        if not seal_name:
            flash('印章名称不能为空')
            return redirect(url_for('adm.seal_edit', vid=vid))
        r.seal_name, r.use_date = seal_name, _f('use_date')
        r.applicant, r.dept = _f('applicant'), _f('dept')
        r.use_type, r.purpose, r.doc_name = _f('use_type') or 'seal', _f('purpose'), _f('doc_name')
        r.copies, r.approval_code = _to_int(_f('copies'), 1), _f('approval_code')
        r.keeper, r.status, r.note = _f('keeper'), _f('status') or 'done', _f('note')
        db.session.commit()
        flash('用章登记已更新')
        return redirect(url_for('adm.seals'))
    return render_template('adm/seal_form.html', row=r, seal_names=_cats('seal'),
                           use_types=SEAL_USE_TYPES, status_list=SEAL_STATUS,
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/adm/seals/<int:vid>/delete', methods=['POST'])
@login_required
@permission_required('adm.service.manage')
def seal_delete(vid):
    """删除用章登记。"""
    r = AdmSealUse.query.get_or_404(vid)
    db.session.delete(r)
    db.session.commit()
    flash('用章登记已删除')
    return redirect(url_for('adm.seals'))


# ---------- ⑥-3 快递收发登记 ----------

@bp.route('/adm/express')
@login_required
@permission_required('adm.view')
def express_list():
    """快递收发台账：列表 + 关键词/收发方向/状态筛选。"""
    kw = (request.args.get('kw') or '').strip()
    dr = (request.args.get('dr') or '').strip()
    st = (request.args.get('st') or '').strip()
    q = AdmExpress.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmExpress.waybill.like(like), AdmExpress.counterpart.like(like),
                            AdmExpress.handler.like(like), AdmExpress.item.like(like),
                            AdmExpress.code.like(like), AdmExpress.company.like(like)))
    if dr:
        q = q.filter_by(direction=dr)
    items = [r for r in q.order_by(AdmExpress.id.desc()).all() if not st or st == (r.status or '')]
    return render_template('adm/express_list.html', items=items, kw=kw, dr=dr, st=st,
                           dirs=EXPRESS_DIRS, status_list=EXPRESS_STATUS,
                           fee_total=round(sum((x.fee or 0) for x in items
                                               if x.direction == 'out'), 2),
                           pending_n=AdmExpress.query.filter_by(status='pending').count(),
                           can_edit=_can('adm.service.manage'))


@bp.route('/adm/express/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def express_new():
    """登记一条快递收件 / 寄件。"""
    if request.method == 'POST':
        direction = _f('direction') or 'in'
        if direction not in ('in', 'out'):
            flash('收发方向不合法')
            return redirect(url_for('adm.express_new'))
        if not _f('counterpart') and not _f('waybill'):
            flash('对方名称与运单号至少填一项')
            return redirect(url_for('adm.express_new'))
        code = _next_code(AdmExpress, 'KD')
        db.session.add(AdmExpress(
            code=code, direction=direction, company=_f('company'), waybill=_f('waybill'),
            counterpart=_f('counterpart'), handler=_f('handler'), dept=_f('dept'),
            item=_f('item'), fee=_to_money(_f('fee')),
            express_date=_f('express_date') or datetime.now().strftime('%Y-%m-%d'),
            status=_f('status') or 'done', note=_f('note'),
            created_by=session.get('user_id')))
        db.session.commit()
        flash('快递登记已提交，编号 %s' % code)
        return redirect(url_for('adm.express_list'))
    return render_template('adm/express_form.html', row=None, dirs=EXPRESS_DIRS,
                           status_list=EXPRESS_STATUS,
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/adm/express/<int:xid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.service.manage')
def express_edit(xid):
    """修改快递登记（含状态流转：待签收/待寄出 → 已签收/已寄出 → 已退回）。"""
    r = AdmExpress.query.get_or_404(xid)
    if request.method == 'POST':
        direction = _f('direction') or 'in'
        if direction not in ('in', 'out'):
            flash('收发方向不合法')
            return redirect(url_for('adm.express_edit', xid=xid))
        r.direction, r.company, r.waybill = direction, _f('company'), _f('waybill')
        r.counterpart, r.handler, r.dept = _f('counterpart'), _f('handler'), _f('dept')
        r.item, r.fee = _f('item'), _to_money(_f('fee'))
        r.express_date, r.status, r.note = _f('express_date'), _f('status') or 'done', _f('note')
        db.session.commit()
        flash('快递登记已更新')
        return redirect(url_for('adm.express_list'))
    return render_template('adm/express_form.html', row=r, dirs=EXPRESS_DIRS,
                           status_list=EXPRESS_STATUS,
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/adm/express/<int:xid>/delete', methods=['POST'])
@login_required
@permission_required('adm.service.manage')
def express_delete(xid):
    """删除快递登记。"""
    r = AdmExpress.query.get_or_404(xid)
    db.session.delete(r)
    db.session.commit()
    flash('快递登记已删除')
    return redirect(url_for('adm.express_list'))


# ===================== ⑤ 费用台账（三期，2026-10-01） =====================
# 周期性行政费用（水费 / 电费 / 物业费 / 房租 / 宽带通讯…）的「登记 + 缴费提醒」。
# 边界不变：本模块只登记台账、只做提醒——需要付款时走 OA 申请中心，
# 本表用 approval_code 回填关联单号，绝不在这里产生付款单据或凭证。

@bp.route('/adm/expenses')
@login_required
@permission_required('adm.view')
def expenses():
    """费用台账：列表（筛选）+ 年度汇总（按月 / 按类型）+ 缴费提醒（逾期 / 临期）。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()   # unpaid 待缴 / paid 已缴 / overdue 已逾期 / alert 需缴费
    year = (request.args.get('year') or str(date.today().year)).strip()

    all_rows = AdmExpense.query.all()
    # 年度下拉：台账里出现过的年份 + 今年（倒序）
    years = sorted({(r.period or '')[:4] for r in all_rows if (r.period or '')[:4].isdigit()},
                   reverse=True)
    if str(date.today().year) not in years:
        years.insert(0, str(date.today().year))

    # —— 年度汇总（该年度全量，不受下方关键词 / 类型 / 状态筛选影响）——
    year_rows = [r for r in all_rows if (r.period or '').startswith(year)]
    due_sum = round(sum(r.amount or 0 for r in year_rows), 2)
    paid_sum = round(sum(r.amount or 0 for r in year_rows if (r.status or '') == 'paid'), 2)
    by_month = [{'m': i, 'due': 0.0, 'paid': 0.0} for i in range(1, 13)]
    by_cat = {}
    for r in year_rows:
        try:
            mi = int((r.period or '')[-2:])
        except ValueError:
            mi = 0
        if 1 <= mi <= 12:
            by_month[mi - 1]['due'] = round(by_month[mi - 1]['due'] + (r.amount or 0), 2)
            if (r.status or '') == 'paid':
                by_month[mi - 1]['paid'] = round(by_month[mi - 1]['paid'] + (r.amount or 0), 2)
        d = by_cat.setdefault(r.category or '未分类', {'n': 0, 'amount': 0.0, 'paid': 0.0})
        d['n'] += 1
        d['amount'] = round(d['amount'] + (r.amount or 0), 2)
        if (r.status or '') == 'paid':
            d['paid'] = round(d['paid'] + (r.amount or 0), 2)
    by_cat_list = sorted([{'cat': k, 'n': v['n'], 'amount': v['amount'], 'paid': v['paid']}
                          for k, v in by_cat.items()], key=lambda x: -x['amount'])
    alert_rows = [r for r in year_rows if _due_state(r)[0] in ('overdue', 'danger')]

    # —— 列表（应用全部筛选条件）——
    q = AdmExpense.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmExpense.code.like(like), AdmExpense.category.like(like),
                            AdmExpense.payee.like(like), AdmExpense.invoice_no.like(like),
                            AdmExpense.handler.like(like), AdmExpense.note.like(like),
                            AdmExpense.approval_code.like(like)))
    if cat:
        q = q.filter_by(category=cat)
    if year:
        q = q.filter(AdmExpense.period.like(year + '%'))
    items = []
    for r in q.order_by(AdmExpense.period.desc(), AdmExpense.id.desc()).all():
        s, n = _due_state(r)
        if st == 'unpaid' and (r.status or '') == 'paid':
            continue
        if st == 'paid' and (r.status or '') != 'paid':
            continue
        if st == 'overdue' and s != 'overdue':
            continue
        if st == 'alert' and s not in ('overdue', 'danger'):
            continue
        items.append({'r': r, 'state': s, 'days': n})
    return render_template(
        'adm/expense_list.html', items=items, cats=_cats('expense'),
        kw=kw, cat=cat, st=st, year=year, years=years,
        due_sum=due_sum, paid_sum=paid_sum, unpaid_sum=round(due_sum - paid_sum, 2),
        overdue_n=sum(1 for r in year_rows if _due_state(r)[0] == 'overdue'),
        alert_n=len(alert_rows), by_month=by_month, by_cat=by_cat_list,
        contracts={c.id: c.name for c in AdmContract.query.all()},
        assets={a.id: a.name for a in AdmAsset.query.all()},
        can_edit=_can('adm.expense.manage'))


def _expense_form(row):
    """费用台账表单页渲染（新建 / 编辑共用，带上关联合同与资产下拉）。"""
    return render_template(
        'adm/expense_form.html', row=row, cats=_cats('expense'),
        statuses=EXPENSE_STATUS, pay_modes=PAY_MODES,
        contracts=AdmContract.query.order_by(AdmContract.id.desc()).all(),
        assets=AdmAsset.query.order_by(AdmAsset.id.desc()).all(),
        today=datetime.now().strftime('%Y-%m-%d'),
        this_month=datetime.now().strftime('%Y-%m'))


@bp.route('/adm/expenses/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.expense.manage')
def expense_new():
    """登记一笔行政费用（水电 / 物业 / 房租 / 通讯…）。"""
    if request.method == 'POST':
        category = _f('category')
        if not category:
            flash('请选择费用类型')
            return redirect(url_for('adm.expense_new'))
        period = _f('period') or datetime.now().strftime('%Y-%m')
        code = _next_code(AdmExpense, 'FY')
        fn, disk, err = _save_file(request.files.get('attach'), 'expense', code)
        if err:
            flash(err)
            return redirect(url_for('adm.expense_new'))
        db.session.add(AdmExpense(
            code=code, category=category, period=period, amount=_to_money(_f('amount')),
            payee=_f('payee'), due_date=_f('due_date'), pay_date=_f('pay_date'),
            pay_mode=_f('pay_mode'), invoice_no=_f('invoice_no'),
            contract_id=_to_int(_f('contract_id')) or None,
            asset_id=_to_int(_f('asset_id')) or None,
            handler=_f('handler'), dept=_f('dept'), approval_code=_f('approval_code'),
            status='paid' if _f('status') == 'paid' else 'unpaid',
            file_name=fn, file_path=disk, note=_f('note'),
            created_by=session.get('user_id')))
        db.session.commit()
        flash('费用已登记，编号 %s' % code)
        return redirect(url_for('adm.expenses'))
    return _expense_form(None)


@bp.route('/adm/expenses/<int:eid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.expense.manage')
def expense_edit(eid):
    """修改费用登记（可换附件；换附件时删除旧文件）。"""
    r = AdmExpense.query.get_or_404(eid)
    if request.method == 'POST':
        category = _f('category')
        if not category:
            flash('请选择费用类型')
            return redirect(url_for('adm.expense_edit', eid=eid))
        fn, disk, err = _save_file(request.files.get('attach'), 'expense', r.code)
        if err:
            flash(err)
            return redirect(url_for('adm.expense_edit', eid=eid))
        if disk:                       # 传了新附件 → 旧文件删掉，避免留孤儿
            _drop_file('expense', r.file_path)
            r.file_name, r.file_path = fn, disk
        r.category, r.period = category, (_f('period') or r.period)
        r.amount, r.payee = _to_money(_f('amount')), _f('payee')
        r.due_date, r.pay_date = _f('due_date'), _f('pay_date')
        r.pay_mode, r.invoice_no = _f('pay_mode'), _f('invoice_no')
        r.contract_id = _to_int(_f('contract_id')) or None
        r.asset_id = _to_int(_f('asset_id')) or None
        r.handler, r.dept = _f('handler'), _f('dept')
        r.approval_code, r.note = _f('approval_code'), _f('note')
        r.status = 'paid' if _f('status') == 'paid' else 'unpaid'
        db.session.commit()
        flash('费用登记已更新')
        return redirect(url_for('adm.expenses'))
    return _expense_form(r)


@bp.route('/adm/expenses/<int:eid>/pay', methods=['POST'])
@login_required
@permission_required('adm.expense.manage')
def expense_pay(eid):
    """快捷标记为已缴（缴费日期默认今天）。"""
    r = AdmExpense.query.get_or_404(eid)
    r.status = 'paid'
    r.pay_date = _f('pay_date') or datetime.now().strftime('%Y-%m-%d')
    db.session.commit()
    flash('「%s」已标记为已缴' % (r.category or '费用'))
    return redirect(url_for('adm.expenses'))


@bp.route('/adm/expenses/<int:eid>/delete', methods=['POST'])
@login_required
@permission_required('adm.expense.manage')
def expense_delete(eid):
    """删除费用登记（连同附件文件）。"""
    r = AdmExpense.query.get_or_404(eid)
    _drop_file('expense', r.file_path)
    db.session.delete(r)
    db.session.commit()
    flash('费用登记已删除')
    return redirect(url_for('adm.expenses'))


@bp.route('/adm/expenses/<int:eid>/file')
@login_required
@permission_required('adm.view')
def expense_file(eid):
    """下载发票 / 账单附件（鉴权下载，不暴露静态目录）。"""
    r = AdmExpense.query.get_or_404(eid)
    if not r.file_path:
        flash('该费用记录未上传附件')
        return redirect(url_for('adm.expenses'))
    return send_from_directory(_attach_dir('expense'), r.file_path,
                               as_attachment=True, download_name=r.file_name or r.file_path)


# ===================== ⑦ 车辆管理（四期，2026-10-01） =====================
# 公司车辆一本账：车辆档案（保险 / 年检到期提醒）+ 保养 / 维修 / 违章 / 用车 动态流水。
# 边界不变：车险年检等费用报销走 OA 申请中心；车辆固定资产记账(1601)与折旧走 FMS。

@bp.route('/adm/vehicles')
@login_required
@permission_required('adm.view')
def vehicles():
    """车辆台账：列表（关键词/类型/状态/到期提醒筛选）+ 保险 / 年检到期 KPI。"""
    kw = (request.args.get('kw') or '').strip()
    cat = (request.args.get('cat') or '').strip()
    st = (request.args.get('st') or '').strip()   # 车辆状态 / alert 到期提醒
    q = AdmVehicle.query
    if kw:
        like = '%' + kw + '%'
        q = q.filter(db.or_(AdmVehicle.plate_no.like(like), AdmVehicle.name.like(like),
                            AdmVehicle.brand.like(like), AdmVehicle.keeper.like(like),
                            AdmVehicle.code.like(like)))
    if cat:
        q = q.filter_by(kind=cat)
    items = []
    for r in q.order_by(AdmVehicle.id.desc()).all():
        ins = _expire_state(r.insurance_expire)
        insp = _expire_state(r.inspect_expire)
        alert = ins[0] in ('expired', 'danger', 'warn') or insp[0] in ('expired', 'danger', 'warn')
        if st == 'alert' and not alert:
            continue
        if st != 'alert' and st and (r.status or '') != st:
            continue
        items.append({'r': r, 'ins': ins, 'insp': insp, 'alert': alert})
    # KPI 按车辆全量统计（不受筛选影响，和费用台账口径一致）
    all_rows = AdmVehicle.query.all()
    alert_ins = [r for r in all_rows
                 if _expire_state(r.insurance_expire)[0] in ('expired', 'danger', 'warn')]
    alert_insp = [r for r in all_rows
                  if _expire_state(r.inspect_expire)[0] in ('expired', 'danger', 'warn')]
    return render_template(
        'adm/vehicle_list.html', items=items, cats=_cats('vehicle'),
        statuses=VEH_STATUS, kw=kw, cat=cat, st=st,
        total_n=len(all_rows),
        in_use_n=sum(1 for r in all_rows if (r.status or '') == 'in_use'),
        ins_n=len(alert_ins), insp_n=len(alert_insp),
        alert_n=len([r for r in all_rows if r in alert_ins or r in alert_insp]),
        can_edit=_can('adm.vehicle.manage'))


def _fill_vehicle(r):
    """把表单值写进车辆对象（新建 / 编辑共用；字段口径见 AdmVehicle 注释）。"""
    r.plate_no, r.name = _f('plate_no'), _f('name')
    r.kind, r.brand = _f('kind'), _f('brand')
    r.vin, r.engine_no, r.color = _f('vin'), _f('engine_no'), _f('color')
    r.seats = _to_int(_f('seats'), 5)
    r.owner = _f('owner')
    r.buy_date, r.price = _f('buy_date'), _to_money(_f('price'))
    r.mileage = _to_money(_f('mileage'))
    r.keeper, r.dept, r.place = _f('keeper'), _f('dept'), _f('place')
    r.status = _f('status') or 'in_use'
    r.insurer, r.insurance_no = _f('insurer'), _f('insurance_no')
    r.insurance_expire, r.inspect_expire = _f('insurance_expire'), _f('inspect_expire')
    r.note = _f('note')


@bp.route('/adm/vehicles/new', methods=['GET', 'POST'])
@login_required
@permission_required('adm.vehicle.manage')
def vehicle_new():
    """新建车辆档案（车牌号必填，编号系统自动生成 CAR-年月-序号）。"""
    if request.method == 'POST':
        plate = _f('plate_no')
        if not plate:
            flash('车牌号不能为空')
            return redirect(url_for('adm.vehicle_new'))
        code = _next_code(AdmVehicle, 'CAR')
        r = AdmVehicle(code=code, created_by=session.get('user_id'))
        _fill_vehicle(r)
        fn, disk, err = _save_file(request.files.get('attach'), 'vehicle', code)
        if err:
            flash(err)
            return redirect(url_for('adm.vehicle_new'))
        r.file_name, r.file_path = fn, disk
        db.session.add(r)
        db.session.commit()
        flash('车辆「%s」已建档，编号 %s' % (plate, code))
        return redirect(url_for('adm.vehicles'))
    return render_template('adm/vehicle_form.html', row=None, cats=_cats('vehicle'),
                           statuses=VEH_STATUS)


@bp.route('/adm/vehicles/<int:vid>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('adm.vehicle.manage')
def vehicle_edit(vid):
    """修改车辆档案（可换附件；换附件时删除旧文件）。"""
    r = AdmVehicle.query.get_or_404(vid)
    if request.method == 'POST':
        if not _f('plate_no'):
            flash('车牌号不能为空')
            return redirect(url_for('adm.vehicle_edit', vid=vid))
        _fill_vehicle(r)
        fn, disk, err = _save_file(request.files.get('attach'), 'vehicle', r.code)
        if err:
            flash(err)
            return redirect(url_for('adm.vehicle_edit', vid=vid))
        if disk:                       # 传了新附件 → 旧文件删掉，避免留孤儿
            _drop_file('vehicle', r.file_path)
            r.file_name, r.file_path = fn, disk
        db.session.commit()
        flash('车辆档案已更新')
        return redirect(url_for('adm.vehicles'))
    return render_template('adm/vehicle_form.html', row=r, cats=_cats('vehicle'),
                           statuses=VEH_STATUS)


@bp.route('/adm/vehicles/<int:vid>/delete', methods=['POST'])
@login_required
@permission_required('adm.vehicle.manage')
def vehicle_delete(vid):
    """删除车辆（连同其全部动态流水与附件）。"""
    r = AdmVehicle.query.get_or_404(vid)
    for rec in AdmVehRecord.query.filter_by(vehicle_id=vid).all():
        _drop_file('vehicle', rec.file_path)
        db.session.delete(rec)
    _drop_file('vehicle', r.file_path)
    db.session.delete(r)
    db.session.commit()
    flash('车辆档案已删除')
    return redirect(url_for('adm.vehicles'))


@bp.route('/adm/vehicles/<int:vid>/file')
@login_required
@permission_required('adm.view')
def vehicle_file(vid):
    """下载行驶证 / 登记证附件（鉴权下载）。"""
    r = AdmVehicle.query.get_or_404(vid)
    if not r.file_path:
        flash('该车辆未上传附件')
        return redirect(url_for('adm.vehicle_detail', vid=vid))
    return send_from_directory(_attach_dir('vehicle'), r.file_path,
                               as_attachment=True, download_name=r.file_name or r.file_path)


@bp.route('/adm/vehicles/<int:vid>', methods=['GET', 'POST'])
@login_required
@permission_required('adm.view')
def vehicle_detail(vid):
    """车辆详情：档案要点 + 动态流水（按类型筛选）+ 登记 / 编辑动态（同页内联表单）。

    POST 带 rid = 编辑该条动态；不带 rid = 新登记一条。
    里程联动：动态里填了比车辆当前里程更大的值，自动把车辆里程往上更新（只增不减）。
    """
    r = AdmVehicle.query.get_or_404(vid)
    back = url_for('adm.vehicle_detail', vid=vid)
    if request.method == 'POST':
        if not _can('adm.vehicle.manage'):
            flash('无权限登记车辆动态')
            return redirect(back)
        rid = _to_int(_f('rid'))
        rec = AdmVehRecord.query.get(rid) if rid else None
        if rec and rec.vehicle_id != vid:      # 防止跨车辆改记录
            rec = None
        kind = _f('kind') if _f('kind') in [k for k, _ in VEH_REC_KINDS] else 'use'
        fn, disk, err = _save_file(request.files.get('attach'), 'vehicle', r.code)
        if err:
            flash(err)
            return redirect(back + ('?rid=%d' % rid if rid else ''))
        if rec is None:
            rec = AdmVehRecord(vehicle_id=vid, created_by=session.get('user_id'))
            db.session.add(rec)
        elif disk:
            _drop_file('vehicle', rec.file_path)   # 换附件 → 删旧文件
        if disk:
            rec.file_name, rec.file_path = fn, disk
        rec.kind = kind
        rec.rec_date = _f('rec_date') or datetime.now().strftime('%Y-%m-%d')
        rec.mileage, rec.cost = _to_money(_f('mileage')), _to_money(_f('cost'))
        rec.driver, rec.place = _f('driver'), _f('place')
        rec.detail, rec.note = _f('detail'), _f('note')
        rec.fine_point = _to_int(_f('fine_point')) if kind == 'fine' else 0
        rec.fine_status = (_f('fine_status') or 'pending') if kind == 'fine' else None
        rec.next_date = _f('next_date') if kind == 'maintain' else ''
        if (rec.mileage or 0) > (r.mileage or 0):   # 里程只增不减
            r.mileage = rec.mileage
        db.session.commit()
        flash('车辆动态已%s' % ('更新' if rid else '登记'))
        return redirect(back)

    k = (request.args.get('k') or '').strip()
    all_recs = AdmVehRecord.query.filter_by(vehicle_id=vid).order_by(
        AdmVehRecord.rec_date.desc(), AdmVehRecord.id.desc()).all()
    items = [{'r': x,
              'next': _expire_state(x.next_date) if (x.kind == 'maintain' and x.next_date) else None}
             for x in all_recs if not k or (x.kind or '') == k]
    edit = None
    rid = _to_int(request.args.get('rid'))
    if rid:
        edit = AdmVehRecord.query.get(rid)
        if edit and edit.vehicle_id != vid:
            edit = None
    kinds_label = dict(VEH_REC_KINDS)
    return render_template(
        'adm/vehicle_detail.html', row=r, items=items, k=k, edit=edit,
        kinds=VEH_REC_KINDS, kinds_label=kinds_label, fine_status=FINE_STATUS,
        statuses=VEH_STATUS,
        sums={kk: round(sum(x.cost or 0 for x in all_recs if (x.kind or '') == kk), 2)
              for kk, _ in VEH_REC_KINDS},
        fine_pending=sum(1 for x in all_recs
                         if x.kind == 'fine' and (x.fine_status or '') != 'done'),
        today=datetime.now().strftime('%Y-%m-%d'),
        ins=_expire_state(r.insurance_expire), insp=_expire_state(r.inspect_expire),
        can_edit=_can('adm.vehicle.manage'))


@bp.route('/adm/veh-records/<int:rid>/delete', methods=['POST'])
@login_required
@permission_required('adm.vehicle.manage')
def veh_record_delete(rid):
    """删除一条车辆动态（连同附件）。"""
    rec = AdmVehRecord.query.get_or_404(rid)
    vid = rec.vehicle_id
    _drop_file('vehicle', rec.file_path)
    db.session.delete(rec)
    db.session.commit()
    flash('车辆动态已删除')
    return redirect(url_for('adm.vehicle_detail', vid=vid))


@bp.route('/adm/veh-records/<int:rid>/file')
@login_required
@permission_required('adm.view')
def veh_record_file(rid):
    """下载车辆动态的单据 / 发票附件（鉴权下载）。"""
    rec = AdmVehRecord.query.get_or_404(rid)
    if not rec.file_path:
        flash('该动态未上传附件')
        return redirect(url_for('adm.vehicle_detail', vid=rec.vehicle_id))
    return send_from_directory(_attach_dir('vehicle'), rec.file_path,
                               as_attachment=True, download_name=rec.file_name or rec.file_path)


# ===================== 本版块设置：行政分类字典 =====================

@bp.route('/adm/settings/categories')
@login_required
@permission_required('adm.view')
def categories():
    """行政分类设置：证照类型 / 合同类型 / 资产类别 / 办公用品 / 印章 / 费用类型，可增删。"""
    groups = [{'kind': k, 'label': lb,
               'items': AdmCategory.query.filter_by(kind=k)
               .order_by(AdmCategory.sort, AdmCategory.id).all()}
              for k, lb in (('license', '证照类型'), ('contract', '合同类型'), ('asset', '资产类别'),
                            ('supply', '办公用品类别'), ('seal', '印章名称'), ('expense', '费用类型'),
                            ('vehicle', '车辆类型'))]
    return render_template('adm/category_list.html', groups=groups, can_edit=_any_manage())


@bp.route('/adm/settings/categories/new', methods=['POST'])
@login_required
def category_new():
    """新增分类（需任一行政台账管理权）。"""
    if not _any_manage():
        flash('无权限维护行政分类')
        return redirect(url_for('home'))
    kind = (request.form.get('kind') or '').strip()
    name = (request.form.get('name') or '').strip()
    if kind not in ('license', 'contract', 'asset', 'supply', 'seal', 'expense', 'vehicle') or not name:
        flash('分类域或名称不合法')
        return redirect(url_for('adm.categories'))
    if AdmCategory.query.filter_by(kind=kind, name=name).first():
        flash('该分类已存在：%s' % name)
        return redirect(url_for('adm.categories'))
    db.session.add(AdmCategory(kind=kind, name=name,
                               sort=_to_int(request.form.get('sort'), 99)))
    db.session.commit()
    flash('分类「%s」已新增' % name)
    return redirect(url_for('adm.categories'))


@bp.route('/adm/settings/categories/<int:cid>/delete', methods=['POST'])
@login_required
def category_delete(cid):
    """删除分类（需任一行政台账管理权）。"""
    if not _any_manage():
        flash('无权限维护行政分类')
        return redirect(url_for('home'))
    c = AdmCategory.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    flash('分类已删除')
    return redirect(url_for('adm.categories'))
