# 文件路径: test_harness_gui.py

import asyncio
import uuid

import seed_database
import streamlit as st
from src.bootstrap.builder import ServiceBuilder, ServiceContainer
from src.cognitive_cycle import CognitiveCycle  # 导入新的认知周期

# --- Streamlit 页面配置 ---
st.set_page_config(layout="wide", page_title="AIC-OS Interactive Test Harness")


# --- 初始化 ---
async def initialize_session_state() -> None:
    """初始化会话状态，模拟一次完整的冷启动流程."""
    if "services_initialized" in st.session_state:
        return

    st.toast("首次运行，正在初始化所有核心服务...")
    print("首次运行，正在初始化所有核心服务...")

    builder = ServiceBuilder()
    container: ServiceContainer = await builder.build_container()

    st.toast("正在清理并填充数据库...")
    await seed_database.clear_database(container)
    await seed_database.seed_data(container.entity_graph_service, container.event_storage_service)
    st.toast("数据库准备就绪！", icon="🗄️")

    qq_builder = container.application_manager.get_builder_by_name("qq")
    if qq_builder:
        await qq_builder.run_on_connect_inspection(container)

    container.aicos_state_generator.is_connected = True
    st.toast("AIC-OS 已连接！", icon="🔌")

    # [新] 将 CognitiveCycle 实例化并存入 session
    st.session_state.cognitive_cycle = CognitiveCycle(container)
    st.session_state.container = container
    st.session_state.services_initialized = True
    st.toast("服务初始化完成！", icon="✅")
    print("服务初始化完成！")


# --- 动作分发 ---
async def _dispatch_harness_action(
    decision_json: dict, ui_mapping: dict, cognitive_cycle: CognitiveCycle
) -> None:
    """将测试工具中的交互转换为对 CognitiveCycle._orchestrate_action 的调用."""
    thought_key = f"harness-thought-{uuid.uuid4().hex}"
    await cognitive_cycle._orchestrate_action(
        decision_json=decision_json,
        ui_mapping=ui_mapping,
        thought_key=thought_key,
    )

# --- UI渲染辅助函数 ---
def render_file_explorer_actions(
    container: ServiceContainer,
    cognitive_cycle: "CognitiveCycle",
    ui_mapping: dict,
    fe_actions: dict,
) -> None:
    """渲染文件资源管理器的所有动作UI."""
    with st.expander("🗂️ File Explorer Actions", expanded=True):
        command_props = fe_actions.get("properties", {}).get("command", {}).get("properties", {})

        # --- Create Action ---
        if "create" in command_props:
            st.markdown("---")
            st.markdown("**Create File/Folder**")
            create_type = st.radio("Type", ["file", "folder"], key="create_type", horizontal=True)
            create_path = st.text_input(
                "Path (e.g., /desktop/new.txt or /desktop/new_folder/)", key="create_path"
            )
            create_content = st.text_area("Initial Content (for files)", key="create_content")

            if st.button("Create", key="create_btn"):
                command = {"create": {"type": create_type, "path": create_path}}
                if create_type == "file" and create_content:
                    command["create"]["content"] = create_content

                decision = {
                    "action": {
                        "external": {
                            "AIC-OS": {
                                "file_explorer": {
                                    "command": command,
                                    "motivation": "Harness command",
                                }
                            }
                        }
                    }
                }
                asyncio.run(_dispatch_harness_action(decision, ui_mapping, cognitive_cycle))
                st.rerun()

        # --- Delete Action ---
        if "delete" in command_props:
            st.markdown("---")
            st.markdown("**Delete File/Folder**")
            delete_enum = command_props["delete"]["properties"]["item_id"]["enum"]
            if delete_enum:
                delete_target = st.selectbox(
                    "Visible Item to Delete", delete_enum, key="delete_target"
                )
                if st.button("Delete", key="delete_btn"):
                    command = {"delete": {"item_id": delete_target}}
                    decision = {"action": {"external": {"AIC-OS": {"file_explorer": {
                        "command": command,
                        "motivation": "Harness command"
                    }}}}}
                    asyncio.run(_dispatch_harness_action(decision, ui_mapping, cognitive_cycle))
                    st.rerun()
            else:
                st.info("No visible items to delete.")

        # --- Rename Action ---
        if "rename" in command_props:
            st.markdown("---")
            st.markdown("**Rename File/Folder**")
            rename_enum = command_props["rename"]["properties"]["item_id"]["enum"]
            if rename_enum:
                rename_target = st.selectbox(
                    "Visible Item to Rename", rename_enum, key="rename_target"
                )
                new_name = st.text_input("New Name (incl. extension)", key="rename_new_name")
                if st.button("Rename", key="rename_btn"):
                    command = {"rename": {"item_id": rename_target, "new_name": new_name}}
                    decision = {
                        "action": {
                            "external": {
                                "AIC-OS": {
                                    "file_explorer": {
                                        "command": command,
                                        "motivation": "Harness command",
                                    }
                                }
                            }
                        }
                    }
                    asyncio.run(_dispatch_harness_action(decision, ui_mapping, cognitive_cycle))
                    st.rerun()
            else:
                st.info("No visible items to rename.")

def render_text_editor_actions(
    container: ServiceContainer,
    cognitive_cycle: "CognitiveCycle",
    ui_mapping: dict,
    te_actions: dict,
) -> None:
    """渲染文本编辑器动作UI."""
    with st.expander("📝 Text Editor Actions", expanded=True):
        edit_props = te_actions.get("properties", {}).get("edit", {})
        if edit_props:
            st.markdown("**Edit Active File**")
            edit_content = st.text_area("New Content", key="edit_content")
            edit_append = st.checkbox("Append to file (otherwise overwrite)", key="edit_append")
            if st.button("Save Content", key="edit_btn"):
                command = {"edit": {"content": edit_content, "append": edit_append}}
                decision = {"action": {"external": {"AIC-OS": {"text_editor": command,
                                                                "motivation": "Harness command"}}}}
                asyncio.run(_dispatch_harness_action(decision, ui_mapping, cognitive_cycle))
                st.rerun()
        else:
            st.info("Open a file in the text editor to enable actions.")


async def main() -> None:
    """主函数."""
    st.title("🤖 AIC-OS Interactive Test Harness (File System Refactor)")
    st.caption("在这里，你就是AI。观察、决策、行动！")

    await initialize_session_state()

    container: ServiceContainer = st.session_state.container
    cognitive_cycle: CognitiveCycle = st.session_state.cognitive_cycle

    prompt_components, _, ui_mapping, _ = await container.prompt_builder.build_prompts_components(
        last_external_info_snapshot=None
    )
    xml_state = prompt_components.user_prompt_blocks.get(
        "external_info_block", "<error>渲染失败</error>"
    )
    action_schema = prompt_components.response_schema

    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("🖥️ AIC-OS State (What you 'see')")
        st.code(xml_state, language="xml", line_numbers=True)

    with col2:
        st.subheader("⚡ Available Actions (What you can 'do')")

        action_props = action_schema.get("properties", {}).get("action", {}).get("properties", {})
        external_actions = action_props.get("external", {}).get("properties", {})
        aicos_interactions = external_actions.get("AIC-OS", {}).get("properties", {})

        # --- Base UI Actions ---
        base_interactions = aicos_interactions.get("base", {}).get("properties", {})
        if base_interactions and "double_click" in base_interactions:
            with st.expander("🖱️ UI Interactions (Click/Double Click)", expanded=True):
                double_clickable_ids = base_interactions["double_click"]["properties"][
                    "target_id"
                ]["enum"]
                if double_clickable_ids:
                    for dbl_click_id in double_clickable_ids:
                        if st.button(
                            f"Double Click: {dbl_click_id}", key=f"dbl_click_btn_{dbl_click_id}"
                        ):
                                decision = {
                                    "action": {
                                        "external": {
                                            "AIC-OS": {
                                                "base": {
                                                    "double_click": {
                                                        "target_id": dbl_click_id,
                                                        "motivation": "Harness command",
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                await _dispatch_harness_action(
                                    decision,
                                    ui_mapping,
                                    cognitive_cycle,
                                )
                                st.rerun()
                    else:
                        st.info("No double-clickable items.")

                    clickable_ids = base_interactions.get("click", {}).get("properties", {}).get(
                        "target_id", {}
                    ).get("enum", [])
                    if clickable_ids:
                        for click_id in clickable_ids:
                            if st.button(f"Click: {click_id}", key=f"click_btn_{click_id}"):
                                decision = {
                                    "action": {
                                        "external": {
                                            "AIC-OS": {
                                                "base": {
                                                    "click": {
                                                        "target_id": click_id,
                                                        "motivation": "Harness command",
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                                await _dispatch_harness_action(
                                    decision, ui_mapping, cognitive_cycle
                                )
                                st.rerun()

        # --- File Explorer Actions ---
        if "file_explorer" in aicos_interactions:
            render_file_explorer_actions(
                container, cognitive_cycle, ui_mapping, aicos_interactions["file_explorer"]
            )

        # --- Text Editor Actions ---
        if "text_editor" in aicos_interactions:
            render_text_editor_actions(
                container, cognitive_cycle, ui_mapping, aicos_interactions["text_editor"]
            )

if __name__ == "__main__":
    asyncio.run(main())
