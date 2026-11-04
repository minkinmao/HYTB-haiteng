# oa.py
# OA 办公系统模块（用 Blueprint 与 app.py 解耦：以后加审批、待办都放这里）
import csv
import io
import json
import math
import os
import re
import uuid
from flask import (Blueprint, render_template, request, redirect, url_for, session, flash,
                   send_from_directory, current_app)
from datetime import datetime, date
from models import (db, Announcement, Approval, User, Employee, AttendanceRecord,
                   ApprovalFlow, ApprovalFlowStep, Mail,
                   ExpenseClaim, ExpenseReimburse, ExpenseReimburseItem,
                   LeaveType, EXPENSE_CATEGORIES, EXPENSE_CATEGORY_GROUPS)
from app import login_required, permission_required, log_audit

# Blueprint 名字叫 'oa'，模板默认就在项目 templates/ 下
bp = Blueprint('oa', __name__)


@bp.route('/oa/announcements', methods=['GET'])
@login_required
def announcements():
    """公告列表：置顶的排前面，再按发布时间倒序。
    所有登录用户可看；发布/删除需 oa.announce.manage 权限（行政负责人）。"""
    items = Announcement.query.order_by(
        Announcement.is_pinned.desc(),
        Announcement.created_at.desc()
    ).all()
    # 当前用户是否有公告管理权限（决定是否显示发布表单/删除按钮）
    user = User.query.get(session['user_id'])
    can_manage = bool(user and user.level and
                      any(p.code == 'oa.announce.manage' for p in user.level.permissions))
    return render_template('oa/announcement_list.html', items=items, can_manage=can_manage)


@bp.route('/oa/announcements/new', methods=['POST'])
@login_required
@permission_required('oa.announce.manage')
def announcement_new():
    """发布新公告：取当前登录用户作为发布人；置顶由表单复选框控制；scope 区分内部/官网"""
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    # 复选框只有勾选时才会上传 'on'，未勾选则取不到 -> 用 == 'on' 判定
    is_pinned = request.form.get('is_pinned') == 'on'
    scope = request.form.get('scope', 'internal').strip()
    if scope not in ('internal', 'website'):
        scope = 'internal'
    if not title:
        flash('标题不能为空')
        return redirect(url_for('oa.announcements'))
    ann = Announcement(title=title, content=content,
                       is_pinned=is_pinned, scope=scope,
                       publisher_id=session['user_id'])
    db.session.add(ann)
    db.session.commit()
    flash('公告已发布')
    return redirect(url_for('oa.announcements'))


@bp.route('/oa/announcements/<int:ann_id>/delete', methods=['POST'])
@login_required
@permission_required('oa.announce.manage')
def announcement_delete(ann_id):
    """删除公告：先查到再删，查不到返回 404"""
    ann = Announcement.query.get_or_404(ann_id)
    db.session.delete(ann)
    db.session.commit()
    flash('公告已删除')
    return redirect(url_for('oa.announcements'))


# ===================== OA · 审批模块 =====================

@bp.route('/oa/approvals', methods=['GET'])
@login_required
def approvals():
    """审批列表：按提交时间倒序（先易后难，暂不分角色，后续可加审批权限）"""
    items = Approval.query.order_by(Approval.created_at.desc()).all()
    return render_template('oa/approval_list.html', items=items)


@bp.route('/oa/approvals/new', methods=['POST'])
@login_required
def approval_new():
    """提交申请：申请人=当前登录用户，状态默认为待审批"""
    type_ = request.form.get('type', '通用').strip()
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    if not title:
        flash('标题不能为空')
        return redirect(url_for('oa.approvals'))
    appr = Approval(code=_next_expense_code(Approval, 'SP'),
                    type=type_, title=title, content=content,
                    applicant_id=session['user_id'])
    db.session.add(appr)
    db.session.commit()
    flash('申请已提交，等待审批')
    return redirect(url_for('oa.approvals'))


@bp.route('/oa/approvals/<int:aid>/decide', methods=['POST'])
@login_required
def approval_decide(aid):
    """审批：通过 / 驳回，审批人=当前登录用户，记录意见与时间"""
    appr = Approval.query.get_or_404(aid)
    action = request.form.get('action')  # approve 通过 / reject 驳回
    if action == 'approve':
        appr.status = 'approved'
    elif action == 'reject':
        appr.status = 'rejected'
    else:
        flash('无效的审批操作')
        return redirect(url_for('oa.approvals'))
    appr.opinion = request.form.get('opinion', '').strip()
    appr.approver_id = session['user_id']
    appr.decided_at = datetime.utcnow()
    db.session.commit()
    flash('已审批')
    return redirect(url_for('oa.approvals'))


# ===================== OA · 差勤管理（复用通用审批引擎，不另建表） =====================
# 差勤 = 请假 / 出差 / 公出 / 补卡，本质是一类审批单。复用 Approval 模型 + approval.approve 权限流闸门，
# 审批动作直接走已有的 approval_decide 接口，避免重复造轮子。
ATTENDANCE_TYPES = ['请假', '出差', '公出', '补卡']
# 差勤申请单号前缀（按类型区分：请假 QJ / 出差 CC / 公出 GC / 补卡 BK）
_ATTD_PREFIX = {'请假': 'QJ', '出差': 'CC', '公出': 'GC', '补卡': 'BK'}
# 出行方式（出差/公出填）：下拉选项，落到 Approval.transport
TRANSPORTS = ['高铁/动车', '飞机', '自驾', '大巴/客车', '网约车/出租车', '其他']
# 补卡时段（补卡填）：落到 Approval.sub_type
CARD_SLOTS = ['上班卡', '下班卡']


def _norm_dt(raw):
    """把 datetime-local（2026-10-01T09:00）或普通日期串规整成 'YYYY-MM-DD HH:MM'。
    只填日期则补 00:00；无法解析返回 ''。"""
    s = (raw or '').strip().replace('T', ' ')
    if not s:
        return ''
    s = s[:16]
    if len(s) == 10:  # 只有日期
        return s + ' 00:00'
    m = re.match(r'^(\d{4}-\d{2}-\d{2})[ ](\d{1,2}:\d{2})', s)
    if not m:
        return ''
    hh, mm = m.group(2).split(':')
    return '%s %02d:%s' % (m.group(1), int(hh), mm)


def _span_days(start_time, end_time):
    """按起止日期算天数（含首尾）；同一天按小时折算（4 小时=0.5 天、8 小时=1 天）。"""
    try:
        s = datetime.strptime(start_time[:16], '%Y-%m-%d %H:%M')
        e = datetime.strptime(end_time[:16], '%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return 0.0
    if e < s:
        return 0.0
    d = (e.date() - s.date()).days
    if d == 0:
        hours = (e - s).total_seconds() / 3600.0
        if hours >= 8:
            return 1.0
        if hours <= 0:
            return 0.0
        return max(round(hours / 8.0 * 2) / 2.0, 0.5)  # 0.5 天为一档
    return float(d + 1)


def _fmt_day(v):
    """天数显示：整数不带小数点（3 天），半天带一位（0.5 天）"""
    v = float(v or 0)
    return '%g' % v


def _span_hours(start_time, end_time):
    """按起止时间算「小时数」（事假等以小时为单位的假期用）。
    同一天：按实际小时；跨天：按日历天数 × 8 小时（含首尾）。"""
    try:
        s = datetime.strptime(start_time[:16], '%Y-%m-%d %H:%M')
        e = datetime.strptime(end_time[:16], '%Y-%m-%d %H:%M')
    except (ValueError, TypeError):
        return 0.0
    if e < s:
        return 0.0
    d = (e.date() - s.date()).days
    if d == 0:
        return max((e - s).total_seconds() / 3600.0, 0.0)
    return float(d + 1) * 8.0


@bp.route('/oa/attendances', methods=['GET'])
@login_required
def attendances():
    """差勤列表 + 提交入口：只显示 请假/出差/公出/补卡 类型的审批单"""
    items = (Approval.query.filter(Approval.type.in_(ATTENDANCE_TYPES))
             .order_by(Approval.created_at.desc()).all())
    user = User.query.get(session['user_id'])
    can_approve = bool(user and user.level and
                       any(p.code == 'approval.approve' for p in user.level.permissions))
    can_attendance_manage = bool(user and user.level and
                                 any(p.code == 'hr.attendance.manage' for p in user.level.permissions))
    # 请假类别下拉 + 本人各假期额度提示（与休假申请同一套口径）
    leave_types = LeaveType.query.order_by(LeaveType.id).all()
    me = Employee.query.filter_by(employee_no=session.get('username')).first()
    my_quotas = {}
    try:
        from hr import _leave_quota, _leave_used
        year = datetime.now().year
        my_leaves = Approval.query.filter_by(applicant_id=session['user_id'], type='请假').all()
        for lt in leave_types:
            q = _leave_quota(lt, me, year)
            used, pend = _leave_used(my_leaves, lt, year)
            q['used'], q['pending'] = used, pend
            q['remain'] = None if q['quota'] is None else max(q['quota'] - used - pend, 0.0)
            my_quotas[lt.name] = q
    except Exception:
        my_quotas = {}
    # 附件清单（请假单的病例/结婚证等）；小时单位假期名（时长按小时展示）
    try:
        from hr import _leave_attach_list
        attach_list = {a.id: _leave_attach_list(a) for a in items if a.type == '请假'}
    except Exception:
        attach_list = {}
    hour_leave_names = {lt.name for lt in leave_types if (lt.unit or 'day') == 'hour'}
    return render_template('oa/attendance_list.html', items=items, can_approve=can_approve,
                           can_attendance_manage=can_attendance_manage,
                           attend_types=ATTENDANCE_TYPES, leave_types=leave_types,
                           transports=TRANSPORTS, card_slots=CARD_SLOTS,
                           my_quotas=my_quotas, me=me, attach_list=attach_list,
                           hour_leave_names=hour_leave_names,
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/oa/attendances/new', methods=['POST'])
@login_required
def attendance_new():
    """提交差勤申请：请假 / 出差 / 公出 / 补卡，按类型收各自的结构化字段，
    统一落到 Approval（type/title/content + 时间/时长/地点/出行方式/子类）。
    请假分支复用「假期额度」口径：本人额度按累计工作年限分级，超额拦截。"""
    type_ = request.form.get('type', '请假').strip()
    if type_ not in ATTENDANCE_TYPES:
        type_ = '请假'  # 非法类型兜底为请假
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('事由不能为空，请说明具体原因')
        return redirect(url_for('oa.attendances'))

    place = request.form.get('place', '').strip()
    transport = request.form.get('transport', '').strip()
    sub_type = ''
    title = ''
    start_time = end_time = ''
    days = 0.0
    lines = []

    if type_ == '请假':
        leave_name = request.form.get('leave_type', '').strip()
        lt = LeaveType.query.filter_by(name=leave_name).first() if leave_name else None
        if not lt:
            flash('请选择请假类别（年假/事假/病假等）')
            return redirect(url_for('oa.attendances'))
        start_time = _norm_dt(request.form.get('start_time'))
        end_time = _norm_dt(request.form.get('end_time'))
        if not start_time or not end_time:
            flash('请填写请假的开始/结束时间')
            return redirect(url_for('oa.attendances'))
        if end_time < start_time:
            flash('结束时间不能早于开始时间')
            return redirect(url_for('oa.attendances'))
        is_hour = (lt.unit or 'day') == 'hour'
        # 时长：天单位按「天」，小时单位（事假）按「小时」，不足 1 小时按 1 小时
        if is_hour:
            days = float(math.ceil(_span_hours(start_time, end_time)))
            if days <= 0:
                flash('请假时长必须大于 0')
                return redirect(url_for('oa.attendances'))
        else:
            days = _to_money(request.form.get('days')) or _span_days(start_time, end_time)
            if days <= 0:
                flash('请假天数必须大于 0')
                return redirect(url_for('oa.attendances'))
        # 必传附件（病假交病例 / 婚假交结婚证 / 产假交生育证明…）
        leave_files = request.files.getlist('attach')
        has_leave_file = any(f and (f.filename or '').strip() for f in leave_files)
        if (lt.need_attach or '0') == '1' and not has_leave_file:
            flash('「%s」必须上传%s后才能提交' % (lt.name, lt.attach_hint or '证明附件'))
            return redirect(url_for('oa.attendances'))
        # 额度校验（与休假申请同口径）：超额直接拦下
        try:
            from hr import _leave_quota, _leave_used
            me = Employee.query.filter_by(employee_no=session.get('username')).first()
            year = datetime.now().year
            q = _leave_quota(lt, me, year)
            if q['quota'] is not None:
                my_leaves = Approval.query.filter_by(applicant_id=session['user_id'],
                                                     type='请假').all()
                used, pend = _leave_used(my_leaves, lt, year)
                if days + used + pend > q['quota'] + 1e-9:
                    flash('超出本人本年度%s额度（%s）：本次 %g %s，最多还能请 %g %s'
                          % (lt.name, q['note'], days, '小时' if is_hour else '天',
                             max(q['quota'] - used - pend, 0), '小时' if is_hour else '天'))
                    return redirect(url_for('oa.attendances'))
        except ImportError:
            pass
        sub_type = lt.name
        title = lt.name + '申请'
        lines = ['假期类别：%s' % lt.name,
                 '%s：%g %s' % ('时长' if is_hour else '天数', days, '小时' if is_hour else '天'),
                 '起止：%s ~ %s' % (start_time, end_time), '事由：%s' % reason]
    elif type_ in ('出差', '公出'):
        if not place:
            flash('请填写%s地点' % ('出差' if type_ == '出差' else '外出'))
            return redirect(url_for('oa.attendances'))
        if not transport:
            flash('请选择出行方式')
            return redirect(url_for('oa.attendances'))
        start_time = _norm_dt(request.form.get('start_time'))
        end_time = _norm_dt(request.form.get('end_time'))
        if not start_time or not end_time:
            flash('请填写%s的开始/结束时间' % type_)
            return redirect(url_for('oa.attendances'))
        if end_time < start_time:
            flash('结束时间不能早于开始时间')
            return redirect(url_for('oa.attendances'))
        days = _to_money(request.form.get('days')) or _span_days(start_time, end_time)
        label = '出差地点' if type_ == '出差' else '外出地点'
        title = '%s申请｜%s' % (type_, place)
        lines = ['%s：%s' % (label, place), '出行方式：%s' % transport,
                 '天数：%s' % _fmt_day(days), '起止：%s ~ %s' % (start_time, end_time),
                 '事由：%s' % reason]
    else:  # 补卡
        card_date = (request.form.get('card_date') or '').strip()[:10]
        slot = request.form.get('card_slot', '').strip()
        if slot not in CARD_SLOTS:
            slot = CARD_SLOTS[0]
        if not card_date:
            flash('请选择漏卡日期')
            return redirect(url_for('oa.attendances'))
        # 实际打卡时间来自 <input type="time">，只有 HH:MM 没有日期（也容忍传完整日期时间）
        card_raw = (request.form.get('card_time') or '').strip()
        m_hm = re.search(r'(\d{1,2}):(\d{2})', card_raw)
        card_hm = '%02d:%s' % (int(m_hm.group(1)), m_hm.group(2)) if m_hm else ''
        start_time = card_date + ' ' + (card_hm or '00:00')
        sub_type = slot
        title = '补卡申请｜%s %s' % (card_date, slot)
        lines = ['漏卡日期：%s' % card_date, '补卡时段：%s' % slot,
                 '实际打卡时间：%s' % (card_hm or '—'), '事由：%s' % reason]

    content = '\n'.join(lines)
    code = _next_expense_code(Approval, _ATTD_PREFIX.get(type_, 'CC'))
    appr = Approval(code=code, type=type_, title=title, content=content,
                    applicant_id=session['user_id'],
                    start_time=start_time, end_time=end_time, att_days=days,
                    place=place, transport=transport, sub_type=sub_type)
    attach_errs = []
    if type_ == '请假':  # 请假附件（病例/结婚证/生育证明…）与休假申请同一套存储
        try:
            from hr import _save_leave_files, _set_leave_attach
            saved, attach_errs = _save_leave_files(request.files.getlist('attach'), code)
            if saved:
                _set_leave_attach(appr, saved)
        except ImportError:
            pass
    db.session.add(appr)
    db.session.commit()
    log_audit('OA-差勤申请提交', target_type='approval', target_no=appr.code,
              detail='%s：%s' % (type_, title))
    if attach_errs:
        flash('部分附件未上传：' + '；'.join(attach_errs))
    flash('%s已提交，等待审批' % type_)
    return redirect(url_for('oa.attendances'))


# ===================== EHR · 出勤数据导入 + 自然月度出勤查询 =====================
# 出勤记录 = "结果数据"（员工每天到底出没出勤），与上面的差勤申请（审批流程）是两回事。
# 通过 CSV 批量导入，导入后可查询某员工某自然月的出勤情况（日历表 + 每日打卡明细）。
# 列顺序：工号,日期,状态,上班打卡,下班打卡,备注
# 状态取值：正常/迟到/早退/旷工/请假（中文，导入时自动映射成英文编码存库）。

# 中文状态 → 存库编码 的映射（CSV 导入时用）
# 支持「正常」与「出勤」两种写法（用户习惯称"出勤"），都映射为 normal
ATT_STATUS_MAP = {
    '正常': 'normal', '出勤': 'normal', '迟到': 'late', '早退': 'early',
    '旷工': 'absent', '请假': 'leave',
}
# 存库编码 → 界面显示名（反查）。「正常」对外显示为「出勤」（用户口径）
ATT_STATUS_NAME = {
    'normal': '出勤', 'late': '迟到', 'early': '早退', 'absent': '旷工', 'leave': '请假',
}
# 「未打下班卡」不是独立的存库状态，而是派生态：
# 正常出勤(normal)但下班打卡为空 → 界面显示为「未打下班卡」。
# 这样既不改动 CSV 导入的数据结构，又能如实反映打卡机缺卡的情况。


@bp.route('/oa/attendances/import', methods=['POST'])
@login_required
@permission_required('hr.attendance.manage')
def attendance_import():
    """CSV 导入出勤记录：列 工号,日期,状态,上班打卡,下班打卡,备注（首行可为表头）。
    按 (工号,日期) 去重：同一天重复导入时覆盖旧记录，避免刷出重复数据。"""
    file = request.files.get('file')
    if not file or file.filename == '':
        flash('请选择要导入的 CSV 文件')
        return redirect(url_for('oa.attendances'))
    # 用标准库 csv 解析（简单优先，与员工批量导入一致）；尝试 GBK 与 UTF-8 两种编码
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
        return redirect(url_for('oa.attendances'))

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        flash('文件为空')
        return redirect(url_for('oa.attendances'))

    # 跳过表头行（若首行第一个单元格是"工号/日期/员工"之类的文字）
    start = 0
    head = (rows[0][0] or '').strip() if rows[0] else ''
    if head and not head.isdigit():
        start = 1

    imported = 0
    errors = []
    for r in rows[start:]:
        if not r or len(r) < 3:
            continue
        emp_no = (r[0] or '').strip()
        date_s = (r[1] or '').strip()
        status_cn = (r[2] or '').strip()
        if not emp_no or not date_s:
            continue
        # 按工号找员工
        emp = Employee.query.filter_by(employee_no=emp_no).first()
        if not emp:
            errors.append('工号 %s 不存在，已跳过' % emp_no)
            continue
        # 解析日期（支持 2026-09-17 或 2026/09/17）
        d = None
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
            try:
                d = datetime.strptime(date_s, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            errors.append('工号 %s 日期 %s 无法解析，已跳过' % (emp_no, date_s))
            continue
        status = ATT_STATUS_MAP.get(status_cn, 'normal')  # 未知状态兜底为"正常"
        # 去重：同员工同日期已存在则更新，否则新建
        rec = AttendanceRecord.query.filter_by(employee_id=emp.id, work_date=d).first()
        if rec is None:
            rec = AttendanceRecord(employee_id=emp.id, work_date=d)
            db.session.add(rec)
        rec.status = status
        rec.check_in = (r[3] or '').strip()[:8] if len(r) > 3 else ''
        rec.check_out = (r[4] or '').strip()[:8] if len(r) > 4 else ''
        rec.note = (r[5] or '').strip()[:200] if len(r) > 5 else ''
        imported += 1
    db.session.commit()
    flash('成功导入 %d 条出勤记录' % imported)
    for e in errors[:5]:  # 最多提示 5 条，避免刷屏
        flash(e)
    return redirect(url_for('oa.attendances'))


@bp.route('/oa/attendance/records', methods=['GET'])
@login_required
def attendance_records():
    """自然月度出勤查询：上方日历表（可点击某天过滤下方明细）+ 下方每日打卡明细。
    日历里自动标注：周六/周日=休息日、法定节假日=休/班（补班）、出勤/迟到/早退/未打下班卡/旷工/请假。"""
    # 查询月份：支持 2026-09 或 2026-09-17（取到月即可）
    month_s = (request.args.get('month') or '').strip()
    emp_id = request.args.get('employee_id') or ''
    q_month = None
    try:
        q_month = datetime.strptime(month_s[:7], '%Y-%m').date().replace(day=1)
    except (ValueError, TypeError):
        q_month = date.today().replace(day=1)  # 非法则回退当月 1 号

    # 该月天数与末尾日期（calendar.monthrange 返回 (首日星期, 天数)）
    import calendar as _cal
    _, month_days = _cal.monthrange(q_month.year, q_month.month)
    month_end = date(q_month.year, q_month.month, month_days)

    # 该月出勤记录（按员工过滤）
    q = AttendanceRecord.query.filter(
        AttendanceRecord.work_date >= q_month,
        AttendanceRecord.work_date <= month_end,
    )
    if emp_id:
        q = q.filter_by(employee_id=int(emp_id))
    records = q.order_by(AttendanceRecord.work_date).all()

    # 法定节假日：一次性查出全年（含调休补班），按日期建字典
    from models import Holiday
    holiday_map = {h.holiday_date: h for h in Holiday.query.filter(
        Holiday.holiday_date >= date(q_month.year, 1, 1),
        Holiday.holiday_date <= date(q_month.year, 12, 31)).all()}

    # 考勤规则（用于判断迟到/早退/未打下班卡的展示口径，将来接打卡机同步也用）
    from models import AttendanceRule
    rule = AttendanceRule.query.filter_by(code='global').first()

    # 把 "HH:MM" 字符串转成当天分钟数（自 00:00 起），用于比较早晚。无效返回 None
    def to_minutes(s):
        if not s:
            return None
        try:
            hh, mm = s.strip().split(':')
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return None

    # 判定某条记录的"界面显示状态"（按考勤规则自动判定，优先于存库状态）：
    #   1) 状态是 迟到/早退/旷工/请假 → 直接保留（导入时已明确指定）
    #   2) 状态是 normal（出勤）时，用打卡时间 + 规则阈值自动判定：
    #      - 要求打下班卡但缺下班卡 → 「未打下班卡」
    #      - 上班卡时间 > 上班时间 + late_after 分钟 → 「迟到」
    #      - 下班卡时间 < 下班时间 - early_before 分钟 → 「早退」
    #      - 否则 → 「出勤」
    #   （存库状态保持不变，迟到/早退/未打下班卡都是"按规则派生的显示态"，见上方注释）
    def display_status(r):
        st = r.status
        # 非 normal 的状态（迟到/早退/旷工/请假）是导入时已明确的，直接保留
        if st != 'normal':
            return st
        # normal 出勤：按规则逐项判定
        if rule and rule.need_check_out and not r.check_out:
            return 'no_checkout'          # 伪状态：未打下班卡
        if rule:
            ws = to_minutes(rule.work_start)
            we = to_minutes(rule.work_end)
            ci = to_minutes(r.check_in)
            co = to_minutes(r.check_out)
            late_after = rule.late_after or 0
            early_before = rule.early_before or 0
            # 迟到：打了上班卡且晚于 上班时间 + 宽限分钟
            if ci is not None and ws is not None and ci > ws + late_after:
                return 'late'
            # 早退：打了下班卡且早于 下班时间 - 宽限分钟
            if co is not None and we is not None and co < we - early_before:
                return 'early'
        return 'normal'

    # 构建 {日期 -> {状态中文, 显示状态, 上班, 下班, 备注}} 映射，供日历与明细共用
    day_map = {}
    for r in records:
        st = display_status(r)
        # 给每条记录挂上"界面显示状态"，明细表直接用，避免模板里重复判定
        r._display_status = st
        # 注意：st 可能是派生的 late/early/no_checkout，必须按 st（而非存库 status）取显示名
        if st == 'no_checkout':
            r._status_cn = '未打下班卡'
        else:
            r._status_cn = ATT_STATUS_NAME.get(st, st)
        day_map[r.work_date] = {
            'status': r.status,
            'display': st,            # 派生显示态编码（normal/late/early/absent/leave/no_checkout）
            'status_cn': r._status_cn,
            'check_in': r.check_in or '',
            'check_out': r.check_out or '',
            'note': r.note or '',
        }

    # 判断某天是「休息日/法定休/补班/普通工作日」，供日历打标
    # 返回 (标签文字, 类型)，类型用于前端配色：rest休息日 / holiday法定休 / workday补班/工作日
    def day_tag(d):
        h = holiday_map.get(d)
        if h:
            if h.day_type == 'work':
                return ('班', 'workday')      # 调休补班（周末上班）
            return (h.name, 'holiday')        # 法定放假
        # 周六(5)/周日(6) 固定休息日
        if d.weekday() >= 5:
            return ('休', 'rest')             # 周末休息日
        return (None, 'workday')              # 普通工作日

    # 生成日历表格行：按周一~周日排布，每周 7 格（存 date 对象，模板直接索引 day_map）
    first_weekday = _cal.monthrange(q_month.year, q_month.month)[0]  # 0=周一 ... 6=周日
    weeks = []          # 每项为 7 个元素（date 或 None）
    week = [None] * first_weekday            # 月初左侧补空
    for d in range(1, month_days + 1):
        week.append(date(q_month.year, q_month.month, d))
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:                                  # 月末补齐空位
        week += [None] * (7 - len(week))
        weeks.append(week)

    # 员工下拉：花名册按工号排序
    employees = Employee.query.order_by(Employee.employee_no).all()

    # 当月状态统计（概览）：按"派生显示态"统计，与日历/明细口径一致
    stats = {}
    for r in records:
        key = getattr(r, '_display_status', r.status)
        stats[key] = stats.get(key, 0) + 1
    return render_template('oa/attendance_records.html',
                           q_month=q_month, month_days=month_days,
                           weeks=weeks, day_map=day_map, employees=employees,
                           emp_id=emp_id, stats=stats, records=records,
                           att_status_name=ATT_STATUS_NAME, day_tag=day_tag,
                           rule=rule)

@bp.route('/oa/todos', methods=['GET'])
@login_required
def todos():
    """OA 待办：直接从审批数据派生，不另建表（避免重复存储）。
    分两类：
      ① 待我审批 —— 状态=待审批，且当前用户有审批权限（按权限流判断）
      ② 我发起的待处理 —— 申请人=我，且状态仍为待审批
    """
    user = User.query.get(session['user_id'])
    # 复用审批页同样的权限判断：所属职级是否挂了 approval.approve 权限
    can_approve = bool(user and user.level and
                       any(p.code == 'approval.approve' for p in user.level.permissions))
    # ① 待我审批：仅当有审批权限时才有内容（无权限则为空列表）
    to_approve = (Approval.query.filter_by(status='pending')
                  .order_by(Approval.created_at.desc()).all()) if can_approve else []
    # ② 我发起的待处理：申请人是我，且还没批完
    mine_pending = (Approval.query.filter_by(applicant_id=user.id, status='pending')
                    .order_by(Approval.created_at.desc()).all())
    return render_template('oa/todo_list.html',
                           to_approve=to_approve, mine_pending=mine_pending,
                           can_approve=can_approve)


# ===================== OA · 审批流设置（行政管理员配置审批人） =====================
# 审批流 = 名称 + 一串「有序审批人」。行政管理员在后台设置，员工提交申请后按顺序逐级审批。
# 需要权限 oa.flow.manage（授予行政管理人员）。

@bp.route('/oa/flows', methods=['GET'])
@login_required
@permission_required('oa.flow.manage')
def flows():
    """审批流列表：展示所有审批流及其有序审批人"""
    flows_ = ApprovalFlow.query.order_by(ApprovalFlow.id).all()
    users = User.query.order_by(User.username).all()
    return render_template('oa/flow_list.html', flows=flows_, users=users)


@bp.route('/oa/flows/new', methods=['POST'])
@login_required
@permission_required('oa.flow.manage')
def flow_new():
    """新建审批流：名称 + 适用类型 + 有序审批人（多个，按提交顺序）"""
    name = request.form.get('name', '').strip()
    apply_type = request.form.get('apply_type', '').strip()
    note = request.form.get('note', '').strip()
    enabled = request.form.get('enabled') == 'on'
    # 审批人：一个或多个 user_id，按表单提交顺序作为审批顺序
    approver_ids = request.form.getlist('approver_ids')
    if not name:
        flash('审批流名称不能为空')
        return redirect(url_for('oa.flows'))
    if not approver_ids or not any(approver_ids):
        flash('请至少添加一位审批人')
        return redirect(url_for('oa.flows'))
    flow = ApprovalFlow(name=name, apply_type=apply_type, note=note, enabled=enabled)
    db.session.add(flow)
    db.session.flush()  # 拿到 flow.id
    # 按顺序写入审批步骤（seq 从 1 开始）
    seq = 1
    for uid in approver_ids:
        uid = (uid or '').strip()
        if not uid:
            continue
        db.session.add(ApprovalFlowStep(flow_id=flow.id, user_id=int(uid), seq=seq))
        seq += 1
    db.session.commit()
    flash('审批流已创建')
    return redirect(url_for('oa.flows'))


@bp.route('/oa/flows/<int:fid>/edit', methods=['POST'])
@login_required
@permission_required('oa.flow.manage')
def flow_edit(fid):
    """编辑审批流：改名称/类型/启用状态，并重建审批人顺序（先删后加）"""
    flow = ApprovalFlow.query.get_or_404(fid)
    flow.name = request.form.get('name', flow.name).strip() or flow.name
    flow.apply_type = request.form.get('apply_type', '').strip()
    flow.note = request.form.get('note', '').strip()
    flow.enabled = request.form.get('enabled') == 'on'
    approver_ids = request.form.getlist('approver_ids')
    if not any(approver_ids):
        flash('请至少保留一位审批人')
        return redirect(url_for('oa.flows'))
    # 重建审批步骤：清掉旧的，按新顺序重写
    ApprovalFlowStep.query.filter_by(flow_id=flow.id).delete()
    seq = 1
    for uid in approver_ids:
        uid = (uid or '').strip()
        if not uid:
            continue
        db.session.add(ApprovalFlowStep(flow_id=flow.id, user_id=int(uid), seq=seq))
        seq += 1
    db.session.commit()
    flash('审批流已更新')
    return redirect(url_for('oa.flows'))


@bp.route('/oa/flows/<int:fid>/delete', methods=['POST'])
@login_required
@permission_required('oa.flow.manage')
def flow_delete(fid):
    """删除审批流（连带删除其审批步骤）"""
    flow = ApprovalFlow.query.get_or_404(fid)
    db.session.delete(flow)  # cascade 会删掉 steps
    db.session.commit()
    flash('审批流已删除')
    return redirect(url_for('oa.flows'))


# ===================== OA · 邮箱（公司内部站内邮件） =====================
# 内部管理人员（User）之间互发邮件：收件箱 / 发件箱 / 写信 / 详情（标记已读）/ 删除。
# 邮箱是公司内部沟通工具，不接外部 SMTP（未来如需对接企业邮箱再扩展）。

@bp.route('/oa/mails', methods=['GET'])
@login_required
def mails():
    """邮箱首页：收件箱 / 发件箱 双 Tab（?box=in 收件箱，?box=out 发件箱）。"""
    user = User.query.get(session['user_id'])
    box = request.args.get('box', 'in')
    if box == 'out':
        items = Mail.query.filter_by(sender_id=user.id)\
                   .order_by(Mail.created_at.desc()).all()
    else:
        items = Mail.query.filter_by(recipient_id=user.id)\
                   .order_by(Mail.created_at.desc()).all()
    unread = Mail.query.filter_by(recipient_id=user.id, is_read=False).count()
    users = User.query.order_by(User.username).all()  # 写信时的收件人下拉
    return render_template('oa/mail_list.html', items=items, box=box,
                           unread=unread, users=users)


@bp.route('/oa/mails/send', methods=['POST'])
@login_required
def mail_send():
    """发信：选收件人 + 标题 + 正文。收件人必须存在（从 User 选）。"""
    recipient_id = request.form.get('recipient_id', '').strip()
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    recipient = User.query.get(int(recipient_id)) if recipient_id.isdigit() else None
    if not recipient:
        flash('请选择收件人')
        return redirect(url_for('oa.mails'))
    if not title:
        flash('邮件标题不能为空')
        return redirect(url_for('oa.mails'))
    mail = Mail(sender_id=session['user_id'], recipient_id=recipient.id,
                title=title, content=content)
    db.session.add(mail)
    db.session.commit()
    flash('邮件已发送')
    return redirect(url_for('oa.mails', box='out'))


@bp.route('/oa/mails/<int:mid>', methods=['GET'])
@login_required
def mail_detail(mid):
    """查看邮件详情：若是收件人查看，自动标记已读。"""
    mail = Mail.query.get_or_404(mid)
    user = User.query.get(session['user_id'])
    # 只有发件人或收件人才能看这封邮件
    if mail.sender_id != user.id and mail.recipient_id != user.id:
        flash('无权查看该邮件')
        return redirect(url_for('oa.mails'))
    if mail.recipient_id == user.id and not mail.is_read:
        mail.is_read = True
        db.session.commit()
    return render_template('oa/mail_detail.html', mail=mail)


@bp.route('/oa/mails/<int:mid>/delete', methods=['POST'])
@login_required
def mail_delete(mid):
    """删除邮件：仅收件人或发件人可删自己那侧（简化：直接物理删除该邮件）。"""
    mail = Mail.query.get_or_404(mid)
    user = User.query.get(session['user_id'])
    if mail.sender_id != user.id and mail.recipient_id != user.id:
        flash('无权删除该邮件')
        return redirect(url_for('oa.mails'))
    db.session.delete(mail)
    db.session.commit()
    flash('邮件已删除')
    return redirect(url_for('oa.mails'))


# ===================== OA · 申请中心（统一申请入口） =====================
# ===================== OA · 统一申请中心（申请类型目录 + 新建申请对话框） =====================
# 口径（2026-10-01 按威哥公司现行流程定）：人事 / 财务 / 行政 / 通用 四大类。
# 每项字段：
#   code     申请类型编码（唯一、长期不变，用于回找类型）
#   name     申请单名称（写进单据标题与列表「业务类型」列）
#   prefix   单号前缀（本单号 = 前缀-YYYYMM-NNN，同类连续编号）
#   desc     一句话说明（对话框里显示，让员工知道该选哪个）
#   endpoint 已上线时指向专属表单端点；None = 走统一「通用申请单」表单
#   need_amt 表单是否显示金额栏（盖章/合同等无金额的填 0 也不影响）
APPLY_CATALOG = [
    {'key': 'hr', 'name': '人事', 'en': 'HR', 'items': [
        {'code': 'HR-SALARY', 'name': '薪酬发放申请', 'prefix': 'XC', 'need_amt': True,
         'desc': '按月申请发放薪酬，含应发/实发/单位社保公积金与人工成本拆解 + 工资表附件',
         'endpoint': 'oa.salary_apply'},
        {'code': 'HR-LEAVE', 'name': '请假申请', 'prefix': 'QJ', 'need_amt': False,
         'desc': '年假 / 事假 / 病假等，带假期额度校验', 'endpoint': 'hr.leave_apply'},
        {'code': 'HR-ATTD', 'name': '出差 / 补卡申请', 'prefix': 'CC', 'need_amt': False,
         'desc': '出差报备、漏打卡补卡，统一归入差勤记录', 'endpoint': 'oa.attendances'},
    ]},
    {'key': 'fin', 'name': '财务', 'en': 'FIN', 'items': [
        {'code': 'FIN-REIMB', 'name': '报销申请', 'prefix': 'BX', 'need_amt': True,
         'desc': '垫付后凭票报销：直接报销 / 关联费用申请双轨，审批后可打印装订',
         'endpoint': 'oa.expense_reimburse_new'},
        {'code': 'FIN-PRE', 'name': '费用申请单（事前额度）', 'prefix': 'SQB', 'need_amt': True,
         'desc': '事前申请一笔费用额度，批准后报销时可关联，形成合规闭环',
         'endpoint': 'oa.expense_claim_new'},
        {'code': 'FIN-PAY', 'name': '请（付）款申请', 'prefix': 'QK', 'need_amt': True,
         'desc': '对公请款 / 付款申请，需注明收款单位与用途', 'endpoint': None},
        {'code': 'FIN-TRANS', 'name': '资金划拨申请', 'prefix': 'HB', 'need_amt': True,
         'desc': '账户间资金划拨，需注明转出 / 转入账户', 'endpoint': None},
        {'code': 'FIN-LOAN', 'name': '个人借支申请', 'prefix': 'JZ', 'need_amt': True,
         'desc': '员工个人预支款项，需注明归还方式与期限', 'endpoint': None},
        {'code': 'FIN-SURR', 'name': '客户非正常退保费用申请', 'prefix': 'TB', 'need_amt': True,
         'desc': '客户非正常退保产生的费用支出申请', 'endpoint': None},
    ]},
    {'key': 'adm', 'name': '行政', 'en': 'ADM', 'items': [
        {'code': 'ADM-SEAL', 'name': '盖章申请', 'prefix': 'GZ', 'need_amt': False,
         'desc': '公司印章使用申请，需注明用途、份数与归还时间', 'endpoint': None},
        {'code': 'ADM-CONTRACT', 'name': '合同审批及盖章申请表', 'prefix': 'HT', 'need_amt': True,
         'desc': '商务合同审批 + 用章，需附合同文本与对方签约主体', 'endpoint': None},
        {'code': 'ADM-GEN', 'name': '行政申请', 'prefix': 'XZ', 'need_amt': False,
         'desc': '办公用品、场地、物料等行政类事务申请', 'endpoint': None},
    ]},
    {'key': 'gen', 'name': '通用', 'en': 'GEN', 'items': [
        {'code': 'GEN-REPORT', 'name': '呈批件申请', 'prefix': 'CB', 'need_amt': True,
         'desc': '需呈报领导批示的事项，走通用呈批流程', 'endpoint': None},
    ]},
]

# 状态 → 中文与颜色类（三类单据状态合并展示用）
_STATUS_CN = {'pending': '审批中', 'approved': '审批通过', 'rejected': '已驳回',
              'closed': '已核销', 'paid': '已付款', 'returned': '退回修改'}
_STATUS_CLS = {'pending': 'wait', 'approved': 'ok', 'rejected': 'no',
               'closed': 'gray', 'paid': 'info', 'returned': 'gray'}


def _apply_catalog():
    """申请类型目录（补 url）：已上线项 → 专属表单；其余 → 通用申请单表单。"""
    groups = []
    for g in APPLY_CATALOG:
        items = []
        for it in g['items']:
            ep = it.get('endpoint')
            url = url_for(ep) if ep else url_for('oa.apply_form', code=it['code'])
            items.append(dict(it, url=url, is_on=bool(ep)))
        groups.append(dict(g, items=items))
    return groups


def _find_apply_type(code):
    """按编码在目录里找申请类型定义（附所属分类名），找不到返回 None。"""
    for g in APPLY_CATALOG:
        for it in g['items']:
            if it['code'] == code:
                return dict(it, group=g['name'])
    return None


def _apply_flat():
    """目录拍平成一维（给前端搜索/弹窗用）。"""
    out = []
    for g in _apply_catalog():
        for it in g['items']:
            out.append(dict(it, group_key=g['key'], group_name=g['name']))
    return out


def _my_applies(me):
    """我的申请：把三类单据（通用申请 / 费用申请单 / 费用报销单）合并成统一结构，
    按提交时间倒序。结构：code/kind/summary/amount/created_at/status/status_cn/cls/url"""
    rows = []

    for a in Approval.query.filter_by(applicant_id=me['id']).all():
        if a.type == '薪酬发放':
            rows.append({'code': a.code or ('XC%05d' % a.id), 'kind': '薪酬发放申请',
                         'summary': '%s ｜ %s 人 ｜ %s' % (a.sa_month or '', a.headcount or 0,
                                                          (a.title or '')[:24]),
                         'amount': a.actual_amount or 0, 'created_at': a.created_at,
                         'status': a.status,
                         'url': url_for('oa.salary_apply_detail', aid=a.id)})
            continue
        rows.append({'code': a.code or ('SP%05d' % a.id), 'kind': a.type or '通用申请',
                     'summary': a.title or '', 'amount': a.amount or 0,
                     'created_at': a.created_at, 'status': a.status,
                     'url': url_for('oa.approvals')})

    for c in ExpenseClaim.query.filter_by(applicant_id=me['id']).all():
        rows.append({'code': c.code or ('SQB%05d' % c.id), 'kind': '费用申请单',
                     'summary': '%s｜%s' % (c.category or '', (c.purpose or '')[:40]),
                     'amount': c.amount or 0, 'created_at': c.created_at,
                     'status': c.status,
                     'url': url_for('oa.expense_claim_detail', cid=c.id)})

    for r in ExpenseReimburse.query.filter_by(applicant_id=me['id']).all():
        tag = '直接报销' if r.mode != 'apply' else ('关联 ' + (r.claim_code or ''))
        rows.append({'code': r.code or ('BXD%05d' % r.id), 'kind': '费用报销单',
                     'summary': '%s｜%s' % (tag, (r.purpose or '')[:40]),
                     'amount': r.amount or 0, 'created_at': r.created_at,
                     'status': r.status,
                     'url': url_for('oa.expense_reimburse_detail', rid=r.id)})

    for x in rows:
        x['status_cn'] = _STATUS_CN.get(x['status'], x['status'] or '')
        x['cls'] = _STATUS_CLS.get(x['status'], 'gray')
    rows.sort(key=lambda x: x['created_at'] or datetime.min, reverse=True)
    return rows


# 把所有「申请」集中到一个入口：人事/财务/行政/通用各类申请在此发起并查询。

@bp.route('/oa/apply', methods=['GET'])
@login_required
def apply_hub():
    """申请中心（原「申请」+「费用申请」两个模块入口已合并到这里）：
    点「新建申请」弹对话框选申请类型（人事/财务/行政/通用 13 类，含报销申请与费用申请单），
    下方按「业务类型 / 流程状态」筛选查询本人已提交的各类申请（含费用申请单 / 报销单）。"""
    me = _me_info()
    rows = _my_applies(me)
    return render_template('oa/apply_hub.html', catalog=_apply_catalog(),
                           rows=rows, me=me,
                           kinds=sorted({r['kind'] for r in rows if r['kind']}))


@bp.route('/oa/apply/form', methods=['GET', 'POST'])
@login_required
def apply_form():
    """通用申请单：目录中暂无专属表单的申请类型统一走这里。
    提交后写入 Approval（type=申请单名称），进入统一审批流程。"""
    me = _me_info()
    code = (request.args.get('code') or request.form.get('code') or '').strip()
    atype = _find_apply_type(code)
    if not atype:
        flash('申请类型不存在，请重新选择')
        return redirect(url_for('oa.apply_hub'))

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('申请事由不能为空')
            return redirect(url_for('oa.apply_form', code=code))
        amount = _to_money(request.form.get('amount'))
        expect = (request.form.get('expect_date') or '').strip()
        detail = (request.form.get('detail') or '').strip()
        files = (request.form.get('attachment_count') or '').strip()
        head = []
        if amount:
            head.append('金额：￥%.2f' % amount)
        if expect:
            head.append('期望完成日期：%s' % expect)
        if files:
            head.append('附件：%s 张' % files)
        content = (('【' + '｜'.join(head) + '】\n' if head else '') + detail).strip()
        appr = Approval(code=_next_expense_code(Approval, atype['prefix']),
                        type=atype['name'], title=title, content=content,
                        amount=amount, applicant_id=me['id'])
        db.session.add(appr)
        db.session.commit()
        log_audit('OA-申请单提交', module='oa', target_type='approval',
                  target_no=appr.code, detail='%s：%s' % (atype['name'], title))
        flash('《%s》已提交（%s），等待审批' % (atype['name'], appr.code))
        return redirect(url_for('oa.apply_hub'))

    return render_template('oa/apply_form.html', atype=atype, me=me,
                           today=datetime.now().strftime('%Y-%m-%d'),
                           next_code=_next_expense_code(Approval, atype['prefix']))


# ===================== OA · 薪酬发放申请（专属表单，按公司《薪酬发放申请单》图例做） =====================
# 走 Approval 审批引擎（type='薪酬发放'），不复用通用申请单：
# 字段固定、带成本拆解与附件，需要结构化落库（便于后续接财务计提与人工成本报表）。
# 口径：
#   · 应发薪酬 = 税前应发合计；绩效合计 = 应发中属于绩效的部分；
#   · 实发金额 = 实际打款合计（应发 − 个人社保公积金 − 个税）；
#   · 薪酬成本 = 应发薪酬 + 单位社保 + 单位公积金（前端自动算，可手改）；
#   · 人工成本 = 薪酬成本（本单口径下相同，可手改调增）。
PAY_ENTITIES = ['广东海盈通保智能科技有限公司', '海腾保险代理有限公司（代发）']
SALARY_ATTACH_EXT = {'.pdf', '.xls', '.xlsx', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.zip'}


def _save_salary_attach(file_storage, code=None):
    """保存薪酬申请附件到 static/uploads/salary/ 下，返回保存后的文件名（存入 Approval.attach_file）。
    命名：<单号或时间戳>_<8位随机码><原扩展名> —— 避开中文名下载乱码与重名/路径穿越。"""
    if not file_storage or file_storage.filename == '':
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in SALARY_ATTACH_EXT:
        flash('附件类型不支持：%s（支持 pdf/word/excel/图片/zip）' % ext)
        return '__INVALID__'
    upload_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'salary')
    os.makedirs(upload_dir, exist_ok=True)
    prefix = (code or datetime.now().strftime('%Y%m%d%H%M%S')).replace('/', '-')
    filename = '%s_%s%s' % (prefix, uuid.uuid4().hex[:8], ext)
    file_storage.save(os.path.join(upload_dir, filename))
    return filename


def _salary_related_pool(uid, me_name=None):
    """「关联申请」弹窗的数据池，两个页签：
      · tab='me'       我的申请（本人提交的）
      · tab='approved' 我已审批（本人作为审批人处理过的）
    每行字段：code / kind / who / date / summary / amount / status / status_cn / tab
    —— 一次性全量取回、服务端渲染成表格行，前端只做「筛选显隐 + 勾选」，不用再请求后端。"""
    out = []
    seen = set()
    # User 没有 name 列（B 模式：登录名 == 工号），姓名要回 Employee 花名册取
    name_map = {e.employee_no: e.name for e in Employee.query.all() if e.employee_no}

    def who_of(u):
        if not u:
            return ''
        return name_map.get(u.username) or u.username or ''

    def add(code, kind, who, dt, summary, amount, status, tab):
        if not code or (code, tab) in seen:
            return
        seen.add((code, tab))
        out.append({
            'code': code, 'kind': (kind or '通用申请'), 'who': who or '',
            'date': dt.strftime('%Y-%m-%d') if dt else '',
            'summary': (summary or '').replace('\n', ' ').strip(),
            'amount': round(float(amount or 0), 2), 'status': status or '',
            'status_cn': _STATUS_CN.get(status, status or '—'), 'tab': tab})

    # —— 我的申请：本人提交（排除薪酬发放，自己关联自己没意义） ——
    for a in (Approval.query.filter_by(applicant_id=uid)
              .order_by(Approval.id.desc()).limit(50).all()):
        if a.type == '薪酬发放':
            continue
        add(a.code, a.type, who_of(a.applicant),
            a.created_at, a.title, a.amount, a.status, 'me')
    for c in (ExpenseClaim.query.filter_by(applicant_id=uid)
              .order_by(ExpenseClaim.id.desc()).limit(50).all()):
        add(c.code, '费用申请单', c.applicant_name, c.created_at,
            c.purpose, c.amount, c.status, 'me')
    for r in (ExpenseReimburse.query.filter_by(applicant_id=uid)
              .order_by(ExpenseReimburse.id.desc()).limit(50).all()):
        add(r.code, '费用报销单', r.applicant_name, r.created_at,
            r.purpose, r.amount, r.status, 'me')

    # —— 我已审批：本人作为审批人（费用两表只有 approver_name 冗余字段，按姓名匹配） ——
    for a in (Approval.query.filter_by(approver_id=uid)
              .order_by(Approval.id.desc()).limit(50).all()):
        add(a.code, a.type, who_of(a.applicant),
            a.decided_at or a.created_at, a.title, a.amount, a.status, 'approved')
    if me_name:
        for c in (ExpenseClaim.query.filter_by(approver_name=me_name)
                  .order_by(ExpenseClaim.id.desc()).limit(50).all()):
            add(c.code, '费用申请单', c.applicant_name, c.created_at,
                c.purpose, c.amount, c.status, 'approved')
        for r in (ExpenseReimburse.query.filter_by(approver_name=me_name)
                  .order_by(ExpenseReimburse.id.desc()).limit(50).all()):
            add(r.code, '费用报销单', r.applicant_name, r.created_at,
                r.purpose, r.amount, r.status, 'approved')

    out.sort(key=lambda x: (x['date'], x['code']), reverse=True)
    return out


def _salary_content(a):
    """把薪酬申请的关键数据拼成审批人看的摘要文本（存 Approval.content，列表/详情直接读）。"""
    return '\n'.join([
        '薪酬月份：%s' % (a.sa_month or '—'),
        '发薪单位：%s' % (a.pay_entity or '—'),
        '发薪人数：%s 人' % (a.headcount or 0),
        '应发薪酬：￥%.2f' % (a.gross_amount or 0),
        '绩效合计：￥%.2f' % (a.perf_amount or 0),
        '实发金额：￥%.2f' % (a.actual_amount or 0),
        '单位社保：￥%.2f' % (a.corp_social or 0),
        '单位公积金：￥%.2f' % (a.corp_fund or 0),
        '薪酬成本：￥%.2f' % (a.salary_cost or 0),
        '人工成本：￥%.2f' % (a.labor_cost or 0),
        '关联申请：%s' % (a.related_code or '无'),
        '备注：%s' % (a.title or ''),
    ])


@bp.route('/oa/apply/salary', methods=['GET', 'POST'])
@login_required
def salary_apply():
    """薪酬发放申请单：新建并提交（type='薪酬发放'，走统一审批流）。"""
    me = _me_info()
    if request.method == 'POST':
        month = (request.form.get('sa_month') or '').strip()[:7]
        entity = (request.form.get('pay_entity') or '').strip()
        remark = (request.form.get('remark') or '').strip()
        if not month:
            flash('请选择薪酬月份')
            return redirect(url_for('oa.salary_apply'))
        if not entity:
            flash('请填写发薪单位')
            return redirect(url_for('oa.salary_apply'))
        headcount = int(_to_money(request.form.get('headcount')))
        gross = _to_money(request.form.get('gross_amount'))
        perf = _to_money(request.form.get('perf_amount'))
        actual = _to_money(request.form.get('actual_amount'))
        c_social = _to_money(request.form.get('corp_social'))
        c_fund = _to_money(request.form.get('corp_fund'))
        # 成本类字段：前端会自动算，这里做兜底（留空/为 0 时按口径补算）
        cost = _to_money(request.form.get('salary_cost')) or round(gross + c_social + c_fund, 2)
        labor = _to_money(request.form.get('labor_cost')) or cost
        if headcount <= 0:
            flash('发薪人数必须大于 0')
            return redirect(url_for('oa.salary_apply'))
        if gross <= 0 or actual <= 0:
            flash('应发薪酬与实发金额必须大于 0')
            return redirect(url_for('oa.salary_apply'))
        code = _next_expense_code(Approval, 'XC')
        attach = _save_salary_attach(request.files.get('attach'), code)
        if attach == '__INVALID__':
            return redirect(url_for('oa.salary_apply'))
        related = request.form.getlist('related_code')
        appr = Approval(code=code, type='薪酬发放',
                        title=remark or ('%s 薪酬发放' % month),
                        applicant_id=me['id'],
                        sa_month=month, pay_entity=entity, headcount=headcount,
                        gross_amount=gross, perf_amount=perf, actual_amount=actual,
                        corp_social=c_social, corp_fund=c_fund,
                        salary_cost=cost, labor_cost=labor,
                        attach_file=attach, related_code=','.join(related))
        appr.content = _salary_content(appr)
        db.session.add(appr)
        db.session.commit()
        log_audit('OA-薪酬发放申请', module='oa', target_type='approval',
                  target_no=appr.code, detail='%s 薪酬发放申请 ￥%.2f' % (month, actual))
        flash('薪酬发放申请（%s）已提交，等待审批' % appr.code)
        return redirect(url_for('oa.salary_apply_detail', aid=appr.id))

    pool = _salary_related_pool(me['id'], me.get('name'))
    # 下拉选项按池子里出现的顺序去重（保持稳定，不按字典序乱跳）
    kinds, seen_st, statuses = [], set(), []
    for p in pool:
        if p['kind'] not in kinds:
            kinds.append(p['kind'])
        if p['status'] and p['status'] not in seen_st:
            seen_st.add(p['status'])
            statuses.append((p['status'], p['status_cn']))
    return render_template('oa/salary_apply.html', me=me,
                           today=datetime.now().strftime('%Y-%m-%d'),
                           next_code=_next_expense_code(Approval, 'XC'),
                           pay_entities=PAY_ENTITIES,
                           related_pool=pool, kinds=kinds, statuses=statuses)


@bp.route('/oa/apply/salary/<int:aid>', methods=['GET'])
@login_required
def salary_apply_detail(aid):
    """薪酬发放申请单详情：字段全览 + 附件下载 + 审批（有权限时）。"""
    a = Approval.query.filter_by(id=aid, type='薪酬发放').first_or_404()
    me = _me_info()
    can_approve = _can('approval.approve')
    can_pay = _can('oa.expense.pay')
    return render_template('oa/salary_apply_detail.html', a=a, me=me,
                           can_approve=can_approve, can_pay=can_pay)


@bp.route('/oa/apply/salary/<int:aid>/decide', methods=['POST'])
@login_required
@permission_required('approval.approve')
def salary_apply_decide(aid):
    """薪酬发放申请审批：通过 / 驳回。"""
    a = Approval.query.filter_by(id=aid, type='薪酬发放').first_or_404()
    action = (request.form.get('action') or '').strip()
    if a.status != 'pending':
        flash('该申请已处理，不能重复审批')
        return redirect(url_for('oa.salary_apply_detail', aid=a.id))
    if action not in ('approve', 'reject'):
        flash('无效的审批动作')
        return redirect(url_for('oa.salary_apply_detail', aid=a.id))
    a.status = 'approved' if action == 'approve' else 'rejected'
    a.approver_id = session.get('user_id')
    a.opinion = (request.form.get('opinion') or '').strip()
    a.decided_at = datetime.now()
    db.session.commit()
    log_audit('OA-薪酬发放审批', module='oa', target_type='approval', target_no=a.code,
              detail='%s → %s' % (a.code, a.status))
    flash('已%s《%s》' % ('通过' if action == 'approve' else '驳回', a.code))
    return redirect(url_for('oa.salary_apply_detail', aid=a.id))


@bp.route('/oa/apply/salary/<int:aid>/attach', methods=['GET'])
@login_required
def salary_attach(aid):
    """附件下载：走鉴权（薪酬数据敏感，不放到 /static 直接暴露）。"""
    a = Approval.query.filter_by(id=aid, type='薪酬发放').first_or_404()
    if not a.attach_file:
        flash('该申请没有附件')
        return redirect(url_for('oa.salary_apply_detail', aid=a.id))
    folder = os.path.join(current_app.config['UPLOAD_DIR'], 'salary')
    return send_from_directory(folder, a.attach_file, as_attachment=True)


# ===================== OA · 费用申请与报销（双轨 + 关联闭环） =====================
# 两条通道，都汇入同一套审批与财务付款：
#   ① 直接报销  —— 员工垫付后凭票直接出「报销单」（mode=direct），无前置申请；
#   ② 先申请后报销 —— 先出「费用申请单」获批额度，实际发生后出「报销单」并关联该申请编号，
#      报销额 ≤ 批准余额放行、超出需写理由（over_reason），审批通过后回写申请单已核销额，
#      核销满额自动置「已核销」→ 形成合规闭环。
# 审批权限 oa.expense.approve（S/A/B/C），付款确认 oa.expense.pay（S/A/B）。

def _me_info():
    """当前登录人的单据身份信息（姓名 / 工号 / 部门）。B 模式：登录名 == 工号。"""
    uname = session.get('username', '') or ''
    emp = Employee.query.filter_by(employee_no=uname).first()
    name = (emp.name if emp else None) or uname
    org_name = emp.org.name if (emp and emp.org) else ''
    return {'id': session.get('user_id'), 'name': name,
            'employee_no': uname, 'org_name': org_name}


def _can(code):
    """当前用户是否具备指定权限码（与 permission_required 同口径，用于模板显隐）。"""
    u = User.query.get(session.get('user_id'))
    return bool(u and u.level and any(p.code == code for p in u.level.permissions))


def _next_expense_code(model, prefix):
    """生成当月单据号：前缀-YYYYMM-NNN（按当月已有最大序号 +1）。"""
    ym = datetime.now().strftime('%Y%m')
    last = (model.query.filter(model.code.like('%s-%s-%%' % (prefix, ym)))
            .order_by(model.code.desc()).first())
    seq = 1
    if last and last.code:
        try:
            seq = int(last.code.rsplit('-', 1)[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return '%s-%s-%03d' % (prefix, ym, seq)


def _to_money(v):
    """宽松解析金额：容忍千分位逗号与空格；非法或为空返回 0。"""
    try:
        return round(float(str(v).replace(',', '').strip()), 2)
    except (TypeError, ValueError):
        return 0.0


_CN_DIGIT = '零壹贰叁肆伍陆柒捌玖'
_CN_UNIT4 = ['', '拾', '佰', '仟']
_CN_GROUP = ['', '万', '亿', '万亿']


def _amount_cn(num):
    """金额转人民币大写（打印单据用）：2860.00 -> 贰仟捌佰陆拾元整。"""
    try:
        num = round(float(num or 0), 2)
    except (TypeError, ValueError):
        return ''
    if num < 0:
        return '负' + _amount_cn(-num)
    yuan = int(num)
    jiao, fen = divmod(int(round((num - yuan) * 100)), 10)

    def _four(n):
        """0~9999 → 中文（段内零合并），如 1005 -> 壹仟零伍、1205 -> 壹仟贰佰零伍。"""
        s, zero = '', False
        for i in range(3, -1, -1):
            d = (n // (10 ** i)) % 10
            if d == 0:
                if s:
                    zero = True
            else:
                if zero:
                    s += _CN_DIGIT[0]
                    zero = False
                s += _CN_DIGIT[d] + _CN_UNIT4[i]
        return s

    if yuan == 0:
        head = '零元'
    else:
        groups, n, gi, need_zero = [], yuan, 0, False
        while n > 0:
            g = n % 10000
            n //= 10000
            if g == 0:
                need_zero = True
            else:
                if need_zero and groups:          # 低位段不足四位 → 高位段前补零
                    groups[0] = _CN_DIGIT[0] + groups[0]
                groups.insert(0, _four(g) + _CN_GROUP[gi])
                need_zero = g < 1000
            gi += 1
        head = ''.join(groups).strip(_CN_DIGIT[0]) + '元'

    if jiao == 0 and fen == 0:
        return head + '整'
    tail = ''
    if jiao:
        tail += _CN_DIGIT[jiao] + '角'
    elif fen:
        tail += _CN_DIGIT[0]
    if fen:
        tail += _CN_DIGIT[fen] + '分'
    return head + tail


def _expense_overview():
    """费用中心所需数据：我相关的单据 + 待我审批（按金额、时间倒序）。"""
    me = _me_info()
    can_approve = _can('oa.expense.approve')
    can_pay = _can('oa.expense.pay')
    if can_approve:
        claims = ExpenseClaim.query.order_by(ExpenseClaim.created_at.desc()).all()
        reimbs = ExpenseReimburse.query.order_by(ExpenseReimburse.created_at.desc()).all()
        todo_claims = ExpenseClaim.query.filter_by(status='pending')\
            .order_by(ExpenseClaim.created_at.desc()).all()
        todo_reimbs = ExpenseReimburse.query.filter_by(status='pending')\
            .order_by(ExpenseReimburse.created_at.desc()).all()
    else:
        claims = ExpenseClaim.query.filter_by(applicant_id=me['id'])\
            .order_by(ExpenseClaim.created_at.desc()).all()
        reimbs = ExpenseReimburse.query.filter_by(applicant_id=me['id'])\
            .order_by(ExpenseReimburse.created_at.desc()).all()
        todo_claims, todo_reimbs = [], []
    return me, can_approve, can_pay, claims, reimbs, todo_claims, todo_reimbs


@bp.route('/oa/expenses', methods=['GET'])
@login_required
def expenses():
    """【兼容入口】费用中心已并入「申请中心」（oa.apply_hub）。
    旧书签 / 旧链接访问这里时直接跳转到申请中心，不再单独渲染费用页。"""
    return redirect(url_for('oa.apply_hub'))


# ---------- 费用申请单（先申请后报销用的「额度批准」） ----------

@bp.route('/oa/expenses/claim/new', methods=['GET', 'POST'])
@login_required
def expense_claim_new():
    """新建费用申请单：提交后进入审批，批准后可在报销时被关联。"""
    me = _me_info()
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip()
        if category not in EXPENSE_CATEGORIES:
            category = '其他'
        amount = _to_money(request.form.get('amount'))
        purpose = (request.form.get('purpose') or '').strip()
        if amount <= 0:
            flash('申请金额必须大于 0')
            return redirect(url_for('oa.expense_claim_new'))
        if not purpose:
            flash('请填写事由说明')
            return redirect(url_for('oa.expense_claim_new'))
        c = ExpenseClaim(
            code=_next_expense_code(ExpenseClaim, 'SQB'),
            applicant_id=me['id'], applicant_name=me['name'],
            employee_no=me['employee_no'], org_name=me['org_name'],
            category=category, amount=amount, used_amount=0,
            period_start=(request.form.get('period_start') or '').strip(),
            period_end=(request.form.get('period_end') or '').strip(),
            pay_mode=('prepay' if request.form.get('pay_mode') == 'prepay' else 'self'),
            purpose=purpose, status='pending')
        db.session.add(c)
        db.session.commit()
        log_audit('add', module='oa', target_type='expense_claim', target_id=c.id,
                  target_no=c.code, detail='提交费用申请单 %s' % c.code)
        flash('费用申请单 %s 已提交，等待审批' % c.code)
        return redirect(url_for('oa.expense_claim_detail', cid=c.id))
    return render_template('oa/expense_claim_new.html', me=me,
                           categories=EXPENSE_CATEGORIES,
                           category_groups=EXPENSE_CATEGORY_GROUPS,
                           code_preview=_next_expense_code(ExpenseClaim, 'SQB'),
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/oa/expenses/claim/<int:cid>', methods=['GET'])
@login_required
def expense_claim_detail(cid):
    """费用申请单详情：含已关联的报销单（闭环可追）。"""
    c = ExpenseClaim.query.get_or_404(cid)
    me = _me_info()
    can_approve = _can('oa.expense.approve')
    if c.applicant_id != me['id'] and not can_approve:
        flash('无权限查看该申请单')
        return redirect(url_for('oa.expenses'))
    rels = ExpenseReimburse.query.filter_by(claim_id=c.id)\
        .order_by(ExpenseReimburse.created_at.desc()).all()
    return render_template('oa/expense_claim_detail.html', c=c, me=me,
                           can_approve=can_approve, rels=rels,
                           amount_cn=_amount_cn(c.amount))


@bp.route('/oa/expenses/claim/<int:cid>/decide', methods=['POST'])
@login_required
@permission_required('oa.expense.approve')
def expense_claim_decide(cid):
    """审批费用申请单：同意 / 驳回。仅 pending 可审。"""
    c = ExpenseClaim.query.get_or_404(cid)
    if c.status != 'pending':
        flash('该申请单已处理，不能重复审批')
        return redirect(url_for('oa.expense_claim_detail', cid=cid))
    action = request.form.get('action')
    if action not in ('approve', 'reject'):
        flash('无效的审批操作')
        return redirect(url_for('oa.expense_claim_detail', cid=cid))
    c.status = 'approved' if action == 'approve' else 'rejected'
    c.remark = (request.form.get('opinion') or '').strip()
    c.approver_name = _me_info()['name']
    c.decided_at = datetime.utcnow()
    db.session.commit()
    log_audit('update', module='oa', target_type='expense_claim', target_id=c.id,
              target_no=c.code, detail='费用申请单审批：%s' % c.status_cn)
    flash('已%s费用申请单 %s' % ('批准' if action == 'approve' else '驳回', c.code))
    return redirect(url_for('oa.expense_claim_detail', cid=cid))


# ---------- 费用报销单（双轨：直接报销 / 关联费用申请） ----------

# 电子发票附件：允许的扩展名与单文件大小上限（超限直接提示，不落盘）
INVOICE_EXT = {'.pdf', '.ofd', '.xml', '.jpg', '.jpeg', '.png', '.bmp', '.xls', '.xlsx', '.zip'}
INVOICE_MAX_BYTES = 10 * 1024 * 1024
# 进项抵扣常用票种（明细行「发票类型」下拉）
INVOICE_TYPES = ['增值税电子普票', '增值税专用发票', '增值税电子专票（数电票）',
                 '普通发票', '财政票据', '行程单 / 车票', '境外票据', '无票']


def _invoice_upload_dim():
    """返回 static/uploads/invoices 目录并确保存在。"""
    d = os.path.join(current_app.config['UPLOAD_DIR'], 'invoices')
    os.makedirs(d, exist_ok=True)
    return d


def _save_invoice_files(file_list, code):
    """保存报销单的电子发票附件，返回 [{name, file, size}] 列表。
    · name = 原始文件名（展示用，只留文件名不含路径，防穿越）
    · file = 落盘后的安全文件名 <单号>_<8位随机码><ext>
    返回 (list, error)：error 非空表示有文件被拒（类型/大小），调用方据此提示。"""
    saved, errs = [], []
    for fs in (file_list or []):
        if not fs or not fs.filename:
            continue
        raw = fs.filename
        ext = os.path.splitext(raw)[1].lower()
        if ext not in INVOICE_EXT:
            errs.append('%s：类型不支持' % raw)
            continue
        fs.stream.seek(0, os.SEEK_END)
        size = fs.stream.tell()
        fs.stream.seek(0)
        if size > INVOICE_MAX_BYTES:
            errs.append('%s：超过 10MB' % raw)
            continue
        fname = '%s_%s%s' % (code, uuid.uuid4().hex[:8], ext)
        fs.save(os.path.join(_invoice_upload_dim(), fname))
        saved.append({'name': os.path.basename(raw), 'file': fname, 'size': size})
    return saved, errs


def _reimb_attach_list(r):
    """读报销单的附件清单（容错：字段为空/坏 JSON 都返回 []）。"""
    if not r or not r.attach_files:
        return []
    try:
        lst = json.loads(r.attach_files)
        return lst if isinstance(lst, list) else []
    except (ValueError, TypeError):
        return []


def _set_reimb_attach(r, lst):
    """写回附件清单，并同步附件张数（有上传文件时以文件数为准）。"""
    r.attach_files = json.dumps(lst, ensure_ascii=False) if lst else None
    if lst:
        r.attachment_count = len(lst)


@bp.route('/oa/expenses/reimburse/new', methods=['GET', 'POST'])
@login_required
def expense_reimburse_new():
    """新建报销单：mode=direct 直接报销；mode=apply 必须关联一张已批准且仍有余额的申请单。"""
    me = _me_info()
    # 可关联的申请单：已批准且还有余额
    avail = [c for c in ExpenseClaim.query.filter_by(status='approved')
             .order_by(ExpenseClaim.created_at.desc()).all() if c.remain_amount > 0]
    if request.method == 'POST':
        category = (request.form.get('category') or '').strip()
        if category not in EXPENSE_CATEGORIES:
            category = '其他'
        mode = 'apply' if request.form.get('mode') == 'apply' else 'direct'
        claim, over_reason = None, ''
        if mode == 'apply':
            try:
                claim_id = int(request.form.get('claim_id') or 0)
            except (TypeError, ValueError):
                claim_id = 0
            claim = ExpenseClaim.query.get(claim_id) if claim_id else None
            if not claim or claim.status != 'approved':
                flash('请选择一张「已批准」的费用申请单后再提交')
                return redirect(url_for('oa.expense_reimburse_new'))

        # 明细行（平行数组，逐列取；空行/零金额行自动跳过）
        dates = request.form.getlist('item_date')
        cats = request.form.getlist('item_category')
        details = request.form.getlist('item_detail')
        amounts = request.form.getlist('item_amount')
        taxes = request.form.getlist('item_tax')
        itypes = request.form.getlist('item_invoice_type')
        inos = request.form.getlist('item_invoice_no')

        def _at(lst, i):
            return lst[i] if i < len(lst) else ''

        rows = []
        for i, raw in enumerate(details):
            amt = _to_money(_at(amounts, i))
            if amt <= 0:
                continue
            rows.append({
                'occur_date': _at(dates, i).strip(),
                'category': (_at(cats, i) or '其他').strip(),
                'detail': raw.strip(),
                'amount': amt,
                'tax': _to_money(_at(taxes, i)),
                'invoice_type': _at(itypes, i).strip(),
                'invoice_no': _at(inos, i).strip(),
            })
        if not rows:
            flash('请至少填写一行有效的费用明细（金额需大于 0）')
            return redirect(url_for('oa.expense_reimburse_new'))

        total = round(sum(r['amount'] for r in rows), 2)
        tax_total = round(sum(r['tax'] for r in rows), 2)

        # 闭环校验：关联申请时，超出批准余额必须写理由
        if claim and total > claim.remain_amount + 0.005:
            over_reason = (request.form.get('over_reason') or '').strip()
            if not over_reason:
                flash('报销金额 ￥%.2f 超出申请单 %s 的可用余额 ￥%.2f，请填写超额原因'
                      % (total, claim.code, claim.remain_amount))
                return redirect(url_for('oa.expense_reimburse_new'))

        r = ExpenseReimburse(
            code=_next_expense_code(ExpenseReimburse, 'BXD'),
            mode=mode, claim_id=(claim.id if claim else None),
            claim_code=(claim.code if claim else ''), over_reason=over_reason,
            applicant_id=me['id'], applicant_name=me['name'],
            employee_no=me['employee_no'], org_name=me['org_name'],
            category=category, amount=total, tax_amount=tax_total,
            purpose=(request.form.get('purpose') or '').strip(),
            pay_method=(request.form.get('pay_method') or '银行转账（对私）').strip(),
            payee_name=(request.form.get('payee_name') or '').strip(),
            bank_name=(request.form.get('bank_name') or '').strip(),
            bank_account=(request.form.get('bank_account') or '').strip(),
            status='pending')
        db.session.add(r)
        db.session.flush()
        # 电子发票：新建时一并上传（也可提交后在详情页补传）
        inv_files, inv_errs = _save_invoice_files(request.files.getlist('invoices'), r.code)
        if inv_files:
            _set_reimb_attach(r, inv_files)
        for i, row in enumerate(rows, 1):
            db.session.add(ExpenseReimburseItem(reimburse_id=r.id, sort=i, **row))
        db.session.commit()
        for e in inv_errs:
            flash('附件未保存：' + e)
        log_audit('add', module='oa', target_type='expense_reimburse', target_id=r.id,
                  target_no=r.code, detail='提交费用报销单 %s（%s，附件 %d 个）'
                  % (r.code, r.mode_cn, len(inv_files)))
        flash('报销单 %s 已提交，等待审批' % r.code)
        if inv_files:
            flash('已上传 %d 个电子发票附件' % len(inv_files))
        return redirect(url_for('oa.expense_reimburse_detail', rid=r.id))

    claim_json = [{'id': c.id, 'code': c.code, 'category': c.category,
                   'amount': '%.2f' % (c.amount or 0),
                   'used': '%.2f' % (c.used_amount or 0),
                   'remain': '%.2f' % c.remain_amount,
                   'purpose': c.purpose or '',
                   'period': '%s ~ %s' % (c.period_start or '—', c.period_end or '—'),
                   'approver': c.approver_name or ''} for c in avail]
    return render_template('oa/expense_reimburse_new.html', me=me, avail=avail,
                           claim_json=claim_json, categories=EXPENSE_CATEGORIES,
                           category_groups=EXPENSE_CATEGORY_GROUPS,
                           code_preview=_next_expense_code(ExpenseReimburse, 'BXD'),
                           today=datetime.now().strftime('%Y-%m-%d'))


@bp.route('/oa/expenses/reimburse/<int:rid>', methods=['GET'])
@login_required
def expense_reimburse_detail(rid):
    """报销单详情：单据全貌 + 明细 + 审批操作 + 财务衔接。"""
    r = ExpenseReimburse.query.get_or_404(rid)
    me = _me_info()
    can_approve = _can('oa.expense.approve')
    can_pay = _can('oa.expense.pay')
    if r.applicant_id != me['id'] and not can_approve:
        flash('无权限查看该报销单')
        return redirect(url_for('oa.expenses'))
    claim = ExpenseClaim.query.get(r.claim_id) if r.claim_id else None
    return render_template('oa/expense_reimburse_detail.html', r=r, me=me, claim=claim,
                           can_approve=can_approve, can_pay=can_pay,
                           inv_files=_reimb_attach_list(r),
                           amount_cn=_amount_cn(r.amount),
                           claim_cn=_amount_cn(claim.amount) if claim else '')


@bp.route('/oa/expenses/reimburse/<int:rid>/decide', methods=['POST'])
@login_required
@permission_required('oa.expense.approve')
def expense_reimburse_decide(rid):
    """审批报销单：同意（写记账期间并核销申请余额）/ 驳回 / 退回修改。"""
    r = ExpenseReimburse.query.get_or_404(rid)
    if r.status != 'pending':
        flash('该报销单已处理，不能重复审批')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    action = request.form.get('action')
    if action not in ('approve', 'reject', 'return'):
        flash('无效的审批操作')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    r.remark = (request.form.get('opinion') or '').strip()
    r.approver_name = _me_info()['name']
    r.decided_at = datetime.utcnow()
    if action == 'approve':
        r.status = 'approved'
        r.period = datetime.now().strftime('%Y-%m')   # 供财务自动记账按期间取数
        # 闭环核销：把本单金额累加到申请单已用额；核销满额则关闭该申请单
        if r.claim_id:
            cl = ExpenseClaim.query.get(r.claim_id)
            if cl:
                cl.used_amount = round((cl.used_amount or 0) + (r.amount or 0), 2)
                if cl.used_amount >= (cl.amount or 0) - 0.005:
                    cl.status = 'closed'
    elif action == 'reject':
        r.status = 'rejected'
    else:
        r.status = 'returned'
    db.session.commit()
    log_audit('update', module='oa', target_type='expense_reimburse', target_id=r.id,
              target_no=r.code, detail='报销单审批：%s' % r.status_cn)
    flash('报销单 %s 已%s' % (r.code, r.status_cn))
    return redirect(url_for('oa.expense_reimburse_detail', rid=rid))


@bp.route('/oa/expenses/reimburse/<int:rid>/pay', methods=['POST'])
@login_required
@permission_required('oa.expense.pay')
def expense_reimburse_pay(rid):
    """确认付款：仅「已通过」的报销单可置为已付款。"""
    r = ExpenseReimburse.query.get_or_404(rid)
    if r.status != 'approved':
        flash('只有「已通过」的报销单才能确认付款')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    r.status = 'paid'
    db.session.commit()
    log_audit('update', module='oa', target_type='expense_reimburse', target_id=r.id,
              target_no=r.code, detail='报销单确认付款')
    flash('报销单 %s 已确认付款' % r.code)
    return redirect(url_for('oa.expense_reimburse_detail', rid=rid))


# ---------- 电子发票上传 / 下载 / 删除（报销单的票据电子化） ----------

def _reimb_guard(rid):
    """报销单附件的访问闸门：申请人本人或有审批权限者可操作。返回 (单据, 错误响应)。"""
    r = ExpenseReimburse.query.get_or_404(rid)
    me = _me_info()
    if r.applicant_id != me['id'] and not _can('oa.expense.approve'):
        flash('无权限操作该报销单的附件')
        return None, redirect(url_for('oa.expenses'))
    return r, None


@bp.route('/oa/expenses/reimburse/<int:rid>/files/upload', methods=['POST'])
@login_required
def reimburse_invoice_upload(rid):
    """上传电子发票（支持多选）。已审批通过/付款后仍可补传，便于财务留档。"""
    r, err = _reimb_guard(rid)
    if err:
        return err
    files, errs = _save_invoice_files(request.files.getlist('invoices'), r.code)
    if not files:
        flash('没有可保存的文件' + ('（%s）' % '；'.join(errs) if errs else '，请先选择文件'))
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    lst = _reimb_attach_list(r) + files
    _set_reimb_attach(r, lst)
    db.session.commit()
    log_audit('update', module='oa', target_type='expense_reimburse', target_id=r.id,
              target_no=r.code, detail='上传电子发票 %d 个' % len(files))
    for e in errs:
        flash('附件未保存：' + e)
    flash('已上传 %d 个电子发票，当前共 %d 个' % (len(files), len(lst)))
    return redirect(url_for('oa.expense_reimburse_detail', rid=rid))


@bp.route('/oa/expenses/reimburse/<int:rid>/files/<int:idx>', methods=['GET'])
@login_required
def reimburse_invoice_file(rid, idx):
    """下载/预览某个电子发票附件（走登录鉴权，不暴露在 /static）。"""
    r, err = _reimb_guard(rid)
    if err:
        return err
    lst = _reimb_attach_list(r)
    if idx < 0 or idx >= len(lst):
        flash('附件不存在')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    item = lst[idx]
    return send_from_directory(_invoice_upload_dim(), item.get('file', ''),
                               as_attachment=True, download_name=item.get('name') or item.get('file'))


@bp.route('/oa/expenses/reimburse/<int:rid>/files/<int:idx>/delete', methods=['POST'])
@login_required
def reimburse_invoice_delete(rid, idx):
    """删除某个电子发票附件（仅未付款前可删，避免动了已留档的凭证）。"""
    r, err = _reimb_guard(rid)
    if err:
        return err
    if r.status == 'paid':
        flash('该报销单已付款，附件已留档，不能删除')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    lst = _reimb_attach_list(r)
    if idx < 0 or idx >= len(lst):
        flash('附件不存在')
        return redirect(url_for('oa.expense_reimburse_detail', rid=rid))
    item = lst.pop(idx)
    # 落盘文件同步删掉（只在本单目录内删，文件名由系统生成，不含路径）
    try:
        p = os.path.join(_invoice_upload_dim(), item.get('file', ''))
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
    _set_reimb_attach(r, lst)
    if not lst:
        r.attachment_count = 0
    db.session.commit()
    log_audit('update', module='oa', target_type='expense_reimburse', target_id=r.id,
              target_no=r.code, detail='删除电子发票附件 %s' % (item.get('name') or ''))
    flash('附件已删除')
    return redirect(url_for('oa.expense_reimburse_detail', rid=rid))


@bp.route('/oa/expenses/reimburse/<int:rid>/print', methods=['GET'])
@login_required
def expense_print(rid):
    """打印单据：费用申请单（如有）+ 费用报销单 + 票据附件封面，共 1~3 页 A4。"""
    r = ExpenseReimburse.query.get_or_404(rid)
    me = _me_info()
    can_approve = _can('oa.expense.approve')
    if r.applicant_id != me['id'] and not can_approve:
        flash('无权限打印该单据')
        return redirect(url_for('oa.expenses'))
    claim = ExpenseClaim.query.get(r.claim_id) if r.claim_id else None
    pages = (2 if claim else 1) + 1   # 申请单(可选) + 报销单 + 附件封面
    return render_template('oa/expense_print.html', r=r, claim=claim,
                           amount_cn=_amount_cn(r.amount),
                           claim_cn=_amount_cn(claim.amount) if claim else '',
                           pages=pages)
