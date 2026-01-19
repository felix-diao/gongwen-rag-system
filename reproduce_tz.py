from datetime import datetime
from pydantic import BaseModel
from sqlalchemy import Column, Integer, DateTime, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.utils.logger import get_logger

logger = get_logger("reproduce_tz")

Base = declarative_base()

class Meeting(Base):
    __tablename__ = "test_meetings"
    id = Column(Integer, primary_key=True)
    date = Column(DateTime)

class MeetingCreate(BaseModel):
    date: datetime

# Use in-memory SQLite
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
session = Session()


# 1. Simulate frontend sending UTC+8
# 2024-01-01 10:00:00+08:00
input_str = "2024-01-01T10:00:00+08:00"
m_create = MeetingCreate.parse_obj({"date": input_str})


# 2. Store in DB
try:
    m_db = Meeting(date=m_create.date)
    session.add(m_db)
    session.commit()
    session.refresh(m_db)

except Exception as e:
    logger.error(f"Error storing meeting: {e}")

input_str_naive = "2024-01-01T10:00:00"
m_create_naive = MeetingCreate.parse_obj({"date": input_str_naive})


m_db_naive = Meeting(date=m_create_naive.date)
session.add(m_db_naive)
session.commit()
session.refresh(m_db_naive)

