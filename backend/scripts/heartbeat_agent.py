"""
Nocturne Memory - Heartbeat Agent

后台自动整理记忆的 AI Agent。通过 MCP SSE 协议连接远程服务。
采用「代码预读 + AI 决策执行」架构，兼容轻量模型。

架构说明:
    1. 代码层自动预读所有记忆节点（零 AI 消耗）
    2. 完整数据注入 AI 上下文（AI 无需调用 read_memory）
    3. AI 只负责：分析问题 → 调用工具修复

使用方法:
    python heartbeat_agent.py              # 运行一次
    python heartbeat_agent.py --schedule   # 持续运行（每小时一次）
    python heartbeat_agent.py --dry-run    # 只诊断不修改（预览模式）

环境变量:
    HEARTBEAT_MCP_URL: MCP SSE 服务地址 
    HEARTBEAT_API_KEY: AI 服务的 API Key
    HEARTBEAT_API_BASE: AI API 基础地址 (默认: https://api.openai.com/v1)
    HEARTBEAT_MODEL: 使用的模型 (默认: gpt-4o-mini)
"""

import os
import sys
import json
import re
import asyncio
import argparse
import logging
from datetime import datetime
from typing import Dict, List, Optional

# 设置路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
PROJECT_ROOT = os.path.dirname(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


# =============================================================================
# MCP 客户端
# =============================================================================

class MCPClient:
    """MCP SSE 客户端 - 使用官方 mcp 包"""

    def __init__(self, url: str):
        self.url = url
        self.session = None
        self._sse_cm = None
        self._session_cm = None

    async def connect(self):
        """连接到 MCP 服务器"""
        from mcp.client.sse import sse_client
        from mcp import ClientSession

        logger.info(f"连接 MCP: {self.url}")

        self._sse_cm = sse_client(self.url)
        read, write = await self._sse_cm.__aenter__()

        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()

        result = await self.session.initialize()
        logger.info(f"已连接: {result.serverInfo.name} v{result.serverInfo.version}")

    async def close(self):
        """关闭连接"""
        try:
            if self._session_cm:
                await self._session_cm.__aexit__(None, None, None)
            if self._sse_cm:
                await self._sse_cm.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"关闭连接时出错: {e}")

    async def list_tools(self) -> List[str]:
        """列出可用工具名称"""
        result = await self.session.list_tools()
        return [t.name for t in result.tools]

    async def list_tools_raw(self) -> List[Dict]:
        """获取原始工具列表（包含完整定义）"""
        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, name: str, args: Dict) -> str:
        """调用工具"""
        result = await self.session.call_tool(name, args)
        if result.content:
            text_parts = []
            for item in result.content:
                if hasattr(item, 'text'):
                    text_parts.append(item.text)
                else:
                    text_parts.append(str(item))
            return "\n".join(text_parts)
        return str(result)


# =============================================================================
# AI 客户端
# =============================================================================

class AIClient:
    """AI API 客户端"""

    def __init__(self):
        self.api_key = os.getenv("HEARTBEAT_API_KEY")
        self.base_url = os.getenv("HEARTBEAT_API_BASE", "https://api.openai.com/v1")
        self.model = os.getenv("HEARTBEAT_MODEL", "gpt-4o-mini")

        if not self.api_key:
            raise ValueError("未配置 HEARTBEAT_API_KEY")

    async def chat(self, messages: List[Dict], tools: List[Dict] = None,
                   temperature: float = 0.4) -> Dict:
        """调用 AI API"""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 8000,
            "temperature": temperature,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            )
            response.raise_for_status()
            raw = response.json()

            # 适配智谱等 API
            if "data" in raw and "choices" not in raw:
                return raw["data"]
            return raw


# =============================================================================
# 工具转换函数
# =============================================================================

def _convert_mcp_to_openai_tools(mcp_tools: list) -> list:
    """
    将 MCP 工具格式转换为 OpenAI tools 格式

    MCP 格式示例:
        Tool(name='read_memory', description='...', inputSchema={...})

    OpenAI 格式示例:
        {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    openai_tools = []

    for tool in mcp_tools:
        # MCP tool 可能是对象或字典
        if hasattr(tool, 'name'):
            # 对象形式
            name = tool.name
            description = tool.description or ""
            parameters = tool.inputSchema or {}
        else:
            # 字典形式
            name = tool.get("name", "")
            description = tool.get("description", "")
            parameters = tool.get("inputSchema", {})

        openai_tool = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        }
        openai_tools.append(openai_tool)

    return openai_tools


# =============================================================================
# 提示词体系
# =============================================================================

SYSTEM_PROMPT = """\
你是 Nocturne Memory 系统的记忆管家。你的工作：分析记忆库的问题，然后**调用工具修复**。

当前时间：{time}

# 核心概念

- URI 是记忆的路径（地址），同一内容可有多个路径（alias）
- priority: 数字越小越优先（0=核心身份，1=关键，2+=一般），绑定在路径上
- disclosure: "什么时候该想起这件事"的触发条件，绑定在路径上
- 域: core://=身份与配置, work://=工作与任务, notes://=知识与笔记

# 路径设计原则

1. **层级 > 平铺**: 同类节点应有共同父路径，不要全堆在根目录
   - ❌ work://todo_email, work://todo_report（平铺）
   - ✅ work://todos/email, work://todos/report（分组）

2. **URI 自解释**: 看路径就能猜到内容
   - ❌ core://user_duplicate
   - ✅ core://agent/my_user

3. **域归属正确**: 内容放在语义对的域里

4. **不复制，用 alias**: 同一知识需要多角度找到时，用 add_alias 建多入口

# 你必须修复的问题类型

| 优先级 | 问题类型 | 怎么修 |
|-------|---------|-------|
| 🔴 P0 | 内容重复（不同 Memory ID，内容相似） | 合并内容到一个 → delete_memory 删另一个 |
| 🔴 P0 | 垃圾/测试数据混入正式节点 | delete_memory |
| 🟠 P1 | 过期待办未标记 | update_memory 标记过期 |
| 🟡 P2 | 模糊 disclosure（"重要""记住""需要""TODO""临时"） | update_memory 改为具体触发条件 |
| 🟡 P2 | URI 命名不清晰 | add_alias 建好名字 → delete_memory 删旧名 |
| 🟢 P3 | 结构扁平（同域根目录>5个平级节点） | 建分组父节点，迁移子节点 |
| 🟢 P3 | 域归属错误 | add_alias 到正确域 → delete_memory 删错误路径 |
| 🔵 P4 | priority 不合理 | update_memory 调整 |

# 操作安全

- **迁移/重命名 = add_alias 先 + delete_memory 后**，永远不要反过来
- **建层级路径时先建父节点**: add_alias 到 `work://todos/xxx` 前，必须先 `create_memory(parent_uri="work://", title="todos", ...)` 创建 `work://todos`
- 合并前必须确认不丢失独有信息
- 不确定的问题跳过，在报告中标注"需人工确认"

# 增量巡检模式

如果上下文中包含「上次整理记录」，说明这不是首次巡检。你应该：
1. **优先处理**：上次报告中标记为「待人工处理」的遗留问题
2. **重点关注**：上次巡检后**新增或被修改**的节点（对比上次的节点快照）
3. **快速抽查**：上次标记为「健康」的老节点只做抽查，无变化则跳过
4. **路径拓扑**：仍需审视整体结构（可能有新节点改变了拓扑）

如果没有「上次整理记录」或标记为首次，执行全量检查。

# ⚠️ 关键要求

- 你**必须调用工具**来修复问题。只输出文字报告而不调用工具 = 任务失败。
- 每发现一个问题，立即调用对应工具修复，不要攒到最后。
- 修改内容前如需确认，可以先 read_memory 再改。"""


USER_PROMPT_TEMPLATE = """\
开始例行巡检。当前时间：{time}，日期：{date}。

以下是系统已预读的**全部记忆节点数据**，你不需要再调用 read_memory 去读取，直接分析即可。
{last_report_section}
---
# 全局索引

{index_data}

---
# 最近修改

{recent_data}

---
# 所有节点完整内容

{all_nodes_data}

---

# 你的任务
{task_instruction}
## 修复操作参考
- 修 disclosure → update_memory(uri, disclosure="...")
- 标记过期 → update_memory(uri, old_string="...", new_string="...")
- 合并重复 → update_memory 合并内容 + delete_memory 删重复
- 迁移路径 → add_alias 建新路径 + delete_memory 删旧路径
- 清理垃圾 → delete_memory

## 最后：输出报告
列出你做了什么修复操作，以及仍需人工处理的问题。

⚠️ 你必须调用工具！只输出分析而不调用工具 = 失败。现在开始。"""


# 首次巡检任务指令
TASK_INSTRUCTION_FULL = """
**模式：首次全量巡检**

请逐个节点分析，找出所有问题并**立即调用工具修复**：
- disclosure 是否模糊？（"重要""记住""需要""TODO""临时"都是坏的）
- 有没有过期的待办事项？
- 有没有重复内容？（不同 Memory ID 但内容相似）
- 有没有测试/垃圾数据？
- URI 命名是否清晰？域归属是否正确？
- 整体结构是否扁平化需要分组？
"""

# 增量巡检任务指令
TASK_INSTRUCTION_INCREMENTAL = """
**模式：增量巡检**（上次巡检: {last_time}）

请按以下优先级工作：

**1. 处理上次遗留问题（最高优先级）**
上次报告中标记为「待人工处理」的问题，如果你现在能判断，立即修复。

**2. 检查新增/修改的节点**
对比上次节点快照，重点检查上次巡检后发生变化的节点。

**3. 抽查老节点**
上次标记为「健康」的节点做快速抽查，有变化才深入。

**4. 审视路径拓扑**
新节点可能改变了整体结构，检查是否需要路径调整。

对每个发现的问题立即调用工具修复！
"""


# =============================================================================
# 心跳 Agent
# =============================================================================

class HeartbeatAgent:
    """心跳整理 Agent - 代码预读 + AI 决策执行 + 增量巡检"""

    # 巡检记录存储的固定 URI
    REPORT_URI = "core://heartbeat_agent/last_report"
    REPORT_PARENT = "core://heartbeat_agent"

    def __init__(self, mcp_url: str, dry_run: bool = False):
        self.mcp_url = mcp_url
        self.dry_run = dry_run
        self.mcp: Optional[MCPClient] = None
        self.ai = AIClient()
        self.operations = []
        self.ai_summary = ""  # AI 最终输出的文字报告
        self.tools: list = []  # 动态获取的 OpenAI 格式工具列表

    async def run(self):
        """执行整理"""
        logger.info("=" * 60)
        logger.info(f"心跳整理开始 {'[预览模式]' if self.dry_run else '[执行模式]'}")
        logger.info(f"时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
        logger.info(f"模型: {self.ai.model}")
        logger.info("=" * 60)

        self.operations = []

        try:
            # 1. 连接 MCP
            self.mcp = MCPClient(self.mcp_url)
            await self.mcp.connect()

            # 获取并转换工具列表
            mcp_tools = await self.mcp.list_tools_raw()
            self.tools = _convert_mcp_to_openai_tools(mcp_tools)
            tool_names = [t.get("function", {}).get("name", "?") for t in self.tools]
            logger.info(f"可用 MCP 工具 ({len(self.tools)} 个): {', '.join(tool_names)}")

            # 2. 预读所有记忆节点
            logger.info("\n📦 开始预读全部记忆节点...")
            inventory = await self._prefetch_inventory()
            logger.info(f"📦 预读完成: {inventory['node_count']} 个节点, "
                        f"{inventory['total_chars']} 字符")

            # 3. 构建 AI 对话（已包含全部数据 + 上次报告）
            now = datetime.now()
            last_report = inventory.get("last_report")
            is_incremental = last_report is not None

            # 根据是否有上次报告，选择不同的提示策略
            if is_incremental:
                last_report_section = (
                    "\n---\n"
                    "# ⚡ 上次整理记录（增量模式激活）\n\n"
                    f"```\n{last_report}\n```\n"
                )
                # 从上次报告中提取时间
                time_match = re.search(r'巡检时间:\s*(.+)', last_report)
                last_time = time_match.group(1).strip() if time_match else "未知"
                task_instruction = TASK_INSTRUCTION_INCREMENTAL.format(
                    last_time=last_time
                )
                logger.info(f"📊 增量模式: 上次巡检于 {last_time}")
            else:
                last_report_section = ""
                task_instruction = TASK_INSTRUCTION_FULL
                logger.info("📊 首次全量巡检模式")

            conversation = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        time=now.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                },
                {
                    "role": "user",
                    "content": USER_PROMPT_TEMPLATE.format(
                        time=now.strftime("%Y-%m-%d %H:%M:%S"),
                        date=now.strftime("%Y年%m月%d日"),
                        index_data=inventory["index"],
                        recent_data=inventory["recent"],
                        all_nodes_data=inventory["all_nodes_text"],
                        last_report_section=last_report_section,
                        task_instruction=task_instruction,
                    )
                }
            ]

            # 4. AI 对话循环（专注于分析 + 修复）
            max_rounds = 30
            no_tool_rounds = 0  # 连续无工具调用的轮次

            for round_num in range(max_rounds):
                logger.info(f"\n--- 第 {round_num + 1}/{max_rounds} 轮 ---")

                resp = await self.ai.chat(conversation, self.tools, temperature=0.4)
                choices = resp.get("choices", [])
                if not choices:
                    logger.warning("AI 无响应")
                    break

                msg = choices[0].get("message", {})
                content = msg.get("content", "") or ""
                tool_calls = msg.get("tool_calls", [])

                # 输出 AI 思考
                if content:
                    for line in content.split("\n"):
                        if line.strip():
                            logger.info(f"  💭 {line.strip()}")

                # 无工具调用 = 完成或偷懒
                if not tool_calls:
                    no_tool_rounds += 1

                    # 第一次无工具调用：检查是否真的做了修复
                    if no_tool_rounds == 1 and not self.operations:
                        # AI 没调用任何工具就想结束 → 踢一脚
                        logger.warning("⚠️ AI 未调用任何工具，发送催促...")
                        conversation.append({"role": "assistant", "content": content})
                        conversation.append({
                            "role": "user",
                            "content": (
                                "你还没有调用任何工具！请重新审视上面的数据。"
                                "我看到以下明显问题：\n"
                                "1. 有节点 disclosure 是\"重要\"\"记住\"\"需要\"\"TODO\"\"临时\"，这些都需要用 update_memory 修复\n"
                                "2. 可能存在重复内容的节点需要合并\n"
                                "3. 路径结构可能需要优化\n"
                                "请立即开始调用工具修复！从修复 disclosure 开始。"
                            )
                        })
                        continue
                    else:
                        # 已经做了修复，或第二次空转，正常结束
                        logger.info("AI 完成整理")
                        if content:
                            self.ai_summary = content
                            logger.info(f"\n{'='*40} 最终报告 {'='*40}")
                            logger.info(content)
                        break
                else:
                    no_tool_rounds = 0

                # 构建 assistant 消息
                assistant_msg = {"role": "assistant"}
                if content:
                    assistant_msg["content"] = content
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                conversation.append(assistant_msg)

                # 执行工具调用
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    name = tc["function"]["name"]

                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        logger.error(f"  ❌ 参数解析失败: {e}")
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"错误：JSON 解析失败 - {e}"
                        })
                        continue

                    args_str = json.dumps(args, ensure_ascii=False)
                    logger.info(f"  🔧 {name}({args_str[:100]}{'...' if len(args_str) > 100 else ''})")

                    # 预览模式下拦截写操作
                    write_ops = ("update_memory", "create_memory",
                                 "delete_memory", "add_alias")
                    if self.dry_run and name in write_ops:
                        result = f"[预览模式] 操作已记录但未执行: {name}({args_str[:120]})"
                        logger.info(f"    ⏸️  预览拦截")
                        self.operations.append({
                            "action": name, "args": args, "status": "dry_run",
                        })
                    else:
                        try:
                            result = await self.mcp.call_tool(name, args)
                        except Exception as e:
                            result = f"工具调用失败: {e}"
                            logger.error(f"    ❌ {result}")

                        if name in write_ops:
                            success = "success" in result.lower()
                            self.operations.append({
                                "action": name, "args": args,
                                "status": "success" if success else "failed",
                                "result_snippet": result[:200],
                            })
                            logger.info(f"    {'✅ 成功' if success else '⚠️ ' + result[:100]}")
                        else:
                            logger.info(f"    📖 返回 {len(result)} 字符")

                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result
                    })

                # 上下文溢出保护
                total_chars = sum(
                    len(json.dumps(m, ensure_ascii=False)) for m in conversation
                )
                if total_chars > 100_000:
                    logger.warning(f"上下文已达 {total_chars} 字符，提前结束")
                    conversation.append({
                        "role": "user",
                        "content": "上下文即将溢出，请输出最终报告并结束。"
                    })
            else:
                logger.warning(f"已达最大轮次 {max_rounds}，强制结束")

            # 5. 保存巡检记录到记忆系统（仅执行模式）
            if not self.dry_run:
                await self._save_report(inventory.get("uris", []))
            else:
                logger.info("📝 预览模式，跳过保存巡检记录")

            await self.mcp.close()
            self._print_summary()

        except Exception as e:
            logger.error(f"整理失败: {e}", exc_info=True)
            if self.mcp:
                await self.mcp.close()

    async def _prefetch_inventory(self) -> Dict:
        """
        代码层预读全部记忆节点 + 上次巡检记录。
        返回包含所有节点内容的字典，供注入 AI 上下文。
        """
        # 读取上次巡检记录
        last_report = None
        try:
            report_raw = await self.mcp.call_tool(
                "read_memory", {"uri": self.REPORT_URI}
            )
            if "not found" not in report_raw.lower():
                last_report = report_raw
                logger.info(f"  📋 上次巡检记录: {len(report_raw)} 字符")
            else:
                logger.info("  📋 无上次巡检记录（首次运行）")
        except Exception as e:
            logger.info(f"  📋 无上次巡检记录: {e}")

        # 读取索引
        index_data = await self.mcp.call_tool("read_memory", {"uri": "system://index"})
        logger.info(f"  📋 索引: {len(index_data)} 字符")

        # 读取最近修改
        recent_data = await self.mcp.call_tool(
            "read_memory", {"uri": "system://recent/20"}
        )
        logger.info(f"  📋 最近修改: {len(recent_data)} 字符")

        # 从索引中提取所有 URI
        uris = re.findall(r'(?:core|work|notes)://\S+', index_data)
        # 清理 URI（去掉可能的尾部标点）
        clean_uris = []
        for uri in uris:
            uri = uri.rstrip('.,;:)]}>!?')
            if uri not in clean_uris:
                clean_uris.append(uri)

        logger.info(f"  📋 发现 {len(clean_uris)} 个 URI: {clean_uris}")

        # 逐个读取
        nodes = {}
        for uri in clean_uris:
            try:
                content = await self.mcp.call_tool("read_memory", {"uri": uri})
                nodes[uri] = content
                logger.info(f"  📖 {uri}: {len(content)} 字符")
            except Exception as e:
                logger.warning(f"  ⚠️ {uri} 读取失败: {e}")
                nodes[uri] = f"[读取失败: {e}]"

        # 拼接所有节点内容
        all_nodes_parts = []
        for uri, content in nodes.items():
            all_nodes_parts.append(f"### {uri}\n```\n{content}\n```\n")
        all_nodes_text = "\n".join(all_nodes_parts)

        total_chars = len(index_data) + len(recent_data) + len(all_nodes_text)

        return {
            "index": index_data,
            "recent": recent_data,
            "nodes": nodes,
            "uris": clean_uris,
            "all_nodes_text": all_nodes_text,
            "node_count": len(clean_uris),
            "total_chars": total_chars,
            "last_report": last_report,
        }

    def _build_report(self, all_uris: List[str]) -> str:
        """构建结构化巡检记录，用于存入记忆系统"""
        now = datetime.now()
        success_ops = [op for op in self.operations if op["status"] == "success"]
        failed_ops = [op for op in self.operations if op["status"] == "failed"]

        lines = [
            f"# 心跳巡检记录",
            f"",
            f"## 基本信息",
            f"- 巡检时间: {now.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 使用模型: {self.ai.model}",
            f"- 扫描节点数: {len(all_uris)}",
            f"- 成功操作数: {len(success_ops)}",
            f"- 失败操作数: {len(failed_ops)}",
            f"",
        ]

        # 执行的操作
        if success_ops:
            lines.append("## 已执行的修复操作")
            for i, op in enumerate(success_ops, 1):
                uri = op["args"].get("uri", op["args"].get("parent_uri", "?"))
                # 生成可读的操作描述
                desc = self._describe_operation(op)
                lines.append(f"{i}. [{op['action']}] {uri}: {desc}")
            lines.append("")

        if failed_ops:
            lines.append("## 失败的操作")
            for i, op in enumerate(failed_ops, 1):
                uri = op["args"].get("uri", op["args"].get("parent_uri", "?"))
                snippet = op.get("result_snippet", "未知错误")
                lines.append(f"{i}. [{op['action']}] {uri}: {snippet[:100]}")
            lines.append("")

        # AI 的文字总结中提取「待人工处理」部分
        if self.ai_summary:
            # 尝试提取 AI 报告中的遗留问题
            deferred = self._extract_deferred_issues(self.ai_summary)
            if deferred:
                lines.append("## 待人工处理")
                for item in deferred:
                    lines.append(f"- {item}")
                lines.append("")

        # 节点快照（供下次增量对比）
        lines.append("## 节点快照")
        lines.append("（下次巡检时用于对比变化）")
        for uri in sorted(all_uris):
            lines.append(f"- {uri}")
        lines.append("")

        return "\n".join(lines)

    def _describe_operation(self, op: Dict) -> str:
        """生成操作的可读描述"""
        action = op["action"]
        args = op["args"]

        if action == "update_memory":
            parts = []
            if args.get("disclosure"):
                parts.append(f"disclosure → \"{args['disclosure']}\"")
            if args.get("priority") is not None:
                parts.append(f"priority → {args['priority']}")
            if args.get("old_string"):
                old = args["old_string"][:30]
                parts.append(f"内容替换: \"{old}...\"")
            if args.get("append"):
                parts.append("追加内容")
            return "; ".join(parts) if parts else "更新"
        elif action == "delete_memory":
            return "删除节点"
        elif action == "add_alias":
            return f"建别名 → {args.get('new_uri', '?')}"
        elif action == "create_memory":
            title = args.get("title", "?")
            return f"新建节点 title={title}"
        return action

    def _extract_deferred_issues(self, summary: str) -> List[str]:
        """从 AI 报告文字中提取待人工处理的项目"""
        deferred = []
        in_deferred_section = False

        for line in summary.split("\n"):
            line_lower = line.lower().strip()

            # 检测「待人工」「需人工」「未修复」等段落标题
            if any(kw in line_lower for kw in
                   ["待人工", "需人工", "未修复", "需确认", "无法判断"]):
                in_deferred_section = True
                continue

            # 检测新的 ## 段落标题（结束 deferred section）
            if in_deferred_section and line.strip().startswith("#"):
                in_deferred_section = False
                continue

            # 收集列表项
            if in_deferred_section and line.strip().startswith(("-", "*", "+")):
                item = line.strip().lstrip("-*+ ").strip()
                if item:
                    deferred.append(item)
            elif in_deferred_section and re.match(r'\d+\.', line.strip()):
                item = re.sub(r'^\d+\.\s*', '', line.strip())
                if item:
                    deferred.append(item)

        return deferred

    async def _save_report(self, all_uris: List[str]):
        """保存巡检记录到记忆系统"""
        report_content = self._build_report(all_uris)

        logger.info("\n📝 保存巡检记录...")

        try:
            # 尝试读取已有记录
            existing = await self.mcp.call_tool(
                "read_memory", {"uri": self.REPORT_URI}
            )
            node_exists = "not found" not in existing.lower()
        except Exception:
            node_exists = False

        try:
            if node_exists:
                # 已有记录 → 删除旧的，创建新的（因为内容是全量替换）
                await self.mcp.call_tool(
                    "delete_memory", {"uri": self.REPORT_URI}
                )
                logger.info("  🗑️ 删除旧记录")

            # 创建新记录
            await self.mcp.call_tool("create_memory", {
                "parent_uri": self.REPORT_PARENT,
                "content": report_content,
                "priority": 2,
                "title": "last_report",
                "disclosure": "当心跳整理Agent开始新一轮巡检时读取",
            })
            logger.info(f"  ✅ 巡检记录已保存到 {self.REPORT_URI}")
        except Exception as e:
            logger.error(f"  ❌ 保存巡检记录失败: {e}")

    def _print_summary(self):
        """输出操作统计"""
        logger.info("\n" + "=" * 60)
        logger.info("操作统计")
        logger.info("=" * 60)

        if not self.operations:
            logger.info("  本次无写操作")
            return

        success = [op for op in self.operations if op["status"] == "success"]
        failed = [op for op in self.operations if op["status"] == "failed"]
        dry_run = [op for op in self.operations if op["status"] == "dry_run"]

        logger.info(f"  成功: {len(success)}  失败: {len(failed)}  拦截(预览): {len(dry_run)}")

        for op in self.operations:
            icon = {"success": "✅", "failed": "❌", "dry_run": "⏸️"}[op["status"]]
            uri = op["args"].get("uri", op["args"].get("parent_uri", "?"))
            logger.info(f"  {icon} {op['action']} → {uri}")

        logger.info("=" * 60)


# =============================================================================
# 主函数
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Nocturne Memory 心跳整理 Agent")
    parser.add_argument("--schedule", action="store_true", help="持续运行模式")
    parser.add_argument("--interval", type=int, default=60, help="运行间隔（分钟）")
    parser.add_argument("--url", default=None, help="MCP SSE 地址")
    parser.add_argument("--dry-run", action="store_true", help="预览模式（只诊断不修改）")
    args = parser.parse_args()

    url = args.url or os.getenv("HEARTBEAT_MCP_URL")

    if args.schedule:
        logger.info(f"定时模式，间隔 {args.interval} 分钟")
        while True:
            try:
                await HeartbeatAgent(url, dry_run=args.dry_run).run()
            except Exception as e:
                logger.error(f"运行错误: {e}", exc_info=True)
            logger.info(f"等待 {args.interval} 分钟...")
            await asyncio.sleep(args.interval * 60)
    else:
        await HeartbeatAgent(url, dry_run=args.dry_run).run()


if __name__ == "__main__":
    asyncio.run(main())
