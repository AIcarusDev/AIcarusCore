# test_harness_gui.py
import asyncio

import streamlit as st
from AIcarusCore.src.os.ui_dispatcher import process_aicos_decision

# --- 导入所有需要的 AIC-OS 核心服务 ---
from src.bootstrap.builder import ServiceBuilder
from src.os.models import Application

# --- Streamlit 页面配置 ---
st.set_page_config(layout="wide", page_title="AIC-OS Interactive Test Harness")


# --- 核心：会话状态初始化 ---
# Streamlit 每次交互都会重跑脚本，所以我们必须用 session_state 来持久化我们的服务实例
async def initialize_session_state() -> None:
    """初始化会话状态."""
    if "services_initialized" in st.session_state:
        return

    st.toast("首次运行，正在初始化所有核心服务...")
    print("首次运行，正在初始化所有核心服务...")

    builder = ServiceBuilder()
    container = await builder.build_container()

    # [核心修改] 将完整的容器实例存储到 session_state 中
    st.session_state.container = container

    # 为了向后兼容或方便直接访问，仍然可以将部分常用服务单独存储
    st.session_state.window_manager = container.window_manager
    st.session_state.application_manager = container.application_manager
    st.session_state.aicos_state_generator = container.aicos_state_generator
    st.session_state.action_handler = container.action_handler
    st.session_state.state_manager = container.state_manager

    # 模拟安检后的 bot_ids (从我们的 seed_database.py 中获取)
    self_bot_ids_map = {"qq": "10001"}
    container.application_manager.set_self_bot_ids_map(self_bot_ids_map)

    # 加载已安装的应用
    container.application_manager.load_installed_apps(
        [Application(id="app-001", name="qq", title="QQ")]
    )

    st.session_state.services_initialized = True
    st.toast("服务初始化完成！", icon="✅")
    print("服务初始化完成！")


async def main() -> None:
    """主函数."""
    st.title("🤖 AIC-OS Interactive Test Harness")
    st.caption("在这里，你就是AI。观察、决策、行动！")

    # 确保服务已初始化
    await initialize_session_state()

    # [核心修改] 从 session_state 中获取完整的容器
    container = st.session_state.container
    # 从容器中获取需要的服务实例
    state_gen = container.aicos_state_generator

    # --- 核心渲染循环 ---
    # 1. 生成当前状态的 XML 和 UI 映射
    xml_state, ui_mapping = await state_gen.build_current_state()

    # 2. 生成当前可用的动作 Schema
    # 注意：在新架构中，schema是由ThoughtPromptBuilder在内部构建的，
    # test_harness_gui 主要是为了模拟UI交互，所以我们直接从 aicos_state_generator 获取UI映射
    # 并手动构建一个简化的 action_schema 用于显示
    action_schema = container.prompt_builder._build_response_schema(ui_mapping)


    # --- 布局 ---
    col1, col2 = st.columns([2, 1])

    # --- 左侧列：显示 AIC-OS 状态 ---
    with col1:
        st.subheader("🖥️ AIC-OS State (What you 'see')")
        st.code(xml_state, language="xml", line_numbers=True)

    # --- 右侧列：显示可用动作并允许交互 ---
    with col2:
        st.subheader("⚡ Available Actions (What you can 'do')")

        # [核心修改] 从完整的 schema 中解析动作
        aicos_actions = action_schema.get(
            "properties",{}).get(
                "external_action", {}
            ).get("properties", {}).get("AIC-OS", {}).get("properties", {})
        base_interactions = aicos_actions.get("base", {}).get("properties", {})
        qq_interactions = aicos_actions.get("qq", {}).get("properties", {})

        if not base_interactions and not qq_interactions:
            st.info("当前没有可用的外部动作。")

        # --- 渲染 Click 动作 ---
        if "click" in base_interactions:
            with st.expander("🖱️ Click Actions", expanded=True):
                clickable_ids = base_interactions["click"]["properties"]["target_id"]["enum"]
                if clickable_ids:
                    selected_click_id = st.radio(
                        "Select a target to click:", clickable_ids, key="click_target"
                    )
                    if st.button("Perform Click", key=f"click_btn_{selected_click_id}"):
                        decision_json = {
                            "external_action": {
                                "AIC-OS": {
                                    "base": {
                                        "click": {
                                            "target_id": selected_click_id,
                                            "motivation": "User initiated test click",
                                        }
                                    }
                                }
                            }
                        }
                        # 调用时只传递 container
                        await process_aicos_decision(
                            decision_json,
                            ui_mapping,
                            container,
                        )
                        st.rerun()
                else:
                    st.write("No clickable items available.")

        # --- 渲染 Double Click 动作 ---
        if "double_click" in base_interactions:
            with st.expander("💨 Double Click Actions", expanded=True):
                double_clickable_ids = base_interactions[
                    "double_click"
                    ]["properties"]["target_id"]["enum"]
                if double_clickable_ids:
                    selected_double_click_id = st.radio(
                        "Select a target to double click:",
                        double_clickable_ids,
                        key="double_click_target",
                    )
                    if st.button(
                        "Perform Double Click", key=f"double_click_btn_{selected_double_click_id}"
                    ):
                        decision_json = {
                            "external_action": {
                                "AIC-OS": {
                                    "base": {
                                        "double_click": {
                                            "target_id": selected_double_click_id,
                                            "motivation": "User initiated test double click",
                                        }
                                    }
                                }
                            }
                        }
                        # [核心修改] 调用时只传递 container
                        await process_aicos_decision(
                            decision_json,
                            ui_mapping,
                            container,
                        )
                        st.rerun()
                else:
                    st.write("No double-clickable items available.")

        # --- 渲染 Send Message 动作 ---
        if "send_message" in qq_interactions:
            with st.expander("💬 Send Message Actions", expanded=True):
                chat_window_ids = qq_interactions["send_message"]["properties"][
                    "target_window_id"
                ]["enum"]
                if chat_window_ids:
                    selected_window_id = st.selectbox("Select chat window:", chat_window_ids)
                    message_content = st.text_area(
                        "Message to send:", key=f"msg_content_{selected_window_id}"
                    )
                    if st.button("Send Message", key=f"send_btn_{selected_window_id}"):
                        decision_json = {
                            "external_action": {
                                "AIC-OS": {
                                    "qq": {
                                        "send_message": {
                                            "target_window_id": selected_window_id,
                                            "steps": [
                                                {
                                                    "command": "text",
                                                    "params": {"content": message_content}
                                                }
                                            ],
                                            "motivation": "User initiated test message",
                                        }
                                    }
                                }
                            }
                        }
                        # [核心修改] 调用时只传递 container
                        await process_aicos_decision(
                            decision_json,
                            ui_mapping,
                            container,
                        )
                        st.rerun()
                else:
                    st.write("No active chat windows to send messages to.")


if __name__ == "__main__":
    # 使用 asyncio.run() 来启动异步的 Streamlit 应用
    asyncio.run(main())
