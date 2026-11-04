# app.py
# 海盈通保经营管理系统 —— 基础框架（增量 #1：地基）
# 目前只做"能跑起来 + 数据库可用"，登录、页面、业务模块都留到后续增量。
import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import check_password_hash, generate_password_hash
from models import db, User, FinAuditLog, Employee, Organization


def create_app():
    """工厂函数：创建并配置 Flask 应用"""
    app = Flask(__name__)
    # 数据库文件放在项目目录下，名为 haiying.db（开发期用 SQLite，生产再换 MySQL/PG）
    db_path = os.path.join(app.root_path, 'haiying.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False  # 关闭额外追踪，省资源
    # 会话密钥：登录状态依赖 session 存储，不设置会导致登录时报 500。
    # 开发期用固定值即可；将来上线前改成随机长字符串，并通过环境变量传入。
    app.config['SECRET_KEY'] = os.environ.get('HYTB_SECRET_KEY', 'haiyingtongbao-dev-key-2026')
    # 附件上传：电子版合同等文件存到 static/uploads/ 下（按子目录分类），限制 20MB 单文件。
    app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024
    app.config['UPLOAD_DIR'] = os.path.join(app.root_path, 'static', 'uploads')
    db.init_app(app)
    return app


app = create_app()


def login_required(f):
    """登录保护装饰器：未登录访问时跳转到登录页"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def permission_required(code):
    """权限闸门：要求当前登录用户所属职级拥有指定权限 code，否则拦截。
    与 login_required 配合：先登录，再校验权限（权限流）。"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = User.query.get(session.get('user_id'))
            # 判断该用户职级下是否挂着指定权限 code
            has = bool(user and user.level and
                       any(p.code == code for p in user.level.permissions))
            if not has:
                flash('无权限执行该操作（缺少权限：%s）' % code)
                return redirect(url_for('home'))
            return f(*args, **kwargs)
        return wrapper
    return decorator




def log_audit(action, module='', target_type='', target_id=None, target_no='', detail=''):
    """记录操作审计日志（#40）。在视图中调用，自动取当前登录用户与 IP。

    用法：log_audit('add', module='fin', target_type='voucher',
                   target_id=v.id, target_no=v.voucher_no, detail='新增凭证')
    写入失败不影响主流程（回滚后继续）。
    """
    try:
        rec = FinAuditLog(
            user_id=session.get('user_id'),
            user_name=session.get('username', '') or '',
            action=action,
            module=module,
            target_type=target_type,
            target_id=target_id,
            target_no=target_no,
            detail=detail,
            ip=request.remote_addr or '',
        )
        db.session.add(rec)
        db.session.commit()
    except Exception:
        db.session.rollback()

@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页：GET 显示表单，POST 校验账号密码"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        # 校验账号存在 + 密码哈希匹配（密码不存明文，用 werkzeug 校验）
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            # 记下职级代码/名称：后续首页范围、权限闸门都靠它
            session['level_code'] = user.level.code if user.level else ''
            session['level_name'] = user.level.name if user.level else ''
            log_audit('login', module='auth')
            return redirect(url_for('home'))
        flash('用户名或密码错误')  # 校验失败提示
    return render_template('login.html')


@app.route('/logout')
def logout():
    """退出登录：清空会话"""
    session.clear()
    # 企业微信内置浏览器里退出后回到授权入口（密码登录页在企微内无意义）
    if 'wxwork' in (request.user_agent.string or '').lower():
        return redirect(url_for('wecom.entry'))
    return redirect(url_for('login'))


@app.route('/account/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """用户自助修改密码：需先校验当前密码，再写入新密码（服务端再次校验强度与一致性）。"""
    user = User.query.get(session.get('user_id'))
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        cur = request.form.get('current_password', '')
        new = request.form.get('new_password', '')
        new2 = request.form.get('confirm_password', '')
        if not check_password_hash(user.password_hash, cur):
            flash('当前密码错误')
            return redirect(url_for('change_password'))
        if len(new) < 6:
            flash('新密码至少 6 位')
            return redirect(url_for('change_password'))
        if new != new2:
            flash('两次输入的新密码不一致')
            return redirect(url_for('change_password'))
        user.password_hash = generate_password_hash(new)
        db.session.commit()
        log_audit('change_password', module='auth', detail='用户自助修改密码')
        flash('密码修改成功')
        return redirect(url_for('home'))
    return render_template('account/change_password.html')


# ===================== 系统总览（顶层架构） =====================
# 公司整体系统分为六大一级系统；每个系统含"业务模块"与"本版块设置"。
# 全局权限/职级由【IT管理系统】统一配置（总版块的系统管理）；
# 其余系统的"设置"仅负责本版块的细分配置与版块内权限，不做全局权限流。
# 分类依据（与用户确认）：
#  - 所有「申请/审批」类功能统一归【OA办公系统】（公告、审批、差勤申请、休假申请、待办、审批流设置）；
#  - 人员差勤管理仅保留「月度出勤查询」（出勤结果数据查询，归 EHR）；
#  - 合同分两类：EHR 的"人事合同"= 劳动/代理合同，行政的"合同管理"= 商务合同（物业租赁/商业合作等）。
# 命名统一规范：界面一律按「英文简称 · 中文名」呈现，简称与中文名不重复。
# 系统顺序（用户指定）：OA办公系统 排最前（在行政管理系统上方），其后依次为 行政/人事/财务/核心业务/IT。
SYSTEMS = [
    {
        'key': 'oa', 'name': 'OA办公系统', 'en': 'OA',
        'color': '#E6823C', 'soft': '#FBEEE2',   # 取品牌"常用橙"
        'desc': '统一办公：邮箱、公告、申请中心（各类申请与费用报销统一发起、统一查询）、审批、待办，'
                '以及审批流设置（行政管理员配置审批人）。',
        'modules': [
            {'name': '邮箱', 'en': 'Mail', 'url': 'oa.mails', 'done': True},
            {'name': '公告', 'en': 'Notice', 'url': 'oa.announcements', 'done': True},
            # 「申请」与「费用申请」已合并为同一个入口（申请中心内含费用单据区）
            {'name': '申请中心', 'en': 'Apply', 'url': 'oa.apply_hub', 'done': True},
            {'name': '审批', 'en': 'Appr', 'url': 'oa.approvals', 'done': True},
            {'name': '待办', 'en': 'Todo', 'url': 'oa.todos', 'done': True},
        ],
        'settings': [
            {'name': '审批流设置', 'en': 'FlowCfg', 'url': 'oa.flows', 'done': True},
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
    {
        'key': 'admin', 'name': '行政管理系统', 'en': 'ADM',
        'color': '#C86432', 'soft': '#F8E9DD',   # 取品牌"主色·深橙"

        'desc': '行政事务台账：证照资质、商务合同（租赁/物业/采购/服务）、固定资产、车辆、'
                '水电物业费用与行政事务登记。本系统只管"物"（台账 + 到期提醒）——'
                '报销请款走 OA 申请中心，固定资产记账走财务管理系统，员工劳动合同归人事管理系统。',
        'modules': [
            # 一期（2026-10-01）已上线：证照资质 / 商务合同 / 固定资产 三本台账
            {'name': '证照资质', 'en': 'License', 'url': 'adm.licenses', 'done': True},
            {'name': '商务合同', 'en': 'Contract', 'url': 'adm.contracts', 'done': True},
            {'name': '固定资产', 'en': 'Asset', 'url': 'adm.assets', 'done': True},
            # 四期（2026-10-01）已上线：车辆管理（档案 + 保险/年检提醒 + 保养维修违章用车登记）
            {'name': '车辆管理', 'en': 'Vehicle', 'url': 'adm.vehicles', 'done': True},
            # 三期（2026-10-01）已上线：费用台账（水电 / 物业 / 房租 / 通讯 + 缴费提醒）
            {'name': '费用台账', 'en': 'Expense', 'url': 'adm.expenses', 'done': True},
            # 二期（2026-10-01）已上线：行政事务（办公用品 / 印章使用登记 / 快递收发）
            {'name': '行政事务', 'en': 'AdmSvc', 'url': 'adm.services', 'done': True},
        ],
        'settings': [
            {'name': '行政分类设置', 'en': 'Category', 'url': 'adm.categories', 'done': True},
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
    {
        'key': 'ehr', 'name': '人事管理系统', 'en': 'EHR',
        'color': '#E6823C', 'soft': '#FBEEE2',   # 取品牌"常用橙"
        'desc': '员工与人事：按 组织架构管理 / 人员薪资管理 / 人员差勤管理 三大板块组织；'
                '入职全流程（建号→个人填报→主管确认→入册花名册）在组织架构管理内完成。',
        'modules': [
            {'name': '组织架构管理', 'en': 'OrgMgr', 'url': 'hr.org_admin', 'done': True},
            {'name': '人员薪资管理', 'en': 'PayMgr', 'url': 'hr.pay_admin', 'done': True},
            {'name': '人员差勤管理', 'en': 'AttMgr', 'url': 'hr.att_admin', 'done': True},
        ],
        'settings': [
            {'name': '假期设置', 'en': 'LeaveCfg', 'url': 'hr.leave_types', 'done': True},
            {'name': '法定节假日', 'en': 'Holiday', 'url': 'hr.holidays', 'done': True},
            {'name': '日常考勤规则', 'en': 'AttRule', 'url': 'hr.attendance_rules', 'done': True},
            {'name': '工资自定义字段', 'en': 'PayItem', 'url': 'hr.salary_fields', 'done': True},
            {'name': '五险一金比例', 'en': 'Social', 'url': 'hr.social_config', 'done': True},
            {'name': '岗位补贴项', 'en': 'Allowance', 'url': 'hr.allowance_config', 'done': True},
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
    {
        'key': 'finance', 'name': '财务管理系统', 'en': 'FMS',
        'color': '#C9902A', 'soft': '#F8F0DC',   # 品牌浅橙偏金
        'desc': '财务主线：账务管理（账套/科目/期初/凭证/账簿/期末结转与结账）→ 报表中心（利润表、资产负债表、'
                '现金流量表）→ 税务测算。公司不代收客户保费，代理人佣金由第三方代发、佣金外费用公对公结算，'
                '佣金与奖金池同样归在本系统。',
        'modules': [
            {'name': '账务管理', 'en': 'Acct', 'url': 'fin.acct_hub', 'done': True},
            {'name': '银企对账', 'en': 'Bank', 'url': 'fin.bank_hub', 'done': True},
            {'name': '报表中心', 'en': 'Report', 'url': 'fin.report_hub', 'done': True},
            {'name': '发票管理', 'en': 'Invoice', 'url': 'fin.invoices', 'done': True},
            {'name': '自动记账', 'en': 'Auto', 'url': 'fin.auto_book', 'done': True},
            {'name': '操作审计', 'en': 'Audit', 'url': 'fin.fin_audit', 'done': True},
            {'name': '数据备份', 'en': 'Backup', 'url': 'fin.fin_backup', 'done': True},
            {'name': '费用结算确认函', 'en': 'Confirm', 'url': 'fin.confirm_hub', 'done': True},
            {'name': '出纳收付款', 'en': 'Cash', 'url': 'fin.cash_orders', 'done': True},
            {'name': '数据体检', 'en': 'Audit', 'url': 'fin.audit_hub', 'done': True},
            {'name': '佣金', 'en': 'Comm', 'url': 'commission.commissions', 'done': True},
            {'name': '奖金池', 'en': 'Bonus', 'url': 'commission.bonuses', 'done': True},
        ],
        'settings': [
            {'name': '账套设置', 'en': 'Ledger', 'url': 'fin.config', 'done': True},
            {'name': '会计科目', 'en': 'Account', 'url': 'fin.accounts', 'done': True},
            {'name': '增值税参数', 'en': 'VatCfg', 'done': False},
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
    {
        'key': 'marketing', 'name': '核心业务管理系统', 'en': 'CRM',
        'color': '#9C4E2C', 'soft': '#F6E5DC',   # 品牌深橙偏棕
        'desc': '核心业务：保司档案（保险公司基础信息主数据）+ 产品合约（总总框架 / 分分合作协议台账，'
                '内含产品资料库 / 佣金查询 / 产品查询）。签约主体为海腾，本系统作内部协议备份与政策留痕，'
                '不产生佣金业绩数据。',
        'modules': [
            # 一期（2026-10-01）已上线：保司档案 + 产品合约（模块内含产品资料库）
            # 二期（2026-10-01）已上线：保司画像增强（省分公司 / 岗位联络 / 经营指标）
            #                     + 产品合约多缴别改造 + 继续率考核规则库
            # 十五改（2026-10-02）导航合并（威哥口径）：
            #   保司档案 ← 保险公司档案 + 省分公司档案（后者改为页内「省分公司」页签）
            #   产品合约 ← 合约总览 + 总公司合约协议 + 分公司合约协议 + 继续率考核规则
            #              （四者保留独立 URL，改挂「产品合约」页内页签，另加「佣金查询」）
            {'name': '保司档案', 'en': 'Insurer', 'url': 'crm.insurers', 'done': True},
            {'name': '产品合约', 'en': 'ProdCtr', 'url': 'crm.contracts', 'done': True},
            # 十九改（2026-10-02）：业务团队费用设置 = 先建架构、再录人、后配费用政策。
            #   「人力架构」= 省公司→分支→分区→营业部→营业组（录人前必须先建）；
            #   「人力数据清单」= 人员底表（谁属哪类团队、什么职级、推荐关系、银行卡）。
            {'name': '人力架构', 'en': 'OrgStruct', 'url': 'crm.org_struct', 'done': True},
            # 十九改++（2026-10-02）：合作经代公司基础信息（人力档案「所属经代公司」的维护端口）
            {'name': '合作经代', 'en': 'Agency', 'url': 'crm.agencies', 'done': True},
            {'name': '人力数据清单', 'en': 'Manpower', 'url': 'crm.manpower', 'done': True},
            # 二十一改（2026-10-03）：团队系数设置 = 系数 × 基准费率（对齐小程序分配机制）
            {'name': '团队系数设置', 'en': 'TeamCoef', 'url': 'crm.team_coefficient', 'done': True},
            # 二十二改（2026-10-05）：销售费用清单 = 先扣公司税费 × 团队系数（按团队查询）
            {'name': '销售费用清单', 'en': 'SalesFee', 'url': 'crm.sales_fee', 'done': True},
            # 2026-11-02 新增：产品佣金系数（一键导入采购成本 → 设销售系数 → 自动生成基础/销售/经营费用）
            {'name': '产品佣金系数', 'en': 'CommCoef', 'url': 'crm.commission_coefficient', 'done': True},
            # 十八改（2026-10-02）：产品查询 与 产品资料库 合并，统一命名「产品资料库」
            {'name': '产品资料库', 'en': 'ProdDoc', 'url': 'crm.docs', 'done': True},
            {'name': '投保订单录入', 'en': 'Perf', 'url': 'crm.policy_orders', 'done': True},
            # 新契约业务清单查询与提取（2026-10）：两端口汇总 + 多维筛选 + 导出
            {'name': '新契约查询', 'en': 'Query', 'url': 'crm.policy_query', 'done': True},
            # 经营报表（2026-10）：可视化经营数据阅览（ECharts 局部引入，破「无 ECharts」红线）
            {'name': '经营报表', 'en': 'Report', 'url': 'crm.dashboard', 'done': True},
            {'name': '续期管理', 'en': 'Renew', 'done': False},
            {'name': '保费测算', 'en': 'Calc', 'url': 'crm.premium_calc', 'done': False},
        ],
        'settings': [
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
    {
        'key': 'it', 'name': '信息技术管理系统', 'en': 'IT',
        'color': '#4A443E', 'soft': '#EDEAE6',   # 取品牌"文字·炭黑"
        'desc': '全局系统管理：职级权限流、用户、系统参数等，由 IT 统一配置（总版块的系统管理）。',
        'modules': [
            {'name': '权限设置·权限流配置', 'en': 'Perm', 'url': 'admin.levels', 'done': True},
            {'name': '用户管理', 'en': 'User', 'url': 'admin.users', 'done': True},
            {'name': '系统参数', 'en': 'SysCfg', 'done': False},
        ],
        'settings': [
            {'name': '全局权限流配置', 'en': 'PermFlow', 'url': 'admin.levels', 'done': True},
        ],
    },
    {
        # 2026-10-06 威哥拍板：模块名称由「计划书」升级为「销售支持系统」
        # （key 仍为 proposal，不动路由 / ENDPOINT_SYSTEM / BACK_MAP，避免连锁改动）
        'key': 'proposal', 'name': '销售支持系统', 'en': 'SALES',
        'color': '#B5532A', 'soft': '#F7E7DC',   # 品牌深橙偏砖红，区别于 CRM 深棕
        'desc': '销售支持系统（原「计划书」）：①产品计划书引擎配置——智能体解析保司费率表 / 现金价值表 / '
                '计划书 / 红利表，先跑通验证、再安装到系统；②录入保单要素自动测算基本保额、逐年现金价值、'
                '单利与 IRR、分红演示，生成可打印、可导出长图、可对接微信小程序的网页版产品计划书。',
        'modules': [
            # 本轮（2026-10-05）上线：录入 + 测算 + 保存 + 查看（复用保费测算引擎）
            {'name': '计划书', 'en': 'Plan', 'url': 'crm.proposals', 'done': True},
            # 2026-10-06 上线：智能体工作台（上传→解析→试跑→安装）
            {'name': '计划书引擎配置（智能体）', 'en': 'Agent', 'url': 'crm.proposal_engine', 'done': True},
        ],
        'settings': [
            {'name': '版块权限配置', 'en': 'PermCfg', 'done': False},
        ],
    },
]
SYSTEM_MAP = {s['key']: s for s in SYSTEMS}


# ===================== 二级菜单模块权限（用户级授权） =====================
# 与职级权限流(level_permission)是两条独立维度：职级决定功能操作权限，
# 本处决定"用户能看见/进入哪些二级菜单"（左侧菜单过滤 + before_request 拦截）。
def all_module_keys():
    """所有可授权的二级菜单端点（done=True 且有 url 的模块/设置）"""
    keys = []
    for s in SYSTEMS:
        for grp in ('modules', 'settings'):
            for m in s.get(grp, []):
                if m.get('done') and m.get('url'):
                    keys.append(m['url'])
    return keys


MODULE_KEYS = all_module_keys()


def user_module_keys(user):
    """返回用户可访问的二级菜单端点集合；is_admin 返回全部"""
    if not user:
        return set()
    if getattr(user, 'is_admin', False):
        return set(MODULE_KEYS)
    return {p.menu_key for p in user.module_perms}


def build_menu_tree(allowed):
    """按一级系统组织二级菜单（allowed 为可访问端点集合）；admin 传入全集。
    每项 subs 仅含该用户有权限的模块（纯中文 name，不带英文名）。"""
    tree = []
    for s in SYSTEMS:
        subs = []
        for grp in ('modules', 'settings'):
            for m in s.get(grp, []):
                if m.get('url') and m['url'] in allowed:
                    subs.append({'name': m['name'], 'url': m['url'], 'en': m.get('en', '')})
        tree.append({'name': s['name'], 'key': s['key'], 'subs': subs})
    return tree


# ===================== 各业务蓝图归属的一级系统（用于顶部标题栏显示"OA · OA办公系统"之类） =====================
BP_TO_SYSTEM = {'oa': 'oa', 'hr': 'ehr', 'commission': 'finance', 'fin': 'finance',
                'admin': 'it', 'adm': 'admin', 'crm': 'marketing'}
# 个别端点按业务归属覆盖蓝图默认值
# 原则（2026-09-18）：所有申请/审批类统一归 OA；人员差勤管理仅保留「月度出勤查询」（归 EHR）。
ENDPOINT_SYSTEM = {'oa.attendance_records': 'ehr',
                   'crm.proposals': 'proposal', 'crm.proposal_view': 'proposal',
                   'crm.proposal_engine': 'proposal'}


# ===================== 统一返回层级表（全站返回条） =====================
# 目的：每个模块页面都有返回，且位置、样式、返回目标口径完全一致。
# 做法：这里只声明"每页的上一级是谁"，返回条由 base.html 统一渲染，各业务模板不再自己写返回链接。
# 层级：系统总览(/) → 系统详情(/system/<key>) → 业务列表页 → 新建/编辑页
# 写法：端点: (父级端点, 父级参数, 按钮文字)；按钮文字留空(None)时，
#       只要父级参数里有 key，就自动显示该系统名（如 "ADM · 行政管理系统"），避免两处维护名字。
BACK_MAP = {
    # —— L2：系统详情页 → 返回系统总览
    'system_detail':            ('home', {}, '系统总览'),

    # —— L3：业务列表页 → 返回所属系统详情页
    #      EHR 业务模块按三大板块组织：组织架构管理 / 人员薪资管理 / 人员差勤管理，
    #      各子页面返回对应板块 hub；差勤按分类归属 EHR（不在 OA）
    'oa.announcements':         ('system_detail', {'key': 'oa'},      None),
    'oa.mails':                 ('system_detail', {'key': 'oa'},      None),
    # 「申请」与「费用申请」已合并 → 统一叫「申请中心」，回到 OA 系统详情页
    'oa.apply_hub':             ('system_detail', {'key': 'oa'},      None),
    'oa.approvals':             ('system_detail', {'key': 'oa'},      None),
    'oa.todos':                 ('system_detail', {'key': 'oa'},      None),
    # 费用申请与报销：子页面统一返回「申请中心」（原费用中心已并入）
    'oa.expense_claim_new':         ('oa.apply_hub', {}, '申请中心'),
    'oa.expense_claim_detail':      ('oa.apply_hub', {}, '申请中心'),
    'oa.expense_reimburse_new':     ('oa.apply_hub', {}, '申请中心'),
    'oa.expense_reimburse_detail':  ('oa.apply_hub', {}, '申请中心'),
    'oa.expense_print':             ('oa.apply_hub', {}, '申请中心'),
    # 统一申请中心：通用申请单（人事/财务/行政/通用 目录中未建专属表单的类型）
    'oa.apply_form':                ('oa.apply_hub', {}, '申请中心'),
    # 薪酬发放申请（人事类专属表单）：新建页 / 详情页都回「申请中心」
    'oa.salary_apply':              ('oa.apply_hub', {}, '申请中心'),
    'oa.salary_apply_detail':       ('oa.apply_hub', {}, '申请中心'),
    'hr.org_admin':             ('system_detail', {'key': 'ehr'},     None),
    'hr.pay_admin':             ('system_detail', {'key': 'ehr'},     None),
    'hr.att_admin':             ('system_detail', {'key': 'ehr'},     None),
    'hr.hr_home':               ('system_detail', {'key': 'ehr'},     None),
    # 行政管理系统（ADM 一期）：三本台账列表回 ADM 系统详情页；表单页回对应台账列表
    'adm.licenses':             ('system_detail', {'key': 'admin'},   None),
    'adm.contracts':            ('system_detail', {'key': 'admin'},   None),
    'adm.assets':               ('system_detail', {'key': 'admin'},   None),
    'adm.categories':           ('system_detail', {'key': 'admin'},   None),
    'adm.license_new':          ('adm.licenses',  {},                 '证照资质'),
    'adm.license_edit':         ('adm.licenses',  {},                 '证照资质'),
    'adm.contract_new':         ('adm.contracts', {},                 '商务合同'),
    'adm.contract_edit':        ('adm.contracts', {},                 '商务合同'),
    'adm.asset_new':            ('adm.assets',    {},                 '固定资产'),
    'adm.asset_edit':           ('adm.assets',    {},                 '固定资产'),
    # 三期：费用台账（水电 / 物业 / 房租 / 通讯 + 缴费提醒）
    'adm.expenses':             ('system_detail', {'key': 'admin'},   None),
    'adm.expense_new':          ('adm.expenses',  {},                 '费用台账'),
    'adm.expense_edit':         ('adm.expenses',  {},                 '费用台账'),
    # —— 二期：行政事务（办公用品 / 印章使用登记 / 快递收发）
    'adm.services':             ('system_detail', {'key': 'admin'},   None),
    'adm.supplies':             ('adm.services',  {},                 '行政事务'),
    'adm.supply_new':           ('adm.supplies',  {},                 '办公用品'),
    'adm.supply_edit':          ('adm.supplies',  {},                 '办公用品'),
    'adm.supply_flow':          ('adm.supplies',  {},                 '办公用品'),
    'adm.seals':                ('adm.services',  {},                 '行政事务'),
    'adm.seal_new':             ('adm.seals',     {},                 '印章使用登记'),
    'adm.seal_edit':            ('adm.seals',     {},                 '印章使用登记'),
    'adm.express_list':         ('adm.services',  {},                 '行政事务'),
    'adm.express_new':          ('adm.express_list', {},              '快递收发'),
    'adm.express_edit':         ('adm.express_list', {},              '快递收发'),
    # —— 四期：车辆管理（车辆档案 / 保险年检提醒 / 保养维修违章用车动态）—— #
    'adm.vehicles':             ('system_detail', {'key': 'admin'},   None),
    'adm.vehicle_new':          ('adm.vehicles', {},                  '车辆管理'),
    'adm.vehicle_edit':         ('adm.vehicles', {},                  '车辆管理'),
    'adm.vehicle_detail':       ('adm.vehicles', {},                  '车辆管理'),
    # CRM 核心业务（一期：保司档案 + 产品合约 + 产品资料库）
    # 十五改：导航合并 ——「保司档案」吞并省分公司档案；
    #                    「产品合约」吞并 合约总览 + 总公司/分公司合约协议 + 继续率考核规则。
    #   被吞并的页面 URL 全部保留（可从页签直达），返回条父级统一改指向合并后的入口。
    'crm.insurers':             ('system_detail', {'key': 'marketing'}, None),
    'crm.insurer_detail':       ('crm.insurers',  {},                   '保司档案'),
    'crm.insurer_new':          ('crm.insurers',  {},                   '保司档案'),
    'crm.insurer_edit':         ('crm.insurers',  {},                   '保司档案'),
    # 四十一改：保司档案「数据源配置」页 → 返回保司档案列表
    'crm.insurer_source':       ('crm.insurers',  {},                   '保司档案'),
    'crm.contracts':            ('system_detail', {'key': 'marketing'}, None),
    'crm.contract_detail':      ('crm.contracts', {},                   '产品合约'),
    'crm.commission_query':     ('crm.contracts', {},                   '产品合约'),
    # 十七改：产品查询（按产品维度的台账视图，与佣金查询互补）
    'crm.product_query':        ('crm.contracts', {},                   '产品合约'),
    # 五改：合约按「总公司 / 分公司」两个口径分录（十五改起并入「产品合约」）
    'crm.hq_contracts':         ('crm.contracts', {},                   '产品合约'),
    'crm.hq_contract_new':      ('crm.contracts', {},                   '产品合约'),
    'crm.hq_contract_edit':     ('crm.contracts', {},                   '产品合约'),
    'crm.branch_contracts':     ('crm.contracts', {},                   '产品合约'),
    'crm.branch_contract_new':  ('crm.contracts', {},                   '产品合约'),
    'crm.branch_contract_edit': ('crm.contracts', {},                   '产品合约'),
    'crm.team_coefficient':     ('system_detail', {'key': 'marketing'}, None),
    # 二十二改：销售费用清单（按团队查询，复用 crm.view）
    'crm.sales_fee':            ('system_detail', {'key': 'marketing'}, None),
    # 2026-11-02 新增：产品佣金系数模块
    'crm.commission_coefficient':         ('system_detail', {'key': 'marketing'}, None),
    'crm.commission_coefficient_import':  ('crm.commission_coefficient', {}, '产品佣金系数'),
    'crm.commission_coefficient_config':  ('crm.commission_coefficient', {}, '产品佣金系数'),
    'crm.commission_coefficient_init':    ('crm.commission_coefficient', {}, '产品佣金系数'),
    # 十九改：人力架构 + 人力数据清单（业务团队费用设置的取数底表）
    'crm.org_struct':           ('system_detail', {'key': 'marketing'}, None),
    'crm.org_struct_new':       ('crm.org_struct', {},                  '人力架构'),
    'crm.org_struct_edit':      ('crm.org_struct', {},                  '人力架构'),
    # 十九改++：合作经代公司基础信息（人力档案「所属经代公司」的维护端口）
    'crm.agencies':             ('system_detail', {'key': 'marketing'}, None),
    'crm.agency_new':           ('crm.agencies',  {},                   '合作经代公司'),
    'crm.agency_edit':          ('crm.agencies',  {},                   '合作经代公司'),
    'crm.manpower':             ('system_detail', {'key': 'marketing'}, None),
    'crm.manpower_new':         ('crm.manpower',  {},                   '人力数据清单'),
    'crm.manpower_edit':        ('crm.manpower',  {},                   '人力数据清单'),
    # 十六改：唯一性规则设置（保司 / 分公司 / 合约 的「不允许重复录入」开关）
    'crm.unique_rules':         ('system_detail', {'key': 'marketing'}, None),
    # 分公司档案已并入「保司档案」（URL 保留，可从「省分公司」页签进入）
    'crm.branch_list':          ('crm.insurers',  {},                   '保司档案'),
    'crm.branch_new':           ('crm.insurers',  {},                   '保司档案'),
    'crm.branch_edit':          ('crm.insurers',  {},                   '保司档案'),
    'crm.docs':                 ('crm.contracts', {},                   '产品合约'),
    # 业绩管理 · 投保订单录入（2026-10）：新单录入 + Excel 导入 两端口汇总
    'crm.policy_orders':        ('system_detail', {'key': 'marketing'}, None),
    'crm.policy_order_new':     ('crm.policy_orders', {}, '投保订单录入'),
    'crm.policy_order_edit':    ('crm.policy_orders', {}, '投保订单录入'),
    # 业绩管理 · 新契约查询 / 经营报表（2026-10）
    'crm.policy_query':         ('system_detail', {'key': 'marketing'}, None),
    'crm.policy_query_export':  ('crm.policy_query', {}, '新契约查询'),
    'crm.dashboard':            ('system_detail', {'key': 'marketing'}, None),
    'crm.dashboard_data':       ('crm.dashboard', {}, '经营报表'),
    'crm.premium_calc':        ('system_detail', {'key': 'marketing'}, None),
    # 计划书系统（独立一级导航 'proposal'）：列表回系统详情页；查看页回计划书列表
    'crm.proposals':           ('system_detail', {'key': 'proposal'}, None),
    'crm.proposal_view':       ('crm.proposals',  {},                  '计划书'),
    'crm.proposal_engine':     ('system_detail',  {'key': 'proposal'}, None),
    # 继续率考核规则已并入「产品合约」
    'crm.rule_list':            ('crm.contracts', {},                   '产品合约'),
    'crm.rule_new':             ('crm.contracts', {},                   '产品合约'),
    'crm.rule_edit':            ('crm.contracts', {},                   '产品合约'),
    'crm.rule_match':           ('crm.contracts', {},                   '产品合约'),
    'commission.commissions':   ('system_detail', {'key': 'finance'}, None),
    'commission.bonuses':       ('system_detail', {'key': 'finance'}, None),
    'fin.acct_hub':             ('system_detail', {'key': 'finance'}, None),
    'fin.confirm_hub':          ('system_detail', {'key': 'finance'}, None),
    'admin.levels':             ('system_detail', {'key': 'it'},      None),

    # —— L3.5：板块内的子页面 → 返回所属板块 hub
    #      申请类统一归 OA「申请中心」；人员差勤管理仅保留「月度出勤查询」
    'fin.accounts':             ('fin.acct_hub', {},                  '账务管理'),
    'fin.opening':              ('fin.acct_hub', {},                  '账务管理'),
    'fin.vouchers':             ('fin.acct_hub', {},                  '账务管理'),
    'fin.voucher_new':          ('fin.vouchers', {},                  '记账凭证'),
    'fin.voucher_detail':       ('fin.vouchers', {},                  '记账凭证'),
    # 发票台账（#37）：列表/新增/详情/测算同挂发票台账，发票台账回到账务管理
    'fin.invoices':             ('fin.acct_hub', {},                  '账务管理'),
    'fin.invoice_new':          ('fin.invoices', {},                  '发票台账'),
    'fin.invoice_detail':       ('fin.invoices', {},                  '发票台账'),
    'fin.invoice_vat':          ('fin.invoices', {},                  '发票台账'),
    'fin.auto_book':             ('fin.acct_hub', {},                  '账务管理'),
    'fin.fin_audit':            ('fin.acct_hub', {},                  '账务管理'),
    'fin.fin_backup':           ('fin.acct_hub', {},                  '账务管理'),
    # 账簿查询（#34）：三张账同挂账簿首页，首页再回到账务管理
    'fin.ledger_hub':           ('fin.acct_hub', {},                  '账务管理'),
    'fin.ledger_balance':       ('fin.ledger_hub', {},                '账簿查询'),
    'fin.ledger_detail':        ('fin.ledger_hub', {},                '账簿查询'),
    'fin.ledger_journal':       ('fin.ledger_hub', {},                '账簿查询'),
    # 期末结转与结账（#36）：结账页挂账务管理
    'fin.closing':              ('fin.acct_hub', {},                  '账务管理'),
    # 报表中心（#35）：三张报表同挂报表中心，报表中心再回到账务管理
    'fin.report_hub':           ('fin.acct_hub', {},                  '账务管理'),
    'fin.report_bs':            ('fin.report_hub', {},                '报表中心'),
    'fin.report_pl':            ('fin.report_hub', {},                '报表中心'),
    'fin.report_cf':            ('fin.report_hub', {},                '报表中心'),
    'fin.report_custom':        ('fin.report_hub', {},                '报表中心'),
    'fin.confirm_batch':        ('fin.confirm_hub', {},               '费用结算确认函'),
    # 银企对账（二期 #42）：中心/账户管理归账务管理；流水/对账/调节表归对账中心
    'fin.bank_hub':             ('fin.acct_hub', {},                  '账务管理'),
    'fin.bank_accounts':        ('fin.acct_hub', {},                  '账务管理'),
    'fin.bank_statements':      ('fin.bank_hub', {},                  '银企对账'),
    'fin.bank_reconcile':       ('fin.bank_hub', {},                  '银企对账'),
    'fin.bank_adjust':          ('fin.bank_hub', {},                  '银企对账'),
    'fin.cash_orders':          ('fin.acct_hub', {},                  '账务管理'),
    'fin.audit_hub':           ('fin.acct_hub', {},                  '账务管理'),
    'oa.attendances':           ('oa.apply_hub', {},                  '申请中心'),
    'oa.attendance_records':    ('hr.att_admin', {},                  '人员差勤管理'),
    'oa.mail_detail':           ('oa.mails', {},                      '邮箱'),
    'hr.leave_apply':           ('oa.attendances', {},                '差勤申请'),
    'hr.salaries':              ('hr.pay_admin', {},                  '人员薪资管理'),
    'hr.onboard_list':          ('hr.org_admin', {},                  '组织架构管理'),
    'hr.employees':             ('hr.org_admin', {},                  '组织架构管理'),
    'hr.employees_active':      ('hr.org_admin', {},                  '组织架构管理'),
    'hr.employees_off':         ('hr.org_admin', {},                  '组织架构管理'),
    'hr.organization':          ('hr.org_admin', {},                  '组织架构管理'),
    'hr.contracts':             ('hr.org_admin', {},                  '组织架构管理'),

    # —— L3：本版块设置类页面 → 返回所属系统的设置页（与"系统详情→系统设置"入口对应）
    'fin.config':               ('system_settings', {'key': 'finance'}, 'FMS 系统设置'),
    'oa.flows':                 ('system_settings', {'key': 'oa'},    'OA 系统设置'),
    'hr.leave_types':           ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),
    'hr.salary_fields':         ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),
    'hr.holidays':              ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),
    'hr.attendance_rules':      ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),
    'hr.social_config':          ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),
    'hr.allowance_config':       ('system_settings', {'key': 'ehr'},   'EHR 系统设置'),

    # —— L4：编辑页 → 返回它的列表页
    #      说明："新建"表单直接嵌在各自列表页里（提交走 POST，无独立页面），故这里只有编辑页
    'hr.employee_edit':         ('hr.employees',      {}, '员工花名册'),
    'hr.salary_edit':           ('hr.salaries',       {}, '工资清单'),
    'hr.contract_edit':         ('hr.contracts',      {}, '人事合同'),
    'hr.hr_service_stub':       ('hr.hr_home',        {}, 'EHR 服务导航'),
    'hr.my_profile':            ('hr.hr_home',        {}, 'EHR 服务导航'),
    'hr.onboard_confirm':       ('hr.onboard_list',   {}, '入职登记'),
    'hr.onboard_edit':          ('hr.onboard_list',   {}, '入职登记'),
    'hr.employee_view':         ('hr.org_admin',      {}, '组织架构管理'),
    # 个人资料 / 人事卡片（#145）：入口在系统总览首页「员工工卡」右上角
    'hr.profile':               ('home',              {}, '系统总览'),
    'hr.profile_card':          ('hr.profile',        {}, '个人资料'),
    'hr.employee_change':       ('hr.employees_active', {}, '在职人员'),
    'commission.bonus_edit_page':  ('commission.bonuses',     {}, '奖金池'),
}


def _sys_title(key):
    """把系统 key 转成统一称谓「英文简称 · 中文名」（如 ADM · 行政管理系统）"""
    s = SYSTEM_MAP.get(key)
    return '%s · %s' % (s['en'], s['name']) if s else '系统总览'


def build_back_link(ep, view_args):
    """按 BACK_MAP 算出当前页的返回目标，返回 {'url','label'} 或 None（顶层页不显示返回条）。
    仅 GET 页面有返回条；纯表单提交（POST）类端点不在表里，自然不显示。"""
    # 1) 系统设置页：返回该系统详情页（key 来自 URL，需动态取）
    if ep == 'system_settings':
        key = (view_args or {}).get('key')
        return {'url': url_for('system_detail', key=key), 'label': _sys_title(key)}
    # 2) 规划中占位页：优先回到它所属的系统（链接带 ?from=<key>），没有来源则回总览
    if ep == 'stub':
        key = request.args.get('from')
        if key in SYSTEM_MAP:
            return {'url': url_for('system_detail', key=key), 'label': _sys_title(key)}
        return {'url': url_for('home'), 'label': '系统总览'}
    # 2.5) 费用结算确认函 · 单人页：返回它所属的那个批次（批次号来自 URL，需动态取）
    if ep == 'fin.confirm_letter':
        bid = (view_args or {}).get('bid')
        if bid:
            return {'url': url_for('fin.confirm_batch', bid=bid), 'label': '确认函批次'}
        return {'url': url_for('fin.confirm_hub'), 'label': '费用结算确认函'}
    # 2.6) 记账凭证 · 修改页：返回该凭证自己的详情页（凭证 id 来自 URL，需动态取）
    if ep == 'fin.voucher_edit':
        vid = (view_args or {}).get('vid')
        if vid:
            return {'url': url_for('fin.voucher_detail', vid=vid), 'label': '凭证详情'}
        return {'url': url_for('fin.vouchers'), 'label': '记账凭证'}
    # 2.7) 人事卡片 · 录入页：返回该员工的人事卡片（员工 id 来自 URL，需动态取）
    if ep == 'hr.card_edit':
        eid = (view_args or {}).get('eid')
        if eid:
            return {'url': url_for('hr.profile_card', eid=eid), 'label': '人事卡片'}
        return {'url': url_for('hr.profile'), 'label': '个人资料'}
    # 2.8) 核心业务（CRM）二期子页：返回所属保司详情 / 继续率规则列表
    if ep in ('crm.branch_new', 'crm.contact_new'):
        iid = (view_args or {}).get('iid')
        if iid:
            return {'url': url_for('crm.insurer_detail', iid=iid), 'label': '保司档案详情'}
        return {'url': url_for('crm.insurers'), 'label': '保司档案'}
    if ep in ('crm.branch_edit', 'crm.contact_edit'):
        from models import InsurerBranch, InsurerContact
        if ep == 'crm.branch_edit':
            obj = InsurerBranch.query.get((view_args or {}).get('bid'))
        else:
            obj = InsurerContact.query.get((view_args or {}).get('kid'))
        if obj:
            return {'url': url_for('crm.insurer_detail', iid=obj.insurer_id), 'label': '保司档案详情'}
        return {'url': url_for('crm.insurers'), 'label': '保司档案'}
    # 2.9) AI 机器人 · 模型配置页：优先回「来时那一页」（机器人齿轮跳转时带 ?from=），
    #      没有来源就回核心业务系统详情页。
    #      ⚠️ 这一页原来没进 BACK_MAP，从机器人齿轮进来后没有任何出口（威哥 2026-10-29 反馈）。
    if ep == 'ai.ai_config':
        frm = (request.args.get('from') or '').strip()
        if frm.startswith('/') and not frm.startswith('//'):
            return {'url': frm, 'label': '上一页'}
        return {'url': url_for('system_detail', key='marketing'),
                'label': _sys_title('marketing')}
    # 3) 其余页面按表查父级
    cfg = BACK_MAP.get(ep)
    if not cfg:
        return None
    parent_ep, kwargs, label = cfg
    # 按钮文字留空时，按父级参数里的 key 自动生成系统名
    if label is None:
        label = _sys_title(kwargs.get('key'))
    return {'url': url_for(parent_ep, **kwargs), 'label': label}


# ===================== 全局模板注入：左侧导航 / LOGO / 页面标题 =====================
# 作用：任何页面只要 extends base.html，就自动带上左侧系统导航与品牌 LOGO，
# 不必在每个模板里重复写一遍导航（新增系统只改上面的 SYSTEMS 一处）。
def find_static_img(stem, exts=('png', 'jpg', 'jpeg', 'svg', 'webp')):
    """在 static/img/ 下按优先级查找图片，返回静态地址；找不到返回 None。
    以后更换图片：把新图按 stem 命名丢进 static/img/ 即可，无需改代码。"""
    for ext in exts:
        fn = '%s.%s' % (stem, ext)
        if os.path.exists(os.path.join(app.static_folder, 'img', fn)):
            return url_for('static', filename='img/' + fn)
    return None


def find_logo():
    """公司完整 LOGO：static/img/logo.*。没有则由模板用内置橙色字标兜底。"""
    return find_static_img('logo')


def find_logo_mark():
    """公司 LOGO 图标（圆形海浪标记，无文字、透明底）：static/img/logo-mark.png。
    侧边栏/登录页那个"白色圆角框"里放的就是它，图标已裁掉四周白边并居中，
    因此在框里能撑满、不偏不倚。若没有该文件则退回完整 LOGO。"""
    return find_static_img('logo-mark') or find_logo()


@app.context_processor
def inject_layout():
    """向所有模板注入：nav_systems（左侧导航）、logo_mark_url（LOGO 图标）、page_title（页面标题）"""
    path = request.path
    ep = request.endpoint or ''
    if path.startswith('/system/'):                      # 系统详情 / 设置页
        parts = path.split('/')
        s = SYSTEM_MAP.get(parts[2]) if len(parts) > 2 else None
        if s:
            title = '%s · %s' % (s['en'], s['name'])
            if path.endswith('/settings'):
                title += ' · 系统设置'
        else:
            title = '系统总览'
    elif path == '/':
        title = '系统总览'
    elif ep in ENDPOINT_SYSTEM:                          # 个别端点按业务归属覆盖
        s = SYSTEM_MAP[ENDPOINT_SYSTEM[ep]]
        title = '%s · %s' % (s['en'], s['name'])
    elif ep.split('.')[0] in BP_TO_SYSTEM:               # 各业务页按所属系统显示
        s = SYSTEM_MAP[BP_TO_SYSTEM[ep.split('.')[0]]]
        title = '%s · %s' % (s['en'], s['name'])
    else:
        title = '海盈通保OA管理系统'
    return {
        'nav_systems': SYSTEMS,
        # 当前用户可见的层级菜单（已按权限过滤，二级项仅含被授权的模块，纯中文）
        'menu_tree': build_menu_tree(user_module_keys(
            User.query.get(session['user_id']) if session.get('user_id') else None)),
        'logo_url': find_logo(),             # 公司完整 LOGO（备用）
        'logo_mark_url': find_logo_mark(),   # LOGO 图标：白色圆角框内使用
        'login_bg_url': find_static_img('login-bg'),  # 登录页左侧大图（放入 static/img/login-bg.jpg 即自动启用）
        'page_title': title,
        # 统一返回条：由 BACK_MAP 算出当前页的上一级，base.html 负责渲染（顶层页为 None）
        'back': build_back_link(ep, request.view_args),
    }


@app.before_request
def guard_module_access():
    """二级菜单模块权限拦截：登录用户访问受控模块端点且无权限时跳回首页。
    仅拦截 MODULE_KEYS 中的端点（各系统的二级菜单模块）；
    登录/首页/系统详情/IT后台自身管理页等不受影响。"""
    if 'user_id' not in session:
        return
    ep = request.endpoint or ''
    if ep not in MODULE_KEYS:
        return
    user = User.query.get(session['user_id'])
    if user and (getattr(user, 'is_admin', False) or ep in user_module_keys(user)):
        return
    flash('您没有访问该模块的权限')
    return redirect(url_for('home'))


@app.route('/')
@login_required
def home():
    """系统总览（需登录）：六大系统导航 + 当前用户与数据可见范围"""
    user = User.query.get(session['user_id'])
    # 数据可见范围由"职级所拥有的权限"推导，而不是写死角色名
    # 这样 IT 后台改了某职级的权限流，首页范围会自动跟着变
    scope = '无权限'
    if user and user.level:
        codes = {p.code for p in user.level.permissions}
        if 'data.view_all' in codes:
            scope = '全量数据'
        elif 'data.view_team' in codes:
            scope = '本团队数据'
        elif 'data.view_self' in codes:
            scope = '本人数据'
    # 员工工卡（系统总览首页展示）：
    # 按「工号」把登录账号对应到员工花名册（B 模式：档案与账号分离，靠工号人工对应，不建外键）
    emp = Employee.query.filter_by(employee_no=user.username).first() if user else None
    # 部门：优先取员工档案的部门；档案缺省时退回账号所属组织
    org_name = ''
    if emp and emp.org:
        org_name = emp.org.name
    elif user and user.org_id:
        _o = Organization.query.get(user.org_id)
        org_name = _o.name if _o else ''
    # 入职时间：兼容 Date 对象与历史字符串两种存法，统一转成 YYYY-MM-DD 再交给模板
    hire_str = ''
    if emp and emp.hire_date:
        hire_str = (emp.hire_date.strftime('%Y-%m-%d')
                    if hasattr(emp.hire_date, 'strftime') else str(emp.hire_date))
    # 应用角色：显示职级，并附该职级当前可用权限数（IT 后台调整权限流后这里自动跟着变）
    perm_n = len(user.level.permissions) if (user and user.level) else 0
    return render_template('system_map.html', user=user, scope=scope, systems=SYSTEMS,
                           emp=emp, org_name=org_name, hire_str=hire_str, perm_n=perm_n)


@app.route('/health')
def health():
    """健康检查接口：后续部署/监控会用到（保持公开，无需登录）"""
    return {'status': 'ok'}


# 注册 OA 办公模块（放在最后，避免与 app 形成循环导入）
from oa import bp as oa_bp
app.register_blueprint(oa_bp)

# 注册 IT 管理员后台（职级权限流配置）
from admin import bp as admin_bp
app.register_blueprint(admin_bp)

# 注册人事系统（员工花名册等）
from hr import bp as hr_bp
app.register_blueprint(hr_bp)

# 注册佣金系统（销售渠道人员佣金管理）
from commission import bp as commission_bp
app.register_blueprint(commission_bp)

# 注册财务系统（账务管理：账套设置 + 会计科目表）
from finance import bp as finance_bp
app.register_blueprint(finance_bp)

# 注册行政管理系统（ADM 一期：证照资质 / 商务合同 / 固定资产 三本台账）
# 蓝图名用 'adm'，避免与 IT 后台的 admin.py（蓝图 'admin'）撞名。
from adm import bp as adm_bp
app.register_blueprint(adm_bp)

# 注册核心业务管理系统（CRM 一期：保司档案 / 产品合约 / 产品资料库）
from crm import bp as crm_bp
app.register_blueprint(crm_bp)

# 注册企业微信接入（OAuth 免密登录 + JS-SDK 签名）
from wecom import bp as wecom_bp
app.register_blueprint(wecom_bp)

# 注册 AI 悬浮机器人（四十四改：协议识别 → 待审草稿 → 人工确认）
# 必须放在 crm 之后：ai_bot.py 里 from crm import ... 复用合约保存函数
from ai_bot import bp as ai_bp
app.register_blueprint(ai_bp)


# ===================== 系统总览 / 各系统导航 / 设置 =====================
# 顶层架构导航：系统总览、单系统详情、单系统设置、规划中占位。
# 业务逻辑仍在各蓝图（oa/hr/commission/admin）中实现，这里只做"导航与分类"。

@app.route('/system/<key>')
@login_required
def system_detail(key):
    """单系统导航页：列出该系统的业务模块与本版块设置入口"""
    s = SYSTEM_MAP.get(key)
    if not s:
        flash('未知系统')
        return redirect(url_for('home'))
    # 十七改：核心业务（CRM）系统页顶部加「合约到期提醒」——进版块就能看见临期合约
    expiry = expiry_kpi = None
    if key == 'marketing':
        try:
            from crm import _expiry_alerts, _expiry_kpi
            expiry, expiry_kpi = _expiry_alerts(6), _expiry_kpi()
        except Exception:
            expiry, expiry_kpi = None, None
    return render_template('system_detail.html', system=s,
                           expiry=expiry, expiry_kpi=(expiry_kpi if expiry else None))


@app.route('/system/<key>/settings')
@login_required
def system_settings(key):
    """单系统设置页：本版块的细分配置与版块内权限配置。
    说明：全局权限流（职级×权限）由【IT管理系统】统一配置；
    此处仅负责本版块内的设置与版块权限，不做全局改动。"""
    s = SYSTEM_MAP.get(key)
    if not s:
        flash('未知系统')
        return redirect(url_for('home'))
    is_global = (key == 'it')  # IT 为全局系统管理（总版块）
    return render_template('system_settings.html', system=s, is_global=is_global)


@app.route('/stub/<name>')
@login_required
def stub(name):
    """规划中占位页：点击尚未开发的模块时显示"""
    return render_template('stub.html', name=name)


@app.route('/WW_verify_<fname>')
def wecom_verify(fname):
    """企业微信「可信域名」校验文件响应。

    使用场景：本地穿透 / 直连时，企微会访问 https://域名/WW_verify_xxx.txt 做域名归属校验。
    （nginx 环境由 nginx 的 location ^~ /WW_verify_ 处理；这里兜底 Flask 直接对外的情况。）

    用法：把企微后台下载的 WW_verify_xxx.txt 原样丢进项目根 wecom_verify/ 目录，
    文件名一个字都别改。企微来取时把文件内容原样吐回去。
    """
    import os
    import re
    # 防目录穿越：只放行常规文件名（字母/数字/下划线/点/横线）
    if not re.match(r'^[A-Za-z0-9_.-]+$', fname):
        return 'bad file name', 400
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wecom_verify')
    # 兼容两种存放方式：带 WW_verify_ 前缀（企微下载的原始文件名）或不带前缀
    for name in (fname, 'WW_verify_' + fname):
        path = os.path.join(base, name)
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), 200, {'Content-Type': 'text/plain; charset=utf-8'}
    return 'WW_verify file not found', 404


if __name__ == '__main__':
    # 【重要】请统一用 run.py 启动本系统（双击 启动系统.bat 即可）。
    # 直接运行本文件会与下方业务模块形成循环导入，启动会报 ImportError。
    app.run(debug=True)
