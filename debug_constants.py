"""验证 ArangoDBHandler 常量定义"""

from src.database.arangodb_handler import ArangoDBHandler

def check_constants():
    """检查所有必要的常量是否定义"""
    required_constants = [
        'THOUGHTS_COLLECTION_NAME',
        'RAW_CHAT_MESSAGES_COLLECTION_NAME', 
        'INTRUSIVE_THOUGHTS_POOL_COLLECTION_NAME',
        'ACTION_LOGS_COLLECTION_NAME'
    ]
    
    print("🔍 检查 ArangoDBHandler 常量定义...")
    
    for const_name in required_constants:
        if hasattr(ArangoDBHandler, const_name):
            value = getattr(ArangoDBHandler, const_name)
            print(f"✅ {const_name} = '{value}'")
        else:
            print(f"❌ 缺少常量: {const_name}")
            return False
    
    print("🎉 所有必要常量都已定义！")
    return True

if __name__ == "__main__":
    check_constants()
