# confirm_doc.py
# FMS · 费用结算确认函 · 数据处理库（增量 #41）
# ---------------------------------------------------------------------
# 本模块只做纯数据处理，不含任何 Flask 代码，可单独运行测试：
#     venv/Scripts/python.exe confirm_doc.py <费用结算数据.xlsx> [输出目录]
# 三段职责：
#   ① parse_workbook()  —— 读 Excel 四张表（人员信息 / 保单明细1 / 保单明细2 / 费用接收账户信息）
#   ② validate()        —— 四表勾稽防呆校验：错了不许生成，缺了提前提醒
#   ③ generate_pdf()    —— 按原版式逐人出 PDF
#
# 三条业务硬规则（沿用原「生成确认函.py」，不可改动）：
#   规则 1：保费总额按「保单号」去重后再合计（个人业务 + 渠道管理业务合并去重，不能直接相加）
#   规则 2：渠道管理业务只对「职位 = 渠道总」的人生效，其余人只看个人业务
#   规则 3：四表勾稽校验（工号无主 / 姓名不符 / 行内矛盾 / 账户行缺失）—— 有问题宁可不出函
# 与原脚本的两处差异（用户 2026-09-18 确认）：
#   差异 1：出具方抬头由「泛华联兴保险销售股份公司广东分公司」改为「广东海盈通保智能科技有限公司」
#   差异 2：承诺条款第 4 条的受托代发方固定为「海腾保险代理有限公司」，不再逐人取数
import os
from datetime import date, datetime

try:
    import openpyxl
except ImportError:                                    # pragma: no cover
    openpyxl = None

# ===================== 常量：Excel 表名与列名 =====================

SHEET_PERSON = '人员信息'
SHEET_SELF = '保单明细1'
SHEET_CHANNEL = '保单明细2'
SHEET_ACCOUNT = '费用接收账户信息'

# 渠道管理业务只对「这个职位」的人生效（规则 2）
CHANNEL_POSITION = '渠道总'
# 系统里数据来源的中文名（页面展示用）
KIND_CN = {'self': '个人业务', 'channel': '渠道管理业务'}

# 各表必需列（缺列直接拒绝导入，避免"少一列就静默算出错数"）
REQUIRED_COLS = {
    SHEET_PERSON: ['姓名', '工号', '职位'],
    SHEET_SELF: ['工号', '保单号', '投保人姓名', '险种名称', '承保日期',
                 '保费金额(元)', '费用比例', '实发费用(元)'],
    SHEET_CHANNEL: ['保单号', '投保人姓名', '险种名称', '承保日期',
                    '保费金额(元)', '费用比例', '实发费用(元)'],
    SHEET_ACCOUNT: ['工号', '收款人', '开户行', '银行账号', '身份证号', '手机号', '金额(元)'],
}

# 固定文字（版式与原脚本一致）
TITLE = '业务服务费用结算确认函'

COMMIT_TEMPLATE = """1、本人系上述保单相关业务的服务人员（或联络人），上述费用系基于本人为前述保单所提供的客户沟通、信息核对、资料传递、咨询解答等服务而产生。
2、 本人已核对上述保单明细及费用金额，确认所有数据真实、准确，无任何异议。
3、本人对上述保单的服务品质承担相应责任。若任一保单因本人提供的服务存在瑕疵、误导、代签名、未如实告知等品质问题引发退保、客户投诉或公司被追偿，公司有权按实际比例追回已发放的对应费用，本人同意无条件配合返还。公司可从本人未发放的费用中直接扣除，不足部分本人承诺在收到通知后15日内返还。
4、 本人同意并委托公司将本函项下费用支付至指定第三方公司（名称：{}）账户，由该第三方公司转付至本人。公司向该第三方公司账户完成支付后，即视为公司已履行完毕对本人的全部支付义务。本人与第三方公司之间的资金结算、延迟或纠纷，由本人自行与第三方公司解决，与公司无关。本人已知悉并同意上述支付安排。"""

NOTE_PARAGRAPHS = [
    ('备注：', True),
    ('1、 本确认函可通过电子签名或纸质签字签署。电子签名完成即生效；纸质签字自本人签字之日起生效。', False),
    ('2、 签署后，请将确认函扫描/拍照上报公司备份。指定第三方账户收款的，需同时提供对应人员银行卡、开户行、手机号、身份证号，以备打款。', False),
]


# ===================== 通用小工具 =====================

def missing_deps():
    """返回缺失的第三方依赖名（空列表 = 依赖齐全）。
    页面据此提前提示"去装依赖"，而不是等到点生成时才报错。"""
    miss = []
    if openpyxl is None:
        miss.append('openpyxl')
    try:
        import reportlab                                   # noqa: F401
    except ImportError:
        miss.append('reportlab')
    return miss


def s(v):
    """安全转字符串：防止科学计数法、去掉首尾空白。日期按 YYYY-MM-DD 输出。"""
    if v is None:
        return ''
    if isinstance(v, datetime):                 # datetime 是 date 的子类，必须先判 datetime
        return v.strftime('%Y-%m-%d')
    if isinstance(v, date):
        return v.strftime('%Y-%m-%d')
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))                  # 手机号/身份证号被存成数字时不出现 .0
        return ('%.10f' % v).rstrip('0').rstrip('.')
    if isinstance(v, int):
        return str(v)
    return str(v).strip()


def s_date(v):
    """日期列专用：datetime -> YYYY-MM-DD；字符串取空格前那段。"""
    if v is None:
        return ''
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y-%m-%d')
    return s(v).split(' ')[0]


def num(v):
    """安全转数字：容忍千分位逗号与空格；空/非法一律当 0。"""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(',', '').replace('，', '').replace(' ', '')
    if not t:
        return 0.0
    try:
        return float(t)
    except ValueError:
        return 0.0


def norm_month(v):
    """把结算年月统一成 6 位 YYYYMM（兼容 202607 / '2026-07' / 日期）。"""
    if v is None:
        return ''
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y%m')
    t = s(v).strip()
    if t.isdigit() and len(t) == 6:
        return t
    digits = ''.join(ch for ch in t if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return t


# ===================== ① 解析 Excel =====================

# 模板里用来占位/分隔的写法（用户的分组表在这些行上填了字面文字）
PLACEHOLDER = {'(空白)', '（空白）', '-', '—', '/', 'N/A', 'NA', '无'}


def _is_blank_row(row):
    """整行都是空、或全是「(空白)」这类占位符 → 当作空行忽略。
    业务背景：这份 Excel 按团队分组，组与组之间会用一整行「(空白)」做分隔，
    它不带任何业务数据，若当成真数据会误报「保单无主」。"""
    return all((not s(v)) or s(v) in PLACEHOLDER for v in row.values())


def _read_sheet(ws):
    """读一张表：首行当表头，返回 (列名列表, 数据行列表)。
    全空行 / 占位行跳过；表头重复时后者覆盖前者。"""
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    head = [s(c) for c in rows[0]]
    body = []
    for r in rows[1:]:
        if all(c is None or str(c).strip() == '' for c in r):
            continue
        d = {}
        for i, h in enumerate(head):
            if not h:
                continue
            d[h] = r[i] if i < len(r) else None
        if d and not _is_blank_row(d):
            body.append(d)
    return head, body


def parse_workbook(path):
    """解析《费用结算数据》Excel。
    返回 (parsed, errors)：
      parsed = {'person','person_order','self','channel','account'}
        各 *_dict 以工号为键；person_order 保持人员表原始顺序（出函顺序）
      errors = 结构性错误清单（缺表/缺列/文件读不了），有错就不该继续
    """
    errors = []
    if openpyxl is None:
        return None, ['服务器缺少 openpyxl 依赖，无法读取 Excel。请在项目目录执行「安装依赖.bat」']
    if not path or not os.path.exists(path):
        return None, ['找不到导入文件：%s' % (path or '(空)')]

    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as e:                              # 文件损坏 / 不是 xlsx
        return None, ['Excel 打开失败（请确认是 .xlsx 且未加密）：%s' % e]

    try:
        need = [SHEET_PERSON, SHEET_SELF, SHEET_CHANNEL, SHEET_ACCOUNT]
        miss = [n for n in need if n not in wb.sheetnames]
        if miss:
            return None, ['Excel 缺少工作表：%s（现有：%s）' % ('、'.join(miss), '、'.join(wb.sheetnames))]

        heads, bodies = {}, {}
        for n in need:
            heads[n], bodies[n] = _read_sheet(wb[n])

        # 必需列检查（保单明细2 的工号列名不固定，单独判断）
        for n in [SHEET_PERSON, SHEET_SELF, SHEET_ACCOUNT]:
            lack = [c for c in REQUIRED_COLS[n] if c not in heads[n]]
            if lack:
                errors.append('《%s》缺少列：%s' % (n, '、'.join(lack)))
        lack = [c for c in REQUIRED_COLS[SHEET_CHANNEL] if c not in heads[SHEET_CHANNEL]]
        if lack:
            errors.append('《%s》缺少列：%s' % (SHEET_CHANNEL, '、'.join(lack)))
        # 保单明细2 的「渠道总工号」列：优先按名找，找不到就用第一列（兼容旧表）
        id_col_ch = '渠道总工号' if '渠道总工号' in heads[SHEET_CHANNEL] else (
            '工号' if '工号' in heads[SHEET_CHANNEL] else (heads[SHEET_CHANNEL][0] if heads[SHEET_CHANNEL] else ''))
        if not id_col_ch:
            errors.append('《%s》找不到工号列（应为「渠道总工号」）' % SHEET_CHANNEL)
        if errors:
            return None, errors

        parsed = {'person': {}, 'person_order': [], 'self': {}, 'channel': {},
                  'account': {}, 'id_col_channel': id_col_ch, 'dup_person': []}

        # ---- 工号列空值审计 ----
        # 业务坑：账户表的「工号」「金额」等列若是公式，且文件由程序另存过（公式没有缓存值），
        # openpyxl 只能读到 None。这时整列取不到工号，这些行会被静默忽略，
        # 最后只表现成"账户行缺失"，很难排查——所以这里先单独统计，交给 validate 明确报出来。
        def _audit(rows_, col):
            total = len(rows_)
            empty = sum(1 for r in rows_ if not s(r.get(col)))
            return {'sheet': '', 'col': col, 'empty': empty, 'total': total}

        audit = {
            SHEET_SELF: _audit(bodies[SHEET_SELF], '工号'),
            SHEET_CHANNEL: _audit(bodies[SHEET_CHANNEL], id_col_ch),
            SHEET_ACCOUNT: _audit(bodies[SHEET_ACCOUNT], '工号'),
        }
        for k, v in audit.items():
            v['sheet'] = k
        parsed['id_gaps'] = audit

        # ---- 人员信息（工号唯一，重复时后者覆盖前者并记下来交给校验提示）----
        for r in bodies[SHEET_PERSON]:
            gid = s(r.get('工号'))
            if not gid:
                parsed.setdefault('person_no_id', []).append(r)
                continue
            if gid in parsed['person']:
                parsed['dup_person'].append(gid)
            else:
                parsed['person_order'].append(gid)
            parsed['person'][gid] = r

        # ---- 保单明细1：个人业务 ----
        for r in bodies[SHEET_SELF]:
            gid = s(r.get('工号'))
            if not gid:
                continue
            parsed['self'].setdefault(gid, []).append(r)

        # ---- 保单明细2：渠道管理业务 ----
        for r in bodies[SHEET_CHANNEL]:
            gid = s(r.get(id_col_ch))
            if not gid:
                continue
            parsed['channel'].setdefault(gid, []).append(r)

        # ---- 费用接收账户信息 ----
        for r in bodies[SHEET_ACCOUNT]:
            gid = s(r.get('工号'))
            if not gid:
                continue
            parsed['account'].setdefault(gid, []).append(r)

        return parsed, []
    finally:
        try:
            wb.close()
        except Exception:
            pass


# ===================== ② 四表勾稽校验 =====================

def validate(parsed):
    """防呆校验。返回 {'errors': [...], 'warnings': [...]}。
    分级原则：
      errors   —— 会算出错数据或让数据静默丢失，必须在修正后才能生成 PDF；
      warnings —— 结果仍可用，但有可疑之处（如某人第四节的账户表会是空的），提醒人工确认。
      另有 skipped（不出的函的人），结构化返回给页面单列，不混在 warnings 里刷屏。
    """
    errs, warns, skipped = [], [], []
    if not parsed:
        return {'errors': ['没有可校验的数据'], 'warnings': [], 'skipped': []}

    person = parsed['person']

    # 0) 工号列整列取不到值：多半是该列是公式、而文件被程序另存过导致缓存值丢失。
    #    这种情况必须直接拦住——否则所有行会被静默忽略，用户只会看到"账户行缺失"，无从下手。
    for sheet, g in (parsed.get('id_gaps') or {}).items():
        if not g['total'] or not g['empty']:
            continue
        if g['empty'] == g['total']:
            errs.append('【%s·工号列整列为空】《%s》共 %d 行数据，但「%s」列一行都没取到值。'
                        '如果该列是公式（例如从别的表带过来的），请用 Excel 打开该文件、'
                        '按 Ctrl+S 保存一次，让公式算出结果后再导入'
                        % (sheet, sheet, g['total'], g['col']))
        else:
            warns.append('【%s·部分行无工号】《%s》有 %d 行没填「%s」，这些行已被忽略'
                         % (sheet, sheet, g['empty'], g['col']))

    # 1) 结算月度：一张表里出现多个月度，说明数据混了，提醒按哪一个归档
    months = sorted({norm_month(r.get('结算年月')) for r in person.values()} - {''})
    if len(months) > 1:
        warns.append('【结算月度不一致】《人员信息》表里出现多个结算月度：%s，'
                     '本批次将按 %s 归档，请确认是否要分开导入' % ('、'.join(months), months[0]))

    # 1) 人员表有行但工号为空
    for r in parsed.get('person_no_id', []):
        errs.append('【人员表工号为空】「%s」这行没填工号，无法定位到人，请补全或删除该行'
                    % (s(r.get('姓名')) or '(未填姓名)'))
    # 2) 人员表工号重复
    for gid in parsed.get('dup_person', []):
        errs.append('【人员表工号重复】工号 %s 在《人员信息》表里出现多次，只会按最后一行出函，请去重' % gid)

    # 3) 账户表：工号无主 / 姓名与工号不符 / 行内信息矛盾 / 打款信息不全
    for gid, rows in parsed['account'].items():
        p = person.get(gid)
        for r in rows:
            payee, row_name = s(r.get('收款人')), s(r.get('姓名'))
            if not p:
                errs.append('【账户表工号无主】工号 %s（收款人 %s）在《人员信息》表中不存在，'
                            '该行不会被任何确认函引用，请核对' % (gid, payee or '(空)'))
                continue
            expected = s(p.get('姓名'))
            if row_name and row_name != expected:
                errs.append('【姓名与工号不符】工号 %s 在《人员信息》表里是「%s」，'
                            '但《费用接收账户信息》该行「姓名」列填的是「%s」，疑似填错人'
                            % (gid, expected, row_name))
            if payee and row_name and payee != row_name:
                errs.append('【行内信息矛盾】工号 %s 的账户行里「收款人 %s」与「姓名 %s」不一致'
                            % (gid, payee, row_name))
            if not payee:
                errs.append('【收款人为空】工号 %s 的账户行没填收款人，打款信息不完整' % gid)
            if not s(r.get('银行账号')):
                errs.append('【银行账号为空】工号 %s（%s）的账户行没填银行账号，打款信息不完整'
                            % (gid, expected or payee or ''))

    # 4) 保单表里出现人员表没有的工号（会被静默丢掉，属于必须修正）
    p1_missing = sorted({g for g in parsed['self'] if g not in person})
    p2_missing = sorted({g for g in parsed['channel'] if g not in person})
    if p1_missing:
        errs.append('【保单无主·个人业务】《保单明细1》里的工号 %s 在《人员信息》表中不存在，'
                    '这些保单不会出现在任何确认函里' % '、'.join(p1_missing))
    if p2_missing:
        errs.append('【保单无主·渠道管理】《保单明细2》里的工号 %s 在《人员信息》表中不存在，'
                    '这些保单不会出现在任何确认函里' % '、'.join(p2_missing))

    # 5) 逐人检查（与出函口径完全一致）
    no_policy = []          # 无保单的人聚合提示，避免刷屏
    for gid in parsed['person_order']:
        p = person[gid]
        name = s(p.get('姓名')) or gid
        self_rows = parsed['self'].get(gid, [])
        ch_rows = parsed['channel'].get(gid, []) if s(p.get('职位')) == CHANNEL_POSITION else []
        acc_rows = parsed['account'].get(gid, [])

        # 渠道管理业务有数据但职位不是「渠道总」——会被规则 2 丢掉，必须提醒
        if parsed['channel'].get(gid) and s(p.get('职位')) != CHANNEL_POSITION:
            warns.append('【渠道管理业务被忽略】%s（%s）在《保单明细2》里有 %d 条渠道管理业务，'
                         '但其职位是「%s」而不是「%s」，按规则这部分不会出现在确认函里'
                         % (name, gid, len(parsed['channel'][gid]), s(p.get('职位')) or '(空)', CHANNEL_POSITION))

        if not self_rows and not ch_rows:
            no_policy.append('%s（%s）' % (name, gid))
            skipped.append({'employee_no': gid, 'name': name, 'reason': '无任何保单'})
            continue
        if not acc_rows:
            warns.append('【账户行缺失】%s（%s）有保单但《费用接收账户信息》表里没有对应账户行，'
                         '确认函第四节会是空表、合计 0.00' % (name, gid))

        # 金额对不上（账户表合计 vs 明细实发费用合计）——只提醒，不阻断
        fee = round(sum(num(r.get('实发费用(元)')) for r in self_rows)
                    + sum(num(r.get('实发费用(元)')) for r in ch_rows), 2)
        acc = round(sum(num(r.get('金额(元)')) for r in acc_rows), 2)
        if acc_rows and abs(fee - acc) > 0.01:
            warns.append('【金额不一致】%s（%s）保单明细实发费用合计 %s，'
                         '但账户表金额合计 %s，相差 %s，请核对'
                         % (name, gid, _fmt(fee), _fmt(acc), _fmt(round(fee - acc, 2))))

    if no_policy:
        head = '、'.join(no_policy[:6])
        more = '，等共 %d 人' % len(no_policy) if len(no_policy) > 6 else ''
        warns.append('【无保单·跳过】以下人员本期没有保单，不会生成确认函：%s%s' % (head, more))
    return {'errors': errs, 'warnings': warns, 'skipped': skipped}


def _fmt(v):
    """金额显示：千分位 + 两位小数。"""
    return '{:,.2f}'.format(round(v or 0, 2))


# ===================== ③ 组装确认函数据（逐人） =====================

def _items(rows, kind):
    """把保单行转成明细 dict 列表（序号按来源表原顺序）。"""
    out = []
    for i, r in enumerate(rows, 1):
        out.append({
            'kind': kind, 'seq': i,
            'policy_no': s(r.get('保单号')),
            'holder_name': s(r.get('投保人姓名')),
            'product_name': s(r.get('险种名称')),
            'insure_date': s_date(r.get('承保日期')),
            'premium': round(num(r.get('保费金额(元)')), 2),
            'ratio': num(r.get('费用比例')),
            'fee': round(num(r.get('实发费用(元)')), 2),
        })
    return out


def build_letters(parsed):
    """按人员表顺序逐人组装确认函数据。
    返回 (letters, skipped)：
      letters —— 每人一份的 dict（含明细、账户、三项汇总）
      skipped —— 被跳过的人（无任何保单），页面要如实列出来
    规则 1（保费按保单号去重）、规则 2（渠道管理业务只对渠道总）在这里落地。
    """
    letters, skipped = [], []
    if not parsed:
        return letters, skipped

    for gid in parsed['person_order']:
        p = parsed['person'][gid]
        name = s(p.get('姓名')) or gid
        self_rows = parsed['self'].get(gid, [])
        ch_rows = parsed['channel'].get(gid, []) if s(p.get('职位')) == CHANNEL_POSITION else []

        if not self_rows and not ch_rows:
            skipped.append({'employee_no': gid, 'name': name,
                            'reason': '无任何保单'})
            continue

        # 规则 1：个人业务 + 渠道管理业务合并，按保单号去重后计算保费总额
        seen, uniq = set(), []
        for r in self_rows + ch_rows:
            pn = s(r.get('保单号'))
            if pn:
                if pn in seen:
                    continue
                seen.add(pn)
            uniq.append(r)
        premium_total = round(sum(num(r.get('保费金额(元)')) for r in uniq), 2)

        # 实发费用总额：两段各自求和后相加（与原脚本一致，不去重）
        fee_self = round(sum(num(r.get('实发费用(元)')) for r in self_rows), 2)
        fee_ch = round(sum(num(r.get('实发费用(元)')) for r in ch_rows), 2)
        fee_total = round(fee_self + fee_ch, 2)

        accounts = []
        for i, a in enumerate(parsed['account'].get(gid, []), 1):
            accounts.append({
                'seq': i, 'payee': s(a.get('收款人')), 'bank_name': s(a.get('开户行')),
                'bank_account': s(a.get('银行账号')), 'id_card': s(a.get('身份证号')),
                'phone': s(a.get('手机号')), 'amount': round(num(a.get('金额(元)')), 2),
            })
        account_total = round(sum(a['amount'] for a in accounts), 2)

        letters.append({
            'employee_no': gid, 'name': name,
            'position': s(p.get('职位')), 'team': s(p.get('所属团队')),
            'id_card': s(p.get('身份证号')), 'bank_card': s(p.get('银行卡号')),
            'bank_name': s(p.get('开户银行')), 'sign_state': s(p.get('签约状态')),
            'sign_date': s_date(p.get('签约日期')), 'phone': s(p.get('手机号')),
            'settle_month': norm_month(p.get('结算年月')),
            'self_items': _items(self_rows, 'self'),
            'channel_items': _items(ch_rows, 'channel'),
            'accounts': accounts,
            'premium_total': premium_total, 'fee_total': fee_total,
            'account_total': account_total,
        })
    return letters, skipped


# ===================== ④ 生成 PDF（版式沿用原脚本） =====================
# 纸张：宽固定 A4 宽 210mm；高度按内容自适应（长内容自动加长，保证不被截断）

PAGE_W = 210
MARGIN_L = 15
MARGIN_R = 15
MARGIN_T = 20
MARGIN_B = 12
ROW_H = 8
TABLE_WIDTH = PAGE_W - MARGIN_L - MARGIN_R
MM = 72.0 / 25.4                     # 1mm = 2.8346pt

FONT = 'STSong-Light'
FONT_BOLD = 'STSong-Light'
_font_ready = False


def _ensure_libs():
    """延迟导入 reportlab 并注册中文字体（缺库时给出可读的报错）。"""
    global _font_ready
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except ImportError:
        raise RuntimeError('服务器缺少 reportlab 依赖，无法生成 PDF。请在项目目录执行「安装依赖.bat」')
    if not _font_ready:
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))   # 内置中文字体，无需外部字体文件
        _font_ready = True


def _sw(text, size, bold=False):
    """量文字宽度（pt）。"""
    from reportlab.pdfbase import pdfmetrics
    return pdfmetrics.stringWidth(s(text), FONT_BOLD if bold else FONT, size)


def _draw_center(c, x_mm, y_mm, text, size=10, bold=False):
    txt = s(text)
    font = FONT_BOLD if bold else FONT
    c.setFont(font, size)
    c.drawString(x_mm * MM - c.stringWidth(txt, font, size) / 2, y_mm * MM, txt)


def _draw_left(c, x_mm, y_mm, text, size=10, bold=False):
    c.setFont(FONT_BOLD if bold else FONT, size)
    c.drawString(x_mm * MM, y_mm * MM, s(text))


def _split_lines(text, max_width_mm, size=10):
    """按可用宽度把一句中文折行（逐字测量）。"""
    txt = s(text)
    if not txt:
        return []
    lines, cur = [], ''
    for ch in txt:
        if _sw(cur + ch, size) <= max_width_mm * MM:
            cur += ch
        else:
            if cur:
                lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def _draw_paragraph(c, x_mm, y_mm, text, max_width_mm, size=10, line_spacing=3, bold=False):
    """画一段自动折行的文字，返回下一行的 y。"""
    y = y_mm
    for line in _split_lines(text, max_width_mm, size):
        _draw_left(c, x_mm, y, line, size, bold=bold)
        y -= (size / MM + line_spacing)
    return y


def _draw_table(c, y_start, col_widths, data, font_size=6.5):
    """画表格：外框 + 列线 + 首行浅蓝表头 + 「合计」行加粗。返回表格底部 y。"""
    from reportlab.lib import colors
    total_spec = sum(col_widths)
    if total_spec == 0:
        return y_start
    widths = [w / total_spec * TABLE_WIDTH for w in col_widths]
    start_x = (PAGE_W - TABLE_WIDTH) / 2
    xs, acc = [start_x], start_x
    for w in widths:
        acc += w
        xs.append(acc)

    rows, cols = len(data), len(widths)
    for r in range(rows + 1):
        y = y_start - r * ROW_H
        c.line(xs[0] * MM, y * MM, xs[-1] * MM, y * MM)
    for xv in xs:
        c.line(xv * MM, y_start * MM, xv * MM, (y_start - rows * ROW_H) * MM)

    if rows > 0:                                  # 表头浅蓝底色
        c.setFillColor(colors.lightblue)
        c.rect(xs[0] * MM, (y_start - ROW_H) * MM, (xs[-1] - xs[0]) * MM, ROW_H * MM,
               fill=1, stroke=0)
        c.setFillColor(colors.black)

    for ri in range(rows):
        row = data[ri]
        cell_y = y_start - ri * ROW_H - ROW_H / 2
        bold = (ri == 0) or (s(row[0]) == '合计')
        for ci in range(cols):
            txt = s(row[ci])
            if not txt.strip():
                continue
            _draw_center(c, xs[ci] + widths[ci] / 2, cell_y, txt, font_size, bold=bold)
    return y_start - rows * ROW_H


def generate_pdf(letter, out_path, issuer, agent_company, logo_path=None):
    """生成一份确认函 PDF。letter 为 build_letters() 里的一个 dict。
    issuer       —— 出具方抬头（广东海盈通保智能科技有限公司）
    agent_company—— 受托代发方（海腾保险代理有限公司），写入承诺条款第 4 条
    """
    _ensure_libs()
    from reportlab.lib.pagesizes import mm
    from reportlab.pdfgen import canvas

    self_items = letter.get('self_items') or []
    ch_items = letter.get('channel_items') or []
    accounts = letter.get('accounts') or []
    has_self, has_channel = bool(self_items), bool(ch_items)
    third_party = s(agent_company)

    month_str = _cn_month(letter.get('settle_month'))

    # ---- 先估算整页高度（内容超长就往下加长，不做分页，避免表格被切断）----
    height_logo = 20 if (logo_path and os.path.exists(logo_path)) else 0
    height_main_title, height_company, height_month = 14, 12, 12
    height_info = 6 * 6 + 10                                  # 人员信息 8 项、每行 2 列
    rows_self = len(self_items) + 2 if has_self else 0        # +表头 +合计
    rows_ch = len(ch_items) + 2 if has_channel else 0
    height_table_self = rows_self * ROW_H + 15 if rows_self else 0
    height_table_ch = rows_ch * ROW_H + 15 if rows_ch else 0
    gap = 10 if (has_self and has_channel) else 0
    sub_title = (6 if has_self else 0) + (6 if has_channel else 0)
    commit_lines = [ln.strip() for ln in COMMIT_TEMPLATE.format(third_party).splitlines() if ln.strip()]
    height_commit = sum(len(_split_lines(ln, TABLE_WIDTH, 10)) * (10 / MM + 3) + 3
                        for ln in commit_lines) + 10
    account_rows = 1 + len(accounts) + 1
    height_pay = account_rows * ROW_H + 20
    height_note = sum(len(_split_lines(p, TABLE_WIDTH, 9)) * (9 / MM + 3) + 3
                      for p, _ in NOTE_PARAGRAPHS) + 10

    total = (MARGIN_T + height_logo + height_main_title + height_company + height_month
             + height_info + 12 + sub_title + height_table_self + gap + height_table_ch
             + 12 + (3 * ROW_H + 15) + 12 + height_commit + 12 + height_pay
             + 16 + height_note + MARGIN_B)
    page_h = max(total, 297)

    c = canvas.Canvas(out_path, pagesize=(PAGE_W * mm, page_h * mm))
    y = page_h - MARGIN_T

    # ---- LOGO（有就画，没有就跳过）----
    if logo_path and os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, MARGIN_L * mm, (y - 16) * mm, width=50 * mm,
                        preserveAspectRatio=True, height=16 * mm)
            y -= 20
        except Exception:
            pass

    # ---- 标题 / 出具方 / 结算月度 ----
    _draw_center(c, PAGE_W / 2, y, TITLE, 16, bold=True)
    y -= 14
    _draw_center(c, PAGE_W / 2, y, s(issuer), 11)
    y -= 12
    _draw_center(c, PAGE_W / 2, y, '结算月度：%s' % month_str, 11, bold=True)
    y -= 18

    # ---- 人员信息（8 项、两列排布）----
    info = [('姓名', letter.get('name')), ('工号', letter.get('employee_no')),
            ('所属团队', letter.get('team')), ('身份证号', letter.get('id_card')),
            ('银行卡号', letter.get('bank_card')), ('开户银行', letter.get('bank_name')),
            ('签约状态', letter.get('sign_state')), ('签约日期', letter.get('sign_date'))]
    for i, (label, value) in enumerate(info):
        lx = MARGIN_L if i % 2 == 0 else MARGIN_L + 110
        lab = '%s：' % label
        _draw_left(c, lx, y, lab, 10, bold=True)
        _draw_left(c, lx + _sw(lab, 10, bold=True) / MM, y, s(value), 10)
        if i % 2 == 1:
            y -= 6
    y -= 14

    # ---- 一、服务费用明细 ----
    _draw_left(c, MARGIN_L, y, '一、服务费用明细', 11, bold=True)
    y -= 12
    cols = [5, 15, 10, 40, 13, 13, 8, 13]
    header = ['序号', '保单号', '投保人姓名', '险种名称', '承保日期', '保费金额(元)', '费用比例', '实发费用(元)']

    def table_data(items):
        data = [header]
        for it in items:
            data.append([it['seq'], it['policy_no'], it['holder_name'], it['product_name'],
                         it['insure_date'], '%.2f' % it['premium'],
                         '%.2f%%' % (it['ratio'] * 100), '%.2f' % it['fee']])
        data.append(['合计', '', '', '', '',
                     '%.2f' % sum(i['premium'] for i in items),
                     '', '%.2f' % sum(i['fee'] for i in items)])
        return data

    if has_self:
        _draw_left(c, MARGIN_L, y, '（个人业务）', 9)
        y -= 6
        y = _draw_table(c, y, cols, table_data(self_items), font_size=6.5) - 8
    if has_channel:
        _draw_left(c, MARGIN_L, y, '（渠道管理业务）', 9)
        y -= 6
        y = _draw_table(c, y, cols, table_data(ch_items), font_size=6.5) - 8
    y -= 7

    # ---- 二、费用发放汇总 ----
    _draw_left(c, MARGIN_L, y, '二、费用发放汇总', 11, bold=True)
    y -= 12
    y = _draw_table(c, y, [46, 38, 76], [
        ['项目', '金额(元)', '备注'],
        ['月度对应保费总额', '%.2f' % letter.get('premium_total', 0), '月度累计业务总计'],
        ['月度实发费用总额', '%.2f' % letter.get('fee_total', 0), '实际支付金额（委托第三方公司代付）'],
    ], font_size=9) - 15

    # ---- 三、确认与承诺 ----
    _draw_left(c, MARGIN_L, y, '三、确认与承诺', 11, bold=True)
    y -= 12
    for ln in commit_lines:
        y = _draw_paragraph(c, MARGIN_L, y, ln, TABLE_WIDTH, size=10, line_spacing=3) - 3
    y -= 5

    # ---- 四、费用接收账户信息 ----
    _draw_left(c, MARGIN_L, y, '四、费用接收账户信息（须附加银行卡和身份证复印件）', 10, bold=True)
    y -= 12
    pay_cols = [8, 18, 24, 32, 32, 18, 20]
    pay_data = [['序号', '收款人', '开户行', '银行账号', '身份证号', '手机号', '金额（元）']]
    for a in accounts:
        pay_data.append([a['seq'], a['payee'], a['bank_name'], a['bank_account'],
                         a['id_card'], a['phone'], '%.2f' % a['amount']])
    pay_data.append(['合计', '', '', '', '', '', '%.2f' % sum(a['amount'] for a in accounts)])
    y = _draw_table(c, y, pay_cols, pay_data, font_size=9) - 20

    # ---- 签字栏 ----
    for lab in ['本人签字：', '公司经办人签字：']:
        _draw_left(c, MARGIN_L, y, lab, 10, bold=True)
        _draw_left(c, MARGIN_L + _sw(lab, 10, bold=True) / MM, y, '____________________', 10)
        _draw_left(c, MARGIN_L + 140, y, '日期：', 10, bold=True)
        _draw_left(c, MARGIN_L + 140 + _sw('日期：', 10, bold=True) / MM, y, '____________', 10)
        y -= 16

    # ---- 备注 ----
    for para, bold in NOTE_PARAGRAPHS:
        y = _draw_paragraph(c, MARGIN_L, y, para, TABLE_WIDTH, size=9, line_spacing=2, bold=bold) - 3

    c.save()
    return out_path


def _cn_month(ym):
    """202607 -> 2026年07月"""
    t = s(ym)
    return '%s年%s月' % (t[:4], t[4:6]) if len(t) == 6 and t.isdigit() else t


# ===================== 单独测试入口 =====================

if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print('用法：python confirm_doc.py <费用结算数据.xlsx> [输出目录]')
        sys.exit(1)

    src = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                '_confirm_test_out')
    os.makedirs(outdir, exist_ok=True)

    lines = []
    data, errs = parse_workbook(src)
    lines.append('=== 解析结果 ===')
    if errs:
        lines.append('结构性错误：')
        lines += ['  - ' + e for e in errs]
    else:
        lines.append('人员表人数：%d' % len(data['person']))
        lines.append('个人业务表工号：%s' % sorted(data['self'].keys()))
        lines.append('渠道管理业务表工号：%s' % sorted(data['channel'].keys()))
        lines.append('账户表工号：%s' % sorted(data['account'].keys()))

        v = validate(data)
        lines.append('')
        lines.append('=== 校验：错误 %d 条 ===' % len(v['errors']))
        lines += ['  ✗ ' + e for e in v['errors']]
        lines.append('=== 校验：提醒 %d 条 ===' % len(v['warnings']))
        lines += ['  ! ' + w for w in v['warnings']]

        letters, skipped = build_letters(data)
        lines.append('')
        lines.append('=== 出函清单（%d 份）===' % len(letters))
        for L in letters:
            lines.append('  %s(%s) 职位=%s 个人%d条 渠道%d条 保费合计=%s 费用合计=%s 账户合计=%s'
                         % (L['name'], L['employee_no'], L['position'], len(L['self_items']),
                            len(L['channel_items']), _fmt(L['premium_total']),
                            _fmt(L['fee_total']), _fmt(L['account_total'])))
        lines.append('=== 跳过（%d 人）===' % len(skipped))
        for k in skipped:
            lines.append('  - %s(%s) %s' % (k['name'], k['employee_no'], k['reason']))

        if letters:
            for L in letters:
                p = os.path.join(outdir, '%s_%s_费用确认函.pdf' % (L['name'], L['employee_no']))
                generate_pdf(L, p, '广东海盈通保智能科技有限公司', '海腾保险代理有限公司')
                lines.append('  已生成：%s（%d 字节）' % (os.path.basename(p), os.path.getsize(p)))

    report = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_confirm_report.txt')
    with open(report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('report -> %s' % report)
