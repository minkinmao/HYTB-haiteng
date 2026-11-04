# commission.py
# 佣金系统：销售渠道人员（外勤）的销售业绩与报酬管理。
# 权限按 agent_type + sub_team_ids 分层（渠道总/主管/代理人 分别看自己、本团队、全团队）。
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from models import db, Employee, Commission, Bonus, BonusAllocation, User
from app import login_required, permission_required

bp = Blueprint('commission', __name__)


# ===================== 权限辅助函数 =====================

def get_my_agent_team():
    """当前登录账号对应的员工（外勤）。非外勤返回 None。"""
    user_id = session.get('user_id')
    if not user_id:
        return None
    u = User.query.get(user_id)
    if not u:
        return None
    # 注意：外勤人员不挂 S/A/B/C/D/E 职级（level 为空），
    # 所以这里不能要求"必须有职级"，否则外勤账号永远查不到自己的佣金。
    emp = Employee.query.filter_by(employee_no=u.username).first()
    if not emp or emp.staff_type != 'external':
        return None
    return emp


def get_can_view_employee_ids(user_level_code, my_agent):
    """返回当前用户可以查看的 employee.id 集合。
    - 营销负责人 (S级, 有 commission.admin) : 全量
    - 渠道总: 本团队全员 + 下属主管的团队
    - 主管: 本团队全员
    - 代理人: 仅本人
    - 非营销负责人: 查不到佣金（前端应拦截）
    """
    if not my_agent:
        return set()
    # 只有"挂了职级且该职级带佣金管理权限"的账号才算营销负责人；
    # 外勤没有职级，直接按 agent_type（渠道总/主管/代理人）分层取数。
    admin_perm = bool(my_agent.level and
                      any(p.code == 'commission.admin' for p in my_agent.level.permissions))
    if admin_perm:
        return set(e.id for e in Employee.query.filter_by(staff_type='external').all())
    if my_agent.agent_type == '渠道总':
        team_ids = {my_agent.id}
        for aid in _safe_ints(my_agent.sub_team_ids):
            if aid > 0:
                team_ids.add(aid)
                sub = Employee.query.get(aid)
                if sub:
                    for sid in _safe_ints(sub.sub_team_ids):
                        team_ids.add(sid)
        return team_ids
    if my_agent.agent_type == '主管':
        team_ids = {my_agent.id}
        for sid in _safe_ints(my_agent.sub_team_ids):
            team_ids.add(sid)
        return team_ids
    # 代理人
    return {my_agent.id}


def _safe_ints(s):
    """把 '1,3,5' 字符串切成整数列表，非数字忽略。"""
    if not s:
        return []
    out = []
    for x in s.split(','):
        try:
            out.append(int(x.strip()))
        except ValueError:
            pass
    return out


# ===================== 佣金明细 =====================

@bp.route('/commission/commissions', methods=['GET'])
@login_required
def commissions():
    """佣金明细：按权限过滤可见范围（渠道总/主管/代理人/管理员 分别看不同数据）。"""
    my_agent = get_my_agent_team()
    user = User.query.get(session.get('user_id'))
    admin_perm = any(p.code == 'commission.admin' for p in (user.level.permissions if user and user.level else []))
    if my_agent and admin_perm:
        visible_ids = set(e.id for e in Employee.query.filter_by(staff_type='external').all())
    elif my_agent:
        visible_ids = get_can_view_employee_ids(user.level.code if user and user.level else None, my_agent)
    else:
        visible_ids = set()

    items = Commission.query.filter(Commission.employee_id.in_(visible_ids)).order_by(Commission.month.desc(), Commission.id).all()

    # 按员工分组，方便展示"我的 / 我的团队"
    grouped = {}
    for it in items:
        key = it.employee_id
        grouped.setdefault(key, []).append(it)

    # 当前用户自己的佣金（用于"我的佣金"卡片）
    # 注意：my_agent 为 None 时不能写成 in my_agent.id（int 不可迭代），要单独判空
    my_commissions = [it for it in items if it.employee_id == my_agent.id] if my_agent else []
    # 合计先在 Python 里算好再交给模板（Jinja 模板不支持生成器表达式求和）
    my_confirmed_total = sum((it.commission_amount or 0) for it in my_commissions if it.status == 'confirmed')
    my_pending_total = sum((it.commission_amount or 0) for it in my_commissions if it.status != 'confirmed')

    employees = {e.id: e for e in Employee.query.filter(Employee.id.in_(visible_ids)).all()}

    return render_template('commission/commission_list.html',
                           items=items, grouped=grouped, my_commissions=my_commissions,
                           my_confirmed_total=my_confirmed_total, my_pending_total=my_pending_total,
                           employees=employees, my_agent=my_agent, can_manage=admin_perm)


@bp.route('/commission/commissions/new', methods=['POST'])
@login_required
@permission_required('commission.admin')
def commission_new():
    """新增佣金记录（仅营销负责人可操作）。"""
    emp_id = request.form.get('employee_id')
    month = request.form.get('month', '').strip()
    product_name = request.form.get('product_name', '').strip()
    if not emp_id or not month:
        flash('销售人员和月份不能为空')
        return redirect(url_for('commission.commissions'))
    c = Commission(
        employee_id=int(emp_id), month=month,
        product_name=product_name,
        channel_name=request.form.get('channel_name', '').strip() or None,
        premium=float(request.form.get('premium', 0) or 0),
        commission_rate=float(request.form.get('commission_rate', 0) or 0),
        commission_amount=float(request.form.get('commission_amount', 0) or 0),
        status=request.form.get('status', 'pending'),
        note=request.form.get('note', '').strip(),
    )
    db.session.add(c)
    db.session.commit()
    flash('佣金已添加')
    return redirect(url_for('commission.commissions'))


@bp.route('/commission/commissions/<int:cid>/confirm', methods=['POST'])
@login_required
@permission_required('commission.admin')
def commission_confirm(cid):
    """确认佣金（状态从 pending -> confirmed）"""
    c = Commission.query.get_or_404(cid)
    c.status = 'confirmed'
    db.session.commit()
    flash('佣金已确认')
    return redirect(url_for('commission.commissions'))


# ===================== 奖金池 =====================

@bp.route('/commission/bonuses', methods=['GET'])
@login_required
@permission_required('commission.admin')
def bonuses():
    """奖金池：仅营销负责人可见（管理员权限）。"""
    items = Bonus.query.order_by(Bonus.quarter.desc(), Bonus.id).all()
    return render_template('commission/bonus_list.html', items=items)


@bp.route('/commission/bonuses/new', methods=['POST'])
@login_required
@permission_required('commission.admin')
def bonus_new():
    """新增奖金池。"""
    q = request.form.get('quarter', '').strip()
    name = request.form.get('name', '').strip()
    if not q or not name:
        flash('季度和名称不能为空')
        return redirect(url_for('commission.bonuses'))
    b = Bonus(
        quarter=q, name=name,
        total_amount=float(request.form.get('total_amount', 0) or 0),
        note=request.form.get('note', '').strip(),
    )
    db.session.add(b)
    db.session.commit()
    flash('奖金池已创建')
    return redirect(url_for('commission.bonuses'))


@bp.route('/commission/bonuses/<int:bid>/edit', methods=['GET'])
@login_required
@permission_required('commission.admin')
def bonus_edit_page(bid):
    """编辑奖金池：显示现有分配明细，支持新增分配。"""
    b = Bonus.query.get_or_404(bid)
    allocs = BonusAllocation.query.filter_by(bonus_id=bid).order_by(BonusAllocation.id).all()
    agents = Employee.query.filter_by(staff_type='external').order_by(Employee.agent_type, Employee.name).all()
    return render_template('commission/bonus_edit.html', b=b, allocs=allocs, agents=agents)


@bp.route('/commission/bonuses/<int:bid>/edit', methods=['POST'])
@login_required
@permission_required('commission.admin')
def bonus_edit(bid):
    """保存奖金分配（覆盖写入：先删后插，防止脏数据）。"""
    b = Bonus.query.get_or_404(bid)
    b.name = request.form.get('name', '').strip() or b.name
    b.total_amount = float(request.form.get('total_amount', 0) or 0) or b.total_amount
    b.note = request.form.get('note', '').strip()
    # 分配明细：表单字段为 alloc_emp_<id>，值为金额
    allocations = []
    for key, amount in request.form.items():
        if key.startswith('alloc_emp_'):
            eid = int(key.split('_')[-1])
            if eid:
                allocations.append((eid, float(amount) if amount else 0))
    # 删除旧分配
    BonusAllocation.query.filter_by(bonus_id=bid).delete()
    # 写入新分配
    for eid, amt in allocations:
        if amt > 0:
            db.session.add(BonusAllocation(bonus_id=bid, employee_id=eid, amount=amt))
    db.session.commit()
    flash('奖金分配已更新')
    return redirect(url_for('commission.bonuses'))


@bp.route('/commission/bonuses/<int:bid>/delete', methods=['POST'])
@login_required
@permission_required('commission.admin')
def bonus_delete(bid):
    """删除奖金池（连同分配明细一起删除）。"""
    b = Bonus.query.get_or_404(bid)
    BonusAllocation.query.filter_by(bonus_id=bid).delete()
    db.session.delete(b)
    db.session.commit()
    flash('奖金池已删除')
    return redirect(url_for('commission.bonuses'))
