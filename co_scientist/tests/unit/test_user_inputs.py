"""Actual upload/parse/context/confirmation paths, isolated SQLite and no APIs."""
import asyncio
import io
import json
import zipfile
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from co_scientist import inputs
from co_scientist.llm.openai_client import _build_openai_request
from co_scientist.llm.routing import ModelRoute
from co_scientist.llm.types import (
    AgentCallSpec,
    CachedBlock,
    InputImage,
    SynthMessage,
    TextBlock,
    Usage,
)
from co_scientist.models.scoping_limits import ScopingLimits
from co_scientist.scoping import MockScopingProvider
from co_scientist.storage import db
from co_scientist.storage.repos import scoping, sessions
from co_scientist.tests.unit.test_research_scoping import turn
from co_scientist.web.app import create_app


@pytest.fixture
def app(tmp_cfg, monkeypatch):
    application=create_app(tmp_cfg)
    application.state.launched=[]
    def capture(app,cfg,options,session):
        application.state.launched.append((options,session))
    monkeypatch.setattr("co_scientist.web.app.schedule_research",capture)
    monkeypatch.setattr("co_scientist.web.scoping.schedule_research",capture)
    return application


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as c:
        yield c


async def group(client,draft=None):
    r=await client.post("/api/inputs/groups",json={"draft_id":draft["id"]} if draft else {})
    assert r.status_code==200,r.text
    return r.json()["id"]


async def upload(client,gid,name="notes.txt",data=b"Public dataset; no new experiments.",mime="text/plain"):
    return await client.post(f"/api/inputs/groups/{gid}/files",files={"files":(name,data,mime)})


def png():
    out=io.BytesIO()
    Image.new("RGB",(20,10),"white").save(out,format="PNG")
    return out.getvalue()


def pdf(*,text=True):
    writer=PdfWriter()
    page=writer.add_blank_page(300,200)
    if text:
        font=DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})
        page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
        stream=DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 20 100 Td (PUBLIC RESEARCH DATA) Tj ET")
        page[NameObject("/Contents")]=writer._add_object(stream)
    out=io.BytesIO()
    writer.write(out)
    return out.getvalue()


def docx():
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as archive:
        archive.writestr("word/document.xml",'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Public dataset only.</w:t></w:r></w:p></w:body></w:document>')
    return out.getvalue()


async def test_custom_formal_budget_tokens_and_time_reach_session(client,app,conn):
    r=await client.post("/sessions/new",data={"goal":"Research question","budget_usd":.25,"budget_tokens":32000,"wall_clock_seconds":120},headers={"Accept":"application/json"})
    assert r.status_code==201,r.text
    s=await sessions.fetch(conn,r.json()["session_id"])
    assert s.budget_usd==.25 and s.budget_tokens==32000
    options,_=app.state.launched[0]
    assert options.wall_clock_seconds==120
    assert s.config_snapshot["run"]["budget_tokens"]==32000


async def test_server_selects_model_without_frontend_checkbox(client,tmp_cfg):
    with patch("co_scientist.scoping.get_provider") as provider:
        d=(await client.post("/api/scoping",json={})).json()
        assert not d["use_live"]
        tmp_cfg.scoping.allow_live_calls=True
        d=(await client.post("/api/scoping",json={})).json()
        assert d["use_live"]
        provider.assert_not_called()  # creation/upload never bills a model


async def test_custom_scoping_limits_survive_refresh_and_enforce_admission(client,app):
    d=(await client.post("/api/scoping",json={"limits":{"budget_usd":.1,"budget_tokens":1000,"wall_clock_seconds":12}})).json()
    restored=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert restored["limits"]=={"budget_usd":.1,"budget_tokens":1000,"wall_clock_seconds":12}
    factory=AsyncMock()
    app.state.scoping_provider_factory=factory
    assert (await turn(client,d,"A vague idea")).status_code==429
    factory.assert_not_called()
    r=await client.put(f"/api/scoping/{d['id']}/limits",json={"limits":{"budget_usd":.2,"budget_tokens":30000,"wall_clock_seconds":30},"version":d["version"]})
    assert r.status_code==200,r.text
    assert r.json()["limits"]["budget_tokens"]==30000
    assert (await client.put(f"/api/scoping/{d['id']}/limits",json={"limits":{"budget_usd":26,"budget_tokens":30000,"wall_clock_seconds":30},"version":r.json()["version"]})).status_code==422


async def test_total_model_runtime_limits_call_timeout(client,app,conn):
    class Slow(MockScopingProvider):
        async def call(self,*args,**kwargs):
            await asyncio.sleep(.1)
            return await super().call(*args,**kwargs)
    d=(await client.post("/api/scoping",json={"limits":{"budget_usd":1,"budget_tokens":60000,"wall_clock_seconds":1}})).json()
    await conn.execute("""INSERT INTO scoping_calls(id,draft_id,request_id,operation,status,model,prompt_json,prompt_sha256,created_at,usage_known,elapsed_seconds)
        VALUES('previous',?,'previous','chat','done','mock','{}','hash','now',1,.99)""",(d["id"],))
    await conn.commit()
    app.state.scoping_provider_factory=lambda *_:Slow()
    r=await turn(client,d,"Study idea")
    assert r.status_code==502
    restored=(await client.get(f"/api/scoping/{d['id']}")).json()
    assert restored["usage"]["run_seconds"]>=1
    assert (await turn(client,restored,op="regenerate")).status_code==429


@pytest.mark.parametrize(("name","data","expected"),[("paper.pdf",pdf(),"PUBLIC RESEARCH DATA"),("notes.docx",docx(),"Public dataset only."),("data.csv",b"name,value\na,2","name,value"),("notes.md",b"# Study\nOnly public data","Only public data")])
async def test_document_parsing_and_unverified_context(client,conn,tmp_cfg,name,data,expected):
    gid=await group(client)
    r=await upload(client,gid,name,data,"application/octet-stream")
    assert r.status_code==200,r.text
    file=r.json()["files"][0]
    assert file["parse_status"]=="text" and "path" not in file and "text_content" not in file
    context=await inputs.material_context(conn,tmp_cfg,gid)
    assert expected in context[0]["text"]
    assert context[0]["source"]=="user_material_unverified"
    assert (await client.get(file["url"])).content==data


async def test_scanned_pdf_is_not_claimed_as_read(client):
    gid=await group(client)
    r=await upload(client,gid,"scan.pdf",pdf(text=False))
    assert r.status_code==200
    f=r.json()["files"][0]
    assert f["parse_status"]=="unavailable" and "OCR" in f["parse_error"]


async def test_upload_enters_scoping_prompt_then_confirmed_research(client,app,conn,tmp_cfg):
    captured=[]
    class Spy(MockScopingProvider):
        async def call(self,spec,ctx,**kwargs):
            captured.append(json.loads(spec.user_blocks[0].text))
            return await super().call(spec,ctx,**kwargs)
    app.state.scoping_provider_factory=lambda *_:Spy()
    d=(await client.post("/api/scoping",json={})).json()
    gid=await group(client,d)
    r=await upload(client,gid)
    assert r.status_code==200
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    d=(await turn(client,d,"Help clarify a study")).json()
    assert "no new experiments" in captured[-1]["attachments"][0]["text"]
    assert not app.state.launched
    d=(await turn(client,d,op="brief")).json()
    r=await client.post(f"/api/scoping/{d['id']}/confirm",json={"brief":d["brief"],"version":d["version"],"confirmed":True})
    assert r.status_code==200,r.text
    s=await sessions.fetch(conn,r.json()["session_id"])
    assert "no new experiments" in str(s.research_plan.preferences)
    assert "未经核验" in str(s.research_plan.preferences)
    assert s.config_snapshot["web_launch"]["upload_group_id"]==gid
    assert (await upload(client,gid,"new.txt",b"different")).status_code==409
    async with conn.execute("SELECT COUNT(*) FROM scientific_evidence") as cur:
        assert (await cur.fetchone())[0]==0


async def test_direct_attachment_link_and_foreign_draft_rejection(client,app,conn):
    gid=await group(client)
    f=(await upload(client,gid)).json()["files"][0]
    r=await client.post("/sessions/new",data={"goal":"Study files","upload_group_id":gid},headers={"Accept":"application/json"})
    assert r.status_code==201,r.text
    assert (await client.get(f"/api/inputs/groups/{gid}")).json()["session_id"]==r.json()["session_id"]
    assert (await client.delete(f"/api/inputs/groups/{gid}/files/{f['id']}")).status_code==409
    d=(await client.post("/api/scoping",json={})).json()
    foreign=await group(client,d)
    await upload(client,foreign)
    before=len(app.state.launched)
    r=await client.post("/sessions/new",data={"goal":"Should reject","upload_group_id":foreign},headers={"Accept":"application/json"})
    assert r.status_code==409 and len(app.state.launched)==before
    async with conn.execute("SELECT COUNT(*) FROM sessions") as cur:
        assert (await cur.fetchone())[0]==1


@pytest.mark.parametrize(("name","data"),[("bad.svg",b'<svg onload="bad()"/>'),("run.exe",b'MZ'),("fake.png",b'not a png'),("broken.pdf",b'%PDF-broken')])
async def test_invalid_files_rejected_without_records(client,conn,name,data):
    gid=await group(client)
    r=await upload(client,gid,name,data)
    assert r.status_code in (415,422)
    assert (await client.get(f"/api/inputs/groups/{gid}")).json()["files"]==[]


async def test_filename_path_symlink_and_image_preview(client,tmp_cfg,tmp_path):
    gid=await group(client)
    r=await upload(client,gid,"../../notes.txt",b"notes")
    assert r.status_code==200 and r.json()["files"][0]["name"]=="notes.txt"
    image=(await upload(client,gid,"image.png",png(),"image/png")).json()["files"][-1]
    preview=await client.get(image["url"]+"?preview=true")
    assert preview.status_code==200 and preview.headers["content-type"]=="image/png"
    assert preview.headers["content-disposition"].startswith("inline")
    assert preview.headers["x-content-type-options"]=="nosniff"
    assert image["width"]==20 and not image["vision_enabled"]
    other=await group(client)
    root=tmp_cfg.data_dir/"inputs"/other
    outside=tmp_path/"outside"
    outside.mkdir()
    root.symlink_to(outside,target_is_directory=True)
    assert (await upload(client,other)).status_code==422
    assert list(outside.iterdir())==[]


async def test_concurrent_quota_dedupe_and_soft_remove(client,tmp_cfg,conn):
    gid=await group(client)
    first=await upload(client,gid)
    second=await upload(client,gid)
    assert first.json()["files"]==second.json()["files"]
    tmp_cfg.uploads.max_files=1
    r=await asyncio.gather(upload(client,gid,"a.txt",b"a"),upload(client,gid,"b.txt",b"b"))
    assert all(x.status_code==413 for x in r)
    f=first.json()["files"][0]
    assert (await client.delete(f"/api/inputs/groups/{gid}/files/{f['id']}")).status_code==200
    assert (await client.get(f["url"])).status_code==404
    assert await inputs.material_context(conn,tmp_cfg,gid)==[]
    fresh=await group(client)
    r=await asyncio.gather(upload(client,fresh,"a.txt",b"a"),upload(client,fresh,"b.txt",b"b"))
    assert sorted(x.status_code for x in r)==[200,413]


async def test_image_inputs_reach_provider_only_when_enabled(client,app,tmp_cfg):
    captured=[]
    class Spy(MockScopingProvider):
        async def call(self,spec,ctx,**kwargs):
            captured.append(spec)
            return await super().call(spec,ctx,**kwargs)
    app.state.scoping_provider_factory=lambda *_:Spy()
    tmp_cfg.scoping.allow_live_calls=True
    d=(await client.post("/api/scoping",json={})).json()
    gid=await group(client,d)
    await upload(client,gid,"image.png",png())
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    d=(await turn(client,d,"Describe the uploaded material")).json()
    assert captured[-1].input_images==[]
    assert not json.loads(captured[-1].user_blocks[0].text)["attachments"][0]["image_content_available"]
    tmp_cfg.uploads.vision_enabled=True
    r=await turn(client,d,"Now use configured vision")
    assert r.status_code==200,r.text
    spec=captured[-1]
    assert len(spec.input_images)==1 and spec.input_images[0].media_type=="image/png"
    assert json.loads(spec.user_blocks[0].text)["attachments"][0]["image_content_available"]


def test_openai_multimodal_request_uses_canonical_image_input():
    spec=AgentCallSpec(route=ModelRoute("research_scoping","chat","fake"),user_blocks=[CachedBlock("Read inputs")],input_images=[InputImage("image/png","YWJj","fil_test")])
    request=_build_openai_request(spec)
    assert request["messages"][0]["content"][-1]["image_url"]["url"]=="data:image/png;base64,YWJj"


async def test_anthropic_multimodal_with_mock_sdk(client,tmp_cfg):
    tmp_cfg.scoping.allow_live_calls=True
    tmp_cfg.uploads.vision_enabled=True
    tmp_cfg.llm.provider="anthropic"
    tmp_cfg.secrets.ANTHROPIC_API_KEY="fake-test-only"
    d=(await client.post("/api/scoping",json={})).json()
    gid=await group(client,d)
    await upload(client,gid,"image.png",png())
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    with patch("co_scientist.llm.anthropic_client.AsyncAnthropic") as sdk:
        sdk.return_value.messages.create=AsyncMock(return_value=SynthMessage(content=[TextBlock(json.dumps({"reply":"Attachment received","ready_for_brief":False}))],usage=Usage(input_tokens=100,output_tokens=20)))
        sdk.return_value.close=AsyncMock()
        r=await turn(client,d,"Explain input")
        assert r.status_code==200,r.text
        content=sdk.return_value.messages.create.await_args.kwargs["messages"][0]["content"]
        assert any(b["type"]=="image" and b["source"]["media_type"]=="image/png" for b in content)


async def test_v11_migration_preserves_existing_draft(tmp_cfg,conn):
    d=await scoping.create(conn)
    await conn.execute("DROP TABLE input_files")
    await conn.execute("DROP TABLE input_groups")
    await conn.execute("ALTER TABLE scoping_drafts DROP COLUMN limits_json")
    await conn.execute("ALTER TABLE scoping_calls DROP COLUMN elapsed_seconds")
    await conn.execute("DELETE FROM schema_migrations WHERE version=12")
    await conn.commit()
    await db.init_db(tmp_cfg)
    restored=await scoping.fetch(conn,d["id"])
    assert restored["messages"]==d["messages"] and restored["id"]==d["id"]
    async with conn.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall()==[]


def test_extraction_truncation_is_explicit(tmp_cfg):
    tmp_cfg.uploads.max_text_chars=1000
    result=inputs.parse_file(tmp_cfg,"large.txt",b"a"*2000)
    assert len(result["text_content"])==1000 and result["extraction_truncated"]
    assert ScopingLimits(budget_usd=.1,budget_tokens=1000,wall_clock_seconds=10).wall_clock_seconds==10


async def test_chunked_upload_body_rejected_before_framework_spooling(tmp_cfg):
    tmp_cfg.uploads.max_group_bytes=1024
    application=create_app(tmp_cfg)
    async def chunks():
        yield b"a"*(700*1024)
        yield b"b"*(700*1024)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url="http://test") as c:
        r=await c.post("/api/inputs/groups/inp_fake/files",content=chunks(),headers={"Content-Type":"multipart/form-data; boundary=test"})
    assert r.status_code==413
    assert not (tmp_cfg.data_dir/"inputs").exists()


async def test_mixed_invalid_batch_is_atomic_and_file_size_is_bounded(client,tmp_cfg):
    gid=await group(client)
    r=await client.post(f"/api/inputs/groups/{gid}/files",files=[("files",("good.txt",b"ok")),("files",("bad.svg",b"<svg/>"))])
    assert r.status_code==415
    assert (await client.get(f"/api/inputs/groups/{gid}")).json()["files"]==[]
    tmp_cfg.uploads.max_file_bytes=32
    assert (await upload(client,gid,"large.txt",b"x"*33)).status_code==413


async def test_image_tamper_is_rejected_before_model_call(client,app,tmp_cfg,conn):
    tmp_cfg.scoping.allow_live_calls=True
    tmp_cfg.uploads.vision_enabled=True
    d=(await client.post("/api/scoping",json={})).json()
    gid=await group(client,d)
    await upload(client,gid,"image.png",png())
    async with conn.execute("SELECT path FROM input_files WHERE group_id=?",(gid,)) as cur:
        path=(await cur.fetchone())["path"]
    (tmp_cfg.data_dir/path).write_bytes(b"tampered")
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    factory=AsyncMock()
    app.state.scoping_provider_factory=factory
    assert (await turn(client,d,"Inspect image")).status_code==422
    factory.assert_not_called()


async def test_image_limit_marks_only_images_actually_sent(client,app,tmp_cfg):
    tmp_cfg.scoping.allow_live_calls=True
    tmp_cfg.uploads.vision_enabled=True
    seen=[]
    class Spy(MockScopingProvider):
        async def call(self,spec,ctx,**kwargs):
            seen.append(spec)
            return await super().call(spec,ctx,**kwargs)
    app.state.scoping_provider_factory=lambda *_:Spy()
    d=(await client.post("/api/scoping",json={})).json()
    gid=await group(client,d)
    for i in range(5):
        out=io.BytesIO()
        Image.new("RGB",(5,5),(i,0,0)).save(out,format="PNG")
        assert (await upload(client,gid,f"{i}.png",out.getvalue())).status_code==200
    d=(await client.get(f"/api/scoping/{d['id']}")).json()
    r=await turn(client,d,"Inspect images")
    assert r.status_code==200,r.text
    spec=seen[-1]
    assert len(spec.input_images)==4
    materials=json.loads(spec.user_blocks[0].text)["attachments"]
    assert sum(x["image_content_available"] for x in materials)==4


async def test_studio_preserves_confirmed_attachment_links(client,conn):
    gid=await group(client)
    file=(await upload(client,gid)).json()["files"][0]
    r=await client.post("/sessions/new",data={"goal":"Study files","upload_group_id":gid},headers={"Accept":"application/json"})
    assert r.status_code==201
    payload=(await client.get(f"/api/studio/sessions/{r.json()['session_id']}")).json()
    assert payload["attachments"][0]["id"]==file["id"]
    assert "path" not in payload["attachments"][0]
