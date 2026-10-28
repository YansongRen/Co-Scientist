"""Offline scoping API, provenance, Provider accounting and research handoff."""
# ruff: noqa: RUF001 -- Deliberately natural Chinese user messages.
import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from co_scientist.agents.supervisor import Supervisor
from co_scientist.llm.accounting import persist_call
from co_scientist.llm.types import LLMResponse, SynthMessage, TextBlock
from co_scientist.models import ResearchPlan, Session, Transcript
from co_scientist.models.scoping import ResearchBrief
from co_scientist.scoping import MockScopingProvider, ResearchScopingAssistant
from co_scientist.storage import db
from co_scientist.storage.repos import scoping as repo
from co_scientist.storage.repos import sessions
from co_scientist.web.app import create_app


@pytest.fixture
def application(tmp_cfg, monkeypatch):
    app = create_app(tmp_cfg)
    app.state.launched = []

    def capture(app, cfg, options, session):
        app.state.launched.append((options, session))

    monkeypatch.setattr("co_scientist.web.scoping.schedule_research", capture)
    monkeypatch.setattr("co_scientist.web.app.schedule_research", capture)
    return app


@pytest.fixture
async def client(application):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as c:
        yield c


async def turn(client, draft, text="", op="message", **overrides):
    return await client.post(f"/api/scoping/{draft['id']}/turns", json={
        "operation": op, "text": text, "request_id": uuid.uuid4().hex, "version": draft["version"], **overrides})


async def create_brief(client, idea="我们实验室做纳米孔，想提高多肽识别准确率"):
    draft = (await client.post("/api/scoping", json={})).json()
    response = await turn(client, draft, idea)
    assert response.status_code == 200, response.text
    draft = response.json()
    response = await turn(client, draft, op="brief")
    assert response.status_code == 200, response.text
    return response.json()


async def test_vague_idea_dynamic_questions_skip_and_early_brief(client, conn):
    d = (await client.post("/api/scoping", json={})).json()
    d = (await turn(client, d, "我最近在研究肝纤维化，还没有创新方向")).json()
    first = d["messages"][-1]["content"]
    assert "肝纤维化" in first and "具体问题" in first
    d = (await turn(client, d, "我们希望优化方法，有初步数据")).json()
    assert first != d["messages"][-1]["content"]
    d = (await turn(client, d, "不知道，先跳过")).json()
    assert "可以先跳过" in d["messages"][-1]["content"]
    d = (await turn(client, d, op="brief")).json()
    assert d["brief"]["known_facts"][0]["source"] == "user_confirmed"
    assert d["brief"]["directions"][0]["source"] == "ai_suggestion"
    assert d["brief"]["constraints"][0]["source"] == "unconfirmed"
    async with conn.execute("SELECT COUNT(*) n FROM sessions") as cur:
        assert (await cur.fetchone())["n"] == 0


async def test_confirm_edited_brief_is_atomic_idempotent_and_shared_launch(client, application, conn):
    d = await create_brief(client)
    assert not application.state.launched
    d["brief"]["topic"] = "用户修改的研究主题"
    d["brief"]["core_question"] = "只比较多肽长度对纳米孔识别错误的影响"
    d["brief"]["constraints"] = [{"text": "只使用已有公开数据", "source": "user_confirmed", "message_ids": []}]
    url = f"/api/scoping/{d['id']}/brief"
    saved = await client.put(url, json={"brief": d["brief"], "version": d["version"]})
    assert saved.status_code == 200
    d = saved.json()
    assert not application.state.launched
    body = {"brief": d["brief"], "version": d["version"], "confirmed": True, "options": {"n_initial": 2,"target_molecule":"RNA"}}
    responses = await asyncio.gather(*(client.post(f"/api/scoping/{d['id']}/confirm", json=body) for _ in range(4)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["session_id"] for r in responses}) == 1
    assert len(application.state.launched) == 1
    session = await sessions.fetch(conn, responses[0].json()["session_id"])
    assert session.research_goal == d["brief"]["core_question"]
    assert session.research_plan.target_molecule == "RNA"
    assert "只使用已有公开数据" in str(session.research_plan.constraints)
    assert "用户修改的研究主题" in session.research_plan.preferences[0]
    assert "AI 建议" in session.research_plan.preferences[0]
    assert "messages" not in session.config_snapshot["web_launch"]
    assert (await client.put(url, json={"brief": d["brief"], "version": d["version"]})).status_code == 409


async def test_confirmation_required_direct_research_still_works(client, application):
    d = await create_brief(client)
    for confirmed in (False, None):
        response = await client.post(f"/api/scoping/{d['id']}/confirm", json={"brief":d["brief"],"version":d["version"],"confirmed":confirmed})
        assert response.status_code == 422
    assert not application.state.launched
    response = await client.post("/sessions/new", data={"goal":"明确的独立研究问题"}, headers={"Accept":"application/json"})
    assert response.status_code == 201, response.text
    assert application.state.launched[0][1].research_goal == "明确的独立研究问题"
    assert application.state.launched[0][1].config_snapshot["web_launch"]["confirmed_brief"] is None
    assert (await client.post("/sessions/new", data={"goal":" "})).status_code == 422


async def test_refresh_isolation_versions_and_request_replay(client, tmp_cfg):
    a = await create_brief(client, "只属于会话A的想法")
    b = await create_brief(client, "只属于会话B的想法")
    # A fresh app/client has no in-memory conversation state to recover from.
    fresh = create_app(tmp_cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fresh), base_url="http://test") as other:
        restored = (await other.get(f"/api/scoping/{a['id']}")).json()
        assert restored["brief"] == a["brief"] and restored["messages"] == a["messages"]
        assert "会话B" not in json.dumps(restored, ensure_ascii=False)
    assert "会话A" not in json.dumps(b, ensure_ascii=False)
    assert (await turn(client, a, "旧版本", version=0)).status_code == 409
    request_id=uuid.uuid4().hex
    first=await turn(client,a,"新的回答",request_id=request_id)
    second=await turn(client,a,"新的回答",request_id=request_id)
    assert first.status_code == second.status_code == 200
    assert first.json()["messages"] == second.json()["messages"]
    assert first.json()["usage"]["calls"] == second.json()["usage"]["calls"]


def test_llm_cannot_label_invented_fact_as_user_confirmed():
    b=ResearchBrief(topic="主题",core_question="问题",known_facts=[
        {"text":"用户原话", "source":"user_confirmed","message_ids":["m1"]},
        {"text":"虚构的实验结果", "source":"user_confirmed","message_ids":["m1"]},
        {"text":"用户原话", "source":"user_confirmed","message_ids":["other-session"]}])
    b.enforce_user_sources([{"id":"m1","role":"user","content":"这是用户原话"}])
    assert [c.source for c in b.known_facts] == ["user_confirmed","unconfirmed","unconfirmed"]


@pytest.mark.parametrize("failure", ["timeout","json","service","truncated"])
async def test_llm_failure_records_usage_and_recovers(client, application, tmp_cfg, failure):
    class Broken:
        async def call(self, spec, ctx, **kwargs):
            if failure=="timeout":
                await asyncio.sleep(1)
            if failure=="service":
                raise RuntimeError("fake-secret-key-never-expose")
            return LLMResponse(raw=SynthMessage(content=[TextBlock('{bad' if failure=="json" else '{"reply":"ok"}')],stop_reason="max_tokens" if failure=="truncated" else "end_turn"),
                transcript_id="",cost_usd=0,input_tokens=10,output_tokens=5,cache_read=0,cache_write=0)
    tmp_cfg.scoping.timeout_seconds=.01
    application.state.scoping_provider_factory=lambda *_:Broken()
    d=(await client.post("/api/scoping",json={})).json()
    r=await turn(client,d,"模糊想法")
    assert r.status_code==502 and "fake-secret" not in r.text
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert len(d["messages"])==1 and not d["busy"]
    assert d["calls"][0]["status"]=="error"
    assert d["calls"][0]["usage_known"] == (failure in ("json","truncated"))
    application.state.scoping_provider_factory=lambda *_:MockScopingProvider()
    r=await turn(client,d,op="regenerate")
    assert r.status_code==200 and len(r.json()["messages"])==2


async def test_budget_turn_limits_and_live_double_optin(client, application, tmp_cfg):
    assert (await client.post("/api/scoping",json={"use_live":True})).status_code==403
    tmp_cfg.scoping.max_turns=1
    d=(await client.post("/api/scoping",json={})).json()
    d=(await turn(client,d,"一个想法")).json()
    assert (await turn(client,d,"第二轮")).status_code==429
    d=(await turn(client,d,op="brief")).json()  # early brief remains allowed
    assert d["brief"]
    tmp_cfg.scoping.max_calls=2
    assert (await turn(client,d,op="regenerate")).status_code==429
    tmp_cfg.scoping.allow_live_calls=True
    tmp_cfg.llm.provider="codex_cli"
    assert (await client.post("/api/scoping",json={"use_live":True})).status_code==403


async def test_concurrent_turn_single_admission(client, application):
    class Slow(MockScopingProvider):
        async def call(self,*args,**kwargs):
            await asyncio.sleep(.03)
            return await super().call(*args,**kwargs)
    application.state.scoping_provider_factory=lambda *_:Slow()
    d=(await client.post("/api/scoping",json={})).json()
    responses=await asyncio.gather(turn(client,d,"一"),turn(client,d,"二"))
    assert sorted(r.status_code for r in responses)==[200,409]


async def test_context_is_bounded_and_does_not_blend_sessions(tmp_cfg):
    assistant=ResearchScopingAssistant(tmp_cfg)
    draft={"messages":[{"id":str(i),"role":"user" if i%2==0 else "assistant","content":"界"*4000} for i in range(30)],"brief":None}
    ctx=assistant.context(draft,"message")
    assert ctx["history_truncated"] and ctx["messages"][0]["id"]=="0"
    assert ctx["messages"][-1]["id"]=="29"
    assert sum(len(m["content"]) for m in ctx["messages"])<=tmp_cfg.scoping.context_chars


async def test_provider_accounting_without_formal_session(conn):
    d=await repo.create(conn,use_live=True)
    await conn.execute("""INSERT INTO scoping_calls(id,draft_id,request_id,operation,status,model,prompt_json,prompt_sha256,created_at)
        VALUES('call',?,'req','message','pending','fake','{}','hash','now')""",(d["id"],))
    await conn.commit()
    now=datetime.now(UTC)
    await persist_call(conn,Transcript(id="trn",session_id=d["id"],task_id="call",agent="research_scoping",action="chat",model="fake",
        input_tokens=100,output_tokens=20,cost_usd=.01,started_at=now,finished_at=now,artifact_path="artifacts/fake.json"))
    restored=await repo.fetch(conn,d["id"])
    assert restored["usage"]["tokens"]==120 and restored["usage"]["cost_usd"]==.01
    for table in ("sessions","tasks","transcripts"):
        async with conn.execute(f"SELECT COUNT(*) n FROM {table}") as cur:
            assert (await cur.fetchone())["n"]==0


async def test_legacy_v10_migration_preserves_session(tmp_cfg, conn):
    now=datetime.now(UTC)
    old=Session(id="ses_legacy_scoping",created_at=now,updated_at=now,status="done",research_goal="已有科研数据",
                research_plan=ResearchPlan(objective="保持不变"),config_snapshot={},budget_tokens=500,budget_usd=1)
    await sessions.insert(conn,old)
    # Reconstruct a pre-scoping database, keeping its existing research tables.
    await conn.execute("DROP TABLE input_files")
    await conn.execute("DROP TABLE input_groups")
    await conn.execute("DROP TABLE scoping_calls")
    await conn.execute("DROP TABLE scoping_drafts")
    await conn.execute("DELETE FROM schema_migrations WHERE version>=11")
    await conn.commit()
    await db.init_db(tmp_cfg)
    assert await repo.create(conn)
    assert await sessions.fetch(conn,old.id)==old
    async with conn.execute("PRAGMA integrity_check") as cur:
        assert (await cur.fetchone())[0]=="ok"


async def test_confirmed_plan_bootstraps_original_supervisor_once(client, application, tmp_cfg, conn):
    d=await create_brief(client)
    r=await client.post(f"/api/scoping/{d['id']}/confirm",json={"brief":d["brief"],"version":d["version"],"confirmed":True})
    sid=r.json()["session_id"]
    observed=[]
    async def main_loop(self, c, deps, session, tracker):
        observed.append(session)
        async with c.execute("SELECT * FROM tasks WHERE session_id=?",(sid,)) as cur:
            rows=await cur.fetchall()
        assert len(rows)==2 and all(row["agent"]=="generation" for row in rows)
        return None
    with patch("co_scientist.agents.supervisor.get_provider", return_value=MockScopingProvider()), \
         patch.object(Supervisor,"_parse_goal",new_callable=AsyncMock) as parse, \
         patch.object(Supervisor,"_main_loop",main_loop), \
         patch.object(Supervisor,"_finalize",new_callable=AsyncMock):
        await Supervisor(tmp_cfg).run_session("",prepared_session_id=sid,n_initial=2)
        parse.assert_not_called()
        with pytest.raises(RuntimeError,match="already started"):
            await Supervisor(tmp_cfg).run_session("",prepared_session_id=sid)
    assert observed[0].research_plan.objective==d["brief"]["core_question"]
    assert "AI 建议" in str(observed[0].research_plan.preferences)


async def test_configured_openai_provider_runs_with_mock_sdk_and_separate_accounting(client, tmp_cfg, conn):
    from co_scientist.tests.unit.test_provider import _fake_openai_response

    tmp_cfg.scoping.allow_live_calls=True
    tmp_cfg.llm.provider="openai_compatible"
    tmp_cfg.secrets.OPENAI_API_KEY="sk-fake-test-only"
    d=(await client.post("/api/scoping",json={"use_live":True})).json()
    with patch("openai.AsyncOpenAI") as sdk:
        sdk.return_value.chat.completions.create=AsyncMock(return_value=_fake_openai_response(text=json.dumps({"reply":"你的现有数据适合怎样的验证？","ready_for_brief":False})))
        sdk.return_value.close=AsyncMock()
        response=await turn(client,d,"一个模糊的研究想法")
        assert response.status_code==200,response.text
        sdk.return_value.chat.completions.create.assert_awaited_once()
        sdk.return_value.close.assert_awaited_once()
    d=response.json()
    assert d["usage"]["tokens"]==150 and d["usage"]["cost_usd"]>0
    async with conn.execute("SELECT transcript_json,prompt_json FROM scoping_calls WHERE draft_id=?",(d["id"],)) as cur:
        record=await cur.fetchone()
    assert json.loads(record["transcript_json"])["agent"]=="research_scoping"
    assert "sk-fake-test-only" not in record["prompt_json"]+response.text
    async with conn.execute("SELECT COUNT(*) n FROM sessions") as cur:
        assert (await cur.fetchone())["n"]==0


async def test_unknown_remote_usage_reservation_survives_restart(client, tmp_cfg, conn):
    tmp_cfg.scoping.allow_live_calls=True
    d=await repo.create(conn,use_live=True)
    await conn.execute("""INSERT INTO scoping_calls(id,draft_id,request_id,operation,status,model,prompt_json,prompt_sha256,
        reserved_tokens,reserved_usd,created_at) VALUES('lost',?,'lost-req','message','pending','fake','{}','hash',60000,1,'now')""",(d["id"],))
    await conn.execute("UPDATE scoping_drafts SET busy_call_id='lost',busy_until=1 WHERE id=?",(d["id"],))
    await conn.commit()
    with patch("co_scientist.scoping.get_provider") as provider:
        response=await turn(client,d,"恢复时不能把远端未知开销当作零")
        assert response.status_code==429
        provider.assert_not_called()
    restored=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert not restored["busy"] and restored["usage"]["tokens"]==60000
    assert restored["usage"]["has_estimates"]


async def test_invalid_brief_never_launches_and_long_first_message_still_works(client, application):
    d=await create_brief(client,"想法"*2000)
    assert d["brief"]["known_facts"][0]["text"]==("想法"*1500)
    d["brief"]["core_question"]=" "
    response=await client.post(f"/api/scoping/{d['id']}/confirm",json={"brief":d["brief"],"version":d["version"],"confirmed":True})
    assert response.status_code==422 and not application.state.launched


async def test_brief_generation_failure_can_retry_without_duplicate_user_message(client, application):
    d=(await client.post("/api/scoping",json={})).json()
    d=(await turn(client,d,"我只有一个大概想法")).json()
    class Invalid:
        async def call(self,*args,**kwargs):
            return LLMResponse(raw=SynthMessage(content=[TextBlock('{"topic":"missing fields"}')]),
                               transcript_id="",cost_usd=0,input_tokens=10,output_tokens=5,cache_read=0,cache_write=0)
    application.state.scoping_provider_factory=lambda *_:Invalid()
    assert (await turn(client,d,op="brief")).status_code==502
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert d["brief"] is None and len(d["messages"])==2
    application.state.scoping_provider_factory=lambda *_:MockScopingProvider()
    d=(await turn(client,d,op="brief")).json()
    assert d["brief"] and len(d["messages"])==2


async def test_prepared_direct_session_keeps_goal_parser_and_human_feedback(client, tmp_cfg, conn):
    r=await client.post("/sessions/new",data={"goal":"DNA研究","preferences":"只使用公开数据"},headers={"Accept":"application/json"})
    sid=r.json()["session_id"]
    with patch("co_scientist.agents.supervisor.get_provider", return_value=MockScopingProvider()), \
         patch.object(Supervisor,"_parse_goal",new_callable=AsyncMock,return_value=ResearchPlan(objective="DNA研究")) as parse, \
         patch.object(Supervisor,"_main_loop",new_callable=AsyncMock,return_value=None), \
         patch.object(Supervisor,"_finalize",new_callable=AsyncMock):
        await Supervisor(tmp_cfg).run_session("",prepared_session_id=sid,n_initial=2)
        parse.assert_awaited_once()
        assert parse.await_args.args[-1]=="只使用公开数据"
    async with conn.execute("SELECT text FROM system_feedback WHERE session_id=?",(sid,)) as cur:
        rows=await cur.fetchall()
    assert len(rows)==1 and rows[0]["text"]=="只使用公开数据"


async def test_snapshot_taken_during_llm_call_cannot_overwrite_completed_brief(client, application):
    d=await create_brief(client)
    started,release=asyncio.Event(),asyncio.Event()
    class Slow(MockScopingProvider):
        async def call(self,*args,**kwargs):
            started.set()
            await release.wait()
            return await super().call(*args,**kwargs)
    application.state.scoping_provider_factory=lambda *_:Slow()
    pending=asyncio.create_task(turn(client,d,op="brief"))
    await asyncio.wait_for(started.wait(),1)
    during=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert during["busy"]
    release.set()
    assert (await pending).status_code==200
    stale=await client.put(f"/api/scoping/{d['id']}/brief",json={"brief":during["brief"],"version":during["version"]})
    assert stale.status_code==409
