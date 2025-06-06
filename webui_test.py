# webui_test.py (或者你可以改名叫 main_app.py)
import asyncio
import datetime
import json
import logging #
import os
import uuid #
import threading #
import streamlit as st

# --- 小懒猫的温馨提示 ---
# (导入模块部分，与上一版基本一致)
try:
    from src.action.action_handler import ActionHandler #
    from src.config.aicarus_configs import ( # <--- 修改路径
        AlcarusRootConfig,
        CoreLogicSettings,
        DatabaseSettings,
        InnerConfig,
        IntrusiveThoughtsSettings,
        LLMClientSettings,
        LoggingSettings,
        ModelParams,
        PersonaSettings,
        # ProviderModels, # 不再需要
        # ProvidersConfig, # 不再需要
        # ProviderSettings, # 不再需要
        AllModelPurposesConfig, # <-- 新增导入
        ProxySettings,
        ServerSettings
    )
    from src.core_logic.consciousness_flow import CoreLogic as CoreLogicFlow # <-- 导入新的 CoreLogic 文件并重命名
    from src.core_logic.prompt_builder import ThoughtPromptBuilder # <-- 新增导入
    from src.core_logic.state_manager import AIStateManager # <-- 新增导入
    # StorageManager 可能不再直接使用，因为我们将通过 CoreSystemInitializer 获得 ArangoDBHandler
    # from src.database import StorageManager
    from src.llmrequest.llm_processor import Client as ProcessorClient #
    from src.main import CoreSystemInitializer # <-- 导入 CoreSystemInitializer

except ImportError as e: #
    st.error(f"哎呀，导入模块又双叒叕失败了！是不是路径没搞对？错误：{e}")
    st.info("提示：请确保你的项目结构能正确导入所有需要的模块。")
    st.stop()

# --- 日志记录器配置 ---
logger = logging.getLogger("webui_logger_aicarus_core") # 给logger起个更独特的名字，避免潜在冲突
if not logger.handlers: #
    handler = logging.StreamHandler() #
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s") #
    handler.setFormatter(formatter) #
    logger.addHandler(handler) #
logger.setLevel(logging.INFO) #


# --- .env 加载器 (保持不变) ---
def load_custom_env(dotenv_path: str = ".env", override: bool = True) -> tuple[bool, int, list[str]]: #
    if not os.path.exists(dotenv_path) or not os.path.isfile(dotenv_path): #
        logger.debug(f".env 文件未找到: {dotenv_path}")
        return False, 0, [] #
    loaded_count = 0 #
    expected_env_keys_map = { #
        "GEMINI": ["GEMINI_KEY"],
        "OPENAI": ["OPENAI_KEY", "OPENAI_BASE_URL"],
        "CLAUDE": ["ANTHROPIC_KEY"],
        "GROQ": ["GROQ_KEY"],
        "OLLAMA": ["OLLAMA_BASE_URL"], # 修正：OLLAMA 通常只有一个 BASE_URL
    }
    all_expected_keys_flat = {k for keys in expected_env_keys_map.values() for k in keys} #
    found_keys_in_env = set() #
    try:
        with open(dotenv_path, encoding="utf-8") as f: #
            lines = f.readlines() #
        i = 0 #
        while i < len(lines): #
            line = lines[i].strip() #
            i += 1 #
            if not line or line.startswith("#"): #
                continue
            if "=" not in line: #
                logger.warning(f".env 行 {i} 格式无效: {line}")
                continue
            key, value_part = line.split("=", 1) #
            key = key.strip() #
            value_part = value_part.strip() #
            final_value = value_part #
            open_quote_char = None #
            if value_part.startswith("'") or value_part.startswith('"'): #
                open_quote_char = value_part[0] #
                # 修正了多行值处理中对未闭合引号的检查逻辑
                if ( #
                    len(value_part) > 1  
                    and value_part.endswith(open_quote_char)  
                    and (value_part[1:-1].count(open_quote_char) == 0 or (value_part[1:-1].replace(f"\\{open_quote_char}", "").count(open_quote_char) % 2 == 0) ) #
                ) : #
                    final_value = value_part[1:-1] #
                elif open_quote_char: # # 多行值处理
                    accumulated_value_lines = [value_part[1:]] #
                    found_closing_quote = False #
                    while i < len(lines): #
                        next_line_raw = lines[i].rstrip("\n") #
                        i += 1 #
                        # 检查行尾是否有结束引号，并且这个引号不是转义的
                        if next_line_raw.endswith(open_quote_char) and not next_line_raw.endswith(f"\\{open_quote_char}"): #
                            accumulated_value_lines.append(next_line_raw[:-1]) #
                            found_closing_quote = True #
                            break #
                        else:
                            accumulated_value_lines.append(next_line_raw) #
                    full_multiline_value = "\n".join(accumulated_value_lines) #
                    if found_closing_quote: #
                        final_value = full_multiline_value #
                    else: #
                        logger.warning(f"多行值 {key} 未找到结束引号")
                        final_value = value_part # Fallback to original if not properly closed
            try:
                final_value = bytes(final_value, "utf-8").decode("unicode_escape") #
            except UnicodeDecodeError: #
                logger.debug(f"unicode_escape 解码失败: {final_value[:30]}...")
            if key and (override or key not in os.environ): #
                os.environ[key] = final_value #
                logger.debug(f"加载 env: {key}")
                loaded_count += 1 #
            if key in all_expected_keys_flat: #
                found_keys_in_env.add(key) #
        missing_critical_keys_summary = list(all_expected_keys_flat - found_keys_in_env) #
        detailed_missing_summary = [] #
        for provider, expected_keys in expected_env_keys_map.items(): #
            for ek in expected_keys: #
                if ek in missing_critical_keys_summary: #
                    is_opt = provider == "OLLAMA" and "BASE_URL" in ek #
                    if not is_opt or not any(k_ollama in found_keys_in_env for k_ollama in expected_env_keys_map.get("OLLAMA", [])): # 确保 OLLAMA 的 key 存在才判断可选 #
                        detailed_missing_summary.append(f"{provider} 的 {ek}{' (可选)' if is_opt else ''}") #
        logger.info(f"从 {dotenv_path} 加载了 {loaded_count} 个变量。")
        return True, loaded_count, sorted(set(detailed_missing_summary)) #
    except Exception as e: #
        logger.error(f"加载 .env 文件 {dotenv_path} 错误: {e}", exc_info=True)
        return False, 0, [f"读取.env出错: {str(e)[:50]}..."] #


# --- 全局初始化 (会话状态) ---
def initialize_session_state() -> None: #
    if "llm_initialized" not in st.session_state: #
        st.session_state.llm_initialized = False #
    # storage_manager 和 storage_initialized 将通过 core_initializer 管理
    if "storage_initialized" not in st.session_state:
        st.session_state.storage_initialized = False
    if "core_initializer" not in st.session_state: #
        st.session_state.core_initializer = None #
    
    if "env_load_attempted_this_session" not in st.session_state: #
        (
            st.session_state.env_loaded_successfully, #
            st.session_state.env_vars_loaded_count, #
            st.session_state.env_missing_keys_info, #
        ) = load_custom_env()
        st.session_state.env_load_attempted_this_session = True #

    if "new_ui_prompt_components" not in st.session_state: #
        try:
            initial_output_format = ( #
                ThoughtPromptBuilder.PROMPT_TEMPLATE.split("严格以json字段输出：")[1].split("请输出你的思考 JSON：")[0].strip() # <-- 修改
            )
        except IndexError: #
            initial_output_format = '{\n  "think": "思考内容",\n  "emotion": "当前心情和原因",\n  "to_do": "目标",\n  "done": false,\n  "action_to_take": "想做的动作",\n  "action_motivation": "动作的动机",\n  "next_think": "下一步思考方向"\n}' #

        st.session_state.new_ui_prompt_components = { #
            "persona_block": "我是AI小懒猫，一个爱睡觉的代码专家，最讨厌麻烦事了，但最后总能搞定。性别是秘密哦！", #
            "task_rules_block": "当前任务是：帮助用户测试不同的 Prompt 组合，并根据指令进行思考。\n输出时请严格遵循下方“输出格式要求”中的JSON结构。", #
            "context_history_block": f"""{AIStateManager.INITIAL_STATE["previous_thinking"]} 
{AIStateManager.INITIAL_STATE["action_result_info"]}
{AIStateManager.INITIAL_STATE["pending_action_status"]}
{AIStateManager.INITIAL_STATE["recent_contextual_information"]}""", # <-- 修改
            "output_format_block": initial_output_format, #
            "thinking_guidance_block": AIStateManager.INITIAL_STATE["thinking_guidance"] # <-- 修改
            .split("：", 1)[-1] #
            .strip(), 
            "mood_block": AIStateManager.INITIAL_STATE["mood"].split("：", 1)[-1].strip(), # <-- 修改
            "intrusive_thought_block": "又有人来打扰我睡觉了，真烦！但好像有点意思...", #
            "current_time_block": datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒"), #
        }
    if "current_page" not in st.session_state: #
        st.session_state.current_page = "原始版本测试器" #


# --- LLM客户端配置和初始化UI (侧边栏) ---
def llm_configuration_sidebar() -> None: #
    with st.sidebar: #
        st.header("全局LLM与数据库配置 ⚙️") #
        if st.session_state.env_loaded_successfully: #
            st.success(f"已从 .env 加载 {st.session_state.env_vars_loaded_count} 个变量。") #
            if st.session_state.env_missing_keys_info: #
                st.warning("以下 .env 中可能缺失的常用环境变量：") #
                for item in st.session_state.env_missing_keys_info: #
                    st.caption(f"- {item}") #
        else: #
            st.error("未能从 .env 加载环境变量！请检查或直接设置系统环境变量。") #

        st.subheader("主意识LLM") #
        main_provider = st.selectbox( #
            "提供商", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="main_prov_cfg_ui", index=0 # 更改key防止冲突
        )
        main_model_name = st.text_input(f"{main_provider} 模型名", "gemini-1.5-flash-latest", key="main_mod_cfg_ui") #

        st.subheader("动作决策LLM") #
        action_provider = st.selectbox( #
            "提供商 ", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="action_prov_cfg_ui", index=0 # 更改key防止冲突 (末尾加空格)
        )
        action_model_name = st.text_input(f"{action_provider} 模型名 ", "gemini-1.5-flash-latest", key="action_mod_cfg_ui") #
        
        # 为信息总结和侵入性思维也添加配置选项
        st.subheader("信息总结LLM")
        summary_provider = st.selectbox(
            "提供商  ", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="summary_prov_cfg_ui", index=0
        )
        summary_model_name = st.text_input(f"{summary_provider} 模型名  ", "gemini-1.5-flash-latest", key="summary_mod_cfg_ui")

        st.subheader("侵入思维LLM")
        intrusive_provider = st.selectbox(
            "提供商   ", ["GEMINI", "OPENAI", "CLAUDE", "GROQ", "OLLAMA"], key="intrusive_prov_cfg_ui", index=0
        )
        intrusive_model_name = st.text_input(f"{intrusive_provider} 模型名   ", "gemini-1.5-flash-latest", key="intrusive_mod_cfg_ui")


        temp = st.slider("LLM Temperature", 0.0, 2.0, 0.7, 0.05, key="llm_temp_cfg_ui") #
        max_tokens = st.number_input("LLM Max Tokens", 50, 8192, 2048, key="llm_max_tok_cfg_ui") #

        if st.button("✔️ 应用LLM配置并初始化", key="init_llm_cfg_btn_ui"): #
            key_errors = [] #
            prov_map = {"GEMINI": "GEMINI_KEY", "OPENAI": "OPENAI_KEY", "CLAUDE": "ANTHROPIC_KEY", "GROQ": "GROQ_KEY"} #
            
            providers_to_check = {
                "主意识LLM": (main_provider, prov_map.get(main_provider)),
                "动作决策LLM": (action_provider, prov_map.get(action_provider)),
                "信息总结LLM": (summary_provider, prov_map.get(summary_provider)),
                "侵入思维LLM": (intrusive_provider, prov_map.get(intrusive_provider)),
            }

            for llm_purpose, (provider_name, env_key_name) in providers_to_check.items():
                if provider_name != "OLLAMA" and env_key_name and not os.getenv(env_key_name): #
                    key_errors.append(f"{llm_purpose} ({provider_name}) 的 {env_key_name} 未在环境变量找到！") #
            
            if key_errors: #
                for msg in key_errors: #
                    st.error(msg) #
            else: #
                try:
                    # 创建一个临时的 AlcarusRootConfig 对象，填充UI上的选择
                    _persona_s = PersonaSettings(bot_name="UI测试小喵", description="...", profile="...") #
                    _proxy_s = ProxySettings(use_proxy=False) #
                    _llm_client_s = LLMClientSettings() #
                    _core_logic_s = CoreLogicSettings() #
                    _intrusive_s = IntrusiveThoughtsSettings(enabled=(intrusive_provider is not None and intrusive_model_name is not None)) # 根据是否有配置决定是否启用 #
                    _db_s = DatabaseSettings() # 稍后由数据库配置部分填充
                    _log_s = LoggingSettings() #
                    _inner_s = InnerConfig(version="ui-test-v0.4") #
                    _server_s = ServerSettings() #

                    # _providers_s = ProvidersConfig( #  <-- 不再需要这个
                    #     gemini=ProviderSettings(models=ProviderModels()),
                    #     openai=ProviderSettings(models=ProviderModels()),
                    #     # ... 其他提供商可以类似初始化 ...
                    # )
                    _llm_models_cfg = AllModelPurposesConfig() # <-- 创建新的模型配置对象
                    
                    # 填充模型配置
                    model_configs_ui = {
                        "main_consciousness": (main_provider, main_model_name),
                        "action_decision": (action_provider, action_model_name),
                        "information_summary": (summary_provider, summary_model_name),
                        "intrusive_thoughts": (intrusive_provider, intrusive_model_name),
                    }

                    for purpose_key, (prov_name, model_n) in model_configs_ui.items():
                        if prov_name and model_n:
                            mp = ModelParams(provider=prov_name, model_name=model_n, temperature=temp, max_output_tokens=max_tokens)
                            # prov_attr_name = prov_name.lower() # 不再需要这个
                            if hasattr(_llm_models_cfg, purpose_key): # 直接检查 AllModelPurposesConfig 是否有该用途的字段
                                setattr(_llm_models_cfg, purpose_key, mp)
                            # else: # ProviderSettings 和 ProviderModels 的逻辑不再需要
                                # logger.warning(f"提供商 '{prov_attr_name}' 在 ProvidersConfig 中没有预定义属性，请检查 alcarus_configs.py。") # 旧的警告
                                # 新的结构下，如果 AllModelPurposesConfig 没有定义某个 purpose_key，那是个结构问题，但这里我们假设 purpose_key 都是有效的
                            else:
                                logger.warning(f"模型用途 '{purpose_key}' 在 AllModelPurposesConfig 中没有预定义属性，请检查 aicarus_configs.py。")


                    temp_root_cfg_for_llm = AlcarusRootConfig( #
                        inner=_inner_s,
                        llm_client_settings=_llm_client_s,
                        persona=_persona_s,
                        proxy=_proxy_s,
                        core_logic_settings=_core_logic_s,
                        intrusive_thoughts_module_settings=_intrusive_s,
                        # providers=_providers_s, # <-- 替换为 llm_models
                        llm_models=_llm_models_cfg, # <-- 使用新的模型配置
                        database=_db_s, # 使用一个默认的，实际DB连接在下面处理
                        logging=_log_s,
                        server=_server_s 
                    )

                    if st.session_state.core_initializer is None: #
                        st.session_state.core_initializer = CoreSystemInitializer() #
                    
                    st.session_state.core_initializer.root_cfg = temp_root_cfg_for_llm #
                    
                    async def initialize_llm_clients_async_ui(): #
                        # _initialize_llm_clients 会使用 self.root_cfg
                        await st.session_state.core_initializer._initialize_llm_clients() #
                        st.session_state.llm_initialized = True #
                        st.success("LLM 客户端已根据最新选择和环境变量成功初始化！喵！") #
                    
                    # 在Streamlit中直接运行异步函数
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(initialize_llm_clients_async_ui())
                    loop.close()

                except Exception as e_init: #
                    st.error(f"初始化LLM客户端又双叒叕出错了！喵的！错误：{e_init}", icon="🙀") #
                    st.exception(e_init) #
                    st.session_state.llm_initialized = False #

        st.markdown("---") #
        st.subheader("数据库配置 🗄️") #

        if not st.session_state.storage_initialized: #
            db_host = st.text_input("数据库地址", os.getenv("ARANGODB_HOST", "http://localhost:8529"), key="db_host_cfg_ui") #
            db_name = st.text_input("数据库名", os.getenv("ARANGODB_DATABASE", "aicarus_core_ui_test"), key="db_name_cfg_ui") #
            db_user = st.text_input("用户名", os.getenv("ARANGODB_USER", "root"), key="db_user_cfg_ui") #
            db_pass = st.text_input("密码", os.getenv("ARANGODB_PASSWORD", ""), type="password", key="db_pass_cfg_ui") #

            if st.button("🔌 连接数据库", key="init_db_cfg_btn_ui"): #
                async def connect_to_database_ui() -> None: #
                    if st.session_state.core_initializer is None: #
                        st.session_state.core_initializer = CoreSystemInitializer() #
                        # 为初始化器提供一个最小的root_cfg，如果它还没有的话
                        if st.session_state.core_initializer.root_cfg is None:
                             st.session_state.core_initializer.root_cfg = AlcarusRootConfig(
                                inner=InnerConfig(version="ui-db-init-temp"),
                                llm_client_settings=LLMClientSettings(), persona=PersonaSettings(),
                                proxy=ProxySettings(), core_logic_settings=CoreLogicSettings(),
                                intrusive_thoughts_module_settings=IntrusiveThoughtsSettings(),
                                database=DatabaseSettings(), logging=LoggingSettings(), server=ServerSettings()
                            )
                    
                    # 更新 CoreInitializer 实例中的数据库配置
                    current_root_cfg = st.session_state.core_initializer.root_cfg
                    if current_root_cfg is None: # 双重保险
                        st.error("CoreInitializer的root_cfg未初始化，无法设置数据库配置。")
                        return

                    current_root_cfg.database.host = db_host #
                    current_root_cfg.database.database_name = db_name #
                    current_root_cfg.database.username = db_user #
                    current_root_cfg.database.password = db_pass #
                    
                    try:
                        # 调用 CoreSystemInitializer 内部的数据库初始化方法
                        # 这个方法会使用 self.root_cfg.database
                        await st.session_state.core_initializer._initialize_database_and_services() # <-- 修改方法名
                        
                        # 如果成功，conn_manager 和其内部的 db 应该已经被设置在 core_initializer 实例上
                        if st.session_state.core_initializer.conn_manager and st.session_state.core_initializer.conn_manager.db: # <-- 修改检查逻辑
                            # st.session_state.storage_manager = st.session_state.core_initializer.db_handler # storage_manager不再单独使用
                            st.session_state.storage_initialized = True #
                            st.success("数据库连接成功！🎉 (通过 CoreSystemInitializer 的新方法)") #
                            st.rerun() #  替换 st.experimental_rerun()
                        else:
                            st.error("数据库初始化后，CoreSystemInitializer.conn_manager 或其内部db为空！😿") # <-- 修改错误信息
                            st.session_state.storage_initialized = False
                    except Exception as e_db_init_ui:
                        st.error(f"数据库连接失败 (通过 CoreSystemInitializer 的新方法): {e_db_init_ui} 😿") # <-- 修改错误信息
                        st.exception(e_db_init_ui)
                        st.session_state.storage_initialized = False
                
                # 在Streamlit中直接运行异步函数
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(connect_to_database_ui())
                loop.close()
        else: #
            st.success("✅ 数据库已连接") #
            if st.button("🔄 重新配置数据库", key="reset_db_cfg_btn_ui"): #
                if st.session_state.core_initializer and st.session_state.core_initializer.db_handler: #
                    # 关闭旧的连接
                    close_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(close_loop)
                    try:
                        close_loop.run_until_complete(st.session_state.core_initializer.db_handler.close())
                    finally:
                        close_loop.close()
                st.session_state.storage_initialized = False #
                # st.session_state.storage_manager = None # 不再单独使用
                st.session_state.core_initializer = None # 重置 initializer 以便下次重新创建并配置
                st.rerun() #


# --- 页面一：原始版本UI ---
async def show_original_ui() -> None: #
    st.header("原始版本 Prompt 测试器 🧐") #
    st.caption("这是你之前那个版本的界面，简单直接，哼。") #

    if not st.session_state.llm_initialized: #
        st.warning("先去侧边栏把LLM客户端初始化了再说！", icon="⚠️") #
        return
    if not st.session_state.storage_initialized: #
        st.warning("数据库还没连接好，请先去侧边栏连接数据库！", icon="⚠️") #
        return
    if st.session_state.core_initializer is None or \
       st.session_state.core_initializer.main_consciousness_llm_client is None or \
       st.session_state.core_initializer.action_handler_instance is None or \
       st.session_state.core_initializer.db_handler is None or \
       st.session_state.core_initializer.root_cfg is None: # 确保 root_cfg 也已通过 Initializer 设置
        st.warning("核心组件未完全初始化（LLM, DB, ActionHandler, Config），请检查侧边栏配置并应用！", icon="⚠️") #
        return


    # 获取初始化后的组件
    main_llm_client = st.session_state.core_initializer.main_consciousness_llm_client #
    action_handler_instance = st.session_state.core_initializer.action_handler_instance #
    # db_handler = st.session_state.core_initializer.db_handler # <-- 不再直接使用 db_handler
    event_storage_service_ui = st.session_state.core_initializer.event_storage_service # <-- 获取新的服务实例
    thought_storage_service_ui = st.session_state.core_initializer.thought_storage_service # <-- 获取新的服务实例
    root_cfg = st.session_state.core_initializer.root_cfg #

    # initial_state_orig = CoreLogicFlow.INITIAL_STATE # <-- 不再需要，因为 CoreLogicFlow 不再有 INITIAL_STATE
    initial_state_orig = AIStateManager.INITIAL_STATE # <-- 改用 AIStateManager 的初始状态

    # 确保 ActionHandler 的依赖在 UI 环境中是最新的
    if action_handler_instance and thought_storage_service_ui: # <-- 修改依赖检查
        # UI测试中，core_comm_layer 可能为 None，ActionHandler 应能处理
        comm_layer_for_ui_test = getattr(st.session_state.core_initializer, 'core_comm_layer', None)
        action_handler_instance.set_dependencies( # <-- 修改依赖设置
            thought_service=thought_storage_service_ui, 
            comm_layer=comm_layer_for_ui_test
        )
        
        # 确保 ActionHandler 的 LLM 客户端也已通过 Initializer 设置
        if not action_handler_instance.action_llm_client and st.session_state.core_initializer.action_llm_client:
            action_handler_instance.action_llm_client = st.session_state.core_initializer.action_llm_client
        if not action_handler_instance.summary_llm_client and st.session_state.core_initializer.summary_llm_client:
            action_handler_instance.summary_llm_client = st.session_state.core_initializer.summary_llm_client

        if not action_handler_instance.action_llm_client or not action_handler_instance.summary_llm_client: #
            st.warning("ActionHandler 的 LLM 客户端在 UI 中可能未完全设置，将尝试在首次使用时重新初始化。") #
            # ActionHandler 内部的 process_action_flow 会调用 initialize_llm_clients

    # initial_state_orig = CoreLogicFlow.INITIAL_STATE #  <-- 这个已经被 AIStateManager.INITIAL_STATE 替换了，确保这里用的是更新后的
    # persona_cfg_orig = root_cfg.persona # 这行没问题，保留

    # 如果 initial_state_orig 还需要在这里被重新赋值，确保它从 AIStateManager 获取
    # 但从逻辑上看，上面的 initial_state_orig = AIStateManager.INITIAL_STATE 应该已经够用了
    # 所以这里可能不需要再次赋值 initial_state_orig，除非之前的赋值在某个条件下被跳过
    # 为保险起见，如果之前的赋值是正确的，这里就不需要这行了。
    # 但如果之前的赋值可能因为某些原因没有执行，那么这里需要确保 initial_state_orig 是正确的。
    # 考虑到代码结构，上面的赋值是无条件的，所以这里可以安全地移除或注释掉对 CoreLogicFlow.INITIAL_STATE 的引用。
    # 我们已经在上面将 initial_state_orig 设置为 AIStateManager.INITIAL_STATE
    # 所以，下面的 persona_cfg_orig 赋值之前不需要再动 initial_state_orig
    persona_cfg_orig = root_cfg.persona #

    st.subheader("第一步：生成内心思考 🤔") #
    with st.form("original_thought_form_ui"): #
        _bot_name_orig = st.text_input("机器人名称", persona_cfg_orig.bot_name, key="orig_bot_name_ui") #
        mood_orig = st.text_input("当前心情", initial_state_orig["mood"], key="orig_mood_ui") #
        previous_thinking_orig = st.text_area("上一轮思考", initial_state_orig["previous_thinking"], height=100, key="orig_prev_think_ui") #
        thinking_guidance_orig = st.text_area("思考方向指引", initial_state_orig["thinking_guidance"], height=100, key="orig_think_guidance_ui") #
        current_task_orig = st.text_input("当前任务", initial_state_orig["current_task"], key="orig_current_task_ui") #
        action_result_info_orig = st.text_area("上轮行动结果", initial_state_orig["action_result_info"], height=100, key="orig_action_result_ui") #
        pending_action_status_orig = st.text_input("待处理行动状态", initial_state_orig["pending_action_status"], key="orig_pending_action_ui") #
        recent_context_orig = st.text_area( #
            "最近上下文信息", initial_state_orig["recent_contextual_information"], height=150, key="orig_context_ui" #
        )
        intrusive_thought_orig = st.text_input("侵入性思维", "", key="orig_intrusive_thought_ui") #

        submitted_orig_thought = st.form_submit_button("生成思考！") #

    if submitted_orig_thought: #
        if main_llm_client and event_storage_service_ui and thought_storage_service_ui and root_cfg and action_handler_instance: # <-- 修改依赖检查
            st.info("正在生成思考...请稍候。") #
            try:
                # 创建临时的 CoreLogicFlow 实例用于测试 Prompt 生成
                temp_core_logic_for_prompt_ui = CoreLogicFlow( #
                    # root_cfg=root_cfg, # CoreLogicFlow 现在直接从全局 config 获取配置
                    event_storage_service=event_storage_service_ui, # <-- 修改
                    thought_storage_service=thought_storage_service_ui, # <-- 新增
                    main_consciousness_llm_client=main_llm_client, #
                    intrusive_thoughts_llm_client=st.session_state.core_initializer.intrusive_thoughts_llm_client, #
                    core_comm_layer=getattr(st.session_state.core_initializer, 'core_comm_layer', None), #
                    action_handler_instance=action_handler_instance, #
                    intrusive_generator_instance=getattr(st.session_state.core_initializer, 'intrusive_generator_instance', None), #
                    stop_event=threading.Event(), #
                    immediate_thought_trigger=asyncio.Event() # <-- CoreLogicFlow 需要这个参数
                )
                
                current_state_for_prompt_ui_dict = { #
                    "mood": mood_orig, #
                    "previous_thinking": previous_thinking_orig, #
                    "thinking_guidance": thinking_guidance_orig, #
                    "current_task": current_task_orig, #
                    "action_result_info": action_result_info_orig, #
                    "pending_action_status": pending_action_status_orig, #
                    "recent_contextual_information": recent_context_orig, #
                }
                
                current_time_formatted_str_ui = datetime.datetime.now().strftime("%Y年%m月%d日 %H点%M分%S秒") #

                with st.spinner("思考生成中..."): #
                    # _generate_thought_from_llm 内部会构建 system_prompt
                    generated_thought_json, full_prompt_text_sent, system_prompt_sent = await temp_core_logic_for_prompt_ui._generate_thought_from_llm( #
                        llm_client=main_llm_client, #
                        current_state_for_prompt=current_state_for_prompt_ui_dict, #
                        current_time_str=current_time_formatted_str_ui, #
                        intrusive_thought_str=f"你突然有一个神奇的念头：{intrusive_thought_orig}" if intrusive_thought_orig else "" #
                    )

                st.session_state.last_full_prompt_sent_orig = full_prompt_text_sent #
                st.session_state.last_system_prompt_sent_orig = system_prompt_sent #

                if generated_thought_json: #
                    st.session_state.last_thought_json_orig = generated_thought_json #
                    st.success("思考生成成功！") #
                    st.subheader("主意识LLM结构化输出 (JSON):") #
                    st.json(st.session_state.last_thought_json_orig, expanded=True) #

                    if generated_thought_json.get("action_to_take"): #
                        st.info("LLM产生了行动意图！可以继续进行动作决策。") #
                        st.session_state.action_description_for_next_step = generated_thought_json["action_to_take"] #
                        st.session_state.action_motivation_for_next_step = generated_thought_json["action_motivation"] #
                        st.session_state.current_thought_context_for_next_step = generated_thought_json.get("think", "无特定思考上下文。") #
                    else: #
                        st.info("LLM没有产生明确的行动意图。") #
                else: #
                    st.error("LLM未能生成有效思考JSON。") #

                with st.expander("发送给主意识LLM的完整Prompt (原始UI)", expanded=False): #
                    st.text_area("System Prompt:", value=st.session_state.get("last_system_prompt_sent_orig",""), height=150, disabled=True, key="orig_system_prompt_display_ui") #
                    st.text_area("User Prompt:", value=st.session_state.get("last_full_prompt_sent_orig",""), height=400, disabled=True, key="orig_full_prompt_display_ui") #

            except Exception as e_gen: #
                st.error(f"生成思考时发生错误: {e_gen}", icon="💥") #
                st.exception(e_gen) #
                st.session_state.last_thought_json_orig = None #

    st.subheader("第二步：决策动作工具 🛠️") #
    if "last_thought_json_orig" in st.session_state and st.session_state.last_thought_json_orig \
       and st.session_state.last_thought_json_orig.get("action_to_take"): #
        st.success("检测到LLM有行动意图，现在可以进行动作决策！") #
        with st.form("action_decision_form_ui"): #
            action_desc = st.text_input("行动描述", st.session_state.get("action_description_for_next_step",""), key="act_desc_ui") #
            action_motive = st.text_input("行动动机", st.session_state.get("action_motivation_for_next_step",""), key="act_motive_ui") #
            current_thought_context_act = st.text_area("当前思考上下文", st.session_state.get("current_thought_context_for_next_step",""), height=100, key="act_thought_context_ui") #
            
            relevant_adapter_messages_context_mock_ui = "无相关外部消息或请求。" #

            submitted_action_decision = st.form_submit_button("决策动作！") #

        if submitted_action_decision: #
            if action_handler_instance and action_handler_instance.action_llm_client and hasattr(action_handler_instance, 'ACTION_DECISION_PROMPT_TEMPLATE') and hasattr(action_handler_instance, 'AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI'): # 确保 action_llm_client 和模板/schema 属性也存在 #
                st.info("正在进行动作决策...") #
                try:
                    tools_json_str_ui = json.dumps(action_handler_instance.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI, indent=2, ensure_ascii=False) #
                    decision_prompt_ui = action_handler_instance.ACTION_DECISION_PROMPT_TEMPLATE.format( #
                        tools_json_string=tools_json_str_ui, #
                        current_thought_context=current_thought_context_act, #
                        action_description=action_desc, #
                        action_motivation=action_motive, #
                        relevant_adapter_messages_context=relevant_adapter_messages_context_mock_ui, #
                    )

                    with st.spinner("决策LLM正在努力思考调用哪个工具..."): #
                        # ActionHandler 的决策通常不带特定的 system_prompt，除非其模板设计需要
                        decision_response = await action_handler_instance.action_llm_client.make_llm_request( # 使用 make_llm_request #
                            prompt=decision_prompt_ui,
                            system_prompt=None, # ActionHandler 的决策LLM通常只用用户prompt
                            is_stream=False,
                            tools=action_handler_instance.AVAILABLE_TOOLS_SCHEMA_FOR_GEMINI,
                        )
                    
                    if decision_response.get("error"): #
                        st.error(f"动作决策LLM调用失败: {decision_response.get('message')}") #
                        st.session_state.last_action_decision_json_orig = None #
                    else: #
                        tool_call_chosen: dict | None = None #
                        # 检查 make_llm_request 返回的结构，它应该直接包含 tool_calls
                        if decision_response.get("tool_calls") and isinstance(decision_response["tool_calls"], list) and len(decision_response["tool_calls"]) > 0: #
                             tool_call_chosen = decision_response["tool_calls"][0] #
                        elif decision_response.get("text"): # Fallback if tool_calls not directly in response but in text #
                            llm_text_output_ui: str = decision_response.get("text", "").strip() #
                            try:
                                if llm_text_output_ui.startswith("```json"): #
                                    llm_text_output_ui = llm_text_output_ui[7:-3].strip() #
                                elif llm_text_output_ui.startswith("```"): #
                                    llm_text_output_ui = llm_text_output_ui[3:-3].strip() #
                                parsed_text_json_ui: dict = json.loads(llm_text_output_ui) #
                                if ( #
                                    isinstance(parsed_text_json_ui, dict)
                                    and parsed_text_json_ui.get("tool_calls")
                                    and isinstance(parsed_text_json_ui["tool_calls"], list)
                                    and len(parsed_text_json_ui["tool_calls"]) > 0
                                ):
                                    tool_call_chosen = parsed_text_json_ui["tool_calls"][0] #
                            except json.JSONDecodeError: #
                                st.warning(f"决策LLM返回的文本不是有效JSON: {llm_text_output_ui[:100]}...") #

                        if tool_call_chosen: #
                            st.session_state.last_action_decision_json_orig = tool_call_chosen #
                            st.success("动作决策成功！") #
                            st.subheader("动作决策LLM输出 (JSON):") #
                            st.json(st.session_state.last_action_decision_json_orig, expanded=True) #
                            
                            tool_name_ui = tool_call_chosen.get("function", {}).get("name") #
                            tool_args_ui = tool_call_chosen.get("function", {}).get("arguments") #
                            st.info(f"决策结果：调用工具 **`{tool_name_ui}`**，参数：`{tool_args_ui}`") #
                        else: #
                            st.error("动作决策LLM未能提供有效工具调用或解析失败。") #

                except Exception as e_decide: #
                    st.error(f"决策动作时发生错误: {e_decide}", icon="💥") #
                    st.exception(e_decide) #
                    st.session_state.last_action_decision_json_orig = None #
            else: #
                st.error("ActionHandler 或其 LLM 客户端未初始化，无法决策动作。", icon="⚠️") #

    else: #
        st.info("需要先在“第一步”中生成带有行动意图的思考，才能进行动作决策。", icon="ℹ️") #

# --- 页面二：仿截图版本UI ---
async def show_new_ui() -> None: #
    st.header("仿截图版本 Prompt 测试器 (新) ✨") #
    st.caption("哼，这是按你那个花里胡哨的截图改的，是不是觉得很高级？") #

    if not st.session_state.llm_initialized: #
        st.warning("先把LLM客户端初始化了，笨蛋！不然我怎么干活？", icon="💢") #
        return
    # storage_initialized 检查的是数据库，对于仅 Prompt 测试的此页面可能不是严格必须，但最好有
    if not st.session_state.storage_initialized: #
        st.warning("数据库还没连接好，虽然这个页面主要玩弄Prompt，但最好还是先去连接一下数据库嘛！", icon="⚠️") #
        # return # 不强制返回，允许仅测试Prompt组合
    if st.session_state.core_initializer is None or \
       st.session_state.core_initializer.main_consciousness_llm_client is None or \
       st.session_state.core_initializer.root_cfg is None: # 确保 root_cfg 也可用
        st.warning("核心组件（LLM或Config）未完全初始化，请检查侧边栏配置并应用！", icon="⚠️") #
        return

    comps = st.session_state.new_ui_prompt_components #
    persona_cfg_new_ui = st.session_state.core_initializer.root_cfg.persona #


    st.subheader("🎨 Prompt 可配置部分") #
    st.caption("在这里一块一块地修改你的Prompt吧，本小懒猫已经帮你预设了一些值。") #

    tab_titles = [ #
        "👤人格面具", "📜任务规则", "💬上下文历史", "⚙️思考指引", 
        "😊心情", "⚡️侵入思维", "📐输出格式", "⏱️当前时间",
    ]
    tabs = st.tabs(tab_titles) #

    with tabs[0]: #
        comps["persona_block"] = st.text_area( #
            "Persona / System Prompt 主体", # 更改标签以更清晰
            value=comps["persona_block"], #
            height=200, #
            key="new_persona_ui", #
            help="定义AI的角色、背景、性格、说话风格等。这部分会主要构成 System Prompt。", #
        )
    with tabs[1]: #
        comps["task_rules_block"] = st.text_area( #
            "Task & Rules (用户指令的一部分)", # 更改标签
            value=comps["task_rules_block"], #
            height=200, #
            key="new_task_ui", #
            help="明确AI当前需要完成的具体任务和必须遵守的规则。这部分会是 User Prompt 的一部分。", #
        )
    # ... 其他 tabs 的内容保持不变，key 也使用 _ui 后缀 ...
    with tabs[2]: #
        comps["context_history_block"] = st.text_area( #
            "Context & History",
            value=comps["context_history_block"], #
            height=300, #
            key="new_context_ui", #
            help="提供相关的对话历史、之前的思考、行动结果等上下文信息。", #
        )
    with tabs[3]: #
        comps["thinking_guidance_block"] = st.text_area( #
            "Thinking Guidance",
            value=comps["thinking_guidance_block"], #
            height=100, #
            key="new_guidance_ui", #
            help="引导AI接下来的思考方向。", #
        )
    with tabs[4]: #
        comps["mood_block"] = st.text_input( #
            "Mood", value=comps["mood_block"], key="new_mood_ui", help="AI当前的心情状态。" #
        )
    with tabs[5]: #
        comps["intrusive_thought_block"] = st.text_input( #
            "Intrusive Thought",
            value=comps["intrusive_thought_block"], #
            key="new_intrusive_ui", #
            help="一个突然产生的、可能不相关的念头。", #
        )
    with tabs[6]: #
        comps["output_format_block"] = st.text_area( #
            "Output Format Requirement (JSON Schema)",
            value=comps["output_format_block"], #
            height=250, #
            key="new_output_format_ui", #
            help="严格定义模型输出的JSON结构。", #
        )
    with tabs[7]: #
        comps["current_time_block"] = st.text_input( #
            "Current Time (用于 System Prompt)", value=comps["current_time_block"], key="new_time_ui", help="传递给模型的当前时间字符串，会包含在 System Prompt 中。" # 更改标签和帮助文本 #
        )


    st.session_state.new_ui_prompt_components = comps #

    st.markdown("---") #
    if st.button("🧠 生成思考 (新版UI)", type="primary", key="new_generate_thought_btn_ui"): #
        if st.session_state.core_initializer and st.session_state.core_initializer.main_consciousness_llm_client: #
            bot_name_val_new_ui = persona_cfg_new_ui.bot_name #

            # 构建 System Prompt (人格面具 + 当前时间)
            final_system_prompt_for_llm_ui = f"当前时间：{comps['current_time_block']}\n你是{bot_name_val_new_ui}；\n{comps['persona_block']}" #
            if persona_cfg_new_ui.profile and persona_cfg_new_ui.profile.strip(): # 如果 profile 有内容且不只是空格
                 final_system_prompt_for_llm_ui += f"\n{persona_cfg_new_ui.profile}"


            # 构建 User Prompt (任务规则 + 上下文历史 + 思考指引 + 心情 + 侵入思维 + 输出格式要求)
            # 使用 ThoughtPromptBuilder 的模板作为基础，替换其中的占位符
            # 这确保了即使模板结构复杂，我们也能正确填充
            try: #
                # 从 ThoughtPromptBuilder.PROMPT_TEMPLATE 中提取 JSON Schema 前后的部分
                template_parts_ui = ThoughtPromptBuilder.PROMPT_TEMPLATE.split("严格以json字段输出：", 1) # <-- 修改
                part1_before_json_schema_ui = template_parts_ui[0] + "严格以json字段输出：" #
                schema_and_suffix_parts_ui = template_parts_ui[1].split("请输出你的思考 JSON：", 1) #
                part2_after_json_schema_ui = "请输出你的思考 JSON：" + schema_and_suffix_parts_ui[1] #
                
                # 用UI中的输出格式块替换原始模板中的JSON Schema部分
                user_prompt_template_with_custom_schema = ( #
                    part1_before_json_schema_ui + f"\n{comps['output_format_block']}\n" + part2_after_json_schema_ui #
                )
            except IndexError: #
                st.warning("无法按预期分割原始Prompt模板以插入自定义JSON Schema，将使用原始模板结构（自定义Schema可能未生效）。") #
                user_prompt_template_with_custom_schema = ThoughtPromptBuilder.PROMPT_TEMPLATE # <-- 修改

            # 填充 User Prompt 模板
            # 注意：ThoughtPromptBuilder.PROMPT_TEMPLATE 中的 {current_time}, {bot_name}, {persona_description}, {persona_profile}
            # 这些已经移到 System Prompt 中了，所以在 format 用户 prompt 时，它们不应该再被期望。
            # 我们需要确保 user_prompt_template_with_custom_schema 只包含 User Prompt 该有的占位符。
            # ThoughtPromptBuilder.PROMPT_TEMPLATE 本身就只包含该有的 User Prompt 占位符。
            
            # 从 comps 中获取行动结果、待处理行动状态等，如果它们是动态的
            # 为简化，暂时使用 AIStateManager.INITIAL_STATE 中的值
            action_result_info_for_user_prompt = AIStateManager.INITIAL_STATE["action_result_info"] # <-- 修改
            pending_action_status_for_user_prompt = AIStateManager.INITIAL_STATE["pending_action_status"] # <-- 修改
            previous_thinking_for_user_prompt = AIStateManager.INITIAL_STATE["previous_thinking"] # 假设这些也是从历史记录中来 # <-- 修改

            final_user_prompt_for_llm = user_prompt_template_with_custom_schema.format( #
                current_task_info=comps["task_rules_block"],  # 这是UI中的 "Task & Rules" #
                action_result_info=action_result_info_for_user_prompt, 
                pending_action_status=pending_action_status_for_user_prompt, 
                recent_contextual_information=comps["context_history_block"], # 这是UI中的 "Context & History" #
                master_chat_context="你和主人之间没有最近的聊天记录。", # <-- 新增 master_chat_context
                previous_thinking=previous_thinking_for_user_prompt, 
                mood=f"你现在的心情大概是：{comps['mood_block']}", # 这是UI中的 "Mood" #
                thinking_guidance=f"经过你上一轮的思考，你目前打算的思考方向是：{comps['thinking_guidance_block']}", # 这是UI中的 "Thinking Guidance" #
                intrusive_thought=f"你突然有一个神奇的念头：{comps['intrusive_thought_block']}" if comps["intrusive_thought_block"] else "", # 这是UI中的 "Intrusive Thought" #
            )


            with st.expander("发送给主意识LLM的完整Prompt (新版UI组合结果)", expanded=False): #
                st.text_area("System Prompt:", value=final_system_prompt_for_llm_ui, height=150, disabled=True, key="new_final_system_prompt_display_ui") #
                st.text_area("User Prompt:", value=final_user_prompt_for_llm, height=400, disabled=True, key="new_final_user_prompt_display_ui") #

            with st.spinner("新版UI的LLM也在努力思考中...喵..."): #
                try:
                    response_data_new_ui = await st.session_state.core_initializer.main_consciousness_llm_client.make_llm_request( #
                        prompt=final_user_prompt_for_llm, #
                        system_prompt=final_system_prompt_for_llm_ui, #
                        is_stream=False #
                    )
                    
                    if response_data_new_ui.get("error"): #
                        st.error(f"LLM调用失败: {response_data_new_ui.get('message')}") #
                        st.session_state.last_thought_json_new = None #
                        st.session_state.last_raw_response_new = f"LLM Error: {response_data_new_ui.get('message')}" #
                    else: #
                        raw_text_new_ui = response_data_new_ui.get("text", "") #
                        st.session_state.last_raw_response_new = raw_text_new_ui #
                        json_to_parse_new_ui = raw_text_new_ui.strip() #
                        if json_to_parse_new_ui.startswith("```json"): #
                            json_to_parse_new_ui = json_to_parse_new_ui[7:-3].strip() #
                        elif json_to_parse_new_ui.startswith("```"): #
                            json_to_parse_new_ui = json_to_parse_new_ui[3:-3].strip() #
                        try:
                            st.session_state.last_thought_json_new = json.loads(json_to_parse_new_ui) #
                        except json.JSONDecodeError as e: #
                            st.error(f"解析LLM的JSON响应失败 (新版UI): {e}") #
                            st.session_state.last_thought_json_new = None #
                except Exception as e_new_call: #
                    st.error(f"在新版UI生成思考时发生错误: {e_new_call}", icon="💥") #
                    st.exception(e_new_call) #
                    st.session_state.last_thought_json_new = None #
                    st.session_state.last_raw_response_new = str(e_new_call) #

        if "last_raw_response_new" in st.session_state: #
            with st.expander("主意识LLM的原始回复 (新版UI)", expanded=False): #
                st.text_area( #
                    "",
                    value=st.session_state.last_raw_response_new, #
                    height=200, #
                    disabled=True, #
                    key="new_raw_output_display_ui", #
                )

        if "last_thought_json_new" in st.session_state and st.session_state.last_thought_json_new: #
            st.subheader("主意识LLM结构化输出 (JSON) (新版UI):") #
            st.json(st.session_state.last_thought_json_new, expanded=True) #
            if st.session_state.last_thought_json_new.get("action_to_take"): #
                st.success("耶！LLM又想搞事情了 (新版UI)！可以去测动作决策。", icon="🎉") #
            else: #
                st.info("LLM这次很乖，没啥行动意图 (新版UI)。", icon="🧸") #


# --- 主应用逻辑 ---
def main_app() -> None: #
    st.set_page_config(layout="wide", page_title="小懒猫 Prompt 测试乐园 V2.2") #
    initialize_session_state() #
    llm_configuration_sidebar() #

    page_options = {"原始版本测试器 (旧)": show_original_ui, "仿截图版本测试器 (新)": show_new_ui} #

    with st.sidebar: #
        st.markdown("---") #
        st.title("页面导航 NekoNavi™ V2.1") #
        chosen_page_title = st.radio( #
            "选择一个测试页面玩玩吧:",
            options=list(page_options.keys()), #
            key="page_selector_radio_ui", #
        )
    st.session_state.current_page = chosen_page_title #

    if st.session_state.current_page in page_options: #
        page_function = page_options[st.session_state.current_page]
        # 对于Streamlit，如果页面函数是异步的，需要用asyncio.run包装
        # 或者让Streamlit支持异步回调（但它本身是同步执行模型的）
        # 为了简单，如果page_function是async def，则使用asyncio.run
        if asyncio.iscoroutinefunction(page_function):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(page_function())
            finally:
                loop.close()
        else: # 如果是同步函数（虽然这里都是async def）
            page_function()
    else: #
        # 默认显示原始UI
        default_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(default_loop)
        try:
            default_loop.run_until_complete(show_original_ui()) #
        finally:
            default_loop.close()


if __name__ == "__main__": #
    main_app() #
