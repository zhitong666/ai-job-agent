"""D5 算法：有效括号。详细要求见 review/20。

实现 is_valid_parentheses；空字符串返回 True，非括号字符返回 False。
不得修改自检样本；先写栈不变式，再实现代码。
"""


def is_valid_parentheses(s: str) -> bool:
    stack = []
    pairs = {
        ")": "(",
        "]": "[",
        "}": "{",
    }
    opening = "([{"
    closing = ")]}"

    for char in s:
        if char in opening:
            stack.append(char)
        elif char in closing:
            if not stack or stack[-1] != pairs[char]:
                return False
            stack.pop()
        else:
            return False

    return not stack


def _self_check() -> None:
    cases = [
        ("", True),
        ("()", True),
        ("()[]{}", True),
        ("{[]}", True),
        ("([{}])", True),
        ("(]", False),
        ("([)]", False),
        ("(((", False),
        ("))", False),
        ("{[}]", False),
        ("(()())", True),
        ("a", False),
        ("( )", False),
    ]
    for value, expected in cases:
        actual = is_valid_parentheses(value)
        assert type(actual) is bool, (value, "必须返回 bool", actual)
        assert actual == expected, (value, expected, actual)
    print(f"D5 valid parentheses: {len(cases)} cases passed")


if __name__ == "__main__":
    _self_check()
