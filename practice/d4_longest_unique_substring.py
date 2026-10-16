"""D4 算法：最长无重复字符子串。题目、阅读提示与验收见 review/19。

只实现 length_of_longest_substring，不改自检数据。
返回长度，字符串按 Python 的字符迭代，空格和中文也算字符。
"""


def length_of_longest_substring(s: str) -> int:
    # 第一版解法
    # s_list = list(s)

    # if len(s_list) == 0 or len(s_list) == 1:
    #     return len(s_list)
    # no_repeat_list = []
    # max_len = 0
    # for i in range(len(s_list)):
    #     if s_list[i] not in no_repeat_list:
    #         no_repeat_list.append(s_list[i])
    #     else:
    #         max_len = max(max_len, len(no_repeat_list))
    #         no_repeat_list = no_repeat_list[no_repeat_list.index(s_list[i]) + 1:]
    #         no_repeat_list.append(s_list[i])
    # max_len = max(max_len, len(no_repeat_list))
    # return max_len

    # 第二版解法
    last = {} # 字符 -> 上次出现下标
    left = 0 # 当前窗口左边界
    best = 0
    for right, ch in enumerate(s):
        # 如果 ch 在窗口内出现过，左边界跳到它上次出现位置的右边
        if ch in last and last[ch] >= left:
            left = last[ch] + 1
        last[ch] = right
        best = max(best, right - left + 1)
    return best

    # raise NotImplementedError("请先写窗口不变式，再实现滑动窗口")


def _self_check() -> None:
    cases = [
        ("", 0),
        ("abcabcbb", 3),
        ("bbbbb", 1),
        ("pwwkew", 3),
        ("abba", 2),
        ("dvdf", 3),
        (" ", 1),
        ("你好你", 2),
        ("abcdef", 6),
        ("tmmzuxt", 5),
    ]
    for value, expected in cases:
        actual = length_of_longest_substring(value)
        assert actual == expected, (value, expected, actual)
    print(f"D4 sliding window: {len(cases)} cases passed")


if __name__ == "__main__":
    _self_check()
