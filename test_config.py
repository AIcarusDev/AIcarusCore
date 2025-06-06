# 简单的配置系统测试脚本
# 测试重构后的配置管理系统是否正常工作

import sys
import os
from pathlib import Path

# 确保项目路径在 sys.path 中
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

def test_config_loading():
    """测试配置加载功能"""
    print("🧪 开始测试配置加载...")
    
    try:
        # 测试导入配置模块
        print("📦 导入配置模块...")
        from src.config import config
        
        print(f"✅ 配置加载成功！")
        print(f"📝 配置类型: {type(config)}")
        print(f"📋 配置版本: {config.inner.version}")
        
        # 测试点式访问
        print("\n🔍 测试点式访问...")
        
        # 测试不同层级的配置访问
        try:
            print(f"🤖 Bot名称: {config.persona.bot_name}")
            print(f"💭 思考间隔: {config.core_logic_settings.thinking_interval_seconds}秒")
            print(f"🌐 代理设置: {'启用' if config.proxy.use_proxy else '禁用'}")
            
            # 测试嵌套访问
            if config.llm_models and config.llm_models.main_consciousness:
                print(f"🧠 主意识模型: {config.llm_models.main_consciousness.model_name}")
            else:
                print("⚠️  主意识模型配置未找到")
                
            print("✅ 点式访问测试通过！")
            
        except AttributeError as e:
            print(f"❌ 点式访问失败: {e}")
            return False
            
    except ImportError as e:
        print(f"❌ 导入失败: {e}")
        return False
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return False
    
    return True

def test_config_structure():
    """测试配置结构的完整性"""
    print("\n🏗️  测试配置结构...")
    
    try:
        from src.config import config
        
        # 检查关键配置项是否存在
        required_sections = [
            'inner',
            'persona', 
            'llm_client_settings',
            'proxy',
            'core_logic_settings',
            'intrusive_thoughts_module_settings'
        ]
        
        missing_sections = []
        for section in required_sections:
            if not hasattr(config, section):
                missing_sections.append(section)
                
        if missing_sections:
            print(f"❌ 缺少配置节: {missing_sections}")
            return False
            
        print("✅ 配置结构完整！")
        return True
        
    except Exception as e:
        print(f"❌ 配置结构检查失败: {e}")
        return False

def main():
    """主测试函数"""
    print("🚀 AIcarusCore 配置系统测试")
    print("=" * 50)
    
    # 运行测试
    tests = [
        ("配置加载测试", test_config_loading),
        ("配置结构测试", test_config_structure),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n📋 运行: {test_name}")
        print("-" * 30)
        if test_func():
            passed += 1
            print(f"✅ {test_name} 通过")
        else:
            print(f"❌ {test_name} 失败")
    
    print("\n" + "=" * 50)
    print(f"📊 测试结果: {passed}/{total} 通过")
    
    if passed == total:
        print("🎉 所有测试通过！配置系统重构成功！")
        print("\n💡 你现在可以使用以下方式访问配置：")
        print("   from src.config import config")
        print("   config.persona.bot_name")
        print("   config.core_logic_settings.thinking_interval_seconds")
        print("   config.llm_models.main_consciousness.model_name")
        return True
    else:
        print("💥 部分测试失败，请检查配置文件和代码。")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
