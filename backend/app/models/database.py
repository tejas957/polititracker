from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,        # test connection before using it
    pool_recycle=300,          # recycle connections every 5 min
    connect_args={"connect_timeout": 10}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# extracts and creates data tables for politicians, bills, votes, statements, and contradictions

class Politician(Base):
    __tablename__ = "politicians"
    id = Column(String, primary_key=True)  # Congress.gov member ID
    name = Column(String, nullable=False)
    party = Column(String)
    state = Column(String)
    chamber = Column(String)  # house or senate
    twitter_handle = Column(String)
    votes = relationship("Vote", back_populates="politician")
    statements = relationship("Statement", back_populates="politician")

class Bill(Base):
    __tablename__ = "bills"
    id = Column(String, primary_key=True)
    title = Column(Text)
    summary = Column(Text)
    topic = Column(String)  # gun_control, immigration, etc.
    topic_confidence = Column(Float)
    date = Column(DateTime)
    votes = relationship("Vote", back_populates="bill")

class Vote(Base):
    __tablename__ = "votes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    politician_id = Column(String, ForeignKey("politicians.id"))
    bill_id = Column(String, ForeignKey("bills.id"))
    position = Column(String)  # Yes, No, Not Voting
    date = Column(DateTime)
    politician = relationship("Politician", back_populates="votes")
    bill = relationship("Bill", back_populates="votes")

class Statement(Base):
    __tablename__ = "statements"
    id = Column(Integer, primary_key=True, autoincrement=True)
    politician_id = Column(String, ForeignKey("politicians.id"))
    text = Column(Text)
    source = Column(String)  # twitter, speech
    topic = Column(String)
    stance = Column(String)  # FOR, AGAINST, NEUTRAL
    stance_confidence = Column(Float)
    date = Column(DateTime)
    politician = relationship("Politician", back_populates="statements")

class Contradiction(Base):
    __tablename__ = "contradictions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    politician_id = Column(String, ForeignKey("politicians.id"))
    topic = Column(String)
    vote_id = Column(Integer, ForeignKey("votes.id"))
    statement_id = Column(Integer, ForeignKey("statements.id"))
    contradiction_score = Column(Float)
    explanation = Column(Text)
    date_detected = Column(DateTime, default=datetime.utcnow)

def create_tables():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    create_tables()
    print("Tables created.")