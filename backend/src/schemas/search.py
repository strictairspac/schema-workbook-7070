from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    relation_id: int = Field(gt=0)
    max_tables: int = Field(ge=1, le=10, description="表数上限（1-10）")
    max_attrs_per_table: int = Field(ge=1, le=10, description="每表属性数上限（1-10）")
    time_limit_seconds: float = Field(
        gt=0, le=3600, description="单轮求解时限（秒，0-3600）")
