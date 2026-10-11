# 13 前端基础、Python 与现场编码

不要求一个月补成所有领域的高级工程师。目标是能独立写当前项目需要的小模块，
能解释 Vue 经验、React 流式交互与后端语言基础，并通过常见现场题。

## 1. 闭卷基础清单

| 方向 | 本月必会 | 项目锚点与练习 |
|---|---|---|
| JS | 事件循环与微任务、闭包、this、原型、浅/深拷贝、Promise 错误传播 | 手写并发限制器，处理失败、空输入、结果顺序 |
| TS | union、类型收窄、泛型、unknown/any、判别联合、null 边界 | 用 status 判别 RunEvent；JSON parse 后仍需运行时校验 |
| Vue | ref/reactive、computed/watch、组件通信、生命周期、key、更新批处理 | 选真实 Vue 页面讲状态来源、刷新、竞态和性能测量 |
| React | state 函数式更新、effect cleanup、ref、闭包、稳定 key、渲染 | 解释 SSE 回调为何使用 current；卸载时如何停止 reader |
| 网络 | HTTP 状态、fetch/流、CORS、cookie/Bearer、SSE/WS、代理缓冲 | POST + Authorization 为什么选 fetch stream，不能直接用普通 EventSource |
| Python | dict/list/set、可变默认值、生成器、with、异常链、dataclass/Pydantic | 手写工具 registry、验证参数、统一 error result |
| asyncio | task/gather、并发限制、timeout/cancel、同步阻塞 | 并发 3 个 fake 调用，最大 2；取消后释放 slot |
| SQL | JOIN、索引、事务、唯一约束、分页、对象归属 | 查某用户消息；(tenant,user,request) 唯一键如何去重 |
| Node.js | 事件循环、stream/backpressure、子进程、ESM/CommonJS | 理解 spawn/exec 和 shell 风险；本月不再搭第二套后端 |
| 架构 | Adapter、Strategy、状态机、依赖注入、组合 | 解释 VectorStore 协议、registry、LLM fake；给出过度抽象的代价 |

### LLM 原理最小补课

理解 Transformer 的 token embedding、attention、位置编码、前馈层、生成概率；不要求手写训练框架。
讲清 prefill 处理输入、decode 自回归生成、KV cache 的内存/速度关系、采样与上下文。
面试题：「首 token 慢和输出 token 慢分别可能是什么原因？」结合网络、检索、输入长度、模型排队、decode 分析。

## 2. 前端现有实现的现场题

入口：[App.tsx](/Users/zhitong/Desktop/AI-Agent/ai-job-agent/frontend/src/App.tsx)、
[api.ts](/Users/zhitong/Desktop/AI-Agent/ai-job-agent/frontend/src/lib/api.ts)、
[sseParser.ts](/Users/zhitong/Desktop/AI-Agent/ai-job-agent/frontend/src/lib/sseParser.ts)。

1. 画 `submitChatOrAgent → apiFetch → reader.read → TextDecoder → buffer → parseSSE → applySseEvent`。
2. 手写 parser 支持分片、多行 data、注释心跳、CRLF；保留不完整事件，避免一个网络 chunk 当一个事件。
3. 现有 parser 按 `\n\n` 分块，只读 event/data，未完整支持 id/retry/CRLF；这是项目子集，不是完整 SSE 标准实现。
4. 快速切换会话时旧流如何避免写入新会话？使用 run/session 关联、generation 标识和 abort 清理，而非只一个 sending 布尔值。
5. 现有 120 秒 AbortController 是超时路径；UI「停止生成」和服务端取消是不同能力，需要实际接口配合。
6. 流关闭没有 done 时是否算成功？明确 terminal 状态、decoder flush、尾部残片处理，不静默展示完成。
7. 逐 token setState / Markdown 全量解析怎样测和改？先测 render/parse 耗时，按时间批量更新，仅处理活跃消息，必要时虚拟化历史。
8. token 放 localStorage 有何权衡？XSS 可以读取；解释替代的 HttpOnly cookie、CSRF 边界与后端支持需求，不只说「JWT 安全」。

可用 `sseParser.test.ts` 的既有用例作起点，增加 CRLF、UTF-8 跨字节、多个事件和异常终止场景。

## 3. 前端工程化与性能的最低证据

- 构建：讲 package scripts、锁文件、TS check、Vite 打包、CI；tree shaking 依赖 ESM/side effects，不是压缩的同义词。
- 性能：选择一个真实 Vue 经历或当前聊天页，记录优化前后同环境指标；不要背 useMemo 就称性能优化。
- 观测：首屏可用时间、长任务、渲染次数、长聊天内存、流式首 token；前端感知与服务端 TTFT 定义分开。
- 设计：组件边界、事件类型、请求状态机、错误展示；能解释 App 单文件继续增长后的拆分理由，不为面试重写整站。

## 4. 30 天算法题型分配

每天 1 小时：10 分钟回忆，30 分钟独立编码，10 分钟验证/复杂度，10 分钟记录。
优先 Python 主语言，JS 复练与前端相关题；避免两门语言重复刷全部题。

| 日程 | 题型 | 代表题 / 能力 | 数量目标 |
|---|---|---|---|
| D1–D4 | 数组、哈希、双指针 | 两数之和、有效字母异位词、合并有序数组、三数之和 | 4 |
| D5–D7 | 滑动窗口、栈 | 无重复最长子串、最小覆盖子串、有效括号 | 3 |
| D8–D10 | 二分、区间 | 搜索插入/旋转数组、合并区间 | 3 |
| D11–D13 | 链表、LRU | 反转链表、环检测、LRU 的哈希+双向链表 | 3 |
| D14–D17 | 树、BFS/DFS | 层序、深度、岛屿数量、最小深度 | 4 |
| D18–D20 | 图、堆 | 课程表拓扑排序、TopK、合并 K 有序列表 | 3 |
| D21–D24 | 基础 DP、回溯 | 爬楼梯、打家劫舍、零钱兑换、全排列 | 4 |
| D25–D30 | 复做和工程题 | 重做错题、并发限制器、SSE、幂等状态迁移 | 1–6，新题从简 |

25–30 道是训练样本，不是录用保证。不会的题次日重做、D+3/D+7 再做；
卡住 20 分钟可看一个提示，当天不能标「掌握」，次日必须闭卷复现。

## 5. Agent 工程现场编码题

| 限时 | 题目 | 必须覆盖 |
|---|---|---|
| 30 min | 最小工具循环（fake LLM） | 多 tool_calls、ID 对齐、未知工具、schema 错、max_steps |
| 20 min | async concurrency limiter | 非零容量、结果顺序、异常、取消、slot 清理 |
| 25 min | SSE 增量 parser | 分片、多行、CRLF、心跳、尾部不完整事件 |
| 20 min | 任务状态机 | 合法转移、终态不可重跑、版本 CAS、重复请求 |
| 20 min | 请求重试器 | 可重试错误、退避、deadline、禁止副作用盲重试 |
| 15 min | Recall@K / MRR | 空数据、重复 IDs、分母约定 |
| 25 min | 安全路径解析 | 相对路径、越界、symlink、禁止文件、输出限制 |
| 20 min | tenant/user SQL 查询 | 服务端 identity、参数化、索引、越权拒绝 |

每次讲输入/输出、数据结构、复杂度、异常、测试。主观感受「懂了」不算验收。

## 6. 7 年经历怎么准备

选择 2 个真实 Vue 项目，分别写：业务约束、个人职责、遇到的问题、方案比较、上线验证、遗留限制。
没有性能专项就讲复杂状态/稳定性/交付协调的真实案例，不虚构性能数字。
架构经验少可以具体解释「当时为什么这样切组件和接口，现在哪些地方会改」，不硬套专家级身份。
