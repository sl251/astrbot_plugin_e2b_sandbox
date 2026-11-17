"""通用轻量辅助函数。"""


def shlex_quote(value: str):
    """对传入远端 Shell 的参数做单引号安全转义。"""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"
