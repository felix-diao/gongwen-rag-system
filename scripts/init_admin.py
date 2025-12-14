"""
管理员初始化脚本
运行方式: python -m app.scripts.init_admin
"""

import sys
from pathlib import Path
from getpass import getpass
import re

sys.path.append(str(Path(__file__).parent.parent.parent))

from sqlalchemy.orm import Session
from app.models.database import SessionLocal, User
from app.utils.auth import get_password_hash
import uuid


def validate_password(password: str) -> tuple[bool, str]:
    """验证密码强度"""
    if len(password) < 8:
        return False, "密码长度至少8位"
    if not any(char.isdigit() for char in password):
        return False, "密码必须包含至少一个数字"
    if not any(char.isupper() for char in password):
        return False, "密码必须包含至少一个大写字母"
    if not any(char.islower() for char in password):
        return False, "密码必须包含至少一个小写字母"
    if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-+=\[\]\\\/~`]', password):
        return False, "密码必须包含至少一个特殊符号"
    
    weak = ['12345678', 'Password1', 'Admin123', 'Qwerty123']
    if password in weak:
        return False, "密码过于简单，请使用更复杂的密码"
    
    return True, ""


def create_admin_user(username: str, password: str, department: str = "系统管理部") -> bool:
    """创建管理员账号"""
    db: Session = SessionLocal()
    
    try:
        # 检查用户是否已存在
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            print(f"用户 '{username}' 已存在")
            print(f"用户ID: {existing.user_id}")
            print(f"角色: {existing.role}")
            print(f"创建时间: {existing.created_at}\n")
            return False
        
        # 验证密码
        is_valid, msg = validate_password(password)
        if not is_valid:
            print(f"{msg}\n")
            return False
        
        # 创建管理员
        user_id = f"admin_{uuid.uuid4().hex[:16]}"
        hashed = get_password_hash(password)
        
        admin = User(
            user_id=user_id,
            username=username,
            hashed_password=hashed,
            department=department,
            role="admin"  # 关键：设置角色为 admin
        )
        
        db.add(admin)
        db.commit()
        db.refresh(admin)
        
        print("\n" + "=" * 70)
        print("管理员账号创建成功！")
        print("=" * 70)
        print(f"用户ID:     {admin.user_id}")
        print(f"用户名:     {admin.username}")
        print(f"角色:       {admin.role}")
        print(f"部门:       {admin.department}")
        print(f"创建时间:   {admin.created_at}")
        print("=" * 70)
        print("请妥善保管管理员密码！")
        print("登录地址: /api/auth/login\n")
        
        return True
        
    except Exception as e:
        db.rollback()
        print(f"创建失败: {e}\n")
        return False
    finally:
        db.close()


def list_admins():
    """列出所有管理员"""
    db: Session = SessionLocal()
    
    try:
        admins = db.query(User).filter(User.role == "admin").all()
        
        if not admins:
            print("当前系统中没有管理员账号")
            print("请运行脚本创建管理员: python -m scripts.init_admin\n")
            return
        
        print("\n" + "=" * 85)
        print(f"{'用户ID':<25} {'用户名':<20} {'部门':<20} {'创建时间':<20}")
        print("=" * 85)
        
        for admin in admins:
            created = admin.created_at.strftime("%Y-%m-%d %H:%M") if admin.created_at else "N/A"
            print(f"{admin.user_id:<25} {admin.username:<20} {admin.department or '未设置':<20} {created:<20}")
        
        print("=" * 85)
        print(f"共 {len(admins)} 个管理员账号\n")
        
    except Exception as e:
        print(f"查询失败: {e}\n")
    finally:
        db.close()


def interactive_create():
    """交互式创建管理员"""
    print("\n" + "=" * 70)
    print("管理员账号创建工具")
    print("=" * 70)
    
    username = input("\n请输入管理员用户名 (至少3位): ").strip()
    if not username or len(username) < 3:
        print("用户名长度至少3位")
        return
    
    print("\n密码要求:")
    print("  ✓ 长度至少8位")
    print("  ✓ 包含大写字母、小写字母")
    print("  ✓ 包含数字")
    print("  ✓ 包含特殊符号 (!@#$%^&* 等)")
    print("  ✓ 不能使用常见弱密码\n")
    
    password = getpass("请输入管理员密码: ")
    confirm = getpass("请再次确认密码: ")
    
    if password != confirm:
        print("\n两次密码输入不一致\n")
        return
    
    department = input("\n请输入部门名称 [默认: 系统管理部]: ").strip()
    if not department:
        department = "系统管理部"
    
    print("\n确认创建信息:")
    print(f"  用户名: {username}")
    print(f"  部门:   {department}")
    print(f"  角色:   admin (管理员)")
    
    confirm = input("\n确认创建? (y/n): ").strip().lower()
    if confirm == 'y':
        create_admin_user(username, password, department)
    else:
        print("\n已取消创建\n")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="管理员账号管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  
  # 交互式创建管理员
  python -m scripts.init_admin
  
  # 命令行快速创建
  python -m scripts.init_admin -u admin -p Admin@123456! -d "系统管理部"

  # 列出所有管理员
  python -m scripts.init_admin --list
        """
    )
    
    parser.add_argument("-u", "--username", help="管理员用户名")
    parser.add_argument("-p", "--password", help="管理员密码")
    parser.add_argument("-d", "--department", default="系统管理部", help="所属部门")
    parser.add_argument("-l", "--list", action="store_true", help="列出所有管理员")
    
    args = parser.parse_args()
    
    if args.list:
        list_admins()
    elif args.username and args.password:
        # 命令行模式
        create_admin_user(args.username, args.password, args.department)
    else:
        # 交互模式（默认）
        interactive_create()