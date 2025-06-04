from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

Base = declarative_base()

class Package(Base):
    __tablename__ = 'packages'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    source = Column(String, nullable=False)  # 'pypi' or 'upload'
    added = Column(DateTime, default=datetime.utcnow)
    user_id = Column(String, nullable=True)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    
