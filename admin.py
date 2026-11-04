# admin.py
# IT 管理员后台：按职级配置"权限流"（每个职级拥有哪些权限）
# 仅 S 级（总经理/老板）可进入，保证权限设置不被越权修改
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash
from models import db, Level, Permission, User, Employee, UserModulePerm
from app import login_required, MODULE_KEYS, build_menu_tree, log_audit

bp = Blueprint('admin', __name__)


def admin_required(f):
    """权限闸门（#40）：只有拥有 it.system.manage 权限才能进 IT 管理员后台。

    S/A/B 主管层（含老板）与独立 IT 系统管理员职级都能进；
    IT 系统管理员职级不挂任何 fin.* 财务数据权限，进不了财务页面（红线）。
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = User.query.get(session.get('user_id'))
        has = bool(user and (user.is_admin or
                   (user.level and any(p.code == 'it.system.manage' for p in user.level.permissions))))
        if not has:
            flash('无权限访问 IT 管理员后台（缺少权限：it.system.manage）')
            return redirect(url_for('home'))
        return f(*args, **kwargs)
    return wrapper


@bp.route('/admin/levels', methods=['GET'])
@login_required
@admin_required
def levels():
    """列出所有职级，及其当前拥有的权限（勾选即属于该职级权限流）"""
    levels = Level.query.order_by(Level.code).all()
    perms = Permission.query.order_by(Permission.id).all()
    return render_template('admin/levels.html', levels=levels, perms=perms)


# ---------- 用户管理：绑定企业微信 userid（2026-09-28）----------
@bp.route('/admin/users')
@login_required
@admin_required
def users():
    """IT 用户管理：列出所有用户，可绑定企微、开通二级菜单模块权限"""
    us = User.query.order_by(User.id).all()
    rows = []
    for u in us:
        # 按"工号=登录账号"关联员工花名册取姓名（B 模式：档案与账号分离）
        emp = Employee.query.filter_by(employee_no=u.username).first()
        perm_n = UserModulePerm.query.filter_by(user_id=u.id).count()
        rows.append({'u': u, 'emp': emp, 'perm_n': perm_n})
    return render_template('admin/users.html', rows=rows)


@bp.route('/admin/users/<int:uid>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def user_edit(uid):
    """编辑某用户的企业微信 userid（绑定 / 解绑）"""
    u = User.query.get_or_404(uid)
    if request.method == 'POST':
        u.wecom_userid = (request.form.get('wecom_userid') or '').strip()
        u.pinyin = (request.form.get('pinyin') or '').strip().lower()
        db.session.commit()
        flash('已保存「%s」的企业微信绑定' % u.username)
        return redirect(url_for('admin.users'))
    return render_template('admin/user_edit.html', u=u)


@bp.route('/admin/levels/<code>/permissions', methods=['POST'])
@login_required
@admin_required
def set_level_permissions(code):
    """保存某职级的权限流：表单提交被选中的权限 id 列表（多选框）"""
    lv = Level.query.filter_by(code=code).first_or_404()
    perm_ids = request.form.getlist('perm_ids')  # 多选框的值列表
    # 按提交的 id 集合重建该职级的权限；为空则清空权限流
    lv.permissions = Permission.query.filter(Permission.id.in_(perm_ids)).all() if perm_ids else []
    db.session.commit()
    flash(f'已更新「{lv.name}」的权限流')
    return redirect(url_for('admin.levels'))


# ---------- 用户管理：新建用户 + 开通二级菜单模块权限 ----------
@bp.route('/admin/users/new', methods=['GET', 'POST'])
@login_required
@admin_required
def user_new():
    """新建用户：工号(登录账号) + 密码 + 职级 + 可访问的二级菜单模块权限"""
    levels = Level.query.order_by(Level.code).all()
    full_tree = build_menu_tree(set(MODULE_KEYS))  # 全量模块（按系统分组），供勾选
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        pinyin = (request.form.get('pinyin') or '').strip().lower()
        pwd = (request.form.get('password') or '').strip()
        pwd2 = (request.form.get('password2') or '').strip()
        # 初始密码默认 123456（未填写时使用默认值，所有新建用户初始密码统一为 123456）
        if not pwd:
            pwd = '123456'
            pwd2 = '123456'
        level_id = request.form.get('level_id')
        keys = request.form.getlist('keys')  # 勾选的模块端点
        # 校验失败也回显已勾选的模块，避免用户重选
        owned_keys = set(keys)
        if not username:
            flash('登录账号（工号）不能为空')
            return render_template('admin/user_new.html', levels=levels, full_tree=full_tree, owned=owned_keys)
        if len(pwd) < 6:
            flash('密码至少 6 位')
            return render_template('admin/user_new.html', levels=levels, full_tree=full_tree, owned=owned_keys)
        if pwd != pwd2:
            flash('两次输入的密码不一致')
            return render_template('admin/user_new.html', levels=levels, full_tree=full_tree, owned=owned_keys)
        if User.query.filter_by(username=username).first():
            flash('该工号账号已存在')
            return render_template('admin/user_new.html', levels=levels, full_tree=full_tree, owned=owned_keys)
        # 初始密码为 123456 的账号，首次登录强制改密（沿用既有机制）
        must_change = (pwd == '123456')
        u = User(username=username, password_hash=generate_password_hash(pwd),
                 level_id=int(level_id) if level_id else None, is_admin=False,
                 must_change_password=must_change, pinyin=pinyin)
        db.session.add(u)
        db.session.flush()  # 拿到 u.id 再写模块权限
        added = 0
        for k in keys:
            if k in MODULE_KEYS:
                db.session.add(UserModulePerm(user_id=u.id, menu_key=k))
                added += 1
        db.session.commit()
        flash('已创建用户「%s」并开通 %d 个模块权限' % (username, added))
        return redirect(url_for('admin.users'))
    return render_template('admin/user_new.html', levels=levels, full_tree=full_tree, owned=set())


@bp.route('/admin/users/<int:uid>/perms', methods=['GET', 'POST'])
@login_required
@admin_required
def user_perms(uid):
    """给某用户开通/调整二级菜单模块权限（粒度到二级菜单）"""
    u = User.query.get_or_404(uid)
    full_tree = build_menu_tree(set(MODULE_KEYS))  # 全量模块（按系统分组），供勾选
    owned = {p.menu_key for p in u.module_perms}
    if request.method == 'POST':
        keys = request.form.getlist('keys')
        # 重建权限：先删后建（空列表即清空该用户全部模块权限）
        UserModulePerm.query.filter_by(user_id=u.id).delete()
        added = 0
        for k in keys:
            if k in MODULE_KEYS:
                db.session.add(UserModulePerm(user_id=u.id, menu_key=k))
                added += 1
        db.session.commit()
        flash('已更新「%s」的模块权限（%d 个）' % (u.username, added))
        return redirect(url_for('admin.users'))
    return render_template('admin/user_perms.html', u=u, full_tree=full_tree, owned=owned)


@bp.route('/admin/users/<int:uid>/password', methods=['GET', 'POST'])
@login_required
@admin_required
def user_password(uid):
    """管理员后台重置某用户密码（无需原密码，用于员工忘记密码等场景）"""
    u = User.query.get_or_404(uid)
    if request.method == 'POST':
        new = request.form.get('new_password', '')
        new2 = request.form.get('confirm_password', '')
        if len(new) < 6:
            flash('新密码至少 6 位')
            return render_template('admin/user_password.html', u=u)
        if new != new2:
            flash('两次输入的新密码不一致')
            return render_template('admin/user_password.html', u=u)
        u.password_hash = generate_password_hash(new)
        db.session.commit()
        log_audit('reset_password', module='auth', target_id=u.id,
                  target_no=u.username, detail='管理员重置用户密码：%s' % u.username)
        flash('已重置「%s」的密码' % u.username)
        return redirect(url_for('admin.users'))
    return render_template('admin/user_password.html', u=u)


@bp.route('/admin/users/<int:uid>/delete', methods=['POST'])
@login_required
@admin_required
def user_delete(uid):
    """删除用户（含其模块权限）；带防护：不能删自己、不能删唯一的系统管理员"""
    u = User.query.get_or_404(uid)
    # 防护1：不能删除当前登录账号，否则会把自己锁在后台外
    if u.id == session.get('user_id'):
        flash('不能删除当前登录的账号')
        return redirect(url_for('admin.users'))
    # 防护2：不能删除唯一的系统管理员，避免后台被锁死
    if u.is_admin:
        admin_count = User.query.filter_by(is_admin=True).count()
        if admin_count <= 1:
            flash('不能删除唯一的系统管理员账号')
            return redirect(url_for('admin.users'))
    # 先清理该用户的模块权限，再删账号
    UserModulePerm.query.filter_by(user_id=u.id).delete()
    username = u.username
    db.session.delete(u)
    db.session.commit()
    log_audit('delete_user', module='auth', target_id=uid, target_no=username,
              detail='管理员删除用户：%s' % username)
    flash('已删除用户「%s」' % username)
    return redirect(url_for('admin.users'))
