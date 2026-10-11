"""D2 双指针现场题：先独立写，再运行本文件核对。"""


def merge_sorted_array(nums1: list[int], m: int, nums2: list[int], n: int) -> None:
    """把 nums2 合并到 nums1，结果仍升序；原地修改，不返回新数组。

    nums1 长度为 m+n，前 m 个数有效；nums2 长度为 n，全部有效。
    目标：O(m+n) 时间、O(1) 额外空间。
    """
    # raise NotImplementedError("请先闭卷实现：考虑从数组尾部写入")
    i = m - 1 # nums1 有效部分的最后一个元素
    j = n - 1 # nums2 的最后一个元素
    k = m + n - 1 # 指向 nums1 当前可以写入的最后位置
    while j >= 0:
        if i >= 0 and nums1[i] > nums2[j]:
            nums1[k] = nums1[i]
            i -= 1
        else:
            nums1[k] = nums2[j]
            j -= 1
        k -= 1


def check() -> None:
    cases = [
        ([1, 2, 3, 0, 0, 0], 3, [2, 5, 6], 3, [1, 2, 2, 3, 5, 6]),
        ([1], 1, [], 0, [1]),
        ([0], 0, [1], 1, [1]),
        ([2, 0], 1, [1], 1, [1, 2]),
        ([-3, -1, 0, 0], 2, [-2, 4], 2, [-3, -2, -1, 4]),
    ]
    for nums1, m, nums2, n, expected in cases:
        result = merge_sorted_array(nums1, m, nums2, n)
        assert result is None, "题目要求原地修改并返回 None"
        assert nums1 == expected, (nums1, expected)
    print(f"{len(cases)} cases passed")


if __name__ == "__main__":
    check()
