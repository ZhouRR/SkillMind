"""汎用 Git Provider の契約検証に使う synthetic source。"""


def validate_user_name(user_name: str) -> bool:
    """空でない user name だけを有効として扱う。"""

    return bool(user_name.strip())
