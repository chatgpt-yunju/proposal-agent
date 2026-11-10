"""核心功能简单测试"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from app import db


def test_db_init():
    """测试数据库初始化"""
    db.init_db()
    projects = db.list_projects()
    assert isinstance(projects, list), "数据库初始化失败"
    print("✅ 数据库初始化正常")


def test_project_crud():
    """测试项目增删改查"""
    # 创建测试项目
    proj = db.create_project("测试项目_临时")
    pid = proj["id"]
    assert pid, "创建项目失败"
    
    # 查询
    proj2 = db.get_project(pid)
    assert proj2 and proj2["name"] == "测试项目_临时", "查询项目失败"
    
    # 重命名
    db.update_project(pid, name="测试项目_已重命名")
    proj3 = db.get_project(pid)
    assert proj3["name"] == "测试项目_已重命名", "重命名失败"
    
    print("✅ 项目CRUD正常")


def test_chapter_content():
    """测试章节内容读写"""
    proj = db.create_project("测试章节_临时")
    pid = proj["id"]
    # 添加章节
    ch = db.create_chapter(pid, "01", "测试章节", "测试描述")
    cid = ch["id"]
    assert cid, "创建章节失败"
    
    # 写入内容
    test_content = "这是测试内容，用于验证数据库读写。"
    db.update_chapter_content(cid, test_content)
    
    # 读取
    ch2 = db.get_chapter(cid)
    assert ch2 and ch2["content"] == test_content, "章节内容读写失败"
    
    print("✅ 章节内容读写正常")


def test_operation_audit():
    """测试操作审计日志的写入与查询"""
    proj = db.create_project("测试审计_临时")
    pid = proj["id"]

    # 写入成功、失败、进行中各一条
    db.add_operation(pid, "chapter_generate", "测试章节", "started", "开始生成", chapter_id="c_test01")
    db.add_operation(pid, "chapter_generate", "测试章节", "success", "字数 200，引用 3 条，耗时 60 秒")
    db.add_operation(pid, "chapter_generate", "测试章节", "failed", "生成失败：超时")

    ops = db.list_operations(pid, limit=10)
    assert len(ops) >= 3, "操作审计记录数量不足"
    # 按时间倒序：最新写入的失败记录应排在最前
    assert ops[0]["status"] == "failed", "审计记录未按时间倒序返回"
    assert ops[0]["target"] == "测试章节", "审计记录对象字段错误"
    assert all(o["project_id"] == pid for o in ops), "审计记录项目归属错误"

    # limit 生效
    limited = db.list_operations(pid, limit=2)
    assert len(limited) == 2, "审计记录 limit 参数未生效"

    # 全局查询包含该项目记录
    glob = db.list_operations(None, limit=50)
    assert any(o["project_id"] == pid for o in glob), "全局审计查询未包含本项目记录"

    print("✅ 操作审计日志正常")


if __name__ == "__main__":
    print("开始运行核心测试...")
    test_db_init()
    test_project_crud()
    test_chapter_content()
    test_operation_audit()
    print("\n🎉 所有测试通过！")
