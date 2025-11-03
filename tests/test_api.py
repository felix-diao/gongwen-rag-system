"""API 测试"""
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_check():
    """测试健康检查"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

def test_root():
    """测试根路径"""
    response = client.get("/")
    assert response.status_code == 200
    assert "message" in response.json()

def test_register():
    """测试用户注册"""
    response = client.post(
        "/api/auth/register",
        params={
            "username": "test_user",
            "password": "test_password",
            "department": "测试部门"
        }
    )
    assert response.status_code == 200
    assert "user_id" in response.json()

def test_login():
    """测试用户登录"""
    # 先注册
    client.post(
        "/api/auth/register",
        params={
            "username": "login_test",
            "password": "password123"
        }
    )
    
    # 登录
    response = client.post(
        "/api/auth/login",
        json={
            "username": "login_test",
            "password": "password123"
        }
    )
    assert response.status_code == 200
    assert "access_token" in response.json()