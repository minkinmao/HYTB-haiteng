# models.py
# 数据模型：RBAC（用户/角色/权限） + 主数据（组织/渠道/产品）
# 这是"地基型"首期增量的核心：先把权限与主数据这两块底座建好。
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

# ---------- 一、RBAC 权限体系（按职级分层） ----------
# 职级与权限是多对多关系，用一张中间表来关联 —— 这就是"权限流"的载体
level_permission = db.Table(
    'level_permission',
    db.Column('level_id', db.Integer, db.ForeignKey('level.id'), primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permission.id'), primary_key=True),
)


class Permission(db.Model):
    """权限：系统中最小的可授权操作，如 'data.view_self'（看自己）"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), comment='权限名称')
    code = db.Column(db.String(64), unique=True, comment='权限编码，代码里用它来判断')


class Level(db.Model):
    """人员职级：权限分配的最小单位（S/A/B/C/D/E）。
    IT 管理员后台可为每个职级匹配一组权限（权限流）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, comment='职级代码：S/A/B/C/D/E')
    name = db.Column(db.String(32), comment='职级名称')
    titles = db.Column(db.String(128), comment='对应职务，如 总经理/老板')
    # 一个职级拥有多组权限（通过中间表关联），就是它的"权限流"
    permissions = db.relationship('Permission', secondary=level_permission, backref='levels')


class User(db.Model):
    """用户：公司员工账号"""
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, comment='登录账号')
    password_hash = db.Column(db.String(128), comment='密码哈希（不存明文）')
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), comment='所属职级')
    level = db.relationship('Level', backref='users')
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), comment='所属组织/团队')
    # 企业微信 userid：在 IT 用户管理页绑定后，员工在企业微信里点应用可免密自动登录
    wecom_userid = db.Column(db.String(64), default='', comment='企业微信 userid（绑定后免密登录）')
    # 是否系统管理员：拥有全部二级菜单模块权限（用户级模块授权的豁免标识）。
    # 与职级权限流(level_permission)是两条独立维度：职级决定功能操作权限，
    # 本标识决定用户能看见/进入哪些二级菜单（UserModulePerm）。
    is_admin = db.Column(db.Boolean, default=False, comment='是否系统管理员（拥有全部模块权限）')
    # 首次登录强制改密码：初始弱口令（如 admin/123456）账号置 True，登录后必须改密码，改完置 False
    must_change_password = db.Column(db.Boolean, default=False, nullable=False,
                                     comment='首次登录是否必须改密码（初始弱口令账号）')
    # 登录用姓名拼音：允许员工用「姓名拼音」登录（与工号 username 二选一，可空）
    pinyin = db.Column(db.String(64), default='', comment='登录用姓名拼音（可空，填后可用拼音登录）')


# ---------- 一之一、用户·二级菜单模块权限（用户级授权） ----------
class UserModulePerm(db.Model):
    """用户·二级菜单模块权限：直接给用户开通可访问的模块（粒度到二级菜单）。

    与职级权限流(level_permission)是两条独立维度：
    - 职级决定"功能操作权限"（permission_required 用 level.permissions 判断）；
    - 本表决定"用户能看见/进入哪些二级菜单"（左侧菜单过滤 + 路由拦截）。
    menu_key 存模块端点，如 'oa.mails'（与 SYSTEMS 里 module 的 url 一一对应）。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, comment='用户')
    menu_key = db.Column(db.String(64), nullable=False, comment='模块端点，如 oa.mails')
    __table_args__ = (db.UniqueConstraint('user_id', 'menu_key', name='uq_user_module'),)
    user = db.relationship('User', backref='module_perms')


# ---------- 二、主数据（其它模块都要挂靠的对象） ----------
class Organization(db.Model):
    """组织/团队：用 parent_id 自关联，构成公司->团队的树形结构"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), comment='组织/团队名称')
    parent_id = db.Column(db.Integer, db.ForeignKey('organization.id'), nullable=True)
    type = db.Column(db.String(32), comment='类型：company 公司 / team 团队')
    # 编制人数（#145 第4项 部门架构设置）：0 表示未设编制，不参与超编判断
    headcount = db.Column(db.Integer, default=0, comment='编制人数（0=未设编制）')


class Channel(db.Model):
    """渠道：保险销售渠道，如 个险 / 银保 / 网销"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), comment='渠道名称')
    company = db.Column(db.String(64), comment='所属保司（可选）')


class Product(db.Model):
    """产品：在售保险产品（后续产品合约管理系统会用到）"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), comment='产品名称')
    company = db.Column(db.String(64), comment='承保保司')
    status = db.Column(db.String(16), default='on', comment='状态：on 在售 / off 停售')


class Announcement(db.Model):
    """OA 公告公示：公司内部通知 + 未来官网对外发布（新闻/动态）统一端口。
    scope 区分：internal 内部公告（OA 展示）/ website 官网发布（未来对接公司官网）。"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), comment='标题')
    content = db.Column(db.Text, comment='正文')
    is_pinned = db.Column(db.Boolean, default=False, comment='是否置顶')
    # 发布范围：internal 内部公告 / website 官网发布（新闻、动态等，未来对接官网）
    scope = db.Column(db.String(16), default='internal', comment='发布范围：internal 内部 / website 官网')
    publisher_id = db.Column(db.Integer, db.ForeignKey('user.id'), comment='发布人ID')
    # 关联发布人，列表页可直接取发布人姓名
    publisher = db.relationship('User', backref='announcements')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='发布时间')


class Approval(db.Model):
    """OA 审批：员工提交申请，管理人员审批（先做通用审批，后续可细分类型）"""
    id = db.Column(db.Integer, primary_key=True)
    # 申请单号（2026-10-01 加）：前缀-YYYYMM-NNN，如 BX-202610-001；
    # 前缀由「申请类型目录」APPLY_CATALOG(oa.py) 决定，同类单据同一套连续编号
    code = db.Column(db.String(32), comment='申请单号：前缀-YYYYMM-NNN')
    type = db.Column(db.String(32), default='通用', comment='审批类型：请假/报销/通用')
    title = db.Column(db.String(200), comment='标题')
    content = db.Column(db.Text, comment='申请说明')
    status = db.Column(db.String(16), default='pending', comment='状态：pending 待审批 / approved 通过 / rejected 驳回')
    applicant_id = db.Column(db.Integer, db.ForeignKey('user.id'), comment='申请人ID')
    # 一张表两次关联 User，必须用 foreign_keys 区分，否则 SQLAlchemy 报错
    applicant = db.relationship('User', foreign_keys=[applicant_id], backref='applications')
    approver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, comment='审批人ID')
    approver = db.relationship('User', foreign_keys=[approver_id], backref='approvals')
    opinion = db.Column(db.Text, comment='审批意见')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='提交时间')
    decided_at = db.Column(db.DateTime, nullable=True, comment='审批时间')
    # —— 报销审批单（#38 自动记账用）：仅 type='报销' 时填写金额与费用科目 ——
    amount = db.Column(db.Float, default=0, comment='报销金额（元），type=报销 时用')
    expense_account = db.Column(db.String(32), default='560202', comment='费用科目编码（报销用，默认 560202 办公费）')
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填）')
    # —— 差勤申请结构化字段（2026-10-01 加）：仅 type in 请假/出差/公出/补卡 时填写 ——
    # 说明：内容仍会汇总进 content 供列表/详情直接读；这几个列让「时长/地点/出行方式」
    #       可查询可统计（后续出差补贴、差勤报表都靠它们）。
    start_time = db.Column(db.String(20), comment='差勤开始时间 YYYY-MM-DD HH:MM')
    end_time = db.Column(db.String(20), comment='差勤结束时间 YYYY-MM-DD HH:MM')
    att_days = db.Column(db.Float, default=0, comment='时长：请假/出差按天（8 小时=1 天），事假按小时')
    place = db.Column(db.String(128), comment='出差/公出地点')
    transport = db.Column(db.String(32), comment='出行方式：高铁/飞机/自驾/大巴/其他')
    sub_type = db.Column(db.String(32), comment='子类：请假类别（年假/事假…）或补卡时段（上班卡/下班卡）')
    # —— 薪酬发放申请（2026-10-01 按威哥公司表单《薪酬发放申请单》加）：仅 type='薪酬发放' 时填写 ——
    # 口径说明：应发薪酬=税前应发；绩效合计=应发中绩效部分；实发金额=实际打款；
    #   薪酬成本=应发薪酬+单位社保+单位公积金（企业为薪酬付出的直接成本，可手改）；
    #   人工成本=薪酬成本（本单口径下相同，如另有其他人工类费用可手工调增）。
    sa_month = db.Column(db.String(16), comment='薪酬月份 YYYY-MM')
    pay_entity = db.Column(db.String(64), comment='发薪单位')
    headcount = db.Column(db.Integer, default=0, comment='发薪人数')
    gross_amount = db.Column(db.Float, default=0, comment='应发薪酬')
    perf_amount = db.Column(db.Float, default=0, comment='绩效合计')
    actual_amount = db.Column(db.Float, default=0, comment='实发金额')
    corp_social = db.Column(db.Float, default=0, comment='单位社保')
    corp_fund = db.Column(db.Float, default=0, comment='单位公积金')
    salary_cost = db.Column(db.Float, default=0, comment='薪酬成本')
    labor_cost = db.Column(db.Float, default=0, comment='人工成本')
    attach_file = db.Column(db.String(255), comment='附件文件名（存 static/uploads/salary/ 下，页面走鉴权下载）')
    related_code = db.Column(db.String(200), comment='关联申请单号（逗号分隔）')
    # —— 通用附件（2026-10-01 加）：休假申请的「病例 / 结婚证 / 生育证明」等 ——
    # 存 JSON 数组字符串：[{"name": 原文件名, "file": 落盘文件名, "size": 字节数}, …]
    # 与费用报销单的电子发票同一套机制（oa.py 的 _reimb_attach_list / _save_invoice_files 同款）。
    attach_files = db.Column(db.Text, comment='通用附件（JSON 数组：[{name,file,size}…]）')

class Employee(db.Model):
    """人事系统 · 员工花名册：公司全员档案。
    与登录账号 User 区分：User 是内部管理人员登录用，Employee 是 HR 档案（含非系统用户的营销类人员）。
    职级(level) 与 部门(org) 直接复用已有的 Level / Organization 模型 —— 不重复造轮子。"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), comment='姓名')
    employee_no = db.Column(db.String(32), unique=True, comment='工号')
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), comment='职级（复用 Level 模型）')
    level = db.relationship('Level', backref='employees')
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), comment='所属部门/团队（复用 Organization 模型）')
    org = db.relationship('Organization', backref='employees')
    title = db.Column(db.String(64), comment='职务')
    phone = db.Column(db.String(20), comment='手机号')
    hire_date = db.Column(db.Date, comment='入职日期')
    # 入职照片：存相对 static 的路径（如 uploads/employees/emp7_ab12cd34.jpg），系统总览员工工卡展示
    photo = db.Column(db.String(255), comment='入职照片路径（相对 static 目录）')
    status = db.Column(db.String(16), default='active', comment='状态：active 在职 / left 离职')
    # 用工分类（地基级）：两类员工签不同合同，后续合同档案/工资/导入均据此分支
    #   内勤 internal = 签劳动合同，含 S/A/B/C/D/E 职级（总经理/财务/营销/行政/系统等）
    #   外勤 external = 签代理合同，为销售渠道人员（渠道总/主管/代理人），不挂职级
    staff_type = db.Column(db.String(16), default='internal',
                           comment='用工类型：internal 内勤(劳动合同) / external 外勤(代理合同)')
    # 外勤专用：代理类型（渠道总/主管/代理人）；内勤此字段为空
    agent_type = db.Column(db.String(16), comment='外勤代理类型：渠道总/主管/代理人（仅外勤填写）')
    # 团队归属（渠道总/主管 管理下属代理人，空=无下属）
    #   渠道总：sub_team_ids 存所有主管 ID（逗号分隔字符串）
    #   主管：   sub_team_ids 存所有代理人 ID
    #   代理人：sub_team_ids 留空
    sub_team_ids = db.Column(db.Text, default='', comment='所属或管理的下属员工ID列表（逗号分隔，仅外勤代理链条有效）')
    # ===== 个人档案扩展字段（入职登记流程：个人填报后由主管确认入册）=====
    # 参照人事卡片格式：联系信息 / 基本信息；花名册详情页只读展示
    gender = db.Column(db.String(8), comment='性别：男/女')
    birth_date = db.Column(db.Date, comment='出生日期')
    idcard = db.Column(db.String(18), comment='身份证号码（入册时从入职登记带入，人事卡片展示）')
    email = db.Column(db.String(64), comment='公司邮箱')
    work_start_date = db.Column(db.Date, comment='参加工作日期')
    marital = db.Column(db.String(16), comment='婚姻状况：已婚/未婚/保密等')
    children = db.Column(db.String(8), comment='是否有子女：是/否')
    hometown = db.Column(db.String(64), comment='籍贯')
    hobby = db.Column(db.String(128), comment='兴趣爱好')
    last_job = db.Column(db.String(64), comment='最后工作日（上一家单位）')
    # 离职信息（在职→离职时登记，离职人员/内部人才库展示）
    leave_date = db.Column(db.Date, comment='离职日期')
    leave_reason = db.Column(db.String(200), comment='离职原因')
    # ===== 个人电子信息（员工登录"我的资料"自助完善，HR 开通账号后待补全）=====
    address = db.Column(db.String(200), comment='现居住址')
    emergency_name = db.Column(db.String(32), comment='紧急联系人姓名')
    emergency_phone = db.Column(db.String(20), comment='紧急联系人电话')
    bank_name = db.Column(db.String(64), comment='工资卡开户行（发薪用）')
    bank_account = db.Column(db.String(32), comment='工资卡账号')
    # 待完善个人电子信息标记：HR 开通办公账号后置 True，员工登录填写"我的资料"后置 False
    need_profile = db.Column(db.Boolean, default=False, comment='待完善个人电子信息（True=待员工补全）')


class Commission(db.Model):
    """佣金：销售渠道人员（外勤）的销售业绩报酬，与工资清单（内勤）本质不同。
    归属/查看权限由 agent_type + sub_team_ids 三层决定：
    - 管理员/营销负责人：全量（commission.admin）
    - 渠道总：本渠道下全部人员
    - 主管：本主管下全部人员
    - 代理人：仅本人"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='销售人员ID（外勤）')
    employee = db.relationship('Employee', backref='commissions')
    month = db.Column(db.String(7), comment='归属月份，如 2026-08')
    product_name = db.Column(db.String(128), comment='产品名称')
    channel_name = db.Column(db.String(64), comment='渠道名称')
    premium = db.Column(db.Float, default=0, comment='保费（元）')
    commission_rate = db.Column(db.Float, default=0, comment='佣金率（%）')
    commission_amount = db.Column(db.Float, default=0, comment='佣金金额（元）= 保费 * 佣金率')
    status = db.Column(db.String(16), default='pending', comment='状态：pending 待确认 / confirmed 已确认')
    note = db.Column(db.Text, comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')
    # 自动记账（#38）：生成的凭证 id，用于反查与防重复生成
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填）')

class Bonus(db.Model):
    """奖金池：按季度计算，用于渠道/主管 额外奖励发放。
    分配由营销负责人在后台完成，代理人看不到分配明细（只查自己的佣金）。"""
    id = db.Column(db.Integer, primary_key=True)
    quarter = db.Column(db.String(8), comment='季度，如 2026-Q2')
    name = db.Column(db.String(64), comment='奖金名称，如 季度渠道奖励')
    total_amount = db.Column(db.Float, default=0, comment='奖金池总额（元）')
    note = db.Column(db.Text, comment='说明')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')
    # 自动记账（#38）：按奖金池汇总生成一张凭证，分配明细共享此凭证 id
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填）')

class BonusAllocation(db.Model):
    """奖金分配：奖金池 -> 销售人员 的分配明细。
    分配人需有 commission.admin 权限。"""
    id = db.Column(db.Integer, primary_key=True)
    bonus_id = db.Column(db.Integer, db.ForeignKey('bonus.id'), comment='奖金池ID')
    bonus = db.relationship('Bonus', backref='allocations')
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='被分配人（外勤）')
    employee = db.relationship('Employee', backref='bonus_allocations')
    amount = db.Column(db.Float, default=0, comment='分配金额（元）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='分配时间')
    # 自动记账（#38）：分配明细共享奖金池的凭证 id，用于反查与防重复生成
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填）')


class Salary(db.Model):
    """人事系统 · 工资清单：仅用于公司内部员工的工资（与销售渠道人员的"佣金"本质不同，佣金走小程序/营销系统）。
    关联到 Employee（HR 全员档案中的内部员工）。标准字段先行，HR 后续可在后台扩展自定义字段。"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID（关联 Employee 档案）')
    employee = db.relationship('Employee', backref='salaries')
    month = db.Column(db.String(7), comment='发放月份，如 2026-08')
    base_salary = db.Column(db.Float, default=0, comment='基本薪资')
    position_salary = db.Column(db.Float, default=0, comment='岗位工资')
    performance_salary = db.Column(db.Float, default=0, comment='绩效薪资')
    subsidy = db.Column(db.Float, default=0, comment='补贴')
    pension = db.Column(db.Float, default=0, comment='养老保险（个人缴纳，扣减项）')
    medical = db.Column(db.Float, default=0, comment='医疗保险（个人缴纳，扣减项）')
    housing_fund = db.Column(db.Float, default=0, comment='住房公积金（个人缴纳，扣减项）')
    note = db.Column(db.Text, comment='备注')
    # 自定义工资项：HR 后台配置的额外工资项（如 交通补贴），以 {code: 数值} 形式存储，避免频繁改表结构
    custom = db.Column(db.JSON, comment='自定义工资项（HR 后台配置），存储 {code: 数值}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')
    # 自动记账（#38）：生成的凭证 id，用于反查与防重复生成
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填）')

class Contract(db.Model):
    """人事系统 · 员工合同档案：内勤签劳动合同，外勤签代理合同（两类合同本质不同，分区管理）。"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID（关联 Employee 档案）')
    employee = db.relationship('Employee', backref='contracts')
    # 合同类型由员工用工类型推导：内勤=劳动合同(labor)，外勤=代理合同(agency)
    contract_type = db.Column(db.String(16), comment='合同类型：labor 劳动合同 / agency 代理合同')
    contract_no = db.Column(db.String(64), comment='合同编号')
    start_date = db.Column(db.Date, comment='合同起始日')
    end_date = db.Column(db.Date, comment='合同到期日')
    status = db.Column(db.String(16), default='active', comment='状态：active 生效中 / expired 已到期')
    file_path = db.Column(db.String(255), comment='合同文件存放路径/备注（备份用）')
    # 电子版合同：上传后的真实文件名（保存在 static/uploads/contracts/ 下）
    file_name = db.Column(db.String(255), comment='电子版合同文件名（上传备份）')
    note = db.Column(db.Text, comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AttendanceRecord(db.Model):
    """EHR · 出勤记录：员工每日出勤情况（由 CSV 导入，供每日出勤查询）。
    与差勤申请（请假/出差/补卡审批单）区分：这是"结果数据"，不是审批流程。"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID（关联 Employee 档案）')
    employee = db.relationship('Employee', backref='attendance_records')
    work_date = db.Column(db.Date, comment='出勤日期')
    # 状态：normal 正常 / late 迟到 / early 早退 / absent 旷工 / leave 请假
    status = db.Column(db.String(16), default='normal', comment='出勤状态：normal/late/early/absent/leave')
    check_in = db.Column(db.String(8), comment='上班打卡时间 HH:MM（可空）')
    check_out = db.Column(db.String(8), comment='下班打卡时间 HH:MM（可空）')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='导入时间')


class SalaryField(db.Model):
    """人事系统 · 工资自定义字段配置：HR 在后台增配的工资项（如 交通补贴/通讯补贴）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='字段编码，如 traffic_subsidy')
    name = db.Column(db.String(32), comment='字段名称，如 交通补贴')
    # 扣减项（True）会从应发中扣除，如 迟到扣款；非扣减项计入应发，如 交通补贴
    is_deduction = db.Column(db.Boolean, default=False, comment='是否扣减项（True 从应发扣除）')


class LeaveType(db.Model):
    """人事系统 · 假期类型与额度：人事管理员在后台配置假期类型及年度可请天数。
    休假申请（考勤服务）从这里选取假期类型，并按 total_days 校验/展示可请额度。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='假期类型编码，如 annual 年假')
    name = db.Column(db.String(32), comment='假期名称，如 年假')
    # 年度可请天数（额度）；设为 0 表示不限额度（如 病假凭证明），前端展示"不限"
    # 注意：只要该假期类型配了「分级档位」(LeaveTier)，就以档位算，本字段作废不用。
    total_days = db.Column(db.Float, default=0, comment='年度可请天数（0=不限额度；配了分级档位则本字段不用）')
    # 当年度新入职是否按「剩余日历天数/365」折算（法定口径应开；小公司图省事可关）
    prorate = db.Column(db.String(4), default='0', comment='当年新入职是否折算年假：1 折算 / 0 不折算')
    # —— 计量单位（2026-10-01 加）：事假以「小时」为单位，其余假期以「天」为单位 ——
    # hour 的假期：时长按小时计（不足 1 小时按 1 小时），额度/已用/剩余都按小时展示。
    unit = db.Column(db.String(8), default='day', comment='计量单位：day 天 / hour 小时（事假用小时）')
    # —— 附件要求（2026-10-01 加）：病假须交病例、婚假须交结婚证、产假须交生育证明等 ——
    need_attach = db.Column(db.String(4), default='0', comment='是否必须上传附件：1 必须 / 0 否')
    attach_hint = db.Column(db.String(64), comment='附件要求说明，如「病例 / 诊断证明」')
    description = db.Column(db.Text, comment='说明')


class LeaveTier(db.Model):
    """人事系统 · 假期分级档位：按「累计工作年限」给不同天数（年假法定标准即按此分级）。

    法律依据：《职工带薪年休假条例》（国务院令第514号）第三条 ——
      累计工作已满 1 年不满 10 年：年休假 5 天；
      已满 10 年不满 20 年：10 天；
      已满 20 年：15 天。
    《企业职工带薪年休假实施办法》第五条 —— 新进用人单位当年度年休假按
      (当年度在本单位剩余日历天数 ÷ 365) × 全年应享受天数，折算后不足 1 整天的不享受。

    用法：某假期类型配了档位 → 按员工工龄落档算天数；没配档位 → 用 LeaveType.total_days 固定值。
    max_years = 0 表示最后一档（不设上限）。
    """
    __tablename__ = 'leave_tier'
    id = db.Column(db.Integer, primary_key=True)
    leave_type_id = db.Column(db.Integer, db.ForeignKey('leave_type.id'), comment='所属假期类型')
    min_years = db.Column(db.Float, default=0, comment='累计工作年限下限（含）')
    max_years = db.Column(db.Float, default=0, comment='累计工作年限上限（不含），0=不限')
    days = db.Column(db.Float, default=0, comment='本档年度可休天数')
    sort = db.Column(db.Integer, default=0, comment='排序（小到大）')
    leave_type = db.relationship('LeaveType', backref=db.backref(
        'tiers', order_by='LeaveTier.sort, LeaveTier.min_years', cascade='all, delete-orphan'))


class Holiday(db.Model):
    """EHR · 法定节假日：人事管理员每年年初导入国务院下发的年度假期安排。
    用于考勤日历里把法定节假日标成「休」（放假）或「班」（调休补班）。
    周六/周日属固定休息日，无需入库，由日历逻辑自动判定。"""
    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, unique=True, comment='节假日日期（唯一）')
    name = db.Column(db.String(64), comment='节假日名称，如 国庆节')
    # day_type：rest 放假（法定休）/ work 调休补班（周末上班）
    day_type = db.Column(db.String(16), default='rest', comment='类型：rest 放假 / work 补班')
    year = db.Column(db.Integer, comment='所属年份（便于按年导入/查询）')
    note = db.Column(db.String(200), comment='备注')


class AttendanceRule(db.Model):
    """EHR · 日常考勤规则：人事管理员配置上下班时间与打卡判定规则。
    用于考勤数据导入后自动判定「迟到/早退/未打下班卡」，也是将来对接
    企微/电子考勤机时，同步打卡时间后做考勤判定的依据。
    设计为全局单条规则（code='global'），后续如需按部门差异化可再加 code 区分。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, default='global', comment='规则编码，默认 global 全局规则')
    # 上下班时间（字符串 HH:MM，避免时区/类型复杂度）
    work_start = db.Column(db.String(8), default='09:00', comment='上班时间 HH:MM')
    work_end = db.Column(db.String(8), default='18:00', comment='下班时间 HH:MM')
    # 迟到/早退判定：晚于上班时间/早于下班时间的分钟数阈值（0=一到点即判定）
    late_after = db.Column(db.Integer, default=0, comment='晚于上班时间 N 分钟算迟到（0=过点即算）')
    early_before = db.Column(db.Integer, default=0, comment='早于下班时间 N 分钟算早退（0=提前即算）')
    # 打卡规则
    need_check_in = db.Column(db.Boolean, default=True, comment='是否要求打上班卡')
    need_check_out = db.Column(db.Boolean, default=True, comment='是否要求打下班卡')
    # 工作日：1~7 位字符串表示周一~周日是否上班，如 '1111100'=周一至周五上班
    workdays = db.Column(db.String(7), default='1111100', comment='工作日(周一到周日，1上班0休息)')
    note = db.Column(db.String(200), comment='规则说明')


class ApprovalFlow(db.Model):
    """OA · 审批流：行政管理员在后台配置的审批流程模板。
    一条审批流 = 一个名称 + 一串「有序审批人」；员工提交申请后按顺序逐级审批。
    审批人存 user_id，用 ApprovalFlowStep 记录顺序（第 1 级 → 第 2 级 → ...）。
    本表仅做「流程定义」，实际审批单(Approval)仍是独立流转，后续对接时按 flow 匹配。"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), comment='审批流名称，如 请假审批流')
    # 适用类型：可关联审批类型（请假/报销/通用），空字符串表示通用默认流
    apply_type = db.Column(db.String(32), default='', comment='适用审批类型（空=通用默认流）')
    enabled = db.Column(db.Boolean, default=True, comment='是否启用')
    note = db.Column(db.String(200), comment='说明')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')
    # 有序审批步骤
    steps = db.relationship('ApprovalFlowStep', backref='flow', cascade='all, delete-orphan',
                            order_by='ApprovalFlowStep.seq')


class ApprovalFlowStep(db.Model):
    """OA · 审批流步骤：审批流里的一级审批人（按 seq 排序）。"""
    id = db.Column(db.Integer, primary_key=True)
    flow_id = db.Column(db.Integer, db.ForeignKey('approval_flow.id'), comment='所属审批流')
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), comment='审批人用户ID')
    seq = db.Column(db.Integer, default=1, comment='审批顺序（1=第一级）')
    approver = db.relationship('User', backref='flow_steps')


class Onboarding(db.Model):
    """EHR · 入职登记：新员工入职全流程载体。
    流程（与用户确认）：
      1) 人事操作员在「组织架构管理 → 入职人员信息增加」录入基础信息（姓名/手机/部门/岗位），
         系统自动生成工号（格式 HY***，如 HY001/HY002），状态=pending（待个人填报）；
      2) 系统生成个人填报链接（token 令牌，无需登录），推送至个人处；
      3) 个人按人事卡片格式填写：联系信息/基本信息/工作履历/教育经历，提交后状态=submitted；
      4) 数据自动回传到人事管理主管处，主管完善：用工类型(内勤/外勤)、职级(内勤)、
         代理类型(外勤)、职务、部门等，点击确认后状态=confirmed，数据自动备份至员工花名册；
      5) 花名册仅供查询，不做修改。"""
    id = db.Column(db.Integer, primary_key=True)
    # 系统编码（#145 补充）：系统内部自增的业务编码（RC0001 起），自动生成、不允许修改。
    # 之所以单独给一个码：工号是给人看、可人工改的（如 001-010 留给创始人），
    # 系统需要一个稳定不变的标识来引用这条登记，所以两者分开。
    sys_code = db.Column(db.String(16), unique=True, comment='系统编码（自动生成 RC****，不随工号改动）')
    employee_no = db.Column(db.String(16), unique=True,
                            comment='工号（建号时自动给出候选值 HY***，有权限者可改成 001 等自有编号）')
    name = db.Column(db.String(32), comment='姓名')
    phone = db.Column(db.String(20), comment='个人手机')
    token = db.Column(db.String(64), unique=True, comment='个人填报链接令牌（推送个人用，无需登录）')
    # 状态流：pending 待个人填报 → submitted 待主管确认 → confirmed 已入册 / cancelled 已取消
    #         主管审核不通过可退回：submitted → returned 已退回修改 →（本人重填）→ submitted
    status = db.Column(db.String(16), default='pending', comment='pending待填报/submitted待确认/returned已退回/confirmed已入册/cancelled已取消')
    # ---- 建号时操作员录入的任职信息（主管确认阶段可修改）----
    org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), comment='部门（建号时指定，主管可改）')
    org = db.relationship('Organization', backref='onboardings')
    position = db.Column(db.String(64), comment='岗位')
    job_title = db.Column(db.String(64), comment='职务')
    # ---- 个人填报内容（参照人事卡片：联系信息 / 基本信息）----
    email = db.Column(db.String(64), comment='公司邮箱')
    gender = db.Column(db.String(8), comment='性别：男/女')
    birth_date = db.Column(db.Date, comment='出生日期')
    work_start_date = db.Column(db.Date, comment='参加工作日期')
    hire_date = db.Column(db.Date, comment='入职日期')
    marital = db.Column(db.String(16), comment='婚姻状况')
    children = db.Column(db.String(8), comment='是否有子女：是/否')
    hometown = db.Column(db.String(64), comment='籍贯')
    hobby = db.Column(db.String(128), comment='兴趣爱好')
    last_job = db.Column(db.String(64), comment='最后工作日（上一家单位离职日）')
    # 履历子集：JSON 文本（动态行，前端可增删）
    work_history = db.Column(db.Text, comment='工作履历 JSON：[{start,end,desc}]')
    education = db.Column(db.Text, comment='教育经历 JSON：[{start,end,school,major,degree,edu,edu_type,first,highest_edu,highest_degree,note}]')
    # ---- 人事主管确认阶段完善 ----
    staff_type = db.Column(db.String(16), comment='用工类型：internal 内勤 / external 外勤')
    level_id = db.Column(db.Integer, db.ForeignKey('level.id'), comment='职级（仅内勤）')
    level = db.relationship('Level', backref='onboardings')
    agent_type = db.Column(db.String(16), comment='代理类型（仅外勤）：渠道总/主管/代理人')
    confirmed_at = db.Column(db.DateTime, comment='主管确认时间')
    # 退回修改（#145）：主管审核个人填报时若发现不符合要求，可退回本人重填，并留下退回原因
    return_reason = db.Column(db.String(200), comment='退回修改原因（状态 returned 时展示给本人）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='建号时间')
    note = db.Column(db.String(200), comment='备注')
    # ===== 招募阶段（操作员建号时一次性录入，#145 增强）=====
    id_card = db.Column(db.String(18), comment='身份证号码')
    age = db.Column(db.Integer, comment='入职年龄（按身份证自动测算）')
    probation = db.Column(db.String(1), default='N', comment='是否试用期：Y/N')
    probation_start = db.Column(db.Date, comment='试用期起始')
    probation_end = db.Column(db.Date, comment='试用期结束（≤6个月）')
    probation_salary_pct = db.Column(db.Float, default=0, comment='试用期薪资发放比例（%）')
    # 薪资构成（转正后口径）：总薪资 = 基本薪资 + 绩效薪资 + 岗位补贴（后端合计，前端只读展示）
    regular_base = db.Column(db.Float, default=0, comment='基本薪资')
    regular_perf = db.Column(db.Float, default=0, comment='绩效薪资')
    regular_allow = db.Column(db.Float, default=0, comment='岗位补贴（通讯费/交通费等，明细见 allowance_detail）')
    regular_total = db.Column(db.Float, default=0, comment='总薪资（＝基本+绩效+岗位补贴）')
    allowance_detail = db.Column(db.Text, comment='岗位补贴明细 JSON：[{name, amount}]')
    social_pension = db.Column(db.Float, default=0, comment='养老保险（个人，按配置测算）')
    social_medical = db.Column(db.Float, default=0, comment='医疗保险（个人，按配置测算）')
    social_unemploy = db.Column(db.Float, default=0, comment='失业保险（个人，按配置测算）')
    social_fund = db.Column(db.Float, default=0, comment='住房公积金（个人，按配置测算）')


class HrSocialConfig(db.Model):
    """EHR · 五险一金比例配置（单例，id=1）：招募时按此自动测算个人缴纳额。
    比例与缴费基数上下限均可由 HR 在后台调整（对接国家最新上下限）。"""
    id = db.Column(db.Integer, primary_key=True)
    # ---- 个人承担比例 ----
    pension_pct = db.Column(db.Float, default=8, comment='养老保险个人比例（%）')
    medical_pct = db.Column(db.Float, default=2, comment='医疗保险个人比例（%）')
    unemploy_pct = db.Column(db.Float, default=0.5, comment='失业保险个人比例（%）')
    fund_pct = db.Column(db.Float, default=12, comment='住房公积金个人比例（%）')
    # ---- 单位（企业）承担比例：用于测算「总人力成本 = 总薪资 + 单位五险一金」----
    # 工伤/生育个人不缴，只有单位比例。
    corp_pension_pct = db.Column(db.Float, default=16, comment='养老保险公司比例（%）')
    corp_medical_pct = db.Column(db.Float, default=8, comment='医疗保险公司比例（%）')
    corp_unemploy_pct = db.Column(db.Float, default=0.5, comment='失业保险公司比例（%）')
    corp_injury_pct = db.Column(db.Float, default=0.5, comment='工伤保险公司比例（%，仅单位）')
    corp_maternity_pct = db.Column(db.Float, default=0.8, comment='生育保险公司比例（%，仅单位）')
    corp_fund_pct = db.Column(db.Float, default=12, comment='住房公积金公司比例（%）')
    base_min = db.Column(db.Float, default=0, comment='缴费基数下限（当地社平60%，可填）')
    base_max = db.Column(db.Float, default=999999, comment='缴费基数上限（当地社平300%，可填）')
    note = db.Column(db.String(200), comment='备注')


class HrAllowanceItem(db.Model):
    """EHR · 岗位补贴项配置（后台端口）：通讯费 / 交通费 / 餐补等补贴字段的自定义清单。
    建号时按启用项逐条预填金额（可改），合计计入「岗位补贴」；
    HR 可随时增删改（停用项不再出现在建号页）。"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), comment='补贴项名称（如 通讯费 / 交通费 / 餐补）')
    default_amount = db.Column(db.Float, default=0, comment='默认月金额（建号时预填）')
    enabled = db.Column(db.Boolean, default=True, comment='是否启用（停用后建号页不再出现）')
    note = db.Column(db.String(100), comment='备注')
    sort = db.Column(db.Integer, default=0, comment='排序（越小越靠前）')


class EmployeeChange(db.Model):
    """EHR · 员工异动记录：晋升 / 降职 / 调岗（组织架构管理里的"人员异动"）。
    记录异动前后的部门/职级/职务快照，生效日期与事由；异动时同步更新 Employee 当前值。"""
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='changes')
    change_type = db.Column(db.String(16), comment='异动类型：晋升/降职/调岗')
    from_org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), comment='原部门')
    to_org_id = db.Column(db.Integer, db.ForeignKey('organization.id'), comment='新部门')
    from_org = db.relationship('Organization', foreign_keys=[from_org_id])
    to_org = db.relationship('Organization', foreign_keys=[to_org_id])
    from_level_id = db.Column(db.Integer, db.ForeignKey('level.id'), comment='原职级（内勤）')
    to_level_id = db.Column(db.Integer, db.ForeignKey('level.id'), comment='新职级（内勤）')
    from_level = db.relationship('Level', foreign_keys=[from_level_id])
    to_level = db.relationship('Level', foreign_keys=[to_level_id])
    from_title = db.Column(db.String(64), comment='原职务')
    to_title = db.Column(db.String(64), comment='新职务')
    effective_date = db.Column(db.Date, comment='生效日期')
    reason = db.Column(db.Text, comment='异动事由')
    operator = db.Column(db.String(32), comment='操作人（登录名）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='记录时间')


# ===================== 人事卡片二期（#145）：七大子集板块 =====================
# 人事卡片（templates/hr/card.html）的三~九板块，每个板块对应一张 Employee 子表（一对多）。
# 这些是"历史档案"性质的信息，日期只作展示、不参与计算，故统一用字符串存
# （如「2007-09」「2025-09-01」），既灵活又避开 SQLite Date 列不收字符串的坑。
# 录入入口：HR 权限者在人事卡片页点「录入 / 编辑」进入 templates/hr/card_edit.html。

class HrEducation(db.Model):
    """三、教育经历。"""
    __tablename__ = 'hr_education'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='educations')
    start_time = db.Column(db.String(16), comment='入学时间')
    end_time = db.Column(db.String(16), comment='毕业时间')
    school = db.Column(db.String(64), comment='毕业学校')
    major = db.Column(db.String(64), comment='专业名称')
    # 学历与学位在人事卡片上同属一栏（表头「学历 / 学位」），故合并存放，如「本科 / 学士」
    edu_degree = db.Column(db.String(32), comment='学历 / 学位')
    edu_type = db.Column(db.String(32), comment='学习形式 / 院校类别')
    sort = db.Column(db.Integer, default=0, comment='排序')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrFamily(db.Model):
    """四、家庭信息。"""
    __tablename__ = 'hr_family'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='families')
    relation = db.Column(db.String(16), comment='家庭关系（配偶/父亲/母亲/子女等）')
    member_name = db.Column(db.String(32), comment='成员姓名')
    birth_date = db.Column(db.String(16), comment='出生日期')
    work_unit = db.Column(db.String(64), comment='工作单位')
    position = db.Column(db.String(64), comment='岗位')
    contact = db.Column(db.String(32), comment='联系方式')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrTraining(db.Model):
    """五、培训经历。"""
    __tablename__ = 'hr_training'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='trainings')
    start_time = db.Column(db.String(16), comment='开始时间')
    end_time = db.Column(db.String(16), comment='结束时间')
    hours = db.Column(db.String(16), comment='学时（天）')
    organizer = db.Column(db.String(64), comment='培训主办单位')
    course_name = db.Column(db.String(64), comment='培训班名称')
    course_desc = db.Column(db.String(128), comment='主要课程 / 获得证书')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrTechTitle(db.Model):
    """六、专业技术职务。"""
    __tablename__ = 'hr_tech_title'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='tech_titles')
    obtain_date = db.Column(db.String(16), comment='取得时间')
    skill_name = db.Column(db.String(64), comment='技术技能名称')
    series = db.Column(db.String(32), comment='系列')
    level = db.Column(db.String(32), comment='等级')
    major = db.Column(db.String(64), comment='专业')
    org_cert = db.Column(db.String(100), comment='评审/鉴定机构 · 证书编号')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrQualification(db.Model):
    """七、职业资格。"""
    __tablename__ = 'hr_qualification'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='qualifications')
    obtain_date = db.Column(db.String(16), comment='取证日期')
    valid_until = db.Column(db.String(16), comment='证书有效期')
    level = db.Column(db.String(32), comment='等级')
    cert_name = db.Column(db.String(64), comment='证书名称')
    issuer = db.Column(db.String(64), comment='发证单位')
    cert_no = db.Column(db.String(64), comment='证书编号 / 保管状态')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrReward(db.Model):
    """八、奖励子集。"""
    __tablename__ = 'hr_reward'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='rewards')
    reward_date = db.Column(db.String(16), comment='获奖时间')
    reward_name = db.Column(db.String(64), comment='奖励名称')
    judge_org = db.Column(db.String(64), comment='评选单位')
    reason = db.Column(db.String(128), comment='奖励原因')
    bonus_point = db.Column(db.String(16), comment='奖励加分')
    level = db.Column(db.String(32), comment='奖励级别')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class HrPunishment(db.Model):
    """九、惩处子集。"""
    __tablename__ = 'hr_punishment'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), comment='员工ID')
    employee = db.relationship('Employee', backref='punishments')
    punish_date = db.Column(db.String(16), comment='惩处时间')
    punish_name = db.Column(db.String(64), comment='惩处名称')
    approve_org = db.Column(db.String(64), comment='惩处批准单位')
    reason = db.Column(db.String(128), comment='惩处原因')
    deduct_point = db.Column(db.String(16), comment='惩罚扣分')
    level = db.Column(db.String(32), comment='惩处级别')
    sort = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Mail(db.Model):
    """OA 邮箱：公司内部站内邮件（内部人员互发）。
    收发双方都是 User（内部管理人员）；收件人标记 is_read 已读状态。"""
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), comment='发件人ID')
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), comment='收件人ID')
    title = db.Column(db.String(200), comment='邮件标题')
    content = db.Column(db.Text, comment='邮件正文')
    is_read = db.Column(db.Boolean, default=False, comment='收件人是否已读')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='发送时间')
    # 双向关联：发件人 / 收件人（同一 User 表两次关联，需 foreign_keys 区分）
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_mails')
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref='received_mails')


# ===================== 六、FMS 财务管理系统（增量 #30：财务地基） =====================
# 财务这一块的地基只有两张表：账套设置 + 会计科目表。
# 后面的期初建账、凭证、账簿、报表、发票，全部挂靠在这两张表上。

def fin_level(code):
    """按科目编码推导级次：一级 4 位（1001）、二级 6 位（100101）、三级 8 位，以此类推。"""
    code = (code or '').strip()
    if len(code) <= 4:
        return 1
    return len(code) // 2 - 1


class FinAccount(db.Model):
    """FMS · 会计科目：财务系统的地基表，凭证分录、账簿、报表取数都引用它。
    预设一套《小企业会计准则》下保险中介常用的科目；科目编码是唯一钥匙，
    树形关系用 parent_code 自关联（不建外键，简单直观，也方便将来导入导出）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='科目编码，如 1001 / 100101')
    name = db.Column(db.String(64), comment='科目名称，如 库存现金')
    # 科目类别（小企业会计准则五大类）
    category = db.Column(db.String(16), comment='类别：asset资产/liability负债/equity权益/cost成本/profit损益')
    direction = db.Column(db.String(8), default='debit', comment='余额方向：debit借 / credit贷')
    parent_code = db.Column(db.String(32), default='', comment='上级科目编码（空=一级科目）')
    level = db.Column(db.Integer, default=1, comment='科目级次（由编码长度推导）')
    is_leaf = db.Column(db.Boolean, default=True, comment='是否明细科目（只有明细科目才能做分录）')
    enabled = db.Column(db.Boolean, default=True, comment='是否启用（停用不影响历史数据，只是不能再选它做新分录）')
    # 辅助核算：逗号分隔，如 customer,dept（一期先登记开关，二期凭证录入时按此校验）
    aux = db.Column(db.String(64), default='', comment='辅助核算（逗号分隔：customer客户/employee员工/dept部门/project项目）')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')

    # 中文名映射：放模型里，模板直接用 category_cn / direction_cn，避免模板写映射表
    CATEGORY_CN = {'asset': '资产', 'liability': '负债', 'equity': '所有者权益',
                   'cost': '成本', 'profit': '损益'}
    DIRECTION_CN = {'debit': '借', 'credit': '贷'}

    @property
    def category_cn(self):
        """科目类别中文名"""
        return self.CATEGORY_CN.get(self.category, self.category or '')

    @property
    def direction_cn(self):
        """余额方向中文名"""
        return self.DIRECTION_CN.get(self.direction, self.direction or '')


class FinConfig(db.Model):
    """FMS · 账套设置：一期只服务 1 个核算主体，全表固定只存一条记录（code='default'）。
    启用期间 = 期初建账的起点；凭证字别 = 收/付/转三类凭证各自独立连续编号（用户指定）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, default='default', comment='账套标识（单账套固定 default）')
    company_name = db.Column(db.String(128), comment='核算主体名称')
    standard = db.Column(db.String(16), default='small', comment='会计制度：small 小企业会计准则 / general 企业会计准则')
    taxpayer = db.Column(db.String(16), default='general', comment='纳税人身份：general 一般纳税人 / small_scale 小规模纳税人')
    start_period = db.Column(db.String(7), comment='启用期间 YYYY-MM（期初建账的起点）')
    currency = db.Column(db.String(8), default='CNY', comment='本位币')
    voucher_types = db.Column(db.String(64), default='收,付,转', comment='凭证字别（逗号分隔，各自独立连续编号）')
    # 公司委托代发佣金的第三方（用于费用结算确认函与凭证摘要，如 海腾保险代理有限公司）
    agent_company = db.Column(db.String(128), comment='委托代发佣金的第三方（如 海腾保险代理有限公司）')
    note = db.Column(db.String(200), comment='备注')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')
    # 期初建账状态（增量 #31）：试算平衡通过后由财务主管「确认建账」，此后才允许录凭证
    opening_done = db.Column(db.Boolean, default=False, comment='期初是否已建账（平衡确认后置 True）')
    opening_done_at = db.Column(db.DateTime, comment='建账确认时间')
    opening_done_by = db.Column(db.String(32), comment='建账确认人')


class FinOpening(db.Model):
    """FMS · 期初余额（增量 #31）：启用期间各科目的期初余额 + 本年累计发生额。
    每个科目一条记录（account_code 唯一）；汇总科目不单独存余额，由明细科目汇总得出。
    「本年累计发生额」是为了在年中启用账套时，利润表能取到 1 月至启用前一月的累计数。"""
    __tablename__ = 'fin_opening'
    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(7), comment='启用期间快照 YYYY-MM')
    account_code = db.Column(db.String(32), unique=True, comment='科目编码（只录明细科目）')
    account_name = db.Column(db.String(64), comment='科目名称快照（科目改名后历史仍可读）')
    category = db.Column(db.String(16), comment='类别快照：asset/liability/equity/cost/profit')
    direction = db.Column(db.String(8), comment='余额方向快照')
    debit = db.Column(db.Float, default=0, comment='期初借方余额')
    credit = db.Column(db.Float, default=0, comment='期初贷方余额')
    ytd_debit = db.Column(db.Float, default=0, comment='本年累计借方发生额（可选）')
    ytd_credit = db.Column(db.Float, default=0, comment='本年累计贷方发生额（可选）')
    updated_by = db.Column(db.String(32), comment='最后修改人')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')


# ===================== 六·2、FMS · 记账凭证（增量 #32 / #33） =====================
# 凭证是账务的核心：一张凭证 = 一个业务事项的完整会计分录（借方合计 = 贷方合计）。
# 字别按用户口径取「收 / 付 / 转」三类（**不使用统一的「记」字**），
# 三类字别各自独立连续编号，编号形如 收-202609-001；某月某字别没有凭证就不占号段。
# 流转（增量 #33）：草稿 → 已审核 → 已记账；未记账可作废，已记账只能红字冲销。
#   硬规则①：制单 ≠ 审核（同一个人不能既录又审这张凭证）。
#   硬规则②：只有「已记账」的凭证才进账簿与报表。
#   硬规则③：已审核 / 已记账凭证不可改不可删，只能由主管红冲。

class FinVoucher(db.Model):
    """FMS · 记账凭证主表（增量 #32）。一张凭证挂多行分录（FinVoucherEntry）。"""
    __tablename__ = 'fin_voucher'
    id = db.Column(db.Integer, primary_key=True)
    voucher_no = db.Column(db.String(32), unique=True, comment='凭证号，如 收-202609-001')
    word = db.Column(db.String(4), default='转', comment='字别：收 / 付 / 转（不用「记」字）')
    period = db.Column(db.String(7), comment='会计期间 YYYY-MM（由凭证日期推导，与期初/账套一致）')
    seq = db.Column(db.Integer, default=0, comment='该期间该字别内的序号（决定凭证号）')
    voucher_date = db.Column(db.Date, comment='凭证日期')
    attach_count = db.Column(db.Integer, default=0, comment='附件张数')
    summary = db.Column(db.String(200), comment='凭证摘要')
    source_type = db.Column(db.String(16), default='manual',
                            comment='来源：manual手工/commission佣金/salary工资/expense报销/bonus奖金池/invoice发票')
    source_id = db.Column(db.Integer, comment='来源单据 ID（自动记账用，手工录入为空）')
    status = db.Column(db.String(12), default='draft',
                       comment='状态：draft草稿/audited已审核/posted已记账/void已作废')
    is_red = db.Column(db.Boolean, default=False, comment='是否红字（冲销）凭证')
    amount_debit = db.Column(db.Float, default=0, comment='借方合计（保存时强制借贷相等）')
    amount_credit = db.Column(db.Float, default=0, comment='贷方合计')
    maker_id = db.Column(db.Integer, comment='制单人 User.id')
    maker_name = db.Column(db.String(32), comment='制单人姓名')
    checker_id = db.Column(db.Integer, comment='审核人 User.id（#33 凭证流转）')
    checker_name = db.Column(db.String(32), comment='审核人姓名')
    checked_at = db.Column(db.DateTime, comment='审核时间')
    posted_at = db.Column(db.DateTime, comment='记账时间')
    # —— 凭证流转（增量 #33）——
    red_of_id = db.Column(db.Integer, comment='红字凭证：被冲销的原凭证 id（原凭证为空）')
    void_at = db.Column(db.DateTime, comment='作废时间（作废凭证留痕，不删除）')
    void_by = db.Column(db.String(32), comment='作废人姓名')
    remark = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')

    # 分录：随凭证级联删除；按行号排序（打印与账簿都按这个顺序）
    entries = db.relationship('FinVoucherEntry', backref='voucher',
                              cascade='all, delete-orphan',
                              order_by='FinVoucherEntry.seq', lazy='selectin')

    # 中文名映射放模型里，模板直接用 status_cn / source_cn，不在模板写映射表
    STATUS_CN = {'draft': '草稿', 'audited': '已审核', 'posted': '已记账', 'void': '已作废'}
    SOURCE_CN = {'manual': '手工录入', 'commission': '佣金结算', 'salary': '工资清单',
                 'expense': '报销审批', 'bonus': '奖金池', 'invoice': '发票',
                 'closing': '期末结转'}

    @property
    def status_cn(self):
        """凭证状态中文名"""
        return self.STATUS_CN.get(self.status, self.status or '')

    @property
    def source_cn(self):
        """凭证来源中文名"""
        return self.SOURCE_CN.get(self.source_type, self.source_type or '')

    @property
    def is_editable(self):
        """是否可改：只有草稿能改；已审核/已记账/已作废一律锁死（改用红字冲销）。"""
        return self.status == 'draft'

    @property
    def is_closing(self):
        """是否「期末结转」自动生成的系统凭证（增量 #36）。
        系统凭证的内容完全由账上的损益算出来，不允许手工改 / 删 / 作废 ——
        真要调整，走「反结账」，系统会把这张凭证一并撤销。"""
        return self.source_type == 'closing'

    @property
    def period_cn(self):
        """会计期间中文显示，如 2026年9月"""
        s = self.period or ''
        return '%s年%s月' % (s[:4], int(s[5:7])) if len(s) == 7 and s[5:7].isdigit() else s

    # —— 凭证流转（#33）：状态机 draft → audited → posted；任意未记账环节可 void；已记账只能红冲 ——
    @property
    def can_audit(self):
        """可审核：草稿状态（还须再校验「制单人≠审核人」，在路由里做）。"""
        return self.status == 'draft'

    @property
    def can_post(self):
        """可记账：必须先审核通过（未审核就记账等于自己给自己批）。"""
        return self.status == 'audited'

    @property
    def can_void(self):
        """可作废：草稿与已审核都行；已记账的账已经进了账，只能红冲不能作废。"""
        return self.status in ('draft', 'audited')

    @property
    def can_reverse(self):
        """可红冲：已审核 / 已记账的凭证会计改不了，只能由主管红字冲销。"""
        return self.status in ('audited', 'posted')

    @property
    def flow_cn(self):
        """流转轨迹一句话：制单 → (审核) → (记账) → (作废)，详情页直接显示。"""
        parts = ['%s 制单' % (self.maker_name or '—')]
        if self.checked_at:
            parts.append('%s 审核' % (self.checker_name or '—'))
        if self.posted_at:
            parts.append('已记账')
        if self.status == 'void':
            parts.append('%s 作废' % (self.void_by or '—'))
        return ' → '.join(parts)


class FinVoucherEntry(db.Model):
    """FMS · 凭证分录（增量 #32）：一借一贷 / 多借多贷 的每一行。
    科目存「编码 + 名称快照」——科目日后改名或停用，历史凭证仍能原样显示。"""
    __tablename__ = 'fin_voucher_entry'
    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey('fin_voucher.id'), comment='所属凭证')
    seq = db.Column(db.Integer, default=1, comment='行号（从 1 开始）')
    summary = db.Column(db.String(200), comment='分行摘要（留空则打印凭证摘要）')
    account_code = db.Column(db.String(32), comment='科目编码')
    account_name = db.Column(db.String(64), comment='科目名称快照')
    debit = db.Column(db.Float, default=0, comment='借方金额')
    credit = db.Column(db.Float, default=0, comment='贷方金额')
    aux_json = db.Column(db.Text, comment='辅助核算 JSON（部门/员工/客户/项目，二期启用）')
    cashflow_item = db.Column(db.String(32), comment='现金流量项目（#35 现金流量表用）')


# ===================== 六·3、FMS · 报表模板（增量 #35） =====================
# 报表不存"算好的数"，只存"报表长什么样"——每个行项目挂一条取数公式，
# 打开报表时用当前账上的数实时算出来。凭证一改，三张表立刻跟着变，永不"账表对不上"。
# 取数函数（与规划书 §6 一致）：QC 期初 / QM 期末 / DF 本期借方 / CF 本期贷方 /
#   BF 本期净发生（按科目余额方向取正）/ LF 本年累计净发生。
# 内置三张为系统模板（is_system=True）：不可删，改了公式请「另存为」新模板。
# rows 用 JSON 存，结构：[{label, formula, line, side, head, total, note}, ...]
#   side: left/right —— 只有资产负债表用（左资产 / 右负债与所有者权益）
#   head: True 表示分类小标题（如"流动资产："），不算数
#   total: True 表示合计行，页面加粗 + 加双线
class FinReportTemplate(db.Model):
    __tablename__ = 'fin_report_template'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), comment='报表名称')
    category = db.Column(db.String(16), comment='类别：bs 资产负债表 / pl 利润表 / cf 现金流量表 / custom 自定义')
    rows = db.Column(db.Text, comment='行项目 JSON：[{label, formula, line, side, head, total, note}]')
    is_system = db.Column(db.Boolean, default=False, comment='系统内置（不可删，只能改公式后另存）')
    created_by = db.Column(db.String(32), comment='创建人')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='最后修改时间')
    note = db.Column(db.String(200), comment='说明')

    CATEGORY_CN = {'bs': '资产负债表', 'pl': '利润表', 'cf': '现金流量表', 'custom': '自定义报表'}

    @property
    def category_cn(self):
        return self.CATEGORY_CN.get(self.category, self.category or '自定义报表')

    @property
    def rows_list(self):
        """把 rows 的 JSON 解析成行列表；坏数据一律返回空列表，不让报表页整页崩。"""
        try:
            data = json.loads(self.rows or '[]')
        except Exception:
            return []
        return data if isinstance(data, list) else []


# ===================== 六·4、FMS · 会计期间（增量 #36 期末结转与结账） =====================
# 会计期间是"账的时间格子"。每期做完账，要把损益类科目的余额结转到本年利润（3103），
# 然后"结账"——结账后这一期就锁死，凭证不能再增删改、不能再流转，防止事后改账。
# 记录是"按需创建"的：没结过账的期间在库里没有行，一律视为未结账（open）。

class FinPeriod(db.Model):
    """FMS · 会计期间状态表（增量 #36）：记录每一期的结账状态与结转凭证。"""
    __tablename__ = 'fin_period'
    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(7), unique=True, comment='会计期间 YYYY-MM')
    status = db.Column(db.String(8), default='open', comment='状态：open 未结账 / closed 已结账')
    closed_at = db.Column(db.DateTime, comment='结账时间')
    closed_by = db.Column(db.String(32), comment='结账人姓名')
    # 本期损益结转自动生成的凭证（结转损益到本年利润 3103）
    closing_voucher_id = db.Column(db.Integer, comment='结转凭证 id（本期无损益发生则为空）')
    closing_voucher_no = db.Column(db.String(32), comment='结转凭证号快照')
    profit_amount = db.Column(db.Float, default=0, comment='本期净利润（结账时快照：>0 盈利 / <0 亏损）')
    # 反结账留痕：谁在什么时候把这一期重新打开了（审计要看得见）
    reopen_at = db.Column(db.DateTime, comment='最后一次反结账时间')
    reopen_by = db.Column(db.String(32), comment='最后一次反结账人')
    reopen_count = db.Column(db.Integer, default=0, comment='反结账次数')
    note = db.Column(db.String(200), comment='备注')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')

    STATUS_CN = {'open': '未结账', 'closed': '已结账'}

    @property
    def status_cn(self):
        """期间状态中文名"""
        return self.STATUS_CN.get(self.status, self.status or '')

    @property
    def is_closed(self):
        """是否已结账（结账 = 本期间锁死，凭证不能再动）"""
        return self.status == 'closed'

    @property
    def period_cn(self):
        """期间中文显示，如 2026年9月"""
        s = self.period or ''
        return '%s年%s月' % (s[:4], int(s[5:7])) if len(s) == 7 and s[5:7].isdigit() else s


# ===================== FMS · 发票台账（增量 #37） =====================
# 进/销项发票统一台账：一张发票记不含税金额/税率/税额/价税合计；进项含认证状态与认证所属期。
# voucher_id 预留与凭证的关联（#38 自动记账填），#37 只做台账 + 认证抵扣 + 增值税测算，不生成凭证。

class FinInvoice(db.Model):
    """FMS · 发票台账（增量 #37）：进项(收票)/销项(开票)统一管理。"""
    __tablename__ = 'fin_invoice'
    id = db.Column(db.Integer, primary_key=True)
    direction = db.Column(db.String(8), default='in', comment='方向：in 进项(收票) / out 销项(开票)')
    invoice_code = db.Column(db.String(40), comment='发票代码')
    invoice_no = db.Column(db.String(40), comment='发票号码')
    invoice_type = db.Column(db.String(16), default='数电票', comment='发票类型')
    invoice_date = db.Column(db.Date, comment='开票日期')
    period = db.Column(db.String(7), comment='税款所属期 YYYY-MM（默认取开票日期月份）')
    seller_name = db.Column(db.String(128), comment='销售方名称')
    buyer_name = db.Column(db.String(128), comment='购买方名称')
    amount = db.Column(db.Float, default=0, comment='不含税金额')
    tax_rate = db.Column(db.Float, default=0, comment='税率，小数存储，如 0.06 表示 6%')
    tax = db.Column(db.Float, default=0, comment='税额')
    total = db.Column(db.Float, default=0, comment='价税合计 = 金额 + 税额')
    # 进项认证（仅 direction=in 有意义）：进项在「认证所属期」抵扣，不是开票期
    cert_status = db.Column(db.String(12), default='uncert',
                            comment='认证状态：uncert未认证 / certed已认证 / nodeduct不予抵扣')
    cert_period = db.Column(db.String(7), comment='认证所属期 YYYY-MM（进项专用）')
    # 关联凭证（#38 自动记账填，#37 仅留字段）
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 填），#37 为空')
    remark = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.String(32), comment='录入人')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    DIRECTION_CN = {'in': '进项', 'out': '销项'}
    TYPE_OPTIONS = [('数电票', '数电票'), ('电子专票', '电子专票'), ('电子普票', '电子普票'),
                    ('纸质专票', '纸质专票'), ('纸质普票', '纸质普票'),
                    ('机动车', '机动车销售统一发票'), ('其他', '其他')]
    CERT_CN = {'uncert': '未认证', 'certed': '已认证', 'nodeduct': '不予抵扣'}

    @property
    def direction_cn(self):
        return self.DIRECTION_CN.get(self.direction, self.direction or '')

    @property
    def cert_cn(self):
        return self.CERT_CN.get(self.cert_status, self.cert_status or '')

    @property
    def period_cn(self):
        s = self.period or ''
        return '%s年%s月' % (s[:4], int(s[5:7])) if len(s) == 7 and s[5:7].isdigit() else s

    @property
    def is_deductible(self):
        """是否计入「已认证可抵扣进项」（只有进项 + 已认证才算）。"""
        return self.direction == 'in' and self.cert_status == 'certed'


# ===================== FMS · 费用结算确认函（增量 #41） =====================
# 业务背景：公司做海腾保险代理的非持牌机构业务销售，代理人佣金由海腾代发。
#   每月由财务导入《费用结算数据》Excel（人员信息 / 保单明细1 个人业务 / 保单明细2 渠道管理业务 /
#   费用接收账户信息 四张表），系统逐人生成《业务服务费用结算确认函》PDF，
#   交代理人线下签署后回传归档；二期再接入微信小程序做实名核验 + 电子签名。
# 四张表的关系：批次(1) → 确认函(每人 1 份) → 保单明细行 / 费用接收账户行

class ConfirmBatch(db.Model):
    """FMS · 确认函批次：一次 Excel 导入 = 一个批次，是台账与追溯的入口。"""
    __tablename__ = 'fin_confirm_batch'
    id = db.Column(db.Integer, primary_key=True)
    batch_no = db.Column(db.String(40), unique=True, comment='批次号，如 C202607-01')
    settle_month = db.Column(db.String(6), comment='结算月度 YYYYMM（取自人员信息表）')
    issuer_name = db.Column(db.String(128), comment='出具方（本函抬头），如 广东海盈通保智能科技有限公司')
    agent_company = db.Column(db.String(128), comment='受托代发方（承诺条款第 4 条的第三方），如 海腾保险代理有限公司')
    source_name = db.Column(db.String(255), comment='导入的 Excel 文件名')
    source_path = db.Column(db.String(255), comment='导入的 Excel 存放路径（留档）')
    # 汇总（导入时算一次并留档，便于台账展示；以各确认函的数据为准）
    person_count = db.Column(db.Integer, default=0, comment='人员信息表人数')
    letter_count = db.Column(db.Integer, default=0, comment='实际生成的确认函份数（无保单的人跳过）')
    premium_total = db.Column(db.Float, default=0, comment='保费总额（按保单号去重后合计）')
    fee_total = db.Column(db.Float, default=0, comment='实发费用总额')
    # 状态：checked 已导入待生成 / generated 已生成 PDF / archived 已归档（存在已签回）
    status = db.Column(db.String(16), default='checked', comment='checked/generated/archived')
    check_json = db.Column(db.Text, comment='导入校验结果 JSON：{"errors":[], "warnings":[]}')
    signed_count = db.Column(db.Integer, default=0, comment='已签回归档份数')
    created_by = db.Column(db.String(32), comment='导入人')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='导入时间')
    generated_at = db.Column(db.DateTime, comment='生成 PDF 时间')
    note = db.Column(db.String(300), comment='备注')

    @property
    def status_cn(self):
        return {'checked': '待生成', 'generated': '已生成', 'archived': '已归档'}.get(self.status, self.status or '')

    @property
    def settle_month_cn(self):
        return cn_month(self.settle_month)


class ConfirmLetter(db.Model):
    """FMS · 确认函：一份 Excel 里的一个人 = 一份确认函（含人员快照与汇总）。"""
    __tablename__ = 'fin_confirm_letter'
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('fin_confirm_batch.id'), comment='所属批次')
    batch = db.relationship('ConfirmBatch', backref='letters')
    # 明细与账户随确认函一起增删（改批次删掉时会连带清理）
    items = db.relationship('ConfirmItem', backref='letter', order_by='ConfirmItem.id',
                            cascade='all, delete-orphan')
    accounts = db.relationship('ConfirmAccount', backref='letter', order_by='ConfirmAccount.seq',
                               cascade='all, delete-orphan')
    employee_no = db.Column(db.String(40), comment='工号')
    name = db.Column(db.String(40), comment='姓名')
    position = db.Column(db.String(40), comment='职位（渠道总 / 团队长 / 精英理财师…）')
    team = db.Column(db.String(128), comment='所属团队')
    id_card = db.Column(db.String(32), comment='身份证号')
    bank_card = db.Column(db.String(40), comment='银行卡号')
    bank_name = db.Column(db.String(64), comment='开户银行')
    sign_state = db.Column(db.String(16), comment='签约状态（人员信息表原值）')
    sign_date = db.Column(db.String(32), comment='签约日期')
    settle_month = db.Column(db.String(6), comment='结算月度 YYYYMM')
    has_self = db.Column(db.Boolean, default=False, comment='是否有个人业务明细')
    has_channel = db.Column(db.Boolean, default=False, comment='是否有渠道管理业务明细')
    item_count = db.Column(db.Integer, default=0, comment='保单明细条数')
    premium_total = db.Column(db.Float, default=0, comment='保费总额（本函口径：按保单号去重）')
    fee_total = db.Column(db.Float, default=0, comment='实发费用总额')
    account_total = db.Column(db.Float, default=0, comment='费用接收账户合计')
    pdf_path = db.Column(db.String(255), comment='生成的 PDF 相对路径')
    # 签署状态：pending 待签署 / signed 已签回归档
    sign_status = db.Column(db.String(16), default='pending', comment='pending待签 / signed已签回')
    signed_file = db.Column(db.String(255), comment='签回扫描件/照片的相对路径')
    signed_at = db.Column(db.DateTime, comment='归档时间')
    signed_by = db.Column(db.String(32), comment='归档操作人')
    remark = db.Column(db.String(300), comment='备注')

    @property
    def pdf_name(self):
        """下载时的文件名：姓名_工号_费用确认函.pdf"""
        safe = (self.name or '无名').replace('/', '_').replace('\\', '_')
        return '%s_%s_费用确认函.pdf' % (safe, self.employee_no or '')

    @property
    def biz_type_cn(self):
        """业务类型描述：个人业务 / 渠道管理业务 / 两者都有"""
        if self.has_self and self.has_channel:
            return '个人业务 + 渠道管理业务'
        if self.has_channel:
            return '渠道管理业务'
        if self.has_self:
            return '个人业务'
        return '无明细'


class ConfirmItem(db.Model):
    """FMS · 确认函保单明细行（第一节「服务费用明细」的每一行）。"""
    __tablename__ = 'fin_confirm_item'
    id = db.Column(db.Integer, primary_key=True)
    letter_id = db.Column(db.Integer, db.ForeignKey('fin_confirm_letter.id'), comment='所属确认函')
    kind = db.Column(db.String(16), comment='来源：self 个人业务 / channel 渠道管理业务')
    seq = db.Column(db.Integer, comment='序号（按来源表原有顺序）')
    policy_no = db.Column(db.String(48), comment='保单号')
    holder_name = db.Column(db.String(40), comment='投保人姓名')
    product_name = db.Column(db.String(128), comment='险种名称')
    insure_date = db.Column(db.String(32), comment='承保日期')
    premium = db.Column(db.Float, default=0, comment='保费金额(元)')
    ratio = db.Column(db.Float, default=0, comment='费用比例（小数，如 0.0572）')
    fee = db.Column(db.Float, default=0, comment='实发费用(元)')


class ConfirmAccount(db.Model):
    """FMS · 确认函费用接收账户行（第四节「费用接收账户信息」的每一行）。"""
    __tablename__ = 'fin_confirm_account'
    id = db.Column(db.Integer, primary_key=True)
    letter_id = db.Column(db.Integer, db.ForeignKey('fin_confirm_letter.id'), comment='所属确认函')
    seq = db.Column(db.Integer, comment='序号')
    payee = db.Column(db.String(40), comment='收款人')
    bank_name = db.Column(db.String(64), comment='开户行')
    bank_account = db.Column(db.String(48), comment='银行账号')
    id_card = db.Column(db.String(32), comment='身份证号')
    phone = db.Column(db.String(24), comment='手机号')
    amount = db.Column(db.Float, default=0, comment='金额(元)')


def cn_month(ym):
    """把结算月度 202607 转成中文 2026年07月；异常值原样返回。"""
    s = str(ym or '').strip()
    if len(s) == 6 and s.isdigit():
        return '%s年%s月' % (s[:4], s[4:])
    return s


class FinAuditLog(db.Model):
    """财务操作审计日志（#40）：记录所有关键写操作，可追溯、防越权。"""
    __tablename__ = 'fin_audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, comment='操作用户 id')
    user_name = db.Column(db.String(32), comment='操作用户名（冗余存，便于审计）')
    action = db.Column(db.String(16), comment='动作：登录/新增/修改/删除/审核/记账/红冲/弃审/结账/反结账/导出/备份/恢复')
    module = db.Column(db.String(32), comment='所属模块：fin/oa/hr/...')
    target_type = db.Column(db.String(32), comment='操作对象类型：voucher/invoice/opening/period/...')
    target_id = db.Column(db.Integer, comment='操作对象 id')
    target_no = db.Column(db.String(64), comment='操作对象单号（如凭证号、发票号）')
    detail = db.Column(db.Text, comment='变更说明/前后差异（JSON 或文本）')
    ip = db.Column(db.String(64), comment='操作用户 IP')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='操作时间')

    ACTION_CN = {'login': '登录', 'add': '新增', 'update': '修改', 'delete': '删除',
                 'audit': '审核', 'post': '记账', 'reverse': '红冲', 'unaudit': '弃审',
                 'close': '结账', 'reopen': '反结账', 'export': '导出', 'backup': '备份', 'restore': '恢复'}

    @property
    def action_cn(self):
        return self.ACTION_CN.get(self.action, self.action or '')


class FinBackup(db.Model):
    """财务数据备份记录（#40）：一键备份 SQLite 库文件，可追溯、可恢复。"""
    __tablename__ = 'fin_backup'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(128), comment='备份文件名')
    size = db.Column(db.Integer, comment='文件大小（字节）')
    type = db.Column(db.String(16), default='manual', comment='manual 手动 / auto 自动')
    created_by = db.Column(db.String(32), comment='操作人')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='备份时间')
    remark = db.Column(db.Text, comment='备注')


# ===================== 六·7、FMS · 银企对账（二期 #42） =====================
# 补齐一期"账实不符"的半截子：凭证/账簿已就绪，但账上的银行存款(1002)和企业
# 实际银行流水没对账。本期打通「银行账户 → 流水导入 → 自动+手动对账 → 余额调节表」。
# 银行账户对应一个 1002 明细科目(account_code)，是对账匹配的桥梁。

class FinBankAccount(db.Model):
    """FMS · 银行账户（二期 #42）：一个账户 = 一个 1002 银行存款明细科目。
    account_code 存对应科目编码，对账时抓该科目下已记账(posted)的凭证分录。"""
    __tablename__ = 'fin_bank_account'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, comment='账户编码，如 B01')
    name = db.Column(db.String(64), comment='账户名称/户名，如 广东海盈通保智能科技有限公司-招商银行广州分行')
    bank_name = db.Column(db.String(64), comment='开户行，如 招商银行广州分行')
    account_no = db.Column(db.String(32), comment='银行账号')
    account_code = db.Column(db.String(32), comment='对应 1002 明细科目编码（对账桥梁）')
    currency = db.Column(db.String(8), default='CNY', comment='币种')
    opening_balance = db.Column(db.Float, default=0, comment='期初余额（启用期间前的银行账余额）')
    is_default = db.Column(db.Boolean, default=False, comment='是否默认账户')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')

    @property
    def balance_cn(self):
        """账户余额中文提示（仅用于列表显示，实际对账以流水/账为准）"""
        return '%.2f' % (self.opening_balance or 0)


class FinBankStatement(db.Model):
    """FMS · 银行流水（二期 #42）：导入企业网银对账单，与账上 1002 分录对账。
    方向（银行视角）：贷方=银行收(企业增加)，借方=银行付(企业减少)。"""
    __tablename__ = 'fin_bank_statement'
    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey('fin_bank_account.id'), comment='所属账户')
    trans_date = db.Column(db.Date, comment='交易日期')
    summary = db.Column(db.String(200), comment='摘要')
    counterparty = db.Column(db.String(64), comment='对方户名')
    debit = db.Column(db.Float, default=0, comment='借方（银行付出，企业账减少）')
    credit = db.Column(db.Float, default=0, comment='贷方（银行收进，企业账增加）')
    balance = db.Column(db.Float, default=0, comment='余额（银行对账单余额，末笔=期末余额）')
    voucher_id = db.Column(db.Integer, comment='已对上的凭证 id（未对为空）')
    entry_id = db.Column(db.Integer, comment='已对上的凭证分录 id')
    reconciled = db.Column(db.Boolean, default=False, comment='是否已对账')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')


class FinCashOrder(db.Model):
    """FMS · 出纳收付款单（二期 #43）：录入收/付款单，提交即自动生成 1002 银行存款凭证，
    与银企对账衔接（凭证记账 posted 后成为对账候选）。type=receive 收款 / pay 付款。"""
    __tablename__ = 'fin_cash_order'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='单号，如 SKD-202609-001 / FKD-202609-001')
    type = db.Column(db.String(8), default='receive', comment='receive 收款 / pay 付款')
    bank_account_id = db.Column(db.Integer, db.ForeignKey('fin_bank_account.id'), comment='关联银行账户(1002明细)')
    account_code = db.Column(db.String(32), comment='对方科目编码')
    account_name = db.Column(db.String(64), comment='对方科目名称（冗余）')
    amount = db.Column(db.Float, default=0, comment='金额')
    summary = db.Column(db.String(200), comment='摘要')
    trans_date = db.Column(db.Date, comment='业务/单据日期（=凭证日期）')
    voucher_id = db.Column(db.Integer, comment='生成的凭证 id（空=未生成）')
    status = db.Column(db.String(8), default='draft', comment='draft 未生成凭证 / done 已生成凭证')
    created_by = db.Column(db.Integer, comment='制单人 user_id')
    created_name = db.Column(db.String(32), comment='制单人姓名')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='创建时间')

    @property
    def type_cn(self):
        return '收款' if self.type == 'receive' else '付款'

    @property
    def amount_cn(self):
        return '%.2f' % (self.amount or 0)


# ===================== OA · 费用申请与报销（双轨 + 关联闭环） =====================
# 两条通道：
#   ① 直接报销 —— 员工垫付后凭票直接出「报销单」，无前置申请；
#   ② 先申请后报销 —— 先出「费用申请单」获批额度，实际发生后再出「报销单」并关联该申请编号，
#      报销额 ≤ 批准余额自动放行，超出需写理由（形成合规闭环）。
# 三张表都是新表，由 db.create_all() 直接建，不需要在 seed 的 pending 里登记。

# ============ OA 费用类别体系（费用申请单 / 费用报销单 共用同一口径） ============
# 三层结构：① 一级分组（界面用 optgroup 分组）→ ② 费用类别（报销人选的，细）
#           → ③ 会计科目（账务口径，适度合并）
# 设计要点：**界面类别多、会计科目少**，是刻意的——
#   报销人需要能选准「油费 / 路桥费 / 停车费」，但账务上它们都是「车辆使用费」，
#   全部单列科目会让小公司的科目表碎成上百条。
# 口径纪律（改动时务必守住）：
#   · 差旅费 = 560203、办公费 = 560202、业务招待费 = 560206 —— 回归脚本按此断言，勿改；
#   · 车辆费用 7 项全归 560208 车辆使用费；差旅类 3 项全归 560203；
#   · 「员工借支」不是费用，是资产（1221 其他应收款）；「固定资产」也不是费用（1601）。
EXPENSE_CATEGORY_GROUPS = [
    ('差旅交通', [
        ('差旅费', '560203'),          # 出差综合（车票/机票）
        ('交通费（市内）', '560210'),   # 市内打车/公交/地铁
        ('住宿费', '560203'),
        ('出差餐补', '560203'),
    ]),
    ('车辆费用', [
        ('油费', '560208'), ('路桥费', '560208'), ('停车费', '560208'),
        ('车辆维修费', '560208'), ('车辆保险费', '560208'),
        ('车辆年审费', '560208'), ('其他车辆费', '560208'),
    ]),
    ('办公行政', [
        ('办公费', '560202'),          # 办公用品 / 耗材 / 低值易耗
        ('印刷费', '560213'),
        ('快递费', '560212'),
        ('邮电及通讯费', '560207'),    # 电话 / 宽带 / 邮寄
        ('房租费', '560204'),
        ('水电及物业管理费', '560204'),
        ('租赁费', '560211'),          # 车辆 / 设备 / 场地租赁（非房租）
        ('办公设备维修费', '560214'),
    ]),
    ('业务与市场', [
        ('业务招待费', '560206'),
        ('餐饮服务费', '560206'),
        ('公关费', '560206'),
        ('会务费', '560209'),
        ('培训费', '560215'),
        ('市场推广费', '560101'),
        ('广告费', '560102'),
        ('销售激励费', '560103'),
    ]),
    ('人力与福利', [
        ('工资', '560201'),
        ('社会保险费（单位）', '560216'),
        ('住房公积金（单位）', '560217'),
        ('职工福利费', '560218'),
        ('员工商业保险', '560219'),
        ('招聘费', '560220'),
        ('残疾人就业保障金', '560221'),
    ]),
    ('专业服务', [
        ('咨询费', '560222'),
        ('律师费', '560223'),
        ('审计费', '560224'),
        ('技术服务费', '560225'),
        ('服务费', '560229'),
    ]),
    ('财务费用', [
        ('银行手续费', '560301'),
        ('利息支出', '560302'),
        ('利息收入', '560303'),
    ]),
    ('税费与规费', [
        ('印花税', '540301'),
        ('税金及附加（其他）', '540302'),
        ('其他税费', '540302'),
    ]),
    ('资产与摊销', [
        ('固定资产', '1601'),
        ('无形资产', '1701'),
        ('装修费', '560226'),
    ]),
    ('其他', [
        ('员工借支', '1221'),          # 借给员工的钱，是资产不是费用
        ('开办费', '560227'),
        ('研发费用', '560228'),
        ('罚款 / 罚金', '5711'),
        ('其他', '560299'),
    ]),
]
# 类别 → 科目（扁平映射，供记账与校验用）
EXPENSE_CATEGORY_ACCOUNT = {n: c for _g, _items in EXPENSE_CATEGORY_GROUPS for n, c in _items}
# 全部类别名（按分组顺序，扁平）
EXPENSE_CATEGORIES = list(EXPENSE_CATEGORY_ACCOUNT.keys())
# 兼容旧名：早前只按「7 大类」展示，现在仍保留这几个大类名做默认值
EXPENSE_DEFAULT_CATEGORY = '差旅费'


class ExpenseClaim(db.Model):
    """费用申请单（事前申请）：先申请一笔费用额度，批准后实际发生费用再出报销单关联核销。"""
    __tablename__ = 'expense_claim'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='申请单号 SQB-YYYYMM-NNN')
    applicant_id = db.Column(db.Integer, comment='申请人 user_id')
    applicant_name = db.Column(db.String(32), comment='申请人姓名（冗余，便于打印）')
    employee_no = db.Column(db.String(32), comment='申请人工号')
    org_name = db.Column(db.String(64), comment='所属部门')
    category = db.Column(db.String(32), default='差旅费', comment='费用大类')
    amount = db.Column(db.Float, default=0, comment='申请金额（批准额度）')
    used_amount = db.Column(db.Float, default=0, comment='已报销核销金额（累计）')
    period_start = db.Column(db.String(16), comment='使用期间起（YYYY-MM-DD，字符串存避开 SQLite Date 坑）')
    period_end = db.Column(db.String(16), comment='使用期间止')
    pay_mode = db.Column(db.String(16), default='prepay', comment='prepay 预支 / self 不预支（垫付后报）')
    purpose = db.Column(db.Text, comment='事由说明')
    remark = db.Column(db.String(200), comment='审批意见')
    status = db.Column(db.String(16), default='pending',
                       comment='pending 审批中 / approved 已批准 / rejected 已驳回 / closed 已核销')
    approver_name = db.Column(db.String(32), comment='审批人姓名')
    decided_at = db.Column(db.DateTime, comment='审批时间')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='提交时间')

    @property
    def remain_amount(self):
        """剩余可报销余额 = 批准额 − 已核销额。"""
        return round((self.amount or 0) - (self.used_amount or 0), 2)

    @property
    def status_cn(self):
        return {'pending': '审批中', 'approved': '已批准',
                'rejected': '已驳回', 'closed': '已核销'}.get(self.status, self.status)

    @property
    def pay_mode_cn(self):
        return '预支（公司先付）' if self.pay_mode == 'prepay' else '不预支（垫付后报）'


class ExpenseReimburse(db.Model):
    """费用报销单：审核通过后财务可据此自动生成付款凭证（voucher_id 回填）。"""
    __tablename__ = 'expense_reimburse'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='报销单号 BXD-YYYYMM-NNN')
    mode = db.Column(db.String(8), default='direct', comment='direct 直接报销 / apply 关联费用申请')
    claim_id = db.Column(db.Integer, db.ForeignKey('expense_claim.id'), nullable=True, comment='关联费用申请单 id')
    claim = db.relationship('ExpenseClaim', foreign_keys=[claim_id], backref='reimburses')
    claim_code = db.Column(db.String(32), comment='关联申请单号（冗余，打印用）')
    over_reason = db.Column(db.String(200), comment='超出批准余额时的说明')
    applicant_id = db.Column(db.Integer, comment='报销人 user_id')
    applicant_name = db.Column(db.String(32), comment='报销人姓名')
    employee_no = db.Column(db.String(32), comment='报销人工号')
    org_name = db.Column(db.String(64), comment='所属部门')
    category = db.Column(db.String(32), default='差旅费', comment='费用大类')
    amount = db.Column(db.Float, default=0, comment='合计金额（含税）')
    tax_amount = db.Column(db.Float, default=0, comment='其中可抵扣进项税额')
    purpose = db.Column(db.Text, comment='事由说明')
    remark = db.Column(db.String(200), comment='审批意见')
    pay_method = db.Column(db.String(32), default='银行转账（对私）', comment='收款方式')
    payee_name = db.Column(db.String(32), comment='收款户名')
    bank_name = db.Column(db.String(64), comment='开户行')
    bank_account = db.Column(db.String(32), comment='银行账号')
    attach_files = db.Column(db.Text, comment='电子发票/票据附件（JSON 数组：[{name,file,size}]，存 static/uploads/invoices/）')
    attachment_count = db.Column(db.Integer, default=0, comment='附件张数')
    status = db.Column(db.String(16), default='pending',
                       comment='pending 审批中 / approved 已通过 / rejected 已驳回 / returned 退回修改 / paid 已付款')
    approver_name = db.Column(db.String(32), comment='审批人姓名')
    decided_at = db.Column(db.DateTime, comment='审批时间')
    period = db.Column(db.String(7), comment='记账期间 YYYY-MM（审批通过时写入，供自动记账取数）')
    voucher_id = db.Column(db.Integer, comment='自动记账生成的凭证 id（#38 回填）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='提交时间')

    @property
    def status_cn(self):
        return {'pending': '审批中', 'approved': '已通过', 'rejected': '已驳回',
                'returned': '退回修改', 'paid': '已付款'}.get(self.status, self.status)

    @property
    def mode_cn(self):
        return '关联费用申请' if self.mode == 'apply' else '直接报销'

    @property
    def account_code(self):
        """本单费用类别对应的会计科目编码（类别未登记时兜底到 560299 其他管理费用）。"""
        return EXPENSE_CATEGORY_ACCOUNT.get(self.category, '560299')


class ExpenseReimburseItem(db.Model):
    """报销单明细行：一行一张票据。"""
    __tablename__ = 'expense_reimburse_item'
    id = db.Column(db.Integer, primary_key=True)
    reimburse_id = db.Column(db.Integer, db.ForeignKey('expense_reimburse.id'), comment='所属报销单 id')
    occur_date = db.Column(db.String(16), comment='发生日期（字符串存，不参与计算）')
    category = db.Column(db.String(32), comment='费用类别（与单据费用大类同一套字典，便于逐行归类）')
    detail = db.Column(db.String(200), comment='费用说明')
    amount = db.Column(db.Float, default=0, comment='金额（含税）')
    tax = db.Column(db.Float, default=0, comment='其中税额')
    invoice_type = db.Column(db.String(32), comment='发票类型')
    invoice_no = db.Column(db.String(64), comment='发票号码')
    sort = db.Column(db.Integer, default=0, comment='行序')

    reimburse = db.relationship('ExpenseReimburse', foreign_keys=[reimburse_id],
                                backref=db.backref('items', order_by='ExpenseReimburseItem.sort'))


# ===================== 行政管理系统（ADM）· 一期（2026-10-01）=====================
# 定位：行政事务的「台账登记 + 到期提醒」中心。只管"物"，不管"钱"——
#   · 报销 / 请款 / 借支 → 走 OA 申请中心（本系统不做单据流）
#   · 固定资产记账(1601) / 折旧凭证 → 走 FMS 财务（本系统只出台账）
#   · 员工劳动合同 / 代理合同 → 归 EHR（本表的 AdmContract 只管对外商务合同）
# 本期建 3 张台账表 + 1 张分类字典；车辆管理 / 费用台账 / 行政事务留待二三期。
# 说明：新表由 db.create_all() 直接创建，不需要在 seed._migrate_columns 里登记。

class AdmCategory(db.Model):
    """行政管理 · 分类字典：证照类型 / 合同类型 / 资产类别等，后台可增删。"""
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(16), comment='分类域：license 证照 / contract 合同 / asset 资产')
    name = db.Column(db.String(64), comment='分类名称')
    sort = db.Column(db.Integer, default=0, comment='排序（小的在前）')
    enabled = db.Column(db.String(4), default='1', comment='是否启用：1 启用 / 0 停用')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmLicense(db.Model):
    """行政管理 · 证照资质台账：营业执照 / 许可证 / 资质证书 / 印章 / 商标专利。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='证照编号（系统自动 ZZ-YYYYMM-NNN）')
    name = db.Column(db.String(128), comment='证照名称')
    category = db.Column(db.String(64), comment='证照类型（取 AdmCategory.kind=license）')
    no = db.Column(db.String(64), comment='证照号码 / 注册号 / 统一社会信用代码')
    issuer = db.Column(db.String(128), comment='发证机关')
    issue_date = db.Column(db.String(16), comment='发证日期 YYYY-MM-DD（字符串存，避开 SQLite Date 坑）')
    expire_date = db.Column(db.String(16), comment='有效期至 YYYY-MM-DD（空 = 长期有效）')
    keeper = db.Column(db.String(32), comment='保管人')
    place = db.Column(db.String(128), comment='存放地点')
    file_name = db.Column(db.String(255), comment='扫描件原文件名')
    file_path = db.Column(db.String(255), comment='扫描件落盘文件名（static/uploads/adm/license/）')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmContract(db.Model):
    """行政管理 · 商务合同台账（非人事）：房屋租赁 / 物业 / 采购 / 服务外包 / 保险。

    注意与 EHR 的 Contract（员工劳动合同 / 代理合同）区分：两类合同本质不同、分区管理。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='合同编号（系统自动 SWHT-YYYYMM-NNN）')
    name = db.Column(db.String(128), comment='合同名称')
    category = db.Column(db.String(64), comment='合同类型（取 AdmCategory.kind=contract）')
    party = db.Column(db.String(128), comment='对方单位 / 供应商')
    sign_date = db.Column(db.String(16), comment='签订日期 YYYY-MM-DD')
    start_date = db.Column(db.String(16), comment='合同起始日 YYYY-MM-DD')
    end_date = db.Column(db.String(16), comment='合同到期日 YYYY-MM-DD（空 = 无固定期限）')
    amount = db.Column(db.Float, default=0, comment='合同金额（元）')
    pay_mode = db.Column(db.String(16), comment='付款方式：once 一次性 / install 分期')
    pay_plan = db.Column(db.Text, comment='付款计划说明（分期节点、每期金额）')
    handler = db.Column(db.String(32), comment='经办人')
    dept = db.Column(db.String(64), comment='承办部门')
    asset_id = db.Column(db.Integer, comment='关联资产 id（如租赁合同关联固定资产，可空）')
    file_name = db.Column(db.String(255), comment='合同电子版原文件名')
    file_path = db.Column(db.String(255), comment='合同电子版落盘文件名')
    status = db.Column(db.String(16), default='active', comment='状态：active 履行中 / ended 已终止')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmAsset(db.Model):
    """行政管理 · 固定资产台账：电子设备 / 办公家具 / 车辆等。

    折旧按【年限平均法】在页面实时计算，不落库（避免与财务口径不一致时两处打架）。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='资产编号（系统自动 ZC-YYYYMM-NNN）')
    name = db.Column(db.String(128), comment='资产名称')
    category = db.Column(db.String(64), comment='资产类别（取 AdmCategory.kind=asset）')
    spec = db.Column(db.String(128), comment='规格型号')
    price = db.Column(db.Float, default=0, comment='资产原值（元）')
    buy_date = db.Column(db.String(16), comment='购置日期 YYYY-MM-DD')
    life_years = db.Column(db.Integer, default=3, comment='折旧年限（年）')
    salvage_rate = db.Column(db.Float, default=5, comment='残值率（%）')
    dept = db.Column(db.String(64), comment='使用部门')
    keeper = db.Column(db.String(32), comment='使用人 / 保管人')
    place = db.Column(db.String(128), comment='存放地点')
    status = db.Column(db.String(16), default='in_use',
                       comment='状态：in_use 在用 / idle 闲置 / repair 维修中 / scrap 已报废')
    file_name = db.Column(db.String(255), comment='购置凭证 / 发票原文件名')
    file_path = db.Column(db.String(255), comment='购置凭证 / 发票落盘文件名')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# ===================== ⑥ 行政事务（二期，2026-10-01） =====================
# 三本子台账：办公用品（含库存流水）/ 印章使用登记 / 快递收发。
# 定位不变：只管"物"与"登记"，用章申请走 OA 审批流，本模块只登记结果。

class AdmSupply(db.Model):
    """行政事务 · 办公用品台账（带当前库存，领用/入库自动增减）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='用品编号（系统自动 BYP-YYYYMM-NNN）')
    name = db.Column(db.String(128), comment='用品名称')
    category = db.Column(db.String(64), comment='用品类别（取 AdmCategory.kind=supply）')
    spec = db.Column(db.String(128), comment='规格型号')
    unit = db.Column(db.String(16), comment='计量单位（个/支/包/箱…）')
    stock = db.Column(db.Float, default=0, comment='当前库存数量')
    min_stock = db.Column(db.Float, default=0, comment='安全库存（低于则预警，0=不预警）')
    place = db.Column(db.String(128), comment='存放地点')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmSupplyFlow(db.Model):
    """行政事务 · 办公用品出入库流水（入库 in / 领用 out）。"""
    id = db.Column(db.Integer, primary_key=True)
    supply_id = db.Column(db.Integer, comment='所属用品 id（AdmSupply.id）')
    direction = db.Column(db.String(8), comment='方向：in 入库 / out 领用')
    qty = db.Column(db.Float, default=0, comment='数量')
    who = db.Column(db.String(32), comment='领用人（入库时为空）')
    dept = db.Column(db.String(64), comment='领用部门')
    flow_date = db.Column(db.String(16), comment='发生日期 YYYY-MM-DD')
    purpose = db.Column(db.String(200), comment='用途 / 事由')
    operator = db.Column(db.String(32), comment='登记人')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmSealUse(db.Model):
    """行政事务 · 印章使用登记台账。

    印章本体（刻章备案、保管责任）在①证照资质；这里只登记每一次用章。
    用章申请如需审批走 OA 申请中心，本表用 approval_code 关联审批单号。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='登记编号（系统自动 YZ-YYYYMM-NNN）')
    seal_name = db.Column(db.String(64), comment='印章名称（公章/合同章/财务章…）')
    use_date = db.Column(db.String(16), comment='用章日期 YYYY-MM-DD')
    applicant = db.Column(db.String(32), comment='申请人 / 用章人')
    dept = db.Column(db.String(64), comment='申请部门')
    use_type = db.Column(db.String(16), default='seal', comment='方式：seal 现场加盖 / borrow 借出使用')
    purpose = db.Column(db.String(200), comment='用章事由')
    doc_name = db.Column(db.String(200), comment='用章文件名称')
    copies = db.Column(db.Integer, default=1, comment='用章份数')
    approval_code = db.Column(db.String(32), comment='关联 OA 审批单号（可空）')
    keeper = db.Column(db.String(32), comment='印章保管人 / 监印人')
    status = db.Column(db.String(16), default='done',
                       comment='状态：pending 待办 / done 已盖章 / returned 已归还')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmExpress(db.Model):
    """行政事务 · 快递收发登记台账（收件 / 寄件）。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='登记编号（系统自动 KD-YYYYMM-NNN）')
    direction = db.Column(db.String(8), default='in', comment='方向：in 收件 / out 寄件')
    company = db.Column(db.String(32), comment='快递公司（顺丰/京东/EMS…）')
    waybill = db.Column(db.String(64), comment='运单号')
    counterpart = db.Column(db.String(64), comment='对方：收件时=寄件人 / 寄件时=收件人')
    handler = db.Column(db.String(32), comment='本公司经手人')
    dept = db.Column(db.String(64), comment='相关部门')
    item = db.Column(db.String(128), comment='物品名称 / 文件摘要')
    fee = db.Column(db.Float, default=0, comment='快递费用（寄件，元）')
    express_date = db.Column(db.String(16), comment='发生日期 YYYY-MM-DD')
    status = db.Column(db.String(16), default='done',
                       comment='状态：pending 待签收/待寄出 / done 已签收/已寄出 / returned 已退回')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# ===================== ⑤ 费用台账（三期，2026-10-01） =====================
# 周期性行政费用（水费 / 电费 / 物业费 / 房租 / 宽带通讯 …）的「登记 + 缴费提醒」台账。
# 定位不变（红线）：本表只做登记与提醒，**不产生任何付款单据、不生成凭证**——
#   需要付款时点「发起报销」跳 OA 申请中心，本表只回填关联单号（approval_code）。

class AdmExpense(db.Model):
    """行政管理 · 费用台账：按月登记的周期性行政费用。

    状态两态：unpaid 待缴 / paid 已缴；逾期、临期按 due_date 在页面实时算（不落库）。
    金额合计与年度汇总均由本表实时汇总，不做冗余字段。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='费用编号（系统自动 FY-YYYYMM-NNN）')
    category = db.Column(db.String(64), comment='费用类型（取 AdmCategory.kind=expense）')
    period = db.Column(db.String(7), comment='所属期间 YYYY-MM（费用归属月份）')
    amount = db.Column(db.Float, default=0, comment='应缴金额（元）')
    payee = db.Column(db.String(128), comment='缴费对象 / 收款方')
    due_date = db.Column(db.String(16), comment='缴费截止日 YYYY-MM-DD（用于缴费提醒，空 = 不提醒）')
    pay_date = db.Column(db.String(16), comment='实际缴费日期 YYYY-MM-DD')
    pay_mode = db.Column(db.String(16), comment='付款方式：bank 银行转账 / cash 现金 / other 其他')
    invoice_no = db.Column(db.String(64), comment='发票号 / 账单号')
    contract_id = db.Column(db.Integer, comment='关联商务合同 id（如房租关联租赁合同，可空）')
    asset_id = db.Column(db.Integer, comment='关联资产 id（如车险关联车辆资产，可空）')
    handler = db.Column(db.String(32), comment='经办人')
    dept = db.Column(db.String(64), comment='承办部门')
    approval_code = db.Column(db.String(32), comment='关联 OA 单据号（报销 / 请款，可空）')
    status = db.Column(db.String(16), default='unpaid', comment='状态：unpaid 待缴 / paid 已缴')
    file_name = db.Column(db.String(255), comment='发票 / 账单附件原文件名')
    file_path = db.Column(db.String(255), comment='发票 / 账单附件落盘文件名')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# ===================== ⑦ 车辆管理（四期，2026-10-01） =====================
# 公司车辆一本账：车辆档案（带保险 / 年检到期提醒）+ 一张动态流水表
# （保养 maintain / 维修 repair / 违章 fine / 用车 use 四类合用，用 kind 区分）。
# 定位不变（红线）：只管"物的登记与提醒"——
#   · 车险 / 年检 / 保养产生的费用报销 → 走 OA 申请中心（本模块不做单据流）
#   · 车辆作为固定资产入账(1601) 与折旧凭证 → 走 FMS 财务（本模块只出台账）
# 保险 / 年检的临期状态一律页面实时算（adm._expire_state），不落库。

class AdmVehicle(db.Model):
    """车辆管理 · 车辆档案：一辆车一行，含保险 / 年检到期日用于到期提醒。"""
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='车辆编号（系统自动 CAR-YYYYMM-NNN）')
    plate_no = db.Column(db.String(32), comment='车牌号码（如 粤A·12345）')
    name = db.Column(db.String(64), comment='车辆名称 / 别名（如 公司公务用车）')
    kind = db.Column(db.String(32), comment='车辆类型（取 AdmCategory.kind=vehicle）')
    brand = db.Column(db.String(64), comment='品牌型号（如 大众帕萨特 330TSI）')
    vin = db.Column(db.String(64), comment='车架号 / VIN')
    engine_no = db.Column(db.String(64), comment='发动机号')
    color = db.Column(db.String(16), comment='车身颜色')
    seats = db.Column(db.Integer, default=5, comment='核定载人数')
    owner = db.Column(db.String(64), comment='登记所有人（公司全称 / 个人挂靠）')
    buy_date = db.Column(db.String(16), comment='购置日期 YYYY-MM-DD')
    price = db.Column(db.Float, default=0, comment='购置价格（元）')
    mileage = db.Column(db.Float, default=0, comment='当前里程（公里）')
    keeper = db.Column(db.String(32), comment='车辆责任人 / 使用人')
    dept = db.Column(db.String(64), comment='使用部门')
    place = db.Column(db.String(128), comment='常停放地点')
    status = db.Column(db.String(16), default='in_use',
                       comment='状态：in_use 在用 / idle 闲置 / repair 维修中 / disposed 已处置')
    insurer = db.Column(db.String(64), comment='保险公司')
    insurance_no = db.Column(db.String(64), comment='保单号')
    insurance_expire = db.Column(db.String(16), comment='保险到期日 YYYY-MM-DD（用于提醒）')
    inspect_expire = db.Column(db.String(16), comment='年检到期日 YYYY-MM-DD（用于提醒）')
    file_name = db.Column(db.String(255), comment='行驶证 / 登记证附件原文件名')
    file_path = db.Column(db.String(255), comment='行驶证 / 登记证附件落盘文件名')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class AdmVehRecord(db.Model):
    """车辆管理 · 车辆动态流水（保养 / 维修 / 违章 / 用车 四类合用一张表）。

    kind 决定用到的字段（页面按类型联动显示/隐藏）：
      · maintain 保养：rec_date / mileage 当时里程 / cost 费用 / place 保养厂 / next_date 下次保养日
      · repair   维修：rec_date / mileage / cost / place 维修厂 / detail 维修项目
      · fine     违章：rec_date / cost 罚款 / fine_point 扣分 / place 违章地点
                       / detail 违章行为 / fine_status 处理状态
      · use      用车：rec_date / driver 驾驶人 / place 起止地点 / detail 事由 / mileage 返回里程
    """
    id = db.Column(db.Integer, primary_key=True)
    vehicle_id = db.Column(db.Integer, comment='所属车辆 id（AdmVehicle.id）')
    kind = db.Column(db.String(16), comment='类型：maintain 保养 / repair 维修 / fine 违章 / use 用车')
    rec_date = db.Column(db.String(16), comment='发生日期 YYYY-MM-DD')
    mileage = db.Column(db.Float, default=0, comment='里程（公里）：保养/维修为当时里程，用车为返回里程')
    cost = db.Column(db.Float, default=0, comment='费用（元）：保养/维修支出、违章罚款')
    driver = db.Column(db.String(32), comment='驾驶人（用车 / 违章）')
    place = db.Column(db.String(128), comment='地点 / 单位（保养维修厂、违章地点、用车起止）')
    detail = db.Column(db.String(200), comment='事项说明（保养项目 / 维修内容 / 违章行为 / 用车事由）')
    fine_point = db.Column(db.Integer, default=0, comment='违章扣分（仅 kind=fine）')
    fine_status = db.Column(db.String(16), comment='违章处理状态：pending 未处理 / done 已处理')
    next_date = db.Column(db.String(16), comment='下次提醒日（如 next_date=下次保养日期）')
    file_name = db.Column(db.String(255), comment='单据 / 发票附件原文件名')
    file_path = db.Column(db.String(255), comment='单据 / 发票附件落盘文件名')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# ===================== 核心业务管理系统（CRM）· 产品合约管理（一期，2026-10-01） =====================
# 定位：产品合约 = 我司内部的「协议备份台账」。
#   签约主体是海腾（我司无保险牌照、无资质），本系统只做登记 / 查阅 / 政策留痕，
#   不产生佣金与业绩数据（佣金在 commission.py），不作法律归档（ADM 商务合同另有其表）。
# 三张表：
#   · Insurer            保司档案（保险公司基础信息主数据，供产品合约引用）
#   · CrmProductContract 产品合约（一条记录 = 一个保司的一个产品；总总 / 分分两套方案字段并存）
#   · CrmProductDoc      产品资料库（微投指南 / 产品条款 / 说明书 / 费率表 等附件）
# 口径（威哥 2026-10-01 拍板）：
#   · FYD 已剔除；原「代理人第一年佣金」统一并入 FYC → 拆为 总总FYC / 分分FYC；
#   · FYC、RYC 一律是「佣金率（%）」，页面按 2 位小数展示；
#   · 三个首年合计由后端自动算（crm._calc_totals），不手工录入。
# 日期一律 String(16) 存 YYYY-MM-DD；本组表为纯新增表，靠 create_all 直建、不入 _migrate_columns。


class Insurer(db.Model):
    """保司档案：保险公司基础信息主数据（工商注册 / 许可证 / 法人 / 地址 等）。
    供产品合约引用；支持内置保司库一键填充、Excel 导入导出，抓取接口预留。"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, comment='保司全称（如 中国人寿保险股份有限公司）')
    short_name = db.Column(db.String(64), comment='保司简称（如 中国人寿）')
    credit_code = db.Column(db.String(32), comment='统一社会信用代码（18 位，即现行工商注册号）')
    reg_no = db.Column(db.String(32), comment='工商注册号（旧版 15 位，历史保司可能仅有此项）')
    license_no = db.Column(db.String(64), comment='保险许可证号（保险公司法人许可证编号）')
    # 四十一改（2026-10-29）：许可证批准设立日期 —— 从金融监管总局许可证库抓回来的
    #   setDate。⚠️ 与 establish_date（工商成立日期）**不是同一个口径**，通常相差几天到
    #   几个月（批筹→开业），故单独存一列，绝不互相覆盖。
    license_date = db.Column(db.String(16), comment='许可证批准设立日期 YYYY-MM-DD（监管口径）')
    tax_no = db.Column(db.String(32), comment='税号（一般与统一社会信用代码一致）')
    legal_person = db.Column(db.String(32), comment='法定代表人')
    reg_capital = db.Column(db.String(32), comment='注册资本')
    establish_date = db.Column(db.String(16), comment='成立日期 YYYY-MM-DD')
    address = db.Column(db.String(200), comment='工商营业地址')
    phone = db.Column(db.String(32), comment='联系电话')
    website = db.Column(db.String(128), comment='官方网站')
    business_scope = db.Column(db.Text, comment='经营范围')
    insurer_type = db.Column(db.String(16), comment='公司类型：life 寿险 / property 财险 / health 健康险 / pension 养老险 / other 其他')
    status = db.Column(db.String(16), default='coop', comment='合作状态：coop 合作中 / paused 暂停 / none 未合作')
    contact_person = db.Column(db.String(32), comment='对接人姓名（保司方对接人）')
    contact_title = db.Column(db.String(64), comment='对接人身份与职务')
    contact_phone = db.Column(db.String(32), comment='对接人联系电话')
    source = db.Column(db.String(16), default='manual', comment='数据来源：manual 手工 / builtin 内置库 / fetch 抓取 / import 导入')
    data_updated_at = db.Column(db.String(16), comment='基础信息最后更新时间 YYYY-MM-DD')
    # 四十一改：上次「一键抓取」尝试时间（YYYY-MM-DD HH:MM:SS）。
    #   批量抓取按此字段升序轮转 —— 抓不到的大保司（分支机构上千家，监管站翻不到总公司）
    #   会被排到后面，不会每次都卡在前几家、让后面的中小保司永远轮不到。
    fetch_try_at = db.Column(db.String(20), comment='上次抓取尝试时间')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmProductContract(db.Model):
    """产品合约：保司产品合约台账（内部协议备份）——产品级主表。

    一条记录 = 一个保司的一个产品；**缴别与费用政策在子表 CrmContractTier 中按缴别分行存放**
    （一款产品多种缴别，每缴别对应的费用政策不同）。
    签约级别 sign_level 标记主签约层级（tt 总-总 / tf 总-分 / ff 分-分）；
    总-分 / 分-分签约时用 branch_id 指明省分公司。
    继续率考核引用规则库 CrmPersistRule（多产品共用同一条规则）。

    ⚠️ 二期（2026-10-01）起：本表 ④~⑩ 组旧列（首期收入 / 总总·分分方案金额 /
       RYC-2~10 / 三个合计 / 继续率系数）**不再写入**，仅为兼容一期数据保留；
       阅读与统计一律以 CrmContractTier 为准。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='合约编号（系统自动 PTC-YYYYMM-NNN）')
    # ① 归属
    insurer_id = db.Column(db.Integer, db.ForeignKey('insurer.id'), comment='关联保司 Insurer.id')
    insurer_name = db.Column(db.String(128), comment='保司名称（冗余快照，防保司改名影响历史）')
    insurer_short = db.Column(db.String(64), comment='保司简称（冗余快照）')
    branch_id = db.Column(db.Integer, comment='关联省分公司 InsurerBranch.id（总-分 / 分-分 签约时填）')
    branch_name = db.Column(db.String(128), comment='省分公司名称（冗余快照）')
    sign_level = db.Column(db.String(8), comment='签约级别：tt 总-总 / tf 总-分 / ff 分-分')
    # ② 产品
    product_name = db.Column(db.String(128), comment='产品名称')
    product_code = db.Column(db.String(64), comment='产品编码')
    # 三十五改（2026-10-29）：保障期限 —— 一款产品可能同时有「保终身」「保至70岁」等
    #   多套费率政策，**一条合约 = 一个保障期限**；同一产品不同保障期限就分别建合约。
    #   取值来自 crm.COVERAGE_TERMS（内置常用项）＋ 历史自定义值（crm._coverage_terms），
    #   表单用 datalist 可下拉选、也可手工输入新值。
    coverage_term = db.Column(db.String(64), default='', comment='保障期限（保终身 / 保至70岁 / 自定义）')
    # 二十七改2（2026-10-29）：定制类产品（组合产品）标记 —— 1 是 / 0 否。
    #   勾选后 product_name/product_code 即「定制类产品」名称/代码，组合组件登记在
    #   CrmContractComponent；佣金/FYC/RYC 仍按整个定制产品在 CrmContractTier 录一套。
    is_custom = db.Column(db.Integer, default=0, comment='是否定制类产品（组合产品）：1 是 / 0 否')
    # 四十三改（2026-10-29）：销售渠道 —— online 网销 / offline 线下 / 空 未指定。
    #   渠道不同费用政策口径不同（网销多为线上专属费率），故在合约级标一个。
    sale_channel = db.Column(db.String(16), default='', comment='销售渠道：online 网销 / offline 线下')
    # 四十四改（2026-10-29）：AI 悬浮机器人识别协议后生成的**待审草稿**标记。
    #   1＝这条合约是 AI 按协议内容自动填的、还没经过人工核对（合约列表默认过滤掉）；
    #   用户在标准编辑页点一次「保存」即清 0，正式转为生效合约。0＝正常合约。
    ai_draft = db.Column(db.Integer, default=0, comment='AI 识别草稿：1 待人工核对 / 0 正常')
    ai_note = db.Column(db.Text, default='', comment='AI 识别说明（抽到哪些、哪几项要人工补）')
    insurance_type = db.Column(db.String(32), comment='险种类型（常量 crm.INSURANCE_TYPES）')
    status = db.Column(db.String(16), default='active', comment='状态：draft 草稿 / active 生效 / ended 终止')
    # ③ 缴费
    pay_type = db.Column(db.String(8), comment='缴费类型：year 年缴 / month 月缴')
    pay_period = db.Column(db.String(16), comment='缴别（常量 crm.PAY_PERIODS）')
    # ④ 金额
    first_income = db.Column(db.Float, default=0, comment='首期总收入（元）')
    # ⑤ 总总方案
    tt_fyc = db.Column(db.Float, default=0, comment='总总FYC（首年佣金率 %）')
    tt_coef = db.Column(db.Float, default=0, comment='总总方案·奖金系数')
    tt_amount = db.Column(db.Float, default=0, comment='总总方案·奖金金额（元）')
    tt_other = db.Column(db.String(200), comment='总总方案·其他')
    tt_start = db.Column(db.String(16), comment='总总方案起始日 YYYY-MM-DD')
    tt_end = db.Column(db.String(16), comment='总总方案终止日 YYYY-MM-DD')
    # ⑥ 分分方案
    ff_fyc = db.Column(db.Float, default=0, comment='分分FYC（首年佣金率 %）')
    ff_coef = db.Column(db.Float, default=0, comment='分分方案·奖金系数')
    ff_amount = db.Column(db.Float, default=0, comment='分分方案·奖金金额（元）')
    ff_other = db.Column(db.String(200), comment='分分方案·其他')
    ff_start = db.Column(db.String(16), comment='分分方案起始日 YYYY-MM-DD')
    ff_end = db.Column(db.String(16), comment='分分方案终止日 YYYY-MM-DD')
    # ⑦ 首年合计（后端自动算，不手工填）
    fyc_total = db.Column(db.Float, default=0, comment='首年FYC合计（%）= 总总FYC + 分分FYC')
    plan_total = db.Column(db.Float, default=0, comment='首年方案合计（元）= 总总方案奖金金额 + 分分方案奖金金额')
    grand_total = db.Column(db.Float, default=0, comment='首年总合计 = 首年FYC合计 + 首年方案合计')
    # ⑧ 续期（一律为续期佣金率 %）
    ryc_2 = db.Column(db.Float, default=0, comment='RYC-2 续期佣金率 %')
    ryc_3 = db.Column(db.Float, default=0, comment='RYC-3 续期佣金率 %')
    ryc_4 = db.Column(db.Float, default=0, comment='RYC-4 续期佣金率 %')
    ryc_5 = db.Column(db.Float, default=0, comment='RYC-5 续期佣金率 %')
    ryc_6 = db.Column(db.Float, default=0, comment='RYC-6 续期佣金率 %')
    ryc_7 = db.Column(db.Float, default=0, comment='RYC-7 续期佣金率 %')
    ryc_8 = db.Column(db.Float, default=0, comment='RYC-8 续期佣金率 %')
    ryc_9 = db.Column(db.Float, default=0, comment='RYC-9 续期佣金率 %')
    ryc_10 = db.Column(db.Float, default=0, comment='RYC-10 续期佣金率 %')
    # ⑨ 继续率考核（二期起改为引用规则库；下列旧列停止写入，仅兼容一期数据）
    persist_rule_id = db.Column(db.Integer, comment='关联继续率考核规则 CrmPersistRule.id（多产品共用）')
    persist_rule_name = db.Column(db.String(64), comment='继续率考核规则名称（冗余快照）')
    persist_metric = db.Column(db.String(64), comment='继续率考核指标（旧：一期合约内联字段）')
    persist_coef = db.Column(db.Float, default=0, comment='继续率奖金系数')
    long_service_coef = db.Column(db.Float, default=0, comment='长期服务奖系数')
    # ⑩ 倒扣机制
    has_clawback = db.Column(db.Boolean, default=False, comment='是否有倒扣机制')
    clawback_std = db.Column(db.Text, comment='倒扣机制考核标准')
    # ⑪ 附件与其他
    file_name = db.Column(db.String(255), comment='协议扫描件原文件名')
    file_path = db.Column(db.String(255), comment='协议扫描件落盘文件名')
    # ⑫ 文字类方案（十三改 2026-10-02）
    # 涉及金额 / 旅游 / 实物 / 权益的方案，无法按缴别算系数 → 统一收进合约级一个文本框，
    # 纯文字描述、**不参与系数合计与报表合并**，报表里以「其他现金、实物或权益方案」标识呈现。
    misc_plan_text = db.Column(db.Text, comment='其他现金、实物或权益方案（文字描述，不参与合计）')
    note = db.Column(db.Text, comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmContractFile(db.Model):
    """产品合约的协议附件（四十三改，2026-10-29）—— 一条合约可挂多个扫描件。

    威哥口径：上传成功的文件要**以列表方式呈现**、并且能逐个删除。
    旧版只有主表 `CrmProductContract.file_name/file_path` 两个字段（单文件、再传即覆盖），
    本表上线后按「一条记录 = 一个文件」存；旧字段保留不删，读取时由
    `crm._contract_files()` 自动把旧字段里的那个文件补登进来（零迁移、老数据不丢）。
    本表靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'),
                            comment='关联产品合约 CrmProductContract.id')
    file_name = db.Column(db.String(255), comment='原文件名')
    file_path = db.Column(db.String(255), comment='落盘文件名（static/uploads/crm/contract/）')
    file_size = db.Column(db.Integer, default=0, comment='文件大小（字节）')
    upload_by = db.Column(db.Integer, comment='上传人 user_id')
    upload_at = db.Column(db.DateTime, default=datetime.utcnow, comment='上传时间')
    sort_no = db.Column(db.Integer, default=0, comment='展示顺序')


class CrmProductDoc(db.Model):
    """产品资料库：按「保司 + 产品」归集的资料附件。

    独立于具体合约——同一产品的多份材料集中存放，供业务人员查阅下载：
    保司微投指南、产品条款、产品说明书、费率表、现价表、投保规则、EXCEL 版计划书等。
    """
    id = db.Column(db.Integer, primary_key=True)
    insurer_id = db.Column(db.Integer, db.ForeignKey('insurer.id'), comment='关联保司 Insurer.id')
    insurer_name = db.Column(db.String(128), comment='保司名称（冗余快照）')
    product_name = db.Column(db.String(128), comment='产品名称')
    contract_id = db.Column(db.Integer, comment='可选关联的产品合约 id（CrmProductContract.id）')
    doc_type = db.Column(db.String(24), comment='资料类型（常量 crm.DOC_TYPES）')
    title = db.Column(db.String(128), comment='资料标题')
    version = db.Column(db.String(32), comment='版本 / 生效日期说明')
    file_name = db.Column(db.String(255), comment='附件原文件名')
    file_path = db.Column(db.String(255), comment='附件落盘文件名')
    file_size = db.Column(db.Integer, default=0, comment='附件大小（字节）')
    note = db.Column(db.String(200), comment='备注')
    upload_by = db.Column(db.Integer, comment='上传人 user_id')
    upload_at = db.Column(db.DateTime, default=datetime.utcnow, comment='上传时间')


class CrmUniqueRule(db.Model):
    """CRM 唯一性校验规则（十六改，2026-10-02）：后台可开关的「不允许重复录入」约束。

    威哥口径：同一保司、同一产品、同一缴别**只允许录入一次**；保司基础信息同样唯一。
    业务上「总总」与「分分」是两个独立口径（同一产品两个口径各录一次属正常），
    故默认按「同一口径内」唯一；想连跨口径也拦，把 `contract_cross_side` 打开即可。

    ⚠️ 规则只存**开关**，真正的比对逻辑在 crm.py（`_dup_insurer` / `_dup_branch` /
       `_dup_contract` / `_dup_tiers`）。查不到规则行时**默认按启用处理**（零配置也安全）。
    本表靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='规则代号（代码里 switch 用）')
    name = db.Column(db.String(64), comment='规则名称（设置页展示）')
    group = db.Column(db.String(16), comment='分组：insurer 保司档案 / branch 省分公司 / contract 产品合约')
    desc = db.Column(db.String(200), comment='规则说明（设置页展示）')
    enabled = db.Column(db.String(8), default='on', comment='on 启用（拦重复） / off 停用（放行）')
    sort_no = db.Column(db.Integer, default=0, comment='展示顺序（小者在前）')
    updated_at = db.Column(db.String(20), comment='最后修改时间 YYYY-MM-DD HH:MM')


class CrmAgency(db.Model):
    """合作经代公司基础信息（十九改++，2026-10-02）。

    威哥口径：人力档案的「所属经代公司」不能手填，要有一个**维护端口**，
    字段登记规则**参照保司基础信息（Insurer）与保司省分公司（InsurerBranch）**。

    经代公司＝保险中介（代理 / 经纪）法人主体，业务人员挂在这些主体下出单。
    本表建好后，人力档案的经代公司下拉只从这里取值（库里历史值并进来兜底）。
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), unique=True, comment='经代公司全称（工商注册名）')
    short_name = db.Column(db.String(64), comment='经代公司简称')
    credit_code = db.Column(db.String(32), comment='统一社会信用代码（18 位）')
    license_no = db.Column(db.String(64), comment='保险中介业务许可证号（经营保险代理/经纪业务许可证）')
    tax_no = db.Column(db.String(32), comment='税号')
    legal_person = db.Column(db.String(32), comment='法定代表人')
    reg_capital = db.Column(db.String(32), comment='注册资本')
    establish_date = db.Column(db.String(16), comment='成立日期 YYYY-MM-DD')
    address = db.Column(db.String(200), comment='工商营业地址')
    phone = db.Column(db.String(32), comment='联系电话')
    website = db.Column(db.String(128), comment='官方网站')
    business_scope = db.Column(db.Text, comment='经营范围')
    agency_type = db.Column(db.String(16), comment='机构类型：agent 保险代理 / broker 保险经纪 / adjust 保险公估 / other 其他')
    status = db.Column(db.String(16), default='coop', comment='合作状态：coop 合作中 / paused 暂停 / none 未合作')
    contact_person = db.Column(db.String(32), comment='对接人姓名')
    contact_title = db.Column(db.String(64), comment='对接人身份与职务')
    contact_phone = db.Column(db.String(32), comment='对接人联系电话')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmTeamCoefficient(db.Model):
    """业务团队 · 团队系数设置（二十一改，2026-10-03）。

    威哥口径：对齐「保险佣金查询」小程序的分配机制 ——
    **系数 × 基准费率**，而非职级之间层层往下分比例。

    模型 = 小程序 generateCommission 的 computeOne 系数：
      · 底稿存「基准费率」（对应现有 CrmContractTier.tt_fyc/ff_fyc 等）；
      · 生成时对同批底稿套「团队系数」，最终佣金 = 基准费率 × 系数。

    三类系数**互相独立**、都是百分比数值（如 70 = 70%）：
      · kf = FYC 系数（基础协议费用）；
      · kr = RYC 系数（续年度佣金，单独一套）；
      · ka = 加佣方案系数（变动费用）。

    团队类型两套：
      · basic   基本法团队 —— 统一一套 kf/kr/ka，owner_name 为空；
      · channel 渠道团队 —— **每个渠道负责人各挂一套** kf/kr/ka（owner_name 必填）。

    计算口径（_commission_calc）：
        FYC合计  = (总总FYC + 分分FYC) × kf/100
        加佣合计 = (总总加佣 + 分分加佣) × ka/100
        总合计   = FYC合计 + 加佣合计
        RYC_n    = 底稿RYC_n × kr/100
    ⚠️ 只作用于 FYC + 方案加佣；RYC 用 kr 单独乘；继续率考核、增员奖不受影响。

    本表为新增表，靠 create_all 直建，不入 seed._migrate_columns。
    （取代已废弃的 CrmTeamQuota / CrmTeamPolicy 两套「职级分层比例」模型。）
    """
    id = db.Column(db.Integer, primary_key=True)
    team_type = db.Column(db.String(16), index=True,
                          comment='团队类型：basic 基本法团队 / channel 渠道团队')
    owner_name = db.Column(db.String(64), default='',
                           comment='渠道负责人姓名（仅 channel 填，basic 为空；channel 每个负责人一条）')
    # 二十二改（2026-10-05）：适用范围细分到「分区 / 营业部」（威哥口径：架构向下最低指向营业部）。
    # 空 = 总计 / 全员（不限定）；组合键 = team_type + owner_name + scope_zone + scope_dept。
    scope_zone = db.Column(db.String(32), default='', comment='适用分区（空 = 全部分区 / 总计）')
    scope_dept = db.Column(db.String(64), default='', comment='适用营业部（空 = 全部营业部 / 全员）')
    kf = db.Column(db.Float, default=100, comment='FYC 系数 %（基础协议费用，如 70 = 70%）')
    kr = db.Column(db.Float, default=100, comment='RYC 系数 %（续年度佣金）')
    ka = db.Column(db.Float, default=100, comment='加佣方案系数 %（变动费用）')
    name = db.Column(db.String(64), comment='系数方案名称（如「基本法统一系数」「张三渠道系数」）')
    sort_no = db.Column(db.Integer, default=0, comment='同团队类型内的展示顺序（小者优先）')
    status = db.Column(db.String(8), default='on', comment='状态：on 启用 / off 停用')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmTaxConfig(db.Model):
    """业务团队 · 分配税费参数（二十二改，2026-10-05）。

    威哥口径：保司按签约费率结算给我方的是**含税价**（我方须开 6% 增值税发票、
    税费由我方承担）；给团队分配销售费用前，必须**先把公司承担的税费扣掉**，
    剩下的净额才是「公司可分配基数」，再乘团队系数分给团队。

    计算链路（_net_rate）：
        含税结算   = 保费 × 签约费率（如 62%）
        不含税收入 = 含税 ÷ (1 + 增值税率)
        增值税     = 不含税 × 增值税率
        附加税费   = 增值税 × 附加税率（城建税 + 教育费附加 + 地方教育附加）
        税后净额   = 含税 − 增值税 − 附加税费

    威哥确认：**全局统一一套**（不按保司分别配置）——故本表只保留一条有效行。
    本表为新增表，靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), default='默认税费参数', comment='参数名称')
    vat_rate = db.Column(db.Float, default=6.0, comment='增值税率 %（默认 6）')
    surcharge_rate = db.Column(db.Float, default=12.0,
                               comment='附加税费率 %（按增值税额计，默认 12 = 城建7+教育3+地方教育2）')
    status = db.Column(db.String(8), default='on', comment='状态：on 启用 / off 停用')
    note = db.Column(db.String(200), comment='备注')
    updated_by = db.Column(db.Integer, comment='最后修改人 user_id')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')


class CrmFetchConfig(db.Model):
    """保司档案 · 外部数据源配置（四十一改，2026-10-29）。

    威哥诉求：保司档案里「信用代码 / 法人 / 注册资本 / 地址」等工商字段一条条手填太费事，
    只填保司全称，其余希望系统自动抓。

    技术现实（已实测，别再试第二次）：
      · 国家金融监督管理总局「金融许可证信息（保险机构）」xkz.nfra.gov.cn
        —— 公开查询接口，**可编程抓取**，能给：保险许可证号 / 批准设立日期 / 规范全称。
      · 天眼查 / 企查查 / 爱企查 / 启信宝 —— 匿名访问一律 302 跳登录或有滑块加密反爬，
        **抓不到**；官方开放平台 API 按次收费、需申请 token。
      · 信用中国 412、国家企业信用信息公示系统 521（滑块）—— 同样不可编程。

    故本表只存两项开关/凭据，均为**全局单例**（id=1）：
      · nfra_enabled  监管站（免费，默认开）→ 抓许可证号 + 批准设立日期
      · tyc_token     天眼查开放平台 token（留空＝不启用）→ 抓工商字段
    本表为新增表，靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    nfra_enabled = db.Column(db.String(8), default='on',
                             comment='金融监管总局许可证库：on 启用 / off 停用（免费公共源）')
    tyc_token = db.Column(db.String(200), comment='天眼查开放平台 token（留空＝不启用）')
    tyc_enabled = db.Column(db.String(8), default='off',
                            comment='天眼查：on 启用 / off 停用（需 token）')
    note = db.Column(db.String(200), comment='备注')
    updated_by = db.Column(db.Integer, comment='最后修改人 user_id')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后修改时间')


class CrmOrgStruct(db.Model):
    """业务团队 · 人力架构基础信息（十九改，2026-10-02）。

    威哥口径：录人之前**先建人力架构**，人只能挂到已有的营业组上。
    层级：**省公司 → 分支 → 分区 → 营业部 → 营业组**。
    一条记录 = 一个**营业组**（最末级），上级链冗余存在同一行里，读起来最直观。

    分区 / 营业部 / 营业组编码由系统**自动生成**（层级式，一眼看出从属）：
        分区    FQ001
        营业部  FQ001-B01      （同分区下第 1 个营业部）
        营业组  FQ001-B01-G01  （同营业部下第 1 个营业组）
    新建时若「省公司 + 分支 + 分区」已存在则**复用**该分区的编码，营业部同理，
    避免同一个分区被反复建成 FQ001 / FQ002 两套编码。

    本表靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    province = db.Column(db.String(32), comment='省公司')
    branch = db.Column(db.String(64), comment='分支')
    zone = db.Column(db.String(32), comment='分区（短代号，如 一区）')
    zone_code = db.Column(db.String(32), comment='分区编码（系统自动生成 FQ001）')
    zone_name = db.Column(db.String(64), comment='分区名称（如 广州第一分区）')
    dept = db.Column(db.String(64), comment='营业部')
    dept_code = db.Column(db.String(32), comment='营业部编码（自动 FQ001-B01）')
    group_name = db.Column(db.String(64), comment='营业组')
    group_code = db.Column(db.String(32), comment='营业组编码（自动 FQ001-B01-G01）')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')

    @property
    def path(self):
        """架构全路径（列表 / 人员归属展示用）。"""
        return ' / '.join([x for x in [self.province, self.branch, self.zone_name or self.zone,
                                       self.dept, self.group_name] if x])


class CrmManpower(db.Model):
    """业务团队 · 人力数据清单（十九改，2026-10-02）。

    威哥口径：做「业务团队费用设置」先要有人员底表 —— 费用政策是**发给谁**的，
    所以人员归属（团队类型 / 职级 / 推荐关系 / 银行卡）必须先落库：
      · **基本法团队**：统一套一套**标准化**费用政策（不分层，全员同一口径）；
      · **渠道团队**：可**分层级**（渠道总 → 团队长 → 营销员）下发不同费用政策。
    本表是后续「费用政策分配」的取数来源，本期只做清单本身的增删改查 + 导入导出。

    业务主键＝**代理人工号**（agent_no），代码层判重（不建唯一约束，避免批量导入时整批失败）；
    在职状态**不落库**，由离职时间 leave_date 推导（空 或 > 今天 = 在职），避免两处数据打架。

    日期一律 String（项目既有口径，SQLite 下不做日期类型）；本表为新增表，
    靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    province = db.Column(db.String(32), comment='省公司')
    branch = db.Column(db.String(64), comment='分支')
    # 十九改补充：工号有**两个**——
    #   sys_no   海盈通保**系统工号**，自动生成 SA-001 格式，全系统唯一、不可改（本表的业务主键）
    #   agent_no **代理人工号**（在海腾上号的工号），可空、填了才唯一（不是每个人都在海腾上号）
    sys_no = db.Column(db.String(32), index=True, comment='系统工号（自动生成 SA-001）')
    agent_no = db.Column(db.String(32), index=True, comment='代理人工号（海腾上号工号，选填）')
    agency = db.Column(db.String(128), comment='所属经代公司')
    name = db.Column(db.String(32), comment='姓名')
    rank_code = db.Column(db.String(32), comment='职级编码')
    rank = db.Column(db.String(16), comment='职级：channel_boss 渠道总 / team_leader 团队长 / agent 营销员')
    # 十九改+（2026-10-02）：原名「销售能力等级」，按监管口径改为**销售资质等级** 一级~四级
    #   一级最高（可售 P1-P5 全品类），四级入门（仅可售 P1-P2）；资质不得低于所售产品等级
    sale_level = db.Column(db.String(16), comment='销售资质等级：一级 / 二级 / 三级 / 四级')
    hire_date = db.Column(db.String(16), comment='入司时间 YYYY-MM-DD')
    leave_date = db.Column(db.String(16), comment='离职时间 YYYY-MM-DD（空 = 在职）')
    ref_name = db.Column(db.String(32), comment='推荐人姓名（填姓名后工号自动带出）')
    ref_no = db.Column(db.String(32), comment='推荐人工号')
    ref_rank = db.Column(db.String(16), comment='推荐人职级')
    idcard = db.Column(db.String(32), comment='身份证号码')
    gender = db.Column(db.String(8), comment='性别：male 男 / female 女')
    birth_date = db.Column(db.String(16), comment='出生年月 YYYY-MM-DD')
    team_type = db.Column(db.String(16), comment='团队类型：basic 基本法团队 / channel 渠道团队')
    team_name = db.Column(db.String(64), comment='所属团队（取所挂营业组名）')
    # 十九改补充：人员必须挂到「人力架构」的某个营业组上（先建架构、再录人）
    org_id = db.Column(db.Integer, index=True, comment='所属营业组 CrmOrgStruct.id')
    org_path = db.Column(db.String(200), comment='架构全路径快照（省/分支/分区/营业部/营业组）')
    bank_name = db.Column(db.String(64), comment='开户行（下拉选，主流银行）')
    bank_branch = db.Column(db.String(128), comment='开户支行')
    bank_card = db.Column(db.String(32), comment='银行卡号')
    signed = db.Column(db.String(8), default='no', comment='是否签署渠道协议：yes 是 / no 否')
    sign_date = db.Column(db.String(16), comment='签署日期 YYYY-MM-DD')
    # 十九改+：三个**非强制**电子版附件（原文件名 + 落盘文件名，同 CRM 其他附件口径）
    #   sign_file   渠道协议电子版（选「已签」时才需要）
    #   idcard_file 身份证电子版
    #   bank_file   银行卡电子版
    sign_file_name = db.Column(db.String(255), comment='渠道协议电子版 · 原文件名')
    sign_file = db.Column(db.String(255), comment='渠道协议电子版 · 落盘文件名')
    idcard_file_name = db.Column(db.String(255), comment='身份证电子版（正面）· 原文件名')
    idcard_file = db.Column(db.String(255), comment='身份证电子版（正面）· 落盘文件名')
    # 十九改++（2026-10-02）：身份证要**正反面**各一张
    idcard_file2_name = db.Column(db.String(255), comment='身份证电子版（反面）· 原文件名')
    idcard_file2 = db.Column(db.String(255), comment='身份证电子版（反面）· 落盘文件名')
    bank_file_name = db.Column(db.String(255), comment='银行卡电子版 · 原文件名')
    bank_file = db.Column(db.String(255), comment='银行卡电子版 · 落盘文件名')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# =====================================================================
# 核心业务（CRM）二期 2026-10-01
#   · 保司档案扩为「工商 + 省分公司 + 岗位人事 + 年度经营指标」四位一体画像
#   · 产品合约拆为「产品级主表 + 缴别明细子表」，继续率考核抽成独立规则库
#   口径（威哥二次确认）：三个合计一律为百分比系数；奖金金额不参与合计、仅文字展示；
#   现金形式的费用政策一律归「协议外」（pay_form=cash → policy_scope=out）。
#   本组为纯新增表，靠 create_all 直建，不入 seed._migrate_columns。
# =====================================================================


class InsurerBranch(db.Model):
    """保司省分公司：保险公司在各省 / 直辖市设立的分公司基础信息。

    总公司（Insurer）与省分公司是一对多；产品合约按「总-分 / 分-分」签约时引用。
    """
    id = db.Column(db.Integer, primary_key=True)
    insurer_id = db.Column(db.Integer, db.ForeignKey('insurer.id'), comment='关联总公司 Insurer.id')
    insurer_name = db.Column(db.String(128), comment='总公司名称（冗余快照）')
    province = db.Column(db.String(32), comment='省 / 直辖市 / 计划单列市（如 广东省、深圳市）')
    name = db.Column(db.String(128), comment='分公司全称')
    short_name = db.Column(db.String(64), comment='分公司简称')
    credit_code = db.Column(db.String(32), comment='统一社会信用代码')
    reg_no = db.Column(db.String(32), comment='工商注册号')
    license_no = db.Column(db.String(64), comment='经营保险业务许可证号')
    tax_no = db.Column(db.String(32), comment='税号')
    principal = db.Column(db.String(32), comment='分公司负责人（省分总经理）')
    phone = db.Column(db.String(32), comment='分公司联系电话')
    address = db.Column(db.String(200), comment='营业地址')
    establish_date = db.Column(db.String(16), comment='成立日期 YYYY-MM-DD')
    # 四十二改（2026-10-29）：省分公司也支持一键抓取 —— 监管站口径的许可证批准日
    #   单独一列，不混进 establish_date（工商成立日），与总公司 Insurer.license_date 对齐。
    license_date = db.Column(db.String(16), comment='许可证批准日期 YYYY-MM-DD（监管口径）')
    fetch_try_at = db.Column(db.String(20), comment='上次抓取尝试时间（批量轮转排队用）')
    contact_person = db.Column(db.String(32), comment='我方对接人')
    contact_phone = db.Column(db.String(32), comment='我方对接人电话')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class InsurerContact(db.Model):
    """保司岗位人员联络簿：总经理 / 中介分管总 / 中介渠道负责人 / 对接人。

    「动态手工更新」= 人员变动时新增一条记录，并把该岗位原「现任」记录标记为离任
    （写 end_date），从而保留任职历史，供销售端判断找谁对接。
    """
    id = db.Column(db.Integer, primary_key=True)
    insurer_id = db.Column(db.Integer, db.ForeignKey('insurer.id'), comment='关联总公司 Insurer.id')
    insurer_name = db.Column(db.String(128), comment='总公司名称（冗余快照）')
    branch_id = db.Column(db.Integer, comment='可选关联省分公司 InsurerBranch.id')
    role = db.Column(db.String(16), comment='岗位：gm 总经理 / mid_head 中介分管总 / channel_head 中介渠道负责人 / contact 对接人 / other 其他')
    name = db.Column(db.String(32), comment='姓名')
    phone = db.Column(db.String(32), comment='联系电话')
    status = db.Column(db.String(16), default='current', comment='任职状态：current 现任 / left 离任')
    start_date = db.Column(db.String(16), comment='上任日期 YYYY-MM-DD')
    end_date = db.Column(db.String(16), comment='离任日期 YYYY-MM-DD')
    note = db.Column(db.String(200), comment='备注（分管范围等）')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class InsurerLinkman(db.Model):
    """保司 / 省分公司·联络人（一对多，自由登记）——「联系与对接」段。

    与 InsurerContact（岗位人员联络簿，固定角色 + 任职历史）**不同**：
    本表是**自由的多联络人列表**，一个保司或一个省分公司都可登记多人，
    每人字段 = 联络人姓名 / 身份与职务 / 联系电话 / 邮箱 / 备注。

    owner_type 区分归属主体（insurer 总公司 / branch 省分公司），owner_id 指向对应主表 id；
    保存时按 owner 全量替换（同 _save_tiers 口径）。纯新增表，create_all 直建，
    不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    owner_type = db.Column(db.String(8), comment='归属类型：insurer 总公司 / branch 省分公司')
    owner_id = db.Column(db.Integer, comment='归属主体 id（Insurer.id 或 InsurerBranch.id）')
    name = db.Column(db.String(32), comment='联络人姓名')
    title = db.Column(db.String(64), comment='联络人身份与职务')
    phone = db.Column(db.String(32), comment='联络人联系电话')
    email = db.Column(db.String(64), comment='联络人邮箱')
    note = db.Column(db.String(200), comment='备注')
    sort_no = db.Column(db.Integer, default=0, comment='排序号（页面展示顺序）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class InsurerMetric(db.Model):
    """保司年度经营指标：投资回报率 / 综合投资收益率 / 评级 / 年度总保费等。

    按「保司 + 年度」唯一，页面直接改、保存即写库（Upsert），支持实时手工更新；
    结构化落库，为未来销售端（小程序）提供销售策略数据参考。
    """
    id = db.Column(db.Integer, primary_key=True)
    insurer_id = db.Column(db.Integer, db.ForeignKey('insurer.id'), comment='关联保司 Insurer.id')
    insurer_name = db.Column(db.String(128), comment='保司名称（冗余快照）')
    year = db.Column(db.String(8), comment='年度（如 2025）')
    roi = db.Column(db.Float, default=0, comment='投资回报率（%）')
    composite_yield = db.Column(db.Float, default=0, comment='综合投资收益率（%）')
    rating = db.Column(db.String(16), comment='风险综合评级：AAA / AA / A / BBB / BB / B / C / D')
    rating_org = db.Column(db.String(32), comment='评级机构 / 来源（监管 / 惠誉 / 标普 等）')
    premium = db.Column(db.Float, default=0, comment='当年度总保费')
    premium_unit = db.Column(db.String(8), default='yi', comment='保费单位：yi 亿元 / wan 万元')
    net_profit = db.Column(db.Float, default=0, comment='净利润（与保费同单位）')
    solvency = db.Column(db.Float, default=0, comment='核心偿付能力充足率（%）')
    source = db.Column(db.String(64), comment='数据来源（年报 / 监管披露 / 手工录入）')
    note = db.Column(db.String(200), comment='备注')
    updated_by = db.Column(db.Integer, comment='最后更新人 user_id')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, comment='最后更新时间')


class CrmContractComponent(db.Model):
    """定制类产品·组合组件：一条 = 一个定制类产品（组合产品）下的一个基础/附加产品。

    二十七改2（2026-10-29）威哥口径：达尔文12号＝主险（优选K重疾）+ 附加险（投保人豁免 B款）
    这类「定制类产品」，在合约录入时勾选「定制类产品」，再按组合种类个数登记组件。
    本表**只登记组合构成**（组件保司 / 产品名称 / 代码 / 险种 / 备注），
    佣金 / FYC / RYC 仍按**整个定制产品**在 CrmContractTier 录一套 —— 不做组件级费率拆分，
    这样佣金查询 / 销售费用清单天然「合并为定制产品一行」，不重复计数（威哥 2026-10-29 拍板）。
    """
    __tablename__ = 'crm_contract_component'
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'), index=True,
                            comment='关联定制类产品合约 CrmProductContract.id')
    insurer_id = db.Column(db.Integer, comment='组件保司 id（可空＝同主保司）')
    insurer_name = db.Column(db.String(128), comment='组件保司名称（冗余快照）')
    product_name = db.Column(db.String(128), comment='组件产品名称（如 复星联合优选K重大疾病保险）')
    product_code = db.Column(db.String(64), comment='组件产品代码')
    insurance_type = db.Column(db.String(32), comment='组件险种类型')
    sort_no = db.Column(db.Integer, default=0, comment='排序序号')
    note = db.Column(db.String(255), comment='备注（如 主险 / 附加险）')


class CrmContractTier(db.Model):
    """产品合约·缴别基础合约：一条 = 一个缴别的**基础费用政策**（核心，必录）。

    一款产品多种缴别（趸交 / 2~30 年交 / 交至 50·55·60·65 岁），每缴别的基础合约不同，
    故从主表拆出，按缴别分行存放；RYC-2 ~ RYC-10 同属该缴别的基础合约。

    口径（威哥 2026-10-01 三改确认）：
      · 缴别基础合约只含 **FYC（总总/分分）+ RYC-2~10 + 首期总收入**；
        三个首年合计中**仅保留 FYC 合计**（fyc_total = 总总FYC + 分分FYC，%）。
      · **方案政策（总总/分分系数、奖金金额、方案起止日、协议内外）已拆出到
        CrmContractPlan**——一个缴别可挂多条方案政策（按保司阶段/期次灵活登记，可空），
        故缴别级不再有「方案合计 / 首总合计」（无唯一总额），每条方案各自算 plan_total。
      · 下列字段为二期历史遗留、自三改起**不再写入**：tt_coef/tt_amount/tt_other/
        ff_coef/ff_amount/ff_other/policy_scope/pay_form/plan_total/grand_total。
        模型保留这些列仅为兼容一期存量数据，新代码不读写。
    """
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'), comment='关联主表 CrmProductContract.id')
    # 二十八改（2026-10-29）：所属组合组件 —— 定制类产品「按组件分别录费率」的落点。
    #   空 = 常规单一产品，或定制产品的「整体」费率行；有值 = 该缴别费率属于某个组件。
    component_id = db.Column(db.Integer, db.ForeignKey('crm_contract_component.id'), index=True,
                             comment='所属组合组件 CrmContractComponent.id（空＝常规产品/整体）')
    # ① 缴别
    pay_period = db.Column(db.String(16), comment='缴别（常量 crm.PAY_PERIODS）')
    first_income = db.Column(db.Float, default=0, comment='首期总收入（元）——已废弃，仅兼容一期，新代码不读写')
    # ② 基础协议费用·有效期（总总 / 分分各自一套费率，一定时段内保持不变）
    tt_start = db.Column(db.String(16), comment='总总·合约起始日 YYYY-MM-DD（该费率的有效期起）')
    tt_end = db.Column(db.String(16), comment='总总·合约终止日 YYYY-MM-DD（该费率的有效期止）')
    ff_start = db.Column(db.String(16), comment='分分·合约起始日 YYYY-MM-DD')
    ff_end = db.Column(db.String(16), comment='分分·合约终止日 YYYY-MM-DD')
    # ③ 政策归属
    policy_scope = db.Column(db.String(8), default='in', comment='政策归属：in 协议内 / out 协议外（现金形式强制协议外）')
    pay_form = db.Column(db.String(16), default='transfer', comment='费用形式：cash 现金 / transfer 银行转账 / other 其他')
    # ③ 总总方案
    tt_fyc = db.Column(db.Float, default=0, comment='总总FYC（首年佣金率 %）')
    tt_coef = db.Column(db.Float, default=0, comment='总总方案·奖金系数（%）')
    tt_amount = db.Column(db.Float, default=0, comment='总总方案·奖金金额（元，不参与合计、仅展示）')
    tt_other = db.Column(db.String(200), comment='总总方案·其他约定')
    # ④ 分分方案
    ff_fyc = db.Column(db.Float, default=0, comment='分分FYC（首年佣金率 %）')
    ff_coef = db.Column(db.Float, default=0, comment='分分方案·奖金系数（%）')
    ff_amount = db.Column(db.Float, default=0, comment='分分方案·奖金金额（元，不参与合计、仅展示）')
    ff_other = db.Column(db.String(200), comment='分分方案·其他约定')
    # ⑤ 首年合计（后端自动算，一律为 %）
    fyc_total = db.Column(db.Float, default=0, comment='首年FYC合计（%）= 总总FYC + 分分FYC')
    plan_total = db.Column(db.Float, default=0, comment='首年方案合计（%）= 总总系数 + 分分系数')
    grand_total = db.Column(db.Float, default=0, comment='首年总合计（%）= 首年FYC合计 + 首年方案合计')
    # ⑥ 续期（一律为续期佣金率 %，同属该缴别）
    ryc_2 = db.Column(db.Float, default=0, comment='RYC-2 续期佣金率 %')
    ryc_3 = db.Column(db.Float, default=0, comment='RYC-3 续期佣金率 %')
    ryc_4 = db.Column(db.Float, default=0, comment='RYC-4 续期佣金率 %')
    ryc_5 = db.Column(db.Float, default=0, comment='RYC-5 续期佣金率 %')
    ryc_6 = db.Column(db.Float, default=0, comment='RYC-6 续期佣金率 %')
    ryc_7 = db.Column(db.Float, default=0, comment='RYC-7 续期佣金率 %')
    ryc_8 = db.Column(db.Float, default=0, comment='RYC-8 续期佣金率 %')
    ryc_9 = db.Column(db.Float, default=0, comment='RYC-9 续期佣金率 %')
    ryc_10 = db.Column(db.Float, default=0, comment='RYC-10 续期佣金率 %')
    sort_no = db.Column(db.Integer, default=0, comment='排序号（页面展示顺序）')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmContractRycStep(db.Model):
    """续期服务费·继续率分档（二十九改，2026-10-29）—— 续期佣金的「特殊通道」。

    业务背景（达尔文12号 服务费详情实证）：
      很多产品的**续期服务费不是一个固定值**，而是按「继续率」分档发放，例如 20 年交：
        次年：继续率 0%~80% → 0%；80%~88% → 30%；88%~100% → 50%
        第3年：继续率 0%~80% → 0%；80%~93% → 12.6%；93%~100% → 18%
      且**每个年度的档位阈值各不相同**（次年 80/88，第3年 80/93），
      所以不能用一套共用区间，必须「按年度各存一组档位」。

    存法：一行 = 某缴别某年度的**一个档位**（继续率区间 → 该档续期服务费率）。
      · 继续率落在 [min_rate, max_rate) 区间内时，该年度续期服务费率为 fee_rate；
        **max_rate 为空 ＝ 无上限**（即 ≥ min_rate 就是该档）。
      · 三十一改（2026-10-29）：威哥「继续率是要有区间的」——补 max_rate，
        录入界面同时改成**「区间做行 × 年度做列」的矩阵**（一个区间行里直接填 RYC2/RYC3…），
        避免同一套区间在每个年度块里重复录一遍。
        同一区间跨年度共用（如 35 年缴：0≤X<80 时 RYC2=0、RYC3=0；80≤X<88 时 RYC2=30、RYC3=50）。
        ⚠️ 若某产品各年度阈值不同（如 20 年交：次年 80/88，第3年 80/93），
          把区间拆细即可（80-88、88-93、93-100），仍然表达得出来。
      · 同一 (tier_id, ryc_year) 下多行按 min_rate 升序，取「满足条件的最大 min_rate」档；
      · **满档费率**（min_rate 最大的那档）在保存时回写到 CrmContractTier.ryc_N，
        这样佣金查询 / 销售费用清单 / 团队系数 / 试算器全链路零改动即按满档计算。
    """
    __tablename__ = 'crm_contract_ryc_step'

    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'), index=True,
                            comment='关联合约 CrmProductContract.id（便于按合约级联清理）')
    tier_id = db.Column(db.Integer, db.ForeignKey('crm_contract_tier.id'), index=True,
                        comment='关联缴别 CrmContractTier.id')
    ryc_year = db.Column(db.Integer, default=2, comment='续期年度：2 = 次年(RYC-2)，3 = 第3年(RYC-3) …')
    min_rate = db.Column(db.Float, default=0, comment='继续率下限 %（含）')
    # 三十一改（2026-10-29）：继续率区间必须有上限 —— 威哥「继续率是要有区间的」。
    # 空＝没有上限（≥min 即该档，等价到 100%）。
    max_rate = db.Column(db.Float, nullable=True, comment='继续率上限 %（不含）；空＝无上限')
    fee_rate = db.Column(db.Float, default=0, comment='该档续期服务费率 %')
    sort_no = db.Column(db.Integer, default=0, comment='排序号')
    note = db.Column(db.String(128), comment='备注（如「首续期总包最高」等口径说明）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmRycRatio(db.Model):
    """续期服务费比例（四十改，2026-10-29）—— 整个定制类产品共用一份的「发放比例」。

    业务背景（威哥 2026-10-29 截图实证，达尔文12号）：

        统计周期   继续率要求            续期服务费比例
        次年       0% ≤ X < 80%          0
                   80% ≤ X < 88%         60
                   88% ≤ X ≤ 100%        100
        第3年      0% ≤ X < 80%          0
                   80% ≤ X < 93%         70
                   93% ≤ X ≤ 100%        100

    这是「乘数」而不是「费率」：实发续期服务费 = 该缴别当年的基础费率（RYC-N）
    × 本表给出的比例。例：RYC-2=50%、继续率 85% 落在 80~88 档（比例 60%）
    → 实发 50% × 60% = 30%；RYC-3=18% × 70% = 12.6%（与达尔文12号服务费详情完全吻合）。

    ⚠️ 与 CrmContractRycStep 的分工（重要）：
      · 旧表 CrmContractRycStep = 「缴别级 · 区间×年度 → 实发费率」，同一套政策要在每个缴别
        各录一遍，且存的是已经乘过的结果，改一次基础费率要重算所有档。
      · 本表 = 「合约级 · 区间×年度 → 比例%」，**整个定制产品（所有缴别、所有组件）共用一份**，
        正是威哥说的「对于定制产品整体」。基础费率仍由缴别卡的 RYC-N 输入框维护。
      · 旧表数据保留留档，表单不再录入；金额类计算一律按满档（比例通常 100%），口径不变。

    存法：一行 = 某年度的一个档位（继续率区间 → 该档比例）。
      · max_rate 为空 ＝ 无上限（≥ min_rate 即该档）；
      · 同一年度多行按 min_rate 升序，取「满足条件的最大 min_rate」档；
      · 纯新增表，靠 create_all 直建，**不入** seed._migrate_columns。
    """
    __tablename__ = 'crm_ryc_ratio'

    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'), index=True,
                            comment='关联合约 CrmProductContract.id（整个定制产品共用一份）')
    ryc_year = db.Column(db.Integer, default=2, comment='续期年度：2 = 次年(RYC-2)，3 = 第3年(RYC-3) …')
    min_rate = db.Column(db.Float, default=0, comment='继续率下限 %（含）')
    max_rate = db.Column(db.Float, nullable=True, comment='继续率上限 %（不含）；空＝无上限')
    ratio_pct = db.Column(db.Float, default=100, comment='续期服务费比例 %（实发＝基础费率×本比例）')
    sort_no = db.Column(db.Integer, default=0, comment='排序号')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmPersistRule(db.Model):
    """继续率考核规则：多产品共用同一条规则，改规则所有引用它的合约同步生效。

    对应威哥「继续率考核基本上多产品都是一个规则」；合约主表 persist_rule_id 引用。
    具体档位（继续率区间 → 奖金系数 / 长期服务奖系数）在 CrmPersistRuleItem。

    ⚠️ 六改（2026-10-02）：继续率考核**不再挂在每个产品合约里选**，
      而是反过来在规则这一侧登记「适用范围」——保险公司 / 省分公司（非必选）/
      产品名称（多选）/ 缴别（多选）。命中范围的产品合约自动套用该规则，
      实现「祁 流程第 4 步：继续率规则匹配 保司→总总合约→分分合约」。

    ⚠️ 七改（2026-10-02）：改「两步制」——
      **第一步（本表 + CrmPersistRuleItem）**：建初始续期考核规则。
        保险公司（必选）+ 保司分公司（**可多选**，非必选）+
        继续率考核类型（13/25/37/49 个月继续率考核标准）+
        奖励项目（bonus 继续率奖金系数 / long_service 长期服务奖系数）+
        各档位（下限 / 上限 / 奖金系数 / 长期服务奖系数）+ 倒扣机制。
        **本步不再选产品、不再选缴别。**
      **第二步（「产品匹配」页）**：管理员选保司 → 系统自动弹出该保司产品 →
        产品多选 → 与本人已建的规则建立关联，写回本表 product_names。
        匹配**只用产品**，不再用缴别（pay_periods 保留兼容、不参与匹配）。

    ⚠️ 八改（2026-10-02）：奖励项目 ↔ 考核标准 ↔ 档位三者绑定（威哥口径）：
      · **继续率奖金系数**：先勾选本项，再**多选**继续率考核标准
        （13/25/37/49 个月继续率考核标准，存本表 cycles）；
        档位表按**每条已选标准分块**登记（下限 / 上限 / 继续率奖金系数）。
        → 档位明细的 cycle 列 = 该档位所属标准名。
      · **长期服务奖系数**：只关心「下限 / 上限 / 系数」，
        与 13/25 等月份**无关**，单独一块登记。
        → 档位明细的 cycle 列 = LS_CYCLE 哨兵值（'long_service'）。
      · 单值 cycle 列（兼容保留）= cycles 的第一个，老数据不丢。

    ⚠️ 九改（2026-10-02）：档位的「下限 / 上限」要能看出**是否含等号**（威哥口径）。
      例：13J＜88 系数 0；88%≤13J＜90 系数 2.5%——
      光填 88 / 90 看不出是「≤」还是「＜」，所以每行补两个运算符列：
        · min_op：'>=' 含等号（默认） / '>' 不含
        · max_op：'<'  不含（默认） / '<=' 含
      默认即行业惯用的「左闭右开」[下限, 上限)。
      规则名称改为**系统自动生成**（保司简称 + 继续率考核规则），不再手工填写；
      考核指标说明（本表 metric）不再录入（列保留，老数据不动）。

    ⚠️ 八改+（2026-10-02，对齐威哥提供的 6 份真实保司协议）：
      · **继续率算法口径 caliber / 精度规则 round_mode —— 十五改（2026-10-02）整块删除**：
          原「各家不同、需按合约登记」的口径与精度选择取消，系统统一为
          继续率精度 = 四舍五入、保留 1 位小数（见 crm.round_persist）。
          下列两列已从模型移除（老库若仍残留列，SQLAlchemy 忽略、零迁移）。
      · **倒扣机制结构化**（原来只有一段文本 clawback_std，看不出起扣线/基数）：
          clawback_line 起扣线（%，如 88 表示低于 88% 起扣）；
          clawback_base 扣费基数（all 全部推广费 / fyc 首年佣金 / ryc 续期佣金 /
                        persist 继续率奖金 / other 其他）；
          clawback_std 文本保留 = 具体公式说明（如幸福：退回费用 =
                        (88% − 实际达成值) × 第13月应收保单对应产生的全部推广费）。
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, comment='规则名称（留空则按「保司+考核类型」自动生成）')
    metric = db.Column(db.String(64), comment='考核指标说明（如 13 个月继续率）')
    cycle = db.Column(db.String(32), comment='（兼容保留）首个继续率考核标准；多选用 cycles')
    has_clawback = db.Column(db.Boolean, default=False, comment='是否有倒扣机制')
    clawback_std = db.Column(db.Text, comment='倒扣机制考核标准')
    status = db.Column(db.String(16), default='on', comment='状态：on 启用 / off 停用')
    note = db.Column(db.String(200), comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')
    # —— 六改：适用范围（在规则侧登记，反向匹配产品合约）——
    insurer_id = db.Column(db.Integer, comment='适用保险公司 Insurer.id（七改起必选）')
    insurer_name = db.Column(db.String(128), comment='保险公司名称（冗余快照）')
    branch_id = db.Column(db.Integer, comment='（兼容保留）首个适用省分公司 InsurerBranch.id')
    branch_name = db.Column(db.String(128), comment='适用省分公司名称串（多选时用「、」拼接）')
    product_names = db.Column(db.Text, comment='适用产品名称，JSON 数组文本（第二步「产品匹配」写入）')
    pay_periods = db.Column(db.Text, comment='（七改起不再使用）适用缴别，JSON 数组文本')
    # —— 七改：分公司改多选 + 奖励项目 ——
    branch_ids = db.Column(db.Text, comment='适用省分公司 InsurerBranch.id 列表，JSON 数组文本（空=不限分公司）')
    award_items = db.Column(db.String(64), comment='奖励项目：bonus 继续率奖金系数 / long_service 长期服务奖系数（逗号分隔）')
    # —— 八改：继续率考核标准（多选，仅勾选「继续率奖金系数」时有效）——
    cycles = db.Column(db.Text, comment='继续率考核标准列表，JSON 数组文本（13/25/37/49 个月继续率考核标准；空=未设）')
    # —— 八改+：倒扣结构化（caliber / round_mode 已于十五改删除，见上方类注释）——
    clawback_line = db.Column(db.Float, default=0, comment='倒扣起扣线（%），如 88 表示低于 88% 起扣')
    clawback_base = db.Column(db.String(16), default='all', comment='倒扣扣费基数：all 全部推广费 / fyc 首年佣金 / ryc 续期佣金 / persist 继续率奖金 / other 其他')
    # —— 二十四改（2026-10-28）：继续率规则跟着产品合约走 ——
    #   威哥口径：不再单独建「继续率考核规则」模块，改为在录产品合约时一并登记该产品的
    #   继续率奖金率（区间 × 缴别 → R13/R25/R37/R49）。一条合约对应一条规则。
    #   contract_id 为空 = 二十四改之前的独立规则（老数据兼容，_match_rules 仍走旧匹配）。
    contract_id = db.Column(db.Integer, comment='所属产品合约 CrmProductContract.id（空=独立规则）')


class CrmPersistRuleItem(db.Model):
    """继续率考核规则·档位明细：下限 / 上限 → 继续率奖金系数 或 长期服务奖系数。

    ⚠️ 八改（2026-10-02）：档位按「考核标准」归属（本表 cycle 列）：
      · cycle = 13/25/37/49 个月继续率考核标准 → 本行是**该标准下**的
        继续率奖金系数档位（bonus_coef 有值，long_service_coef = 0）。
      · cycle = LS_CYCLE（'long_service'）→ 本行是**长期服务奖系数**档位，
        与月份无关，只看下限/上限（long_service_coef 有值，bonus_coef = 0）。
    """
    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(db.Integer, db.ForeignKey('crm_persist_rule.id'), comment='关联规则 CrmPersistRule.id')
    cycle = db.Column(db.String(32), comment='档位归属：继续率考核标准名 / long_service（长期服务奖档位）')
    # —— 二十四改（2026-10-28）：档位带「缴别」维度 ——
    #   同一继续率区间，3年交 / 5年交 的奖金率可以不同（保司协议就是按缴费年期分行的）。
    #   空 = 全缴别通用（二十四改之前的独立规则数据）。
    pay_period = db.Column(db.String(32), comment='适用缴别（如 3年交）；空=全缴别通用')
    rate_min = db.Column(db.Float, default=0, comment='继续率下限（%）')
    rate_max = db.Column(db.Float, default=0, comment='继续率上限（%，0 表示不限）')
    # 九改：边界是否含等号（展示为 ≥ / ＞ 和 ＜ / ≤）
    min_op = db.Column(db.String(4), default='>=', comment='下限运算符：>= 含等号（默认） / > 不含')
    max_op = db.Column(db.String(4), default='<', comment='上限运算符：< 不含（默认） / <= 含等号')
    bonus_coef = db.Column(db.Float, default=0, comment='继续率奖金系数（%，仅考核标准档位用）')
    long_service_coef = db.Column(db.Float, default=0, comment='长期服务奖系数（%，仅长期服务奖档位用）')
    sort_no = db.Column(db.Integer, default=0, comment='排序号')
    note = db.Column(db.String(200), comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmContractPlan(db.Model):
    """产品合约·方案政策：挂在缴别基础合约下，**一个缴别可挂多条**（按保司阶段/期次灵活登记）。

    与 CrmContractTier（基础合约，必录核心）分离——基础合约登记的是 FYC / RYC / 继续率规则，
    方案政策是「根据保司阶段实际情况」追加的阶段性费用政策，可空、可多条、各带方案起止日。

    口径（威哥 2026-10-01 三改确认）：
      · plan_total = tt_coef + ff_coef（% 系数合计，后端自动算）；
        奖金金额 tt_amount / ff_amount（元）**不参与合计、仅单独展示**。
      · 政策归属 policy_scope（in 协议内 / out 协议外）**决定**费用形式 pay_form：
        协议内 → 只能是 transfer 协议内结算；
        协议外 → coef 系数 / cash 现金 / other 购物或其他（八改，后端强校验）。

    ⚠️ 八改+（2026-10-02，对齐真实协议）：本层对应协议里的「变动费用」，
      真实业务共 4 类（外加其他），故补 fee_type 费项类型：
        service 续期服务津贴 ／ quality 业务品质奖金 ／
        monthly 月度销售奖金（仅首年、续年度不发）／
        stage 阶段补充佣金·推动费（协议③阶段方案）／ other 其他。
      （首年佣金 / 续年佣金 / 继续率奖金属**基础协议费用**，登记在 CrmContractTier。）
    """
    id = db.Column(db.Integer, primary_key=True)
    contract_id = db.Column(db.Integer, db.ForeignKey('crm_product_contract.id'), comment='冗余关联主表')
    tier_id = db.Column(db.Integer, db.ForeignKey('crm_contract_tier.id'), comment='关联缴别基础合约 CrmContractTier.id')
    # ① 方案标识与期间（同一产品各阶段方案期间不同，故下放到方案层）
    plan_name = db.Column(db.String(64), comment='方案名称 / 期次（如 2026开门红 / 阶段一）')
    #   八改+：费项类型（对齐真实协议「变动费用」4 类）
    fee_type = db.Column(db.String(16), default='stage', comment='费项类型：service 续期服务津贴 / quality 业务品质奖金 / monthly 月度销售奖金 / stage 阶段补充佣金·推动费 / other 其他')
    tt_start = db.Column(db.String(16), comment='总总方案起始日 YYYY-MM-DD')
    tt_end = db.Column(db.String(16), comment='总总方案终止日 YYYY-MM-DD')
    ff_start = db.Column(db.String(16), comment='分分方案起始日 YYYY-MM-DD')
    ff_end = db.Column(db.String(16), comment='分分方案终止日 YYYY-MM-DD')
    # ② 政策归属（协议内 / 协议外 决定费用形式，八改起后端双向强校验）
    policy_scope = db.Column(db.String(8), default='in', comment='政策归属：in 协议内 / out 协议外')
    pay_form = db.Column(db.String(16), default='transfer', comment='费用形式：transfer 协议内结算 / coef 系数 / cash 现金 / other 购物或其他')
    #  六改加列：pay_form=cash（协议外净费）时的具体发放渠道；pay_form=other 时的备注说明
    pay_channel = db.Column(db.String(16), comment='协议外净费形式：cash 现金 / card 购物卡（pay_form=cash 时填）')
    other_remark = db.Column(db.String(200), comment='费用形式选「其他」时的备注说明')
    # ⑬ 文字类方案说明（十五改 2026-10-02）：pay_form 为现金 / 购物或其他时，本方案的文字描述。
    #    ⚠️ 这类方案**不按缴别展开**（tier_id 为空、合约下只落一行），也**不参与系数合计与报表合并**。
    plan_text = db.Column(db.Text, comment='文字类方案说明（现金/购物或其他的方案内容，不参与合计）')
    # ③ 总总方案
    tt_coef = db.Column(db.Float, default=0, comment='总总方案·奖金系数（%）')
    tt_amount = db.Column(db.Float, default=0, comment='总总方案·奖金金额（元，不参与合计、仅展示）')
    tt_other = db.Column(db.String(200), comment='总总方案·其他约定')
    # ④ 分分方案
    ff_coef = db.Column(db.Float, default=0, comment='分分方案·奖金系数（%）')
    ff_amount = db.Column(db.Float, default=0, comment='分分方案·奖金金额（元，不参与合计、仅展示）')
    ff_other = db.Column(db.String(200), comment='分分方案·其他约定')
    # ⑤ 方案合计（后端自动算，% 系数）
    plan_total = db.Column(db.Float, default=0, comment='方案合计（%）= 总总系数 + 分分系数')
    sort_no = db.Column(db.Integer, default=0, comment='排序号（同缴别内展示顺序）')
    note = db.Column(db.String(200), comment='备注（灵活补充）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


# =====================================================================
# 业绩管理 · 投保订单录入（2026-10 新增，威哥规格）
#   · 一个模块「投保订单录入」= 新单录入（手工）+ 新单导入（Excel）两端口汇总
#   · CrmCustomer：投保人 / 被保人客户主表（按身份证号唯一，编码自动生成、历史带出）
#   · CrmPolicyOrder：投保订单主表（覆盖威哥全部字段 + 自动测算字段 + 8 类附件）
#   本组为纯新增表，靠 create_all 直建，不入 seed._migrate_columns。
# =====================================================================


class CrmCustomer(db.Model):
    """投保人 / 被保人客户主表：按身份证号唯一，自动生成客户编码，历史数据复用。

    威哥口径（2026-10）：录入订单时填身份证号 → 系统自动生成唯一客户编码；
    若系统内已有该身份证的历史客户，则自动带出其编码与姓名 / 银行等信息。
    一张表存所有客户（不分投保人 / 被保人），按 idcard 唯一；投保人专属的
    开户行 / 银行账号允许为空（被保人一般不填）。
    """
    id = db.Column(db.Integer, primary_key=True)
    idcard = db.Column(db.String(32), index=True, comment='身份证号码（唯一键）')
    customer_code = db.Column(db.String(32), unique=True, index=True, comment='系统客户编码（按身份证自动生成，唯一）')
    name = db.Column(db.String(64), comment='姓名')
    gender = db.Column(db.String(8), comment='性别：male 男 / female 女（身份证自动带出）')
    birth_date = db.Column(db.String(16), comment='出生年月 YYYY-MM-DD（身份证自动带出）')
    age = db.Column(db.Integer, comment='年龄（身份证自动带出）')
    mobile = db.Column(db.String(32), comment='手机号')
    # 住址细分（省 / 地市 / 县区 / 街道）
    addr_province = db.Column(db.String(32), comment='家庭住址·省份')
    addr_city = db.Column(db.String(32), comment='家庭住址·地市')
    addr_county = db.Column(db.String(32), comment='家庭住址·县区')
    addr_street = db.Column(db.String(128), comment='家庭住址·街道（详细地址）')
    # 投保人专属（被保人一般留空）
    bank_name = db.Column(db.String(64), comment='开户行（投保人专属）')
    bank_account = db.Column(db.String(64), comment='银行账号（投保人专属）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='首次录入时间')


class CrmPolicyOrder(db.Model):
    """投保订单主表（新契约受理明细清单）。

    威哥规格（2026-10）：两个端口（手工录入 / Excel 导入）统一汇总到本表。
    自动带出 / 自动测算字段（后端落库，前端同步展示）：
      · 代理人组织：输入代理人工号 → 带出 姓名/省公司/分支/所属经代公司/所属架构(营业组)
      · 投保人/被保人：性别/出生年月/年龄/客户编码 由身份证自动算（历史客户自动带出）
      · 缴费年期 pay_years（实际缴费年限，年）：表单独立录入（趸交=1 / 3年 / 5年 / 10年 / …），
        优先于下方推算，用于行业标保折标率 = min(缴费年期×10, 100)%；行业标保(元)=规模保费×折标系数。
        （趸交=10% · 3年=30% · 5年=50% · 10年=100% · 10年及以上封顶100%，与威哥示例一致）
      · 受理件数 = 缴费年期>1 ? 1 : 0
      · 长短险标识 = 缴费年期==1 ? 短期险 : 长期险
      · ⚠️ 表单未填缴费年期时，按「缴费方式+保险年期」兜底推算（趸交/1年→1，终身期交→10）。
      · 自互保件标识：被保人是公司入职代理人=自保件；投保人与被保人都=互保件
      · 代理人佣金 = 规模保费 × 佣金系数%；（佣金系数优先带出产品合约 FYC，可手改）
    日期一律 String（项目口径）。金额为 Float（展示 2 位）。
    """
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='订单编号（系统自动 PO-YYYYMM-NNN）')
    # —— 出单平台 + 代理人（手工输入工号，自动带出架构）——
    agency_platform = db.Column(db.String(128), comment='出单平台（合作经代公司）')
    agent_no = db.Column(db.String(32), index=True, comment='代理人工号（海腾上号工号）')
    agent_name = db.Column(db.String(32), comment='代理人姓名（自动带出）')
    agent_province = db.Column(db.String(32), comment='省公司（自动带出）')
    agent_branch = db.Column(db.String(64), comment='分支（自动带出）')
    agent_agency = db.Column(db.String(128), comment='所属经代公司（自动带出）')
    agent_org = db.Column(db.String(128), comment='所属架构（营业组，自动带出）')
    # —— 投保单 / 保单标识 ——
    app_form_no = db.Column(db.String(64), comment='投保单号')
    policy_no = db.Column(db.String(64), comment='保单号')
    # —— 投保人 ——
    app_name = db.Column(db.String(64), comment='投保人姓名')
    app_idcard = db.Column(db.String(32), comment='投保人身份证号')
    app_gender = db.Column(db.String(8), comment='投保人性别（自动算）')
    app_birth = db.Column(db.String(16), comment='投保人出生年月（自动算）')
    app_age = db.Column(db.Integer, comment='投保人年龄（自动算）')
    app_mobile = db.Column(db.String(32), comment='投保人手机号')
    app_addr_province = db.Column(db.String(32), comment='投保人住址·省份')
    app_addr_city = db.Column(db.String(32), comment='投保人住址·地市')
    app_addr_county = db.Column(db.String(32), comment='投保人住址·县区')
    app_addr_street = db.Column(db.String(128), comment='投保人住址·街道')
    app_code = db.Column(db.String(32), comment='投保人编码（自动生成/带出）')
    app_bank_name = db.Column(db.String(64), comment='投保人开户行')
    app_bank_account = db.Column(db.String(64), comment='投保人银行账号')
    # —— 被保人 ——
    ins_name = db.Column(db.String(64), comment='被保险人姓名')
    ins_idcard = db.Column(db.String(32), comment='被保险人身份证号')
    ins_gender = db.Column(db.String(8), comment='被保人性别（自动算）')
    ins_birth = db.Column(db.String(16), comment='被保人出生年月（自动算）')
    ins_age = db.Column(db.Integer, comment='被保人年龄（自动算）')
    ins_mobile = db.Column(db.String(32), comment='被保人手机号')
    ins_addr_province = db.Column(db.String(32), comment='被保人住址·省份')
    ins_addr_city = db.Column(db.String(32), comment='被保人住址·地市')
    ins_addr_county = db.Column(db.String(32), comment='被保人住址·县区')
    ins_addr_street = db.Column(db.String(128), comment='被保人住址·街道')
    ins_code = db.Column(db.String(32), comment='被保人编码（自动生成/带出）')
    # —— 保单状态 / 产品 ——
    policy_main_status = db.Column(db.String(16), comment='保单主状态')
    policy_sub_status = db.Column(db.String(16), comment='保单子状态')
    policy_risk_status = db.Column(db.String(16), comment='险种状态')
    insurer_name = db.Column(db.String(128), comment='保险公司名称（下拉，取签约数据）')
    product_name = db.Column(db.String(128), comment='产品名称')
    product_code = db.Column(db.String(64), comment='产品编码（自动带出）')
    insurance_type = db.Column(db.String(32), comment='险种类别（自动带出）')
    plan = db.Column(db.String(128), comment='投保计划')
    accept_count = db.Column(db.Integer, default=0, comment='受理件数（缴费年期>1?1:0，自动算）')
    # —— 保费（2 位）——
    sum_assured = db.Column(db.Float, default=0, comment='保额（元，2位）')
    basic_premium = db.Column(db.Float, default=0, comment='基本保费（元，2位）')
    extra_premium = db.Column(db.Float, default=0, comment='额外保费（元，2位）')
    add_premium = db.Column(db.Float, default=0, comment='追加保费（元，2位）')
    health_load = db.Column(db.Float, default=0, comment='职业健康加费（元，2位）')
    scale_premium = db.Column(db.Float, default=0, comment='规模保费（元，2位）')
    # —— 自动测算 ——
    commission = db.Column(db.Float, default=0, comment='代理人佣金（元，自动算，2位）')
    industry_std_rate = db.Column(db.Float, default=0, comment='行业标保折标率%（自动算：min(缴费年期×10,100)）')
    industry_std_premium = db.Column(db.Float, default=0, comment='行业标保（元）=规模保费×折标系数，自动算')
    commission_rate = db.Column(db.Float, default=0, comment='佣金系数（%，2位）')
    # —— 缴费 / 年期 ——
    pay_method = db.Column(db.String(8), comment='缴费方式：year 年缴 / month 月缴 / lump 趸交')
    ins_period_flag = db.Column(db.String(8), default='', comment='保险年期标志：blank 空白 / year 年 / age 岁')
    ins_period = db.Column(db.String(16), comment='保险年期：lifetime 终身 / 1year 1年')
    pay_years = db.Column(db.Integer, default=0, comment='缴费年期（实际缴费年限，年）：表单录入优先；用于行业标保折标（趸交=1，封顶10）。未填时按缴费方式+保险年期推算')
    # —— 关键日期 ——
    apply_date = db.Column(db.String(16), comment='投保日期 YYYY-MM-DD')
    underwrite_date = db.Column(db.String(16), comment='承保日期 YYYY-MM-DD')
    effective_date = db.Column(db.String(16), comment='保单生效日期 YYYY-MM-DD')
    callback_date = db.Column(db.String(16), comment='回访成功日期 YYYY-MM-DD')
    cancel_date = db.Column(db.String(16), comment='撤单日期 YYYY-MM-DD')
    cancel_reason = db.Column(db.String(200), comment='撤单原因（非强制）')
    # —— 标识类（自动 / 下拉）——
    self_policy_flag = db.Column(db.String(8), default='', comment='自互保件标识：self 自保件 / mutual 互保件 / 空')
    order_type = db.Column(db.String(8), comment='出单类型：online 线上单 / offline 线下单')
    auto_loan = db.Column(db.String(8), default='no', comment='是否自动垫交：yes 是 / no 否')
    policy_kind = db.Column(db.String(16), comment='保单类型：in_service 在职单 / orphan 孤儿单 / inherit 继承单 / recursive 递归单')
    ls_flag = db.Column(db.String(8), default='', comment='长短险标识：short 短期险 / long 长期险（自动算）')
    dual_record_flag = db.Column(db.String(8), default='no', comment='双录单标识：yes 双录单 / no 非双录单')
    dual_record_type = db.Column(db.String(16), comment='双录类型：reg 保司监管双录 / company 公司内部双录 / none 不需要 / blank 空白')
    dual_record_status = db.Column(db.String(8), comment='双录状态：pass 通过 / fail 未通过')
    tax_pref_flag = db.Column(db.String(8), default='', comment='税优产品标识：yes 税优产品 / 空')
    note = db.Column(db.Text, comment='备注')
    created_by = db.Column(db.Integer, comment='录入人 user_id')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')
    # —— 8 类后台附件（原文件名 + 落盘文件名）——
    app_idcard_front = db.Column(db.String(255), comment='投保人身份证正面')
    app_idcard_front_name = db.Column(db.String(255), comment='投保人身份证正面·原名')
    app_idcard_back = db.Column(db.String(255), comment='投保人身份证反面')
    app_idcard_back_name = db.Column(db.String(255), comment='投保人身份证反面·原名')
    app_bank_front = db.Column(db.String(255), comment='投保人银行卡正面')
    app_bank_front_name = db.Column(db.String(255), comment='投保人银行卡正面·原名')
    app_bank_back = db.Column(db.String(255), comment='投保人银行卡反面')
    app_bank_back_name = db.Column(db.String(255), comment='投保人银行卡反面·原名')
    ins_idcard_front = db.Column(db.String(255), comment='被保人身份证正面')
    ins_idcard_front_name = db.Column(db.String(255), comment='被保人身份证正面·原名')
    ins_idcard_back = db.Column(db.String(255), comment='被保人身份证反面')
    ins_idcard_back_name = db.Column(db.String(255), comment='被保人身份证反面·原名')
    policy_contract_file = db.Column(db.String(255), comment='保单电子合同')
    policy_contract_file_name = db.Column(db.String(255), comment='保单电子合同·原名')
    decl_file = db.Column(db.String(255), comment='投保声明（纸质电子版/视频）')
    decl_file_name = db.Column(db.String(255), comment='投保声明·原名')
    # —— 数据来源（两端口区分：手工录入 vs Excel 导入）——
    src = db.Column(db.String(16), default='manual', comment='数据来源：manual 手工录入 / import Excel 导入')


class CrmRenewal(db.Model):
    """续期管理：一份保单 × 一个续期年度（第 2 ~ 缴费年期）一行。

    定位（2026-10-28 续期管理 v1）：把「已录入保单 + 合约 RYC 续期佣金率 + 团队系数 kr」
    联动，做续期跟踪 + 续期佣金清分 + 递归单/孤儿单续期服务人员再分配。

    计算口径完全复用现有：续期佣金 = 规模保费 × RYC_n%(净) × kr（先扣增值税+附加再乘系数）。
    ryc_rate 为合约 tier 的 ryc_n（含税费率%）；金额展示时实时用 _net_rate 重算、不落库冻结，
    避免税率/系数调整后数据陈旧（同销售费用清单口径）。

    ⚠️ 纯新增表，靠 create_all 直建，**不入** seed._migrate_columns。
    """
    __tablename__ = 'crm_renewal'
    id = db.Column(db.Integer, primary_key=True)
    policy_order_id = db.Column(db.Integer, index=True, comment='关联投保订单 CrmPolicyOrder.id')
    policy_no = db.Column(db.String(64), index=True, comment='保单号（冗余快照）')
    insurer_name = db.Column(db.String(128), comment='保险公司（冗余快照）')
    product_name = db.Column(db.String(128), comment='产品名称（冗余快照）')
    agent_no = db.Column(db.String(32), comment='原代理人工号（冗余快照）')
    agent_name = db.Column(db.String(32), comment='原代理人姓名（冗余快照）')
    renewal_year = db.Column(db.Integer, comment='续期年度（2..缴费年期）')
    due_date = db.Column(db.String(16), comment='应续日期 YYYY-MM-DD（生效日 + (年度-1) 年）')
    scale_premium = db.Column(db.Float, default=0, comment='规模保费（元，续期佣金计算基数）')
    ryc_rate = db.Column(db.Float, default=0, comment='RYC 续期佣金率 %（取自合约 tier 的 ryc_n）')
    service_agent_no = db.Column(db.String(32), comment='续期服务人员工号（默认=原代理人；递归单/孤儿单可改）')
    service_agent_name = db.Column(db.String(32), comment='续期服务人员姓名')
    status = db.Column(db.String(12), default='pending', comment='续期状态：pending 待续 / done 已续 / lapse 失效')
    paid_flag = db.Column(db.String(8), default='no', comment='续期佣金是否已发：yes 是 / no 否')
    note = db.Column(db.Text, comment='备注')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='生成时间')
    __table_args__ = (
        db.UniqueConstraint('policy_order_id', 'renewal_year', name='uq_crm_renewal_py'),
    )


# ======================================================================
# 保费测算 · 统一因子长表（智能体导入保司费率/现金价值后的归一化存储）
# ----------------------------------------------------------------------
# 口径：所有数值均以「每 base_unit(默认1000)元保费」为基准单位，
#       测算时按 实际保费/base_unit × 系数 还原。
# 一家保司一个产品的数据 = 若干行 (sex, issue_age, pay_term, policy_year)：
#   - policy_year = 0  → 费率行，仅 sa（基本保额）有意义；
#   - policy_year >= 1 → 现金价值行，sa 可一并带出（因子档）或留空（纯现价表）。
# 这样爱心(因子档长表)/幸福(费率表+现金价值两个长表)/各公司宽表归一化后都能装下。
# 纯新增表，靠 create_all 直建，不入 seed._migrate_columns。
# ======================================================================
class CrmPremiumFactor(db.Model):
    __tablename__ = 'crm_premium_factor'
    id = db.Column(db.Integer, primary_key=True)
    insurer_name = db.Column(db.String(120), nullable=False, comment='保司名称')
    product_name = db.Column(db.String(160), nullable=False, comment='产品名称')
    sex = db.Column(db.String(4), default='', comment='性别：M 男 / F 女 / 空 通用')
    issue_age = db.Column(db.Integer, default=0, comment='投保年龄')
    pay_term = db.Column(db.Integer, default=0, comment='交费方式/交费期间：0=趸交，其余为年数')
    policy_year = db.Column(db.Integer, default=0, comment='保单年度：0=费率行(仅基本保额)，>=1=现金价值行')
    sa = db.Column(db.Float, default=0.0, comment='基本保险金额（每 base_unit 元保费）')
    cv = db.Column(db.Float, default=0.0, comment='保单年度末现金价值（每 base_unit 元保费）')
    cv_begin = db.Column(db.Float, default=0.0, comment='保单年度初现金价值')
    tr = db.Column(db.Float, default=0.0, comment='责任准备金 TR')
    trnp = db.Column(db.Float, default=0.0, comment='净保费责任准备金 TRNP')
    cvnp = db.Column(db.Float, default=0.0, comment='CVNP')
    sb = db.Column(db.Float, default=0.0, comment='SB')
    dividend_rate = db.Column(db.Float, default=0.0, comment='分红利率')
    # 分红型产品的红利（每 base_unit 元保费）——计划书「红利利益演示」用
    div_g = db.Column(db.Float, default=0.0, comment='当年红利：保证利益演示口径')
    div_d = db.Column(db.Float, default=0.0, comment='当年红利：红利利益演示口径（非保证）')
    # 第三种领取方式「购买交清增额保险」（2026-10-06 二十三改，条款3.1/3.2）
    # pua_sa＝累积交清增额基本保险金额（累计红利买到的增额保额，非保证）
    # pua_add＝当年新增的交清增额基本保险金额
    # pua_cv＝增额部分的现金价值【每 1000 元增额保额】——注意它不是「每 base_unit 保费」，不参与保费缩放
    pua_sa = db.Column(db.Float, default=0.0, comment='累积交清增额基本保险金额（每 base_unit 元保费）')
    pua_add = db.Column(db.Float, default=0.0, comment='当年增额基本保险金额（每 base_unit 元保费）')
    pua_cv = db.Column(db.Float, default=0.0, comment='增额部分现金价值（每1000元增额保额）')
    convert_cv = db.Column(db.Float, default=0.0, comment='转换现价')
    base_unit = db.Column(db.Float, default=1000.0, comment='保费基准单位（默认1000元）')
    kind = db.Column(db.String(8), default='', comment='来源类型：rate 费率 / cash 现金价值 / factor 因子档')
    src_file = db.Column(db.String(200), default='', comment='来源文件名')
    batch_no = db.Column(db.String(40), default='', comment='导入批次号')
    created_at = db.Column(db.String(20), default='', comment='导入时间 YYYY-MM-DD HH:MM')
    __table_args__ = (
        db.Index('ix_cpf_key', 'insurer_name', 'product_name', 'sex',
                 'issue_age', 'pay_term', 'policy_year'),
    )


class CrmProposal(db.Model):
    """计划书（行销辅助）：手工录入保单要素 + 复用已导入费率/现金价值因子自动测算。

    说明：逐年利益演示表（现金价值/单利/IRR）由 _premium_calc 实时重算，不入库，避免冗余；
    仅快照一条关键结果 base_sa（基本保险金额）用于列表展示。
    纯新增表，靠 create_all 直建，不入 seed._migrate_columns。"""
    __tablename__ = 'crm_proposal'
    id = db.Column(db.Integer, primary_key=True)
    proposal_no = db.Column(db.String(40), unique=True, nullable=False, comment='计划书编号')
    insurer_name = db.Column(db.String(120), default='', comment='保司名称')
    product_name = db.Column(db.String(160), default='', comment='产品名称')
    client_name = db.Column(db.String(60), default='', comment='客户姓名')
    sex = db.Column(db.String(4), default='', comment='性别：M 男 / F 女 / 空 通用')
    issue_age = db.Column(db.Integer, default=0, comment='投保年龄')
    pay_term = db.Column(db.Integer, default=0, comment='交费方式：0=趸交，其余为年数')
    premium = db.Column(db.Float, default=0.0, comment='年交保费（元）')
    base_sa = db.Column(db.Float, default=0.0, comment='基本保险金额（测算结果快照）')
    illustration_mode = db.Column(db.String(20), default='', comment='演示类型：标准/红利累积生息等')
    # 分红演示口径（条款3.2 两种红利领取方式）：cash 现金领取 / accum 累积生息
    div_mode = db.Column(db.String(16), default='accum', comment='红利领取方式：cash 现金分红 / accum 累积生息')
    accum_rate = db.Column(db.Float, default=0.0175, comment='红利累积生息年利率（默认1.75%）')
    owner_user = db.Column(db.String(40), default='', comment='归属业务员账号')
    status = db.Column(db.String(10), default='draft', comment='draft 草稿 / done 已交付')
    notes = db.Column(db.Text, default='', comment='备注')
    created_at = db.Column(db.String(20), default='', comment='创建时间 YYYY-MM-DD HH:MM')
    updated_at = db.Column(db.String(20), default='', comment='更新时间 YYYY-MM-DD HH:MM')
    __table_args__ = (
        db.Index('ix_cp_owner', 'owner_user'),
        db.Index('ix_cp_no', 'proposal_no'),
    )


class CrmProposalProduct(db.Model):
    """产品计划书引擎配置（智能体工作台产物）——一款产品一条。

    智能体流程（2026-10-06 威哥拍板）：
      选产品 → 上传保司文件 → 智能体自动解析并列出识别到的列映射 → 人工可修正
      → 输入要素「试跑」看演示结果 → 确认无误点「安装」→ 配置落这里 + 因子入库 → 产品可用。
    ⚠️ 配置驱动，不生成代码：换一家公司只是多一条配置，不动任何 .py。

    签约匹配：contract_id 关联 CrmProductContract（可空）。手工添加且查不到合约时
    signed='no'，页面红字提示「该产品还未签约，请及时签约」——不拦截，只提醒。
    """
    __tablename__ = 'crm_proposal_product'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, comment='配置编号（系统自动 PPE-YYYYMM-NNN）')
    # ① 产品
    insurer_name = db.Column(db.String(120), default='', comment='保司名称')
    product_name = db.Column(db.String(160), default='', comment='产品名称')
    insurance_type = db.Column(db.String(32), default='', comment='险种类型')
    # 计划书产品分类（2026-10-06 二十三改）：驱动「分类智能体」——不同类别走不同的
    # 解析指引 / 演示口径 / 校验规则。取值见 crm.PROPOSAL_CATEGORIES 的 key。
    category = db.Column(db.String(24), default='', comment='计划书产品分类 key（见 crm.PROPOSAL_CATEGORIES）')
    # ② 签约匹配
    contract_id = db.Column(db.Integer, comment='关联产品合约 CrmProductContract.id（可空）')
    signed = db.Column(db.String(8), default='no', comment='签约状态：yes 已签约 / no 未签约')
    # ③ 智能体解析配置
    source_files = db.Column(db.Text, default='', comment='来源文件名 JSON 数组')
    sheet_name = db.Column(db.String(64), default='', comment='主工作表名（空=全 sheet 扫描）')
    header_row = db.Column(db.Integer, default=0, comment='表头所在行号（1 起，0=自动）')
    col_map = db.Column(db.Text, default='', comment='列映射 JSON：{字段名: 原表头名}')
    base_unit = db.Column(db.Float, default=1000.0, comment='保费基准（默认 1000 元/单位）')
    # ④ 演示口径
    has_dividend = db.Column(db.Integer, default=0, comment='是否分红型：0 否 / 1 是')
    div_mode = db.Column(db.String(16), default='', comment='红利演示口径：cash 现金分红 / accum 累积生息')
    # 红利累积生息年利率（条款3.2「红利留存在本公司以年复利方式累积生息」）
    # 默认取保司因子档「分红信息配置」的定价利率（爱心=1.75%）；可在页面按产品改
    accum_rate = db.Column(db.Float, default=0.0175, comment='红利累积生息年利率（默认1.75%）')
    div_ratio = db.Column(db.Float, default=0.0, comment='分红比例（保司给的可分配盈余分配比例，如0.7）')
    div_demo_rate = db.Column(db.Float, default=0.0, comment='红利演示利率（保司演示假设，如0.035）')
    # ⑤ 状态
    status = db.Column(db.String(10), default='draft',
                       comment='draft 草稿(未安装) / installed 已安装 / disabled 停用')
    factor_rows = db.Column(db.Integer, default=0, comment='已入库因子行数（安装后回填）')
    trial_note = db.Column(db.Text, default='', comment='试跑结论备注')
    installed_at = db.Column(db.String(20), default='', comment='安装时间 YYYY-MM-DD HH:MM')
    owner_user = db.Column(db.String(40), default='', comment='配置人账号')
    notes = db.Column(db.Text, default='', comment='备注')
    created_at = db.Column(db.String(20), default='', comment='创建时间 YYYY-MM-DD HH:MM')
    updated_at = db.Column(db.String(20), default='', comment='更新时间 YYYY-MM-DD HH:MM')
    __table_args__ = (
        db.Index('ix_cpp_prod', 'insurer_name', 'product_name'),
        db.Index('ix_cpp_status', 'status'),
    )


class AiConfig(db.Model):
    """AI 悬浮机器人 · 大模型接入配置（四十四改，2026-10-29）。

    威哥诉求：核心业务管理系统右下角挂一个 AI 机器人，能对话提问，
    并把「产品合约协议」丢给它 → 自动识别条款 → 把数据填进系统 → 人工审核后提交。

    技术现实：本系统是本地 Flask，没有内建模型能力，必须外接一家。
    选用**智谱 GLM-4-Flash**（每天 100 万 token 免费、128K 长上下文，能一次吞下整份协议），
    接口兼容 OpenAI 的 `/chat/completions` 规范，所以把 base_url 做成可改的 ——
    换 DeepSeek / 通义 / Kimi 只改地址与模型名，代码不用动。

    全局单例（id=1）：
      · provider   仅作显示（zhipu/deepseek/other）
      · base_url   形如 https://open.bigmodel.cn/api/paas/v4（不带 /chat/completions）
      · api_key    控制台申请的 Key；留空＝机器人可打开但只能回「未配置」提示
      · model      默认 glm-4-flash
    本表为新增表，靠 create_all 直建，不入 seed._migrate_columns。
    """
    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(16), default='zhipu', comment='服务商标记（仅显示）')
    base_url = db.Column(db.String(200), default='https://open.bigmodel.cn/api/paas/v4',
                         comment='接口根地址（不含 /chat/completions）')
    api_key = db.Column(db.String(300), default='', comment='API Key（留空＝未启用）')
    model = db.Column(db.String(64), default='glm-4-flash', comment='模型名')
    enabled = db.Column(db.String(8), default='on', comment='on 启用 / off 停用')
    max_tokens = db.Column(db.Integer, default=4096, comment='单次回复最大 token')
    temperature = db.Column(db.Float, default=0.2, comment='温度（抽数据要低温）')
    timeout_sec = db.Column(db.Integer, default=180, comment='单次请求超时秒数（长协议要留足）')
    updated_at = db.Column(db.String(20), default='', comment='上次保存时间 YYYY-MM-DD HH:MM')
    note = db.Column(db.String(300), default='', comment='备注')


# ===================== 产品佣金系数模块（2026-11-02 新增）=====================
# 威哥口径：一键导入「采购成本下发」Excel → 按渠道类型(互联网/线下)设销售系数 →
# 自动生成 基础佣金 / 销售费用 / 经营费用。
#   · CrmProductCommission  导入的产品合约原始数据（一行 = 一个产品的一个缴别）
#   · CrmCommCoefConfig     系数配置（按渠道类型各一套）
#   · CrmCommCoefBackup     月度系数备份（每月按渠道归档一份系数快照 + 当期结果汇总，带覆盖保护）
# 三张均为纯新增表，靠 create_all 直建，不入 seed._migrate_columns。
COMM_CHANNELS = [
    ('offline', '线下产品'),
    ('internet', '互联网产品'),
]
# 六个费用项（对应导入表的列）：键 = 模型字段，值 = 中文名
COMM_ITEMS = [
    ('base_comm', '首年基础佣金'),
    ('plan', '首年方案'),
    ('y2', '第二年采购'),
    ('y3', '第三年采购'),
    ('y4', '第四年采购'),
    ('y5', '第五年采购'),
]
# 三条取整规则（应用于销售费用计算结果）：一律「直接舍去」，四舍五也舍，不做四舍五入
ROUND_RULES = [
    ('1', '保留2位小数（第三位起舍去）'),
    ('2', '取整（小数舍去）'),
    ('3', '减1%后保留2位（直接舍去·默认）'),
]


class CrmProductCommission(db.Model):
    """导入的产品合约原始数据（来自「采购成本下发」Excel，一行 = 一个产品×缴别）。"""
    __tablename__ = 'crm_product_commission'
    id = db.Column(db.Integer, primary_key=True)
    insurer = db.Column(db.String(128), default='', comment='保险公司')
    product = db.Column(db.String(128), default='', comment='险种名称')
    channel = db.Column(db.String(64), default='', comment='渠道')
    is_internet = db.Column(db.Integer, default=0, comment='是否互联网产品：1=E列="是" / 0=空(线下)')
    internet_heavy = db.Column(db.String(16), default='', comment='互险网重疾标识')
    region = db.Column(db.String(128), default='', comment='投保区域')
    coverage_term = db.Column(db.String(64), default='', comment='保障期限')
    pay_period = db.Column(db.String(16), default='', comment='缴费期')
    purchase_y1 = db.Column(db.Float, default=0, comment='首年采购')
    base_comm = db.Column(db.Float, default=0, comment='首年基础佣金')
    plan = db.Column(db.Float, default=0, comment='首年方案')
    purchase_y2 = db.Column(db.Float, default=0, comment='第二年采购')
    purchase_y3 = db.Column(db.Float, default=0, comment='第三年采购')
    purchase_y4 = db.Column(db.Float, default=0, comment='第四年采购')
    purchase_y5 = db.Column(db.Float, default=0, comment='第五年采购')
    note = db.Column(db.String(200), default='', comment='备注')
    imported_at = db.Column(db.String(20), default='', comment='导入时间 YYYY-MM-DD HH:MM')
    month = db.Column(db.String(16), default='', comment='导入归属月份 YYYY-MM（用于月度区分）')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, comment='录入时间')


class CrmCommCoefConfig(db.Model):
    """系数配置：按渠道类型(互联网/线下)各一套（每套含6个销售系数+基础/经营比例+取整规则）。"""
    __tablename__ = 'crm_comm_coef_config'
    id = db.Column(db.Integer, primary_key=True)
    channel_type = db.Column(db.String(16), unique=True, comment='offline 线下 / internet 互联网')
    # 6 个销售系数（百分比数值，如 5 = 5%）
    coef_base_comm = db.Column(db.Float, default=5, comment='销售系数·首年基础佣金(%)')
    coef_plan = db.Column(db.Float, default=5, comment='销售系数·首年方案(%)')
    coef_y2 = db.Column(db.Float, default=5, comment='销售系数·第二年采购(%)')
    coef_y3 = db.Column(db.Float, default=5, comment='销售系数·第三年采购(%)')
    coef_y4 = db.Column(db.Float, default=5, comment='销售系数·第四年采购(%)')
    coef_y5 = db.Column(db.Float, default=5, comment='销售系数·第五年采购(%)')
    # 基础 / 经营比例（百分比，相对各费用项本身）
    base_ratio = db.Column(db.Float, default=80, comment='基础佣金比例(%)')
    operate_ratio = db.Column(db.Float, default=4, comment='经营费用比例(%)')
    sales_ratio = db.Column(db.Float, default=5, comment='销售费用统一比例(%)：各项目系数留空(0)时沿用')
    ryc_base_ratio = db.Column(db.Float, default=0, comment='RYC佣金比例(%%)：统一兜底，0=沿用基础佣金比例')
    # RYC 分年期佣金比例（每年可不同；0/空 → 沿用 ryc_base_ratio，再退基础佣金比例）
    ryc_ratio_y2 = db.Column(db.Float, default=0, comment='RYC2(第二年)佣金比例(%)')
    ryc_ratio_y3 = db.Column(db.Float, default=0, comment='RYC3(第三年)佣金比例(%)')
    ryc_ratio_y4 = db.Column(db.Float, default=0, comment='RYC4(第四年)佣金比例(%)')
    ryc_ratio_y5 = db.Column(db.Float, default=0, comment='RYC5(第五年)佣金比例(%)')
    # RYC 续年度佣金 <1% 自动置空开关（线下/互联网统一规则，2026-11-03 二改）
    ryc_hide_lt1 = db.Column(db.Boolean, default=False,
                             comment='RYC续年度佣金<1%自动置空（线下/互联网统一）')
    team_base_ratio = db.Column(db.Float, default=100, comment='团队长基础佣金比例(%)：占子公司基础佣金')
    # 取整规则 1/2/3
    round_rule = db.Column(db.String(4), default='3', comment='取整规则 1=保留2位小数 2=取整 3=四舍五入后减1%取整（默认）')
    updated_at = db.Column(db.String(20), default='', comment='更新时间')
    note = db.Column(db.String(200), default='', comment='备注')


class CrmCommCoefBackup(db.Model):
    """产品佣金系数「月度备份」：每月归档一份当期生效系数 + 当期计算结果汇总。

    用途：系数设置完成、能成功算出销售费用后，系统按月自动归档，形成可追溯的月度台账；
    后续调整系数时走保护机制 —— 已归档的月份不允许静默覆盖，必须显式确认，
    覆盖前的快照保留在 prev_snapshot（JSON，仅留最近一次），并累计 override_count。
    """
    __tablename__ = 'crm_comm_coef_backup'
    __table_args__ = (db.UniqueConstraint('month', 'channel_type', name='uq_comm_backup_month_ch'),)
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.String(7), default='', index=True, comment='归属月份 YYYY-MM')
    channel_type = db.Column(db.String(16), default='offline', index=True, comment='offline 线下 / internet 互联网')
    # ---- 系数快照 ----
    base_ratio = db.Column(db.Float, default=0, comment='基础佣金比例(%)')
    sales_ratio = db.Column(db.Float, default=0, comment='销售费用比例(%)')
    operate_ratio = db.Column(db.Float, default=0, comment='经营费用比例(%)')
    team_base_ratio = db.Column(db.Float, default=0, comment='团队长基础佣金比例(%)')
    ryc_base_ratio = db.Column(db.Float, default=0, comment='RYC 统一佣金比例(%)')
    ryc_ratio_y2 = db.Column(db.Float, default=0, comment='RYC2 佣金比例(%)')
    ryc_ratio_y3 = db.Column(db.Float, default=0, comment='RYC3 佣金比例(%)')
    ryc_ratio_y4 = db.Column(db.Float, default=0, comment='RYC4 佣金比例(%)')
    ryc_ratio_y5 = db.Column(db.Float, default=0, comment='RYC5 佣金比例(%)')
    ryc_hide_lt1 = db.Column(db.Boolean, default=False, comment='RYC续年度佣金<1%自动置空（统一）')
    round_rule = db.Column(db.String(4), default='3', comment='取整规则')
    net_ratio = db.Column(db.Float, default=0, comment='销售净费比例(%)＝基础+销售+经营')
    # ---- 当期计算结果汇总（Σ 各产品占保费比例，%）----
    row_count = db.Column(db.Integer, default=0, comment='当期产品行数')
    base_pct_total = db.Column(db.Float, default=0, comment='FYC首佣合计(%)')
    sales_pct_total = db.Column(db.Float, default=0, comment='销售费用合计(%)')
    operate_pct_total = db.Column(db.Float, default=0, comment='经营费用合计(%)')
    net_pct_total = db.Column(db.Float, default=0, comment='销售净费合计(%)')
    # ---- 归档状态 / 覆盖保护 ----
    source = db.Column(db.String(8), default='auto', comment='auto 自动归档 / manual 手动备份')
    override_count = db.Column(db.Integer, default=0, comment='被覆盖次数')
    last_override_at = db.Column(db.String(20), default='', comment='最近一次覆盖时间')
    prev_snapshot = db.Column(db.Text, default='', comment='被覆盖前的快照 JSON（仅保留最近一次）')
    created_at = db.Column(db.String(20), default='', comment='首次备份时间')
    updated_at = db.Column(db.String(20), default='', comment='最近更新时间')
    note = db.Column(db.String(200), default='', comment='备注')
