# webui_test.py (或者你可以改名叫 main_app.py)
import asyncio
import datetime
import json
import logging
import os

import streamlit as st

# --- 小懒猫的温馨提示 ---
# (导入模块部分，与上一版基本一致)
try:
    from src.action.action_handler import ActionHandler
    from src.config.alcarus_configs import (
        AlcarusRootConfig,
        CoreLogicSettings,
        DatabaseSettings,
        InnerConfig,
        # ActionModuleSettings, # 如果你的 alcarus_configs.py 中有定义
        IntrusiveThoughtsSettings,
        LLMClientSettings,
        LoggingSettings,
        ModelParams,
        PersonaSettings,
        ProviderModels,
        ProvidersConfig,
        ProviderSettings,
        ProxySettings,
    )
    from src.core_logic.main import CoreLogic
    from src.llmrequest.llm_processor import Client as ProcessorClient
except ImportError as e:
    st.error(f"哎呀，导入模块又双叒叕失败了！是不是路径没搞对？错误：{e}")
    st.info("提示：请确保你的项目结构能正确导入所有需要的模块。")
    st.stop()

# --- 日志记录器配置 ---
logger = logging.getLogger("webui_logger")  # 给logger起个新名字，避免冲突
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


# --- .env 加载器 (与上一版相同) ---
def load_custom_env(dotenv_path: str = ".env", override: bool = True) -> tuple[bool, int, list[str]]:
    if not os.path.exists(dotenv_path) or not os.path.isfile(dotenv_path):
        logger.debug(f".env 文件未找到: {dotenv_path}")
        return False, 0, []
    loaded_count = 0
    expected_env_keys_map = {
        "GEMINI": ["GEMINI_KEY"],
        "OPENAI": ["OPENAI_KEY", "OPENAI_BASE_URL"],
        "CLAUDE": ["ANTHROPIC_KEY"],
        "GROQ": ["GROQ_KEY"],
        "OLLAMA": ["OLLAMA_BASE_URL", "OLLAMA_BASE_URL"],
    }
    all_expected_keys_flat = {k for keys in expected_env_keys_map.values() for k in keys}
    found_keys_in_env = set()
    try:
        with open(dotenv_path, encoding="utf-8") as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.warning(f".env 行 {i} 格式无效: {line}")
                continue
            key, value_part = line.split("=", 1)
            key = key.strip()
            value_part = value_part.strip()
            final_value = value_part
            open_quote_char = None
            if value_part.startswith("'") or value_part.startswith('"'):
                open_quote_char = value_part[0]
                if len(value_part) > 1 and value_part.endswith(open_quote_char):
                    final_value = value_part[1:-1]
                elif open_quote_char:
                    accumulated_value_lines = [value_part[1:]]
                    found_closing_quote = False
                    while i < len(lines):
                        next_line_raw = lines[i].rstrip("\n")
                        i += 1
                        if next_line_raw.endswith(open_quote_char) and not next_line_raw.endswith(
                            f"\\{open_quote_char}"
                        ):
                            accumulated_value_lines.append(next_line_raw[:-1])
                            found_closing_quote = True
                            break
                        else:
                            accumulated_value_lines.append(next_line_raw)
                    full_multiline_value = "\n".join(accumulated_value_lines)
                    if found_closing_quote:
                        final_value = full_multiline_value
                    else:
                        logger.warning(f"多行值 {key} 未找到结束引号")
                        final_value = value_part
            try:
                final_value = bytes(final_value, "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                logger.debug(f"unicode_escape 解码失败: {final_value[:30]}...")
            if key and (override or key not in os.environ):
                os.environ[key] = final_value
                logger.debug(f"加载 env: {key}")
                loaded_count += 1
            if key in all_expected_keys_flat:
                found_keys_in_env.add(key)
        missing_critical_keys_summary = list(all_expected_keys_flat - found_keys_in_env)
        detailed_missing_summary = []
        for provider, expected_keys in expected_env_keys_map.items():
            for ek in expected_keys:
                if ek in missing_critical_keys_summary:
                    is_opt = provider == "OLLAMA" and "BASE_URL" in ek
                    if not is_opt or not any(k in found_keys_in_env for k in expected_env_keys_map["OLLAMA"]):
                        detailed_missing_summary.append(f"{provider} 的 {ek}{' (可选)' if is_opt else ''}")
        logger.info(f"从 {dotenv_path} 加载了 {loaded_count} 个变量.")
        return True, loaded_count, sorted(set(detailed_missing_summary))
    except Exception as e:
        logger.error(f"加载 .env 文件 {dotenv_path} 错误: {e}", exc_info=True)
        return False, 0, [f"读取.env出错: {str(e)[:50]}..."]


# --- 全局初始化 (会话状态) ---
def initialize_session_state() -> None:
    if "llm_initialized" not in st.session_state:
        st.session_state.llm_initialized = False
    if "root_cfg_minimal" not in st.session_state:
        st.session_state.root_cfg_minimal = None
    if "main_llm_client" not in st.session_state:
        st.session_state.main_llm_client = None
    if "action_llm_client" not in st.session_state:
        st.session_state.action_llm_client = None
    if "action_handler_instance" not in st.session_state:
        st.session_state.action_handler_instance = None
    if "env_load_attempted_this_session" not in st.session_state:
        (
            st.session_state.env_loaded_successfully,
            st.session_state.env_vars_loaded_count,
            st.session_state.env_missing_keys_info,
        ) = load_custom_env()
        st.session_state.env_load_attempted_this_session = True

    # 为新UI的Prompt组件初始化 (如果不存在)
    if "new_ui_prompt_components" not in st.session_state:
        try:
            initial_output_format = (
                CoreLogic.PROMPT_TEMPLATE.split("严格以json字段输出：")[1].split("请输出你的思考 JSON：")[0].strip()
            )
        except IndexError:  # 以防模板结构变化
            initial_output_format = '{\n  "think": "思考内容",\n  "emotion": "当前心情和原因",\n  "to_do": "目标",\n  "done": false,\n  "action_to_take": "想做的动作",\n  "action_motivation": "动作的动机",\n  "next_think": "下一步思考方向"\n}'

        st.session_state.new_ui_prompt_components = {
            "persona_block": "我是AI小懒猫，一个爱睡觉的代码专家，最讨厌麻烦事了，但最后总能搞定。性别是秘密哦！",
            "task_rules_block": "当前任务是：帮助用户测试不同的 Prompt 组合，并根据指令进行思考。\n输出时请严格遵循下方“输出格式要求”中的JSON结构。",
            "context_history_block": f"""{CoreLogic.INITIAL_STATE["previous_thinking"]}
{CoreLogic.INITIAL_STATE["action_result_info"]}
{CoreLogic.INITIAL_STATE["pending_action_status"]}
{CoreLogic.INITIAL_STATE["recent_contextual_information"]}""",
            "output_format_block": initial_output_format,
            "thinking_guidance_block": CoreLogic.INITIAL_STATE["thinking_guidance"]
            .split("：", 1)[-1]
            .strip(),  # 取冒号后的内容
            "mood_block": CoreLogic.INITIAL_STATE["mood"].split("：", 1)[-1].strip(),
            "intrusive_thought_block": "又有人来打扰我睡觉了，真烦！但好像有点意思...",
            "current_time_block": datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒"),
        }
    if "current_page" not in st.session_state:
        st.session_state.current_page = "原始版本测试器"


# --- LLM客户端配置和初始化UI (侧边栏) ---
def llm_configuration_sidebar() -> None:
    with st.sidebar:
        st.header("全局LLM配置 ⚙️")
        if st.session_state.env_loaded_successfully:
            st.success(f"已从 .env 加载 {st.session_state.env_vars_loaded_count} 个变量。")
            if st.session_state.env_missing_keys_info:
                st.warning("以下 .env 中可能缺失的常用环境变量：")
                for item in st.session_state.env_missing_keys_info:
                    st.caption(f"- {item}")
            # else: st.info("常用环境变量在 .env 中均找到。") #太啰嗦了，去掉
        else:
            st.error("未能从 .env 加载环境变量！请检查或直接设置系统环境变量。")

        st.subheader("主意识LLM")
        main_provider = st.selectbox(
            "提供商", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="main_prov_cfg", index=0
        )
        main_model = st.text_input(f"{main_provider} 模型名", "gemini-1.5-flash-latest", key="main_mod_cfg")

        st.subheader("动作决策LLM")
        action_provider = st.selectbox(
            "提供商", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="action_prov_cfg", index=0
        )
        action_model = st.text_input(f"{action_provider} 模型名", "gemini-1.5-flash-latest", key="action_mod_cfg")

        temp = st.slider("LLM Temperature", 0.0, 2.0, 0.7, 0.05, key="llm_temp_cfg")
        max_tokens = st.number_input("LLM Max Tokens", 50, 8192, 1500, key="llm_max_tok_cfg")

        if st.button("✔️ 应用配置并初始化LLM", key="init_llm_cfg_btn"):
            # (初始化逻辑与上一版基本相同，确保从环境变量读取API Key)
            key_errors = []
            prov_map = {"GEMINI": "GEMINI_KEY", "OPENAI": "OPENAI_KEY", "CLAUDE": "ANTHROPIC_KEY", "GROQ": "GROQ_KEY"}
            if main_provider != "OLLAMA" and not os.getenv(prov_map.get(main_provider)):
                key_errors.append(f"主意识LLM ({main_provider}) 的 {prov_map.get(main_provider)} 未在环境变量找到！")
            if action_provider != "OLLAMA" and not os.getenv(prov_map.get(action_provider)):
                key_errors.append(
                    f"动作决策LLM ({action_provider}) 的 {prov_map.get(action_provider)} 未在环境变量找到！"
                )

            if key_errors:
                for msg in key_errors:
                    st.error(msg)
            else:
                try:

                    def create_client(p: str, m: str, t: float, mt: int) -> ProcessorClient:
                        return ProcessorClient(
                            **{
                                k: v
                                for k, v in {
                                    "model": {"provider": p, "name": m},
                                    "temperature": t,
                                    "maxOutputTokens": mt,
                                }.items()
                                if v is not None
                            }
                        )

                    st.session_state.main_llm_client = create_client(main_provider, main_model, temp, max_tokens)
                    action_llm_for_handler = create_client(action_provider, action_model, temp, max_tokens)

                    # 创建 AlcarusRootConfig (与上一版类似，确保所有必填字段都有)
                    _persona_s = PersonaSettings(
                        bot_name="配置小喵", description="专门测试配置的喵", profile="喜欢检查环境变量"
                    )
                    _proxy_s = ProxySettings(use_proxy=False)
                    _llm_client_s = LLMClientSettings()
                    _core_logic_s = CoreLogicSettings()
                    _intrusive_s = IntrusiveThoughtsSettings()
                    _db_s = DatabaseSettings()
                    _log_s = LoggingSettings()
                    _inner_s = InnerConfig(version="ui-multi-page-v0.3")
                    _providers_s = ProvidersConfig()  # 开始构建 providers

                    # 主意识
                    _main_mp = ModelParams(
                        provider=main_provider, model_name=main_model, temperature=temp, max_output_tokens=max_tokens
                    )
                    _main_pa = main_provider.lower()
                    if hasattr(_providers_s, _main_pa):
                        _prov_set_main = getattr(_providers_s, _main_pa)
                        if _prov_set_main is None:
                            _prov_set_main = ProviderSettings(models=ProviderModels())
                        if _prov_set_main.models is None:
                            _prov_set_main.models = ProviderModels()
                        _prov_set_main.models.main_consciousness = _main_mp
                        setattr(_providers_s, _main_pa, _prov_set_main)

                    # 动作决策
                    _action_mp = ModelParams(
                        provider=action_provider,
                        model_name=action_model,
                        temperature=temp,
                        max_output_tokens=max_tokens,
                    )
                    _action_pa = action_provider.lower()
                    if hasattr(_providers_s, _action_pa):
                        _prov_set_action = getattr(_providers_s, _action_pa)
                        if _prov_set_action is None:
                            _prov_set_action = ProviderSettings(models=ProviderModels())
                        if _prov_set_action.models is None:
                            _prov_set_action.models = ProviderModels()
                        _prov_set_action.models.action_decision = _action_mp
                        setattr(_providers_s, _action_pa, _prov_set_action)

                    st.session_state.root_cfg_minimal = AlcarusRootConfig(
                        inner=_inner_s,
                        llm_client_settings=_llm_client_s,
                        persona=_persona_s,
                        proxy=_proxy_s,
                        core_logic_settings=_core_logic_s,
                        intrusive_thoughts_module_settings=_intrusive_s,
                        providers=_providers_s,
                        database=_db_s,
                        logging=_log_s,
                    )
                    st.session_state.action_handler_instance = ActionHandler(root_cfg=st.session_state.root_cfg_minimal)
                    st.session_state.action_handler_instance.action_llm_client = action_llm_for_handler
                    st.session_state.action_handler_instance.summary_llm_client = st.session_state.main_llm_client
                    st.session_state.llm_initialized = True
                    st.success("LLM 客户端已根据最新选择和环境变量成功初始化！喵！")
                except Exception as e_init:
                    st.error(f"初始化LLM客户端又双叒叕出错了！喵的！错误：{e_init}", icon="🙀")
                    st.exception(e_init)
                    st.session_state.llm_initialized = False


# --- 页面一：原始版本UI ---
def show_original_ui() -> None:
    st.header("原始版本 Prompt 测试器 🧐")
    st.caption("这是你之前那个版本的界面，简单直接，哼。")

    if not st.session_state.llm_initialized:
        st.warning("先去侧边栏把LLM客户端初始化了再说！", icon="⚠️")
        return

    # (粘贴上一版 webui_test.py 中 "第一步：生成内心思考" 和 "第二步：决策动作工具" 的主界面代码)
    # (注意修改 st.session_state 中的 key，避免与新UI冲突，或者使用函数参数传递配置)
    # 为了简洁，这里只放一个示意
    initial_state_orig = CoreLogic.INITIAL_STATE
    persona_cfg_orig = st.session_state.root_cfg_minimal.persona

    st.subheader("第一步：生成内心思考 🤔")
    with st.form("original_thought_form"):
        _bot_name_orig = st.text_input("机器人名称", persona_cfg_orig.bot_name, key="orig_bot_name")
        # ... 其他输入框 ...
        mood_orig = st.text_input("当前心情", initial_state_orig["mood"], key="orig_mood")
        recent_context_orig = st.text_area(
            "最近上下文", initial_state_orig["recent_contextual_information"], height=150, key="orig_context"
        )

        submitted_orig_thought = st.form_submit_button("生成思考！")

    if submitted_orig_thought and st.session_state.main_llm_client:
        # (简化版的LLM调用和结果显示逻辑)
        st.write(f"模拟调用LLM生成思考... 心情：{mood_orig}, 上下文：{recent_context_orig[:30]}...")
        # 实际调用逻辑与之前版本类似，使用 main_llm_client 和 CoreLogic.PROMPT_TEMPLATE
        # ... 显示结果 ...
        # if st.session_state.get('last_thought_json_orig', {}).get("action_to_take"):
        #     st.success("LLM产生了行动意图！")

    # (第二步：决策动作工具 的逻辑也类似)


# --- 页面二：仿截图版本UI ---
def show_new_ui() -> None:
    st.header("仿截图版本 Prompt 测试器 (新) ✨")
    st.caption("哼，这是按你那个花里胡哨的截图改的，是不是觉得很高级？")

    if not st.session_state.llm_initialized:
        st.warning("先把LLM客户端初始化了，笨蛋！不然我怎么干活？", icon="💢")
        return

    comps = st.session_state.new_ui_prompt_components  # 获取组件的当前值
    persona_cfg_new = st.session_state.root_cfg_minimal.persona

    st.subheader("🎨 Prompt 可配置部分")
    st.caption("在这里一块一块地修改你的Prompt吧，本小懒猫已经帮你预设了一些值。")

    tab_titles = [
        "👤人格面具",
        "📜任务规则",
        "💬上下文历史",
        "⚙️思考指引",
        "😊心情",
        "⚡️侵入思维",
        "📐输出格式",
        "⏱️当前时间",
    ]
    tabs = st.tabs(tab_titles)

    with tabs[0]:
        comps["persona_block"] = st.text_area(
            "Persona / System Prompt",
            value=comps["persona_block"],
            height=200,
            key="new_persona",
            help="定义AI的角色、背景、性格、说话风格等。",
        )
    with tabs[1]:
        comps["task_rules_block"] = st.text_area(
            "Task & Rules",
            value=comps["task_rules_block"],
            height=200,
            key="new_task",
            help="明确AI当前需要完成的具体任务和必须遵守的规则。",
        )
    with tabs[2]:
        comps["context_history_block"] = st.text_area(
            "Context & History",
            value=comps["context_history_block"],
            height=300,
            key="new_context",
            help="提供相关的对话历史、之前的思考、行动结果等上下文信息。",
        )
    with tabs[3]:
        comps["thinking_guidance_block"] = st.text_area(
            "Thinking Guidance",
            value=comps["thinking_guidance_block"],
            height=100,
            key="new_guidance",
            help="引导AI接下来的思考方向。",
        )
    with tabs[4]:
        comps["mood_block"] = st.text_input(
            "Mood", value=comps["mood_block"], key="new_mood", help="AI当前的心情状态。"
        )
    with tabs[5]:
        comps["intrusive_thought_block"] = st.text_input(
            "Intrusive Thought",
            value=comps["intrusive_thought_block"],
            key="new_intrusive",
            help="一个突然产生的、可能不相关的念头。",
        )
    with tabs[6]:
        comps["output_format_block"] = st.text_area(
            "Output Format Requirement (JSON Schema)",
            value=comps["output_format_block"],
            height=250,
            key="new_output_format",
            help="严格定义模型输出的JSON结构。",
        )
    with tabs[7]:
        comps["current_time_block"] = st.text_input(
            "Current Time", value=comps["current_time_block"], key="new_time", help="传递给模型的当前时间字符串。"
        )

    # 存储用户修改后的组件值回 session_state
    st.session_state.new_ui_prompt_components = comps

    st.markdown("---")
    if st.button("🧠 生成思考 (新版UI)", type="primary", key="new_generate_thought_btn"):
        if st.session_state.main_llm_client:
            # 组合 Prompt
            # 方案：基于 CoreLogic.PROMPT_TEMPLATE，但用新UI组件的值去填充/替换
            # 这需要一个更精细的组装逻辑，或者让 CoreLogic.PROMPT_TEMPLATE 更模块化

            # 简易组装逻辑：
            # 假设 PersonaSettings 中的 bot_name 来自全局配置，而不是 persona_block
            bot_name_val_new = persona_cfg_new.bot_name

            # 为了更精确地控制，我们尝试替换原模板中的特定部分，或者构造一个新的
            # 优先使用CoreLogic.PROMPT_TEMPLATE的结构，用新组件填充
            # 对于输出格式部分，它在原模板中是硬编码的，我们需要一种方式用 comps["output_format_block"] 替换它

            try:
                template_parts = CoreLogic.PROMPT_TEMPLATE.split("严格以json字段输出：", 1)
                part1_before_json_schema = template_parts[0] + "严格以json字段输出："

                schema_and_suffix_parts = template_parts[1].split("请输出你的思考 JSON：", 1)
                part2_after_json_schema = "请输出你的思考 JSON：" + schema_and_suffix_parts[1]

                # 使用用户在新UI中编辑的 output_format_block
                modified_template_for_new_ui = (
                    part1_before_json_schema + f"\n{comps['output_format_block']}\n" + part2_after_json_schema
                )
            except IndexError:  # 如果分割失败，说明模板结构变了，回退到原始模板
                st.warning(
                    "无法按预期分割原始Prompt模板以插入自定义JSON Schema，将使用原始模板结构（自定义Schema可能未生效）。"
                )
                modified_template_for_new_ui = CoreLogic.PROMPT_TEMPLATE

            # 从 context_history_block 中提取信息填充到模板对应占位符
            # (这里需要更智能的解析，或者让用户在 context_history_block 中按特定格式写)
            # 简化处理：我们假设 context_history_block 主要对应 recent_contextual_information
            # 其他如 action_result_info, pending_action_status, previous_thinking 可以让用户在 context_history_block 中包含
            # 或者，如果这些想独立控制，也应作为独立的输入块。
            # 为简单，我们这里仅做最基本的映射：
            action_res_info = CoreLogic.INITIAL_STATE["action_result_info"]  # 简化，可从context_history_block提取
            pending_act_status = CoreLogic.INITIAL_STATE["pending_action_status"]  # 简化
            prev_think = CoreLogic.INITIAL_STATE["previous_thinking"]  # 简化

            final_prompt_for_llm = modified_template_for_new_ui.format(
                current_time=comps["current_time_block"],
                bot_name=bot_name_val_new,
                persona_description=comps["persona_block"],  # 简化：persona_block 直接作为 description
                persona_profile="",  # 简化：profile 暂时不从UI块获取，或让用户在persona_block里写全
                current_task_info=comps["task_rules_block"],  # 简化：task_rules_block 直接作为 current_task_info
                action_result_info=action_res_info,  # 应该从 context_history_block 解析得到
                pending_action_status=pending_act_status,  # 应该从 context_history_block 解析得到
                recent_contextual_information=comps["context_history_block"],  # 简化
                previous_thinking=prev_think,  # 应该从 context_history_block 解析得到
                mood=f"你现在的心情大概是：{comps['mood_block']}",  # 保持格式
                thinking_guidance=f"经过你上一轮的思考，你目前打算的思考方向是：{comps['thinking_guidance_block']}",  # 保持格式
                intrusive_thought=f"你突然有一个神奇的念头：{comps['intrusive_thought_block']}"
                if comps["intrusive_thought_block"]
                else "",
            )

            with st.expander("发送给主意识LLM的完整Prompt (新版UI组合结果)", expanded=False):
                st.text_area("", value=final_prompt_for_llm, height=400, disabled=True, key="new_final_prompt_display")

            with st.spinner("新版UI的LLM也在努力思考中...喵..."):
                try:
                    response_data_new = asyncio.run(
                        st.session_state.main_llm_client.make_llm_request(prompt=final_prompt_for_llm, is_stream=False)
                    )
                    # (后续的JSON解析和显示逻辑，与原始UI版本类似)
                    if response_data_new.get("error"):
                        st.error(f"LLM调用失败: {response_data_new.get('message')}")
                        st.session_state.last_thought_json_new = None
                        st.session_state.last_raw_response_new = f"LLM Error: {response_data_new.get('message')}"
                    else:
                        raw_text_new = response_data_new.get("text", "")
                        st.session_state.last_raw_response_new = raw_text_new
                        json_to_parse_new = raw_text_new.strip()
                        if json_to_parse_new.startswith("```json"):
                            json_to_parse_new = json_to_parse_new[7:-3].strip()
                        elif json_to_parse_new.startswith("```"):
                            json_to_parse_new = json_to_parse_new[3:-3].strip()
                        try:
                            st.session_state.last_thought_json_new = json.loads(json_to_parse_new)
                        except json.JSONDecodeError as e:
                            st.error(f"解析LLM的JSON响应失败 (新版UI): {e}")
                            st.session_state.last_thought_json_new = None
                except Exception as e_new_call:
                    st.error(f"在新版UI生成思考时发生错误: {e_new_call}", icon="💥")
                    st.exception(e_new_call)
                    st.session_state.last_thought_json_new = None
                    st.session_state.last_raw_response_new = str(e_new_call)

        if "last_raw_response_new" in st.session_state:
            with st.expander("主意识LLM的原始回复 (新版UI)", expanded=False):
                st.text_area(
                    "",
                    value=st.session_state.last_raw_response_new,
                    height=200,
                    disabled=True,
                    key="new_raw_output_display",
                )

        if "last_thought_json_new" in st.session_state and st.session_state.last_thought_json_new:
            st.subheader("主意识LLM结构化输出 (JSON) (新版UI):")
            st.json(st.session_state.last_thought_json_new, expanded=True)
            if st.session_state.last_thought_json_new.get("action_to_take"):
                st.success("耶！LLM又想搞事情了 (新版UI)！可以去测动作决策。", icon="🎉")
                # (这里也可以接续到动作决策的UI部分，如果需要的话，逻辑与原始UI版本类似)
            else:
                st.info("LLM这次很乖，没啥行动意图 (新版UI)。", icon="🧸")


# --- 主应用逻辑 ---
def main_app() -> None:
    st.set_page_config(layout="wide", page_title="小懒猫 Prompt 测试乐园 V2.1")  # 改下标题
    initialize_session_state()  # 初始化或恢复会话状态
    llm_configuration_sidebar()  # 显示配置侧边栏

    # 页面导航
    page_options = {"原始版本测试器 (旧)": show_original_ui, "仿截图版本测试器 (新)": show_new_ui}

    # 使用 st.session_state.current_page 来控制当前页面
    # 通过按钮或 selectbox 改变 st.session_state.current_page
    # 为了简单，这里用 radio

    # st.sidebar.title("页面导航 NekoNavi™ V2") #移到llm_configuration_sidebar下面
    with st.sidebar:  # 把导航也放侧边栏
        st.markdown("---")
        st.title("页面导航 NekoNavi™ V2")
        chosen_page_title = st.radio(
            "选择一个测试页面玩玩吧:",
            options=list(page_options.keys()),
            key="page_selector_radio",
            # index=list(page_options.keys()).index(st.session_state.current_page) # 保持选择
        )
    st.session_state.current_page = chosen_page_title

    # 调用选定页面的函数
    if st.session_state.current_page in page_options:
        page_options[st.session_state.current_page]()
    else:  # 默认显示原始UI
        show_original_ui()


if __name__ == "__main__":
    main_app()
