# 文件路径: test_harness_gui.py

import asyncio
import uuid

# --- [核心修改] 导入新的依赖 ---
import seed_database
import streamlit as st
from src.bootstrap.builder import ServiceBuilder, ServiceContainer
from src.mind import action_orchestrator
from src.os.apps.registry import platform_builder_registry
from src.os.models import Application

# --- Streamlit 页面配置 ---
st.set_page_config(layout="wide", page_title="AIC-OS Interactive Test Harness")


# --- [核心重构] 模拟完整的冷启动和安检流程 ---
async def initialize_session_state() -> None:
    """初始化会话状态，模拟一次完整的冷启动流程."""
    if "services_initialized" in st.session_state:
        return

    st.toast("首次运行，正在初始化所有核心服务...")
    print("首次运行，正在初始化所有核心服务...")

    # 1. 构建服务容器
    builder = ServiceBuilder()
    container: ServiceContainer = await builder.build_container()

    # 2. 清空并注入种子数据
    st.toast("正在清理并填充数据库...")
    await seed_database.clear_database(container)
    await seed_database.seed_data(container.entity_graph_service, container.event_storage_service)
    st.toast("数据库准备就绪！", icon="🗄️")

    # 3. 加载已安装的应用
    container.application_manager.load_installed_apps(
        [Application(id="app-001", name="qq", title="QQ")]
    )

    # 4. 模拟 QQ 适配器连接并执行安检
    st.toast("模拟QQ适配器连接并执行安检...")
    qq_builder = platform_builder_registry.get_builder("qq")
    if qq_builder:
        await qq_builder.run_on_connect_inspection(container)
        st.toast("QQ安检完成！", icon="🛡️")
    else:
        st.error("严重错误：找不到 QQ 应用构建器！")
        return
    container.aicos_state_generator.is_connected = True
    st.toast("AIC-OS 已连接！", icon="🔌")

    # 5. 将完全准备好的容器存入会话
    st.session_state.container = container
    st.session_state.services_initialized = True
    st.toast("服务初始化完成！", icon="✅")
    print("服务初始化完成！")


# --- [核心新增] 新的动作分发辅助函数 ---
async def _dispatch_harness_action(
    decision_json: dict,
    ui_mapping: dict,
    container: ServiceContainer
) -> None:
    """将测试工具中的交互转换为对 action_orchestrator 的调用."""
    # 为模拟动作创建一个临时的 thought_key
    thought_key = f"harness-thought-{uuid.uuid4().hex}"
    await action_orchestrator.orchestrate_action(
        decision_json=decision_json,
        ui_mapping=ui_mapping,
        container=container,
        thought_key=thought_key,
    )


async def main() -> None:
    """主函数."""
    st.title("🤖 AIC-OS Interactive Test Harness (Refactored)")
    st.caption("在这里，你就是AI。观察、决策、行动！")

    await initialize_session_state()

    container: ServiceContainer = st.session_state.container

    # --- [核心重构] 使用正确的接口获取状态和 Schema ---
    prompt_components, _, ui_mapping, _ = await container.prompt_builder.build_prompts_components(
        last_external_info_snapshot=None
    )
    xml_state = prompt_components.user_prompt_blocks.get(
        "external_info_block", "<error>渲染失败</error>"
    )
    action_schema = prompt_components.response_schema

    # --- 布局 ---
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("🖥️ AIC-OS State (What you 'see')")
        st.code(xml_state, language="xml", line_numbers=True)

    with col2:
        st.subheader("⚡ Available Actions (What you can 'do')")

        # --- [核心重构] 从新的 Schema 结构中解析动作 ---
        action_props = action_schema.get("properties", {}).get("action", {}).get("properties", {})
        external_actions = action_props.get("external", {}).get("properties", {})
        innate_actions = external_actions.get("innate", {}).get("properties", {})
        if innate_actions:
            with st.expander("🧠 Innate Abilities", expanded=True):
                if "connect" in innate_actions and st.button(
                    "Connect to AIC-OS",
                    key="connect_btn"
                    ):
                    decision_json = {
                        "action": {
                            "external": {
                                "innate": {
                                    "connect": {
                                        "device_name": "AIC-OS",
                                        "motivation": "Harness user command"
                                    }
                                }
                            }
                        }
                    }
                    await _dispatch_harness_action(decision_json, ui_mapping, container)
                    st.rerun()
        aicos_interactions = external_actions.get("AIC-OS", {}).get("properties", {})
        base_interactions = aicos_interactions.get("base", {}).get("properties", {})
        qq_interactions = aicos_interactions.get("qq", {}).get("properties", {})

        if not base_interactions and not qq_interactions:
            st.info("当前没有可用的外部动作。")

        # --- [核心重构] 动作按钮逻辑 ---
        if "click" in base_interactions:
            with st.expander("🖱️ Click Actions", expanded=True):
                clickable_ids = base_interactions["click"]["properties"]["target_id"]["enum"]
                if clickable_ids:
                    for click_id in clickable_ids:
                        if st.button(f"Click: {click_id}", key=f"click_btn_{click_id}"):
                            decision_json = {
                                "action": {
                                    "external": {
                                        "AIC-OS": {
                                            "base": {"click": {"target_id": click_id}}
                                        }
                                    }
                                }
                            }
                            await _dispatch_harness_action(decision_json, ui_mapping, container)
                            st.rerun()
                else:
                    st.write("No clickable items available.")

        if "double_click" in base_interactions:
            with st.expander("💨 Double Click Actions"):
                double_clickable_ids = base_interactions["double_click"]["properties"]["target_id"]["enum"]  # noqa: E501
                if double_clickable_ids:
                    for dbl_click_id in double_clickable_ids:
                        if st.button(f"Double Click: {dbl_click_id}", key=f"dbl_click_btn_{dbl_click_id}"):  # noqa: E501
                            decision_json = {
                                "action": {
                                    "external": {
                                        "AIC-OS": {
                                            "base": {"double_click": {"target_id": dbl_click_id}}
                                        }
                                    }
                                }
                            }
                            await _dispatch_harness_action(decision_json, ui_mapping, container)
                            st.rerun()
                else:
                    st.write("No double-clickable items available.")

        if "send_message" in qq_interactions:
            with st.expander("💬 Send Message Actions"):
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
                            "action": {
                                "external": {
                                    "AIC-OS": {
                                        "qq": {
                                            "send_message": {
                                                "target_window_id": selected_window_id,
                                                "steps": [{"command": "text", "params": {"content": message_content}}],  # noqa: E501
                                                "motivation": "User initiated test message"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        await _dispatch_harness_action(decision_json, ui_mapping, container)
                        st.rerun()
                else:
                    st.write("No active chat windows.")

if __name__ == "__main__":
    asyncio.run(main())
