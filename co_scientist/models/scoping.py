"""Pre-research drafts; these are not Agent tasks or research Sessions."""
# ruff: noqa: RUF001 -- Chinese UI/provenance labels use Chinese punctuation.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BriefClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=3000)
    source: Literal["user_confirmed", "ai_suggestion", "unconfirmed"] = "unconfirmed"
    message_ids: list[str] = Field(default_factory=list, max_length=20)


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    topic: str = Field(min_length=1, max_length=300)
    core_question: str = Field(min_length=1, max_length=3000)
    background: list[BriefClaim] = Field(default_factory=list, max_length=20)
    objectives: list[BriefClaim] = Field(default_factory=list, max_length=20)
    known_facts: list[BriefClaim] = Field(default_factory=list, max_length=20)
    directions: list[BriefClaim] = Field(default_factory=list, max_length=20)
    constraints: list[BriefClaim] = Field(default_factory=list, max_length=20)
    open_questions: list[BriefClaim] = Field(default_factory=list, max_length=20)
    tasks: list[BriefClaim] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def bounded_brief(self):
        if len(self.topic) + len(self.core_question) + sum(len(c.text) for key in CLAIM_SECTIONS for c in getattr(self, key)) > 20000:
            raise ValueError("研究任务书正文不能超过20000字")
        return self

    def enforce_user_sources(self, messages: list[dict]) -> ResearchBrief:
        """LLM cannot promote paraphrases/guesses to confirmed facts.

        Exact user quotations with valid message IDs may be attributed. User
        edits/confirmation are a separate explicit operation and keep provenance.
        """
        user = {m["id"]: m["content"] for m in messages if m["role"] == "user"}
        for key in CLAIM_SECTIONS:
            for claim in getattr(self, key):
                if claim.source == "user_confirmed" and not (
                    claim.message_ids and all(i in user for i in claim.message_ids)
                    and any(claim.text in user[i] for i in claim.message_ids)
                ):
                    claim.source = "unconfirmed"
                    claim.message_ids = []
        return self

    def research_input(self) -> tuple[str, str]:
        labels = {"user_confirmed": "用户已确认（未经独立验证）", "ai_suggestion": "AI 建议（非事实）",
                  "unconfirmed": "尚未确认（不得视为事实）"}
        sections = [f"研究主题：{self.topic}", f"核心科学问题：{self.core_question}",
                    "以下是用户确认采用的任务书。确认研究范围不表示所有建议和假设已被证实。"]
        for key, title in CLAIM_SECTIONS.items():
            sections.append(f"\n{title}：")
            sections.extend(f"- [{labels[c.source]}] {c.text}" for c in getattr(self, key))
        return self.core_question, "\n".join(sections)


CLAIM_SECTIONS = {"background": "研究背景", "objectives": "研究目标", "known_facts": "已知信息与已有工作",
                  "directions": "值得探索的方向", "constraints": "研究约束", "open_questions": "尚待确认的问题",
                  "tasks": "建议 Co-Scientist 执行的任务"}
