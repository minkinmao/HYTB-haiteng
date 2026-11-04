# hr.py
# 人事系统 · 第一个增量：员工花名册
# 复用 models.py 里的 Level（职级）与 Organization（部门），不重复建表。
import csv
import io
import json
import math
import os
import re
import uuid
from datetime import datetime, date, timedelta
from urllib.parse import quote
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, make_response, send_from_directory, current_app
from werkzeug.security import generate_password_hash
from models import db, Employee, Level, Organization, User, Salary, Contract, SalaryField, LeaveType, LeaveTier, Approval, Holiday, AttendanceRule, Onboarding, EmployeeChange, HrSocialConfig, HrAllowanceItem
from models import (HrEducation, HrFamily, HrTraining, HrTechTitle,
                    HrQualification, HrReward, HrPunishment)
from app import login_required, permission_required, log_audit

# 蓝图名 'hr'，模板放在 templates/hr/ 下（与 oa 模块一致的做法）
bp = Blueprint('hr', __name__)

# 公司邮箱统一后缀（2026-09-30 定）：只填本地名自动补全，已含 @ 的保留原样
COMPANY_EMAIL_SUFFIX = '@hywinpro.cn'


def _norm_company_email(raw):
    """规范化公司邮箱：只填本地名（无 @）自动补 @hywinpro.cn；已含 @ 保留；空值返回 None。"""
    s = (raw or '').strip()
    if s and '@' not in s:
        s = s + COMPANY_EMAIL_SUFFIX
    return s or None


def _roster_sort_key(e):
    """花名册排序键：先按部门（按组织架构页的配置顺序 org_id，未设部门的排最后），再按工号。
    用 org_id 而非部门名称排序：改名不会导致花名册重排，顺序与「组织架构」页一致。"""
    return (0 if e.org else 1, (e.org_id or 0), (e.employee_no or ''))


def _roster_data():
    """花名册数据（页面展示与 CSV 导出共用同一份口径，避免两处排序/字段不一致）。
    返回 (items, groups, emp_data)：
      items    —— 已按「部门 + 工号」排好序的全员列表；
      groups   —— [(部门名, [员工…]), …] 供页面分组展示；
      emp_data —— {员工id: 弹窗档案字段} 供前端「点姓名弹档案」使用。"""
    items = Employee.query.all()
    items.sort(key=_roster_sort_key)

    groups = []
    for it in items:
        dname = it.org.name if it.org else '未设部门'
        if not groups or groups[-1][0] != dname:
            groups.append((dname, []))
        groups[-1][1].append(it)

    emp_data = {}
    for e in items:
        if e.staff_type == 'external':  # 外勤：代理合同，用代理类型，不挂职级
            rank, stype = (e.agent_type or ''), '外勤（代理合同）'
        else:                           # 内勤：劳动合同，挂 S/A/B/C/D/E 职级
            rank = (e.level.code + ' - ' + e.level.name) if e.level else ''
            stype = '内勤（劳动合同）'
        emp_data[e.id] = {
            'name': e.name or '',
            'no': e.employee_no or '',
            'dept': e.org.name if e.org else '未设部门',
            'title': e.title or '',
            'rank': rank,
            'type': stype,
            'hire': e.hire_date.isoformat() if e.hire_date else '',
            'phone': e.phone or '',
            'email': e.email or '',
            'status': '在职' if e.status == 'active' else '离职',
            'photo': url_for('static', filename=e.photo) if e.photo else '',
            'card': url_for('hr.profile_card', eid=e.id),   # 人事卡片
            'view': url_for('hr.employee_view', eid=e.id),  # 完整只读档案
        }
    return items, groups, emp_data


@bp.route('/hr/employees', methods=['GET'])
@login_required
def employees():
    """员工花名册（#145 第6项）：按部门排序分组展示 → 点姓名弹档案 → 一键导出 CSV。
    本页为全员档案数据备份，仅供查询、不做任何修改（档案由入职确认流程入册）。"""
    items, groups, emp_data = _roster_data()
    user = User.query.get(session.get('user_id'))
    # 2026-11-04：管理员(is_admin)默认具备；其余按职级权限流判定
    can_manage = bool(user and (user.is_admin or (user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))))
    can_delete = bool(user and (user.is_admin or (user.level and
                      any(p.code == 'hr.employee.delete' for p in user.level.permissions))))
    return render_template('hr/employee_list.html', items=items, groups=groups,
                           emp_data=emp_data, can_manage=can_manage, can_delete=can_delete)


@bp.route('/hr/employees/export', methods=['GET'])
@login_required
def roster_export():
    """员工花名册「一键导出 CSV」：口径与页面完全一致（按部门 + 工号排序）。
    用 utf-8-sig（带 BOM）写出，Excel 直接双击打开不乱码。"""
    items, _, _ = _roster_data()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['工号', '姓名', '用工类型', '部门', '职务', '职级/代理类型',
                     '在职状态', '入职日期', '离职日期', '联系电话', '公司邮箱',
                     '性别', '出生日期', '身份证号'])
    for e in items:
        if e.staff_type == 'external':
            rank, stype = (e.agent_type or ''), '外勤'
        else:
            rank = e.level.code if e.level else ''
            stype = '内勤'
        writer.writerow([
            e.employee_no or '', e.name or '', stype,
            (e.org.name if e.org else '未设部门'), e.title or '', rank,
            '在职' if e.status == 'active' else '离职',
            e.hire_date.isoformat() if e.hire_date else '',
            e.leave_date.isoformat() if e.leave_date else '',
            e.phone or '', e.email or '',
            e.gender or '', e.birth_date or '', e.idcard or '',
        ])
    # \ufeff = UTF-8 BOM：让 Excel 识别编码，中文不乱码
    payload = ('\ufeff' + buf.getvalue()).encode('utf-8')
    resp = make_response(payload)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    fname = '员工花名册_%s.csv' % datetime.now().strftime('%Y%m%d')
    resp.headers['Content-Disposition'] = (
        "attachment; filename=\"roster.csv\"; filename*=UTF-8''%s" % quote(fname))
    log_audit('导出员工花名册', module='EHR', target_type='Employee',
              detail='导出全员档案 CSV，共 %d 人' % len(items))
    return resp


@bp.route('/hr/employees/new', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')  # 仅 S/A/B/C 级（默认权限流）可新增
def employee_new():
    """新增员工：取表单字段写入 Employee 表"""
    name = request.form.get('name', '').strip()
    employee_no = request.form.get('employee_no', '').strip()
    if not name or not employee_no:
        flash('姓名和工号不能为空')
        return redirect(url_for('hr.employees'))

    emp = Employee(name=name, employee_no=employee_no)
    # 用工类型：内勤(internal) 用 S/A/B/C/D/E 职级；外勤(external) 用代理类型，二者互斥
    emp.staff_type = request.form.get('staff_type', 'internal')
    if emp.staff_type == 'external':
        emp.agent_type = request.form.get('agent_type', '').strip() or None
        emp.level_id = None  # 外勤不挂职级
    else:
        emp.agent_type = None
        level_id = request.form.get('level_id')
        if level_id:
            emp.level_id = int(level_id)
    org_id = request.form.get('org_id')
    if org_id:
        emp.org_id = int(org_id)
    emp.title = request.form.get('title', '').strip()
    emp.phone = request.form.get('phone', '').strip()
    db.session.add(emp)
    db.session.commit()
    flash('员工已添加')
    return redirect(url_for('hr.employees'))


# 入职照片允许的格式（系统总览「员工工卡」展示用）
PHOTO_ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}


def _save_employee_photo(file_storage, emp_id):
    """保存入职照片到 static/uploads/employees/ 下，返回「相对 static 的路径」；没传文件返回 None。
    命名规则：emp<员工ID>_<8位随机码>.<扩展名>，避免重名与中文文件名乱码。
    与合同文件不同：照片存相对路径，模板里直接 url_for('static', filename=路径) 即可引用。"""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in PHOTO_ALLOWED_EXT:
        flash('入职照片格式不支持：%s（仅支持 jpg / jpeg / png / webp / gif）' % ext)
        return '__INVALID__'
    upload_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'employees')
    os.makedirs(upload_dir, exist_ok=True)
    filename = 'emp%d_%s%s' % (emp_id, uuid.uuid4().hex[:8], ext)
    file_storage.save(os.path.join(upload_dir, filename))
    return 'uploads/employees/' + filename


def _delete_photo_file(rel_path):
    """删除 static 下已存在的照片文件（换照片 / 清照片时调用）。失败不抛错，不阻断业务。"""
    if not rel_path:
        return
    try:
        p = os.path.join(current_app.static_folder, rel_path.replace('/', os.sep))
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


@bp.route('/hr/employees/<int:eid>/edit', methods=['GET'])
@login_required
@permission_required('hr.employee.manage')  # 仅 S/A/B/C 级（默认权限流）可编辑
def employee_edit(eid):
    """编辑员工：GET 显示已填好的表单"""
    emp = Employee.query.get_or_404(eid)
    levels = Level.query.order_by(Level.code).all()
    orgs = Organization.query.all()
    return render_template('hr/employee_edit.html', emp=emp, levels=levels, orgs=orgs)


@bp.route('/hr/employees/<int:eid>/edit', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_update(eid):
    """编辑员工：POST 保存修改（姓名/工号不可空，其余可选）"""
    emp = Employee.query.get_or_404(eid)
    name = request.form.get('name', '').strip()
    employee_no = request.form.get('employee_no', '').strip()
    if not name or not employee_no:
        flash('姓名和工号不能为空')
        return redirect(url_for('hr.employee_edit', eid=eid))

    emp.name = name
    emp.employee_no = employee_no
    level_id = request.form.get('level_id')
    org_id = request.form.get('org_id')
    emp.level_id = int(level_id) if level_id else None  # 空字符串转 None，避免存空串
    emp.org_id = int(org_id) if org_id else None
    emp.title = request.form.get('title', '').strip()
    emp.phone = request.form.get('phone', '').strip()
    # 身份证号码（人事卡片展示用；空值存 None）
    emp.idcard = request.form.get('idcard', '').strip() or None
    # 公司邮箱：统一后缀 @hywinpro.cn（只填本地名自动补全，已含 @ 保留）
    emp.email = _norm_company_email(request.form.get('email'))
    # 入职日期：Date 列不接受字符串，必须先解析（空值存 None）
    _hire = request.form.get('hire_date', '').strip()
    emp.hire_date = datetime.strptime(_hire, '%Y-%m-%d').date() if _hire else None
    # 入职照片：勾选「清除」则删除；有上传则替换旧的（格式不对会 flash 并回到编辑页）
    _old_photo = emp.photo
    if request.form.get('del_photo'):
        emp.photo = None
    _saved_photo = _save_employee_photo(request.files.get('photo'), emp.id)
    if _saved_photo == '__INVALID__':
        return redirect(url_for('hr.employee_edit', eid=eid))
    if _saved_photo:
        emp.photo = _saved_photo
    if _old_photo and _old_photo != emp.photo:
        _delete_photo_file(_old_photo)   # 换/删后清掉旧文件，避免 static 目录堆积
    db.session.commit()
    flash('员工信息已更新')
    return redirect(url_for('hr.employees'))


@bp.route('/hr/employees/<int:eid>/delete', methods=['POST'])
@login_required
@permission_required('hr.employee.delete')
def employee_delete(eid):
    """手工删除员工（不可恢复）：先清理所有含 employee_id 的关联表，再删除员工本身。
    2026-11-04 威哥要求：人事员工支持手工删除，权限码 hr.employee.delete（admin 默认具备）。"""
    from sqlalchemy import text
    emp = Employee.query.get_or_404(eid)
    name, no = emp.name, emp.employee_no
    # 所有含 employee_id 列的关联表（不清理会留下孤儿数据；SQLite 默认不强制外键）
    rel_tables = ['commission', 'bonus_allocation', 'salary', 'contract',
                  'attendance_record', 'employee_change', 'hr_education',
                  'hr_family', 'hr_training', 'hr_tech_title',
                  'hr_qualification', 'hr_reward', 'hr_punishment']
    for t in rel_tables:
        db.session.execute(text('DELETE FROM %s WHERE employee_id = :eid' % t), {'eid': eid})
    db.session.delete(emp)
    db.session.commit()
    log_audit('删除员工', module='EHR', target_type='Employee', target_id=eid,
              target_no=no or '', detail='手工删除员工：%s（%s）' % (name, no or ''))
    flash('已删除员工「%s」及其关联记录（不可恢复）' % name)
    return redirect(url_for('hr.employees'))


@bp.route('/hr/employees/<int:eid>/leave', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_leave(eid):
    """离职/复职切换（软状态，不删除数据）：在职→离职，离职→复职"""
    emp = Employee.query.get_or_404(eid)
    emp.status = 'left' if emp.status == 'active' else 'active'
    db.session.commit()
    flash('离职' if emp.status == 'left' else '已复职')
    return redirect(url_for('hr.employees'))


# ===================== 人事 · 工资清单（仅内部员工，佣金走小程序不在此） =====================

# 工资各数值字段：空串统一转 0，避免存空/报错
_SALARY_FIELDS = ['base_salary', 'position_salary', 'performance_salary',
                  'subsidy', 'pension', 'medical', 'housing_fund']


def _fill_salary(s, form):
    """从表单填充工资数值字段（空串按 0 处理），列表式写法避免重复代码
    同时把 HR 配置的自定义工资项以 {code: 数值} 存入 JSON 列 custom。"""
    for f in _SALARY_FIELDS:
        val = form.get(f, '').strip()
        setattr(s, f, float(val) if val else 0.0)
    # 自定义工资项：表单字段名为 custom_<code>
    custom = {}
    for f in SalaryField.query.all():
        v = form.get('custom_' + f.code, '').strip()
        if v:
            custom[f.code] = float(v)
    s.custom = custom


def salary_net(s, fields):
    """计算实发 = 应发(基本+岗位+绩效+补贴+自定义非扣减) - 扣减(养老+医疗+公积金+自定义扣减)"""
    gross = (s.base_salary or 0) + (s.position_salary or 0) + \
            (s.performance_salary or 0) + (s.subsidy or 0)
    deduct = (s.pension or 0) + (s.medical or 0) + (s.housing_fund or 0)
    for f in fields:
        v = (s.custom or {}).get(f.code) or 0
        if f.is_deduction:
            deduct += v
        else:
            gross += v
    return round(gross - deduct, 2)


@bp.route('/hr/salaries', methods=['GET'])
@login_required
def salaries():
    """工资清单：列出内部员工工资记录（外勤拿佣金，不在此列）"""
    items = Salary.query.order_by(Salary.month.desc(), Salary.id).all()
    # 工资仅面向内勤（签劳动合同）员工；外勤(代理合同)不在此录入
    employees = Employee.query.filter_by(staff_type='internal').order_by(Employee.id).all()
    salary_fields = SalaryField.query.order_by(SalaryField.id).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.salary.manage' for p in user.level.permissions))
    return render_template('hr/salary_list.html',
                           items=items, employees=employees, salary_fields=salary_fields,
                           salary_net=salary_net, can_manage=can_manage)


@bp.route('/hr/salaries/new', methods=['POST'])
@login_required
@permission_required('hr.salary.manage')  # 仅权限流授予者可录入/修改
def salary_new():
    """新增工资记录"""
    employee_id = request.form.get('employee_id')
    month = request.form.get('month', '').strip()
    if not employee_id or not month:
        flash('员工和发放月份不能为空')
        return redirect(url_for('hr.salaries'))
    s = Salary(employee_id=int(employee_id), month=month)
    _fill_salary(s, request.form)
    s.note = request.form.get('note', '').strip()
    db.session.add(s)
    db.session.commit()
    flash('工资记录已添加')
    return redirect(url_for('hr.salaries'))


@bp.route('/hr/salaries/<int:sid>/edit', methods=['GET'])
@login_required
@permission_required('hr.salary.manage')
def salary_edit(sid):
    """编辑工资：GET 显示已填好的表单"""
    s = Salary.query.get_or_404(sid)
    employees = Employee.query.order_by(Employee.id).all()
    return render_template('hr/salary_edit.html', s=s, employees=employees)


@bp.route('/hr/salaries/<int:sid>/edit', methods=['POST'])
@login_required
@permission_required('hr.salary.manage')
def salary_update(sid):
    """编辑工资：POST 保存修改（含自定义字段）"""
    s = Salary.query.get_or_404(sid)
    employee_id = request.form.get('employee_id')
    month = request.form.get('month', '').strip()
    if not employee_id or not month:
        flash('员工和发放月份不能为空')
        return redirect(url_for('hr.salary_edit', sid=sid))
    s.employee_id = int(employee_id)
    s.month = month
    _fill_salary(s, request.form)
    s.note = request.form.get('note', '').strip()
    db.session.commit()
    flash('工资记录已更新')
    return redirect(url_for('hr.salaries'))


@bp.route('/hr/salaries/<int:sid>/delete', methods=['POST'])
@login_required
@permission_required('hr.salary.manage')
def salary_delete(sid):
    """删除工资记录（软删除未做，直接删；工资属敏感数据，仅授权者可操作）"""
    s = Salary.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    flash('工资记录已删除')
    return redirect(url_for('hr.salaries'))


# ===================== 人事 · 员工合同档案（内勤劳动合同 / 外勤代理合同） =====================

# 允许上传的合同文件类型（电子版劳动合同备份）
CONTRACT_ALLOWED_EXT = ('.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.xls', '.xlsx')


def _parse_date(s):
    """把表单里的日期字符串（YYYY-MM-DD）转成 date 对象；空或非法返回 None。
    SQLite 的 Date 列只接受 date 对象，不能直接存字符串。"""
    s = (s or '').strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _save_contract_file(file_storage, contract_id=None):
    """保存电子版合同文件到 static/uploads/contracts/ 下，返回保存后的文件名；无文件返回 None。
    文件名规则：<合同ID或时间戳>_<8位随机码>.<原扩展名>，避免重名与路径穿越。"""
    if not file_storage or file_storage.filename == '':
        return None
    original = file_storage.filename
    ext = os.path.splitext(original)[1].lower()
    if ext not in CONTRACT_ALLOWED_EXT:
        flash('不支持的文件类型：%s（仅支持 pdf/doc/docx/图片/表格）' % ext)
        return '__INVALID__'
    upload_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'contracts')
    os.makedirs(upload_dir, exist_ok=True)
    # 用合同ID（若有）+ 随机码命名，避免中文文件名在浏览器下载时乱码
    prefix = str(contract_id) if contract_id else datetime.now().strftime('%Y%m%d%H%M%S')
    filename = '%s_%s%s' % (prefix, uuid.uuid4().hex[:8], ext)
    file_storage.save(os.path.join(upload_dir, filename))
    return filename


@bp.route('/hr/contracts', methods=['GET'])
@login_required
def contracts():
    """合同档案：列出全员合同（内勤=劳动合同，外勤=代理合同）"""
    items = Contract.query.order_by(Contract.id).all()
    employees = Employee.query.order_by(Employee.id).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))
    return render_template('hr/contract_list.html', items=items, employees=employees, can_manage=can_manage)


@bp.route('/hr/contracts/new', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')  # 复用员工管理权限
def contract_new():
    """新增合同：合同类型由所选员工的用工类型自动推导（内勤=劳动/外勤=代理）。
    支持上传电子版合同文件（劳动合同备份）。"""
    employee_id = request.form.get('employee_id')
    if not employee_id:
        flash('请选择员工')
        return redirect(url_for('hr.contracts'))
    emp = Employee.query.get_or_404(int(employee_id))
    c = Contract(employee_id=emp.id)
    c.contract_type = 'labor' if emp.staff_type == 'internal' else 'agency'
    c.contract_no = request.form.get('contract_no', '').strip()
    c.start_date = _parse_date(request.form.get('start_date'))
    c.end_date = _parse_date(request.form.get('end_date'))
    c.status = request.form.get('status', 'active')
    c.file_path = request.form.get('file_path', '').strip()
    c.note = request.form.get('note', '').strip()
    db.session.add(c)
    db.session.flush()  # 先拿到 c.id，用于给上传文件命名
    # 电子版合同上传（文件类型非法时回滚并提示）
    saved = _save_contract_file(request.files.get('contract_file'), c.id)
    if saved == '__INVALID__':
        db.session.rollback()
        return redirect(url_for('hr.contracts'))
    if saved:
        c.file_name = saved
    db.session.commit()
    flash('合同已添加' + ('（已上传电子版）' if saved else ''))
    return redirect(url_for('hr.contracts'))


@bp.route('/hr/contracts/<int:cid>/edit', methods=['GET'])
@login_required
@permission_required('hr.employee.manage')
def contract_edit(cid):
    c = Contract.query.get_or_404(cid)
    employees = Employee.query.order_by(Employee.id).all()
    return render_template('hr/contract_edit.html', c=c, employees=employees)


@bp.route('/hr/contracts/<int:cid>/edit', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def contract_update(cid):
    c = Contract.query.get_or_404(cid)
    employee_id = request.form.get('employee_id')
    if not employee_id:
        flash('请选择员工')
        return redirect(url_for('hr.contract_edit', cid=cid))
    emp = Employee.query.get_or_404(int(employee_id))
    c.employee_id = emp.id
    c.contract_type = 'labor' if emp.staff_type == 'internal' else 'agency'
    c.contract_no = request.form.get('contract_no', '').strip()
    c.start_date = _parse_date(request.form.get('start_date'))
    c.end_date = _parse_date(request.form.get('end_date'))
    c.status = request.form.get('status', 'active')
    c.file_path = request.form.get('file_path', '').strip()
    c.note = request.form.get('note', '').strip()
    # 电子版合同：若重新上传，替换旧文件
    saved = _save_contract_file(request.files.get('contract_file'), c.id)
    if saved == '__INVALID__':
        db.session.rollback()
        return redirect(url_for('hr.contract_edit', cid=cid))
    if saved:
        c.file_name = saved
    db.session.commit()
    flash('合同已更新' + ('（已更新电子版）' if saved else ''))
    return redirect(url_for('hr.contracts'))


@bp.route('/hr/contracts/<int:cid>/download', methods=['GET'])
@login_required
def contract_download(cid):
    """下载电子版合同文件（登录即可下载，备份查阅用）"""
    c = Contract.query.get_or_404(cid)
    if not c.file_name:
        flash('该合同尚未上传电子版文件')
        return redirect(url_for('hr.contracts'))
    # 仅允许下载本上传目录里的文件，防止路径穿越
    upload_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'contracts')
    # 用原始文件名做下载名（拼接员工名 + 扩展名，便于识别）
    ext = os.path.splitext(c.file_name)[1]
    emp_name = c.employee.name if c.employee else '员工'
    download_name = '%s_劳动合同%s' % (emp_name, ext)
    return send_from_directory(upload_dir, c.file_name, as_attachment=True,
                               download_name=download_name)


# ===================== 人事 · 工资自定义字段配置（HR 后台） =====================

@bp.route('/hr/salary-fields', methods=['GET'])
@login_required
@permission_required('hr.salary.manage')
def salary_fields():
    """工资自定义字段：列出已配置的自定义工资项"""
    items = SalaryField.query.order_by(SalaryField.id).all()
    return render_template('hr/salary_fields.html', items=items)


@bp.route('/hr/salary-fields/new', methods=['POST'])
@login_required
@permission_required('hr.salary.manage')
def salary_field_new():
    """新增自定义工资项（如 交通补贴）"""
    code = request.form.get('code', '').strip()
    name = request.form.get('name', '').strip()
    if not code or not name:
        flash('编码和名称不能为空')
        return redirect(url_for('hr.salary_fields'))
    if SalaryField.query.filter_by(code=code).first():
        flash('该字段编码已存在')
        return redirect(url_for('hr.salary_fields'))
    f = SalaryField(code=code, name=name,
                    is_deduction=bool(request.form.get('is_deduction')))
    db.session.add(f)
    db.session.commit()
    flash('工资字段已添加')
    return redirect(url_for('hr.salary_fields'))


@bp.route('/hr/salary-fields/<int:fid>/delete', methods=['POST'])
@login_required
@permission_required('hr.salary.manage')
def salary_field_delete(fid):
    f = SalaryField.query.get_or_404(fid)
    db.session.delete(f)
    db.session.commit()
    flash('工资字段已删除')
    return redirect(url_for('hr.salary_fields'))


# ===================== 人事 · 员工批量导入（CSV，简单优先，用标准库 csv） =====================

# CSV 导入字段（供模板参考），与导入解析一一对应
_IMPORT_COLUMNS = ['employee_no', 'name', 'staff_type', 'level_code', 'agent_type',
                   'title', 'phone', 'org_name', 'hire_date', 'status']


@bp.route('/hr/employees/template')
@login_required
def employee_template():
    """下载 CSV 导入模板（含表头与一行示例）"""
    header = ','.join(_IMPORT_COLUMNS)
    sample = 'HY100,张三,internal,S,,,13800000000,海盈通保,2026-01-01,active'
    body = header + '\n' + sample + '\n'
    resp = make_response(body)
    resp.mimetype = 'text/csv'
    resp.headers['Content-Disposition'] = 'attachment; filename=employee_template.csv'
    return resp


@bp.route('/hr/employees/import', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_import():
    """批量导入员工：解析上传的 CSV（兼容 utf-8 / gbk），按工号去重，失败行计入提示"""
    f = request.files.get('file')
    if not f or not f.filename:
        flash('请先选择 CSV 文件')
        return redirect(url_for('hr.employees'))
    # 尝试多种编码读取，兼容 Excel 导出的 gbk
    raw = f.read()
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('文件编码无法识别（请保存为 UTF-8 或 GBK 的 CSV）')
        return redirect(url_for('hr.employees'))
    reader = csv.DictReader(io.StringIO(text))
    ok, skip = 0, 0
    for row in reader:
        eno = (row.get('employee_no') or '').strip()
        name = (row.get('name') or '').strip()
        if not eno or not name:
            skip += 1
            continue
        # 按工号去重：已存在则跳过（可重复执行，不覆盖）
        if Employee.query.filter_by(employee_no=eno).first():
            skip += 1
            continue
        emp = Employee(name=name, employee_no=eno)
        emp.staff_type = (row.get('staff_type') or 'internal').strip() or 'internal'
        if emp.staff_type == 'external':
            emp.agent_type = (row.get('agent_type') or '').strip() or None
        else:
            emp.agent_type = None
            lv_code = (row.get('level_code') or '').strip()
            if lv_code:
                lv = Level.query.filter_by(code=lv_code).first()
                emp.level_id = lv.id if lv else None
        emp.title = (row.get('title') or '').strip() or None
        emp.phone = (row.get('phone') or '').strip() or None
        org_name = (row.get('org_name') or '').strip()
        if org_name:
            org = Organization.query.filter_by(name=org_name).first()
            emp.org_id = org.id if org else None
        emp.hire_date = (row.get('hire_date') or '').strip() or None
        emp.status = (row.get('status') or 'active').strip() or 'active'
        db.session.add(emp)
        ok += 1
    db.session.commit()
    flash('导入完成：成功 {} 条，跳过（重复/缺字段）{} 条'.format(ok, skip))
    return redirect(url_for('hr.employees'))


# ===================== EHR · 三大业务板块入口（组织架构管理 / 人员薪资管理 / 人员差勤管理） =====================
# 业务模块按用户要求重排为三大板块，每个板块一个 hub 页聚合子功能。

@bp.route('/hr/org-admin', methods=['GET'])
@login_required
def org_admin():
    """组织架构管理 hub：入职登记 / 在职人员 / 离职人员(内部人才) / 人事合同 / 组织架构 / 员工花名册(只读)"""
    stats = {
        'onboard_pending': Onboarding.query.filter_by(status='pending').count(),
        'onboard_submitted': Onboarding.query.filter_by(status='submitted').count(),
        'active': Employee.query.filter_by(status='active').count(),
        'off': Employee.query.filter_by(status='left').count(),
        'contract': Contract.query.count(),
    }
    return render_template('hr/org_admin.html', stats=stats)


@bp.route('/hr/pay-admin', methods=['GET'])
@login_required
def pay_admin():
    """人员薪资管理 hub：工资清单（仅内勤，外勤佣金走小程序/营销系统）"""
    internal_count = Employee.query.filter_by(status='active', staff_type='internal').count()
    return render_template('hr/pay_admin.html', internal_count=internal_count)


@bp.route('/hr/att-admin', methods=['GET'])
@login_required
def att_admin():
    """人员差勤管理 hub：差勤（申请审批）/ 月度出勤查询 / 休假申请"""
    return render_template('hr/att_admin.html')


# ===================== EHR · 入职登记全流程 =====================
# 流程：操作员建号(自动工号 HY***) → 推送个人填报链接 → 个人提交 → 主管确认入册花名册。

def _next_employee_no():
    """自动生成下一个工号：扫描员工花名册 + 入职登记里的 HY 编号，取最大值 +1。
    格式 HY***（HY001/HY002/...），三位起步，超过 999 自然进位。"""
    max_no = 0
    for (eno,) in db.session.query(Employee.employee_no).all():
        m = re.match(r'^HY(\d+)$', eno or '')
        if m:
            max_no = max(max_no, int(m.group(1)))
    for (eno,) in db.session.query(Onboarding.employee_no).all():
        m = re.match(r'^HY(\d+)$', eno or '')
        if m:
            max_no = max(max_no, int(m.group(1)))
    return 'HY%03d' % (max_no + 1)


def _next_sys_code():
    """自动生成下一个系统编码：扫描入职登记的 RC 编号取最大值 +1（RC0001 起）。"""
    max_no = 0
    for (code,) in db.session.query(Onboarding.sys_code).all():
        m = re.match(r'^RC(\d+)$', code or '')
        if m:
            max_no = max(max_no, int(m.group(1)))
    return 'RC%04d' % (max_no + 1)


def _clean_employee_no(no):
    """工号格式校验：汉字/字母/数字/短横线/下划线，不超过 16 位。返回 (规范值, 错误提示)。"""
    no = (no or '').strip()
    if not no:
        return '', '工号不能为空'
    if len(no) > 16:
        return '', '工号最长 16 位'
    if not re.match(r'^[0-9A-Za-z\-_\u4e00-\u9fa5]+$', no):
        return '', '工号只能用汉字/字母/数字/短横线/下划线'
    return no, ''


def _no_taken(no, exclude_emp_id=None, exclude_onb_id=None):
    """工号占用校验：员工花名册(Employee) 与入职登记(Onboarding) 任一处已用即算被占。
    改号时用 exclude_* 把自身记录排除，避免"自己和自己冲突"。返回占用位置或空串。"""
    q = Employee.query.filter(Employee.employee_no == no)
    if exclude_emp_id:
        q = q.filter(Employee.id != exclude_emp_id)
    if q.first():
        return '员工花名册'
    q2 = Onboarding.query.filter(Onboarding.employee_no == no)
    if exclude_onb_id:
        q2 = q2.filter(Onboarding.id != exclude_onb_id)
    return '入职登记' if q2.first() else ''


def _load_json_list(text):
    """把 JSON 文本安全解析成列表（履历子集用），坏数据返回空列表。"""
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _parse_idcard(idcard):
    """从 18 位身份证解析出生日期与年龄；非法返回 (None, None)。（#145）"""
    if not idcard or not re.match(r'^\d{17}[\dXx]$', idcard or ''):
        return None, None
    try:
        b = date(int(idcard[6:10]), int(idcard[10:12]), int(idcard[12:14]))
    except ValueError:
        return None, None
    today = date.today()
    age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return b, age


def _clamp_base(amount, cfg):
    """缴费基数 = 金额夹在 [base_min, base_max] 之间（上限为 0 视为未设）。"""
    v = float(amount or 0)
    if not cfg:
        return v
    lo = float(cfg.base_min or 0)
    hi = float(cfg.base_max or 0) or float('inf')
    return max(lo, min(v, hi))


def _calc_social(total, cfg):
    """按五险一金配置测算【个人】缴纳额：(养老, 医疗, 失业, 公积金)。基数 = clamp(总薪资)。"""
    if not total or total <= 0 or not cfg:
        return 0.0, 0.0, 0.0, 0.0
    base = _clamp_base(total, cfg)
    return (round(base * (cfg.pension_pct or 0) / 100.0, 2),
            round(base * (cfg.medical_pct or 0) / 100.0, 2),
            round(base * (cfg.unemploy_pct or 0) / 100.0, 2),
            round(base * (cfg.fund_pct or 0) / 100.0, 2))


def _calc_corp_social(total, cfg):
    """测算【单位（企业）】承担的五险一金：(养老, 医疗, 失业, 工伤, 生育, 公积金)。
    工伤/生育个人不缴，只有单位比例；公积金单位另有自己的比例。"""
    if not total or total <= 0 or not cfg:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    base = _clamp_base(total, cfg)
    return (round(base * (cfg.corp_pension_pct or 0) / 100.0, 2),
            round(base * (cfg.corp_medical_pct or 0) / 100.0, 2),
            round(base * (cfg.corp_unemploy_pct or 0) / 100.0, 2),
            round(base * (cfg.corp_injury_pct or 0) / 100.0, 2),
            round(base * (cfg.corp_maternity_pct or 0) / 100.0, 2),
            round(base * (cfg.corp_fund_pct or 0) / 100.0, 2))


def _corp_total(total, cfg):
    """单位五险一金合计"""
    return round(sum(_calc_corp_social(total, cfg)), 2)


def _total_cost(salary, cfg):
    """总人力成本 = 总薪资 + 单位五险一金合计"""
    s = float(salary or 0)
    return round(s + _corp_total(s, cfg), 2)


def _solve_salary_from_cost(cost, cfg):
    """已知【总人力成本】反推【总薪资】。单位五险一金按基数分段（区间内按比例、越界按上下限固定），
    所以分段解析：设单位比例合计 r，
      区间内  ：cost = 薪资 ×(1+r)          → 薪资 = cost / (1+r)
      超上限  ：单位部分固定 = 上限 × r      → 薪资 = cost − 上限 × r
      低于下限：单位部分固定 = 下限 × r      → 薪资 = cost − 下限 × r
    返回 (总薪资, 单位五险一金合计, 有效基数)。"""
    c = float(cost or 0)
    if c <= 0 or not cfg:
        return 0.0, 0.0, 0.0
    r = ((cfg.corp_pension_pct or 0) + (cfg.corp_medical_pct or 0) + (cfg.corp_unemploy_pct or 0)
         + (cfg.corp_injury_pct or 0) + (cfg.corp_maternity_pct or 0) + (cfg.corp_fund_pct or 0)) / 100.0
    if r <= 0:
        return round(c, 2), 0.0, round(c, 2)
    lo = float(cfg.base_min or 0)
    hi = float(cfg.base_max or 0) or float('inf')
    s = c / (1 + r)
    if s < lo:
        s = max(0.0, c - lo * r)
    elif s > hi:
        s = c - hi * r
    s = max(0.0, round(s, 2))
    return s, _corp_total(s, cfg), round(_clamp_base(s, cfg), 2)


def _social_summary(salary, cfg):
    """一次性测算结果（按总薪资正算），模板/接口共用，保证前后端口径一致。"""
    s = float(salary or 0)
    pen, med, une, fund = _calc_social(s, cfg)
    cpen, cmed, cune, cinj, cmat, cfund = _calc_corp_social(s, cfg)
    return {
        'salary': round(s, 2), 'base': round(_clamp_base(s, cfg), 2),
        'personal': [('养老', pen), ('医疗', med), ('失业', une), ('公积金', fund)],
        'personal_total': round(pen + med + une + fund, 2),
        'corp': [('养老', cpen), ('医疗', cmed), ('失业', cune), ('工伤', cinj),
                 ('生育', cmat), ('公积金', cfund)],
        'corp_total': round(cpen + cmed + cune + cinj + cmat + cfund, 2),
        'cost': round(s + cpen + cmed + cune + cinj + cmat + cfund, 2),
    }


def _social_cfg_json(cfg):
    """把配置整理成前端 JS 可用的扁平结构（前端实时测算用，口径与后端一致）。"""
    if not cfg:
        return {}
    return {
        'person': {'养老': cfg.pension_pct or 0, '医疗': cfg.medical_pct or 0,
                   '失业': cfg.unemploy_pct or 0, '公积金': cfg.fund_pct or 0},
        'corp': {'养老': cfg.corp_pension_pct or 0, '医疗': cfg.corp_medical_pct or 0,
                 '失业': cfg.corp_unemploy_pct or 0, '工伤': cfg.corp_injury_pct or 0,
                 '生育': cfg.corp_maternity_pct or 0, '公积金': cfg.corp_fund_pct or 0},
        'baseMin': cfg.base_min or 0,
        'baseMax': cfg.base_max or 0,
    }


def _load_allowance_items():
    """取启用的岗位补贴项（按 sort 排序），建号页据此预填金额。"""
    return (HrAllowanceItem.query.filter_by(enabled=True)
            .order_by(HrAllowanceItem.sort, HrAllowanceItem.id).all())


def _apply_salary_form(o, form):
    """从表单读取薪资构成并落库（建号 / 修改共用）：
      总薪资 = 基本薪资 + 绩效薪资 + 岗位补贴（后端合计，前端只做展示）；
      岗位补贴明细从 allowance_name[] / allowance_amount[] 平行数组收集；
      试用薪资比例仅在「是试用期」时有意义，非试用期一律置 0；
      五险一金按当前配置重算（个人部分入库）。"""
    o.regular_base = float(form.get('regular_base') or 0) or 0
    o.regular_perf = float(form.get('regular_perf') or 0) or 0
    # 岗位补贴口径：优先由「补贴明细」逐项合计（明细是补贴构成的唯一来源）；
    # 未提交任何明细行（后台还没配置补贴项）时，才采用手填的岗位补贴金额。
    names = form.getlist('allowance_name')
    amounts = form.getlist('allowance_amount')
    detail = []
    for i, nm in enumerate(names):
        nm = (nm or '').strip()
        if not nm:
            continue
        try:
            amt = float(amounts[i] or 0)
        except (ValueError, IndexError):
            amt = 0.0
        detail.append({'name': nm, 'amount': round(amt, 2)})
    o.allowance_detail = json.dumps(detail, ensure_ascii=False) if detail else None
    o.regular_allow = (round(sum(d['amount'] for d in detail), 2) if detail
                       else (float(form.get('regular_allow') or 0) or 0))
    o.regular_total = round(o.regular_base + o.regular_perf + o.regular_allow, 2)
    pct = float(form.get('probation_salary_pct') or 0) or 0
    o.probation_salary_pct = pct if o.probation == 'Y' else 0
    cfg = HrSocialConfig.query.get(1)
    (o.social_pension, o.social_medical,
     o.social_unemploy, o.social_fund) = _calc_social(o.regular_total, cfg)
    return cfg


ONBOARD_STATUS_NAME = {
    'pending': '待个人填报', 'submitted': '待主管确认',
    'returned': '已退回修改', 'confirmed': '已入册', 'cancelled': '已取消',
}


@bp.route('/hr/onboards', methods=['GET'])
@login_required
def onboard_list():
    """入职登记列表：全流程跟踪（建号 → 个人填报 → 主管确认 → 入册）"""
    items = Onboarding.query.order_by(Onboarding.id.desc()).all()
    orgs = Organization.query.order_by(Organization.id).all()
    levels = Level.query.order_by(Level.code).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))
    cfg = HrSocialConfig.query.get(1)
    return render_template('hr/onboard_list.html', items=items, orgs=orgs, levels=levels,
                           can_manage=can_manage, status_name=ONBOARD_STATUS_NAME,
                           cfg=cfg, allowance_items=_load_allowance_items(),
                           cfg_json=_social_cfg_json(cfg))


@bp.route('/hr/onboards/new', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_new():
    """操作员建号（#145 增强）：一次性录入招募阶段全部信息，系统自动测算年龄/五险一金。"""
    name = request.form.get('name', '').strip()
    if not name:
        flash('姓名不能为空')
        return redirect(url_for('hr.onboard_list'))
    o = Onboarding(name=name, sys_code=_next_sys_code())
    # 工号：可人工指定（如 001-010 留给公司创始人），留空则按 HY*** 自动给出候选值
    want_no = (request.form.get('employee_no') or '').strip()
    if want_no:
        want_no, err = _clean_employee_no(want_no)
        if err:
            flash('工号未按您的填写保存（%s），已自动生成：%s' % (err, _next_employee_no()))
            o.employee_no = _next_employee_no()
        elif _no_taken(want_no):
            flash('工号 %s 已被占用，已自动生成：%s' % (want_no, _next_employee_no()))
            o.employee_no = _next_employee_no()
        else:
            o.employee_no = want_no
    else:
        o.employee_no = _next_employee_no()
    o.phone = request.form.get('phone', '').strip()
    o.position = request.form.get('position', '').strip()
    o.job_title = request.form.get('job_title', '').strip()
    # 入职日期：由 HR 管理员在建号/修改时填写（不走个人自助填报）
    o.hire_date = _parse_date(request.form.get('hire_date'))
    org_id = request.form.get('org_id')
    o.org_id = int(org_id) if org_id else None
    # ===== #145 招募阶段字段 =====
    o.gender = request.form.get('gender', '').strip() or None
    # 身份证 → 自动测算出生日期 + 入职年龄
    id_card = request.form.get('id_card', '').strip()
    if id_card:
        birth, age = _parse_idcard(id_card)
        if birth is None:
            flash('身份证号码格式不正确（应为18位），该字段未保存')
        else:
            o.id_card = id_card
            o.birth_date = birth
            o.age = age
    # 职级（招募阶段预选，主管确认时可改）
    level_id = request.form.get('level_id')
    o.level_id = int(level_id) if level_id else None
    # 试用期（是/否，起止 ≤ 6 个月）
    o.probation = 'Y' if request.form.get('probation') == 'Y' else 'N'
    ps = _parse_date(request.form.get('probation_start'))
    pe = _parse_date(request.form.get('probation_end'))
    if o.probation == 'Y':
        if not (ps and pe):
            flash('已选试用期，请补全起止日期')
            o.probation = 'N'
        elif (pe - ps).days > 183:
            flash('试用期不得超过 6 个月（约183天），该设置已忽略')
            o.probation = 'N'
        else:
            o.probation_start = ps
            o.probation_end = pe
    # 薪资构成（基本+绩效+岗位补贴=总薪资）+ 五险一金按当前配置自动测算
    _apply_salary_form(o, request.form)
    # token：随机码，个人填报链接的唯一凭证（无需登录即可填报）
    o.token = uuid.uuid4().hex
    o.status = 'pending'
    db.session.add(o)
    db.session.commit()
    flash('入职登记已建号：系统编码 %s ／ 工号 %s。请把个人填报链接推送给本人填写（工号如需调整，可在下方列表点「改工号」）'
          % (o.sys_code, o.employee_no))
    return redirect(url_for('hr.onboard_list'))


@bp.route('/hr/onboard/form/<token>', methods=['GET'])
def onboard_form(token):
    """个人填报页：新员工通过链接打开（无需登录），按人事卡片格式填写。
    GET 展示表单；pending（待填报）与 returned（被退回修改）两种状态可填，
    returned 时会把上次填的内容预填出来，方便本人在原基础上修改。"""
    o = Onboarding.query.filter_by(token=token).first_or_404()
    # 动态行（工作履历/教育经历）统一取出，可填时用于预填，只读回显时也可展示
    wh, edu = _load_json_list(o.work_history), _load_json_list(o.education)
    editable = o.status in ('pending', 'returned')
    return render_template('hr/onboard_form.html', o=o, readonly=not editable,
                           work_history=wh, education=edu)


@bp.route('/hr/onboard/form/<token>', methods=['POST'])
def onboard_submit(token):
    """个人提交填报：写入联系信息/基本信息/工作履历/教育经历，状态改为 submitted（待主管确认）。"""
    o = Onboarding.query.filter_by(token=token).first_or_404()
    # pending（首次填报）与 returned（被退回后重填）都允许提交
    if o.status not in ('pending', 'returned'):
        flash('该入职登记当前状态不可填报')
        return redirect(url_for('hr.onboard_form', token=token))
    # 公司邮箱：只填本地名自动补全 @hywinpro.cn（保持原来"空值存空串"的语义）
    o.email = _norm_company_email(request.form.get('email')) or ''
    o.gender = request.form.get('gender', '').strip()
    o.birth_date = _parse_date(request.form.get('birth_date'))
    o.work_start_date = _parse_date(request.form.get('work_start_date'))
    # 入职日期由 HR 管理员在建号/修改时填写，个人填报不覆盖（避免个人提交把 HR 的值清掉）
    o.marital = request.form.get('marital', '').strip()
    o.children = request.form.get('children', '').strip()
    o.hometown = request.form.get('hometown', '').strip()
    o.hobby = request.form.get('hobby', '').strip()
    o.last_job = request.form.get('last_job', '').strip()
    # 履历子集：前端动态行以 JSON 字符串整体提交，后端校验后存储
    for field in ('work_history', 'education'):
        raw = request.form.get(field, '').strip()
        if raw:
            try:
                data = json.loads(raw)
                setattr(o, field, json.dumps(data, ensure_ascii=False) if isinstance(data, list) else None)
            except ValueError:
                pass  # JSON 非法则跳过该子集，不阻断提交
    o.status = 'submitted'
    o.return_reason = None  # 重新提交后清空退回原因
    db.session.commit()
    return render_template('hr/onboard_form.html', o=o, readonly=True, submitted=True,
                           work_history=_load_json_list(o.work_history),
                           education=_load_json_list(o.education))


@bp.route('/hr/onboards/<int:oid>/confirm', methods=['GET'])
@login_required
@permission_required('hr.employee.manage')
def onboard_confirm(oid):
    """主管确认页：左侧展示个人填报内容（只读），右侧完善用工类型/职级/代理类型/职务/部门。"""
    o = Onboarding.query.get_or_404(oid)
    levels = Level.query.order_by(Level.code).all()
    orgs = Organization.query.order_by(Organization.id).all()
    return render_template('hr/onboard_confirm.html', o=o, levels=levels, orgs=orgs,
                           work_history=_load_json_list(o.work_history),
                           education=_load_json_list(o.education))


@bp.route('/hr/onboards/<int:oid>/confirm', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_confirm_save(oid):
    """主管确认：完善用工类型/职级/代理类型/职务/部门 → 创建员工档案 → 数据自动备份至花名册。"""
    o = Onboarding.query.get_or_404(oid)
    if o.status != 'submitted':
        flash('该登记不在"待主管确认"状态，无法入册')
        return redirect(url_for('hr.onboard_list'))
    # 创建员工档案（Employee），工号沿用入职登记里登记的工号（可被人工改过）
    if Employee.query.filter_by(employee_no=o.employee_no).first():
        flash('工号 %s 已被花名册占用，请先在招募列表「改工号」调整后再入册' % o.employee_no)
        return redirect(url_for('hr.onboard_confirm', oid=oid))
    emp = Employee(name=o.name, employee_no=o.employee_no)
    emp.phone = o.phone
    emp.hire_date = o.hire_date or date.today()
    emp.staff_type = request.form.get('staff_type', 'internal')
    if emp.staff_type == 'external':
        # 外勤：代理类型（渠道总/主管/代理人），不挂职级
        emp.agent_type = request.form.get('agent_type', '').strip() or None
        emp.level_id = None
    else:
        # 内勤：挂 S/A/B/C/D/E 职级
        emp.agent_type = None
        level_id = request.form.get('level_id')
        emp.level_id = int(level_id) if level_id else None
    org_id = request.form.get('org_id')
    emp.org_id = int(org_id) if org_id else o.org_id
    emp.title = request.form.get('title', '').strip() or o.job_title
    # 个人填报的档案字段同步入册（花名册详情可查）
    emp.gender = o.gender
    emp.birth_date = o.birth_date
    emp.idcard = o.id_card  # 身份证：入册时从入职登记带入，人事卡片展示
    emp.email = o.email
    emp.work_start_date = o.work_start_date
    emp.marital = o.marital
    emp.children = o.children
    emp.hometown = o.hometown
    emp.hobby = o.hobby
    emp.last_job = o.last_job
    emp.status = 'active'
    db.session.add(emp)
    db.session.flush()  # 先拿到 emp.id，才能把个人填报的教育经历挂到该员工档案
    edu_n = _sync_education_from_onboarding(emp, o.education)
    o.status = 'confirmed'
    o.confirmed_at = datetime.utcnow()
    db.session.commit()
    flash('确认完成：%s（%s）已入册员工花名册%s'
          % (emp.name, emp.employee_no, '，已同步教育经历 %d 条' % edu_n if edu_n else ''))
    return redirect(url_for('hr.onboard_list'))


@bp.route('/hr/onboards/<int:oid>/cancel', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_cancel(oid):
    """取消入职登记（未入册前可取消）"""
    o = Onboarding.query.get_or_404(oid)
    if o.status in ('confirmed',):
        flash('该登记已入册，不可取消')
        return redirect(url_for('hr.onboard_list'))
    o.status = 'cancelled'
    db.session.commit()
    flash('入职登记已取消')
    return redirect(url_for('hr.onboard_list'))


@bp.route('/hr/onboards/<int:oid>/delete', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_delete(oid):
    """删除入职登记（#145 补充）：把不再需要的招募记录彻底清掉。

    口径：仅「未入册」的登记可删除（pending/submitted/returned/cancelled）；
         已入册(confirmed)的登记关联着员工花名册档案，一律不可删除。
    删除前先在库内取好标量（姓名/工号），再删记录、写审计日志。
    """
    o = Onboarding.query.get_or_404(oid)
    if o.status == 'confirmed':
        flash('该登记已入册员工花名册，不可删除（如需清理请先在花名册办理离职）')
        return redirect(url_for('hr.onboard_list'))
    name, no = o.name, (o.employee_no or '')
    db.session.delete(o)
    db.session.commit()
    log_audit('删除入职登记', module='EHR', target_type='Onboarding',
              target_id=oid, target_no=no, detail='彻底删除招募登记：%s（%s）' % (name, no))
    flash('已删除入职登记：%s（%s）' % (name, no))
    return redirect(url_for('hr.onboard_list'))


@bp.route('/hr/onboards/<int:oid>/return', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_return(oid):
    """退回修改（#145）：主管审核个人填报时若发现填写不符合要求，退回本人重填。
    仅"待主管确认(submitted)"状态可退回；退回后状态=returned，
    本人可凭原填报链接重新填写（表单会预填上次内容），重新提交后回到 submitted。"""
    o = Onboarding.query.get_or_404(oid)
    if o.status != 'submitted':
        flash('仅"待主管确认"状态的登记可以退回修改')
        return redirect(url_for('hr.onboard_list'))
    o.status = 'returned'
    o.return_reason = (request.form.get('return_reason') or '').strip() or None
    db.session.commit()
    log_audit('退回入职登记', module='EHR', target_type='Onboarding',
              target_id=o.id, target_no=o.employee_no or '',
              detail='退回原因：%s' % (o.return_reason or '未填写'))
    flash('已退回 %s（%s）修改，请把个人填报链接重新推送给本人' % (o.name, o.employee_no))
    return redirect(url_for('hr.onboard_list'))


# ===================== EHR · 入职登记修改（#145 补充）=====================
# 口径：建号后录入的招募信息（部门/岗位/职务/手机/身份证/职级/试用期/薪资等）如填错，
#       有权限者可在此更正。系统编码不可改；工号改号走「改工号」入口。

@bp.route('/hr/onboards/<int:oid>/edit', methods=['GET'])
@login_required
@permission_required('hr.employee.manage')
def onboard_edit(oid):
    """修改入职登记页：预填建号阶段录入的全部字段（工号/系统编码只读展示）。"""
    o = Onboarding.query.get_or_404(oid)
    orgs = Organization.query.order_by(Organization.id).all()
    levels = Level.query.order_by(Level.code).all()
    cfg = HrSocialConfig.query.get(1)
    # 已保存的补贴明细：名称 → 金额（编辑时按它预填，未匹配到的项回落到配置默认值）
    detail_map = {}
    for r in _load_json_list(o.allowance_detail):
        if isinstance(r, dict) and r.get('name'):
            try:
                detail_map[r['name']] = float(r.get('amount') or 0)
            except (TypeError, ValueError):
                detail_map[r['name']] = 0.0
    return render_template('hr/onboard_edit.html', o=o, orgs=orgs, levels=levels,
                           status_name=ONBOARD_STATUS_NAME,
                           cfg=cfg, allowance_items=_load_allowance_items(),
                           cfg_json=_social_cfg_json(cfg), detail_map=detail_map)


@bp.route('/hr/onboards/<int:oid>/edit', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_update(oid):
    """保存「修改入职登记」：更新建号阶段字段，重算年龄与五险一金；已入册的同步花名册档案。"""
    o = Onboarding.query.get_or_404(oid)
    name = request.form.get('name', '').strip()
    if not name:
        flash('姓名不能为空')
        return redirect(url_for('hr.onboard_edit', oid=oid))

    o.name = name
    o.phone = request.form.get('phone', '').strip()
    o.position = request.form.get('position', '').strip()
    o.job_title = request.form.get('job_title', '').strip()
    # 入职日期：由 HR 管理员填写
    o.hire_date = _parse_date(request.form.get('hire_date'))
    org_id = request.form.get('org_id')
    o.org_id = int(org_id) if org_id else None
    o.gender = request.form.get('gender', '').strip() or None
    # 身份证 → 重算出生日期/年龄；本次清空身份证则一并清掉测算结果
    id_card = request.form.get('id_card', '').strip()
    if id_card:
        birth, age = _parse_idcard(id_card)
        if birth is None:
            flash('身份证号码格式不正确（应为18位），该字段未保存')
        else:
            o.id_card, o.birth_date, o.age = id_card, birth, age
    else:
        o.id_card = None
        o.birth_date = None
        o.age = None
    level_id = request.form.get('level_id')
    o.level_id = int(level_id) if level_id else None
    # 试用期（是/否，起止 ≤ 6 个月）
    o.probation = 'Y' if request.form.get('probation') == 'Y' else 'N'
    ps = _parse_date(request.form.get('probation_start'))
    pe = _parse_date(request.form.get('probation_end'))
    if o.probation == 'Y':
        if not (ps and pe):
            flash('已选试用期，请补全起止日期')
            o.probation = 'N'
        elif (pe - ps).days > 183:
            flash('试用期不得超过 6 个月（约183天），该设置已忽略')
            o.probation = 'N'
        else:
            o.probation_start, o.probation_end = ps, pe
    if o.probation != 'Y':  # 非试用期则清空起止，避免留旧值
        o.probation_start = None
        o.probation_end = None
    # 薪资构成 + 五险一金按当前配置重算
    _apply_salary_form(o, request.form)
    # 已入册：同步花名册档案（仅 Employee 存在且可映射的字段）
    synced = False
    if o.status == 'confirmed':
        emp = Employee.query.filter_by(employee_no=o.employee_no).first()
        if emp:
            emp.name = o.name
            emp.phone = o.phone
            emp.org_id = o.org_id
            emp.title = o.job_title
            emp.level_id = o.level_id
            emp.gender = o.gender
            emp.birth_date = o.birth_date
            synced = True
    db.session.commit()
    flash('入职登记已更新' + ('，并同步花名册档案' if synced else ''))
    return redirect(url_for('hr.onboard_list'))


# ===================== EHR · 工号管理（#145 补充） =====================
# 口径：系统编码 RC**** 由系统自动生成、不可修改，是登记记录的稳定标识；
#       工号（员工编号）由人事人工指定/修改 —— 未来 001-010 这类编号要留给公司创始人，
#       所以工号不能锁死在自动规则里。改号会同步花名册档案与已开通的登录账号。

@bp.route('/hr/onboards/<int:oid>/set-no', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def onboard_set_no(oid):
    """修改入职登记的工号（系统编码不变）。已入册的登记：同步花名册档案 + 已开通账号的登录名。"""
    o = Onboarding.query.get_or_404(oid)
    new_no, err = _clean_employee_no(request.form.get('employee_no'))
    if err:
        flash(err)
        return redirect(url_for('hr.onboard_list'))
    old_no = o.employee_no
    if new_no == old_no:
        flash('工号未变化')
        return redirect(url_for('hr.onboard_list'))
    # 已入册的登记，花名册里那条同名档案也要一起改，先取出来排除在冲突检查之外
    emp = Employee.query.filter_by(employee_no=old_no).first() if o.status == 'confirmed' else None
    taken = _no_taken(new_no, exclude_emp_id=emp.id if emp else None, exclude_onb_id=o.id)
    if taken:
        flash('工号 %s 已被占用（%s），请换一个' % (new_no, taken))
        return redirect(url_for('hr.onboard_list'))
    if User.query.filter_by(username=new_no).first():
        flash('工号 %s 已被占用（存在同名登录账号），请换一个' % new_no)
        return redirect(url_for('hr.onboard_list'))
    o.employee_no = new_no
    if emp:
        emp.employee_no = new_no
        u = User.query.filter_by(username=old_no).first()
        if u:
            u.username = new_no  # 登录名=工号：改号后用新工号登录，密码不变
    db.session.commit()
    log_audit('hr_employee_no_change', module='hr', target_type='入职登记',
              target_no=new_no,
              detail='工号 %s → %s（系统编码 %s 不变）' % (old_no, new_no, o.sys_code or '—'))
    flash('工号已由 %s 改为 %s（系统编码 %s 不变）' % (old_no, new_no, o.sys_code or '—'))
    return redirect(url_for('hr.onboard_list'))


@bp.route('/hr/employees/<int:eid>/set-no', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_set_no(eid):
    """修改花名册员工工号（员工档案页入口）：校验唯一，同步已开通账号的登录名与对应的入职登记。"""
    emp = Employee.query.get_or_404(eid)
    new_no, err = _clean_employee_no(request.form.get('employee_no'))
    if err:
        flash(err)
        return redirect(url_for('hr.employee_view', eid=eid))
    old_no = emp.employee_no
    if new_no == old_no:
        flash('工号未变化')
        return redirect(url_for('hr.employee_view', eid=eid))
    taken = _no_taken(new_no, exclude_emp_id=emp.id)
    if taken:
        flash('工号 %s 已被占用（%s），请换一个' % (new_no, taken))
        return redirect(url_for('hr.employee_view', eid=eid))
    if User.query.filter_by(username=new_no).first():
        flash('工号 %s 已被占用（存在同名登录账号），请换一个' % new_no)
        return redirect(url_for('hr.employee_view', eid=eid))
    emp.employee_no = new_no
    # 同步：对应的入职登记 + 已开通办公账号的登录名（否则员工用新工号登不上）
    onb = Onboarding.query.filter_by(employee_no=old_no).first()
    if onb:
        onb.employee_no = new_no
    u = User.query.filter_by(username=old_no).first()
    if u:
        u.username = new_no
    db.session.commit()
    log_audit('hr_employee_no_change', module='hr', target_type='员工',
              target_no=new_no, detail='工号 %s → %s' % (old_no, new_no))
    flash('工号已由 %s 改为 %s%s' % (old_no, new_no, '，登录名已同步' if u else ''))
    return redirect(url_for('hr.employee_view', eid=eid))



@bp.route('/hr/social-config', methods=['GET', 'POST'])
@login_required
@permission_required('hr.employee.manage')
def social_config():
    """五险一金比例端口（#145）：HR 调整各险个人比例与缴费基数上下限，招募时按此自动测算。"""
    cfg = HrSocialConfig.query.get(1)
    if request.method == 'POST':
        try:
            cfg.pension_pct = float(request.form.get('pension_pct') or 8)
            cfg.medical_pct = float(request.form.get('medical_pct') or 2)
            cfg.unemploy_pct = float(request.form.get('unemploy_pct') or 0.5)
            cfg.fund_pct = float(request.form.get('fund_pct') or 12)
            # 单位承担比例（用于「总人力成本」测算）
            cfg.corp_pension_pct = float(request.form.get('corp_pension_pct') or 16)
            cfg.corp_medical_pct = float(request.form.get('corp_medical_pct') or 8)
            cfg.corp_unemploy_pct = float(request.form.get('corp_unemploy_pct') or 0.5)
            cfg.corp_injury_pct = float(request.form.get('corp_injury_pct') or 0.5)
            cfg.corp_maternity_pct = float(request.form.get('corp_maternity_pct') or 0.8)
            cfg.corp_fund_pct = float(request.form.get('corp_fund_pct') or 12)
            cfg.base_min = float(request.form.get('base_min') or 0)
            cfg.base_max = float(request.form.get('base_max') or 999999)
        except ValueError:
            flash('比例/基数必须为数字')
            return redirect(url_for('hr.social_config'))
        cfg.note = request.form.get('note', '').strip()
        db.session.commit()
        log_audit('hr_social_config', module='hr', target_type='五险一金配置',
                  detail='个人比例 养老%s/医疗%s/失业%s/公积金%s；单位比例 养老%s/医疗%s/失业%s/工伤%s/生育%s/公积金%s'
                         % (cfg.pension_pct, cfg.medical_pct, cfg.unemploy_pct, cfg.fund_pct,
                            cfg.corp_pension_pct, cfg.corp_medical_pct, cfg.corp_unemploy_pct,
                            cfg.corp_injury_pct, cfg.corp_maternity_pct, cfg.corp_fund_pct))
        flash('五险一金比例配置已保存')
        return redirect(url_for('hr.social_config'))
    return render_template('hr/social_config.html', cfg=cfg)


@bp.route('/hr/allowance-config', methods=['GET', 'POST'])
@login_required
@permission_required('hr.employee.manage')
def allowance_config():
    """岗位补贴项设置（后台端口）：通讯费 / 交通费等补贴字段的增删改，一次保存。
    建号页的「岗位补贴」会按这些启用项预填金额并合计。"""
    if request.method == 'POST':
        ids = request.form.getlist('item_id')
        names = request.form.getlist('item_name')
        amounts = request.form.getlist('item_amount')
        enables = request.form.getlist('item_enabled')  # 与 names/amounts 同序（'1'=启用）
        touched = []
        rows = 0
        for i, nm in enumerate(names):
            nm = (nm or '').strip()
            if not nm:
                continue
            try:
                amt = float(amounts[i] or 0)
            except (ValueError, IndexError):
                amt = 0.0
            try:
                iid = int(ids[i]) if i < len(ids) and ids[i] else None
            except ValueError:
                iid = None
            obj = HrAllowanceItem.query.get(iid) if iid else None
            if obj is None:                       # 新行
                obj = HrAllowanceItem()
                db.session.add(obj)
            obj.name = nm[:32]
            obj.default_amount = round(amt, 2)
            obj.enabled = (i < len(enables) and enables[i] == '1')
            obj.sort = rows + 1
            touched.append(obj)
            rows += 1
        db.session.flush()  # 让新增行拿到 id，再判断哪些该删
        keep_ids = [o.id for o in touched]
        for old in HrAllowanceItem.query.all():
            if old.id not in keep_ids:            # 表单里被移除的行 → 删除
                db.session.delete(old)
        db.session.commit()
        log_audit('hr_allowance_config', module='hr', target_type='岗位补贴项',
                  detail='保存 %d 项' % rows)
        flash('岗位补贴项已保存（共 %d 项）' % rows)
        return redirect(url_for('hr.allowance_config'))
    items = HrAllowanceItem.query.order_by(HrAllowanceItem.sort, HrAllowanceItem.id).all()
    return render_template('hr/allowance_config.html', items=items)


# ===================== EHR · 在职 / 离职人员维护 + 异动（晋升/降职/调岗）+ 只读详情 =====================

@bp.route('/hr/employees/active', methods=['GET'])
@login_required
def employees_active():
    """在职人员信息维护：列表 + 异动（晋升/降职/调岗）+ 离职办理入口"""
    items = Employee.query.filter_by(status='active').order_by(Employee.employee_no).all()
    orgs = Organization.query.order_by(Organization.id).all()
    levels = Level.query.order_by(Level.code).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))
    # 已开通办公账号的工号集合（用于列表里隐藏"开通账号"按钮，避免重复开通）
    accounted_ids = {u.username for u in User.query.all()}
    return render_template('hr/employee_active.html', items=items, orgs=orgs, levels=levels,
                           can_manage=can_manage, accounted_ids=accounted_ids)


@bp.route('/hr/employees/off', methods=['GET'])
@login_required
def employees_off():
    """离职人员信息维护（内部人才库）：离职员工档案保留，可查看详情、可复职"""
    items = Employee.query.filter_by(status='left').order_by(Employee.leave_date.desc()).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))
    return render_template('hr/employee_off.html', items=items, can_manage=can_manage)


@bp.route('/hr/employees/<int:eid>/view', methods=['GET'])
@login_required
def employee_view(eid):
    """员工只读详情：联系信息/基本信息/任职信息/异动记录/合同，全站统一入口（花名册仅供查询）"""
    emp = Employee.query.get_or_404(eid)
    changes = (EmployeeChange.query.filter_by(employee_id=eid)
               .order_by(EmployeeChange.effective_date.desc(), EmployeeChange.id.desc()).all())
    contracts = Contract.query.filter_by(employee_id=eid).order_by(Contract.id.desc()).all()
    user = User.query.get(session.get('user_id'))
    can_manage = bool(user and user.level and
                      any(p.code == 'hr.employee.manage' for p in user.level.permissions))
    has_account = bool(User.query.filter_by(username=emp.employee_no).first())
    return render_template('hr/employee_view.html', emp=emp, changes=changes, contracts=contracts,
                           can_manage=can_manage, has_account=has_account)


@bp.route('/hr/employees/<int:eid>/change', methods=['GET', 'POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_change(eid):
    """人员异动：晋升 / 降职 / 调岗。POST 校验并记录异动快照，同步更新员工当前部门/职级/职务。"""
    emp = Employee.query.get_or_404(eid)
    levels = Level.query.order_by(Level.code).all()
    orgs = Organization.query.order_by(Organization.id).all()
    if request.method == 'POST':
        change_type = request.form.get('change_type', '').strip()
        if change_type not in ('晋升', '降职', '调岗'):
            flash('请选择异动类型（晋升/降职/调岗）')
            return redirect(url_for('hr.employee_change', eid=eid))
        # 记录异动前快照
        c = EmployeeChange(employee_id=emp.id, change_type=change_type)
        c.from_org_id = emp.org_id
        c.from_level_id = emp.level_id
        c.from_title = emp.title
        # 应用新值
        to_org = request.form.get('to_org_id')
        to_level = request.form.get('to_level_id')
        c.to_org_id = int(to_org) if to_org else emp.org_id
        c.to_level_id = int(to_level) if to_level else emp.level_id
        c.to_title = request.form.get('to_title', '').strip() or emp.title
        c.effective_date = _parse_date(request.form.get('effective_date')) or date.today()
        c.reason = request.form.get('reason', '').strip()
        c.operator = session.get('username', '')
        emp.org_id = c.to_org_id
        emp.level_id = c.to_level_id
        emp.title = c.to_title
        db.session.add(c)
        db.session.commit()
        flash('%s 异动已生效：%s → %s' % (change_type, emp.name, c.to_title or '职务不变'))
        return redirect(url_for('hr.employee_view', eid=eid))
    return render_template('hr/employee_change.html', emp=emp, levels=levels, orgs=orgs)


@bp.route('/hr/employees/<int:eid>/off-do', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def employee_off_do(eid):
    """离职办理：登记离职日期与离职原因（保留档案进入离职人员/内部人才库）；离职员工可复职。"""
    emp = Employee.query.get_or_404(eid)
    if emp.status == 'active':
        emp.status = 'left'
        emp.leave_date = _parse_date(request.form.get('leave_date')) or date.today()
        emp.leave_reason = request.form.get('leave_reason', '').strip()
        flash('%s 已办理离职，档案转入离职人员/内部人才库' % emp.name)
    else:
        emp.status = 'active'
        emp.leave_date = None
        emp.leave_reason = ''
        flash('%s 已复职' % emp.name)
    db.session.commit()
    return redirect(url_for('hr.employees_active'))


# ===================== EHR · 开通办公账号 + 员工自助完善个人信息（#145 第2项） =====================

@bp.route('/hr/employees/<int:eid>/open-account', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')  # 仅人事管理员可开通账号
def employee_open_account(eid):
    """开通系统办公权限：为已入册员工创建登录账号（登录名=工号，初始密码 123456），
    并置 need_profile=True，由系统向员工推送"完善个人入职电子信息"链接（员工登录后首页横幅）。"""
    emp = Employee.query.get_or_404(eid)
    if User.query.filter_by(username=emp.employee_no).first():
        flash('%s（%s）已开通办公账号，无需重复开通' % (emp.name, emp.employee_no))
        return redirect(url_for('hr.employee_view', eid=eid))
    u = User(username=emp.employee_no,
             password_hash=generate_password_hash('123456'),
             level_id=emp.level_id, org_id=emp.org_id)
    db.session.add(u)
    emp.need_profile = True
    db.session.commit()
    log_audit('hr_account_open', module='hr',
              target_type='员工', target_no=emp.employee_no,
              detail='开通办公账号，初始密码 123456')
    flash('已为 %s（工号 %s）开通办公账号：登录名=%s，初始密码=123456；请通知其登录后在「我的资料」完善个人信息'
          % (emp.name, emp.employee_no, emp.employee_no))
    return redirect(url_for('hr.employee_view', eid=eid))


@bp.route('/hr/my-profile', methods=['GET', 'POST'])
@login_required
def my_profile():
    """员工自助：完善个人入职电子信息。
    登录用户按工号（username）对应到本人 Employee；只改本人档案，不受 hr.employee.manage 限制。
    GET 展示已填表单（无对应员工档案时显示提示，仍返回 200）；POST 保存并清除 need_profile 待完善标记。"""
    emp = Employee.query.filter_by(employee_no=session.get('username')).first()
    if request.method == 'POST':
        if emp is None:
            flash('未找到与当前登录账号对应的员工档案（请确认登录名是否为工号）')
            return redirect(url_for('hr.hr_home'))
        emp.phone = request.form.get('phone', '').strip() or emp.phone
        # 公司邮箱：只填本地名自动补全 @hywinpro.cn；未填则保留原值
        emp.email = _norm_company_email(request.form.get('email')) or emp.email
        emp.address = request.form.get('address', '').strip() or None
        emp.marital = request.form.get('marital', '').strip() or None
        emp.emergency_name = request.form.get('emergency_name', '').strip() or None
        emp.emergency_phone = request.form.get('emergency_phone', '').strip() or None
        emp.bank_name = request.form.get('bank_name', '').strip() or None
        emp.bank_account = request.form.get('bank_account', '').strip() or None
        emp.hobby = request.form.get('hobby', '').strip() or None
        emp.hometown = request.form.get('hometown', '').strip() or None
        emp.need_profile = False
        db.session.commit()
        flash('个人入职电子信息已提交，感谢配合')
        return redirect(url_for('hr.my_profile'))
    return render_template('hr/my_profile.html', emp=emp, username=session.get('username'))


# ===================== EHR · 个人资料 / 人事卡片（#145 新增：员工查看本人档案） =====================
# 入口链路：系统总览首页「员工工卡」右上角「个人资料」→ 个人资料页 → 左侧「人事卡片」→ 正式版人事卡片。
# 口径：员工本人按「登录名 == 工号」对应档案；HR 管理员可带 ?eid=<员工id> 查看任意员工。

def _can_manage_hr():
    """当前登录人是否有 hr.employee.manage（用于判断能否查看他人档案）"""
    user = User.query.get(session.get('user_id'))
    return bool(user and user.level and
                any(p.code == 'hr.employee.manage' for p in user.level.permissions))


def _resolve_profile_emp():
    """解析要展示的员工档案。带 ?eid= 时需 HR 权限，否则只看登录账号对应的本人档案。
    返回 (emp, error_response)：emp 可能为 None（账号未匹配到档案，如 admin）。"""
    eid = request.args.get('eid', type=int)
    if eid:
        if not _can_manage_hr():
            flash('无权限查看他人档案')
            return None, redirect(url_for('hr.profile'))
        return Employee.query.get_or_404(eid), None
    return Employee.query.filter_by(employee_no=session.get('username')).first(), None


def _emp_changes(emp):
    """员工异动记录（晋升/降职/调岗），人事卡片「工作经历」与个人资料「工作履历」用"""
    if not emp:
        return []
    return (EmployeeChange.query.filter_by(employee_id=emp.id)
            .order_by(EmployeeChange.effective_date.desc(), EmployeeChange.id.desc()).all())


def _years_between(d, today=None):
    """按整年数算工龄/司龄/年龄（例：2007-07-01 到 2026-09-30 = 19）"""
    if not d:
        return None
    today = today or date.today()
    n = today.year - d.year - (1 if (today.month, today.day) < (d.month, d.day) else 0)
    return n if n >= 0 else None


def _card_calc(emp):
    """人事卡片用到的测算值（年龄/工龄/司龄）+ 单位名称"""
    comp = Organization.query.filter_by(type='company').first()
    return {
        'age': _years_between(emp.birth_date) if emp else None,
        'work_years': _years_between(emp.work_start_date) if emp else None,
        'company_years': _years_between(emp.hire_date) if emp else None,
        'company_name': (comp.name if comp else '') or '广东海盈通保智能科技有限公司',
        'today': date.today().isoformat(),
    }


@bp.route('/hr/profile', methods=['GET'])
@login_required
def profile():
    """个人资料（只读展示）：头像卡 + 分组档案信息 + 右侧锚点导航，左侧为「人事卡片」入口。
    与「我的资料(my-profile)」区别：那个是员工自助**填写**，这个是**查看**。"""
    emp, err = _resolve_profile_emp()
    if err:
        return err
    return render_template('hr/profile.html', emp=emp, changes=_emp_changes(emp),
                           calc=_card_calc(emp), sections=CARD_SECTIONS, items=_card_items(emp),
                           username=session.get('username'), can_manage=_can_manage_hr())


@bp.route('/hr/profile/card', methods=['GET'])
@login_required
def profile_card():
    """人事卡片：正式版式（参照公司人事卡片表单——基本信息/工作经历/教育经历/家庭信息/
    培训经历/专业技术职务/职业资格/奖励子集/惩处子集）。一期按现有档案字段渲染，
    尚未落地的子集板块保留表头并显示「暂无记录」，后续增量补齐。"""
    emp, err = _resolve_profile_emp()
    if err:
        return err
    return render_template('hr/card.html', emp=emp, changes=_emp_changes(emp),
                           calc=_card_calc(emp), sections=CARD_SECTIONS, items=_card_items(emp),
                           username=session.get('username'), can_manage=_can_manage_hr())


@bp.route('/hr/profile/photo', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def profile_photo():
    """HR 上传 / 更换 / 删除员工个人照片（完善人事档案）。

    - 带 ?eid=<id> 时作用于指定员工；不带则作用于「登录名 == 工号」的本人档案。
    - 删除：表单里带 del_photo=1（同表单里的「删除」按钮），清空照片并删磁盘文件。
    - 该入口与「员工档案编辑页」的照片上传共用 `_save_employee_photo`，格式白名单一致。
    """
    eid = request.args.get('eid', type=int) or request.form.get('eid', type=int)
    emp = None
    if eid:
        emp = Employee.query.get(eid)
    else:
        emp = Employee.query.filter_by(employee_no=session.get('username')).first()
    dest = url_for('hr.profile', eid=eid) if eid else url_for('hr.profile')
    if not emp:
        flash('未找到对应的员工档案，无法上传照片（请先在员工花名册按工号建档）')
        return redirect(dest)

    # 删除照片
    if request.form.get('del_photo'):
        _old = emp.photo
        emp.photo = None
        db.session.commit()
        _delete_photo_file(_old)
        log_audit('删除员工照片', module='EHR', target_type='Employee',
                  target_id=emp.id, target_no=emp.employee_no or '',
                  detail='清除 %s 的个人照片' % emp.name)
        flash('已移除 %s 的个人照片' % emp.name)
        return redirect(dest)

    # 上传 / 更换照片
    saved = _save_employee_photo(request.files.get('photo'), emp.id)
    if saved == '__INVALID__':
        return redirect(dest)
    if not saved:
        flash('请先选择要上传的照片文件（jpg / png / webp / gif）')
        return redirect(dest)
    _old = emp.photo
    emp.photo = saved
    db.session.commit()
    if _old and _old != saved:
        _delete_photo_file(_old)
    log_audit('上传员工照片', module='EHR', target_type='Employee',
              target_id=emp.id, target_no=emp.employee_no or '',
              detail='更新 %s 的个人照片' % emp.name)
    flash('已更新 %s 的个人照片' % emp.name)
    return redirect(dest)


# ===================== 人事卡片二期（#145）：七大子集板块 =====================
# 每个板块元组：(key, 名称, 模型, [(字段名, 表头)...])
#   - 表头顺序与人事卡片表格一一对应，录入页与卡片页都遍历本表渲染 → 只维护一处；
#   - categories 里「录入 / 编辑」入口需 hr.employee.manage 权限，员工本人只能查看。
CARD_SECTIONS = [
    ('edu', '教育经历', HrEducation, [
        ('start_time', '入学时间'), ('end_time', '毕业时间'), ('school', '毕业学校'),
        ('major', '专业名称'), ('edu_degree', '学历 / 学位'), ('edu_type', '学习形式 / 院校类别')]),
    ('family', '家庭信息', HrFamily, [
        ('relation', '家庭关系'), ('member_name', '成员姓名'), ('birth_date', '出生日期'),
        ('work_unit', '工作单位'), ('position', '岗位'), ('contact', '联系方式')]),
    ('training', '培训经历', HrTraining, [
        ('start_time', '开始时间'), ('end_time', '结束时间'), ('hours', '学时（天）'),
        ('organizer', '培训主办单位'), ('course_name', '培训班名称'), ('course_desc', '主要课程 / 获得证书')]),
    ('tech', '专业技术职务', HrTechTitle, [
        ('obtain_date', '取得时间'), ('skill_name', '技术技能名称'), ('series', '系列'),
        ('level', '等级'), ('major', '专业'), ('org_cert', '评审/鉴定机构 · 证书编号')]),
    ('qual', '职业资格', HrQualification, [
        ('obtain_date', '取证日期'), ('valid_until', '证书有效期'), ('level', '等级'),
        ('cert_name', '证书名称'), ('issuer', '发证单位'), ('cert_no', '证书编号 / 保管状态')]),
    ('reward', '奖励子集', HrReward, [
        ('reward_date', '获奖时间'), ('reward_name', '奖励名称'), ('judge_org', '评选单位'),
        ('reason', '奖励原因'), ('bonus_point', '奖励加分'), ('level', '奖励级别')]),
    ('punish', '惩处子集', HrPunishment, [
        ('punish_date', '惩处时间'), ('punish_name', '惩处名称'), ('approve_org', '惩处批准单位'),
        ('reason', '惩处原因'), ('deduct_point', '惩罚扣分'), ('level', '惩处级别')]),
]
CARD_SECTION_MAP = {s[0]: s for s in CARD_SECTIONS}


def _card_items(emp):
    """取某员工全部子集板块记录：{板块key: [记录...]}。员工为 None 时全为空列表。"""
    out = {}
    for key, _name, model, _fields in CARD_SECTIONS:
        out[key] = (model.query.filter_by(employee_id=emp.id)
                    .order_by(model.sort, model.id).all()) if emp else []
    return out


def _sync_education_from_onboarding(emp, raw_json):
    """入册时把入职登记里个人填报的「教育经历」JSON 转成 HrEducation 记录，
    这样新人入册后人事卡片就直接有教育经历，不用 HR 再手录一遍。"""
    if not raw_json:
        return 0
    try:
        rows = json.loads(raw_json)
    except ValueError:
        return 0
    if not isinstance(rows, list):
        return 0
    n = 0
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        # 个人填报表里「学历」「学位」是分开的，卡片上合成一栏「学历 / 学位」
        edu = (r.get('edu') or '').strip()
        degree = (r.get('degree') or '').strip()
        edu_degree = ('%s / %s' % (edu, degree)) if (edu and degree) else (edu or degree)
        db.session.add(HrEducation(
            employee_id=emp.id, start_time=(r.get('start') or '').strip() or None,
            end_time=(r.get('end') or '').strip() or None,
            school=(r.get('school') or '').strip() or None,
            major=(r.get('major') or '').strip() or None,
            edu_degree=edu_degree or None,
            edu_type=(r.get('edu_type') or '').strip() or None, sort=i))
        n += 1
    return n


@bp.route('/hr/employees/<int:eid>/card-edit', methods=['GET'])
@login_required
@permission_required('hr.employee.manage')
def card_edit(eid):
    """人事卡片录入 / 编辑页（二期）：分板块增删改员工的教育经历/家庭信息/培训经历/
    专业技术职务/职业资格/奖励/惩处记录。"""
    emp = Employee.query.get_or_404(eid)
    return render_template('hr/card_edit.html', emp=emp,
                           sections=CARD_SECTIONS, items=_card_items(emp))


@bp.route('/hr/employees/<int:eid>/card-item/<section>', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def card_item_save(eid, section):
    """新增 / 保存一条子集记录：带 item_id 为修改，否则新增。"""
    emp = Employee.query.get_or_404(eid)
    sec = CARD_SECTION_MAP.get(section)
    if not sec:
        flash('未知的档案板块')
        return redirect(url_for('hr.card_edit', eid=eid))
    _key, name, model, fields = sec
    item_id = request.form.get('item_id', type=int)
    if item_id:
        obj = model.query.get(item_id)
        if not obj or obj.employee_id != emp.id:
            flash('记录不存在')
            return redirect(url_for('hr.card_edit', eid=eid))
    else:
        obj = model(employee_id=emp.id)
    for fname, _label in fields:
        setattr(obj, fname, (request.form.get(fname) or '').strip() or None)
    if not item_id:
        db.session.add(obj)
    db.session.commit()
    flash('%s 已保存' % name)
    return redirect(url_for('hr.card_edit', eid=eid) + '#' + section)


@bp.route('/hr/employees/<int:eid>/card-item/<section>/<int:item_id>/delete', methods=['POST'])
@login_required
@permission_required('hr.employee.manage')
def card_item_delete(eid, section, item_id):
    """删除一条子集记录。"""
    sec = CARD_SECTION_MAP.get(section)
    if not sec:
        flash('未知的档案板块')
        return redirect(url_for('hr.card_edit', eid=eid))
    _key, name, model, _fields = sec
    obj = model.query.get(item_id)
    if not obj or obj.employee_id != eid:
        flash('记录不存在')
        return redirect(url_for('hr.card_edit', eid=eid))
    db.session.delete(obj)
    db.session.commit()
    flash('%s 记录已删除' % name)
    return redirect(url_for('hr.card_edit', eid=eid) + '#' + section)


# ===================== 人事 · 服务中心（三大服务分组导航） =====================
# 把用户要求的"管理服务 / 员工服务 / 考勤服务"三大类做成统一入口。
# done=True 的已落地（点击进真实页面），否则进入"规划中"占位页（不提前开发）。

# key -> 名称：用于占位页查名；同时作为三大分组里各服务的唯一标识
SERVICE_NAMES = {
    'org': '组织架构', 'workforce': '人力分析', 'team-att': '团队考勤',
    'perf-calib': '绩效校准', 'dept-perf': '部门绩效结果', 'team-perf': '团队绩效',
    'perf-fill': '绩效业务数据填报', 'q360': '360测评问卷',
    'my-profile': '我的资料', 'my-payslip': '我的工资条', 'my-learn': '我的学习',
    'my-template': '我的模板', 'my-perf': '我的绩效', 'my-assess': '我的测评',
    'cert-work': '在职证明', 'cert-income': '收入证明', 'resign': '离职申请',
    'resign-card': '补签申请', 'trip': '出差申请', 'overtime': '加班申请',
    'att-exception': '考勤异常中心', 'att-confirm': '考勤确认', 'my-field': '我的外勤',
}

# 三大分组：管理服务（HR 管理员）/ 员工服务（全员自助）/ 考勤服务（全员自助）
# 每个分组与板块均带 en（英文简称），前端以"英文简称 · 中文"呈现（便于国际化与快速识别）
HR_GROUPS = [
    {'key': 'manage', 'en': 'HR-Admin', 'title': '管理服务',
     'desc': '面向人事管理员：组织、绩效与人力相关配置', 'items': [
        {'key': 'org', 'en': 'Org', 'name': '组织架构', 'url': 'hr.organization', 'done': True},
        {'key': 'leave-type', 'en': 'LeaveCfg', 'name': '假期设置', 'url': 'hr.leave_types', 'done': True},
        {'key': 'holiday', 'en': 'Holiday', 'name': '法定节假日', 'url': 'hr.holidays', 'done': True},
        {'key': 'att-rule', 'en': 'AttRule', 'name': '日常考勤规则', 'url': 'hr.attendance_rules', 'done': True},
        {'key': 'emp', 'en': 'Emp', 'name': '员工花名册', 'url': 'hr.employees', 'done': True},
        {'key': 'salary', 'en': 'Payroll', 'name': '工资清单', 'url': 'hr.salaries', 'done': True},
        {'key': 'contract', 'en': 'Contract', 'name': '合同档案', 'url': 'hr.contracts', 'done': True},
        {'key': 'salary-field', 'en': 'PayItem', 'name': '工资自定义字段', 'url': 'hr.salary_fields', 'done': True},
        {'key': 'workforce', 'en': 'WFA', 'name': '人力分析', 'done': False},
        {'key': 'team-att', 'en': 'TeamAtt', 'name': '团队考勤', 'done': False},
        {'key': 'perf-calib', 'en': 'PerfCal', 'name': '绩效校准', 'done': False},
        {'key': 'dept-perf', 'en': 'DeptPerf', 'name': '部门绩效结果', 'done': False},
        {'key': 'team-perf', 'en': 'TeamPerf', 'name': '团队绩效', 'done': False},
        {'key': 'perf-fill', 'en': 'PerfData', 'name': '绩效业务数据填报', 'done': False},
        {'key': 'q360', 'en': '360', 'name': '360测评问卷', 'done': False},
    ]},
    {'key': 'employee', 'en': 'ESS', 'title': '员工服务',
     'desc': '面向全体员工自助（Employee Self-Service）', 'items': [
        {'key': 'my-profile', 'en': 'MyProfile', 'name': '我的资料', 'url': 'hr.my_profile', 'done': True},
        {'key': 'my-payslip', 'en': 'MyPay', 'name': '我的工资条', 'done': False},
        {'key': 'my-learn', 'en': 'MyLearn', 'name': '我的学习', 'done': False},
        {'key': 'my-template', 'en': 'MyTpl', 'name': '我的模板', 'done': False},
        {'key': 'my-perf', 'en': 'MyPerf', 'name': '我的绩效', 'done': False},
        {'key': 'my-assess', 'en': 'MyAssess', 'name': '我的测评', 'done': False},
        {'key': 'cert-work', 'en': 'CertW', 'name': '在职证明', 'done': False},
        {'key': 'cert-income', 'en': 'CertI', 'name': '收入证明', 'done': False},
        {'key': 'resign', 'en': 'Resign', 'name': '离职申请', 'done': False},
    ]},
    {'key': 'attendance', 'en': 'ATT', 'title': '考勤服务',
     'desc': '面向全体员工自助：补签 / 休假 / 出差 / 加班等', 'items': [
        {'key': 'leave-apply', 'en': 'LeaveReq', 'name': '休假申请', 'url': 'hr.leave_apply', 'done': True},
        {'key': 'resign-card', 'en': 'ReSign', 'name': '补签申请', 'done': False},
        {'key': 'trip', 'en': 'Trip', 'name': '出差申请', 'done': False},
        {'key': 'overtime', 'en': 'OT', 'name': '加班申请', 'done': False},
        {'key': 'att-exception', 'en': 'AttEx', 'name': '考勤异常中心', 'done': False},
        {'key': 'att-confirm', 'en': 'AttConf', 'name': '考勤确认', 'done': False},
        {'key': 'my-field', 'en': 'MyField', 'name': '我的外勤', 'done': False},
    ]},
]


@bp.route('/hr/')
@login_required
def hr_home():
    """EHR 系统：三大服务分组导航（英文简称 · 中文）"""
    # 待完善个人电子信息：登录用户对应到本人 Employee，need_profile=True 时首页弹横幅推送
    me = Employee.query.filter_by(employee_no=session.get('username')).first()
    profile_todo = bool(me and me.need_profile)
    return render_template('hr/hub.html', groups=HR_GROUPS, profile_todo=profile_todo)


@bp.route('/hr/service/<key>')
@login_required
def hr_service_stub(key):
    """规划中占位页：展示待开发的二级服务名称（不提前开发，仅占位）"""
    name = SERVICE_NAMES.get(key, key)
    return render_template('hr/service_stub.html', name=name)


# ===================== 人事 · 组织架构管理（部门设置 + 人员部门归属） =====================

def _parse_headcount(raw):
    """编制人数解析：空 / 非法 / 负数一律归 0（0 表示未设编制，不参与超编判断）。"""
    try:
        n = int(float(raw or 0))
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else 0


@bp.route('/hr/organization', methods=['GET'])
@login_required
@permission_required('hr.org.manage')  # 仅人事管理员可管理
def organization():
    """组织架构管理：部门树 + 人员部门归属设置"""
    orgs = Organization.query.order_by(Organization.id).all()
    employees = Employee.query.order_by(Employee.id).all()
    # 计算每层组织深度，用于缩进显示（自关联 parent_id）
    org_by_id = {o.id: o for o in orgs}

    def depth(o):
        d = 0
        cur = o
        while cur.parent_id:
            cur = org_by_id.get(cur.parent_id)
            if not cur:
                break
            d += 1
        return d

    return render_template('hr/organization.html', orgs=orgs, employees=employees, depth=depth)


@bp.route('/hr/organization/new', methods=['POST'])
@login_required
@permission_required('hr.org.manage')
def organization_new():
    """新增部门：挂在某父级（公司/团队）之下，构成树"""
    name = request.form.get('name', '').strip()
    if not name:
        flash('部门名称不能为空')
        return redirect(url_for('hr.organization'))
    parent_id = request.form.get('parent_id') or None
    o = Organization(name=name, parent_id=int(parent_id) if parent_id else None,
                      type=request.form.get('type', 'team'),
                      headcount=_parse_headcount(request.form.get('headcount')))
    db.session.add(o)
    db.session.commit()
    flash('部门已添加')
    return redirect(url_for('hr.organization'))


@bp.route('/hr/organization/<int:oid>/edit', methods=['POST'])
@login_required
@permission_required('hr.org.manage')
def organization_update(oid):
    """编辑部门：名称 / 父级 / 类型 / 编制人数"""
    o = Organization.query.get_or_404(oid)
    o.name = request.form.get('name', '').strip() or o.name
    parent_id = request.form.get('parent_id') or None
    o.parent_id = int(parent_id) if parent_id else None
    o.type = request.form.get('type', o.type)
    o.headcount = _parse_headcount(request.form.get('headcount'))
    db.session.commit()
    flash('部门已更新')
    return redirect(url_for('hr.organization'))


@bp.route('/hr/organization/<int:oid>/delete', methods=['POST'])
@login_required
@permission_required('hr.org.manage')
def organization_delete(oid):
    """删除部门：公司/部门均可删。
    删除前把直接下级部门上提一级（挂到被删部门的上级，无上级则成为顶级），
    并把该部门下的员工置为未设部门，避免悬空。"""
    o = Organization.query.get_or_404(oid)
    # 直接下级上提一级
    for child in Organization.query.filter_by(parent_id=oid).all():
        child.parent_id = o.parent_id
    # 该部门下的员工置为未设部门
    Employee.query.filter_by(org_id=oid).update({'org_id': None})
    db.session.delete(o)
    db.session.commit()
    flash('部门已删除（下级已上提一级，原成员置为未设部门）')
    return redirect(url_for('hr.organization'))


@bp.route('/hr/employees/<int:eid>/org', methods=['POST'])
@login_required
@permission_required('hr.org.manage')
def employee_set_org(eid):
    """人员部门归属设置：把员工划归某部门（含"未设"）"""
    emp = Employee.query.get_or_404(eid)
    org_id = request.form.get('org_id') or None
    emp.org_id = int(org_id) if org_id else None
    db.session.commit()
    flash('已更新 {} 的部门归属'.format(emp.name))
    return redirect(url_for('hr.organization'))


# ===================== 人事 · 假期类型与额度设置（人事管理员后台） =====================

def _parse_float(raw, default=0.0):
    """宽松解析浮点数（'3'、'3.5'、'' → default），后端算额度用。"""
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# —— 工作日计算（2026-10-01 加）：休假申请「先选起止日期 → 自动算时长」用 ——
# 口径：① 先看考勤规则的工作日位串（周一到周日，1 上班 0 休息）；
#       ② 法定节假日里 day_type=rest 的排除、day_type=work（调休补班）的计入；
#       ③ 含首尾两天。跨周末/法定假自动剔除，与员工实际请假口径一致。
DAILY_HOURS = 8.0  # 标准工作日工时（事假按小时折算：1 个工作日 = 8 小时）


def _workday_flags():
    """取全局考勤规则的工作日位串（默认 '1111100'=周一至周五上班）。"""
    rule = AttendanceRule.query.order_by(AttendanceRule.id).first()
    wd = (rule.workdays if rule and rule.workdays else '1111100')
    return (wd + '1111100')[:7]


def _holiday_map():
    """{日期字符串: 'rest'|'work'} —— 法定节假日/调休补班日；rest=放假、work=补班。"""
    out = {}
    for h in Holiday.query.all():
        if h.holiday_date:
            out[h.holiday_date.strftime('%Y-%m-%d')] = h.day_type or 'rest'
    return out


def _work_days(start_s, end_s, workdays=None, holis=None):
    """算 [start, end] 区间内的「工作日天数」与「日历天数」（含首尾）。

    返回 (工作日天数, 日历天数)；日期缺失/非法/倒序 → (0.0, 0.0)。
    workdays/holis 可外部传入（避免循环内反复查库）；不传则各自查一次。
    """
    d1 = _parse_date(start_s)
    d2 = _parse_date(end_s)
    if not d1 or not d2 or d2 < d1:
        return 0.0, 0.0
    if workdays is None:
        workdays = _workday_flags()
    if holis is None:
        holis = _holiday_map()
    n, cal, cur, one = 0, 0, d1, timedelta(days=1)
    while cur <= d2:
        cal += 1
        dtype = holis.get(cur.strftime('%Y-%m-%d'))
        if dtype == 'work':
            n += 1                                   # 调休补班 → 算工作日
        elif dtype == 'rest':
            pass                                     # 法定放假 → 不算
        elif workdays[cur.weekday()] != '0':
            n += 1                                   # 规则里是上班日
        cur += one
    return float(n), float(cal)


def _emp_seniority_years(emp, on_date=None):
    """员工的「累计工作年限」（整年，法定年假按此落档）。

    口径：累计工作年限 = 从「参加工作日期(work_start_date)」算起（跨单位累计，法定口径）；
          未登记则回退「本单位入职日期(hire_date)」；两者都没有 → None（无法判定）。
    例：2007-07-01 参加工作，算到 2026-12-31 = 19 年。
    """
    if not emp:
        return None
    base = emp.work_start_date or emp.hire_date
    if not base:
        return None
    return _years_between(base, on_date)


def _leave_quota(lt, emp, year):
    """算某个假期类型对某员工的年度额度。

    返回 dict：
      quota     —— 年度可休天数；None 表示不限额度
      mode      —— 'tier' 按工龄分级 / 'fixed' 固定天数 / 'free' 不限额度
      seniority —— 员工累计工作年限（int 或 None）
      tier      —— 命中的档位 LeaveTier（仅 mode='tier'）
      prorate   —— 本次是否做了当年新入职折算（bool）
      note      —— 给界面看的一句话说明
    """
    tiers = list(lt.tiers or [])
    unit = (lt.unit or 'day')
    uname = '小时' if unit == 'hour' else '天'
    today = date.today()
    # 算工龄的基准日：查的是今年就按今天算，往年就按那年年末算
    base_date = today if year == today.year else date(year, 12, 31)
    sen = _emp_seniority_years(emp, base_date)

    if not tiers:
        # 固定天数模式：total_days=0 视为不限额度
        if not lt.total_days or lt.total_days <= 0:
            return {'quota': None, 'mode': 'free', 'seniority': sen, 'unit': unit,
                    'tier': None, 'prorate': False, 'note': '不限额度'}
        return {'quota': float(lt.total_days), 'mode': 'fixed', 'seniority': sen, 'unit': unit,
                'tier': None, 'prorate': False, 'note': '固定 %g %s/年' % (lt.total_days, uname)}

    # 分级模式：按工龄落档（档位按 min_years 升序）
    tiers.sort(key=lambda t: (t.sort or 0, t.min_years or 0))
    hit = None
    for t in tiers:
        lo = t.min_years or 0
        hi = t.max_years or 0
        if sen is None:
            continue
        if sen >= lo and (hi <= 0 or sen < hi):
            hit = t
            break
    if hit is None:
        # 没登记工作日期、或工龄落在档位缝隙 → 用最低档兜底，避免员工完全请不了假
        hit = tiers[0]
    days = float(hit.days or 0)

    # 当年新入职折算：法定口径（实施办法第五条）——剩余日历天数/365 × 全年天数，不足 1 整天不计
    prorated = False
    if (lt.prorate or '0') == '1' and emp and emp.hire_date and emp.hire_date.year == year:
        remain = (date(year, 12, 31) - emp.hire_date).days + 1
        ratio = max(remain, 0) / 365.0
        days = float(int(days * ratio))  # 不足 1 整天不享受
        prorated = True

    if sen is None:
        note = '未登记参加工作/入职日期，暂按最低档 %g 天' % days
    else:
        note = '累计工作年限 %d 年 → %g 天/年' % (sen, float(hit.days or 0))
    if prorated:
        note += '（当年入职已按剩余天数折算为 %g 天）' % days
    return {'quota': days, 'mode': 'tier', 'seniority': sen, 'unit': unit,
            'tier': hit, 'prorate': prorated, 'note': note}


@bp.route('/hr/leave-types', methods=['GET'])
@login_required
@permission_required('hr.leave.manage')  # 人事管理员权限
def leave_types():
    """假期类型与额度：列出后台配置的假期、年度可请天数与工龄分级档位"""
    items = LeaveType.query.order_by(LeaveType.id).all()
    return render_template('hr/leave_types.html', items=items)


@bp.route('/hr/leave-types/new', methods=['POST'])
@login_required
@permission_required('hr.leave.manage')
def leave_type_new():
    """新增假期类型"""
    code = request.form.get('code', '').strip()
    name = request.form.get('name', '').strip()
    if not code or not name:
        flash('编码和名称不能为空')
        return redirect(url_for('hr.leave_types'))
    if LeaveType.query.filter_by(code=code).first():
        flash('该假期编码已存在')
        return redirect(url_for('hr.leave_types'))
    days = _parse_float(request.form.get('total_days', '0'))
    unit = request.form.get('unit', 'day')
    if unit not in ('day', 'hour'):
        unit = 'day'
    lt = LeaveType(code=code, name=name, total_days=days, unit=unit,
                   need_attach='1' if request.form.get('need_attach') else '0',
                   attach_hint=(request.form.get('attach_hint', '') or '').strip(),
                   prorate='1' if request.form.get('prorate') else '0',
                   description=request.form.get('description', '').strip())
    db.session.add(lt)
    db.session.commit()
    flash('假期类型已添加')
    return redirect(url_for('hr.leave_types'))


@bp.route('/hr/leave-types/<int:lid>/edit', methods=['POST'])
@login_required
@permission_required('hr.leave.manage')
def leave_type_update(lid):
    """编辑假期类型：名称 / 额度 / 单位 / 附件要求 / 当年是否折算 / 说明"""
    lt = LeaveType.query.get_or_404(lid)
    lt.name = request.form.get('name', '').strip() or lt.name
    days = request.form.get('total_days', '').strip()
    if days:
        lt.total_days = _parse_float(days, lt.total_days)
    unit = request.form.get('unit')
    if unit in ('day', 'hour'):
        lt.unit = unit
    lt.need_attach = '1' if request.form.get('need_attach') else '0'
    lt.attach_hint = (request.form.get('attach_hint', '') or '').strip()
    lt.prorate = '1' if request.form.get('prorate') else '0'
    lt.description = request.form.get('description', '').strip()
    db.session.commit()
    flash('假期类型已更新')
    return redirect(url_for('hr.leave_types'))


@bp.route('/hr/leave-types/<int:lid>/delete', methods=['POST'])
@login_required
@permission_required('hr.leave.manage')
def leave_type_delete(lid):
    """删除假期类型"""
    lt = LeaveType.query.get_or_404(lid)
    db.session.delete(lt)
    db.session.commit()
    flash('假期类型已删除')
    return redirect(url_for('hr.leave_types'))


@bp.route('/hr/leave-types/<int:lid>/tiers/new', methods=['POST'])
@login_required
@permission_required('hr.leave.manage')
def leave_tier_new(lid):
    """给假期类型加一条「工龄档位」：累计工作年限 [min, max) 年 → 可休 X 天。
    max 留空或填 0 = 最后一档（不设上限）。加了档位后，该假期就按工龄算，不再用固定天数。"""
    lt = LeaveType.query.get_or_404(lid)
    lo = _parse_float(request.form.get('min_years', ''))
    hi = _parse_float(request.form.get('max_years', ''))
    days = _parse_float(request.form.get('days', ''))
    if days <= 0:
        flash('档位天数必须大于 0')
        return redirect(url_for('hr.leave_types'))
    if hi and hi <= lo:
        flash('年限上限必须大于下限（留空表示不限）')
        return redirect(url_for('hr.leave_types'))
    sort = len(lt.tiers or [])
    db.session.add(LeaveTier(leave_type_id=lt.id, min_years=lo, max_years=hi,
                             days=days, sort=sort))
    db.session.commit()
    flash('已添加工龄档位：满 %g 年 → %g 天/年' % (lo, days))
    return redirect(url_for('hr.leave_types'))


@bp.route('/hr/leave-tiers/<int:tid>/delete', methods=['POST'])
@login_required
@permission_required('hr.leave.manage')
def leave_tier_delete(tid):
    """删除一条工龄档位（档位全删光后，该假期回到「固定天数」模式）"""
    t = LeaveTier.query.get_or_404(tid)
    lt_id = t.leave_type_id
    db.session.delete(t)
    db.session.commit()
    # 删完顺手重排序号，保证界面顺序稳定
    lt = LeaveType.query.get(lt_id)
    if lt:
        for i, x in enumerate(sorted(lt.tiers, key=lambda z: (z.min_years or 0))):
            x.sort = i
        db.session.commit()
    flash('工龄档位已删除')
    return redirect(url_for('hr.leave_types'))


# ===================== 人事 · 考勤服务 · 休假申请（员工自助，复用审批流） =====================

def _next_leave_code(prefix='QJ'):
    """生成当月请假单号：QJ-YYYYMM-NNN（与 OA 统一申请中心同一套编号口径）。
    放在 hr.py 里而不是复用 oa._next_expense_code，是为了避免 hr ↔ oa 模块互相 import。"""
    ym = datetime.now().strftime('%Y%m')
    last = (Approval.query.filter(Approval.code.like('%s-%s-%%' % (prefix, ym)))
            .order_by(Approval.code.desc()).first())
    seq = 1
    if last and last.code:
        try:
            seq = int(last.code.rsplit('-', 1)[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return '%s-%s-%03d' % (prefix, ym, seq)


def _leave_used(my_leaves, lt, year):
    """某人某假期类型本年度的「已用量」= 已通过 + 审批中（待审批也占额度，防重复超请）。
    单位随假期类型：天单位解析 content 的「天数：X」，小时单位（事假）解析「时长：X」。
    返回 (已通过量, 审批中量)，单位与该假期一致。"""
    unit = (getattr(lt, 'unit', None) or 'day')
    name = lt.name
    pat = (r'时长[:：]\s*([0-9]+(?:\.[0-9]+)?)' if unit == 'hour'
           else r'天数[:：]\s*([0-9]+(?:\.[0-9]+)?)')
    approved = pending = 0.0
    for a in my_leaves:
        if a.status not in ('approved', 'pending'):
            continue
        if not a.created_at or a.created_at.year != year:
            continue
        if not (a.title or '').startswith(name + '申请'):
            continue
        m = re.search(pat, a.content or '')
        if not m:
            continue
        v = float(m.group(1))
        if a.status == 'approved':
            approved += v
        else:
            pending += v
    return approved, pending


def _fmt_qty(v, unit):
    """格式化时长：天单位 → '3 天'；小时单位 → '4 小时'（去掉多余小数）。"""
    return '%g %s' % (v, '小时' if (unit or 'day') == 'hour' else '天')


# ===================== 人事 · 休假申请附件（病例 / 结婚证 / 生育证明）=====================
# 复用 Approval.attach_files（JSON 数组），落盘 static/uploads/leave/，页面走鉴权下载不暴露静态目录。
LEAVE_ATTACH_EXT = {'pdf', 'ofd', 'xml', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp',
                    'doc', 'docx', 'xls', 'xlsx', 'zip'}
LEAVE_ATTACH_MAX = 10 * 1024 * 1024  # 单文件 10MB


def _leave_attach_dir():
    """休假附件目录（不存在则建）。"""
    d = os.path.join(current_app.config['UPLOAD_DIR'], 'leave')
    os.makedirs(d, exist_ok=True)
    return d


def _save_leave_files(files, code):
    """保存休假附件，返回 (已存列表, 错误说明列表)。
    已存项形如 {'name': 原文件名, 'file': 落盘文件名, 'size': 字节数}。"""
    saved, errs = [], []
    for fs in (files or []):
        if not fs or not (fs.filename or '').strip():
            continue
        fn = fs.filename.strip()
        ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
        if ext not in LEAVE_ATTACH_EXT:
            errs.append('%s：不支持的格式' % fn)
            continue
        data = fs.read()
        if len(data) > LEAVE_ATTACH_MAX:
            errs.append('%s：超过 10MB' % fn)
            continue
        disk = '%s_%s.%s' % ((code or 'QJ').replace('-', ''), uuid.uuid4().hex[:8], ext)
        with open(os.path.join(_leave_attach_dir(), disk), 'wb') as f:
            f.write(data)
        saved.append({'name': fn, 'file': disk, 'size': len(data)})
    return saved, errs


def _leave_attach_list(a):
    """读某审批单的附件列表（容错：非 JSON/异常 → 空列表）。"""
    try:
        v = json.loads(a.attach_files or '[]')
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _set_leave_attach(a, lst):
    """写回附件列表。"""
    a.attach_files = json.dumps(lst, ensure_ascii=False)


def _can_view_leave(a):
    """能否查看某休假单附件：本人 + 有 hr.leave.manage / approval.approve 的人。"""
    uid = session.get('user_id')
    if a.applicant_id == uid:
        return True
    user = User.query.get(uid) if uid else None
    if user and user.level:
        codes = {p.code for p in user.level.permissions}
        if 'hr.leave.manage' in codes or 'approval.approve' in codes:
            return True
    return False


@bp.route('/hr/leave/attach/<int:aid>/<int:idx>')
@login_required
def leave_attach(aid, idx):
    """下载休假附件（鉴权：仅本人或有审批/人事权限者）。"""
    a = Approval.query.get_or_404(aid)
    if not _can_view_leave(a):
        flash('无权查看该附件')
        return redirect(url_for('hr.leave_apply'))
    lst = _leave_attach_list(a)
    if idx < 0 or idx >= len(lst):
        flash('附件不存在')
        return redirect(url_for('hr.leave_apply'))
    it = lst[idx]
    return send_from_directory(_leave_attach_dir(), it['file'],
                               as_attachment=True, download_name=it.get('name') or it['file'])


@bp.route('/hr/leave/attach/<int:aid>/<int:idx>/delete', methods=['POST'])
@login_required
def leave_attach_delete(aid, idx):
    """删除休假附件（仅本人在「待审批」时可删，避免事后篡改已批单据）。"""
    a = Approval.query.get_or_404(aid)
    if a.applicant_id != session.get('user_id'):
        flash('只能删除本人在待审批状态下的附件')
        return redirect(url_for('hr.leave_apply'))
    if a.status != 'pending':
        flash('该申请已审批，附件不可删除')
        return redirect(url_for('hr.leave_apply'))
    if Approval.query.filter_by(id=aid).first() is None or idx < 0 or idx >= len(_leave_attach_list(a)):
        flash('附件不存在')
        return redirect(url_for('hr.leave_apply'))
    lst = _leave_attach_list(a)
    it = lst.pop(idx)
    _set_leave_attach(a, lst)
    db.session.commit()
    try:
        os.remove(os.path.join(_leave_attach_dir(), it['file']))
    except OSError:
        pass
    flash('附件已删除')
    return redirect(url_for('hr.leave_apply'))


@bp.route('/hr/leave/apply', methods=['GET', 'POST'])
@login_required
def leave_apply():
    """休假申请（2026-10-01 重做）。

    流程：① 先选假期类型 → ② 选开始/结束日期 → ③ 系统按工作日自动算时长（天或小时）
          → ④ 超出本人该假期额度即时提醒并拦截 → ⑤ 病假/婚假/产假等「须证明」假期必须传附件。
    口径：年假按「累计工作年限」分级（法定），其余按固定额度；事假以小时为单位（不足 1 小时按 1 小时）。
    """
    leave_types = LeaveType.query.order_by(LeaveType.id).all()
    my_leaves = (Approval.query.filter_by(applicant_id=session['user_id'], type='请假')
                 .order_by(Approval.created_at.desc()).all())
    # 本人员工档案（B 模式：登录名 == 工号）；admin 等无档案账号为 None，额度按最低档兜底
    emp = Employee.query.filter_by(employee_no=session.get('username')).first()

    year = date.today().year
    # 每种假期：本人额度 + 已用 + 审批中 + 剩余（事假等按「小时」，其余按「天」）
    quotas = {}
    for lt in leave_types:
        q = _leave_quota(lt, emp, year)
        used, pend = _leave_used(my_leaves, lt, year)
        q['used'], q['pending'] = used, pend
        q['remain'] = None if q['quota'] is None else max(q['quota'] - used - pend, 0.0)
        quotas[lt.name] = q

    if request.method == 'POST':
        lt_id = request.form.get('leave_type_id')
        lt = LeaveType.query.get(lt_id) if lt_id else None
        if not lt:
            flash('请选择假期类型')
            return redirect(url_for('hr.leave_apply'))
        unit = lt.unit or 'day'
        start = (request.form.get('start_date') or '').strip()
        end = (request.form.get('end_date') or '').strip()
        d1, d2 = _parse_date(start), _parse_date(end)
        if not d1 or not d2:
            flash('请选择开始日期和结束日期')
            return redirect(url_for('hr.leave_apply'))
        if d2 < d1:
            flash('结束日期不能早于开始日期')
            return redirect(url_for('hr.leave_apply'))
        # 时长：优先用前端自动算出的值；没给就后端按工作日重算一遍，保证两边口径一致
        qty = _parse_float(request.form.get('qty', ''), 0.0)
        if qty <= 0:
            wd, _cal = _work_days(start, end)
            qty = wd * DAILY_HOURS if unit == 'hour' else wd
        if qty <= 0:
            flash('起止区间内没有工作日，时长为 0，请检查日期')
            return redirect(url_for('hr.leave_apply'))
        if unit == 'hour':
            qty = float(math.ceil(qty))  # 事假以小时为单位，不足 1 小时按 1 小时
        reason = (request.form.get('reason') or '').strip()
        if not reason:
            flash('请填写请假事由')
            return redirect(url_for('hr.leave_apply'))
        # 必传附件校验（病假交病例 / 婚假交结婚证 / 产假交生育证明…）
        files = request.files.getlist('attach')
        has_file = any(f and (f.filename or '').strip() for f in files)
        if (lt.need_attach or '0') == '1' and not has_file:
            flash('「%s」必须上传%s后才能提交' % (lt.name, lt.attach_hint or '证明附件'))
            return redirect(url_for('hr.leave_apply'))
        # 额度校验：不限额度(None)放行；有限额则「本次 + 本年已用 + 审批中」不得超额度
        q = quotas.get(lt.name) or _leave_quota(lt, emp, year)
        if q['quota'] is not None:
            take = qty + (q.get('used') or 0) + (q.get('pending') or 0)
            if take > q['quota'] + 1e-9:
                flash('超出本人本年度%s额度：%s。本次 %s，最多还能请 %s'
                      % (lt.name, q['note'], _fmt_qty(qty, unit),
                         _fmt_qty(max(q['quota'] - (q.get('used') or 0)
                                      - (q.get('pending') or 0), 0), unit)))
                return redirect(url_for('hr.leave_apply'))
        code = _next_leave_code('QJ')
        saved, errs = _save_leave_files(files, code)
        if errs and (lt.need_attach or '0') == '1' and not saved:
            flash('附件上传失败：' + '；'.join(errs))
            return redirect(url_for('hr.leave_apply'))
        is_hour = (unit == 'hour')
        content = '假期类型：%s\n%s：%s\n起止：%s ~ %s\n事由：%s' % (
            lt.name, '时长' if is_hour else '天数', _fmt_qty(qty, unit), start, end, reason)
        appr = Approval(code=code, type='请假', title=lt.name + '申请', content=content,
                        applicant_id=session['user_id'],
                        start_time=start + ' 09:00', end_time=end + ' 18:00',
                        att_days=qty, sub_type=lt.name)
        if saved:
            _set_leave_attach(appr, saved)
        db.session.add(appr)
        db.session.commit()
        log_audit('HR-休假申请提交', target_type='approval', target_no=code,
                  detail='%s：%s' % (lt.name, _fmt_qty(qty, unit)))
        if errs:
            flash('部分附件未上传：' + '；'.join(errs))
        flash('休假申请已提交，等待审批')
        return redirect(url_for('hr.leave_apply'))

    # 供前端「选日期 → 自动算时长」：注入工作日位串 + 法定节假日 + 每日工时
    return render_template('hr/leave_apply.html',
                           leave_types=leave_types, my_leaves=my_leaves,
                           quotas=quotas, emp=emp, year=year,
                           seniority=_emp_seniority_years(emp),
                           workdays=_workday_flags(), holidays=_holiday_map(),
                           daily_hours=DAILY_HOURS,
                           attach_list={a.id: _leave_attach_list(a) for a in my_leaves})


# ===================== 人事 · 法定节假日管理（人事管理员每年年初导入国务院数据） =====================

@bp.route('/hr/holidays', methods=['GET'])
@login_required
@permission_required('hr.holiday.manage')  # 人事管理员权限
def holidays():
    """法定节假日：列出已导入的节假日（按年份倒序），供人事管理员维护"""
    items = Holiday.query.order_by(Holiday.holiday_date.desc()).all()
    # 按年份分组，方便展示"今年/往年"
    years = sorted({h.year for h in items if h.year}, reverse=True)
    return render_template('hr/holidays.html', items=items, years=years)


@bp.route('/hr/holidays/import', methods=['POST'])
@login_required
@permission_required('hr.holiday.manage')
def holiday_import():
    """导入法定节假日：CSV 列 = 日期,名称,类型,备注（首行可为表头）。
    类型：rest=放假（法定休）/ work=调休补班（周末上班），空则默认 rest。
    按日期去重：同一天重复导入时覆盖（方便年度更新）。
    兼容国务院办公厅发布的放假通知整理出的 CSV。"""
    file = request.files.get('file')
    if not file or file.filename == '':
        flash('请选择要导入的 CSV 文件')
        return redirect(url_for('hr.holidays'))
    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'gbk', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        flash('文件编码无法识别，请保存为 UTF-8 或 GBK 的 CSV')
        return redirect(url_for('hr.holidays'))

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        flash('文件为空')
        return redirect(url_for('hr.holidays'))
    # 跳过表头（首格非数字即视为表头）
    start = 0
    head = (rows[0][0] or '').strip() if rows[0] else ''
    if head and not head.replace('-', '').replace('/', '').replace('.', '').isdigit():
        start = 1

    imported = 0
    errors = []
    for r in rows[start:]:
        if not r or len(r) < 1:
            continue
        date_s = (r[0] or '').strip()
        if not date_s:
            continue
        # 解析日期（支持 2026-10-01 / 2026/10/01 / 2026.10.01）
        d = None
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
            try:
                d = datetime.strptime(date_s, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            errors.append('日期 %s 无法解析，已跳过' % date_s)
            continue
        name = (r[1] or '').strip() if len(r) > 1 else ''
        type_cn = (r[2] or '').strip() if len(r) > 2 else ''
        note = (r[3] or '').strip() if len(r) > 3 else ''
        # 类型中文 → 编码：放假/休/法定 → rest；补班/班/调休 → work
        if type_cn in ('补班', '班', '调休', 'work'):
            day_type = 'work'
        else:
            day_type = 'rest'
        # 按日期去重覆盖
        h = Holiday.query.filter_by(holiday_date=d).first()
        if h is None:
            h = Holiday(holiday_date=d)
            db.session.add(h)
        h.name = name or h.name
        h.day_type = day_type
        h.year = d.year
        h.note = note or h.note
        imported += 1
    db.session.commit()
    flash('成功导入 %d 条法定节假日' % imported)
    for e in errors[:5]:
        flash(e)
    return redirect(url_for('hr.holidays'))


@bp.route('/hr/holidays/new', methods=['POST'])
@login_required
@permission_required('hr.holiday.manage')
def holiday_new():
    """手动新增一条节假日（补漏用）"""
    date_s = request.form.get('holiday_date', '').strip()
    name = request.form.get('name', '').strip()
    day_type = request.form.get('day_type', 'rest')
    if not date_s or not name:
        flash('日期和名称不能为空')
        return redirect(url_for('hr.holidays'))
    d = _parse_date(date_s)
    if not d:
        flash('日期格式无效')
        return redirect(url_for('hr.holidays'))
    if Holiday.query.filter_by(holiday_date=d).first():
        flash('该日期已存在节假日')
        return redirect(url_for('hr.holidays'))
    h = Holiday(holiday_date=d, name=name, day_type=day_type, year=d.year,
                note=request.form.get('note', '').strip())
    db.session.add(h)
    db.session.commit()
    flash('节假日已添加')
    return redirect(url_for('hr.holidays'))


@bp.route('/hr/holidays/<int:hid>/delete', methods=['POST'])
@login_required
@permission_required('hr.holiday.manage')
def holiday_delete(hid):
    """删除一条节假日"""
    h = Holiday.query.get_or_404(hid)
    db.session.delete(h)
    db.session.commit()
    flash('节假日已删除')
    return redirect(url_for('hr.holidays'))


# ===================== 人事 · 日常考勤规则设置（上下班时间/打卡规则） =====================

@bp.route('/hr/attendance-rules', methods=['GET'])
@login_required
@permission_required('hr.attrule.manage')  # 人事管理员权限
def attendance_rules():
    """日常考勤规则：全局单条规则，配置上下班时间与打卡判定"""
    rule = AttendanceRule.query.filter_by(code='global').first()
    return render_template('hr/attendance_rules.html', rule=rule)


@bp.route('/hr/attendance-rules/save', methods=['POST'])
@login_required
@permission_required('hr.attrule.manage')
def attendance_rule_save():
    """保存考勤规则（无则建 global 一条，有则更新）"""
    rule = AttendanceRule.query.filter_by(code='global').first()
    if rule is None:
        rule = AttendanceRule(code='global')
        db.session.add(rule)
    rule.work_start = request.form.get('work_start', '09:00').strip() or '09:00'
    rule.work_end = request.form.get('work_end', '18:00').strip() or '18:00'

    def _int(name, default=0):
        try:
            return int(request.form.get(name, str(default)).strip() or default)
        except ValueError:
            return default

    rule.late_after = _int('late_after', 0)
    rule.early_before = _int('early_before', 0)
    rule.need_check_in = request.form.get('need_check_in') == 'on'
    rule.need_check_out = request.form.get('need_check_out') == 'on'
    # 工作日：周一~周日 7 个复选框，勾选=上班(1)
    wd = ''
    for i in range(7):
        wd += '1' if request.form.get('wd_%d' % i) == 'on' else '0'
    rule.workdays = wd
    rule.note = request.form.get('note', '').strip()
    db.session.commit()
    flash('考勤规则已保存')
    return redirect(url_for('hr.attendance_rules'))
