# ai_bot.py
# 核心业务管理系统 · AI 悬浮机器人（四十四改，2026-10-29）
#
# 威哥诉求：核心业务管理系统右下角挂一个 AI 机器人 —— 能打开 / 最小化 / 输入问题 / 发送消息 /
#   加载提示 / 消息滚动；用户提问后机器人「调用专家」，结合上传的产品合约协议内容做识别与分析，
#   自动把数据填进系统，再由人工审核确认后提交。
#
# 三条实现红线（必须守住，否则后患无穷）：
#   ① AI **只生成待审草稿**，绝不直接写正式合约。
#      草稿＝CrmProductContract 上 ai_draft=1 的一条记录，合约列表默认不显示；
#      走的是**标准编辑页**，威哥核对/改完点一次「保存」才转正（保存即把 ai_draft 清 0）。
#   ② 落库**复用 crm.py 的** _save_components / _save_tiers / _save_ryc_ratios，
#      不另写一套口径 —— 否则 AI 填的数与手工录的数在佣金查询/销售费用里算法会不一致。
#   ③ 机器人界面纯原生 JS（项目红线：不用前端框架），全站一份 templates/_ai_bot.html。
#
# 模型选型：智谱 GLM-4-Flash（每天 100 万 token 免费、128K 长上下文，能一次吞下整份协议）。
#   接口兼容 OpenAI 的 /chat/completions 规范，base_url 可在「AI 配置」页改，
#   换 DeepSeek / 通义 / Kimi 只改地址与模型名，本文件不用动。
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from types import SimpleNamespace

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, Response, session, url_for)

from models import (db, AiConfig, Insurer, InsurerBranch, CrmProductContract,
                    CrmContractTier, CrmContractFile)
from app import login_required, permission_required

# 复用 CRM 的保存函数与常量 —— 口径只有一份
from crm import (_save_components, _save_tiers, _save_ryc_ratios,
                 _next_code, _resolve_product_code, _components_of,
                 _contract_files, _attach_dir, _nk,
                 PAY_PERIODS, INSURANCE_TYPES, COVERAGE_TERMS, SALE_CHANNELS,
                 PAY_TYPES, CONTRACT_STATUS)

bp = Blueprint('ai', __name__)

# 单次喂给模型的协议正文上限（字符）。GLM-4-Flash 上下文 128K token，
# 中文约 1.5 字符/token，6 万字符≈4 万 token，留足余量给提示词与输出。
MAX_DOC_CHARS = 60000

# 支持的协议文件后缀（能抽文字的）
TEXT_EXT = {'pdf', 'doc', 'docx', 'xlsx', 'xlsm', 'txt', 'md', 'csv'}


# ===================== 一、配置 =====================

def ai_cfg():
    """取（必要时创建）全局唯一的 AI 配置单例 id=1。"""
    cfg = AiConfig.query.get(1)
    if cfg is None:
        cfg = AiConfig(id=1)
        db.session.add(cfg)
        db.session.commit()
    return cfg


def cfg_ready(cfg=None):
    """配置是否可用 → (ok, 原因)。没填 Key 时机器人能打开但只能提示去配置。"""
    cfg = cfg or ai_cfg()
    if (cfg.enabled or 'on') != 'on':
        return False, 'AI 机器人当前已停用（可在「AI 配置」里重新启用）'
    if not (cfg.api_key or '').strip():
        return False, '还没配置大模型 API Key —— 点机器人右上角 ⚙ 去填，或找系统管理员配置'
    if not (cfg.base_url or '').strip():
        return False, 'AI 接口地址为空，请到「AI 配置」填写'
    return True, ''


# ===================== 二、协议正文抽取 =====================

def _pdf_text(path):
    """PDF → 文本（pypdf，逐页抽）。扫描件没有文字层，会返回空。"""
    from pypdf import PdfReader
    rd = PdfReader(path)
    out = []
    for i, pg in enumerate(rd.pages):
        try:
            out.append(pg.extract_text() or '')
        except Exception:
            out.append('')
        if i > 120:                      # 防超长协议把内存吃满
            break
    return '\n'.join(out)


def _docx_text(path):
    """docx → 文本。docx 本质是 zip + XML，直接解 word/document.xml，不额外装 python-docx。"""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n == 'word/document.xml']
        if not names:
            return ''
        xml = z.read('word/document.xml').decode('utf-8', 'ignore')
    # 段落结束 → 换行；制表符 → 空格；再剥掉所有标签
    xml = re.sub(r'</w:p>', '\n', xml)
    xml = re.sub(r'<w:tab[^>]*/>', '\t', xml)
    txt = re.sub(r'<[^>]+>', '', xml)
    # XML 实体还原（&amp; 等）
    for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&apos;', "'")):
        txt = txt.replace(a, b)
    return txt


def _doc_text(path):
    """doc（Word 97-2003 二进制）→ 文本。五十改（2026-10-29）。

    .doc 不是 zip（那是 docx），而是 OLE 复合文档：正文散落在 `WordDocument` 流里，
    **不能**直接解码整个流（会带出样式名、页眉残留、乱码）。正确读法：
      ① FIB（WordDocument 前 0x0200 字节的头）里取 `fcClx/lcbClx`；
      ② 到 `1Table` 或 `0Table` 流里取出 CLX → 跳过 Prc 块，拿到 piece table（Pcdt，标志 0x02）；
      ③ piece table 里每个 piece 给出「字符区间」与「文件偏移 + 编码」：
         fc 的第 30 位＝1 表示该段是 8 位 ANSI（中文文档常用 CP936），否则是 UTF-16LE；
      ④ 逐段解码拼起来，再把 Word 的控制字符换成正常符号（\\x07 单元格、\\r 段落）。
    纯 Python + olefile，不依赖 Office/WPS。
    """
    import struct
    try:
        import olefile
    except ImportError:
        return ''
    # 有些系统把 docx 存成 .doc —— 先当 zip 试一次，能读就直接用
    if not olefile.isOleFile(path):
        try:
            return _docx_text(path)
        except Exception:
            return ''
    ole = olefile.OleFileIO(path)
    try:
        wd = ole.openstream('WordDocument').read()
        if len(wd) < 0x0200 or struct.unpack('<H', wd[0:2])[0] != 0xA5EC:
            return ''
        flags = struct.unpack('<H', wd[0x0A:0x0C])[0]
        if flags & 0x0100:                          # fEncrypted：设了打开密码，正文是密文
            return ''
        tname = '1Table' if (flags & 0x0200) else '0Table'
        if not ole.exists(tname):
            tname = '1Table' if ole.exists('1Table') else '0Table'
        if not ole.exists(tname):
            return ''
        tbl = ole.openstream(tname).read()
        fc_clx, lcb_clx = struct.unpack('<II', wd[0x01A2:0x01AA])
        clx = tbl[fc_clx:fc_clx + lcb_clx]
        i = 0
        while i < len(clx) and clx[i] == 0x01:      # 跳过 Prc（打印机设置等）块
            i += 3 + struct.unpack('<H', clx[i + 1:i + 3])[0]
        if i >= len(clx) or clx[i] != 0x02:
            return ''
        lcb = struct.unpack('<I', clx[i + 1:i + 5])[0]
        plc = clx[i + 5:i + 5 + lcb]
        n = max(0, (len(plc) - 4) // 12)            # 每条 piece 12 字节，CP 多一个
        if not n:
            return ''
        cps = [struct.unpack('<I', plc[j * 4:j * 4 + 4])[0] for j in range(n + 1)]
        base = (n + 1) * 4
        out = []
        for k in range(n):
            pcd = plc[base + k * 8: base + k * 8 + 8]
            fc = struct.unpack('<I', pcd[2:6])[0]
            ln = cps[k + 1] - cps[k]
            if ln <= 0:
                continue
            if fc & 0x40000000:                     # 8 位 ANSI 段（中文文档＝CP936）
                off = (fc & 0x3FFFFFFF) // 2
                out.append(wd[off:off + ln].decode('cp936', 'ignore'))
            else:                                  # UTF-16LE 段
                off = fc & 0x3FFFFFFF
                out.append(wd[off:off + ln * 2].decode('utf-16-le', 'ignore'))
        txt = ''.join(out)
    except Exception:
        return ''
    finally:
        try:
            ole.close()
        except Exception:
            pass
    # Word 控制字符：\x07 单元格/行结束、\r 段落、\x0b 软换行、\x0c 分页
    txt = (txt.replace('\x07', '\t').replace('\r', '\n')
              .replace('\x0b', '\n').replace('\x0c', '\n'))
    txt = ''.join(ch for ch in txt if ch >= ' ' or ch in '\n\t')
    return txt


def _xlsx_text(path):
    """Excel 协议（保司常用）→ 按「工作表 / 行 / 单元格」平铺成文本，保号靠 | 分隔。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append('【工作表】%s' % (ws.title or ''))
        for row in ws.iter_rows(values_only=True):
            cells = [('' if c is None else str(c)).strip() for c in row]
            if any(cells):
                out.append(' | '.join(cells))
    try:
        wb.close()
    except Exception:
        pass
    return '\n'.join(out)


# ===================== 二·B 扫描件识别（视觉模型 OCR，四十五改 2026-10-29）=====================
# 背景：保司协议大量是**盖章扫描件** —— PDF 里每页就是一整张图、没有文字层，
#   pypdf 抽出来是空的。以前只能回一句「读不到文字」，等于这个功能废了（威哥当天实测反馈）。
#   现在：把每页原图抠出来 → 视觉模型逐页转录 → 再走原来那套结构化。
#
# 为什么不装 PDF 渲染库：扫描件每页本来就存着一整张 JPEG，直接从 PDF 里取即可，
#   零新增依赖（不用 pymupdf / poppler）。
# 为什么用 glm-4v-flash：智谱免费档、能读中文，竖排和旋转 90° 的扫描件也认。

OCR_MAX_PAGES = 20        # 最多识别 20 页（再多就是附件堆，耗时也扛不住）
OCR_MAX_PX = 2400         # 发模型前单边上限，太大 base64 接口会拒
VISION_MODEL_OF = {'zhipu': 'glm-4v-flash'}   # 服务商 → 视觉模型（免费档）
IMAGE_EXT = {'jpg', 'jpeg', 'png', 'bmp', 'webp', 'tif', 'tiff'}

OCR_PROMPT = ('这是一份合同扫描件的一页。请把页面上的全部文字按阅读顺序转录成横排纯文本；'
              '保留条款编号、甲乙双方信息；表格用 | 分隔单元格。'
              '页面如果是横向放置或旋转过的，请按正常阅读方向转录。'
              '只输出转录结果本身 —— 不要描述「图中是什么」、不要总结、不要解释、不要加任何说明。')


def _ocr_page(im, cfg):
    """一张图 → 文字。返回 (text, err)。"""
    vmodel = VISION_MODEL_OF.get((cfg.provider or 'zhipu').strip())
    if not vmodel:
        return '', ('当前服务商（%s）没有配视觉模型，读不了扫描件；'
                    '扫描件请改用智谱（glm-4v-flash 免费），或手工录入。' % (cfg.provider or ''))
    try:
        from PIL import Image
        img = im.convert('RGB')
        w, h = img.size
        if max(w, h) > OCR_MAX_PX:            # 缩一缩，原图 3400px 太大接口会拒
            k = float(OCR_MAX_PX) / max(w, h)
            img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=88)
    except Exception as e:
        return '', '图片处理失败：%s' % e
    b64 = base64.b64encode(buf.getvalue()).decode()
    # 视觉模型走同一个 OpenAI 兼容接口，只是 model 换视觉版、输出上限 1024
    vcfg = SimpleNamespace(enabled='on', base_url=cfg.base_url, model=vmodel,
                           api_key=cfg.api_key, max_tokens=1000, temperature=0.1,
                           timeout_sec=120)
    txt, err = _llm([{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
        {'type': 'text', 'text': OCR_PROMPT}]}], cfg=vcfg, timeout=120)
    return (txt or '').strip(), err


def _pdf_page_images(path, max_pages=OCR_MAX_PAGES):
    """把 PDF 每页的第一张大图取出来 → ([(页号, PIL图)], 总页数)。"""
    from pypdf import PdfReader
    from PIL import Image
    rd = PdfReader(path)
    total = len(rd.pages)
    out = []
    for i, pg in enumerate(rd.pages[:max_pages]):
        try:
            imgs = pg.images
        except Exception:
            continue
        if not imgs:
            continue
        try:
            out.append((i + 1, Image.open(io.BytesIO(imgs[0].data))))
        except Exception:
            continue
    return out, total


def ocr_pdf(path, cfg=None):
    """扫描版 PDF → 文字（逐页 OCR）。返回 (text, err)。"""
    cfg = cfg or ai_cfg()
    try:
        pages, total = _pdf_page_images(path)
    except Exception as e:
        return '', 'PDF 打不开：%s' % e
    if not pages:
        return '', ('这个 PDF 里既没有文字层、也没有可提取的整页图片，没法读；'
                    '请提供原始 PDF，或把关键页拍照上传（jpg / png 也行）。')
    parts, errs = [], []
    for no, im in pages:
        t, err = _ocr_page(im, cfg)
        if err:
            errs.append('第%d页：%s' % (no, err))
            continue
        if t:
            parts.append('【第%d页】\n%s' % (no, t))
    txt = '\n\n'.join(parts)
    if len(txt) < 40:
        return '', ('扫描件一页都没读出来（%s）。换个清楚点的扫描件或照片，'
                    '也可以直接把条款打给我。' % ('；'.join(errs[:2]) or '模型没返回文字'))
    if total > len(pages):
        txt += '\n\n……（原件共 %d 页，本次只识别了前 %d 页）' % (total, len(pages))
    return txt, ''


def doc_text(path, cfg=None):
    """任意支持的协议文件 → 纯文本。返回 (text, err)。

    三档兜底（四十五改）：
      ① 有文字层 → 直接抽（pdf / doc / docx / xlsx / txt）
      ② PDF 抽不到文字（盖章扫描件）→ 抠页图 + 视觉模型逐页 OCR
      ③ 直接传图片（jpg / png，手机拍的）→ 视觉模型 OCR
    """
    ext = (os.path.splitext(path)[1] or '').lower().lstrip('.')

    # ③ 图片：直接 OCR
    if ext in IMAGE_EXT:
        try:
            from PIL import Image
            im = Image.open(path)
            t, err = _ocr_page(im.convert('RGB'), cfg or ai_cfg())
        except Exception as e:
            return '', '图片打不开：%s' % e
        if err:
            return '', err
        if len(t) < 40:
            return '', '这张图里没读出文字，换一张清楚点的再试。'
        return t[:MAX_DOC_CHARS], ''

    if ext not in TEXT_EXT:
        return '', ('暂不支持 .%s 的正文识别（支持 pdf / doc / docx / xlsx / txt / jpg / png）。'
                    % ext)
    try:
        if ext == 'pdf':
            t = _pdf_text(path)
        elif ext == 'doc':
            t = _doc_text(path)                 # 五十改：Word 97-2003 二进制
        elif ext == 'docx':
            t = _docx_text(path)
        elif ext in ('xlsx', 'xlsm'):
            t = _xlsx_text(path)
        else:
            with io.open(path, encoding='utf-8', errors='ignore') as f:
                t = f.read()
    except Exception as e:
        return '', '文件解析失败：%s（可能是加密或损坏的 PDF）' % e
    t = (t or '').strip()

    # ② 有文字层的 PDF 正常走上面；抽不到就是扫描件，换视觉模型逐页 OCR
    if len(t) < 40 and ext == 'pdf':
        t2, err2 = ocr_pdf(path, cfg)
        if err2:
            return '', err2
        t = t2

    if len(t) < 40:
        return '', ('这个文件里几乎读不到文字（多半是空页、图片版，或 Word 设了打开密码）。'
                    '请提供可复制文字的 PDF / Word；老的 .doc 若读不出来，'
                    '用 Word 另存为 .docx 或 PDF 再传。')
    if len(t) > MAX_DOC_CHARS:
        t = t[:MAX_DOC_CHARS] + '\n\n……（正文过长，已截断到 %d 字）' % MAX_DOC_CHARS
    return t, ''


# ===================== 三、大模型调用 =====================

# 「专家」角色设定 —— 威哥说的「调用专家」，落在这里：让模型以保险中介合约条款专家的身份读协议。
EXPERT_SYS = ("""你是「广东海盈通保智能科技有限公司」产品合约管理系统的资深条款专家，
长期处理保险公司与保险中介机构之间的产品代理/合作协议，熟悉寿险、重疾险、医疗险、意外险、年金险、
终身寿险的政策口径。你的任务是把用户发来的**产品合约协议**读懂，并整理成系统可直接录入的结构化数据。

必须遵守的业务口径（这是本公司系统的定式，不能自行改动）：
1. FYC / RYC 都是**佣金率百分比**（15.5 表示 15.5%），保留 2 位小数；不要换算成金额。
2. 缴费期（缴别）用中文写法：「趸交」「3年交」「5年交」「10年交」「20年交」「交至60岁」等。
3. 续期佣金按年度记：`ryc_2` 表示第 2 个保单年度（次年），依次到 `ryc_10`（第 10 年）。
4. 「续期服务费比例」＝按继续率（保单继续率 %）分档给出的**发放比例 %**，是一个矩阵：
   区间（下限/上限，上限空＝无上限）× 年度（2~10）→ 比例。它与 RYC 基础费率是两回事，别混。
5. 签约级别：`tt`＝总对总、`tf`＝总对分（任一方是分公司）、`ff`＝分对分。
   判断看**签约主体**：保司方是「XX分公司」→ 已经不是总对总；
   保司分公司 + 中介总公司 → `tf`；两边都是分公司 → `ff`；两边都是总公司才 `tt`。
6. 销售渠道：`offline`＝线下、`online`＝网销；协议没写就留空。
7. 险种类型只能从这些值里选：""" + '、'.join(INSURANCE_TYPES) + """。
8. 保障期限写法：「保终身」「保至70岁」「保20年」等，协议没写就留空。
9. 协议写「经纪费比例：每年 X%」「代理费：每年 X%」时，意思是**每个保单年度**都按 X% 结算
   （首年与各续期年度同价），**不是**"缴费 20 年"，更不要把 X 当成缴别。

输出要求（**极重要**）：
· 只输出**一个 JSON 对象**，不要任何解释文字、不要 ```json 围栏、不要注释。
· 协议里没有的信息一律给空字符串 "" 或 0，**绝对不要编造**。
· 表格里的数字必须**逐格照抄**（产品代码、费率、比例尤其如此）；看不清就填 0 并写进 missing，
  宁可留空也不要填一个"看起来合理"的数字。
· 不确定的项写进 missing 数组，用一句中文说明缺什么。
· 数字只填数字（不要带 %、不要带千分位）。""")

# 抽取用的字段说明（贴在用户消息里，让模型照着填）
# ⚠️ 这里的示例值曾用真实数字（15.5 / 5.0 / 60 / 70 / "只含主险"），结果模型**直接照抄示例**，
#    把协议里根本没有的继续率档位和备注填进了草稿（2026-10-29 威哥实测踩到）。
#    所以：凡示例一律用 0 / "" / [] 占位，并明确写"占位不是答案"。
EXTRACT_ASK = """请阅读下面的协议正文，按这个 JSON 结构输出识别结果。

注意：下面 JSON 里的 0、""、[] 都只是**字段占位**，只告诉你"这个位置该放什么"，
**不是答案**。协议里没写的一律留 0 或 ""，绝对不要把这些占位数字当成数据填回去。

{
  "insurer_name": "签约保险公司全称（照抄协议抬头，是分公司就带「XX分公司」）",
  "sign_level": "tt 或 tf 或 ff（判断规则见口径第 5 条）",
  "product_name": "产品名称（不含保司名）",
  "product_code": "产品代码，逐字照抄协议，不许改写",
  "insurance_type": "险种类型（见口径第 7 条，只能取其中一个值）",
  "coverage_term": "保障期限（见口径第 8 条），没写就空",
  "sale_channel": "offline 或 online 或 空",
  "pay_type": "year 或 month，判断不出就 year",
  "is_custom": false,
  "components": [],
  "tiers": [
    {"pay_period": "", "tt_fyc": 0, "ff_fyc": 0,
     "ryc_2": 0, "ryc_3": 0, "ryc_4": 0, "ryc_5": 0, "ryc_6": 0,
     "ryc_7": 0, "ryc_8": 0, "ryc_9": 0, "ryc_10": 0, "note": ""}
  ],
  "ryc_ratios": {"years": [], "rows": []},
  "plans": [],
  "missing": [],
  "summary": ""
}

各字段怎么填：
· components：**只有**协议是「定制类组合产品」时才填，每项 {"product_name": "组件名", "product_code": "组件代码"}；
  普通产品给 []。
· tiers：一个缴别一条（趸交 / 3年交 / 5年交 / 10年交 / 20年交 …），按签约级别把费率填进 tt_fyc 或 ff_fyc。
  协议写「每年 X%%」＝每个保单年度都是 X%%，此时 tt_fyc（或 ff_fyc）与 ryc_2~ryc_10 全填 X，pay_period 记「每年」（见口径第 9 条）。
· ryc_ratios：**只有协议真的给了「按继续率分档发放」的表才填**，格式
  {"years": [涉及的年度，如 2,3,4], "rows": [{"min": 区间下限, "max": 区间上限, "ratios": {"2": 比例, "3": 比例}}]}，
  rows 按区间从小到大排。协议里没有继续率分档表 → years 与 rows 都给 []，
  **绝对不要自己编档位和比例**。
· plans：协议里的阶段激励方案（名称 + 原文摘要），没有给 []。
· missing：协议里缺、需要人工补的项，一句话一条。
· summary：两三句话总结这份协议的费用政策要点。

协议正文如下（可能是扫描件 OCR 出来的，表格会错位，请结合上下文判断；看不清的数字留 0 并写进 missing）：
============================
%s
============================"""


def _llm(messages, cfg=None, timeout=None):
    """调 OpenAI 兼容的 /chat/completions → (reply_text, err)。

    零第三方依赖：用标准库 urllib，避免再往 venv 塞 requests。
    智谱 / DeepSeek / 通义 / Kimi 的接口签名都一样，只是 base_url 和 model 不同。
    """
    cfg = cfg or ai_cfg()
    ok, why = cfg_ready(cfg)
    if not ok:
        return '', why
    url = (cfg.base_url or '').strip().rstrip('/') + '/chat/completions'
    body = {
        'model': (cfg.model or 'glm-4-flash').strip(),
        'messages': messages,
        'temperature': float(cfg.temperature if cfg.temperature is not None else 0.2),
        'max_tokens': int(cfg.max_tokens or 4096),
        'stream': False,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + (cfg.api_key or '').strip()})
    try:
        with urllib.request.urlopen(req, timeout=timeout or int(cfg.timeout_sec or 180)) as r:
            raw = r.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'ignore')[:400]
        except Exception:
            pass
        if e.code == 401:
            return '', 'API Key 无效或已过期（401）。请到「AI 配置」重新粘贴 Key。'
        if e.code == 429:
            return '', '免费额度调用太频繁了（429），等一两分钟再试；或换付费模型。'
        return '', '模型接口返回 %d：%s' % (e.code, detail or e.reason)
    except Exception as e:
        return '', '连不上模型接口：%s（检查网络，或确认接口地址 %s 正确）' % (e, url)
    try:
        d = json.loads(raw)
        return (d['choices'][0]['message']['content'] or ''), ''
    except Exception:
        return '', '模型返回格式看不懂：%s' % raw[:300]


def _json_of(text):
    """从模型回复里抠出 JSON 对象（容错：```json 围栏、前后废话）。"""
    s = (text or '').strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    i, j = s.find('{'), s.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:
        # 常见毛病：尾逗号 / 中文引号 → 修一把再试
        t = s[i:j + 1]
        t = re.sub(r',\s*([}\]])', r'\1', t)
        t = t.replace('“', '"').replace('”', '"')
        try:
            return json.loads(t)
        except Exception:
            return None


# ===================== 四、把识别结果映射成「表单」 =====================

class _FDict(dict):
    """伪表单对象：让 crm 的保存函数当作 request.form 来读。

    只需要 get / getlist / __contains__ 三个能力（_save_tiers / _save_ryc_ratios /
    _save_components 都只用这三个），所以继承 dict 再补个 getlist 就够。
    ⚠️ 值一律用**字符串**：crm 里的 _to_num() 会调 .replace()，传 float 会直接崩。
    """

    def getlist(self, k):
        v = self.get(k)
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]


def _n(v):
    """任意值 → 数字字符串（保留 2 位；None/空 → '0'）。"""
    try:
        if v is None or v == '':
            return '0'
        return '%.2f' % float(str(v).replace('%', '').replace(',', '').strip())
    except Exception:
        return '0'


def _s(v):
    return ('' if v is None else str(v)).strip()


def _pick(value, allowed, default=''):
    """值在允许集合里才用，否则给 default（防 AI 自由发挥出非法枚举值）。"""
    v = _s(value)
    if not v:
        return default
    for a in allowed:
        if _s(a).lower() == v.lower() or _s(a) == v:
            return _s(a)
        if isinstance(a, tuple) and len(a) == 2:
            if v.lower() == _s(a[0]).lower() or v == _s(a[1]):
                return _s(a[0])
    return default


def _match_insurer(name):
    """按保司全称/简称在档案里找 → (id, 匹配到的全称)。找不到 → (None, '')。"""
    nm = _s(name)
    if not nm:
        return None, ''
    rows = Insurer.query.all()
    norm = lambda x: re.sub(r'[\s（）()]', '', _s(x))
    k = norm(nm)
    for r in rows:                       # ① 全称完全相同
        if norm(r.name) == k:
            return r.id, r.name
    for r in rows:                       # ② 简称相同
        if norm(getattr(r, 'short_name', '')) and norm(r.short_name) == k:
            return r.id, r.name
    for r in rows:                       # ③ 互相包含（AI 少写「股份」两字也认）
        a, b = norm(r.name), k
        if a and b and (a in b or b in a) and abs(len(a) - len(b)) <= 8:
            return r.id, r.name
    return None, ''


def build_form(d):
    """识别结果 dict → 伪表单（字段名与 contract_form.html 完全一致）。"""
    form = _FDict()
    # —— 合约主信息 ——
    form['product_name'] = _s(d.get('product_name'))
    form['product_code'] = _s(d.get('product_code'))
    form['sign_level'] = _pick(d.get('sign_level'), ['tt', 'tf', 'ff'], 'tt')
    form['insurance_type'] = _pick(d.get('insurance_type'), INSURANCE_TYPES, '')
    form['coverage_term'] = _s(d.get('coverage_term'))
    form['sale_channel'] = _pick(d.get('sale_channel'), SALE_CHANNELS, '')
    form['pay_type'] = _pick(d.get('pay_type'), PAY_TYPES, 'year')
    form['is_custom'] = '1' if d.get('is_custom') else ''
    form['status'] = 'active'
    form['note'] = ''

    # —— 组合组件（仅定制类）——
    comps = d.get('components') or []
    cidx = []
    for i, cp in enumerate(comps):
        nm = _s((cp or {}).get('product_name'))
        if not nm:
            continue
        form['comp_name_%d' % i] = nm
        form['comp_code_%d' % i] = _s((cp or {}).get('product_code'))
        form['comp_type_%d' % i] = _s((cp or {}).get('insurance_type'))
        form['comp_note_%d' % i] = ''
        cidx.append(str(i))
    if cidx:
        form['comp_idx'] = cidx

    # —— 缴别费率（一行一个缴别）——
    tix = []
    for i, t in enumerate(d.get('tiers') or []):
        t = t or {}
        p = _s(t.get('pay_period'))
        if not p:
            continue
        form['tier_period_%d' % i] = p
        form['tier_tt_fyc_%d' % i] = _n(t.get('tt_fyc'))
        form['tier_ff_fyc_%d' % i] = _n(t.get('ff_fyc'))
        for y in range(2, 11):
            form['tier_ryc_%d_%d' % (y, i)] = _n(t.get('ryc_%d' % y))
        # note 有防呆：模型偶尔把提示词里的示例说明照抄进来，当备注落库很难看
        nt = _s(t.get('note'))
        if nt in ('该缴别的补充说明，如 只含主险', '该缴别的补充说明'):
            nt = ''
        form['tier_note_%d' % i] = nt
        # 定制类：缴别挂在哪个组件上（AI 没标就全挂第 1 个组件）
        _ci = _s(t.get('comp'))
        form['tier_comp_%d' % i] = _ci if _ci.isdigit() else ('0' if cidx else '')
        tix.append(str(i))
    if tix:
        form['tier_idx'] = tix

    # —— 续期服务费比例（区间 × 年度 → 发放比例 %）——
    rr = d.get('ryc_ratios') or {}
    years = []
    for y in (rr.get('years') or []):
        try:
            y = int(str(y).strip())
        except Exception:
            continue
        if 2 <= y <= 10 and y not in years:
            years.append(y)
    # 防呆：模型偶尔照抄示例的「全 0 比例」行，这种行落库等于凭空多一档，直接丢掉
    def _row_ok(row):
        row = row or {}
        for y in years:
            v = _s((row.get('ratios') or {}).get(str(y)))
            try:
                if v and float(v.replace('%', '').strip()) != 0:
                    return True
            except Exception:
                continue
        return False
    rows = [r for r in (rr.get('rows') or []) if _row_ok(r)]

    if years and rows:
        form['has_ratio'] = '1'
        form['rr_years'] = [str(y) for y in years]
        form['rr_rcnt'] = str(len(rows))
        for r, row in enumerate(rows):
            row = row or {}
            form['rr_min_%d' % r] = _s(row.get('min'))
            form['rr_max_%d' % r] = _s(row.get('max'))
            for y in years:
                v = (row.get('ratios') or {}).get(str(y))
                if v is not None:
                    form['rr_ratio_%d_%d' % (r, y)] = _s(v)
    return form


def _fill_by_payload(c, d, iid, iname):
    """把识别结果写进合约**主表**字段。

    ⚠️ 不能复用 crm._fill_contract()：那个函数内部用 `_f()` 读 **HTTP request.form**，
       传自定义表单对象它不认（会读到空值把字段冲掉）。所以这里直接 setattr。
    """
    c.insurer_id = iid
    c.insurer_name = iname
    it = Insurer.query.get(iid) if iid else None
    c.insurer_short = (getattr(it, 'short_name', '') or '') if it else ''
    c.sign_level = _pick(d.get('sign_level'), ['tt', 'tf', 'ff'], 'tt')
    c.product_name = _s(d.get('product_name'))
    c.product_code = _s(d.get('product_code'))
    c.coverage_term = _s(d.get('coverage_term'))
    c.sale_channel = _pick(d.get('sale_channel'), SALE_CHANNELS, '')
    c.insurance_type = _pick(d.get('insurance_type'), INSURANCE_TYPES, '')
    c.pay_type = _pick(d.get('pay_type'), PAY_TYPES, 'year')
    c.is_custom = 1 if d.get('is_custom') else 0
    c.status = 'active'
    c.note = ''
    c.misc_plan_text = ''


def _same_contract(iid, pname, pcode):
    """已有**正式**合约里，同保司且（产品代码相同 或 产品名规范化后相同）的那条 → 合约对象 / None。

    用途：① 建草稿前提醒「这产品已经有了，别重复录」；
         ② 产品名写法不一致时（「平安e惠保医疗」vs 库里「平安互联网e惠保医疗」）
            沿用库里的写法 —— 产品资料库是按名称挂材料的，名称一改就断链。
    """
    pn = _nk(pname or '')
    pc = (pcode or '').strip().upper()
    if not (pn or pc):
        return None
    q = CrmProductContract.query.filter(db.func.coalesce(CrmProductContract.ai_draft, 0) == 0)
    if iid:
        q = q.filter(CrmProductContract.insurer_id == iid)
    for r in q.all():
        if pc and (r.product_code or '').strip().upper() == pc:
            return r
        if pn and _nk(r.product_name or '') == pn:
            return r
    return None


def create_draft(d, src_name=''):
    """按识别结果生成一条**待审草稿合约** → (合约对象, 提示列表)。

    草稿＝ai_draft=1；缴别/比例走 crm 的原保存函数，口径与手工录入完全一致。
    """
    msgs = []
    iid, iname = _match_insurer(d.get('insurer_name'))
    if not iid and _s(d.get('insurer_name')):
        msgs.append('保司档案里没找到「%s」—— 草稿先不挂保司，审核时请手动选（或先去保司档案新增）'
                    % _s(d.get('insurer_name')))
    if not _s(d.get('product_name')):
        return None, ['协议里没识别出产品名称，没法建草稿（请在对话里告诉我产品名再试）']

    # 查重：同保司同代码 / 同产品名已经是正式合约了 → 提示，并且沿用库里的产品名写法
    same = _same_contract(iid, _s(d.get('product_name')), _s(d.get('product_code')))
    if same:
        msgs.append('这个产品系统里已经有正式合约了：%s《%s》。如果这份协议只是改条款，'
                    '建议直接改那份、别再新建；确认要另存一份再往下走。'
                    % (same.code, same.product_name or ''))

    c = CrmProductContract(code=_next_code(CrmProductContract, 'PTC'),
                           created_by=session.get('user_id'))
    _fill_by_payload(c, d, iid, iname)
    if same and _nk(same.product_name) != _nk(c.product_name):
        # 名称口径统一到库里的写法（产品资料库靠名称匹配，名字不同就挂不上材料）
        c.product_name = same.product_name
    # 产品代码：留空自动生成；同保司同产品沿用已有代码（三十八改口径）
    pcode, perr = _resolve_product_code(iid, c.product_name, c.product_code, exclude_cid=0)
    if perr:
        msgs.append('产品代码：%s（已留空，保存时自动生成）' % perr)
        pcode = ''
    c.product_code = pcode
    c.ai_draft = 1
    db.session.add(c)
    db.session.flush()                   # 先拿 id，缴别/组件才能挂上去

    form = build_form(d)
    compmap, _ = _save_components(c, form)
    n = _save_tiers(c, form, compmap)
    if not n:
        db.session.rollback()
        return None, ['协议里没识别出任何缴别的费率，没法建草稿。'
                      '请在对话里把费率表的缴别与 FYC / RYC 数值打给我，我再填。']
    _save_ryc_ratios(c, form)            # 续期服务费比例（没有就自动跳过）

    # 待审说明：抽到多少 + 还缺什么 + 让 AI 自己交代的缺项
    lines = ['来源：%s' % (src_name or '对话上传'),
             '识别缴别 %d 条，产品 %s' % (n, c.product_name or '—')]
    if compmap:
        lines.append('组合组件 %d 个' % len(compmap))
    miss = [x for x in (d.get('missing') or []) if _s(x)]
    if miss:
        lines.append('待人工补：' + '；'.join(_s(x) for x in miss[:8]))
    plans = d.get('plans') or []
    if plans:
        lines.append('协议里的阶段方案（系统未自动落库，请审核时手工确认）：' +
                     '；'.join(_s((p or {}).get('plan_name')) for p in plans[:6] if _s((p or {}).get('plan_name'))))
    if _s(d.get('summary')):
        lines.append('AI 摘要：' + _s(d.get('summary')))
    c.ai_note = '\n'.join(lines)
    db.session.commit()
    return c, msgs


# ===================== 五、路由 =====================

def _j(obj, code=200):
    """返回 JSON（中文不转义，前端直接显示）。"""
    return Response(json.dumps(obj, ensure_ascii=False), status=code,
                    mimetype='application/json; charset=utf-8')


def can_manage():
    """当前用户是否有「产品合约管理」权限。

    ⚠️ AJAX 接口不能直接用 permission_required —— 它无权限时是 flash + 302 跳首页，
       fetch 会拿到一整页 HTML，前端 JSON.parse 直接炸。所以这里自己判、自己回 JSON。
    """
    from models import User
    u = User.query.get(session.get('user_id'))
    return bool(u and u.level and any(p.code == 'crm.contract.manage' for p in u.level.permissions))


def _deny():
    return _j({'ok': False, 'error': '你没有「产品合约管理」权限，不能生成合约草稿。'
                                     '可以问其他问题，或找管理员开权限。'})


@bp.route('/ai/config', methods=['GET', 'POST'])
@login_required
@permission_required('crm.contract.manage')
def ai_config():
    """AI 机器人配置页：填 API Key / 模型 / 接口地址。"""
    cfg = ai_cfg()
    if request.method == 'POST':
        cfg.provider = (request.form.get('provider') or 'zhipu').strip()
        cfg.base_url = (request.form.get('base_url') or '').strip()
        cfg.model = (request.form.get('model') or 'glm-4-flash').strip()
        key = (request.form.get('api_key') or '').strip()
        if key:                          # 留空＝不改（避免每次保存都要重贴 Key）
            cfg.api_key = key
        if request.form.get('clear_key'):
            cfg.api_key = ''
        cfg.enabled = 'on' if request.form.get('enabled') else 'off'
        try:
            cfg.max_tokens = int(request.form.get('max_tokens') or 4096)
        except ValueError:
            cfg.max_tokens = 4096
        try:
            cfg.temperature = float(request.form.get('temperature') or 0.2)
        except ValueError:
            cfg.temperature = 0.2
        try:
            cfg.timeout_sec = int(request.form.get('timeout_sec') or 180)
        except ValueError:
            cfg.timeout_sec = 180
        cfg.note = (request.form.get('note') or '').strip()[:300]
        cfg.updated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        db.session.commit()
        flash('AI 配置已保存')
        # 「保存并返回」：直接跳回上一页（返回目标由 app.py 的 BACK_MAP 统一算，
        #   ?from= 会带回来源页）；「保存配置」则留在本页，方便接着点测试连接。
        if (request.form.get('act') or '') == 'save_back':
            from app import build_back_link
            b = build_back_link('ai.ai_config', {})
            return redirect(b['url'] if b else url_for('home'))
        return redirect(request.full_path.rstrip('?'))
    return render_template('ai/config.html', cfg=cfg,
                           ready=cfg_ready(cfg))


@bp.route('/ai/status')
@login_required
def ai_status():
    """机器人打开时先问一句「能不能用」——没配 Key 就直接弹配置卡片，别让用户白打字。"""
    ok, why = cfg_ready()
    return _j({'ok': True, 'ready': ok, 'msg': why})


@bp.route('/ai/ping', methods=['POST'])
@login_required
def ai_ping():
    """配置页「测试连接」——发一句最短的话，验证 Key / 地址 / 模型名。

    ⚠️ 必须**优先用页面上刚填、还没保存的值**（前端把表单一起 POST 上来）。
       否则威哥填完 Key 直接点「测试连接」，读到的是数据库里的空 Key，
       永远提示「还没配置 API Key」——这是个坑（四十四改 bugfix）。
       测试是**只读**行为：无论通不通，都不往数据库写任何东西。
    """
    if not can_manage():
        return _deny()
    d = request.get_json(silent=True) or {}
    base = ai_cfg()
    # 页面上填了就用页面上的；没填就退回库里已保存的
    def pick(k, fallback):
        v = _s(d.get(k))
        return v or _s(fallback)
    tmp = SimpleNamespace(
        enabled='on',                       # 测试时不受「停用」开关影响
        base_url=pick('base_url', base.base_url),
        model=pick('model', base.model or 'glm-4-flash'),
        api_key=pick('api_key', base.api_key),
        max_tokens=64,
        temperature=0.2,
        timeout_sec=30,
    )
    if not tmp.api_key:
        return _j({'ok': False, 'error': '还没填 API Key —— 请在左侧「API Key」框里粘贴 Key 后再点测试'})
    if not tmp.base_url:
        return _j({'ok': False, 'error': '接口地址为空，请先填 https://open.bigmodel.cn/api/paas/v4'})

    t0 = time.time()
    reply, err = _llm([{'role': 'user', 'content': '请只回复两个字：可用'}], cfg=tmp, timeout=30)
    if err:
        return _j({'ok': False, 'error': err,
                   'hint': '地址=%s 模型=%s' % (tmp.base_url, tmp.model)})
    return _j({'ok': True, 'reply': (reply or '').strip()[:80],
               'ms': int((time.time() - t0) * 1000),
               'model': tmp.model,
               'saved': not _s(d.get('api_key'))})   # saved=False 表示用的是页面新填的 Key，还没存库


@bp.route('/ai/chat', methods=['POST'])
@login_required
def ai_chat():
    """纯对话（不带文件）：用户问什么答什么，带最近几轮上下文。"""
    d = request.get_json(silent=True) or {}
    q = (d.get('message') or '').strip()
    if not q:
        return _j({'ok': False, 'error': '请先输入问题'})
    cfg = ai_cfg()
    ok, why = cfg_ready(cfg)
    if not ok:
        return _j({'ok': False, 'error': why, 'need_config': True})

    msgs = [{'role': 'system', 'content': EXPERT_SYS}]
    for h in (d.get('history') or [])[-8:]:      # 只带最近 8 条，省 token
        role = h.get('role')
        txt = _s(h.get('content'))[:2000]
        if role in ('user', 'assistant') and txt:
            msgs.append({'role': role, 'content': txt})
    msgs.append({'role': 'user', 'content': q})
    reply, err = _llm(msgs)
    if err:
        return _j({'ok': False, 'error': err})
    return _j({'ok': True, 'reply': (reply or '').strip()})


@bp.route('/ai/analyze', methods=['POST'])
@login_required
def ai_analyze():
    """识别协议并生成待审草稿。

    两种来源：
      · 直接上传文件（multipart，字段名 file）
      · 指定系统里已有的合约附件（contract_id + 可选 file_id）
    返回 {ok, cid, code, url, product, tiers, note}，前端把 url 做成「去审核」按钮。
    """
    if not can_manage():
        return _deny()
    # 先看有没有协议来源，再谈配置：用户连文件都没选，先提示选文件更直接
    _fs0 = request.files.get('file')
    if not (_fs0 and (_fs0.filename or '').strip()) and \
            not _s(request.form.get('contract_id')).isdigit():
        return _j({'ok': False, 'error': '请先选一份协议文件（点输入框左边的 📎）'})
    cfg = ai_cfg()
    ok, why = cfg_ready(cfg)
    if not ok:
        return _j({'ok': False, 'error': why, 'need_config': True})

    tmp = None
    src_name = ''
    fs = request.files.get('file')
    if fs and (fs.filename or '').strip():
        src_name = fs.filename
        ext = os.path.splitext(src_name)[1].lower().lstrip('.')
        if ext not in TEXT_EXT and ext not in IMAGE_EXT:
            return _j({'ok': False, 'error': '暂不支持 .%s 格式，请上传 pdf / doc / docx / xlsx，'
                                             '也可以直接传 jpg / png 的照片。' % ext})
        tmp_dir = os.path.join(current_app.config['UPLOAD_DIR'], 'crm', '_ai_tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        tmp = os.path.join(tmp_dir, 'ai_%s_%s.%s'
                           % (datetime.now().strftime('%Y%m%d%H%M%S'), os.getpid(), ext))
        fs.save(tmp)
        path = tmp
    else:
        cid = _s(request.form.get('contract_id'))
        fid = _s(request.form.get('file_id'))
        if not cid.isdigit():
            return _j({'ok': False, 'error': '请先选一份协议文件（点输入框左边的 📎）'})
        files = _contract_files(int(cid))
        if fid.isdigit():
            files = [f for f in files if f.id == int(fid)]
        if not files:
            return _j({'ok': False, 'error': '这条合约在系统里还没有可读的附件文件'})
        f0 = files[0]
        path = os.path.join(_attach_dir('contract'), f0.file_path or '')
        src_name = f0.file_name or ''
        if not os.path.exists(path):
            return _j({'ok': False, 'error': '附件在磁盘上找不到了（%s），请重新上传一份' % src_name})

    try:
        text, err = doc_text(path, cfg)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)           # 临时文件用完即删，不在磁盘留垃圾
            except OSError:
                pass
    if err:
        return _j({'ok': False, 'error': err})

    # 让模型读协议 → 出 JSON
    reply, err = _llm([{'role': 'system', 'content': EXPERT_SYS},
                       {'role': 'user', 'content': EXTRACT_ASK % text}])
    if err:
        return _j({'ok': False, 'error': err})
    d = _json_of(reply)
    if not d:
        return _j({'ok': False, 'error': '模型这次没按格式回数据（可能协议太长或表格太乱），'
                                         '再点一次试试；连续失败就换 Word 版协议或分段贴给我。',
                   'raw': (reply or '')[:500]})

    c, msgs = create_draft(d, src_name='%s（%s）' % (src_name, _s(d.get('product_name'))))
    if not c:
        return _j({'ok': False, 'error': '；'.join(msgs) or '识别失败'})

    side = 'hq' if (c.sign_level or 'tt') in ('tt', 'tf') else 'br'
    ep = 'crm.hq_contract_edit' if side == 'hq' else 'crm.branch_contract_edit'
    try:
        url = url_for(ep, cid=c.id)
    except Exception:
        url = '/crm/contracts/%d' % c.id
    note = _s(d.get('summary'))
    return _j({'ok': True, 'cid': c.id, 'code': c.code, 'url': url,
               'product': c.product_name or '',
               'tiers': _count_tiers(c.id),
               'insurer': c.insurer_name or '',
               'note': note, 'warn': msgs})


def _count_tiers(cid):
    return CrmContractTier.query.filter_by(contract_id=cid).count()


@bp.route('/ai/draft/<int:cid>/discard', methods=['POST'])
@login_required
@permission_required('crm.contract.manage')
def ai_draft_discard(cid):
    """放弃一条 AI 草稿（连带缴别/比例/附件一起删干净）—— 页面表单提交，不是 AJAX。"""
    c = CrmProductContract.query.get_or_404(cid)
    if not getattr(c, 'ai_draft', 0):
        flash('这条不是 AI 草稿，不能从这里删除')
        return redirect(url_for('crm.hq_contracts'))
    for t in CrmContractTier.query.filter_by(contract_id=cid).all():
        db.session.delete(t)
    from models import CrmContractComponent, CrmRycRatio, CrmContractPlan, CrmContractFile
    for m in (CrmContractComponent, CrmRycRatio, CrmContractPlan, CrmContractFile):
        for r in m.query.filter_by(contract_id=cid).all():
            if m is CrmContractFile and r.file_path:
                try:
                    os.remove(os.path.join(_attach_dir('contract'), r.file_path))
                except OSError:
                    pass
            db.session.delete(r)
    code = c.code                       # 先存下来：commit 之后对象已失效，再取属性会报错
    db.session.delete(c)
    db.session.commit()
    flash('AI 草稿已放弃（%s）' % code)
    return redirect(url_for('crm.hq_contracts'))
