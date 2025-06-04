"""
诊断 save_thought_document 方法的返回值问题
"""
import asyncio
import datetime
from src.database.arangodb_handler import ArangoDBHandler

async def test_save_document():
    """测试保存文档的返回值"""
    try:
        # 创建数据库处理器
        db_handler = await ArangoDBHandler.create()
        
        # 测试文档
        test_document = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "think_output": "这是一个测试思考",
            "emotion_output": "平静",
            "test_flag": True
        }
        
        print("🔍 测试保存文档...")
        result = await db_handler.save_thought_document(test_document)
        
        print(f"返回值类型: {type(result)}")
        print(f"返回值内容: {result}")
        
        if isinstance(result, str):
            print("✅ 返回值类型正确（字符串）")
        elif isinstance(result, bool):
            print("❌ 返回值类型错误（布尔值）")
        elif result is None:
            print("⚠️ 返回值为None（可能是错误）")
        else:
            print(f"❓ 返回值类型未知: {type(result)}")
            
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_save_document())
