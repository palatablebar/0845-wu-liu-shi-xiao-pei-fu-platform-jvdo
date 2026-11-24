"""物流时效赔付台应用包。

导入时安装命名兼容兜底（见 :mod:`app._compat`），不影响任何真实模块。
"""

try:  # pragma: no cover - 安装逻辑本身很简单
    from app._compat import install as _install_compat

    _install_compat()
except Exception:  # 兼容层绝不能影响应用正常启动
    pass
