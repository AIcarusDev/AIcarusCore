# test_harness_gui.py
import asyncio

import streamlit as st
from src.aicos.models import Application

# --- 导入所有需要的 AIC-OS 核心服务 ---
from src.bootstrap.builder import ServiceBuilder
from src.core_logic.decision_dispatcher import process_aicos_decision
from src.focus_chat_mode.chat_session_manager import ChatSessionManager

# --- Streamlit 页面配置 ---
st.set_page_config(layout="wide", page_title="AIC-OS Interactive Test Harness")

# --- 核心：会话状态初始化 ---
# Streamlit 每次交互都会重跑脚本，所以我们必须用 session_state 来持久化我们的服务实例
async def initialize_session_state() -> None:
    """初始化会话状态."""
    if 'services_initialized' in st.session_state:
        return

    st.toast("首次运行，正在初始化所有核心服务...")
    print("首次运行，正在初始化所有核心服务...")

    builder = ServiceBuilder()
    container = await builder.build_container()

    # 模拟动态依赖注入
    # 1. 从容器中获取所有需要的服务
    llm_client = container.focused_chat_llm_client
    deliberation_llm = container.deliberation_llm_client
    event_storage = container.event_storage_service
    action_handler = container.action_handler
    interrupter = container.intelligent_interrupter
    entity_service = container.entity_graph_service
    thought_storage = container.thought_storage_service
    internal_info_builder = container.internal_info_builder
    core_logic = container.core_logic

    # 2. 模拟安检后的 bot_ids (从我们的 seed_database.py 中获取)
    self_bot_ids_map = {"qq": "10001"}

    # 3. 创建 ChatSessionManager 实例
    chat_session_manager = ChatSessionManager(
        config=container.config.focus_chat_mode,
        llm_client=llm_client,
        deliberation_llm_client=deliberation_llm,
        event_storage=event_storage,
        action_handler=action_handler,
        self_bot_ids_map=self_bot_ids_map,
        intelligent_interrupter=interrupter,
        entity_graph_service=entity_service,
        thought_storage_service=thought_storage,
        internal_info_builder=internal_info_builder,
        core_logic=core_logic,
    )

    # 4. 将 CSM 实例回填到需要它的服务中
    action_handler.chat_session_manager = chat_session_manager
    core_logic.chat_session_manager = chat_session_manager

    # 5. 将所有需要的服务实例存储在 session_state 中
    st.session_state.window_manager = container.window_manager
    st.session_state.application_manager = container.application_manager
    st.session_state.aicos_state_generator = container.aicos_state_generator
    st.session_state.schema_builder = container.schema_builder
    st.session_state.action_handler = action_handler # 使用我们刚刚更新过的 action_handler
    st.session_state.chat_session_manager = chat_session_manager # 存储 csm
    st.session_state.state_manager = container.state_manager

    st.session_state.application_manager.load_installed_apps([
        Application(id="app-001", name="qq", title="QQ")
    ])

    st.session_state.services_initialized = True
    st.toast("服务初始化完成！", icon="✅")
    print("服务初始化完成！")

async def main() -> None:
    """主函数."""
    st.title("🤖 AIC-OS Interactive Test Harness")
    st.caption("在这里，你就是AI。观察、决策、行动！")

    # 确保服务已初始化
    await initialize_session_state()

    # 从 session_state 中获取服务实例
    wm = st.session_state.window_manager
    am = st.session_state.application_manager
    state_gen = st.session_state.aicos_state_generator
    schema_builder = st.session_state.schema_builder
    action_handler = st.session_state.action_handler
    csm = st.session_state.chat_session_manager
    state_manager = st.session_state.state_manager

    # --- 核心渲染循环 ---
    # 1. 生成当前状态的 XML 和 UI 映射
    xml_state, ui_mapping = await state_gen.build_current_state()

    # 2. 生成当前可用的动作 Schema
    action_schema = schema_builder.build_response_schema(ui_mapping)

    # --- 布局 ---
    col1, col2 = st.columns([2, 1])

    # --- 左侧列：显示 AIC-OS 状态 ---
    with col1:
        st.subheader("🖥️ AIC-OS State (What you 'see')")
        st.code(xml_state, language='xml', line_numbers=True)

    # --- 右侧列：显示可用动作并允许交互 ---
    with col2:
        st.subheader("⚡ Available Actions (What you can 'do')")

        external_actions = action_schema.get(
            "properties", {}
            ).get("external_action", {}).get("properties", {})

        if not external_actions:
            st.info("当前没有可用的外部动作。")

        # --- 渲染 Click 动作 ---
        if 'click' in external_actions:
            with st.expander("🖱️ Click Actions", expanded=True):
                clickable_ids = external_actions['click']['properties']['target_id']['enum']
                if clickable_ids:
                    selected_click_id = st.radio(
                        "Select a target to click:",
                        clickable_ids, key="click_target"
                    )
                    if st.button("Perform Click", key=f"click_btn_{selected_click_id}"):
                        decision_json = {
                            "external_action": {
                                "click": {
                                    "target_id": selected_click_id,
                                    "motivation": "User initiated test click"
                                }
                            }
                        }
                        await process_aicos_decision(
                            decision_json,
                            ui_mapping,
                            wm,
                            am,
                            action_handler,
                            csm,
                            state_manager
                        )
                        st.rerun()
                else:
                    st.write("No clickable items available.")

        # --- 渲染 Double Click 动作 ---
        if 'double_click' in external_actions:
            with st.expander("💨 Double Click Actions", expanded=True):
                double_clickable_ids = (
                    external_actions['double_click']['properties']['target_id']['enum']
                )
                if double_clickable_ids:
                    selected_double_click_id = st.radio(
                        "Select a target to double click:",
                        double_clickable_ids,
                        key="double_click_target"
                    )
                    if st.button(
                        "Perform Double Click",
                        key=f"double_click_btn_{selected_double_click_id}"
                        ):
                        decision_json = {
                            "external_action": {
                                "double_click": {
                                    "target_id": selected_double_click_id,
                                    "motivation": "User initiated test double click"
                                }
                            }
                        }
                        await process_aicos_decision(
                            decision_json,
                            ui_mapping,
                            wm,
                            am,
                            action_handler,
                            csm,
                            state_manager
                        )
                        st.rerun()
                else:
                    st.write("No double-clickable items available.")

        # --- 渲染 Send Message 动作 ---
        if 'send_message' in external_actions:
            with st.expander("💬 Send Message Actions", expanded=True):
                chat_window_ids = (
                    external_actions['send_message']['properties']['target_window_id']['enum']
                    )
                if chat_window_ids:
                    selected_window_id = st.selectbox("Select chat window:", chat_window_ids)
                    message_content = st.text_area(
                        "Message to send:",
                        key=f"msg_content_{selected_window_id}"
                        )
                    if st.button("Send Message", key=f"send_btn_{selected_window_id}"):
                        decision_json = {
                            "external_action": {
                                "send_message": {
                                    "target_window_id": selected_window_id,
                                    "steps": [{
                                        "command": "text",
                                        "params": {"content": message_content}
                                    }],
                                    "motivation": "User initiated test message"
                                }
                            }
                        }
                        await process_aicos_decision(
                            decision_json, ui_mapping, wm, am, action_handler, csm, state_manager
                        )
                        st.rerun()
                else:
                    st.write("No active chat windows to send messages to.")

if __name__ == "__main__":
    # 使用 asyncio.run() 来启动异步的 Streamlit 应用
    asyncio.run(main())
