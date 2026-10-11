"""D1 现场编码：两数之和。先独立完成 two_sum，再运行本文件。"""


def two_sum(nums: list[int], target: int) -> list[int]:
    """返回两个不同下标；最多一个解，无解返回 []。"""
    # TODO: 在这里写你的解法。先保留自己的首次尝试，再改进。
    # for i in range(len(nums)):
    #     for j in range(i + 1, len(nums)):
    #         if nums[i] + nums[j] == target:
    #             return [i, j]
    # return []
    seen = {}  # 存储 {数值: 下标}
    for i, num in enumerate(nums):
        complement = target - num 
        if complement in seen:
            return [seen[complement], i]
        seen[num] = i
    return []


def _check(nums: list[int], target: int, expected: list[int]) -> None:
    result = two_sum(nums, target)
    assert result == expected, (nums, target, result, expected)


if __name__ == "__main__":
    _check([2, 7, 11, 15], 9, [0, 1])
    _check([3, 2, 4], 6, [1, 2])
    _check([3, 3], 6, [0, 1])
    _check([-2, 1, 9], 7, [0, 2])
    _check([1, 2, 3], 20, [])
    print("5 cases passed")
