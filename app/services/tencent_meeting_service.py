import httpx
import hmac
import hashlib
import random
import string
import json
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from app.models.database import Meeting
from app.models.schemas import MeetingCreate, MeetingUpdate
from app.config import settings
from app.utils.logger import logger


class TencentMeetingService:
    """腾讯会议服务"""
    
    def __init__(self):
        self.app_id = settings.TENCENT_MEETING_APP_ID
        self.sdk_id = settings.TENCENT_MEETING_SDK_ID
        self.secret_id = settings.TENCENT_MEETING_SECRET_ID
        self.secret_key = settings.TENCENT_MEETING_SECRET_KEY
        self.api_url = settings.TENCENT_MEETING_API_URL
        self.client = httpx.AsyncClient(timeout=30.0)
    
    def _generate_nonce(self, length: int = 16) -> str:
        """生成随机字符串"""
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))
    
    def _generate_signature(
        self,
        method: str,
        uri: str,
        timestamp: str,
        nonce: str,
        body: str
    ) -> str:
        """生成签名"""
        header_string = '&'.join([
            f"X-TC-Key={self.secret_id}",
            f"X-TC-Nonce={nonce}",
            f"X-TC-Timestamp={timestamp}"
        ])
        
        string_to_sign = '\n'.join([
            method.upper(),
            header_string,
            uri,
            body
        ])
        
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            string_to_sign.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _get_headers(self, method: str, uri: str, body: Dict = None) -> Dict:
        """获取请求头"""
        import time
        
        timestamp = str(int(time.time()))
        nonce = self._generate_nonce()
        body_str = json.dumps(body) if body else ""
        
        signature = self._generate_signature(
            method=method,
            uri=uri,
            timestamp=timestamp,
            nonce=nonce,
            body=body_str
        )
        
        return {
            "X-TC-Key": self.secret_id,
            "X-TC-Timestamp": timestamp,
            "X-TC-Nonce": nonce,
            "X-TC-Signature": signature,
            "AppId": self.app_id,
            "SdkId": self.sdk_id,
            "Content-Type": "application/json"
        }
    
    async def create_meeting(
        self,
        db: Session,
        user_id: str,
        meeting_data: MeetingCreate
    ) -> Meeting:
        """创建会议"""
        
        # 构建请求体
        body = {
            "userid": self.sdk_id,
            "instanceid": 1,
            "subject": meeting_data.subject,
            "type": meeting_data.type,
        }
        
        if meeting_data.start_time:
            body["start_time"] = str(meeting_data.start_time)
        
        if meeting_data.end_time:
            body["end_time"] = str(meeting_data.end_time)
        
        if meeting_data.settings:
            body["settings"] = meeting_data.settings.dict()
        
        # 发送请求
        uri = "/meetings"
        headers = self._get_headers("POST", uri, body)
        
        try:
            response = await self.client.post(
                f"{self.api_url}{uri}",
                json=body,
                headers=headers
            )
            
            response.raise_for_status()
            result = response.json()
            
            # 提取会议信息
            meeting_info = result["meeting_info"][0]
            
            # 保存到数据库
            db_meeting = Meeting(
                meeting_id=meeting_info["meeting_id"],
                meeting_code=meeting_info["meeting_code"],
                subject=meeting_info["subject"],
                join_url=meeting_info["join_url"],
                meeting_type=meeting_data.type,
                start_time=meeting_data.start_time,
                end_time=meeting_data.end_time,
                settings=json.dumps(meeting_data.settings.dict()) if meeting_data.settings else None,
                user_id=user_id,
                status="active"
            )
            
            db.add(db_meeting)
            db.commit()
            db.refresh(db_meeting)
            
            logger.info(f"创建会议成功: {db_meeting.meeting_id}")
            return db_meeting
            
        except httpx.HTTPStatusError as e:
            logger.error(f"创建会议失败: {e.response.text}")
            raise Exception(f"创建会议失败: {e.response.json().get('error_msg', '未知错误')}")
        except Exception as e:
            logger.error(f"创建会议异常: {e}")
            raise
    
    async def get_meeting_info(self, meeting_id: str) -> Dict:
        """查询会议详情"""
        
        uri = f"/meetings/{meeting_id}"
        params = {
            "userid": self.sdk_id,
            "instanceid": "1"
        }
        
        # 构建完整 URI（包含查询参数）
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        full_uri = f"{uri}?{query_string}"
        
        headers = self._get_headers("GET", full_uri)
        
        try:
            response = await self.client.get(
                f"{self.api_url}{full_uri}",
                headers=headers
            )
            
            response.raise_for_status()
            result = response.json()
            
            meeting_info = result["meeting_info_list"][0]
            
            return {
                "meeting_id": meeting_info["meeting_id"],
                "meeting_code": meeting_info["meeting_code"],
                "subject": meeting_info["subject"],
                "join_url": meeting_info["join_url"],
                "start_time": meeting_info.get("start_time"),
                "end_time": meeting_info.get("end_time"),
                "status": meeting_info.get("status")
            }
            
        except httpx.HTTPStatusError as e:
            logger.error(f"查询会议失败: {e.response.text}")
            raise Exception(f"查询会议失败: {e.response.json().get('error_msg', '未知错误')}")
    
    async def cancel_meeting(
        self,
        db: Session,
        user_id: str,
        meeting_id: str,
        reason: str = "主动取消"
    ):
        """取消会议"""
        
        body = {
            "userid": self.sdk_id,
            "instanceid": 1,
            "reason_code": 1,
            "reason_detail": reason
        }
        
        uri = f"/meetings/{meeting_id}/cancel"
        headers = self._get_headers("POST", uri, body)
        
        try:
            response = await self.client.post(
                f"{self.api_url}{uri}",
                json=body,
                headers=headers
            )
            
            response.raise_for_status()
            
            # 更新数据库状态
            db_meeting = db.query(Meeting).filter(
                Meeting.meeting_id == meeting_id,
                Meeting.user_id == user_id
            ).first()
            
            if db_meeting:
                db_meeting.status = "cancelled"
                db.commit()
            
            logger.info(f"取消会议成功: {meeting_id}")
            
        except httpx.HTTPStatusError as e:
            logger.error(f"取消会议失败: {e.response.text}")
            raise Exception(f"取消会议失败: {e.response.json().get('error_msg', '未知错误')}")
    
    async def update_meeting(
        self,
        db: Session,
        user_id: str,
        meeting_id: str,
        update_data: MeetingUpdate
    ):
        """修改会议"""
        
        body = {
            "userid": self.sdk_id,
            "instanceid": 1
        }
        
        if update_data.subject:
            body["subject"] = update_data.subject
        
        if update_data.start_time:
            body["start_time"] = str(update_data.start_time)
        
        if update_data.end_time:
            body["end_time"] = str(update_data.end_time)
        
        if update_data.settings:
            body["settings"] = update_data.settings.dict()
        
        uri = f"/meetings/{meeting_id}"
        headers = self._get_headers("PUT", uri, body)
        
        try:
            response = await self.client.put(
                f"{self.api_url}{uri}",
                json=body,
                headers=headers
            )
            
            response.raise_for_status()
            
            # 更新数据库
            db_meeting = db.query(Meeting).filter(
                Meeting.meeting_id == meeting_id,
                Meeting.user_id == user_id
            ).first()
            
            if db_meeting:
                if update_data.subject:
                    db_meeting.subject = update_data.subject
                if update_data.start_time:
                    db_meeting.start_time = update_data.start_time
                if update_data.end_time:
                    db_meeting.end_time = update_data.end_time
                if update_data.settings:
                    db_meeting.settings = json.dumps(update_data.settings.dict())
                
                db.commit()
            
            logger.info(f"修改会议成功: {meeting_id}")
            
        except httpx.HTTPStatusError as e:
            logger.error(f"修改会议失败: {e.response.text}")
            raise Exception(f"修改会议失败: {e.response.json().get('error_msg', '未知错误')}")
    
    async def list_user_meetings(
        self,
        db: Session,
        user_id: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> List[Meeting]:
        """查询用户的会议列表"""
        
        query = db.query(Meeting).filter(
            Meeting.user_id == user_id,
            Meeting.status == "active"
        )
        
        if start_time:
            query = query.filter(Meeting.start_time >= start_time)
        
        if end_time:
            query = query.filter(Meeting.end_time <= end_time)
        
        return query.order_by(Meeting.created_at.desc()).all()
    
    async def get_participants(self, meeting_id: str) -> List[Dict]:
        """获取参会成员列表"""
        
        uri = f"/meetings/{meeting_id}/participants"
        params = {"userid": self.sdk_id}
        
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        full_uri = f"{uri}?{query_string}"
        
        headers = self._get_headers("GET", full_uri)
        
        try:
            response = await self.client.get(
                f"{self.api_url}{full_uri}",
                headers=headers
            )
            
            response.raise_for_status()
            result = response.json()
            
            return result.get("participants", [])
            
        except httpx.HTTPStatusError as e:
            logger.error(f"获取参会成员失败: {e.response.text}")
            raise Exception(f"获取参会成员失败: {e.response.json().get('error_msg', '未知错误')}")
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()


# 创建全局实例
tencent_meeting_service = TencentMeetingService()