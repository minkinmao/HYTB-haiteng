# wecom.py
# 企业微信接入（2026-09-28）：自建网页应用「OAuth 免密登录」+ JS-SDK 签名
#
# 登录链路：员工在企业微信点应用图标 -> 打开 /wecom/entry
#   -> 未登录且企微内置浏览器 -> 跳 open.weixin.qq.com 授权
#   -> 企微带回 code 到 /wecom/callback -> 用 code 换 userid
#   -> 查 User.wecom_userid -> 写 session 自动登录（写法与 login() 完全一致，权限流零改动）
#
# 凭证从环境变量读（HYTB_WECOM_*），绝不写死、绝不下发前端。
import os
import time
import hashlib
import random
import string
import json
from urllib.request import urlopen
from urllib.parse import urlencode
from flask import Blueprint, request, redirect, url_for, session, jsonify, flash
from app import login_required, log_audit
from models import db, User

bp = Blueprint('wecom', __name__)

# ---------- 配置（环境变量优先，其次是项目根目录的 wecom_config.txt）----------
# 为什么加文件兜底？本地联调时改一个 txt 比配环境变量简单得多（尤其对不熟命令行的用户）。
# 生产部署仍推荐用环境变量（更安全，不会被误传到代码仓库）。
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wecom_config.txt')


def _read_config_file():
    """读取 wecom_config.txt，格式为 `KEY=值`，一行一个；# 开头为注释。"""
    data = {}
    try:
        with open(_CONFIG_FILE, 'r', encoding='utf-8-sig') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                data[k.strip().upper()] = v.strip()
    except Exception:
        pass
    return data


_FILE_CFG = _read_config_file()


def _cfg(key, default=''):
    """取配置：环境变量 HYTB_WECOM_<key> 优先；没有则取 wecom_config.txt 里的 <key>。"""
    v = os.environ.get('HYTB_WECOM_' + key)
    if v:
        return v
    return _FILE_CFG.get(key, default)

WECOM_CORPID = _cfg('CORPID')
WECOM_AGENTID = _cfg('AGENTID')
WECOM_SECRET = _cfg('SECRET')
WECOM_DOMAIN = _cfg('DOMAIN')  # 可信域名，如 https://oa.haiying.com（不含尾斜杠）

# 进程内缓存（开发/单进程够用；多进程部署可换 Redis 或落文件）
_token_cache = {'token': '', 'exp': 0}
_ticket_cache = {'ticket': '', 'exp': 0}


def _http_get(url, timeout=8):
    """调企业微信接口（标准库，不新增依赖）。返回 dict；异常返回带 errcode 的 dict。"""
    try:
        with urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {'errcode': -1, 'errmsg': 'http_error:%s' % e}


def get_access_token():
    """企业 access_token（corpid+secret）。带过期缓存。"""
    if _token_cache['token'] and _token_cache['exp'] > time.time():
        return _token_cache['token']
    if not (WECOM_CORPID and WECOM_SECRET):
        return ''
    url = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s' % (WECOM_CORPID, WECOM_SECRET)
    d = _http_get(url)
    if d.get('errcode') == 0:
        _token_cache['token'] = d['access_token']
        _token_cache['exp'] = time.time() + d.get('expires_in', 7200) - 200
        return d['access_token']
    return ''


def get_jsapi_ticket(token):
    """jsapi_ticket（JS-SDK 签名用）。带过期缓存。"""
    if _ticket_cache['ticket'] and _ticket_cache['exp'] > time.time():
        return _ticket_cache['ticket']
    if not token:
        return ''
    url = 'https://qyapi.weixin.qq.com/cgi-bin/get_jsapi_ticket?access_token=%s' % token
    d = _http_get(url)
    if d.get('errcode') == 0:
        _ticket_cache['ticket'] = d['ticket']
        _ticket_cache['exp'] = time.time() + d.get('expires_in', 7200) - 200
        return d['ticket']
    return ''


def build_oauth_url(redirect_uri):
    """构造网页授权链接（snsapi_base 静默授权，仅拿 userid）"""
    params = {
        'appid': WECOM_CORPID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'snsapi_base',
        'agentid': WECOM_AGENTID,
        'state': 'haiying',
    }
    return 'https://open.weixin.qq.com/connect/oauth2/authorize?' + urlencode(params) + '#wechat_redirect'


def jssdk_sign(url):
    """生成 JS-SDK 签名（企业身份：用 jsapi_ticket）"""
    ticket = get_jsapi_ticket(get_access_token())
    if not ticket:
        return {}
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
    ts = int(time.time())
    raw = 'jsapi_ticket=%s&noncestr=%s&timestamp=%s&url=%s' % (ticket, nonce, ts, url)
    sig = hashlib.sha1(raw.encode('utf-8')).hexdigest()
    return {
        'appId': WECOM_CORPID,
        'timestamp': ts,
        'nonceStr': nonce,
        'signature': sig,
        'jsApiList': ['closeWindow', 'hideOptionMenu', 'showOptionMenu',
                      'setNavigationBarTitle', 'chooseImage', 'previewImage',
                      'getLocalImgData', 'onHistoryBack'],
    }


def _is_wecom_ua():
    return 'wxwork' in (request.user_agent.string or '').lower()


@bp.route('/wecom/entry')
def entry():
    """企业微信应用入口。已登录直接进 OA；未登录且在企微内跳授权；否则走密码登录。"""
    if session.get('user_id'):
        return redirect(url_for('system_detail', key='oa'))
    if not _is_wecom_ua():
        return redirect(url_for('login'))
    if not (WECOM_CORPID and WECOM_AGENTID and WECOM_DOMAIN):
        flash('企业微信未配置（请设置 HYTB_WECOM_CORPID / AGENTID / DOMAIN 环境变量）')
        return redirect(url_for('login'))
    callback = WECOM_DOMAIN.rstrip('/') + url_for('wecom.callback')
    return redirect(build_oauth_url(callback))


@bp.route('/wecom/callback')
def callback():
    """OAuth 回调：code 换 userid -> 查 User -> 写 session 自动登录"""
    code = request.args.get('code', '')
    if not code:
        flash('企业微信授权失败（缺少 code）')
        return redirect(url_for('login'))
    token = get_access_token()
    if not token:
        flash('企业微信配置错误或接口不可用')
        return redirect(url_for('login'))
    info = _http_get('https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo?access_token=%s&code=%s' % (token, code))
    userid = info.get('UserId') or info.get('userid')
    if not userid:
        flash('企业微信授权失败：%s' % info.get('errmsg', '未知错误'))
        return redirect(url_for('login'))
    user = User.query.filter_by(wecom_userid=userid).first()
    if not user:
        flash('该企业微信账号（%s）尚未绑定系统账号，请联系管理员在「IT 用户管理」绑定 userid' % userid)
        return redirect(url_for('login'))
    # 写 session，与 login() 完全一致 -> login_required / permission_required 权限流零改动
    session['user_id'] = user.id
    session['username'] = user.username
    session['level_code'] = user.level.code if user.level else ''
    session['level_name'] = user.level.name if user.level else ''
    log_audit('login_wecom', module='auth', detail='企业微信免密登录 userid=%s' % userid)
    return redirect(url_for('system_detail', key='oa'))


@bp.route('/wecom/jssdk_config')
@login_required
def jssdk_config():
    """前端 JS-SDK 初始化拿签名（必须已登录，企微内已登录）。"""
    url = request.args.get('url') or (request.url_root.rstrip('/') + request.path)
    cfg = jssdk_sign(url)
    if not cfg:
        return jsonify({'errcode': -1, 'errmsg': '企微未配置或获取 ticket 失败'}), 500
    return jsonify(cfg)


@bp.route('/wecom/diag')
@login_required
def diag():
    """企微配置自检页（联调排错用）。
    用法：先用密码登录系统，再在浏览器打开 http://127.0.0.1:5002/wecom/diag
    它会告诉你：凭证读到没有、能不能换到 access_token、回调地址长什么样。
    secret 只显示掩码，不会泄露。"""
    def mask(s, keep=4):
        if not s:
            return '(未配置)'
        if len(s) <= keep * 2:
            return s[:2] + '**'
        return s[:keep] + '*' * 6 + s[-keep:]

    token = get_access_token()
    ticket = get_jsapi_ticket(token) if token else ''
    return jsonify({
        'corpid': mask(WECOM_CORPID),
        'agentid': WECOM_AGENTID or '(未配置)',
        'secret': mask(WECOM_SECRET, 3),
        'domain': WECOM_DOMAIN or '(未配置)',
        'oauth_callback': (WECOM_DOMAIN.rstrip('/') + '/wecom/callback') if WECOM_DOMAIN else '(未配置 DOMAIN)',
        'access_token_ok': bool(token),
        'jsapi_ticket_ok': bool(ticket),
        'hint': 'access_token_ok=false 时：核对 CORPID/SECRET；若返回 60020，说明本机出口IP 没加进企微「企业可信IP」。',
    })
