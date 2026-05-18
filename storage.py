# storage.py
# Modified from original by wolfycz - Removed relationship feature
# Original work: https://github.com/nuomicici/astrbot_plugin_Favour_Ultra/
# Licensed under the Apache License, Version 2.0
import json
import asyncio
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime
from aiofiles import open as aio_open
from sqlmodel import SQLModel, Field, select, delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from astrbot.api import logger
from .utils import is_valid_userid

# 定义数据库模型
class FavourRecord(SQLModel, table=True):
    __tablename__ = "favour_records"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    favour: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

class FavourDBManager:
    """基于SQLite的好感度数据库管理器"""
    def __init__(self, data_dir: Path, min_val: int = -100, max_val: int = 100):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "favour.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.min_val = min_val
        self.max_val = max_val

        self.engine = create_async_engine(self.db_url, echo=False)
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def init_db(self):
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.create_all)
                self._initialized = True
                logger.info(f"好感度数据库已初始化: {self.db_path}")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

    async def backup_data(self, records: List[FavourRecord], prefix: str) -> Optional[str]:
        if not records:
            return None
        try:
            backup_dir = self.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = backup_dir / f"{prefix}_{timestamp}.json"

            data_to_save = []
            for r in records:
                d = r.dict()
                d['created_at'] = d['created_at'].isoformat() if d.get('created_at') else None
                d['updated_at'] = d['updated_at'].isoformat() if d.get('updated_at') else None
                data_to_save.append(d)

            async with aio_open(filename, "w", encoding="utf-8") as f:
                await f.write(json.dumps(data_to_save, ensure_ascii=False, indent=2))
            return str(filename)
        except Exception as e:
            logger.error(f"备份数据失败: {e}")
            return None

    async def get_favour(self, user_id: str) -> Optional[FavourRecord]:
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord).where(FavourRecord.user_id == user_id)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def update_favour(self, user_id: str, favour: Optional[int] = None) -> bool:
        await self.init_db()
        if not is_valid_userid(user_id):
            return False

        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(FavourRecord.user_id == user_id)
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    init_favour = max(self.min_val, min(self.max_val, favour)) if favour is not None else 0
                    record = FavourRecord(
                        user_id=user_id,
                        favour=init_favour,
                    )
                    session.add(record)
                else:
                    if favour is not None:
                        record.favour = max(self.min_val, min(self.max_val, favour))
                    record.updated_at = datetime.now()
                    session.add(record)

                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新数据库失败: {str(e)}")
            return False

    async def delete_favour(self, user_id: str) -> Tuple[bool, str]:
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = select(FavourRecord).where(FavourRecord.user_id == user_id)
                result = await session.execute(stmt)
                record = result.scalars().first()

                if not record:
                    return False, "未找到记录"

                await session.delete(record)
                await session.commit()
                return True, "删除成功"
        except Exception as e:
            logger.error(f"删除记录失败: {str(e)}")
            return False, f"数据库错误: {str(e)}"

    async def get_global_records(self) -> List[FavourRecord]:
        await self.init_db()
        async with self.async_session() as session:
            stmt = select(FavourRecord)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def clear_all(self) -> bool:
        await self.init_db()
        try:
            async with self.async_session() as session:
                stmt = delete(FavourRecord)
                await session.execute(stmt)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"清空所有记录失败: {str(e)}")
            return False
