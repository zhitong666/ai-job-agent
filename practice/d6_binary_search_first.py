"""D6 二分：在有序整数列表中找 target 第一次出现的位置。

输入按升序排列，允许重复；不存在返回 -1；不得修改 nums。
只实现 binary_search_first；保留样本与 expected。详细要求见 review/21。
"""


def binary_search_first(nums: list[int], target: int) -> int:
    if not len(nums):
        return -1
    left, right = 0, len(nums)
    while left < right:
        mid = (left + right) // 2
        if nums[mid] < target:
            left = mid + 1
        else:
            right = mid

    if left >= len(nums) or nums[left] != target:
        return -1
    return left

def _self_check() -> None:
    cases = [
        ([], 1, -1),
        ([1], 1, 0),
        ([1], 0, -1),
        ([1], 2, -1),
        ([1, 3], 1, 0),
        ([1, 3], 3, 1),
        ([1, 3], 2, -1),
        ([1, 2, 2, 2, 3], 2, 1),
        ([2, 2, 2], 2, 0),
        ([1, 1, 2, 3, 3], 3, 3),
        ([-5, -3, -3, 0, 4], -3, 1),
        ([1, 3, 5, 7, 9], 6, -1),
        ([1, 3, 5, 7, 9], 0, -1),
        ([1, 3, 5, 7, 9], 10, -1),
    ]
    for nums, target, expected in cases:
        before = nums.copy()
        actual = binary_search_first(nums, target)
        assert type(actual) is int, (nums, target, "必须返回 int", actual)
        assert actual == expected, (nums, target, expected, actual)
        assert nums == before, (before, nums, "不得修改输入")
    print(f"D6 binary search first: {len(cases)} cases passed")


if __name__ == "__main__":
    _self_check()
