# run.py —— 系统启动入口（务必用本文件启动，不要直接运行 app.py）
#
# 为什么需要这个文件？
# app.py 末尾要注册各个业务模块（oa/hr/admin/commission），
# 而 oa.py 等模块又需要从 app.py 里取 login_required 装饰器，两者互相引用。
# 如果直接运行 app.py，Python 会把 app.py 当成 __main__，
# 导致 oa.py 再次去导入 app.py，形成循环，启动即报 ImportError。
# 用本文件启动：先正常加载 app 模块，再启动服务，即可绕开该问题。
from app import app

if __name__ == '__main__':
    print('=' * 50)
    print('  海盈通保OA管理系统 已启动')
    print('  访问地址：http://127.0.0.1:5002')
    print('  默认账号：admin / 123456')
    print('=' * 50)
    # 生产环境必须 debug=False：公网暴露下 Werkzeug 调试器有被远程利用的风险
    app.run(debug=False, port=5002)
